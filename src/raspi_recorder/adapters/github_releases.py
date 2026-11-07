"""Bounded GitHub Releases API client for a configured, fixed repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from raspi_recorder.domain.updates import Release, ReleaseAsset
from raspi_recorder.ports.updates import ReleaseSource

_OWNER_OR_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")
_API_VERSION = "2026-03-10"
_API_JSON_LIMIT = 2 * 1024 * 1024
_ASSET_SIZE_LIMIT = 512 * 1024 * 1024
_RELEASES_PER_PAGE = 100
_RELEASE_PAGE_LIMIT = 10


class GitHubReleaseError(RuntimeError):
    """A release request or asset failed bounded validation."""


def release_source_from_environment(
    environment: Mapping[str, str] | None = None,
) -> GitHubReleaseClient:
    """Use the product's official source; reject conflicting deployment settings."""
    values = os.environ if environment is None else environment
    owner = values.get("RASPI_RECORDER_UPDATE_OWNER", "").strip()
    repository = values.get("RASPI_RECORDER_UPDATE_REPOSITORY", "").strip()
    if not owner and not repository:
        owner, repository = "raspikoubou", "raspi-recorder"
    elif not owner or not repository:
        raise ValueError("both update owner and repository must be configured")
    if (owner, repository) != ("raspikoubou", "raspi-recorder"):
        raise ValueError("update source must be raspikoubou/raspi-recorder")
    return GitHubReleaseClient(owner, repository, timeout_seconds=4.0, max_pages=4)


def _is_github_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and (
                host in {"github.com", "api.github.com"}
                or host.endswith(".githubusercontent.com")
            )
        )
    except ValueError:
        return False


def _valid_owner_or_repository(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and ".." not in value
        and _OWNER_OR_REPOSITORY.fullmatch(value) is not None
    )


class _GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow GitHub's HTTPS asset redirects without accepting arbitrary hosts."""

    max_redirections = 5
    max_repeats = 2

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        if not _is_github_https_url(new_url):
            raise GitHubReleaseError(
                "GitHub asset redirect left the HTTPS host allowlist"
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


class GitHubReleaseClient(ReleaseSource):
    """Read stable release metadata and download assets from a fixed GitHub repo.

    The owner and repository must be selected by the product build or an explicit
    deployment configuration. The client never searches for or guesses a repository.
    """

    def __init__(
        self,
        owner: str,
        repository: str,
        *,
        timeout_seconds: float = 10.0,
        max_pages: int = _RELEASE_PAGE_LIMIT,
        opener: Any | None = None,
    ) -> None:
        if not _valid_owner_or_repository(owner):
            raise ValueError("GitHub owner is invalid")
        if not _valid_owner_or_repository(repository):
            raise ValueError("GitHub repository name is invalid")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if type(max_pages) is not int or not 1 <= max_pages <= _RELEASE_PAGE_LIMIT:
            raise ValueError("max_pages is outside the allowed range")
        self.owner = owner
        self.repository = repository
        self._timeout = float(timeout_seconds)
        self._max_pages = max_pages
        self._opener = opener or urllib.request.build_opener(_GitHubRedirectHandler())

    def list_releases(self) -> tuple[Release, ...]:
        """Fetch a bounded list of release pages from api.github.com."""
        releases: list[Release] = []
        seen_ids: set[int] = set()
        for page in range(1, self._max_pages + 1):
            payload = self._request_json(
                f"https://api.github.com/repos/{self.owner}/{self.repository}/releases"
                f"?per_page={_RELEASES_PER_PAGE}&page={page}"
            )
            if not isinstance(payload, list) or len(payload) > _RELEASES_PER_PAGE:
                raise GitHubReleaseError("GitHub release response has an invalid shape")
            for item in payload:
                release = self._parse_release(item)
                if release.release_id in seen_ids:
                    raise GitHubReleaseError("GitHub release response repeated an id")
                seen_ids.add(release.release_id)
                releases.append(release)
            if len(payload) < _RELEASES_PER_PAGE:
                return tuple(releases)
        raise GitHubReleaseError(
            "GitHub release list exceeds the configured page limit"
        )

    def download_asset(
        self,
        release_id: int,
        asset: ReleaseAsset,
        destination: Path,
        *,
        max_bytes: int = _ASSET_SIZE_LIMIT,
    ) -> str:
        """Download by release asset ID, verify size/digest, and remove partials."""
        if type(release_id) is not int or release_id < 1:
            raise ValueError("release_id must be a positive integer")
        if not isinstance(asset, ReleaseAsset):
            raise ValueError("asset must be a ReleaseAsset")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _ASSET_SIZE_LIMIT:
            raise ValueError("max_bytes is outside the allowed range")
        if asset.size <= 0 or asset.size > max_bytes:
            raise GitHubReleaseError(
                "GitHub release asset size is outside the allowed range"
            )
        destination = Path(destination)
        if destination.is_symlink() or destination.exists():
            raise GitHubReleaseError("asset destination already exists or is a symlink")
        if not destination.parent.is_dir():
            raise GitHubReleaseError("asset destination directory does not exist")

        url = (
            f"https://api.github.com/repos/{self.owner}/{self.repository}/releases/"
            f"assets/{asset.asset_id}"
        )
        request = urllib.request.Request(
            url,
            headers=self._headers("application/octet-stream"),
            method="GET",
        )
        digest = hashlib.sha256()
        total = 0
        created = False
        try:
            response = self._open(request)
            try:
                self._require_success(response, expected_host_allowlist=True)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError) as exc:
                        raise GitHubReleaseError(
                            "asset Content-Length is invalid"
                        ) from exc
                    if declared_length != asset.size:
                        raise GitHubReleaseError(
                            "asset Content-Length differs from release metadata"
                        )
                with destination.open("xb") as output:
                    created = True
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes or total > asset.size:
                            raise GitHubReleaseError(
                                "downloaded asset exceeds its declared size"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                if total != asset.size:
                    raise GitHubReleaseError("downloaded asset is incomplete")
            finally:
                response.close()
            hexdigest = digest.hexdigest()
            if asset.digest is not None and asset.digest[7:].lower() != hexdigest:
                raise GitHubReleaseError(
                    "GitHub asset digest does not match downloaded bytes"
                )
            return hexdigest
        except GitHubReleaseError:
            if created:
                destination.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if created:
                destination.unlink(missing_ok=True)
            raise GitHubReleaseError("GitHub release asset request failed") from exc
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

    def _request_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers=self._headers("application/vnd.github+json"),
            method="GET",
        )
        response = self._open(request)
        try:
            self._require_success(response, expected_host_allowlist=False)
            raw = response.read(_API_JSON_LIMIT + 1)
            if len(raw) > _API_JSON_LIMIT:
                raise GitHubReleaseError(
                    "GitHub release metadata exceeds the size limit"
                )
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubReleaseError(
                    "GitHub release metadata is not valid UTF-8 JSON"
                ) from exc
        finally:
            response.close()

    def _open(self, request: urllib.request.Request) -> Any:
        try:
            return self._opener.open(request, timeout=self._timeout)
        except GitHubReleaseError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubReleaseError("GitHub release request failed") from exc

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "User-Agent": "raspi-recorder-update-checker",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    @staticmethod
    def _require_success(response: Any, *, expected_host_allowlist: bool) -> None:
        status = getattr(response, "status", getattr(response, "code", None))
        if status != 200:
            raise GitHubReleaseError(
                "GitHub release request returned a non-success status"
            )
        final_url = response.geturl()
        if expected_host_allowlist:
            if not _is_github_https_url(final_url):
                raise GitHubReleaseError("GitHub asset response used an untrusted URL")
        else:
            parsed = urlsplit(final_url)
            if (
                not _is_github_https_url(final_url)
                or parsed.hostname != "api.github.com"
            ):
                raise GitHubReleaseError("GitHub API response used an unexpected URL")

    @classmethod
    def _parse_release(cls, value: Any) -> Release:
        if not isinstance(value, dict):
            raise GitHubReleaseError("GitHub release entry is not an object")
        try:
            assets_value = value["assets"]
            if not isinstance(assets_value, list) or len(assets_value) > 100:
                raise ValueError("assets list is invalid")
            assets = tuple(cls._parse_asset(asset) for asset in assets_value)
            if len({asset.asset_id for asset in assets}) != len(assets):
                raise ValueError("assets contain duplicate ids")
            name = value.get("name")
            body = value.get("body")
            return Release(
                release_id=value["id"],
                tag_name=value["tag_name"],
                name=name if isinstance(name, str) else value["tag_name"],
                body=body if isinstance(body, str) else "",
                draft=value["draft"],
                prerelease=value["prerelease"],
                published_at=value.get("published_at"),
                assets=assets,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubReleaseError("GitHub release entry failed validation") from exc

    @staticmethod
    def _parse_asset(value: Any) -> ReleaseAsset:
        if not isinstance(value, dict):
            raise ValueError("asset entry is not an object")
        digest = value.get("digest")
        if digest is not None and not isinstance(digest, str):
            raise ValueError("asset digest is invalid")
        return ReleaseAsset(
            asset_id=value["id"],
            name=value["name"],
            size=value["size"],
            state=value["state"],
            digest=digest,
        )

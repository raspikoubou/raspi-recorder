"""Stable update metadata and SemVer ordering without OS or network dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER = re.compile(
    r"^(?:v)?"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PROJECT_DEV = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.dev(0|[1-9][0-9]*)$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """Validated Semantic Version with SemVer 2.0 precedence."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        """Parse a SemVer tag, optionally prefixed with `v`."""
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(
                "version tag must be a non-empty string of at most 128 characters"
            )
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise ValueError("version tag is not valid SemVer")
        major, minor, patch, prerelease_text, build_text = match.groups()
        prerelease: list[int | str] = []
        if prerelease_text:
            for identifier in prerelease_text.split("."):
                if identifier.isdigit():
                    if len(identifier) > 1 and identifier.startswith("0"):
                        raise ValueError(
                            "numeric prerelease identifier has a leading zero"
                        )
                    prerelease.append(int(identifier))
                else:
                    prerelease.append(identifier)
        build = tuple(build_text.split(".")) if build_text else ()
        return cls(int(major), int(minor), int(patch), tuple(prerelease), build)

    @classmethod
    def from_project_version(cls, value: str) -> SemanticVersion:
        """Parse a stable project version or its `X.Y.Z.devN` PEP 440 form."""
        try:
            return cls.parse(value)
        except ValueError:
            match = _PROJECT_DEV.fullmatch(value) if isinstance(value, str) else None
            if match is None:
                raise ValueError(
                    "project version is neither SemVer nor X.Y.Z.devN"
                ) from None
            major, minor, patch, development = match.groups()
            return cls(
                int(major),
                int(minor),
                int(patch),
                ("dev", int(development)),
            )

    @property
    def is_prerelease(self) -> bool:
        """Return whether this is a prerelease rather than a stable version."""
        return bool(self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (
            other.major,
            other.minor,
            other.patch,
        ):
            return (self.major, self.minor, self.patch) < (
                other.major,
                other.minor,
                other.patch,
            )
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            if isinstance(left, int) and isinstance(right, int):
                return left < right
            if isinstance(left, int):
                return True
            if isinstance(right, int):
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease,
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease,
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(str(item) for item in self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """Small, validated subset of a GitHub release asset record."""

    asset_id: int
    name: str
    size: int
    state: str
    digest: str | None

    def __post_init__(self) -> None:
        if type(self.asset_id) is not int or self.asset_id < 1:
            raise ValueError("asset id must be a positive integer")
        if not isinstance(self.name, str) or not self.name or len(self.name) > 255:
            raise ValueError(
                "asset name must be a non-empty string of at most 255 characters"
            )
        if type(self.size) is not int or self.size < 0:
            raise ValueError("asset size must be a non-negative integer")
        if self.state != "uploaded":
            raise ValueError("release asset is not uploaded")
        if self.digest is not None and not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", self.digest
        ):
            raise ValueError("asset digest must use the sha256:<hex> format")


@dataclass(frozen=True, slots=True)
class Release:
    """Release metadata needed for stable candidate selection."""

    release_id: int
    tag_name: str
    name: str
    body: str
    draft: bool
    prerelease: bool
    published_at: str | None
    assets: tuple[ReleaseAsset, ...]

    def __post_init__(self) -> None:
        if type(self.release_id) is not int or self.release_id < 1:
            raise ValueError("release id must be a positive integer")
        if not isinstance(self.tag_name, str) or not self.tag_name:
            raise ValueError("release tag is required")
        if not isinstance(self.name, str) or not isinstance(self.body, str):
            raise ValueError("release name/body must be strings")
        if type(self.draft) is not bool or type(self.prerelease) is not bool:
            raise ValueError("release draft/prerelease flags must be booleans")
        if self.published_at is not None and not isinstance(self.published_at, str):
            raise ValueError("published_at must be a string or None")
        if not isinstance(self.assets, tuple) or any(
            not isinstance(asset, ReleaseAsset) for asset in self.assets
        ):
            raise ValueError("release assets must be a tuple of ReleaseAsset values")

    @property
    def version(self) -> SemanticVersion:
        """Return the SemVer encoded in the release tag."""
        return SemanticVersion.parse(self.tag_name)


def select_stable_update(
    releases: tuple[Release, ...], current_version: str
) -> Release | None:
    """Select the highest newer stable release; reject ambiguous duplicate tags."""
    if not isinstance(releases, tuple) or any(
        not isinstance(release, Release) for release in releases
    ):
        raise ValueError("releases must be a tuple of Release values")
    current = SemanticVersion.from_project_version(current_version)
    candidates: list[tuple[SemanticVersion, Release]] = []
    for release in releases:
        if release.draft or release.prerelease:
            continue
        version = release.version
        if version.is_prerelease or version <= current:
            continue
        candidates.append((version, release))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    highest = candidates[-1][0]
    matching = [release for version, release in candidates if version == highest]
    if len(matching) != 1:
        raise ValueError("multiple published releases use the same SemVer version")
    return matching[0]

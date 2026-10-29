"""Application use cases for checking and staging checksum-verified releases."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from raspi_recorder.domain.updates import (
    Release,
    ReleaseAsset,
    SemanticVersion,
    select_stable_update,
)
from raspi_recorder.ports.updates import (
    ReleaseSource,
    UpdateBundleValidator,
    ValidatedUpdateBundle,
)

_MAX_ASSET_BYTES = 512 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 64 * 1024
_MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
_UPDATE_ID = re.compile(r"^[0-9a-f]{32}$")
_MINIMUM_FREE_BYTES = 512 * 1024 * 1024
_CHECKSUM_LINE = re.compile(
    r"^([0-9a-f]{64})  (raspi-recorder-[0-9]+\.[0-9]+\.[0-9]+\.tar\.zst)$"
)


class UpdateServiceError(RuntimeError):
    """A check or package preparation step could not safely continue."""


class UpdateSourceUnconfigured(UpdateServiceError):
    """No fixed official release repository is configured."""


@dataclass(frozen=True, slots=True)
class UpdateCandidate:
    """Selected stable release and its exact downloadable asset."""

    release_id: int
    version: str
    release_name: str
    notes: str
    published_at: str | None
    asset_id: int
    asset_name: str
    asset_size: int
    asset_digest: str | None
    available_bytes: int
    install_available_bytes: int
    privileged_state_available_bytes: int
    required_bytes: int

    @property
    def enough_space(self) -> bool:
        """Return whether the current filesystem has the minimum free space."""
        return (
            min(
                self.available_bytes,
                self.install_available_bytes,
                self.privileged_state_available_bytes,
            )
            >= self.required_bytes
        )

    def to_payload(self) -> dict[str, object]:
        """Return the fixed public update-status shape."""
        return {
            "version": self.version,
            "release_name": self.release_name,
            "notes": self.notes,
            "published_at": self.published_at,
            "asset_size_bytes": self.asset_size,
            "asset_digest_available": self.asset_digest is not None,
            "free_bytes": self.available_bytes,
            "install_free_bytes": self.install_available_bytes,
            "privileged_state_free_bytes": self.privileged_state_available_bytes,
            "required_bytes": self.required_bytes,
            "enough_space": self.enough_space,
        }


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """A downloaded, verified package ready for the restricted root helper."""

    candidate: UpdateCandidate
    bundle: ValidatedUpdateBundle
    operation_id: str
    package_path: Path
    archive_path: Path
    archive_sha256: str
    required_bytes_after_download: int
    available_bytes_after_download: int


class UpdateService:
    """Use one fixed repository to select and stage stable Pi-targeted releases."""

    def __init__(
        self,
        *,
        current_version: str,
        source: ReleaseSource | None,
        data_directory: Path,
        install_directory: Path | None = None,
        privileged_state_directory: Path | None = None,
        bundle_validator: UpdateBundleValidator | None = None,
    ) -> None:
        self._current_version = current_version
        SemanticVersion.from_project_version(current_version)
        self._source = source
        self._data_directory = Path(data_directory)
        self._install_directory = (
            self._data_directory
            if install_directory is None
            else Path(install_directory)
        )
        self._privileged_state_directory = (
            self._data_directory
            if privileged_state_directory is None
            else Path(privileged_state_directory)
        )
        self._bundle_validator = bundle_validator
        self._candidate: UpdateCandidate | None = None

    @property
    def source_configured(self) -> bool:
        """Whether an explicit release repository has been configured."""
        return self._source is not None

    def status(self) -> dict[str, object]:
        """Read cached update metadata without making a network request."""
        return {
            "current_version": self._current_version,
            "source_configured": self.source_configured,
            "candidate": (
                None if self._candidate is None else self._candidate.to_payload()
            ),
            "message": (
                "更新元未設定" if self._source is None else "更新確認を実行してください"
            ),
        }

    def check(self) -> dict[str, object]:
        """Select the newest stable release and expose disk preconditions."""
        release = self._select_candidate()
        if release is None:
            self._candidate = None
            return {
                **self.status(),
                "candidate": None,
                "message": "利用可能な安定版はありません",
            }
        candidate, _asset, _checksums = self._candidate_and_asset(release)
        self._candidate = candidate
        return {
            **self.status(),
            "candidate": candidate.to_payload(),
            "message": (
                "更新候補があります"
                if candidate.enough_space
                else "更新に必要な空き容量が不足しています"
            ),
        }

    def prepare(self, *, version: str, operation_id: str) -> PreparedUpdate:
        """Recheck a selected version, download by asset ID, and validate it."""
        if self._source is None:
            raise UpdateSourceUnconfigured("update_source_unconfigured")
        if not isinstance(operation_id, str) or not _UPDATE_ID.fullmatch(operation_id):
            raise ValueError("operation_id must be a 32-character lowercase hex ID")
        release = self._select_candidate()
        if release is None:
            raise UpdateServiceError("no_stable_update_available")
        candidate, asset, checksums_asset = self._candidate_and_asset(release)
        if candidate.version != version:
            raise UpdateServiceError("selected_update_changed")
        if not candidate.enough_space:
            raise UpdateServiceError("update_disk_space_insufficient")

        staging_root = self._safe_staging_root()
        operation_root = staging_root / operation_id
        try:
            operation_root.mkdir(mode=0o700)
        except OSError as exc:
            raise UpdateServiceError("update_operation_directory_unavailable") from exc

        archive = operation_root / "release.tar.zst"
        checksums_path = operation_root / "SHA256SUMS"
        extracted = operation_root / "package"
        try:
            checksums_digest = self._source.download_asset(
                release.release_id,
                checksums_asset,
                checksums_path,
                max_bytes=_MAX_CHECKSUM_BYTES,
            )
            if not isinstance(checksums_digest, str) or not re.fullmatch(
                r"[0-9a-f]{64}", checksums_digest
            ):
                raise UpdateServiceError("downloaded_checksum_digest_invalid")
            if (
                checksums_asset.digest is not None
                and checksums_digest != checksums_asset.digest[7:].lower()
            ):
                raise UpdateServiceError("downloaded_checksum_digest_mismatch")
            downloaded_digest = self._source.download_asset(
                release.release_id,
                asset,
                archive,
                max_bytes=_MAX_ASSET_BYTES,
            )
            if not isinstance(downloaded_digest, str) or not re.fullmatch(
                r"[0-9a-f]{64}", downloaded_digest
            ):
                raise UpdateServiceError("downloaded_asset_digest_invalid")
            if (
                asset.digest is not None
                and downloaded_digest != asset.digest[7:].lower()
            ):
                raise UpdateServiceError("downloaded_asset_digest_mismatch")
            verify_release_checksum(checksums_path, asset.name, downloaded_digest)
            if self._bundle_validator is None:
                raise UpdateServiceError("update_bundle_validator_unavailable")
            bundle = self._bundle_validator.stage(
                archive,
                expected_version=candidate.version,
                destination=extracted,
            )
            required = _required_bytes_after_download(
                archive_bytes=asset.size + checksums_asset.size,
                bundle=bundle,
            )
            try:
                available = shutil.disk_usage(self._data_directory).free
                install_available = shutil.disk_usage(self._install_directory).free
                privileged_state_available = shutil.disk_usage(
                    self._privileged_state_directory
                ).free
            except OSError as exc:
                raise UpdateServiceError("update_disk_space_unavailable") from exc
            if min(available, install_available, privileged_state_available) < required:
                raise UpdateServiceError("update_disk_space_insufficient")
            prepared = PreparedUpdate(
                candidate=candidate,
                bundle=bundle,
                operation_id=operation_id,
                package_path=bundle.package_root,
                archive_path=archive,
                archive_sha256=downloaded_digest,
                required_bytes_after_download=required,
                available_bytes_after_download=min(
                    available, install_available, privileged_state_available
                ),
            )
            self._candidate = candidate
            return prepared
        except BaseException:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise

    def cleanup(self, operation_id: str) -> None:
        """Remove only one task-owned staging directory after apply or rollback."""
        if not isinstance(operation_id, str) or not _UPDATE_ID.fullmatch(operation_id):
            raise ValueError("operation_id must be a 32-character lowercase hex ID")
        root = self._safe_staging_root()
        target = root / operation_id
        if target.is_symlink():
            raise UpdateServiceError("update_staging_path_is_symlink")
        if target.exists():
            shutil.rmtree(target)

    def _select_candidate(self) -> Release | None:
        if self._source is None:
            raise UpdateSourceUnconfigured("update_source_unconfigured")
        releases = self._source.list_releases()
        try:
            return select_stable_update(releases, self._current_version)
        except ValueError as exc:
            raise UpdateServiceError("release_selection_failed") from exc

    def _candidate_and_asset(
        self, release: Release
    ) -> tuple[UpdateCandidate, ReleaseAsset, ReleaseAsset]:
        version = str(release.version)
        asset, checksums_asset = release_package_assets(release)
        try:
            available = shutil.disk_usage(self._data_directory).free
            install_available = shutil.disk_usage(self._install_directory).free
            privileged_state_available = shutil.disk_usage(
                self._privileged_state_directory
            ).free
        except OSError as exc:
            raise UpdateServiceError("update_disk_space_unavailable") from exc
        required = (
            asset.size
            + checksums_asset.size
            + 5 * _MAX_UNPACKED_BYTES
            + _MINIMUM_FREE_BYTES
        )
        candidate = UpdateCandidate(
            release_id=release.release_id,
            version=version,
            release_name=release.name,
            notes=release.body[:20_000],
            published_at=release.published_at,
            asset_id=asset.asset_id,
            asset_name=asset.name,
            asset_size=asset.size,
            asset_digest=asset.digest,
            available_bytes=available,
            install_available_bytes=install_available,
            privileged_state_available_bytes=privileged_state_available,
            required_bytes=required,
        )
        return candidate, asset, checksums_asset

    def _safe_staging_root(self) -> Path:
        try:
            base = self._data_directory.resolve(strict=True)
        except OSError as exc:
            raise UpdateServiceError("update_data_directory_unavailable") from exc
        if self._data_directory.is_symlink() or not base.is_dir():
            raise UpdateServiceError("update_data_directory_invalid")
        root = base / "update-staging"
        if root.is_symlink():
            raise UpdateServiceError("update_staging_path_is_symlink")
        try:
            root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise UpdateServiceError("update_staging_path_unavailable") from exc
        if root.is_symlink() or not root.is_dir():
            raise UpdateServiceError("update_staging_path_invalid")
        return root


def _required_bytes_after_download(
    *, archive_bytes: int, bundle: ValidatedUpdateBundle
) -> int:
    return archive_bytes + 3 * bundle.unpacked_bytes + _MINIMUM_FREE_BYTES


def release_package_assets(release: Release) -> tuple[ReleaseAsset, ReleaseAsset]:
    """Select the one bounded source archive and checksum asset in a Release."""
    expected_name = f"raspi-recorder-{release.version}.tar.zst"
    archives = [asset for asset in release.assets if asset.name == expected_name]
    checksums = [asset for asset in release.assets if asset.name == "SHA256SUMS"]
    if len(archives) != 1 or len(checksums) != 1:
        raise UpdateServiceError("release_package_asset_missing_or_ambiguous")
    archive, checksum = archives[0], checksums[0]
    if not 0 < archive.size <= _MAX_ASSET_BYTES:
        raise UpdateServiceError("release_package_asset_size_invalid")
    if not 0 < checksum.size <= _MAX_CHECKSUM_BYTES:
        raise UpdateServiceError("release_checksum_asset_size_invalid")
    return archive, checksum


def verify_release_checksum(path: Path, expected_name: str, actual_digest: str) -> None:
    """Require the same Release's SHA256SUMS to name and hash the selected asset."""
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_CHECKSUM_BYTES + 1)
        if len(data) > _MAX_CHECKSUM_BYTES:
            raise UpdateServiceError("release_checksum_asset_size_invalid")
        lines = data.decode("ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise UpdateServiceError("release_checksum_asset_invalid") from exc
    if len(lines) != 1:
        raise UpdateServiceError("release_checksum_asset_invalid")
    match = _CHECKSUM_LINE.fullmatch(lines[0])
    if match is None or match.group(2) != expected_name:
        raise UpdateServiceError("release_checksum_asset_invalid")
    if match.group(1) != actual_digest:
        raise UpdateServiceError("release_archive_checksum_mismatch")

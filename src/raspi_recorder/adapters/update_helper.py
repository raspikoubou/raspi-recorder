"""Restricted root helper for atomic release installation and rollback."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from raspi_recorder.adapters.command_process import run_isolated_command
from raspi_recorder.adapters.github_releases import GitHubReleaseClient
from raspi_recorder.adapters.settings_repository import SettingsRepository
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.adapters.update_bundle import (
    TarZstdUpdateBundleValidator,
    UpdateBundleError,
)
from raspi_recorder.application.settings_service import SettingsService
from raspi_recorder.application.update_service import (
    release_package_assets,
    verify_release_checksum,
)
from raspi_recorder.domain.operations import OperationSnapshot, OperationStage
from raspi_recorder.domain.updates import select_stable_update
from raspi_recorder.ports.updates import ReleaseSource

_MAX_REQUEST_BYTES = 4096
_MAX_RESPONSE_BYTES = 4096
_MAX_STATE_BYTES = 32 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_OLD_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.dev(0|[1-9][0-9]*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_NAMES = (
    "settings.sqlite3",
    "recordings.sqlite3",
    "authentication.sqlite3",
)
_TERMINAL_PHASES = {"succeeded", "rolled_back"}
_KNOWN_PHASES = _TERMINAL_PHASES | {
    "prepared",
    "installing_release",
    "release_installed",
    "stopping_services",
    "services_stopped",
    "backups_complete",
    "switching_current",
    "current_switched",
    "starting",
    "health_verified",
    "rollback_in_progress",
    "recovery_failed",
}


class UpdateHelperError(RuntimeError):
    """A constrained update operation failed or could not be recovered."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class UpdateHelperConfig:
    """Fixed privileged paths and bounded wait values."""

    opt_root: Path = Path("/opt/raspi-recorder")
    state_directory: Path = Path("/var/lib/raspi-recorder")
    privileged_state_directory: Path = Path("/var/lib/raspi-recorder-update")
    runtime_directory: Path = Path("/run/raspi-recorder-update")
    engine_socket: Path = Path("/run/raspi-recorder/engine.sock")
    systemctl: Path = Path("/usr/bin/systemctl")
    system_python: Path = Path("/usr/bin/python3.13")
    engine_unit: str = "raspi-recorder-engine.service"
    web_unit: str = "raspi-recorder-web.service"
    build_user: str = "raspi-recorder-update"
    build_group: str = "raspi-recorder-update"
    health_timeout_seconds: float = 75.0
    command_timeout_seconds: float = 90.0
    install_timeout_seconds: float = 1800.0


class UpdateHelper:
    """Perform only the fixed release install/restore operations."""

    def __init__(
        self,
        config: UpdateHelperConfig | None = None,
        *,
        require_root: bool = True,
        command_runner: Callable[[list[str], float], int] | None = None,
        service_command_runner: Callable[[list[str], float], int] | None = None,
        release_source: ReleaseSource | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        config = UpdateHelperConfig() if config is None else config
        self.config = config
        self._require_root = require_root
        self._command_runner = command_runner or self._run_command
        self._service_command_runner = (
            service_command_runner or self._run_as_update_user
        )
        self._release_source = (
            GitHubReleaseClient("raspikoubou", "raspi-recorder", timeout_seconds=4.0)
            if release_source is None
            else release_source
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._settings = SettingsService(
            SettingsRepository(config.state_directory / "settings.sqlite3"),
            SystemClock(),
        )
        self._journal: dict[str, Any] | None = None

    def serve_one(self, source: BinaryIO, destination: BinaryIO) -> int:
        """Read one bounded JSON request, acknowledge durably, then apply it."""
        try:
            request_line = source.readline(_MAX_REQUEST_BYTES + 1)
            request = self._decode_request(request_line)
            self._assert_privileged()
            with self._exclusive_lock():
                prepared = self._prepare(request)
                self._write_response(destination, {"ok": True, "accepted": True})
                self._apply_prepared(prepared)
            return 0
        except UpdateHelperError as exc:
            self._write_response(
                destination,
                {
                    "ok": False,
                    "error": exc.code,
                    "uncertain": exc.uncertain,
                },
            )
            return 1
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self._write_response(
                destination,
                {"ok": False, "error": type(exc).__name__},
            )
            return 1

    def recover(self) -> int:
        """Resolve any interrupted operation before recorder services start."""
        self._assert_privileged()
        with self._exclusive_lock():
            journal = self._read_journal()
            if journal is None:
                self._fail_orphaned_update_operations(None)
                return 0
            if journal["phase"] == "succeeded":
                if self._current_target() != f"releases/{journal['version']}":
                    self._mark_failure(
                        journal["operation_id"],
                        "successful_update_link_unexpected",
                        {"state": "manual_recovery_required"},
                    )
                    journal["phase"] = "recovery_failed"
                    self._write_journal(journal)
                    return 1
                self._mark_success(journal)
                self._cleanup_staging(journal["operation_id"], journal)
                self._fail_orphaned_update_operations(journal["operation_id"])
                return 0
            if journal["phase"] == "rolled_back":
                if self._current_target() != journal["old_target"]:
                    self._mark_failure(
                        journal["operation_id"],
                        "rolled_back_update_link_unexpected",
                        {"state": "manual_recovery_required"},
                    )
                    journal["phase"] = "recovery_failed"
                    self._write_journal(journal)
                    return 1
                self._mark_failure(
                    journal["operation_id"],
                    journal.get("failure_code", "update_interrupted"),
                    {
                        "state": "previous_version_restored",
                        "current_version": _version_from_target(journal["old_target"]),
                        "database_backup_restored": bool(
                            journal.get("backup_complete")
                        ),
                    },
                )
                self._cleanup_staging(
                    journal["operation_id"], journal, remove_release=True
                )
                self._fail_orphaned_update_operations(journal["operation_id"])
                return 0
            operation_id = journal["operation_id"]
            old_target = journal["old_target"]
            new_target = f"releases/{journal['version']}"
            current_target = self._current_target()
            if current_target not in {old_target, new_target}:
                self._mark_failure(
                    operation_id,
                    "current_link_unexpected",
                    {"state": "manual_recovery_required"},
                )
                journal["phase"] = "recovery_failed"
                self._write_journal(journal)
                return 1
            if self._is_active(self.config.engine_unit) or self._is_active(
                self.config.web_unit
            ):
                journal["phase"] = "recovery_failed"
                journal["rollback_failure"] = "recovery_services_unexpectedly_active"
                self._write_journal(journal)
                return 1
            if current_target == new_target:
                self._switch_current(old_target, operation_id)
            self._restore_database_backups(journal)
            journal["phase"] = "rolled_back"
            journal["recovery_reason"] = "power_loss_or_helper_interruption"
            self._write_journal(journal)
            self._mark_failure(
                operation_id,
                "update_interrupted",
                {
                    "state": "previous_version_restored",
                    "current_version": _version_from_target(old_target),
                    "database_backup_restored": bool(journal.get("backup_complete")),
                },
            )
            self._cleanup_staging(operation_id, journal, remove_release=True)
            self._fail_orphaned_update_operations(operation_id)
            return 0

    def _prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        self._validate_request_fields(request)
        operation_id = request["operation_id"]
        version = request["version"]
        asset_sha256 = request["asset_sha256"]
        self._prepare_previous_journal()
        old_target = self._current_target()
        old_version = _version_from_target(old_target)
        if version == old_version:
            raise UpdateHelperError("update_target_is_current")
        release_path = self.config.opt_root / "releases" / version
        if release_path.exists() or release_path.is_symlink():
            raise UpdateHelperError("update_release_version_already_exists")
        operation = self._load_operation(operation_id)
        if (
            operation is None
            or operation.kind != "update"
            or operation.stage is not OperationStage.VALIDATING
            or operation.target.get("version") != version
            or operation.target.get("asset_sha256") != asset_sha256
        ):
            raise UpdateHelperError("update_operation_mismatch")

        self._verify_release_origin(version, old_version, asset_sha256)
        archive = self._staging_archive(operation_id)
        protected_archive = (
            self.config.opt_root / f".update-archive-{operation_id}.tar.zst"
        )
        protected_stage = self.config.opt_root / f".update-package-{operation_id}"
        if (
            protected_archive.exists()
            or protected_archive.is_symlink()
            or protected_stage.exists()
            or protected_stage.is_symlink()
        ):
            raise UpdateHelperError("protected_staging_path_exists")
        try:
            self._copy_verified_archive(archive, protected_archive, asset_sha256)
        except BaseException:
            protected_archive.unlink(missing_ok=True)
            raise
        try:
            bundle = TarZstdUpdateBundleValidator().stage(
                protected_archive,
                expected_version=version,
                destination=protected_stage,
            )
        except (UpdateBundleError, OSError, ValueError) as exc:
            if not protected_stage.is_symlink():
                shutil.rmtree(protected_stage, ignore_errors=True)
            protected_archive.unlink(missing_ok=True)
            raise UpdateHelperError("release_bundle_validation_failed") from exc
        try:
            required_opt = (
                protected_archive.stat().st_size
                + 3 * bundle.unpacked_bytes
                + 512 * 1024 * 1024
            )
            required_state = sum(self._database_size(name) for name in _DATABASE_NAMES)
            required_state += 512 * 1024 * 1024
            if shutil.disk_usage(self.config.opt_root).free < required_opt:
                raise UpdateHelperError("update_opt_space_insufficient")
            if shutil.disk_usage(self.config.state_directory).free < required_state:
                raise UpdateHelperError("update_state_space_insufficient")
            if (
                shutil.disk_usage(self.config.privileged_state_directory).free
                < required_state
            ):
                raise UpdateHelperError("update_privileged_state_space_insufficient")
        except OSError as exc:
            if not protected_stage.is_symlink():
                shutil.rmtree(protected_stage, ignore_errors=True)
            protected_archive.unlink(missing_ok=True)
            raise UpdateHelperError("update_disk_space_unavailable") from exc
        except BaseException:
            if not protected_stage.is_symlink():
                shutil.rmtree(protected_stage, ignore_errors=True)
            protected_archive.unlink(missing_ok=True)
            raise
        try:
            self._advance_operation(operation_id, OperationStage.FINALIZING)
            journal = {
                "schema": 1,
                "operation_id": operation_id,
                "old_target": old_target,
                "version": version,
                "asset_sha256": asset_sha256,
                "phase": "prepared",
                "backup_complete": False,
                "install_started": False,
                "release_created": False,
                "install_stage": str(
                    self.config.opt_root / "releases" / f".update-{operation_id}"
                ),
                "created_utc": datetime.now(UTC).isoformat(),
            }
            self._write_journal(journal)
        except BaseException as exc:
            shutil.rmtree(protected_stage, ignore_errors=True)
            code = (
                exc.code if isinstance(exc, UpdateHelperError) else type(exc).__name__
            )
            self._mark_failure(
                operation_id,
                code,
                {
                    "state": "previous_release_unchanged",
                    "current_version": old_version,
                },
            )
            raise
        return {
            **journal,
            "archive": str(archive),
            "package_path": str(bundle.package_root),
            "unpacked_bytes": bundle.unpacked_bytes,
        }

    def _apply_prepared(self, journal: dict[str, Any]) -> None:
        operation_id = journal["operation_id"]
        try:
            self._require_active(self.config.engine_unit)
            self._require_active(self.config.web_unit)
            journal["phase"] = "installing_release"
            journal["install_started"] = True
            self._write_journal(journal)
            self._install_release(Path(journal["package_path"]), journal)

            journal["phase"] = "stopping_services"
            self._write_journal(journal)
            self._stop_unit(self.config.engine_unit)
            self._stop_unit(self.config.web_unit)
            journal["phase"] = "services_stopped"
            self._write_journal(journal)

            # Keep the recording gate in every restorable database snapshot.
            self._advance_operation(operation_id, OperationStage.APPLYING)
            backup_directory = self._backup_databases(operation_id)
            journal["backup_directory"] = str(backup_directory)
            journal["backup_complete"] = True
            journal["phase"] = "backups_complete"
            self._write_journal(journal)

            journal["phase"] = "switching_current"
            self._write_journal(journal)
            self._switch_current(f"releases/{journal['version']}", operation_id)
            if self._current_target() != f"releases/{journal['version']}":
                raise UpdateHelperError("current_release_switch_failed")

            journal["phase"] = "current_switched"
            self._write_journal(journal)
            self._advance_operation(operation_id, OperationStage.STARTING)
            self._start_unit(self.config.engine_unit)
            self._start_unit(self.config.web_unit)
            journal["phase"] = "starting"
            self._write_journal(journal)
            if not self._wait_for_health(journal["version"]):
                raise UpdateHelperError("new_release_health_check_failed")

            journal["phase"] = "health_verified"
            self._write_journal(journal)
            journal["phase"] = "succeeded"
            self._write_journal(journal)
            self._mark_success(journal)
            self._cleanup_staging(operation_id, journal)
        except BaseException as exc:
            # The durable commit precedes publication to the engine. Once it
            # exists, recovery must finish publication, never restore the index
            # behind recordings the committed engine may already have written.
            if journal.get("phase") == "succeeded":
                raise
            self._rollback(journal, exc)

    def _rollback(self, journal: dict[str, Any], cause: BaseException) -> None:
        operation_id = journal["operation_id"]
        if journal.get("phase") in {
            "prepared",
            "installing_release",
            "release_installed",
        }:
            journal["phase"] = "rolled_back"
            journal["failure_code"] = (
                cause.code
                if isinstance(cause, UpdateHelperError)
                else type(cause).__name__
            )
            self._write_journal(journal)
            self._mark_failure(
                operation_id,
                journal["failure_code"],
                {
                    "state": "previous_version_preserved",
                    "current_version": _version_from_target(journal["old_target"]),
                },
            )
            self._cleanup_staging(operation_id, journal, remove_release=True)
            return
        journal["phase"] = "rollback_in_progress"
        journal["failure_code"] = (
            cause.code if isinstance(cause, UpdateHelperError) else type(cause).__name__
        )
        self._write_journal(journal)
        try:
            self._stop_if_active(self.config.web_unit)
            self._stop_if_active(self.config.engine_unit)
            current = self._current_target()
            new_target = f"releases/{journal['version']}"
            if current not in {journal["old_target"], new_target}:
                raise UpdateHelperError("current_link_unexpected")
            self._restore_database_backups(journal)
            if current == new_target:
                self._switch_current(journal["old_target"], operation_id)
            # Commit restoration before starting even an older engine that
            # does not implement the pending-update recording gate.
            journal["phase"] = "rolled_back"
            self._write_journal(journal)
            self._mark_failure(
                operation_id,
                journal["failure_code"],
                {
                    "state": "previous_version_restored",
                    "current_version": _version_from_target(journal["old_target"]),
                    "database_backup_restored": bool(journal.get("backup_complete")),
                },
            )
            self._start_unit(self.config.engine_unit)
            self._start_unit(self.config.web_unit)
            if not self._wait_for_health(_version_from_target(journal["old_target"])):
                raise UpdateHelperError("rollback_health_check_failed")
            self._cleanup_staging(operation_id, journal, remove_release=True)
        except BaseException as rollback_error:
            if journal.get("phase") == "rolled_back":
                journal["rollback_failure"] = (
                    rollback_error.code
                    if isinstance(rollback_error, UpdateHelperError)
                    else type(rollback_error).__name__
                )
                self._write_journal(journal)
                raise
            journal["phase"] = "recovery_failed"
            journal["rollback_failure"] = (
                rollback_error.code
                if isinstance(rollback_error, UpdateHelperError)
                else type(rollback_error).__name__
            )
            self._write_journal(journal)
            # Keep the pending operation's recording gate closed. Recovery can
            # still restore the backup; releasing it here would orphan media.

    def _backup_databases(self, operation_id: str) -> Path:
        backup_root = self.config.privileged_state_directory / "backups"
        if backup_root.is_symlink() or (
            backup_root.exists() and not backup_root.is_dir()
        ):
            raise UpdateHelperError("database_backup_path_is_symlink")
        backup_root.mkdir(mode=0o700, exist_ok=True)
        destination = backup_root / operation_id
        destination.mkdir(mode=0o700)
        try:
            for name in _DATABASE_NAMES:
                source = self.config.state_directory / name
                if source.is_symlink() or not source.is_file():
                    raise UpdateHelperError("database_backup_source_invalid")
                target = destination / name
                with (
                    closing(sqlite3.connect(source)) as source_db,
                    closing(sqlite3.connect(target)) as target_db,
                ):
                    source_db.backup(target_db)
                target.chmod(0o600)
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        _fsync_directory(destination)
        _fsync_directory(backup_root)
        return destination

    def _verify_release_origin(
        self, version: str, old_version: str, digest: str
    ) -> None:
        """Authenticate the archive independently of the unprivileged caller.

        A checksum supplied by the engine only pins bytes; it does not authorize
        installing code that future privileged helpers will execute.
        """
        try:
            release = select_stable_update(
                self._release_source.list_releases(), old_version
            )
            if release is None or str(release.version) != version:
                raise UpdateHelperError("official_update_version_changed")
            archive, checksums = release_package_assets(release)
            if archive.digest is not None and archive.digest[7:].lower() != digest:
                raise UpdateHelperError("official_update_digest_mismatch")
            with tempfile.TemporaryDirectory(
                prefix=".release-origin-", dir=self.config.privileged_state_directory
            ) as temporary:
                path = Path(temporary) / "SHA256SUMS"
                self._release_source.download_asset(
                    release.release_id, checksums, path, max_bytes=64 * 1024
                )
                verify_release_checksum(path, archive.name, digest)
        except UpdateHelperError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise UpdateHelperError("official_update_verification_failed") from exc

    def _install_release(self, package_root: Path, journal: dict[str, Any]) -> None:
        """Build the isolated venv without executing release code as root."""
        import grp
        import pwd

        operation_id = journal["operation_id"]
        version = journal["version"]
        release_root = self.config.opt_root / "releases"
        release_path = release_root / version
        stage = Path(journal["install_stage"])
        if (
            release_root.is_symlink()
            or not release_root.is_dir()
            or stage.parent != release_root
            or stage.name != f".update-{operation_id}"
            or stage.exists()
            or stage.is_symlink()
            or release_path.exists()
            or release_path.is_symlink()
            or package_root.is_symlink()
            or not package_root.is_dir()
        ):
            raise UpdateHelperError("release_install_path_invalid")
        try:
            uid = pwd.getpwnam(self.config.build_user).pw_uid
            gid = grp.getgrnam(self.config.build_group).gr_gid
            shutil.copytree(package_root, stage, symlinks=False)
            _set_tree_owner_and_modes(stage, uid, gid, writable=True)
            venv_python = stage / ".venv/bin/python"
            venv_recorder = stage / ".venv/bin/raspi-recorder"
            result = self._service_command_runner(
                [
                    str(self.config.system_python),
                    "-m",
                    "venv",
                    "--copies",
                    str(stage / ".venv"),
                ],
                self.config.install_timeout_seconds,
            )
            if result != 0:
                raise UpdateHelperError("release_venv_creation_failed")
            venv_lib64 = stage / ".venv/lib64"
            if venv_lib64.is_symlink():
                if os.readlink(venv_lib64) != "lib":
                    raise UpdateHelperError("release_venv_layout_invalid")
                venv_lib64.unlink()
            command = [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-input",
                "--no-binary=av,blinker,click,flask,gunicorn,"
                "itsdangerous,jinja2,markupsafe,pillow,werkzeug",
                "--index-url",
                "https://pypi.org/simple",
                str(stage),
            ]
            result = self._service_command_runner(
                command, self.config.install_timeout_seconds
            )
            if result != 0:
                raise UpdateHelperError("release_dependency_install_failed")
            for command in (
                [str(venv_recorder), "--version"],
                [
                    str(venv_python),
                    "-c",
                    "import av, flask, PIL",
                ],
            ):
                result = self._service_command_runner(
                    command, self.config.install_timeout_seconds
                )
                if result != 0:
                    raise UpdateHelperError("release_startup_validation_failed")
            _relocate_venv_paths(stage / ".venv", stage, release_path)
            _set_tree_owner_and_modes(stage, 0, 0, writable=False)
            os.rename(stage, release_path)
            _fsync_directory(release_root)
            journal["release_created"] = True
            journal["phase"] = "release_installed"
            self._write_journal(journal)
            for command in (
                [str(release_path / ".venv/bin/raspi-recorder"), "--version"],
                [
                    str(release_path / ".venv/bin/python"),
                    "-c",
                    "import av, flask, PIL",
                ],
            ):
                result = self._service_command_runner(
                    command, self.config.install_timeout_seconds
                )
                if result != 0:
                    raise UpdateHelperError("release_startup_validation_failed")
        except UpdateHelperError:
            raise
        except (OSError, KeyError, subprocess.TimeoutExpired) as exc:
            raise UpdateHelperError("release_install_failed") from exc

    def _run_as_update_user(self, arguments: list[str], timeout: float) -> int:
        """Run source builds and smoke commands as the dedicated unprivileged user."""
        import grp
        import pwd

        try:
            account = pwd.getpwnam(self.config.build_user)
            group = grp.getgrnam(self.config.build_group)
            return run_isolated_command(
                arguments,
                timeout,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8",
                    "HOME": "/nonexistent",
                    "PIP_CONFIG_FILE": os.devnull,
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONSAFEPATH": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                user=account.pw_uid,
                group=group.gr_gid,
                extra_groups=[],
            )
        except (OSError, KeyError, subprocess.TimeoutExpired) as exc:
            raise UpdateHelperError("release_install_command_failed") from exc

    def _restore_database_backups(self, journal: dict[str, Any]) -> None:
        if not journal.get("backup_complete"):
            return
        backup = Path(journal.get("backup_directory", ""))
        expected_parent = self.config.privileged_state_directory / "backups"
        if (
            backup.parent != expected_parent
            or backup.name != journal["operation_id"]
            or backup.is_symlink()
            or not backup.is_dir()
        ):
            raise UpdateHelperError("database_backup_path_invalid")
        try:
            import grp
            import pwd

            uid = pwd.getpwnam("raspi-recorder").pw_uid
            web_uid = pwd.getpwnam("raspi-recorder-web").pw_uid
            gid = grp.getgrnam("raspi-recorder").gr_gid
        except KeyError as exc:
            raise UpdateHelperError("recorder_service_account_missing") from exc
        for name in _DATABASE_NAMES:
            source = backup / name
            if source.is_symlink() or not source.is_file():
                raise UpdateHelperError("database_backup_file_missing")
            target = self.config.state_directory / name
            if target.is_symlink():
                raise UpdateHelperError("database_path_is_symlink")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{name}.restore-", dir=self.config.state_directory
            )
            temporary = Path(temporary_name)
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    source.open("rb") as input_file,
                ):
                    shutil.copyfileobj(input_file, output)
                    mode = 0o600 if name == "authentication.sqlite3" else 0o660
                    os.fchmod(output.fileno(), mode)
                    owner = web_uid if name == "authentication.sqlite3" else uid
                    os.fchown(output.fileno(), owner, gid)
                    output.flush()
                    os.fsync(output.fileno())
                for suffix in ("-wal", "-shm"):
                    (self.config.state_directory / f"{name}{suffix}").unlink(
                        missing_ok=True
                    )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        _fsync_directory(self.config.state_directory)

    def _advance_operation(
        self,
        operation_id: str,
        next_stage: OperationStage,
        *,
        error: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        operation = self._load_operation(operation_id)
        if operation is None:
            raise UpdateHelperError("update_operation_missing")
        if operation.stage is next_stage:
            return
        if operation.status in {"succeeded", "failed"}:
            raise UpdateHelperError("update_operation_already_finished")
        try:
            self._settings.transition_operation(
                operation_id,
                operation.stage,
                next_stage,
                error=error,
                recovery=recovery,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise UpdateHelperError("update_operation_transition_failed") from exc

    def _mark_failure(
        self, operation_id: str, code: str, recovery: dict[str, Any]
    ) -> None:
        operation = self._load_operation(operation_id)
        if operation is None or operation.status in {"succeeded", "failed"}:
            return
        self._advance_operation(
            operation_id,
            OperationStage.FAILED,
            error={"code": code[:120]},
            recovery=recovery,
        )

    def _mark_success(self, journal: dict[str, Any]) -> None:
        operation_id = journal["operation_id"]
        self._advance_operation(
            operation_id,
            OperationStage.SUCCEEDED,
            recovery={
                "state": "updated",
                "current_version": journal["version"],
                "database_backup_created": bool(journal.get("backup_complete")),
            },
        )

    def _fail_orphaned_update_operations(self, active_operation_id: str | None) -> None:
        """Fail interrupted update requests that never reached a root journal."""
        for operation in self._settings.load_pending_operations():
            if (
                operation.kind != "update"
                or operation.operation_id == active_operation_id
            ):
                continue
            self._mark_failure(
                operation.operation_id,
                "update_interrupted_before_recovery_journal",
                {
                    "state": "previous_version_preserved",
                    "current_version": _version_from_target(self._current_target()),
                },
            )
            if _OPERATION_ID.fullmatch(operation.operation_id):
                self._cleanup_staging(operation.operation_id, {})

    def _load_operation(self, operation_id: str) -> OperationSnapshot | None:
        try:
            return self._settings.load_operation(operation_id)
        except (OSError, RuntimeError):
            return None

    def _staging_archive(self, operation_id: str) -> Path:
        if not _OPERATION_ID.fullmatch(operation_id):
            raise UpdateHelperError("operation_id_invalid")
        root = self.config.state_directory / "update-staging"
        operation_root = root / operation_id
        for path in (self.config.state_directory, root, operation_root):
            if path.is_symlink() or not path.is_dir():
                raise UpdateHelperError("update_staging_path_invalid")
        archive = operation_root / "release.tar.zst"
        if archive.is_symlink() or not archive.is_file():
            raise UpdateHelperError("update_archive_missing")
        return archive

    def _copy_verified_archive(
        self, source: Path, destination: Path, expected_sha256: str
    ) -> None:
        """Pin untrusted staging bytes into root-owned storage before extraction."""
        source_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(source, source_flags)
        except OSError as exc:
            raise UpdateHelperError("update_archive_unavailable") from exc
        destination_fd = -1
        try:
            source_info = os.fstat(source_fd)
            if (
                not stat.S_ISREG(source_info.st_mode)
                or source_info.st_size <= 0
                or source_info.st_size > _MAX_ARCHIVE_BYTES
            ):
                raise UpdateHelperError("update_archive_invalid")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(source_fd, "rb") as input_file:
                source_fd = -1
                with os.fdopen(destination_fd, "wb") as output_file:
                    destination_fd = -1
                    while chunk := input_file.read(1024 * 1024):
                        total += len(chunk)
                        if total > _MAX_ARCHIVE_BYTES:
                            raise UpdateHelperError("update_archive_invalid")
                        digest.update(chunk)
                        output_file.write(chunk)
                    output_file.flush()
                    os.fsync(output_file.fileno())
            if digest.hexdigest() != expected_sha256:
                raise UpdateHelperError("downloaded_asset_digest_mismatch")
            _fsync_directory(destination.parent)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise UpdateHelperError("update_archive_copy_failed") from exc
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if destination_fd >= 0:
                os.close(destination_fd)

    def _database_size(self, name: str) -> int:
        path = self.config.state_directory / name
        if path.is_symlink() or not path.is_file():
            raise UpdateHelperError("database_backup_source_invalid")
        return path.stat().st_size

    def _current_target(self) -> str:
        current = self.config.opt_root / "current"
        release_root = self.config.opt_root / "releases"
        if (
            self.config.opt_root.is_symlink()
            or not self.config.opt_root.is_dir()
            or release_root.is_symlink()
            or not release_root.is_dir()
            or not current.is_symlink()
        ):
            raise UpdateHelperError("current_link_invalid")
        target = os.readlink(current)
        match = re.fullmatch(r"releases/([^/]+)", target)
        if match is None or _OLD_VERSION.fullmatch(match.group(1)) is None:
            raise UpdateHelperError("current_link_target_invalid")
        release = self.config.opt_root / target
        if release.is_symlink() or not release.is_dir():
            raise UpdateHelperError("current_release_missing")
        return target

    def _switch_current(self, target: str, operation_id: str) -> None:
        match = re.fullmatch(r"releases/([^/]+)", target)
        if match is None or _OLD_VERSION.fullmatch(match.group(1)) is None:
            raise UpdateHelperError("current_link_target_invalid")
        temporary = self.config.opt_root / f".current-{operation_id}"
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        os.symlink(target, temporary)
        os.replace(temporary, self.config.opt_root / "current")
        _fsync_directory(self.config.opt_root)

    def _wait_for_health(self, expected_version: str) -> bool:
        deadline = self._monotonic() + self.config.health_timeout_seconds
        while self._monotonic() < deadline:
            if (
                self._is_active(self.config.engine_unit)
                and self._is_active(self.config.web_unit)
                and self._engine_version() == expected_version
                and self._web_responds()
            ):
                return True
            self._sleep(1.0)
        return False

    def _engine_version(self) -> str | None:
        request = {
            "request_id": "update-health-check",
            "method": "GET",
            "path": "/api/v1/status",
            "body": None,
        }
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(self.config.engine_socket))
                client.sendall(payload)
                raw = _readline_limited(client, 1_048_576)
            response = json.loads(raw)
            body = response.get("body")
            if response.get("status_code") == 200 and isinstance(body, dict):
                version = body.get("current_version")
                return version if isinstance(version, str) else None
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return None

    def _web_responds(self) -> bool:
        request = urllib.request.Request(
            "http://127.0.0.1:8080/api/v1/auth/status",
            headers={"Host": "localhost"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                if type(response.status) is not int or response.status != 200:
                    return False
                payload = response.read(_MAX_STATE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        if len(payload) > _MAX_STATE_BYTES:
            return False
        try:
            body = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(body, dict):
            return False
        return (
            type(body.get("login_protection_enabled")) is bool
            and type(body.get("admin_protection_enabled")) is bool
            and type(body.get("login_password_configured")) is bool
            and type(body.get("admin_password_configured")) is bool
            and type(body.get("authenticated")) is bool
            and type(body.get("csrf_token")) is str
            and type(body.get("revision")) is int
        )

    def _require_active(self, unit: str) -> None:
        if not self._is_active(unit):
            raise UpdateHelperError("recorder_service_not_active")

    def _is_active(self, unit: str) -> bool:
        return (
            self._command_runner(
                [str(self.config.systemctl), "is-active", "--quiet", unit], 10.0
            )
            == 0
        )

    def _stop_unit(self, unit: str) -> None:
        result = self._command_runner(
            [str(self.config.systemctl), "stop", unit],
            self.config.command_timeout_seconds,
        )
        if result != 0:
            raise UpdateHelperError("service_stop_failed")
        # systemctl stop can return success even when the service's shutdown
        # fails. Do not switch releases after a failed or unknown stop state.
        failed_status = self._command_runner(
            [str(self.config.systemctl), "is-failed", "--quiet", unit], 10.0
        )
        if failed_status != 1:
            raise UpdateHelperError("service_stop_failed")

    def _stop_if_active(self, unit: str) -> None:
        if self._is_active(unit):
            self._stop_unit(unit)

    def _start_unit(self, unit: str) -> None:
        result = self._command_runner(
            [str(self.config.systemctl), "start", unit],
            self.config.command_timeout_seconds,
        )
        if result != 0:
            raise UpdateHelperError("service_start_failed")

    def _prepare_previous_journal(self) -> None:
        existing = self._read_journal()
        if existing is None:
            return
        if existing["phase"] not in _TERMINAL_PHASES:
            raise UpdateHelperError("update_recovery_required")
        history = self.config.privileged_state_directory / "history"
        if history.is_symlink():
            raise UpdateHelperError("update_history_path_invalid")
        history.mkdir(mode=0o700, exist_ok=True)
        destination = history / f"{existing['operation_id']}.json"
        if destination.exists() or destination.is_symlink():
            raise UpdateHelperError("update_history_entry_exists")
        os.replace(self._journal_path, destination)
        _fsync_directory(history)
        _fsync_directory(self.config.privileged_state_directory)

    @property
    def _journal_path(self) -> Path:
        return self.config.privileged_state_directory / "journal.json"

    def _read_journal(self) -> dict[str, Any] | None:
        path = self._journal_path
        if path.is_symlink():
            raise UpdateHelperError("update_journal_path_is_symlink")
        if not path.exists():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE_BYTES:
            raise UpdateHelperError("update_journal_invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateHelperError("update_journal_invalid") from exc
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise UpdateHelperError("update_journal_invalid")
        operation_id = value.get("operation_id")
        version = value.get("version")
        asset_sha256 = value.get("asset_sha256")
        phase = value.get("phase")
        old_target = value.get("old_target")
        if (
            not isinstance(operation_id, str)
            or _OPERATION_ID.fullmatch(operation_id) is None
            or not isinstance(version, str)
            or _VERSION.fullmatch(version) is None
            or not isinstance(asset_sha256, str)
            or _SHA256.fullmatch(asset_sha256) is None
            or not isinstance(phase, str)
            or phase not in _KNOWN_PHASES
            or not isinstance(old_target, str)
            or _version_from_target(old_target) is None
            or type(value.get("backup_complete")) is not bool
            or type(value.get("install_started")) is not bool
            or type(value.get("release_created")) is not bool
            or value.get("install_stage")
            != str(self.config.opt_root / "releases" / f".update-{operation_id}")
        ):
            raise UpdateHelperError("update_journal_invalid")
        if value.get("backup_complete"):
            expected_backup = (
                self.config.privileged_state_directory / "backups" / operation_id
            )
            if value.get("backup_directory") != str(expected_backup):
                raise UpdateHelperError("update_journal_invalid")
        elif "backup_directory" in value:
            raise UpdateHelperError("update_journal_invalid")
        return value

    def _write_journal(self, journal: dict[str, Any]) -> None:
        path = self._journal_path
        if path.is_symlink():
            raise UpdateHelperError("update_journal_path_is_symlink")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        encoded = json.dumps(
            journal, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            raise UpdateHelperError("update_journal_too_large")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".update-journal-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _cleanup_staging(
        self,
        operation_id: str,
        journal: dict[str, Any],
        *,
        remove_release: bool = False,
    ) -> None:
        operation_root = self.config.state_directory / "update-staging" / operation_id
        if not operation_root.is_symlink():
            shutil.rmtree(operation_root, ignore_errors=True)
        protected = self.config.opt_root / f".update-package-{operation_id}"
        if not protected.is_symlink():
            shutil.rmtree(protected, ignore_errors=True)
        protected_archive = (
            self.config.opt_root / f".update-archive-{operation_id}.tar.zst"
        )
        if not protected_archive.is_symlink():
            protected_archive.unlink(missing_ok=True)
        backup = self.config.privileged_state_directory / "backups" / operation_id
        if not backup.is_symlink():
            shutil.rmtree(backup, ignore_errors=True)
        install_stage = self.config.opt_root / "releases" / f".update-{operation_id}"
        if not install_stage.is_symlink():
            shutil.rmtree(install_stage, ignore_errors=True)
        if remove_release:
            candidate = self.config.opt_root / "releases" / journal.get("version", "")
            if (
                _VERSION.fullmatch(journal.get("version", ""))
                and candidate.is_dir()
                and not candidate.is_symlink()
                and self._current_target() != f"releases/{journal['version']}"
            ):
                shutil.rmtree(candidate, ignore_errors=True)

    def _exclusive_lock(self) -> Any:
        self.config.runtime_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        lock_path = self.config.runtime_directory / "update-helper.lock"
        if lock_path.is_symlink():
            raise UpdateHelperError("update_lock_path_is_symlink")
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise UpdateHelperError("update_busy") from exc
        return _LockedFile(descriptor)

    def _assert_privileged(self) -> None:
        if self._require_root and os.geteuid() != 0:
            raise UpdateHelperError("update_helper_requires_root")
        if self._require_root:
            root_state = self.config.privileged_state_directory
            try:
                info = root_state.lstat()
            except OSError as exc:
                raise UpdateHelperError("update_privileged_state_unavailable") from exc
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise UpdateHelperError("update_privileged_state_permissions_invalid")

    def _run_command(self, arguments: list[str], timeout: float) -> int:
        try:
            return run_isolated_command(
                arguments,
                timeout,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateHelperError("system_command_failed") from exc

    @staticmethod
    def _decode_request(line: bytes) -> dict[str, Any]:
        if not line.endswith(b"\n") or len(line) > _MAX_REQUEST_BYTES:
            raise UpdateHelperError("update_request_invalid")
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateHelperError("update_request_invalid") from exc
        if not isinstance(request, dict):
            raise UpdateHelperError("update_request_invalid")
        return request

    @staticmethod
    def _validate_request_fields(request: dict[str, Any]) -> None:
        if set(request) != {"action", "operation_id", "version", "asset_sha256"}:
            raise UpdateHelperError("update_request_invalid")
        if request["action"] != "apply":
            raise UpdateHelperError("update_action_invalid")
        if (
            not isinstance(request["operation_id"], str)
            or _OPERATION_ID.fullmatch(request["operation_id"]) is None
        ):
            raise UpdateHelperError("operation_id_invalid")
        if (
            not isinstance(request["version"], str)
            or _VERSION.fullmatch(request["version"]) is None
        ):
            raise UpdateHelperError("update_version_invalid")
        if (
            not isinstance(request["asset_sha256"], str)
            or _SHA256.fullmatch(request["asset_sha256"]) is None
        ):
            raise UpdateHelperError("update_asset_digest_invalid")

    @staticmethod
    def _write_response(destination: BinaryIO, response: dict[str, Any]) -> None:
        payload = (
            json.dumps(
                response, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > _MAX_RESPONSE_BYTES:
            payload = b'{"ok":false,"error":"update_response_too_large"}\n'
        try:
            destination.write(payload)
            destination.flush()
        except (BrokenPipeError, OSError):
            pass


def _relocate_venv_paths(venv: Path, old_root: Path, new_root: Path) -> None:
    """Replace only verified text metadata that embeds the temporary stage path."""
    old_bytes = old_root.as_posix().encode("utf-8")
    old_text = old_bytes.decode("utf-8")
    new_text = new_root.as_posix()
    cache_dirs = sorted(
        venv.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True
    )
    for cache in cache_dirs:
        if cache.is_symlink() or not cache.is_dir():
            raise UpdateHelperError("release_venv_layout_invalid")
        shutil.rmtree(cache)

    bin_directory = venv / "bin"
    if bin_directory.is_symlink() or not bin_directory.is_dir():
        raise UpdateHelperError("release_venv_layout_invalid")
    text_files = {venv / "pyvenv.cfg"}
    text_files.update(path for path in bin_directory.iterdir() if path.is_file())
    text_files.update(
        path
        for path in venv.rglob("*")
        if path.name == "direct_url.json" or path.suffix in {".cfg", ".json", ".pth"}
    )
    for path in text_files:
        try:
            info = path.lstat()
        except OSError as exc:
            raise UpdateHelperError("release_venv_layout_invalid") from exc
        if not stat.S_ISREG(info.st_mode):
            raise UpdateHelperError("release_venv_layout_invalid")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise UpdateHelperError("release_venv_layout_invalid") from exc
        if old_bytes not in content:
            continue
        try:
            relocated = (
                content.decode("utf-8").replace(old_text, new_text).encode("utf-8")
            )
        except UnicodeDecodeError as exc:
            raise UpdateHelperError("release_venv_path_in_binary_file") from exc
        try:
            path.write_bytes(relocated)
            path.chmod(stat.S_IMODE(info.st_mode))
        except OSError as exc:
            raise UpdateHelperError("release_venv_relocation_failed") from exc

    for path in text_files:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise UpdateHelperError("release_venv_layout_invalid") from exc
        if old_bytes in content:
            raise UpdateHelperError("release_venv_relocation_incomplete")


class _LockedFile:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def __enter__(self) -> _LockedFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)


def _readline_limited(client: socket.socket, maximum: int) -> bytes:
    response = bytearray()
    while len(response) <= maximum:
        chunk = client.recv(1)
        if not chunk:
            break
        response.extend(chunk)
        if chunk == b"\n":
            break
    if not response.endswith(b"\n") or len(response) > maximum:
        raise UpdateHelperError("engine_health_response_invalid")
    return bytes(response)


def _version_from_target(target: str) -> str:
    match = re.fullmatch(r"releases/([^/]+)", target)
    if match is None or _OLD_VERSION.fullmatch(match.group(1)) is None:
        raise UpdateHelperError("current_link_target_invalid")
    return match.group(1)


def _set_tree_owner_and_modes(
    root: Path, uid: int, gid: int, *, writable: bool
) -> None:
    """Set a regular-file-only release tree to builder or immutable ownership."""
    if root.is_symlink() or not root.is_dir():
        raise UpdateHelperError("release_tree_invalid")
    directories: list[Path] = []
    regular_files: list[Path] = []
    for current_name, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_name)
        directories.append(current)
        for name in tuple(directory_names):
            path = current / name
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UpdateHelperError("release_tree_contains_special_path")
        for name in file_names:
            path = current / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise UpdateHelperError("release_tree_contains_special_path")
            regular_files.append(path)
    directory_mode = 0o750 if writable else 0o755
    file_mode = 0o640 if writable else None
    for path in reversed(directories):
        os.chown(path, uid, gid, follow_symlinks=False)
        path.chmod(directory_mode)
    for path in regular_files:
        info = path.stat(follow_symlinks=False)
        mode = file_mode if writable else (0o755 if info.st_mode & 0o111 else 0o644)
        os.chown(path, uid, gid, follow_symlinks=False)
        assert mode is not None
        path.chmod(mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run_recovery() -> int:
    return UpdateHelper().recover()


_ALLOWED_PEER_USER = "raspi-recorder"


def _assert_peer_allowed() -> None:
    """socket activationの接続元がengine userまたはrootだけか確認する。

    socket fileのmodeはgroup単位の防御しかできず、groupを共有する
    raspi-recorder-web userからもroot helperへ接続できてしまうため、
    SO_PEERCREDで接続元uid自体を検証する。
    """
    import sys

    try:
        with socket.fromfd(
            sys.stdin.fileno(), socket.AF_UNIX, socket.SOCK_STREAM
        ) as connection:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("iII"),
            )
    except OSError as exc:
        raise UpdateHelperError("update_peer_check_failed") from exc
    _pid, uid, _gid = struct.unpack("iII", credentials)
    if uid == 0:
        return
    try:
        allowed_uid = pwd.getpwnam(_ALLOWED_PEER_USER).pw_uid
    except KeyError as exc:
        raise UpdateHelperError("update_peer_check_failed") from exc
    if uid != allowed_uid:
        raise UpdateHelperError("update_peer_not_authorized")


def main(argv: list[str] | None = None) -> int:
    if argv == ["recover"]:
        return _run_recovery()
    helper = UpdateHelper()
    import sys

    try:
        _assert_peer_allowed()
    except UpdateHelperError as exc:
        UpdateHelper._write_response(
            sys.stdout.buffer,
            {"ok": False, "error": exc.code, "uncertain": exc.uncertain},
        )
        return 1
    return helper.serve_one(sys.stdin.buffer, sys.stdout.buffer)

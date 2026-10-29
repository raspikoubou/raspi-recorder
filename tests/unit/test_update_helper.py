import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from raspi_recorder.adapters.settings_repository import SettingsRepository
from raspi_recorder.adapters.update_helper import (
    UpdateHelper,
    UpdateHelperConfig,
    UpdateHelperError,
)
from raspi_recorder.application.settings_service import SettingsService
from raspi_recorder.domain.operations import OperationStage
from raspi_recorder.domain.settings import ApplicationSettings
from tests.unit.test_update_bundle import VERSION, bundle_files, write_archive
from tests.unit.test_update_service import FakeReleaseSource, release_with_asset


class _Clock:
    def __init__(self) -> None:
        self.value = 100

    def utc_ns(self) -> int:
        self.value += 1
        return self.value

    def monotonic_ns(self) -> int:
        return self.value

    def boot_id(self) -> str:
        return "test-boot"


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.protected = self.root / "root-state"
        self.run = self.root / "run"
        self.opt = self.root / "opt"
        self.releases = self.opt / "releases"
        self.state.mkdir()
        self.protected.mkdir(mode=0o700)
        self.run.mkdir()
        self.releases.mkdir(parents=True)
        self.old_version = "0.1.0.dev0"
        old_release = self.releases / self.old_version
        (old_release / ".venv/bin").mkdir(parents=True)
        (old_release / ".venv/bin/raspi-recorder").write_text("#!/bin/sh\n")
        (old_release / ".venv/bin/raspi-recorder").chmod(0o755)
        (self.opt / "current").symlink_to(f"releases/{self.old_version}")

        self.settings = SettingsService(
            SettingsRepository(self.state / "settings.sqlite3"), _Clock()
        )
        self.settings.initialize(ApplicationSettings())
        for name in ("recordings.sqlite3", "authentication.sqlite3"):
            (self.state / name).touch()
        self.operation_id = "a" * 32
        self.archive_path = self._create_operation()
        self.digest = hashlib.sha256(self.archive_path.read_bytes()).hexdigest()
        trusted_archive = self.root / "official.tar.zst"
        trusted_archive.write_bytes(self.archive_path.read_bytes())
        self.official_release = release_with_asset(trusted_archive)
        self.release_source = FakeReleaseSource(
            trusted_archive, (self.official_release,)
        )
        self.config = UpdateHelperConfig(
            opt_root=self.opt,
            state_directory=self.state,
            privileged_state_directory=self.protected,
            runtime_directory=self.run,
            engine_socket=self.run / "engine.sock",
            systemctl=Path("/usr/bin/systemctl"),
            system_python=Path("/usr/bin/python3.13"),
            health_timeout_seconds=0.01,
            command_timeout_seconds=1,
            install_timeout_seconds=1,
        )
        self.active = {self.config.engine_unit, self.config.web_unit}
        self.commands = []
        self.helper = UpdateHelper(
            self.config,
            require_root=False,
            command_runner=self._system_command,
            service_command_runner=self._install_command,
            release_source=self.release_source,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _create_operation(self) -> Path:
        staging = self.state / "update-staging" / self.operation_id
        staging.mkdir(parents=True)
        (staging / "package").mkdir()
        archive = write_archive(staging / "release.tar.zst", bundle_files())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        operation = self.settings.create_operation(
            operation_id=self.operation_id,
            request_id="update-helper-test",
            kind="update",
            target={
                "scope": "update",
                "version": VERSION,
                "asset_sha256": digest,
            },
            previous_settings={},
            requested_settings={"version": VERSION, "confirmed": True},
        )
        self.settings.transition_operation(
            operation.operation_id,
            OperationStage.QUEUED,
            OperationStage.VALIDATING,
        )
        return archive

    def _system_command(self, command, _timeout):
        self.commands.append(tuple(command))
        if command[1] == "is-active":
            return 0 if command[-1] in self.active else 3
        if command[1] == "stop":
            self.active.discard(command[-1])
            return 0
        if command[1] == "start":
            self.active.add(command[-1])
            return 0
        return 1

    def _install_command(self, command, _timeout):
        self.commands.append(tuple(command))
        if command[1:4] == ["-m", "venv", "--copies"]:
            environment = Path(command[-1])
            venv = environment / "bin"
            venv.mkdir(parents=True)
            (environment / "lib64").symlink_to("lib")
            (environment / "pyvenv.cfg").write_text(
                f"home = /usr/bin\ncommand = python -m venv {environment}\n",
                encoding="utf-8",
            )
            for name in ("python", "raspi-recorder"):
                executable = venv / name
                shebang = (
                    f"#!{environment}/bin/python\n"
                    if name == "raspi-recorder"
                    else "#!/bin/sh\n"
                )
                executable.write_text(shebang, encoding="utf-8")
                executable.chmod(0o755)
        return 0

    def _prepare(self):
        return self.helper._prepare(
            {
                "action": "apply",
                "operation_id": self.operation_id,
                "version": VERSION,
                "asset_sha256": self.digest,
            }
        )

    def _patch_release_account(self):
        return (
            patch(
                "pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=os.getuid()),
            ),
            patch(
                "grp.getgrnam",
                return_value=SimpleNamespace(gr_gid=os.getgid()),
            ),
            patch("raspi_recorder.adapters.update_helper._set_tree_owner_and_modes"),
        )

    def test_prepare_and_apply_switches_only_after_release_smoke_checks(self):
        prepared = self._prepare()
        self.assertEqual(prepared["phase"], "prepared")
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage,
            OperationStage.FINALIZING,
        )
        health_versions = []

        def healthy(version):
            health_versions.append(version)
            self.assertEqual(
                self.settings.load_operation(self.operation_id).stage,
                OperationStage.STARTING,
            )
            return True

        self.helper._wait_for_health = healthy
        account_patches = self._patch_release_account()
        with account_patches[0], account_patches[1], account_patches[2]:
            self.helper._apply_prepared(prepared)

        self.assertEqual(os.readlink(self.opt / "current"), f"releases/{VERSION}")
        self.assertFalse((self.releases / VERSION / ".venv/lib64").is_symlink())
        relocated_venv = self.releases / VERSION / ".venv"
        relocated_recorder = relocated_venv / "bin/raspi-recorder"
        self.assertEqual(
            relocated_recorder.read_text(encoding="utf-8").splitlines()[0],
            f"#!{relocated_venv}/bin/python",
        )
        self.assertNotIn(
            str(self.releases / f".update-{self.operation_id}"),
            "".join(
                path.read_text(encoding="utf-8")
                for path in (relocated_venv / "pyvenv.cfg", relocated_recorder)
            ),
        )
        self.assertEqual(
            self.settings.load_operation(self.operation_id).status, "succeeded"
        )
        self.assertEqual(health_versions, [VERSION])
        self.assertIn("pip", " ".join(" ".join(command) for command in self.commands))
        pip_command = next(command for command in self.commands if "pip" in command)
        self.assertIn(
            "--no-binary=av,blinker,click,flask,gunicorn,"
            "itsdangerous,jinja2,markupsafe,pillow,werkzeug",
            pip_command,
        )
        self.assertNotIn("--no-binary=:all:", pip_command)
        self.assertIn(
            (str(relocated_recorder), "--version"),
            self.commands,
        )
        self.assertFalse((self.state / "update-staging" / self.operation_id).exists())

    def test_failed_new_release_health_restores_previous_link_and_database(self):
        prepared = self._prepare()
        health_versions = []

        def healthy(version):
            health_versions.append(version)
            self.assertEqual(
                self.settings.load_operation(self.operation_id).stage,
                OperationStage.STARTING
                if version == VERSION
                else OperationStage.FAILED,
            )
            return version == self.old_version

        self.helper._wait_for_health = healthy
        account_patches = self._patch_release_account()
        with account_patches[0], account_patches[1], account_patches[2]:
            self.helper._apply_prepared(prepared)

        operation = self.settings.load_operation(self.operation_id)
        self.assertEqual(
            os.readlink(self.opt / "current"), f"releases/{self.old_version}"
        )
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.recovery["state"], "previous_version_restored")
        self.assertEqual(health_versions, [VERSION, self.old_version])
        self.assertEqual(self.active, {self.config.engine_unit, self.config.web_unit})
        self.assertFalse((self.releases / VERSION).exists())

    def test_failed_service_shutdown_prevents_switch_and_restores_services(self):
        prepared = self._prepare()
        self.helper._wait_for_health = lambda version: version == self.old_version

        def failed_shutdown(command, timeout):
            if command[1] == "is-failed" and command[-1] == self.config.engine_unit:
                return 0
            return self._system_command(command, timeout)

        self.helper._command_runner = failed_shutdown
        account_patches = self._patch_release_account()
        with account_patches[0], account_patches[1], account_patches[2]:
            self.helper._apply_prepared(prepared)

        operation = self.settings.load_operation(self.operation_id)
        self.assertEqual(
            os.readlink(self.opt / "current"), f"releases/{self.old_version}"
        )
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.error["code"], "service_stop_failed")
        self.assertEqual(operation.recovery["state"], "previous_version_restored")
        self.assertEqual(self.active, {self.config.engine_unit, self.config.web_unit})
        self.assertFalse((self.releases / VERSION).exists())

    def test_unknown_service_shutdown_state_is_not_accepted(self):
        def unavailable_status(command, timeout):
            if command[1] == "is-failed":
                return 4
            return self._system_command(command, timeout)

        self.helper._command_runner = unavailable_status
        with self.assertRaisesRegex(UpdateHelperError, "service_stop_failed"):
            self.helper._stop_unit(self.config.engine_unit)

    def test_success_publication_failure_keeps_commit_and_recovery_retries(self):
        prepared = self._prepare()
        self.helper._wait_for_health = lambda _version: True
        account_patches = self._patch_release_account()
        transition = self.helper._settings.transition_operation

        def unavailable(operation_id, expected_stage, next_stage, **kwargs):
            if next_stage is OperationStage.SUCCEEDED:
                raise RuntimeError("database unavailable")
            return transition(operation_id, expected_stage, next_stage, **kwargs)

        with (
            account_patches[0],
            account_patches[1],
            account_patches[2],
            patch.object(
                self.helper._settings, "transition_operation", side_effect=unavailable
            ),
            self.assertRaisesRegex(
                UpdateHelperError, "update_operation_transition_failed"
            ),
        ):
            self.helper._apply_prepared(prepared)
        self.assertEqual(self.helper._read_journal()["phase"], "succeeded")
        self.assertEqual(os.readlink(self.opt / "current"), f"releases/{VERSION}")
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage,
            OperationStage.STARTING,
        )
        self.assertEqual(self.helper.recover(), 0)
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage,
            OperationStage.SUCCEEDED,
        )

    def test_cleanup_failure_after_success_does_not_restore_database(self):
        self._assert_cleanup_failure_preserves_committed_index(success=True)

    def test_cleanup_failure_after_rollback_does_not_restore_database(self):
        self._assert_cleanup_failure_preserves_committed_index(success=False)

    def _assert_cleanup_failure_preserves_committed_index(self, *, success):
        prepared = self._prepare()
        self.helper._wait_for_health = lambda version: (
            success or version == self.old_version
        )
        account_patches = self._patch_release_account()
        with (
            account_patches[0],
            account_patches[1],
            account_patches[2],
            patch.object(
                self.helper, "_cleanup_staging", side_effect=OSError("cleanup")
            ),
            self.assertRaisesRegex(OSError, "cleanup"),
        ):
            self.helper._apply_prepared(prepared)
        expected_phase = "succeeded" if success else "rolled_back"
        self.assertEqual(self.helper._read_journal()["phase"], expected_phase)
        # A recording indexed after publication must survive recovery.
        with sqlite3.connect(self.state / "recordings.sqlite3") as database:
            database.execute("CREATE TABLE sentinel(value TEXT)")
            database.execute("INSERT INTO sentinel VALUES ('new recording')")
        with patch.object(self.helper, "_restore_database_backups") as restore:
            self.assertEqual(self.helper.recover(), 0)
        restore.assert_not_called()
        with sqlite3.connect(self.state / "recordings.sqlite3") as database:
            self.assertEqual(
                database.execute("SELECT value FROM sentinel").fetchone(),
                ("new recording",),
            )

    def test_failed_rollback_restore_keeps_recording_gate_until_recovery(self):
        prepared = self._prepare()
        self.helper._wait_for_health = lambda _version: False
        account_patches = self._patch_release_account()
        with (
            account_patches[0],
            account_patches[1],
            account_patches[2],
            patch.object(
                self.helper, "_restore_database_backups", side_effect=OSError("restore")
            ),
        ):
            self.helper._apply_prepared(prepared)
        self.assertEqual(self.helper._read_journal()["phase"], "recovery_failed")
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage,
            OperationStage.STARTING,
        )
        # An attempted recovery while services are active must retain the gate.
        self.active.add(self.config.engine_unit)
        self.assertEqual(self.helper.recover(), 1)
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage,
            OperationStage.STARTING,
        )
        self.active.clear()
        account_patches = self._patch_release_account()
        with account_patches[0], account_patches[1]:
            self.assertEqual(self.helper.recover(), 0)
        self.assertEqual(
            self.settings.load_operation(self.operation_id).stage, OperationStage.FAILED
        )

    def test_failed_rollback_health_never_restores_index_behind_older_engine(self):
        prepared = self._prepare()

        def unhealthy(version):
            if version == self.old_version:
                with sqlite3.connect(self.state / "recordings.sqlite3") as database:
                    database.execute("CREATE TABLE older_engine_recording(id TEXT)")
                    database.execute(
                        "INSERT INTO older_engine_recording VALUES ('new')"
                    )
            return False

        self.helper._wait_for_health = unhealthy
        account_patches = self._patch_release_account()
        with (
            account_patches[0],
            account_patches[1],
            account_patches[2],
            self.assertRaisesRegex(UpdateHelperError, "rollback_health_check_failed"),
        ):
            self.helper._apply_prepared(prepared)
        journal = self.helper._read_journal()
        self.assertEqual(journal["phase"], "rolled_back")
        self.assertEqual(journal["rollback_failure"], "rollback_health_check_failed")
        with patch.object(self.helper, "_restore_database_backups") as restore:
            self.assertEqual(self.helper.recover(), 0)
        restore.assert_not_called()
        with sqlite3.connect(self.state / "recordings.sqlite3") as database:
            self.assertEqual(
                database.execute("SELECT id FROM older_engine_recording").fetchone(),
                ("new",),
            )

    def test_caller_digest_cannot_authorize_a_forged_release(self):
        write_archive(
            self.archive_path, bundle_files(payload=b"forged privileged code")
        )
        self.digest = hashlib.sha256(self.archive_path.read_bytes()).hexdigest()
        # A compromised engine controls its operation DB and its staging files.
        with sqlite3.connect(self.state / "settings.sqlite3") as connection:
            connection.execute(
                "UPDATE operations SET target_json=? WHERE operation_id=?",
                (
                    json.dumps(
                        {
                            "scope": "update",
                            "version": VERSION,
                            "asset_sha256": self.digest,
                        }
                    ),
                    self.operation_id,
                ),
            )
        with self.assertRaisesRegex(
            UpdateHelperError, "official_update_digest_mismatch"
        ):
            self._prepare()
        self.assertEqual(
            os.readlink(self.opt / "current"), f"releases/{self.old_version}"
        )
        self.assertEqual(self.commands, [])
        self.assertFalse((self.opt / f".update-package-{self.operation_id}").exists())

    def test_origin_verification_uses_trusted_checksums_when_api_digest_absent(self):
        self.release_source.releases = (
            replace(
                self.official_release,
                assets=tuple(
                    replace(asset, digest=None)
                    for asset in self.official_release.assets
                ),
            ),
        )
        self.assertEqual(self._prepare()["phase"], "prepared")
        self.assertEqual(len(self.release_source.download_calls), 1)

    def test_origin_verification_fails_closed_for_changed_or_unavailable_release(self):
        for releases in ((), (replace(self.official_release, draft=True),)):
            with self.subTest(releases=releases):
                self.release_source.releases = releases
                with self.assertRaisesRegex(
                    UpdateHelperError, "official_update_version_changed"
                ):
                    self._prepare()
        with (
            patch.object(
                self.release_source,
                "list_releases",
                side_effect=RuntimeError("offline"),
            ),
            self.assertRaisesRegex(
                UpdateHelperError, "official_update_verification_failed"
            ),
        ):
            self._prepare()
        self.assertEqual(self.active, {self.config.engine_unit, self.config.web_unit})

    def test_restore_preserves_authentication_permissions_and_ignores_planted_link(
        self,
    ):
        journal = self._prepare()
        journal["backup_directory"] = str(
            self.helper._backup_databases(self.operation_id)
        )
        journal["backup_complete"] = True
        sentinel = self.root / "outside-state"
        sentinel.write_bytes(b"must remain unchanged")
        planted = self.state / f".authentication.sqlite3.{self.operation_id}.restore"
        planted.symlink_to(sentinel)
        account_patches = self._patch_release_account()
        with account_patches[0], account_patches[1]:
            self.helper._restore_database_backups(journal)
        self.assertEqual(sentinel.read_bytes(), b"must remain unchanged")
        self.assertTrue(planted.is_symlink())
        for name in (
            "settings.sqlite3",
            "recordings.sqlite3",
            "authentication.sqlite3",
        ):
            expected_mode = 0o600 if name == "authentication.sqlite3" else 0o660
            self.assertEqual((self.state / name).stat().st_mode & 0o777, expected_mode)
            self.assertEqual(
                (self.state / name).read_bytes(),
                (Path(journal["backup_directory"]) / name).read_bytes(),
            )

    def test_restore_assigns_authentication_to_web_and_other_databases_to_engine(self):
        journal = self._prepare()
        journal["backup_directory"] = str(
            self.helper._backup_databases(self.operation_id)
        )
        journal["backup_complete"] = True
        with (
            patch(
                "pwd.getpwnam",
                side_effect=lambda name: SimpleNamespace(
                    pw_uid={"raspi-recorder": 1001, "raspi-recorder-web": 1002}[name]
                ),
            ),
            patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=2001)),
            patch("raspi_recorder.adapters.update_helper.os.fchown") as chown,
        ):
            self.helper._restore_database_backups(journal)
        self.assertEqual(
            [call.args[1:] for call in chown.call_args_list],
            [(1001, 2001), (1001, 2001), (1002, 2001)],
        )

    def test_recover_interrupted_switch_reverts_before_services_start(self):
        prepared = self._prepare()
        new_release = self.releases / VERSION
        new_release.mkdir()
        prepared["phase"] = "current_switched"
        prepared["release_created"] = True
        self.helper._write_journal(prepared)
        self.helper._switch_current(f"releases/{VERSION}", self.operation_id)
        self.active.clear()

        self.assertEqual(self.helper.recover(), 0)

        self.assertEqual(
            os.readlink(self.opt / "current"), f"releases/{self.old_version}"
        )
        operation = self.settings.load_operation(self.operation_id)
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.recovery["state"], "previous_version_restored")
        self.assertFalse(new_release.exists())
        self.assertEqual(self.active, set())

    def test_invalid_helper_request_is_rejected_without_mutation(self):
        from io import BytesIO

        result = self.helper.serve_one(
            BytesIO(b'{"action":"run-command"}\n'), BytesIO()
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            os.readlink(self.opt / "current"), f"releases/{self.old_version}"
        )
        self.assertEqual(self.active, {self.config.engine_unit, self.config.web_unit})

    def test_journal_rejects_symlinks_and_unknown_phase(self):
        journal = {
            "schema": 1,
            "operation_id": self.operation_id,
            "version": VERSION,
            "asset_sha256": self.digest,
            "phase": "prepared",
            "old_target": f"releases/{self.old_version}",
            "backup_complete": False,
            "install_started": False,
            "release_created": False,
            "install_stage": str(self.releases / f".update-{self.operation_id}"),
        }
        self.helper._write_journal(journal)
        self.assertEqual(self.helper._read_journal()["phase"], "prepared")
        journal["phase"] = "run-arbitrary-command"
        self.helper._write_journal(journal)
        with self.assertRaisesRegex(UpdateHelperError, "update_journal_invalid"):
            self.helper._read_journal()
        journal_path = self.protected / "journal.json"
        journal_path.unlink()
        journal_path.symlink_to(self.state / "settings.sqlite3")
        with self.assertRaisesRegex(
            UpdateHelperError, "update_journal_path_is_symlink"
        ):
            self.helper._read_journal()

    def test_archive_digest_mismatch_removes_protected_copy(self):
        destination = self.opt / "bad-digest.tar.zst"

        with self.assertRaisesRegex(
            UpdateHelperError, "downloaded_asset_digest_mismatch"
        ):
            self.helper._copy_verified_archive(self.archive_path, destination, "0" * 64)

        self.assertFalse(destination.exists())

    def test_archive_copy_rejects_symlink_source(self):
        source = self.root / "archive-link.tar.zst"
        source.symlink_to(self.archive_path)
        destination = self.opt / "linked-archive.tar.zst"

        with self.assertRaisesRegex(UpdateHelperError, "update_archive_unavailable"):
            self.helper._copy_verified_archive(source, destination, self.digest)

        self.assertFalse(destination.exists())

    def test_privileged_state_rejects_group_access(self):
        self.protected.chmod(0o750)
        with (
            patch("raspi_recorder.adapters.update_helper.os.geteuid", return_value=0),
            self.assertRaisesRegex(
                UpdateHelperError, "update_privileged_state_permissions_invalid"
            ),
        ):
            UpdateHelper(self.config, require_root=True)._assert_privileged()

    def test_install_and_update_lock_rejects_a_second_holder(self):
        with self.helper._exclusive_lock():
            with self.assertRaisesRegex(UpdateHelperError, "update_busy"):
                self.helper._exclusive_lock()
        with self.helper._exclusive_lock():
            pass

    def test_source_build_ignores_caller_python_and_pip_configuration(self):
        with (
            patch.dict(
                os.environ,
                {
                    "PYTHONPATH": "/untrusted",
                    "PIP_EXTRA_INDEX_URL": "https://untrusted.invalid",
                },
            ),
            patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1001)),
            patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=2001)),
            patch(
                "raspi_recorder.adapters.update_helper.run_isolated_command",
                return_value=0,
            ) as run,
        ):
            self.assertEqual(
                self.helper._run_as_update_user(
                    ["/usr/bin/python3.13", "-m", "venv"], 10
                ),
                0,
            )
        options = run.call_args.kwargs
        self.assertEqual(
            (options["user"], options["group"], options["extra_groups"]),
            (1001, 2001, []),
        )
        environment = options["env"]
        self.assertEqual(environment["HOME"], "/nonexistent")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["PYTHONSAFEPATH"], "1")
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PIP_EXTRA_INDEX_URL", environment)


if __name__ == "__main__":
    unittest.main()

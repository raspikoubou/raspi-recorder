"""Acceptance setup errors must fail, while missing prerequisites may skip."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tests.acceptance import test_virtual_two_camera_engine as acceptance_tests
from tests.acceptance import virtual_two_camera as fixture
from tests.acceptance.run_pi_usb_two_camera import create_capacity_blocker


class AcceptanceEnvironmentTests(unittest.TestCase):
    def test_default_usb_fixture_never_uses_the_production_recordings_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                fixture.os,
                "statvfs",
                return_value=SimpleNamespace(
                    f_bavail=2_000_000_000, f_blocks=2_000_000_000, f_frsize=1
                ),
            ),
            patch.object(fixture.RecorderRuntime, "start"),
        ):
            root = Path(directory)
            production = root / "storage" / "raspi-recorder" / "recordings"
            production.mkdir(parents=True)
            sentinel = production / "existing.mp4"
            sentinel.write_bytes(b"existing production recording")
            volume = SimpleNamespace(
                kind=fixture.VolumeKind.USB,
                filesystem="ext4",
                read_only=False,
                mountpoint=root / "storage",
                persistent_id="isolated-usb",
            )
            with patch.object(
                fixture.LinuxVolumeScanner, "scan", return_value=(volume,)
            ):
                environment = fixture.create_acceptance_environment(
                    root / "fixture", mounted_volume=volume
                )
            try:
                self.assertNotEqual(environment.volume_root.parent, production.parent)
                self.assertTrue(
                    environment.volume_root.parent.name.startswith(
                        "raspi-recorder-acceptance-"
                    )
                )
                self.assertEqual(
                    sentinel.read_bytes(), b"existing production recording"
                )
            finally:
                environment.runtime.close()

    def test_capacity_filler_preserves_preexisting_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing"
            existing.write_bytes(b"must remain")
            link = root / "link"
            link.symlink_to(existing)
            for candidate in (existing, link):
                with self.subTest(path=candidate), self.assertRaises(FileExistsError):
                    create_capacity_blocker(candidate, 10)
                self.assertEqual(existing.read_bytes(), b"must remain")
            self.assertTrue(link.is_symlink())

    def test_capacity_allocation_failure_closes_and_removes_only_owned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned"
            opened = []

            def fail(descriptor, _offset, _size):
                opened.append(descriptor)
                raise OSError("allocation failed")

            with (
                patch.object(os, "posix_fallocate", side_effect=fail, create=True),
                self.assertRaisesRegex(OSError, "allocation failed"),
            ):
                create_capacity_blocker(path, 10)
            self.assertFalse(path.exists())
            with self.assertRaises(OSError):
                os.fstat(opened[0])

    def test_only_explicit_precondition_errors_skip_acceptance(self):
        for test in (
            acceptance_tests.test_engine_records_independent_cameras_and_serves_web_live_mp4_and_zip,
            acceptance_tests.test_real_and_virtual_camera_one_hour_continuous_recording_metrics,
        ):
            for error, expected in (
                (RuntimeError("engine startup failed"), RuntimeError),
                (
                    fixture.AcceptancePreconditionError("no camera"),
                    pytest.skip.Exception,
                ),
            ):
                with (
                    self.subTest(test=test.__name__, error=type(error).__name__),
                    patch.dict(
                        os.environ,
                        RASPI_RECORDER_ACCEPTANCE_REAL_CAMERA="1",
                        RASPI_RECORDER_ACCEPTANCE_LONG_RUN_SECONDS="3600",
                    ),
                    patch.object(pytest, "importorskip", return_value=Mock()),
                    patch.object(
                        fixture, "create_acceptance_environment", side_effect=error
                    ),
                    self.assertRaises(expected),
                ):
                    test()

    def test_setup_failure_closes_runtime_and_preserves_original_error(self):
        original_close = fixture.RecorderRuntime.close
        for stage in ("engine", "authentication", "web"):
            error = RuntimeError(f"{stage} failed")
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    fixture.os,
                    "statvfs",
                    return_value=SimpleNamespace(
                        f_bavail=2_000_000_000, f_blocks=2_000_000_000, f_frsize=1
                    ),
                ),
                patch.object(
                    fixture.RecorderRuntime,
                    "start",
                    side_effect=error if stage == "engine" else None,
                ),
                patch.object(
                    fixture.AuthenticationService,
                    "initialize",
                    side_effect=error if stage == "authentication" else None,
                ),
                patch.object(fixture, "create_app", side_effect=error),
                patch.object(
                    fixture.RecorderRuntime,
                    "close",
                    autospec=True,
                    side_effect=original_close,
                ) as close,
                self.assertRaises(RuntimeError) as raised,
            ):
                try:
                    fixture.create_acceptance_environment(Path(directory))
                finally:
                    close.assert_called_once()
            self.assertIs(raised.exception, error)

    def test_insufficient_disk_is_a_precondition_before_runtime_creation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                fixture.os,
                "statvfs",
                return_value=SimpleNamespace(f_bavail=1, f_blocks=1, f_frsize=1),
            ),
            patch.object(fixture, "RecorderRuntime") as runtime,
            self.assertRaises(fixture.AcceptancePreconditionError),
        ):
            try:
                fixture.create_acceptance_environment(Path(directory))
            finally:
                runtime.assert_not_called()

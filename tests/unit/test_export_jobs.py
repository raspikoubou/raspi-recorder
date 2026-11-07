from __future__ import annotations

import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from raspi_recorder.adapters.export_jobs import (
    _EXPORT_MAX_RETAINED_JOBS,
    ExportJobService,
)
from raspi_recorder.adapters.linux_storage import ManagedVolumeStore
from raspi_recorder.application.recording_access import RecordingAccessError
from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.domain.settings import ApplicationSettings
from tests.fakes import FakeClock


class ExportJobTests(unittest.TestCase):
    def test_active_and_retained_export_job_counts_are_bounded(self):
        clock = FakeClock()
        access = Mock()
        service = ExportJobService(
            access,
            Mock(),
            Mock(),
            clock,
            Mock(side_effect=AssertionError("capacity must be checked first")),
            ApplicationSettings,
        )
        service._jobs["active"] = SimpleNamespace(
            state="running",
            active_downloads=0,
            last_access_mono_ns=clock.monotonic_ns(),
        )

        with self.assertRaises(RecordingAccessError) as active_error:
            service.create(["recording-1"])
        self.assertEqual(str(active_error.exception), "export_worker_busy")

        service._jobs.clear()
        now = clock.monotonic_ns()
        for number in range(_EXPORT_MAX_RETAINED_JOBS):
            service._jobs[f"retained-{number}"] = SimpleNamespace(
                state="failed", active_downloads=0, last_access_mono_ns=now
            )

        with self.assertRaises(RecordingAccessError) as count_error:
            service.create(["recording-1"])
        self.assertEqual(str(count_error.exception), "export_job_limit")
        access.read_lease.assert_not_called()

    def test_creates_zip64_stream_and_removes_it_after_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recordings_root = root / "raspi-recorder" / "recordings"
            recordings_root.mkdir(parents=True)
            source_path = root / "capture.mp4"
            source_path.write_bytes(b"mp4-content" * 1000)
            source = source_path.open("rb")
            volume = SimpleNamespace(
                persistent_id="volume-one",
                mountpoint=root,
                filesystem="ext4",
                kind=VolumeKind.MICROSD,
                device_number=root.stat().st_dev,
                filesystem_bytes=100 * 1024**3,
            )
            recording = SimpleNamespace(
                recording_id="recording-identifier-1",
                volume_id="volume-identifier-1",
                relative_path="2026-10-16/capture.mp4",
            )
            lease = SimpleNamespace(
                file=source, recording=recording, close=source.close
            )
            access = Mock()
            access.read_lease.return_value = lease
            scanner = Mock()
            scanner.scan.return_value = (volume,)
            store = Mock(spec=ManagedVolumeStore)
            store.recordings_root.side_effect = lambda selected: (
                selected.mountpoint / "raspi-recorder" / "recordings"
            )
            service = ExportJobService(
                access,
                scanner,
                store,
                FakeClock(),
                lambda: (volume, recordings_root, "volume-id"),
                ApplicationSettings,
            )
            statfs = SimpleNamespace(f_bavail=100 * 1024**3, f_frsize=1)
            with patch(
                "raspi_recorder.adapters.export_jobs.os.statvfs",
                return_value=statfs,
            ):
                accepted = service.create([recording.recording_id])
                deadline = time.monotonic() + 5
                job = service.get(accepted["export_id"])
                while job is not None and job["state"] in {"queued", "running"}:
                    if time.monotonic() >= deadline:
                        self.fail("export job did not finish")
                    time.sleep(0.01)
                    job = service.get(accepted["export_id"])
                self.assertIsNotNone(job)
                self.assertEqual(job["state"], "succeeded")
                export = service._jobs[accepted["export_id"]].final_path
                with zipfile.ZipFile(export) as archive:
                    self.assertEqual(archive.testzip(), None)
                    entries = archive.namelist()
                    self.assertEqual(len(entries), 1)
                    self.assertTrue(entries[0].endswith("_capture.mp4"))
                    self.assertEqual(archive.read(entries[0]), b"mp4-content" * 1000)
                handle, size, close = service.open_download(accepted["export_id"])
                try:
                    self.assertEqual(size, export.stat().st_size)
                    self.assertTrue(handle.read().startswith(b"PK"))
                finally:
                    close()
                self.assertFalse(export.exists())
                self.assertIsNone(service.get(accepted["export_id"]))
            service.close()

    def test_duplicate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recordings_root = root / "raspi-recorder" / "recordings"
            recordings_root.mkdir(parents=True)
            volume = SimpleNamespace(
                persistent_id="volume-one",
                mountpoint=root,
                filesystem="ext4",
                kind=VolumeKind.MICROSD,
                device_number=root.stat().st_dev,
                filesystem_bytes=100 * 1024**3,
            )
            service = ExportJobService(
                Mock(),
                Mock(),
                Mock(),
                FakeClock(),
                lambda: (volume, recordings_root, "volume-id"),
                ApplicationSettings,
            )
            with self.assertRaises(ValueError):
                service.create(["same", "same"])

    def test_export_thread_start_failure_removes_reserved_job_and_partial_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recordings_root = root / "raspi-recorder" / "recordings"
            recordings_root.mkdir(parents=True)
            volume = SimpleNamespace(
                persistent_id="volume-one",
                mountpoint=root,
                filesystem="ext4",
                kind=VolumeKind.MICROSD,
                device_number=root.stat().st_dev,
                filesystem_bytes=100 * 1024**3,
            )
            source_path = root / "capture.mp4"
            source_path.write_bytes(b"mp4")
            source = source_path.open("rb")
            lease = SimpleNamespace(
                file=source,
                recording=SimpleNamespace(
                    recording_id="recording-1",
                    volume_id="volume-1",
                    relative_path="capture.mp4",
                ),
                close=Mock(side_effect=source.close),
            )
            access = Mock()
            access.read_lease.return_value = lease
            scanner = Mock()
            scanner.scan.return_value = (volume,)
            store = Mock(spec=ManagedVolumeStore)
            store.recordings_root.return_value = recordings_root
            service = ExportJobService(
                access,
                scanner,
                store,
                FakeClock(),
                lambda: (volume, recordings_root, "volume-id"),
                ApplicationSettings,
            )
            thread = Mock()
            thread.start.side_effect = RuntimeError("thread start failed")
            statfs = SimpleNamespace(f_bavail=100 * 1024**3, f_frsize=1)
            with (
                patch(
                    "raspi_recorder.adapters.export_jobs.os.statvfs",
                    return_value=statfs,
                ),
                patch(
                    "raspi_recorder.adapters.export_jobs.threading.Thread",
                    return_value=thread,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    service.create(["recording-1"])

            self.assertEqual(service._jobs, {})
            self.assertEqual(service._reservations, {})
            self.assertEqual(service._threads, set())
            self.assertFalse(tuple((recordings_root / ".exports").iterdir()))
            lease.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

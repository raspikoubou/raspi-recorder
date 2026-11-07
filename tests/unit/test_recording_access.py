from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from raspi_recorder.adapters.export_jobs import ExportJobService
from raspi_recorder.adapters.linux_storage import ManagedVolumeStore
from raspi_recorder.adapters.recording_catalog import RecordingCatalog
from raspi_recorder.application.recording_access import (
    RecordingAccessError,
    RecordingAccessService,
)
from raspi_recorder.domain.camera import RecordingMode
from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.domain.recordings import RecordingTimeSpan
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.ports.storage import MountedVolume
from tests.fakes import FakeClock


class FakeVolumeLocator:
    def __init__(self, volume: MountedVolume, root: Path) -> None:
        self.volume = volume
        self.root = root

    def resolve(self, volume_id: str):
        if volume_id != "volume-1":
            raise KeyError(volume_id)
        return self.volume, self.root


class RecordingAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        mount = Path(self.temp_dir.name) / "mount"
        mount.mkdir()
        self.volume = MountedVolume(
            persistent_id="test-uuid",
            device_path="/dev/test",
            mountpoint=mount,
            filesystem="ext4",
            kind=VolumeKind.USB,
            device_number=os.stat(mount).st_dev,
            filesystem_bytes=100_000_000_000,
            available_bytes=90_000_000_000,
            read_only=False,
        )
        self.volume_store = ManagedVolumeStore()
        self.root = self.volume_store.verify(self.volume)
        self.catalog = RecordingCatalog(Path(self.temp_dir.name) / "catalog.db")
        self.catalog.initialize()
        self.catalog.upsert_volume(
            volume_id="volume-1",
            persistent_id="test-uuid",
            kind=VolumeKind.USB,
            fs_type="ext4",
            marker_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "raspi-recorder:test-uuid")),
            connected=True,
            selected=True,
            last_seen_utc_ns=1,
        )
        self.clock = FakeClock()
        self.service = RecordingAccessService(
            self.catalog,
            FakeVolumeLocator(self.volume, self.root),
            self.volume_store,
            self.clock,
        )

    def test_read_lease_blocks_delete_and_deletes_manifest_after_release(self):
        relative_path = "continuous/cam1/2026/10/16/clip.mp4"
        self._add_finalized("recording-1", relative_path)
        directory = self.volume_store.ensure_managed_directory(
            self.volume, self.root, "continuous/cam1/2026/10/16"
        )
        media = directory / "clip.mp4"
        manifest = directory / "clip.mp4.recording.json"
        media.write_bytes(b"mp4")
        manifest.write_text("{}", encoding="utf-8")

        lease = self.service.read_lease("recording-1")
        self.assertEqual(lease.file.read(), b"mp4")
        preview = self.service.preview_delete(["recording-1"])
        blocked = self.service.delete_confirmed(
            preview.confirmation_id, ["recording-1"]
        )
        self.assertFalse(blocked[0].deleted)
        self.assertEqual(blocked[0].code, "recording_in_use")
        lease.close()

        preview = self.service.preview_delete(["recording-1"])
        deleted = self.service.delete_confirmed(
            preview.confirmation_id, ["recording-1"]
        )
        self.assertTrue(deleted[0].deleted)
        self.assertFalse(media.exists())
        self.assertFalse(manifest.exists())
        self.assertIsNone(self.catalog.get_recording("recording-1"))

    def test_recording_search_remains_available_during_download_lease(self):
        relative_path = "continuous/cam1/2026/10/16/clip.mp4"
        self._add_finalized("recording-search-download", relative_path)
        directory = self.volume_store.ensure_managed_directory(
            self.volume, self.root, "continuous/cam1/2026/10/16"
        )
        media = directory / "clip.mp4"
        media.write_bytes(b"mp4-content")

        with self.service.read_lease(
            "recording-search-download", purpose="download"
        ) as lease:
            results = self.catalog.list_recordings(
                camera_id=1,
                from_utc_ns=1_000,
                to_utc_ns=1_101,
            )
            self.assertEqual(
                [item.recording_id for item in results],
                ["recording-search-download"],
            )
            self.assertEqual(lease.file.read(), b"mp4-content")
            self.assertEqual(
                self.service.read_lease_count("recording-search-download"), 1
            )

        self.assertEqual(self.service.read_lease_count("recording-search-download"), 0)

    def test_abnormal_download_uses_partial_file_and_delete_removes_sidecar(self):
        relative_path = "continuous/cam1/2026/10/16/interrupted.mp4"
        self._add_writing("recording-2", relative_path)
        self.catalog.mark_recording_abnormal("recording-2", "worker_failed")
        directory = self.volume_store.ensure_managed_directory(
            self.volume, self.root, "continuous/cam1/2026/10/16"
        )
        partial = directory / "interrupted.mp4.partial"
        manifest = directory / "interrupted.mp4.recording.json"
        partial.write_bytes(b"partial-mp4")
        manifest.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(RecordingAccessError, "not_readable"):
            self.service.read_lease("recording-2", purpose="playback")
        with self.service.read_lease("recording-2", purpose="download") as lease:
            self.assertEqual(lease.relative_path, relative_path + ".partial")
            self.assertEqual(lease.file.read(), b"partial-mp4")

        try:
            export_lease = self.service.read_lease("recording-2", purpose="export")
        except RecordingAccessError as exc:
            self.assertEqual(str(exc), "recording_not_readable")
        else:
            export_lease.close()
            self.fail("an unfinalized partial recording must not enter a ZIP export")

        preview = self.service.preview_delete(["recording-2"])
        result = self.service.delete_confirmed(
            preview.confirmation_id, ["recording-2"]
        )[0]
        self.assertTrue(result.deleted)
        self.assertFalse(partial.exists())
        self.assertFalse(manifest.exists())

    def test_zip_export_rejects_mixed_or_abnormal_only_selection(self):
        finalized_path = "continuous/cam1/2026/10/16/finished.mp4"
        abnormal_path = "continuous/cam1/2026/10/16/interrupted.mp4"
        self._add_finalized("recording-finalized", finalized_path)
        self._add_writing("recording-abnormal", abnormal_path)
        self.catalog.mark_recording_abnormal("recording-abnormal", "worker_failed")
        finalized_directory = self.volume_store.ensure_managed_directory(
            self.volume, self.root, "continuous/cam1/2026/10/16"
        )
        finalized_directory.joinpath("finished.mp4").write_bytes(b"finalized")
        abnormal_directory = self.volume_store.ensure_managed_directory(
            self.volume, self.root, "continuous/cam1/2026/10/16"
        )
        abnormal_directory.joinpath("interrupted.mp4.partial").write_bytes(b"partial")

        export = ExportJobService(
            self.service,
            Mock(),
            self.volume_store,
            self.clock,
            lambda: (self.volume, self.root, "volume-1"),
            ApplicationSettings,
        )

        with self.assertRaisesRegex(RecordingAccessError, "recording_not_readable"):
            export.create(["recording-finalized", "recording-abnormal"])
        with self.assertRaisesRegex(RecordingAccessError, "recording_not_readable"):
            export.create(["recording-abnormal"])
        with self.assertRaisesRegex(ValueError, "1～100件"):
            export.create([])

        self.assertEqual(self.service.read_lease_count("recording-finalized"), 0)
        self.assertFalse((self.root / ".exports").exists())

    def test_delete_preview_count_is_bounded_and_expired_entries_are_pruned(self):
        self._add_finalized("recording-preview", "continuous/cam1/preview.mp4")
        with patch.object(RecordingAccessService, "_MAX_DELETE_PREVIEWS", 2):
            first = self.service.preview_delete(["recording-preview"])
            second = self.service.preview_delete(["recording-preview"])
            third = self.service.preview_delete(["recording-preview"])
            self.assertEqual(len(self.service._previews), 2)
            self.assertNotIn(first.confirmation_id, self.service._previews)
            self.assertIn(second.confirmation_id, self.service._previews)
            self.assertIn(third.confirmation_id, self.service._previews)
            with self.assertRaisesRegex(
                RecordingAccessError, "delete_confirmation_expired"
            ):
                self.service.delete_confirmed(
                    first.confirmation_id, ["recording-preview"]
                )

            self.clock.advance(121_000_000_000)
            fourth = self.service.preview_delete(["recording-preview"])
            self.assertEqual(list(self.service._previews), [fourth.confirmation_id])

    def _add_writing(self, recording_id: str, relative_path: str):
        self.catalog.create_recording(
            recording_id=recording_id,
            volume_id="volume-1",
            relative_path=relative_path,
            mode=RecordingMode.CONTINUOUS,
            camera_id=1,
            logical_event_id=None,
            part_index=1,
            boot_id="boot-1",
            writer_epoch="epoch-1",
            created_utc_ns=1_000,
            settings_snapshot={"width": 1280},
        )

    def _add_finalized(self, recording_id: str, relative_path: str):
        self._add_writing(recording_id, relative_path)
        span = RecordingTimeSpan(1, 2, 100, 200, 1_000, 1_100, "UTC")
        self.catalog.finalize_recording(
            recording_id,
            start_mono_ns=100,
            end_mono_ns=200,
            start_utc_ns=1_000,
            end_utc_ns=1_100,
            size_bytes=3,
            spans=(span,),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from raspi_recorder.adapters.catalogued_writer import (
    CataloguedSegmentWriter,
    CataloguedWriterFactory,
    ProgressReportingWriter,
)
from raspi_recorder.adapters.linux_storage import ManagedVolumeStore
from raspi_recorder.adapters.recording_catalog import RecordingCatalog
from raspi_recorder.domain.camera import (
    CameraSettings,
    OverlayCorner,
    RecordingMode,
)
from raspi_recorder.domain.capacity import RecordingFileState, VolumeKind
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.domain.recordings import (
    RecordingArtifact,
    RecordingProgress,
    RecordingTimeSpan,
)
from raspi_recorder.ports.storage import MountedVolume


class FakeMp4Writer(ProgressReportingWriter):
    def __init__(self, root: Path, file_name: str) -> None:
        self.final_path = root / file_name
        self.partial_path = root / f"{file_name}.partial"
        self.partial_path.write_bytes(b"")
        self.frames: list[FrameEnvelope] = []
        self.audio_blocks: list[AudioEnvelope] = []
        self._progress: RecordingProgress | None = None
        self.aborted: str | None = None

    def write(self, envelope: FrameEnvelope) -> None:
        self.frames.append(envelope)
        first, last = self.frames[0], self.frames[-1]
        span = RecordingTimeSpan(
            first.sequence,
            last.sequence,
            first.capture_mono_ns,
            last.capture_mono_ns,
            first.capture_utc_ns,
            last.capture_utc_ns,
            first.timezone,
        )
        self._progress = RecordingProgress(
            len(self.frames),
            first.sequence,
            last.sequence,
            first.capture_mono_ns,
            last.capture_mono_ns,
            first.capture_utc_ns,
            last.capture_utc_ns,
            len(self.frames) * 2,
            (span,),
        )
        self.partial_path.write_bytes(b"x" * (len(self.frames) * 2))

    def write_audio(self, envelope: AudioEnvelope) -> None:
        self.audio_blocks.append(envelope)

    def progress(self) -> RecordingProgress | None:
        return self._progress

    def finalize(self) -> RecordingArtifact:
        if not self.frames or self._progress is None:
            raise RuntimeError("empty")
        self.partial_path.replace(self.final_path)
        first, last = self.frames[0], self.frames[-1]
        return RecordingArtifact(
            str(self.final_path),
            len(self.frames),
            first.sequence,
            last.sequence,
            first.capture_mono_ns,
            last.capture_mono_ns,
            first.capture_utc_ns,
            last.capture_utc_ns,
            self.final_path.stat().st_size,
        )

    def abort(self, reason: str) -> None:
        self.aborted = reason


class CataloguedWriterTests(unittest.TestCase):
    def test_finalize_failure_records_stage_and_cause_without_exception_message(self):
        writer = Mock()
        writer.progress.return_value = None

        def fail():
            try:
                raise OSError("private filesystem path")
            except OSError as cause:
                raise RuntimeError("private detail") from cause

        writer.finalize.side_effect = fail
        index = Mock()
        segment = CataloguedSegmentWriter(
            writer=writer, index=index, progress_index=Mock(), recording_id="test"
        )
        with self.assertRaises(RuntimeError):
            segment.finalize()
        reason = writer.abort.call_args.args[0]
        self.assertEqual(
            reason, "writer_or_index_finalize_failed:media_writer:RuntimeError:OSError"
        )
        self.assertNotIn("private", reason)
        self.assertEqual(index.mark_recording_abnormal.call_args.args[1], reason)

    def test_first_frame_name_index_progress_and_final_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            mount = base / "mount"
            mount.mkdir()
            volume = MountedVolume(
                "uuid-1",
                "/dev/test",
                mount,
                "ext4",
                VolumeKind.USB,
                os.stat(mount).st_dev,
                10_000_000_000,
                9_000_000_000,
                False,
            )
            store = ManagedVolumeStore()
            root = store.verify(volume)
            index = RecordingCatalog(base / "recordings.db")
            index.initialize()
            index.upsert_volume(
                volume_id="volume-1",
                persistent_id=volume.persistent_id,
                kind=VolumeKind.USB,
                fs_type="ext4",
                marker_id="marker-1",
                connected=True,
                selected=True,
                last_seen_utc_ns=1,
            )
            local_start = datetime(2026, 10, 16, 3, 0, 0, tzinfo=UTC)
            frame = FrameEnvelope(
                sequence=1,
                capture_mono_ns=1_000,
                capture_utc_ns=int(local_start.timestamp()) * 1_000_000_000,
                timezone="Asia/Tokyo",
                generation=1,
                jpeg_bytes=b"jpeg",
            )
            writers: list[FakeMp4Writer] = []
            built_settings: list[CameraSettings] = []

            def build(path, name, settings, **_options):
                writer = FakeMp4Writer(Path(path), name)
                writers.append(writer)
                built_settings.append(settings)
                return writer

            factory = CataloguedWriterFactory(
                index=index,
                volume_store=store,
                volume=volume,
                recordings_root=root,
                volume_id="volume-1",
                settings=CameraSettings(),
                camera_id=1,
                mode=RecordingMode.CONTINUOUS,
                boot_id="boot-1",
                writer_epoch="epoch-1",
                writer_builder=build,
                identifier_factory=lambda: f"recording-{len(writers) + 1}",
            )
            writer = factory(1, frame)
            first_row = index.get_recording("recording-1")
            self.assertIsNotNone(first_row)
            self.assertEqual(first_row.state, RecordingFileState.WRITING)
            writer.write(frame)
            artifact = writer.finalize()

            final_row = index.get_recording("recording-1")
            self.assertIsNotNone(final_row)
            self.assertEqual(final_row.state, RecordingFileState.FINALIZED)
            self.assertEqual(final_row.start_utc_ns, frame.capture_utc_ns)
            self.assertEqual(final_row.spans[0].last_sequence, 1)
            self.assertTrue((root / final_row.relative_path).is_file())
            self.assertIn(
                "/2026/10/16/continuous_cam1_20261016_120000.mp4", artifact.path
            )

            collision_writer = factory(2, frame)
            collision_row = index.get_recording("recording-2")
            self.assertEqual(
                collision_row.relative_path.rsplit("/", 1)[-1],
                "continuous_cam1_20261016_120000_001.mp4",
            )
            collision_writer.abort("test_cleanup")

            next_settings = CameraSettings(
                overlay_enabled=False,
                overlay_corner=OverlayCorner.TOP_RIGHT,
            )
            factory.update_settings(next_settings)
            next_writer = factory(3, frame)
            next_row = index.get_recording("recording-3")
            self.assertIsNotNone(next_row)
            assert next_row is not None
            self.assertEqual(built_settings[2], next_settings)
            with sqlite3.connect(base / "recordings.db") as connection:
                snapshot_json = connection.execute(
                    "SELECT settings_snapshot_json FROM recordings "
                    "WHERE recording_id = ?",
                    ("recording-3",),
                ).fetchone()[0]
            snapshot = json.loads(snapshot_json)
            self.assertFalse(snapshot["overlay_enabled"])
            self.assertEqual(snapshot["overlay_corner"], "top_right")
            next_writer.abort("test_cleanup")

    def test_progress_is_coalesced_off_thread_and_drained_before_finalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            mount = base / "mount"
            mount.mkdir()
            volume = MountedVolume(
                "uuid-1",
                "/dev/test",
                mount,
                "ext4",
                VolumeKind.USB,
                os.stat(mount).st_dev,
                10_000_000_000,
                9_000_000_000,
                False,
            )
            store = ManagedVolumeStore()
            root = store.verify(volume)
            index = RecordingCatalog(base / "recordings.db")
            index.initialize()
            index.upsert_volume(
                volume_id="volume-1",
                persistent_id=volume.persistent_id,
                kind=VolumeKind.USB,
                fs_type="ext4",
                marker_id="marker-1",
                connected=True,
                selected=True,
                last_seen_utc_ns=1,
            )
            main_thread_id = threading.get_ident()
            progress_started = threading.Event()
            release_progress = threading.Event()
            order: list[tuple[str, int, int | None]] = []
            original_open = index.open_writer_session
            open_count = 0

            class PrimarySession:
                def __init__(self, delegate):
                    self._delegate = delegate

                def __getattr__(self, name):
                    return getattr(self._delegate, name)

                def finalize_recording(self, *args, **kwargs):
                    order.append(("finalize", threading.get_ident(), None))
                    return self._delegate.finalize_recording(*args, **kwargs)

            class DelayedProgressSession:
                def __init__(self, delegate):
                    self._delegate = delegate

                def __getattr__(self, name):
                    return getattr(self._delegate, name)

                def update_recording_progress(self, recording_id, **fields):
                    sequence = fields["spans"][-1].last_sequence
                    order.append(("progress-start", threading.get_ident(), sequence))
                    progress_started.set()
                    if not release_progress.wait(3):
                        raise TimeoutError("test did not release SQLite progress write")
                    result = self._delegate.update_recording_progress(
                        recording_id, **fields
                    )
                    order.append(("progress-saved", threading.get_ident(), sequence))
                    return result

                def close(self):
                    order.append(("progress-close", threading.get_ident(), None))
                    return self._delegate.close()

            def open_session():
                nonlocal open_count
                open_count += 1
                session = original_open()
                if open_count == 1:
                    return PrimarySession(session)
                return DelayedProgressSession(session)

            index.open_writer_session = open_session
            writers: list[FakeMp4Writer] = []

            def build(path, name, _settings, **_options):
                writer = FakeMp4Writer(Path(path), name)
                writers.append(writer)
                return writer

            factory = CataloguedWriterFactory(
                index=index,
                volume_store=store,
                volume=volume,
                recordings_root=root,
                volume_id="volume-1",
                settings=CameraSettings(),
                camera_id=1,
                mode=RecordingMode.CONTINUOUS,
                boot_id="boot-progress",
                writer_epoch="epoch-progress",
                writer_builder=build,
                identifier_factory=lambda: "recording-progress",
            )
            first_mono_ns = 1_000_000_000
            first_utc_ns = 1_800_000_000_000_000_000

            def frame(sequence: int) -> FrameEnvelope:
                mono_ns = first_mono_ns + (sequence - 1) * 5_000_000_000
                return FrameEnvelope(
                    sequence=sequence,
                    capture_mono_ns=mono_ns,
                    capture_utc_ns=first_utc_ns + mono_ns - first_mono_ns,
                    timezone="Asia/Tokyo",
                    generation=1,
                    jpeg_bytes=b"jpeg",
                )

            writer = factory(1, frame(1))
            try:
                writer.write(frame(1))
                self.assertTrue(progress_started.wait(2))
                for sequence in (2, 3, 4):
                    writer.write(frame(sequence))
                self.assertGreaterEqual(writer._progress_worker.coalesced_updates, 2)
                release_progress.set()
                writer.finalize()
            finally:
                release_progress.set()
                if not writer._terminal:
                    writer.abort("test_cleanup")

            progress_sequences = [
                entry[2] for entry in order if entry[0] == "progress-saved"
            ]
            self.assertEqual(progress_sequences, [1, 4])
            self.assertEqual(open_count, 2)
            self.assertTrue(
                all(
                    entry[1] != main_thread_id
                    for entry in order
                    if entry[0].startswith("progress-")
                )
            )
            self.assertLess(
                next(
                    i for i, entry in enumerate(order) if entry[0] == "progress-close"
                ),
                next(i for i, entry in enumerate(order) if entry[0] == "finalize"),
            )
            final_row = index.get_recording("recording-progress")
            self.assertIsNotNone(final_row)
            assert final_row is not None
            self.assertEqual(final_row.state, RecordingFileState.FINALIZED)
            self.assertEqual(final_row.spans[-1].last_sequence, 4)


if __name__ == "__main__":
    unittest.main()

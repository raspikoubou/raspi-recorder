import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.recording_catalog import (
    RecordingCatalog,
    RecordingTimeSpan,
)
from raspi_recorder.domain.camera import RecordingMode
from raspi_recorder.domain.capacity import RecordingFileState, VolumeKind


class RecordingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.catalog = RecordingCatalog(Path(self.temporary.name) / "state.sqlite3")
        self.catalog.initialize()
        self.catalog.upsert_volume(
            volume_id="vol-a",
            persistent_id="uuid-a",
            kind=VolumeKind.USB,
            fs_type="ext4",
            marker_id="marker-a",
            connected=True,
            selected=True,
            last_seen_utc_ns=10,
        )

    def test_recording_progress_finalize_and_time_range_search(self):
        self.catalog.create_recording(
            recording_id="rec-a",
            volume_id="vol-a",
            relative_path="recordings/continuous/cam1/2026/10/16/continuous_cam1_20261016_000000.mp4",
            mode=RecordingMode.CONTINUOUS,
            camera_id=1,
            logical_event_id=None,
            part_index=1,
            boot_id="boot-a",
            writer_epoch="epoch-a",
            created_utc_ns=2_000,
            settings_snapshot={"fps": 15},
        )
        span = RecordingTimeSpan(1, 4, 10, 40, 90, 120, "UTC")
        self.catalog.update_recording_progress(
            "rec-a",
            start_mono_ns=10,
            end_mono_ns=40,
            start_utc_ns=90,
            end_utc_ns=120,
            size_bytes=4_000,
            spans=[span],
        )
        self.assertEqual(
            self.catalog.get_recording("rec-a").state,
            RecordingFileState.WRITING,
        )

        self.catalog.finalize_recording(
            "rec-a",
            start_mono_ns=10,
            end_mono_ns=40,
            start_utc_ns=90,
            end_utc_ns=120,
            size_bytes=4_000,
            spans=[span],
        )
        result = self.catalog.list_recordings(
            camera_id=1,
            mode=RecordingMode.CONTINUOUS,
            from_utc_ns=100,
            to_utc_ns=101,
        )

        self.assertEqual([item.recording_id for item in result], ["rec-a"])
        self.assertEqual(result[0].state, RecordingFileState.FINALIZED)
        self.assertEqual(result[0].spans, (span,))
        self.assertEqual(result[0].duration_ns, 30)

    def test_writer_session_uses_one_connection_and_one_commit_per_update(self):
        opened = []
        statements = []
        open_connection = self.catalog._open_connection

        def track_connection():
            connection = open_connection()
            opened.append(connection)
            return connection

        with patch.object(self.catalog, "_open_connection", track_connection):
            session = self.catalog.open_writer_session()
            session._connection.set_trace_callback(statements.append)
            try:
                session.create_recording(**self._recording_fields("rec-session"))
                session.update_recording_progress(
                    "rec-session", **self._progress_fields(10, 20, 100, 110, 1_000)
                )
                session.update_recording_progress(
                    "rec-session", **self._progress_fields(20, 30, 110, 120, 2_000)
                )
                session.finalize_recording(
                    "rec-session",
                    start_mono_ns=10,
                    end_mono_ns=30,
                    start_utc_ns=100,
                    end_utc_ns=120,
                    size_bytes=2_000,
                    end_reason=None,
                    spans=(RecordingTimeSpan(1, 3, 10, 30, 100, 120, "UTC"),),
                )
            finally:
                session.close()

        self.assertEqual(len(opened), 1)
        self.assertEqual(
            sum(statement.upper() == "COMMIT" for statement in statements), 4
        )
        result = self.catalog.get_recording("rec-session")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.FINALIZED)
        self.assertEqual(result.spans[0].last_sequence, 3)

    def test_writer_session_rejects_cross_thread_use_and_close_is_idempotent(self):
        session = self.catalog.open_writer_session()
        errors = []

        def use_from_other_thread():
            try:
                session.close()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=use_from_other_thread)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "作成thread")
        session.close()
        session.close()

    def test_writer_sessions_serialize_concurrent_writes(self):
        original = self.catalog._create_recording_in_connection
        counter_lock = threading.Lock()
        active_writes = 0
        max_active_writes = 0
        start = threading.Barrier(3)
        errors = []

        def tracked_create(connection, **fields):
            nonlocal active_writes, max_active_writes
            with counter_lock:
                active_writes += 1
                max_active_writes = max(max_active_writes, active_writes)
            try:
                time.sleep(0.05)
                original(connection, **fields)
            finally:
                with counter_lock:
                    active_writes -= 1

        def create_recording(recording_id):
            try:
                session = self.catalog.open_writer_session()
                try:
                    start.wait(2)
                    session.create_recording(**self._recording_fields(recording_id))
                finally:
                    session.close()
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            self.catalog, "_create_recording_in_connection", tracked_create
        ):
            threads = [
                threading.Thread(target=create_recording, args=(recording_id,))
                for recording_id in ("rec-serialized-a", "rec-serialized-b")
            ]
            for thread in threads:
                thread.start()
            start.wait(2)
            for thread in threads:
                thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(max_active_writes, 1)
        for recording_id in ("rec-serialized-a", "rec-serialized-b"):
            self.assertIsNotNone(self.catalog.get_recording(recording_id))

    def test_progress_failure_rolls_back_span_and_recording_row_together(self):
        self.catalog.create_recording(**self._recording_fields("rec-progress"))
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        self.catalog.update_recording_progress(
            "rec-progress",
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )
        replace_spans = self.catalog._replace_spans

        def fail_after_span_write(connection, recording_id, spans):
            replace_spans(connection, recording_id, spans)
            raise OSError("injected post-span failure")

        with patch.object(
            self.catalog, "_replace_spans", side_effect=fail_after_span_write
        ):
            with self.assertRaisesRegex(OSError, "post-span"):
                self.catalog.update_recording_progress(
                    "rec-progress",
                    start_mono_ns=30,
                    end_mono_ns=40,
                    start_utc_ns=120,
                    end_utc_ns=130,
                    size_bytes=2_000,
                    spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
                )

        result = self.catalog.get_recording("rec-progress")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.WRITING)
        self.assertEqual(result.start_mono_ns, 10)
        self.assertEqual(result.end_mono_ns, 20)
        self.assertEqual(result.size_bytes, 1_000)
        self.assertEqual(result.spans, (old_span,))

    def test_finalize_failure_rolls_back_span_and_terminal_row_together(self):
        self.catalog.create_recording(**self._recording_fields("rec-finalize"))
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        self.catalog.update_recording_progress(
            "rec-finalize",
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )
        replace_spans = self.catalog._replace_spans

        def fail_after_span_write(connection, recording_id, spans):
            replace_spans(connection, recording_id, spans)
            raise OSError("injected finalize failure")

        with patch.object(
            self.catalog, "_replace_spans", side_effect=fail_after_span_write
        ):
            with self.assertRaisesRegex(OSError, "finalize failure"):
                self.catalog.finalize_recording(
                    "rec-finalize",
                    start_mono_ns=30,
                    end_mono_ns=40,
                    start_utc_ns=120,
                    end_utc_ns=130,
                    size_bytes=2_000,
                    spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
                )

        result = self.catalog.get_recording("rec-finalize")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.WRITING)
        self.assertEqual(result.start_mono_ns, 10)
        self.assertEqual(result.end_mono_ns, 20)
        self.assertEqual(result.size_bytes, 1_000)
        self.assertEqual(result.spans, (old_span,))

    def test_commit_failure_rolls_back_span_and_recording_row_together(self):
        session = self.catalog.open_writer_session()
        session.create_recording(**self._recording_fields("rec-commit"))
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        session.update_recording_progress(
            "rec-commit",
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )

        def deny_commit(action, argument_one, _argument_two, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and argument_one == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        session._connection.set_authorizer(deny_commit)
        with self.assertRaises(sqlite3.DatabaseError):
            session.update_recording_progress(
                "rec-commit",
                start_mono_ns=30,
                end_mono_ns=40,
                start_utc_ns=120,
                end_utc_ns=130,
                size_bytes=2_000,
                spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
            )
        session._connection.set_authorizer(None)
        session.close()

        result = self.catalog.get_recording("rec-commit")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.WRITING)
        self.assertEqual(result.start_mono_ns, 10)
        self.assertEqual(result.end_mono_ns, 20)
        self.assertEqual(result.size_bytes, 1_000)
        self.assertEqual(result.spans, (old_span,))

    def test_sqlite_io_and_full_errors_at_commit_roll_back_both_generations(self):
        messages = ("disk I/O error", "database or disk is full")
        original_connect = sqlite3.connect

        for index, message in enumerate(messages):
            recording_id = f"rec-storage-error-{index}"
            old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
            self.catalog.create_recording(**self._recording_fields(recording_id))
            self.catalog.update_recording_progress(
                recording_id,
                start_mono_ns=10,
                end_mono_ns=20,
                start_utc_ns=100,
                end_utc_ns=110,
                size_bytes=1_000,
                spans=(old_span,),
            )
            injected = {"message": message}

            def make_faulting_connection(state):
                class FaultingConnection(sqlite3.Connection):
                    def commit(self):
                        if state["message"] is not None:
                            error = state["message"]
                            state["message"] = None
                            raise sqlite3.OperationalError(error)
                        return super().commit()

                return FaultingConnection

            def connect_with_fault(*args, fault_state=injected, **kwargs):
                kwargs["factory"] = make_faulting_connection(fault_state)
                return original_connect(*args, **kwargs)

            with self.subTest(message=message):
                with patch(
                    "raspi_recorder.adapters.recording_catalog.sqlite3.connect",
                    side_effect=connect_with_fault,
                ):
                    with self.assertRaisesRegex(sqlite3.OperationalError, message):
                        self.catalog.update_recording_progress(
                            recording_id,
                            start_mono_ns=30,
                            end_mono_ns=40,
                            start_utc_ns=120,
                            end_utc_ns=130,
                            size_bytes=2_000,
                            spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
                        )

                result = self.catalog.get_recording(recording_id)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.state, RecordingFileState.WRITING)
                self.assertEqual(result.end_mono_ns, 20)
                self.assertEqual(result.size_bytes, 1_000)
                self.assertEqual(result.spans, (old_span,))

    def test_read_only_database_rejects_progress_without_partial_span_change(self):
        recording_id = "rec-read-only"
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        self.catalog.create_recording(**self._recording_fields(recording_id))
        self.catalog.update_recording_progress(
            recording_id,
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )
        database = self.catalog._path
        directory = database.parent
        original_database_mode = database.stat().st_mode & 0o777
        original_directory_mode = directory.stat().st_mode & 0o777
        try:
            database.chmod(0o444)
            directory.chmod(original_directory_mode & ~0o222)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                self.catalog.update_recording_progress(
                    recording_id,
                    start_mono_ns=30,
                    end_mono_ns=40,
                    start_utc_ns=120,
                    end_utc_ns=130,
                    size_bytes=2_000,
                    spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
                )
        finally:
            directory.chmod(original_directory_mode)
            database.chmod(original_database_mode)

        result = self.catalog.get_recording(recording_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.end_mono_ns, 20)
        self.assertEqual(result.size_bytes, 1_000)
        self.assertEqual(result.spans, (old_span,))

    def test_process_crash_after_span_write_recovers_old_atomic_generation(self):
        recording_id = "rec-process-crash"
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        partial = Path(self.temporary.name) / "rec-process-crash.mp4.partial"
        partial.write_bytes(b"partial-media-before-crash")
        self.catalog.create_recording(**self._recording_fields(recording_id))
        self.catalog.update_recording_progress(
            recording_id,
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )
        marker = Path(self.temporary.name) / "span-written-uncommitted"
        script = """
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from raspi_recorder.adapters.recording_catalog import (
    RecordingCatalog,
    RecordingTimeSpan,
)

catalog = RecordingCatalog(Path(sys.argv[1]))
marker = Path(sys.argv[2])
replace_spans = catalog._replace_spans
def pause_after_spans(connection, recording_id, spans):
    replace_spans(connection, recording_id, spans)
    marker.write_text('ready', encoding='ascii')
    while True:
        time.sleep(1)
catalog._replace_spans = pause_after_spans
catalog.finalize_recording(
    'rec-process-crash', start_mono_ns=30, end_mono_ns=40,
    start_utc_ns=120, end_utc_ns=130, size_bytes=2000,
    spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, 'UTC'),),
)
"""
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.catalog._path),
                str(marker),
                str(Path.cwd() / "src"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(timeout=5)
                    stderr = process.stderr.read() if process.stderr else ""
                    self.fail(f"crash fixture did not reach span write: {stderr}")
                time.sleep(0.01)
            if not marker.exists():
                stdout, stderr = process.communicate(timeout=2)
                self.fail(
                    "child did not reach the uncommitted span: "
                    f"exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        result = self.catalog.get_recording(recording_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.WRITING)
        self.assertEqual(result.end_mono_ns, 20)
        self.assertEqual(result.size_bytes, 1_000)
        self.assertEqual(result.spans, (old_span,))
        self.assertEqual(partial.read_bytes(), b"partial-media-before-crash")

        self.assertEqual(
            self.catalog.mark_unfinished_from_previous_epoch_abnormal("new-epoch"),
            1,
        )
        recovered = self.catalog.get_recording(recording_id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.state, RecordingFileState.ABNORMAL)
        self.assertEqual(recovered.end_reason, "previous_epoch_incomplete")
        self.assertEqual(recovered.spans, (old_span,))
        self.assertEqual(partial.read_bytes(), b"partial-media-before-crash")

    def test_reader_never_sees_finalize_spans_without_terminal_row(self):
        self.catalog.create_recording(**self._recording_fields("rec-visible"))
        old_span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        self.catalog.update_recording_progress(
            "rec-visible",
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(old_span,),
        )

        entered = threading.Event()
        release = threading.Event()
        error: list[Exception] = []
        replace_spans = self.catalog._replace_spans

        def pause_after_span_write(connection, recording_id, spans):
            replace_spans(connection, recording_id, spans)
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test reader did not release transaction")

        def finalize():
            try:
                with patch.object(
                    self.catalog, "_replace_spans", side_effect=pause_after_span_write
                ):
                    self.catalog.finalize_recording(
                        "rec-visible",
                        start_mono_ns=30,
                        end_mono_ns=40,
                        start_utc_ns=120,
                        end_utc_ns=130,
                        size_bytes=2_000,
                        spans=(RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),),
                    )
            except Exception as exc:
                error.append(exc)

        thread = threading.Thread(target=finalize)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            during = self.catalog.get_recording("rec-visible")
            self.assertIsNotNone(during)
            assert during is not None
            self.assertEqual(during.state, RecordingFileState.WRITING)
            self.assertEqual(during.spans, (old_span,))
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(error, [])
        after = self.catalog.get_recording("rec-visible")
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(after.state, RecordingFileState.FINALIZED)
        self.assertEqual(
            after.spans, (RecordingTimeSpan(3, 4, 30, 40, 120, 130, "UTC"),)
        )

    def test_terminal_recording_cannot_be_reopened_or_marked_abnormal(self):
        self.catalog.create_recording(**self._recording_fields("rec-terminal"))
        span = RecordingTimeSpan(1, 2, 10, 20, 100, 110, "UTC")
        self.catalog.finalize_recording(
            "rec-terminal",
            start_mono_ns=10,
            end_mono_ns=20,
            start_utc_ns=100,
            end_utc_ns=110,
            size_bytes=1_000,
            spans=(span,),
        )
        with self.assertRaisesRegex(RuntimeError, "writing録画"):
            self.catalog.update_recording_progress(
                "rec-terminal",
                **self._progress_fields(30, 40, 120, 130, 2_000),
            )
        with self.assertRaisesRegex(RuntimeError, "確定済み"):
            self.catalog.mark_recording_abnormal("rec-terminal", "late_abort")
        result = self.catalog.get_recording("rec-terminal")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, RecordingFileState.FINALIZED)
        self.assertEqual(result.spans, (span,))

    def test_connected_media_filter_and_unfinished_epoch_recovery(self):
        self.catalog.create_recording(
            recording_id="rec-old",
            volume_id="vol-a",
            relative_path="recordings/continuous/cam1/old.mp4",
            mode=RecordingMode.CONTINUOUS,
            camera_id=1,
            logical_event_id=None,
            part_index=1,
            boot_id="boot-old",
            writer_epoch="epoch-old",
            created_utc_ns=10,
            settings_snapshot={},
        )
        self.assertEqual(
            self.catalog.mark_unfinished_from_previous_epoch_abnormal("epoch-new"), 1
        )
        self.assertEqual(
            self.catalog.get_recording("rec-old").state,
            RecordingFileState.ABNORMAL,
        )
        self.catalog.set_volume_connected("vol-a", False, 20)
        self.assertEqual(self.catalog.list_recordings(), ())
        self.assertEqual(len(self.catalog.list_volumes()), 1)

    def test_rejects_unsafe_paths_and_switches_single_selected_volume(self):
        with self.assertRaises(ValueError):
            self.catalog.create_recording(
                recording_id="bad",
                volume_id="vol-a",
                relative_path="recordings/../outside.mp4",
                mode=RecordingMode.CONTINUOUS,
                camera_id=1,
                logical_event_id=None,
                part_index=1,
                boot_id="boot-a",
                writer_epoch="epoch-a",
                created_utc_ns=10,
                settings_snapshot={},
            )
        self.catalog.upsert_volume(
            volume_id="vol-b",
            persistent_id="uuid-b",
            kind=VolumeKind.USB,
            fs_type="exfat",
            marker_id="marker-b",
            connected=True,
            selected=True,
            last_seen_utc_ns=20,
        )
        self.assertEqual(
            [
                volume.volume_id
                for volume in self.catalog.list_volumes()
                if volume.selected
            ],
            ["vol-b"],
        )

    def test_abnormal_recording_is_kept_for_display(self):
        self.catalog.create_recording(
            recording_id="rec-bad",
            volume_id="vol-a",
            relative_path="recordings/continuous/cam1/bad.mp4",
            mode=RecordingMode.CONTINUOUS,
            camera_id=1,
            logical_event_id=None,
            part_index=1,
            boot_id="boot-a",
            writer_epoch="epoch-a",
            created_utc_ns=10,
            settings_snapshot={},
        )
        self.catalog.mark_recording_abnormal("rec-bad", "worker_failed")

        result = self.catalog.list_recordings()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].state, RecordingFileState.ABNORMAL)

    @staticmethod
    def _recording_fields(recording_id: str) -> dict[str, object]:
        return {
            "recording_id": recording_id,
            "volume_id": "vol-a",
            "relative_path": f"recordings/continuous/cam1/{recording_id}.mp4",
            "mode": RecordingMode.CONTINUOUS,
            "camera_id": 1,
            "logical_event_id": None,
            "part_index": 1,
            "boot_id": "boot-a",
            "writer_epoch": "epoch-a",
            "created_utc_ns": 1,
            "settings_snapshot": {},
        }

    @staticmethod
    def _progress_fields(
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
    ) -> dict[str, object]:
        return {
            "start_mono_ns": start_mono_ns,
            "end_mono_ns": end_mono_ns,
            "start_utc_ns": start_utc_ns,
            "end_utc_ns": end_utc_ns,
            "size_bytes": size_bytes,
            "spans": (
                RecordingTimeSpan(
                    1,
                    2,
                    start_mono_ns,
                    end_mono_ns,
                    start_utc_ns,
                    end_utc_ns,
                    "UTC",
                ),
            ),
        }


if __name__ == "__main__":
    unittest.main()

"""録画・媒体のSQLite catalog。旧索引のlayoutはデータ互換のため維持する。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from raspi_recorder.domain.camera import RecordingMode
from raspi_recorder.domain.capacity import RecordingFileState, VolumeKind
from raspi_recorder.domain.recordings import (
    RecordingSnapshot,
    RecordingTimeSpan,
    VolumeSnapshot,
)
from raspi_recorder.ports.recording_index import RecordingWriterSession

# SQLite versions on supported Pi images may predate the upstream WAL-reset
# fix. Serialize all in-process catalog write/checkpoint activity, while keeping
# WAL readers concurrent. Coordinate connection open/close separately so a
# last-connection checkpoint cannot overlap a new writer's first commit.
_CATALOG_WRITE_LOCK = threading.RLock()
_CATALOG_CONNECTION_LOCK = threading.RLock()


class _RecordingCatalogWriterSession:
    """1つの物理recordingを所有threadの接続で更新する。"""

    def __init__(
        self, catalog: RecordingCatalog, connection: sqlite3.Connection
    ) -> None:
        self._catalog = catalog
        self._connection = connection
        self._owner_thread_id = threading.get_ident()
        self._closed = False

    def create_recording(
        self,
        *,
        recording_id: str,
        volume_id: str,
        relative_path: str,
        mode: RecordingMode,
        camera_id: int,
        logical_event_id: str | None,
        part_index: int,
        boot_id: str,
        writer_epoch: str,
        created_utc_ns: int,
        settings_snapshot: dict[str, Any],
    ) -> None:
        self._run(
            lambda: self._catalog._create_recording_in_connection(
                self._connection,
                recording_id=recording_id,
                volume_id=volume_id,
                relative_path=relative_path,
                mode=mode,
                camera_id=camera_id,
                logical_event_id=logical_event_id,
                part_index=part_index,
                boot_id=boot_id,
                writer_epoch=writer_epoch,
                created_utc_ns=created_utc_ns,
                settings_snapshot=settings_snapshot,
            )
        )

    def update_recording_progress(
        self,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        self._run(
            lambda: self._catalog._update_recording_progress_in_connection(
                self._connection,
                recording_id,
                start_mono_ns=start_mono_ns,
                end_mono_ns=end_mono_ns,
                start_utc_ns=start_utc_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
                spans=spans,
            )
        )

    def finalize_recording(
        self,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        end_reason: str | None,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        self._run(
            lambda: self._catalog._finalize_recording_in_connection(
                self._connection,
                recording_id,
                start_mono_ns=start_mono_ns,
                end_mono_ns=end_mono_ns,
                start_utc_ns=start_utc_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
                end_reason=end_reason,
                spans=spans,
            )
        )

    def mark_recording_abnormal(
        self,
        recording_id: str,
        reason: str,
        *,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        self._run(
            lambda: self._catalog._mark_recording_abnormal_in_connection(
                self._connection,
                recording_id,
                reason,
                end_mono_ns=end_mono_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
            )
        )

    def close(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("recording writer sessionは作成threadで閉じてください")
        if self._closed:
            return
        with _CATALOG_WRITE_LOCK:
            with _CATALOG_CONNECTION_LOCK:
                try:
                    if self._connection.in_transaction:
                        self._connection.rollback()
                finally:
                    self._connection.close()
                    self._closed = True

    def _run(self, operation: Callable[[], None]) -> None:
        self._assert_owner()
        with _CATALOG_WRITE_LOCK:
            with self._catalog._transaction(self._connection):
                operation()

    def _assert_owner(self) -> None:
        if self._closed:
            raise RuntimeError("recording writer sessionは既に閉じています")
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("recording writer sessionは作成threadで使ってください")


class RecordingCatalog:
    """SQLite catalog。映像frameごとの同期書込は行わない。"""

    _SCHEMA_VERSION = 2
    _BUSY_TIMEOUT_MS = 5_000

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)

    def open_writer_session(self) -> RecordingWriterSession:
        """物理recordingの期間だけ使うthread-confined接続を開く。"""
        with _CATALOG_CONNECTION_LOCK:
            connection = self._open_connection()
        return _RecordingCatalogWriterSession(self, connection)

    def initialize(self) -> None:
        """WAL・foreign keys・versioned catalog schemaを作る。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_migrations("
                "version INTEGER PRIMARY KEY, applied_utc_ns INTEGER NOT NULL)"
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM catalog_migrations"
            ).fetchone()
            version = int(row["version"])
            if version > self._SCHEMA_VERSION:
                raise RuntimeError("catalog schemaがこの版の上限を超えています")
            if version == 0:
                self._create_v1(connection)
                connection.execute(
                    "INSERT INTO catalog_migrations(version, applied_utc_ns) "
                    "VALUES(1, 0)"
                )
                version = 1
            if version == 1:
                self._migrate_v2(connection)
                connection.execute(
                    "INSERT INTO catalog_migrations(version, applied_utc_ns) "
                    "VALUES(2, 0)"
                )
            connection.commit()

    def upsert_volume(
        self,
        *,
        volume_id: str,
        persistent_id: str,
        kind: VolumeKind,
        fs_type: str,
        marker_id: str,
        connected: bool,
        selected: bool,
        last_seen_utc_ns: int,
    ) -> VolumeSnapshot:
        """認識した媒体を更新する。selectedは全volumeで排他的。"""
        self._validate_path_identifier("volume_id", volume_id)
        self._validate_path_identifier("persistent_id", persistent_id)
        self._validate_path_identifier("marker_id", marker_id)
        self._validate_nonnegative_int("last_seen_utc_ns", last_seen_utc_ns)
        if not isinstance(kind, VolumeKind):
            raise ValueError("kindが不正です")
        if fs_type not in {"ext4", "exfat", "microSD"}:
            raise ValueError("未対応のfilesystemです")
        if type(connected) is not bool or type(selected) is not bool:
            raise ValueError("connectedとselectedはboolで指定してください")

        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if selected:
                connection.execute("UPDATE volumes SET selected = 0")
            connection.execute(
                "INSERT INTO volumes(volume_id, persistent_id, kind, fs_type, "
                "marker_id, connected, selected, last_seen_utc_ns) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(volume_id) DO UPDATE SET "
                "persistent_id=excluded.persistent_id, kind=excluded.kind, "
                "fs_type=excluded.fs_type, marker_id=excluded.marker_id, "
                "connected=excluded.connected, selected=excluded.selected, "
                "last_seen_utc_ns=excluded.last_seen_utc_ns",
                (
                    volume_id,
                    persistent_id,
                    kind.value,
                    fs_type,
                    marker_id,
                    int(connected),
                    int(selected),
                    last_seen_utc_ns,
                ),
            )
            row = connection.execute(
                "SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._volume_from_row(row)

    def set_volume_connected(
        self, volume_id: str, connected: bool, last_seen_utc_ns: int
    ) -> None:
        """媒体scan結果を記録する。未接続でも索引は保持する。"""
        if type(connected) is not bool:
            raise ValueError("connectedはboolで指定してください")
        self._validate_nonnegative_int("last_seen_utc_ns", last_seen_utc_ns)
        with self._write_connection() as connection:
            cursor = connection.execute(
                "UPDATE volumes SET connected = ?, last_seen_utc_ns = ? "
                "WHERE volume_id = ?",
                (int(connected), last_seen_utc_ns, volume_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(volume_id)

    def list_volumes(
        self, *, connected_only: bool = False
    ) -> tuple[VolumeSnapshot, ...]:
        """接続中または登録済み媒体をID順で返す。"""
        if type(connected_only) is not bool:
            raise ValueError("connected_onlyはboolで指定してください")
        query = "SELECT * FROM volumes"
        if connected_only:
            query += " WHERE connected = 1"
        query += " ORDER BY volume_id"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(self._volume_from_row(row) for row in rows)

    def create_recording(
        self,
        *,
        recording_id: str,
        volume_id: str,
        relative_path: str,
        mode: RecordingMode,
        camera_id: int,
        logical_event_id: str | None,
        part_index: int,
        boot_id: str,
        writer_epoch: str,
        created_utc_ns: int,
        settings_snapshot: dict[str, Any],
    ) -> None:
        """writing索引を作る。パスは専用領域からのrelative pathのみ。"""
        with self._write_connection() as connection:
            self._create_recording_in_connection(
                connection,
                recording_id=recording_id,
                volume_id=volume_id,
                relative_path=relative_path,
                mode=mode,
                camera_id=camera_id,
                logical_event_id=logical_event_id,
                part_index=part_index,
                boot_id=boot_id,
                writer_epoch=writer_epoch,
                created_utc_ns=created_utc_ns,
                settings_snapshot=settings_snapshot,
            )

    def _create_recording_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        recording_id: str,
        volume_id: str,
        relative_path: str,
        mode: RecordingMode,
        camera_id: int,
        logical_event_id: str | None,
        part_index: int,
        boot_id: str,
        writer_epoch: str,
        created_utc_ns: int,
        settings_snapshot: dict[str, Any],
    ) -> None:
        """writer sessionまたは短命接続上でwriting rowを作る。"""
        self._validate_path_identifier("recording_id", recording_id)
        self._validate_path_identifier("volume_id", volume_id)
        self._validate_path_identifier("boot_id", boot_id)
        self._validate_path_identifier("writer_epoch", writer_epoch)
        # engine_socket / linux_storageと同じ規則にそろえる:
        # 絶対path・空・'.'/'..' segment・backslashを拒否する。
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("relative_pathは相対pathで指定してください")
        pure_path = PurePosixPath(relative_path)
        if (
            pure_path.is_absolute()
            or not pure_path.parts
            or ".." in pure_path.parts
            or "." in pure_path.parts
            or "\\" in relative_path
        ):
            raise ValueError("relative_pathに親移動・Windows separatorは使えません")
        if not isinstance(mode, RecordingMode):
            raise ValueError("modeが不正です")
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        if type(part_index) is not int or part_index < 1:
            raise ValueError("part_indexは1以上の整数で指定してください")
        self._validate_nonnegative_int("created_utc_ns", created_utc_ns)
        settings_json = self._encode_object(settings_snapshot)
        connection.execute(
            "INSERT INTO recordings(recording_id, volume_id, relative_path, "
            "mode, camera_id, logical_event_id, part_index, state, boot_id, "
            "writer_epoch, created_utc_ns, settings_snapshot_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'writing', ?, ?, ?, ?)",
            (
                recording_id,
                volume_id,
                relative_path,
                mode.value,
                camera_id,
                logical_event_id,
                part_index,
                boot_id,
                writer_epoch,
                created_utc_ns,
                settings_json,
            ),
        )

    def update_recording_progress(
        self,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        """遅くとも定期manifest更新時に進捗を同期する。"""
        with self._write_connection() as connection:
            self._update_recording_progress_in_connection(
                connection,
                recording_id,
                start_mono_ns=start_mono_ns,
                end_mono_ns=end_mono_ns,
                start_utc_ns=start_utc_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
                spans=spans,
            )

    def _update_recording_progress_in_connection(
        self,
        connection: sqlite3.Connection,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        """writing確認・span置換・進捗更新を一 transaction で行う。"""
        self._validate_path_identifier("recording_id", recording_id)
        self._validate_nonnegative_int("start_mono_ns", start_mono_ns)
        self._validate_nonnegative_int("end_mono_ns", end_mono_ns)
        self._validate_nonnegative_int("start_utc_ns", start_utc_ns)
        self._validate_nonnegative_int("end_utc_ns", end_utc_ns)
        self._validate_nonnegative_int("size_bytes", size_bytes)
        if end_mono_ns < start_mono_ns:
            raise ValueError("end_mono_nsがstartより前です")
        if not spans or not all(isinstance(span, RecordingTimeSpan) for span in spans):
            raise ValueError("spansに1つ以上のRecordingTimeSpanが必要です")
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state FROM recordings WHERE recording_id=?", (recording_id,)
        ).fetchone()
        if current is None or current["state"] != "writing":
            raise RuntimeError("writing録画が見つかりません")
        self._replace_spans(connection, recording_id, spans)
        cursor = connection.execute(
            "UPDATE recordings SET start_mono_ns=?, end_mono_ns=?, "
            "start_utc_ns=?, end_utc_ns=?, duration_ns=?, size_bytes=? "
            "WHERE recording_id=? AND state='writing'",
            (
                start_mono_ns,
                end_mono_ns,
                start_utc_ns,
                end_utc_ns,
                end_mono_ns - start_mono_ns,
                size_bytes,
                recording_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("writing録画が見つかりません")

    def finalize_recording(
        self,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        end_reason: str | None = None,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        """MP4確定と索引stateを原子的に更新する。"""
        with self._write_connection() as connection:
            self._finalize_recording_in_connection(
                connection,
                recording_id,
                start_mono_ns=start_mono_ns,
                end_mono_ns=end_mono_ns,
                start_utc_ns=start_utc_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
                end_reason=end_reason,
                spans=spans,
            )

    def _finalize_recording_in_connection(
        self,
        connection: sqlite3.Connection,
        recording_id: str,
        *,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        end_reason: str | None = None,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        """span・録画時刻・terminal stateを一 transaction で確定する。"""
        self._validate_path_identifier("recording_id", recording_id)
        self._validate_recording_times(
            start_mono_ns, end_mono_ns, start_utc_ns, end_utc_ns, size_bytes, spans
        )
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state FROM recordings WHERE recording_id=?", (recording_id,)
        ).fetchone()
        if current is None or current["state"] != "writing":
            raise RuntimeError("writing録画が見つかりません")
        self._replace_spans(connection, recording_id, spans)
        cursor = connection.execute(
            "UPDATE recordings SET state='finalized', start_mono_ns=?, "
            "end_mono_ns=?, start_utc_ns=?, end_utc_ns=?, duration_ns=?, "
            "size_bytes=?, end_reason=? WHERE recording_id=? AND state='writing'",
            (
                start_mono_ns,
                end_mono_ns,
                start_utc_ns,
                end_utc_ns,
                end_mono_ns - start_mono_ns,
                size_bytes,
                None if end_reason is None else end_reason[:120],
                recording_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("writing録画が見つかりません")

    def mark_recording_abnormal(
        self,
        recording_id: str,
        reason: str,
        *,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """partialを削除せずabnormal状態へ移す。"""
        with self._write_connection() as connection:
            self._mark_recording_abnormal_in_connection(
                connection,
                recording_id,
                reason,
                end_mono_ns=end_mono_ns,
                end_utc_ns=end_utc_ns,
                size_bytes=size_bytes,
            )

    def _mark_recording_abnormal_in_connection(
        self,
        connection: sqlite3.Connection,
        recording_id: str,
        reason: str,
        *,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """writingをabnormalへ一方向に遷移する。"""
        self._validate_path_identifier("recording_id", recording_id)
        self._validate_identifier("reason", reason)
        for name, value in (
            ("end_mono_ns", end_mono_ns),
            ("end_utc_ns", end_utc_ns),
            ("size_bytes", size_bytes),
        ):
            if value is not None:
                self._validate_nonnegative_int(name, value)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM recordings WHERE recording_id=?", (recording_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("異常化できる録画が見つかりません")
        if row["state"] == "abnormal":
            return
        if row["state"] != "writing":
            raise RuntimeError("確定済み録画を異常状態へ戻せません")
        cursor = connection.execute(
            "UPDATE recordings SET state='abnormal', end_mono_ns=?, "
            "end_utc_ns=?, size_bytes=?, end_reason=? "
            "WHERE recording_id=? AND state='writing'",
            (
                end_mono_ns,
                end_utc_ns,
                size_bytes,
                reason[:120],
                recording_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("異常化できる録画が見つかりません")

    def mark_recording_deleting(self, recording_id: str) -> RecordingFileState:
        """確定済みnormal/abnormalをdeletingへロックする。"""
        self._validate_path_identifier("recording_id", recording_id)
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM recordings WHERE recording_id=?",
                (recording_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(recording_id)
            previous = RecordingFileState(row["state"])
            if previous not in {
                RecordingFileState.FINALIZED,
                RecordingFileState.ABNORMAL,
            }:
                connection.rollback()
                raise RuntimeError("writing/deleting/missing fileは削除できません")
            connection.execute(
                "UPDATE recordings SET state='deleting' WHERE recording_id=?",
                (recording_id,),
            )
            connection.commit()
        return previous

    def restore_recording_state(
        self, recording_id: str, previous_state: RecordingFileState
    ) -> None:
        """unlink失敗時にdeleting lease stateを元へ戻す。"""
        if previous_state not in {
            RecordingFileState.FINALIZED,
            RecordingFileState.ABNORMAL,
        }:
            raise ValueError("previous_stateはfinalizedまたはabnormalが必要です")
        with self._write_connection() as connection:
            cursor = connection.execute(
                "UPDATE recordings SET state=? "
                "WHERE recording_id=? AND state='deleting'",
                (previous_state.value, recording_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("deleting録画を復元できません")

    def delete_recording_index(self, recording_id: str) -> None:
        """unlink済みdeleting行とtime spansを消す。"""
        self._validate_path_identifier("recording_id", recording_id)
        with self._write_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT logical_event_id FROM recordings "
                "WHERE recording_id=? AND state='deleting'",
                (recording_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("deleting録画がありません")
            event_id = row["logical_event_id"]
            connection.execute(
                "DELETE FROM recordings WHERE recording_id=?", (recording_id,)
            )
            if event_id is not None:
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM recordings WHERE logical_event_id=?",
                    (event_id,),
                ).fetchone()
                assert remaining is not None
                if int(remaining["count"]) == 0:
                    connection.execute(
                        "DELETE FROM logical_events WHERE event_id=?", (event_id,)
                    )
                else:
                    connection.execute(
                        "UPDATE logical_events SET state='partial', "
                        "end_reason='part_deleted' WHERE event_id=?",
                        (event_id,),
                    )
            connection.commit()

    def mark_recording_missing(self, recording_id: str) -> None:
        """実ファイルがない索引をmissingとして残す。"""
        with self._write_connection() as connection:
            cursor = connection.execute(
                "UPDATE recordings SET state='missing' "
                "WHERE recording_id=? AND state IN "
                "('finalized','abnormal','deleting')",
                (recording_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("missingへ移行できる録画がありません")

    def mark_unfinished_from_previous_epoch_abnormal(
        self, current_writer_epoch: str
    ) -> int:
        """起動時に前epochの未完了録画・削除・logical eventを閉じる。"""
        self._validate_path_identifier("current_writer_epoch", current_writer_epoch)
        with self._write_connection() as connection:
            cursor = connection.execute(
                "UPDATE recordings SET state='abnormal', "
                "end_reason=CASE WHEN state='deleting' "
                "THEN 'previous_epoch_deletion_interrupted' "
                "ELSE 'previous_epoch_incomplete' END "
                "WHERE state IN ('writing','deleting') AND writer_epoch != ?",
                (current_writer_epoch,),
            )
            recovered = cursor.rowcount
            connection.execute(
                "UPDATE logical_events SET state='abnormal', "
                "end_reason='previous_epoch_incomplete' "
                "WHERE state='active' AND NOT EXISTS ("
                "SELECT 1 FROM recordings r "
                "WHERE r.logical_event_id=logical_events.event_id "
                "AND r.writer_epoch=?)",
                (current_writer_epoch,),
            )
            return recovered

    def get_recording(self, recording_id: str) -> RecordingSnapshot | None:
        """物理録画をIDで返す。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT r.* FROM recordings r "
                "JOIN volumes v USING(volume_id) WHERE r.recording_id=?",
                (recording_id,),
            ).fetchone()
            # creation_sequence is an INTEGER PRIMARY KEY in recordings.
            spans = self._load_spans(connection, recording_id)
        return None if row is None else self._recording_from_row(row, spans)

    def oldest_deletion_candidate(
        self, volume_id: str, *, excluded_ids: tuple[str, ...] = ()
    ) -> str | None:
        """指定済み接続媒体から最古の確定物理fileを1件だけ返す。"""
        self._validate_path_identifier("volume_id", volume_id)
        if not isinstance(excluded_ids, tuple) or not all(
            isinstance(item, str) and item for item in excluded_ids
        ):
            raise ValueError("excluded_idsは空でない録画IDのtupleが必要です")
        clauses = [
            "r.volume_id=?",
            "v.connected=1",
            "r.state IN ('finalized','abnormal')",
        ]
        params: list[Any] = [volume_id]
        if excluded_ids:
            clauses.append(
                "r.recording_id NOT IN (" + ",".join("?" for _ in excluded_ids) + ")"
            )
            params.extend(excluded_ids)
        timestamp = (
            "COALESCE((SELECT MIN(MIN(s.start_utc_ns,s.end_utc_ns)) "
            "FROM recording_time_spans s WHERE s.recording_id=r.recording_id), "
            "r.start_utc_ns)"
        )
        query = (
            "SELECT r.recording_id FROM recordings r "
            "JOIN volumes v USING(volume_id) WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY CASE WHEN {timestamp} IS NULL THEN 1 ELSE 0 END, "
            + f"{timestamp}, r.creation_sequence, r.recording_id LIMIT 1"
        )
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return None if row is None else str(row["recording_id"])

    def list_recordings(
        self,
        *,
        camera_id: int | None = None,
        mode: RecordingMode | None = None,
        from_utc_ns: int | None = None,
        to_utc_ns: int | None = None,
        limit: int = 100,
        before_sequence: int | None = None,
    ) -> tuple[RecordingSnapshot, ...]:
        """接続中mediaのみを、作成順cursorで検索する。"""
        if camera_id is not None and (
            type(camera_id) is not int or camera_id not in (1, 2)
        ):
            raise ValueError("camera_idは1または2で指定してください")
        if mode is not None and not isinstance(mode, RecordingMode):
            raise ValueError("modeが不正です")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limitは1～500で指定してください")
        for name, value in (
            ("from_utc_ns", from_utc_ns),
            ("to_utc_ns", to_utc_ns),
            ("before_sequence", before_sequence),
        ):
            if value is not None:
                self._validate_nonnegative_int(name, value)
        if (
            from_utc_ns is not None
            and to_utc_ns is not None
            and from_utc_ns >= to_utc_ns
        ):
            raise ValueError("検索区間はfrom < toが必要です")

        clauses = ["v.connected=1", "r.state IN ('finalized','abnormal')"]
        params: list[Any] = []
        if camera_id is not None:
            clauses.append("r.camera_id=?")
            params.append(camera_id)
        if mode is not None:
            clauses.append("r.mode=?")
            params.append(mode.value)
        if before_sequence is not None:
            clauses.append("r.creation_sequence<?")
            params.append(before_sequence)
        if from_utc_ns is not None or to_utc_ns is not None:
            time_clause = [
                "EXISTS (SELECT 1 FROM recording_time_spans s "
                "WHERE s.recording_id=r.recording_id"
            ]
            if to_utc_ns is not None:
                time_clause.append("AND min(s.start_utc_ns,s.end_utc_ns) < ?")
                params.append(to_utc_ns)
            if from_utc_ns is not None:
                time_clause.append("AND max(s.start_utc_ns,s.end_utc_ns) >= ?")
                params.append(from_utc_ns)
            time_clause.append(")")
            clauses.append(" ".join(time_clause))
        query = (
            "SELECT r.* FROM recordings r JOIN volumes v USING(volume_id) WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.creation_sequence DESC LIMIT ?"
        )
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
            snapshots = tuple(
                self._recording_from_row(
                    row, self._load_spans(connection, str(row["recording_id"]))
                )
                for row in rows
            )
        return snapshots

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """WAL reader connection; open/close cannot race a final checkpoint."""
        with _CATALOG_CONNECTION_LOCK:
            connection = self._open_connection()
        try:
            yield connection
        finally:
            with _CATALOG_CONNECTION_LOCK:
                connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize writes through commit and connection close within the process."""
        with _CATALOG_WRITE_LOCK:
            with _CATALOG_CONNECTION_LOCK:
                connection = self._open_connection()
            try:
                with self._transaction(connection):
                    yield connection
            finally:
                with _CATALOG_CONNECTION_LOCK:
                    connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        """接続ごとのtimeout・foreign key・durability設定を適用する。"""
        connection = sqlite3.connect(self._path, timeout=self._BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """成功時だけcommitし、例外時は同じ接続のtransactionをrollbackする。"""
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    def _replace_spans(
        self,
        connection: sqlite3.Connection,
        recording_id: str,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        if not spans or not all(isinstance(span, RecordingTimeSpan) for span in spans):
            raise ValueError("spansに1つ以上のRecordingTimeSpanが必要です")
        for span in spans:
            self._validate_time_span(span)
        connection.execute(
            "DELETE FROM recording_time_spans WHERE recording_id=?",
            (recording_id,),
        )
        connection.executemany(
            "INSERT INTO recording_time_spans(recording_id, ordinal, "
            "first_sequence, last_sequence, start_mono_ns, end_mono_ns, "
            "start_utc_ns, end_utc_ns, timezone) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    recording_id,
                    index,
                    span.first_sequence,
                    span.last_sequence,
                    span.start_mono_ns,
                    span.end_mono_ns,
                    span.start_utc_ns,
                    span.end_utc_ns,
                    span.timezone,
                )
                for index, span in enumerate(spans)
            ],
        )

    @staticmethod
    def _load_spans(
        connection: sqlite3.Connection, recording_id: str
    ) -> tuple[RecordingTimeSpan, ...]:
        rows = connection.execute(
            "SELECT * FROM recording_time_spans WHERE recording_id=? ORDER BY ordinal",
            (recording_id,),
        ).fetchall()
        return tuple(
            RecordingTimeSpan(
                first_sequence=int(row["first_sequence"]),
                last_sequence=int(row["last_sequence"]),
                start_mono_ns=int(row["start_mono_ns"]),
                end_mono_ns=int(row["end_mono_ns"]),
                start_utc_ns=int(row["start_utc_ns"]),
                end_utc_ns=int(row["end_utc_ns"]),
                timezone=str(row["timezone"]),
            )
            for row in rows
        )

    @staticmethod
    def _volume_from_row(row: sqlite3.Row) -> VolumeSnapshot:
        return VolumeSnapshot(
            volume_id=str(row["volume_id"]),
            persistent_id=str(row["persistent_id"]),
            kind=VolumeKind(row["kind"]),
            fs_type=str(row["fs_type"]),
            marker_id=str(row["marker_id"]),
            connected=bool(row["connected"]),
            selected=bool(row["selected"]),
            last_seen_utc_ns=int(row["last_seen_utc_ns"]),
        )

    @staticmethod
    def _recording_from_row(
        row: sqlite3.Row, spans: tuple[RecordingTimeSpan, ...]
    ) -> RecordingSnapshot:
        return RecordingSnapshot(
            recording_id=str(row["recording_id"]),
            creation_sequence=int(row["creation_sequence"]),
            volume_id=str(row["volume_id"]),
            relative_path=str(row["relative_path"]),
            mode=str(row["mode"]),
            camera_id=int(row["camera_id"]),
            logical_event_id=row["logical_event_id"],
            part_index=int(row["part_index"]),
            state=RecordingFileState(row["state"]),
            boot_id=str(row["boot_id"]),
            writer_epoch=str(row["writer_epoch"]),
            start_mono_ns=None
            if row["start_mono_ns"] is None
            else int(row["start_mono_ns"]),
            end_mono_ns=None if row["end_mono_ns"] is None else int(row["end_mono_ns"]),
            start_utc_ns=None
            if row["start_utc_ns"] is None
            else int(row["start_utc_ns"]),
            end_utc_ns=None if row["end_utc_ns"] is None else int(row["end_utc_ns"]),
            duration_ns=None if row["duration_ns"] is None else int(row["duration_ns"]),
            size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
            created_utc_ns=int(row["created_utc_ns"]),
            end_reason=row["end_reason"],
            spans=spans,
        )

    @staticmethod
    def _validate_time_span(span: RecordingTimeSpan) -> None:
        for name in (
            "first_sequence",
            "last_sequence",
            "start_mono_ns",
            "end_mono_ns",
            "start_utc_ns",
            "end_utc_ns",
        ):
            RecordingCatalog._validate_nonnegative_int(name, getattr(span, name))
        if span.first_sequence > span.last_sequence:
            raise ValueError("time span sequence rangeが逆です")
        if span.start_mono_ns > span.end_mono_ns:
            raise ValueError("monotonic time spanが逆です")
        RecordingCatalog._validate_identifier("timezone", span.timezone)

    @classmethod
    def _validate_recording_times(
        cls,
        start_mono_ns: int,
        end_mono_ns: int,
        start_utc_ns: int,
        end_utc_ns: int,
        size_bytes: int,
        spans: Sequence[RecordingTimeSpan],
    ) -> None:
        for name, value in (
            ("start_mono_ns", start_mono_ns),
            ("end_mono_ns", end_mono_ns),
            ("start_utc_ns", start_utc_ns),
            ("end_utc_ns", end_utc_ns),
            ("size_bytes", size_bytes),
        ):
            cls._validate_nonnegative_int(name, value)
        if end_mono_ns < start_mono_ns:
            raise ValueError("end_mono_nsがstartより前です")
        if not spans:
            raise ValueError("spansに1つ以上のtime spanが必要です")
        for span in spans:
            cls._validate_time_span(span)

    @staticmethod
    def _validate_identifier(name: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{name}は1～200文字の文字列が必要です")

    @classmethod
    def _validate_path_identifier(cls, name: str, value: str) -> None:
        """ID系の値にpath区切り・traversal・NULを持ち込ませない。

        timezone(Asia/Tokyo等)や自由文のreasonには使わないこと。
        """
        cls._validate_identifier(name, value)
        if "/" in value or "\\" in value or "\x00" in value or value in {".", ".."}:
            raise ValueError(f"{name}にpath区切りやtraversalは使えません")

    @staticmethod
    def _validate_nonnegative_int(name: str, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name}は0以上の整数が必要です")

    @staticmethod
    def _encode_object(value: dict[str, Any]) -> str:
        if not isinstance(value, dict):
            raise ValueError("settings_snapshotはJSON objectが必要です")
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _create_v1(connection: sqlite3.Connection) -> None:
        # executescriptは保留中のtransactionを暗黙にCOMMITしてしまうため、
        # 呼び出し側のBEGIN IMMEDIATEの中で原子的に作成できるよう、
        # sqlite_storeと同様に文を1つずつexecuteする。
        connection.execute(
            "CREATE TABLE IF NOT EXISTS volumes ("
            "volume_id TEXT PRIMARY KEY, persistent_id TEXT NOT NULL, "
            "kind TEXT NOT NULL CHECK(kind IN ('microSD','USB')), "
            "fs_type TEXT NOT NULL CHECK(fs_type IN ('ext4','exfat','microSD')), "
            "marker_id TEXT NOT NULL, connected INTEGER NOT NULL "
            "CHECK(connected IN (0,1)), "
            "selected INTEGER NOT NULL CHECK(selected IN (0,1)), "
            "last_seen_utc_ns INTEGER NOT NULL CHECK(last_seen_utc_ns >= 0))"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_selected_volume_idx "
            "ON volumes(selected) "
            "WHERE selected=1"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS logical_events ("
            "event_id TEXT PRIMARY KEY, camera_id INTEGER NOT NULL "
            "CHECK(camera_id IN (1,2)), "
            "boot_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN "
            "('active','finalized','abnormal','partial')), "
            "first_trigger_mono_ns INTEGER NOT NULL, "
            "last_trigger_mono_ns INTEGER NOT NULL, "
            "first_trigger_utc_ns INTEGER NOT NULL, "
            "last_trigger_utc_ns INTEGER NOT NULL, requested_pre_ns INTEGER NOT NULL, "
            "actual_pre_ns INTEGER NOT NULL, pre_shortfall_ns INTEGER NOT NULL, "
            "sequence_gaps INTEGER NOT NULL, created_utc_ns INTEGER NOT NULL, "
            "end_reason TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS event_triggers ("
            "trigger_id TEXT PRIMARY KEY, event_id TEXT NOT NULL "
            "REFERENCES logical_events(event_id), "
            "sequence INTEGER NOT NULL, edge TEXT NOT NULL "
            "CHECK(edge IN ('off_to_on','on_to_off')), "
            "raw_mono_ns INTEGER NOT NULL, confirmed_mono_ns INTEGER NOT NULL, "
            "raw_utc_ns INTEGER NOT NULL, confirmed_utc_ns INTEGER NOT NULL, "
            "boot_id TEXT NOT NULL, input_revision INTEGER NOT NULL, "
            "UNIQUE(event_id, sequence))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recordings ("
            "creation_sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "recording_id TEXT NOT NULL UNIQUE, volume_id TEXT NOT NULL "
            "REFERENCES volumes(volume_id), "
            "relative_path TEXT NOT NULL, mode TEXT NOT NULL "
            "CHECK(mode IN ('continuous','event')), "
            "camera_id INTEGER NOT NULL CHECK(camera_id IN (1,2)), "
            "logical_event_id TEXT REFERENCES logical_events(event_id), "
            "part_index INTEGER NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN "
            "('writing','finalized','abnormal','deleting','missing')), "
            "boot_id TEXT NOT NULL, writer_epoch TEXT NOT NULL, "
            "start_mono_ns INTEGER, end_mono_ns INTEGER, "
            "start_utc_ns INTEGER, end_utc_ns INTEGER, duration_ns INTEGER, "
            "size_bytes INTEGER, created_utc_ns INTEGER NOT NULL, end_reason TEXT, "
            "settings_snapshot_json TEXT NOT NULL, "
            "UNIQUE(volume_id, relative_path))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS recordings_time_idx "
            "ON recordings(start_utc_ns, creation_sequence)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS recordings_event_idx "
            "ON recordings(logical_event_id, part_index)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS recordings_volume_idx "
            "ON recordings(volume_id, state, start_utc_ns)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recording_time_spans ("
            "recording_id TEXT NOT NULL REFERENCES recordings(recording_id) "
            "ON DELETE CASCADE, "
            "ordinal INTEGER NOT NULL, first_sequence INTEGER NOT NULL, "
            "last_sequence INTEGER NOT NULL, "
            "start_mono_ns INTEGER NOT NULL, end_mono_ns INTEGER NOT NULL, "
            "start_utc_ns INTEGER NOT NULL, end_utc_ns INTEGER NOT NULL, "
            "timezone TEXT NOT NULL, "
            "PRIMARY KEY(recording_id, ordinal))"
        )

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """全part削除時にevent triggerもcascadeできるようFKを更新する。"""
        connection.execute(
            "CREATE TABLE event_triggers_v2 ("
            "trigger_id TEXT PRIMARY KEY, event_id TEXT NOT NULL "
            "REFERENCES logical_events(event_id) ON DELETE CASCADE, "
            "sequence INTEGER NOT NULL, edge TEXT NOT NULL "
            "CHECK(edge IN ('off_to_on','on_to_off')), "
            "raw_mono_ns INTEGER NOT NULL, confirmed_mono_ns INTEGER NOT NULL, "
            "raw_utc_ns INTEGER NOT NULL, confirmed_utc_ns INTEGER NOT NULL, "
            "boot_id TEXT NOT NULL, input_revision INTEGER NOT NULL, "
            "UNIQUE(event_id, sequence))"
        )
        connection.execute(
            "INSERT INTO event_triggers_v2(trigger_id, event_id, sequence, edge, "
            "raw_mono_ns, confirmed_mono_ns, raw_utc_ns, confirmed_utc_ns, "
            "boot_id, input_revision) SELECT trigger_id, event_id, sequence, edge, "
            "raw_mono_ns, confirmed_mono_ns, raw_utc_ns, confirmed_utc_ns, "
            "boot_id, input_revision FROM event_triggers"
        )
        connection.execute("DROP TABLE event_triggers")
        connection.execute("ALTER TABLE event_triggers_v2 RENAME TO event_triggers")

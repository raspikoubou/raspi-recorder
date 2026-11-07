"""録画・媒体indexのengine-owned永続化境界。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from raspi_recorder.domain.camera import RecordingMode
from raspi_recorder.domain.capacity import RecordingFileState
from raspi_recorder.domain.recordings import (
    RecordingSnapshot,
    RecordingTimeSpan,
    VolumeSnapshot,
)


class RecordingIndex(Protocol):
    """録画行・媒体接続状態・削除stateを管理する。"""

    def get_recording(self, recording_id: str) -> RecordingSnapshot | None:
        """物理録画をIDで返す。"""
        ...

    def oldest_deletion_candidate(
        self, volume_id: str, *, excluded_ids: tuple[str, ...] = ()
    ) -> str | None:
        """指定媒体の最古finalized/abnormal IDを1件だけ返す。"""
        ...

    def list_volumes(
        self, *, connected_only: bool = False
    ) -> tuple[VolumeSnapshot, ...]:
        """登録済みまたは接続中媒体を返す。"""
        ...

    def mark_recording_deleting(self, recording_id: str) -> RecordingFileState:
        """対象をdelete lock stateへ移す。"""
        ...

    def restore_recording_state(
        self, recording_id: str, previous_state: RecordingFileState
    ) -> None:
        """削除失敗時に元stateへ戻す。"""
        ...

    def delete_recording_index(self, recording_id: str) -> None:
        """unlink済み録画行を消し、旧索引の参照も整える。"""
        ...

    def mark_recording_missing(self, recording_id: str) -> None:
        """欠落fileをmissing stateで保持する。"""
        ...


class RecordingWriterIndex(RecordingIndex, Protocol):
    """writerが少頻度で行うrecording index更新。"""

    def open_writer_session(self) -> RecordingWriterSession:
        """単一writer threadが物理partの確定まで使う接続を開く。"""
        ...

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
        """writer開始前にwriting rowを登録する。"""
        ...

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
        """一定間隔でwriting進捗を保存する。"""
        ...

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
        """正常確定済みMP4とindex stateを保存する。"""
        ...

    def mark_recording_abnormal(
        self,
        recording_id: str,
        reason: str,
        *,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """partial/最終fileをabnormalとして保持する。"""
        ...


class RecordingWriterSession(Protocol):
    """単一物理writerのDB更新を同じthread所有connectionで行う。"""

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
        """writing rowを作る。"""
        ...

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
        """進捗を一つのtransactionで更新する。"""
        ...

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
        """spanとterminal stateを一つのtransactionで確定する。"""
        ...

    def mark_recording_abnormal(
        self,
        recording_id: str,
        reason: str,
        *,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """writing rowをabnormalへ進める。"""
        ...

    def close(self) -> None:
        """所有thread上で接続を閉じる。"""
        ...

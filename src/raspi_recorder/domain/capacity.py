"""媒体空き容量の削除・強制停止規則。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

BYTES_PER_GB = 1_000_000_000
BYTES_PER_TENTH_GB = 100_000_000


class VolumeKind(StrEnum):
    """サポート対象の保存先種別。"""

    MICROSD = "microSD"
    USB = "USB"


class CapacityAction(StrEnum):
    """容量監視が次に行う処理。"""

    CONTINUE = "continue"
    DELETE_OLDEST = "delete_oldest"
    STOP_RECORDING = "stop_recording"


class RecordingFileState(StrEnum):
    """削除候補となる物理録画ファイルの状態。"""

    WRITING = "writing"
    FINALIZED = "finalized"
    ABNORMAL = "abnormal"
    DELETING = "deleting"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class CapacityThresholds:
    """容量設定。GBは10進GB、設定刻みは0.1GB。"""

    volume_kind: VolumeKind
    volume_bytes: int
    start_tenths_gb: int = 50
    recover_tenths_gb: int = 70

    def __post_init__(self) -> None:
        if not isinstance(self.volume_kind, VolumeKind):
            raise ValueError("volume_kindが不正です")
        if type(self.volume_bytes) is not int or self.volume_bytes <= 0:
            raise ValueError("volume_bytesは正の整数で指定してください")
        if (
            type(self.start_tenths_gb) is not int
            or type(self.recover_tenths_gb) is not int
        ):
            raise ValueError("容量閾値は0.1GB単位の整数で指定してください")
        if self.start_tenths_gb <= self.stop_tenths_gb:
            raise ValueError("削除開始閾値は強制停止閾値より大きくしてください")
        if self.recover_tenths_gb <= self.start_tenths_gb:
            raise ValueError("回復目標は削除開始閾値より大きくしてください")
        if self.recover_bytes > self.volume_bytes:
            raise ValueError("回復目標は媒体の実容量以下にしてください")

    @property
    def stop_tenths_gb(self) -> int:
        """媒体種別ごとの強制停止値。"""
        return 20 if self.volume_kind is VolumeKind.MICROSD else 10

    @property
    def stop_bytes(self) -> int:
        """強制停止値をbytesで返す。"""
        return self.stop_tenths_gb * BYTES_PER_TENTH_GB

    @property
    def start_bytes(self) -> int:
        """自動削除開始値をbytesで返す。"""
        return self.start_tenths_gb * BYTES_PER_TENTH_GB

    @property
    def recover_bytes(self) -> int:
        """削除後の回復目標をbytesで返す。"""
        return self.recover_tenths_gb * BYTES_PER_TENTH_GB


def choose_capacity_action(
    free_bytes: int,
    thresholds: CapacityThresholds,
    cleanup_active: bool,
) -> CapacityAction:
    """空き容量と既存削除状態から録画継続・削除・停止を決める。

    削除開始はstart未満、回復完了はrecover以上、強制停止はstop以下。
    """
    if type(free_bytes) is not int or free_bytes < 0:
        raise ValueError("free_bytesは0以上の整数で指定してください")
    if free_bytes > thresholds.volume_bytes:
        raise ValueError("free_bytesは媒体の実容量以下で指定してください")
    if type(cleanup_active) is not bool:
        raise ValueError("cleanup_activeはboolで指定してください")
    if free_bytes <= thresholds.stop_bytes:
        return CapacityAction.STOP_RECORDING
    if cleanup_active:
        return (
            CapacityAction.DELETE_OLDEST
            if free_bytes < thresholds.recover_bytes
            else CapacityAction.CONTINUE
        )
    if free_bytes < thresholds.start_bytes:
        return CapacityAction.DELETE_OLDEST
    return CapacityAction.CONTINUE


@dataclass(frozen=True, slots=True)
class DeletionCandidate:
    """削除順を決めるための、アダプターで解決済みのファイル情報。"""

    recording_id: str
    volume_id: str
    state: RecordingFileState
    managed_by_app: bool
    sort_timestamp_ns: int | None
    creation_sequence: int
    read_lease_count: int = 0

    def __post_init__(self) -> None:
        if not self.recording_id or not self.volume_id:
            raise ValueError("recording_idとvolume_idは空にできません")
        if not isinstance(self.state, RecordingFileState):
            raise ValueError("stateが不正です")
        if type(self.managed_by_app) is not bool:
            raise ValueError("managed_by_appはboolで指定してください")
        if self.sort_timestamp_ns is not None and (
            type(self.sort_timestamp_ns) is not int or self.sort_timestamp_ns < 0
        ):
            raise ValueError(
                "sort_timestamp_nsは未設定または0以上の整数で指定してください"
            )
        if type(self.creation_sequence) is not int or self.creation_sequence < 0:
            raise ValueError("creation_sequenceは0以上の整数で指定してください")
        if type(self.read_lease_count) is not int or self.read_lease_count < 0:
            raise ValueError("read_lease_countは0以上の整数で指定してください")


def oldest_deletion_candidate(
    candidates: Iterable[DeletionCandidate], current_volume_id: str
) -> DeletionCandidate | None:
    """現在媒体の管理対象確定済みnormal/abnormalから最古を返す。"""
    eligible = [
        candidate
        for candidate in candidates
        if candidate.volume_id == current_volume_id
        and candidate.managed_by_app
        and candidate.state
        in (RecordingFileState.FINALIZED, RecordingFileState.ABNORMAL)
        and candidate.read_lease_count == 0
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            candidate.sort_timestamp_ns
            if candidate.sort_timestamp_ns is not None
            else 2**63 - 1,
            candidate.creation_sequence,
            candidate.recording_id,
        ),
    )

"""録画優先の容量整理use case。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from raspi_recorder.application.recording_access import RecordingAccessService
from raspi_recorder.domain.capacity import (
    CapacityAction,
    CapacityThresholds,
    choose_capacity_action,
)
from raspi_recorder.ports.recording_index import RecordingIndex


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """現在接続中の選択媒体の空き・識別情報。"""

    volume_id: str
    persistent_id: str
    total_bytes: int
    available_bytes: int


class CapacityReader(Protocol):
    """kernel statfs値を現在のmount identityと共に取得する。"""

    def refresh(self, volume_id: str) -> CapacitySnapshot:
        """媒体切断・識別変更を検出し、最新空き容量を返す。"""
        ...


class LowSpaceBlocker(Protocol):
    """録画監督がlow_space blockerだけを操作する境界。"""

    def set_low_space_blocked(self, blocked: bool, reason: str | None = None) -> None:
        """trueで録画を安全停止、falseで解除可能状態へする。"""
        ...


@dataclass(frozen=True, slots=True)
class CapacityResult:
    """1回の容量整理結果。"""

    action: CapacityAction
    available_bytes: int
    deleted_ids: tuple[str, ...]
    warning: str | None


class CapacityManager:
    """強制停止・古い物理file削除・回復判定を直列に行う。"""

    def __init__(
        self,
        index: RecordingIndex,
        access: RecordingAccessService,
        reader: CapacityReader,
        blocker: LowSpaceBlocker,
        *,
        max_deletions_per_cycle: int = 100,
    ) -> None:
        if type(max_deletions_per_cycle) is not int or max_deletions_per_cycle < 1:
            raise ValueError("max_deletions_per_cycleは1以上で指定してください")
        self._index = index
        self._access = access
        self._reader = reader
        self._blocker = blocker
        self._max_deletions_per_cycle = max_deletions_per_cycle
        self._cleanup_active = False
        self._low_space_blocked = False

    def maintain(
        self,
        volume_id: str,
        persistent_id: str,
        thresholds: CapacityThresholds,
    ) -> CapacityResult:
        """現在媒体のstop/start/recover閾値に従って整理する。"""
        if not isinstance(volume_id, str) or not volume_id:
            raise ValueError("volume_idが必要です")
        if not isinstance(persistent_id, str) or not persistent_id:
            raise ValueError("persistent_idが必要です")
        if not isinstance(thresholds, CapacityThresholds):
            raise ValueError("thresholdsが不正です")

        snapshot = self._read_current(volume_id, persistent_id, thresholds)
        action = choose_capacity_action(
            snapshot.available_bytes, thresholds, self._cleanup_active
        )
        if action is CapacityAction.STOP_RECORDING:
            self._set_blocked(True, "low_space_stop_threshold")
            self._cleanup_active = True
        elif action is CapacityAction.CONTINUE:
            self._cleanup_active = False
            self._set_blocked(False)
            return CapacityResult(action, snapshot.available_bytes, (), None)
        else:
            self._cleanup_active = True
        deleted: list[str] = []
        excluded: set[str] = set()
        warning: str | None = None
        for _ in range(self._max_deletions_per_cycle):
            if snapshot.available_bytes >= thresholds.recover_bytes:
                self._cleanup_active = False
                self._set_blocked(False)
                return CapacityResult(
                    CapacityAction.CONTINUE,
                    snapshot.available_bytes,
                    tuple(deleted),
                    None,
                )

            candidate = self._index.oldest_deletion_candidate(
                volume_id, excluded_ids=tuple(sorted(excluded))
            )
            if candidate is None:
                warning = "deletion_candidates_exhausted"
                break
            result = self._access.delete_automatic(candidate)
            if result.deleted:
                deleted.append(candidate)
                snapshot = self._read_current(volume_id, persistent_id, thresholds)
            else:
                excluded.add(candidate)

        if snapshot.available_bytes <= thresholds.stop_bytes:
            self._set_blocked(True, "low_space_stop_threshold")
            return CapacityResult(
                CapacityAction.STOP_RECORDING,
                snapshot.available_bytes,
                tuple(deleted),
                warning,
            )

        if snapshot.available_bytes >= thresholds.recover_bytes:
            self._cleanup_active = False
            self._set_blocked(False)
            return CapacityResult(
                CapacityAction.CONTINUE,
                snapshot.available_bytes,
                tuple(deleted),
                None,
            )

        return CapacityResult(
            (
                CapacityAction.STOP_RECORDING
                if self._low_space_blocked
                else CapacityAction.DELETE_OLDEST
            ),
            snapshot.available_bytes,
            tuple(deleted),
            warning or "deletion_cycle_limit_reached",
        )

    def _set_blocked(self, blocked: bool, reason: str | None = None) -> None:
        if blocked == self._low_space_blocked:
            return
        self._blocker.set_low_space_blocked(blocked, reason)
        self._low_space_blocked = blocked

    def _read_current(
        self,
        volume_id: str,
        persistent_id: str,
        thresholds: CapacityThresholds,
    ) -> CapacitySnapshot:
        snapshot = self._reader.refresh(volume_id)
        if snapshot.volume_id != volume_id or snapshot.persistent_id != persistent_id:
            raise RuntimeError("selected_volume_identity_changed")
        if snapshot.total_bytes != thresholds.volume_bytes:
            raise RuntimeError("capacity_thresholds_require_revalidation")
        if (
            type(snapshot.available_bytes) is not int
            or snapshot.available_bytes < 0
            or snapshot.available_bytes > snapshot.total_bytes
        ):
            raise RuntimeError("invalid_capacity_snapshot")
        return snapshot

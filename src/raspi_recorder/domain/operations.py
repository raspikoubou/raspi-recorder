"""録画切替など、長い操作の段階と許可遷移。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    """再接続後も照会できる設定操作の値。"""

    operation_id: str
    request_id: str
    kind: str
    stage: OperationStage
    target: dict[str, Any]
    previous_settings: dict[str, Any]
    requested_settings: dict[str, Any]
    created_utc_ns: int
    updated_utc_ns: int
    error: dict[str, Any] | None
    recovery: dict[str, Any] | None

    @property
    def status(self) -> str:
        """Web API向けのqueued/running/succeeded/failed状態。"""
        if self.stage is OperationStage.QUEUED:
            return "queued"
        if self.stage is OperationStage.SUCCEEDED:
            return "succeeded"
        if self.stage is OperationStage.FAILED:
            return "failed"
        return "running"


class OperationStage(StrEnum):
    """操作journalへ永続化する段階。"""

    QUEUED = "queued"
    VALIDATING = "validating"
    FINALIZING = "finalizing"
    APPLYING = "applying"
    STARTING = "starting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: dict[OperationStage, frozenset[OperationStage]] = {
    OperationStage.QUEUED: frozenset(
        {OperationStage.VALIDATING, OperationStage.FAILED}
    ),
    OperationStage.VALIDATING: frozenset(
        {
            OperationStage.FINALIZING,
            OperationStage.APPLYING,
            OperationStage.SUCCEEDED,
            OperationStage.FAILED,
        }
    ),
    OperationStage.FINALIZING: frozenset(
        {OperationStage.APPLYING, OperationStage.FAILED}
    ),
    OperationStage.APPLYING: frozenset(
        {OperationStage.STARTING, OperationStage.FAILED}
    ),
    OperationStage.STARTING: frozenset(
        {OperationStage.SUCCEEDED, OperationStage.FAILED}
    ),
    OperationStage.SUCCEEDED: frozenset(),
    OperationStage.FAILED: frozenset(),
}


def validate_operation_transition(
    current: OperationStage, requested: OperationStage
) -> None:
    """許可されないjournal遷移を拒否する。"""
    if not isinstance(current, OperationStage) or not isinstance(
        requested, OperationStage
    ):
        raise ValueError("operation stageが不正です")
    if requested not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"operation stageを{current}から{requested}へ変更できません")

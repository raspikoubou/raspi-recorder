"""設定snapshotの保存・CAS境界。"""

from __future__ import annotations

from typing import Any, Protocol

from raspi_recorder.domain.operations import OperationSnapshot, OperationStage
from raspi_recorder.domain.settings import ApplicationSettings


class SettingsRepository(Protocol):
    """Applicationが必要とする設定永続化操作。"""

    def initialize(self, applied_utc_ns: int) -> None:
        """永続schemaを準備する。"""
        ...

    def load(self) -> ApplicationSettings | None:
        """現在snapshotを返す。未登録ならNone。"""
        ...

    def save(
        self,
        settings: ApplicationSettings,
        *,
        expected_revision: int,
        updated_utc_ns: int,
    ) -> ApplicationSettings:
        """期待revisionと一致する場合だけ保存する。"""
        ...

    def save_default_if_missing(
        self, settings: ApplicationSettings, updated_utc_ns: int
    ) -> ApplicationSettings:
        """初回だけdefaultを作成し、既存値は維持する。"""
        ...

    def create_operation(
        self,
        *,
        operation_id: str,
        request_id: str,
        kind: str,
        target: dict[str, Any],
        previous_settings: dict[str, Any],
        requested_settings: dict[str, Any],
        created_utc_ns: int,
    ) -> OperationSnapshot:
        """操作journalを永続化する。"""
        ...

    def load_operation(self, operation_id: str) -> OperationSnapshot | None:
        """operation IDでjournalを取得する。"""
        ...

    def load_operation_by_request_id(self, request_id: str) -> OperationSnapshot | None:
        """request IDで既存journalを取得する。"""
        ...

    def update_operation_target(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        target: dict[str, Any],
        updated_utc_ns: int,
    ) -> OperationSnapshot:
        """期待段階にある操作のtargetだけをCASで更新する。"""
        ...

    def load_pending_operations(self) -> tuple[OperationSnapshot, ...]:
        """非終端operationを作成順で返す。"""
        ...

    def transition_operation(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        next_stage: OperationStage,
        updated_utc_ns: int,
        *,
        error: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> OperationSnapshot:
        """CASでoperation stageを更新する。"""
        ...

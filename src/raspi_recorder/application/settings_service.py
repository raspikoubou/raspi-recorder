"""設定snapshotの起動初期化とcompare-and-swap use case。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.domain.operations import OperationSnapshot, OperationStage
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.ports.clock import Clock
from raspi_recorder.ports.settings_repository import SettingsRepository


class SettingsService:
    """DBへ保存された設定と期待revisionを一貫して扱う。"""

    def __init__(self, repository: SettingsRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def initialize(
        self, defaults: ApplicationSettings | None = None
    ) -> ApplicationSettings:
        """既存値を上書きせず、初回だけdefault設定を保存する。"""
        self._repository.initialize(self._clock.utc_ns())
        return self._repository.save_default_if_missing(
            ApplicationSettings() if defaults is None else defaults,
            self._clock.utc_ns(),
        )

    def current(self) -> ApplicationSettings:
        """保存済み設定を返す。初期化未実行は明示的なエラーにする。"""
        settings = self._repository.load()
        if settings is None:
            raise RuntimeError("settings serviceをinitializeしてください")
        return settings

    def compare_and_swap(
        self, candidate: ApplicationSettings, expected_revision: int
    ) -> ApplicationSettings:
        """候補をCAS保存する。物理workerへの適用完了は別操作で判定する。"""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revisionは0以上の整数で指定してください")
        if not isinstance(candidate, ApplicationSettings):
            raise ValueError("candidateはApplicationSettingsで指定してください")
        current = self.current()
        if current.revision != expected_revision:
            raise RevisionConflict(expected_revision, current.revision)
        prepared = replace(candidate, revision=expected_revision)
        return self._repository.save(
            prepared,
            expected_revision=expected_revision,
            updated_utc_ns=self._clock.utc_ns(),
        )

    def create_operation(
        self,
        *,
        operation_id: str,
        request_id: str,
        kind: str,
        target: dict[str, Any],
        previous_settings: dict[str, Any],
        requested_settings: dict[str, Any],
    ) -> OperationSnapshot:
        """同一request IDの再送を判定できる操作journalを作る。"""
        return self._repository.create_operation(
            operation_id=operation_id,
            request_id=request_id,
            kind=kind,
            target=target,
            previous_settings=previous_settings,
            requested_settings=requested_settings,
            created_utc_ns=self._clock.utc_ns(),
        )

    def load_operation(self, operation_id: str) -> OperationSnapshot | None:
        """再接続後も使えるjournal snapshotを返す。"""
        return self._repository.load_operation(operation_id)

    def load_operation_by_request_id(self, request_id: str) -> OperationSnapshot | None:
        """request IDに対応する永続journal snapshotを返す。"""
        return self._repository.load_operation_by_request_id(request_id)

    def update_operation_target(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        target: dict[str, Any],
    ) -> OperationSnapshot:
        """非終端操作のtargetを期待stageを照合して差し替える。"""
        return self._repository.update_operation_target(
            operation_id,
            expected_stage,
            target,
            self._clock.utc_ns(),
        )

    def load_pending_operations(self) -> tuple[OperationSnapshot, ...]:
        """再起動前に終端しなかった操作を返す。"""
        return self._repository.load_pending_operations()

    def transition_operation(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        next_stage: OperationStage,
        *,
        error: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> OperationSnapshot:
        """許可遷移だけを現在時刻付きで保存する。"""
        return self._repository.transition_operation(
            operation_id,
            expected_stage,
            next_stage,
            self._clock.utc_ns(),
            error=error,
            recovery=recovery,
        )

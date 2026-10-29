"""domain設定とengine-owned SQLite設定snapshotの境界。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raspi_recorder.adapters.sqlite_store import SqliteStore
from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.domain.operations import OperationSnapshot, OperationStage
from raspi_recorder.domain.settings import ApplicationSettings


class SettingsRepository:
    """設定schemaを検査してからSQLiteへCAS保存する。"""

    def __init__(self, database_path: str | Path) -> None:
        self._store = SqliteStore(database_path)

    def initialize(self, applied_utc_ns: int) -> None:
        """WAL schemaを作成・検査する。"""
        self._store.initialize(applied_utc_ns)

    def load(self) -> ApplicationSettings | None:
        """保存済み設定を復元する。未登録ならNone。"""
        snapshot = self._store.load_settings()
        if snapshot is None:
            return None
        settings = ApplicationSettings.from_saved_payload(snapshot.payload)
        if settings.revision != snapshot.revision:
            raise RuntimeError("settings JSONとSQLite revisionが一致しません")
        if snapshot.schema_version not in (1, 2):
            raise RuntimeError("未対応のsettings schema_versionです")
        return settings

    def save(
        self,
        settings: ApplicationSettings,
        *,
        expected_revision: int,
        updated_utc_ns: int,
    ) -> ApplicationSettings:
        """期待revisionを照合して原子的に保存する。"""
        if not isinstance(settings, ApplicationSettings):
            raise ValueError("settingsはApplicationSettingsで指定してください")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revisionは0以上の整数で指定してください")
        if settings.revision != expected_revision:
            raise ValueError("candidate revisionとexpected_revisionが一致しません")
        saved = self._store.save_settings(
            expected_revision=expected_revision,
            schema_version=2,
            payload=settings.to_payload(),
            updated_utc_ns=updated_utc_ns,
        )
        result = ApplicationSettings.from_payload(saved.payload)
        if result.revision != saved.revision:
            raise RuntimeError("保存後のsettings revisionが一致しません")
        return result

    def save_default_if_missing(
        self, settings: ApplicationSettings, updated_utc_ns: int
    ) -> ApplicationSettings:
        """初回だけdefault設定を作成し、既存設定は上書きしない。"""
        existing = self.load()
        if existing is not None:
            return existing
        try:
            return self.save(
                settings,
                expected_revision=0,
                updated_utc_ns=updated_utc_ns,
            )
        except RevisionConflict:
            concurrent = self.load()
            if concurrent is None:
                raise
            return concurrent

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
        """設定databaseへ操作のrequestと前後snapshotを永続化する。"""
        return self._store.create_operation(
            operation_id=operation_id,
            request_id=request_id,
            kind=kind,
            target=target,
            previous_settings=previous_settings,
            requested_settings=requested_settings,
            created_utc_ns=created_utc_ns,
        )

    def load_operation(self, operation_id: str) -> OperationSnapshot | None:
        """操作IDで永続journalを取得する。"""
        return self._store.load_operation(operation_id)

    def load_operation_by_request_id(self, request_id: str) -> OperationSnapshot | None:
        """request IDで永続journalを取得する。"""
        return self._store.load_operation_by_request_id(request_id)

    def update_operation_target(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        target: dict[str, Any],
        updated_utc_ns: int,
    ) -> OperationSnapshot:
        """期待段階と操作種類を照合してtargetを更新する。"""
        return self._store.update_operation_target(
            operation_id,
            expected_stage,
            target,
            updated_utc_ns,
        )

    def load_pending_operations(self) -> tuple[OperationSnapshot, ...]:
        """未終端operationのstartup reconciliation対象を返す。"""
        return self._store.load_pending_operations()

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
        """journalの段階と障害復帰情報をCAS更新する。"""
        return self._store.transition_operation(
            operation_id,
            expected_stage,
            next_stage,
            updated_utc_ns,
            error=error,
            recovery=recovery,
        )

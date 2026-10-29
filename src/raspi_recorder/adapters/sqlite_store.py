"""設定revisionと操作journalのSQLite永続化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.domain.operations import (
    OperationSnapshot,
    OperationStage,
    validate_operation_transition,
)

# recording_catalogと同様、古いSQLiteのWALリセット問題に備えて
# 接続の生成・利用・closeをprocess内で直列化する。
_STORE_CONNECTION_LOCK = threading.RLock()


class RequestIdConflict(RuntimeError):
    """同じrequest IDが別内容で再利用された。"""


class OperationConflict(RuntimeError):
    """操作が期待段階にない、または許可されない遷移を要求された。"""


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """保存済み設定とrevision。"""

    revision: int
    schema_version: int
    payload: dict[str, Any]
    updated_utc_ns: int


class SqliteStore:
    """engine所有のSQLite DBを短いtransactionで更新する。"""

    _SCHEMA_VERSION = 1
    _BUSY_TIMEOUT_MS = 5_000
    _MAX_PENDING_OPERATIONS = 1_024
    _MAX_TERMINAL_OPERATIONS = 256
    _TERMINAL_OPERATION_STAGES = (
        OperationStage.SUCCEEDED.value,
        OperationStage.FAILED.value,
    )
    _ACTIVE_OPERATION_STAGES = (
        OperationStage.QUEUED.value,
        OperationStage.VALIDATING.value,
        OperationStage.FINALIZING.value,
        OperationStage.APPLYING.value,
        OperationStage.STARTING.value,
    )

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def initialize(self, applied_utc_ns: int) -> None:
        """WALと初期schemaを準備する。既知より新しいDBは拒否する。"""
        self._validate_nonnegative_int("applied_utc_ns", applied_utc_ns)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_utc_ns INTEGER NOT NULL)"
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            current_version = int(row["version"])
            if current_version > self._SCHEMA_VERSION:
                raise RuntimeError(
                    f"DB schema {current_version}はこの版の上限を超えています"
                )
            if current_version == 0:
                self._create_v1(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_utc_ns) "
                    "VALUES(?, ?)",
                    (self._SCHEMA_VERSION, applied_utc_ns),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS operations_stage_created_idx "
                "ON operations(stage, created_utc_ns, operation_id)"
            )
            connection.commit()

    def load_settings(self) -> SettingsSnapshot | None:
        """現在の設定を読み込む。未保存ならNoneを返す。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision, schema_version, settings_json, updated_utc_ns "
                "FROM settings WHERE singleton_key = 1"
            ).fetchone()
        if row is None:
            return None
        return SettingsSnapshot(
            revision=int(row["revision"]),
            schema_version=int(row["schema_version"]),
            payload=self._decode_object(row["settings_json"]),
            updated_utc_ns=int(row["updated_utc_ns"]),
        )

    def save_settings(
        self,
        expected_revision: int,
        schema_version: int,
        payload: dict[str, Any],
        updated_utc_ns: int,
    ) -> SettingsSnapshot:
        """CASで設定を保存し、新しいrevisionを返す。"""
        self._validate_nonnegative_int("expected_revision", expected_revision)
        self._validate_nonnegative_int("schema_version", schema_version)
        if schema_version == 0:
            raise ValueError("schema_versionは1以上で指定してください")
        self._validate_nonnegative_int("updated_utc_ns", updated_utc_ns)
        if not isinstance(payload, dict):
            raise ValueError("settings payloadはobjectで指定してください")
        payload_copy = dict(payload)
        next_payload = {
            **payload_copy,
            "schema_version": schema_version,
            "revision": expected_revision + 1,
        }
        encoded = self._encode_json(next_payload)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM settings WHERE singleton_key = 1"
            ).fetchone()
            actual_revision = 0 if row is None else int(row["revision"])
            if actual_revision != expected_revision:
                connection.rollback()
                raise RevisionConflict(expected_revision, actual_revision)
            new_revision = actual_revision + 1
            next_payload["revision"] = new_revision
            encoded = self._encode_json(next_payload)
            connection.execute(
                "INSERT INTO settings(singleton_key, revision, schema_version, "
                "settings_json, updated_utc_ns) "
                "VALUES(1, ?, ?, ?, ?) "
                "ON CONFLICT(singleton_key) DO UPDATE SET "
                "revision=excluded.revision, schema_version=excluded.schema_version, "
                "settings_json=excluded.settings_json, "
                "updated_utc_ns=excluded.updated_utc_ns",
                (new_revision, schema_version, encoded, updated_utc_ns),
            )
            connection.commit()
        return SettingsSnapshot(
            revision=new_revision,
            schema_version=schema_version,
            payload=next_payload,
            updated_utc_ns=updated_utc_ns,
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
        created_utc_ns: int,
    ) -> OperationSnapshot:
        """journalを作成する。同じrequest ID・同じ内容の再送は冪等。"""
        for name, value in (
            ("operation_id", operation_id),
            ("request_id", request_id),
            ("kind", kind),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}は空でない文字列で指定してください")
        self._validate_nonnegative_int("created_utc_ns", created_utc_ns)
        target_json = self._encode_object("target", target)
        previous_json = self._encode_object("previous_settings", previous_settings)
        requested_json = self._encode_object("requested_settings", requested_settings)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                existing_target = self._decode_object(existing["target_json"])
                target_matches = existing["target_json"] == target_json
                if kind == "update" and existing["kind"] == "update":
                    target_matches = all(
                        existing_target.get(key) == target.get(key)
                        for key in ("scope", "version")
                    )
                same_request = (
                    existing["kind"] == kind
                    and target_matches
                    and existing["requested_settings_json"] == requested_json
                )
                if not same_request:
                    connection.rollback()
                    raise RequestIdConflict("request_idが異なる内容で再利用されました")
                connection.commit()
                return self._operation_from_row(existing)
            try:
                connection.execute(
                    "INSERT INTO operations(operation_id, request_id, kind, stage, "
                    "target_json, previous_settings_json, requested_settings_json, "
                    "created_utc_ns, updated_utc_ns) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        request_id,
                        kind,
                        OperationStage.QUEUED.value,
                        target_json,
                        previous_json,
                        requested_json,
                        created_utc_ns,
                        created_utc_ns,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise OperationConflict(
                    "operation_idが既存操作と重複しています"
                ) from exc
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            self._prune_terminal_operations(connection)
            connection.commit()
        assert row is not None
        return self._operation_from_row(row)

    @classmethod
    def _prune_terminal_operations(cls, connection: sqlite3.Connection) -> None:
        """終端状態のjournalが無制限に増えないよう、直近分だけ残して削除する。

        非終端の行と直近 _MAX_TERMINAL_OPERATIONS 件の終端行は保持する。
        """
        placeholders = ",".join("?" for _ in cls._TERMINAL_OPERATION_STAGES)
        connection.execute(
            f"DELETE FROM operations WHERE stage IN ({placeholders}) "
            "AND operation_id NOT IN ("
            f"SELECT operation_id FROM operations WHERE stage IN ({placeholders}) "
            "ORDER BY created_utc_ns DESC, operation_id DESC LIMIT ?)",
            (
                *cls._TERMINAL_OPERATION_STAGES,
                *cls._TERMINAL_OPERATION_STAGES,
                cls._MAX_TERMINAL_OPERATIONS,
            ),
        )

    def load_operation(self, operation_id: str) -> OperationSnapshot | None:
        """operation IDでjournalを読み込む。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return None if row is None else self._operation_from_row(row)

    def load_operation_by_request_id(self, request_id: str) -> OperationSnapshot | None:
        """request IDでjournalを読み込む。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE request_id = ?", (request_id,)
            ).fetchone()
        return None if row is None else self._operation_from_row(row)

    def update_operation_target(
        self,
        operation_id: str,
        expected_stage: OperationStage,
        target: dict[str, Any],
        updated_utc_ns: int,
    ) -> OperationSnapshot:
        """操作種類とstageを照合してtargetを原子的に更新する。"""
        self._validate_nonnegative_int("updated_utc_ns", updated_utc_ns)
        target_json = self._encode_object("target", target)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(operation_id)
            if (
                row["kind"] != "update"
                or OperationStage(row["stage"]) is not expected_stage
            ):
                connection.rollback()
                raise OperationConflict("update target stage or kind mismatch")
            connection.execute(
                "UPDATE operations SET target_json = ?, updated_utc_ns = ? "
                "WHERE operation_id = ?",
                (target_json, updated_utc_ns, operation_id),
            )
            updated = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            connection.commit()
        assert updated is not None
        return self._operation_from_row(updated)

    def load_pending_operations(self) -> tuple[OperationSnapshot, ...]:
        """engine再起動時の非終端操作を上限付きで返す。"""
        placeholders = ",".join("?" for _ in self._ACTIVE_OPERATION_STAGES)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM operations WHERE stage IN ({placeholders}) "
                "ORDER BY created_utc_ns, operation_id LIMIT ?",
                (
                    *self._ACTIVE_OPERATION_STAGES,
                    self._MAX_PENDING_OPERATIONS + 1,
                ),
            ).fetchall()
        if len(rows) > self._MAX_PENDING_OPERATIONS:
            raise RuntimeError("pending operation journal exceeds safety limit")
        return tuple(self._operation_from_row(row) for row in rows)

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
        """期待段階を照合して操作journalを進める。"""
        self._validate_nonnegative_int("updated_utc_ns", updated_utc_ns)
        try:
            validate_operation_transition(expected_stage, next_stage)
        except ValueError as exc:
            raise OperationConflict(str(exc)) from exc
        error_json = None if error is None else self._encode_object("error", error)
        recovery_json = (
            None if recovery is None else self._encode_object("recovery", recovery)
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(operation_id)
            current_stage = OperationStage(row["stage"])
            if current_stage is not expected_stage:
                connection.rollback()
                raise OperationConflict(
                    f"操作段階競合: expected={expected_stage}, actual={current_stage}"
                )
            connection.execute(
                "UPDATE operations SET stage = ?, updated_utc_ns = ?, error_json = ?, "
                "recovery_json = ? WHERE operation_id = ?",
                (
                    next_stage.value,
                    updated_utc_ns,
                    error_json,
                    recovery_json,
                    operation_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            connection.commit()
        assert updated is not None
        return self._operation_from_row(updated)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=self._BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with _STORE_CONNECTION_LOCK:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    @staticmethod
    def _create_v1(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE settings ("
            "singleton_key INTEGER PRIMARY KEY CHECK(singleton_key = 1),"
            "revision INTEGER NOT NULL CHECK(revision > 0),"
            "schema_version INTEGER NOT NULL CHECK(schema_version > 0),"
            "settings_json TEXT NOT NULL,"
            "updated_utc_ns INTEGER NOT NULL CHECK(updated_utc_ns >= 0)"
            ")"
        )
        connection.execute(
            "CREATE TABLE operations ("
            "operation_id TEXT PRIMARY KEY,"
            "request_id TEXT NOT NULL UNIQUE,"
            "kind TEXT NOT NULL,"
            "stage TEXT NOT NULL CHECK(stage IN "
            "('queued','validating','finalizing','applying',"
            "'starting','succeeded','failed')),"
            "target_json TEXT NOT NULL,"
            "previous_settings_json TEXT NOT NULL,"
            "requested_settings_json TEXT NOT NULL,"
            "created_utc_ns INTEGER NOT NULL CHECK(created_utc_ns >= 0),"
            "updated_utc_ns INTEGER NOT NULL CHECK(updated_utc_ns >= 0),"
            "error_json TEXT,"
            "recovery_json TEXT"
            ")"
        )
        connection.execute(
            "CREATE INDEX operations_created_idx "
            "ON operations(created_utc_ns, operation_id)"
        )

    @staticmethod
    def _validate_nonnegative_int(name: str, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name}は0以上の整数で指定してください")

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _encode_object(cls, name: str, value: dict[str, Any]) -> str:
        if not isinstance(value, dict):
            raise ValueError(f"{name}はJSON objectで指定してください")
        return cls._encode_json(value)

    @staticmethod
    def _decode_object(value: str) -> dict[str, Any]:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("SQLite JSON payloadはobjectである必要があります")
        return parsed

    @classmethod
    def _operation_from_row(cls, row: sqlite3.Row) -> OperationSnapshot:
        return OperationSnapshot(
            operation_id=str(row["operation_id"]),
            request_id=str(row["request_id"]),
            kind=str(row["kind"]),
            stage=OperationStage(row["stage"]),
            target=cls._decode_object(row["target_json"]),
            previous_settings=cls._decode_object(row["previous_settings_json"]),
            requested_settings=cls._decode_object(row["requested_settings_json"]),
            created_utc_ns=int(row["created_utc_ns"]),
            updated_utc_ns=int(row["updated_utc_ns"]),
            error=None
            if row["error_json"] is None
            else cls._decode_object(row["error_json"]),
            recovery=None
            if row["recovery_json"] is None
            else cls._decode_object(row["recovery_json"]),
        )

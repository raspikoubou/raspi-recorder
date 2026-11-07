"""独立した認証保護設定とcredentialをSQLiteへ保存する。"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.ports.authentication import CredentialSnapshot


class AuthenticationStore:
    """credential databaseと旧schemaの安全な移行を管理する。"""

    _BUSY_TIMEOUT_MS = 5_000
    _MIGRATION_BACKUP_SUFFIX = ".pre_optional_auth_migration.sqlite3"

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)

    def initialize(self) -> bool:
        """DBを準備する。旧schemaはbackup後transaction内で移行する。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._needs_flag_migration():
            self._backup_before_migration()

        created = False
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='authentication'"
            ).fetchone()
            if table is None:
                connection.execute(
                    "CREATE TABLE authentication ("
                    "singleton_key INTEGER PRIMARY KEY CHECK(singleton_key=1), "
                    "revision INTEGER NOT NULL CHECK(revision>=0), "
                    "login_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(login_enabled IN (0,1)), "
                    "admin_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(admin_enabled IN (0,1)), "
                    "login_password_hash TEXT, admin_password_hash TEXT)"
                )
                created = True
            else:
                columns = self._columns(connection)
                legacy_flags = (
                    "login_enabled" not in columns or "admin_enabled" not in columns
                )
                if "login_password_hash" not in columns:
                    connection.execute(
                        "ALTER TABLE authentication ADD COLUMN login_password_hash TEXT"
                    )
                    columns.add("login_password_hash")
                    if "password_hash" in columns:
                        connection.execute(
                            "UPDATE authentication SET login_password_hash="
                            "CASE WHEN password_hash IS NOT NULL "
                            "AND password_hash!='disabled' THEN password_hash END "
                            "WHERE login_password_hash IS NULL"
                        )
                if "admin_password_hash" not in columns:
                    connection.execute(
                        "ALTER TABLE authentication ADD COLUMN admin_password_hash TEXT"
                    )
                    columns.add("admin_password_hash")
                if "login_enabled" not in columns:
                    connection.execute(
                        "ALTER TABLE authentication ADD COLUMN login_enabled "
                        "INTEGER NOT NULL DEFAULT 0 CHECK(login_enabled IN (0,1))"
                    )
                    columns.add("login_enabled")
                if "admin_enabled" not in columns:
                    connection.execute(
                        "ALTER TABLE authentication ADD COLUMN admin_enabled "
                        "INTEGER NOT NULL DEFAULT 0 CHECK(admin_enabled IN (0,1))"
                    )
                    columns.add("admin_enabled")

                if legacy_flags:
                    if "enabled" in columns:
                        connection.execute(
                            "UPDATE authentication SET login_enabled=CASE "
                            "WHEN login_password_hash IS NOT NULL AND enabled=1 "
                            "THEN 1 ELSE 0 END"
                        )
                    else:
                        connection.execute(
                            "UPDATE authentication SET login_enabled=CASE "
                            "WHEN login_password_hash IS NOT NULL THEN 1 ELSE 0 END"
                        )
                    # 旧版では管理者操作が常時保護されていた。hashを引き継ぎ、
                    # 旧未登録rowだけは両方OFFで通常利用へ移行する。
                    connection.execute(
                        "UPDATE authentication SET "
                        "admin_password_hash=login_password_hash "
                        "WHERE admin_password_hash IS NULL "
                        "AND login_password_hash IS NOT NULL"
                    )
                    connection.execute(
                        "UPDATE authentication SET admin_enabled=CASE "
                        "WHEN admin_password_hash IS NOT NULL "
                        "OR login_password_hash IS NOT NULL THEN 1 ELSE 0 END"
                    )
                    connection.execute(
                        "UPDATE authentication SET revision=revision+1 "
                        "WHERE singleton_key=1"
                    )

                if "setup_token_digest" in columns:
                    connection.execute(
                        "UPDATE authentication SET setup_token_digest=NULL "
                        "WHERE setup_token_digest IS NOT NULL"
                    )

            cursor = connection.execute(
                "INSERT OR IGNORE INTO authentication("
                "singleton_key, revision, login_enabled, admin_enabled, "
                "login_password_hash, admin_password_hash) "
                "VALUES(1,0,0,0,NULL,NULL)"
            )
            created = created or cursor.rowcount == 1
            connection.commit()
        os.chmod(self._path, 0o600)
        return created

    def load(self) -> CredentialSnapshot:
        """現在の保護設定を返す。hashはWeb応答へ含めない。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision, login_enabled, admin_enabled, "
                "login_password_hash, admin_password_hash "
                "FROM authentication WHERE singleton_key=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("authentication storeをinitializeしてください")
        return CredentialSnapshot(
            revision=int(row["revision"]),
            login_enabled=bool(row["login_enabled"]),
            admin_enabled=bool(row["admin_enabled"]),
            login_password_hash=self._optional_text(row["login_password_hash"]),
            admin_password_hash=self._optional_text(row["admin_password_hash"]),
        )

    def update_protections(
        self,
        *,
        login_enabled: bool,
        admin_enabled: bool,
        login_password_hash: str | None,
        admin_password_hash: str | None,
        expected_revision: int,
    ) -> CredentialSnapshot:
        """両保護と入力されたhashだけを競合検査付きで更新する。"""
        if type(login_enabled) is not bool or type(admin_enabled) is not bool:
            raise ValueError("保護設定はboolで指定してください")
        self._validate_revision(expected_revision)
        if login_password_hash is not None:
            self._validate_hash(login_password_hash)
        if admin_password_hash is not None:
            self._validate_hash(admin_password_hash)

        assignments = ["login_enabled=?", "admin_enabled=?", "revision=revision+1"]
        values: list[object] = [int(login_enabled), int(admin_enabled)]
        if login_password_hash is not None:
            assignments.append("login_password_hash=?")
            values.append(login_password_hash)
        if admin_password_hash is not None:
            assignments.append("admin_password_hash=?")
            values.append(admin_password_hash)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = self._columns(connection)
            if "enabled" in columns:
                assignments.append("enabled=?")
                values.append(int(login_enabled))
            if login_password_hash is not None and "password_hash" in columns:
                assignments.append("password_hash=?")
                values.append(login_password_hash)
            cursor = connection.execute(
                "UPDATE authentication SET "
                + ", ".join(assignments)
                + " WHERE singleton_key=1 AND revision=? "
                "AND (?=0 OR COALESCE(?,login_password_hash) IS NOT NULL) "
                "AND (?=0 OR COALESCE(?,admin_password_hash) IS NOT NULL)",
                (
                    *values,
                    expected_revision,
                    int(login_enabled),
                    login_password_hash,
                    int(admin_enabled),
                    admin_password_hash,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_revision_or_invalid_state(connection, expected_revision)
            connection.commit()
        return self.load()

    def reset_for_local_setup(self) -> CredentialSnapshot:
        """認証情報・session権限だけを消し、保護OFFへ戻す。"""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = self._columns(connection)
            assignments = [
                "revision=revision+1",
                "login_enabled=0",
                "admin_enabled=0",
                "login_password_hash=NULL",
                "admin_password_hash=NULL",
            ]
            if "enabled" in columns:
                assignments.append("enabled=0")
            if "password_hash" in columns:
                # 古いNOT NULL列が残るDBでもresetできるよう無効値に置き換える。
                assignments.append("password_hash='disabled'")
            cursor = connection.execute(
                "UPDATE authentication SET "
                + ", ".join(assignments)
                + " WHERE singleton_key=1"
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("authentication storeをinitializeしてください")
            if "setup_token_digest" in columns:
                connection.execute(
                    "UPDATE authentication SET setup_token_digest=NULL "
                    "WHERE singleton_key=1"
                )
            connection.commit()
        return self.load()

    def _needs_flag_migration(self) -> bool:
        if not self._path.exists():
            return False
        try:
            with closing(sqlite3.connect(self._path)) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='authentication'"
                ).fetchone()
                if table is None:
                    return False
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(authentication)")
                }
        except sqlite3.DatabaseError:
            return False
        return "login_enabled" not in columns or "admin_enabled" not in columns

    def _backup_before_migration(self) -> None:
        backup_path = self._path.with_name(
            self._path.name + self._MIGRATION_BACKUP_SUFFIX
        )
        if backup_path.exists() or backup_path.is_symlink():
            info = backup_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise RuntimeError(
                    "認証DBのmigration backupが安全なregular fileではありません"
                )
            try:
                with closing(sqlite3.connect(backup_path)) as existing:
                    check = existing.execute("PRAGMA quick_check").fetchone()
                    table = existing.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='authentication'"
                    ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    "既存の認証DB migration backupを検証できません"
                ) from exc
            if check is None or check[0] != "ok" or table is None:
                raise RuntimeError("既存の認証DB migration backupが不正です")
            return

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self._path.name}.migration-",
                suffix=".tmp",
                dir=self._path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            with (
                closing(sqlite3.connect(self._path)) as source,
                closing(sqlite3.connect(temporary_path)) as target,
            ):
                source.backup(target)
            os.chmod(temporary_path, 0o600)
            os.link(temporary_path, backup_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _columns(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(authentication)")
        }

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=self._BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return None if value is None else str(value)

    @classmethod
    def _raise_revision_or_invalid_state(
        cls, connection: sqlite3.Connection, expected_revision: int
    ) -> None:
        current = connection.execute(
            "SELECT revision FROM authentication WHERE singleton_key=1"
        ).fetchone()
        connection.rollback()
        if current is None:
            raise RuntimeError("authentication storeをinitializeしてください")
        actual_revision = int(current["revision"])
        if actual_revision != expected_revision:
            raise RevisionConflict(expected_revision, actual_revision)
        raise ValueError("保護がONの場合は対応するパスワードを登録してください")

    @staticmethod
    def _validate_revision(value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("expected_revisionは0以上の整数が必要です")

    @staticmethod
    def _validate_hash(value: str) -> None:
        if not isinstance(value, str) or not value.startswith("scrypt:"):
            raise ValueError("password_hashはWerkzeug scrypt形式が必要です")

"""認証credentialとpassword hashing境界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CredentialSnapshot:
    """Webへは返さない認証状態。"""

    revision: int
    login_enabled: bool
    admin_enabled: bool
    login_password_hash: str | None
    admin_password_hash: str | None


class CredentialRepository(Protocol):
    """2種類のcredentialを原子的に保存する。"""

    def initialize(self) -> bool:
        """DBと初期rowを作り、新規作成ならTrueを返す。"""
        ...

    def load(self) -> CredentialSnapshot:
        """現在stateを返す。"""
        ...

    def update_protections(
        self,
        *,
        login_enabled: bool,
        admin_enabled: bool,
        login_password_hash: str | None,
        admin_password_hash: str | None,
        expected_revision: int,
    ) -> CredentialSnapshot:
        """二つの保護設定と指定passwordをrevision付きで保存する。"""
        ...

    def reset_for_local_setup(self) -> CredentialSnapshot:
        """認証だけを初回設定へ戻し、録画DBへ触れない。"""
        ...


class PasswordHasher(Protocol):
    """パスワードの一方向hash実装。"""

    def hash(self, password: str) -> str:
        """salt付きpassword hashを作る。"""
        ...

    def verify(self, password_hash: str, password: str) -> bool:
        """平文passwordをhashと照合する。"""
        ...

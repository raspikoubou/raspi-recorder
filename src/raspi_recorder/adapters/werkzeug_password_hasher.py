"""Werkzeug scrypt password hashing adapter。"""

from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash


class WerkzeugPasswordHasher:
    """salt付きscryptを利用する。"""

    def hash(self, password: str) -> str:
        return generate_password_hash(password, method="scrypt")

    def verify(self, password_hash: str, password: str) -> bool:
        return check_password_hash(password_hash, password)

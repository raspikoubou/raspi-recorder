"""Webと録画engine間の小さなJSON control plane。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class EngineResponse:
    """engine HTTP-like statusとJSON result。"""

    status_code: int
    body: dict[str, Any]


@dataclass(slots=True)
class EngineStreamResponse:
    """engine-owned file leaseを保つbinary response。"""

    status_code: int
    headers: dict[str, str]
    handle: BinaryIO
    close: Any


class EngineGateway(Protocol):
    """Web adapterからの許可された操作をengineへ渡す。"""

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> EngineResponse:
        """認証済みcontrol-plane requestを実行する。"""
        ...

    def stream(
        self,
        method: str,
        path: str,
        *,
        range_header: str | None = None,
    ) -> Any:
        """MP4をbounded streamとして転送する。"""
        ...


class EngineRequestHandler(Protocol):
    """Unix socket server内でHTTP-like requestを処理する。"""

    def handle(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> EngineResponse:
        """許可済みrouteを処理してJSON resultを返す。"""
        ...

    def stream(
        self, method: str, path: str, range_header: str | None = None
    ) -> EngineResponse | EngineStreamResponse:
        """許可済み録画media requestをengine-owned leaseで返す。"""
        ...

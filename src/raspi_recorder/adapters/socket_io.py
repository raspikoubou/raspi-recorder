"""小さなhelper応答を、サイズと合計時間の両方を制限して読む。"""

from __future__ import annotations

import socket
import time


def set_deadline_timeout(client: socket.socket, deadline: float) -> None:
    """次のsocket操作へ、合計時間上限までの残り時間を設定する。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("helper_response_deadline")
    client.settimeout(remaining)


def receive_line(client: socket.socket, maximum: int, *, deadline: float) -> bytes:
    """改行を含む最初の行を返す。不完全／過大な応答の判断は呼び出し側で行う。"""
    response = bytearray()
    while len(response) <= maximum:
        set_deadline_timeout(client, deadline)
        chunk = client.recv(min(4096, maximum + 1 - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        newline = response.find(b"\n")
        if newline >= 0:
            return bytes(response[: newline + 1])
    return bytes(response)

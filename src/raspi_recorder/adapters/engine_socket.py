"""制限したUnix domain socket JSON transport。"""

from __future__ import annotations

import json
import os
import pwd
import socket
import socketserver
import stat
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from raspi_recorder.ports.engine import (
    EngineRequestHandler,
    EngineResponse,
    EngineStreamResponse,
)

_MAX_REQUEST_BYTES = 1_048_576
_MAX_RESPONSE_BYTES = 4_194_304
_MAX_STREAM_RESPONSE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_CONTROL_HANDLERS = 12
_MAX_STREAM_HANDLERS = 4
_REQUEST_QUEUE_SIZE = 16
_REQUEST_READ_TIMEOUT_SECONDS = 2.0
_REQUEST_READ_DEADLINE_SECONDS = 5.0
_RESPONSE_WRITE_TIMEOUT_SECONDS = 15.0
_STREAM_SLOT_WAIT_SECONDS = 0.1
_HANDLER_EXECUTION_DEADLINE_SECONDS = 60.0
_PEER_USER_NAMES = ("raspi-recorder", "raspi-recorder-web")


class EngineSocketError(RuntimeError):
    """engineとのcontrol socket通信失敗。"""


@dataclass(slots=True)
class EngineSocketStream:
    """Unix socketからcontent-lengthぶんを読むstreaming response。"""

    status_code: int
    headers: dict[str, str]
    _socket: socket.socket
    _remaining: int
    _closed: bool = False
    _initial: bytes = b""

    def chunks(self, chunk_size: int = 65_536) -> Iterator[bytes]:
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError("chunk_sizeは1以上の整数が必要です")
        try:
            while self._initial and self._remaining:
                chunk = self._initial[: self._remaining]
                self._initial = self._initial[len(chunk) :]
                self._remaining -= len(chunk)
                yield chunk
            while self._remaining:
                chunk = self._socket.recv(min(chunk_size, self._remaining))
                if not chunk:
                    raise EngineSocketError("engine_stream_incomplete")
                self._remaining -= len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()


class EngineSocketClient:
    """接続ごとに1 JSON-line requestを送るclient。"""

    def __init__(self, socket_path: str | Path, timeout_seconds: float = 55.0) -> None:
        self._socket_path = Path(socket_path)
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは0より大きい数値が必要です")
        self._timeout = float(timeout_seconds)

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> EngineResponse:
        _validate_request(method, path, body)
        payload = json.dumps(
            {
                "request_id": uuid.uuid4().hex,
                "method": method,
                "path": path,
                "body": body,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ValueError("engine requestが最大サイズを超えています")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(str(self._socket_path))
                _send_request(client, payload)
                deadline = time.monotonic() + self._timeout
                response = _readline_limited(
                    client, _MAX_RESPONSE_BYTES, deadline=deadline
                )
        except (OSError, TimeoutError) as exc:
            raise EngineSocketError("engine_socket_unavailable") from exc
        return _decode_response(response)

    def stream(
        self,
        method: str,
        path: str,
        *,
        range_header: str | None = None,
    ) -> EngineSocketStream | EngineResponse:
        """録画をJSON/base64化せず、bounded chunkで転送する。"""
        _validate_request(method, path, None)
        _validate_range_header(range_header)
        payload = json.dumps(
            {
                "request_id": uuid.uuid4().hex,
                "method": method,
                "path": path,
                "body": None,
                "range": range_header,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(self._timeout)
            client.connect(str(self._socket_path))
            _send_request(client, payload)
            deadline = time.monotonic() + self._timeout
            response, initial = _readline_buffered(
                client, _MAX_RESPONSE_BYTES, deadline=deadline
            )
            decoded = json.loads(response)
            if isinstance(decoded, dict) and set(decoded) == {
                "status_code",
                "body",
            }:
                status = decoded["status_code"]
                body = decoded["body"]
                if (
                    type(status) is int
                    and 100 <= status <= 599
                    and isinstance(body, dict)
                ):
                    client.close()
                    return EngineResponse(status, body)
                raise EngineSocketError("engine_stream_invalid_error_header")
            if not isinstance(decoded, dict) or set(decoded) != {
                "status_code",
                "headers",
                "content_length",
                "body",
            }:
                raise EngineSocketError("engine_stream_invalid_header")
            status = decoded["status_code"]
            headers = decoded["headers"]
            length = decoded["content_length"]
            body = decoded["body"]
            if type(status) is not int or not 100 <= status <= 599:
                raise EngineSocketError("engine_stream_invalid_status")
            if body is not None:
                if not isinstance(body, dict) or not isinstance(headers, dict):
                    raise EngineSocketError("engine_stream_invalid_error_body")
                client.close()
                return EngineResponse(status, body)
            if (
                not isinstance(headers, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                )
                or type(length) is not int
                or length < 0
                or length > _MAX_STREAM_RESPONSE_BYTES
            ):
                raise EngineSocketError("engine_stream_invalid_fields")
            client.settimeout(60.0)
            return EngineSocketStream(status, headers, client, length, False, initial)
        except (OSError, TimeoutError, json.JSONDecodeError, EngineSocketError) as exc:
            client.close()
            if isinstance(exc, EngineSocketError):
                raise
            raise EngineSocketError("engine_stream_unavailable") from exc


def _send_request(client: socket.socket, payload: bytes) -> None:
    """An overloaded server can respond and close before the request is sent."""
    try:
        client.sendall(payload + b"\n")
    except (BrokenPipeError, ConnectionResetError):
        # The server may already have queued its bounded busy response. Read
        # it before deciding that the connection is unavailable.
        pass


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = _REQUEST_QUEUE_SIZE

    def __init__(self, socket_path: str, handler: EngineRequestHandler) -> None:
        self.request_handler = handler
        self._control_slots = threading.BoundedSemaphore(_MAX_CONTROL_HANDLERS)
        self._stream_slots = threading.BoundedSemaphore(_MAX_STREAM_HANDLERS)
        self._request_lock = threading.Lock()
        self._control_requests: set[socket.socket] = set()
        self._active_requests: set[socket.socket] = set()
        self._handler_threads: set[threading.Thread] = set()
        super().__init__(socket_path, _EngineRequestHandler, bind_and_activate=False)

    def get_request(self) -> tuple[socket.socket, str]:
        request, address = super().get_request()
        request.settimeout(_REQUEST_READ_TIMEOUT_SECONDS)
        return request, address

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: str,
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("Unix stream server received a non-socket request")
        if not self._control_slots.acquire(blocking=False):
            _send_busy_response(request)
            self.shutdown_request(request)
            return
        with self._request_lock:
            self._control_requests.add(request)
            self._active_requests.add(request)
        thread = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name="raspi-recorder-engine-handler",
            daemon=True,
        )
        with self._request_lock:
            self._handler_threads.add(thread)
        try:
            thread.start()
        except Exception:
            self._release_control_slot(request)
            with self._request_lock:
                self._active_requests.discard(request)
                self._handler_threads.discard(thread)
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: str,
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("Unix stream server received a non-socket request")
        thread = threading.current_thread()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_control_slot(request)
            with self._request_lock:
                self._active_requests.discard(request)
                self._handler_threads.discard(thread)

    def _release_control_slot(self, request: socket.socket) -> None:
        with self._request_lock:
            reserved = request in self._control_requests
            self._control_requests.discard(request)
        if reserved:
            self._control_slots.release()

    def acquire_stream_slot(self) -> bool:
        return self._stream_slots.acquire(timeout=_STREAM_SLOT_WAIT_SECONDS)

    def release_stream_slot(self) -> None:
        self._stream_slots.release()

    def release_control_slot(self, request: socket.socket) -> None:
        self._release_control_slot(request)

    def close_active_requests(self) -> None:
        with self._request_lock:
            requests = tuple(self._active_requests)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass

    def wait_for_handlers(self, deadline: float) -> tuple[str, ...]:
        current = threading.current_thread()
        while True:
            with self._request_lock:
                threads = tuple(
                    thread for thread in self._handler_threads if thread is not current
                )
            if not threads:
                return ()
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            with self._request_lock:
                remaining = tuple(
                    thread
                    for thread in self._handler_threads
                    if thread is not current and thread.is_alive()
                )
            alive = tuple(thread.name for thread in remaining)
            if alive:
                return alive
            return ()


def _send_busy_response(request: socket.socket) -> None:
    data = json.dumps(
        {
            "status_code": 503,
            "body": {"error": {"code": "engine_busy"}},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        request.settimeout(0.1)
        request.sendall(data + b"\n")
    except OSError:
        return


class _EngineRequestHandler(socketserver.StreamRequestHandler):
    server: _ThreadedUnixServer

    def handle(self) -> None:
        watchdog = threading.Timer(
            _HANDLER_EXECUTION_DEADLINE_SECONDS, self._expire_request
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            if not _peer_authorized(self.request):
                self._send_error(403, "engine_peer_not_authorized")
                return
            request_bytes = _read_request_line(
                self.request,
                _MAX_REQUEST_BYTES,
                idle_timeout_seconds=_REQUEST_READ_TIMEOUT_SECONDS,
                deadline_seconds=_REQUEST_READ_DEADLINE_SECONDS,
            )
            if (
                not request_bytes.endswith(b"\n")
                or len(request_bytes) > _MAX_REQUEST_BYTES
            ):
                self._send_error(400, "invalid_request_size")
                return
            payload = json.loads(request_bytes)
            if not isinstance(payload, dict) or not {
                "request_id",
                "method",
                "path",
                "body",
            }.issubset(payload):
                raise ValueError("invalid_request_shape")
            request_id = payload["request_id"]
            if not isinstance(request_id, str) or len(request_id) > 80:
                raise ValueError("invalid_request_id")
            method, path, body = payload["method"], payload["path"], payload["body"]
            _validate_request(method, path, body)
            if "range" in payload:
                if set(payload) != {
                    "request_id",
                    "method",
                    "path",
                    "body",
                    "range",
                }:
                    raise ValueError("invalid_stream_request_shape")
                range_header = payload["range"]
                _validate_range_header(range_header)
                if not self.server.acquire_stream_slot():
                    self._send_stream_busy()
                    return
                self.server.release_control_slot(self.request)
                # stream転送は書込みごとのtimeoutで有界なので、
                # handler実行watchdogの対象からは外す。
                watchdog.cancel()
                try:
                    self.request.settimeout(_RESPONSE_WRITE_TIMEOUT_SECONDS)
                    response = self.server.request_handler.stream(
                        method, path, range_header
                    )
                    if isinstance(response, EngineStreamResponse):
                        try:
                            try:
                                content_length = int(response.headers["Content-Length"])
                            except (KeyError, TypeError, ValueError):
                                self._send_error(500, "invalid_stream_length")
                                return
                            if not 0 <= content_length <= _MAX_STREAM_RESPONSE_BYTES:
                                self._send_error(413, "stream_response_too_large")
                                return
                            encoded = json.dumps(
                                {
                                    "status_code": response.status_code,
                                    "headers": response.headers,
                                    "content_length": content_length,
                                    "body": None,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            if len(encoded) > _MAX_RESPONSE_BYTES:
                                self._send_error(500, "stream_header_too_large")
                                return
                            self.wfile.write(encoded + b"\n")
                            sent = 0
                            while sent < content_length:
                                chunk = response.handle.read(
                                    min(65_536, content_length - sent)
                                )
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                sent += len(chunk)
                        finally:
                            response.close()
                        return
                    encoded = json.dumps(
                        {
                            "status_code": response.status_code,
                            "headers": {},
                            "content_length": 0,
                            "body": response.body,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.wfile.write(encoded + b"\n")
                    return
                finally:
                    self.server.release_stream_slot()
            if set(payload) != {"request_id", "method", "path", "body"}:
                raise ValueError("invalid_request_shape")
            self.request.settimeout(_RESPONSE_WRITE_TIMEOUT_SECONDS)
            response = self.server.request_handler.handle(method, path, body)
            encoded = json.dumps(
                {"status_code": response.status_code, "body": response.body},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > _MAX_RESPONSE_BYTES:
                self._send_error(500, "response_too_large")
            else:
                self.wfile.write(encoded + b"\n")
        except TimeoutError:
            self._send_error(408, "request_timeout")
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send_error(400, "invalid_request")
        except Exception:
            self._send_error(500, "engine_request_failed")
        finally:
            watchdog.cancel()

    def _expire_request(self) -> None:
        """handler実行が上限を超えたら接続を切り、control slotのleaseを解放する。

        handler thread自体は止められないが、socketを閉じれば応答書込みは
        失敗して終了に向かい、slotはmembership確認付きで解放されるため
        後続requestが恒常的にengine_busyになることはない。
        """
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.request.close()
        except OSError:
            pass
        self.server.release_control_slot(self.request)

    def _send_error(self, status_code: int, code: str) -> None:
        data = json.dumps(
            {"status_code": status_code, "body": {"error": {"code": code}}},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self.wfile.write(data + b"\n")
        except OSError:
            # A peer may close before sending a full request (the server uses
            # this pattern to probe an existing socket) or while an error is
            # being returned. Do not turn that normal disconnect into a
            # traceback from a request-handler thread.
            return

    def _send_stream_busy(self) -> None:
        data = json.dumps(
            {
                "status_code": 503,
                "headers": {},
                "content_length": 0,
                "body": {"error": {"code": "engine_busy"}},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self.wfile.write(data + b"\n")
        except OSError:
            return


class EngineSocketServer:
    """0660 socketを作り、固定route handlerを並行処理する。"""

    def __init__(self, socket_path: str | Path, handler: EngineRequestHandler) -> None:
        self._path = Path(socket_path)
        self._handler = handler
        self._server: _ThreadedUnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """active serverを壊さず、stale socketだけを差し替える。"""
        if self._server is not None:
            raise RuntimeError("engine socket serverは既に起動しています")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        parent_info = os.lstat(self._path.parent)
        if not stat.S_ISDIR(parent_info.st_mode):
            raise RuntimeError("engine socket directoryがdirectoryではありません")
        if self._path.exists() or self._path.is_symlink():
            info = self._path.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise FileExistsError("engine socket pathがsocketではありません")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self._path))
            except ConnectionRefusedError:
                self._path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError("既存engine socketの停止を確認できません") from exc
            else:
                raise RuntimeError("別のengine socket serverが稼働しています")
            finally:
                probe.close()
        server = _ThreadedUnixServer(str(self._path), self._handler)
        bound = False
        try:
            server.server_bind()
            bound = True
            os.chmod(self._path, 0o660)
            server.server_activate()
        except Exception:
            server.server_close()
            if bound:
                self._path.unlink(missing_ok=True)
            raise
        self._server = server
        thread = threading.Thread(
            target=server.serve_forever,
            args=(0.05,),
            name="raspi-recorder-engine-socket",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def close(self, timeout_seconds: float = 5.0) -> None:
        """serverを停止しsocket inodeを片付ける。"""
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        server = self._server
        if server is None:
            return
        deadline = time.monotonic() + float(timeout_seconds)
        server.shutdown()
        server.server_close()
        server.close_active_requests()
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise TimeoutError("engine_socket_server_stop_timeout")
        live_handlers = server.wait_for_handlers(deadline)
        if live_handlers:
            raise TimeoutError(
                "engine_socket_handler_stop_timeout: " + ", ".join(live_handlers)
            )
        self._path.unlink(missing_ok=True)
        self._server = None
        self._thread = None

    def __enter__(self) -> EngineSocketServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _validate_request(method: object, path: object, body: object) -> None:
    if not isinstance(method, str) or method not in {
        "GET",
        "POST",
        "PATCH",
        "DELETE",
    }:
        raise ValueError("engine methodが不正です")
    if not isinstance(path, str) or len(path) > 1024:
        raise ValueError("engine pathが不正です")
    parsed = urlsplit(path)
    if (
        not parsed.path.startswith("/api/v1/")
        or parsed.netloc
        or parsed.fragment
        or "\\" in path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("engine pathは許可prefixの絶対API pathが必要です")
    if body is not None and not isinstance(body, dict):
        raise ValueError("engine bodyはJSON objectまたはnullが必要です")


def _read_request_line(
    client: socket.socket,
    limit: int,
    *,
    idle_timeout_seconds: float,
    deadline_seconds: float,
) -> bytes:
    """1 JSON lineをサイズ・無通信時間・合計時間の上限付きで読む。"""
    deadline = time.monotonic() + deadline_seconds
    request = bytearray()
    while len(request) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("engine_request_deadline_exceeded")
        client.settimeout(min(idle_timeout_seconds, remaining))
        chunk = client.recv(min(4096, limit + 1 - len(request)))
        if not chunk:
            return bytes(request)
        request.extend(chunk)
        newline = request.find(b"\n")
        if newline >= 0:
            return bytes(request[: newline + 1])
    return bytes(request)


def _validate_range_header(range_header: object) -> None:
    if range_header is not None and (
        not isinstance(range_header, str)
        or len(range_header) > 128
        or not range_header.startswith("bytes=")
    ):
        raise ValueError("Range headerが不正です")


def _peer_authorized(request: socket.socket) -> bool:
    """SO_PEERCREDで接続元uidを確認する。

    socket fileのmode（0660/group）だけではgroupを共有する別service
    userからも接続できるため、update_helperと同様に接続元uid自体を
    検証する。許可するのはroot、server自身のuser、engine/webの
    service userだけ。
    """
    try:
        credentials = request.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("iII"),
        )
    except OSError:
        return False
    _pid, uid, _gid = struct.unpack("iII", credentials)
    if uid == 0 or uid == os.getuid():
        return True
    for name in _PEER_USER_NAMES:
        try:
            if pwd.getpwnam(name).pw_uid == uid:
                return True
        except KeyError:
            continue
    return False


def _apply_read_deadline(client: socket.socket, deadline: float | None) -> None:
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("engine_response_deadline_exceeded")
    client.settimeout(remaining)


def _readline_limited(
    client: socket.socket, limit: int, *, deadline: float | None = None
) -> bytes:
    chunks = bytearray()
    while len(chunks) <= limit:
        _apply_read_deadline(client, deadline)
        chunk = client.recv(min(65_536, limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        newline = chunks.find(b"\n")
        if newline >= 0:
            return bytes(chunks[:newline])
    raise EngineSocketError("engine_response_too_large_or_incomplete")


def _readline_buffered(
    client: socket.socket, limit: int, *, deadline: float | None = None
) -> tuple[bytes, bytes]:
    """header行と、行の後に先読みしたbody先頭bytesを返す。

    1 byteずつのrecvを避けつつ、stream bodyのbytesを破棄しないため、
    改行以降は呼び出し側（EngineSocketStream）へ引き渡す。
    """
    chunks = bytearray()
    while len(chunks) <= limit:
        _apply_read_deadline(client, deadline)
        chunk = client.recv(min(65_536, limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        newline = chunks.find(b"\n")
        if newline >= 0:
            return bytes(chunks[:newline]), bytes(chunks[newline + 1 :])
    raise EngineSocketError("engine_response_too_large_or_incomplete")


def _decode_response(payload: bytes) -> EngineResponse:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineSocketError("engine_response_invalid_json") from exc
    if not isinstance(value, dict) or set(value) != {"status_code", "body"}:
        raise EngineSocketError("engine_response_invalid_shape")
    status_code, body = value["status_code"], value["body"]
    if (
        type(status_code) is not int
        or not 100 <= status_code <= 599
        or not isinstance(body, dict)
    ):
        raise EngineSocketError("engine_response_invalid_fields")
    return EngineResponse(status_code=status_code, body=body)

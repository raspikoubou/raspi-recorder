from __future__ import annotations

import json
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from raspi_recorder.adapters.engine_socket import (
    EngineSocketClient,
    EngineSocketError,
    EngineSocketServer,
    _read_request_line,
)
from raspi_recorder.ports.engine import EngineResponse, EngineStreamResponse


class FakeHandler:
    def __init__(self) -> None:
        self.calls = []

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        return EngineResponse(202, {"operation_id": "op-1", "stage": "queued"})


class FakeStreamHandler(FakeHandler):
    def __init__(self, handle) -> None:
        super().__init__()
        self.handle_to_stream = handle

    def stream(self, method, path, range_header=None):
        return EngineStreamResponse(
            200,
            {
                "Content-Type": "video/mp4",
                "Content-Length": str(self.handle_to_stream.stat().st_size),
                "Accept-Ranges": "bytes",
            },
            self.handle_to_stream.open("rb"),
            lambda: None,
        )


class CloseTrackingStreamHandler(FakeHandler):
    def __init__(self, media: Path) -> None:
        super().__init__()
        self.closed = threading.Event()
        self.media = media

    def stream(self, method, path, range_header=None):
        return EngineStreamResponse(
            200,
            {
                "Content-Type": "video/mp4",
                "Content-Length": "1",
                "X-Large": "x" * 256,
            },
            self.media.open("rb"),
            self.closed.set,
        )


class EngineSocketTests(unittest.TestCase):
    def test_busy_response_can_be_read_after_send_races_server_close(self):
        response = b'{"status_code":503,"body":{"error":{"code":"engine_busy"}}}\n'
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                client_socket = MagicMock()
                client_socket.__enter__.return_value = client_socket
                client_socket.sendall.side_effect = BrokenPipeError()
                client_socket.recv.return_value = response
                with patch(
                    "raspi_recorder.adapters.engine_socket.socket.socket",
                    return_value=client_socket,
                ):
                    client = EngineSocketClient("/unused.sock")
                    result = (
                        client.stream("GET", "/api/v1/recordings/test/download")
                        if streaming
                        else client.request("GET", "/api/v1/status")
                    )
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.body["error"]["code"], "engine_busy")

    def test_client_default_timeout_covers_bounded_camera_worker_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            client = EngineSocketClient(Path(directory) / "engine.sock")

        self.assertEqual(client._timeout, 55.0)

    def test_request_read_has_total_deadline_during_slow_trickle(self):
        client, peer = socket.socketpair()
        sender_errors = []

        def send_slowly():
            try:
                for _ in range(20):
                    peer.sendall(b"{")
                    time.sleep(0.02)
            except OSError as exc:
                sender_errors.append(exc)
            finally:
                peer.close()

        sender = threading.Thread(target=send_slowly)
        sender.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                _read_request_line(
                    client,
                    64,
                    idle_timeout_seconds=0.2,
                    deadline_seconds=0.12,
                )
        finally:
            client.close()
            sender.join(timeout=1)
        self.assertFalse(sender.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)

    def test_one_json_line_round_trip_and_socket_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            handler = FakeHandler()
            server = EngineSocketServer(path, handler)
            try:
                server.start()
            except PermissionError:
                self.skipTest("execution sandbox does not permit Unix socket bind")
            try:
                response = EngineSocketClient(path).request(
                    "PATCH", "/api/v1/settings", {"expected_revision": 2}
                )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.body["operation_id"], "op-1")
                self.assertEqual(
                    handler.calls,
                    [("PATCH", "/api/v1/settings", {"expected_revision": 2})],
                )
            finally:
                server.close()
            self.assertFalse(path.exists())

    def test_unverifiable_existing_socket_is_preserved(self):
        for failure in (PermissionError("denied"), TimeoutError("busy")):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "engine.sock"
                path.write_bytes(b"existing listener sentinel")
                server = EngineSocketServer(path, FakeHandler())
                with (
                    patch.object(
                        Path,
                        "lstat",
                        return_value=SimpleNamespace(st_mode=stat.S_IFSOCK),
                    ),
                    patch(
                        "raspi_recorder.adapters.engine_socket.socket.socket"
                    ) as probe,
                ):
                    probe.return_value.connect.side_effect = failure
                    with self.assertRaisesRegex(RuntimeError, "停止を確認できません"):
                        server.start()
                self.assertEqual(path.read_bytes(), b"existing listener sentinel")

    def test_bind_collision_does_not_delete_another_listeners_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            server = EngineSocketServer(path, FakeHandler())

            def collide():
                path.write_bytes(b"concurrent listener sentinel")
                raise OSError("address already in use")

            with patch(
                "raspi_recorder.adapters.engine_socket._ThreadedUnixServer"
            ) as listener:
                listener.return_value.server_bind.side_effect = collide
                with self.assertRaisesRegex(OSError, "address already in use"):
                    server.start()
            self.assertEqual(path.read_bytes(), b"concurrent listener sentinel")

    def test_invalid_path_and_request_body_are_rejected_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            client = EngineSocketClient(Path(directory) / "missing.sock")
            with self.assertRaises(ValueError):
                client.request("GET", "/api/v1/../settings")
            with self.assertRaises(ValueError):
                client.request("POST", "/api/v1/status", [])  # type: ignore[arg-type]
            with self.assertRaises(EngineSocketError):
                client.request("GET", "/api/v1/status")

    def test_binary_stream_keeps_first_body_bytes_and_closes_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            media = Path(directory) / "capture.mp4"
            expected = b"first bytes\x00and the rest" * 1_000
            media.write_bytes(expected)
            server = EngineSocketServer(path, FakeStreamHandler(media))
            try:
                server.start()
            except PermissionError:
                self.skipTest("execution sandbox does not permit Unix socket bind")
            try:
                stream = EngineSocketClient(path).stream(
                    "GET", "/api/v1/recordings/recording-id/download"
                )
                self.assertEqual(stream.status_code, 200)
                self.assertEqual(stream.headers["Content-Length"], str(len(expected)))
                self.assertEqual(b"".join(stream.chunks()), expected)
            finally:
                server.close()

    def test_active_socket_is_not_unlinked_by_second_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            first = EngineSocketServer(path, FakeHandler())
            try:
                first.start()
            except PermissionError:
                self.skipTest("execution sandbox does not permit Unix socket bind")
            try:
                second = EngineSocketServer(path, FakeHandler())
                with self.assertRaisesRegex(RuntimeError, "稼働"):
                    second.start()
                self.assertTrue(path.exists())
            finally:
                first.close()

    def test_incomplete_request_is_timed_out_and_handler_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            server = EngineSocketServer(path, FakeHandler())
            try:
                server.start()
            except PermissionError:
                self.skipTest("execution sandbox does not permit Unix socket bind")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(4)
            try:
                client.connect(str(path))
                client.sendall(b'{"request_id":')
                response = bytearray()
                while not response.endswith(b"\n"):
                    response.extend(client.recv(1024))
                self.assertEqual(json.loads(response)["status_code"], 408)
            finally:
                client.close()
                server.close()
            self.assertFalse(path.exists())

    def test_handler_limit_returns_busy_without_growing_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            server = EngineSocketServer(path, FakeHandler())
            with patch(
                "raspi_recorder.adapters.engine_socket._MAX_CONTROL_HANDLERS", 1
            ):
                try:
                    server.start()
                except PermissionError:
                    self.skipTest("execution sandbox does not permit Unix socket bind")
                first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                first.settimeout(1)
                try:
                    first.connect(str(path))
                    first.sendall(b"partial")
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        active = server._server
                        if active is not None and active._handler_threads:
                            break
                        time.sleep(0.005)
                    response = EngineSocketClient(path).request("GET", "/api/v1/status")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.body["error"]["code"], "engine_busy")
                    active = server._server
                    self.assertIsNotNone(active)
                    self.assertLessEqual(len(active._handler_threads), 1)
                finally:
                    first.close()
                    server.close()

    def test_stream_response_closes_lease_when_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.sock"
            media = Path(directory) / "capture.mp4"
            media.write_bytes(b"x")
            handler = CloseTrackingStreamHandler(media)
            server = EngineSocketServer(path, handler)
            try:
                server.start()
            except PermissionError:
                self.skipTest("execution sandbox does not permit Unix socket bind")
            try:
                with patch(
                    "raspi_recorder.adapters.engine_socket._MAX_RESPONSE_BYTES", 128
                ):
                    response = EngineSocketClient(path).stream(
                        "GET", "/api/v1/recordings/recording-id/download"
                    )
                self.assertIsInstance(response, EngineResponse)
                self.assertEqual(response.status_code, 500)
                self.assertTrue(handler.closed.wait(1))
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()

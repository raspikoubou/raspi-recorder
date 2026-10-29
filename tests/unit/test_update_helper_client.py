import json
import unittest
from unittest.mock import patch

from raspi_recorder.adapters.update_helper_client import (
    UpdateHelperClient,
    UpdateHelperClientError,
)


class _Socket:
    def __init__(self, response: bytes = b"") -> None:
        self.response = bytearray(response)
        self.timeout = None
        self.connected = None
        self.sent = b""
        self.connect_error: OSError | None = None
        self.send_error: OSError | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.connected = path
        if self.connect_error:
            raise self.connect_error

    def sendall(self, payload):
        self.sent = payload
        if self.send_error:
            raise self.send_error

    def recv(self, size):
        if not self.response:
            return b""
        result = self.response[:size]
        del self.response[:size]
        return bytes(result)


class UpdateHelperClientTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "operation_id": "a" * 32,
            "version": "1.2.3",
            "asset_sha256": "b" * 64,
        }

    def _apply_with_socket(self, sock):
        with patch(
            "raspi_recorder.adapters.update_helper_client.socket.socket",
            return_value=sock,
        ):
            UpdateHelperClient("/run/test.sock").apply(**self.request)

    def test_sends_fixed_bounded_payload_and_accepts_durable_ack(self):
        sock = _Socket(b'{"ok":true,"accepted":true}\n')

        self._apply_with_socket(sock)

        self.assertEqual(sock.connected, "/run/test.sock")
        self.assertGreater(sock.timeout, 0)
        self.assertLessEqual(sock.timeout, 180)
        self.assertEqual(
            json.loads(sock.sent),
            {
                "action": "apply",
                "operation_id": self.request["operation_id"],
                "version": self.request["version"],
                "asset_sha256": self.request["asset_sha256"],
            },
        )
        self.assertLessEqual(len(sock.sent), 4096)

    def test_rejection_is_not_uncertain(self):
        sock = _Socket(b'{"ok":false,"error":"update_busy","uncertain":false}\n')

        with self.assertRaisesRegex(UpdateHelperClientError, "update_busy") as raised:
            self._apply_with_socket(sock)

        self.assertFalse(raised.exception.uncertain)

    def test_partial_handoff_and_malformed_ack_are_uncertain(self):
        send_failure = _Socket()
        send_failure.send_error = OSError("closed during send")
        with patch(
            "raspi_recorder.adapters.update_helper_client.socket.socket",
            return_value=send_failure,
        ):
            with self.assertRaises(UpdateHelperClientError) as raised:
                UpdateHelperClient("/run/test.sock").apply(**self.request)
        self.assertTrue(raised.exception.uncertain)

        malformed = _Socket(b"not-json\n")
        with patch(
            "raspi_recorder.adapters.update_helper_client.socket.socket",
            return_value=malformed,
        ):
            with self.assertRaises(UpdateHelperClientError) as raised:
                UpdateHelperClient("/run/test.sock").apply(**self.request)
        self.assertTrue(raised.exception.uncertain)

    def test_connection_failure_before_request_is_not_uncertain(self):
        sock = _Socket()
        sock.connect_error = OSError("unavailable")
        with patch(
            "raspi_recorder.adapters.update_helper_client.socket.socket",
            return_value=sock,
        ):
            with self.assertRaisesRegex(
                UpdateHelperClientError, "update_helper_unavailable"
            ) as raised:
                UpdateHelperClient("/run/test.sock").apply(**self.request)
        self.assertFalse(raised.exception.uncertain)

    def test_rejects_nan_timeout_and_malformed_request_arguments(self):
        for timeout in (float("nan"), float("inf"), 0, 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                UpdateHelperClient(timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            UpdateHelperClient().apply(
                operation_id="../../unsafe",
                version=self.request["version"],
                asset_sha256=self.request["asset_sha256"],
            )


if __name__ == "__main__":
    unittest.main()

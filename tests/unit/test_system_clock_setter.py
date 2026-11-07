from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from raspi_recorder.adapters.system_clock_setter import SystemClockSetter


class SystemClockSetterTests(unittest.TestCase):
    def test_client_uses_one_bounded_json_request_per_unix_connection(self):
        status_response = {
            "ok": True,
            "status": {
                "timezone": "Asia/Tokyo",
                "ntp_enabled": False,
                "synchronized": False,
            },
        }
        clients = []
        for response in (status_response, {"ok": True}):
            client = MagicMock()
            client.__enter__.return_value = client
            client.recv.return_value = (
                json.dumps(response, separators=(",", ":")).encode() + b"\n"
            )
            clients.append(client)

        with patch(
            "raspi_recorder.adapters.system_clock_setter.socket.socket"
        ) as make_socket:
            make_socket.side_effect = clients
            setter = SystemClockSetter("/run/test-clock.sock", timeout_seconds=1)

            status = setter.status()
            setter.set_utc_ns(1_800_000_000_000_000_000, "keep")

        self.assertEqual(status.timezone, "Asia/Tokyo")
        self.assertIs(status.ntp_enabled, False)
        for client in clients:
            self.assertTrue(
                all(0 < call.args[0] <= 1 for call in client.settimeout.call_args_list)
            )
            client.connect.assert_called_once_with("/run/test-clock.sock")
        self.assertEqual(clients[0].sendall.call_args.args[0], b'{"action":"status"}\n')
        self.assertEqual(
            json.loads(clients[1].sendall.call_args.args[0]),
            {
                "action": "set",
                "utc_ns": 1_800_000_000_000_000_000,
                "ntp_policy": "keep",
            },
        )


if __name__ == "__main__":
    unittest.main()

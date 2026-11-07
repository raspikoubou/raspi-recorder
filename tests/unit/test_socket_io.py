from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch

import pytest

from raspi_recorder.adapters.socket_io import receive_line
from raspi_recorder.adapters.system_clock_setter import SystemClockSetter
from raspi_recorder.adapters.update_helper_client import (
    UpdateHelperClient,
    UpdateHelperClientError,
)
from raspi_recorder.ports.clock import ClockSetError


def test_line_size_and_eof_are_bounded():
    for payload, expected in (
        (b"abc\nsecond\n", b"abc\n"),
        (b"123456", b"12345"),
        (b"ab", b"ab"),
    ):
        client, peer = socket.socketpair()
        try:
            peer.sendall(payload)
            peer.shutdown(socket.SHUT_WR)
            assert receive_line(client, 4, deadline=time.monotonic() + 1) == expected
        finally:
            client.close()
            peer.close()


@pytest.mark.parametrize("helper", ["clock", "update"])
def test_helper_trickling_reply_obeys_total_deadline_and_remains_uncertain(helper):
    client, peer = socket.socketpair()

    class ConnectedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            client.close()

        def connect(self, _path):
            pass

        def __getattr__(self, name):
            return getattr(client, name)

    def trickle():
        try:
            peer.recv(4096)
            for _ in range(100):
                peer.sendall(b"{")
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            peer.close()

    sender = threading.Thread(target=trickle)
    sender.start()
    started = time.monotonic()
    try:
        with patch("socket.socket", return_value=ConnectedSocket()):
            if helper == "clock":
                with pytest.raises(ClockSetError) as error:
                    SystemClockSetter(timeout_seconds=0.12).set_utc_ns(
                        123, "pause_and_resume"
                    )
                assert error.value.changed
                assert error.value.ntp_state_uncertain
            else:
                with pytest.raises(UpdateHelperClientError) as error:
                    UpdateHelperClient(timeout_seconds=0.12).apply(
                        operation_id="a" * 32, version="1.2.3", asset_sha256="b" * 64
                    )
                assert error.value.uncertain
        assert time.monotonic() - started < 1
    finally:
        client.close()
        sender.join(timeout=3)
        assert not sender.is_alive()

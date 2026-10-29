from __future__ import annotations

import os
import subprocess
import sys
import time
from unittest.mock import Mock, patch

import pytest

from raspi_recorder.adapters.capture_process import CaptureProcess


def test_kills_and_reaps_a_child_that_ignores_sigterm():
    owner = CaptureProcess()
    child = owner.start(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)",
        ]
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        started = time.monotonic()
        owner.close(0.3)
        assert time.monotonic() - started < 1.0
        assert child.returncode is not None
        assert owner.active_pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
        owner.close(0.1)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_failed_reap_retains_ownership_and_prevents_replacement():
    child = Mock()
    child.poll.return_value = None
    child.wait.side_effect = subprocess.TimeoutExpired("capture", 0.01)
    owner = CaptureProcess()
    with patch(
        "raspi_recorder.adapters.capture_process.subprocess.Popen", return_value=child
    ):
        owner.start(["test-capture"])
        with pytest.raises(TimeoutError, match="reap_timeout"):
            owner.close(0.01)
        assert owner.active_pid == child.pid
        child.stdout.close.assert_not_called()
        with pytest.raises(RuntimeError, match="not_reaped"):
            owner.start(["replacement"])
        child.poll.return_value = 0
        owner.close(0.01)
        child.stdout.close.assert_called_once()
        assert owner.active_pid is None


def test_cleanup_lock_wait_does_not_take_process_ownership():
    owner = CaptureProcess()
    owner._cleanup_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="cleanup_lock_timeout"):
            owner.close(0.01)
    finally:
        owner._cleanup_lock.release()
    owner.close(0.01)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_close_rejects_unbounded_or_invalid_timeouts(timeout):
    with pytest.raises(ValueError):
        CaptureProcess().close(timeout)

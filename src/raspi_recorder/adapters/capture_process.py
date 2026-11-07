"""カメラ・音声capture用の、期限と所有権を持つ子process管理。"""

from __future__ import annotations

import math
import subprocess
import time
from threading import Lock


class CaptureProcess:
    """停止要求は待たずに送信し、回収失敗時もprocessの所有権を保持する。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cleanup_lock = Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, command: list[str]) -> subprocess.Popen[bytes]:
        """前の子を回収するまで、新しいcapture processは起動しない。"""
        with self._lock:
            if self._process is not None:
                raise RuntimeError("capture_process_not_reaped")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._process = process
            return process

    @property
    def active_pid(self) -> int | None:
        """未終了の子processがある場合はPIDを返す。"""
        with self._lock:
            process = self._process
            return (
                None if process is None or process.poll() is not None else process.pid
            )

    def request_stop(self) -> None:
        """読み取りthreadの終了を待たずにSIGTERMを送る。"""
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

    def close(self, timeout_seconds: float = 2.0) -> None:
        """lock取得・SIGTERM猶予・SIGKILL後の回収を同じ期限内で行う。"""
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_secondsは有限の正の数値が必要です")
        deadline = time.monotonic() + timeout_seconds
        if not self._cleanup_lock.acquire(timeout=timeout_seconds):
            raise TimeoutError("capture_process_cleanup_lock_timeout")
        try:
            with self._lock:
                process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                remaining = max(0.0, deadline - time.monotonic())
                kill_grace = min(1.0, remaining / 2)
                try:
                    process.wait(timeout=max(0.0, remaining - kill_grace))
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        # The child may still own a device. Keep it registered
                        # so close() can retry and a replacement cannot start.
                        raise TimeoutError("capture_process_reap_timeout") from exc
            try:
                if process.stdout is not None:
                    process.stdout.close()
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
        finally:
            self._cleanup_lock.release()

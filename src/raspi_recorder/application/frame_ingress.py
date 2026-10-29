"""camera ingestと常時録画frame queueの競合調停。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from threading import Condition, Lock, RLock

from raspi_recorder.domain.frames import (
    FrameEnvelope,
    FrameLease,
    FrameRing,
)
from raspi_recorder.ports.clock import Clock


class FrameQueueError(RuntimeError):
    """frame queueが欠落なく継続できない。"""


class FrameBatch:
    """writerが処理する有界frame batchと明示ack。"""

    __slots__ = ("frames", "_leases", "_released", "_ack_lock", "_on_ack")

    def __init__(
        self,
        entries: tuple[tuple[FrameEnvelope, FrameLease], ...],
        on_ack: Callable[[int, int], None] | None = None,
    ) -> None:
        self.frames = tuple(frame for frame, _ in entries)
        self._leases = tuple(lease for _, lease in entries)
        self._released = False
        self._ack_lock = Lock()
        self._on_ack = on_ack

    @property
    def size_bytes(self) -> int:
        """batchが保持するJPEG payloadの合計。"""
        return sum(frame.size_bytes for frame in self.frames)

    def acknowledge(self) -> None:
        """全frameの書込完了後にだけ呼び、media leaseを解放する。"""
        with self._ack_lock:
            if self._released:
                return
            frame_count = len(self.frames)
            size_bytes = self.size_bytes
            first_error: Exception | None = None
            for lease in self._leases:
                try:
                    lease.release()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._leases = ()
            self.frames = ()
            self._released = True
            callback = self._on_ack
            self._on_ack = None
            if callback is not None:
                try:
                    callback(size_bytes, frame_count)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

    def __enter__(self) -> FrameBatch:
        if self._released:
            raise RuntimeError("ack済みframe batchは再利用できません")
        return self

    def __exit__(self, *_: object) -> None:
        self.acknowledge()


class ContinuousFrameSession:
    """常時録画の有界frame queueとring共有lease。"""

    def __init__(self, max_queue_bytes: int) -> None:
        if type(max_queue_bytes) is not int or max_queue_bytes < 1:
            raise ValueError("max_queue_bytesは1以上の整数で指定してください")
        self._condition = Condition(RLock())
        self._queue: deque[tuple[FrameEnvelope, FrameLease]] = deque()
        self._queued_bytes = 0
        self._queued_frames = 0
        self._in_flight_bytes = 0
        self._in_flight_frames = 0
        self._high_water_bytes = 0
        self._high_water_frames = 0
        self._max_queue_bytes = max_queue_bytes
        self._producer_finished = False
        self._failure: str | None = None

    @property
    def finished(self) -> bool:
        """capture producerが終了したか返す。"""
        with self._condition:
            return self._producer_finished

    @property
    def failure(self) -> str | None:
        """queue停止理由を返す。"""
        with self._condition:
            return self._failure

    @property
    def queued_bytes(self) -> int:
        """未処理JPEGのbyte数。"""
        with self._condition:
            return self._queued_bytes

    @property
    def memory_snapshot(self) -> dict[str, int | str | None | bool]:
        """queue・in-flight参照の実測量とpeakを返す。"""
        with self._condition:
            return {
                "queued_bytes": self._queued_bytes,
                "queued_frames": self._queued_frames,
                "in_flight_bytes": self._in_flight_bytes,
                "in_flight_frames": self._in_flight_frames,
                "high_water_bytes": self._high_water_bytes,
                "high_water_frames": self._high_water_frames,
                "failure": self._failure,
                "finished": self._producer_finished,
            }

    def enqueue(self, frame: FrameEnvelope, lease: FrameLease) -> bool:
        """frameとring leaseを追加し、超過時は全queueを明示失敗する。"""
        with self._condition:
            if self._failure is not None or self._producer_finished:
                lease.release()
                return False
            if self._queued_bytes + frame.size_bytes > self._max_queue_bytes:
                lease.release()
                self._fail_locked("continuous_queue_memory_limit")
                return False
            self._queue.append((frame, lease))
            self._queued_bytes += frame.size_bytes
            self._queued_frames += 1
            self._update_high_water_locked()
            self._condition.notify_all()
            return True

    def take_batch(
        self, max_frames: int = 128, timeout_seconds: float = 0.0
    ) -> FrameBatch | None:
        """次batchを取り出し、writer完了後にackできる形で返す。"""
        if type(max_frames) is not int or max_frames < 1:
            raise ValueError("max_framesは1以上の整数で指定してください")
        if type(timeout_seconds) not in (int, float) or timeout_seconds < 0:
            raise ValueError("timeout_secondsは0以上の数値で指定してください")
        with self._condition:
            if not self._queue and not self._producer_finished and timeout_seconds:
                self._condition.wait(timeout_seconds)
            if self._failure is not None:
                raise FrameQueueError(self._failure)
            if not self._queue:
                return None
            entries = tuple(
                self._queue.popleft() for _ in range(min(max_frames, len(self._queue)))
            )
            byte_count = sum(frame.size_bytes for frame, _ in entries)
            frame_count = len(entries)
            self._queued_bytes -= byte_count
            self._queued_frames -= frame_count
            self._in_flight_bytes += byte_count
            self._in_flight_frames += frame_count
            self._update_high_water_locked()
            return FrameBatch(entries, self._acknowledge_batch)

    def finish(self) -> None:
        """追加を閉じ、残queueをwriterがdrainできるようにする。"""
        with self._condition:
            self._producer_finished = True
            self._condition.notify_all()

    def fail(self, reason: str) -> None:
        """queueを異常終了し、未処理leaseを解放する。"""
        with self._condition:
            self._fail_locked(reason)

    def _fail_locked(self, reason: str) -> None:
        if self._failure is not None:
            return
        self._failure = reason[:120] if reason else "continuous_failed"
        while self._queue:
            _, lease = self._queue.popleft()
            lease.release()
        self._queued_bytes = 0
        self._queued_frames = 0
        self._producer_finished = True
        self._condition.notify_all()

    def _acknowledge_batch(self, size_bytes: int, frame_count: int) -> None:
        with self._condition:
            if (
                size_bytes > self._in_flight_bytes
                or frame_count > self._in_flight_frames
            ):
                raise RuntimeError("continuous in-flight counterが不正です")
            self._in_flight_bytes -= size_bytes
            self._in_flight_frames -= frame_count
            self._condition.notify_all()

    def _update_high_water_locked(self) -> None:
        total_bytes = self._queued_bytes + self._in_flight_bytes
        total_frames = self._queued_frames + self._in_flight_frames
        self._high_water_bytes = max(self._high_water_bytes, total_bytes)
        self._high_water_frames = max(self._high_water_frames, total_frames)


class CameraFramePipeline:
    """単一カメラの常時録画queueと共有frame予算を管理する。"""

    def __init__(
        self,
        camera_id: int,
        ring: FrameRing,
        clock: Clock,
        *,
        max_queue_bytes: int | None = None,
    ) -> None:
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        self.ring = ring
        self._lock = RLock()
        self._max_queue_bytes = (
            ring.max_bytes if max_queue_bytes is None else max_queue_bytes
        )
        if type(self._max_queue_bytes) is not int or self._max_queue_bytes < 1:
            raise ValueError("max_queue_bytesは1以上の整数で指定してください")
        if self._max_queue_bytes > ring.max_bytes:
            raise ValueError("queue上限はring全体memory上限以下が必要です")
        self._continuous: ContinuousFrameSession | None = None

    @property
    def active_mode(self) -> str | None:
        with self._lock:
            return "continuous" if self._continuous is not None else None

    @property
    def memory_snapshot(self) -> dict[str, object]:
        with self._lock:
            result: dict[str, object] = dict(self.ring.memory_snapshot)
            result["active_mode"] = self.active_mode
            if self._continuous is not None:
                result["active_session"] = self._continuous.memory_snapshot
            return result

    def start_continuous(self) -> ContinuousFrameSession:
        with self._lock:
            if self._continuous is not None:
                raise RuntimeError("既に録画queueが動作しています")
            self._continuous = ContinuousFrameSession(self._max_queue_bytes)
            return self._continuous

    def push(self, frame: FrameEnvelope) -> None:
        if not isinstance(frame, FrameEnvelope):
            raise ValueError("frameはFrameEnvelopeで指定してください")
        with self._lock:
            self.ring.append(frame)
            continuous = self._continuous
            if continuous is None:
                return
            lease = self.ring.acquire_frame(frame)
            if lease is None:
                continuous.fail("continuous_frame_evicted_before_pin")
                self._continuous = None
                return
            if not continuous.enqueue(frame, lease):
                self._continuous = None

    def finish_continuous(self, session: ContinuousFrameSession) -> None:
        with self._lock:
            if self._continuous is not session:
                raise ValueError("指定sessionはactive continuousではありません")
            session.finish()
            self._continuous = None

    def clear(self, reason: str = "camera_generation_changed") -> None:
        with self._lock:
            if self._continuous is not None:
                self._continuous.fail(reason)
                self._continuous = None
            self.ring.clear()

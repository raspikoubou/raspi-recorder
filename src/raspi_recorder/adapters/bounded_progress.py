"""Bounded, coalescing worker for noncritical recording progress I/O."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Condition, Thread

# A stuck consumer (e.g. fsync on a wedged device) must not wedge the
# caller's finalize/abort forever; close gives up after this bound and
# reports the failure instead. Leave time beyond the catalog's five-second
# SQLite busy timeout for the pending operation and connection cleanup.
_CLOSE_TIMEOUT_SECONDS = 15.0


class LatestValueWorker[T]:
    """Persist the newest pending value without blocking its producer."""

    def __init__(
        self,
        consume: Callable[[T], None],
        *,
        name: str,
        close_consumer: Callable[[], None] | None = None,
    ) -> None:
        if not callable(consume):
            raise ValueError("consumeはcallableが必要です")
        if not name:
            raise ValueError("nameが必要です")
        self._consume = consume
        self._close_consumer = close_consumer
        self._condition = Condition()
        self._pending: tuple[int, T] | None = None
        self._submitted_sequence = 0
        self._completed_sequence = 0
        self._coalesced_updates = 0
        self._error: BaseException | None = None
        self._stopping = False
        self._finished = False
        self._thread = Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def coalesced_updates(self) -> int:
        """Return how many pending values were replaced by newer snapshots."""
        with self._condition:
            return self._coalesced_updates

    def submit(self, value: T) -> None:
        """Replace the pending snapshot in constant time."""
        with self._condition:
            if self._error is not None:
                raise RuntimeError(
                    "progress persistence worker failed"
                ) from self._error
            if self._stopping or self._finished:
                raise RuntimeError("progress persistence worker is stopping")
            self._submitted_sequence += 1
            if self._pending is not None:
                self._coalesced_updates += 1
            self._pending = (self._submitted_sequence, value)
            self._condition.notify_all()

    def close(self, timeout_seconds: float = _CLOSE_TIMEOUT_SECONDS) -> None:
        """Drain the newest value, close its consumer, and surface failures.

        The drain and the thread join share one deadline. On timeout the
        worker stays in its stopping state (submit keeps failing) and the
        caller gets a RuntimeError caused by TimeoutError instead of
        hanging on a stuck consumer.
        """
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            self._stopping = True
            target = self._submitted_sequence
            self._condition.notify_all()
            while (
                self._completed_sequence < target
                and self._error is None
                and not self._finished
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        self._thread.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            error = self._error
            drained = self._completed_sequence >= target and self._finished
            if error is None and not drained:
                error = TimeoutError(
                    "progress persistence worker did not finish within "
                    f"{timeout_seconds} seconds"
                )
        if error is not None:
            raise RuntimeError("progress persistence worker failed") from error

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._pending is not None or self._stopping
                    )
                    if self._pending is None:
                        return
                    sequence, value = self._pending
                    self._pending = None
                self._consume(value)
                with self._condition:
                    self._completed_sequence = sequence
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._pending = None
                self._stopping = True
                self._condition.notify_all()
        finally:
            if self._close_consumer is not None:
                try:
                    self._close_consumer()
                except BaseException as exc:
                    with self._condition:
                        if self._error is None:
                            self._error = exc
                        else:
                            self._error.add_note(
                                "progress consumer close also failed: "
                                f"{type(exc).__name__}: {exc}"
                            )
            with self._condition:
                self._finished = True
                self._condition.notify_all()

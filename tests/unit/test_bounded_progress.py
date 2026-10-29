from __future__ import annotations

import threading
import unittest

from raspi_recorder.adapters.bounded_progress import LatestValueWorker


class LatestValueWorkerTests(unittest.TestCase):
    def test_pending_updates_coalesce_and_close_drains_latest(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        consumed: list[str] = []

        def consume(value: str) -> None:
            consumed.append(value)
            if value == "first":
                first_started.set()
                if not release_first.wait(2):
                    raise TimeoutError("test did not release first update")

        worker = LatestValueWorker(consume, name="test-latest-value-worker")
        worker.submit("first")
        self.assertTrue(first_started.wait(2))
        worker.submit("oldest-pending")
        worker.submit("middle-pending")
        worker.submit("latest-pending")
        self.assertEqual(worker.coalesced_updates, 2)
        release_first.set()
        worker.close()

        self.assertEqual(consumed, ["first", "latest-pending"])

    def test_consumer_failure_is_reported_by_close(self) -> None:
        started = threading.Event()

        def fail(_value: object) -> None:
            started.set()
            raise ValueError("disk write failed")

        worker = LatestValueWorker(fail, name="test-failing-progress-worker")
        worker.submit(object())
        self.assertTrue(started.wait(2))
        with self.assertRaisesRegex(RuntimeError, "worker failed") as raised:
            worker.close()
        self.assertIsInstance(raised.exception.__cause__, ValueError)

    def test_default_close_allows_slow_but_recovering_progress(self) -> None:
        started = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def consume(_value: object) -> None:
            started.set()
            if not release.wait(10):
                raise TimeoutError("test consumer was not released")

        worker = LatestValueWorker(consume, name="test-slow-progress-worker")
        worker.submit(object())
        self.assertTrue(started.wait(2))

        def close() -> None:
            try:
                worker.close()
            except Exception as exc:
                errors.append(exc)

        closer = threading.Thread(target=close)
        closer.start()
        closer.join(5.5)
        self.assertTrue(closer.is_alive())
        release.set()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])

    def test_stuck_progress_still_times_out(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def consume(_value: object) -> None:
            started.set()
            release.wait(2)

        worker = LatestValueWorker(consume, name="test-stuck-progress-worker")
        worker.submit(object())
        self.assertTrue(started.wait(2))
        try:
            with self.assertRaises(RuntimeError) as raised:
                worker.close(timeout_seconds=0.05)
            self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()

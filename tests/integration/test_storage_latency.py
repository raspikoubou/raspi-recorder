from __future__ import annotations

import threading
import time
import unittest

from raspi_recorder.application.frame_ingress import (
    CameraFramePipeline,
    FrameQueueError,
)
from raspi_recorder.domain.frames import FrameEnvelope, FrameRing
from tests.fakes import FakeClock


class StorageLatencyIntegrationTests(unittest.TestCase):
    def test_capture_queue_remains_bounded_under_finite_storage_delays(self) -> None:
        frame_bytes = b"x" * 4_096
        frame_period_ns = 1_000_000_000 // 15
        max_queue_bytes = 128 * 1_024
        target_frames = 60
        results: dict[int, dict[str, int | str | None | bool]] = {}

        for delay_ms in (0, 50, 250, 1_000):
            with self.subTest(storage_delay_ms=delay_ms):
                pipeline = CameraFramePipeline(
                    1,
                    FrameRing(max_duration_ns=10_000_000_000, max_bytes=512 * 1_024),
                    FakeClock(),
                    max_queue_bytes=max_queue_bytes,
                )
                session = pipeline.start_continuous()
                produced = 0
                written: list[int] = []

                def drain(
                    current_session=session,
                    current_delay_ms=delay_ms,
                    output=written,
                ) -> None:
                    while True:
                        try:
                            batch = current_session.take_batch(
                                max_frames=1, timeout_seconds=0.05
                            )
                        except FrameQueueError:
                            return
                        if batch is None:
                            if current_session.finished:
                                return
                            continue
                        with batch:
                            if current_delay_ms:
                                time.sleep(current_delay_ms / 1_000)
                            output.extend(frame.sequence for frame in batch.frames)

                writer = threading.Thread(target=drain, name="delayed-storage-writer")
                writer.start()
                started = time.monotonic()
                for sequence in range(1, target_frames + 1):
                    monotonic_ns = (sequence - 1) * frame_period_ns
                    pipeline.push(
                        FrameEnvelope(
                            sequence=sequence,
                            capture_mono_ns=monotonic_ns,
                            capture_utc_ns=monotonic_ns + 1_800_000_000_000_000_000,
                            timezone="UTC",
                            generation=1,
                            jpeg_bytes=frame_bytes,
                        )
                    )
                    produced += 1
                    if session.failure is not None:
                        break
                    target = started + sequence / 15
                    time.sleep(max(0.0, target - time.monotonic()))

                if pipeline.active_mode == "continuous":
                    pipeline.finish_continuous(session)
                writer.join(timeout=4)
                self.assertFalse(writer.is_alive())
                snapshot = session.memory_snapshot
                results[delay_ms] = snapshot
                pipeline.clear("storage_latency_test_complete")

                self.assertLessEqual(
                    int(snapshot["high_water_bytes"]),
                    max_queue_bytes + len(frame_bytes),
                )
                self.assertEqual(snapshot["queued_bytes"], 0)
                self.assertEqual(snapshot["in_flight_bytes"], 0)
                self.assertLessEqual(produced * len(frame_bytes), 512 * 1_024)
                if delay_ms <= 50:
                    self.assertIsNone(session.failure)
                    self.assertEqual(produced, target_frames)
                    self.assertEqual(len(written), target_frames)
                    self.assertEqual(written, list(range(1, target_frames + 1)))
                else:
                    self.assertEqual(session.failure, "continuous_queue_memory_limit")
                    self.assertLess(produced, target_frames)
                    self.assertLess(len(written), produced)

        self.assertEqual(set(results), {0, 50, 250, 1_000})


if __name__ == "__main__":
    unittest.main()

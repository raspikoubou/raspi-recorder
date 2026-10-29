import unittest

from raspi_recorder.application.frame_ingress import (
    CameraFramePipeline,
)
from raspi_recorder.domain.frames import FrameEnvelope, FrameRing
from tests.fakes import FakeClock


class FrameIngressTests(unittest.TestCase):
    def test_pipeline_enforces_one_active_recording_mode(self):
        pipeline = CameraFramePipeline(1, FrameRing(100, 100), FakeClock())
        continuous = pipeline.start_continuous()
        self.assertEqual(pipeline.active_mode, "continuous")
        with self.assertRaises(RuntimeError):
            pipeline.start_continuous()

        pipeline.push(self.frame(1, 10))
        pipeline.finish_continuous(continuous)
        self.assertTrue(continuous.finished)
        self.assertIsNone(pipeline.active_mode)

    def test_continuous_queue_capacity_failure_releases_all_leases(self):
        pipeline = CameraFramePipeline(
            1,
            FrameRing(max_duration_ns=1, max_bytes=10),
            FakeClock(),
            max_queue_bytes=1,
        )
        session = pipeline.start_continuous()
        pipeline.push(self.frame(1, 10))
        pipeline.push(self.frame(2, 20))

        self.assertEqual(session.failure, "continuous_queue_memory_limit")
        self.assertIsNone(pipeline.active_mode)
        self.assertEqual(pipeline.ring.retained_bytes, 1)

    @staticmethod
    def frame(
        sequence: int,
        mono_ns: int,
        payload: bytes = b"x",
        generation: int = 1,
    ) -> FrameEnvelope:
        return FrameEnvelope(
            sequence=sequence,
            capture_mono_ns=mono_ns,
            capture_utc_ns=mono_ns + 10_000,
            timezone="UTC",
            generation=generation,
            jpeg_bytes=payload,
        )


if __name__ == "__main__":
    unittest.main()

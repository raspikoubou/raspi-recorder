import unittest

from raspi_recorder.domain.frames import (
    AudioEnvelope,
    FrameBufferFullError,
    FrameEnvelope,
    FrameRing,
)


class FrameEnvelopeTests(unittest.TestCase):
    def test_rejects_invalid_frame_metadata_and_empty_payload(self):
        with self.assertRaises(ValueError):
            FrameEnvelope(0, 0, 0, "UTC", 1, b"jpeg")
        with self.assertRaises(ValueError):
            FrameEnvelope(1, -1, 0, "UTC", 1, b"jpeg")
        with self.assertRaises(ValueError):
            FrameEnvelope(1, 0, 0, "UTC", 1, b"")

    def test_audio_envelope_clips_half_open_sample_range(self):
        block = AudioEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000_000,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )

        clipped = block.clip(1_002_500_000, 1_007_500_000)

        self.assertIsNotNone(clipped)
        assert clipped is not None
        self.assertEqual(clipped.sample_count, 240)
        self.assertEqual(clipped.capture_mono_ns, 1_002_500_000)
        self.assertEqual(clipped.capture_utc_ns, 2_002_500_000)

    def test_audio_envelope_rejects_bad_pcm_and_video_counts_audio_memory(self):
        with self.assertRaises(ValueError):
            AudioEnvelope(1, 0, 0, "UTC", 1, 48_000, b"\0")
        audio = AudioEnvelope(1, 0, 0, "UTC", 1, 48_000, b"\0\0" * 48)
        frame = FrameEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000,
            capture_utc_ns=1_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
            audio_blocks=(audio,),
            audio_available=True,
        )
        self.assertEqual(frame.size_bytes, 4 + audio.size_bytes)


class FrameRingTests(unittest.TestCase):
    def test_acquire_uses_half_open_time_window_and_preserves_order(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=100)
        ring.append(self.frame(1, 100, b"a"))
        ring.append(self.frame(2, 150, b"bb"))
        ring.append(self.frame(3, 200, b"ccc"))

        lease = ring.acquire_range(100, 200, generation=1)
        self.assertEqual([frame.sequence for frame in lease.frames], [1, 2])
        self.assertEqual(lease.size_bytes, 3)
        self.assertEqual(ring.retained_bytes, 6)
        lease.release()
        self.assertTrue(lease.released)

    def test_expiry_and_byte_pressure_evict_only_unpinned_oldest_frames(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=6)
        ring.append(self.frame(1, 0, b"aaaa"))
        lease = ring.acquire_range(0, 1, generation=1)
        ring.append(self.frame(2, 10, b"bb"))

        self.assertEqual([frame.sequence for frame in lease.frames], [1])
        self.assertEqual(ring.retained_bytes, 6)
        with self.assertRaises(FrameBufferFullError):
            ring.append(self.frame(3, 20, b"ccc"))

        self.assertEqual(ring.frame_count, 0)
        self.assertEqual(ring.retained_bytes, 4)
        lease.release()
        ring.append(self.frame(3, 20, b"ccc"))
        result = ring.acquire_range(0, 21, 1)
        self.assertEqual([frame.sequence for frame in result.frames], [3])
        result.release()
        self.assertEqual(ring.retained_bytes, 3)

    def test_generation_change_clears_ring_but_keeps_leased_frames_accounted(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=6)
        ring.append(self.frame(1, 10, b"old", generation=1))
        old_lease = ring.acquire_range(10, 20, generation=1)

        ring.append(self.frame(1, 0, b"new", generation=2))
        self.assertEqual(ring.frame_count, 1)
        self.assertEqual(ring.retained_bytes, 6)

        old_lease.release()
        self.assertEqual(ring.retained_bytes, 3)
        self.assertEqual(
            [frame.sequence for frame in ring.acquire_range(0, 1, 2).frames], [1]
        )

    def test_release_is_idempotent_and_clear_keeps_leases_safe(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=10)
        ring.append(self.frame(1, 10, b"jpeg"))
        lease = ring.acquire_range(10, 11, generation=1)
        ring.clear()

        self.assertEqual(ring.frame_count, 0)
        self.assertEqual(ring.retained_bytes, 4)
        lease.release()
        lease.release()
        self.assertEqual(ring.retained_bytes, 0)

    def test_release_drops_frame_references_even_if_lease_object_is_retained(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=10)
        ring.append(self.frame(1, 10, b"jpeg"))
        lease = ring.acquire_range(10, 11, generation=1)
        ring.clear()

        lease.release()

        self.assertEqual(lease.frames, ())
        self.assertEqual(lease.size_bytes, 0)
        self.assertEqual(ring.retained_bytes, 0)
        self.assertEqual(ring.retained_frame_count, 0)
        self.assertEqual(ring.memory_snapshot["high_water_payload_bytes"], 4)
        self.assertEqual(ring.memory_snapshot["high_water_frames"], 1)

    def test_rejects_sequence_clock_and_generation_regressions(self):
        ring = FrameRing(max_duration_ns=100, max_bytes=100)
        ring.append(self.frame(2, 20, b"x"))
        for frame in (self.frame(1, 21, b"x"), self.frame(3, 19, b"x")):
            with self.assertRaises(ValueError):
                ring.append(frame)
        ring.append(self.frame(1, 0, b"x", generation=2))
        with self.assertRaises(ValueError):
            ring.append(self.frame(1, 1, b"x", generation=1))

    @staticmethod
    def frame(
        sequence: int,
        mono_ns: int,
        payload: bytes,
        generation: int = 1,
    ) -> FrameEnvelope:
        return FrameEnvelope(
            sequence=sequence,
            capture_mono_ns=mono_ns,
            capture_utc_ns=mono_ns + 1_000,
            timezone="UTC",
            generation=generation,
            jpeg_bytes=payload,
        )


if __name__ == "__main__":
    unittest.main()

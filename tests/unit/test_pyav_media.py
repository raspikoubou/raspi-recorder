from __future__ import annotations

import io
import unittest
from unittest.mock import Mock

from raspi_recorder.adapters.pyav_media import (
    PyAvMp4Writer,
    _clip_audio_to_timeline_origin,
    _FrameMetadata,
    _update_time_spans,
)
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope


def frame(sequence: int, mono: int, utc: int, timezone: str = "Asia/Tokyo"):
    return FrameEnvelope(
        sequence=sequence,
        capture_mono_ns=mono,
        capture_utc_ns=utc,
        timezone=timezone,
        generation=1,
        jpeg_bytes=b"jpeg",
    )


class ManifestTimeSpanTests(unittest.TestCase):
    def test_contiguous_wall_clock_frames_extend_one_span(self):
        spans: list[dict[str, int | str]] = []
        first = frame(1, 1_000_000_000, 10_000_000_000)
        second = frame(2, 2_000_000_000, 11_000_000_000)
        _update_time_spans(spans, None, first)
        _update_time_spans(spans, first, second)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["first_sequence"], 1)
        self.assertEqual(spans[0]["last_sequence"], 2)
        self.assertEqual(spans[0]["end_mono_ns"], 2_000_000_000)
        self.assertEqual(spans[0]["end_utc_ns"], 11_000_000_000)

    def test_ntp_step_splits_wall_clock_span_without_using_it_for_duration(self):
        spans: list[dict[str, int | str]] = []
        first = frame(1, 1_000_000_000, 10_000_000_000)
        second = frame(2, 2_000_000_000, 3_602_000_000_000)
        _update_time_spans(spans, None, first)
        _update_time_spans(spans, first, second)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["last_sequence"], 1)
        self.assertEqual(spans[1]["first_sequence"], 2)

    def test_timezone_change_splits_display_span_even_if_utc_is_continuous(self):
        spans: list[dict[str, int | str]] = []
        first = frame(1, 1_000_000_000, 10_000_000_000, "UTC")
        second = frame(2, 2_000_000_000, 11_000_000_000, "Asia/Tokyo")
        _update_time_spans(spans, None, first)
        _update_time_spans(spans, first, second)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[1]["timezone"], "Asia/Tokyo")

    def test_writer_metadata_does_not_retain_video_or_audio_payload(self):
        audio = AudioEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000_000,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 24_000,
        )
        envelope = FrameEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000_000,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg" * 100_000,
            audio_blocks=(audio,),
            audio_available=True,
        )

        metadata = _FrameMetadata.from_envelope(envelope)

        self.assertEqual(metadata.sequence, 1)
        self.assertEqual(metadata.generation, 1)
        self.assertFalse(hasattr(metadata, "jpeg_bytes"))
        self.assertFalse(hasattr(metadata, "audio_blocks"))


class PyAvWriterResourceCleanupTests(unittest.TestCase):
    def make_writer(self, container: Mock | None = None):
        writer = PyAvMp4Writer.__new__(PyAvMp4Writer)
        file_handle = io.BytesIO(b"partial")
        resources = {
            "container": container or Mock(),
            "file": file_handle,
            "stream": object(),
            "audio_encoder": object(),
            "reformatter": object(),
        }
        writer._closed = False
        writer._container = resources["container"]
        writer._file = resources["file"]
        writer._stream = resources["stream"]
        writer._audio_encoder = resources["audio_encoder"]
        writer._reformatter = resources["reformatter"]
        return writer, resources

    def assert_resources_released(self, writer: PyAvMp4Writer, resources):
        self.assertTrue(writer._closed)
        self.assertIsNone(writer._container)
        self.assertIsNone(writer._file)
        self.assertIsNone(writer._stream)
        self.assertIsNone(writer._audio_encoder)
        self.assertIsNone(writer._reformatter)
        self.assertTrue(resources["file"].closed)

    def test_close_releases_native_resource_references_and_is_idempotent(self):
        writer, resources = self.make_writer()

        writer._close_resources()
        writer._close_resources()

        self.assert_resources_released(writer, resources)
        resources["container"].close.assert_called_once_with()

    def test_close_releases_references_even_when_container_close_fails(self):
        container = Mock()
        container.close.side_effect = RuntimeError("close failed")
        writer, resources = self.make_writer(container)

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            writer._close_resources()

        self.assert_resources_released(writer, resources)
        container.close.assert_called_once_with()


class AudioTimelineOriginTests(unittest.TestCase):
    def test_discards_audio_block_wholly_before_the_video_origin(self):
        origin = 1_000_000_000
        block = AudioEnvelope(
            sequence=1,
            capture_mono_ns=origin - 20_000_000,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )

        self.assertIsNone(_clip_audio_to_timeline_origin(block, origin))

    def test_clips_only_the_samples_before_origin_for_overlapping_block(self):
        origin = 1_000_000_000
        block = AudioEnvelope(
            sequence=1,
            capture_mono_ns=origin - 5_000_000,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )

        clipped = _clip_audio_to_timeline_origin(block, origin)

        self.assertIsNotNone(clipped)
        assert clipped is not None
        self.assertEqual(clipped.capture_mono_ns, origin)
        self.assertEqual(clipped.sample_count, 240)

    def test_preserves_audio_block_at_or_after_origin(self):
        origin = 1_000_000_000
        block = AudioEnvelope(
            sequence=1,
            capture_mono_ns=origin,
            capture_utc_ns=2_000_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )

        self.assertIs(_clip_audio_to_timeline_origin(block, origin), block)


if __name__ == "__main__":
    unittest.main()

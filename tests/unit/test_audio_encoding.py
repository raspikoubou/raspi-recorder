from __future__ import annotations

import unittest
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from raspi_recorder.adapters.audio_encoding import AacEncoder
from raspi_recorder.domain.frames import AudioEnvelope


class FakePlane:
    def __init__(self) -> None:
        self.data = b""

    def update(self, data: bytes) -> None:
        self.data = data


class FakeAudioFrame:
    def __init__(self, *, format: str, layout: str, samples: int) -> None:
        self.format = format
        self.layout = layout
        self.samples = samples
        self.planes = [FakePlane()]
        self.sample_rate = 0
        self.pts: int | None = None
        self.time_base: Fraction | None = None


class FakeResampler:
    def __init__(self, **kwargs: Any) -> None:
        self.configuration = kwargs
        self.frames: list[FakeAudioFrame] = []

    def resample(self, frame: FakeAudioFrame | None) -> tuple[FakeAudioFrame, ...]:
        if frame is None:
            return ()
        self.frames.append(frame)
        return (frame,)


class FakeAudioStream:
    def __init__(self) -> None:
        self.codec_context = SimpleNamespace(
            time_base=None, sample_rate=None, bit_rate=None
        )
        self.bit_rate: int | None = None
        self.encoded: list[Any] = []

    def encode(self, frame: Any | None) -> tuple[Any, ...]:
        if frame is None:
            self.encoded.append("flush")
            return ("flush",)
        self.encoded.append(frame)
        return (frame,)


class FakeContainer:
    def __init__(self) -> None:
        self.stream = FakeAudioStream()

    def add_stream(self, codec: str, *, rate: int, layout: str) -> FakeAudioStream:
        self.codec = codec
        self.rate = rate
        self.layout = layout
        return self.stream


def block(
    sequence: int,
    mono_ns: int,
    *,
    generation: int = 1,
    samples: int = 480,
) -> AudioEnvelope:
    return AudioEnvelope(
        sequence,
        mono_ns,
        mono_ns + 100_000,
        "UTC",
        generation,
        48_000,
        b"\0\0" * samples,
    )


class AacEncoderTests(unittest.TestCase):
    def test_pcm_frame_keeps_sample_timestamp_and_uses_mono_aac_stream(self):
        container = FakeContainer()
        resampler = FakeResampler()
        av_module = SimpleNamespace(
            AudioFrame=FakeAudioFrame,
            audio=SimpleNamespace(
                resampler=SimpleNamespace(AudioResampler=lambda **kwargs: resampler)
            ),
        )
        encoder = AacEncoder(
            container, av_module, timeline_origin_mono_ns=1_000_000_000
        )
        envelope = block(1, 1_500_000_000)

        packets = encoder.encode(envelope)
        encoded_frame = resampler.frames[0]

        self.assertEqual(container.codec, "aac")
        self.assertEqual(container.rate, 48_000)
        self.assertEqual(container.layout, "mono")
        self.assertEqual(container.stream.bit_rate, 96_000)
        self.assertEqual(encoded_frame.samples, 480)
        self.assertEqual(encoded_frame.planes[0].data, envelope.pcm_s16le)
        self.assertEqual(encoded_frame.pts, 24_000)
        self.assertEqual(encoded_frame.time_base, Fraction(1, 48_000))
        self.assertEqual(packets, (encoded_frame,))
        self.assertEqual(encoder.flush(), ("flush",))
        self.assertEqual(encoder.flush(), ())
        with self.assertRaises(RuntimeError):
            encoder.encode(block(2, envelope.end_mono_ns))

    def test_generation_change_and_overlapping_blocks_are_rejected(self):
        container = FakeContainer()
        av_module = SimpleNamespace(
            AudioFrame=FakeAudioFrame,
            audio=SimpleNamespace(
                resampler=SimpleNamespace(
                    AudioResampler=lambda **kwargs: FakeResampler(**kwargs)
                )
            ),
        )
        encoder = AacEncoder(
            container, av_module, timeline_origin_mono_ns=1_000_000_000
        )
        first = block(1, 1_000_000_000)
        encoder.encode(first)

        with self.assertRaises(ValueError):
            encoder.encode(block(2, first.capture_mono_ns, generation=2))
        with self.assertRaises(ValueError):
            encoder.encode(block(2, first.end_mono_ns - 1))


if __name__ == "__main__":
    unittest.main()

"""PyAV AAC-LC encoder shared by saved MP4 and live HLS output."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from raspi_recorder.domain.frames import AudioEnvelope

AUDIO_SAMPLE_RATE = 48_000
AUDIO_BITRATE = 96_000


class AacEncoder:
    """取得時刻付きmono PCMをAAC-LC/48kHz/monoへ変換する。"""

    def __init__(
        self,
        container: Any,
        av_module: Any,
        *,
        timeline_origin_mono_ns: int,
    ) -> None:
        if type(timeline_origin_mono_ns) is not int or timeline_origin_mono_ns < 0:
            raise ValueError("timeline_origin_mono_nsは0以上の整数が必要です")
        self.stream = container.add_stream("aac", rate=AUDIO_SAMPLE_RATE, layout="mono")
        self.stream.bit_rate = AUDIO_BITRATE
        self.stream.time_base = Fraction(1, AUDIO_SAMPLE_RATE)
        codec_context = self.stream.codec_context
        codec_context.time_base = Fraction(1, AUDIO_SAMPLE_RATE)
        codec_context.sample_rate = AUDIO_SAMPLE_RATE
        codec_context.bit_rate = AUDIO_BITRATE
        self._resampler = av_module.audio.resampler.AudioResampler(
            format="fltp", layout="mono", rate=AUDIO_SAMPLE_RATE
        )
        self._av_module = av_module
        self._origin = timeline_origin_mono_ns
        self._last_end_mono_ns: int | None = None
        self._generation: int | None = None
        self._flushed = False
        self.block_count = 0

    def encode(self, envelope: AudioEnvelope) -> tuple[Any, ...]:
        """PCM blockをencoder packetへ変換し、時刻順・非重複を検証する。"""
        if self._flushed:
            raise RuntimeError("flush済みAAC encoderは再利用できません")
        if not isinstance(envelope, AudioEnvelope):
            raise ValueError("envelopeはAudioEnvelopeで指定してください")
        if envelope.capture_mono_ns < self._origin:
            raise ValueError("audio blockがtimeline originより前です")
        if self._generation is None:
            self._generation = envelope.generation
        elif envelope.generation != self._generation:
            raise ValueError("AAC stream内でgenerationを変更できません")
        if (
            self._last_end_mono_ns is not None
            and envelope.capture_mono_ns < self._last_end_mono_ns
        ):
            raise ValueError("audio blockが時間順でないか重複しています")

        frame = self._audio_frame(envelope)
        packets: list[Any] = []
        for resampled in self._resampler.resample(frame):
            packets.extend(self.stream.encode(resampled))
        self._last_end_mono_ns = envelope.end_mono_ns
        self.block_count += 1
        return tuple(packets)

    def flush(self) -> tuple[Any, ...]:
        """resamplerとAAC encoderの遅延sample/packetを排出する。"""
        if self._flushed:
            return ()
        packets: list[Any] = []
        for resampled in self._resampler.resample(None):
            packets.extend(self.stream.encode(resampled))
        packets.extend(self.stream.encode(None))
        self._flushed = True
        return tuple(packets)

    def _audio_frame(self, envelope: AudioEnvelope) -> Any:
        frame = self._av_module.AudioFrame(
            format="s16", layout="mono", samples=envelope.sample_count
        )
        frame.planes[0].update(envelope.pcm_s16le)
        frame.sample_rate = envelope.sample_rate
        frame.pts = (
            (envelope.capture_mono_ns - self._origin)
            * envelope.sample_rate
            // 1_000_000_000
        )
        frame.time_base = Fraction(1, envelope.sample_rate)
        return frame

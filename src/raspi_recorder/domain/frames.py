"""撮影時刻付き映像frameと、有界の前録画リング。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Self


@dataclass(frozen=True, slots=True)
class AudioEnvelope:
    """単一mono PCM blockと、最初のsampleの取得時刻。"""

    sequence: int
    capture_mono_ns: int
    capture_utc_ns: int
    timezone: str
    generation: int
    sample_rate: int
    pcm_s16le: bytes

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("audio sequenceは1以上の整数で指定してください")
        for name, value in (
            ("capture_mono_ns", self.capture_mono_ns),
            ("capture_utc_ns", self.capture_utc_ns),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}は0以上の整数で指定してください")
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("timezoneは空でない文字列で指定してください")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        if (
            type(self.sample_rate) is not int
            or not 8_000 <= self.sample_rate <= 192_000
        ):
            raise ValueError("sample_rateは8000～192000Hzで指定してください")
        if not isinstance(self.pcm_s16le, bytes) or not self.pcm_s16le:
            raise ValueError("pcm_s16leは空でないbytesで指定してください")
        if len(self.pcm_s16le) % 2:
            raise ValueError("mono S16LEのbyte数は偶数で指定してください")

    @property
    def sample_count(self) -> int:
        """mono PCMのsample数。"""
        return len(self.pcm_s16le) // 2

    @property
    def duration_ns(self) -> int:
        """整数nsに丸めたblock長。"""
        return self.sample_count * 1_000_000_000 // self.sample_rate

    @property
    def end_mono_ns(self) -> int:
        """block終端のmonotonic時刻（exclusive）。"""
        return self.capture_mono_ns + self.duration_ns

    @property
    def size_bytes(self) -> int:
        """PCM payload byte数。"""
        return len(self.pcm_s16le)

    def clip(self, start_mono_ns: int, end_mono_ns: int) -> AudioEnvelope | None:
        """時刻範囲と重なるsampleだけを含むimmutable blockを返す。"""
        if (
            type(start_mono_ns) is not int
            or type(end_mono_ns) is not int
            or start_mono_ns < 0
            or end_mono_ns < start_mono_ns
        ):
            raise ValueError("audio clip時刻範囲が不正です")
        start_delta = max(0, start_mono_ns - self.capture_mono_ns)
        end_delta = min(self.duration_ns, end_mono_ns - self.capture_mono_ns)
        first_sample = (start_delta * self.sample_rate + 999_999_999) // 1_000_000_000
        end_sample = max(0, end_delta * self.sample_rate // 1_000_000_000)
        end_sample = min(self.sample_count, end_sample)
        if first_sample >= end_sample:
            return None
        utc_start = self.capture_utc_ns + (
            first_sample * 1_000_000_000 // self.sample_rate
        )
        return AudioEnvelope(
            sequence=self.sequence,
            capture_mono_ns=self.capture_mono_ns
            + first_sample * 1_000_000_000 // self.sample_rate,
            capture_utc_ns=utc_start,
            timezone=self.timezone,
            generation=self.generation,
            sample_rate=self.sample_rate,
            pcm_s16le=self.pcm_s16le[first_sample * 2 : end_sample * 2],
        )

    def split_at(
        self, boundary_mono_ns: int
    ) -> tuple[AudioEnvelope | None, AudioEnvelope | None]:
        """1つのsampleだけに所属先を決め、blockを境界で分割する。"""
        if type(boundary_mono_ns) is not int or boundary_mono_ns < 0:
            raise ValueError("boundary_mono_nsは0以上の整数で指定してください")
        split_sample = (
            (boundary_mono_ns - self.capture_mono_ns)
            * self.sample_rate
            // 1_000_000_000
        )
        split_sample = min(self.sample_count, max(0, split_sample))
        left = None
        right = None
        if split_sample:
            left = AudioEnvelope(
                sequence=self.sequence,
                capture_mono_ns=self.capture_mono_ns,
                capture_utc_ns=self.capture_utc_ns,
                timezone=self.timezone,
                generation=self.generation,
                sample_rate=self.sample_rate,
                pcm_s16le=self.pcm_s16le[: split_sample * 2],
            )
        if split_sample < self.sample_count:
            sample_offset_ns = split_sample * 1_000_000_000 // self.sample_rate
            right = AudioEnvelope(
                sequence=self.sequence,
                capture_mono_ns=self.capture_mono_ns + sample_offset_ns,
                capture_utc_ns=self.capture_utc_ns + sample_offset_ns,
                timezone=self.timezone,
                generation=self.generation,
                sample_rate=self.sample_rate,
                pcm_s16le=self.pcm_s16le[split_sample * 2 :],
            )
        return left, right


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    """後段処理時刻ではなく、取得時点の時刻を保持する1 frame。"""

    sequence: int
    capture_mono_ns: int
    capture_utc_ns: int
    timezone: str
    generation: int
    jpeg_bytes: bytes
    audio_blocks: tuple[AudioEnvelope, ...] = ()
    audio_available: bool = False

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequenceは1以上の整数で指定してください")
        for name, value in (
            ("capture_mono_ns", self.capture_mono_ns),
            ("capture_utc_ns", self.capture_utc_ns),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}は0以上の整数で指定してください")
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ValueError("timezoneは空でない文字列で指定してください")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        if not isinstance(self.jpeg_bytes, bytes) or not self.jpeg_bytes:
            raise ValueError("jpeg_bytesは空でないbytesで指定してください")
        if not isinstance(self.audio_blocks, tuple) or any(
            not isinstance(block, AudioEnvelope) for block in self.audio_blocks
        ):
            raise ValueError("audio_blocksはAudioEnvelopeのtupleで指定してください")
        if type(self.audio_available) is not bool:
            raise ValueError("audio_availableはboolで指定してください")
        if any(block.generation != self.generation for block in self.audio_blocks):
            raise ValueError("audio/video generationが一致しません")
        if tuple(sorted(self.audio_blocks, key=lambda block: block.sequence)) != (
            self.audio_blocks
        ):
            raise ValueError("audio_blocksはsequence順で指定してください")

    @property
    def size_bytes(self) -> int:
        """JPEGと関連PCM blockのpayload byte数。"""
        return len(self.jpeg_bytes) + sum(
            block.size_bytes for block in self.audio_blocks
        )


class FrameBufferFullError(RuntimeError):
    """録画処理が有界メモリを使い切った。"""


@dataclass(slots=True)
class _StoredFrame:
    frame: FrameEnvelope
    pin_count: int = 0
    in_ring: bool = True


class FrameLease:
    """リングから切り離されてもframeと関連音声を保持する明示lease。"""

    __slots__ = ("_owner", "_entries", "_released", "_lock")

    def __init__(self, owner: FrameRing, entries: tuple[_StoredFrame, ...]) -> None:
        self._owner: FrameRing | None = owner
        self._entries = entries
        self._released = False
        self._lock = Lock()

    @property
    def frames(self) -> tuple[FrameEnvelope, ...]:
        """撮影時刻順のframe参照を返す。JPEG bytesは複製しない。"""
        with self._lock:
            return tuple(entry.frame for entry in self._entries)

    @property
    def size_bytes(self) -> int:
        """leaseが保持しているframe/audio payload byte数の合計。"""
        with self._lock:
            return sum(entry.frame.size_bytes for entry in self._entries)

    @property
    def released(self) -> bool:
        """release済みか返す。"""
        with self._lock:
            return self._released

    def release(self) -> None:
        """保持を解放し、リング外frameの容量を再利用可能にする。"""
        with self._lock:
            if self._released:
                return
            owner = self._owner
            if owner is None:
                raise RuntimeError("frame lease ownerがありません")
            owner._release(self._entries)
            self._entries = ()
            self._owner = None
            self._released = True

    def __enter__(self) -> Self:
        if self.released:
            raise RuntimeError("解放済みleaseは再利用できません")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class FrameRing:
    """期間・byte数の上限を持つ、thread-safeな前録画frame ring。

    writerへ渡すframeは`acquire_frame`でpinし、書込後に解放する。
    呼び出し側がleaseをreleaseするまで、そのframe/audio payloadは総memory予算に
    含まれ、通常のring evictionでは解放されない。
    """

    def __init__(self, max_duration_ns: int, max_bytes: int) -> None:
        if type(max_duration_ns) is not int or max_duration_ns < 1:
            raise ValueError("max_duration_nsは1以上の整数で指定してください")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytesは1以上の整数で指定してください")
        self._max_duration_ns = max_duration_ns
        self._max_bytes = max_bytes
        self._entries: deque[_StoredFrame] = deque()
        self._retained_bytes = 0
        self._retained_frame_count = 0
        self._high_water_bytes = 0
        self._high_water_frame_count = 0
        self._generation: int | None = None
        self._last_sequence: int | None = None
        self._last_mono_ns: int | None = None
        self._lock = RLock()

    @property
    def retained_bytes(self) -> int:
        """ringと全active leaseが保持するframe/audio payload byte数。"""
        with self._lock:
            return self._retained_bytes

    @property
    def ring_bytes(self) -> int:
        """現在のring内にあるframe/audio payload byte数。"""
        with self._lock:
            return sum(entry.frame.size_bytes for entry in self._entries)

    @property
    def retained_frame_count(self) -> int:
        """ring外でもleaseにより保持中のframeを含む件数。"""
        with self._lock:
            return self._retained_frame_count

    @property
    def high_water_bytes(self) -> int:
        """ring作成後の保持payload bytes最大値。"""
        with self._lock:
            return self._high_water_bytes

    @property
    def high_water_frame_count(self) -> int:
        """ring作成後の保持frame最大数。"""
        with self._lock:
            return self._high_water_frame_count

    @property
    def memory_snapshot(self) -> dict[str, int | None]:
        """現在値とpeak値を共有payload予算ベースで返す。"""
        with self._lock:
            return {
                "budget_bytes": self._max_bytes,
                "retained_payload_bytes": self._retained_bytes,
                "ring_payload_bytes": sum(
                    entry.frame.size_bytes for entry in self._entries
                ),
                "retained_frames": self._retained_frame_count,
                "ring_frames": len(self._entries),
                "high_water_payload_bytes": self._high_water_bytes,
                "high_water_frames": self._high_water_frame_count,
                "generation": self._generation,
            }

    @property
    def frame_count(self) -> int:
        """現在のring内のframe数。"""
        with self._lock:
            return len(self._entries)

    @property
    def max_bytes(self) -> int:
        """ringとleaseが共有する上限byte数。"""
        return self._max_bytes

    @property
    def max_duration_ns(self) -> int:
        """ringの最大保持時間。"""
        return self._max_duration_ns

    @property
    def generation(self) -> int | None:
        """現在ringに取り込まれるcamera generation。"""
        with self._lock:
            return self._generation

    def append(self, frame: FrameEnvelope) -> None:
        """新しいframeを追加する。順序違反やmemory超過は例外にする。"""
        if not isinstance(frame, FrameEnvelope):
            raise ValueError("frameはFrameEnvelopeで指定してください")
        if frame.size_bytes > self._max_bytes:
            raise FrameBufferFullError("1 frameがmemory上限を超えています")

        with self._lock:
            if self._generation is not None:
                if frame.generation < self._generation:
                    raise ValueError("古いgenerationのframeは追加できません")
                if frame.generation == self._generation:
                    if (
                        self._last_sequence is not None
                        and frame.sequence <= self._last_sequence
                    ):
                        raise ValueError(
                            "sequenceはgeneration内で増加する必要があります"
                        )
                    if (
                        self._last_mono_ns is not None
                        and frame.capture_mono_ns < self._last_mono_ns
                    ):
                        raise ValueError("capture monotonic時刻を逆行できません")

            if frame.generation != self._generation:
                self._clear_ring()
                self._generation = frame.generation
                self._last_sequence = None
                self._last_mono_ns = None

            cutoff_ns = frame.capture_mono_ns - self._max_duration_ns
            while self._entries and self._entries[0].frame.capture_mono_ns < cutoff_ns:
                self._evict_oldest()

            while self._retained_bytes + frame.size_bytes > self._max_bytes:
                if not self._entries:
                    raise FrameBufferFullError(
                        "active recording leaseがmemory予算を保持しています"
                    )
                self._evict_oldest()

            self._entries.append(_StoredFrame(frame=frame))
            self._retained_bytes += frame.size_bytes
            self._retained_frame_count += 1
            self._high_water_bytes = max(self._high_water_bytes, self._retained_bytes)
            self._high_water_frame_count = max(
                self._high_water_frame_count, self._retained_frame_count
            )
            self._last_sequence = frame.sequence
            self._last_mono_ns = frame.capture_mono_ns

    def acquire_range(
        self, start_mono_ns: int, end_mono_ns: int, generation: int
    ) -> FrameLease:
        """同generationの`[start,end)`を時刻順でpinする。"""
        if (
            type(start_mono_ns) is not int
            or type(end_mono_ns) is not int
            or start_mono_ns < 0
            or end_mono_ns < start_mono_ns
        ):
            raise ValueError("frame時刻範囲が不正です")
        if type(generation) is not int or generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        with self._lock:
            entries = tuple(
                entry
                for entry in self._entries
                if entry.frame.generation == generation
                and start_mono_ns <= entry.frame.capture_mono_ns < end_mono_ns
            )
            for entry in entries:
                entry.pin_count += 1
            return FrameLease(self, entries)

    def acquire_range_entries(
        self, start_mono_ns: int, end_mono_ns: int, generation: int
    ) -> tuple[tuple[FrameEnvelope, FrameLease], ...]:
        """同generationの時刻範囲を1回走査し、frame別leaseを返す。"""
        if (
            type(start_mono_ns) is not int
            or type(end_mono_ns) is not int
            or start_mono_ns < 0
            or end_mono_ns < start_mono_ns
        ):
            raise ValueError("frame時刻範囲が不正です")
        if type(generation) is not int or generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        with self._lock:
            entries = tuple(
                entry
                for entry in self._entries
                if entry.frame.generation == generation
                and start_mono_ns <= entry.frame.capture_mono_ns < end_mono_ns
            )
            leased_entries = tuple(
                (entry.frame, FrameLease(self, (entry,))) for entry in entries
            )
            for entry in entries:
                entry.pin_count += 1
            return leased_entries

    def acquire_frame(self, frame: FrameEnvelope) -> FrameLease | None:
        """まだring内にある同一envelopeをpinする。"""
        if not isinstance(frame, FrameEnvelope):
            raise ValueError("frameはFrameEnvelopeで指定してください")
        with self._lock:
            for entry in self._entries:
                if entry.frame is frame:
                    entry.pin_count += 1
                    return FrameLease(self, (entry,))
        return None

    def clear(self) -> None:
        """ring参照を消す。active leaseのframeはreleaseまで保持する。"""
        with self._lock:
            self._clear_ring()
            self._generation = None
            self._last_sequence = None
            self._last_mono_ns = None

    def _clear_ring(self) -> None:
        while self._entries:
            self._evict_oldest()

    def _evict_oldest(self) -> None:
        entry = self._entries.popleft()
        entry.in_ring = False
        if entry.pin_count == 0:
            self._retained_bytes -= entry.frame.size_bytes
            self._retained_frame_count -= 1

    def _release(self, entries: tuple[_StoredFrame, ...]) -> None:
        with self._lock:
            for entry in entries:
                if entry.pin_count < 1:
                    raise RuntimeError("frame leaseのpin状態が不正です")
                entry.pin_count -= 1
                if entry.pin_count == 0 and not entry.in_ring:
                    self._retained_bytes -= entry.frame.size_bytes
                    self._retained_frame_count -= 1

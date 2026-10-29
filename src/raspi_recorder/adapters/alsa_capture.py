"""USB parent portで固定したALSA capture deviceの検出と取得。"""

from __future__ import annotations

import os
import re
import select
import shutil
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from raspi_recorder.adapters.capture_process import CaptureProcess
from raspi_recorder.domain.frames import AudioEnvelope
from raspi_recorder.ports.clock import Clock

_PCM_CAPTURE = re.compile(r"^pcmC([0-9]+)D([0-9]+)c$")
_CARD = re.compile(r"^card([0-9]+)$")
_SAMPLE_RATE = 48_000
_BLOCK_SAMPLES = 480
_BLOCK_BYTES = _BLOCK_SAMPLES * 2
_BLOCK_DURATION_NS = _BLOCK_SAMPLES * 1_000_000_000 // _SAMPLE_RATE
_MAX_PENDING_BYTES = 16 * 1024 * 1024
_READY_TIMEOUT_NS = 2_000_000_000


class TimezoneName(Protocol):
    """OS IANA timezone source。"""

    def name(self) -> str:
        """現在のtimezone名を返す。"""
        ...


@dataclass(frozen=True, slots=True)
class AlsaCaptureDevice:
    """現在のcard番号と永続USB topology keyを結び付けたcapture PCM。"""

    interface_key: str
    usb_port_key: str
    usb_interface_key: str
    card_index: int
    pcm_device: int
    card_id: str

    @property
    def ffmpeg_device(self) -> str:
        """列挙時点のALSA hardware endpoint。card indexは永続化しない。"""
        return f"plughw:{self.card_index},{self.pcm_device}"

    @property
    def label(self) -> str:
        """設定画面向けの識別表示。"""
        return f"USB audio {self.card_id} / capture {self.pcm_device}"


class AlsaAudioDiscovery:
    """sysfs topologyからUSB capture PCMを列挙する。"""

    def __init__(self, sound_class_directory: str | Path = "/sys/class/sound") -> None:
        self._sound_class = Path(sound_class_directory)

    def enumerate(self) -> tuple[AlsaCaptureDevice, ...]:
        """現在のUSB ALSA capture deviceを安定key順に返す。"""
        devices: list[AlsaCaptureDevice] = []
        if not self._sound_class.is_dir():
            return ()
        for pcm in sorted(self._sound_class.glob("pcmC*D*c")):
            match = _PCM_CAPTURE.fullmatch(pcm.name)
            if match is None:
                continue
            try:
                device_path = pcm.resolve(strict=True)
            except OSError:
                continue
            usb_device, interface = self._usb_identity(device_path)
            if usb_device is None or interface is None:
                continue
            card_index = int(match.group(1))
            pcm_device = int(match.group(2))
            card_id = self._read_text(self._sound_class / f"card{card_index}" / "id")
            if card_id is None:
                card_id = f"card{card_index}"
            port_key = usb_device.name
            interface_key = interface.name
            devices.append(
                AlsaCaptureDevice(
                    interface_key=(f"{port_key}/{interface_key}/capture/{pcm_device}"),
                    usb_port_key=port_key,
                    usb_interface_key=interface_key,
                    card_index=card_index,
                    pcm_device=pcm_device,
                    card_id=card_id[:80],
                )
            )
        return tuple(
            sorted(
                devices,
                key=lambda item: (
                    item.usb_port_key,
                    item.usb_interface_key,
                    item.pcm_device,
                ),
            )
        )

    def for_usb_port(self, usb_port_key: str) -> tuple[AlsaCaptureDevice, ...]:
        """指定USB parent port配下のcapture endpointだけ返す。"""
        if not isinstance(usb_port_key, str) or not usb_port_key:
            return ()
        return tuple(
            device for device in self.enumerate() if device.usb_port_key == usb_port_key
        )

    def resolve(self, interface_key: str) -> AlsaCaptureDevice | None:
        """安定keyを現在のALSA card/device番号へ解決する。"""
        if not isinstance(interface_key, str) or not interface_key:
            return None
        return next(
            (
                device
                for device in self.enumerate()
                if device.interface_key == interface_key
            ),
            None,
        )

    @staticmethod
    def _usb_identity(device: Path) -> tuple[Path | None, Path | None]:
        interface: Path | None = None
        current = device
        while current != current.parent:
            if (current / "bInterfaceNumber").is_file():
                interface = current
            if (current / "idVendor").is_file() and (current / "idProduct").is_file():
                return current, interface
            current = current.parent
        return None, None

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None


class AudioBlockBuffer:
    """video取得時刻までのaudio blockを有界で保持する。"""

    def __init__(self, max_bytes: int = _MAX_PENDING_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes < _BLOCK_BYTES:
            raise ValueError("max_bytesは1 audio block以上が必要です")
        self._max_bytes = max_bytes
        self._entries: deque[AudioEnvelope] = deque()
        self._size_bytes = 0
        self._dropped_blocks = 0
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        """未割当PCM byte数。"""
        with self._lock:
            return self._size_bytes

    @property
    def dropped_blocks(self) -> int:
        """memory上限で破棄したblock数。"""
        with self._lock:
            return self._dropped_blocks

    def append(self, block: AudioEnvelope) -> None:
        """取得順blockを追加し、古い未割当blockを上限時だけ破棄する。"""
        if not isinstance(block, AudioEnvelope):
            raise ValueError("blockはAudioEnvelopeで指定してください")
        if block.size_bytes > self._max_bytes:
            with self._lock:
                self._dropped_blocks += 1
            return
        with self._lock:
            if self._entries and block.sequence <= self._entries[-1].sequence:
                raise ValueError("audio sequenceは増加する必要があります")
            while (
                self._entries and self._size_bytes + block.size_bytes > self._max_bytes
            ):
                removed = self._entries.popleft()
                self._size_bytes -= removed.size_bytes
                self._dropped_blocks += 1
            self._entries.append(block)
            self._size_bytes += block.size_bytes

    def take_before(self, capture_mono_ns: int) -> tuple[AudioEnvelope, ...]:
        """終端がframe取得時刻以前のblockを時刻順に割り当てる。"""
        if type(capture_mono_ns) is not int or capture_mono_ns < 0:
            raise ValueError("capture_mono_nsは0以上の整数で指定してください")
        with self._lock:
            selected: list[AudioEnvelope] = []
            while self._entries and self._entries[0].end_mono_ns <= capture_mono_ns:
                block = self._entries.popleft()
                self._size_bytes -= block.size_bytes
                selected.append(block)
            return tuple(selected)

    def clear(self) -> None:
        """不要になった未割当PCM blockを即時解放する。"""
        with self._lock:
            self._entries.clear()
            self._size_bytes = 0


class AlsaCaptureSource:
    """FFmpeg ALSA captureから固定48kHz mono PCMを有界blockで取得する。"""

    def __init__(
        self,
        interface_key: str,
        usb_port_key: str,
        generation: int,
        discovery: AlsaAudioDiscovery,
        clock: Clock,
        timezone: TimezoneName,
        *,
        ffmpeg_binary: str = "ffmpeg",
        retry_seconds: float = 1.0,
    ) -> None:
        if not isinstance(interface_key, str) or not interface_key:
            raise ValueError("interface_keyが必要です")
        if not isinstance(usb_port_key, str) or not usb_port_key:
            raise ValueError("usb_port_keyが必要です")
        if type(generation) is not int or generation < 1:
            raise ValueError("generationは1以上の整数が必要です")
        if type(retry_seconds) not in (int, float) or retry_seconds <= 0:
            raise ValueError("retry_secondsは正の数値が必要です")
        self._interface_key = interface_key
        self._usb_port_key = usb_port_key
        self._generation = generation
        self._discovery = discovery
        self._clock = clock
        self._timezone = timezone
        self._binary = ffmpeg_binary
        self._retry_seconds = float(retry_seconds)
        self._opened = False
        self._capture_process = CaptureProcess()
        self.last_error: str | None = None
        self.ready = False
        self.last_block_mono_ns: int | None = None
        self._last_end_mono_ns: int | None = None
        self._last_end_utc_ns: int | None = None
        self.stats = AudioCaptureStats()

    @property
    def active_process_pid(self) -> int | None:
        """現在所有しているALSA用FFmpeg child PID。"""
        return self._capture_process.active_pid

    def request_stop(self) -> None:
        """終了待ちを行わず、取得中のFFmpeg childへ停止要求を送る。"""
        self._capture_process.request_stop()

    def close(self, timeout_seconds: float = 2.0) -> None:
        """所有中のFFmpeg childを期限内に終了・回収する。"""
        self._capture_process.close(timeout_seconds)

    def frames(self, stop_event: threading.Event) -> Iterator[AudioEnvelope]:
        """device loss時は映像と独立して再接続を試みる。"""
        if self._opened:
            raise RuntimeError("同じAlsaCaptureSourceを二重に開けません")
        self._opened = True
        self.stats = AudioCaptureStats()
        binary = shutil.which(self._binary)
        if binary is None:
            self.last_error = "ffmpeg_missing"
            self._opened = False
            return
        sequence = 0
        try:
            while not stop_event.is_set():
                device = self._discovery.resolve(self._interface_key)
                if device is None or device.usb_port_key != self._usb_port_key:
                    self.ready = False
                    self.last_error = "audio_interface_unavailable"
                    stop_event.wait(self._retry_seconds)
                    continue
                command = [
                    binary,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "alsa",
                    "-channels",
                    "1",
                    "-sample_rate",
                    str(_SAMPLE_RATE),
                    "-i",
                    device.ffmpeg_device,
                    "-map",
                    "0:a:0",
                    "-ac",
                    "1",
                    "-ar",
                    str(_SAMPLE_RATE),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    "pipe:1",
                ]
                try:
                    process = self._capture_process.start(command)
                except OSError as exc:
                    self.ready = False
                    self.last_error = f"audio_process_{type(exc).__name__}"
                    stop_event.wait(self._retry_seconds)
                    continue
                if process.stdout is None:
                    self.close(2.0)
                    self.last_error = "audio_pipe_unavailable"
                    stop_event.wait(self._retry_seconds)
                    continue
                self.stats.backend = "ffmpeg-alsa"
                pending = bytearray()
                previous_end_mono_ns = self._last_end_mono_ns
                previous_end_utc_ns = self._last_end_utc_ns
                first_block_in_process = True
                last_block_mono_ns = self._clock.monotonic_ns()
                try:
                    while not stop_event.is_set():
                        readable, _, _ = select.select([process.stdout], [], [], 0.25)
                        if not readable:
                            if process.poll() is not None:
                                break
                            if (
                                self._clock.monotonic_ns() - last_block_mono_ns
                                > _READY_TIMEOUT_NS
                            ):
                                # 生存したままPCMを出さないprocessは回復しないため、
                                # readyを落として再接続ループで作り直す。
                                self.ready = False
                                break
                            continue
                        chunk = os.read(process.stdout.fileno(), 65_536)
                        if not chunk:
                            break
                        pending.extend(chunk)
                        now_mono_ns = self._clock.monotonic_ns()
                        now_utc_ns = self._clock.utc_ns()
                        pending_duration_ns = (
                            len(pending) // 2 * 1_000_000_000 // _SAMPLE_RATE
                        )
                        batch_start_mono_ns = max(0, now_mono_ns - pending_duration_ns)
                        batch_start_utc_ns = max(0, now_utc_ns - pending_duration_ns)
                        while len(pending) >= _BLOCK_BYTES:
                            pcm = bytes(pending[:_BLOCK_BYTES])
                            del pending[:_BLOCK_BYTES]
                            if first_block_in_process:
                                start_mono_ns = batch_start_mono_ns
                                start_utc_ns = batch_start_utc_ns
                                if previous_end_mono_ns is not None:
                                    if start_mono_ns < previous_end_mono_ns:
                                        start_utc_ns = (
                                            previous_end_utc_ns
                                            if previous_end_utc_ns is not None
                                            else start_utc_ns
                                        )
                                        start_mono_ns = previous_end_mono_ns
                                first_block_in_process = False
                            elif previous_end_mono_ns is None:
                                start_mono_ns = batch_start_mono_ns
                                start_utc_ns = batch_start_utc_ns
                            else:
                                start_mono_ns = previous_end_mono_ns
                                start_utc_ns = previous_end_utc_ns or 0
                            block = AudioEnvelope(
                                sequence=sequence + 1,
                                capture_mono_ns=start_mono_ns,
                                capture_utc_ns=start_utc_ns,
                                timezone=self._timezone.name(),
                                generation=self._generation,
                                sample_rate=_SAMPLE_RATE,
                                pcm_s16le=pcm,
                            )
                            sequence += 1
                            self.stats.blocks += 1
                            self.stats.bytes += len(pcm)
                            self.last_block_mono_ns = now_mono_ns
                            last_block_mono_ns = now_mono_ns
                            self.ready = True
                            self.last_error = None
                            previous_end_mono_ns = block.end_mono_ns
                            previous_end_utc_ns = (
                                block.capture_utc_ns + block.duration_ns
                            )
                            self._last_end_mono_ns = previous_end_mono_ns
                            self._last_end_utc_ns = previous_end_utc_ns
                            yield block
                finally:
                    if stop_event.is_set():
                        self.request_stop()
                    self.close(2.0)
                if not stop_event.is_set():
                    self.ready = False
                    self.last_error = f"audio_ffmpeg_exit_{process.returncode}"
                if not stop_event.is_set():
                    stop_event.wait(self._retry_seconds)
        finally:
            self.ready = False
            self._opened = False


@dataclass(slots=True)
class AudioCaptureStats:
    """audio capture sourceの最低限の観測値。"""

    blocks: int = 0
    bytes: int = 0
    backend: str | None = None

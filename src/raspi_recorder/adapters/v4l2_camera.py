"""独立したFFmpeg processで取得するLinux V4L2 USB camera source。"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shutil
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Protocol

from raspi_recorder.adapters.capture_process import CaptureProcess
from raspi_recorder.adapters.jpeg_stream import JpegStream
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import FrameEnvelope
from raspi_recorder.ports.camera import CameraFrameSource
from raspi_recorder.ports.clock import Clock

_FFMPEG_FRAME_TIMEOUT_SECONDS = 5.0


class TimezoneName(Protocol):
    """OS IANA timezone source。"""

    def name(self) -> str:
        """現在のOS timezone名を返す。"""
        ...


@dataclass(frozen=True, slots=True)
class V4L2CaptureConfig:
    """V4L2 inputで明示するdevice・profile・generation。"""

    device_path: str
    camera_id: int
    generation: int
    input_format: str
    settings: CameraSettings

    def __post_init__(self) -> None:
        path = Path(self.device_path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("device_pathは絶対pathで指定してください")
        if not self.device_path.startswith("/dev/v4l/by-path/"):
            raise ValueError("device_pathはUSBポート固定のby-pathを指定してください")
        if type(self.camera_id) is not int or self.camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        if self.input_format not in {"mjpeg", "yuyv422"}:
            raise ValueError("input_formatはmjpegまたはyuyv422で指定してください")
        if not isinstance(self.settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")


@dataclass(slots=True)
class CaptureStats:
    """camera workerのframe取込カウンター。"""

    packets: int = 0
    frames: int = 0
    dropped_frames: int = 0
    last_error: str | None = None
    backend: str | None = None


class V4L2CameraSource(CameraFrameSource):
    """カメラごとの単一worker向けcapture source。"""

    def __init__(
        self,
        config: V4L2CaptureConfig,
        clock: Clock,
        timezone: TimezoneName,
        *,
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        if not isinstance(ffmpeg_binary, str) or not ffmpeg_binary:
            raise ValueError("ffmpeg_binaryが必要です")
        self.config = config
        self._clock = clock
        self._timezone = timezone
        self._ffmpeg_binary = ffmpeg_binary
        self.stats = CaptureStats()
        self._opened = False
        self._capture_process = CaptureProcess()

    def request_stop(self) -> None:
        """FFmpegへ停止要求を送り、device readやnative demuxを待たない。"""
        self._capture_process.request_stop()

    def close(self, timeout_seconds: float = 2.0) -> None:
        """所有中のcapture processを期限内に終了・回収する。"""
        self._capture_process.close(timeout_seconds)

    @property
    def active_process_pid(self) -> int | None:
        """capture processがdeviceを所有している間はPIDを返す。"""
        return self._capture_process.active_pid

    def frames(self, stop_event: Event) -> Iterator[FrameEnvelope]:
        """V4L2を別processで一度開き、取得順のJPEG frameをyieldする。

        A device read can block in native code. Keeping it out of the engine
        lets stop terminate it without closing a PyAV container from another
        thread while that thread may still be demuxing or decoding.
        """
        if self._opened:
            raise RuntimeError("同じV4L2CameraSourceを二重に開けません")
        self._opened = True
        self.stats = CaptureStats()
        try:
            if not stop_event.is_set():
                yield from self._frames_with_ffmpeg(stop_event)
        finally:
            self._opened = False

    def _envelope(
        self,
        sequence: int,
        jpeg: bytes,
        capture_mono_ns: int,
        capture_utc_ns: int,
        timezone_name: str,
    ) -> FrameEnvelope:
        return FrameEnvelope(
            sequence=sequence,
            capture_mono_ns=capture_mono_ns,
            capture_utc_ns=capture_utc_ns,
            timezone=timezone_name,
            generation=self.config.generation,
            jpeg_bytes=jpeg,
        )

    def _verify_frame_rate(self) -> None:
        """起動したdriverの実frame intervalを読み、暗黙のfps変更を拒否する。"""
        # v4l2_streamparm: type (u32) + parm union (200 bytes).
        # VIDIOC_G_PARM is read-only; it must run after FFmpeg negotiated input.
        payload = bytearray(204)
        struct.pack_into("<I", payload, 0, 1)  # VIDEO_CAPTURE
        fd = os.open(
            self.config.device_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        )
        try:
            fcntl.ioctl(fd, 0xC0CC5615, payload, True)
        except OSError as exc:
            self.stats.last_error = "camera_frame_rate_unverified"
            raise RuntimeError("カメラの実際のfpsを確認できません") from exc
        finally:
            os.close(fd)
        numerator, denominator = struct.unpack_from("<II", payload, 12)
        requested = self.config.settings.fps
        # Allow nominal 30fps vs 30000/1001, but reject e.g. 30 -> 15fps.
        if (
            numerator == 0
            or denominator == 0
            or abs(denominator / numerator / requested - 1) > 0.01
        ):
            self.stats.last_error = "camera_frame_rate_mismatch"
            raise RuntimeError("カメラの実際のfpsが指定したfpsと異なります")
        self._disable_dynamic_frame_rate()

    def _disable_dynamic_frame_rate(self) -> None:
        """Keep auto exposure from silently reducing a supported frame rate."""
        # V4L2_CID_EXPOSURE_AUTO_PRIORITY permits variable frame intervals
        # even when G_PARM reports the requested nominal rate. Cameras that
        # do not implement this optional control return EINVAL/ENOTTY.
        control = bytearray(struct.pack("<Ii", 0x009A0903, 0))
        fd = os.open(self.config.device_path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            try:
                fcntl.ioctl(fd, 0xC008561B, control, True)  # VIDIOC_G_CTRL
            except OSError as exc:
                if exc.errno in {errno.EINVAL, errno.ENOTTY}:
                    return
                raise
            if struct.unpack_from("<i", control, 4)[0] == 0:
                return
            struct.pack_into("<i", control, 4, 0)
            fcntl.ioctl(fd, 0xC008561C, control, True)  # VIDIOC_S_CTRL
            fcntl.ioctl(fd, 0xC008561B, control, True)
            if struct.unpack_from("<i", control, 4)[0] != 0:
                raise RuntimeError("カメラのfpsを固定できません")
        except (OSError, RuntimeError) as exc:
            self.stats.last_error = "camera_frame_rate_unverified"
            raise RuntimeError("カメラのfpsを固定できません") from exc
        finally:
            os.close(fd)

    def _frames_with_ffmpeg(self, stop_event: Event) -> Iterator[FrameEnvelope]:
        """OS FFmpegでMJPEGをcopyし、YUYVはJPEGへ変換して渡す。"""
        binary = shutil.which(self._ffmpeg_binary)
        if binary is None:
            raise RuntimeError("カメラ取得にはOS ffmpegが必要です")
        command = [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-input_format",
            self.config.input_format,
            "-video_size",
            f"{self.config.settings.width}x{self.config.settings.height}",
            "-framerate",
            str(self.config.settings.fps),
            "-i",
            self.config.device_path,
            "-map",
            "0:v:0",
            "-an",
        ]
        if self.config.input_format == "mjpeg":
            command.extend(("-c:v", "copy"))
        else:
            command.extend(("-c:v", "mjpeg", "-q:v", "3"))
        command.extend(("-fps_mode", "passthrough", "-f", "image2pipe", "pipe:1"))
        process = self._capture_process.start(command)
        self.stats.backend = "ffmpeg-v4l2"
        if process.stdout is None:
            self.close(2.0)
            raise RuntimeError("FFmpeg V4L2 pipeを開けません")
        frames = JpegStream()
        sequence = 0
        last_frame_at = time.monotonic()
        try:
            while not stop_event.is_set():
                if time.monotonic() - last_frame_at >= _FFMPEG_FRAME_TIMEOUT_SECONDS:
                    self.stats.last_error = "camera_frame_timeout"
                    raise TimeoutError("camera_frame_timeout")
                readable, _, _ = select.select([process.stdout], [], [], 0.5)
                if not readable:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                for jpeg in frames.feed(chunk):
                    if sequence == 0:
                        # A V4L2 driver may negotiate a different size despite
                        # -video_size. Do not report capture-ready for it.
                        from PIL import Image

                        with Image.open(BytesIO(jpeg)) as image:
                            if image.size != (
                                self.config.settings.width,
                                self.config.settings.height,
                            ):
                                raise RuntimeError(
                                    "実camera解像度が要求profileと異なります"
                                )
                        self._verify_frame_rate()
                    sequence += 1
                    self.stats.packets += 1
                    self.stats.frames += 1
                    last_frame_at = time.monotonic()
                    yield self._envelope(
                        sequence,
                        jpeg,
                        self._clock.monotonic_ns(),
                        self._clock.utc_ns(),
                        self._timezone.name(),
                    )
                self.stats.dropped_frames = frames.dropped_frames
            return_code = process.poll()
            if not stop_event.is_set() and return_code not in (None, 0):
                self.stats.last_error = f"ffmpeg_exit_{return_code}"
        finally:
            self.close(2.0)

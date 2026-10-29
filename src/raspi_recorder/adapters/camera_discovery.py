"""USBポート固定のUVCカメラ列挙と申告profile取得。"""

from __future__ import annotations

import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from raspi_recorder.adapters.alsa_capture import (
    AlsaAudioDiscovery,
    AlsaCaptureDevice,
)


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


def _fourcc(value: str) -> int:
    return int.from_bytes(value.encode("ascii"), "little")


_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_FRMSIZE_TYPE_DISCRETE = 1
_V4L2_FRMIVAL_TYPE_DISCRETE = 1
_V4L2_IOCTL_BUDGET = 4096
_V4L2_ENUM_FMT = _iowr(2, 64)
_V4L2_ENUM_FRAMESIZES = _iowr(74, 44)
_V4L2_ENUM_FRAMEINTERVALS = _iowr(75, 52)
_PIXEL_FORMATS = {
    _fourcc("MJPG"): "mjpeg",
    _fourcc("YUYV"): "yuyv422",
}


class _IoctlBudget:
    def __init__(self) -> None:
        self.remaining = _V4L2_IOCTL_BUDGET

    def query(self, fd: int, request: int, payload: bytearray) -> None:
        if self.remaining <= 0:
            raise OSError(errno.EOVERFLOW, "V4L2 capability enumeration limit reached")
        self.remaining -= 1
        fcntl.ioctl(fd, request, payload, True)


def _enumerate_ioctl_profiles(fd: int) -> tuple[V4L2Profile, ...]:
    budget = _IoctlBudget()
    profile_rates: dict[tuple[str, int, int], list[int]] = {}
    try:
        for format_index in range(_V4L2_IOCTL_BUDGET):
            format_desc = bytearray(64)
            struct.pack_into(
                "<II", format_desc, 0, format_index, _V4L2_BUF_TYPE_VIDEO_CAPTURE
            )
            try:
                budget.query(fd, _V4L2_ENUM_FMT, format_desc)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    break
                raise
            pixel_format = struct.unpack_from("<I", format_desc, 44)[0]
            input_format = _PIXEL_FORMATS.get(pixel_format)
            if input_format is None:
                continue

            for size_index in range(_V4L2_IOCTL_BUDGET):
                size_desc = bytearray(44)
                struct.pack_into("<II", size_desc, 0, size_index, pixel_format)
                try:
                    budget.query(fd, _V4L2_ENUM_FRAMESIZES, size_desc)
                except OSError as exc:
                    if exc.errno == errno.EINVAL:
                        break
                    raise
                size_type = struct.unpack_from("<I", size_desc, 8)[0]
                if size_type != _V4L2_FRMSIZE_TYPE_DISCRETE:
                    continue
                width, height = struct.unpack_from("<II", size_desc, 12)
                if width == 0 or height == 0:
                    continue

                key = (input_format, width, height)
                rates = profile_rates.setdefault(key, [])
                for interval_index in range(_V4L2_IOCTL_BUDGET):
                    interval_desc = bytearray(52)
                    struct.pack_into(
                        "<IIII",
                        interval_desc,
                        0,
                        interval_index,
                        pixel_format,
                        width,
                        height,
                    )
                    try:
                        budget.query(fd, _V4L2_ENUM_FRAMEINTERVALS, interval_desc)
                    except OSError as exc:
                        if exc.errno == errno.EINVAL:
                            break
                        raise
                    interval_type = struct.unpack_from("<I", interval_desc, 16)[0]
                    if interval_type != _V4L2_FRMIVAL_TYPE_DISCRETE:
                        continue
                    numerator, denominator = struct.unpack_from(
                        "<II", interval_desc, 20
                    )
                    if numerator == 0 or denominator == 0:
                        continue
                    fps = (2 * denominator + numerator) // (2 * numerator)
                    if 1 <= fps <= 120 and fps not in rates:
                        rates.append(fps)
        else:
            raise OSError(errno.EOVERFLOW, "too many V4L2 pixel formats")
    except OSError:
        # 1件のioctl失敗や予算超過で、検証済みのprofileまで全て捨てない。
        # そのdeviceについては取得できた分だけをprofileとして返す
        # (device単位の失敗分離は呼び出し側の `if not profiles` が担う)。
        pass

    profiles = [
        V4L2Profile(input_format, width, height, tuple(rates))
        for (input_format, width, height), rates in profile_rates.items()
        if rates
    ]
    profiles.sort(key=lambda item: (item.width * item.height, item.input_format))
    return tuple(profiles)


@dataclass(frozen=True, slots=True)
class V4L2Profile:
    input_format: str
    width: int
    height: int
    fps: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CameraPort:
    port_key: str
    video_interface_key: str
    device_path: str
    product: str
    profiles: tuple[V4L2Profile, ...]
    audio_interfaces: tuple[AlsaCaptureDevice, ...] = ()


class V4L2CameraDiscovery:
    """metadata nodeを除外し、USB親port単位でcapture nodeを識別する。"""

    def __init__(
        self,
        by_path_directory: str | Path = "/dev/v4l/by-path",
        sys_class_directory: str | Path = "/sys/class/video4linux",
        *,
        audio_discovery: AlsaAudioDiscovery | None = None,
    ) -> None:
        self._by_path = Path(by_path_directory)
        self._sys_class = Path(sys_class_directory)
        self._audio_discovery = (
            AlsaAudioDiscovery() if audio_discovery is None else audio_discovery
        )

    def enumerate(self) -> tuple[CameraPort, ...]:
        """USB camera capture interfaceを一意なUSB親portで返す。"""
        if not self._by_path.is_dir():
            return ()
        devices: dict[str, CameraPort] = {}
        for link in sorted(self._by_path.glob("*-video-index0")):
            try:
                resolved = link.resolve(strict=True)
                if not resolved.name.startswith("video"):
                    continue
                sys_device = (self._sys_class / resolved.name / "device").resolve(
                    strict=True
                )
            except OSError:
                continue
            usb_device, interface = self._usb_identity(sys_device)
            if usb_device is None or interface is None:
                continue
            port_key = usb_device.name
            interface_key = interface.name
            profiles = self._profiles(link)
            if not profiles:
                continue
            product = self._read_text(usb_device / "product") or "USB camera"
            port = CameraPort(
                port_key=port_key,
                video_interface_key=interface_key,
                device_path=str(link),
                product=product[:160],
                profiles=profiles,
                audio_interfaces=self._audio_discovery.for_usb_port(port_key),
            )
            previous = devices.get(port_key)
            previous_key = (
                None
                if previous is None
                else (previous.video_interface_key, previous.device_path)
            )
            if previous_key is None or previous_key > (
                port.video_interface_key,
                port.device_path,
            ):
                devices[port_key] = port
        return tuple(devices[key] for key in sorted(devices))

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

    def _profiles(self, link: Path) -> tuple[V4L2Profile, ...]:
        try:
            fd = os.open(link, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return ()
        try:
            return _enumerate_ioctl_profiles(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

"""隔離した録画engineへ合成カメラ入力を注入する受入fixture。"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.camera_discovery import (
    CameraPort,
    V4L2CameraDiscovery,
    V4L2Profile,
)
from raspi_recorder.adapters.engine_socket import EngineSocketClient
from raspi_recorder.adapters.linux_storage import LinuxVolumeScanner, ManagedVolumeStore
from raspi_recorder.adapters.system_clock import SystemClock, SystemTimezone
from raspi_recorder.adapters.v4l2_camera import (
    V4L2CameraSource,
    V4L2CaptureConfig,
)
from raspi_recorder.adapters.web_app import create_app
from raspi_recorder.adapters.werkzeug_password_hasher import WerkzeugPasswordHasher
from raspi_recorder.application.authentication import AuthenticationService
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.domain.frames import FrameEnvelope
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.engine.runtime import RecorderRuntime
from raspi_recorder.ports.camera import CameraFrameSource
from raspi_recorder.ports.clock import Clock
from raspi_recorder.ports.storage import MountedVolume


class AcceptancePreconditionError(RuntimeError):
    """Required hardware or temporary disk space is unavailable."""


@dataclass(slots=True)
class SourceStats:
    dropped_frames: int = 0
    last_error: str | None = None


class VirtualCameraControl:
    def __init__(self) -> None:
        self.paused = threading.Event()
        self.invalid_jpeg = threading.Event()
        self.extra_delay = threading.Event()


class VirtualCameraSource(CameraFrameSource):
    """JPEGフレームを一定周期で返す、tests/限定のcamera source。"""

    def __init__(
        self,
        config: V4L2CaptureConfig,
        clock: Clock,
        timezone: SystemTimezone,
        control: VirtualCameraControl,
    ) -> None:
        self.config = config
        self._clock = clock
        self._timezone = timezone
        self._control = control
        self._closed = threading.Event()
        self.stats = SourceStats()
        self._jpeg = self._make_jpeg()

    def _make_jpeg(self) -> bytes:
        from PIL import Image, ImageDraw

        color = (210, 45, 35) if self.config.camera_id == 1 else (25, 80, 210)
        image = Image.new(
            "RGB",
            (self.config.settings.width, self.config.settings.height),
            color=color,
        )
        ImageDraw.Draw(image).text(
            (12, 12), f"VIRTUAL CAMERA {self.config.camera_id}", fill="white"
        )
        output = BytesIO()
        image.save(output, format="JPEG", quality=80)
        return output.getvalue()

    def frames(self, stop_event: threading.Event):
        sequence = 0
        cadence = 1 / self.config.settings.fps
        while not stop_event.is_set() and not self._closed.is_set():
            if self._control.paused.is_set():
                self._closed.wait(0.025)
                continue
            delay = cadence * (2 if self._control.extra_delay.is_set() else 1)
            if stop_event.wait(delay) or self._closed.is_set():
                break
            sequence += 1
            jpeg = b"not-a-jpeg" if self._control.invalid_jpeg.is_set() else self._jpeg
            yield FrameEnvelope(
                sequence=sequence,
                capture_mono_ns=self._clock.monotonic_ns(),
                capture_utc_ns=self._clock.utc_ns(),
                timezone=self._timezone.name(),
                generation=self.config.generation,
                jpeg_bytes=jpeg,
            )

    def request_stop(self) -> None:
        self._closed.set()

    def close(self, timeout_seconds: float = 2.0) -> None:
        del timeout_seconds
        self._closed.set()


class StaticVolumeScanner:
    def __init__(self, volume: MountedVolume) -> None:
        self._volume = volume

    def scan(self) -> tuple[MountedVolume, ...]:
        return (self._volume,)


class AcceptanceDiscovery:
    """実機1台＋synthetic port、またはhost上のsynthetic 2台を返す。"""

    def __init__(self, *, real_camera_1: bool) -> None:
        if real_camera_1:
            physical = V4L2CameraDiscovery().enumerate()
            if len(physical) != 1:
                raise AcceptancePreconditionError(
                    f"実カメラは1台である必要があります。検出数: {len(physical)}"
                )
            first = physical[0]
            if not first.profiles:
                raise AcceptancePreconditionError(
                    "実カメラに利用可能なprofileがありません"
                )
            self.camera_1 = first
            virtual_profile = min(
                first.profiles,
                key=lambda item: (item.width * item.height, max(item.fps)),
            )
        else:
            virtual_profile = V4L2Profile("mjpeg", 320, 240, (5,))
            self.camera_1 = self._virtual_port("acceptance-camera-1", virtual_profile)
        self.camera_2 = self._virtual_port(
            "acceptance-virtual-camera-2", virtual_profile
        )

    @staticmethod
    def _virtual_port(port_key: str, profile: V4L2Profile) -> CameraPort:
        suffix = port_key.replace("_", "-")
        return CameraPort(
            port_key=port_key,
            video_interface_key=f"{port_key}:1.0",
            device_path=f"/dev/v4l/by-path/{suffix}-video-index0",
            product="Acceptance virtual input",
            profiles=(profile,),
        )

    def enumerate(self) -> tuple[CameraPort, ...]:
        return self.camera_1, self.camera_2


@dataclass(slots=True)
class AcceptanceEnvironment:
    runtime: RecorderRuntime
    app: Any
    controls: dict[int, VirtualCameraControl]
    volume_root: Path
    camera_1_port_key: str


def create_acceptance_environment(
    root: Path,
    *,
    real_camera_1: bool = False,
    mounted_volume: MountedVolume | None = None,
    volume_store: ManagedVolumeStore | None = None,
    trusted_hosts: tuple[str, ...] = ("localhost",),
) -> AcceptanceEnvironment:
    """一時DB・volume・socketでengineと本物のFlask APIを起動する。"""

    root.mkdir(parents=True, exist_ok=True)
    storage_mount = root / "storage"
    if mounted_volume is None:
        storage_mount.mkdir()
        usage = os.statvfs(storage_mount)
    else:
        if (
            mounted_volume.kind is not VolumeKind.USB
            or mounted_volume.filesystem not in {"ext4", "exfat"}
            or mounted_volume.read_only
            or mounted_volume.mountpoint == Path("/")
        ):
            raise AcceptancePreconditionError(
                "受入fixtureの保存先は書込可能なUSB媒体が必要です"
            )
        matches = tuple(
            candidate
            for candidate in LinuxVolumeScanner().scan()
            if candidate.persistent_id == mounted_volume.persistent_id
        )
        if len(matches) != 1 or matches[0] != mounted_volume:
            raise AcceptancePreconditionError(
                "指定USB媒体のUUID・mount状態を再確認できません"
            )
        usage = os.statvfs(mounted_volume.mountpoint)
    available = usage.f_bavail * usage.f_frsize
    total = usage.f_blocks * usage.f_frsize
    if available < 1_300_000_000 or total < 1_300_000_000:
        raise AcceptancePreconditionError(
            "受入fixtureの一時領域に1.3GB以上の空き容量が必要です。"
            "録画先や本番DBは変更していません。"
        )
    volume = mounted_volume or MountedVolume(
        persistent_id="raspi-recorder-acceptance-only",
        device_path=str(storage_mount),
        mountpoint=storage_mount,
        filesystem="ext4",
        kind=VolumeKind.USB,
        device_number=storage_mount.stat().st_dev,
        filesystem_bytes=total,
        available_bytes=available,
        read_only=False,
    )
    discovery = AcceptanceDiscovery(real_camera_1=real_camera_1)
    profile = discovery.camera_1.profiles[0]
    if real_camera_1:
        profile = min(
            discovery.camera_1.profiles,
            key=lambda item: (item.width * item.height, max(item.fps)),
        )
    controls = {1: VirtualCameraControl(), 2: VirtualCameraControl()}
    clock = SystemClock()
    selected_volume_store = volume_store or ManagedVolumeStore(
        application_directory=f"raspi-recorder-acceptance-{uuid.uuid4().hex}"
    )

    def source_factory(
        config: V4L2CaptureConfig, source_clock: Clock, source_timezone: SystemTimezone
    ) -> CameraFrameSource:
        if real_camera_1 and config.camera_id == 1:
            return V4L2CameraSource(config, source_clock, source_timezone)
        return VirtualCameraSource(
            config, source_clock, source_timezone, controls[config.camera_id]
        )

    socket_path = root / "engine.sock"
    runtime = RecorderRuntime(
        root / "state",
        socket_path=socket_path,
        discovery=discovery,  # type: ignore[arg-type]
        volume_scanner=(
            LinuxVolumeScanner()
            if mounted_volume is not None
            else StaticVolumeScanner(volume)
        ),  # type: ignore[arg-type]
        volume_store=selected_volume_store,
        camera_source_factory=source_factory,
        live_root=root / "run" / "live",
    )
    current = runtime.settings_service.initialize()
    profile_values = (profile.width, profile.height, min(profile.fps))
    settings = ApplicationSettings(
        revision=current.revision,
        auto_delete_start_tenths_gb=11,
        auto_recover_tenths_gb=12,
        volume_kind=VolumeKind.USB,
        volume_persistent_id=volume.persistent_id,
        power_saving_enabled=False,
        cameras=(
            CameraSettings(
                width=profile_values[0],
                height=profile_values[1],
                fps=profile_values[2],
                audio_enabled=False,
                overlay_enabled=False,
                usb_port_key=discovery.camera_1.port_key,
                video_interface_key=discovery.camera_1.video_interface_key,
            ),
            CameraSettings(
                width=profile_values[0],
                height=profile_values[1],
                fps=profile_values[2],
                audio_enabled=False,
                overlay_enabled=False,
                usb_port_key=discovery.camera_2.port_key,
                video_interface_key=discovery.camera_2.video_interface_key,
            ),
        ),
    )
    runtime.settings_service.compare_and_swap(settings, current.revision)
    try:
        runtime.start()

        auth = AuthenticationService(
            AuthenticationStore(root / "state" / "authentication.sqlite3"),
            WerkzeugPasswordHasher(),
            clock,
        )
        auth.initialize()
        app = create_app(
            auth,
            EngineSocketClient(socket_path),
            trusted_hosts=trusted_hosts,
        )
        app.testing = True
    except BaseException as exc:
        try:
            runtime.close()
        except BaseException as cleanup_error:
            exc.add_note(f"acceptance runtime cleanup failed: {cleanup_error!r}")
        raise
    return AcceptanceEnvironment(
        runtime,
        app,
        controls,
        selected_volume_store.recordings_root(volume),
        discovery.camera_1.port_key,
    )

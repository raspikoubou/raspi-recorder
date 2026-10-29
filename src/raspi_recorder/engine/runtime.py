"""録画engineのruntime・camera worker supervision。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from raspi_recorder import __version__
from raspi_recorder.adapters.alsa_capture import (
    AlsaAudioDiscovery,
    AlsaCaptureSource,
    AudioBlockBuffer,
)
from raspi_recorder.adapters.camera_discovery import CameraPort, V4L2CameraDiscovery
from raspi_recorder.adapters.catalogued_writer import CataloguedWriterFactory
from raspi_recorder.adapters.engine_socket import EngineSocketServer
from raspi_recorder.adapters.export_jobs import ExportJobService
from raspi_recorder.adapters.github_releases import release_source_from_environment
from raspi_recorder.adapters.linux_storage import LinuxVolumeScanner, ManagedVolumeStore
from raspi_recorder.adapters.live_hls import LiveHlsPublisher
from raspi_recorder.adapters.live_preview import LivePreviewPublisher
from raspi_recorder.adapters.recording_catalog import RecordingCatalog
from raspi_recorder.adapters.settings_repository import SettingsRepository
from raspi_recorder.adapters.sqlite_store import RequestIdConflict
from raspi_recorder.adapters.system_clock import SystemClock, SystemTimezone
from raspi_recorder.adapters.system_clock_setter import SystemClockSetter
from raspi_recorder.adapters.update_bundle import TarZstdUpdateBundleValidator
from raspi_recorder.adapters.update_helper_client import (
    UpdateHelperClient,
    UpdateHelperClientError,
)
from raspi_recorder.adapters.v4l2_camera import V4L2CameraSource, V4L2CaptureConfig
from raspi_recorder.application.capacity_manager import (
    CapacityManager,
    CapacitySnapshot,
)
from raspi_recorder.application.frame_ingress import CameraFramePipeline
from raspi_recorder.application.recording_access import (
    RecordingAccessError,
    RecordingAccessService,
)
from raspi_recorder.application.recording_workers import (
    record_continuous_session,
)
from raspi_recorder.application.settings_service import SettingsService
from raspi_recorder.application.update_service import (
    UpdateService,
    UpdateServiceError,
)
from raspi_recorder.domain.camera import CameraSettings, RecordingMode
from raspi_recorder.domain.errors import (
    ClockOperationError,
    UnsupportedCameraProfileError,
)
from raspi_recorder.domain.frames import FrameEnvelope, FrameRing
from raspi_recorder.domain.operations import OperationSnapshot, OperationStage
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.ports.camera import CameraFrameSource
from raspi_recorder.ports.clock import (
    Clock,
    ClockSetError,
    ClockSetter,
    ClockSyncStatus,
)
from raspi_recorder.ports.engine import EngineResponse, EngineStreamResponse
from raspi_recorder.ports.storage import ManagedVolumeLocator, MountedVolume

CameraSourceFactory = Callable[
    [V4L2CaptureConfig, Clock, SystemTimezone], CameraFrameSource
]

# JPEG/PCM payload cap shared by the ring, continuous queue, and leases.
# Those queues reference the same FrameEnvelope payloads, so they are not added
# as separate copies. This is a payload budget, not a process RSS limit.
_FRAME_PAYLOAD_BUDGET_BYTES = 384 * 1024 * 1024
_MAX_ENGINE_REQUEST_CACHE = 1_024
_SUPERVISION_INTERVAL_SECONDS = 1.0
_CAMERA_WORKER_STOP_TIMEOUT_SECONDS = 45.0
_UNCERTAIN_UPDATE_OPERATION_TIMEOUT_SECONDS = 600.0
_LIVE_ROOT = Path("/run/raspi-recorder/live")
_OVERLOAD_ERRORS = frozenset({"FrameQueueError", "FrameBufferFullError"})


class CameraWorkerStopTimeout(TimeoutError):
    """worker停止期限を過ぎても残っているthreadを示す。"""

    def __init__(self, camera_id: int, workers: tuple[str, ...]) -> None:
        self.camera_id = camera_id
        self.workers = workers
        super().__init__(
            f"camera {camera_id} worker shutdown timed out: {', '.join(workers)}"
        )


class RecorderRuntimeStopTimeout(TimeoutError):
    """runtime shutdown deadline expired before a supervised thread exited."""

    def __init__(self, worker_name: str) -> None:
        self.worker_name = worker_name
        super().__init__(f"runtime shutdown timed out: {worker_name}")


class RuntimeTransitionBusy(RuntimeError):
    """Another bounded configuration transition currently owns the runtime."""


def _worker_error_blocker(last_error: str | None) -> str:
    """bounded frame/job capacity failures remain distinct from I/O failures."""
    return "overload" if last_error in _OVERLOAD_ERRORS else "io_error"


def _effective_camera_settings(
    camera_settings: CameraSettings,
    power_saving_enabled: bool,
    port: CameraPort | None,
) -> tuple[CameraSettings, bool]:
    """Derive runtime settings without changing the user's saved preferences."""
    if not power_saving_enabled:
        return camera_settings, False

    effective = replace(camera_settings, audio_enabled=False)
    if port is None:
        return effective, False

    profiles = {
        (profile.width, profile.height, fps)
        for profile in port.profiles
        for fps in profile.fps
        if profile.width > 0 and profile.height > 0 and fps > 0
    }
    lower_profiles = [
        profile
        for profile in profiles
        if profile[0] <= camera_settings.width
        and profile[1] <= camera_settings.height
        and profile[2] <= camera_settings.fps
        and profile
        != (camera_settings.width, camera_settings.height, camera_settings.fps)
    ]
    if not lower_profiles:
        return effective, False

    width, height, fps = min(
        lower_profiles,
        key=lambda profile: (
            profile[0] * profile[1] * profile[2],
            profile[0] * profile[1],
            profile[2],
            profile[0],
            profile[1],
        ),
    )
    return replace(effective, width=width, height=height, fps=fps), True


@dataclass(slots=True)
class CameraRuntime:
    camera_id: int
    state_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    desired_running: bool = True
    state: str = "stopped"
    blockers: set[str] = field(default_factory=set)
    generation: int = 0
    worker: CameraCaptureWorker | None = None
    last_error: str | None = None
    last_frame_utc_ns: int | None = None
    failure_count: int = 0
    retry_after_mono_ns: int = 0
    stop_failed_worker: CameraCaptureWorker | None = None
    stop_failure_count: int = 0
    stop_retry_after_mono_ns: int = 0


class CameraCaptureWorker:
    """1台の物理cameraを一度開き、bounded sessionへframeを渡す。"""

    def __init__(
        self,
        *,
        camera_id: int,
        generation: int,
        settings: CameraSettings,
        port: CameraPort,
        recording_enabled: bool,
        clock: Clock,
        timezone: SystemTimezone,
        catalog: RecordingCatalog,
        volume_store: ManagedVolumeStore | None,
        volume: MountedVolume | None,
        recordings_root: Path | None,
        volume_id: str | None,
        engine_epoch: str,
        settings_revision: int,
        live_directory: Path,
        source_factory: CameraSourceFactory | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.generation = generation
        self.settings = settings
        self.port = port
        self.recording_enabled = recording_enabled
        self._clock = clock
        self._timezone = timezone
        self._catalog = catalog
        self._volume_store = volume_store
        self._volume = volume
        self._recordings_root = recordings_root
        self._volume_id = volume_id
        self.engine_epoch = engine_epoch
        self.settings_revision = settings_revision
        self._desired_writer_settings = settings
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._capture_ready = threading.Event()
        self._capture_finished = threading.Event()
        self._capture_wake = threading.Event()
        self._capture_condition = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._audio_stop_event: threading.Event | None = None
        self._audio_lifecycle_lock = threading.RLock()
        self._audio_demand_check_mono_ns = -1_000_000_000
        self._writer_factories: set[CataloguedWriterFactory] = set()
        self._source: CameraFrameSource | None = None
        self._source_factory = (
            V4L2CameraSource if source_factory is None else source_factory
        )
        self._audio_source: AlsaCaptureSource | None = None
        self._audio_buffer = AudioBlockBuffer()
        self._audio_error: str | None = None
        self._audio_part_available = False
        self._continuous_session: Any | None = None
        self._last_writer_frame_mono_ns: int | None = None
        # A short-lived continuous frame still passes through the ring before it
        # reaches its writer. Keep the same bounded payload ceiling so a single
        # legitimate large MJPEG frame is not mistaken for a memory overrun.
        ring_bytes = _FRAME_PAYLOAD_BUDGET_BYTES
        self._pipeline = CameraFramePipeline(camera_id, FrameRing(1, ring_bytes), clock)
        self._live = LiveHlsPublisher(
            live_directory,
            generation=generation,
            settings=settings,
            clock=clock,
            recording_active=recording_enabled,
        )
        self._preview = LivePreviewPublisher(
            generation=generation, settings=settings, clock=clock
        )
        self._capture_open = False
        self._recording_error: str | None = None
        self._frame_count = 0
        self._last_frame_utc_ns: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("camera workerは既に起動しています")
        profile = next(
            (
                item
                for item in self.port.profiles
                if item.width == self.settings.width
                and item.height == self.settings.height
                and self.settings.fps in item.fps
            ),
            None,
        )
        if profile is None:
            raise ValueError("保存されたcamera profileは現在のUSB cameraにありません")
        self._source = self._source_factory(
            V4L2CaptureConfig(
                device_path=self.port.device_path,
                camera_id=self.camera_id,
                generation=self.generation,
                input_format=profile.input_format,
                settings=self.settings,
            ),
            self._clock,
            self._timezone,
        )
        self._live.start()
        self._preview.start()
        self._sync_audio_capture()
        if self.recording_enabled:
            session = self._pipeline.start_continuous()
            self._continuous_session = session
            self._writer_thread = threading.Thread(
                target=self._write_continuous,
                args=(session,),
                name=f"recorder-writer-cam{self.camera_id}",
                daemon=True,
            )
            self._writer_thread.start()
        self._thread = threading.Thread(
            target=self._capture,
            name=f"recorder-capture-cam{self.camera_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(
        self, timeout_seconds: float = _CAMERA_WORKER_STOP_TIMEOUT_SECONDS
    ) -> None:
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        self._stop.set()
        capture_wake = getattr(self, "_capture_wake", None)
        if capture_wake is not None:
            capture_wake.set()
        source = getattr(self, "_source", None)
        with self._audio_lifecycle_lock:
            audio_source = getattr(self, "_audio_source", None)
            audio_stop_event = getattr(self, "_audio_stop_event", None)
            if audio_stop_event is not None:
                audio_stop_event.set()
        source_error: Exception | None = None
        for owned_source in (source, audio_source):
            if owned_source is None:
                continue
            request_stop = getattr(owned_source, "request_stop", None)
            if callable(request_stop):
                try:
                    request_stop()
                except Exception as exc:
                    if source_error is None:
                        source_error = exc
        threads = (
            ("capture", self._thread),
            ("audio", self._audio_thread),
        )
        for _name, thread in threads:
            if thread is not None and thread.ident is not None and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
        if self._writer_thread is not None and self._writer_thread.ident is not None:
            self._writer_thread.join(max(0.0, deadline - time.monotonic()))
        for owned_source in (source, audio_source):
            if owned_source is None:
                continue
            close_source = getattr(owned_source, "close", None)
            if callable(close_source):
                try:
                    close_source(max(0.000001, deadline - time.monotonic()))
                except Exception as exc:
                    if source_error is None:
                        source_error = exc
        live_error: Exception | None = None
        try:
            self._live.close(max(0.000001, deadline - time.monotonic()))
        except Exception as exc:
            live_error = exc
        preview = getattr(self, "_preview", None)
        if preview is not None:
            try:
                preview.close(max(0.000001, deadline - time.monotonic()))
            except Exception as exc:
                if live_error is None:
                    live_error = exc
        alive_workers = tuple(
            name
            for name, thread in (
                *threads,
                ("writer", self._writer_thread),
            )
            if thread is not None and thread.is_alive()
        )
        active_processes: list[str] = []
        for name, owned_source in (
            ("capture_process", source),
            ("audio_process", audio_source),
        ):
            if owned_source is None:
                continue
            try:
                process_pid = getattr(owned_source, "active_process_pid", None)
            except Exception as exc:
                if source_error is None:
                    source_error = exc
                continue
            if process_pid is not None:
                active_processes.append(name)
        if alive_workers or active_processes or source_error is not None:
            with self._lock:
                self._recording_error = "worker_stop_timeout"
            stuck = (
                *alive_workers,
                *active_processes,
                *(("source_cleanup",) if source_error else ()),
            )
            raise CameraWorkerStopTimeout(self.camera_id, stuck)
        if live_error is not None:
            with self._lock:
                self._recording_error = type(live_error).__name__
            raise RuntimeError("live_worker_shutdown_failed") from live_error
        self._audio_buffer.clear()
        self._pipeline.clear("worker_stopped")

    def wait_capture_ready(self, timeout_seconds: float = 10.0) -> bool:
        """初回frameまたはworker終了まで待ち、capture再開を確かめる。"""
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._capture_condition:
            while not self._capture_ready.is_set():
                if self._capture_finished.is_set():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._capture_condition.wait(remaining)
            return self._recording_error is None

    def apply_live_settings(self, settings: CameraSettings, revision: int) -> None:
        """liveと次の物理fileへ設定を反映し、進行中fileは変更しない。"""
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        if type(revision) is not int or revision < 0:
            raise ValueError("revisionは0以上の整数が必要です")
        with self._lock:
            self._desired_writer_settings = settings
            self.settings_revision = revision
            factories = tuple(self._writer_factories)
            self._live.update_settings(settings)
            self._preview.update_settings(settings)
            for factory in factories:
                factory.update_settings(settings)
        self._sync_audio_capture()

    def request_live_demand(self) -> bool:
        """認証済みWeb要求を受け、liveと必要な音声取得を起動する。"""
        if not self._live.request_viewer():
            return False
        self._capture_wake.set()
        self._sync_audio_capture()
        return True

    def request_preview_demand(self) -> bool:
        """通常画面の低頻度preview需要をcamera単位で更新する。"""
        if not self._preview.request_viewer():
            return False
        self._capture_wake.set()
        return True

    def release_preview_demand(self) -> None:
        """camera画面を離れたときframeを解放し、次のloopを起こす。"""
        self._preview.release_viewer()
        self._capture_wake.set()

    def release_live_demand(self) -> None:
        """音声付きliveを離れたときHLSと不要な音声取得を止める。"""
        self._live.release_viewer()
        self._capture_wake.set()
        self._sync_audio_capture()

    @property
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            capture = self._source is not None and self._capture_open
            source_stats = None if self._source is None else self._source.stats
            audio_source = self._audio_source
            writer_alive = (
                self._writer_thread is not None and self._writer_thread.is_alive()
            )
            writing = (
                self._pipeline.active_mode == "continuous"
                and self._frame_count > 0
                and capture
                and writer_alive
                and self._recording_error is None
            )
            frame_memory = self._pipeline.memory_snapshot
            recording_delay_ns = None
            if writer_alive and self._last_writer_frame_mono_ns is not None:
                recording_delay_ns = max(
                    0,
                    self._clock.monotonic_ns() - self._last_writer_frame_mono_ns,
                )
            return {
                "capture_active": capture,
                "recording_active": writing,
                "recording_delay_ns": recording_delay_ns,
                "live": self._live.status,
                "preview": self._preview.status,
                "frames": self._frame_count,
                "dropped_frames": (
                    0 if source_stats is None else source_stats.dropped_frames
                ),
                "audio": {
                    "enabled": self.settings.audio_enabled,
                    "available": audio_source is not None and audio_source.ready,
                    "interface_key": self.settings.audio_interface_key,
                    "error": self._audio_error
                    or (None if audio_source is None else audio_source.last_error),
                    "blocks": 0 if audio_source is None else audio_source.stats.blocks,
                    "dropped_blocks": self._audio_buffer.dropped_blocks,
                    "buffer_bytes": self._audio_buffer.size_bytes,
                },
                "frame_memory": frame_memory,
                "last_frame_utc_ns": self._last_frame_utc_ns,
                "last_error": self._recording_error
                or (None if source_stats is None else source_stats.last_error),
                "fatal_error": self._recording_error is not None,
            }

    def _capture(self) -> None:
        source = self._source
        assert source is not None
        try:
            while not self._stop.is_set():
                if not self._capture_demand_active():
                    with self._lock:
                        self._capture_open = False
                        self._capture_condition.notify_all()
                    self._capture_wake.wait(0.5)
                    self._capture_wake.clear()
                    continue
                frames = source.frames(self._stop)
                try:
                    for frame in frames:
                        if self._stop.is_set():
                            break
                        if not self._capture_demand_active():
                            break
                        audio_blocks = self._audio_buffer.take_before(
                            frame.capture_mono_ns
                        )
                        audio_source = self._audio_source
                        if audio_source is not None:
                            if not audio_source.ready:
                                self._audio_part_available = False
                            elif audio_blocks:
                                self._audio_part_available = True
                            frame = replace(
                                frame,
                                audio_blocks=audio_blocks,
                                audio_available=self._audio_part_available,
                            )
                        with self._lock:
                            self._capture_open = True
                            self._frame_count += 1
                            self._last_frame_utc_ns = frame.capture_utc_ns
                            self._capture_ready.set()
                            self._capture_condition.notify_all()
                        self._preview.submit(frame)
                        self._live.submit(
                            frame,
                            recording_delay_ns=self._recording_delay_for_live(),
                        )
                        if self.recording_enabled:
                            self._pipeline.push(frame)
                        # Re-sync audio on the demand-check cadence in every
                        # mode, so an audio capture thread that died during
                        # recording is restarted instead of leaving the rest
                        # of the recording silently without audio.
                        now_mono_ns = self._clock.monotonic_ns()
                        if (
                            now_mono_ns - self._audio_demand_check_mono_ns
                            >= 1_000_000_000
                        ):
                            self._audio_demand_check_mono_ns = now_mono_ns
                            self._sync_audio_capture()
                    else:
                        if not self._stop.is_set() and self._capture_demand_active():
                            raise EOFError("camera_input_ended")
                finally:
                    close_frames = getattr(frames, "close", None)
                    if callable(close_frames):
                        close_frames()
                with self._lock:
                    self._capture_open = False
                    self._capture_condition.notify_all()
        except Exception as exc:
            with self._lock:
                self._recording_error = type(exc).__name__
            self._stop.set()
        finally:
            with self._lock:
                self._capture_open = False
                if self._stop.is_set() or self._recording_error is not None:
                    self._capture_finished.set()
                self._capture_condition.notify_all()
            if self.recording_enabled:
                session = self._continuous_session
                if session is not None:
                    try:
                        self._pipeline.finish_continuous(session)
                    except ValueError:
                        session.finish()

    def _capture_demand_active(self) -> bool:
        """録画または現在のブラウザ需要がある間だけV4L2を開く。"""
        return (
            self.recording_enabled
            or self._preview.viewer_demand_active
            or self._live.viewer_demand_active
        )

    def _capture_audio(
        self, source: AlsaCaptureSource, stop_event: threading.Event
    ) -> None:
        try:
            for block in source.frames(stop_event):
                if block.generation == self.generation:
                    self._audio_buffer.append(block)
        except Exception as exc:
            with self._lock:
                self._audio_error = type(exc).__name__
        finally:
            source.ready = False
            if source.last_error is not None:
                with self._lock:
                    self._audio_error = source.last_error

    def _sync_audio_capture(self) -> None:
        """録画に不要なALSA取得は視聴需要の猶予終了後に停止する。"""
        with self._lock:
            settings = self._desired_writer_settings
        needed = settings.audio_enabled and (
            self.recording_enabled or self._live.viewer_demand_active
        )
        with self._audio_lifecycle_lock:
            if self._stop.is_set():
                needed = False
            source = self._audio_source
            thread = self._audio_thread
            stop_event = self._audio_stop_event
            if needed:
                if thread is not None and thread.is_alive():
                    return
                if source is not None:
                    try:
                        source.close(1.0)
                    except Exception as exc:
                        with self._lock:
                            self._audio_error = type(exc).__name__
                        return
                    self._audio_source = None
                    self._audio_thread = None
                    self._audio_stop_event = None
                audio_key = settings.audio_interface_key
                if audio_key is None:
                    self._audio_error = "audio_interface_unconfigured"
                    return
                if audio_key.split("/", 1)[0] != self.port.port_key:
                    self._audio_error = "audio_interface_not_on_camera_port"
                    return
                source = AlsaCaptureSource(
                    audio_key,
                    self.port.port_key,
                    self.generation,
                    AlsaAudioDiscovery(),
                    self._clock,
                    self._timezone,
                )
                stop_event = threading.Event()
                thread = threading.Thread(
                    target=self._capture_audio,
                    args=(source, stop_event),
                    name=f"recorder-audio-cam{self.camera_id}",
                    daemon=True,
                )
                self._audio_source = source
                self._audio_stop_event = stop_event
                self._audio_thread = thread
                thread.start()
                return

            if thread is not None and thread.is_alive():
                if stop_event is not None:
                    stop_event.set()
                if source is not None:
                    source.request_stop()
                self._audio_buffer.clear()
                return
            if source is not None:
                try:
                    source.close(1.0)
                except Exception as exc:
                    with self._lock:
                        self._audio_error = type(exc).__name__
                    return
            self._audio_source = None
            self._audio_thread = None
            self._audio_stop_event = None
            self._audio_buffer.clear()

    def _recording_delay_for_live(self) -> int | None:
        with self._lock:
            writer_active = (
                self._writer_thread is not None and self._writer_thread.is_alive()
            )
            last_written = self._last_writer_frame_mono_ns
        if not writer_active or last_written is None:
            return None
        return max(0, self._clock.monotonic_ns() - last_written)

    def _recording_frame_written(self, frame: FrameEnvelope) -> None:
        with self._lock:
            self._last_writer_frame_mono_ns = frame.capture_mono_ns

    def _register_writer_factory(self, writer_factory: CataloguedWriterFactory) -> None:
        with self._lock:
            writer_factory.update_settings(self._desired_writer_settings)
            self._writer_factories.add(writer_factory)

    def _write_continuous(self, session: Any) -> None:
        if (
            self._volume_store is None
            or self._volume is None
            or self._recordings_root is None
            or self._volume_id is None
        ):
            return
        writer_factory = CataloguedWriterFactory(
            index=self._catalog,
            volume_store=self._volume_store,
            volume=self._volume,
            recordings_root=self._recordings_root,
            volume_id=self._volume_id,
            settings=self.settings,
            camera_id=self.camera_id,
            mode=RecordingMode.CONTINUOUS,
            boot_id=self._clock.boot_id(),
            writer_epoch=self.engine_epoch,
        )
        with self._lock:
            self._last_writer_frame_mono_ns = None
        self._register_writer_factory(writer_factory)
        try:
            record_continuous_session(
                session,
                self._stop,
                writer_factory,
                frame_written_callback=self._recording_frame_written,
            )
        except Exception as exc:
            with self._lock:
                self._recording_error = type(exc).__name__
        finally:
            with self._lock:
                self._writer_factories.discard(writer_factory)


class RecorderRuntime:
    """systemd engine。設定・媒体・camera状態の唯一の調整者。"""

    def __init__(
        self,
        data_directory: str | Path,
        *,
        socket_path: str | Path = "/run/raspi-recorder/engine.sock",
        clock: Clock | None = None,
        clock_setter: ClockSetter | None = None,
        discovery: V4L2CameraDiscovery | None = None,
        volume_scanner: LinuxVolumeScanner | None = None,
        volume_store: ManagedVolumeStore | None = None,
        update_helper_client: UpdateHelperClient | None = None,
        camera_source_factory: CameraSourceFactory | None = None,
        live_root: str | Path | None = None,
    ) -> None:
        self.data_directory = Path(data_directory)
        self.socket_path = Path(socket_path)
        self.clock = SystemClock() if clock is None else clock
        self.clock_setter = (
            SystemClockSetter() if clock_setter is None else clock_setter
        )
        self.timezone = SystemTimezone()
        self.settings_service = SettingsService(
            SettingsRepository(self.data_directory / "settings.sqlite3"), self.clock
        )
        self._update_configuration_error: str | None = None
        try:
            update_source = release_source_from_environment()
        except ValueError:
            update_source = None
            self._update_configuration_error = "update_source_configuration_invalid"
        self.update_service = UpdateService(
            current_version=__version__,
            source=update_source,
            data_directory=self.data_directory,
            install_directory=Path("/opt/raspi-recorder"),
            privileged_state_directory=Path("/var/lib/raspi-recorder-update"),
            bundle_validator=TarZstdUpdateBundleValidator(),
        )
        self.update_helper_client = (
            UpdateHelperClient()
            if update_helper_client is None
            else update_helper_client
        )
        self.catalog = RecordingCatalog(self.data_directory / "recordings.sqlite3")
        self.discovery = V4L2CameraDiscovery() if discovery is None else discovery
        self._camera_source_factory = camera_source_factory
        self.live_root = Path(_LIVE_ROOT if live_root is None else live_root)
        self.volume_scanner = (
            LinuxVolumeScanner() if volume_scanner is None else volume_scanner
        )
        self.volume_store = (
            ManagedVolumeStore() if volume_store is None else volume_store
        )
        self.recording_access = RecordingAccessService(
            self.catalog,
            _RuntimeVolumeLocator(self),
            self.volume_store,
            self.clock,
        )
        self.export_jobs = ExportJobService(
            self.recording_access,
            self.volume_scanner,
            self.volume_store,
            self.clock,
            lambda: self._selected,
            self.settings,
        )
        self._low_space_blocked = False
        self._capacity_error: str | None = None
        self._capacity_warning: str | None = None
        self._capacity_manager = CapacityManager(
            self.catalog,
            self.recording_access,
            _RuntimeCapacityReader(self),
            _RuntimeLowSpaceBlocker(self),
        )
        self.epoch = uuid.uuid4().hex
        self._cameras = {1: CameraRuntime(1), 2: CameraRuntime(2)}
        self._lock = threading.RLock()
        self._configuration_lock = threading.Condition(threading.RLock())
        self._configuration_transition_token: object | None = None
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._capacity_supervisor: threading.Thread | None = None
        self._socket_server: EngineSocketServer | None = None
        self._ports: tuple[CameraPort, ...] = ()
        self._volumes: tuple[MountedVolume, ...] = ()
        self._selected: tuple[MountedVolume, Path, str] | None = None
        self._prepared_volume_key: tuple[str, int, str] | None = None
        self._volume_error: str | None = None
        self._discovery_error: str | None = None
        self._uncertain_update_operations: dict[str, int] = {}
        self._started_utc_ns: int | None = None
        self._runtime_state_error: str | None = None
        self._engine_lock_fd: int | None = None
        self._clock_sync_lock = threading.Lock()
        self._clock_sync_status_cache: ClockSyncStatus | None = None
        self._clock_sync_checked_mono_ns: int | None = None

    def start(self) -> None:
        """DBを復旧し、control socketと独立supervisorを開始する。"""
        if self._supervisor is not None or self._engine_lock_fd is not None:
            raise RuntimeError("engineは既に起動しています")
        self.data_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._acquire_engine_lock()
        try:
            self._start_locked()
        except BaseException:
            self.close()
            raise

    def _acquire_engine_lock(self) -> None:
        """Reserve the state directory before any recovery can mutate its DBs."""
        descriptor = os.open(
            self.data_directory / "engine.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("engine lockはregular fileが必要です")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "別のengineが同じdata directoryを使用中です"
                ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._engine_lock_fd = descriptor

    def _start_locked(self) -> None:
        self.catalog.initialize()
        self.settings_service.initialize()
        self._fail_interrupted_operations()
        self._load_desired_state()
        self.catalog.mark_unfinished_from_previous_epoch_abnormal(self.epoch)
        self._started_utc_ns = self.clock.utc_ns()
        handler = EngineControlHandler(self)
        server = EngineSocketServer(self.socket_path, handler)
        server.start()
        self._socket_server = server
        self._stop.clear()
        self._supervisor = threading.Thread(
            target=self._supervise,
            name="raspi-recorder-supervisor",
            daemon=True,
        )
        self._supervisor.start()
        self._capacity_supervisor = threading.Thread(
            target=self._supervise_capacity,
            name="raspi-recorder-capacity-supervisor",
            daemon=True,
        )
        self._capacity_supervisor.start()

    def close(self, timeout_seconds: float = 50.0) -> None:
        """workerを確定停止し、engine control socketを閉じる。"""
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        self._stop.set()
        supervisor = self._supervisor
        if supervisor is not None:
            supervisor.join(max(0.0, deadline - time.monotonic()))
            if supervisor.is_alive():
                raise RecorderRuntimeStopTimeout("supervisor")
        capacity_supervisor = self._capacity_supervisor
        if capacity_supervisor is not None:
            capacity_supervisor.join(max(0.0, deadline - time.monotonic()))
            if capacity_supervisor.is_alive():
                raise RecorderRuntimeStopTimeout("capacity_supervisor")
        if self._socket_server is not None:
            self._socket_server.close(max(0.000001, deadline - time.monotonic()))
            self._socket_server = None
        stop_error: CameraWorkerStopTimeout | None = None
        try:
            with self._configuration_transition(
                operation="shutdown",
                wait_seconds=max(0.0, deadline - time.monotonic()),
            ):
                with self._lock:
                    camera_items = tuple(self._cameras.items())
                workers = []
                for camera_id, camera in camera_items:
                    with camera.state_lock:
                        workers.append((camera_id, camera.worker))
                for camera_id, worker in workers:
                    if worker is not None:
                        try:
                            worker.stop(max(0.000001, deadline - time.monotonic()))
                        except CameraWorkerStopTimeout as exc:
                            # One stuck worker must not abort the remaining
                            # cleanup; surface the failure after it completes.
                            if stop_error is None:
                                stop_error = exc
                            continue
                        camera = self._cameras[camera_id]
                        with camera.state_lock:
                            if camera.worker is worker:
                                camera.worker = None
        except RuntimeTransitionBusy as exc:
            raise RecorderRuntimeStopTimeout("configuration_transition") from exc
        self.export_jobs.close(max(0.000001, deadline - time.monotonic()))
        self._supervisor = None
        self._capacity_supervisor = None
        if stop_error is not None:
            # A stuck worker may still own its device or writer. Retain the
            # state-directory lock until a later close() finishes cleanup.
            raise stop_error
        if self._engine_lock_fd is not None:
            os.close(self._engine_lock_fd)
            self._engine_lock_fd = None

    def settings(self) -> ApplicationSettings:
        return self.settings_service.current()

    @contextmanager
    def _configuration_transition(
        self, *, operation: str, wait_seconds: float
    ) -> Iterator[object]:
        """Reserve a transition briefly under lock, then do I/O without it."""
        if not isinstance(operation, str) or not operation:
            raise ValueError("operationは空でない文字列で指定してください")
        if type(wait_seconds) not in (int, float) or wait_seconds < 0:
            raise ValueError("wait_secondsは0以上の数値で指定してください")
        deadline = time.monotonic() + float(wait_seconds)
        token = object()
        with self._configuration_lock:
            while self._configuration_transition_token is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeTransitionBusy(
                        f"configuration transition is busy: {operation}"
                    )
                self._configuration_lock.wait(remaining)
            self._configuration_transition_token = token
        try:
            yield token
        finally:
            with self._configuration_lock:
                if self._configuration_transition_token is token:
                    self._configuration_transition_token = None
                    self._configuration_lock.notify_all()

    def _fail_interrupted_operations(self) -> None:
        """再起動前に終端しなかった設定切替を不明のまま残さない。"""
        revision = self.settings().revision
        for operation in self.settings_service.load_pending_operations():
            if operation.kind == "update":
                if operation.stage is OperationStage.QUEUED:
                    # The privileged helper is only handed an update after
                    # the VALIDATING transition, so a queued operation was
                    # never handed off; fail it like any other interrupted
                    # operation instead of blocking later operations forever.
                    self.settings_service.transition_operation(
                        operation.operation_id,
                        operation.stage,
                        OperationStage.FAILED,
                        error={"code": "engine_restarted_during_update_operation"},
                        recovery={
                            "state": "update_handoff_not_completed",
                            "current_version": __version__,
                        },
                    )
                elif operation.stage is OperationStage.VALIDATING:
                    # The handoff may or may not have reached the helper.
                    # Bound the uncertainty with supervisor reconciliation
                    # instead of failing it while an apply may be starting.
                    self._track_uncertain_update_operation(operation.operation_id)
                # FINALIZING/APPLYING/STARTING are owned by the privileged
                # helper and resolved by its journal recovery; leave them.
                continue
            self.settings_service.transition_operation(
                operation.operation_id,
                operation.stage,
                OperationStage.FAILED,
                error={"code": "engine_restarted_during_settings_operation"},
                recovery={
                    "state": "current_settings_loaded_and_workers_will_reconcile",
                    "current_revision": revision,
                    "requested_base_revision": operation.target.get("base_revision"),
                },
            )

    def _track_uncertain_update_operation(self, operation_id: str) -> None:
        """引き継ぎが不確かなupdate operationへ有界の猶予を与える。"""
        deadline = self.clock.monotonic_ns() + int(
            _UNCERTAIN_UPDATE_OPERATION_TIMEOUT_SECONDS * 1_000_000_000
        )
        self._uncertain_update_operations.setdefault(operation_id, deadline)

    def _reconcile_uncertain_update_operations(self) -> None:
        if not self._uncertain_update_operations:
            return
        now = self.clock.monotonic_ns()
        for operation_id, deadline in tuple(self._uncertain_update_operations.items()):
            operation = self.settings_service.load_operation(operation_id)
            if operation is None or operation.stage not in {
                OperationStage.QUEUED,
                OperationStage.VALIDATING,
            }:
                # Terminal, or advanced by the helper: no longer uncertain.
                self._uncertain_update_operations.pop(operation_id, None)
                continue
            if now < deadline:
                continue
            try:
                self.settings_service.transition_operation(
                    operation_id,
                    operation.stage,
                    OperationStage.FAILED,
                    error={"code": "update_handoff_unresolved"},
                    recovery={
                        "state": "update_not_applied",
                        "current_version": __version__,
                    },
                )
            except Exception:
                continue
            self._uncertain_update_operations.pop(operation_id, None)
            try:
                self.update_service.cleanup(operation_id)
            except (OSError, RuntimeError, ValueError):
                pass

    def status(self) -> dict[str, Any]:
        sync_status = self._clock_sync_status()
        with self._lock:
            settings = self.settings()
            cameras: list[dict[str, Any]] = []
            for camera_id, runtime in self._cameras.items():
                requested_camera_settings = settings.camera(camera_id)
                camera_port = next(
                    (
                        item
                        for item in self._ports
                        if requested_camera_settings.usb_port_key is not None
                        and item.port_key == requested_camera_settings.usb_port_key
                    ),
                    None,
                )
                effective_camera_settings, profile_reduced = _effective_camera_settings(
                    requested_camera_settings,
                    settings.power_saving_enabled,
                    camera_port,
                )
                with runtime.state_lock:
                    desired_running = runtime.desired_running
                    state = runtime.state
                    blockers = sorted(runtime.blockers)
                    generation = runtime.generation
                    last_error = runtime.last_error
                    worker = runtime.worker
                worker_status = {} if worker is None else worker.snapshot
                if state != "finalizing" and (
                    state == "error" or worker_status.get("fatal_error")
                ):
                    state = "error"
                elif state != "finalizing" and not desired_running:
                    state = "stopped"
                elif state != "finalizing" and blockers:
                    state = "blocked"
                elif state != "finalizing" and worker_status.get("fatal_error"):
                    state = "error"
                elif state != "finalizing" and worker_status.get("recording_active"):
                    state = "continuous"
                elif state != "finalizing" and worker_status.get("capture_active"):
                    state = "starting"
                cameras.append(
                    {
                        "camera_id": camera_id,
                        "desired_running": desired_running,
                        "state": state,
                        "blockers": blockers,
                        "generation": generation,
                        "recording_active": bool(
                            worker_status.get("recording_active", False)
                        ),
                        "capture_active": bool(
                            worker_status.get("capture_active", False)
                        ),
                        "recording_delay_ns": worker_status.get("recording_delay_ns"),
                        "live": worker_status.get("live", {"available": False}),
                        "preview": worker_status.get(
                            "preview", {"available": False, "fresh": False}
                        ),
                        "audio": worker_status.get(
                            "audio",
                            {
                                "enabled": effective_camera_settings.audio_enabled,
                                "available": False,
                                "interface_key": (
                                    effective_camera_settings.audio_interface_key
                                ),
                                "error": "camera_worker_stopped",
                            },
                        ),
                        "effective_settings": {
                            "width": effective_camera_settings.width,
                            "height": effective_camera_settings.height,
                            "fps": effective_camera_settings.fps,
                            "audio_enabled": effective_camera_settings.audio_enabled,
                            "profile_reduced": profile_reduced,
                        },
                        "frames": worker_status.get("frames", 0),
                        "dropped_frames": worker_status.get("dropped_frames", 0),
                        "last_frame_utc_ns": worker_status.get("last_frame_utc_ns"),
                        "last_error": last_error or worker_status.get("last_error"),
                    }
                )
            volume_payload: dict[str, Any] | None = None
            if self._selected is not None:
                volume, _, _ = self._selected
                volume_payload = {
                    "persistent_id": volume.persistent_id,
                    "kind": volume.kind.value,
                    "filesystem": volume.filesystem,
                    "total_bytes": volume.filesystem_bytes,
                    "available_bytes": volume.available_bytes,
                    "read_only": volume.read_only,
                }
            warnings = self._runtime_warnings(settings, sync_status)
            for camera_id, camera_settings in enumerate(settings.cameras, start=1):
                effective_camera_settings, _profile_reduced = (
                    _effective_camera_settings(
                        camera_settings,
                        settings.power_saving_enabled,
                        next(
                            (
                                item
                                for item in self._ports
                                if camera_settings.usb_port_key is not None
                                and item.port_key == camera_settings.usb_port_key
                            ),
                            None,
                        ),
                    )
                )
                if effective_camera_settings.audio_enabled:
                    runtime = self._cameras[camera_id]
                    with runtime.state_lock:
                        worker = runtime.worker
                    worker_status = {} if worker is None else worker.snapshot
                    audio = worker_status.get("audio", {})
                    if not audio.get("available"):
                        warnings.append(f"camera_{camera_id}_audio_unavailable")
            return {
                "service": "running",
                "current_version": __version__,
                "started_utc_ns": self._started_utc_ns,
                "revision": settings.revision,
                "power_saving_enabled": settings.power_saving_enabled,
                "cameras": cameras,
                "volume": volume_payload,
                "volume_error": self._volume_error,
                "capacity_error": self._capacity_error,
                "capacity_warning": self._capacity_warning,
                "runtime_state_error": self._runtime_state_error,
                "time": {
                    "utc_ns": self.clock.utc_ns(),
                    "quality": (
                        "synchronized"
                        if sync_status is not None and sync_status.synchronized is True
                        else "unverified"
                    ),
                    "timezone": self._timezone_name(),
                    "ntp_enabled": (
                        None if sync_status is None else sync_status.ntp_enabled
                    ),
                    "synchronized": (
                        None if sync_status is None else sync_status.synchronized
                    ),
                },
                "warnings": warnings,
            }

    def _timezone_name(self) -> str | None:
        try:
            name = self.timezone.name()
            ZoneInfo(name)
            return name
        except (RuntimeError, ZoneInfoNotFoundError, ValueError):
            return None

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            settings = self.settings()
            power_saving_cameras: list[dict[str, Any]] = []
            for camera_id in (1, 2):
                requested = settings.camera(camera_id)
                port = next(
                    (
                        item
                        for item in self._ports
                        if requested.usb_port_key is not None
                        and item.port_key == requested.usb_port_key
                    ),
                    None,
                )
                effective, profile_reduced = _effective_camera_settings(
                    requested, settings.power_saving_enabled, port
                )
                power_saving_cameras.append(
                    {
                        "camera_id": camera_id,
                        "width": effective.width,
                        "height": effective.height,
                        "fps": effective.fps,
                        "audio_enabled": effective.audio_enabled,
                        "profile_reduced": profile_reduced,
                        "profile_available": port is not None,
                        "reason": (
                            "no_lower_supported_profile"
                            if settings.power_saving_enabled
                            and port is not None
                            and not profile_reduced
                            else "camera_profile_unavailable"
                            if settings.power_saving_enabled and port is None
                            else None
                        ),
                    }
                )
            return {
                "cameras": [
                    {
                        "port_key": port.port_key,
                        "video_interface_key": port.video_interface_key,
                        "device_path": port.device_path,
                        "product": port.product,
                        "profiles": [
                            {
                                "input_format": profile.input_format,
                                "width": profile.width,
                                "height": profile.height,
                                "fps": list(profile.fps),
                            }
                            for profile in port.profiles
                        ],
                        "audio_interfaces": [
                            {
                                "interface_key": audio.interface_key,
                                "label": audio.label,
                            }
                            for audio in port.audio_interfaces
                        ],
                    }
                    for port in self._ports
                ],
                "power_saving": {
                    "enabled": settings.power_saving_enabled,
                    "cameras": power_saving_cameras,
                },
                "volumes": [
                    {
                        "persistent_id": volume.persistent_id,
                        "kind": volume.kind.value,
                        "filesystem": volume.filesystem,
                        "total_bytes": volume.filesystem_bytes,
                        "available_bytes": volume.available_bytes,
                        "read_only": volume.read_only,
                        "selected": (
                            self._selected is not None
                            and self._selected[0].persistent_id == volume.persistent_id
                        ),
                    }
                    for volume in self._volumes
                ],
                "audio": [
                    {
                        "interface_key": audio.interface_key,
                        "usb_port_key": audio.usb_port_key,
                        "label": audio.label,
                    }
                    for port in self._ports
                    for audio in port.audio_interfaces
                ],
                "engine_features": {
                    "continuous_recording": True,
                    "audio_recording": True,
                    "live_hls": True,
                },
            }

    def settings_payload(self) -> dict[str, Any]:
        return self.settings().to_payload()

    def time_status(self) -> dict[str, Any]:
        """OS wall clock・timezone・systemdの時刻同期状態を報告する。"""
        timezone = self._timezone_name()
        helper_status = self._clock_sync_status(refresh=True)
        return {
            "utc_ns": self.clock.utc_ns(),
            "timezone": timezone,
            "helper_timezone": (
                None if helper_status is None else helper_status.timezone
            ),
            "ntp_enabled": (
                None if helper_status is None else helper_status.ntp_enabled
            ),
            "synchronized": (
                None if helper_status is None else helper_status.synchronized
            ),
            "quality": (
                "synchronized"
                if helper_status is not None and helper_status.synchronized is True
                else "unverified"
            ),
            "helper_available": helper_status is not None,
        }

    def _clock_sync_status(self, *, refresh: bool = False) -> ClockSyncStatus | None:
        """systemd状態を最大60秒cacheし、同期確認が遅れて反映されるようにする。"""
        with self._clock_sync_lock:
            now_mono_ns = self.clock.monotonic_ns()
            checked = self._clock_sync_checked_mono_ns
            if refresh or checked is None or now_mono_ns - checked >= 60_000_000_000:
                try:
                    self._clock_sync_status_cache = self.clock_setter.status()
                except Exception:
                    self._clock_sync_status_cache = None
                self._clock_sync_checked_mono_ns = now_mono_ns
            return self._clock_sync_status_cache

    def apply_system_time(
        self,
        utc_ns: int,
        timezone_name: str,
        expected_revision: int,
        ntp_policy: str,
        *,
        on_stage: Callable[[OperationStage], None] | None = None,
    ) -> None:
        """現行workerの物理MP4を確定後、helperで時刻を変えて再開する。"""
        if type(utc_ns) is not int or utc_ns < 0:
            raise ValueError("utc_nsは0以上の整数で指定してください")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ValueError("timezoneが不正です")
        if ntp_policy not in {"keep", "pause_and_resume"}:
            raise ValueError("ntp_policyが不正です")
        with self._configuration_transition(operation="system_time", wait_seconds=30.0):
            current = self.settings()
            if current.revision != expected_revision:
                from raspi_recorder.domain.errors import RevisionConflict

                raise RevisionConflict(expected_revision, current.revision)
            if on_stage is not None:
                on_stage(OperationStage.VALIDATING)
            if timezone_name != self._timezone_name():
                raise ValueError("system_timezone_changed")
            try:
                sync_status = self.clock_setter.status()
            except Exception as exc:
                raise ClockOperationError(
                    "clock_helper_unavailable",
                    {"state": "recording_unchanged", "clock_changed": False},
                ) from exc
            if sync_status.timezone != timezone_name:
                raise ClockOperationError(
                    "system_timezone_changed",
                    {"state": "recording_unchanged", "clock_changed": False},
                )
            if sync_status.ntp_enabled is None:
                raise ClockOperationError(
                    "ntp_state_unknown",
                    {
                        "state": "recording_unchanged",
                        "clock_changed": False,
                        "ntp_state_uncertain": True,
                    },
                )
            if ntp_policy == "pause_and_resume" and sync_status.ntp_enabled is not True:
                raise ValueError("ntp_not_enabled_for_pause_resume")
            if ntp_policy == "keep" and sync_status.ntp_enabled is True:
                # systemd timedated rejects SetTime while an NTP service is
                # active. Fail before finalizing any current MP4; the UI can
                # ask the user to retry with the explicit pause-and-resume mode.
                raise ClockOperationError(
                    "ntp_active_time_change_rejected",
                    {"state": "recording_unchanged", "clock_changed": False},
                )

            previous_utc_ns = self.clock.utc_ns()
            previous_mono_ns = self.clock.monotonic_ns()
            previous_workers = {
                camera_id: self._worker_snapshot(runtime)
                for camera_id, runtime in self._cameras.items()
            }
            desired_running = {
                camera_id: self._desired_running_snapshot(runtime)
                for camera_id, runtime in self._cameras.items()
            }
            if on_stage is not None:
                on_stage(OperationStage.FINALIZING)
            stop_failures: list[int] = []
            for camera_id, worker in previous_workers.items():
                if worker is None:
                    continue
                runtime = self._cameras[camera_id]
                with runtime.state_lock:
                    runtime.state = "finalizing"
                try:
                    worker.stop()
                    with runtime.state_lock:
                        if runtime.worker is worker:
                            runtime.worker = None
                except Exception:
                    stop_failures.append(camera_id)
            if stop_failures:
                resume_failures = self._resume_previous_workers(
                    previous_workers, desired_running
                )
                raise ClockOperationError(
                    "time_change_finalize_failed",
                    {
                        "state": "clock_unchanged",
                        "clock_changed": False,
                        "failed_camera_ids": sorted(stop_failures),
                        "resume_failed_camera_ids": resume_failures,
                    },
                )

            if on_stage is not None:
                on_stage(OperationStage.APPLYING)
            try:
                self.clock_setter.set_utc_ns(utc_ns, ntp_policy)
            except Exception as exc:
                clock_changed = not (isinstance(exc, ClockSetError) and not exc.changed)
                rollback_error: str | None = None
                if clock_changed:
                    rollback_utc_ns = previous_utc_ns + max(
                        0, self.clock.monotonic_ns() - previous_mono_ns
                    )
                    rollback_ntp_policy = "keep"
                    if ntp_policy == "pause_and_resume":
                        try:
                            rollback_status = self.clock_setter.status()
                        except Exception:
                            rollback_status = None
                        if (
                            rollback_status is not None
                            and rollback_status.ntp_enabled is True
                        ):
                            rollback_ntp_policy = "pause_and_resume"
                    try:
                        self.clock_setter.set_utc_ns(
                            rollback_utc_ns, rollback_ntp_policy
                        )
                    except Exception as rollback_exc:
                        rollback_error = getattr(
                            rollback_exc, "code", type(rollback_exc).__name__
                        )
                resume_failures = self._resume_previous_workers(
                    previous_workers, desired_running
                )
                raise ClockOperationError(
                    getattr(exc, "code", "clock_set_failed"),
                    {
                        "state": (
                            "clock_restored"
                            if clock_changed and rollback_error is None
                            else "clock_state_uncertain"
                            if clock_changed
                            else "clock_unchanged"
                        ),
                        "clock_changed": clock_changed and rollback_error is not None,
                        "ntp_state_uncertain": bool(
                            getattr(exc, "ntp_state_uncertain", False)
                        ),
                        "clock_rollback_error": rollback_error,
                        "resume_failed_camera_ids": resume_failures,
                    },
                ) from exc

            if on_stage is not None:
                on_stage(OperationStage.STARTING)
            resume_failures = self._resume_previous_workers(
                previous_workers, desired_running
            )
            if resume_failures:
                raise ClockOperationError(
                    "time_set_camera_resume_failed",
                    {
                        "state": "clock_set_camera_restart_failed",
                        "clock_changed": True,
                        "failed_camera_ids": resume_failures,
                    },
                )

    def _resume_previous_workers(
        self,
        previous_workers: dict[int, CameraCaptureWorker | None],
        desired_running: dict[int, bool],
    ) -> list[int]:
        """希望状態を保ったまま、変更前にworkerがあったcameraを再照合する。"""
        try:
            self._refresh_once()
            for camera_id in (1, 2):
                runtime = self._cameras[camera_id]
                with runtime.state_lock:
                    runtime.desired_running = desired_running[camera_id]
                    runtime.retry_after_mono_ns = 0
                self._reconcile_camera(camera_id)
        except Exception:
            return sorted(
                camera_id
                for camera_id, worker in previous_workers.items()
                if worker is not None
            )
        failed: list[int] = []
        for camera_id, previous_worker in previous_workers.items():
            worker = self._worker_snapshot(self._cameras[camera_id])
            if previous_worker is None:
                continue
            if worker is None:
                failed.append(camera_id)
                continue
            if not getattr(worker, "recording_enabled", True):
                request_preview = getattr(worker, "request_preview_demand", None)
                if callable(request_preview):
                    request_preview()
            wait_ready = getattr(worker, "wait_capture_ready", None)
            if callable(wait_ready) and not wait_ready(10.0):
                failed.append(camera_id)
        return sorted(failed)

    @staticmethod
    def _worker_snapshot(runtime: CameraRuntime) -> CameraCaptureWorker | None:
        with runtime.state_lock:
            return runtime.worker

    @staticmethod
    def _desired_running_snapshot(runtime: CameraRuntime) -> bool:
        with runtime.state_lock:
            return runtime.desired_running

    def set_camera_running(self, camera_id: int, running: bool) -> None:
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        if type(running) is not bool:
            raise ValueError("runningはboolで指定してください")
        with self._configuration_transition(
            operation="camera_state", wait_seconds=12.0
        ):
            runtime = self._cameras[camera_id]
            with runtime.state_lock:
                runtime.desired_running = running
                runtime.last_error = None
            self._save_desired_state()
            self._refresh_once()
            self._reconcile_camera(camera_id, raise_on_stop_failure=True)

    def set_low_space_blocked(self, blocked: bool, reason: str | None = None) -> None:
        if type(blocked) is not bool:
            raise ValueError("blockedはboolで指定してください")
        with self._lock:
            self._low_space_blocked = blocked
            if blocked:
                self._capacity_warning = reason or "low_space"
            elif self._capacity_warning == "low_space_stop_threshold":
                self._capacity_warning = None

    def apply_settings(
        self,
        candidate: ApplicationSettings,
        expected_revision: int,
        *,
        on_stage: Callable[[OperationStage], None] | None = None,
    ) -> ApplicationSettings:
        """候補を検証後に録画を確定し、CAS保存して対象を再開する。"""
        if not isinstance(candidate, ApplicationSettings):
            raise ValueError("candidate settingsが不正です")
        with self._configuration_transition(operation="settings", wait_seconds=30.0):
            current = self.settings()
            if current.revision != expected_revision:
                from raspi_recorder.domain.errors import RevisionConflict

                raise RevisionConflict(expected_revision, current.revision)
            if on_stage is not None:
                on_stage(OperationStage.VALIDATING)
            self._refresh_once()
            self._validate_candidate(candidate)
            restart_all = (
                candidate.power_saving_enabled != current.power_saving_enabled
                or candidate.volume_kind != current.volume_kind
                or candidate.volume_persistent_id != current.volume_persistent_id
            )
            restart_cameras = {
                camera_id
                for camera_id, (before, after) in enumerate(
                    zip(current.cameras, candidate.cameras, strict=True), start=1
                )
                if _capture_settings_changed(before, after)
            }
            if restart_all:
                restart_cameras.update((1, 2))
            if on_stage is not None:
                on_stage(
                    OperationStage.FINALIZING
                    if restart_cameras
                    else OperationStage.APPLYING
                )
            old_workers = {
                camera_id: self._worker_snapshot(self._cameras[camera_id])
                for camera_id in restart_cameras
            }
            for camera_id in restart_cameras:
                runtime = self._cameras[camera_id]
                with runtime.state_lock:
                    runtime.state = "finalizing"
            for camera_id, worker in old_workers.items():
                if worker is not None:
                    worker.stop()
                    runtime = self._cameras[camera_id]
                    with runtime.state_lock:
                        if runtime.worker is worker:
                            runtime.worker = None
            if restart_cameras and on_stage is not None:
                on_stage(OperationStage.APPLYING)
            try:
                saved = self.settings_service.compare_and_swap(
                    candidate, expected_revision
                )
            except Exception:
                self._refresh_once()
                for camera_id in (1, 2):
                    self._reconcile_camera(camera_id)
                raise
            if on_stage is not None:
                on_stage(OperationStage.STARTING)
            self._refresh_once()
            for camera_id in (1, 2):
                self._reconcile_camera(camera_id)
            failed_restarts = {
                camera_id
                for camera_id in restart_cameras
                if old_workers[camera_id] is not None
                and candidate.camera(camera_id).usb_port_key is not None
                and self._worker_snapshot(self._cameras[camera_id]) is None
            }
            if failed_restarts:
                failed_workers = {
                    camera_id: self._worker_snapshot(self._cameras[camera_id])
                    for camera_id in restart_cameras
                }
                for camera_id, worker in failed_workers.items():
                    if worker is not None:
                        worker.stop()
                    runtime = self._cameras[camera_id]
                    with runtime.state_lock:
                        if runtime.worker is worker:
                            runtime.worker = None
                        runtime.retry_after_mono_ns = 0
                try:
                    restored = self.settings_service.compare_and_swap(
                        current, expected_revision=saved.revision
                    )
                    self._refresh_once()
                    for camera_id in (1, 2):
                        runtime = self._cameras[camera_id]
                        with runtime.state_lock:
                            runtime.retry_after_mono_ns = 0
                        self._reconcile_camera(camera_id)
                    rollback_failures = {
                        camera_id
                        for camera_id in restart_cameras
                        if old_workers[camera_id] is not None
                        and current.camera(camera_id).usb_port_key is not None
                        and self._worker_snapshot(self._cameras[camera_id]) is None
                    }
                    if rollback_failures:
                        raise RuntimeError("previous_camera_restart_failed")
                except Exception as exc:
                    affected = sorted(failed_restarts)
                    for camera_id in affected:
                        runtime = self._cameras[camera_id]
                        with runtime.state_lock:
                            runtime.worker = None
                            runtime.blockers.add("settings_rollback_failed")
                            runtime.state = "blocked"
                    from raspi_recorder.domain.errors import SettingsApplyError

                    raise SettingsApplyError(
                        "settings_apply_rollback_failed",
                        {
                            "state": "blocked",
                            "failed_camera_ids": affected,
                            "current_revision": self.settings().revision,
                            "recovery_error": type(exc).__name__,
                        },
                    ) from exc
                from raspi_recorder.domain.errors import SettingsApplyError

                raise SettingsApplyError(
                    "settings_apply_failed_rolled_back",
                    {
                        "state": "previous_settings_restored",
                        "revision": restored.revision,
                        "failed_camera_ids": sorted(failed_restarts),
                    },
                )
            return saved

    def _validate_candidate(self, candidate: ApplicationSettings) -> None:
        port_keys = {port.port_key for port in self._ports}
        for camera_id, camera_settings in enumerate(candidate.cameras, start=1):
            if camera_settings.usb_port_key is None:
                continue
            if camera_settings.usb_port_key not in port_keys:
                raise ValueError(f"カメラ{camera_id}のUSB portが接続されていません")
            port = next(
                item
                for item in self._ports
                if item.port_key == camera_settings.usb_port_key
            )
            if not any(
                profile.width == camera_settings.width
                and profile.height == camera_settings.height
                and camera_settings.fps in profile.fps
                for profile in port.profiles
            ):
                raise UnsupportedCameraProfileError(camera_id, self.settings().revision)
        candidate_volume: MountedVolume | None = None
        if candidate.volume_persistent_id is None:
            matches = tuple(
                volume
                for volume in self._volumes
                if volume.kind is candidate.volume_kind
            )
            if candidate.volume_kind.value != "microSD" or len(matches) != 1:
                raise ValueError("選択媒体を一意に特定できません")
            candidate_volume = matches[0]
        else:
            candidate_volume = next(
                (
                    volume
                    for volume in self._volumes
                    if volume.persistent_id == candidate.volume_persistent_id
                    and volume.kind is candidate.volume_kind
                ),
                None,
            )
            if candidate_volume is None:
                raise ValueError("選択媒体が接続中ではありません")
        candidate.validate_capacity(candidate_volume.filesystem_bytes)
        self.volume_store.verify(candidate_volume)
        self._prepared_volume_key = self._volume_key(candidate_volume)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                with self._configuration_transition(
                    operation="supervisor", wait_seconds=0.0
                ):
                    self._refresh_once()
                    self.export_jobs.expire()
                    for camera_id in (1, 2):
                        self._reconcile_camera(camera_id)
                    self._reconcile_uncertain_update_operations()
            except RuntimeTransitionBusy:
                pass
            except Exception:
                # _refresh_once records volume errors itself; failures from
                # export expiry or camera reconcile must not be misreported
                # as a volume error. The next cycle retries.
                pass
            self._stop.wait(_SUPERVISION_INTERVAL_SECONDS)

    def _supervise_capacity(self) -> None:
        """容量保護はcameraの再調整・停止から独立して継続する。"""
        while not self._stop.is_set():
            try:
                self._maintain_capacity()
            except Exception as exc:
                self._capacity_error = type(exc).__name__
            self._stop.wait(_SUPERVISION_INTERVAL_SECONDS)

    def _maintain_capacity(self) -> None:
        if self._selected is None:
            self._capacity_error = "selected_volume_unavailable"
            return
        volume, root, volume_id = self._selected
        try:
            settings = self.settings()
            if not root.exists() and not any(
                camera.usb_port_key is not None for camera in settings.cameras
            ):
                # A fresh install defers creation of its owned recordings root
                # until a camera is bound. There is no managed media to protect
                # yet, so leave capacity supervision idle without reporting a
                # misleading storage error.
                self._capacity_error = None
                return
            thresholds = settings.validate_capacity(volume.filesystem_bytes)
            result = self._capacity_manager.maintain(
                volume_id, volume.persistent_id, thresholds
            )
        except Exception as exc:
            self._capacity_error = type(exc).__name__
            return
        self._capacity_error = None
        if result.warning is not None or not self._low_space_blocked:
            self._capacity_warning = result.warning

    def _refresh_once(self) -> None:
        # A failed enumeration/scan is not evidence that devices or volumes
        # disappeared: keep the last known state so one transient failure
        # does not tear down recordings, and surface the failure as a
        # discovery error instead.
        discovery_error: str | None = None
        try:
            self._ports = self.discovery.enumerate()
        except Exception as exc:
            discovery_error = type(exc).__name__
        try:
            self._volumes = self.volume_scanner.scan()
        except Exception as exc:
            if discovery_error is None:
                discovery_error = type(exc).__name__
        self._discovery_error = discovery_error
        settings = self.settings()
        requested_id = settings.volume_persistent_id
        if requested_id is None and settings.volume_kind.value == "microSD":
            candidates = tuple(
                volume for volume in self._volumes if volume.kind.value == "microSD"
            )
            selected = candidates[0] if len(candidates) == 1 else None
        else:
            candidates = tuple(
                volume
                for volume in self._volumes
                if volume.persistent_id == requested_id
                and volume.kind.value == settings.volume_kind.value
            )
            selected = candidates[0] if len(candidates) == 1 else None
        has_camera_binding = any(
            camera.usb_port_key is not None for camera in settings.cameras
        )
        seen_ids: set[str] = set()
        selected_state: tuple[MountedVolume, Path, str] | None = None
        selected_error: str | None = None
        for volume in self._volumes:
            volume_id = self._volume_id(volume.persistent_id)
            root = self.volume_store.recordings_root(volume)
            is_selected = selected is not None and (
                volume.persistent_id == selected.persistent_id
            )
            try:
                if is_selected and has_camera_binding:
                    prepared_key = self._volume_key(volume)
                    if self._prepared_volume_key != prepared_key:
                        root = self.volume_store.verify(volume)
                        self._prepared_volume_key = prepared_key
                    else:
                        self.volume_store.assert_owned(volume, root)
                        self.volume_store.assert_current(volume, root)
                elif root.exists():
                    self.volume_store.assert_owned(volume, root)
                elif not is_selected:
                    continue
            except (OSError, ValueError, RuntimeError) as exc:
                if is_selected:
                    selected_error = type(exc).__name__
                continue
            try:
                self.export_jobs.recover(volume, root)
            except (OSError, ValueError, RuntimeError, RecordingAccessError):
                self._capacity_warning = "stale_export_cleanup_failed"
            seen_ids.add(volume_id)
            self.catalog.upsert_volume(
                volume_id=volume_id,
                persistent_id=volume.persistent_id,
                kind=volume.kind,
                fs_type=volume.filesystem,
                marker_id=volume_id,
                connected=True,
                selected=is_selected,
                last_seen_utc_ns=self.clock.utc_ns(),
            )
            if is_selected:
                selected_state = (volume, root, volume_id)
        for previous in self.catalog.list_volumes():
            connected = previous.volume_id in seen_ids
            if previous.connected != connected:
                self.catalog.set_volume_connected(
                    previous.volume_id, connected, self.clock.utc_ns()
                )
        self._selected = selected_state
        if selected_state is None or self._prepared_volume_key != self._volume_key(
            selected_state[0]
        ):
            if selected_state is None:
                self._prepared_volume_key = None
        if selected_state is None:
            self._volume_error = (
                selected_error or "selected_volume_unavailable_or_ambiguous"
            )
        else:
            self._volume_error = None

    @staticmethod
    def _volume_id(persistent_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"raspi-recorder:{persistent_id}"))

    @staticmethod
    def _volume_key(volume: MountedVolume) -> tuple[str, int, str]:
        return (volume.persistent_id, volume.device_number, str(volume.mountpoint))

    def _reconcile_camera(
        self, camera_id: int, *, raise_on_stop_failure: bool = False
    ) -> None:
        runtime = self._cameras[camera_id]
        with runtime.state_lock:
            desired_running = runtime.desired_running
            current = runtime.worker
            retry_after_mono_ns = runtime.retry_after_mono_ns
            last_error = runtime.last_error
            generation = runtime.generation
        blockers: set[str] = set()
        settings = self.settings()
        camera_settings = settings.camera(camera_id)
        ports = self._ports
        port = next(
            (
                item
                for item in ports
                if camera_settings.usb_port_key is not None
                and item.port_key == camera_settings.usb_port_key
            ),
            None,
        )
        effective_camera_settings, _profile_reduced = _effective_camera_settings(
            camera_settings, settings.power_saving_enabled, port
        )
        profile_available = port is not None and any(
            profile.width == effective_camera_settings.width
            and profile.height == effective_camera_settings.height
            and effective_camera_settings.fps in profile.fps
            for profile in port.profiles
        )
        if camera_settings.usb_port_key is None:
            blockers.add("invalid_config")
        elif port is None:
            blockers.add("camera_missing")
        elif not profile_available:
            blockers.add("invalid_config")
        selected = self._selected
        if selected is None:
            blockers.add("storage_missing")
        if self._low_space_blocked:
            blockers.add("low_space")
        if any(
            operation.kind == "update"
            and operation.stage in {OperationStage.APPLYING, OperationStage.STARTING}
            for operation in self.settings_service.load_pending_operations()
        ):
            blockers.add("update_in_progress")
        # Settings block recording, but should not make an otherwise available
        # camera disappear from live view. Manual stop follows the same path.
        desired_recording = desired_running and not blockers
        desired_volume_id = (
            None if not desired_recording or selected is None else selected[2]
        )
        if current is not None:
            thread = current._thread
            alive = thread is not None and thread.is_alive()
            snapshot = current.snapshot
            must_restart = (
                not alive
                or snapshot["fatal_error"]
                or current.recording_enabled != desired_recording
                or _capture_settings_changed(
                    current.settings, effective_camera_settings
                )
                or (
                    current.recording_enabled
                    and desired_recording
                    and current._volume_id != desired_volume_id
                )
                or port is None
                or current.port.port_key != port.port_key
            )
            if must_restart:
                with runtime.state_lock:
                    runtime.state = "finalizing"
                    runtime.blockers = set(blockers)
                    stop_backoff_active = (
                        runtime.stop_failed_worker is current
                        and self.clock.monotonic_ns() < runtime.stop_retry_after_mono_ns
                    )
                if stop_backoff_active and not raise_on_stop_failure:
                    # This worker's stop already timed out once; retrying it
                    # on every supervisor cycle would block the loop for the
                    # full stop timeout each time. Wait out the backoff so
                    # other cameras and transitions keep flowing.
                    with runtime.state_lock:
                        runtime.blockers = {*blockers, "worker_stop_failed"}
                        runtime.state = "error"
                    return
                try:
                    current.stop()
                except Exception as exc:
                    with runtime.state_lock:
                        runtime.blockers = {*blockers, "worker_stop_failed"}
                        runtime.last_error = type(exc).__name__
                        runtime.state = "error"
                        if runtime.stop_failed_worker is current:
                            runtime.stop_failure_count = min(
                                runtime.stop_failure_count + 1, 6
                            )
                        else:
                            runtime.stop_failed_worker = current
                            runtime.stop_failure_count = 1
                        delay = min(300, 15 * 2 ** (runtime.stop_failure_count - 1))
                        runtime.stop_retry_after_mono_ns = (
                            self.clock.monotonic_ns() + delay * 1_000_000_000
                        )
                    if raise_on_stop_failure:
                        raise
                    return
                with runtime.state_lock:
                    if runtime.worker is current:
                        runtime.worker = None
                    runtime.last_error = snapshot["last_error"]
                    runtime.stop_failed_worker = None
                    runtime.stop_failure_count = 0
                    runtime.stop_retry_after_mono_ns = 0
                if snapshot["fatal_error"] and not self._stop.is_set():
                    with runtime.state_lock:
                        runtime.failure_count = min(runtime.failure_count + 1, 6)
                        delay = min(30, 2**runtime.failure_count)
                        runtime.retry_after_mono_ns = (
                            self.clock.monotonic_ns() + delay * 1_000_000_000
                        )
            else:
                if (
                    current.settings_revision != settings.revision
                    or current._desired_writer_settings != effective_camera_settings
                ):
                    current.apply_live_settings(
                        effective_camera_settings, settings.revision
                    )
                if not desired_running:
                    state = "stopped"
                elif blockers:
                    state = "blocked"
                elif snapshot.get("recording_active"):
                    state = "continuous"
                else:
                    state = "starting"
                with runtime.state_lock:
                    runtime.blockers = set(blockers)
                    runtime.state = state
                return

        if port is None or not profile_available:
            with runtime.state_lock:
                runtime.blockers = set(blockers)
                runtime.state = "stopped" if not desired_running else "blocked"
            return
        if self.clock.monotonic_ns() < retry_after_mono_ns:
            blockers.add(_worker_error_blocker(last_error))
            with runtime.state_lock:
                runtime.blockers = set(blockers)
                runtime.state = "error" if desired_running else "stopped"
            return
        recording_volume = selected if desired_recording else None
        if recording_volume is None:
            volume, root, volume_id = None, None, None
        else:
            volume, root, volume_id = recording_volume
        with runtime.state_lock:
            runtime.generation = generation + 1
            generation = runtime.generation
        worker = CameraCaptureWorker(
            camera_id=camera_id,
            generation=generation,
            settings=effective_camera_settings,
            port=port,
            recording_enabled=desired_recording,
            clock=self.clock,
            timezone=self.timezone,
            catalog=self.catalog,
            volume_store=self.volume_store,
            volume=volume,
            recordings_root=root,
            volume_id=volume_id,
            engine_epoch=self.epoch,
            settings_revision=settings.revision,
            live_directory=(
                self.live_root / f"camera-{camera_id}" / f"generation-{generation}"
            ),
            source_factory=self._camera_source_factory,
        )
        try:
            worker.start()
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                worker.stop()
            except Exception as stop_exc:
                with runtime.state_lock:
                    runtime.worker = worker
                    runtime.last_error = type(stop_exc).__name__
                    runtime.blockers = {*blockers, "worker_stop_failed"}
                    runtime.state = "error"
                return
            with runtime.state_lock:
                runtime.last_error = type(exc).__name__
                runtime.blockers = {*blockers, "io_error"}
                runtime.state = "error"
                runtime.failure_count = min(runtime.failure_count + 1, 6)
                delay = min(30, 2**runtime.failure_count)
                runtime.retry_after_mono_ns = (
                    self.clock.monotonic_ns() + delay * 1_000_000_000
                )
            return
        with runtime.state_lock:
            runtime.worker = worker
            runtime.blockers = set(blockers)
            runtime.state = (
                "starting" if desired_recording or desired_running else "stopped"
            )
            runtime.last_error = None
            runtime.failure_count = 0
            runtime.retry_after_mono_ns = 0

    def _load_desired_state(self) -> None:
        path = self.data_directory / "runtime-state.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._runtime_state_error = None
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._runtime_state_error = "runtime_state_invalid"
            for runtime in self._cameras.values():
                runtime.desired_running = False
            return
        if not isinstance(raw, dict) or set(raw) != {"boot_id", "cameras"}:
            self._runtime_state_error = "runtime_state_invalid"
            for runtime in self._cameras.values():
                runtime.desired_running = False
            return
        if raw["boot_id"] != self.clock.boot_id():
            self._runtime_state_error = None
            self._save_desired_state()
            return
        cameras = raw["cameras"]
        if not isinstance(cameras, dict) or set(cameras) != {"1", "2"}:
            self._runtime_state_error = "runtime_state_invalid"
            for runtime in self._cameras.values():
                runtime.desired_running = False
            return
        if not all(type(cameras[key]) is bool for key in ("1", "2")):
            self._runtime_state_error = "runtime_state_invalid"
            for runtime in self._cameras.values():
                runtime.desired_running = False
            return
        for camera_id in (1, 2):
            self._cameras[camera_id].desired_running = cameras[str(camera_id)]
        self._runtime_state_error = None

    def _save_desired_state(self) -> None:
        path = self.data_directory / "runtime-state.json"
        temporary = self.data_directory / f".runtime-state-{uuid.uuid4().hex}.tmp"
        payload = json.dumps(
            {
                "boot_id": self.clock.boot_id(),
                "cameras": {
                    str(camera_id): runtime.desired_running
                    for camera_id, runtime in self._cameras.items()
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.data_directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._runtime_state_error = None
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._runtime_state_error = "runtime_state_write_failed"

    def _runtime_warnings(
        self,
        settings: ApplicationSettings,
        sync_status: ClockSyncStatus | None,
    ) -> list[str]:
        warnings: list[str] = []
        if sync_status is None or sync_status.synchronized is not True:
            warnings.append("time_synchronization_not_verified")
        if self._discovery_error is not None:
            warnings.append("device_discovery_failed")
        return warnings

    def request_live_demand(self, camera_id: int) -> bool:
        """視聴中cameraだけのlive需要を更新する。録画状態は変更しない。"""
        if type(camera_id) is not int or camera_id not in (1, 2):
            return False
        runtime = self._cameras[camera_id]
        with runtime.state_lock:
            worker = runtime.worker
        return worker is not None and worker.request_live_demand()

    def release_live_demand(self, camera_id: int) -> None:
        if type(camera_id) is not int or camera_id not in (1, 2):
            return
        runtime = self._cameras[camera_id]
        with runtime.state_lock:
            worker = runtime.worker
        if worker is not None:
            worker.release_live_demand()

    def request_preview_demand(self, camera_id: int) -> bool:
        """通常previewの需要をcamera workerへ渡す。"""
        if type(camera_id) is not int or camera_id not in (1, 2):
            return False
        runtime = self._cameras[camera_id]
        with runtime.state_lock:
            worker = runtime.worker
        return worker is not None and worker.request_preview_demand()

    def release_preview_demand(self, camera_id: int) -> None:
        if type(camera_id) is not int or camera_id not in (1, 2):
            return
        runtime = self._cameras[camera_id]
        with runtime.state_lock:
            worker = runtime.worker
        if worker is not None:
            worker.release_preview_demand()


class EngineControlHandler:
    """Unix socket上で許可されたcontrol-plane APIを提供する。"""

    def __init__(self, runtime: RecorderRuntime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._requests: dict[str, tuple[str, EngineResponse]] = {}
        self._operation_threads: list[threading.Thread] = []

    def handle(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> EngineResponse:
        from urllib.parse import parse_qs, urlsplit

        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if method == "GET" and parsed.path == "/api/v1/status":
            return EngineResponse(200, self.runtime.status())
        if method == "GET" and parsed.path == "/api/v1/capabilities":
            return EngineResponse(200, self.runtime.capabilities())
        if method == "GET" and parsed.path == "/api/v1/settings":
            return EngineResponse(200, {"settings": self.runtime.settings_payload()})
        if method == "GET" and parsed.path == "/api/v1/time/status":
            return EngineResponse(200, self.runtime.time_status())
        if method == "GET" and parsed.path == "/api/v1/update/status":
            status = self.runtime.update_service.status()
            if self.runtime._update_configuration_error is not None:
                status["message"] = self.runtime._update_configuration_error
            return EngineResponse(200, status)
        if method == "POST" and len(parts) == 5 and parts[2] == "live":
            if parts[3] not in {"1", "2"} or parts[4] != "demand":
                return EngineResponse(404, {"error": {"code": "not_found"}})
            if (
                body is None
                or set(body) != {"active"}
                or type(body["active"]) is not bool
            ):
                return EngineResponse(400, {"error": {"code": "invalid_live_demand"}})
            if body["active"]:
                accepted = self.runtime.request_live_demand(int(parts[3]))
            else:
                self.runtime.release_live_demand(int(parts[3]))
                accepted = True
            return EngineResponse(200, {"accepted": accepted})
        if method == "POST" and len(parts) == 6 and parts[2] == "cameras":
            if parts[3] not in {"1", "2"} or parts[4:] != ["preview", "demand"]:
                return EngineResponse(404, {"error": {"code": "not_found"}})
            if (
                body is None
                or set(body) != {"active"}
                or type(body["active"]) is not bool
            ):
                return EngineResponse(
                    400, {"error": {"code": "invalid_preview_demand"}}
                )
            if body["active"]:
                accepted = self.runtime.request_preview_demand(int(parts[3]))
            else:
                self.runtime.release_preview_demand(int(parts[3]))
                accepted = True
            return EngineResponse(200, {"accepted": accepted})
        if method == "POST" and parsed.path == "/api/v1/update/check":
            return self._post_update_check(body)
        if method == "POST" and parsed.path == "/api/v1/update/apply":
            return self._post_update_apply(body)
        if method == "PATCH" and parsed.path == "/api/v1/settings":
            return self._patch_settings(body)
        if method == "POST" and parsed.path == "/api/v1/time":
            return self._post_time(body)
        if method == "GET" and len(parts) == 4 and parts[2] == "operations":
            try:
                operation = self.runtime.settings_service.load_operation(parts[3])
            except (AttributeError, RuntimeError):
                operation = None
            if operation is None:
                return EngineResponse(404, {"error": {"code": "operation_not_found"}})
            return EngineResponse(200, _operation_json(operation))
        if method == "POST" and parsed.path == "/api/v1/exports":
            if body is None or set(body) != {"recording_ids"}:
                return EngineResponse(400, {"error": {"code": "invalid_export"}})
            try:
                return EngineResponse(
                    202, self.runtime.export_jobs.create(body["recording_ids"])
                )
            except ValueError as exc:
                return EngineResponse(400, {"error": {"code": str(exc)}})
            except RecordingAccessError as exc:
                return EngineResponse(409, {"error": {"code": str(exc)}})
        if method == "GET" and len(parts) == 4 and parts[2] == "exports":
            export = self.runtime.export_jobs.get(parts[3])
            return (
                EngineResponse(200, export)
                if export
                else EngineResponse(404, {"error": {"code": "export_not_found"}})
            )
        if method == "POST" and len(parts) == 5 and parts[2] == "cameras":
            return self._camera_command(parts[3], parts[4])
        if method == "POST" and parsed.path == "/api/v1/recordings/delete-preview":
            if body is None or set(body) != {"recording_ids"}:
                return EngineResponse(
                    400, {"error": {"code": "invalid_delete_preview"}}
                )
            recording_ids = body["recording_ids"]
            if not isinstance(recording_ids, list) or not all(
                isinstance(value, str) for value in recording_ids
            ):
                return EngineResponse(400, {"error": {"code": "invalid_recording_ids"}})
            try:
                preview = self.runtime.recording_access.preview_delete(recording_ids)
            except (RecordingAccessError, ValueError) as exc:
                return EngineResponse(409, {"error": {"code": str(exc)}})
            return EngineResponse(
                200,
                {
                    "confirmation_id": preview.confirmation_id,
                    "recording_ids": list(preview.recording_ids),
                    "recording_count": preview.recording_count,
                    "size_bytes": preview.size_bytes,
                    "expires_mono_ns": preview.expires_mono_ns,
                },
            )
        if method == "POST" and parsed.path == "/api/v1/recordings/delete":
            if body is None or set(body) != {"recording_ids", "confirmation_id"}:
                return EngineResponse(400, {"error": {"code": "invalid_delete"}})
            recording_ids = body["recording_ids"]
            confirmation_id = body["confirmation_id"]
            if (
                not isinstance(recording_ids, list)
                or not all(isinstance(value, str) for value in recording_ids)
                or not isinstance(confirmation_id, str)
            ):
                return EngineResponse(400, {"error": {"code": "invalid_delete"}})
            try:
                results = self.runtime.recording_access.delete_confirmed(
                    confirmation_id, recording_ids
                )
            except (RecordingAccessError, ValueError) as exc:
                return EngineResponse(409, {"error": {"code": str(exc)}})
            return EngineResponse(
                200,
                {
                    "items": [
                        {
                            "recording_id": item.recording_id,
                            "deleted": item.deleted,
                            "code": item.code,
                        }
                        for item in results
                    ]
                },
            )
        if method == "GET" and parsed.path == "/api/v1/recordings":
            try:
                query = parse_qs(parsed.query, strict_parsing=True)
            except ValueError:
                return EngineResponse(400, {"error": {"code": "invalid_query"}})
            allowed_query = {
                "camera_id",
                "from_utc_ns",
                "to_utc_ns",
                "limit",
                "before_sequence",
            }
            if set(query) - allowed_query:
                return EngineResponse(400, {"error": {"code": "invalid_query"}})
            if any(len(values) != 1 for values in query.values()):
                return EngineResponse(400, {"error": {"code": "invalid_query"}})
            try:
                camera_id = _optional_int(query, "camera_id", 1, 2)
                start = _optional_int(query, "from_utc_ns", 0, 2**63 - 1)
                end = _optional_int(query, "to_utc_ns", 0, 2**63 - 1)
                limit = _optional_int(query, "limit", 1, 500) or 100
                cursor = _optional_int(query, "before_sequence", 1, 2**63 - 1)
                records = self.runtime.catalog.list_recordings(
                    camera_id=camera_id,
                    from_utc_ns=start,
                    to_utc_ns=end,
                    limit=limit,
                    before_sequence=cursor,
                )
            except (ValueError, KeyError):
                return EngineResponse(400, {"error": {"code": "invalid_query"}})
            return EngineResponse(
                200,
                {
                    "items": [_recording_json(item) for item in records],
                    "next_before_sequence": (
                        records[-1].creation_sequence if len(records) == limit else None
                    ),
                },
            )
        if method == "GET" and len(parts) == 4 and parts[2] == "recordings":
            recording = self.runtime.catalog.get_recording(parts[3])
            if recording is None:
                return EngineResponse(404, {"error": {"code": "not_found"}})
            return EngineResponse(200, _recording_json(recording))
        return EngineResponse(501, {"error": {"code": "not_implemented"}})

    def stream(
        self, method: str, path: str, range_header: str | None = None
    ) -> EngineResponse | EngineStreamResponse:
        from urllib.parse import urlsplit

        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if method == "GET" and len(parts) == 5 and parts[2] == "cameras":
            if parts[3] not in {"1", "2"} or parts[4] != "preview.jpg":
                return EngineResponse(404, {"error": {"code": "not_found"}})
            camera_id = int(parts[3])
            with self.runtime._lock:
                worker = self.runtime._cameras[camera_id].worker
                if worker is None:
                    return EngineResponse(
                        404, {"error": {"code": "preview_unavailable"}}
                    )
                image = worker._preview.fresh_image()
            if image is None:
                return EngineResponse(503, {"error": {"code": "preview_not_ready"}})
            return EngineStreamResponse(
                200,
                {
                    "Content-Type": "image/jpeg",
                    "Content-Length": str(len(image)),
                    "Cache-Control": "no-store",
                },
                BytesIO(image),
                lambda: None,
            )
        if method == "GET" and len(parts) == 6 and parts[2] == "live":
            return self._live_stream(parts[3], parts[4], parts[5])
        if (
            method == "GET"
            and len(parts) == 5
            and parts[2] == "exports"
            and parts[4] == "download"
        ):
            try:
                handle, size, close = self.runtime.export_jobs.open_download(parts[3])
            except RecordingAccessError as exc:
                return EngineResponse(409, {"error": {"code": str(exc)}})
            return EngineStreamResponse(
                200,
                {
                    "Content-Type": "application/zip",
                    "Content-Length": str(size),
                    "Content-Disposition": (
                        f'attachment; filename="export-{parts[3]}.zip"'
                    ),
                    "Cache-Control": "no-store",
                },
                handle,
                close,
            )
        if method != "GET" or len(parts) != 5 or parts[2] != "recordings":
            return EngineResponse(404, {"error": {"code": "not_found"}})
        recording_id, operation = parts[3], parts[4]
        if operation not in {"content", "download"}:
            return EngineResponse(404, {"error": {"code": "not_found"}})
        lease = None
        try:
            lease = self.runtime.recording_access.read_lease(
                recording_id,
                purpose="playback" if operation == "content" else "download",
            )
            file_handle = lease.file
            file_handle.seek(0, 2)
            size = file_handle.tell()
            start, end, status = 0, size - 1, 200
            if range_header is not None:
                selected = _parse_range(range_header, size)
                if selected is None:
                    lease.close()
                    return EngineResponse(
                        416, {"error": {"code": "range_not_satisfiable"}}
                    )
                start, end = selected
                status = 206
            file_handle.seek(start)
            filename = PurePosixPath(lease.recording.relative_path).name
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(max(0, end - start + 1)),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                    if operation == "download"
                    else "inline"
                ),
            }
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return EngineStreamResponse(status, headers, file_handle, lease.close)
        except RecordingAccessError as exc:
            if lease is not None:
                lease.close()
            error_code = str(exc)
            status = 409 if "writing" in error_code or "in_use" in error_code else 404
            return EngineResponse(status, {"error": {"code": error_code}})
        except (KeyError, OSError, ValueError):
            if lease is not None:
                lease.close()
            return EngineResponse(404, {"error": {"code": "recording_unavailable"}})

    def _live_stream(
        self, camera_text: str, generation_text: str, filename: str
    ) -> EngineResponse | EngineStreamResponse:
        if (
            camera_text not in {"1", "2"}
            or not generation_text.isascii()
            or (not generation_text.isdecimal())
        ):
            return EngineResponse(404, {"error": {"code": "not_found"}})
        if filename != "index.m3u8" and not re.fullmatch(
            r"segment[0-9]{6}\.ts", filename
        ):
            return EngineResponse(404, {"error": {"code": "not_found"}})
        camera_id = int(camera_text)
        generation = int(generation_text)
        if (
            generation < 1
            or generation > 2**63 - 1
            or str(generation) != generation_text
        ):
            return EngineResponse(404, {"error": {"code": "not_found"}})
        with self.runtime._lock:
            worker = self.runtime._cameras[camera_id].worker
            if worker is None:
                return EngineResponse(
                    404, {"error": {"code": "live_generation_expired"}}
                )
            live = worker._live
            directory_for_generation = getattr(live, "directory_for_generation", None)
            if callable(directory_for_generation):
                directory = directory_for_generation(generation)
            elif worker.generation == generation:
                directory = live.directory
            else:
                directory = None
            if directory is None:
                return EngineResponse(
                    404, {"error": {"code": "live_generation_expired"}}
                )
            expected = (
                self.runtime.live_root
                / f"camera-{camera_id}"
                / f"generation-{generation}"
            )
            if directory != expected or any(
                path.is_symlink() for path in (directory, *directory.parents[:4])
            ):
                return EngineResponse(404, {"error": {"code": "live_unavailable"}})
            request_viewer = getattr(live, "request_viewer", None)
            if callable(request_viewer):
                request_viewer()
        target = directory / filename
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(target, flags)
            info = os.fstat(descriptor)
            maximum = 262_144 if filename == "index.m3u8" else 32 * 1024 * 1024
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                os.close(descriptor)
                return EngineResponse(404, {"error": {"code": "live_unavailable"}})
            handle = os.fdopen(descriptor, "rb")
            if filename == "index.m3u8":
                raw = handle.read(maximum + 1)
                handle.close()
                if len(raw) > maximum:
                    return EngineResponse(404, {"error": {"code": "live_unavailable"}})
                try:
                    playlist = raw.decode("ascii")
                except UnicodeDecodeError:
                    return EngineResponse(404, {"error": {"code": "live_unavailable"}})
                base = f"/api/v1/live/{camera_id}/{generation}/"
                lines: list[str] = []
                for line in playlist.splitlines():
                    if not line or line.startswith("#"):
                        lines.append(line)
                    elif re.fullmatch(r"segment[0-9]{6}\.ts", line):
                        lines.append(base + line)
                    else:
                        return EngineResponse(
                            404, {"error": {"code": "live_playlist_invalid"}}
                        )
                payload = ("\n".join(lines) + "\n").encode("ascii")
                return EngineStreamResponse(
                    200,
                    {
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Content-Length": str(len(payload)),
                        "Cache-Control": "no-store",
                    },
                    BytesIO(payload),
                    lambda: None,
                )
            return EngineStreamResponse(
                200,
                {
                    "Content-Type": "video/mp2t",
                    "Content-Length": str(info.st_size),
                    "Cache-Control": "no-store",
                },
                handle,
                handle.close,
            )
        except FileNotFoundError:
            return EngineResponse(404, {"error": {"code": "live_not_ready"}})
        except OSError:
            return EngineResponse(404, {"error": {"code": "live_unavailable"}})

    def _post_update_check(self, body: dict[str, Any] | None) -> EngineResponse:
        if not isinstance(body, dict) or set(body) != {"request_id"}:
            return EngineResponse(400, {"error": {"code": "invalid_update_check"}})
        request_id = body["request_id"]
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            return EngineResponse(400, {"error": {"code": "invalid_request_id"}})
        fingerprint = repr(("update-check",))
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                return previous[1]
            persisted = self.runtime.settings_service.load_operation_by_request_id(
                request_id
            )
            if persisted is not None:
                if (
                    persisted.kind != "update-check"
                    or persisted.target.get("scope") != "update_check"
                ):
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                response = EngineResponse(202, {"operation_id": persisted.operation_id})
                self._remember_request_locked(request_id, fingerprint, response)
                return response
            if not self.runtime.update_service.source_configured:
                code = (
                    self.runtime._update_configuration_error
                    or "update_source_unconfigured"
                )
                return EngineResponse(503, {"error": {"code": code}})
            active = self._active_operation()
            if active is not None:
                return _operation_in_progress(active)
            if self._operation_worker_busy_locked():
                return EngineResponse(503, {"error": {"code": "operation_worker_busy"}})
            settings = self.runtime.settings().to_payload()
            operation_id = uuid.uuid4().hex
            try:
                operation = self.runtime.settings_service.create_operation(
                    operation_id=operation_id,
                    request_id=request_id,
                    kind="update-check",
                    target={"scope": "update_check"},
                    previous_settings=settings,
                    requested_settings=settings,
                )
            except RequestIdConflict:
                return EngineResponse(409, {"error": {"code": "request_id_reused"}})
            response = EngineResponse(202, {"operation_id": operation.operation_id})
            self._remember_request_locked(request_id, fingerprint, response)
            if operation.stage is OperationStage.QUEUED:
                self._start_operation_worker_locked(
                    operation.operation_id,
                    self._run_update_check,
                    (operation.operation_id,),
                    f"recorder-update-check-{operation.operation_id[:8]}",
                )
            return response

    def _post_update_apply(self, body: dict[str, Any] | None) -> EngineResponse:
        if not isinstance(body, dict) or set(body) != {
            "request_id",
            "version",
            "confirmed",
        }:
            return EngineResponse(400, {"error": {"code": "invalid_update_apply"}})
        request_id = body["request_id"]
        version = body["version"]
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            return EngineResponse(400, {"error": {"code": "invalid_request_id"}})
        if not isinstance(version, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            version,
        ):
            return EngineResponse(400, {"error": {"code": "invalid_update_version"}})
        if body["confirmed"] is not True:
            return EngineResponse(
                400, {"error": {"code": "update_confirmation_required"}}
            )
        fingerprint = repr(("update-apply", version, True))
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                return previous[1]
            persisted = self.runtime.settings_service.load_operation_by_request_id(
                request_id
            )
            if persisted is not None:
                if (
                    persisted.kind != "update"
                    or persisted.requested_settings.get("version") != version
                    or persisted.requested_settings.get("confirmed") is not True
                ):
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                response = EngineResponse(202, {"operation_id": persisted.operation_id})
                self._remember_request_locked(request_id, fingerprint, response)
                return response
            if not self.runtime.update_service.source_configured:
                code = (
                    self.runtime._update_configuration_error
                    or "update_source_unconfigured"
                )
                return EngineResponse(503, {"error": {"code": code}})
            active = self._active_operation()
            if active is not None:
                return _operation_in_progress(active)
            if self._operation_worker_busy_locked():
                return EngineResponse(503, {"error": {"code": "operation_worker_busy"}})
            status = self.runtime.update_service.status()
            candidate = status.get("candidate")
            if not isinstance(candidate, dict) or candidate.get("version") != version:
                return EngineResponse(
                    409, {"error": {"code": "update_candidate_changed"}}
                )
            if candidate.get("enough_space") is not True:
                return EngineResponse(
                    409, {"error": {"code": "update_disk_space_insufficient"}}
                )
            settings = self.runtime.settings().to_payload()
            operation_id = uuid.uuid4().hex
            try:
                operation = self.runtime.settings_service.create_operation(
                    operation_id=operation_id,
                    request_id=request_id,
                    kind="update",
                    target={"scope": "update", "version": version},
                    previous_settings=settings,
                    requested_settings={"version": version, "confirmed": True},
                )
            except RequestIdConflict:
                return EngineResponse(409, {"error": {"code": "request_id_reused"}})
            response = EngineResponse(202, {"operation_id": operation.operation_id})
            self._remember_request_locked(request_id, fingerprint, response)
            if operation.stage is OperationStage.QUEUED:
                self._start_operation_worker_locked(
                    operation.operation_id,
                    self._run_update_apply,
                    (operation.operation_id, version),
                    f"recorder-update-apply-{operation.operation_id[:8]}",
                )
            return response

    def _run_update_apply(self, operation_id: str, version: str) -> None:
        stage = OperationStage.QUEUED

        def advance(
            next_stage: OperationStage,
            *,
            error: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> None:
            nonlocal stage
            self.runtime.settings_service.transition_operation(
                operation_id,
                stage,
                next_stage,
                error=error,
                recovery=recovery,
            )
            stage = next_stage

        try:
            advance(OperationStage.VALIDATING)
            prepared = self.runtime.update_service.prepare(
                version=version,
                operation_id=operation_id,
            )
            target = {
                "scope": "update",
                "version": version,
                "asset_sha256": prepared.archive_sha256,
            }
            self.runtime.settings_service.update_operation_target(
                operation_id,
                OperationStage.VALIDATING,
                target,
            )
            self.runtime.update_helper_client.apply(
                operation_id=operation_id,
                version=version,
                asset_sha256=prepared.archive_sha256,
            )
        except Exception as exc:
            if isinstance(exc, UpdateHelperClientError) and exc.uncertain:
                # The helper may or may not have accepted the handoff, so
                # the operation stays pending — but only for a bounded grace
                # period under supervisor reconciliation.
                self.runtime._track_uncertain_update_operation(operation_id)
                return
            try:
                self.runtime.update_service.cleanup(operation_id)
            except (OSError, RuntimeError, ValueError):
                pass
            try:
                code = (
                    str(exc)
                    if isinstance(exc, (UpdateServiceError, UpdateHelperClientError))
                    else type(exc).__name__
                )
                advance(
                    OperationStage.FAILED,
                    error={"code": code[:120]},
                    recovery={"state": "previous_release_unchanged"},
                )
            except Exception:
                pass

    def _run_update_check(self, operation_id: str) -> None:
        stage = OperationStage.QUEUED

        def advance(
            next_stage: OperationStage,
            *,
            error: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> None:
            nonlocal stage
            self.runtime.settings_service.transition_operation(
                operation_id,
                stage,
                next_stage,
                error=error,
                recovery=recovery,
            )
            stage = next_stage

        try:
            advance(OperationStage.VALIDATING)
            status = self.runtime.update_service.check()
            candidate = status.get("candidate")
            advance(
                OperationStage.SUCCEEDED,
                recovery={
                    "state": "update_check_complete",
                    "candidate_available": isinstance(candidate, dict),
                },
            )
        except Exception as exc:
            try:
                code = (
                    str(exc)
                    if isinstance(exc, UpdateServiceError)
                    else type(exc).__name__
                )
                advance(
                    OperationStage.FAILED,
                    error={"code": code[:120]},
                    recovery={"state": "update_check_failed"},
                )
            except Exception:
                pass

    def _patch_settings(self, body: dict[str, Any] | None) -> EngineResponse:
        if not isinstance(body, dict) or set(body) != {
            "expected_revision",
            "request_id",
            "settings",
        }:
            return EngineResponse(400, {"error": {"code": "invalid_settings_request"}})
        expected_revision = body["expected_revision"]
        request_id = body["request_id"]
        candidate_raw = body["settings"]
        if type(expected_revision) is not int or expected_revision < 0:
            return EngineResponse(400, {"error": {"code": "invalid_revision"}})
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            return EngineResponse(400, {"error": {"code": "invalid_request_id"}})
        try:
            candidate = ApplicationSettings.from_payload(candidate_raw)
        except (TypeError, ValueError):
            return EngineResponse(400, {"error": {"code": "invalid_settings"}})
        current = self.runtime.settings()
        fingerprint = repr(("settings", expected_revision, candidate.to_payload()))
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                return previous[1]
            active = self._active_operation()
            if active is not None:
                return _operation_in_progress(active)
            if self._operation_worker_busy_locked():
                return EngineResponse(503, {"error": {"code": "operation_worker_busy"}})
            operation_id = uuid.uuid4().hex
            try:
                operation = self.runtime.settings_service.create_operation(
                    operation_id=operation_id,
                    request_id=request_id,
                    kind="settings",
                    target={"scope": "settings", "base_revision": expected_revision},
                    previous_settings=current.to_payload(),
                    requested_settings=candidate.to_payload(),
                )
            except RequestIdConflict:
                return EngineResponse(409, {"error": {"code": "request_id_reused"}})
            operation_id = operation.operation_id
            response = EngineResponse(202, {"operation_id": operation_id})
            self._remember_request_locked(request_id, fingerprint, response)
            if operation.stage is not OperationStage.QUEUED:
                return response
            self._start_operation_worker_locked(
                operation_id,
                self._apply_settings_job,
                (operation_id, candidate, expected_revision),
                f"recorder-operation-{operation_id[:8]}",
            )
            return response

    def _post_time(self, body: dict[str, Any] | None) -> EngineResponse:
        required = {
            "expected_revision",
            "request_id",
            "datetime",
            "timezone",
            "fold",
            "ntp_policy",
            "confirmed",
        }
        if not isinstance(body, dict) or set(body) != required:
            return EngineResponse(400, {"error": {"code": "invalid_time_request"}})
        expected_revision = body["expected_revision"]
        request_id = body["request_id"]
        local_datetime = body["datetime"]
        timezone_name = body["timezone"]
        fold = body["fold"]
        ntp_policy = body["ntp_policy"]
        if type(expected_revision) is not int or expected_revision < 0:
            return EngineResponse(400, {"error": {"code": "invalid_revision"}})
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            return EngineResponse(400, {"error": {"code": "invalid_request_id"}})
        if (
            not isinstance(local_datetime, str)
            or not isinstance(timezone_name, str)
            or type(fold) is not int
            or fold not in (0, 1)
            or ntp_policy not in {"keep", "pause_and_resume"}
            or body["confirmed"] is not True
        ):
            return EngineResponse(400, {"error": {"code": "invalid_time_request"}})
        current_timezone = self.runtime._timezone_name()
        if current_timezone is None or timezone_name != current_timezone:
            return EngineResponse(409, {"error": {"code": "system_timezone_changed"}})
        try:
            utc_ns = _local_datetime_to_utc_ns(local_datetime, timezone_name, fold)
        except (OverflowError, ValueError, ZoneInfoNotFoundError):
            return EngineResponse(400, {"error": {"code": "invalid_local_datetime"}})
        current = self.runtime.settings()
        if current.revision != expected_revision:
            return EngineResponse(
                409,
                {
                    "error": {
                        "code": "revision_conflict",
                        "current_revision": current.revision,
                    }
                },
            )
        fingerprint = repr(
            (
                "time",
                expected_revision,
                local_datetime,
                timezone_name,
                fold,
                ntp_policy,
                True,
            )
        )
        target = {
            "scope": "system_clock",
            "base_revision": expected_revision,
            "utc_ns": utc_ns,
            "timezone": timezone_name,
            "ntp_policy": ntp_policy,
        }
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    return EngineResponse(409, {"error": {"code": "request_id_reused"}})
                return previous[1]
            active = self._active_operation()
            if active is not None:
                return _operation_in_progress(active)
            if self._operation_worker_busy_locked():
                return EngineResponse(503, {"error": {"code": "operation_worker_busy"}})
            operation_id = uuid.uuid4().hex
            try:
                operation = self.runtime.settings_service.create_operation(
                    operation_id=operation_id,
                    request_id=request_id,
                    kind="time",
                    target=target,
                    previous_settings=current.to_payload(),
                    requested_settings=current.to_payload(),
                )
            except RequestIdConflict:
                return EngineResponse(409, {"error": {"code": "request_id_reused"}})
            operation_id = operation.operation_id
            response = EngineResponse(202, {"operation_id": operation_id})
            self._remember_request_locked(request_id, fingerprint, response)
            if operation.stage is not OperationStage.QUEUED:
                return response
            self._start_operation_worker_locked(
                operation_id,
                self._apply_time_job,
                (operation_id, utc_ns, timezone_name, expected_revision, ntp_policy),
                f"recorder-time-operation-{operation_id[:8]}",
            )
            return response

    def _apply_settings_job(
        self, operation_id: str, candidate: ApplicationSettings, expected_revision: int
    ) -> None:
        stage = OperationStage.QUEUED

        def advance(
            next_stage: OperationStage,
            *,
            error: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> None:
            nonlocal stage
            self.runtime.settings_service.transition_operation(
                operation_id,
                stage,
                next_stage,
                error=error,
                recovery=recovery,
            )
            stage = next_stage

        try:
            self.runtime.apply_settings(candidate, expected_revision, on_stage=advance)
            advance(OperationStage.SUCCEEDED)
        except Exception as exc:
            try:
                current = self.runtime.settings()
                code = getattr(exc, "code", type(exc).__name__)
                recovery = getattr(
                    exc,
                    "recovery",
                    {
                        "state": "settings_and_workers_require_review",
                        "current_revision": current.revision,
                        "requested_base_revision": expected_revision,
                    },
                )
                advance(
                    OperationStage.FAILED,
                    error={"code": code},
                    recovery=recovery,
                )
            except Exception:
                pass

    def _apply_time_job(
        self,
        operation_id: str,
        utc_ns: int,
        timezone_name: str,
        expected_revision: int,
        ntp_policy: str,
    ) -> None:
        stage = OperationStage.QUEUED

        def advance(
            next_stage: OperationStage,
            *,
            error: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> None:
            nonlocal stage
            self.runtime.settings_service.transition_operation(
                operation_id,
                stage,
                next_stage,
                error=error,
                recovery=recovery,
            )
            stage = next_stage

        try:
            self.runtime.apply_system_time(
                utc_ns,
                timezone_name,
                expected_revision,
                ntp_policy,
                on_stage=advance,
            )
            advance(OperationStage.SUCCEEDED)
        except Exception as exc:
            try:
                current = self.runtime.settings()
                advance(
                    OperationStage.FAILED,
                    error={"code": getattr(exc, "code", type(exc).__name__)},
                    recovery=getattr(
                        exc,
                        "recovery",
                        {
                            "state": "clock_and_workers_require_review",
                            "clock_changed": None,
                            "current_revision": current.revision,
                            "requested_base_revision": expected_revision,
                        },
                    ),
                )
            except Exception:
                pass

    def _camera_command(self, camera_text: str, command: str) -> EngineResponse:
        if camera_text not in {"1", "2"} or command not in {"start", "stop"}:
            return EngineResponse(404, {"error": {"code": "not_found"}})
        with self._lock:
            active = self._active_operation()
            if active is not None:
                return _operation_in_progress(active)
            try:
                self.runtime.set_camera_running(int(camera_text), command == "start")
            except (OSError, RuntimeError, ValueError) as exc:
                return EngineResponse(409, {"error": {"code": type(exc).__name__}})
            return EngineResponse(200, self.runtime.status())

    def _remember_request_locked(
        self, request_id: str, fingerprint: str, response: EngineResponse
    ) -> None:
        self._requests[request_id] = (fingerprint, response)
        while len(self._requests) > _MAX_ENGINE_REQUEST_CACHE:
            oldest_request_id = next(iter(self._requests))
            del self._requests[oldest_request_id]

    def _operation_worker_busy_locked(self) -> bool:
        alive_workers: list[threading.Thread] = []
        for worker in tuple(self._operation_threads):
            is_alive = getattr(worker, "is_alive", None)
            if callable(is_alive) and is_alive():
                alive_workers.append(worker)
        self._operation_threads = alive_workers
        return bool(alive_workers)

    def _start_operation_worker_locked(
        self,
        operation_id: str,
        target: Callable[..., None],
        arguments: tuple[Any, ...],
        name: str,
    ) -> None:
        if self._operation_worker_busy_locked():
            raise RuntimeError("operation_worker_busy")
        thread = threading.Thread(target=target, args=arguments, name=name, daemon=True)
        self._operation_threads.append(thread)
        try:
            thread.start()
        except Exception as exc:
            self._operation_threads = [
                worker for worker in self._operation_threads if worker is not thread
            ]
            self.runtime.settings_service.transition_operation(
                operation_id,
                OperationStage.QUEUED,
                OperationStage.FAILED,
                error={
                    "code": "operation_worker_start_failed",
                    "type": type(exc).__name__,
                },
            )

    def _active_operation(self) -> OperationSnapshot | None:
        """Return the oldest nonterminal operation in the shared control queue."""
        pending = self.runtime.settings_service.load_pending_operations()
        if not pending:
            return None
        return pending[0]


def _optional_int(
    query: dict[str, list[str]], key: str, minimum: int, maximum: int
) -> int | None:
    values = query.get(key)
    if values is None:
        return None
    if len(values) != 1 or not re_fullmatch_integer(values[0]):
        raise ValueError(key)
    value = int(values[0])
    if not minimum <= value <= maximum:
        raise ValueError(key)
    return value


def re_fullmatch_integer(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdecimal()


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    import re

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if match is None or size <= 0 or (not match.group(1) and not match.group(2)):
        return None
    first_text, last_text = match.groups()
    if not first_text:
        suffix = int(last_text)
        if suffix <= 0:
            return None
        return max(0, size - suffix), size - 1
    first = int(first_text)
    last = size - 1 if not last_text else int(last_text)
    if first >= size or last < first:
        return None
    return first, min(last, size - 1)


def _capture_settings_changed(before: CameraSettings, after: CameraSettings) -> bool:
    """焼き込みだけの変更を除き、camera worker再作成の要否を返す。"""
    comparable = replace(
        after,
        overlay_enabled=before.overlay_enabled,
        overlay_corner=before.overlay_corner,
        overlay_size=before.overlay_size,
    )
    return before != comparable


def _recording_json(recording: Any) -> dict[str, Any]:
    return {
        "recording_id": recording.recording_id,
        "volume_id": recording.volume_id,
        "relative_path": recording.relative_path,
        "camera_id": recording.camera_id,
        "part_index": recording.part_index,
        "state": recording.state.value,
        "start_utc_ns": recording.start_utc_ns,
        "end_utc_ns": recording.end_utc_ns,
        "duration_ns": recording.duration_ns,
        "size_bytes": recording.size_bytes,
        "end_reason": recording.end_reason,
        "spans": [
            {
                "first_sequence": span.first_sequence,
                "last_sequence": span.last_sequence,
                "start_utc_ns": span.start_utc_ns,
                "end_utc_ns": span.end_utc_ns,
                "timezone": span.timezone,
            }
            for span in recording.spans
        ],
    }


def _operation_json(operation: OperationSnapshot) -> dict[str, Any]:
    """Persistent operation journalをUIの状態polling形式に直す。"""
    base_revision = operation.previous_settings.get("revision")
    if operation.status != "succeeded":
        result = None
    elif operation.kind == "time":
        result = {
            "utc_ns": operation.target.get("utc_ns"),
            "timezone": operation.target.get("timezone"),
        }
    elif operation.kind.startswith("update"):
        result = {
            key: operation.target[key]
            for key in ("version", "scope")
            if key in operation.target
        }
    else:
        result = {"revision": base_revision + 1} if type(base_revision) is int else None
    error = (
        None
        if operation.error is None
        else operation.error.get("code", "operation_failed")
    )
    return {
        "operation_id": operation.operation_id,
        "state": operation.status,
        "stage": operation.stage.value,
        "result": result,
        "error": error,
        "recovery": operation.recovery,
    }


def _operation_in_progress(operation: OperationSnapshot) -> EngineResponse:
    return EngineResponse(
        409,
        {
            "error": {
                "code": "operation_in_progress",
                "message": "別の操作が完了してから再試行してください",
                "operation_id": operation.operation_id,
                "kind": operation.kind,
                "stage": operation.stage.value,
            }
        },
    )


def _local_datetime_to_utc_ns(value: str, timezone_name: str, fold: int) -> int:
    """OS timezoneの壁時計を検証し、重複時刻はfoldで一意に解決する。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
        raise ValueError("datetime must include seconds")
    if type(fold) is not int or fold not in (0, 1):
        raise ValueError("fold must be zero or one")
    naive = datetime.fromisoformat(value)
    if naive.tzinfo is not None:
        raise ValueError("datetime must be local wall time")
    zone = ZoneInfo(timezone_name)
    candidates = [naive.replace(tzinfo=zone, fold=value_fold) for value_fold in (0, 1)]
    round_trips = [
        candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
        for candidate in candidates
    ]
    if not any(round_trips):
        raise ValueError("nonexistent local time")
    ambiguous = (
        all(round_trips) and candidates[0].utcoffset() != candidates[1].utcoffset()
    )
    if not ambiguous and fold != 0:
        raise ValueError("fold is only valid for an ambiguous local time")
    chosen = candidates[fold if ambiguous else 0].astimezone(UTC)
    delta = chosen - datetime(1970, 1, 1, tzinfo=UTC)
    utc_ns = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    ) * 1000
    if utc_ns < 0:
        raise ValueError("pre-epoch wall time is not supported")
    return utc_ns


class _RuntimeCapacityReader:
    """statvfs前にselected UUIDと所有媒体を照合する。"""

    def __init__(self, runtime: RecorderRuntime) -> None:
        self._runtime = runtime
        self._locator = _RuntimeVolumeLocator(runtime)

    def refresh(self, volume_id: str) -> CapacitySnapshot:
        snapshot = next(
            (
                item
                for item in self._runtime.catalog.list_volumes(connected_only=True)
                if item.volume_id == volume_id and item.selected
            ),
            None,
        )
        if snapshot is None:
            raise RuntimeError("selected_volume_not_connected")
        volume, root = self._locator.resolve(volume_id)
        usage = os.statvfs(root)
        return CapacitySnapshot(
            volume_id=volume_id,
            persistent_id=volume.persistent_id,
            total_bytes=usage.f_blocks * usage.f_frsize,
            available_bytes=usage.f_bavail * usage.f_frsize,
        )


class _RuntimeLowSpaceBlocker:
    def __init__(self, runtime: RecorderRuntime) -> None:
        self._runtime = runtime

    def set_low_space_blocked(self, blocked: bool, reason: str | None = None) -> None:
        self._runtime.set_low_space_blocked(blocked, reason)


class _RuntimeVolumeLocator(ManagedVolumeLocator):
    """接続中UUIDと所有markerを毎回照合するmedia resolver。"""

    def __init__(self, runtime: RecorderRuntime) -> None:
        self._runtime = runtime

    def resolve(self, volume_id: str) -> tuple[MountedVolume, Path]:
        snapshot = next(
            (
                volume
                for volume in self._runtime.catalog.list_volumes(connected_only=True)
                if volume.volume_id == volume_id
            ),
            None,
        )
        if snapshot is None:
            raise FileNotFoundError("recording volume is disconnected")
        matches = [
            volume
            for volume in self._runtime.volume_scanner.scan()
            if volume.persistent_id == snapshot.persistent_id
        ]
        if len(matches) != 1:
            raise FileNotFoundError("recording volume is unavailable or ambiguous")
        volume = matches[0]
        root = self._runtime.volume_store.recordings_root(volume)
        self._runtime.volume_store.assert_owned(volume, root)
        return volume, root

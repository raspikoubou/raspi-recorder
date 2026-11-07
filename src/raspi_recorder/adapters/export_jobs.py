"""管理媒体上で一時ZIPを書き、録画read leaseを保つexport job。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from raspi_recorder.adapters.linux_storage import LinuxVolumeScanner, ManagedVolumeStore
from raspi_recorder.application.recording_access import (
    RecordingAccessError,
    RecordingAccessService,
    RecordingReadLease,
)
from raspi_recorder.ports.clock import Clock
from raspi_recorder.ports.storage import MountedVolume

_EXPORT_TTL_NS = 1_800_000_000_000
_EXPORT_MAX_ITEMS = 100
_EXPORT_MAX_RETAINED_JOBS = 8
_COPY_CHUNK_BYTES = 65_536
_ZIP_OVERHEAD_BYTES_PER_ITEM = 65_536


@dataclass(slots=True)
class _ExportJob:
    export_id: str
    state: str
    selected_ids: tuple[str, ...]
    total_bytes: int
    written_bytes: int
    created_mono_ns: int
    last_access_mono_ns: int
    volume: MountedVolume
    recordings_root: Path
    output_directory: Path
    temporary_path: Path
    final_path: Path
    leases: tuple[RecordingReadLease, ...]
    error: str | None = None
    active_downloads: int = 0


class ExportJobService:
    """明示選択したMP4だけを、ZIP64・無圧縮で媒体内へ書き出す。"""

    def __init__(
        self,
        access: RecordingAccessService,
        scanner: LinuxVolumeScanner,
        volume_store: ManagedVolumeStore,
        clock: Clock,
        selected_volume: Any,
        settings: Any,
    ) -> None:
        self._access = access
        self._scanner = scanner
        self._volume_store = volume_store
        self._clock = clock
        self._selected_volume = selected_volume
        self._settings = settings
        self._lock = threading.RLock()
        self._jobs: dict[str, _ExportJob] = {}
        self._reservations: dict[str, int] = {}
        self._pending_deletions: set[Path] = set()
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._recovered_volume_keys: set[tuple[str, int, str]] = set()

    def recover(self, volume: MountedVolume, recordings_root: Path) -> None:
        """engine restart後に残った一時ZIPだけを、所有媒体内で掃除する。"""
        key = (volume.persistent_id, volume.device_number, str(volume.mountpoint))
        with self._lock:
            if key in self._recovered_volume_keys:
                return
            output_directory = recordings_root / ".exports"
            if not output_directory.exists():
                self._recovered_volume_keys.add(key)
                return
            self._volume_store.assert_owned(volume, recordings_root)
            self._volume_store.assert_current(volume, recordings_root)
            if output_directory.is_symlink() or not output_directory.is_dir():
                raise RecordingAccessError("export_directory_invalid")
            for path in output_directory.iterdir():
                if (
                    re.fullmatch(r"export-[0-9a-f]{32}\.(zip|partial)", path.name)
                    is None
                ):
                    continue
                if path.is_symlink():
                    continue
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_dev == volume.device_number:
                    path.unlink()
            self._fsync_directory(output_directory)
            self._recovered_volume_keys.add(key)

    def create(self, recording_ids: object) -> dict[str, Any]:
        ids = self._validate_ids(recording_ids)
        self.expire()
        with self._lock:
            self._assert_job_capacity_locked()
        selected = self._selected_volume()
        if selected is None:
            raise RecordingAccessError("selected_volume_unavailable")
        volume, recordings_root, _volume_id = selected
        self._volume_store.assert_owned(volume, recordings_root)
        self._volume_store.assert_current(volume, recordings_root)
        leases: list[RecordingReadLease] = []
        temporary_path: Path | None = None
        export_id: str | None = None
        thread: threading.Thread | None = None
        try:
            for recording_id in ids:
                leases.append(self._access.read_lease(recording_id, purpose="export"))
            sizes: list[int] = []
            for lease in leases:
                lease.file.seek(0, os.SEEK_END)
                sizes.append(lease.file.tell())
                lease.file.seek(0)
            total = sum(sizes)
            reserved = total + _ZIP_OVERHEAD_BYTES_PER_ITEM * len(leases)
            settings = self._settings()
            thresholds = settings.validate_capacity(volume.filesystem_bytes)
            usage = os.statvfs(recordings_root)
            available = usage.f_bavail * usage.f_frsize
            with self._lock:
                self._assert_job_capacity_locked()
                other_reservations = sum(self._reservations.values())
                if available < reserved + other_reservations + thresholds.recover_bytes:
                    raise RecordingAccessError("export_space_insufficient")
                output_directory = recordings_root / ".exports"
                if output_directory.is_symlink():
                    raise RecordingAccessError("export_directory_invalid")
                output_directory.mkdir(mode=0o750, exist_ok=True)
                self._volume_store.assert_current(volume, recordings_root)
                directory_stat = output_directory.stat()
                if not stat.S_ISDIR(directory_stat.st_mode) or (
                    directory_stat.st_dev != volume.device_number
                ):
                    raise RecordingAccessError("export_directory_invalid")
                export_id = uuid.uuid4().hex
                temporary_path = output_directory / f"export-{export_id}.partial"
                final_path = output_directory / f"export-{export_id}.zip"
                descriptor = os.open(
                    temporary_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o640,
                )
                os.close(descriptor)
                now = self._clock.monotonic_ns()
                job = _ExportJob(
                    export_id=export_id,
                    state="queued",
                    selected_ids=ids,
                    total_bytes=total,
                    written_bytes=0,
                    created_mono_ns=now,
                    last_access_mono_ns=now,
                    volume=volume,
                    recordings_root=recordings_root,
                    output_directory=output_directory,
                    temporary_path=temporary_path,
                    final_path=final_path,
                    leases=tuple(leases),
                )
                self._jobs[export_id] = job
                self._reservations[export_id] = reserved
                thread = threading.Thread(
                    target=self._run,
                    args=(job, tuple(sizes)),
                    name=f"recorder-export-{export_id[:8]}",
                    daemon=True,
                )
                self._threads.add(thread)
                thread.start()
                return self._public(job)
        except Exception:
            with self._lock:
                if export_id is not None:
                    self._jobs.pop(export_id, None)
                    self._reservations.pop(export_id, None)
                if thread is not None:
                    self._threads.discard(thread)
            if temporary_path is not None:
                if not self._safe_unlink(temporary_path):
                    with self._lock:
                        self._pending_deletions.add(temporary_path)
            for lease in leases:
                lease.close()
            raise

    def _assert_job_capacity_locked(self) -> None:
        """ZIPは同時に1件、未失効jobは最大8件に制限する。"""
        if self._stop.is_set():
            raise RecordingAccessError("export_service_stopping")
        if any(job.state in {"queued", "running"} for job in self._jobs.values()):
            raise RecordingAccessError("export_worker_busy")
        if len(self._jobs) >= _EXPORT_MAX_RETAINED_JOBS:
            raise RecordingAccessError("export_job_limit")

    def get(self, export_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(export_id)
            if job is None:
                return None
            job.last_access_mono_ns = self._clock.monotonic_ns()
            return self._public(job)

    def open_download(self, export_id: str) -> tuple[BinaryIO, int, Callable[[], None]]:
        with self._lock:
            job = self._jobs.get(export_id)
            if job is None or job.state != "succeeded":
                raise RecordingAccessError("export_not_ready")
            volume = self._resolve_volume(job)
            self._volume_store.assert_owned(volume, job.recordings_root)
            self._volume_store.assert_current(volume, job.recordings_root)
            descriptor = os.open(
                job.final_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != volume.device_number:
                os.close(descriptor)
                raise RecordingAccessError("export_file_invalid")
            handle = os.fdopen(descriptor, "rb")
            job.active_downloads += 1
            job.last_access_mono_ns = self._clock.monotonic_ns()

        def close() -> None:
            handle.close()
            with self._lock:
                current = self._jobs.get(export_id)
                if current is None:
                    return
                current.active_downloads = max(0, current.active_downloads - 1)
                current.last_access_mono_ns = self._clock.monotonic_ns()
                if current.active_downloads == 0:
                    self._expire_locked(current, reason="download_completed")

        return handle, info.st_size, close

    def expire(self) -> None:
        now = self._clock.monotonic_ns()
        with self._lock:
            for path in tuple(self._pending_deletions):
                if self._safe_unlink(path):
                    self._pending_deletions.discard(path)
            for job in tuple(self._jobs.values()):
                if (
                    job.active_downloads == 0
                    and job.state in {"succeeded", "failed"}
                    and now - job.last_access_mono_ns >= _EXPORT_TTL_NS
                ):
                    self._expire_locked(job, reason="expired")

    def close(self, timeout_seconds: float = 5.0) -> None:
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        self._stop.set()
        with self._lock:
            threads = tuple(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = tuple(thread.name for thread in threads if thread.is_alive())
        if alive:
            raise TimeoutError("export_worker_stop_timeout: " + ", ".join(alive))

    def _run(self, job: _ExportJob, sizes: tuple[int, ...]) -> None:
        with self._lock:
            job.state = "running"
        try:
            flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(job.temporary_path, flags)
            with os.fdopen(descriptor, "wb") as output:
                with zipfile.ZipFile(
                    output,
                    mode="w",
                    compression=zipfile.ZIP_STORED,
                    allowZip64=True,
                ) as archive:
                    current_sizes = list(sizes)
                    copied_total = 0
                    for index, (lease, expected_size) in enumerate(
                        zip(job.leases, sizes, strict=True)
                    ):
                        if self._stop.is_set():
                            raise InterruptedError("export_engine_stopping")
                        current_sizes[index] = self._current_source_size(
                            lease, expected_size
                        )
                        self._ensure_space(job, sum(current_sizes) - copied_total)
                        basename = PurePosixPath(lease.recording.relative_path).name
                        recording_key = hashlib.sha256(
                            lease.recording.recording_id.encode("utf-8")
                        ).hexdigest()
                        volume_key = hashlib.sha256(
                            lease.recording.volume_id.encode("utf-8")
                        ).hexdigest()[:16]
                        entry_name = f"{volume_key}_{recording_key}_{basename}"
                        info = zipfile.ZipInfo(entry_name)
                        info.compress_type = zipfile.ZIP_STORED
                        with archive.open(info, "w", force_zip64=True) as entry:
                            copied = 0
                            while chunk := lease.file.read(_COPY_CHUNK_BYTES):
                                if self._stop.is_set():
                                    raise InterruptedError("export_engine_stopping")
                                entry.write(chunk)
                                copied += len(chunk)
                                copied_total += len(chunk)
                                with self._lock:
                                    job.written_bytes += len(chunk)
                                    self._reservations[job.export_id] = max(
                                        0,
                                        self._reservations.get(job.export_id, 0)
                                        - len(chunk),
                                    )
                                current_sizes[index] = self._current_source_size(
                                    lease, expected_size
                                )
                                self._ensure_space(
                                    job, sum(current_sizes) - copied_total
                                )
                            if copied != expected_size:
                                raise OSError("export_source_size_changed")
                output.flush()
                os.fsync(output.fileno())
            self._volume_store.assert_current(job.volume, job.recordings_root)
            os.replace(job.temporary_path, job.final_path)
            self._fsync_directory(job.output_directory)
            with self._lock:
                job.state = "succeeded"
                self._reservations.pop(job.export_id, None)
        except Exception as exc:
            with self._lock:
                job.state = "failed"
                job.error = type(exc).__name__
                self._reservations.pop(job.export_id, None)
            if not self._safe_unlink(job.temporary_path):
                with self._lock:
                    self._pending_deletions.add(job.temporary_path)
            if not self._safe_unlink(job.final_path):
                with self._lock:
                    self._pending_deletions.add(job.final_path)
        finally:
            for lease in job.leases:
                lease.close()
            with self._lock:
                self._threads.discard(threading.current_thread())

    @staticmethod
    def _current_source_size(lease: RecordingReadLease, fallback: int) -> int:
        """copy中のsource実sizeを返す。作成時sizeのまま見積もると、
        sourceが伸びた分だけstop_bytes reserveを食い潰してしまう。"""
        try:
            return os.fstat(lease.file.fileno()).st_size
        except (OSError, ValueError, AttributeError):
            return fallback

    def _ensure_space(self, job: _ExportJob, remaining_bytes: int) -> None:
        self._volume_store.assert_current(job.volume, job.recordings_root)
        usage = os.statvfs(job.recordings_root)
        available = usage.f_bavail * usage.f_frsize
        settings = self._settings()
        thresholds = settings.validate_capacity(job.volume.filesystem_bytes)
        with self._lock:
            other_reservations = sum(
                value
                for key, value in self._reservations.items()
                if key != job.export_id
            )
        if available < remaining_bytes + other_reservations + thresholds.stop_bytes:
            raise RecordingAccessError("export_yielded_to_recording_capacity")

    def _resolve_volume(self, job: _ExportJob) -> MountedVolume:
        matches = [
            volume
            for volume in self._scanner.scan()
            if volume.persistent_id == job.volume.persistent_id
        ]
        if len(matches) != 1:
            raise RecordingAccessError("export_volume_unavailable")
        return matches[0]

    def _expire_locked(self, job: _ExportJob, *, reason: str) -> None:
        if job.active_downloads:
            return
        if not self._safe_unlink(job.final_path):
            self._pending_deletions.add(job.final_path)
        if not self._safe_unlink(job.temporary_path):
            self._pending_deletions.add(job.temporary_path)
        job.state = "expired"
        job.error = reason
        self._jobs.pop(job.export_id, None)
        self._reservations.pop(job.export_id, None)

    def _safe_unlink(self, path: Path) -> bool:
        """削除できたらTrueを返す。既に無い・自前のfileでない場合も
        「処理済み」としてTrue。媒体が見つからない・所有検証が通らない等で
        消せなかった場合だけFalseを返し、呼び出し側が再試行対象として
        保持できるようにする(従来は失敗が無言で捨てられ孤児fileが残った)。"""
        try:
            volume = self._resolve_volume_for_path(path)
            if volume is None:
                return False
            output_directory = path.parent
            recordings_root = output_directory.parent
            self._volume_store.assert_owned(volume, recordings_root)
            self._volume_store.assert_current(volume, recordings_root)
            if path.is_symlink():
                return True
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_dev == volume.device_number:
                path.unlink()
                self._fsync_directory(output_directory)
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def _resolve_volume_for_path(self, path: Path) -> MountedVolume | None:
        for volume in self._scanner.scan():
            expected = (
                self._volume_store.recordings_root(volume) / ".exports" / path.name
            )
            if path == expected:
                return volume
        return None

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_ids(recording_ids: object) -> tuple[str, ...]:
        if (
            not isinstance(recording_ids, list)
            or not 1 <= len(recording_ids) <= _EXPORT_MAX_ITEMS
        ):
            raise ValueError("recording_idsは1～100件の配列が必要です")
        if not all(isinstance(value, str) and value for value in recording_ids):
            raise ValueError("recording_idsに不正なIDがあります")
        if len(set(recording_ids)) != len(recording_ids):
            raise ValueError("recording_idsの重複は許可されません")
        return tuple(recording_ids)

    @staticmethod
    def _public(job: _ExportJob) -> dict[str, Any]:
        return {
            "export_id": job.export_id,
            "state": job.state,
            "recording_count": len(job.selected_ids),
            "total_bytes": job.total_bytes,
            "written_bytes": job.written_bytes,
            "error": job.error,
            "download_ready": job.state == "succeeded",
        }

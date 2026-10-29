"""SQLite recording indexとPyAV file確定を1物理partへ束ねるadapter。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo

from raspi_recorder.adapters.bounded_progress import LatestValueWorker
from raspi_recorder.adapters.pyav_media import PyAvMp4Writer
from raspi_recorder.domain.camera import CameraSettings, RecordingMode
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.domain.recordings import RecordingArtifact, RecordingProgress
from raspi_recorder.ports.media import SegmentWriter
from raspi_recorder.ports.recording_index import (
    RecordingWriterIndex,
    RecordingWriterSession,
)
from raspi_recorder.ports.storage import MountedVolume, VolumeStore

_PROGRESS_INTERVAL_NS = 5_000_000_000


class ProgressReportingWriter(SegmentWriter, Protocol):
    """manifest経由の時刻span・sizeを返す物理writer。"""

    def progress(self) -> RecordingProgress | None:
        """現在書き込み済みframeのprogressを返す。"""
        ...


class Mp4WriterBuilder(Protocol):
    """単一MP4 writerの構築関数。"""

    def __call__(
        self,
        recordings_root: str | Path,
        file_name: str,
        settings: CameraSettings,
        *,
        camera_id: int,
        mode: str,
        logical_event_id: str | None,
        part_index: int,
        overlay_enabled: bool,
        recording_id: str,
        audio_track_enabled: bool,
        timeline_origin_mono_ns: int,
        generation: int,
    ) -> ProgressReportingWriter: ...


class CataloguedWriterFactory:
    """first saved frameで専用path・writing row・media writerを作る。"""

    def __init__(
        self,
        *,
        index: RecordingWriterIndex,
        volume_store: VolumeStore,
        volume: MountedVolume,
        recordings_root: Path,
        volume_id: str,
        settings: CameraSettings,
        camera_id: int,
        mode: RecordingMode,
        boot_id: str,
        writer_epoch: str,
        logical_event_id: str | None = None,
        overlay_enabled: bool | None = None,
        writer_builder: Mp4WriterBuilder = PyAvMp4Writer,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(mode, RecordingMode):
            raise ValueError("modeが不正です")
        if mode is RecordingMode.CONTINUOUS and logical_event_id is not None:
            raise ValueError("常時録画へlogical_event_idを指定できません")
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsが不正です")
        if not isinstance(volume_id, str) or not volume_id:
            raise ValueError("volume_idが必要です")
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("boot_idが必要です")
        if not isinstance(writer_epoch, str) or not writer_epoch:
            raise ValueError("writer_epochが必要です")
        self._index = index
        self._volume_store = volume_store
        self._volume = volume
        self._root = recordings_root
        self._volume_id = volume_id
        self._lock = RLock()
        self._settings = settings
        self._camera_id = camera_id
        self._mode = mode
        self._boot_id = boot_id
        self._writer_epoch = writer_epoch
        self._event_id = logical_event_id
        self._uses_settings_overlay = overlay_enabled is None
        self._overlay_enabled = (
            settings.overlay_enabled if overlay_enabled is None else overlay_enabled
        )
        if type(self._overlay_enabled) is not bool:
            raise ValueError("overlay_enabledはboolで指定してください")
        self._writer_builder = writer_builder
        self._identifier_factory = identifier_factory

    def update_settings(self, settings: CameraSettings) -> None:
        """以後に作る物理fileの設定を差し替える。既存writerには触れない。"""
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsが不正です")
        with self._lock:
            self._settings = settings
            if self._uses_settings_overlay:
                self._overlay_enabled = settings.overlay_enabled

    def __call__(self, part_index: int, first_frame: FrameEnvelope) -> SegmentWriter:
        """part先頭の撮影wall-timeで階層・名称を決める。"""
        if type(part_index) is not int or part_index < 1:
            raise ValueError("part_indexは1以上の整数で指定してください")
        if not isinstance(first_frame, FrameEnvelope):
            raise ValueError("first_frameはFrameEnvelopeが必要です")
        timezone = ZoneInfo(first_frame.timezone)
        timestamp = datetime.fromtimestamp(
            first_frame.capture_utc_ns // 1_000_000_000, tz=UTC
        ).astimezone(timezone)
        mode_name = self._mode.value
        directory_relative = f"{mode_name}/cam{self._camera_id}/{timestamp:%Y/%m/%d}"
        directory = self._volume_store.ensure_managed_directory(
            self._volume, self._root, directory_relative
        )
        prefix = f"{mode_name}_cam{self._camera_id}_{timestamp:%Y%m%d_%H%M%S}"
        file_name = self._available_name(directory, prefix)
        relative_path = f"{directory_relative}/{file_name}"
        recording_id = (
            uuid.uuid4().hex
            if self._identifier_factory is None
            else self._identifier_factory()
        )
        with self._lock:
            settings = self._settings
            overlay_enabled = self._overlay_enabled
        session = self._index.open_writer_session()
        try:
            session.create_recording(
                recording_id=recording_id,
                volume_id=self._volume_id,
                relative_path=relative_path,
                mode=self._mode,
                camera_id=self._camera_id,
                logical_event_id=self._event_id,
                part_index=part_index,
                boot_id=self._boot_id,
                writer_epoch=self._writer_epoch,
                created_utc_ns=first_frame.capture_utc_ns,
                settings_snapshot=self._settings_payload(
                    settings,
                    overlay_enabled,
                    audio_track_present=(
                        settings.audio_enabled and first_frame.audio_available
                    ),
                ),
            )
        except Exception:
            session.close()
            raise
        try:
            writer = self._writer_builder(
                directory,
                file_name,
                settings,
                camera_id=self._camera_id,
                mode=mode_name,
                logical_event_id=self._event_id,
                part_index=part_index,
                overlay_enabled=overlay_enabled,
                recording_id=recording_id,
                audio_track_enabled=(
                    settings.audio_enabled and first_frame.audio_available
                ),
                timeline_origin_mono_ns=min(
                    [first_frame.capture_mono_ns]
                    + [block.capture_mono_ns for block in first_frame.audio_blocks]
                ),
                generation=first_frame.generation,
            )
        except Exception:
            try:
                session.mark_recording_abnormal(recording_id, "writer_init_failed")
            finally:
                session.close()
            raise
        try:
            return CataloguedSegmentWriter(
                writer=writer,
                index=session,
                progress_index=self._index,
                recording_id=recording_id,
            )
        except Exception as error:
            # 組み立て失敗(worker thread起動失敗等)でも物理writerと
            # sessionを残さず、writing行をabnormalへ倒す。
            try:
                writer.abort("writer_init_failed")
            except Exception as abort_error:
                error.add_note(
                    "media writer cleanup also failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            try:
                session.mark_recording_abnormal(recording_id, "writer_init_failed")
            finally:
                session.close()
            raise

    @staticmethod
    def _available_name(directory: Path, prefix: str) -> str:
        for suffix in range(10_000):
            tail = "" if suffix == 0 else f"_{suffix:03d}"
            file_name = f"{prefix}{tail}.mp4"
            if any(
                candidate.exists() or candidate.is_symlink()
                for candidate in (
                    directory / file_name,
                    directory / f"{file_name}.partial",
                    directory / f"{file_name}.recording.json",
                )
            ):
                continue
            return file_name
        raise FileExistsError("同秒録画名の衝突が上限へ達しました")

    @staticmethod
    def _settings_payload(
        settings: CameraSettings,
        overlay_enabled: bool,
        *,
        audio_track_present: bool,
    ) -> dict[str, object]:
        return {
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "quality": settings.quality.value,
            "audio_enabled": settings.audio_enabled,
            "audio_track_present": audio_track_present,
            "overlay_enabled": overlay_enabled,
            "overlay_corner": settings.overlay_corner.value,
            "overlay_size": settings.overlay_size.value,
            "usb_port_key": settings.usb_port_key,
            "video_interface_key": settings.video_interface_key,
            "audio_interface_key": settings.audio_interface_key,
        }


class CataloguedSegmentWriter(SegmentWriter):
    """DB progressと正常/異常終端を物理writerへ同期する。"""

    def __init__(
        self,
        *,
        writer: ProgressReportingWriter,
        index: RecordingWriterSession,
        progress_index: RecordingWriterIndex,
        recording_id: str,
    ) -> None:
        self._writer = writer
        self._index = index
        self._recording_id = recording_id
        self._last_progress_mono_ns: int | None = None
        self._terminal = False
        self._progress_worker_closed = False
        progress_session: RecordingWriterSession | None = None

        def persist_progress(progress: RecordingProgress) -> None:
            nonlocal progress_session
            if progress_session is None:
                progress_session = progress_index.open_writer_session()
            progress_session.update_recording_progress(
                self._recording_id,
                start_mono_ns=progress.start_mono_ns,
                end_mono_ns=progress.end_mono_ns,
                start_utc_ns=progress.start_utc_ns,
                end_utc_ns=progress.end_utc_ns,
                size_bytes=progress.size_bytes,
                spans=progress.spans,
            )

        def close_progress_session() -> None:
            if progress_session is not None:
                progress_session.close()

        self._progress_worker = LatestValueWorker(
            persist_progress,
            name=f"recording-progress-{recording_id[:32]}",
            close_consumer=close_progress_session,
        )

    def write(self, envelope: FrameEnvelope) -> None:
        if self._terminal:
            raise RuntimeError("終了済みMP4 writerにはframeを書き込めません")
        self._writer.write(envelope)
        if (
            self._last_progress_mono_ns is not None
            and envelope.capture_mono_ns - self._last_progress_mono_ns
            < _PROGRESS_INTERVAL_NS
        ):
            return
        progress = self._writer.progress()
        if progress is not None:
            self._progress_worker.submit(progress)
            self._last_progress_mono_ns = envelope.capture_mono_ns

    def write_audio(self, envelope: AudioEnvelope) -> None:
        if self._terminal:
            raise RuntimeError("終了済みMP4 writerにはaudioを書き込めません")
        self._writer.write_audio(envelope)

    def finalize(self) -> RecordingArtifact:
        artifact: RecordingArtifact | None = None
        abnormal_reason = "writer_or_index_finalize_failed"
        failure_stage = "progress_worker"
        try:
            self._close_progress_worker()
            failure_stage = "media_writer"
            artifact = self._writer.finalize()
            failure_stage = "final_progress"
            progress = self._writer.progress()
            if progress is None:
                abnormal_reason = "final_progress_missing"
                raise RuntimeError("確定後のmanifest progressがありません")
            failure_stage = "recording_index"
            self._index.finalize_recording(
                self._recording_id,
                start_mono_ns=artifact.first_mono_ns,
                end_mono_ns=artifact.last_mono_ns,
                start_utc_ns=artifact.first_utc_ns,
                end_utc_ns=artifact.last_utc_ns,
                size_bytes=artifact.size_bytes,
                end_reason=None,
                spans=progress.spans,
            )
        except Exception as error:
            if abnormal_reason == "writer_or_index_finalize_failed":
                # Keep only exception class names, not messages that can
                # contain local paths or other private information.
                detail = type(error).__name__
                if error.__cause__ is not None:
                    detail += f":{type(error.__cause__).__name__}"
                abnormal_reason = f"{abnormal_reason}:{failure_stage}:{detail}"[:120]
            try:
                self._writer.abort(abnormal_reason)
            except Exception as abort_error:
                error.add_note(
                    "media writer cleanup also failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            try:
                self._mark_abnormal(
                    abnormal_reason,
                    None if artifact is None else artifact.size_bytes,
                )
            except Exception as mark_error:
                error.add_note(
                    "recordingをabnormalへ更新できませんでした: "
                    f"{type(mark_error).__name__}: {mark_error}"
                )
            raise
        assert artifact is not None
        self._terminal = True
        self._index.close()
        return artifact

    def abort(self, reason: str) -> None:
        if self._terminal:
            return
        worker_error: Exception | None = None
        try:
            self._close_progress_worker()
        except Exception as exc:
            worker_error = exc
        writer_error: Exception | None = None
        try:
            self._writer.abort(reason)
        except Exception as exc:
            writer_error = exc
        try:
            self._mark_abnormal(reason)
        except Exception as index_error:
            if worker_error is not None:
                index_error.add_note(
                    "progress persistence also failed: "
                    f"{type(worker_error).__name__}: {worker_error}"
                )
            raise
        if worker_error is not None:
            if writer_error is not None:
                worker_error.add_note(
                    "media writer abort also failed: "
                    f"{type(writer_error).__name__}: {writer_error}"
                )
            raise worker_error
        if writer_error is not None:
            raise writer_error

    def _close_progress_worker(self) -> None:
        if self._progress_worker_closed:
            return
        self._progress_worker_closed = True
        self._progress_worker.close()

    def _mark_abnormal(self, reason: str, size_bytes: int | None = None) -> None:
        try:
            progress = self._writer.progress()
            self._index.mark_recording_abnormal(
                self._recording_id,
                reason[:120] if reason else "unspecified",
                end_mono_ns=None if progress is None else progress.end_mono_ns,
                end_utc_ns=None if progress is None else progress.end_utc_ns,
                size_bytes=(
                    size_bytes
                    if size_bytes is not None
                    else None
                    if progress is None
                    else progress.size_bytes
                ),
            )
        finally:
            self._terminal = True
            self._index.close()

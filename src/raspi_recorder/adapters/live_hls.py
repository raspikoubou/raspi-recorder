"""録画入力を複製せず、1 cameraにつき1本のbounded HLS previewを生成する。"""

from __future__ import annotations

import importlib
import time
from collections import deque
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from threading import Condition, Thread
from typing import Any

from raspi_recorder.adapters.audio_encoding import AacEncoder
from raspi_recorder.adapters.timestamp_overlay import draw_timestamp_overlay
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.ports.clock import Clock

_MAX_BROWSER_SAFE_GENERATION = 9_007_199_254_740_991
_MAX_QUEUE_FRAMES = 20
_MAX_QUEUE_BYTES = 8 * 1024 * 1024
_MAX_AUDIO_QUEUE_BYTES = 2 * 1024 * 1024
_OVERLOAD_RECORDING_DELAY_NS = 1_500_000_000
_OVERLOAD_QUEUE_FRAMES = 15
_OVERLOAD_QUEUE_BYTES = 6 * 1024 * 1024
_OVERLOAD_DWELL_NS = 5_000_000_000
_RECORDING_PRIORITY_DWELL_NS = 1_000_000_000
_QUALITY_RECOVERY_DWELL_NS = 30_000_000_000
_RETIRED_GENERATION_TTL_SECONDS = 30.0
_MAX_RETIRED_GENERATIONS = 8
_VIEWER_DEMAND_TTL_NS = 10_000_000_000


_LIVE_QUALITIES = (
    (640, 360, 10, "640x360@10"),
    (320, 180, 5, "320x180@5"),
    (320, 180, 2, "320x180@2"),
)


def _capture_time_to_frame_pts(
    capture_mono_ns: int,
    timeline_origin_mono_ns: int,
    fps: int,
) -> int:
    """Convert a monotonic timestamp with integer floor to avoid duplicate PTS."""
    return (capture_mono_ns - timeline_origin_mono_ns) * fps // 1_000_000_000


class LiveHlsPublisher:
    """短いlive windowをtmpfsへ書く共有publisher。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        generation: int,
        settings: CameraSettings | None = None,
        bitrate: int = 500_000,
        clock: Clock | None = None,
        recording_active: bool = False,
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("generationは1以上の整数が必要です")
        if type(bitrate) is not int or not 100_000 <= bitrate <= 2_000_000:
            raise ValueError("live bitrateは100kbps～2Mbpsの範囲が必要です")
        self.directory = Path(directory)
        self._camera_generation = generation
        self._base_directory = self.directory.parent
        self._generation = self._next_generation_id(0)
        self.directory = self._base_directory / f"generation-{self._generation}"
        self._settings = CameraSettings() if settings is None else settings
        if not isinstance(self._settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        if type(recording_active) is not bool:
            raise ValueError("recording_activeはboolで指定してください")
        self._recording_active = recording_active
        self._bitrate = bitrate
        self._clock = clock
        self._condition = Condition()
        self._queue: deque[FrameEnvelope] = deque()
        self._audio_queue: deque[AudioEnvelope] = deque()
        self._queued_bytes = 0
        self._audio_queued_bytes = 0
        self._dropped = 0
        self._audio_dropped = 0
        self._audio_encoded_blocks = 0
        self._accepted = 0
        self._sample_origin_mono_ns: int | None = None
        self._last_submitted_sample_slot: int | None = None
        self._sample_slots: dict[int, int] = {}
        initial_quality_index = 1 if recording_active else 0
        self._quality_index = initial_quality_index
        self._requested_quality_index = initial_quality_index
        self._recording_delay_ns: int | None = None
        self._last_encode_duration_ns = 0
        self._overload_since_mono_ns: int | None = None
        self._healthy_since_mono_ns: int | None = None
        self._paused = False
        self._pause_requested = False
        self._viewer_demand_active = False
        self._viewer_demand_expires_ns = 0
        self._viewer_pause_requested = False
        self._reason: str | None = None
        self._active_audio_enabled: bool | None = None
        self._retired_directories: dict[int, tuple[Path, float]] = {}
        self._has_rotated_generation = False
        self._stop = False
        self._started = False
        self._error: str | None = None
        self._thread: Thread | None = None

    @property
    def status(self) -> dict[str, Any]:
        with self._condition:
            self._expire_viewer_demand_locked(self._monotonic_ns())
            width, height, fps, quality_name = _LIVE_QUALITIES[
                self._requested_quality_index
            ]
            return {
                "available": (
                    self._playlist.is_file()
                    and self._error is None
                    and self._viewer_demand_active
                    and not self._paused
                    and not self._pause_requested
                    and not self._viewer_pause_requested
                ),
                "playlist": self._playlist.name,
                "generation": self._generation,
                "quality": quality_name,
                "width": width,
                "height": height,
                "fps": fps,
                "quality_change_pending": (
                    self._quality_index != self._requested_quality_index
                ),
                "paused": (
                    self._paused
                    or self._pause_requested
                    or self._viewer_pause_requested
                    or not self._viewer_demand_active
                ),
                "reason": self._reason
                or (None if self._viewer_demand_active else "no_viewers"),
                "viewer_demand": self._viewer_demand_active,
                "recording_delay_ns": self._recording_delay_ns,
                "accepted_frames": self._accepted,
                "dropped_frames": self._dropped,
                "queued_bytes": self._queued_bytes,
                "audio_enabled": self._settings.audio_enabled,
                "audio_queued_bytes": self._audio_queued_bytes,
                "dropped_audio_blocks": self._audio_dropped,
                "encoded_audio_blocks": self._audio_encoded_blocks,
                "error": self._error,
            }

    @property
    def viewer_demand_active(self) -> bool:
        """現在、有効な視聴要求があるかを返す。"""
        with self._condition:
            self._expire_viewer_demand_locked(self._monotonic_ns())
            return self._viewer_demand_active

    def request_viewer(self) -> bool:
        """カメラ単位の視聴需要を10秒延長する。視聴者一覧は保持しない。"""
        with self._condition:
            if not self._started or self._stop or self._error is not None:
                return False
            now_ns = self._monotonic_ns()
            self._viewer_demand_active = True
            self._viewer_demand_expires_ns = now_ns + _VIEWER_DEMAND_TTL_NS
            self._condition.notify_all()
            return True

    def release_viewer(self) -> None:
        """明示live/audio画面を離れたときqueueを解放する。"""
        with self._condition:
            self._viewer_demand_active = False
            self._viewer_demand_expires_ns = 0
            self._viewer_pause_requested = True
            self._queue.clear()
            self._queued_bytes = 0
            self._sample_slots.clear()
            self._audio_dropped += len(self._audio_queue)
            self._audio_queue.clear()
            self._audio_queued_bytes = 0
            self._condition.notify_all()

    @property
    def generation(self) -> int:
        """現在のHLS出力世代。camera worker generationとは独立している。"""
        with self._condition:
            return self._generation

    @property
    def _playlist(self) -> Path:
        return self.directory / "index.m3u8"

    def directory_for_generation(self, generation: int) -> Path | None:
        """現在または短い猶予時間内のplaylist directoryを返す。"""
        if type(generation) is not int or generation < 1:
            return None
        with self._condition:
            if generation == self._generation:
                return self.directory
            retired = self._retired_directories.get(generation)
            if retired is None or retired[1] <= time.monotonic():
                return None
            return retired[0]

    def start(self) -> None:
        if self._started:
            raise RuntimeError("live publisherは既に起動しています")
        if self._base_directory.is_symlink():
            raise ValueError("live parent directoryにsymlinkは使用できません")
        self._base_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        while self.directory.exists() or self.directory.is_symlink():
            self._generation = self._next_generation_id(self._generation)
            self.directory = self._base_directory / f"generation-{self._generation}"
        self.directory.mkdir(mode=0o750)
        self._stop = False
        self._started = True
        self._thread = Thread(
            target=self._run,
            name=f"recorder-live-hls-{self.generation}",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        frame: FrameEnvelope,
        *,
        recording_delay_ns: int | None = None,
    ) -> bool:
        """captureを待たせずframeをenqueueし、過負荷時は古いpreviewを落とす。"""
        if not isinstance(frame, FrameEnvelope):
            raise ValueError("frameはFrameEnvelopeが必要です")
        if recording_delay_ns is not None and (
            type(recording_delay_ns) is not int or recording_delay_ns < 0
        ):
            raise ValueError("recording_delay_nsは0以上の整数またはNoneが必要です")
        with self._condition:
            if not self._started or self._stop or self._error is not None:
                return False
            if frame.generation != self._camera_generation:
                return False
            self._expire_viewer_demand_locked(self._monotonic_ns())
            if not self._viewer_demand_active:
                return False
            self._recording_delay_ns = recording_delay_ns
            self._update_adaptive_quality_locked(recording_delay_ns)
            if self._settings.audio_enabled:
                for block in frame.audio_blocks:
                    if block.generation != self._camera_generation:
                        raise ValueError(
                            "audio generationがlive generationと異なります"
                        )
                    self._audio_queue.append(block)
                    self._audio_queued_bytes += block.size_bytes
                    while (
                        self._audio_queue
                        and self._audio_queued_bytes > _MAX_AUDIO_QUEUE_BYTES
                    ):
                        removed_audio = self._audio_queue.popleft()
                        self._audio_queued_bytes -= removed_audio.size_bytes
                        self._audio_dropped += 1
            if self._paused or self._pause_requested:
                self._audio_dropped += len(self._audio_queue)
                self._audio_queue.clear()
                self._audio_queued_bytes = 0
                return False
            _width, _height, fps, _name = _LIVE_QUALITIES[self._requested_quality_index]
            if self._sample_origin_mono_ns is None:
                self._sample_origin_mono_ns = frame.capture_mono_ns
            elapsed_ns = frame.capture_mono_ns - self._sample_origin_mono_ns
            if elapsed_ns < 0:
                self._dropped += 1
                return False
            sample_slot = (elapsed_ns * fps + 499_999_999) // 1_000_000_000
            if (
                self._last_submitted_sample_slot is not None
                and sample_slot <= self._last_submitted_sample_slot
            ):
                return False
            self._last_submitted_sample_slot = sample_slot
            preview_frame = replace(frame, audio_blocks=())
            if preview_frame.size_bytes > _MAX_QUEUE_BYTES:
                self._dropped += 1
                return False
            while self._queue and (
                len(self._queue) >= _MAX_QUEUE_FRAMES
                or self._queued_bytes + preview_frame.size_bytes > _MAX_QUEUE_BYTES
            ):
                removed = self._queue.popleft()
                self._queued_bytes -= removed.size_bytes
                self._sample_slots.pop(removed.sequence, None)
                self._dropped += 1
            self._queue.append(preview_frame)
            self._queued_bytes += preview_frame.size_bytes
            self._sample_slots[preview_frame.sequence] = sample_slot
            self._condition.notify()
            return True

    def update_settings(self, settings: CameraSettings) -> None:
        """更新後に生成するpreview frameへoverlay設定を適用する。"""
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        with self._condition:
            audio_changed = self._settings.audio_enabled != settings.audio_enabled
            self._settings = settings
            if audio_changed:
                if not settings.audio_enabled:
                    self._audio_dropped += len(self._audio_queue)
                    self._audio_queue.clear()
                    self._audio_queued_bytes = 0
                self._condition.notify_all()

    def close(self, timeout_seconds: float = 10.0) -> None:
        if not self._started:
            return
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                with self._condition:
                    self._queue.clear()
                    self._queued_bytes = 0
                    self._sample_slots.clear()
                    self._audio_queue.clear()
                    self._audio_queued_bytes = 0
                    self._condition.notify_all()
                self._thread.join(max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise TimeoutError("live_worker_stop_timeout")
        self._started = False
        with self._condition:
            self._queue.clear()
            self._queued_bytes = 0
            self._sample_slots.clear()
            self._audio_queue.clear()
            self._audio_queued_bytes = 0
            directories = [self.directory]
            directories.extend(
                path for path, _expires in self._retired_directories.values()
            )
            self._retired_directories.clear()
        for directory in directories:
            self._remove_directory(directory)

    def _run(self) -> None:
        av: Any | None = None
        container: Any | None = None
        stream: Any | None = None
        audio_encoder: AacEncoder | None = None
        item: tuple[FrameEnvelope, tuple[AudioEnvelope, ...], int] | None = None
        envelope: FrameEnvelope | None = None
        audio_blocks: tuple[AudioEnvelope, ...] = ()
        block: AudioEnvelope | None = None
        canvas: Any | None = None
        frame: Any | None = None
        packet: Any | None = None
        active_quality: int | None = None
        active_audio: bool | None = None
        first_video = True
        timeline_origin_mono_ns: int | None = None
        first_video_sample_slot: int | None = None
        first_video_pts: int | None = None
        needs_new_generation = False
        recovering = False
        try:
            av = importlib.import_module("av")
            while True:
                with self._condition:
                    while not self._stop:
                        self._expire_viewer_demand_locked(self._monotonic_ns())
                        switching = container is not None and (
                            active_quality != self._requested_quality_index
                            or active_audio != self._settings.audio_enabled
                        )
                        if (
                            self._viewer_pause_requested
                            or self._pause_requested
                            or switching
                            or (
                                self._viewer_demand_active
                                and not self._paused
                                and (container is None or self._queue)
                            )
                        ):
                            break
                        self._condition.wait(0.5)
                    if self._stop:
                        break
                    pause_requested = self._pause_requested
                    viewer_pause_requested = self._viewer_pause_requested
                    paused = self._paused
                    requested_quality = self._requested_quality_index
                    audio_enabled = self._settings.audio_enabled
                    switching = container is not None and (
                        active_quality != requested_quality
                        or active_audio != audio_enabled
                    )

                self._cleanup_retired_directories()
                if viewer_pause_requested:
                    with self._condition:
                        self._viewer_pause_requested = False
                    if container is not None:
                        previous_directory = self.directory
                        self._finish_output(
                            container,
                            stream,
                            audio_encoder,
                            first_video=first_video,
                        )
                        container = None
                        stream = None
                        audio_encoder = None
                        active_quality = None
                        active_audio = None
                        needs_new_generation = True
                        self._remove_directory(previous_directory)
                    continue
                if pause_requested or paused:
                    if container is not None:
                        closing_container = container
                        container = None
                        self._finish_output(
                            closing_container,
                            stream,
                            audio_encoder,
                            first_video=first_video,
                        )
                        container = None
                        stream = None
                        audio_encoder = None
                        active_quality = None
                        active_audio = None
                        needs_new_generation = True
                    if pause_requested:
                        with self._condition:
                            self._pause_requested = False
                            self._paused = True
                            self._condition.notify_all()
                    continue

                if switching:
                    assert container is not None
                    closing_container = container
                    container = None
                    self._finish_output(
                        closing_container,
                        stream,
                        audio_encoder,
                        first_video=first_video,
                    )
                    container = None
                    stream = None
                    audio_encoder = None
                    active_quality = None
                    active_audio = None
                    needs_new_generation = True
                    continue

                if container is None:
                    if self._paused:
                        continue
                    if needs_new_generation:
                        self._rotate_generation(requested_quality)
                        needs_new_generation = False
                    with self._condition:
                        requested_quality = self._requested_quality_index
                        audio_enabled = self._settings.audio_enabled
                        output_path = self._playlist
                        output_directory = self.directory
                        has_previous_generation = self._has_rotated_generation
                    container, stream = self._open_output(
                        av,
                        output_path,
                        output_directory,
                        requested_quality,
                        discontinuity=has_previous_generation,
                    )
                    active_quality = requested_quality
                    active_audio = audio_enabled
                    first_video = True
                    timeline_origin_mono_ns = None
                    audio_encoder = None
                    with self._condition:
                        self._quality_index = requested_quality
                        self._active_audio_enabled = audio_enabled
                        if recovering:
                            # submit()/request_viewer()はerror中は受け付けない
                            # ため、代替出力を開けた時点でerrorを解除しないと
                            # 次のframeが入らず回復できない。
                            self._error = None
                        self._condition.notify_all()
                    recovering = False
                    continue

                item = self._take_next_frame(active_quality, active_audio)
                if item is None:
                    continue
                try:
                    envelope, audio_blocks, sample_slot = item
                    encode_started = time.monotonic_ns()
                    if first_video:
                        timeline_origin_mono_ns = min(
                            (
                                envelope.capture_mono_ns,
                                *(block.capture_mono_ns for block in audio_blocks),
                            )
                        )
                        if active_audio:
                            audio_encoder = AacEncoder(
                                container,
                                av,
                                timeline_origin_mono_ns=timeline_origin_mono_ns,
                            )
                        assert active_quality is not None
                        first_video_sample_slot = sample_slot
                        first_video_pts = _capture_time_to_frame_pts(
                            envelope.capture_mono_ns,
                            timeline_origin_mono_ns,
                            _LIVE_QUALITIES[active_quality][2],
                        )
                    if audio_encoder is not None:
                        for block in audio_blocks:
                            for packet in audio_encoder.encode(block):
                                container.mux(packet)
                            with self._condition:
                                self._audio_encoded_blocks += 1
                    assert active_quality is not None
                    canvas = self._render_preview(envelope, active_quality)
                    frame = av.VideoFrame.from_image(canvas)
                    frame = frame.reformat(
                        format="yuv420p",
                        src_color_range="JPEG",
                        dst_color_range="MPEG",
                    )
                    frame.color_range = 1
                    if first_video:
                        frame.pict_type = av.video.frame.PictureType.I
                    assert timeline_origin_mono_ns is not None
                    assert first_video_sample_slot is not None
                    assert first_video_pts is not None
                    fps = _LIVE_QUALITIES[active_quality][2]
                    frame.pts = first_video_pts + sample_slot - first_video_sample_slot
                    frame.time_base = Fraction(1, fps)
                    assert stream is not None
                    for packet in stream.encode(frame):
                        container.mux(packet)
                    first_video = False
                    with self._condition:
                        self._last_encode_duration_ns = max(
                            0, time.monotonic_ns() - encode_started
                        )
                        if self._error is not None:
                            self._error = None
                    # _take_next_frame() may wait for a long time after this encode.
                    # Do not let its worker locals pin the last JPEG, PCM block,
                    # preview image, or PyAV frame while live viewing is idle.
                    item = None
                    envelope = None
                    audio_blocks = ()
                    block = None
                    canvas = None
                    frame = None
                    packet = None
                except Exception as exc:
                    # 1 frame分のencode失敗でlive全体を止めない。壊れた出力を
                    # 閉じて次のframeから新しいgenerationで再開し、成功した
                    # 時点でerror表示を消す。
                    with self._condition:
                        self._error = type(exc).__name__
                    recovering = True
                    if container is not None:
                        try:
                            self._finish_output(
                                container,
                                stream,
                                audio_encoder,
                                first_video=first_video,
                            )
                        except Exception:
                            pass
                        container = None
                        stream = None
                        audio_encoder = None
                        active_quality = None
                        active_audio = None
                        needs_new_generation = True
                    first_video = True
                    timeline_origin_mono_ns = None
                    item = None
                    envelope = None
                    audio_blocks = ()
                    block = None
                    canvas = None
                    frame = None
                    packet = None
                    continue
        except Exception as exc:
            with self._condition:
                self._error = type(exc).__name__
                self._reason = "live_encoder_failed"
                self._queue.clear()
                self._queued_bytes = 0
                self._sample_slots.clear()
                self._audio_queue.clear()
                self._audio_queued_bytes = 0
        finally:
            if container is not None:
                try:
                    self._finish_output(
                        container,
                        stream,
                        audio_encoder,
                        first_video=first_video,
                    )
                except Exception as exc:
                    with self._condition:
                        self._error = type(exc).__name__
                        self._reason = "live_encoder_close_failed"

    def _open_output(
        self,
        av: Any,
        playlist: Path,
        directory: Path,
        quality_index: int,
        *,
        discontinuity: bool,
    ) -> tuple[Any, Any]:
        width, height, fps, _name = _LIVE_QUALITIES[quality_index]
        flags = "delete_segments+independent_segments+omit_endlist"
        if discontinuity:
            flags += "+discont_start"
        container = av.open(
            str(playlist),
            mode="w",
            format="hls",
            options={
                "hls_time": "1",
                "hls_list_size": "5",
                "hls_delete_threshold": "6",
                "hls_flags": flags,
                "hls_segment_filename": str(directory / "segment%06d.ts"),
            },
        )
        try:
            stream = container.add_stream("libx264", rate=fps)
            stream.width = width
            stream.height = height
            stream.pix_fmt = "yuv420p"
            stream.time_base = Fraction(1, fps)
            stream.bit_rate = max(
                100_000,
                self._bitrate * width * height * fps // (640 * 360 * 10),
            )
            context = stream.codec_context
            context.color_range = 1
            context.time_base = Fraction(1, fps)
            context.max_b_frames = 0
            context.gop_size = fps
            context.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "x264-params": (f"keyint={fps}:min-keyint={fps}:scenecut=0:open-gop=0"),
            }
            return container, stream
        except Exception:
            container.close()
            raise

    @staticmethod
    def _finish_output(
        container: Any,
        stream: Any | None,
        audio_encoder: AacEncoder | None,
        *,
        first_video: bool,
    ) -> None:
        try:
            if not first_video and stream is not None:
                for packet in stream.encode(None):
                    container.mux(packet)
            if audio_encoder is not None:
                for packet in audio_encoder.flush():
                    container.mux(packet)
        finally:
            container.close()

    def _take_next_frame(
        self,
        quality_index: int | None,
        audio_enabled: bool | None,
    ) -> tuple[FrameEnvelope, tuple[AudioEnvelope, ...], int] | None:
        with self._condition:
            while (
                not self._queue
                and not self._stop
                and not self._pause_requested
                and not self._paused
                and not self._viewer_pause_requested
                and quality_index == self._requested_quality_index
                and audio_enabled == self._settings.audio_enabled
            ):
                self._expire_viewer_demand_locked(self._monotonic_ns())
                if self._viewer_pause_requested:
                    break
                self._condition.wait(0.5)
            if (
                self._stop
                or self._pause_requested
                or self._paused
                or self._viewer_pause_requested
                or not self._viewer_demand_active
                or quality_index != self._requested_quality_index
                or audio_enabled != self._settings.audio_enabled
                or not self._queue
            ):
                return None
            frame = self._queue.popleft()
            self._queued_bytes -= frame.size_bytes
            sample_slot = self._sample_slots.pop(frame.sequence)
            audio_blocks: list[AudioEnvelope] = []
            while (
                self._audio_queue
                and self._audio_queue[0].end_mono_ns <= frame.capture_mono_ns
            ):
                block = self._audio_queue.popleft()
                self._audio_queued_bytes -= block.size_bytes
                audio_blocks.append(block)
            self._accepted += 1
            return frame, tuple(audio_blocks), sample_slot

    def _rotate_generation(self, quality_index: int) -> None:
        self._base_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        old_generation = self._generation
        old_directory = self.directory
        generation = self._next_generation_id(old_generation)
        directory = self._base_directory / f"generation-{generation}"
        while directory.exists() or directory.is_symlink():
            generation = self._next_generation_id(generation)
            directory = self._base_directory / f"generation-{generation}"
        directory.mkdir(mode=0o750)
        evicted_directories: list[Path] = []
        with self._condition:
            self._retired_directories[old_generation] = (
                old_directory,
                time.monotonic() + _RETIRED_GENERATION_TTL_SECONDS,
            )
            self._has_rotated_generation = True
            self._generation = generation
            self.directory = directory
            self._quality_index = quality_index
            while len(self._retired_directories) > _MAX_RETIRED_GENERATIONS:
                oldest_generation = next(iter(self._retired_directories))
                retired, _expires = self._retired_directories.pop(oldest_generation)
                evicted_directories.append(retired)
            self._condition.notify_all()
        for retired in evicted_directories:
            self._remove_directory(retired)

    def _cleanup_retired_directories(self) -> None:
        now = time.monotonic()
        with self._condition:
            expired = tuple(
                (generation, path)
                for generation, (path, expires) in self._retired_directories.items()
                if expires <= now
            )
            for generation, _path in expired:
                self._retired_directories.pop(generation, None)
        for _generation, path in expired:
            self._remove_directory(path)

    def _remove_directory(self, directory: Path) -> None:
        if directory.parent != self._base_directory or directory.is_symlink():
            return
        try:
            for path in directory.iterdir():
                if path.is_symlink():
                    continue
                if path.is_file():
                    path.unlink()
            directory.rmdir()
        except OSError:
            return

    def _next_generation_id(self, previous: int) -> int:
        generation = max(time.monotonic_ns() // 1_000_000, previous + 1, 1)
        if generation > _MAX_BROWSER_SAFE_GENERATION:
            raise OverflowError("live generation exceeds browser-safe integer range")
        return generation

    def _expire_viewer_demand_locked(self, now_ns: int) -> None:
        """需要下線時だけpreview/audio待ち行列を解放する。condition lock必須。"""
        if not self._viewer_demand_active or now_ns < self._viewer_demand_expires_ns:
            return
        self._viewer_demand_active = False
        self._viewer_pause_requested = True
        self._queue.clear()
        self._queued_bytes = 0
        self._sample_slots.clear()
        self._audio_dropped += len(self._audio_queue)
        self._audio_queue.clear()
        self._audio_queued_bytes = 0
        self._condition.notify_all()

    def _monotonic_ns(self) -> int:
        if self._clock is None:
            return time.monotonic_ns()
        return self._clock.monotonic_ns()

    def _update_adaptive_quality_locked(self, recording_delay_ns: int | None) -> None:
        now = self._monotonic_ns()
        recording_overloaded = (
            recording_delay_ns is not None
            and recording_delay_ns >= _OVERLOAD_RECORDING_DELAY_NS
        )
        if self._pause_requested:
            return
        if self._paused:
            if recording_overloaded:
                self._healthy_since_mono_ns = None
            elif self._healthy_since_mono_ns is None:
                self._healthy_since_mono_ns = now
            elif now - self._healthy_since_mono_ns >= _QUALITY_RECOVERY_DWELL_NS:
                self._paused = False
                self._requested_quality_index = len(_LIVE_QUALITIES) - 1
                self._reason = None
                self._healthy_since_mono_ns = now
                self._overload_since_mono_ns = None
                self._condition.notify_all()
            return

        _width, _height, fps, _name = _LIVE_QUALITIES[self._requested_quality_index]
        interval_ns = 1_000_000_000 // fps
        encode_overloaded = self._last_encode_duration_ns > interval_ns * 3 // 2
        queue_overloaded = (
            len(self._queue) >= _OVERLOAD_QUEUE_FRAMES
            or self._queued_bytes >= _OVERLOAD_QUEUE_BYTES
        )
        overloaded = recording_overloaded or encode_overloaded or queue_overloaded
        if overloaded:
            self._healthy_since_mono_ns = None
            if self._overload_since_mono_ns is None:
                self._overload_since_mono_ns = now
                return
            overload_dwell_ns = (
                _RECORDING_PRIORITY_DWELL_NS
                if self._recording_active and recording_overloaded
                else _OVERLOAD_DWELL_NS
            )
            if now - self._overload_since_mono_ns < overload_dwell_ns:
                return
            if self._requested_quality_index < len(_LIVE_QUALITIES) - 1:
                self._requested_quality_index += 1
                self._reason = (
                    "recording_priority"
                    if recording_overloaded
                    else "live_encoder_overload"
                )
                self._overload_since_mono_ns = now
                self._reset_sample_phase_locked()
                self._condition.notify_all()
            else:
                self._pause_requested = True
                self._reason = (
                    "paused_to_protect_recording"
                    if recording_overloaded
                    else "paused_live_overload"
                )
                self._queue.clear()
                self._queued_bytes = 0
                self._sample_slots.clear()
                self._audio_dropped += len(self._audio_queue)
                self._audio_queue.clear()
                self._audio_queued_bytes = 0
                self._condition.notify_all()
        else:
            self._overload_since_mono_ns = None
            if self._healthy_since_mono_ns is None:
                self._healthy_since_mono_ns = now
            elif (
                self._requested_quality_index > 0
                and now - self._healthy_since_mono_ns >= _QUALITY_RECOVERY_DWELL_NS
            ):
                self._requested_quality_index -= 1
                self._reason = None
                self._healthy_since_mono_ns = now
                self._reset_sample_phase_locked()
                self._condition.notify_all()

    def _reset_sample_phase_locked(self) -> None:
        """fps変更時に旧sample phaseの待ち行列を破棄する。"""
        self._dropped += len(self._queue)
        self._queue.clear()
        self._queued_bytes = 0
        self._sample_slots.clear()
        self._sample_origin_mono_ns = None
        self._last_submitted_sample_slot = None

    def _render_preview(self, envelope: FrameEnvelope, quality_index: int = 0) -> Any:
        """capture JPEGを現在のlive段階へ縮小し、overlayを合成する。"""
        width, height, _fps, _name = _LIVE_QUALITIES[quality_index]
        image = _decode_live_image(envelope.jpeg_bytes)
        image.thumbnail((width, height))
        image_module = importlib.import_module("PIL.Image")
        canvas = image_module.new("RGB", (width, height), color=(0, 0, 0))
        canvas.paste(
            image,
            (
                (width - image.width) // 2,
                (height - image.height) // 2,
            ),
        )
        with self._condition:
            settings = self._settings
        draw_timestamp_overlay(canvas, envelope, settings)
        return canvas


def _decode_live_image(jpeg: bytes) -> Any:
    from io import BytesIO

    try:
        image_module = importlib.import_module("PIL.Image")
        with image_module.open(BytesIO(jpeg)) as source:
            image = source.convert("RGB")
            image.load()
            return image
    except Exception as exc:
        raise ValueError("live JPEG frameをdecodeできません") from exc

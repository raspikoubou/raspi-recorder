"""PyAVによる、独立再生可能なfragmented MP4の作成。"""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from raspi_recorder.adapters.audio_encoding import (
    AUDIO_SAMPLE_RATE,
    AacEncoder,
)
from raspi_recorder.adapters.bounded_progress import LatestValueWorker
from raspi_recorder.adapters.timestamp_overlay import draw_timestamp_overlay
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.encoding import target_video_bitrate
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.domain.recordings import (
    RecordingArtifact,
    RecordingProgress,
    RecordingTimeSpan,
)

_SAFE_MP4_NAME = re.compile(r"^[A-Za-z0-9_-]{1,140}\.mp4$")
_MANIFEST_PROGRESS_INTERVAL_NS = 5_000_000_000
_WALL_CLOCK_JUMP_TOLERANCE_NS = 250_000_000
_MANIFEST_VERSION = 1
_SAFE_RECORDING_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_VIDEO_TIME_BASE = Fraction(1, 90_000)


def _capture_mono_ns_to_video_pts(capture_mono_ns: int, origin_mono_ns: int) -> int:
    """Convert a monotonic capture timestamp to the MP4 video time base."""
    if capture_mono_ns < origin_mono_ns:
        raise ValueError("frameがMP4 timeline originより前です")
    elapsed_ns = capture_mono_ns - origin_mono_ns
    return round(Fraction(elapsed_ns, 1_000_000_000) / _VIDEO_TIME_BASE)


class MediaWriterError(RuntimeError):
    """録画ファイルの生成・確定に失敗した。"""


@dataclass(frozen=True, slots=True)
class _FrameMetadata:
    """writerが保持する時刻・sequenceだけのframe metadata。"""

    sequence: int
    generation: int
    capture_mono_ns: int
    capture_utc_ns: int
    timezone: str

    @classmethod
    def from_envelope(cls, envelope: FrameEnvelope) -> _FrameMetadata:
        return cls(
            sequence=envelope.sequence,
            generation=envelope.generation,
            capture_mono_ns=envelope.capture_mono_ns,
            capture_utc_ns=envelope.capture_utc_ns,
            timezone=envelope.timezone,
        )


def _update_time_spans(
    spans: list[dict[str, int | str]],
    previous: FrameEnvelope | _FrameMetadata | None,
    current: FrameEnvelope | _FrameMetadata,
) -> None:
    """壁時計段差またはtimezone変更を境にmanifest範囲を分ける。"""
    if previous is None:
        spans.append(PyAvMp4Writer._time_span(current))
        return
    expected_utc_ns = previous.capture_utc_ns + (
        current.capture_mono_ns - previous.capture_mono_ns
    )
    wall_clock_jump = abs(current.capture_utc_ns - expected_utc_ns)
    if (
        current.timezone != previous.timezone
        or wall_clock_jump > _WALL_CLOCK_JUMP_TOLERANCE_NS
    ):
        spans.append(PyAvMp4Writer._time_span(current))
        return
    spans[-1].update(
        last_sequence=current.sequence,
        end_mono_ns=current.capture_mono_ns,
        end_utc_ns=current.capture_utc_ns,
    )


def _clip_audio_to_timeline_origin(
    envelope: AudioEnvelope, origin_mono_ns: int
) -> AudioEnvelope | None:
    """timeline開始前に終わるblockを捨て、交差するblockだけをsample境界で切る。"""
    if envelope.end_mono_ns <= origin_mono_ns:
        return None
    if envelope.capture_mono_ns < origin_mono_ns:
        return envelope.clip(origin_mono_ns, envelope.end_mono_ns)
    return envelope


class PyAvMp4Writer:
    """JPEG frameをH.264/yuv420p/MP4へエンコードし、安全に確定する。

    `recordings_root`は上位の媒体adapterが検証した専用録画領域であり、
    `file_name`は単一のASCII basenameに限定する。partialは同一directoryに
    作り、正常終了時だけ最終名へatomic renameする。
    """

    def __init__(
        self,
        recordings_root: str | Path,
        file_name: str,
        settings: CameraSettings,
        *,
        camera_id: int,
        mode: str,
        logical_event_id: str | None = None,
        part_index: int = 1,
        overlay_enabled: bool | None = None,
        recording_id: str | None = None,
        audio_track_enabled: bool = False,
        timeline_origin_mono_ns: int | None = None,
        generation: int | None = None,
    ) -> None:
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        if mode not in {"continuous", "event"}:
            raise ValueError("modeはcontinuousまたはeventで指定してください")
        if type(part_index) is not int or part_index < 1:
            raise ValueError("part_indexは1以上の整数で指定してください")
        if not _SAFE_MP4_NAME.fullmatch(file_name):
            raise ValueError("file_nameは安全な.mp4 basenameで指定してください")
        if recording_id is not None and not _SAFE_RECORDING_ID.fullmatch(recording_id):
            raise ValueError("recording_idは安全なopaque IDで指定してください")
        if type(audio_track_enabled) is not bool:
            raise ValueError("audio_track_enabledはboolで指定してください")
        if audio_track_enabled and not settings.audio_enabled:
            raise ValueError("音声設定OFFでaudio trackを作成できません")
        if audio_track_enabled and timeline_origin_mono_ns is None:
            raise ValueError("audio trackには明示したtimeline originが必要です")
        if timeline_origin_mono_ns is not None and (
            type(timeline_origin_mono_ns) is not int or timeline_origin_mono_ns < 0
        ):
            raise ValueError("timeline_origin_mono_nsは0以上の整数が必要です")
        if generation is not None and (type(generation) is not int or generation < 1):
            raise ValueError("generationは1以上の整数が必要です")
        try:
            root = Path(recordings_root).resolve(strict=True)
        except OSError as exc:
            raise ValueError("recordings_rootは存在するdirectoryが必要です") from exc
        if not root.is_dir():
            raise ValueError("recordings_rootはdirectoryで指定してください")

        self._root = root
        self._path = root / file_name
        if self._path.parent.resolve(strict=True) != root:
            raise ValueError("録画先がrecordings_root直下ではありません")
        self._partial_path = root / f"{file_name}.partial"
        self._manifest_path = root / f"{file_name}.recording.json"
        for candidate in (self._path, self._partial_path, self._manifest_path):
            if candidate.exists() or candidate.is_symlink():
                raise FileExistsError(candidate.name)

        self._settings = settings
        self._recording_id = self._path.stem if recording_id is None else recording_id
        self._camera_id = camera_id
        self._mode = mode
        self._logical_event_id = logical_event_id
        self._part_index = part_index
        self._overlay_enabled = (
            settings.overlay_enabled
            if overlay_enabled is None
            else self._validate_bool("overlay_enabled", overlay_enabled)
        )
        self._bit_rate = target_video_bitrate(settings)
        self._container: Any | None = None
        self._file: Any | None = None
        self._stream: Any | None = None
        self._audio_encoder: AacEncoder | None = None
        self._audio_track_enabled = audio_track_enabled
        self._timeline_origin_mono_ns = timeline_origin_mono_ns
        self._generation = generation
        self._first_audio_start_mono_ns: int | None = None
        self._first_audio_start_utc_ns: int | None = None
        self._last_audio_end_utc_ns: int | None = None
        self._last_audio_end_mono_ns: int | None = None
        self._audio_block_count = 0
        self._audio_sample_count = 0
        self._reformatter: Any | None = None
        self._closed = False
        self._finalized = False
        self._frame_count = 0
        self._first: _FrameMetadata | None = None
        self._last: _FrameMetadata | None = None
        self._last_manifest_mono_ns: int | None = None
        self._time_spans: list[dict[str, int | str]] = []

        self._write_manifest("writing")
        self._manifest_worker_closed = False
        self._manifest_worker = LatestValueWorker(
            self._persist_manifest_json,
            name=f"manifest-progress-{self._recording_id[:32]}",
        )
        try:
            av = self._load_module("av")
            self._file = self._partial_path.open("x+b")
            self._container = av.open(
                self._file,
                mode="w",
                format="mp4",
                container_options={
                    "movflags": "frag_keyframe+empty_moov+default_base_moof"
                },
            )
            self._stream = self._container.add_stream(
                "libx264", rate=Fraction(settings.fps, 1)
            )
            self._stream.width = settings.width
            self._stream.height = settings.height
            self._stream.pix_fmt = "yuv420p"
            self._stream.time_base = _VIDEO_TIME_BASE
            context = self._stream.codec_context
            context.time_base = _VIDEO_TIME_BASE
            context.framerate = Fraction(settings.fps, 1)
            # Bound the encoder's CPU use without forcing full-HD recording
            # through one core. On Pi 5 the single-thread path falls below
            # 30fps and eventually exhausts the retained-frame budget.
            context.thread_count = (
                3 if settings.width * settings.height > 1280 * 720 else 1
            )
            context.max_b_frames = 0
            context.gop_size = max(1, settings.fps * 2)
            context.bit_rate = self._bit_rate
            context.color_range = 1  # AVCOL_RANGE_MPEG: limited-range yuv420p.
            context.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "x264-params": "open-gop=0:scenecut=0",
            }
            if self._audio_track_enabled:
                self._audio_encoder = AacEncoder(
                    self._container,
                    av,
                    timeline_origin_mono_ns=(
                        0
                        if self._timeline_origin_mono_ns is None
                        else self._timeline_origin_mono_ns
                    ),
                )
            self._reformatter = av.video.reformatter.VideoReformatter()
        except Exception as exc:
            try:
                self._close_manifest_worker()
            except Exception as worker_error:
                exc.add_note(
                    "manifest worker cleanup also failed: "
                    f"{type(worker_error).__name__}: {worker_error}"
                )
            try:
                self._close_resources()
            except Exception:
                pass
            self._write_manifest("abnormal", reason=type(exc).__name__)
            raise MediaWriterError("MP4 writerの初期化に失敗しました") from exc

    @property
    def partial_path(self) -> Path:
        """現在書込中のpartial path。"""
        return self._partial_path

    @property
    def frame_count(self) -> int:
        """受け付けたframe数。"""
        return self._frame_count

    @property
    def audio_track_enabled(self) -> bool:
        """この物理fileのMP4にAAC trackを含めるか返す。"""
        return self._audio_track_enabled

    def progress(self) -> RecordingProgress | None:
        """現在のframe時刻とsizeを索引へ反映できる形で返す。"""
        if self._first is None or self._last is None:
            return None
        spans = tuple(
            RecordingTimeSpan(
                first_sequence=int(span["first_sequence"]),
                last_sequence=int(span["last_sequence"]),
                start_mono_ns=int(span["start_mono_ns"]),
                end_mono_ns=int(span["end_mono_ns"]),
                start_utc_ns=int(span["start_utc_ns"]),
                end_utc_ns=int(span["end_utc_ns"]),
                timezone=str(span["timezone"]),
            )
            for span in self._time_spans
        )
        try:
            size_bytes = self._partial_path.stat().st_size
        except FileNotFoundError:
            size_bytes = 0
        return RecordingProgress(
            frame_count=self._frame_count,
            first_sequence=self._first.sequence,
            last_sequence=self._last.sequence,
            start_mono_ns=self._first.capture_mono_ns,
            end_mono_ns=self._last.capture_mono_ns,
            start_utc_ns=self._first.capture_utc_ns,
            end_utc_ns=self._last.capture_utc_ns,
            size_bytes=size_bytes,
            spans=spans,
        )

    def write(self, envelope: FrameEnvelope) -> None:
        """単一JPEG frameを時刻・順序を保ってエンコードする。"""
        if self._closed or self._container is None or self._stream is None:
            raise RuntimeError("既に閉じたMP4 writerです")
        if not isinstance(envelope, FrameEnvelope):
            raise ValueError("envelopeはFrameEnvelopeで指定してください")
        if self._last is not None:
            if envelope.generation != self._last.generation:
                raise ValueError("1つのMP4内でcamera generationを変更できません")
            if envelope.sequence <= self._last.sequence:
                raise ValueError("frame sequenceはMP4内で増加する必要があります")
            if envelope.capture_mono_ns <= self._last.capture_mono_ns:
                raise ValueError("frame monotonic時刻はMP4内で増加する必要があります")
        if self._generation is None:
            self._generation = envelope.generation
        elif envelope.generation != self._generation:
            raise ValueError("MP4内でcamera generationを変更できません")

        if self._timeline_origin_mono_ns is None:
            self._timeline_origin_mono_ns = envelope.capture_mono_ns
        if envelope.capture_mono_ns < self._timeline_origin_mono_ns:
            raise ValueError("frameがMP4 timeline originより前です")

        try:
            image = self._decode_image(envelope.jpeg_bytes)
            self._timezone(envelope.timezone)
            if image.size != (self._settings.width, self._settings.height):
                raise ValueError(
                    f"input frame size mismatch: {image.width}x{image.height}"
                )
            if self._overlay_enabled:
                self._draw_timestamp(image, envelope)
            av = self._load_module("av")
            frame = self._rgb_video_frame(av, image)
            if self._reformatter is None:
                raise RuntimeError("PyAV video reformatterが初期化されていません")
            frame = self._reformatter.reformat(
                frame,
                format="yuv420p",
                src_color_range="JPEG",
                dst_color_range="MPEG",
            )
            if frame.format.name != "yuv420p":
                raise MediaWriterError("encoder入力をyuv420pへ変換できません")
            frame.color_range = 1  # reformatのrange引数はmetadataへ引き継がれない。
            frame.pts = _capture_mono_ns_to_video_pts(
                envelope.capture_mono_ns, self._timeline_origin_mono_ns
            )
            frame.time_base = _VIDEO_TIME_BASE
            if self._first is None:
                frame.pict_type = av.video.frame.PictureType.I
            for packet in self._stream.encode(frame):
                self._container.mux(packet)
        except Exception as exc:
            try:
                self.abort(type(exc).__name__)
            except Exception as abort_error:
                exc.add_note(
                    "MP4 abort cleanup also failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            raise MediaWriterError("映像frameのエンコードに失敗しました") from exc

        metadata = _FrameMetadata.from_envelope(envelope)
        _update_time_spans(self._time_spans, self._last, metadata)
        if self._first is None:
            self._first = metadata
        self._last = metadata
        self._frame_count += 1
        if (
            self._last_manifest_mono_ns is None
            or envelope.capture_mono_ns - self._last_manifest_mono_ns
            >= _MANIFEST_PROGRESS_INTERVAL_NS
        ):
            try:
                self._manifest_worker.submit(self._manifest_json("writing"))
                self._last_manifest_mono_ns = envelope.capture_mono_ns
            except Exception as exc:
                try:
                    self.abort("manifest_write_failed")
                except Exception as abort_error:
                    exc.add_note(
                        "MP4 abort cleanup also failed: "
                        f"{type(abort_error).__name__}: {abort_error}"
                    )
                raise MediaWriterError("録画manifestを更新できません") from exc

    def write_audio(self, envelope: AudioEnvelope) -> None:
        """取得時刻付きmono PCMをAAC-LC/48kHz/monoへencodeする。"""
        if not self._audio_track_enabled:
            return
        if self._closed or self._container is None or self._audio_encoder is None:
            raise RuntimeError("audio writerは利用できません")
        if not isinstance(envelope, AudioEnvelope):
            raise ValueError("envelopeはAudioEnvelopeで指定してください")
        if self._generation is not None and envelope.generation != self._generation:
            raise ValueError("audio generationがMP4のvideo generationと異なります")
        if self._generation is None:
            self._generation = envelope.generation
        origin = self._timeline_origin_mono_ns
        if origin is None:
            raise RuntimeError("audio timeline originがありません")
        clipped_envelope = _clip_audio_to_timeline_origin(envelope, origin)
        if clipped_envelope is None:
            return
        envelope = clipped_envelope

        try:
            for packet in self._audio_encoder.encode(envelope):
                self._container.mux(packet)
        except Exception as exc:
            try:
                self.abort(type(exc).__name__)
            except Exception as abort_error:
                exc.add_note(
                    "MP4 abort cleanup also failed: "
                    f"{type(abort_error).__name__}: {abort_error}"
                )
            raise MediaWriterError("音声blockのエンコードに失敗しました") from exc
        self._last_audio_end_mono_ns = envelope.end_mono_ns
        if self._first_audio_start_mono_ns is None:
            self._first_audio_start_mono_ns = envelope.capture_mono_ns
            self._first_audio_start_utc_ns = envelope.capture_utc_ns
        self._last_audio_end_utc_ns = envelope.capture_utc_ns + envelope.duration_ns
        self._audio_block_count += 1
        self._audio_sample_count += envelope.sample_count

    def finalize(self) -> RecordingArtifact:
        """flush・decode検査・fsync後に同一媒体内renameして確定する。"""
        if self._finalized:
            raise RuntimeError("MP4 writerは既に確定済みです")
        if self._closed or self._container is None or self._stream is None:
            raise RuntimeError("MP4 writerは利用できません")
        if self._frame_count == 0 or self._first is None or self._last is None:
            self.abort("empty_recording")
            raise MediaWriterError("frameのないMP4は確定できません")

        replaced = False
        try:
            self._close_manifest_worker()
            for packet in self._stream.encode(None):
                self._container.mux(packet)
            if self._audio_track_enabled:
                assert self._audio_encoder is not None
                for packet in self._audio_encoder.flush():
                    self._container.mux(packet)
            self._close_resources()
            self._validate_mp4()
            self._fsync_file(self._partial_path)
            self._write_manifest("finalizing")
            os.replace(self._partial_path, self._path)
            # renameが成功した時点でMP4は確定済み。以後のmanifest記録等の
            # 失敗で、完成したfileをabnormalへ書き戻してはならない。
            replaced = True
            self._finalized = True
            self._fsync_directory(self._root)
            size_bytes = self._path.stat().st_size
            self._write_manifest("finalized", size_bytes=size_bytes)
            return RecordingArtifact(
                path=str(self._path),
                frame_count=self._frame_count,
                first_sequence=self._first.sequence,
                last_sequence=self._last.sequence,
                first_mono_ns=self._first.capture_mono_ns,
                last_mono_ns=self._last.capture_mono_ns,
                first_utc_ns=self._first.capture_utc_ns,
                last_utc_ns=self._last.capture_utc_ns,
                size_bytes=size_bytes,
            )
        except Exception as exc:
            try:
                self._close_resources()
            except Exception as close_error:
                exc.add_note(
                    "MP4 resource cleanup also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            if replaced:
                exc.add_note(
                    "MP4 file was already finalized on disk; "
                    "manifest was left as finalizing"
                )
            else:
                try:
                    self._write_manifest("abnormal", reason=type(exc).__name__)
                except Exception as manifest_error:
                    exc.add_note(
                        "abnormal manifest update failed: "
                        f"{type(manifest_error).__name__}: {manifest_error}"
                    )
            raise MediaWriterError("MP4を正常確定できませんでした") from exc

    def abort(self, reason: str) -> None:
        """partialを正常扱いせず閉じ、abnormalとして残す。"""
        if self._finalized:
            return
        worker_error: Exception | None = None
        try:
            self._close_manifest_worker()
        except Exception as exc:
            worker_error = exc
        try:
            self._close_resources()
        except Exception as exc:
            reason = f"{self._safe_reason(reason)}:{type(exc).__name__}"
        try:
            self._write_manifest("abnormal", reason=self._safe_reason(reason))
        except Exception as manifest_error:
            if worker_error is not None:
                manifest_error.add_note(
                    "manifest progress worker also failed: "
                    f"{type(worker_error).__name__}: {worker_error}"
                )
            raise
        if worker_error is not None:
            raise worker_error

    def validate_full_decode(self) -> None:
        """確定後の受入検査用に、全video/audioを最後までdecodeする。"""
        if not self._finalized or not self._path.is_file():
            raise RuntimeError("全量decodeは確定済みMP4に対して実行してください")
        self._validate_mp4_path(self._path, full_decode=True)

    def _validate_mp4(self) -> None:
        """通常確定用の有界な構造・先頭・末尾サンプル検査。"""
        self._validate_mp4_path(self._partial_path, full_decode=False)

    def _validate_mp4_path(self, path: Path, *, full_decode: bool) -> None:
        av = self._load_module("av")
        with av.open(str(path), mode="r", format="mp4") as container:
            video_streams = tuple(container.streams.video)
            audio_streams = tuple(container.streams.audio)
            if len(video_streams) != 1:
                raise MediaWriterError("MP4の映像stream数が1ではありません")
            if len(audio_streams) != int(self._audio_track_enabled):
                raise MediaWriterError("MP4の音声stream数が設定と一致しません")

            video = video_streams[0]
            if video.codec_context.name != "h264":
                raise MediaWriterError("MP4の映像codecがH.264ではありません")
            if video.codec_context.pix_fmt != "yuv420p":
                raise MediaWriterError("MP4のpixel formatがyuv420pではありません")
            if video.codec_context.has_b_frames:
                raise MediaWriterError("MP4にB-frameが含まれています")
            if (video.codec_context.width, video.codec_context.height) != (
                self._settings.width,
                self._settings.height,
            ):
                raise MediaWriterError("MP4の解像度が設定と一致しません")
            if video.time_base is None:
                raise MediaWriterError("MP4映像trackのtime baseがありません")

            if full_decode:
                video_count = 0
                for decoded in container.decode(video=0):
                    self._validate_video_sample(decoded, first=video_count == 0)
                    video_count += 1
                if video_count != self._frame_count:
                    raise MediaWriterError(
                        f"MP4 decode frame数が不一致です: {video_count} != "
                        f"{self._frame_count}"
                    )
            else:
                first_decoder = container.decode(video=0)
                try:
                    first = next(first_decoder, None)
                finally:
                    first_decoder.close()
                if first is None:
                    raise MediaWriterError("MP4先頭video frameをdecodeできません")
                self._validate_video_sample(first, first=True)
                self._validate_video_tail(container, video)

            if self._audio_track_enabled:
                audio = audio_streams[0]
                self._validate_audio_stream(audio)
                if self._audio_block_count <= 0 or self._audio_sample_count <= 0:
                    raise MediaWriterError("MP4音声trackに入力sampleがありません")
                if full_decode:
                    audio_count = 0
                    container.seek(audio.start_time or 0, stream=audio)
                    for decoded in container.decode(audio=0):
                        self._validate_audio_sample(decoded)
                        audio_count += decoded.samples
                    if audio_count <= 0:
                        raise MediaWriterError(
                            "MP4音声trackにdecode可能なsampleがありません"
                        )
                    if abs(audio_count - self._audio_sample_count) > 2_048:
                        raise MediaWriterError(
                            "MP4 decoded audio sample数が入力と一致しません"
                        )
                else:
                    self._validate_audio_samples(container, audio)

    def _validate_video_tail(self, container: Any, stream: Any) -> None:
        first = self._first
        last = self._last
        origin = self._timeline_origin_mono_ns
        if first is None or last is None or origin is None:
            raise MediaWriterError("MP4の映像時刻情報がありません")
        expected_last_ns = max(0, last.capture_mono_ns - origin)
        tail_start_ns = max(0, expected_last_ns - 3_000_000_000)
        ticks_per_second = Fraction(1, 1) / stream.time_base
        start_pts = stream.start_time or 0
        seek_pts = start_pts + int(
            Fraction(tail_start_ns, 1_000_000_000) * ticks_per_second
        )
        container.seek(seek_pts, stream=stream, backward=True)
        decoder = container.decode(video=0)
        tail = None
        try:
            # seekは直前のkey frameへ戻る。3秒の末尾窓と最大2秒GOPを覆う。
            for index, decoded in enumerate(decoder):
                self._validate_video_sample(decoded)
                tail = decoded
                if index + 1 >= max(32, self._settings.fps * 6 + 4):
                    break
        finally:
            decoder.close()
        if tail is None:
            raise MediaWriterError("MP4末尾video frameをdecodeできません")
        if tail.pts is not None:
            expected_pts = start_pts + int(
                Fraction(expected_last_ns, 1_000_000_000) * ticks_per_second
            )
            tolerance = max(
                2,
                int(ticks_per_second * Fraction(2, self._settings.fps)) + 2,
            )
            if tail.pts < expected_pts - tolerance:
                raise MediaWriterError("MP4の末尾frameが入力時刻より欠けています")

    def _validate_audio_samples(self, container: Any, stream: Any) -> None:
        time_base = stream.time_base
        if time_base is None:
            raise MediaWriterError("MP4音声trackのtime baseがありません")
        start_pts = stream.start_time or 0
        duration_ticks = stream.duration
        if duration_ticks is None or duration_ticks <= 0:
            if self._last_audio_end_mono_ns is None:
                raise MediaWriterError("MP4音声trackのdurationがありません")
            origin = self._timeline_origin_mono_ns
            if origin is None:
                origin = self._first_audio_start_mono_ns
            if origin is None:
                raise MediaWriterError("MP4音声trackの開始時刻がありません")
            duration_ns = max(0, self._last_audio_end_mono_ns - origin)
            duration_ticks = int(Fraction(duration_ns, 1_000_000_000) / time_base)
        tail_start = start_pts + max(0, duration_ticks - max(1, int(1 / time_base)))
        for offset in (start_pts, tail_start):
            container.seek(offset, stream=stream, backward=True)
            decoder = container.decode(audio=0)
            try:
                decoded = next(decoder, None)
            finally:
                decoder.close()
            if decoded is None:
                raise MediaWriterError("MP4音声trackの先頭/末尾をdecodeできません")
            self._validate_audio_sample(decoded)

    def _validate_video_sample(self, decoded: Any, *, first: bool = False) -> None:
        if decoded.width != self._settings.width or decoded.height != (
            self._settings.height
        ):
            raise MediaWriterError("MP4のframe解像度が設定と一致しません")
        if decoded.format.name != "yuv420p":
            raise MediaWriterError("decoded pixel formatがyuv420pではありません")
        if first and not decoded.key_frame:
            raise MediaWriterError("MP4先頭frameがkey frameではありません")

    @staticmethod
    def _validate_audio_stream(stream: Any) -> None:
        if stream.codec_context.name != "aac":
            raise MediaWriterError("MP4の音声codecがAACではありません")
        if stream.codec_context.sample_rate != AUDIO_SAMPLE_RATE:
            raise MediaWriterError("MP4の音声sample rateが48kHzではありません")
        if stream.codec_context.layout.name != "mono":
            raise MediaWriterError("MP4の音声channel layoutがmonoではありません")

    @staticmethod
    def _validate_audio_sample(decoded: Any) -> None:
        if decoded.sample_rate != AUDIO_SAMPLE_RATE:
            raise MediaWriterError("decoded audio rateが48kHzではありません")
        if decoded.layout.name != "mono":
            raise MediaWriterError("decoded audio layoutがmonoではありません")

    def _write_manifest(
        self,
        state: str,
        *,
        reason: str | None = None,
        size_bytes: int | None = None,
    ) -> None:
        self._persist_manifest_json(
            self._manifest_json(state, reason=reason, size_bytes=size_bytes)
        )

    def _manifest_json(
        self,
        state: str,
        *,
        reason: str | None = None,
        size_bytes: int | None = None,
    ) -> str:
        first = self._first
        last = self._last
        payload = {
            "schema_version": _MANIFEST_VERSION,
            "recording_id": self._recording_id,
            "camera_id": self._camera_id,
            "mode": self._mode,
            "logical_event_id": self._logical_event_id,
            "part_index": self._part_index,
            "state": state,
            "partial_name": self._partial_path.name,
            "file_name": self._path.name,
            "width": self._settings.width,
            "height": self._settings.height,
            "fps": self._settings.fps,
            "quality": self._settings.quality.value,
            "video_bitrate_target": self._bit_rate,
            "overlay_enabled": self._overlay_enabled,
            "audio_track_present": self._audio_track_enabled,
            "audio_block_count": self._audio_block_count,
            "audio_sample_count": self._audio_sample_count,
            "audio_start_mono_ns": self._first_audio_start_mono_ns,
            "audio_end_mono_ns": self._last_audio_end_mono_ns,
            "audio_start_utc_ns": self._first_audio_start_utc_ns,
            "audio_end_utc_ns": self._last_audio_end_utc_ns,
            "frame_count": self._frame_count,
            "first_sequence": None if first is None else first.sequence,
            "last_sequence": None if last is None else last.sequence,
            "generation": None if first is None else first.generation,
            "timezone": None if first is None else first.timezone,
            "time_spans": [dict(span) for span in self._time_spans],
            "start_mono_ns": None if first is None else first.capture_mono_ns,
            "end_mono_ns": None if last is None else last.capture_mono_ns,
            "start_utc_ns": None if first is None else first.capture_utc_ns,
            "end_utc_ns": None if last is None else last.capture_utc_ns,
            "size_bytes": size_bytes,
            "reason": reason,
            "updated_utc": datetime.now(UTC).isoformat(),
        }
        return json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"

    def _persist_manifest_json(self, serialized: str) -> None:
        temp_path = self._root / f".{self._manifest_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._manifest_path)
            self._fsync_directory(self._root)
        finally:
            temp_path.unlink(missing_ok=True)

    def _close_manifest_worker(self) -> None:
        if self._manifest_worker_closed:
            return
        self._manifest_worker_closed = True
        self._manifest_worker.close()

    def _close_resources(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        if self._container is not None:
            try:
                self._container.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                self._container = None
        if self._file is not None and not self._file.closed:
            try:
                self._file.flush()
            except Exception as exc:
                errors.append(exc)
            try:
                self._file.close()
            except Exception as exc:
                errors.append(exc)
        self._file = None
        self._stream = None
        self._audio_encoder = None
        self._reformatter = None
        if errors:
            raise errors[0]

    def _decode_image(self, jpeg_bytes: bytes) -> Any:
        pillow_image = self._load_module("PIL.Image")
        with pillow_image.open(BytesIO(jpeg_bytes), formats=("JPEG",)) as source:
            # Image.openはlazyなので、画素を全展開する前にheaderの寸法で
            # 検証し、サイズ不一致のframeをdecodeせずに弾く。
            if source.size != (self._settings.width, self._settings.height):
                raise ValueError(
                    f"input frame size mismatch: {source.width}x{source.height}"
                )
            source.load()
            # Loading finishes the JPEG before its file context closes. Its
            # pixels remain valid; copying an already-RGB full-HD image here
            # adds avoidable memory traffic on every captured frame.
            return source if source.mode == "RGB" else source.convert("RGB")

    @staticmethod
    def _rgb_video_frame(av: Any, image: Any) -> Any:
        frame = av.VideoFrame(image.width, image.height, "rgb24")
        plane = frame.planes[0]
        row_bytes = image.width * 3
        if (
            plane.line_size != row_bytes
            or plane.buffer_size != row_bytes * image.height
        ):
            # Unusual widths have aligned row padding. Let PyAV copy those
            # rows rather than treating padding as image pixels.
            return av.VideoFrame.from_image(image)
        plane.update(image.tobytes())
        return frame

    @staticmethod
    def _time_span(
        frame: FrameEnvelope | _FrameMetadata,
    ) -> dict[str, int | str]:
        return {
            "first_sequence": frame.sequence,
            "last_sequence": frame.sequence,
            "start_mono_ns": frame.capture_mono_ns,
            "end_mono_ns": frame.capture_mono_ns,
            "start_utc_ns": frame.capture_utc_ns,
            "end_utc_ns": frame.capture_utc_ns,
            "timezone": frame.timezone,
        }

    def _draw_timestamp(self, image: Any, envelope: FrameEnvelope) -> None:
        draw_timestamp_overlay(
            image,
            envelope,
            self._settings,
            enabled=self._overlay_enabled,
        )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_bool(name: str, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError(f"{name}はboolで指定してください")
        return value

    @staticmethod
    def _safe_reason(reason: str) -> str:
        if not isinstance(reason, str) or not reason:
            return "unspecified"
        return reason[:100]

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("frame timezoneがIANA timezoneとして不正です") from exc

    @staticmethod
    def _load_module(name: str) -> Any:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name.split(".")[0]:
                raise RuntimeError(
                    f"実行時依存{name}がインストールされていません"
                ) from exc
            raise

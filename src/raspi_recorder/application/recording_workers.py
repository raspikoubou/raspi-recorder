"""常時録画のframe分配。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Event
from typing import Protocol

from raspi_recorder.application.frame_ingress import (
    ContinuousFrameSession,
)
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.domain.recordings import (
    RecordingArtifact,
    next_continuous_boundary_mono_ns,
)
from raspi_recorder.ports.media import SegmentWriter


class SegmentWriterFactory(Protocol):
    """物理MP4 partに対応するwriterを作る。"""

    def __call__(
        self, part_index: int, first_frame: FrameEnvelope
    ) -> SegmentWriter: ...


@dataclass(slots=True)
class _RecordingPart:
    writer: SegmentWriter | None = None
    part_index: int = 0
    deadline_ns: int | None = None
    audio_enabled: bool = False


def _write_frame_to_part(
    frame: FrameEnvelope,
    part: _RecordingPart,
    writer_factory: SegmentWriterFactory,
    deadline_factory: Callable[[FrameEnvelope], int],
) -> tuple[RecordingArtifact, ...]:
    """audio track切替とpart境界を保ち、各payloadを一度だけ割り当てる。"""
    artifacts: list[RecordingArtifact] = []
    if part.writer is None:
        pending_audio = frame.audio_blocks
    elif frame.audio_available != part.audio_enabled:
        if part.audio_enabled:
            _write_audio_blocks(part.writer, frame.audio_blocks)
        artifacts.append(part.writer.finalize())
        part.writer = None
        pending_audio = frame.audio_blocks if frame.audio_available else ()
    elif part.deadline_ns is not None and frame.capture_mono_ns >= part.deadline_ns:
        before: list[AudioEnvelope] = []
        after: list[AudioEnvelope] = []
        for block in frame.audio_blocks:
            left, right = block.split_at(part.deadline_ns)
            if left is not None:
                before.append(left)
            if right is not None:
                after.append(right)
        if part.audio_enabled:
            _write_audio_blocks(part.writer, tuple(before))
        artifacts.append(part.writer.finalize())
        part.writer = None
        pending_audio = tuple(after)
    else:
        pending_audio = frame.audio_blocks

    if part.writer is None:
        part.part_index += 1
        part.writer = writer_factory(part.part_index, frame)
        part.audio_enabled = frame.audio_available
        part.deadline_ns = deadline_factory(frame)
    if part.audio_enabled:
        _write_audio_blocks(part.writer, pending_audio)
    part.writer.write(replace(frame, audio_blocks=()))
    return tuple(artifacts)


def _write_audio_blocks(
    writer: SegmentWriter, blocks: tuple[AudioEnvelope, ...]
) -> None:
    for block in blocks:
        writer.write_audio(block)


def record_continuous_session(
    session: ContinuousFrameSession,
    stop_event: Event,
    writer_factory: SegmentWriterFactory,
    *,
    batch_size: int = 128,
    wait_seconds: float = 0.5,
    artifact_callback: Callable[[RecordingArtifact], None] | None = None,
    frame_written_callback: Callable[[FrameEnvelope], None] | None = None,
) -> None:
    """共有ring leaseをackしながら常時frame queueを保存する。"""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_sizeは1以上の整数で指定してください")
    if type(wait_seconds) not in (int, float) or wait_seconds <= 0:
        raise ValueError("wait_secondsは0より大きい数値で指定してください")

    part = _RecordingPart()
    try:
        while True:
            batch = session.take_batch(batch_size, wait_seconds)
            if batch is None:
                if session.finished:
                    break
                continue
            with batch:
                for frame in batch.frames:
                    try:
                        completed = _write_frame_to_part(
                            frame,
                            part,
                            writer_factory,
                            lambda item: next_continuous_boundary_mono_ns(
                                item.capture_mono_ns,
                                item.capture_utc_ns,
                                item.timezone,
                            ),
                        )
                        if artifact_callback is not None:
                            for artifact in completed:
                                artifact_callback(artifact)
                        if frame_written_callback is not None:
                            frame_written_callback(frame)
                    finally:
                        # The batch acknowledges and releases its leases on
                        # context exit. Drop the loop's independent reference
                        # before that accounting reaches zero.
                        del frame
        if part.writer is not None:
            artifact = part.writer.finalize()
            part.writer = None
            if artifact_callback is not None:
                artifact_callback(artifact)
    except Exception:
        if part.writer is not None:
            _abort_best_effort(part.writer, "continuous_worker_failed")
        raise


def _abort_best_effort(writer: SegmentWriter, reason: str) -> None:
    try:
        writer.abort(reason)
    except Exception:
        pass

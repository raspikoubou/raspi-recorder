"""録画writerとengine use caseの境界。"""

from __future__ import annotations

from typing import Protocol

from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from raspi_recorder.domain.recordings import RecordingArtifact


class SegmentWriter(Protocol):
    """1物理MP4 partへ順次frameを書き込む。"""

    def write(self, envelope: FrameEnvelope) -> None:
        """取得順frameをwriteする。"""
        ...

    def write_audio(self, envelope: AudioEnvelope) -> None:
        """音声blockを取得時刻順にwriteする。trackなしwriterでは呼ばない。"""
        ...

    def finalize(self) -> RecordingArtifact:
        """MP4を検査し、正常確定する。"""
        ...

    def abort(self, reason: str) -> None:
        """未確定partialをabnormalとして残す。"""
        ...

"""カメラframe取得境界。"""

from __future__ import annotations

from collections.abc import Iterator
from threading import Event
from typing import Protocol

from raspi_recorder.domain.frames import FrameEnvelope


class CameraFrameSource(Protocol):
    """物理カメラ1台を唯一開き、JPEG frameを順次供給する。"""

    stats: CameraFrameSourceStats

    def frames(self, stop_event: Event) -> Iterator[FrameEnvelope]:
        """停止要求または入力終了までframeを返す。"""
        ...

    def request_stop(self) -> None:
        """ブロック中の読み取りがあれば割り込む。"""
        ...

    def close(self, timeout_seconds: float = 2.0) -> None:
        """sourceが所有する入力資源を期限内に解放する。"""
        ...


class CameraFrameSourceStats(Protocol):
    """engine snapshotに必要なカメラsource統計。"""

    dropped_frames: int
    last_error: str | None

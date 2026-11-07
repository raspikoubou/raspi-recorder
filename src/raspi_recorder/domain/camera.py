"""カメラ設定と録画状態のドメインモデル。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RecordingMode(StrEnum):
    """システム全体で排他的に選ぶ録画モード。"""

    CONTINUOUS = "continuous"


class Quality(StrEnum):
    """録画画質の段階。数値ビットレートは利用者へ公開しない。"""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class OverlayCorner(StrEnum):
    """日時表示の位置。"""

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class OverlaySize(StrEnum):
    """日時表示の文字サイズ。"""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class CameraBlocker(StrEnum):
    """カメラの自動再開を妨げる独立した原因。"""

    CAMERA_MISSING = "camera_missing"
    STORAGE_MISSING = "storage_missing"
    LOW_SPACE = "low_space"
    IO_ERROR = "io_error"
    INVALID_CONFIG = "invalid_config"
    OVERLOAD = "overload"


class CameraRunState(StrEnum):
    """利用者へ表示するカメラの実動作状態。"""

    STOPPED = "stopped"
    STARTING = "starting"
    CONTINUOUS = "continuous"
    FINALIZING = "finalizing"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CameraSettings:
    """カメラ別設定の初期候補。

    720p15fpsは初期候補であり、機器の実能力を確認するまで適用可能とは
    判定しない。
    """

    width: int = 1280
    height: int = 720
    fps: int = 15
    quality: Quality = Quality.STANDARD
    audio_enabled: bool = False
    overlay_enabled: bool = True
    overlay_corner: OverlayCorner = OverlayCorner.BOTTOM_LEFT
    overlay_size: OverlaySize = OverlaySize.MEDIUM
    usb_port_key: str | None = None
    video_interface_key: str | None = None
    audio_interface_key: str | None = None

    def __post_init__(self) -> None:
        """型と機器非依存の範囲を検査する。"""
        for name, value in (
            ("width", self.width),
            ("height", self.height),
            ("fps", self.fps),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}は正の整数で指定してください")
        if type(self.audio_enabled) is not bool:
            raise ValueError("audio_enabledはboolで指定してください")
        if type(self.overlay_enabled) is not bool:
            raise ValueError("overlay_enabledはboolで指定してください")
        if not isinstance(self.quality, Quality):
            raise ValueError("qualityが不正です")
        if not isinstance(self.overlay_corner, OverlayCorner):
            raise ValueError("overlay_cornerが不正です")
        if not isinstance(self.overlay_size, OverlaySize):
            raise ValueError("overlay_sizeが不正です")
        for name, binding_value in (
            ("usb_port_key", self.usb_port_key),
            ("video_interface_key", self.video_interface_key),
            ("audio_interface_key", self.audio_interface_key),
        ):
            if binding_value is not None and (
                not isinstance(binding_value, str) or not binding_value.strip()
            ):
                raise ValueError(
                    f"{name}は未割当または空でない文字列で指定してください"
                )
        if self.audio_interface_key is not None:
            audio_parts = self.audio_interface_key.split("/")
            if (
                len(audio_parts) != 4
                or audio_parts[0] != self.usb_port_key
                or audio_parts[2] != "capture"
                or not audio_parts[3].isdigit()
            ):
                raise ValueError(
                    "audio_interface_keyは同じUSBポートのcapture PCM keyが必要です"
                )


@dataclass(slots=True)
class CameraRuntime:
    """カメラ1台の希望状態と独立した停止原因を保持する。"""

    desired_running: bool = True
    blockers: set[CameraBlocker] = field(default_factory=set)
    capture_active: bool = False
    physical_file_writing: bool = False
    finalizing: bool = False
    fatal_error: bool = False

    def __post_init__(self) -> None:
        """誤ったAPI入力を早い段階で拒否する。"""
        if type(self.desired_running) is not bool:
            raise ValueError("desired_runningはboolで指定してください")
        for name in (
            "capture_active",
            "physical_file_writing",
            "finalizing",
            "fatal_error",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name}はboolで指定してください")
        if not all(isinstance(blocker, CameraBlocker) for blocker in self.blockers):
            raise ValueError("blockersに不正な値があります")

    @property
    def state(self) -> CameraRunState:
        """実際に開始済みの処理だけを稼働中として返す。"""
        if self.finalizing:
            return CameraRunState.FINALIZING
        if self.fatal_error:
            return CameraRunState.ERROR
        if not self.desired_running:
            return CameraRunState.STOPPED
        if self.blockers:
            return CameraRunState.BLOCKED
        if not self.capture_active:
            return CameraRunState.STARTING
        return (
            CameraRunState.CONTINUOUS
            if self.physical_file_writing
            else CameraRunState.STARTING
        )

    def add_blocker(self, blocker: CameraBlocker) -> None:
        """別の停止理由を消さずに追加する。"""
        if not isinstance(blocker, CameraBlocker):
            raise ValueError("blockerが不正です")
        self.blockers.add(blocker)

    def remove_blocker(self, blocker: CameraBlocker) -> None:
        """指定した停止理由だけを解除する。"""
        if not isinstance(blocker, CameraBlocker):
            raise ValueError("blockerが不正です")
        self.blockers.discard(blocker)

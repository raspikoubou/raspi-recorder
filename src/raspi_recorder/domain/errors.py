"""Application-visible concurrency and integrity errors."""

from __future__ import annotations


class RevisionConflict(RuntimeError):
    """期待revisionが保存済みrevisionと一致しない。"""

    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        super().__init__(
            f"設定revision競合: expected={expected_revision}, actual={actual_revision}"
        )
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class SettingsApplyError(RuntimeError):
    """設定保存後の起動失敗と復旧結果を操作journalへ伝える。"""

    def __init__(self, code: str, recovery: dict[str, object]) -> None:
        super().__init__(code)
        self.code = code
        self.recovery = recovery


class UnsupportedCameraProfileError(ValueError):
    """未対応profileはworker停止・設定保存の前に拒否する。"""

    code = "unsupported_camera_profile"

    def __init__(self, camera_id: int, revision: int) -> None:
        super().__init__(f"カメラ{camera_id}は指定した解像度・fpsに対応していません。")
        self.recovery = {"state": "previous_settings_unchanged", "revision": revision}


class ClockOperationError(RuntimeError):
    """OS時刻適用とcamera復旧の結果をoperation journalへ伝える。"""

    def __init__(self, code: str, recovery: dict[str, object]) -> None:
        super().__init__(code)
        self.code = code
        self.recovery = recovery

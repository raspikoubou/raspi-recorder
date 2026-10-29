"""Linux system clockとOS timezoneのadapter。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from raspi_recorder.ports.clock import Clock


class SystemClock(Clock):
    """UTC・boot内monotonic・Linux boot IDを読む。"""

    def __init__(self, boot_id_path: str | Path = "/proc/sys/kernel/random/boot_id"):
        self._boot_id_path = Path(boot_id_path)

    def utc_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def boot_id(self) -> str:
        try:
            value = self._boot_id_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RuntimeError("Linux boot IDを読み取れません") from exc
        if not value:
            raise RuntimeError("Linux boot IDが空です")
        return value


class SystemTimezone:
    """OS設定のIANA timezone名を取得する。"""

    def __init__(
        self,
        timezone_file: str | Path = "/etc/timezone",
        localtime_path: str | Path = "/etc/localtime",
    ) -> None:
        self._timezone_file = Path(timezone_file)
        self._localtime_path = Path(localtime_path)

    def name(self) -> str:
        """timezone名を返す。推定できない時にUTCへ黙って置換しない。"""
        environment_name = os.environ.get("TZ", "").lstrip(":")
        if environment_name:
            return environment_name
        try:
            configured_name = self._timezone_file.read_text(encoding="utf-8").strip()
        except OSError:
            configured_name = ""
        if configured_name:
            return configured_name
        try:
            resolved = self._localtime_path.resolve(strict=True)
            marker = "/zoneinfo/"
            resolved_text = str(resolved)
            if marker in resolved_text:
                return resolved_text.split(marker, 1)[1]
        except OSError:
            pass
        raise RuntimeError("OSのIANA timezone名を判定できません")

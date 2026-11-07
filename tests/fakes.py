"""決定的な単体・結合試験用Fake。製品ランタイムからimportしない。"""

from dataclasses import dataclass

from raspi_recorder.ports.clock import ClockSetError, ClockSyncStatus


@dataclass(slots=True)
class FakeClock:
    """wall clockとmonotonic clockを独立して進めるテスト用時計。"""

    wall_utc_ns: int = 0
    elapsed_mono_ns: int = 0
    current_boot_id: str = "boot-test-1"

    def utc_ns(self) -> int:
        """現在のFake UTCを返す。"""
        return self.wall_utc_ns

    def monotonic_ns(self) -> int:
        """現在のboot内経過時刻を返す。"""
        return self.elapsed_mono_ns

    def boot_id(self) -> str:
        """現在のFake boot IDを返す。"""
        return self.current_boot_id

    def advance(self, elapsed_ns: int, wall_adjustment_ns: int = 0) -> None:
        """経過時間を進め、任意のwall clock補正を加える。"""
        if type(elapsed_ns) is not int or elapsed_ns < 0:
            raise ValueError("elapsed_nsは0以上の整数で指定してください")
        if type(wall_adjustment_ns) is not int:
            raise ValueError("wall_adjustment_nsは整数で指定してください")
        self.elapsed_mono_ns += elapsed_ns
        self.wall_utc_ns += elapsed_ns + wall_adjustment_ns

    def reboot(self, boot_id: str) -> None:
        """Pi再起動を模してmonotonicをリセットし、boot IDを更新する。"""
        if not boot_id or boot_id == self.current_boot_id:
            raise ValueError("新しいboot_idを指定してください")
        self.current_boot_id = boot_id
        self.elapsed_mono_ns = 0


class FakeClockSetter:
    """特権helperを使わず、OS時刻変更の順序を検証するtest fake。"""

    def __init__(
        self,
        clock: FakeClock,
        *,
        timezone: str = "Asia/Tokyo",
        ntp_enabled: bool | None = False,
        synchronized: bool | None = False,
    ) -> None:
        self.clock = clock
        self.timezone = timezone
        self.ntp_enabled = ntp_enabled
        self.synchronized = synchronized
        self.calls: list[tuple[int, str]] = []
        self.failure: ClockSetError | None = None

    def status(self) -> ClockSyncStatus:
        return ClockSyncStatus(self.timezone, self.ntp_enabled, self.synchronized)

    def set_utc_ns(self, utc_ns: int, ntp_policy: str) -> None:
        self.calls.append((utc_ns, ntp_policy))
        if self.failure is not None:
            error = self.failure
            self.failure = None
            if error.changed:
                self.clock.wall_utc_ns = utc_ns
            raise error
        self.clock.wall_utc_ns = utc_ns

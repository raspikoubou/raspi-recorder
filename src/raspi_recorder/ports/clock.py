"""wall clockとmonotonic clockを分けた時刻ポート。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ClockSyncStatus:
    """OS時刻同期サービスから読み取った状態。"""

    timezone: str | None
    ntp_enabled: bool | None
    synchronized: bool | None


class ClockSetError(RuntimeError):
    """特権clock helperの失敗と、適用済みか不明な状態を伝える。"""

    def __init__(
        self,
        code: str,
        *,
        changed: bool = False,
        ntp_state_uncertain: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.changed = changed
        self.ntp_state_uncertain = ntp_state_uncertain


class ClockSetter(Protocol):
    """最小権限helperを介してOS時刻を照会・変更するport。"""

    def status(self) -> ClockSyncStatus:
        """timezone・NTP有効・同期確認を返す。不明値はNone。"""
        ...

    def set_utc_ns(self, utc_ns: int, ntp_policy: str) -> None:
        """指定UTCへ設定する。NTP扱いはkeep/pause_and_resumeのみ。"""
        ...


class Clock(Protocol):
    """OS時刻に依存する処理へ注入する時計。"""

    def utc_ns(self) -> int:
        """現在UTC時刻をepochからの整数nsで返す。"""
        ...

    def monotonic_ns(self) -> int:
        """現在boot内の経過時刻を整数nsで返す。"""
        ...

    def boot_id(self) -> str:
        """現在のOS bootを識別する値を返す。"""
        ...

"""systemd timedatedだけを操作するsocket-activated特権helper。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from raspi_recorder.ports.clock import ClockSetError, ClockSyncStatus

_MAX_REQUEST_BYTES = 4096
_TIMEDATECTL = "/usr/bin/timedatectl"
_BUSCTL = "/usr/bin/busctl"
_TIMEDATE_SERVICE = "org.freedesktop.timedate1"
_TIMEDATE_PATH = "/org/freedesktop/timedate1"
_TIMEDATE_INTERFACE = "org.freedesktop.timedate1"
_MIN_UTC_NS = 0
_MAX_UTC_NS = (2**63 - 1) * 1000
# client(SystemClockSetter)のset上限90秒以内に収めるための全体予算。
# 個別command上限(8秒)とNTP遷移待ち(15秒)を失敗復旧経路まで積み上げても
# この予算を超えないよう、set処理中は全command/待機をこのdeadlineで抑える。
_SET_PATH_BUDGET_SECONDS = 75.0


class TimedateHelper:
    """固定argvのsystemd timedated API以外を呼び出さない。"""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        utc_now_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ntp_transition_timeout_seconds: float = 15.0,
    ) -> None:
        if ntp_transition_timeout_seconds <= 0:
            raise ValueError("NTP transition timeout must be positive")
        self._runner = runner
        self._utc_now_ns = utc_now_ns
        self._monotonic = monotonic
        self._sleep = sleep
        self._ntp_transition_timeout = ntp_transition_timeout_seconds
        self._operation_deadline: float | None = None

    def status(self) -> ClockSyncStatus:
        return self._read_status(timeout_seconds=8)

    def _read_status(self, *, timeout_seconds: float) -> ClockSyncStatus:
        result = self._run(
            [
                _TIMEDATECTL,
                "show",
                "--no-pager",
                "--property=Timezone",
                "--property=NTP",
                "--property=NTPSynchronized",
            ],
            timeout_seconds=timeout_seconds,
        )
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in {"Timezone", "NTP", "NTPSynchronized"}:
                raise RuntimeError("timedate_status_invalid")
            if key in values:
                raise RuntimeError("timedate_status_duplicate")
            values[key] = value
        timezone = values.get("Timezone") or None
        return ClockSyncStatus(
            timezone=timezone,
            ntp_enabled=_parse_yes_no(values.get("NTP")),
            synchronized=_parse_yes_no(values.get("NTPSynchronized")),
        )

    def set_utc_ns(self, utc_ns: int, ntp_policy: str) -> None:
        if type(utc_ns) is not int or not _MIN_UTC_NS <= utc_ns <= _MAX_UTC_NS:
            raise ClockSetError("invalid_utc_ns")
        if ntp_policy not in {"keep", "pause_and_resume"}:
            raise ClockSetError("invalid_ntp_policy")
        self._operation_deadline = self._monotonic() + _SET_PATH_BUDGET_SECONDS
        try:
            self._set_utc_ns_within_budget(utc_ns, ntp_policy)
        finally:
            self._operation_deadline = None

    def _set_utc_ns_within_budget(self, utc_ns: int, ntp_policy: str) -> None:
        before = self.status()
        if before.ntp_enabled is None:
            # A failed/unknown status read cannot establish that SetTime is
            # allowed. Fail before attempting a privileged clock mutation.
            raise ClockSetError("ntp_state_unknown", ntp_state_uncertain=True)
        if ntp_policy == "pause_and_resume" and before.ntp_enabled is not True:
            raise ClockSetError("ntp_not_enabled_for_pause_resume")
        if ntp_policy == "keep" and before.ntp_enabled is True:
            # timedated deliberately refuses SetTime while any configured NTP
            # service is active. Avoid finalizing recordings for a request that
            # the supported system API is certain to reject.
            raise ClockSetError("ntp_active_time_change_rejected")

        previous_utc_ns = self._utc_now_ns()
        paused = ntp_policy == "pause_and_resume"
        if paused:
            try:
                self._change_ntp(False)
            except Exception as exc:
                ntp_restored = self._restore_ntp_enabled()
                raise ClockSetError(
                    "ntp_pause_failed",
                    ntp_state_uncertain=not ntp_restored,
                ) from exc
        try:
            self._set_time(utc_ns)
        except Exception as exc:
            ntp_restored = not paused or self._restore_ntp_enabled()
            code = exc.code if isinstance(exc, ClockSetError) else "clock_set_failed"
            raise ClockSetError(
                code,
                changed=(isinstance(exc, ClockSetError) and exc.changed),
                ntp_state_uncertain=not ntp_restored,
            ) from exc

        if not paused:
            return
        try:
            self._change_ntp(True)
        except Exception as exc:
            if self._restore_ntp_enabled():
                return

            restored_clock = self._restore_clock_and_ntp(previous_utc_ns)
            ntp_restored = self._is_ntp_enabled()
            raise ClockSetError(
                (
                    "ntp_resume_failed_clock_restored"
                    if restored_clock
                    else "ntp_resume_failed"
                ),
                changed=not restored_clock or not ntp_restored,
                ntp_state_uncertain=not ntp_restored,
            ) from exc

    def _change_ntp(self, enabled: bool) -> None:
        self._set_ntp(enabled)
        self._wait_for_ntp(enabled)

    def _wait_for_ntp(self, enabled: bool) -> None:
        deadline = self._monotonic() + self._ntp_transition_timeout
        if self._operation_deadline is not None:
            deadline = min(deadline, self._operation_deadline)
        while True:
            status = self._read_status(timeout_seconds=2)
            if status.ntp_enabled is enabled:
                return
            if status.ntp_enabled is None:
                raise ClockSetError("ntp_state_unknown", ntp_state_uncertain=True)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ClockSetError("ntp_transition_timeout", ntp_state_uncertain=True)
            self._sleep(min(0.25, remaining))

    def _restore_ntp_enabled(self) -> bool:
        try:
            status = self._read_status(timeout_seconds=2)
            if status.ntp_enabled is not True:
                self._change_ntp(True)
            return self._is_ntp_enabled()
        except Exception:
            return False

    def _is_ntp_enabled(self) -> bool:
        try:
            return self._read_status(timeout_seconds=2).ntp_enabled is True
        except Exception:
            return False

    def _restore_clock_and_ntp(self, previous_utc_ns: int) -> bool:
        """時計を戻す必要があるときだけNTPを停止してから復帰する。"""
        try:
            status = self._read_status(timeout_seconds=2)
            if status.ntp_enabled is not False:
                self._change_ntp(False)
            self._set_time(previous_utc_ns)
            self._change_ntp(True)
            return True
        except Exception:
            self._restore_ntp_enabled()
            return False

    def _set_time(self, utc_ns: int) -> None:
        usec_utc = utc_ns // 1000
        if usec_utc > 2**63 - 1:
            raise ClockSetError("invalid_utc_ns")
        self._run(
            [
                _BUSCTL,
                "--system",
                "call",
                _TIMEDATE_SERVICE,
                _TIMEDATE_PATH,
                _TIMEDATE_INTERFACE,
                "SetTime",
                "xbb",
                str(usec_utc),
                "false",
                "false",
            ],
            may_change_clock=True,
        )

    def _set_ntp(self, enabled: bool) -> None:
        self._run(
            [
                _BUSCTL,
                "--system",
                "call",
                _TIMEDATE_SERVICE,
                _TIMEDATE_PATH,
                _TIMEDATE_INTERFACE,
                "SetNTP",
                "bb",
                "true" if enabled else "false",
                "false",
            ]
        )

    def _run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float = 8,
        may_change_clock: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        effective_timeout = timeout_seconds
        if self._operation_deadline is not None:
            remaining = self._operation_deadline - self._monotonic()
            effective_timeout = min(effective_timeout, max(remaining, 0.05))
        try:
            return self._runner(
                list(arguments),
                check=True,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if (
                isinstance(exc, subprocess.CalledProcessError)
                and arguments[0] == _BUSCTL
                and len(arguments) > 6
                and arguments[6] == "SetTime"
                and "org.freedesktop.timedate1.AutomaticTimeSyncEnabled"
                in str(exc.stderr)
            ):
                raise ClockSetError("ntp_active_time_change_rejected") from exc
            raise ClockSetError(
                "timedate_service_failed",
                changed=may_change_clock and isinstance(exc, subprocess.TimeoutExpired),
            ) from exc


def handle_request(payload: object, helper: TimedateHelper) -> dict[str, Any]:
    """厳格な1 requestを処理する。未知action/fieldを拒否する。"""
    if not isinstance(payload, dict):
        return _error("invalid_request")
    action = payload.get("action")
    if action == "status" and set(payload) == {"action"}:
        try:
            status = helper.status()
        except Exception:
            return _error("clock_status_unavailable")
        return {
            "ok": True,
            "status": {
                "timezone": status.timezone,
                "ntp_enabled": status.ntp_enabled,
                "synchronized": status.synchronized,
            },
        }
    if action != "set" or set(payload) != {"action", "utc_ns", "ntp_policy"}:
        return _error("invalid_request")
    utc_ns = payload["utc_ns"]
    policy = payload["ntp_policy"]
    if type(utc_ns) is not int or not isinstance(policy, str):
        return _error("invalid_request")
    try:
        helper.set_utc_ns(utc_ns, policy)
    except ClockSetError as exc:
        return _error(
            exc.code,
            changed=exc.changed,
            ntp_state_uncertain=exc.ntp_state_uncertain,
        )
    except Exception:
        return _error("clock_set_failed")
    return {"ok": True}


def main() -> int:
    """socket-activated serviceはstdin/outの接続ごとに1 JSON-lineを扱う。"""
    request_line = sys.stdin.buffer.readline(_MAX_REQUEST_BYTES + 1)
    if not request_line.endswith(b"\n") or len(request_line) > _MAX_REQUEST_BYTES:
        response = _error("invalid_request_size")
    else:
        try:
            payload = json.loads(
                request_line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("invalid_json_constant")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            response = _error("invalid_json")
        else:
            response = handle_request(payload, TimedateHelper())
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def _parse_yes_no(value: str | None) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _error(
    code: str,
    *,
    changed: bool = False,
    ntp_state_uncertain: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "changed": changed,
            "ntp_state_uncertain": ntp_state_uncertain,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())

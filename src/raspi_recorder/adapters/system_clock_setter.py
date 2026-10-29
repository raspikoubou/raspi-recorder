"""狭いUnix socketでOS時刻helperへ依頼するadapter。"""

from __future__ import annotations

import json
import math
import socket
import time
from pathlib import Path
from typing import Any

from raspi_recorder.adapters.socket_io import receive_line, set_deadline_timeout
from raspi_recorder.ports.clock import ClockSetError, ClockSetter, ClockSyncStatus

_MAX_LINE_BYTES = 4096
_DEFAULT_SOCKET = "/run/raspi-recorder-clock/helper.sock"


class SystemClockSetter(ClockSetter):
    """一般engineから特権時刻変更helperを呼び出す。"""

    def __init__(
        self,
        socket_path: str | Path = _DEFAULT_SOCKET,
        *,
        timeout_seconds: float = 90.0,
        # helper側status commandの上限は8秒(clock_helper._read_status)。
        # それより短いと正常な応答を取りこぼすため、上回る値を既定にする。
        status_timeout_seconds: float = 10.0,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_secondsは0より大きい数値が必要です")
        if (
            type(status_timeout_seconds) not in (int, float)
            or not math.isfinite(status_timeout_seconds)
            or status_timeout_seconds <= 0
        ):
            raise ValueError("status_timeout_secondsは0より大きい数値が必要です")
        self._socket_path = Path(socket_path)
        self._timeout = float(timeout_seconds)
        self._status_timeout = min(self._timeout, float(status_timeout_seconds))

    def status(self) -> ClockSyncStatus:
        response = self._request(
            {"action": "status"}, timeout_seconds=self._status_timeout
        )
        if set(response) != {"ok", "status"} or response["ok"] is not True:
            raise ClockSetError("clock_helper_invalid_response")
        status = response["status"]
        if not isinstance(status, dict) or set(status) != {
            "timezone",
            "ntp_enabled",
            "synchronized",
        }:
            raise ClockSetError("clock_helper_invalid_status")
        timezone = status["timezone"]
        if timezone is not None and not isinstance(timezone, str):
            raise ClockSetError("clock_helper_invalid_timezone")
        ntp_enabled = _optional_bool(status["ntp_enabled"])
        synchronized = _optional_bool(status["synchronized"])
        return ClockSyncStatus(timezone, ntp_enabled, synchronized)

    def set_utc_ns(self, utc_ns: int, ntp_policy: str) -> None:
        if type(utc_ns) is not int or utc_ns < 0:
            raise ValueError("utc_nsは0以上の整数が必要です")
        if ntp_policy not in {"keep", "pause_and_resume"}:
            raise ValueError("ntp_policyが不正です")
        response = self._request(
            {"action": "set", "utc_ns": utc_ns, "ntp_policy": ntp_policy}
        )
        if set(response) != {"ok"} or response["ok"] is not True:
            error = response.get("error")
            code_value = error.get("code") if isinstance(error, dict) else None
            code = (
                code_value
                if isinstance(code_value, str)
                else "clock_helper_invalid_response"
            )
            changed_value = error.get("changed") if isinstance(error, dict) else None
            changed = changed_value if type(changed_value) is bool else True
            ntp_uncertain_value = (
                error.get("ntp_state_uncertain") if isinstance(error, dict) else False
            )
            ntp_uncertain = (
                ntp_uncertain_value if type(ntp_uncertain_value) is bool else True
            )
            raise ClockSetError(
                code, changed=changed, ntp_state_uncertain=ntp_uncertain
            )

    def _request(
        self, payload: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode(
            "ascii"
        )
        request_may_have_reached = False
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self._socket_path))
                set_deadline_timeout(client, deadline)
                request_may_have_reached = True
                client.sendall(encoded + b"\n")
                line = receive_line(client, _MAX_LINE_BYTES, deadline=deadline)
        except (OSError, TimeoutError) as exc:
            is_set = payload.get("action") == "set"
            ntp_policy = payload.get("ntp_policy")
            raise ClockSetError(
                "clock_helper_unavailable",
                changed=is_set and request_may_have_reached,
                ntp_state_uncertain=(
                    is_set
                    and request_may_have_reached
                    and ntp_policy == "pause_and_resume"
                ),
            ) from exc
        if not line.endswith(b"\n") or len(line) > _MAX_LINE_BYTES:
            raise ClockSetError(
                "clock_helper_invalid_response",
                changed=payload.get("action") == "set",
                ntp_state_uncertain=(
                    payload.get("action") == "set"
                    and payload.get("ntp_policy") == "pause_and_resume"
                ),
            )
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClockSetError(
                "clock_helper_invalid_response",
                changed=payload.get("action") == "set",
                ntp_state_uncertain=(
                    payload.get("action") == "set"
                    and payload.get("ntp_policy") == "pause_and_resume"
                ),
            ) from exc
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise ClockSetError(
                "clock_helper_invalid_response",
                changed=payload.get("action") == "set",
                ntp_state_uncertain=(
                    payload.get("action") == "set"
                    and payload.get("ntp_policy") == "pause_and_resume"
                ),
            )
        return response


def _optional_bool(value: object) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise ClockSetError("clock_helper_invalid_status")

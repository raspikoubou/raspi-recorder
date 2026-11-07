"""Unprivileged client for the fixed, root-owned update helper socket."""

from __future__ import annotations

import json
import math
import re
import socket
import time
from pathlib import Path

from raspi_recorder.adapters.socket_io import receive_line, set_deadline_timeout

_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_REQUEST_BYTES = 4096
_MAX_RESPONSE_BYTES = 4096


class UpdateHelperClientError(RuntimeError):
    """A helper rejection or an uncertain handoff to the root service."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class UpdateHelperClient:
    """Submit one immutable, bounded release request over AF_UNIX."""

    def __init__(
        self,
        socket_path: str | Path = "/run/raspi-recorder-update/helper.sock",
        *,
        timeout_seconds: float = 180.0,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise ValueError("timeout_seconds must be between 0 and 3600")
        self._socket_path = Path(socket_path)
        self._timeout = float(timeout_seconds)

    def apply(self, *, operation_id: str, version: str, asset_sha256: str) -> None:
        """Return after the privileged helper durably accepts the request."""
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            raise ValueError("operation_id is invalid")
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError("version is invalid")
        if not isinstance(asset_sha256, str) or not _SHA256.fullmatch(asset_sha256):
            raise ValueError("asset digest is invalid")
        payload = (
            json.dumps(
                {
                    "action": "apply",
                    "operation_id": operation_id,
                    "version": version,
                    "asset_sha256": asset_sha256,
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        if len(payload) > _MAX_REQUEST_BYTES:
            raise ValueError("helper request exceeds the limit")

        sent = False
        deadline = time.monotonic() + self._timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(str(self._socket_path))
                set_deadline_timeout(client, deadline)
                sent = True
                client.sendall(payload)
                response = receive_line(client, _MAX_RESPONSE_BYTES, deadline=deadline)
        except (OSError, TimeoutError) as exc:
            raise UpdateHelperClientError(
                "update_helper_handoff_uncertain"
                if sent
                else "update_helper_unavailable",
                uncertain=sent,
            ) from exc

        if not response.endswith(b"\n") or len(response) > _MAX_RESPONSE_BYTES:
            raise UpdateHelperClientError(
                "update_helper_response_invalid", uncertain=True
            )
        try:
            value = json.loads(response.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateHelperClientError(
                "update_helper_response_invalid", uncertain=True
            ) from exc
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise UpdateHelperClientError(
                "update_helper_response_invalid", uncertain=True
            )
        if value["ok"] is True and value.get("accepted") is True:
            if set(value) != {"ok", "accepted"}:
                raise UpdateHelperClientError(
                    "update_helper_response_invalid", uncertain=True
                )
            return
        if (
            value.get("ok") is False
            and isinstance(value.get("error"), str)
            and type(value.get("uncertain", False)) is bool
            and set(value).issubset({"ok", "error", "uncertain"})
        ):
            raise UpdateHelperClientError(
                value["error"], uncertain=value.get("uncertain", False)
            )
        raise UpdateHelperClientError("update_helper_response_invalid", uncertain=True)

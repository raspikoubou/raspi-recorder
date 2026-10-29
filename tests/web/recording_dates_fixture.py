"""Synthetic date-navigation API for manual browser regressions.

Run with --media pointing to a synthetic MP4. No Pi or production DB is read.
Dates: 26/25 recorded, 24/27 empty, 23 fails once, 22 slow, 21 paginated.
"""

from __future__ import annotations

import argparse
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from werkzeug.serving import make_server

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.adapters.web_app import create_app
from raspi_recorder.adapters.werkzeug_password_hasher import WerkzeugPasswordHasher
from raspi_recorder.application.authentication import AuthenticationService
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.ports.engine import EngineResponse

ZONE = ZoneInfo("Asia/Tokyo")


def clip(day: str, camera: int = 1, minute: int = 0) -> dict:
    start = (
        int(
            datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=ZONE).timestamp()
            * 1e9
        )
        + minute * 60_000_000_000
    )
    return {
        "recording_id": f"synthetic-{day}-cam{camera}-{minute}",
        "camera_id": camera,
        "part_index": minute + 1,
        "state": "finalized",
        "start_utc_ns": start,
        "end_utc_ns": start + 10_000_000_000,
    }


class DateFixture:
    def __init__(self, media: Path) -> None:
        self.media = media
        self.failed_once = False
        self.lock = threading.Lock()

    def request(self, method: str, path: str, body=None) -> EngineResponse:
        url = urlsplit(path)
        query = parse_qs(url.query)
        if url.path == "/api/v1/status":
            return EngineResponse(
                200,
                {
                    "revision": 0,
                    "cameras": [],
                    "service": "running",
                    "volume": None,
                    "time": {
                        "timezone": "Asia/Tokyo",
                        "utc_ns": clip("2026-10-26")["start_utc_ns"],
                    },
                },
            )
        if url.path == "/api/v1/settings":
            return EngineResponse(200, {"settings": ApplicationSettings().to_payload()})
        if url.path == "/api/v1/capabilities":
            return EngineResponse(200, {"volumes": [], "cameras": []})
        if url.path.endswith("/preview/demand"):
            return EngineResponse(200, {"accepted": True})
        if url.path == "/api/v1/recordings":
            if "from_utc_ns" not in query:
                items = [clip("2026-10-26")]
            else:
                day = (
                    datetime.fromtimestamp(int(query["from_utc_ns"][0]) / 1e9, ZONE)
                    .date()
                    .isoformat()
                )
                if day == "2026-10-22":
                    time.sleep(2)
                if day == "2026-10-23":
                    with self.lock:
                        fail = not self.failed_once
                        self.failed_once = True
                    if fail:
                        return EngineResponse(
                            503, {"error": {"message": "テスト用の一時的な通信エラー"}}
                        )
                if day == "2026-10-21":
                    if "before_sequence" not in query:
                        return EngineResponse(
                            200,
                            {
                                "items": [
                                    clip(day, minute=n) for n in range(500, 0, -1)
                                ],
                                "next_before_sequence": 2,
                            },
                        )
                    items = [clip(day)]
                elif day in {"2026-10-24", "2026-10-27"}:
                    items = []
                else:
                    items = [clip(day)]
                    if day == "2026-10-25":
                        items.append(clip(day, camera=2))
            return EngineResponse(200, {"items": items, "next_before_sequence": None})
        return EngineResponse(404, {"error": {"code": "not_found"}})

    def stream(self, method: str, path: str, range_header=None):
        if path.endswith(("/content", "/download")):
            payload = self.media.read_bytes()
            headers = {"Content-Type": "video/mp4", "Content-Length": str(len(payload))}
            if path.endswith("/download"):
                headers["Content-Disposition"] = 'attachment; filename="synthetic.mp4"'
            return SimpleNamespace(
                status_code=200,
                headers=headers,
                chunks=lambda: iter([payload]),
                close=lambda: None,
            )
        return EngineResponse(404, {"error": {"code": "not_found"}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args()
    assert args.media.is_file()
    with tempfile.TemporaryDirectory(prefix="recorder-date-fixture-") as scratch:
        auth = AuthenticationService(
            AuthenticationStore(Path(scratch) / "auth.sqlite3"),
            WerkzeugPasswordHasher(),
            SystemClock(),
        )
        auth.initialize()
        app = create_app(auth, DateFixture(args.media), trusted_hosts=("localhost",))
        server = make_server("127.0.0.1", args.port, app, threaded=True)
        print(
            f"Synthetic date regression fixture: http://localhost:{args.port}",
            flush=True,
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()

"""Manual Chrome fault scenarios using the production Web adapter and UI.

Run: PYTHONPATH=src:. python -m tests.web.interaction_fixture --port 18084
The landing page links select isolated, synthetic states. Never contacts a Pi.
For status-offline, open normal in a second tab to restore the first tab's API.
"""

from __future__ import annotations

import argparse
import copy
import tempfile
import threading
import time
from pathlib import Path

from flask import make_response, redirect, request
from werkzeug.serving import make_server

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.adapters.web_app import create_app
from raspi_recorder.adapters.werkzeug_password_hasher import WerkzeugPasswordHasher
from raspi_recorder.application.authentication import AuthenticationService
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.ports.engine import EngineResponse

SCENARIOS = (
    "normal",
    "missing",
    "no-profile",
    "time-error",
    "update-error",
    "settings-error",
    "save-delay",
    "save-fail",
    "save-conflict",
    "clock-delay",
    "clock-read-delay",
    "live-delay",
    "backlog",
    "update-delay",
    "update-fail",
    "update-status-error",
    "status-offline",
    "auth-error",
    "logout-guard",
)


class InteractionFixture:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.lock = threading.Lock()

    def state(self, scenario: str) -> dict:
        with self.lock:
            if scenario not in self.states:
                settings = ApplicationSettings().to_payload()
                settings["cameras"][0].update(
                    usb_port_key="synthetic-camera",
                    video_interface_key="synthetic-video",
                )
                if scenario == "missing":
                    settings["volume"] = {
                        "kind": "USB",
                        "persistent_id": "synthetic-usb",
                    }
                self.states[scenario] = {
                    "settings": settings,
                    "requests": [],
                    "failed_once": False,
                    "operation_started": 0.0,
                    "pending": None,
                    "stopped": False,
                    "started": time.monotonic(),
                }
            return self.states[scenario]

    def request(self, method: str, path: str, body=None) -> EngineResponse:
        scenario = request.cookies.get("fixture_scenario", "normal")
        state = self.state(scenario)
        state["requests"].append([method, path, body])
        clock = {
            "utc_ns": 1792992000000000000,
            "timezone": "Asia/Tokyo",
            "helper_timezone": "Asia/Tokyo",
            "helper_available": True,
            "ntp_enabled": True,
            "synchronized": True,
        }
        fail_path = {
            "time-error": "/api/v1/time/status",
            "update-error": "/api/v1/update/status",
            "settings-error": "/api/v1/settings",
        }.get(scenario)
        settings_reads = sum(
            item[:2] == ["GET", "/api/v1/settings"] for item in state["requests"]
        )
        fail_now = scenario != "settings-error" or settings_reads >= 2
        if (
            method == "GET"
            and path == fail_path
            and fail_now
            and not state["failed_once"]
        ):
            state["failed_once"] = True
            return EngineResponse(
                503, {"error": {"message": "テスト用の一時的な通信エラー"}}
            )
        if path == "/api/v1/settings" and method == "GET":
            return EngineResponse(200, {"settings": copy.deepcopy(state["settings"])})
        if path == "/api/v1/capabilities":
            return EngineResponse(
                200,
                {
                    "volumes": []
                    if scenario == "missing"
                    else [
                        {
                            "kind": "microSD",
                            "persistent_id": "fixture-sd",
                            "filesystem": "ext4",
                            "total_bytes": 32_000_000_000,
                            "available_bytes": 20_000_000_000,
                        }
                    ],
                    "cameras": []
                    if scenario == "missing"
                    else [
                        {
                            "port_key": "synthetic-camera",
                            "video_interface_key": "synthetic-video",
                            "product": "Synthetic camera",
                            "profiles": [
                                {"width": 640, "height": 480, "fps": [10]},
                                {"width": 1280, "height": 720, "fps": [15]},
                            ],
                            "audio_interfaces": [],
                        }
                    ]
                    + (
                        [
                            {
                                "port_key": "unusable-camera",
                                "product": "No profile camera",
                                "profiles": [],
                            }
                        ]
                        if scenario == "no-profile"
                        else []
                    ),
                },
            )
        if path == "/api/v1/time/status":
            reads = sum(item[:2] == ["GET", path] for item in state["requests"])
            if scenario == "clock-read-delay" and reads >= 2:
                time.sleep(8)
            return EngineResponse(200, clock)
        if path == "/api/v1/update/status":
            reads = sum(item[:2] == ["GET", path] for item in state["requests"])
            if scenario == "update-status-error" and reads > 1:
                return EngineResponse(503, {"error": {"code": "engine_unavailable"}})
            if scenario.startswith("update-") and scenario != "update-error":
                return EngineResponse(
                    200,
                    {
                        "current_version": "0.1.34",
                        "source_configured": True,
                        "candidate": {"version": "0.1.35", "enough_space": True},
                    },
                )
            return EngineResponse(
                200, {"current_version": "0.1.34", "source_configured": False}
            )
        if path == "/api/v1/status":
            reads = sum(item[:2] == ["GET", path] for item in state["requests"])
            if scenario == "status-offline" and reads >= 3:
                return EngineResponse(503, {"error": {"code": "engine_unavailable"}})
            return EngineResponse(
                200,
                {
                    "revision": state["settings"]["revision"],
                    "time": clock,
                    "volume": None,
                    "cameras": [
                        {
                            "camera_id": 1,
                            "state": "stopped" if state["stopped"] else "continuous",
                            "desired_running": not state["stopped"],
                            "recording_active": not state["stopped"],
                            "audio": {"enabled": False},
                            "recording_delay_ns": 12_000_000_000
                            if scenario == "backlog"
                            and time.monotonic() - state["started"] < 12
                            else 0,
                        }
                    ],
                },
            )
        if method == "PATCH" and path == "/api/v1/settings":
            if scenario == "save-conflict":
                return EngineResponse(409, {"error": {"code": "revision_conflict"}})
            state["pending"] = copy.deepcopy(body["settings"])
            state["operation_started"] = time.monotonic()
            return EngineResponse(202, {"operation_id": "a" * 32})
        if method == "POST" and path == "/api/v1/time":
            state["operation_started"] = time.monotonic()
            return EngineResponse(202, {"operation_id": "b" * 32})
        if method == "POST" and path in {
            "/api/v1/update/check",
            "/api/v1/update/apply",
        }:
            state["update_started"] = time.monotonic()
            state["update_kind"] = "check" if path.endswith("check") else "apply"
            return EngineResponse(202, {"operation_id": "c" * 32})
        if path.startswith("/api/v1/operations/"):
            if path.endswith("c" * 32) and "update_kind" in state:
                if time.monotonic() - state["update_started"] < 10:
                    return EngineResponse(
                        200, {"state": "running", "stage": "validating"}
                    )
                if scenario == "update-fail":
                    return EngineResponse(
                        200, {"state": "failed", "error": "GitHubReleaseError"}
                    )
                return EngineResponse(
                    200,
                    {
                        "state": "succeeded",
                        "result": {
                            "scope": "update_check"
                            if state["update_kind"] == "check"
                            else "update",
                            "version": "0.1.35",
                        },
                    },
                )
            if (
                scenario in {"save-delay", "clock-delay"}
                and time.monotonic() - state["operation_started"] < 12
            ):
                return EngineResponse(200, {"state": "applying"})
            if scenario == "save-fail":
                return EngineResponse(
                    200,
                    {
                        "state": "failed",
                        "error": "unsupported_camera_profile",
                        "recovery": {"state": "previous_settings_unchanged"},
                    },
                )
            if state["pending"]:
                state["settings"] = state.pop("pending")
                state["pending"] = None
                state["settings"]["revision"] += 1
            return EngineResponse(
                200,
                {
                    "state": "succeeded",
                    "result": {"revision": state["settings"]["revision"]},
                },
            )
        if path in {"/api/v1/cameras/1/stop", "/api/v1/cameras/1/start"}:
            if scenario == "live-delay":
                time.sleep(8)
            state["stopped"] = path.endswith("stop")
            return EngineResponse(200, {"accepted": True})
        if path.endswith("/demand"):
            return EngineResponse(200, {"accepted": True})
        if path.startswith("/api/v1/recordings"):
            return EngineResponse(200, {"items": [], "next_before_sequence": None})
        return EngineResponse(404, {"error": {"code": "not_found"}})

    def stream(self, method, path, *, range_header=None):
        return EngineResponse(404, {"error": {"code": "not_found"}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18084)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="recorder-interaction-fixture-") as scratch:
        auth = AuthenticationService(
            AuthenticationStore(Path(scratch) / "auth.sqlite3"),
            WerkzeugPasswordHasher(),
            SystemClock(),
        )
        auth.initialize()
        fixture = InteractionFixture()
        app = create_app(auth, fixture, trusted_hosts=("localhost",))

        @app.before_request
        def auth_status_failure():
            if (
                request.path == "/api/v1/auth/status"
                and request.cookies.get("fixture_scenario") == "auth-error"
            ):
                state = fixture.state("auth-error")
                if not state["failed_once"]:
                    state["failed_once"] = True
                    return {"error": {"code": "engine_unavailable"}}, 503
            return None

        @app.after_request
        def synthetic_logout_header(response):
            # Display a logout button without creating any real credentials.
            if (
                request.path == "/api/v1/auth/status"
                and request.cookies.get("fixture_scenario") == "logout-guard"
                and response.status_code == 200
            ):
                status = response.get_json()
                status["login_protection_enabled"] = True
                status["authenticated"] = True
                response.set_data(app.json.dumps(status))
            return response

        @app.get("/fixture")
        def scenarios():
            return "<h1>Synthetic interaction scenarios</h1>" + "".join(
                f'<p><a href="/fixture/{name}">{name}</a></p>' for name in SCENARIOS
            )

        @app.get("/fixture/<name>")
        def scenario(name):
            if name not in SCENARIOS:
                return "unknown", 404
            fixture.states.pop(name, None)
            response = make_response(redirect("/"))
            response.set_cookie(
                "fixture_scenario", name, httponly=True, samesite="Strict"
            )
            return response

        server = make_server("127.0.0.1", args.port, app, threaded=True)
        print(
            f"Synthetic interaction fixture: http://localhost:{args.port}/fixture",
            flush=True,
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()

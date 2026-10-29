from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.web_app import _host_allowed, create_app
from raspi_recorder.application.authentication import AuthenticationService
from raspi_recorder.ports.engine import EngineResponse
from tests.fakes import FakeClock


class FakeHasher:
    def hash(self, password: str) -> str:
        return "scrypt:test$" + password

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.stream_calls: list[tuple[str, str, str | None]] = []

    def request(self, method: str, path: str, body=None) -> EngineResponse:
        self.calls.append((method, path, body))
        if path == "/api/v1/exports":
            return EngineResponse(202, {"export_id": "export-a", "state": "queued"})
        return EngineResponse(
            200, {"mode": "continuous", "revision": 12, "cameras": []}
        )

    def stream(self, method, path, *, range_header=None):
        self.stream_calls.append((method, path, range_header))
        return EngineResponse(404, {"error": {"code": "not_found"}})


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.store = AuthenticationStore(Path(self.temp_dir.name) / "auth.db")
        self.auth = AuthenticationService(self.store, FakeHasher(), self.clock)
        self.auth.initialize()
        self.engine = FakeEngine()
        app = create_app(
            self.auth,
            self.engine,
            trusted_hosts=("recorder.example.test", "localhost"),
        )
        app.testing = True
        self.client = app.test_client()
        self.host = {"Host": "recorder.example.test"}

    def test_homepage_static_assets_and_optional_auth_copy(self):
        page = self.client.get("/", headers=self.host)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"top-navigation", page.data)
        self.assertIn("らずぱいドライブレコーダー".encode(), page.data)
        stylesheet = self.client.get("/static/app.css", headers=self.host)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"--blue: #075ea8", stylesheet.data)
        script = self.client.get("/static/app.js", headers=self.host)
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"preview/demand", script.data)
        self.assertIn("録画品質".encode(), script.data)
        self.assertIn(b"renderDashboard(authenticationStatus)", script.data)
        self.assertIn(b"renderSettings(viewRoot, authenticationStatus)", script.data)
        self.assertNotIn(b"renderSettings(viewRoot, _status)", script.data)
        self.assertNotIn(b"setup_token", script.data)
        self.assertNotIn(b"/api/v1/auth/setup", script.data)
        self.assertNotIn(b"window.prompt", script.data)

    def test_non_ascii_csrf_is_rejected_without_calling_the_engine(self):
        self._status()
        for token in ("é", "日本語", "wrong-ascii-token"):
            with self.subTest(token=token):
                response = self.client.post(
                    "/api/v1/exports",
                    headers={**self.host, "X-CSRF-Token": token},
                    json={"recording_ids": ["rec-a"]},
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["error"]["code"], "csrf_failed")
        self.assertEqual(self.engine.calls, [])

    def test_host_check_accepts_only_configured_names_and_loopback_ips(self):
        trusted = ("recorder.example.test", "localhost")
        self.assertTrue(_host_allowed("recorder.example.test:8080", trusted))
        self.assertTrue(_host_allowed("127.0.0.1:8080", trusted))
        self.assertTrue(_host_allowed("[::1]:8080", trusted))
        self.assertFalse(_host_allowed("192.0.2.181:8080", trusted))
        self.assertFalse(_host_allowed("169.254.1.2:8080", trusted))
        self.assertFalse(_host_allowed("attacker.example", trusted))

    def test_new_state_opens_dashboard_and_settings_without_registration(self):
        status = self._status()
        self.assertFalse(status["login_protection_enabled"])
        self.assertFalse(status["admin_protection_enabled"])
        self.assertFalse(status["login_password_configured"])
        self.assertFalse(status["admin_password_configured"])
        self.assertTrue(status["authenticated"])
        self.assertNotIn("setup_required", status)

        dashboard = self.client.get("/api/v1/status", headers=self.host)
        self.assertEqual(dashboard.status_code, 200)
        settings = self.client.get("/api/v1/settings", headers=self.host)
        self.assertEqual(settings.status_code, 200)

        saved = self.client.patch(
            "/api/v1/settings",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.engine.calls[-1][:2], ("PATCH", "/api/v1/settings"))
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/setup",
                headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
                json={},
            ).status_code,
            404,
        )

    def test_login_protection_gates_api_but_admin_off_allows_settings(self):
        initial = self._status()
        enabled = self._save_protections(
            initial,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_json())
        locked = self.client.get("/api/v1/status", headers=self.host)
        self.assertEqual(locked.status_code, 401)

        locked_status = self._status()
        denied_export = self.client.post(
            "/api/v1/exports",
            headers={**self.host, "X-CSRF-Token": locked_status["csrf_token"]},
            json={"recording_ids": ["rec-a"]},
        )
        self.assertEqual(denied_export.status_code, 401)
        wrong = self.client.post(
            "/api/v1/auth/login",
            headers={**self.host, "X-CSRF-Token": locked_status["csrf_token"]},
            json={"password": "admin-secret-9"},
        )
        self.assertEqual(wrong.status_code, 401)
        login = self.client.post(
            "/api/v1/auth/login",
            headers={**self.host, "X-CSRF-Token": self._status()["csrf_token"]},
            json={"password": "viewer-secret-8"},
        )
        self.assertEqual(login.status_code, 200)
        active = self._status()
        self.assertTrue(active["authenticated"])

        preview = self.client.post(
            "/api/v1/cameras/1/preview/demand",
            headers={**self.host, "X-CSRF-Token": active["csrf_token"]},
            json={"active": True},
        )
        self.assertEqual(preview.status_code, 200)
        self.client.get("/api/v1/cameras/1/preview.jpg", headers=self.host)
        self.assertEqual(
            self.engine.stream_calls[-1], ("GET", "/api/v1/cameras/1/preview.jpg", None)
        )
        self.client.get("/api/v1/recordings/rec-a/download", headers=self.host)
        self.assertEqual(
            self.engine.stream_calls[-1],
            ("GET", "/api/v1/recordings/rec-a/download", None),
        )
        exported = self.client.post(
            "/api/v1/exports",
            headers={**self.host, "X-CSRF-Token": active["csrf_token"]},
            json={"recording_ids": ["rec-a"]},
        )
        self.assertEqual(exported.status_code, 202)

        saved = self.client.patch(
            "/api/v1/settings",
            headers={**self.host, "X-CSRF-Token": active["csrf_token"]},
            json={},
        )
        self.assertEqual(saved.status_code, 200)

    def test_admin_protection_is_independent_and_cannot_be_disabled_in_request(self):
        initial = self._status()
        enabled = self._save_protections(
            initial,
            login_enabled=False,
            admin_enabled=True,
            admin_password="admin-secret-9",
            admin_confirmation="admin-secret-9",
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_json())
        status = self._status()
        self.assertTrue(status["authenticated"])
        self.assertFalse(status["login_protection_enabled"])
        self.assertTrue(status["admin_protection_enabled"])
        self.assertEqual(
            self.client.get("/api/v1/status", headers=self.host).status_code, 200
        )

        blocked = self.client.post(
            "/api/v1/auth/protections",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={
                "login_enabled": False,
                "admin_enabled": False,
                "expected_revision": status["revision"],
            },
        )
        self.assertEqual(blocked.status_code, 428)
        self.assertEqual(
            blocked.get_json()["error"]["code"], "reauthentication_required"
        )

        blocked_time = self.client.post(
            "/api/v1/time",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={},
        )
        self.assertEqual(blocked_time.status_code, 428)
        reauthenticated = self.client.post(
            "/api/v1/auth/admin/reauthenticate",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={"password": "admin-secret-9"},
        )
        self.assertEqual(reauthenticated.status_code, 200)
        applied = self.client.post(
            "/api/v1/time",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={},
        )
        self.assertEqual(applied.status_code, 200)

    def test_both_protections_require_login_then_admin_reauthentication(self):
        initial = self._status()
        self._save_protections(
            initial,
            login_enabled=True,
            admin_enabled=True,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
            admin_password="admin-secret-9",
            admin_confirmation="admin-secret-9",
        )
        locked = self._status()
        self.assertFalse(locked["authenticated"])
        self.assertEqual(
            self.client.get("/api/v1/settings", headers=self.host).status_code, 401
        )
        self.client.post(
            "/api/v1/auth/login",
            headers={**self.host, "X-CSRF-Token": locked["csrf_token"]},
            json={"password": "viewer-secret-8"},
        )
        logged = self._status()
        self.assertTrue(logged["authenticated"])
        blocked = self.client.patch(
            "/api/v1/settings",
            headers={**self.host, "X-CSRF-Token": logged["csrf_token"]},
            json={},
        )
        self.assertEqual(blocked.status_code, 428)
        self.client.post(
            "/api/v1/auth/admin/reauthenticate",
            headers={**self.host, "X-CSRF-Token": logged["csrf_token"]},
            json={"password": "admin-secret-9"},
        )
        allowed = self.client.patch(
            "/api/v1/settings",
            headers={**self.host, "X-CSRF-Token": logged["csrf_token"]},
            json={},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_legacy_single_password_keeps_admin_gate_without_registration(self):
        path = Path(self.temp_dir.name) / "legacy-auth.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE authentication (singleton_key INTEGER PRIMARY KEY, "
            "revision INTEGER NOT NULL, enabled INTEGER NOT NULL, "
            "password_hash TEXT NOT NULL, setup_token_digest TEXT)"
        )
        connection.execute(
            "INSERT INTO authentication VALUES(1,4,0,?,?)",
            (FakeHasher().hash("legacy-secret-8"), "obsolete-token"),
        )
        connection.commit()
        connection.close()
        migrated_store = AuthenticationStore(path)
        migrated_store.initialize()
        migrated_auth = AuthenticationService(migrated_store, FakeHasher(), self.clock)
        app = create_app(
            migrated_auth, self.engine, trusted_hosts=("recorder.example.test",)
        )
        app.testing = True
        client = app.test_client()
        status = client.get("/api/v1/auth/status", headers=self.host).get_json()
        self.assertFalse(status["login_protection_enabled"])
        self.assertTrue(status["admin_protection_enabled"])
        self.assertTrue(status["authenticated"])
        blocked = client.post(
            "/api/v1/time",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={},
        )
        self.assertEqual(blocked.status_code, 428)
        reauthenticated = client.post(
            "/api/v1/auth/admin/reauthenticate",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json={"password": "legacy-secret-8"},
        )
        self.assertEqual(reauthenticated.status_code, 200)
        state = migrated_store.load()
        self.assertEqual(
            state.login_password_hash, FakeHasher().hash("legacy-secret-8")
        )
        self.assertEqual(
            state.admin_password_hash, FakeHasher().hash("legacy-secret-8")
        )

    def test_anonymous_csrf_origin_host_and_unlisted_media_are_rejected(self):
        status = self._status()
        no_csrf = self.client.post("/api/v1/auth/logout", headers=self.host, json={})
        self.assertEqual(no_csrf.status_code, 403)
        cross_origin = self.client.post(
            "/api/v1/auth/logout",
            headers={
                **self.host,
                "Origin": "http://attacker.example",
                "X-CSRF-Token": status["csrf_token"],
            },
            json={},
        )
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(
            self.client.get(
                "/api/v1/auth/status", headers={"Host": "attacker.example"}
            ).status_code,
            400,
        )
        missing = self.client.get("/api/v1/recordings/a/content", headers=self.host)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            self.engine.stream_calls[-1][1], "/api/v1/recordings/a/content"
        )
        invalid = self.client.get("/api/v1/live/1/7/%2e%2e%2fsecret", headers=self.host)
        self.assertEqual(invalid.status_code, 404)

    def test_camera_stop_requires_csrf_before_forwarding_to_engine(self):
        status = self._status()
        rejected = self.client.post(
            "/api/v1/cameras/1/stop", headers=self.host, json={}
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.get_json()["error"]["code"], "csrf_failed")

        accepted = self.client.post(
            "/api/v1/cameras/1/stop",
            headers={
                **self.host,
                "X-CSRF-Token": status["csrf_token"],
                "Origin": "http://recorder.example.test",
            },
            json={},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.engine.calls[-1][:2], ("POST", "/api/v1/cameras/1/stop"))

    def test_external_auth_reset_revision_invalidates_old_server_session(self):
        initial = self._status()
        self._save_protections(
            initial,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        locked = self._status()
        self.client.post(
            "/api/v1/auth/login",
            headers={**self.host, "X-CSRF-Token": locked["csrf_token"]},
            json={"password": "viewer-secret-8"},
        )
        logged = self._status()
        self.assertTrue(logged["authenticated"])

        self.store.reset_for_local_setup()
        self.assertEqual(
            self.client.get("/api/v1/status", headers=self.host).status_code, 200
        )
        reset = self._status()
        self.assertFalse(reset["login_protection_enabled"])
        self.assertFalse(reset["admin_protection_enabled"])
        self.assertTrue(reset["authenticated"])

    def _status(self):
        return self.client.get("/api/v1/auth/status", headers=self.host).get_json()

    def _save_protections(self, status, **changes):
        body = {
            "login_enabled": changes.get(
                "login_enabled", status["login_protection_enabled"]
            ),
            "admin_enabled": changes.get(
                "admin_enabled", status["admin_protection_enabled"]
            ),
            "expected_revision": status["revision"],
        }
        for key in (
            "login_password",
            "login_confirmation",
            "admin_password",
            "admin_confirmation",
        ):
            if key in changes:
                body[key] = changes[key]
        return self.client.post(
            "/api/v1/auth/protections",
            headers={**self.host, "X-CSRF-Token": status["csrf_token"]},
            json=body,
        )

    def test_retired_input_endpoints_are_not_forwarded(self):
        self._status()
        before = len(self.engine.calls)
        for path in (
            "/api/v1/events/old-id",
            "/api/v1/events/old-id/triggers",
            "/api/v1/events/old-id/parts",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.host)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.engine.calls), before)


if __name__ == "__main__":
    unittest.main()

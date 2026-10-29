from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.application.authentication import (
    _MAX_SERVER_SESSIONS,
    AuthenticationError,
    AuthenticationService,
)
from raspi_recorder.domain.errors import RevisionConflict
from tests.fakes import FakeClock


class FakeHasher:
    def hash(self, password: str) -> str:
        return "scrypt:test$" + password

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.store = AuthenticationStore(Path(self.temp_dir.name) / "auth.db")
        self.service = AuthenticationService(self.store, FakeHasher(), self.clock)
        self.service.initialize()

    def test_new_store_is_open_without_password_or_setup_gate(self):
        state = self.store.load()
        self.assertFalse(state.login_enabled)
        self.assertFalse(state.admin_enabled)
        self.assertIsNone(state.login_password_hash)
        self.assertIsNone(state.admin_password_hash)

        session = self.service.session_for_request(None)
        status = self.service.status(session.session_id)
        self.assertFalse(status.login_protection_enabled)
        self.assertFalse(status.admin_protection_enabled)
        self.assertFalse(status.login_password_configured)
        self.assertFalse(status.admin_password_configured)
        self.assertTrue(status.authenticated)
        self.assertTrue(self.service.authorize(session))
        self.service.require_recent_admin_authentication(session)

    def test_four_protection_combinations_are_independent_and_fail_closed(self):
        anonymous = self.service.session_for_request(None)
        with self.assertRaisesRegex(ValueError, "ログイン保護をON"):
            self._update(
                anonymous,
                login_enabled=True,
                admin_enabled=False,
            )
        self.assertFalse(self.store.load().login_enabled)

        self._update(
            anonymous,
            login_enabled=False,
            admin_enabled=True,
            admin_password="admin-secret-9",
            admin_confirmation="admin-secret-9",
        )
        admin_only = self.service.session_for_request(None)
        self.assertTrue(self.service.authorize(admin_only))
        with self.assertRaisesRegex(AuthenticationError, "reauthentication_required"):
            self.service.require_recent_admin_authentication(admin_only)
        self.service.reauthenticate_admin(admin_only, "client", "admin-secret-9")
        self.service.require_recent_admin_authentication(admin_only)

        self._update(
            admin_only,
            login_enabled=True,
            admin_enabled=True,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        both_on_guest = self.service.session_for_request(None)
        self.assertFalse(self.service.authorize(both_on_guest))
        logged_in = self.service.login("client", "viewer-secret-8", both_on_guest)
        self.assertTrue(self.service.authorize(logged_in))
        with self.assertRaisesRegex(AuthenticationError, "reauthentication_required"):
            self.service.require_recent_admin_authentication(logged_in)
        self.service.reauthenticate_admin(logged_in, "client", "admin-secret-9")
        self.service.require_recent_admin_authentication(logged_in)

        self._update(logged_in, login_enabled=False, admin_enabled=True)
        login_off = self.service.session_for_request(None)
        self.assertTrue(self.service.authorize(login_off))
        with self.assertRaisesRegex(AuthenticationError, "reauthentication_required"):
            self.service.require_recent_admin_authentication(login_off)

        self.service.reauthenticate_admin(login_off, "client", "admin-secret-9")
        self._update(login_off, login_enabled=False, admin_enabled=False)
        both_off = self.service.session_for_request(None)
        self.assertTrue(self.service.authorize(both_off))
        self.service.require_recent_admin_authentication(both_off)

    def test_password_inputs_are_optional_but_never_clear_existing_hash(self):
        session = self.service.session_for_request(None)
        self._update(
            session,
            login_enabled=False,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        saved_hash = self.store.load().login_password_hash
        next_session = self.service.session_for_request(None)
        with self.assertRaisesRegex(ValueError, "8～128"):
            self._update(
                next_session,
                login_enabled=False,
                admin_enabled=False,
                login_password="short",
                login_confirmation="short",
            )
        with self.assertRaisesRegex(ValueError, "一致しません"):
            self._update(
                next_session,
                login_enabled=False,
                admin_enabled=False,
                login_password="viewer-new-secret",
                login_confirmation="different-secret",
            )
        self._update(next_session, login_enabled=False, admin_enabled=False)
        self.assertEqual(self.store.load().login_password_hash, saved_hash)

    def test_protection_change_revokes_sessions_and_old_credentials(self):
        anonymous = self.service.session_for_request(None)
        self._update(
            anonymous,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        self.assertFalse(self.service.authorize(anonymous))
        logged_in = self.service.login("client", "viewer-secret-8")
        current = self.store.load()
        changed = self.service.update_protections(
            logged_in,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-new-secret",
            login_confirmation="viewer-new-secret",
            admin_password=None,
            admin_confirmation=None,
            expected_revision=current.revision,
        )
        self.assertEqual(changed.revision, current.revision + 1)
        self.assertFalse(self.service.authorize(logged_in))
        with self.assertRaises(AuthenticationError):
            self.service.login("client", "viewer-secret-8")
        replacement = self.service.login("client", "viewer-new-secret")
        self.assertTrue(self.service.authorize(replacement))
        self.service.require_recent_admin_authentication(replacement)

    def test_admin_setting_uses_current_state_and_revision(self):
        anonymous = self.service.session_for_request(None)
        self._update(
            anonymous,
            login_enabled=False,
            admin_enabled=True,
            admin_password="admin-secret-9",
            admin_confirmation="admin-secret-9",
        )
        unconfirmed = self.service.session_for_request(None)
        revision = self.store.load().revision
        with self.assertRaisesRegex(AuthenticationError, "reauthentication_required"):
            self.service.update_protections(
                unconfirmed,
                login_enabled=False,
                admin_enabled=False,
                login_password=None,
                login_confirmation=None,
                admin_password=None,
                admin_confirmation=None,
                expected_revision=revision,
            )
        self.service.reauthenticate_admin(unconfirmed, "client", "admin-secret-9")
        with self.assertRaises(RevisionConflict):
            self.service.update_protections(
                unconfirmed,
                login_enabled=False,
                admin_enabled=False,
                login_password=None,
                login_confirmation=None,
                admin_password=None,
                admin_confirmation=None,
                expected_revision=revision - 1,
            )

    def test_legacy_registered_dual_hash_database_preserves_login_and_admin(self):
        path = Path(self.temp_dir.name) / "legacy-dual.db"
        login_hash = FakeHasher().hash("viewer-secret-8")
        admin_hash = FakeHasher().hash("admin-secret-9")
        self._create_old_dual_database(
            path, enabled=False, login_hash=login_hash, admin_hash=admin_hash
        )

        store = AuthenticationStore(path)
        store.initialize()
        state = store.load()
        self.assertFalse(state.login_enabled)
        self.assertTrue(state.admin_enabled)
        self.assertEqual(state.login_password_hash, login_hash)
        self.assertEqual(state.admin_password_hash, admin_hash)
        self.assertTrue((Path(str(path) + store._MIGRATION_BACKUP_SUFFIX)).is_file())
        self.assertEqual(
            (Path(str(path) + store._MIGRATION_BACKUP_SUFFIX).stat().st_mode & 0o777),
            0o600,
        )

        changed = store.update_protections(
            login_enabled=True,
            admin_enabled=False,
            login_password_hash=None,
            admin_password_hash=None,
            expected_revision=state.revision,
        )
        store.initialize()
        self.assertTrue(store.load().login_enabled)
        self.assertFalse(store.load().admin_enabled)
        self.assertEqual(store.load().login_password_hash, login_hash)
        self.assertEqual(store.load().admin_password_hash, admin_hash)
        self.assertEqual(store.load().revision, changed.revision)

    def test_legacy_unregistered_database_migrates_to_open_state(self):
        path = Path(self.temp_dir.name) / "legacy-empty.db"
        self._create_old_dual_database(
            path, enabled=True, login_hash=None, admin_hash=None
        )
        store = AuthenticationStore(path)
        store.initialize()
        state = store.load()
        self.assertFalse(state.login_enabled)
        self.assertFalse(state.admin_enabled)
        self.assertIsNone(state.login_password_hash)
        self.assertIsNone(state.admin_password_hash)
        service = AuthenticationService(store, FakeHasher(), self.clock)
        session = service.session_for_request(None)
        self.assertTrue(service.authorize(session))
        service.require_recent_admin_authentication(session)

    def test_legacy_single_hash_and_mid_migration_keep_old_admin_protection(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-single.db"
        legacy_hash = FakeHasher().hash("legacy-secret-8")
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE authentication (singleton_key INTEGER PRIMARY KEY, "
            "revision INTEGER NOT NULL, enabled INTEGER NOT NULL, "
            "password_hash TEXT NOT NULL, setup_token_digest TEXT)"
        )
        connection.execute(
            "INSERT INTO authentication VALUES(1,7,0,?,?)",
            (legacy_hash, "obsolete-digest"),
        )
        connection.execute("CREATE TABLE recording_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO recording_marker VALUES('keep')")
        connection.commit()
        connection.close()

        store = AuthenticationStore(legacy_path)
        store.initialize()
        state = store.load()
        self.assertEqual(state.revision, 8)
        self.assertFalse(state.login_enabled)
        self.assertTrue(state.admin_enabled)
        self.assertEqual(state.login_password_hash, legacy_hash)
        self.assertEqual(state.admin_password_hash, legacy_hash)
        service = AuthenticationService(store, FakeHasher(), self.clock)
        session = service.session_for_request(None)
        self.assertTrue(service.authorize(session))
        with self.assertRaises(AuthenticationError):
            service.require_recent_admin_authentication(session)
        service.reauthenticate_admin(session, "client", "legacy-secret-8")
        service.require_recent_admin_authentication(session)

        connection = sqlite3.connect(legacy_path)
        self.assertIsNone(
            connection.execute(
                "SELECT setup_token_digest FROM authentication"
            ).fetchone()[0]
        )
        self.assertEqual(
            connection.execute("SELECT value FROM recording_marker").fetchone()[0],
            "keep",
        )
        connection.close()

        mid_path = Path(self.temp_dir.name) / "mid-migration.db"
        self._create_old_dual_database(
            mid_path,
            enabled=True,
            login_hash=legacy_hash,
            admin_hash=None,
        )
        mid_store = AuthenticationStore(mid_path)
        mid_store.initialize()
        mid_state = mid_store.load()
        self.assertTrue(mid_state.login_enabled)
        self.assertTrue(mid_state.admin_enabled)
        self.assertEqual(mid_state.admin_password_hash, legacy_hash)

    def test_corrupt_enabled_without_hash_fails_closed_and_reset_preserves_other_tables(
        self,
    ):
        session = self.service.session_for_request(None)
        connection = sqlite3.connect(Path(self.temp_dir.name) / "auth.db")
        connection.execute("UPDATE authentication SET admin_enabled=1, login_enabled=1")
        connection.execute("CREATE TABLE retained_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO retained_data VALUES('recordings stay')")
        connection.commit()
        connection.close()

        self.assertFalse(self.service.authorize(session))
        with self.assertRaises(AuthenticationError):
            self.service.require_recent_admin_authentication(session)
        self.store.reset_for_local_setup()
        state = self.store.load()
        self.assertFalse(state.login_enabled)
        self.assertFalse(state.admin_enabled)
        self.assertIsNone(state.login_password_hash)
        self.assertIsNone(state.admin_password_hash)
        connection = sqlite3.connect(Path(self.temp_dir.name) / "auth.db")
        self.assertEqual(
            connection.execute("SELECT value FROM retained_data").fetchone()[0],
            "recordings stay",
        )
        connection.close()

    def test_session_idle_expiry_and_failure_key_bounds(self):
        session = self.service.session_for_request(None)
        self._update(
            session,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        logged_in = self.service.login("client", "viewer-secret-8")
        self.clock.advance(1_801_000_000_000)
        self.assertFalse(self.service.status(logged_in.session_id).authenticated)
        with patch("raspi_recorder.application.authentication._MAX_LOGIN_RATE_KEYS", 2):
            for remote_key in ("client-1", "client-2", "client-3"):
                with self.assertRaises(AuthenticationError):
                    self.service.login(remote_key, "wrong-password")
        remote_keys = set(self.service._failures) - {"__global__"}
        self.assertLessEqual(len(remote_keys), 2)

    def test_anonymous_session_flood_is_bounded_without_evicting_active_login(self):
        session = self.service.session_for_request(None)
        self._update(
            session,
            login_enabled=True,
            admin_enabled=False,
            login_password="viewer-secret-8",
            login_confirmation="viewer-secret-8",
        )
        authenticated = self.service.login("client", "viewer-secret-8")
        for _ in range(_MAX_SERVER_SESSIONS + 20):
            self.service.session_for_request(None)
        self.assertLessEqual(len(self.service._sessions), _MAX_SERVER_SESSIONS)
        self.assertTrue(self.service.authorize(authenticated))

    def _update(
        self,
        session,
        *,
        login_enabled,
        admin_enabled,
        login_password=None,
        login_confirmation=None,
        admin_password=None,
        admin_confirmation=None,
    ):
        return self.service.update_protections(
            session,
            login_enabled=login_enabled,
            admin_enabled=admin_enabled,
            login_password=login_password,
            login_confirmation=login_confirmation,
            admin_password=admin_password,
            admin_confirmation=admin_confirmation,
            expected_revision=self.store.load().revision,
        )

    @staticmethod
    def _create_old_dual_database(
        path: Path,
        *,
        enabled: bool,
        login_hash: str | None,
        admin_hash: str | None,
    ) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE authentication (singleton_key INTEGER PRIMARY KEY, "
            "revision INTEGER NOT NULL, enabled INTEGER NOT NULL, "
            "login_password_hash TEXT, admin_password_hash TEXT)"
        )
        connection.execute(
            "INSERT INTO authentication VALUES(1,4,?,?,?)",
            (int(enabled), login_hash, admin_hash),
        )
        connection.commit()
        connection.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.cli import main


class CliTests(unittest.TestCase):
    def test_initialize_is_idempotent_and_does_not_require_password_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["initialize", "--data-dir", directory]), 0)
            first_output = output.getvalue()
            self.assertIn("パスワード登録なし", first_output)
            self.assertNotIn("token", first_output.lower())
            self.assertTrue((Path(directory) / "settings.sqlite3").is_file())
            self.assertTrue((Path(directory) / "recordings.sqlite3").is_file())
            self.assertTrue((Path(directory) / "authentication.sqlite3").is_file())
            initial = AuthenticationStore(
                Path(directory) / "authentication.sqlite3"
            ).load()
            self.assertFalse(initial.login_enabled)
            self.assertFalse(initial.admin_enabled)
            self.assertIsNone(initial.login_password_hash)
            self.assertIsNone(initial.admin_password_hash)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["initialize", "--data-dir", directory]), 0)
            self.assertNotIn("token", output.getvalue().lower())

    def test_initialize_uses_private_umask_for_auth_database(self):
        with tempfile.TemporaryDirectory() as directory:
            observed_umasks = []
            observed_file_modes = {}
            original_initialize = AuthenticationStore.initialize
            original_connection = AuthenticationStore._connection

            def capture_umask(store: AuthenticationStore) -> bool:
                current_umask = os.umask(0)
                os.umask(current_umask)
                observed_umasks.append(current_umask)
                return original_initialize(store)

            @contextlib.contextmanager
            def capture_file_modes(store: AuthenticationStore):
                with original_connection(store) as connection:
                    yield connection
                    for suffix in ("", "-wal", "-shm"):
                        path = Path(f"{store._path}{suffix}")
                        if path.exists():
                            observed_file_modes[path.name] = stat.S_IMODE(
                                path.stat().st_mode
                            )

            previous_umask = os.umask(0o007)
            try:
                with patch.object(
                    AuthenticationStore, "_connection", capture_file_modes
                ):
                    with patch.object(AuthenticationStore, "initialize", capture_umask):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(
                                main(["initialize", "--data-dir", directory]), 0
                            )

                self.assertEqual(observed_umasks, [0o077])
                current_umask = os.umask(0)
                os.umask(current_umask)
                self.assertEqual(current_umask, 0o007)
                auth_db = Path(directory) / "authentication.sqlite3"
                self.assertEqual(stat.S_IMODE(auth_db.stat().st_mode), 0o600)
                self.assertEqual(
                    observed_file_modes,
                    {
                        "authentication.sqlite3": 0o600,
                        "authentication.sqlite3-shm": 0o600,
                        "authentication.sqlite3-wal": 0o600,
                    },
                )
            finally:
                os.umask(previous_umask)

    def test_initialize_preserves_existing_authentication_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["initialize", "--data-dir", directory]), 0)
            store = AuthenticationStore(Path(directory) / "authentication.sqlite3")
            initial = store.load()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["initialize", "--data-dir", directory]), 0)
            self.assertEqual(store.load(), initial)
            self.assertNotIn("token", output.getvalue().lower())

    def test_auth_reset_clears_credentials_without_touching_recording_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["initialize", "--data-dir", directory]), 0)
            store = AuthenticationStore(Path(directory) / "authentication.sqlite3")
            state = store.load()
            store.update_protections(
                login_enabled=True,
                admin_enabled=True,
                login_password_hash="scrypt:login",
                admin_password_hash="scrypt:admin",
                expected_revision=state.revision,
            )
            before_revision = store.load().revision
            settings_before = (Path(directory) / "settings.sqlite3").read_bytes()
            recordings_before = (Path(directory) / "recordings.sqlite3").read_bytes()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["auth-reset", "--data-dir", directory]), 0)
            state = store.load()
            self.assertEqual(state.revision, before_revision + 1)
            self.assertFalse(state.login_enabled)
            self.assertFalse(state.admin_enabled)
            self.assertIsNone(state.login_password_hash)
            self.assertIsNone(state.admin_password_hash)
            self.assertIn("ログイン保護・管理者保護をOFF", output.getvalue())
            self.assertEqual(
                (Path(directory) / "settings.sqlite3").read_bytes(), settings_before
            )
            self.assertEqual(
                (Path(directory) / "recordings.sqlite3").read_bytes(), recordings_before
            )


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from raspi_recorder.adapters.settings_repository import SettingsRepository
from raspi_recorder.adapters.sqlite_store import RevisionConflict
from raspi_recorder.application.settings_service import SettingsService
from tests.fakes import FakeClock


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        repository = SettingsRepository(
            Path(self.temp_directory.name) / "state.sqlite3"
        )
        self.clock = FakeClock(wall_utc_ns=10_000)
        self.service = SettingsService(repository, self.clock)

    def test_initialize_is_idempotent_and_keeps_user_settings(self):
        initial = self.service.initialize()
        changed = self.service.compare_and_swap(
            replace(initial, power_saving_enabled=True),
            expected_revision=initial.revision,
        )
        restored = self.service.initialize()

        self.assertEqual(changed.revision, 2)
        self.assertEqual(restored, changed)

    def test_compare_and_swap_rejects_stale_revision_and_bad_candidate(self):
        current = self.service.initialize()
        with self.assertRaises(RevisionConflict):
            self.service.compare_and_swap(current, expected_revision=0)
        with self.assertRaises(ValueError):
            self.service.compare_and_swap(current, expected_revision=-1)

    def test_candidate_is_saved_with_the_next_revision(self):
        current = self.service.initialize()
        changed = self.service.compare_and_swap(
            replace(current, auto_recover_tenths_gb=90),
            current.revision,
        )
        self.assertEqual(changed.revision, current.revision + 1)
        self.assertEqual(changed.auto_recover_tenths_gb, 90)


if __name__ == "__main__":
    unittest.main()

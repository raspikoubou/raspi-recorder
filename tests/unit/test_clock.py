import unittest

from tests.fakes import FakeClock


class FakeClockTests(unittest.TestCase):
    def test_wall_clock_jump_does_not_change_monotonic_time(self):
        clock = FakeClock(wall_utc_ns=1_000, elapsed_mono_ns=500)
        clock.advance(100, wall_adjustment_ns=3_600_000_000_000)

        self.assertEqual(clock.monotonic_ns(), 600)
        self.assertEqual(clock.utc_ns(), 3_600_000_001_100)

    def test_reboot_resets_monotonic_clock_and_changes_boot_id(self):
        clock = FakeClock(elapsed_mono_ns=10)
        clock.reboot("boot-test-2")

        self.assertEqual(clock.monotonic_ns(), 0)
        self.assertEqual(clock.boot_id(), "boot-test-2")


if __name__ == "__main__":
    unittest.main()

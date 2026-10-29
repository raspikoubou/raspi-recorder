import unittest
from datetime import UTC, datetime

from raspi_recorder.domain.recordings import (
    NANOSECONDS_PER_SECOND,
    next_continuous_boundary_mono_ns,
)


class RecordingBoundaryTests(unittest.TestCase):
    def test_continuous_boundary_uses_local_five_minute_time(self):
        utc = datetime(2026, 10, 16, 12, 2, 30, 500_000, tzinfo=UTC)
        utc_ns = int(utc.timestamp() * NANOSECONDS_PER_SECOND)
        deadline = next_continuous_boundary_mono_ns(1_000, utc_ns, "Asia/Tokyo")
        self.assertEqual(deadline - 1_000, 149_500_000_000)

    def test_exact_boundary_starts_a_new_five_minute_interval(self):
        utc = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
        utc_ns = int(utc.timestamp() * NANOSECONDS_PER_SECOND)
        deadline = next_continuous_boundary_mono_ns(10, utc_ns, "Asia/Tokyo")
        self.assertEqual(deadline - 10, 300 * NANOSECONDS_PER_SECOND)

    def test_daylight_saving_jump_uses_next_existing_local_boundary(self):
        utc = datetime(2026, 3, 8, 6, 59, 59, 500_000, tzinfo=UTC)
        utc_ns = int(utc.timestamp() * NANOSECONDS_PER_SECOND)
        deadline = next_continuous_boundary_mono_ns(50, utc_ns, "America/New_York")
        self.assertEqual(deadline - 50, 500_000_000)


if __name__ == "__main__":
    unittest.main()

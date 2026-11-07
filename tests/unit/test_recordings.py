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

    def test_every_supported_interval_aligns_to_the_local_hour(self):
        from zoneinfo import ZoneInfo

        for minutes in (5, 10, 15, 20, 30, 60):
            for zone in ("Asia/Tokyo", "Asia/Kathmandu", "America/New_York"):
                for minute in (0, 17, 59):
                    with self.subTest(minutes=minutes, zone=zone, minute=minute):
                        local = datetime(
                            2026, 11, 1, 23, minute, 13, tzinfo=ZoneInfo(zone)
                        )
                        utc_ns = int(local.timestamp()) * NANOSECONDS_PER_SECOND + 123
                        delay = (
                            next_continuous_boundary_mono_ns(10, utc_ns, zone, minutes)
                            - 10
                        )
                        boundary = datetime.fromtimestamp(
                            (utc_ns + delay) / NANOSECONDS_PER_SECOND, ZoneInfo(zone)
                        )
                        self.assertEqual(boundary.second, 0)
                        self.assertEqual(boundary.microsecond, 0)
                        self.assertEqual(boundary.minute % minutes, 0)
                        self.assertGreater(delay, 0)
                        self.assertLessEqual(
                            delay, minutes * 60 * NANOSECONDS_PER_SECOND
                        )

    def test_exact_boundaries_go_to_the_next_file_for_every_interval(self):
        utc_ns = (
            int(datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp())
            * NANOSECONDS_PER_SECOND
        )
        for minutes in (5, 10, 15, 20, 30, 60):
            self.assertEqual(
                next_continuous_boundary_mono_ns(0, utc_ns, "UTC", minutes),
                minutes * 60 * NANOSECONDS_PER_SECOND,
            )

    def test_invalid_segment_intervals_are_rejected(self):
        for minutes in (0, 1, 25, 35, 40, 45, 50, 55, 65, True, 5.0, "5"):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                next_continuous_boundary_mono_ns(0, 0, "UTC", minutes)


if __name__ == "__main__":
    unittest.main()

import unittest

from raspi_recorder.domain.camera import CameraSettings, Quality
from raspi_recorder.domain.encoding import (
    MAX_VIDEO_BITRATE,
    MIN_VIDEO_BITRATE,
    target_video_bitrate,
)


class EncodingProfileTests(unittest.TestCase):
    def test_quality_bitrates_share_pixel_rate_scaling(self):
        expected = {
            Quality.LOW: 1_000_000,
            Quality.STANDARD: 2_000_000,
            Quality.HIGH: 4_000_000,
        }
        for quality, bitrate in expected.items():
            with self.subTest(quality=quality):
                self.assertEqual(
                    target_video_bitrate(CameraSettings(quality=quality)), bitrate
                )

    def test_resolution_and_fps_scale_all_quality_levels(self):
        settings = CameraSettings(width=1920, height=1080, fps=30)
        self.assertEqual(target_video_bitrate(settings), 9_000_000)

    def test_bitrate_is_bounded_for_extreme_profiles(self):
        lowest = CameraSettings(width=1, height=1, fps=1, quality=Quality.LOW)
        highest = CameraSettings(
            width=10_000, height=10_000, fps=1000, quality=Quality.HIGH
        )
        self.assertEqual(target_video_bitrate(lowest), MIN_VIDEO_BITRATE)
        self.assertEqual(target_video_bitrate(highest), MAX_VIDEO_BITRATE)


if __name__ == "__main__":
    unittest.main()

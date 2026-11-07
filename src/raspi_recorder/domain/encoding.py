"""共通の映像profile計算。"""

from __future__ import annotations

from raspi_recorder.domain.camera import CameraSettings, Quality

REFERENCE_PIXELS_PER_SECOND = 1280 * 720 * 15
_REFERENCE_BITRATES = {
    Quality.LOW: 1_000_000,
    Quality.STANDARD: 2_000_000,
    Quality.HIGH: 4_000_000,
}
MIN_VIDEO_BITRATE = 250_000
MAX_VIDEO_BITRATE = 20_000_000


def target_video_bitrate(settings: CameraSettings) -> int:
    """720p15を基準に画素数×fpsで画質段階の目標値を比例補正する。"""
    if not isinstance(settings, CameraSettings):
        raise ValueError("settingsはCameraSettingsで指定してください")
    base_bitrate = _REFERENCE_BITRATES[settings.quality]
    pixels_per_second = settings.width * settings.height * settings.fps
    scaled = round(base_bitrate * pixels_per_second / REFERENCE_PIXELS_PER_SECOND)
    return min(MAX_VIDEO_BITRATE, max(MIN_VIDEO_BITRATE, scaled))

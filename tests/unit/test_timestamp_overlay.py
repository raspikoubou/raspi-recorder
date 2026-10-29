from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from threading import local
from types import ModuleType
from unittest.mock import patch
from zoneinfo import ZoneInfo

from raspi_recorder.adapters import live_hls, timestamp_overlay
from raspi_recorder.adapters.live_hls import LiveHlsPublisher
from raspi_recorder.adapters.timestamp_overlay import draw_timestamp_overlay
from raspi_recorder.domain.camera import CameraSettings, OverlayCorner
from raspi_recorder.domain.frames import FrameEnvelope


def make_frame() -> FrameEnvelope:
    return FrameEnvelope(
        sequence=1,
        capture_mono_ns=1_000_000_000,
        capture_utc_ns=1_797_523_200_000_000_000,
        timezone="Asia/Tokyo",
        generation=1,
        jpeg_bytes=b"not-used-by-overlay",
    )


class FakeMaskCore:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def __bytes__(self) -> bytes:
        return bytes(self.size[0] * self.size[1])


class FakeOverlayFont:
    def __init__(self, size: int) -> None:
        self.size = size
        self.texts: list[str] = []

    def getmask2(
        self,
        text: str,
        *,
        mode: str,
        stroke_width: int = 0,
        ink: int = 0,
        start: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[FakeMaskCore, tuple[int, int]]:
        del mode, ink, start
        self.texts.append(text)
        if stroke_width:
            return FakeMaskCore((202, 26)), (-2, -3)
        return FakeMaskCore((200, 24)), (0, 0)


class TimestampOverlayTests(unittest.TestCase):
    def test_overlay_is_drawn_into_requested_corner_using_capture_time(self):
        class FakeImage:
            width = 800
            height = 450

            def __init__(self):
                self.pastes = []

            def paste(self, image, position, mask=None):
                self.pastes.append((image, position, mask))

        class FakeMask:
            def __init__(self, width, height):
                self.width = width
                self.height = height

        class FakeDraw:
            def __init__(self, image):
                image.draw = self
                self.image = image
                self.xy = None
                self.text_value = None

            def textbbox(self, _xy, _text, *, font, stroke_width=0):
                del font, stroke_width
                return (-2, -3, 198, 21)

        draw_module = ModuleType("PIL.ImageDraw")
        draw_module.Draw = FakeDraw
        font_module = ModuleType("PIL.ImageFont")
        fonts = []

        def load_default(*, size):
            font = FakeOverlayFont(size)
            fonts.append(font)
            return font

        font_module.load_default = load_default
        image_module = ModuleType("PIL.Image")
        image_module.frombytes = lambda _mode, size, _data: FakeMask(*size)
        pillow_module = ModuleType("PIL")
        pillow_module.__path__ = []
        modules = {
            "PIL": pillow_module,
            "PIL.Image": image_module,
            "PIL.ImageDraw": draw_module,
            "PIL.ImageFont": font_module,
        }
        expected_text = (
            datetime.fromtimestamp(make_frame().capture_utc_ns // 1_000_000_000, tz=UTC)
            .astimezone(ZoneInfo("Asia/Tokyo"))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        for corner in OverlayCorner:
            with self.subTest(corner=corner):
                with (
                    patch.dict("sys.modules", modules),
                    patch.object(timestamp_overlay, "_THREAD_CACHE", local()),
                ):
                    image = FakeImage()
                    draw_timestamp_overlay(
                        image,
                        make_frame(),
                        CameraSettings(overlay_corner=corner),
                    )
                self.assertEqual(fonts[-1].texts, [expected_text, expected_text])
                expected_x = (
                    9
                    if corner
                    in {
                        OverlayCorner.TOP_LEFT,
                        OverlayCorner.BOTTOM_LEFT,
                    }
                    else 591
                )
                expected_y = (
                    9
                    if corner
                    in {
                        OverlayCorner.TOP_LEFT,
                        OverlayCorner.TOP_RIGHT,
                    }
                    else 417
                )
                self.assertEqual(
                    [paste[1] for paste in image.pastes],
                    [(expected_x, expected_y), (expected_x + 2, expected_y + 3)],
                )
                self.assertEqual(
                    [paste[0] for paste in image.pastes],
                    [(0, 0, 0), (255, 255, 255)],
                )

    def test_same_second_mask_reused_and_cache_stays_bounded(self):
        counts = {"bbox": 0, "font": 0}

        class FakeImage:
            width = 800
            height = 450

            def paste(self, _image, _position, _mask=None):
                pass

        class FakeMask:
            def __init__(self, width, height):
                self.width = width
                self.height = height

        class FakeDraw:
            def __init__(self, _image):
                pass

            def textbbox(self, _xy, _text, *, font, stroke_width=0):
                del font, stroke_width
                counts["bbox"] += 1
                return (0, 0, 200, 24)

            def text(self, _xy, _text, **_options):
                pass

        draw_module = ModuleType("PIL.ImageDraw")
        draw_module.Draw = FakeDraw
        font_module = ModuleType("PIL.ImageFont")

        def load_default(*, size):
            counts["font"] += 1
            return FakeOverlayFont(size)

        font_module.load_default = load_default
        image_module = ModuleType("PIL.Image")
        image_module.frombytes = lambda _mode, size, _data: FakeMask(*size)
        pillow_module = ModuleType("PIL")
        pillow_module.__path__ = []
        modules = {
            "PIL": pillow_module,
            "PIL.Image": image_module,
            "PIL.ImageDraw": draw_module,
            "PIL.ImageFont": font_module,
        }
        settings = CameraSettings()
        with (
            patch.dict("sys.modules", modules),
            patch.object(timestamp_overlay, "_THREAD_CACHE", local()),
        ):
            for second in [0, 0, *range(1, 13)]:
                frame = replace(
                    make_frame(),
                    capture_utc_ns=make_frame().capture_utc_ns + second * 1_000_000_000,
                )
                draw_timestamp_overlay(FakeImage(), frame, settings)
            cache = timestamp_overlay._thread_cache()

        self.assertEqual(counts["bbox"], 13)
        self.assertEqual(counts["font"], 1)
        self.assertEqual(cache.font_loads, 1)
        self.assertEqual(len(cache.fonts), 1)
        self.assertEqual(len(cache.masks), timestamp_overlay._MAX_MASK_CACHE_ENTRIES)
        self.assertLessEqual(cache.mask_bytes, timestamp_overlay._MAX_MASK_CACHE_BYTES)
        self.assertEqual(cache.mask_bytes, 8 * (202 * 26 + 200 * 24))

    def test_disabled_overlay_does_not_change_image(self):
        class FakeImage:
            width = 800
            height = 450

        draw_timestamp_overlay(
            FakeImage(), make_frame(), CameraSettings(overlay_enabled=False)
        )

    def test_live_settings_update_does_not_require_publisher_restart(self):
        publisher = LiveHlsPublisher("/tmp/live-test", generation=1)
        settings = CameraSettings(
            overlay_corner=OverlayCorner.TOP_RIGHT,
            overlay_enabled=False,
        )
        publisher.update_settings(settings)
        self.assertEqual(publisher._settings, settings)
        self.assertFalse(publisher._started)

    def test_live_preview_uses_latest_overlay_settings(self):
        class FakeSource:
            width = 320
            height = 180

            def thumbnail(self, _size):
                return None

        class FakeCanvas:
            width = 640
            height = 360

            def __init__(self):
                self.pastes = []

            def paste(self, image, position, mask=None):
                self.pastes.append((image, position, mask))

        class FakeMask:
            def __init__(self, width, height):
                self.width = width
                self.height = height

            def paste(self, image, position, mask=None):
                del image, position, mask

        class FakeDraw:
            def __init__(self, image):
                image.draw = self
                self.image = image

            def textbbox(self, _xy, _text, *, font, stroke_width=0):
                del font, stroke_width
                return (0, 0, 200, 24)

            def text(self, xy, value, **_options):
                self.xy = xy
                self.text_value = value
                self.image.text_draw = self

        canvas = FakeCanvas()
        image_module = ModuleType("PIL.Image")
        image_module.new = lambda mode, size, **_kwargs: (
            canvas if mode == "RGB" else FakeMask(*size)
        )
        image_module.frombytes = lambda _mode, size, _data: FakeMask(*size)

        draw_module = ModuleType("PIL.ImageDraw")
        draw_module.Draw = FakeDraw
        font_module = ModuleType("PIL.ImageFont")
        loaded_fonts = []

        def load_default(*, size):
            font = FakeOverlayFont(size)
            loaded_fonts.append(font)
            return font

        font_module.load_default = load_default
        pillow_module = ModuleType("PIL")
        pillow_module.__path__ = []
        modules = {
            "PIL": pillow_module,
            "PIL.Image": image_module,
            "PIL.ImageDraw": draw_module,
            "PIL.ImageFont": font_module,
        }
        publisher = LiveHlsPublisher("/tmp/live-test", generation=2)
        publisher.update_settings(
            CameraSettings(overlay_corner=OverlayCorner.TOP_RIGHT)
        )
        source = FakeSource()
        with (
            patch.object(live_hls, "_decode_live_image", return_value=source),
            patch.dict("sys.modules", modules),
            patch.object(timestamp_overlay, "_THREAD_CACHE", local()),
        ):
            preview = publisher._render_preview(make_frame())

        expected_text = (
            datetime.fromtimestamp(make_frame().capture_utc_ns // 1_000_000_000, tz=UTC)
            .astimezone(ZoneInfo("Asia/Tokyo"))
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertIs(preview, canvas)
        self.assertIs(canvas.pastes[0][0], source)
        self.assertEqual(canvas.pastes[0][1], (160, 90))
        self.assertEqual(canvas.pastes[1][0], (0, 0, 0))
        self.assertEqual(canvas.pastes[1][1], (431, 4))
        self.assertIsInstance(canvas.pastes[1][2], FakeMask)
        self.assertEqual(canvas.pastes[2][0], (255, 255, 255))
        self.assertEqual(canvas.pastes[2][1], (433, 7))
        self.assertIsInstance(canvas.pastes[2][2], FakeMask)
        self.assertEqual(loaded_fonts[0].texts, [expected_text, expected_text])


if __name__ == "__main__":
    unittest.main()

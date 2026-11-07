"""Validate frame/timestamp integrity across the bounded full-HD encoder path."""

from __future__ import annotations

import tempfile
import unittest
from io import BytesIO

from raspi_recorder.adapters.pyav_media import PyAvMp4Writer
from raspi_recorder.domain.camera import CameraSettings, Quality
from raspi_recorder.domain.frames import FrameEnvelope

try:
    import av
    from PIL import Image
except ImportError:
    av = None
    Image = None


@unittest.skipUnless(av is not None and Image is not None, "PyAV and Pillow required")
class VideoParallelEncodingTests(unittest.TestCase):
    def test_rgb_conversion_preserves_rows_channels_and_unaligned_widths(self):
        for width in (318, 320, 1280, 1920):
            with self.subTest(width=width):
                image = Image.new("RGB", (width, 40), (12, 34, 56))
                for row in range(40):
                    for col in (0, width // 2, width - 1):
                        image.putpixel((col, row), (row * 5, col % 255, 220))
                frame = PyAvMp4Writer._rgb_video_frame(av, image)
                self.assertEqual(frame.to_image().tobytes(), image.tobytes())

    def test_loaded_jpeg_pixels_survive_the_closed_file_context(self):
        for mode in ("RGB", "L"):
            with self.subTest(mode=mode):
                image = Image.new(mode, (320, 240), 40)
                output = BytesIO()
                image.save(output, format="JPEG")
                writer = PyAvMp4Writer.__new__(PyAvMp4Writer)
                writer._settings = CameraSettings(width=320, height=240)
                decoded = writer._decode_image(output.getvalue())
                self.assertEqual(decoded.mode, "RGB")
                self.assertEqual(decoded.size, (320, 240))
                self.assertEqual(len(decoded.tobytes()), 320 * 240 * 3)
                self.assertEqual(av.VideoFrame.from_image(decoded).width, 320)

    def test_full_hd_keeps_all_frames_in_capture_order_and_time(self):
        for width, height, fps in [(1280, 720, 15), (1920, 1080, 30)]:
            with (
                self.subTest(size=(width, height)),
                tempfile.TemporaryDirectory() as root,
            ):
                image = Image.new("RGB", (width, height), (48, 96, 160))
                output = BytesIO()
                image.save(output, format="JPEG", quality=85)
                jpeg = output.getvalue()
                writer = PyAvMp4Writer(
                    root,
                    "synthetic.mp4",
                    CameraSettings(
                        width=width, height=height, fps=fps, quality=Quality.HIGH
                    ),
                    camera_id=1,
                    mode="continuous",
                )
                for index in range(60):
                    elapsed = round(index * 1_000_000_000 / fps)
                    writer.write(
                        FrameEnvelope(
                            sequence=index + 1,
                            capture_mono_ns=1_000_000_000 + elapsed,
                            capture_utc_ns=1_792_992_000_000_000_000 + elapsed,
                            timezone="Asia/Tokyo",
                            generation=1,
                            jpeg_bytes=jpeg,
                        )
                    )
                artifact = writer.finalize()
                with av.open(artifact.path) as container:
                    frames = list(container.decode(video=0))
                    self.assertEqual(len(frames), 60)
                    self.assertTrue(
                        all((f.width, f.height) == (width, height) for f in frames)
                    )
                    timestamps = [float(f.pts * f.time_base) for f in frames]
                    self.assertTrue(
                        all(
                            a < b
                            for a, b in zip(timestamps, timestamps[1:], strict=False)
                        )
                    )
                    self.assertAlmostEqual(
                        timestamps[-1] - timestamps[0], 59 / fps, places=4
                    )

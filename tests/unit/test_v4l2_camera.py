import errno
import os
import struct
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.v4l2_camera import (
    V4L2CameraSource,
    V4L2CaptureConfig,
)
from raspi_recorder.domain.camera import CameraSettings
from tests.fakes import FakeClock


def _jpeg(color="red", size=(1280, 720)):
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


class FakeTimezone:
    def name(self) -> str:
        return "UTC"


class V4L2CaptureConfigTests(unittest.TestCase):
    def test_requires_port_stable_device_path_and_supported_input_format(self):
        with self.assertRaises(ValueError):
            V4L2CaptureConfig("/dev/video0", 1, 1, "mjpeg", CameraSettings())
        with self.assertRaises(ValueError):
            V4L2CaptureConfig(
                "/dev/v4l/by-path/usb-camera", 1, 1, "h264", CameraSettings()
            )

    def test_valid_profile_is_kept_explicit(self):
        config = V4L2CaptureConfig(
            "/dev/v4l/by-path/platform-xhci-1.0-video-index0",
            1,
            2,
            "mjpeg",
            CameraSettings(),
        )
        self.assertEqual(config.camera_id, 1)
        self.assertEqual(config.generation, 2)
        self.assertEqual(config.input_format, "mjpeg")


class FfmpegCaptureTests(unittest.TestCase):
    def setUp(self):
        # The fake executable has no V4L2 device. Negotiation is covered below.
        verifier = patch.object(V4L2CameraSource, "_verify_frame_rate")
        self.verify_rate = verifier.start()
        self.addCleanup(verifier.stop)

    def test_negotiated_fps_mismatch_is_rejected_before_capture_ready(self):
        self.verify_rate.side_effect = RuntimeError("カメラの実際のfpsが異なります")
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                f"#!/usr/bin/env python3\nimport os\nos.write(1, {_jpeg()!r})\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            with self.assertRaisesRegex(RuntimeError, "実際のfps"):
                tuple(source.frames(threading.Event()))
            self.assertEqual(source.stats.frames, 0)
            self.assertIsNone(source.active_process_pid)

    def test_negotiated_size_mismatch_is_rejected_before_capture_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                "#!/usr/bin/env python3\nimport os\n"
                f"os.write(1, {_jpeg(size=(320, 240))!r})\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            with self.assertRaisesRegex(RuntimeError, "実camera解像度"):
                tuple(source.frames(threading.Event()))
            self.assertIsNone(source.active_process_pid)

    def test_continuous_invalid_bytes_still_trigger_the_frame_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import os,time\n"
                "while True:\n    os.write(1,b'not-a-frame'); time.sleep(0.005)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            try:
                with (
                    patch(
                        "raspi_recorder.adapters.v4l2_camera._FFMPEG_FRAME_TIMEOUT_SECONDS",
                        0.1,
                    ),
                    self.assertRaisesRegex(TimeoutError, "camera_frame_timeout"),
                ):
                    tuple(source.frames(threading.Event()))
                self.assertIsNone(source.active_process_pid)
            finally:
                source.close()

    def test_stalled_camera_times_out_and_reaps_ffmpeg_without_manual_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            with (
                patch(
                    "raspi_recorder.adapters.v4l2_camera._FFMPEG_FRAME_TIMEOUT_SECONDS",
                    0.01,
                ),
                self.assertRaisesRegex(TimeoutError, "camera_frame_timeout"),
            ):
                tuple(source.frames(threading.Event()))
            self.assertIsNone(source.active_process_pid)
            self.assertEqual(source.stats.last_error, "camera_frame_timeout")

    def test_captures_ordered_jpeg_frames_with_system_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = _jpeg("red"), _jpeg("blue")
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                f"#!/usr/bin/env python3\nimport os\nos.write(1, {first + second!r})\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            frames = tuple(source.frames(threading.Event()))

            self.assertEqual(
                [frame.jpeg_bytes for frame in frames],
                [first, second],
            )
            self.assertEqual(source.stats.backend, "ffmpeg-v4l2")
            self.assertEqual(source.stats.frames, 2)

    def test_closing_frame_iterator_terminates_and_reaps_owned_ffmpeg_child(self):
        with tempfile.TemporaryDirectory() as directory:
            jpeg = _jpeg()
            script = Path(directory) / "fake-ffmpeg"
            pid_file = Path(directory) / "pid"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                f"os.write(1, {jpeg!r})\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            iterator = source.frames(threading.Event())
            frame = next(iterator)
            self.assertEqual(frame.jpeg_bytes, jpeg)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertEqual(source.active_process_pid, child_pid)
            iterator.close()

            self.assertIsNone(source.active_process_pid)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_stop_event_terminates_a_stalled_ffmpeg_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            source = V4L2CameraSource(
                V4L2CaptureConfig(
                    "/dev/v4l/by-path/usb-test-video-index0",
                    1,
                    1,
                    "mjpeg",
                    CameraSettings(),
                ),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            stop = threading.Event()
            iterator = source.frames(stop)
            worker = threading.Thread(target=lambda: next(iterator, None))
            worker.start()
            time.sleep(0.05)
            stop.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(source.active_process_pid)


class FrameRateNegotiationTests(unittest.TestCase):
    def source(self, fps=30):
        return V4L2CameraSource(
            V4L2CaptureConfig(
                "/dev/v4l/by-path/usb-test-video-index0",
                1,
                1,
                "mjpeg",
                CameraSettings(fps=fps),
            ),
            FakeClock(),
            FakeTimezone(),
        )

    def test_actual_driver_interval_accepts_nominal_rate_and_rejects_fallback(self):
        for numerator, denominator, accepted in [
            (1, 30, True),
            (1001, 30000, True),
            (1, 15, False),
            (1, 25, False),
            (0, 30, False),
            (1, 0, False),
        ]:
            with self.subTest(interval=(numerator, denominator)):
                source = self.source()

                def ioctl(fd, request, payload, mutate, n=numerator, d=denominator):
                    self.assertEqual(fd, 123)
                    self.assertEqual(request, 0xC0CC5615)
                    self.assertEqual(len(payload), 204)
                    self.assertEqual(struct.unpack_from("<I", payload)[0], 1)
                    struct.pack_into("<II", payload, 12, n, d)

                with (
                    patch("os.open", return_value=123),
                    patch("os.close") as close,
                    patch("fcntl.ioctl", side_effect=ioctl),
                    patch.object(source, "_disable_dynamic_frame_rate") as fixed,
                ):
                    if accepted:
                        source._verify_frame_rate()
                        fixed.assert_called_once_with()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "指定したfps"):
                            source._verify_frame_rate()
                        self.assertEqual(
                            source.stats.last_error, "camera_frame_rate_mismatch"
                        )
                    close.assert_called_once_with(123)

    def test_unreadable_interval_fails_closed_and_closes_device(self):
        source = self.source()
        with (
            patch("os.open", return_value=123),
            patch("os.close") as close,
            patch("fcntl.ioctl", side_effect=OSError("not supported")),
        ):
            with self.assertRaisesRegex(RuntimeError, "fpsを確認できません"):
                source._verify_frame_rate()
            close.assert_called_once_with(123)
            self.assertEqual(source.stats.last_error, "camera_frame_rate_unverified")

    def test_variable_exposure_frame_rate_is_disabled_and_read_back(self):
        source = self.source()
        value = 1
        requests = []

        def ioctl(fd, request, payload, mutate):
            nonlocal value
            requests.append(request)
            self.assertEqual(struct.unpack_from("<I", payload)[0], 0x009A0903)
            if request == 0xC008561C:
                value = struct.unpack_from("<i", payload, 4)[0]
            else:
                struct.pack_into("<i", payload, 4, value)

        with (
            patch("os.open", return_value=123),
            patch("os.close") as close,
            patch("fcntl.ioctl", side_effect=ioctl),
        ):
            source._disable_dynamic_frame_rate()
        self.assertEqual(value, 0)
        self.assertEqual(requests, [0xC008561B, 0xC008561C, 0xC008561B])
        close.assert_called_once_with(123)

    def test_fixed_exposure_rate_is_left_unchanged(self):
        source = self.source()
        with (
            patch("os.open", return_value=123),
            patch("os.close"),
            patch("fcntl.ioctl") as ioctl,
        ):
            source._disable_dynamic_frame_rate()
        self.assertEqual(ioctl.call_count, 1)

    def test_cameras_without_optional_exposure_control_still_start(self):
        for code in [errno.EINVAL, errno.ENOTTY]:
            with self.subTest(errno=code):
                source = self.source()
                with (
                    patch("os.open", return_value=123),
                    patch("os.close") as close,
                    patch("fcntl.ioctl", side_effect=OSError(code, "unsupported")),
                ):
                    source._disable_dynamic_frame_rate()
                close.assert_called_once_with(123)

    def test_ignored_exposure_setting_or_permission_error_cannot_report_ready(self):
        for denied in [False, True]:
            source = self.source()

            def ioctl(fd, request, payload, mutate, denied=denied):
                if denied:
                    raise OSError(errno.EACCES, "denied")
                struct.pack_into("<i", payload, 4, 1)

            with (
                self.subTest(denied=denied),
                patch("os.open", return_value=123),
                patch("os.close") as close,
                patch("fcntl.ioctl", side_effect=ioctl),
            ):
                with self.assertRaisesRegex(RuntimeError, "fpsを固定できません"):
                    source._disable_dynamic_frame_rate()
                self.assertEqual(
                    source.stats.last_error, "camera_frame_rate_unverified"
                )
                close.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()

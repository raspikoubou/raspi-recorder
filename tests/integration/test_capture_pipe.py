"""物理USB入力だけを置換し、実FFmpeg→capture source→MP4を通す。"""

from __future__ import annotations

import shutil
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raspi_recorder.adapters.pyav_media import PyAvMp4Writer
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.adapters.v4l2_camera import V4L2CameraSource, V4L2CaptureConfig
from raspi_recorder.domain.camera import CameraSettings


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg required")
def test_real_ffmpeg_capture_pipe_produces_fully_decodable_h264(tmp_path):
    av = pytest.importorskip("av")
    binary = shutil.which("ffmpeg")
    command = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=5",
        "-frames:v",
        "12",
        "-c:v",
        "mjpeg",
        "-threads",
        "1",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    executable = tmp_path / "test-input"
    executable.write_text(
        f"#!{sys.executable}\nimport os\nos.execv({binary!r}, {command!r})\n"
    )
    executable.chmod(0o755)
    settings = CameraSettings(
        width=320, height=240, fps=5, audio_enabled=False, overlay_enabled=False
    )
    source = V4L2CameraSource(
        V4L2CaptureConfig("/dev/v4l/by-path/test-input", 1, 1, "mjpeg", settings),
        SystemClock(),
        SimpleNamespace(name=lambda: "UTC"),
        ffmpeg_binary=str(executable),
    )
    writer = PyAvMp4Writer(
        tmp_path, "capture.mp4", settings, camera_id=1, mode="continuous"
    )
    try:
        # The input is FFmpeg's synthetic testsrc, not a V4L2 device. Driver
        # frame-rate negotiation is covered by the V4L2 unit and Pi tests.
        with patch.object(source, "_verify_frame_rate") as verify_rate:
            for frame in source.frames(threading.Event()):
                writer.write(frame)
            verify_rate.assert_called_once()
        artifact = writer.finalize()
        writer.validate_full_decode()
        assert artifact.frame_count == 12
        assert source.active_process_pid is None
        with av.open(artifact.path) as media:
            assert media.streams.video[0].codec_context.name == "h264"
            assert len(list(media.decode(video=0))) == 12
    finally:
        source.close()
        writer.abort("test_cleanup")

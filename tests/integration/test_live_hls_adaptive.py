from __future__ import annotations

import math
import shutil
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path

try:
    import av
    from PIL import Image
except ImportError:
    av = None
    Image = None

from raspi_recorder.adapters.live_hls import LiveHlsPublisher
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope


@unittest.skipUnless(av is not None and Image is not None, "PyAV and Pillow required")
class AdaptiveLiveHlsIntegrationTests(unittest.TestCase):
    def test_output_switches_resolution_fps_generation_and_keeps_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base_directory = Path(temporary) / "camera-1"
            publisher = LiveHlsPublisher(
                base_directory / "generation-1",
                generation=1,
                settings=CameraSettings(audio_enabled=True, overlay_enabled=False),
            )
            publisher.start()
            self.assertTrue(publisher.request_viewer())
            sequence = 0
            try:
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=24,
                    interval_ns=100_000_000,
                    recording_delay_ns=0,
                )
                initial_generation = publisher.generation
                self._wait_for_output(publisher, minimum_segments=1)
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=58,
                    interval_ns=100_000_000,
                    recording_delay_ns=2_000_000_000,
                )
                self._wait_for_quality(publisher, "320x180@5", initial_generation)
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=6,
                    interval_ns=200_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_discontinuity(publisher)
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=6,
                    interval_ns=200_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_output(publisher)
                initial_directory = publisher.directory_for_generation(
                    initial_generation
                )
                self.assertIsNotNone(initial_directory)
                assert initial_directory is not None
                self._assert_output(
                    initial_directory,
                    width=640,
                    height=360,
                    fps=10,
                    audio=True,
                )
                first_degraded_generation = publisher.generation
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=16,
                    interval_ns=200_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_output(publisher)
                self._assert_output(
                    publisher.directory,
                    width=320,
                    height=180,
                    fps=5,
                    audio=True,
                )

                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=58,
                    interval_ns=100_000_000,
                    recording_delay_ns=2_000_000_000,
                )
                self._wait_for_quality(
                    publisher, "320x180@2", first_degraded_generation
                )
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=4,
                    interval_ns=500_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_discontinuity(publisher)
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=8,
                    interval_ns=500_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_output(publisher)
                five_fps_directory = publisher.directory_for_generation(
                    first_degraded_generation
                )
                self.assertIsNotNone(five_fps_directory)
                assert five_fps_directory is not None
                self._assert_output(
                    five_fps_directory,
                    width=320,
                    height=180,
                    fps=5,
                    audio=True,
                )
                self._send_frames(
                    publisher,
                    sequence,
                    count=12,
                    interval_ns=500_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_output(publisher)
                self._assert_output(
                    publisher.directory,
                    width=320,
                    height=180,
                    fps=2,
                    audio=True,
                )
                self.assertNotEqual(publisher.generation, first_degraded_generation)
                self.assertFalse(publisher.status["paused"])
            finally:
                publisher.close(timeout_seconds=15)

    def test_recording_priority_starts_lower_and_sheds_on_writer_lag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base_directory = Path(temporary) / "camera-1"
            publisher = LiveHlsPublisher(
                base_directory / "generation-1",
                generation=1,
                settings=CameraSettings(audio_enabled=True, overlay_enabled=False),
                recording_active=True,
            )
            publisher.start()
            self.assertTrue(publisher.request_viewer())
            sequence = 0
            try:
                self.assertEqual(publisher.status["quality"], "320x180@5")
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=24,
                    interval_ns=200_000_000,
                    recording_delay_ns=0,
                )
                self._wait_for_output(publisher, minimum_segments=1)
                self._assert_output(
                    publisher.directory,
                    width=320,
                    height=180,
                    fps=5,
                    audio=True,
                )

                initial_generation = publisher.generation
                sequence = self._send_frames(
                    publisher,
                    sequence,
                    count=14,
                    interval_ns=100_000_000,
                    recording_delay_ns=2_000_000_000,
                )
                self._wait_for_quality(publisher, "320x180@2", initial_generation)
                self._send_frames(
                    publisher,
                    sequence,
                    count=14,
                    interval_ns=100_000_000,
                    recording_delay_ns=2_000_000_000,
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not publisher.status["paused"]:
                    if publisher.status["error"] is not None:
                        self.fail(f"live encoder failed: {publisher.status}")
                    time.sleep(0.05)
                self.assertTrue(publisher.status["paused"], publisher.status)
                self.assertEqual(
                    publisher.status["reason"], "paused_to_protect_recording"
                )
                self.assertEqual(publisher.status["queued_bytes"], 0)
                self.assertEqual(publisher.status["audio_queued_bytes"], 0)
            finally:
                publisher.close(timeout_seconds=15)

    @staticmethod
    def _send_frames(
        publisher: LiveHlsPublisher,
        sequence: int,
        *,
        count: int,
        interval_ns: int,
        recording_delay_ns: int,
    ) -> int:
        publisher.request_viewer()
        started_mono_ns = time.monotonic_ns()
        for frame_index in range(count):
            target_mono_ns = started_mono_ns + frame_index * interval_ns
            remaining_ns = target_mono_ns - time.monotonic_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1_000_000_000)
            sequence += 1
            mono_ns = time.monotonic_ns()
            audio_sequence = getattr(publisher, "_test_audio_sequence", 0)
            audio_cursor_ns = getattr(
                publisher, "_test_audio_cursor_ns", mono_ns - 100_000_000
            )
            audio_blocks = []
            while audio_cursor_ns + 100_000_000 <= mono_ns:
                audio_sequence += 1
                audio_blocks.append(
                    AudioEnvelope(
                        sequence=audio_sequence,
                        capture_mono_ns=audio_cursor_ns,
                        capture_utc_ns=(1_797_523_200_000_000_000 + audio_cursor_ns),
                        timezone="UTC",
                        generation=1,
                        sample_rate=48_000,
                        pcm_s16le=b"\0\0" * 4_800,
                    )
                )
                audio_cursor_ns += 100_000_000
            publisher._test_audio_sequence = audio_sequence
            publisher._test_audio_cursor_ns = audio_cursor_ns
            frame = FrameEnvelope(
                sequence=sequence,
                capture_mono_ns=mono_ns,
                capture_utc_ns=1_797_523_200_000_000_000 + mono_ns,
                timezone="UTC",
                generation=1,
                jpeg_bytes=_jpeg(),
                audio_blocks=tuple(audio_blocks),
                audio_available=True,
            )
            publisher.submit(frame, recording_delay_ns=recording_delay_ns)
        return sequence

    @staticmethod
    def _wait_for_quality(
        publisher: LiveHlsPublisher, quality: str, previous_generation: int
    ) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = publisher.status
            if (
                status["quality"] == quality
                and status["generation"] != previous_generation
            ):
                return
            time.sleep(0.05)
        raise AssertionError(
            f"live output did not switch to {quality}: {publisher.status}"
        )

    @staticmethod
    def _wait_for_output(
        publisher: LiveHlsPublisher, *, minimum_segments: int = 2
    ) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = publisher.status
            if status["error"] is not None:
                raise AssertionError(f"live encoder failed: {status}")
            playlist = publisher.directory / "index.m3u8"
            try:
                text = playlist.read_text(encoding="ascii")
            except (FileNotFoundError, UnicodeDecodeError):
                time.sleep(0.05)
                continue
            segments = [
                line for line in text.splitlines() if line and not line.startswith("#")
            ]
            if len(segments) >= minimum_segments and all(
                (publisher.directory / name).is_file() for name in segments
            ):
                return
            time.sleep(0.05)
        raise AssertionError(
            f"live HLS playlist did not gain a segment: {publisher.directory}; "
            f"status={publisher.status}"
        )

    @staticmethod
    def _wait_for_discontinuity(publisher: LiveHlsPublisher) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if publisher.status["error"] is not None:
                raise AssertionError(f"live encoder failed: {publisher.status}")
            playlist = publisher.directory / "index.m3u8"
            try:
                text = playlist.read_text(encoding="ascii")
            except (FileNotFoundError, UnicodeDecodeError):
                time.sleep(0.05)
                continue
            if "#EXT-X-DISCONTINUITY" in text:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"new live HLS generation omitted its discontinuity: {publisher.status}"
        )

    def _assert_output(
        self,
        directory: Path,
        *,
        width: int,
        height: int,
        fps: int,
        audio: bool,
    ) -> None:
        assert av is not None
        snapshot = directory.parent / (
            f"{directory.name}-inspection-{time.monotonic_ns()}"
        )
        shutil.copytree(directory, snapshot)
        playlist = snapshot / "index.m3u8"
        playlist.write_text(
            playlist.read_text(encoding="ascii").rstrip() + "\n#EXT-X-ENDLIST\n",
            encoding="ascii",
        )
        container = av.open(str(snapshot / "index.m3u8"))
        try:
            video_stream = next(
                stream for stream in container.streams if stream.type == "video"
            )
            audio_streams = [
                stream for stream in container.streams if stream.type == "audio"
            ]
            self.assertEqual(video_stream.width, width)
            self.assertEqual(video_stream.height, height)
            actual_rate = video_stream.average_rate
            if actual_rate is not None and math.isfinite(float(actual_rate)):
                self.assertAlmostEqual(float(actual_rate), fps, delta=0.2)
            self.assertEqual(bool(audio_streams), audio)
            video_times: list[float] = []
            audio_times: list[float] = []
            for packet in container.demux():
                stream_type = packet.stream.type
                for frame in packet.decode():
                    if frame.pts is None:
                        continue
                    pts_seconds = float(frame.pts * frame.time_base)
                    if stream_type == "video":
                        video_times.append(pts_seconds)
                    elif stream_type == "audio":
                        audio_times.append(pts_seconds)
            self.assertGreater(len(video_times), 2)
            if len(video_times) > 2:
                self.assertLess(video_times[0], 30.0)
                actual_decoded_rate = (len(video_times) - 1) / (
                    video_times[-1] - video_times[0]
                )
                self.assertAlmostEqual(actual_decoded_rate, fps, delta=0.5)
            if audio:
                self.assertTrue(audio_times)
                start_tolerance = (1 / fps) + (1024 / 48_000)
                self.assertLess(
                    abs(video_times[0] - audio_times[0]),
                    start_tolerance,
                    f"video_start={video_times[0]}, audio_start={audio_times[0]}",
                )
        finally:
            container.close()


def _jpeg() -> bytes:
    assert Image is not None
    image = Image.new("RGB", (640, 480), color=(24, 88, 144))
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()

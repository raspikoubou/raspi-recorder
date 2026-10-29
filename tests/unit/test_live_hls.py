from __future__ import annotations

import tempfile
import time
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from raspi_recorder.adapters import live_hls
from raspi_recorder.adapters.live_hls import (
    LiveHlsPublisher,
    _capture_time_to_frame_pts,
)
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope
from tests.fakes import FakeClock


class LiveHlsTimestampTests(unittest.TestCase):
    def test_fps_grid_conversion_never_collapses_evenly_spaced_capture_times(self):
        origin = 1_000_000_000
        capture_times = (
            origin + 100_000_000,
            origin + 300_000_000,
            origin + 500_000_000,
            origin + 700_000_000,
        )

        points = tuple(
            _capture_time_to_frame_pts(capture_time, origin, 5)
            for capture_time in capture_times
        )

        self.assertEqual(points, (0, 1, 2, 3))


class LiveHlsSamplingTests(unittest.TestCase):
    def test_target_rate_sampling_tolerates_small_capture_jitter(self):
        publisher = LiveHlsPublisher("/tmp/live-jitter-fps", generation=1)
        publisher._started = True
        self.assertTrue(publisher.request_viewer())

        capture_times = (
            1_000_000_000,
            1_096_000_000,
            1_199_000_000,
            1_298_000_000,
            1_397_000_000,
        )
        accepted = tuple(
            publisher.submit(self._frame(index + 1, capture_mono_ns))
            for index, capture_mono_ns in enumerate(capture_times)
        )

        self.assertEqual(accepted, (True, True, True, True, True))
        self.assertEqual(tuple(publisher._sample_slots.values()), (0, 1, 2, 3, 4))

    def test_target_rate_sampling_bounds_fifteen_fps_input_to_ten_fps(self):
        publisher = LiveHlsPublisher("/tmp/live-sampled-fps", generation=1)
        publisher._started = True
        self.assertTrue(publisher.request_viewer())

        capture_times = (
            1_000_000_000,
            1_066_666_667,
            1_133_333_333,
            1_200_000_000,
            1_266_666_667,
            1_333_333_333,
            1_400_000_000,
        )
        accepted = tuple(
            publisher.submit(self._frame(index + 1, capture_mono_ns))
            for index, capture_mono_ns in enumerate(capture_times)
        )

        self.assertEqual(accepted, (True, True, False, True, True, False, True))
        self.assertEqual(tuple(publisher._sample_slots.values()), (0, 1, 2, 3, 4))

    @staticmethod
    def _frame(sequence: int, capture_mono_ns: int) -> FrameEnvelope:
        return FrameEnvelope(
            sequence=sequence,
            capture_mono_ns=capture_mono_ns,
            capture_utc_ns=capture_mono_ns,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
        )


class LiveHlsAudioQueueTests(unittest.TestCase):
    def test_new_generation_remains_discontinuous_after_old_playlist_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            publisher = LiveHlsPublisher(
                Path(temporary) / "camera-1" / "generation-1",
                generation=1,
            )
            publisher._rotate_generation(1)

            with patch.object(
                live_hls.time,
                "monotonic",
                return_value=time.monotonic() + 60,
            ):
                publisher._cleanup_retired_directories()

            self.assertFalse(publisher._retired_directories)
            self.assertTrue(publisher._has_rotated_generation)

    def test_audio_uses_a_separate_bounded_queue_from_sampled_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            publisher = LiveHlsPublisher(
                Path(temporary),
                generation=1,
                settings=CameraSettings(audio_enabled=True),
            )
            publisher._started = True
            self.assertTrue(publisher.request_viewer())
            first = self._block(1, 0)
            second = self._block(2, 13_000_000_000)
            frame = FrameEnvelope(
                sequence=1,
                capture_mono_ns=30_000_000_000,
                capture_utc_ns=30_000_000_000,
                timezone="UTC",
                generation=1,
                jpeg_bytes=b"jpeg",
                audio_blocks=(first, second),
                audio_available=True,
            )

            self.assertTrue(publisher.submit(frame))
            status = publisher.status

        self.assertEqual(status["audio_queued_bytes"], second.size_bytes)
        self.assertEqual(status["dropped_audio_blocks"], 1)
        queued_frame = publisher._queue[0]
        self.assertEqual(queued_frame.audio_blocks, ())
        self.assertEqual(publisher._queued_bytes, len(frame.jpeg_bytes))
        audio_frame, audio_blocks, _sample_slot = publisher._take_next_frame(0, True)
        self.assertIsNotNone(audio_frame)
        assert audio_frame is not None
        self.assertEqual(audio_frame.sequence, 1)
        self.assertEqual(audio_blocks, (second,))

    def test_viewer_demand_expires_and_releases_preview_and_audio_queues(self):
        clock = FakeClock(elapsed_mono_ns=100)
        publisher = LiveHlsPublisher("/tmp/live-demand", generation=1, clock=clock)
        publisher._started = True
        self.assertTrue(publisher.request_viewer())
        frame = FrameEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000_000,
            capture_utc_ns=1_000_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
        )
        self.assertTrue(publisher.submit(frame))
        self.assertEqual(len(publisher._queue), 1)

        clock.advance(10_000_000_000)
        self.assertFalse(publisher.viewer_demand_active)
        self.assertFalse(publisher.submit(frame))
        self.assertFalse(publisher._queue)
        self.assertEqual(publisher._queued_bytes, 0)
        self.assertTrue(publisher._viewer_pause_requested)
        self.assertEqual(publisher.status["reason"], "no_viewers")

    def test_viewer_heartbeat_does_not_bypass_live_fps_limit(self):
        publisher = LiveHlsPublisher("/tmp/live-heartbeat-fps", generation=1)
        publisher._started = True
        publisher._requested_quality_index = 1  # 5 fps
        self.assertTrue(publisher.request_viewer())

        first = FrameEnvelope(
            sequence=1,
            capture_mono_ns=1_000_000_000,
            capture_utc_ns=1_000_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
        )
        within_interval = replace(
            first,
            sequence=2,
            capture_mono_ns=1_100_000_000,
            capture_utc_ns=1_100_000_000,
        )
        at_interval = replace(
            first,
            sequence=3,
            capture_mono_ns=1_200_000_000,
            capture_utc_ns=1_200_000_000,
        )

        self.assertTrue(publisher.submit(first))
        self.assertTrue(publisher.request_viewer())
        self.assertFalse(publisher.submit(within_interval))
        self.assertTrue(publisher.submit(at_interval))

    def test_status_polling_does_not_create_live_viewer_demand(self):
        publisher = LiveHlsPublisher("/tmp/live-demand-status", generation=1)
        publisher._started = True

        for _ in range(5):
            status = publisher.status

        self.assertFalse(status["viewer_demand"])
        self.assertEqual(status["reason"], "no_viewers")
        self.assertFalse(publisher._queue)

    def test_encoder_opens_only_for_demand_and_closes_after_expiry(self):
        class FakeStream:
            def __init__(self):
                self.codec_context = SimpleNamespace()
                self.encoded = 0

            def encode(self, _frame):
                self.encoded += 1
                return ()

        class FakeContainer:
            def __init__(self):
                self.stream = FakeStream()
                self.closed = False

            def add_stream(self, _codec, *, rate):
                del rate
                return self.stream

            def mux(self, _packet):
                pass

            def close(self):
                self.closed = True

        class FakeFrame:
            def __init__(self, canvas):
                self.canvas = canvas

            def reformat(self, **_options):
                return self

        class FakeCanvas:
            pass

        class FakeVideoFrame:
            @staticmethod
            def from_image(image):
                frame = FakeFrame(image)
                rendered_frames.append(weakref.ref(frame))
                return frame

        class FakeAv:
            VideoFrame = FakeVideoFrame
            video = SimpleNamespace(
                frame=SimpleNamespace(PictureType=SimpleNamespace(I="I"))
            )

        clock = FakeClock(elapsed_mono_ns=100)
        containers = []
        rendered_canvases = []
        rendered_frames = []
        av = FakeAv()

        def fake_import(name):
            if name == "av":
                return av
            raise AssertionError(f"unexpected import: {name}")

        def wait_for(predicate):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("timed out waiting for live worker state")

        def render_preview(_envelope, _quality_index):
            canvas = FakeCanvas()
            rendered_canvases.append(weakref.ref(canvas))
            return canvas

        with tempfile.TemporaryDirectory() as temporary:
            publisher = LiveHlsPublisher(
                Path(temporary) / "camera-1" / "generation-1",
                generation=1,
                clock=clock,
            )

            def record_open(_av, _playlist, _directory, _quality_index, **_kwargs):
                container = FakeContainer()
                containers.append(container)
                return container, container.stream

            frame = FrameEnvelope(
                sequence=1,
                capture_mono_ns=1_000_000_000,
                capture_utc_ns=1_000_000_000,
                timezone="UTC",
                generation=1,
                jpeg_bytes=b"jpeg",
            )
            with (
                patch.object(
                    live_hls.importlib, "import_module", side_effect=fake_import
                ),
                patch.object(publisher, "_open_output", side_effect=record_open),
                patch.object(publisher, "_render_preview", side_effect=render_preview),
            ):
                publisher.start()
                time.sleep(0.05)
                self.assertEqual(containers, [])

                self.assertTrue(publisher.request_viewer())
                self.assertTrue(publisher.submit(frame))
                wait_for(lambda: bool(containers) and containers[0].stream.encoded > 0)
                first_generation = publisher.generation

                clock.advance(live_hls._VIEWER_DEMAND_TTL_NS)
                wait_for(lambda: containers[0].closed)
                wait_for(
                    lambda: all(
                        reference() is None
                        for reference in (*rendered_canvases, *rendered_frames)
                    )
                )
                encoded_at_expiry = containers[0].stream.encoded
                time.sleep(0.05)
                self.assertEqual(containers[0].stream.encoded, encoded_at_expiry)
                self.assertFalse(publisher.submit(frame))

                self.assertTrue(publisher.request_viewer())
                resumed_frame = replace(
                    frame,
                    sequence=2,
                    capture_mono_ns=11_000_000_000,
                    capture_utc_ns=11_000_000_000,
                )
                self.assertTrue(publisher.submit(resumed_frame))
                wait_for(
                    lambda: len(containers) == 2 and containers[1].stream.encoded > 0
                )
                self.assertGreater(publisher.generation, first_generation)
                publisher.close()

    def test_recording_active_starts_at_middle_quality(self):
        publisher = LiveHlsPublisher(
            "/tmp/live-recording-priority",
            generation=1,
            recording_active=True,
        )
        self.assertEqual(publisher.status["quality"], "320x180@5")
        self.assertFalse(publisher.status["quality_change_pending"])

    def test_load_sheds_live_quality_then_pauses_and_recovers(self):
        publisher = LiveHlsPublisher(
            "/tmp/live-adaptive",
            generation=1,
            recording_active=True,
        )
        publisher._started = True
        self._update_quality(publisher, 0, 2_000_000_000)
        self._update_quality(publisher, 999_999_999, 2_000_000_000)
        self.assertEqual(publisher._requested_quality_index, 1)
        self._update_quality(publisher, 1_000_000_000, 2_000_000_000)
        self.assertEqual(publisher._requested_quality_index, 2)
        self._update_quality(publisher, 1_999_999_999, 2_000_000_000)
        self.assertFalse(publisher._pause_requested)
        self._update_quality(publisher, 2_000_000_000, 2_000_000_000)
        self.assertTrue(publisher._pause_requested)
        self.assertEqual(publisher.status["reason"], "paused_to_protect_recording")

        publisher._paused = True
        publisher._pause_requested = False
        publisher._healthy_since_mono_ns = None
        self._update_quality(publisher, 20_000_000_000, 0)
        self._update_quality(publisher, 49_999_999_999, 0)
        self.assertTrue(publisher._paused)
        self._update_quality(publisher, 50_000_000_000, 0)
        self.assertFalse(publisher._paused)
        self.assertEqual(publisher._requested_quality_index, 2)
        self._update_quality(publisher, 79_999_999_999, 0)
        self.assertEqual(publisher._requested_quality_index, 2)
        self._update_quality(publisher, 80_000_000_000, 0)
        self.assertEqual(publisher._requested_quality_index, 1)

        self.assertEqual(publisher.status["quality"], "320x180@5")
        self.assertEqual(publisher._settings, CameraSettings())

    @staticmethod
    def _update_quality(
        publisher: LiveHlsPublisher, now_ns: int, recording_delay_ns: int
    ) -> None:
        with patch.object(live_hls.time, "monotonic_ns", return_value=now_ns):
            with publisher._condition:
                publisher._update_adaptive_quality_locked(recording_delay_ns)

    def test_preview_canvas_has_the_exact_output_resolution_for_each_tier(self):
        class FakeSource:
            width = 800
            height = 450

            def thumbnail(self, size):
                self.width = min(self.width, size[0])
                self.height = min(self.height, size[1])

        class FakeCanvas:
            def __init__(self, size):
                self.size = size

            def paste(self, _image, _position):
                return None

        source = FakeSource()
        image_module = ModuleType("PIL.Image")
        image_module.new = lambda _mode, size, **_kwargs: FakeCanvas(size)
        pillow_module = ModuleType("PIL")
        pillow_module.__path__ = []
        publisher = LiveHlsPublisher(
            "/tmp/live-resolution",
            generation=1,
            settings=CameraSettings(overlay_enabled=False),
        )
        with (
            patch.object(live_hls, "_decode_live_image", return_value=source),
            patch.dict(
                "sys.modules",
                {"PIL": pillow_module, "PIL.Image": image_module},
            ),
        ):
            resolutions = tuple(
                publisher._render_preview(self._frame(), index).size
                for index in range(3)
            )
        self.assertEqual(resolutions, ((640, 360), (320, 180), (320, 180)))

    @staticmethod
    def _frame() -> FrameEnvelope:
        return FrameEnvelope(
            sequence=1,
            capture_mono_ns=time.monotonic_ns(),
            capture_utc_ns=1_797_523_200_000_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
        )

    @staticmethod
    def _block(sequence: int, mono_ns: int) -> AudioEnvelope:
        return AudioEnvelope(
            sequence=sequence,
            capture_mono_ns=mono_ns,
            capture_utc_ns=mono_ns,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0" * 1_200_000,
        )


if __name__ == "__main__":
    unittest.main()

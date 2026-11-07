from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from raspi_recorder.adapters.live_preview import LivePreviewPublisher
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import FrameEnvelope
from tests.fakes import FakeClock


class LivePreviewTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(wall_utc_ns=1_800_000_000_000_000_000)
        self.publisher = LivePreviewPublisher(
            generation=1,
            settings=CameraSettings(),
            clock=self.clock,
        )
        self.addCleanup(self.publisher.close)

    def _frame(self, sequence: int) -> FrameEnvelope:
        return FrameEnvelope(
            sequence=sequence,
            capture_mono_ns=self.clock.monotonic_ns(),
            capture_utc_ns=self.clock.utc_ns(),
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"synthetic-frame",
        )

    def test_single_pending_frame_is_shared_at_one_fps_and_release_clears_it(self):
        rendered: list[int] = []
        rendered_once = threading.Event()
        rendered_twice = threading.Event()

        def render(frame, _settings):
            rendered.append(frame.sequence)
            if len(rendered) == 1:
                rendered_once.set()
            if len(rendered) == 2:
                rendered_twice.set()
            return f"jpeg-{frame.sequence}".encode()

        with patch.object(self.publisher, "_render", side_effect=render):
            self.publisher.start()
            self.assertTrue(self.publisher.request_viewer())
            first = self._frame(1)
            self.assertTrue(self.publisher.submit(first))
            self.assertTrue(rendered_once.wait(1))
            self.assertEqual(self.publisher.fresh_image(), b"jpeg-1")
            self.assertTrue(self.publisher.status["available"])

            self.clock.advance(500_000_000)
            self.assertTrue(self.publisher.submit(self._frame(2)))
            self.assertTrue(self.publisher.submit(self._frame(3)))
            self.assertFalse(rendered_twice.wait(0.05))
            self.clock.advance(500_000_000)
            self.assertTrue(self.publisher.request_viewer())
            self.assertTrue(rendered_twice.wait(1))

        self.assertEqual(rendered, [1, 3])
        self.publisher.release_viewer()
        self.assertFalse(self.publisher.viewer_demand_active)
        self.assertIsNone(self.publisher.fresh_image())
        self.assertFalse(self.publisher.submit(self._frame(4)))

    def test_release_discards_a_frame_rendered_after_the_viewer_leaves(self):
        rendering = threading.Event()
        finish_render = threading.Event()
        render_returned = threading.Event()

        def render(_frame, _settings):
            rendering.set()
            self.assertTrue(finish_render.wait(1))
            render_returned.set()
            return b"late-jpeg"

        with patch.object(self.publisher, "_render", side_effect=render):
            self.publisher.start()
            self.assertTrue(self.publisher.request_viewer())
            self.assertTrue(self.publisher.submit(self._frame(1)))
            self.assertTrue(rendering.wait(1))
            self.publisher.release_viewer()
            finish_render.set()
            self.assertTrue(render_returned.wait(1))
            deadline = time.monotonic() + 1
            while self.publisher._thread is not None and time.monotonic() < deadline:
                with self.publisher._condition:
                    if self.publisher._jpeg is not None:
                        break
                    if not self.publisher._thread.is_alive():
                        break
                    self.publisher._condition.wait(0.01)

        self.assertIsNone(self.publisher.fresh_image())
        self.assertFalse(self.publisher.status["available"])
        self.assertTrue(self.publisher.request_viewer())
        self.assertTrue(self.publisher.viewer_demand_active)


if __name__ == "__main__":
    unittest.main()

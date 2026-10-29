from __future__ import annotations

import errno
import json
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.live_hls import LiveHlsPublisher
from raspi_recorder.adapters.pyav_media import MediaWriterError, PyAvMp4Writer
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope

try:
    import av
    from PIL import Image as image_module
except ImportError:
    av = None
    image_module = None

_AUDIO_BLOCK_NS = 10_000_000
_AUDIO_SAMPLES = 480


def _jpeg() -> bytes:
    assert image_module is not None
    image = image_module.new("RGB", (320, 240), color=(32, 96, 160))
    output = BytesIO()
    image.save(output, format="JPEG", quality=85)
    return output.getvalue()


def _audio_block(sequence: int, mono_ns: int) -> AudioEnvelope:
    return AudioEnvelope(
        sequence=sequence,
        capture_mono_ns=mono_ns,
        capture_utc_ns=mono_ns + 1_700_000_000_000_000_000,
        timezone="UTC",
        generation=1,
        sample_rate=48_000,
        pcm_s16le=b"\0\0" * _AUDIO_SAMPLES,
    )


def _video_frame(sequence: int, mono_ns: int, audio=()) -> FrameEnvelope:
    return FrameEnvelope(
        sequence=sequence,
        capture_mono_ns=mono_ns,
        capture_utc_ns=mono_ns + 1_700_000_000_000_000_000,
        timezone="UTC",
        generation=1,
        jpeg_bytes=_jpeg(),
        audio_blocks=tuple(audio),
        audio_available=True,
    )


def _decode_counts(container) -> tuple[int, int]:
    video_frames = 0
    audio_samples = 0
    for packet in container.demux():
        stream_type = packet.stream.type
        for frame in packet.decode():
            if stream_type == "video":
                video_frames += 1
            elif stream_type == "audio":
                audio_samples += frame.samples
    return video_frames, audio_samples


@unittest.skipUnless(
    av is not None and image_module is not None, "PyAV and Pillow required"
)
class AudioMediaIntegrationTests(unittest.TestCase):
    def test_video_encoder_uses_one_thread_while_capture_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            writer = PyAvMp4Writer(
                Path(temporary),
                "thread-limit.mp4",
                CameraSettings(width=320, height=240, fps=15, overlay_enabled=False),
                camera_id=1,
                mode="event",
                logical_event_id="thread-limit",
                generation=1,
            )
            try:
                assert writer._stream is not None
                self.assertEqual(writer._stream.codec_context.thread_count, 1)
            finally:
                writer.abort("test_cleanup")

    def test_writer_manifest_records_identity_once_then_first_frame_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_writes: list[tuple[str, int]] = []
            progress_written = threading.Event()
            persist_manifest = PyAvMp4Writer._persist_manifest_json

            def track_manifest(writer, serialized):
                payload = json.loads(serialized)
                manifest_writes.append((payload["state"], payload["frame_count"]))
                persist_manifest(writer, serialized)
                if payload["frame_count"] == 1:
                    progress_written.set()

            with patch.object(PyAvMp4Writer, "_persist_manifest_json", track_manifest):
                writer = PyAvMp4Writer(
                    root,
                    "manifest-test.mp4",
                    CameraSettings(
                        width=320, height=240, fps=15, overlay_enabled=False
                    ),
                    camera_id=1,
                    mode="continuous",
                    generation=1,
                )
                try:
                    self.assertEqual(manifest_writes, [("writing", 0)])
                    writer.write(_video_frame(1, 30_000_000_000))
                    self.assertTrue(progress_written.wait(2))
                    self.assertEqual(manifest_writes, [("writing", 0), ("writing", 1)])
                finally:
                    writer.abort("test_cleanup")

    def test_manifest_progress_io_does_not_block_frames_and_final_drain_persists_latest(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            progress_started = threading.Event()
            release_progress = threading.Event()
            progress_payloads: list[dict[str, object]] = []
            persist_manifest = PyAvMp4Writer._persist_manifest_json

            def block_first_progress(writer, serialized):
                payload = json.loads(serialized)
                if payload["state"] == "writing" and payload["frame_count"] > 0:
                    progress_payloads.append(payload)
                    if len(progress_payloads) == 1:
                        progress_started.set()
                        if not release_progress.wait(3):
                            raise TimeoutError("test did not release manifest worker")
                persist_manifest(writer, serialized)

            with patch.object(
                PyAvMp4Writer, "_persist_manifest_json", block_first_progress
            ):
                writer = PyAvMp4Writer(
                    root,
                    "manifest-async-test.mp4",
                    CameraSettings(
                        width=320, height=240, fps=15, overlay_enabled=False
                    ),
                    camera_id=1,
                    mode="continuous",
                    generation=1,
                )
                try:
                    writer.write(_video_frame(1, 30_000_000_000))
                    self.assertTrue(progress_started.wait(2))

                    producer_errors: list[BaseException] = []

                    def write_latest_frame():
                        try:
                            writer.write(_video_frame(2, 35_000_000_000))
                        except BaseException as exc:  # captured for the test thread
                            producer_errors.append(exc)

                    producer = threading.Thread(target=write_latest_frame)
                    producer.start()
                    producer.join(3)
                    self.assertFalse(
                        producer.is_alive(), "frame write waited on manifest I/O"
                    )
                    self.assertEqual(producer_errors, [])
                    release_progress.set()
                    artifact = writer.finalize()
                    self.assertEqual(
                        [payload["last_sequence"] for payload in progress_payloads],
                        [1, 2],
                    )
                    with av.open(artifact.path) as container:
                        decoded_frames = list(container.decode(video=0))
                    self.assertEqual(len(decoded_frames), 2)
                    first_pts = decoded_frames[0].pts
                    second_pts = decoded_frames[1].pts
                    first_time_base = decoded_frames[0].time_base
                    second_time_base = decoded_frames[1].time_base
                    self.assertIsNotNone(first_pts)
                    self.assertIsNotNone(second_pts)
                    self.assertIsNotNone(first_time_base)
                    self.assertIsNotNone(second_time_base)
                    decoded_gap_seconds = float(
                        second_pts * second_time_base - first_pts * first_time_base
                    )
                    self.assertAlmostEqual(decoded_gap_seconds, 5.0, delta=0.001)
                finally:
                    release_progress.set()
                    writer.abort("test_cleanup")

    def test_pyav_mp4_muxes_and_decodes_h264_and_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = CameraSettings(
                width=320,
                height=240,
                fps=15,
                audio_enabled=True,
                overlay_enabled=False,
            )
            origin = 30_000_000_000
            writer = PyAvMp4Writer(
                root,
                "audio-test.mp4",
                settings,
                camera_id=1,
                mode="continuous",
                audio_track_enabled=True,
                timeline_origin_mono_ns=origin,
                generation=1,
            )
            audio_sequence = 1
            audio_mono_ns = origin
            validation_modes = []
            validate_path = writer._validate_mp4_path

            def record_validation(path, *, full_decode):
                validation_modes.append(full_decode)
                return validate_path(path, full_decode=full_decode)

            writer._validate_mp4_path = record_validation

            try:
                writer.write_audio(
                    _audio_block(audio_sequence, origin - _AUDIO_BLOCK_NS)
                )
                for video_sequence in range(1, 31):
                    video_mono_ns = origin + (video_sequence - 1) * 66_666_667
                    while audio_mono_ns + _AUDIO_BLOCK_NS <= video_mono_ns:
                        audio_sequence += 1
                        writer.write_audio(_audio_block(audio_sequence, audio_mono_ns))
                        audio_mono_ns += _AUDIO_BLOCK_NS
                    writer.write(_video_frame(video_sequence, video_mono_ns))
                while audio_mono_ns < origin + 2_000_000_000:
                    audio_sequence += 1
                    writer.write_audio(_audio_block(audio_sequence, audio_mono_ns))
                    audio_mono_ns += _AUDIO_BLOCK_NS
                artifact = writer.finalize()
            except Exception:
                writer.abort("integration_test_failed")
                raise

            self.assertEqual(artifact.frame_count, 30)
            self.assertEqual(writer._audio_sample_count, 200 * _AUDIO_SAMPLES)
            self.assertEqual(validation_modes, [False])
            writer.validate_full_decode()
            self.assertEqual(validation_modes, [False, True])
            assert av is not None
            with av.open(artifact.path, mode="r", format="mp4") as container:
                self.assertEqual(len(tuple(container.streams.video)), 1)
                self.assertEqual(len(tuple(container.streams.audio)), 1)
                video_frames, audio_samples = _decode_counts(container)
            self.assertEqual(video_frames, 30)
            self.assertGreater(audio_samples, 0)
            with av.open(artifact.path, mode="r", format="mp4") as container:
                first_video = next(container.decode(video=0))
            rgb = first_video.to_image().getpixel((160, 120))
            for actual, expected in zip(rgb, (32, 96, 160), strict=True):
                self.assertLessEqual(abs(actual - expected), 12)

    def test_bounded_validation_failure_keeps_partial_abnormal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = CameraSettings(
                width=320,
                height=240,
                fps=15,
                overlay_enabled=False,
            )
            writer = PyAvMp4Writer(
                root,
                "invalid-test.mp4",
                settings,
                camera_id=1,
                mode="continuous",
                generation=1,
            )
            writer.write(_video_frame(1, 30_000_000_000))

            with patch.object(
                writer,
                "_validate_mp4",
                side_effect=RuntimeError("forced_validation_failure"),
            ):
                with self.assertRaisesRegex(
                    MediaWriterError, "MP4を正常確定できませんでした"
                ):
                    writer.finalize()

            self.assertFalse((root / "invalid-test.mp4").exists())
            self.assertTrue((root / "invalid-test.mp4.partial").is_file())
            manifest = json.loads(
                (root / "invalid-test.mp4.recording.json").read_text("utf-8")
            )
            self.assertEqual(manifest["state"], "abnormal")
            self.assertEqual(manifest["reason"], "RuntimeError")

    def test_empty_recording_cannot_be_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = PyAvMp4Writer(
                root,
                "empty-test.mp4",
                CameraSettings(width=320, height=240, fps=15, overlay_enabled=False),
                camera_id=1,
                mode="continuous",
                generation=1,
            )

            with self.assertRaisesRegex(MediaWriterError, "frameのないMP4"):
                writer.finalize()

            self.assertFalse((root / "empty-test.mp4").exists())
            self.assertTrue((root / "empty-test.mp4.partial").is_file())
            manifest = json.loads(
                (root / "empty-test.mp4.recording.json").read_text("utf-8")
            )
            self.assertEqual(manifest["state"], "abnormal")
            self.assertEqual(manifest["reason"], "empty_recording")

    def test_truncated_mp4_fixture_is_kept_partial_abnormal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial_path = root / "truncated-test.mp4.partial"
            writer = PyAvMp4Writer(
                root,
                "truncated-test.mp4",
                CameraSettings(width=320, height=240, fps=15, overlay_enabled=False),
                camera_id=1,
                mode="continuous",
                generation=1,
            )
            for sequence in range(1, 31):
                mono_ns = 30_000_000_000 + (sequence - 1) * 66_666_667
                writer.write(_video_frame(sequence, mono_ns))

            close_resources = writer._close_resources

            def close_then_truncate() -> None:
                close_resources()
                with partial_path.open("r+b") as handle:
                    handle.truncate(31)

            with patch.object(
                writer, "_close_resources", side_effect=close_then_truncate
            ):
                with self.assertRaisesRegex(
                    MediaWriterError, "MP4を正常確定できませんでした"
                ) as raised:
                    writer.finalize()

            self.assertFalse((root / "truncated-test.mp4").exists())
            self.assertTrue(partial_path.is_file())
            self.assertEqual(partial_path.stat().st_size, 31)
            manifest = json.loads(
                (root / "truncated-test.mp4.recording.json").read_text("utf-8")
            )
            self.assertEqual(manifest["state"], "abnormal")
            self.assertIsNotNone(raised.exception.__cause__)
            self.assertEqual(
                manifest["reason"], type(raised.exception.__cause__).__name__
            )

    def test_media_fsync_failure_keeps_partial_abnormal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = PyAvMp4Writer(
                root,
                "fsync-test.mp4",
                CameraSettings(width=320, height=240, fps=15, overlay_enabled=False),
                camera_id=1,
                mode="continuous",
                generation=1,
            )
            writer.write(_video_frame(1, 30_000_000_000))

            with patch.object(writer, "_fsync_file", side_effect=OSError("disk_error")):
                with self.assertRaisesRegex(
                    MediaWriterError, "MP4を正常確定できませんでした"
                ):
                    writer.finalize()

            self.assertFalse((root / "fsync-test.mp4").exists())
            self.assertTrue((root / "fsync-test.mp4.partial").is_file())
            manifest = json.loads(
                (root / "fsync-test.mp4.recording.json").read_text("utf-8")
            )
            self.assertEqual(manifest["state"], "abnormal")
            self.assertEqual(manifest["reason"], "OSError")

    def test_full_volume_preserves_partial_and_primary_manifest_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = PyAvMp4Writer(
                root,
                "manifest-full.mp4",
                CameraSettings(width=320, height=240, fps=15, overlay_enabled=False),
                camera_id=1,
                mode="continuous",
                generation=1,
            )
            writer.write(_video_frame(1, 30_000_000_000))
            manifest_states = []

            def fail_manifest(state, **_fields):
                manifest_states.append(state)
                if state in {"finalizing", "abnormal"}:
                    raise OSError(errno.ENOSPC, "simulated full volume")

            with patch.object(writer, "_write_manifest", side_effect=fail_manifest):
                with self.assertRaisesRegex(
                    MediaWriterError, "MP4を正常確定できませんでした"
                ) as raised:
                    writer.finalize()

            self.assertEqual(manifest_states, ["finalizing", "abnormal"])
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(raised.exception.__cause__.errno, errno.ENOSPC)
            notes = getattr(raised.exception.__cause__, "__notes__", [])
            self.assertTrue(
                any("abnormal manifest update failed" in note for note in notes)
            )
            self.assertFalse((root / "manifest-full.mp4").exists())
            self.assertTrue((root / "manifest-full.mp4.partial").is_file())
            persisted_manifest = json.loads(
                (root / "manifest-full.mp4.recording.json").read_text("utf-8")
            )
            self.assertEqual(persisted_manifest["state"], "writing")

    def test_live_hls_muxes_shared_audio_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = CameraSettings(
                width=320,
                height=240,
                fps=10,
                audio_enabled=True,
                overlay_enabled=False,
            )
            publisher = LiveHlsPublisher(
                root / "live",
                generation=1,
                settings=settings,
            )
            origin = 50_000_000_000
            audio_sequence = 0
            audio_mono_ns = origin
            publisher.start()
            self.assertTrue(publisher.request_viewer())

            try:
                for video_sequence in range(1, 31):
                    video_mono_ns = origin + (video_sequence - 1) * 100_000_000
                    audio_blocks = []
                    while audio_mono_ns + _AUDIO_BLOCK_NS <= video_mono_ns:
                        audio_sequence += 1
                        audio_blocks.append(_audio_block(audio_sequence, audio_mono_ns))
                        audio_mono_ns += _AUDIO_BLOCK_NS
                    self.assertTrue(
                        publisher.submit(
                            _video_frame(video_sequence, video_mono_ns, audio_blocks)
                        )
                    )
                    time.sleep(0.1)
                with publisher._condition:
                    publisher._stop = True
                    publisher._condition.notify_all()
                self.assertIsNotNone(publisher._thread)
                assert publisher._thread is not None
                publisher._thread.join(20.0)
                self.assertFalse(publisher._thread.is_alive())
                status = publisher.status
                self.assertIsNone(status["error"])
                self.assertGreater(status["encoded_audio_blocks"], 0)
                self.assertTrue(publisher._playlist.is_file())
                segments = sorted(publisher.directory.glob("segment*.ts"))
                self.assertTrue(segments)
                assert av is not None
                video_frames = 0
                audio_samples = 0
                for segment in segments:
                    with av.open(str(segment), mode="r", format="mpegts") as container:
                        if not container.streams.video or not container.streams.audio:
                            continue
                        decoded_video, decoded_audio = _decode_counts(container)
                        video_frames += decoded_video
                        audio_samples += decoded_audio
                self.assertGreater(video_frames, 0)
                self.assertGreater(audio_samples, 0)
            finally:
                publisher.close()

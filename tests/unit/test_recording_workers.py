import unittest
from datetime import UTC, datetime
from threading import Event

from raspi_recorder.application.frame_ingress import (
    CameraFramePipeline,
)
from raspi_recorder.application.recording_workers import (
    _RecordingPart,
    _write_frame_to_part,
    record_continuous_session,
)
from raspi_recorder.domain.frames import AudioEnvelope, FrameEnvelope, FrameRing
from raspi_recorder.domain.recordings import RecordingArtifact
from tests.fakes import FakeClock


def frame(sequence: int, mono_ns: int, utc_ns: int = 0) -> FrameEnvelope:
    return FrameEnvelope(
        sequence=sequence,
        capture_mono_ns=mono_ns,
        capture_utc_ns=utc_ns,
        timezone="UTC",
        generation=1,
        jpeg_bytes=bytes([sequence % 255 + 1]),
    )


class FakeSegmentWriter:
    def __init__(self, part_index: int) -> None:
        self.part_index = part_index
        self.frames: list[FrameEnvelope] = []
        self.audio_blocks: list[AudioEnvelope] = []
        self.audio_track_enabled = False
        self.aborted: str | None = None

    def write(self, envelope: FrameEnvelope) -> None:
        self.frames.append(envelope)

    def write_audio(self, envelope: AudioEnvelope) -> None:
        self.audio_blocks.append(envelope)

    def finalize(self) -> RecordingArtifact:
        if not self.frames:
            raise AssertionError("empty fake part")
        first, last = self.frames[0], self.frames[-1]
        return RecordingArtifact(
            path=f"part-{self.part_index}.mp4",
            frame_count=len(self.frames),
            first_sequence=first.sequence,
            last_sequence=last.sequence,
            first_mono_ns=first.capture_mono_ns,
            last_mono_ns=last.capture_mono_ns,
            first_utc_ns=first.capture_utc_ns,
            last_utc_ns=last.capture_utc_ns,
            size_bytes=len(self.frames),
        )

    def abort(self, reason: str) -> None:
        self.aborted = reason


class RecordingWorkerTests(unittest.TestCase):
    def test_audio_recovery_and_loss_use_separate_physical_track_layouts(self):
        writers: list[FakeSegmentWriter] = []

        def make_writer(part_index: int, first: FrameEnvelope) -> FakeSegmentWriter:
            writer = FakeSegmentWriter(part_index)
            writer.audio_track_enabled = first.audio_available
            writers.append(writer)
            return writer

        part = _RecordingPart()
        first = frame(1, 10, 10)
        recovery_audio = AudioEnvelope(
            sequence=1,
            capture_mono_ns=10,
            capture_utc_ns=10,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )
        recovered = FrameEnvelope(
            2,
            20_000_000,
            20_000_000,
            "UTC",
            1,
            b"jpeg",
            (recovery_audio,),
            True,
        )
        later_audio = AudioEnvelope(
            sequence=2,
            capture_mono_ns=20_000_000,
            capture_utc_ns=20_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )
        failed = FrameEnvelope(
            3,
            30_000_000,
            30_000_000,
            "UTC",
            1,
            b"jpeg",
            (later_audio,),
            False,
        )

        _write_frame_to_part(first, part, make_writer, lambda _: 1_000_000_000)
        _write_frame_to_part(recovered, part, make_writer, lambda _: 1_000_000_000)
        _write_frame_to_part(failed, part, make_writer, lambda _: 1_000_000_000)

        self.assertEqual(
            [writer.audio_track_enabled for writer in writers], [False, True, False]
        )
        self.assertEqual([len(writer.audio_blocks) for writer in writers], [0, 2, 0])
        self.assertEqual([writer.frames[0].sequence for writer in writers], [1, 2, 3])

    def test_audio_block_is_split_at_physical_part_boundary_without_loss(self):
        writers: list[FakeSegmentWriter] = []

        def make_writer(part: int, first: FrameEnvelope) -> FakeSegmentWriter:
            writer = FakeSegmentWriter(part)
            writer.audio_track_enabled = first.audio_available
            writers.append(writer)
            return writer

        part = _RecordingPart()
        first = FrameEnvelope(
            sequence=1,
            capture_mono_ns=50_000_000,
            capture_utc_ns=50_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"a",
            audio_available=True,
        )
        block = AudioEnvelope(
            sequence=1,
            capture_mono_ns=95_000_000,
            capture_utc_ns=95_000_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )
        second = FrameEnvelope(
            sequence=2,
            capture_mono_ns=110_000_000,
            capture_utc_ns=110_000_000,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"b",
            audio_blocks=(block,),
            audio_available=True,
        )

        _write_frame_to_part(first, part, make_writer, lambda _: 100_000_000)
        artifacts = _write_frame_to_part(
            second, part, make_writer, lambda _: 200_000_000
        )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual([item.sample_count for item in writers[0].audio_blocks], [240])
        self.assertEqual([item.sample_count for item in writers[1].audio_blocks], [240])
        self.assertEqual(
            writers[0].audio_blocks[0].pcm_s16le + writers[1].audio_blocks[0].pcm_s16le,
            block.pcm_s16le,
        )
        self.assertEqual(writers[1].frames[0].sequence, 2)

    def test_continuous_session_drains_and_acknowledges_shared_ring_leases(self):
        pipeline = CameraFramePipeline(
            1,
            FrameRing(max_duration_ns=1, max_bytes=10),
            FakeClock(),
        )
        session = pipeline.start_continuous()
        pipeline.push(frame(1, 100))
        pipeline.push(frame(2, 200))
        pipeline.finish_continuous(session)
        writers: list[FakeSegmentWriter] = []

        artifacts: list[RecordingArtifact] = []
        result = record_continuous_session(
            session,
            Event(),
            lambda part, _: writers.append(FakeSegmentWriter(part)) or writers[-1],
            artifact_callback=artifacts.append,
        )

        self.assertIsNone(result)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual([item.sequence for item in writers[0].frames], [1, 2])
        self.assertEqual(pipeline.ring.retained_bytes, 1)

    def test_long_continuous_session_reports_parts_without_returning_an_unbounded_list(
        self,
    ):
        pipeline = CameraFramePipeline(
            1,
            FrameRing(max_duration_ns=1, max_bytes=100),
            FakeClock(),
        )
        session = pipeline.start_continuous()
        writers: list[FakeSegmentWriter] = []
        for sequence in range(1, 21):
            timestamp_ns = (sequence - 1) * 300_000_000_000 + 1
            pipeline.push(frame(sequence, timestamp_ns, 1_000_000_001 + timestamp_ns))
        pipeline.finish_continuous(session)

        completed_parts = 0

        def count_artifact(_artifact: RecordingArtifact) -> None:
            nonlocal completed_parts
            completed_parts += 1

        result = record_continuous_session(
            session,
            Event(),
            lambda part, _: writers.append(FakeSegmentWriter(part)) or writers[-1],
            artifact_callback=count_artifact,
        )

        self.assertIsNone(result)
        self.assertEqual(completed_parts, 20)
        self.assertEqual(pipeline.ring.retained_bytes, 1)

    def test_continuous_worker_splits_on_next_local_five_minute_boundary(self):
        start = datetime(2026, 10, 16, 12, 4, 59, 999_999, tzinfo=UTC)
        utc_ns = (
            int(start.timestamp()) * 1_000_000_000 + start.microsecond * 1_000 + 999
        )
        frames = [
            frame(1, 100, utc_ns),
            frame(2, 101, utc_ns + 1),
            frame(3, 1_000_000_101, utc_ns + 1_000_000_000),
        ]
        writers: list[FakeSegmentWriter] = []

        def make_writer(part: int, _: FrameEnvelope) -> FakeSegmentWriter:
            writer = FakeSegmentWriter(part)
            writers.append(writer)
            return writer

        artifacts: list[RecordingArtifact] = []
        pipeline = CameraFramePipeline(
            1, FrameRing(max_duration_ns=2_000_000_000, max_bytes=10), FakeClock()
        )
        session = pipeline.start_continuous()
        for item in frames:
            pipeline.push(item)
        pipeline.finish_continuous(session)
        result = record_continuous_session(
            session, Event(), make_writer, artifact_callback=artifacts.append
        )

        self.assertIsNone(result)
        self.assertEqual(
            [item.path for item in artifacts], ["part-1.mp4", "part-2.mp4"]
        )
        self.assertEqual(
            [[item.sequence for item in writer.frames] for writer in writers],
            [[1], [2, 3]],
        )


if __name__ == "__main__":
    unittest.main()

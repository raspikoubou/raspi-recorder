from __future__ import annotations

from io import BytesIO

import pytest

from raspi_recorder.adapters.jpeg_stream import JpegStream


@pytest.mark.parametrize("progressive", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
def test_jpeg_metadata_markers_do_not_split_real_frames(progressive, chunk_size):
    image = pytest.importorskip("PIL.Image")
    output = BytesIO()
    image.new("RGB", (32, 24), color=(200, 25, 80)).save(
        output, format="JPEG", progressive=progressive
    )
    jpeg = output.getvalue()
    embedded = b"metadata-\xff\xd8thumbnail\xff\xd9-end"
    jpeg = (
        jpeg[:2]
        + b"\xff\xe1"
        + (len(embedded) + 2).to_bytes(2, "big")
        + embedded
        + jpeg[2:]
    )
    stream = b"padding\xff" + jpeg + b"\x00\x00" + jpeg
    parser = JpegStream()
    frames = []
    for start in range(0, len(stream), chunk_size):
        frames.extend(parser.feed(stream[start : start + chunk_size]))
    assert frames == [jpeg, jpeg]
    assert parser.dropped_frames == 0
    for frame in frames:
        with image.open(BytesIO(frame)) as decoded:
            decoded.load()
            assert decoded.size == (32, 24)


def test_entropy_stuffing_and_restart_markers_are_not_frame_boundaries():
    jpeg = b"\xff\xd8\xff\xda\x00\x02stuff\xff\x00\xd9\xff\xd0data\xff\xd9"
    assert list(JpegStream().feed(jpeg)) == [jpeg]


def test_corruption_and_oversized_incomplete_frame_allow_resynchronization():
    parser = JpegStream(max_frame_bytes=32)
    assert list(parser.feed(b"\xff\xd8\xff\xda\x00\x02" + b"a" * 40)) == []
    frame = b"\xff\xd8\xff\xda\x00\x02ok\xff\xd9"
    assert list(parser.feed(b"\xff\xd8\xff\xe1\x00\x01" + frame)) == [frame]
    assert parser.dropped_frames == 2

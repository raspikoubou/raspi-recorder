"""連結MJPEGを、segment内の埋め込みマーカーを誤認せず分割する。"""

from __future__ import annotations

from collections.abc import Iterator


class JpegStream:
    """長さ付きsegmentとentropy byte stuffingを扱う有界framer。"""

    def __init__(self, max_frame_bytes: int = 8 * 1024 * 1024) -> None:
        if type(max_frame_bytes) is not int or max_frame_bytes < 4:
            raise ValueError("max_frame_bytesは4以上の整数が必要です")
        self._maximum = max_frame_bytes
        self._buffer = bytearray()
        self._position = 0
        self._entropy = False
        self.dropped_frames = 0

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """完全なJPEGだけを返し、未完の1フレームは次の入力まで保持する。"""
        self._buffer.extend(chunk)
        while True:
            if self._position == 0:
                start = self._buffer.find(b"\xff\xd8")
                if start < 0:
                    self._buffer[:] = b"\xff" if self._buffer.endswith(b"\xff") else b""
                    return
                del self._buffer[:start]
                self._position = 2
                self._entropy = False
            position = self._position
            if self._entropy:
                marker_start = self._buffer.find(b"\xff", position)
                if marker_start < 0:
                    self._position = len(self._buffer)
                    break
                position = marker_start
                self._position = position
            if position + 1 >= len(self._buffer):
                break
            if self._buffer[position] != 0xFF:
                self._discard()
                continue
            marker = self._buffer[position + 1]
            if marker == 0xFF:  # fill byte
                self._position += 1
                continue
            if self._entropy and (marker == 0 or 0xD0 <= marker <= 0xD7):
                self._position = position + 2
                continue
            if marker == 0xD9:
                end = position + 2
                if end <= self._maximum:
                    frame = bytes(self._buffer[:end])
                    del self._buffer[:end]
                    self._position = 0
                    yield frame
                else:
                    self._discard()
                continue
            if marker in (0, 0xD8) or 0xD0 <= marker <= 0xD7:
                self._discard()
                continue
            if marker == 0x01:  # stand-alone TEM marker
                self._position = position + 2
                continue
            if position + 4 > len(self._buffer):
                break
            length = int.from_bytes(self._buffer[position + 2 : position + 4], "big")
            if length < 2:
                self._discard()
                continue
            segment_end = position + 2 + length
            if segment_end > len(self._buffer):
                break
            self._position = segment_end
            self._entropy = marker == 0xDA
        if len(self._buffer) > self._maximum:
            self._discard()

    def _discard(self) -> None:
        """破損frameを捨て、次のSOIから再同期する。"""
        self.dropped_frames += 1
        next_start = self._buffer.find(b"\xff\xd8", 2)
        if next_start >= 0:
            del self._buffer[:next_start]
        else:
            self._buffer[:] = b"\xff" if self._buffer.endswith(b"\xff") else b""
        self._position = 0
        self._entropy = False

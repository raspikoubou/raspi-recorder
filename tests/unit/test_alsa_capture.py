from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from raspi_recorder.adapters.alsa_capture import (
    AlsaAudioDiscovery,
    AlsaCaptureDevice,
    AlsaCaptureSource,
    AudioBlockBuffer,
)
from raspi_recorder.domain.frames import AudioEnvelope
from tests.fakes import FakeClock


class FakeTimezone:
    def name(self) -> str:
        return "UTC"


class AlsaAudioDiscoveryTests(unittest.TestCase):
    def test_resolves_stable_usb_key_to_current_card_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sound = root / "class" / "sound"
            sound.mkdir(parents=True)
            usb = root / "devices" / "usb1" / "1-1.2"
            (usb / "idVendor").parent.mkdir(parents=True)
            (usb / "idVendor").write_text("0411\n", encoding="ascii")
            (usb / "idProduct").write_text("02da\n", encoding="ascii")
            interface = usb / "1-1.2:1.2"
            interface.mkdir()
            (interface / "bInterfaceNumber").write_text("02\n", encoding="ascii")
            first = interface / "sound" / "card7" / "pcmC7D0c"
            first.mkdir(parents=True)
            (sound / "pcmC7D0c").symlink_to(first)
            (sound / "card7").mkdir()
            (sound / "card7" / "id").write_text("Camera\n", encoding="ascii")

            discovery = AlsaAudioDiscovery(sound)
            found = discovery.enumerate()
            self.assertEqual(len(found), 1)
            stable_key = found[0].interface_key
            self.assertEqual(found[0].ffmpeg_device, "plughw:7,0")

            (sound / "pcmC7D0c").unlink()
            (sound / "card7").rename(sound / "card3")
            second = interface / "sound" / "card3" / "pcmC3D0c"
            second.parent.mkdir(parents=True)
            second.mkdir()
            (sound / "pcmC3D0c").symlink_to(second)
            (sound / "card3" / "id").write_text("Camera\n", encoding="ascii")

            resolved = discovery.resolve(stable_key)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.interface_key, stable_key)
        self.assertEqual(resolved.ffmpeg_device, "plughw:3,0")


class AudioBlockBufferTests(unittest.TestCase):
    def test_buffer_is_bounded_and_returns_only_complete_prior_blocks(self):
        buffer = AudioBlockBuffer(max_bytes=960)
        first = self.block(1, 0)
        second = self.block(2, 10_000_000)
        buffer.append(first)
        buffer.append(second)

        self.assertEqual(buffer.dropped_blocks, 1)
        self.assertEqual(buffer.size_bytes, 960)
        self.assertEqual(buffer.take_before(second.end_mono_ns - 1), ())
        self.assertEqual(buffer.take_before(second.end_mono_ns), (second,))
        self.assertEqual(buffer.size_bytes, 0)

    @staticmethod
    def block(sequence: int, mono_ns: int) -> AudioEnvelope:
        return AudioEnvelope(
            sequence=sequence,
            capture_mono_ns=mono_ns,
            capture_utc_ns=mono_ns + 1_000,
            timezone="UTC",
            generation=1,
            sample_rate=48_000,
            pcm_s16le=b"\0\0" * 480,
        )


class AlsaCaptureProcessTests(unittest.TestCase):
    def test_request_stop_terminates_and_reaps_owned_ffmpeg_child(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-ffmpeg"
            pid_file = Path(directory) / "pid"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "os.write(1, b'\\0' * 960)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            device = AlsaCaptureDevice(
                interface_key="1-1/1-1:1.0/capture/0",
                usb_port_key="1-1",
                usb_interface_key="1-1:1.0",
                card_index=2,
                pcm_device=0,
                card_id="Camera",
            )
            source = AlsaCaptureSource(
                device.interface_key,
                device.usb_port_key,
                1,
                SimpleNamespace(resolve=lambda _key: device),
                FakeClock(),
                FakeTimezone(),
                ffmpeg_binary=str(script),
            )
            stop = threading.Event()
            blocks: list[AudioEnvelope] = []
            consumer = threading.Thread(
                target=lambda: blocks.extend(source.frames(stop))
            )
            consumer.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text(encoding="ascii"))
            self.assertEqual(source.active_process_pid, child_pid)

            stop.set()
            source.request_stop()
            consumer.join(3)

            self.assertFalse(consumer.is_alive())
            self.assertIsNone(source.active_process_pid)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()

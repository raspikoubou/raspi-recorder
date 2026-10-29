from __future__ import annotations

import errno
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.alsa_capture import AlsaAudioDiscovery
from raspi_recorder.adapters.camera_discovery import V4L2CameraDiscovery


class CameraDiscoveryTests(unittest.TestCase):
    def test_uses_capture_symlink_usb_parent_and_exact_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            links = root / "by-path"
            links.mkdir()
            sysfs = root / "sys"
            interface = sysfs / "devices" / "usb1" / "1-1.2" / "1-1.2:1.0"
            interface.mkdir(parents=True)
            (interface / "bInterfaceNumber").write_text("00\n", encoding="ascii")
            usb = interface.parent
            (usb / "idVendor").write_text("0411\n", encoding="ascii")
            (usb / "idProduct").write_text("02da\n", encoding="ascii")
            (usb / "product").write_text("Test camera\n", encoding="utf-8")
            audio_interface = usb / "1-1.2:1.2"
            audio_pcm = audio_interface / "sound" / "card7" / "pcmC7D0c"
            audio_pcm.mkdir(parents=True)
            (audio_interface / "bInterfaceNumber").write_text("02\n", encoding="ascii")
            sound_class = root / "sound"
            sound_class.mkdir()
            (sound_class / "pcmC7D0c").symlink_to(audio_pcm)
            (sound_class / "card7").mkdir()
            (sound_class / "card7" / "id").write_text("Camera\n", encoding="ascii")
            class_video = sysfs / "video2"
            class_video.mkdir()
            (class_video / "device").symlink_to(interface)
            fake_device = root / "dev" / "video2"
            fake_device.parent.mkdir()
            fake_device.touch()
            (links / "pci-usb-0:2:1.0-video-index0").symlink_to(fake_device)
            (links / "pci-usb-0:2:1.0-video-index1").symlink_to("/dev/video3")
            requests: list[int] = []

            def ioctl(_fd: int, request: int, payload: bytearray, mutate: bool) -> None:
                self.assertTrue(mutate)
                requests.append(request & 0xFF)
                index = struct.unpack_from("<I", payload, 0)[0]
                if request & 0xFF == 2:
                    if index > 2:
                        raise OSError(errno.EINVAL, "end of formats")
                    pixel_format = ("MJPG", "YUYV", "H264")[index]
                    struct.pack_into(
                        "<I",
                        payload,
                        44,
                        int.from_bytes(pixel_format.encode(), "little"),
                    )
                    return
                pixel_format = struct.unpack_from("<I", payload, 4)[0]
                if request & 0xFF == 74:
                    sizes = {
                        int.from_bytes(b"MJPG", "little"): [
                            (1, 1280, 720),
                            (2, 0, 0),  # Stepwise sizes remain unsupported.
                            (1, 1920, 1080),
                        ],
                        int.from_bytes(b"YUYV", "little"): [(1, 640, 480)],
                    }[pixel_format]
                    if index >= len(sizes):
                        raise OSError(errno.EINVAL, "end of sizes")
                    size_type, width, height = sizes[index]
                    struct.pack_into("<III", payload, 8, size_type, width, height)
                    return
                width, height = struct.unpack_from("<II", payload, 8)
                rates = {
                    (int.from_bytes(b"MJPG", "little"), 1280, 720): [
                        (1, 15),
                        (1001, 30000),
                    ],
                    (int.from_bytes(b"MJPG", "little"), 1920, 1080): [(1, 30)],
                    (int.from_bytes(b"YUYV", "little"), 640, 480): [(1, 15)],
                }[(pixel_format, width, height)]
                if index >= len(rates):
                    raise OSError(errno.EINVAL, "end of frame intervals")
                numerator, denominator = rates[index]
                struct.pack_into("<III", payload, 16, 1, numerator, denominator)

            with (
                patch(
                    "raspi_recorder.adapters.camera_discovery.os.open", return_value=41
                ) as open_device,
                patch(
                    "raspi_recorder.adapters.camera_discovery.os.close"
                ) as close_device,
                patch(
                    "raspi_recorder.adapters.camera_discovery.fcntl.ioctl",
                    side_effect=ioctl,
                ),
            ):
                ports = V4L2CameraDiscovery(
                    links,
                    sysfs,
                    audio_discovery=AlsaAudioDiscovery(sound_class),
                ).enumerate()

        open_device.assert_called_once()
        close_device.assert_called_once_with(41)
        self.assertEqual(set(requests), {2, 74, 75})

        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0].port_key, "1-1.2")
        self.assertEqual(ports[0].video_interface_key, "1-1.2:1.0")
        self.assertEqual(
            ports[0].device_path,
            str(links / "pci-usb-0:2:1.0-video-index0"),
        )
        self.assertEqual(ports[0].product, "Test camera")
        self.assertEqual(len(ports[0].audio_interfaces), 1)
        self.assertEqual(
            ports[0].audio_interfaces[0].interface_key,
            "1-1.2/1-1.2:1.2/capture/0",
        )
        self.assertEqual(ports[0].audio_interfaces[0].ffmpeg_device, "plughw:7,0")
        self.assertEqual(
            [
                (item.input_format, item.width, item.height, item.fps)
                for item in ports[0].profiles
            ],
            [
                ("yuyv422", 640, 480, (15,)),
                ("mjpeg", 1280, 720, (15, 30)),
                ("mjpeg", 1920, 1080, (30,)),
            ],
        )
        self.assertGreater(len(requests), 0)


if __name__ == "__main__":
    unittest.main()

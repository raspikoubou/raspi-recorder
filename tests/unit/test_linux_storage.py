import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raspi_recorder.adapters.linux_storage import (
    LinuxVolumeScanner,
    ManagedVolumeStore,
)
from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.ports.storage import MountedVolume


class LinuxMountInfoTests(unittest.TestCase):
    def test_mount_fields_decode_kernel_octal_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mountinfo"
            path.write_text(
                "42 1 8:1 / /media/test\\040drive rw,relatime - ext4 /dev/sda1 rw\n",
                encoding="utf-8",
            )
            scanner = LinuxVolumeScanner(mountinfo_path=path)

            entry = scanner._read_mountinfo()[0]

        self.assertEqual(entry.mountpoint, Path("/media/test drive"))
        self.assertEqual(entry.filesystem, "ext4")
        self.assertEqual(entry.source, "/dev/sda1")
        self.assertNotIn("ro", entry.mount_options)

    def test_rw_root_micro_sd_is_not_read_only_due_to_mountpoint_mode(self):
        root_stat = Path("/").stat()
        device_key = f"{os.major(root_stat.st_dev)}:{os.minor(root_stat.st_dev)}"
        for mount_options, expected_read_only in (
            ("rw,relatime", False),
            ("ro,relatime", True),
        ):
            with (
                self.subTest(mount_options=mount_options),
                tempfile.TemporaryDirectory() as directory,
            ):
                mountinfo = Path(directory) / "mountinfo"
                mountinfo.write_text(
                    f"36 25 {device_key} / / {mount_options} - ext4 /dev/root ext4\n",
                    encoding="utf-8",
                )
                scanner = LinuxVolumeScanner(mountinfo_path=mountinfo)
                with (
                    patch.object(
                        scanner, "_uuid_aliases", return_value={device_key: "root-uuid"}
                    ),
                    patch.object(scanner, "_kind_for", return_value=VolumeKind.MICROSD),
                ):
                    volumes = scanner.scan()

            self.assertEqual(len(volumes), 1)
            self.assertEqual(volumes[0].read_only, expected_read_only)


class ManagedVolumeStoreTests(unittest.TestCase):
    def test_micro_sd_recordings_live_under_writable_state_directory(self):
        volume = MountedVolume(
            persistent_id="root-uuid",
            device_path="/dev/mmcblk0p2",
            mountpoint=Path("/"),
            filesystem="ext4",
            kind=VolumeKind.MICROSD,
            device_number=0,
            filesystem_bytes=10_000_000_000,
            available_bytes=9_000_000_000,
            read_only=False,
        )
        store = ManagedVolumeStore()

        self.assertEqual(
            store.application_root(volume), Path("/var/lib/raspi-recorder/media")
        )
        self.assertEqual(
            store.recordings_root(volume),
            Path("/var/lib/raspi-recorder/media/recordings"),
        )

    def test_creates_owner_marker_and_recordings_root_then_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            volume = self.volume(mountpoint)
            store = ManagedVolumeStore()

            root = store.verify(volume)
            marker = mountpoint / "raspi-recorder" / ".owner.json"
            self.assertTrue(root.is_dir())
            self.assertTrue(marker.is_file())
            self.assertEqual(store.verify(volume), root)
            self.assertEqual(
                list(root.iterdir()),
                [],
                "検証用書込ファイルは終了時に削除される",
            )

    def test_refuses_existing_unowned_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            (mountpoint / "raspi-recorder").mkdir()
            store = ManagedVolumeStore()
            with self.assertRaisesRegex(ValueError, "marker"):
                store.verify(self.volume(mountpoint))

        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            target = mountpoint / "target"
            target.mkdir()
            (mountpoint / "raspi-recorder").symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                store.verify(self.volume(mountpoint))

    def test_resolve_rejects_parent_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            volume = self.volume(mountpoint)
            store = ManagedVolumeStore()
            root = store.verify(volume)
            (root / "nested").mkdir()
            normal = root / "nested" / "clip.mp4"
            normal.write_bytes(b"video")
            self.assertEqual(
                store.resolve_managed_file(volume, root, "nested/clip.mp4"), normal
            )
            with self.assertRaises(ValueError):
                store.resolve_managed_file(volume, root, "../outside")

            outside = mountpoint / "outside.mp4"
            outside.write_bytes(b"secret")
            (root / "linked.mp4").symlink_to(outside)
            with self.assertRaises(ValueError):
                store.resolve_managed_file(volume, root, "linked.mp4")

    def test_rejects_device_identity_change_before_using_root(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            volume = self.volume(mountpoint)
            store = ManagedVolumeStore()
            root = store.verify(volume)
            mismatched = MountedVolume(
                persistent_id=volume.persistent_id,
                device_path=volume.device_path,
                mountpoint=volume.mountpoint,
                filesystem=volume.filesystem,
                kind=volume.kind,
                device_number=volume.device_number + 1,
                filesystem_bytes=volume.filesystem_bytes,
                available_bytes=volume.available_bytes,
                read_only=False,
            )
            with self.assertRaisesRegex(RuntimeError, "identity"):
                store.assert_current(mismatched, root)

    def test_openat_reads_regular_file_and_unlinks_only_requested_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            volume = self.volume(mountpoint)
            store = ManagedVolumeStore()
            root = store.verify(volume)
            (root / "nested").mkdir()
            target = root / "nested" / "clip.mp4"
            target.write_bytes(b"video-data")

            with store.open_managed_file(volume, root, "nested/clip.mp4") as handle:
                self.assertEqual(handle.read(), b"video-data")
            store.unlink_managed_file(volume, root, "nested/clip.mp4")

            self.assertFalse(target.exists())
            self.assertTrue((root / "nested").is_dir())

    def test_ensure_managed_directory_rejects_symlink_components(self):
        with tempfile.TemporaryDirectory() as directory:
            mountpoint = Path(directory)
            volume = self.volume(mountpoint)
            store = ManagedVolumeStore()
            root = store.verify(volume)
            nested = store.ensure_managed_directory(volume, root, "a/b/c")
            self.assertTrue(nested.is_dir())

            outside = mountpoint / "outside"
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                store.ensure_managed_directory(volume, root, "linked/child")

    @staticmethod
    def volume(mountpoint: Path) -> MountedVolume:
        return MountedVolume(
            persistent_id="test-uuid",
            device_path="/dev/test",
            mountpoint=mountpoint,
            filesystem="ext4",
            kind=VolumeKind.USB,
            device_number=os.stat(mountpoint).st_dev,
            filesystem_bytes=10_000_000,
            available_bytes=9_000_000,
            read_only=False,
        )


if __name__ == "__main__":
    unittest.main()

"""Linux mountinfo・UUID・statvfsを使う安全な媒体adapter。"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.ports.storage import MountedVolume, VolumeScanner, VolumeStore

_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_OWNER_MARKER_VERSION = 1


@dataclass(frozen=True, slots=True)
class MountEntry:
    """/proc/self/mountinfoから読む1 mount。"""

    device_major: int
    device_minor: int
    mountpoint: Path
    filesystem: str
    source: str
    mount_options: frozenset[str]


class LinuxVolumeScanner(VolumeScanner):
    """UUIDが一意なmicroSD rootとUSB ext4/exFAT mountだけを列挙する。"""

    def __init__(
        self,
        mountinfo_path: str | Path = "/proc/self/mountinfo",
        uuid_directory: str | Path = "/dev/disk/by-uuid",
        sys_dev_block: str | Path = "/sys/dev/block",
    ) -> None:
        self._mountinfo_path = Path(mountinfo_path)
        self._uuid_directory = Path(uuid_directory)
        self._sys_dev_block = Path(sys_dev_block)

    def scan(self) -> tuple[MountedVolume, ...]:
        """現在のmount一覧を読み、UUID不明・重複・非対応を除外する。"""
        entries = self._read_mountinfo()
        aliases = self._uuid_aliases()
        candidates: list[MountedVolume] = []
        for entry in entries:
            if entry.filesystem not in {"ext4", "exfat"}:
                continue
            mountpoint = entry.mountpoint
            try:
                resolved = mountpoint.resolve(strict=True)
                mount_stat = resolved.stat()
                usage = os.statvfs(resolved)
            except OSError:
                continue
            actual_major = os.major(mount_stat.st_dev)
            actual_minor = os.minor(mount_stat.st_dev)
            if (actual_major, actual_minor) != (
                entry.device_major,
                entry.device_minor,
            ):
                continue
            device_key = f"{entry.device_major}:{entry.device_minor}"
            persistent_id = aliases.get(device_key)
            if persistent_id is None:
                continue
            kind = self._kind_for(entry)
            if kind is None:
                continue
            total_bytes = usage.f_blocks * usage.f_frsize
            available_bytes = usage.f_bavail * usage.f_frsize
            if total_bytes <= 0 or available_bytes < 0:
                continue
            candidates.append(
                MountedVolume(
                    persistent_id=persistent_id,
                    device_path=entry.source,
                    mountpoint=resolved,
                    filesystem=entry.filesystem,
                    kind=kind,
                    device_number=mount_stat.st_dev,
                    filesystem_bytes=total_bytes,
                    available_bytes=available_bytes,
                    read_only=(
                        "ro" in entry.mount_options
                        or (
                            mountpoint != Path("/")
                            and not os.access(resolved, os.W_OK | os.X_OK)
                        )
                    ),
                )
            )

        counts: dict[str, int] = {}
        for candidate in candidates:
            counts[candidate.persistent_id] = counts.get(candidate.persistent_id, 0) + 1
        unique = [
            candidate
            for candidate in candidates
            if counts[candidate.persistent_id] == 1
        ]
        return tuple(
            sorted(unique, key=lambda volume: (volume.kind.value, volume.persistent_id))
        )

    def _read_mountinfo(self) -> tuple[MountEntry, ...]:
        try:
            lines = self._mountinfo_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError("Linux mountinfoを読み取れません") from exc
        entries: list[MountEntry] = []
        for line in lines:
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            left = before.split()
            right = after.split()
            if len(left) < 6 or len(right) < 3:
                continue
            try:
                major_text, minor_text = left[2].split(":", maxsplit=1)
                entries.append(
                    MountEntry(
                        device_major=int(major_text),
                        device_minor=int(minor_text),
                        mountpoint=Path(self._unescape_mount_field(left[4])),
                        filesystem=right[0],
                        source=self._unescape_mount_field(right[1]),
                        mount_options=frozenset(left[5].split(",")),
                    )
                )
            except (ValueError, IndexError):
                continue
        return tuple(entries)

    def _uuid_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        try:
            paths = tuple(self._uuid_directory.iterdir())
        except OSError:
            return aliases
        for path in paths:
            try:
                device_stat = path.stat()
            except OSError:
                continue
            if not stat.S_ISBLK(device_stat.st_mode):
                continue
            device_key = (
                f"{os.major(device_stat.st_rdev)}:{os.minor(device_stat.st_rdev)}"
            )
            aliases[device_key] = path.name
        return aliases

    def _kind_for(self, entry: MountEntry) -> VolumeKind | None:
        if entry.mountpoint == Path("/"):
            path = self._sys_dev_block / f"{entry.device_major}:{entry.device_minor}"
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return None
            return VolumeKind.MICROSD if "mmc" in str(resolved).lower() else None
        if entry.filesystem not in {"ext4", "exfat"}:
            return None
        path = self._sys_dev_block / f"{entry.device_major}:{entry.device_minor}"
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        return VolumeKind.USB if "usb" in str(resolved).lower() else None

    @staticmethod
    def _unescape_mount_field(value: str) -> str:
        return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


class ManagedVolumeStore(VolumeStore):
    """マーカーの一致する媒体だけに専用recordings領域を作る。"""

    def __init__(self, application_directory: str = "raspi-recorder") -> None:
        if (
            not application_directory
            or "/" in application_directory
            or application_directory in {".", ".."}
        ):
            raise ValueError("application_directoryは単一directory名が必要です")
        self._application_directory = application_directory

    def verify(self, volume: MountedVolume) -> Path:
        """marker・領域・一時書込検査を行ってrecordings rootを返す。"""
        self._validate_mount(volume)
        volume_id = self._volume_id(volume.persistent_id)
        app_root = self.application_root(volume)
        self._assert_parent_directories(volume, app_root)
        if app_root.is_symlink():
            raise ValueError("アプリ専用directoryにsymlinkは使用できません")
        marker_path = app_root / ".owner.json"
        newly_created = not app_root.exists()
        if newly_created:
            app_root.mkdir(mode=0o750)
        elif not app_root.is_dir():
            raise ValueError("アプリ専用pathがdirectoryではありません")

        if marker_path.exists() or marker_path.is_symlink():
            marker = self._read_marker(marker_path)
            expected = {
                "schema_version": _OWNER_MARKER_VERSION,
                "volume_id": volume_id,
                "marker_id": volume_id,
                "persistent_id": volume.persistent_id,
                "filesystem": volume.filesystem,
            }
            if marker != expected:
                raise ValueError("媒体所有markerが選択媒体と一致しません")
        elif newly_created or self._is_adoptable_partial_root(app_root):
            # mkdir直後・marker書込前の電源断では、自前で作った空directory
            # (mode 0750)だけが残る。その形と完全に一致する場合だけ
            # 途中状態とみなしてmarkerを書いて引き取る。中身のある既存
            # directoryは他者のものとして従来どおり拒否する。
            marker = {
                "schema_version": _OWNER_MARKER_VERSION,
                "volume_id": volume_id,
                "marker_id": volume_id,
                "persistent_id": volume.persistent_id,
                "filesystem": volume.filesystem,
            }
            self._write_marker(marker_path, marker)
        else:
            raise ValueError("既存directoryに所有markerがなく、管理対象にできません")

        recordings_root = self.recordings_root(volume)
        if recordings_root.is_symlink():
            raise ValueError("recordings directoryにsymlinkは使用できません")
        if recordings_root.exists():
            if not recordings_root.is_dir():
                raise ValueError("recordings pathがdirectoryではありません")
        else:
            recordings_root.mkdir(mode=0o750)
        self.assert_owned(volume, recordings_root)
        self.assert_current(volume, recordings_root)
        self._test_write(recordings_root)
        return recordings_root

    @staticmethod
    def _is_adoptable_partial_root(app_root: Path) -> bool:
        """verify自身が作った直後の空directoryかどうかを判定する。"""
        try:
            info = app_root.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return False
        if stat.S_IMODE(info.st_mode) != 0o750:
            return False
        try:
            return not any(app_root.iterdir())
        except OSError:
            return False

    def assert_owned(self, volume: MountedVolume, recordings_root: Path) -> None:
        """既存marker・専用領域がこのUUIDの管理対象かread-onlyで確認する。"""
        self._validate_mount(volume)
        expected_root = self.recordings_root(volume)
        if recordings_root != expected_root:
            raise ValueError("recordings rootが既定の専用pathではありません")
        app_root = self.application_root(volume)
        self._assert_parent_directories(volume, app_root)
        marker_path = app_root / ".owner.json"
        expected = {
            "schema_version": _OWNER_MARKER_VERSION,
            "volume_id": self._volume_id(volume.persistent_id),
            "marker_id": self._volume_id(volume.persistent_id),
            "persistent_id": volume.persistent_id,
            "filesystem": volume.filesystem,
        }
        if (
            app_root.is_symlink()
            or marker_path.is_symlink()
            or recordings_root.is_symlink()
        ):
            raise ValueError("専用recordings pathにsymlinkは使用できません")
        if not app_root.is_dir() or not recordings_root.is_dir():
            raise ValueError("媒体専用recordings directoryがありません")
        if self._read_marker(marker_path) != expected:
            raise ValueError("媒体所有markerが選択媒体と一致しません")

    def application_root(self, volume: MountedVolume) -> Path:
        """microSDはsystemd StateDirectory内、USBは媒体直下を使う。"""
        if volume.mountpoint == Path("/"):
            return Path("/var/lib/raspi-recorder/media")
        return volume.mountpoint / self._application_directory

    def recordings_root(self, volume: MountedVolume) -> Path:
        """volume内のrecordings rootを返す。"""
        return self.application_root(volume) / "recordings"

    @staticmethod
    def _assert_parent_directories(volume: MountedVolume, app_root: Path) -> None:
        """媒体mountpointからapp rootの親までsymlinkと別deviceを拒否する。"""
        try:
            relative = app_root.parent.relative_to(volume.mountpoint)
        except ValueError as exc:
            raise ValueError("app rootが媒体mountpoint内にありません") from exc
        current = volume.mountpoint
        for component in relative.parts:
            current = current / component
            try:
                info = current.lstat()
            except OSError as exc:
                raise RuntimeError("app rootの親directoryがありません") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("app rootの親pathにsymlink/非directoryがあります")
            if info.st_dev != volume.device_number:
                raise ValueError("app rootの親directoryが媒体外にあります")

    def assert_current(self, volume: MountedVolume, recordings_root: Path) -> None:
        """mountpointと専用rootが同一device上にあることを検証する。"""
        try:
            mount_stat = volume.mountpoint.stat()
            root_stat = recordings_root.stat()
        except OSError as exc:
            raise RuntimeError("保存媒体が現在利用できません") from exc
        if mount_stat.st_dev != volume.device_number:
            raise RuntimeError("保存mediaのdevice identityが変わりました")
        if root_stat.st_dev != volume.device_number:
            raise RuntimeError("recordings rootが選択media上にありません")

    def resolve_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> Path:
        """親directoryをopenatで固定し、最終要素もfdで検証してPathを返す。

        以前のlstat→stat方式は最終要素のsymlink差し替えで検証対象と
        実際に開く対象がずれる余地があったため、他の管理file APIと
        同じfdベース検証にそろえる。
        """
        self.assert_current(volume, recordings_root)
        components = self._relative_components(relative_path)
        try:
            parent_fd, file_name = self._open_parent(
                volume, recordings_root, components
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("録画pathにsymlinkは使用できません") from exc
            raise
        try:
            try:
                file_fd = os.open(
                    file_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError("録画fileが存在しません") from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError("録画pathにsymlinkは使用できません") from exc
                raise
            try:
                info = os.fstat(file_fd)
            finally:
                os.close(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("録画pathはregular fileが必要です")
            if info.st_dev != volume.device_number:
                raise ValueError("録画fileが選択media外にあります")
        finally:
            os.close(parent_fd)
        return recordings_root.joinpath(*components)

    def ensure_managed_directory(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> Path:
        """componentごとにopenatしてsymlinkを拒否し、directoryを用意する。"""
        self.assert_owned(volume, recordings_root)
        self.assert_current(volume, recordings_root)
        components = self._relative_components(relative_path)
        descriptor = os.open(
            recordings_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if os.fstat(descriptor).st_dev != volume.device_number:
                raise ValueError("recordings rootが選択media外です")
            for component in components:
                try:
                    os.mkdir(component, mode=0o750, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                if os.fstat(descriptor).st_dev != volume.device_number:
                    raise ValueError("録画directoryが選択media外です")
            self.assert_current(volume, recordings_root)
        finally:
            os.close(descriptor)
        return recordings_root.joinpath(*components)

    @contextmanager
    def open_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> Iterator[BinaryIO]:
        """openat+O_NOFOLLOWでsymlink raceを避けて読み取りfdを開く。"""
        self.assert_owned(volume, recordings_root)
        self.assert_current(volume, recordings_root)
        components = self._relative_components(relative_path)
        parent_fd, file_name = self._open_parent(volume, recordings_root, components)
        file_fd: int | None = None
        try:
            file_fd = os.open(
                file_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != volume.device_number:
                raise ValueError("録画pathが選択media内のregular fileではありません")
            self.assert_current(volume, recordings_root)
            handle = os.fdopen(file_fd, "rb")
            file_fd = None
            with handle:
                yield handle
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent_fd)

    def unlink_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> None:
        """openatで親directoryを固定して、管理file 1つだけ削除する。"""
        self.assert_owned(volume, recordings_root)
        self.assert_current(volume, recordings_root)
        components = self._relative_components(relative_path)
        parent_fd, file_name = self._open_parent(volume, recordings_root, components)
        try:
            info = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("削除対象はsymlinkでないregular fileが必要です")
            if info.st_dev != volume.device_number:
                raise ValueError("削除対象が選択media外です")
            self.assert_current(volume, recordings_root)
            os.unlink(file_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            self.assert_current(volume, recordings_root)
        finally:
            os.close(parent_fd)

    def _validate_mount(self, volume: MountedVolume) -> None:
        if not isinstance(volume, MountedVolume):
            raise ValueError("volumeが不正です")
        if volume.filesystem not in {"ext4", "exfat"}:
            if not (volume.kind is VolumeKind.MICROSD and volume.filesystem == "ext4"):
                raise ValueError("未対応のfilesystemです")
        if volume.read_only:
            raise PermissionError("保存媒体がread-onlyです")
        try:
            actual = volume.mountpoint.stat().st_dev
        except OSError as exc:
            raise RuntimeError("保存mediaがmountされていません") from exc
        if actual != volume.device_number:
            raise RuntimeError("保存mediaのdevice identityが一致しません")

    @staticmethod
    def _relative_components(relative_path: str) -> tuple[str, ...]:
        pure_path = PurePosixPath(relative_path)
        if (
            pure_path.is_absolute()
            or not pure_path.parts
            or ".." in pure_path.parts
            or "." in pure_path.parts
            or "\\" in relative_path
        ):
            raise ValueError("録画相対pathが不正です")
        for component in pure_path.parts:
            if component in {"", ".", ".."}:
                raise ValueError("録画path componentが不正です")
        return pure_path.parts

    @staticmethod
    def _open_parent(
        volume: MountedVolume,
        recordings_root: Path,
        components: tuple[str, ...],
    ) -> tuple[int, str]:
        if not components:
            raise ValueError("録画相対pathが空です")
        descriptor = os.open(
            recordings_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if os.fstat(descriptor).st_dev != volume.device_number:
                raise ValueError("recordings rootが選択media外です")
            for component in components[:-1]:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                if os.fstat(descriptor).st_dev != volume.device_number:
                    raise ValueError("録画親directoryが選択media外です")
            return descriptor, components[-1]
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _volume_id(persistent_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"raspi-recorder:{persistent_id}"))

    @staticmethod
    def _read_marker(path: Path) -> dict[str, Any]:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("媒体所有markerがregular fileではありません")
            if path.stat().st_size > 4_096:
                raise ValueError("媒体所有markerが大きすぎます")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("媒体所有markerを読み込めません") from exc
        if not isinstance(value, dict):
            raise ValueError("媒体所有markerの形式が不正です")
        return value

    @staticmethod
    def _write_marker(path: Path, marker: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o640,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(marker, handle, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            ManagedVolumeStore._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _test_write(recordings_root: Path) -> None:
        path = recordings_root / f".write-check-{uuid.uuid4().hex}"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, b"raspi-recorder-write-check\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        path.unlink()
        ManagedVolumeStore._fsync_directory(recordings_root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

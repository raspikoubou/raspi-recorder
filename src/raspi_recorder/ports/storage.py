"""保存媒体を列挙し、識別し、利用する境界。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from raspi_recorder.domain.capacity import VolumeKind


@dataclass(frozen=True, slots=True)
class MountedVolume:
    """kernel mount情報から検証された媒体。"""

    persistent_id: str
    device_path: str
    mountpoint: Path
    filesystem: str
    kind: VolumeKind
    device_number: int
    filesystem_bytes: int
    available_bytes: int
    read_only: bool


class VolumeScanner(Protocol):
    """現在mount済みの対応媒体を検出する。"""

    def scan(self) -> tuple[MountedVolume, ...]:
        """曖昧なUUID複製は省いて利用可能媒体を返す。"""
        ...


class VolumeStore(Protocol):
    """媒体内のアプリ領域と安全なpathを提供する。"""

    def verify(self, volume: MountedVolume) -> Path:
        """所有markerとrecordings rootを検証し、書込可能なら返す。"""
        ...

    def assert_current(self, volume: MountedVolume, recordings_root: Path) -> None:
        """同じdeviceが同じmountpointへ現在も接続されることを確認する。"""
        ...

    def assert_owned(self, volume: MountedVolume, recordings_root: Path) -> None:
        """既存owner markerと専用directoryをread-onlyで再確認する。"""
        ...

    def ensure_managed_directory(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> Path:
        """openat/O_NOFOLLOWで管理root内のdirectoryを作成または検査する。"""
        ...

    def open_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> AbstractContextManager[BinaryIO]:
        """safe openat lease用file handleを開く。"""
        ...

    def unlink_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> None:
        """指定managed file 1つを安全にunlinkする。"""
        ...

    def resolve_managed_file(
        self, volume: MountedVolume, recordings_root: Path, relative_path: str
    ) -> Path:
        """symlink・path traversalを拒否し、管理fileを解決する。"""
        ...


class ManagedVolumeLocator(Protocol):
    """DB volume IDを現在接続中のmountとrecordings rootへ解決する。"""

    def resolve(self, volume_id: str) -> tuple[MountedVolume, Path]:
        """未接続・曖昧・marker不一致なら例外を返す。"""
        ...

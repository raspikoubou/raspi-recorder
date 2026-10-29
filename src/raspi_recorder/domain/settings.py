"""再起動後も保持する、型付きのアプリ設定。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from raspi_recorder.domain.camera import (
    CameraSettings,
    OverlayCorner,
    OverlaySize,
    Quality,
)
from raspi_recorder.domain.capacity import CapacityThresholds, VolumeKind


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """全体設定とカメラ別設定。

    カメラのprofile対応可否と媒体容量の妥当性は、機器能力を得た後に別途検証する。
    """

    revision: int = 0
    auto_delete_start_tenths_gb: int = 50
    auto_recover_tenths_gb: int = 70
    volume_kind: VolumeKind = VolumeKind.MICROSD
    volume_persistent_id: str | None = None
    power_saving_enabled: bool = False
    cameras: tuple[CameraSettings, CameraSettings] = (
        CameraSettings(),
        CameraSettings(),
    )

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revisionは0以上の整数で指定してください")
        if type(self.auto_delete_start_tenths_gb) is not int:
            raise ValueError("削除開始閾値は0.1GB単位の整数で指定してください")
        if type(self.auto_recover_tenths_gb) is not int:
            raise ValueError("回復目標は0.1GB単位の整数で指定してください")
        if not isinstance(self.volume_kind, VolumeKind):
            raise ValueError("volume_kindが不正です")
        if type(self.power_saving_enabled) is not bool:
            raise ValueError("power_saving_enabledはboolで指定してください")
        minimum_start = 21 if self.volume_kind is VolumeKind.MICROSD else 11
        if self.auto_delete_start_tenths_gb < minimum_start:
            raise ValueError("削除開始閾値は媒体の強制停止値より大きくしてください")
        if self.auto_recover_tenths_gb <= self.auto_delete_start_tenths_gb:
            raise ValueError("回復目標は削除開始閾値より大きくしてください")
        if self.volume_persistent_id is not None and (
            not isinstance(self.volume_persistent_id, str)
            or not self.volume_persistent_id
        ):
            raise ValueError(
                "volume_persistent_idは未設定または空でない値にしてください"
            )
        if (
            not isinstance(self.cameras, tuple)
            or len(self.cameras) != 2
            or not all(isinstance(camera, CameraSettings) for camera in self.cameras)
        ):
            raise ValueError("camerasにはカメラ1・2の設定を順に指定してください")
        port_keys = [
            camera.usb_port_key
            for camera in self.cameras
            if camera.usb_port_key is not None
        ]
        if len(port_keys) != len(set(port_keys)):
            raise ValueError("USBポートキーをカメラ間で重複できません")

    def validate_capacity(self, volume_bytes: int) -> CapacityThresholds:
        """実媒体容量と組み合わせた容量設定を検証して返す。"""
        return CapacityThresholds(
            volume_kind=self.volume_kind,
            volume_bytes=volume_bytes,
            start_tenths_gb=self.auto_delete_start_tenths_gb,
            recover_tenths_gb=self.auto_recover_tenths_gb,
        )

    def camera(self, camera_id: int) -> CameraSettings:
        """カメラ番号1または2の設定を返す。"""
        if type(camera_id) is not int or camera_id not in (1, 2):
            raise ValueError("camera_idは1または2で指定してください")
        return self.cameras[camera_id - 1]

    def to_payload(self) -> dict[str, object]:
        """秘密情報を含まないschema v2のJSON互換payloadを返す。"""
        return {
            "schema_version": 2,
            "revision": self.revision,
            "auto_delete_start_tenths_gb": self.auto_delete_start_tenths_gb,
            "auto_recover_tenths_gb": self.auto_recover_tenths_gb,
            "volume": {
                "kind": self.volume_kind.value,
                "persistent_id": self.volume_persistent_id,
            },
            "power_saving_enabled": self.power_saving_enabled,
            "cameras": [
                {
                    "camera_id": camera_id,
                    "width": camera.width,
                    "height": camera.height,
                    "fps": camera.fps,
                    "quality": camera.quality.value,
                    "audio_enabled": camera.audio_enabled,
                    "overlay_enabled": camera.overlay_enabled,
                    "overlay_corner": camera.overlay_corner.value,
                    "overlay_size": camera.overlay_size.value,
                    "usb_port_key": camera.usb_port_key,
                    "video_interface_key": camera.video_interface_key,
                    "audio_interface_key": camera.audio_interface_key,
                }
                for camera_id, camera in enumerate(self.cameras, start=1)
            ],
        }

    @classmethod
    def from_saved_payload(cls, payload: Mapping[str, object]) -> ApplicationSettings:
        """保存済みv1だけを旧入力項目なしのv2へ読み替える。DBは変更しない。"""
        raw = cast(dict[str, Any], dict(payload))
        # Read-only compatibility for saved v1 settings. Retired input fields
        # never enter the new API/domain, and other saved values are preserved.
        if raw.get("schema_version") == 1:
            for name in ("mode", "pre_record_seconds", "post_record_seconds"):
                raw.pop(name, None)
            raw["schema_version"] = 2
            raw["cameras"] = [
                {
                    key: value
                    for key, value in camera.items()
                    if key not in {"gpio_bcm", "trigger_edge", "gpio_filter_ms"}
                }
                if isinstance(camera, Mapping)
                else camera
                for camera in raw.get("cameras", [])
            ]
        return cls.from_payload(raw)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ApplicationSettings:
        """未知キーを拒否し、保存済み設定を型付きdomainへ復元する。"""
        expected = {
            "schema_version",
            "revision",
            "auto_delete_start_tenths_gb",
            "auto_recover_tenths_gb",
            "volume",
            "cameras",
        }
        raw = cast(dict[str, Any], dict(payload))
        if (
            set(raw) not in (expected, expected | {"power_saving_enabled"})
            or raw["schema_version"] != 2
        ):
            raise ValueError("settings payloadのキーまたはschema_versionが不正です")
        volume_raw = raw["volume"]
        camera_values = raw["cameras"]
        if not isinstance(volume_raw, Mapping) or set(volume_raw) != {
            "kind",
            "persistent_id",
        }:
            raise ValueError("volumeのキーが不正です")
        if not isinstance(camera_values, list) or len(camera_values) != 2:
            raise ValueError("camerasにはカメラ1・2の設定が必要です")

        camera_keys = {
            "camera_id",
            "width",
            "height",
            "fps",
            "quality",
            "audio_enabled",
            "overlay_enabled",
            "overlay_corner",
            "overlay_size",
            "usb_port_key",
            "video_interface_key",
            "audio_interface_key",
        }
        cameras_by_id: dict[int, CameraSettings] = {}
        for value in camera_values:
            if not isinstance(value, Mapping) or set(value) != camera_keys:
                raise ValueError("camera設定のキーがschema v2と一致しません")
            camera_raw = cast(dict[str, Any], dict(value))
            camera_id = camera_raw.pop("camera_id")
            if type(camera_id) is not int or camera_id not in (1, 2):
                raise ValueError("camera_idは1または2で指定してください")
            if camera_id in cameras_by_id:
                raise ValueError("camera_idが重複しています")
            try:
                camera_raw["quality"] = Quality(camera_raw["quality"])
                camera_raw["overlay_corner"] = OverlayCorner(
                    camera_raw["overlay_corner"]
                )
                camera_raw["overlay_size"] = OverlaySize(camera_raw["overlay_size"])
                cameras_by_id[camera_id] = CameraSettings(**camera_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"camera {camera_id}の設定が不正です") from exc
        if set(cameras_by_id) != {1, 2}:
            raise ValueError("camera_idは1・2を各1件指定してください")

        try:
            return cls(
                revision=raw["revision"],
                auto_delete_start_tenths_gb=raw["auto_delete_start_tenths_gb"],
                auto_recover_tenths_gb=raw["auto_recover_tenths_gb"],
                volume_kind=VolumeKind(volume_raw["kind"]),
                volume_persistent_id=volume_raw["persistent_id"],
                power_saving_enabled=raw.get("power_saving_enabled", False),
                cameras=(cameras_by_id[1], cameras_by_id[2]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("settings payloadの値が不正です") from exc

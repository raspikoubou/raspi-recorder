"""Run the virtual two-camera engine acceptance flow on a mounted USB volume."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from raspi_recorder.adapters.linux_storage import LinuxVolumeScanner, ManagedVolumeStore
from raspi_recorder.domain.camera import RecordingMode
from raspi_recorder.domain.capacity import RecordingFileState, VolumeKind


def wait_for(client, predicate, *, description: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get("/api/v1/status", headers={"Host": "localhost"})
        assert response.status_code == 200, response.get_data(as_text=True)
        last = response.get_json()
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}; last status={last}")


def camera(status, camera_id: int):
    return next(item for item in status["cameras"] if item["camera_id"] == camera_id)


def post(client, path: str, body: dict, csrf: str):
    response = client.post(
        path,
        headers={"Host": "localhost", "X-CSRF-Token": csrf},
        json=body,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def wait_operation(client, operation_id: str, *, timeout: float = 120.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/operations/{operation_id}", headers={"Host": "localhost"}
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        last = response.get_json()
        if last["state"] in {"succeeded", "failed"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"operation timed out: {last}")


def switch_volume(client, csrf: str, *, kind: str, persistent_id: str | None):
    current = client.get("/api/v1/settings", headers={"Host": "localhost"})
    assert current.status_code == 200
    settings = current.get_json()["settings"]
    expected_revision = settings["revision"]
    settings["volume"] = {"kind": kind, "persistent_id": persistent_id}
    if kind == VolumeKind.MICROSD.value:
        settings["auto_delete_start_tenths_gb"] = 21
        settings["auto_recover_tenths_gb"] = 22
    else:
        settings["auto_delete_start_tenths_gb"] = 11
        settings["auto_recover_tenths_gb"] = 12
    response = client.patch(
        "/api/v1/settings",
        headers={"Host": "localhost", "X-CSRF-Token": csrf},
        json={
            "expected_revision": expected_revision,
            "request_id": f"usb-acceptance-switch-{kind}-{time.time_ns()}",
            "settings": settings,
        },
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    operation = wait_operation(client, response.get_json()["operation_id"])
    assert operation["state"] == "succeeded", operation
    return operation


def test_volume_store(microsd_test_root: Path):
    class IsolatedVolumeStore(ManagedVolumeStore):
        def __init__(self):
            super().__init__(application_directory="raspi-recorder-pi-usb-acceptance")

        def application_root(self, volume):
            if volume.mountpoint == Path("/"):
                return microsd_test_root
            return super().application_root(volume)

    return IsolatedVolumeStore()


def verify_and_remove_owned_test_root(volume_store, volume, root: Path) -> bool:
    if not root.exists():
        return False
    recordings_root = volume_store.recordings_root(volume)
    volume_store.assert_owned(volume, recordings_root)
    marker = json.loads((root / ".owner.json").read_text(encoding="utf-8"))
    assert marker["persistent_id"] == volume.persistent_id, marker
    assert marker["filesystem"] == volume.filesystem, marker
    shutil.rmtree(root)
    return True


def create_capacity_blocker(path: Path, fill_bytes: int) -> Path:
    """Only remove a filler file after this process created it exclusively."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.posix_fallocate(descriptor, 0, fill_bytes)
        os.fsync(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return path


def main() -> None:
    from virtual_two_camera import create_acceptance_environment

    if len(sys.argv) != 2:
        raise SystemExit("usage: run_pi_usb_two_camera.py <USB-filesystem-UUID>")
    wanted_id = sys.argv[1]
    volumes = [
        volume
        for volume in LinuxVolumeScanner().scan()
        if volume.persistent_id == wanted_id and volume.kind.value == "USB"
    ]
    assert len(volumes) == 1, f"USB UUID is missing or ambiguous: {wanted_id}"
    volume = volumes[0]
    assert volume.filesystem in {"ext4", "exfat"} and not volume.read_only
    assert volume.mountpoint != Path("/")

    usb_test_root = volume.mountpoint / "raspi-recorder-pi-usb-acceptance"
    microsd_test_root = Path("/var/tmp/raspi-recorder-pi-usb-acceptance-microsd")
    if usb_test_root.exists() or microsd_test_root.exists():
        raise SystemExit("refusing to reuse an existing acceptance test storage root")
    volume_store = test_volume_store(microsd_test_root)

    temporary = tempfile.TemporaryDirectory(prefix="raspi-recorder-pi-usb-acceptance-")
    environment = create_acceptance_environment(
        Path(temporary.name),
        real_camera_1=True,
        mounted_volume=volume,
        volume_store=volume_store,
    )
    client = environment.app.test_client()
    microsd_volume = None
    capacity_blocker = None
    try:
        csrf = client.get(
            "/api/v1/auth/status", headers={"Host": "localhost"}
        ).get_json()["csrf_token"]
        initial = wait_for(
            client,
            lambda status: all(
                camera(status, camera_id)["frames"] >= 5
                and camera(status, camera_id)["recording_active"]
                for camera_id in (1, 2)
            ),
            description="real USB camera and virtual camera recording",
        )
        frames_before = {
            camera_id: camera(initial, camera_id)["frames"] for camera_id in (1, 2)
        }

        post(client, "/api/v1/cameras/1/preview/demand", {"active": True}, csrf)
        post(client, "/api/v1/cameras/2/preview/demand", {"active": True}, csrf)
        wait_for(
            client,
            lambda status: all(
                camera(status, camera_id)["preview"].get("fresh")
                for camera_id in (1, 2)
            ),
            description="two independent camera previews",
        )

        post(client, "/api/v1/live/1/demand", {"active": True}, csrf)
        live = wait_for(
            client,
            lambda status: camera(status, 1)["live"].get("available"),
            description="real camera HLS live segment",
        )
        generation = camera(live, 1)["live"]["generation"]
        playlist = client.get(
            f"/api/v1/live/1/{generation}/index.m3u8",
            headers={"Host": "localhost"},
        )
        assert playlist.status_code == 200 and b"#EXTM3U" in playlist.data

        environment.controls[2].paused.set()
        time.sleep(0.3)
        paused = client.get("/api/v1/status", headers={"Host": "localhost"}).get_json()
        assert camera(paused, 1)["frames"] > frames_before[1]
        camera_2_paused = camera(paused, 2)["frames"]
        time.sleep(0.7)
        paused_again = client.get(
            "/api/v1/status", headers={"Host": "localhost"}
        ).get_json()
        assert camera(paused_again, 2)["frames"] == camera_2_paused
        assert camera(paused_again, 1)["frames"] > camera(paused, 1)["frames"]
        environment.controls[2].paused.clear()
        wait_for(
            client,
            lambda status: camera(status, 2)["frames"] > camera_2_paused,
            description="virtual camera 2 recovery",
        )

        post(client, "/api/v1/live/1/demand", {"active": False}, csrf)
        post(client, "/api/v1/cameras/1/preview/demand", {"active": False}, csrf)
        post(client, "/api/v1/cameras/2/preview/demand", {"active": False}, csrf)
        post(client, "/api/v1/cameras/1/stop", {}, csrf)
        post(client, "/api/v1/cameras/2/stop", {}, csrf)
        wait_for(
            client,
            lambda status: all(
                camera(status, camera_id)["state"] == "stopped" for camera_id in (1, 2)
            ),
            description="both USB recordings finalized",
        )

        recordings = {}
        media = {}
        for camera_id in (1, 2):
            response = client.get(
                f"/api/v1/recordings?camera_id={camera_id}",
                headers={"Host": "localhost"},
            )
            assert response.status_code == 200
            items = response.get_json()["items"]
            assert items and items[-1]["state"] == "finalized"
            recordings[camera_id] = items[-1]
            path = environment.volume_root / items[-1]["relative_path"]
            assert path.is_file() and path.stat().st_dev == volume.device_number
            import av

            with av.open(str(path)) as container:
                stream = container.streams.video[0]
                assert stream.codec_context.name == "h264"
                assert any(container.decode(video=0))
                media[camera_id] = {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "codec": stream.codec_context.name,
                    "width": stream.width,
                    "height": stream.height,
                    "duration_seconds": round(
                        float(stream.duration * stream.time_base), 3
                    )
                    if stream.duration is not None
                    else None,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            recording_id = recordings[camera_id]["recording_id"]
            content = client.get(
                f"/api/v1/recordings/{recording_id}/content",
                headers={"Host": "localhost"},
            )
            assert content.status_code == 200 and content.data == path.read_bytes()
            seek_start = len(content.data) // 2
            seek_end = min(len(content.data) - 1, seek_start + 1_023)
            seek = client.get(
                f"/api/v1/recordings/{recording_id}/content",
                headers={
                    "Host": "localhost",
                    "Range": f"bytes={seek_start}-{seek_end}",
                },
            )
            assert seek.status_code == 206
            assert seek.headers["Content-Range"] == (
                f"bytes {seek_start}-{seek_end}/{len(content.data)}"
            )
            assert seek.data == content.data[seek_start : seek_end + 1]

            download = client.get(
                f"/api/v1/recordings/{recording_id}/download",
                headers={"Host": "localhost"},
            )
            assert download.status_code == 200
            assert download.headers["Content-Disposition"].startswith("attachment;")
            assert download.data == path.read_bytes()

        abnormal_id = "pi-usb-acceptance-abnormal"
        abnormal_relative_path = "acceptance-abnormal/cam1/interrupted.mp4"
        abnormal_directory = environment.volume_root / "acceptance-abnormal" / "cam1"
        abnormal_directory.mkdir(parents=True, exist_ok=True)
        abnormal_bytes = b"acceptance-only partial MP4; not finalized\n"
        abnormal_partial = abnormal_directory / "interrupted.mp4.partial"
        abnormal_partial.write_bytes(abnormal_bytes)
        environment.runtime.catalog.create_recording(
            recording_id=abnormal_id,
            volume_id=recordings[1]["volume_id"],
            relative_path=abnormal_relative_path,
            mode=RecordingMode.CONTINUOUS,
            camera_id=1,
            logical_event_id=None,
            part_index=1,
            boot_id="pi-usb-acceptance",
            writer_epoch="pi-usb-acceptance-abnormal",
            created_utc_ns=time.time_ns(),
            settings_snapshot={"acceptance_fixture": True},
        )
        environment.runtime.catalog.mark_recording_abnormal(
            abnormal_id, "acceptance_fixture_partial", size_bytes=len(abnormal_bytes)
        )
        abnormal_download = client.get(
            f"/api/v1/recordings/{abnormal_id}/download",
            headers={"Host": "localhost"},
        )
        assert abnormal_download.status_code == 200
        assert abnormal_download.data == abnormal_bytes
        abnormal_playback = client.get(
            f"/api/v1/recordings/{abnormal_id}/content",
            headers={"Host": "localhost"},
        )
        assert abnormal_playback.status_code == 404
        assert abnormal_playback.get_json()["error"]["code"] == "recording_not_readable"

        mixed_export = client.post(
            "/api/v1/exports",
            headers={"Host": "localhost", "X-CSRF-Token": csrf},
            json={
                "recording_ids": [
                    recordings[1]["recording_id"],
                    abnormal_id,
                ]
            },
        )
        assert mixed_export.status_code == 409
        assert mixed_export.get_json()["error"]["code"] == "recording_not_readable"

        export = client.post(
            "/api/v1/exports",
            headers={"Host": "localhost", "X-CSRF-Token": csrf},
            json={
                "recording_ids": [
                    recordings[1]["recording_id"],
                    recordings[2]["recording_id"],
                ]
            },
        )
        assert export.status_code == 202, export.get_data(as_text=True)
        export_id = export.get_json()["export_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.get(
                f"/api/v1/exports/{export_id}", headers={"Host": "localhost"}
            ).get_json()
            if status["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        assert status["state"] == "succeeded", status
        archive = client.get(
            f"/api/v1/exports/{export_id}/download",
            headers={"Host": "localhost"},
        )
        assert archive.status_code == 200
        with zipfile.ZipFile(BytesIO(archive.data)) as bundle:
            entries = bundle.namelist()
            assert len(entries) == 2 and all(name.endswith(".mp4") for name in entries)
            archived_hashes = {
                hashlib.sha256(bundle.read(name)).hexdigest() for name in entries
            }
            assert archived_hashes == {item["sha256"] for item in media.values()}

        post(client, "/api/v1/cameras/1/start", {}, csrf)
        post(client, "/api/v1/cameras/2/start", {}, csrf)
        wait_for(
            client,
            lambda status: all(
                camera(status, camera_id)["recording_active"] for camera_id in (1, 2)
            ),
            description="camera restart before storage switch",
        )
        switch_volume(client, csrf, kind=VolumeKind.MICROSD.value, persistent_id=None)
        microsd_status = wait_for(
            client,
            lambda status: (
                status.get("volume") is not None
                and status["volume"]["kind"] == VolumeKind.MICROSD.value
                and all(
                    camera(status, camera_id)["recording_active"]
                    for camera_id in (1, 2)
                )
            ),
            description="isolated microSD destination selected and recording resumed",
        )
        microsd_volumes = [
            item
            for item in environment.runtime.catalog.list_volumes()
            if item.kind is VolumeKind.MICROSD and item.selected
        ]
        assert len(microsd_volumes) == 1
        microsd_volume = next(
            item
            for item in LinuxVolumeScanner().scan()
            if item.kind is VolumeKind.MICROSD
        )
        assert microsd_volume.persistent_id == microsd_status["volume"]["persistent_id"]
        assert volume_store.application_root(microsd_volume) == microsd_test_root
        assert microsd_test_root.stat().st_dev == os.stat("/").st_dev
        microsd_volume_id = microsd_volumes[0].volume_id

        switch_volume(
            client,
            csrf,
            kind=VolumeKind.USB.value,
            persistent_id=volume.persistent_id,
        )
        microsd_recordings = {}
        microsd_recordings_root = volume_store.recordings_root(microsd_volume)
        for camera_id in (1, 2):
            finalized = [
                item
                for item in environment.runtime.catalog.list_recordings(
                    camera_id=camera_id, limit=100
                )
                if item.volume_id == microsd_volume_id
                and item.state is RecordingFileState.FINALIZED
            ]
            assert finalized, f"camera {camera_id} did not finalize on microSD"
            recording = finalized[0]
            path = microsd_recordings_root / recording.relative_path
            assert path.is_file() and path.stat().st_size > 0
            assert path.stat().st_dev == microsd_volume.device_number
            microsd_recordings[camera_id] = recording.recording_id
        usb_return = wait_for(
            client,
            lambda status: (
                status.get("volume") is not None
                and status["volume"]["kind"] == VolumeKind.USB.value
                and status["volume"]["persistent_id"] == volume.persistent_id
                and all(
                    camera(status, camera_id)["recording_active"]
                    for camera_id in (1, 2)
                )
            ),
            description="USB destination restored and recording resumed",
        )

        available_before = (
            os.statvfs(volume.mountpoint).f_bavail
            * os.statvfs(volume.mountpoint).f_frsize
        )
        target_free_bytes = 850_000_000
        fill_bytes = available_before - target_free_bytes
        assert fill_bytes > 0, (
            f"unexpected USB capacity before pressure test: {available_before}"
        )
        capacity_blocker = create_capacity_blocker(
            volume.mountpoint / ".raspi-recorder-pi-usb-capacity-blocker", fill_bytes
        )
        available_after = (
            os.statvfs(volume.mountpoint).f_bavail
            * os.statvfs(volume.mountpoint).f_frsize
        )
        assert available_after <= 1_000_000_000, available_after
        low_capacity = wait_for(
            client,
            lambda status: (
                status.get("volume") is not None
                and status["volume"]["available_bytes"] <= 1_000_000_000
                and not any(
                    camera(status, camera_id)["recording_active"]
                    for camera_id in (1, 2)
                )
            ),
            description="USB capacity stop threshold stops both recordings",
        )
        assert low_capacity.get("capacity_error") is None, low_capacity
        assert low_capacity.get("capacity_warning") in {
            "low_space_stop_threshold",
            "deletion_candidates_exhausted",
        }, low_capacity
        capacity_blocker.unlink()
        capacity_blocker = None
        recovered = wait_for(
            client,
            lambda status: (
                status.get("volume") is not None
                and status["volume"]["available_bytes"] > 1_100_000_000
                and status.get("capacity_error") is None
                and all(
                    camera(status, camera_id)["recording_active"]
                    for camera_id in (1, 2)
                )
            ),
            description="recording recovers after test filler removal",
        )
        post(client, "/api/v1/cameras/1/stop", {}, csrf)
        post(client, "/api/v1/cameras/2/stop", {}, csrf)
        wait_for(
            client,
            lambda status: all(
                camera(status, camera_id)["state"] == "stopped" for camera_id in (1, 2)
            ),
            description="final recordings stopped after capacity recovery",
        )

        print(
            json.dumps(
                {
                    "host": socket.gethostname(),
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "python": sys.version.split()[0],
                    "volume_uuid": volume.persistent_id,
                    "volume_mount": str(volume.mountpoint),
                    "filesystem": volume.filesystem,
                    "device_path": volume.device_path,
                    "real_camera_port_key": environment.camera_1_port_key,
                    "logical_camera_2": "virtual synthetic frame source",
                    "initial_frames": frames_before,
                    "media": media,
                    "zip_entries": entries,
                    "zip_bytes": len(archive.data),
                    "zip_source_hashes_match": True,
                    "playback_full_and_range_seek": True,
                    "individual_download_sha_matches": True,
                    "abnormal_partial_individual_download_only": True,
                    "mixed_finalized_abnormal_zip_rejected": True,
                    "volume_switch": {
                        "from": "USB",
                        "to": (
                            "real microSD device with test-only /var/tmp storage root"
                        ),
                        "back_to": "USB",
                        "microSD_persistent_id": microsd_volume.persistent_id,
                        "microSD_recordings_written_and_finalized": len(
                            microsd_recordings
                        ),
                        "USB_selected_after_return": (
                            usb_return["volume"]["persistent_id"]
                            == volume.persistent_id
                        ),
                    },
                    "capacity_shortage": {
                        "free_bytes_before_pressure": available_before,
                        "free_bytes_after_filler": available_after,
                        "warning": low_capacity.get("capacity_warning"),
                        "recordings_stopped": True,
                        "free_bytes_after_recovery": recovered["volume"][
                            "available_bytes"
                        ],
                        "recording_resumed": True,
                    },
                },
                indent=2,
            )
        )
    finally:
        if capacity_blocker is not None:
            capacity_blocker.unlink(missing_ok=True)
        environment.runtime.close()
        current_volumes = LinuxVolumeScanner().scan()
        usb_current = [
            item
            for item in current_volumes
            if item.persistent_id == volume.persistent_id
            and item.kind is VolumeKind.USB
        ]
        assert len(usb_current) == 1
        verify_and_remove_owned_test_root(volume_store, usb_current[0], usb_test_root)
        microsd_current = [
            item for item in current_volumes if item.kind is VolumeKind.MICROSD
        ]
        if microsd_test_root.exists():
            assert len(microsd_current) == 1
            verify_and_remove_owned_test_root(
                volume_store, microsd_current[0], microsd_test_root
            )
        temporary.cleanup()


if __name__ == "__main__":
    main()

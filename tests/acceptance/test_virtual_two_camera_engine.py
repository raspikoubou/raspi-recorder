"""Run the recorder, SQLite index, Flask API, media writer, live and ZIP paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from raspi_recorder.adapters.camera_discovery import V4L2CameraDiscovery
from raspi_recorder.adapters.system_clock import SystemClock, SystemTimezone
from raspi_recorder.engine.runtime import RecorderRuntime


def _wait_for(client, predicate, *, timeout: float = 20.0, description: str):
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


def _camera(status, camera_id: int):
    return next(item for item in status["cameras"] if item["camera_id"] == camera_id)


def _post(client, path: str, body: dict, csrf_token: str):
    response = client.post(
        path,
        headers={"Host": "localhost", "X-CSRF-Token": csrf_token},
        json=body,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_engine_records_independent_cameras_and_serves_web_live_mp4_and_zip():
    av = pytest.importorskip("av")
    Image = pytest.importorskip("PIL.Image")
    from tests.acceptance.virtual_two_camera import (
        AcceptancePreconditionError,
        create_acceptance_environment,
    )

    temporary_root = tempfile.TemporaryDirectory(prefix="raspi-recorder-acceptance-")
    try:
        environment = create_acceptance_environment(
            Path(temporary_root.name),
            real_camera_1=os.environ.get("RASPI_RECORDER_ACCEPTANCE_REAL_CAMERA")
            == "1",
        )
    except BaseException as exc:
        temporary_root.cleanup()
        if isinstance(exc, AcceptancePreconditionError):
            pytest.skip(str(exc))
        raise
    client = environment.app.test_client()
    try:
        auth_status = client.get(
            "/api/v1/auth/status", headers={"Host": "localhost"}
        ).get_json()
        csrf = auth_status["csrf_token"]

        initial = _wait_for(
            client,
            lambda status: all(
                _camera(status, camera_id)["frames"] >= 5
                and _camera(status, camera_id)["recording_active"]
                for camera_id in (1, 2)
            ),
            description="two camera workers and writers",
        )
        assert _camera(initial, 1)["generation"] > 0
        assert _camera(initial, 2)["generation"] > 0

        for camera_id in (1, 2):
            _post(
                client,
                f"/api/v1/cameras/{camera_id}/preview/demand",
                {"active": True},
                csrf,
            )
        _wait_for(
            client,
            lambda status: all(
                _camera(status, camera_id)["preview"].get("fresh")
                for camera_id in (1, 2)
            ),
            description="camera previews",
        )
        colors = []
        for camera_id in (1, 2):
            response = client.get(
                f"/api/v1/cameras/{camera_id}/preview.jpg",
                headers={"Host": "localhost"},
            )
            assert response.status_code == 200
            image = Image.open(BytesIO(response.data)).convert("RGB")
            colors.append(image.getpixel((image.width // 2, image.height // 2)))
        assert colors[0] != colors[1]

        _post(client, "/api/v1/live/1/demand", {"active": True}, csrf)
        live_status = _wait_for(
            client,
            lambda status: _camera(status, 1)["live"].get("available"),
            timeout=30,
            description="camera 1 HLS playlist",
        )
        generation = _camera(live_status, 1)["live"]["generation"]
        playlist = client.get(
            f"/api/v1/live/1/{generation}/index.m3u8",
            headers={"Host": "localhost"},
        )
        assert playlist.status_code == 200
        assert b"#EXTM3U" in playlist.data

        control = environment.controls[2]
        control.paused.set()
        time.sleep(0.3)
        paused_status = client.get(
            "/api/v1/status", headers={"Host": "localhost"}
        ).get_json()
        paused_frames = _camera(paused_status, 2)["frames"]
        camera_1_frames = _camera(paused_status, 1)["frames"]
        time.sleep(0.7)
        paused_again = client.get(
            "/api/v1/status", headers={"Host": "localhost"}
        ).get_json()
        assert _camera(paused_again, 2)["frames"] == paused_frames
        assert _camera(paused_again, 1)["frames"] > camera_1_frames
        control.paused.clear()
        resumed = _wait_for(
            client,
            lambda status: _camera(status, 2)["frames"] > paused_frames,
            description="virtual camera 2 recovery",
        )

        _post(client, "/api/v1/cameras/1/stop", {}, csrf)
        after_camera_1_stop = _wait_for(
            client,
            lambda status: (
                _camera(status, 1)["state"] == "stopped"
                and _camera(status, 2)["frames"] > _camera(resumed, 2)["frames"]
            ),
            description="camera 2 continues after camera 1 stops",
        )
        _post(client, "/api/v1/cameras/1/start", {}, csrf)
        _wait_for(
            client,
            lambda status: (
                _camera(status, 1)["recording_active"]
                and _camera(status, 2)["frames"]
                > _camera(after_camera_1_stop, 2)["frames"]
            ),
            description="camera 1 restarts independently",
        )
        _post(client, "/api/v1/live/1/demand", {"active": False}, csrf)
        _post(client, "/api/v1/cameras/1/preview/demand", {"active": False}, csrf)
        _post(client, "/api/v1/cameras/2/preview/demand", {"active": False}, csrf)

        _post(client, "/api/v1/cameras/1/stop", {}, csrf)
        _post(client, "/api/v1/cameras/2/stop", {}, csrf)
        _wait_for(
            client,
            lambda status: all(
                _camera(status, camera_id)["state"] == "stopped" for camera_id in (1, 2)
            ),
            description="both MP4 files finalize",
        )

        records = {}
        for camera_id in (1, 2):
            response = client.get(
                f"/api/v1/recordings?camera_id={camera_id}",
                headers={"Host": "localhost"},
            )
            assert response.status_code == 200
            camera_records = response.get_json()["items"]
            assert camera_records
            latest = camera_records[-1]
            assert latest["state"] == "finalized"
            assert latest["camera_id"] == camera_id
            records[camera_id] = latest
            video_path = environment.volume_root / latest["relative_path"]
            assert video_path.is_file()
            with av.open(str(video_path)) as container:
                stream = container.streams.video[0]
                assert stream.codec_context.name == "h264"
                assert any(container.decode(video=0))

        export = client.post(
            "/api/v1/exports",
            headers={"Host": "localhost", "X-CSRF-Token": csrf},
            json={
                "recording_ids": [
                    records[1]["recording_id"],
                    records[2]["recording_id"],
                ]
            },
        )
        assert export.status_code == 202, export.get_data(as_text=True)
        export_id = export.get_json()["export_id"]
        completed = _wait_for_export(client, export_id)
        assert completed["state"] == "succeeded"
        archive = client.get(
            f"/api/v1/exports/{export_id}/download",
            headers={"Host": "localhost"},
        )
        assert archive.status_code == 200
        with zipfile.ZipFile(BytesIO(archive.data)) as bundle:
            assert len(bundle.namelist()) == 2
            assert all(name.endswith(".mp4") for name in bundle.namelist())
            assert all(bundle.getinfo(name).file_size > 0 for name in bundle.namelist())
    finally:
        environment.runtime.close()
        temporary_root.cleanup()


def _wait_for_export(client, export_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/exports/{export_id}", headers={"Host": "localhost"}
        )
        assert response.status_code == 200
        last = response.get_json()
        if last["state"] in {"succeeded", "failed"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"export timed out: {last}")


def _linux_memory_bytes(key: str) -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, value, *_unit = line.split()
            if name == f"{key}:":
                return int(value) * 1024
    except (OSError, ValueError):
        return None
    return None


def _process_rss_bytes() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            name, value, *_unit = line.split()
            if name == "VmRSS:":
                return int(value) * 1024
    except (OSError, ValueError):
        return None
    return None


def _device_write_bytes(path: Path) -> int | None:
    try:
        device = path.stat().st_dev
        stat_path = (
            Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}" / "stat"
        )
        fields = stat_path.read_text(encoding="ascii").split()
        return int(fields[6]) * 512
    except (OSError, IndexError, ValueError):
        return None


def _vcgencmd(command: str) -> str | None:
    try:
        result = subprocess.run(
            ["vcgencmd", command],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def _temperature_celsius() -> float | None:
    reading = _vcgencmd("measure_temp")
    if reading is None or not reading.startswith("temp="):
        return None
    try:
        return float(reading.removeprefix("temp=").removesuffix("'C"))
    except ValueError:
        return None


def _media_span_metrics(recordings: list[dict]) -> dict[str, float | int]:
    spans = [span for item in recordings for span in item["spans"]]
    spans.sort(key=lambda span: span["start_utc_ns"])
    time_gaps = [
        max(0, current["start_utc_ns"] - previous["end_utc_ns"])
        for previous, current in zip(spans, spans[1:], strict=False)
    ]
    sequence_gaps = sum(
        max(0, current["first_sequence"] - previous["last_sequence"] - 1)
        for previous, current in zip(spans, spans[1:], strict=False)
    )
    return {
        "span_count": len(spans),
        "sequence_gaps": sequence_gaps,
        "max_time_gap_seconds": max(time_gaps, default=0) / 1_000_000_000,
        "recorded_duration_seconds": sum(
            item["duration_ns"] or 0 for item in recordings
        )
        / 1_000_000_000,
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_real_and_virtual_camera_one_hour_continuous_recording_metrics():
    av = pytest.importorskip("av")
    pytest.importorskip("PIL.Image")
    duration_seconds = int(
        os.environ.get("RASPI_RECORDER_ACCEPTANCE_LONG_RUN_SECONDS", "0")
    )
    if duration_seconds < 3_600:
        pytest.skip("set RASPI_RECORDER_ACCEPTANCE_LONG_RUN_SECONDS=3600 for CAM-16")
    if os.environ.get("RASPI_RECORDER_ACCEPTANCE_REAL_CAMERA") != "1":
        pytest.skip("the CAM-16 run requires the connected physical camera")

    from tests.acceptance.virtual_two_camera import (
        AcceptancePreconditionError,
        create_acceptance_environment,
    )

    temporary_root = tempfile.TemporaryDirectory(
        prefix="raspi-recorder-longrun-acceptance-"
    )
    root = Path(temporary_root.name)
    try:
        environment = create_acceptance_environment(root, real_camera_1=True)
    except BaseException as exc:
        temporary_root.cleanup()
        if isinstance(exc, AcceptancePreconditionError):
            pytest.skip(str(exc))
        raise

    client = environment.app.test_client()
    try:
        initial = _wait_for(
            client,
            lambda status: all(
                _camera(status, camera_id)["state"] == "continuous"
                and _camera(status, camera_id)["recording_active"]
                for camera_id in (1, 2)
            ),
            timeout=45,
            description="both cameras continuously recording",
        )
        initial_frames = {
            camera_id: _camera(initial, camera_id)["frames"] for camera_id in (1, 2)
        }
        disk_start = _device_write_bytes(root)
        start_mono = time.monotonic()
        end_mono = start_mono + duration_seconds
        previous_mono = start_mono
        previous_cpu = time.process_time()
        samples = []

        while time.monotonic() < end_mono:
            time.sleep(min(60.0, max(0.0, end_mono - time.monotonic())))
            sample_mono = time.monotonic()
            status_response = client.get(
                "/api/v1/status", headers={"Host": "localhost"}
            )
            assert status_response.status_code == 200
            status = status_response.get_json()
            camera_status = {
                camera_id: _camera(status, camera_id) for camera_id in (1, 2)
            }
            assert all(
                item["state"] == "continuous" and item["recording_active"]
                for item in camera_status.values()
            ), f"recording worker stopped during long run: {camera_status}"
            cpu_now = time.process_time()
            interval = max(sample_mono - previous_mono, 0.001)
            cpu_percent = (cpu_now - previous_cpu) / interval * 100
            samples.append(
                {
                    "elapsed_seconds": round(sample_mono - start_mono, 1),
                    "process_cpu_percent_of_one_core": round(cpu_percent, 1),
                    "process_rss_bytes": _process_rss_bytes(),
                    "memory_available_bytes": _linux_memory_bytes("MemAvailable"),
                    "temperature_celsius": _temperature_celsius(),
                    "throttled": _vcgencmd("get_throttled"),
                    "frames": {
                        str(camera_id): camera_status[camera_id]["frames"]
                        for camera_id in (1, 2)
                    },
                    "dropped_frames": {
                        str(camera_id): camera_status[camera_id]["dropped_frames"]
                        for camera_id in (1, 2)
                    },
                }
            )
            previous_mono = sample_mono
            previous_cpu = cpu_now

        elapsed_seconds = time.monotonic() - start_mono
        final_capture = {
            camera_id: {
                "frames": camera_status[camera_id]["frames"],
                "dropped_frames": camera_status[camera_id]["dropped_frames"],
            }
            for camera_id in (1, 2)
        }
        assert all(
            final_capture[camera_id]["frames"] > initial_frames[camera_id]
            for camera_id in (1, 2)
        )
        csrf = client.get(
            "/api/v1/auth/status", headers={"Host": "localhost"}
        ).get_json()["csrf_token"]
        for camera_id in (1, 2):
            _post(client, f"/api/v1/cameras/{camera_id}/stop", {}, csrf)
        stopped = _wait_for(
            client,
            lambda status: all(
                _camera(status, camera_id)["state"] == "stopped" for camera_id in (1, 2)
            ),
            timeout=60,
            description="final continuous MP4 files",
        )
        assert all(
            _camera(stopped, camera_id)["state"] == "stopped" for camera_id in (1, 2)
        )

        per_camera = {}
        for camera_id in (1, 2):
            response = client.get(
                f"/api/v1/recordings?camera_id={camera_id}",
                headers={"Host": "localhost"},
            )
            assert response.status_code == 200
            recordings = response.get_json()["items"]
            assert len(recordings) >= 10
            assert all(item["state"] == "finalized" for item in recordings)
            metrics = _media_span_metrics(recordings)
            assert metrics["recorded_duration_seconds"] >= elapsed_seconds * 0.95
            assert metrics["max_time_gap_seconds"] <= 5
            assert metrics["sequence_gaps"] >= 0
            for recording in recordings:
                video_path = environment.volume_root / recording["relative_path"]
                assert video_path.is_file()
                with av.open(str(video_path)) as container:
                    assert container.streams.video[0].codec_context.name == "h264"
                    assert next(container.decode(video=0), None) is not None
            per_camera[str(camera_id)] = {
                **metrics,
                "recording_files": len(recordings),
                "capture_frames": final_capture[camera_id]["frames"],
                "capture_frame_delta": (
                    final_capture[camera_id]["frames"] - initial_frames[camera_id]
                ),
                "reported_dropped_frames": final_capture[camera_id]["dropped_frames"],
            }

        disk_end = _device_write_bytes(root)
        cpu_samples = [item["process_cpu_percent_of_one_core"] for item in samples]
        rss_samples = [item["process_rss_bytes"] for item in samples]
        available_samples = [item["memory_available_bytes"] for item in samples]
        temperature_samples = [
            item["temperature_celsius"]
            for item in samples
            if item["temperature_celsius"] is not None
        ]
        report = {
            "duration_seconds": round(elapsed_seconds, 1),
            "sample_count": len(samples),
            "root_device_write_delta_bytes": (
                None
                if disk_start is None or disk_end is None
                else disk_end - disk_start
            ),
            "process_cpu_min_percent_of_one_core": min(cpu_samples, default=None),
            "process_cpu_max_percent_of_one_core": max(cpu_samples, default=None),
            "process_cpu_mean_percent_of_one_core": (
                round(sum(cpu_samples) / len(cpu_samples), 2) if cpu_samples else None
            ),
            "process_rss_min_bytes": min(rss_samples, default=None),
            "process_rss_max_bytes": max(rss_samples, default=None),
            "memory_available_min_bytes": min(available_samples, default=None),
            "temperature_min_celsius": min(temperature_samples, default=None),
            "temperature_max_celsius": max(temperature_samples, default=None),
            "temperature_sample_count": len(temperature_samples),
            "cameras": per_camera,
            "initial_capture_frames": {
                str(camera_id): initial_frames[camera_id] for camera_id in (1, 2)
            },
        }
        print("CAM16_LONG_RUN_METRICS=" + json.dumps(report, sort_keys=True))
    finally:
        environment.runtime.close()
        temporary_root.cleanup()


def test_virtual_source_is_not_the_product_default():
    with tempfile.TemporaryDirectory() as temporary:
        runtime = RecorderRuntime(Path(temporary) / "state")
        assert isinstance(runtime.discovery, V4L2CameraDiscovery)
        assert runtime._camera_source_factory is None


def test_virtual_camera_emits_identifiable_jpeg_and_fault_controls():
    pytest.importorskip("PIL.Image")
    from raspi_recorder.adapters.v4l2_camera import V4L2CaptureConfig
    from raspi_recorder.domain.camera import CameraSettings
    from tests.acceptance.virtual_two_camera import (
        AcceptanceDiscovery,
        VirtualCameraControl,
        VirtualCameraSource,
    )

    discovery = AcceptanceDiscovery(real_camera_1=False)
    control = VirtualCameraControl()
    config = V4L2CaptureConfig(
        discovery.camera_2.device_path,
        2,
        1,
        "mjpeg",
        CameraSettings(width=320, height=240, fps=5),
    )
    source = VirtualCameraSource(config, SystemClock(), SystemTimezone(), control)
    frames = source.frames(threading.Event())
    frame = next(frames)
    assert frame.sequence == 1
    assert frame.generation == 1
    assert frame.jpeg_bytes.startswith(b"\xff\xd8")
    control.invalid_jpeg.set()
    malformed = next(frames)
    assert malformed.sequence == 2
    assert not malformed.jpeg_bytes.startswith(b"\xff\xd8")
    frames.close()
    source.close()

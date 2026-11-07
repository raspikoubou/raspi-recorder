"""Serve a short-lived real-Pi browser fixture with one physical camera.

Run with the complete branch on PYTHONPATH so its UI and engine are tested together.
The update source is display-only; asset download and apply are blocked.
"""

from __future__ import annotations

import json
import signal
import socket
import tempfile
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from virtual_two_camera import create_acceptance_environment
from werkzeug.serving import make_server

from raspi_recorder.adapters.linux_storage import LinuxVolumeScanner
from raspi_recorder.application.update_service import UpdateService
from raspi_recorder.domain.updates import Release, ReleaseAsset


class LocalReleaseSource:
    """Display-only fixture; downloading an update is deliberately blocked."""

    def __init__(self) -> None:
        self.archive = b"local browser fixture; not a published release\n"
        self.checksums = b"0" * 64 + b"  raspi-recorder-0.1.33.tar.zst\n"
        self.release = Release(
            release_id=1901,
            tag_name="v0.1.33",
            name="Local browser fixture (not published)",
            body="Testing only. This is not a GitHub Release or installable asset.",
            draft=False,
            prerelease=False,
            published_at="2026-10-21T00:00:00Z",
            assets=(
                ReleaseAsset(
                    asset_id=1902,
                    name="raspi-recorder-0.1.33.tar.zst",
                    size=len(self.archive),
                    state="uploaded",
                    digest=None,
                ),
                ReleaseAsset(
                    asset_id=1903,
                    name="SHA256SUMS",
                    size=len(self.checksums),
                    state="uploaded",
                    digest=None,
                ),
            ),
        )

    def list_releases(self) -> tuple[Release, ...]:
        return (self.release,)

    def download_asset(
        self, release_id: int, asset: ReleaseAsset, destination: Path, *, max_bytes: int
    ) -> str:
        raise AssertionError("display-only UI fixture must not download or apply")


def main() -> None:
    import sys

    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "usage: serve_pi_usb_browser_fixture.py <USB-UUID> <Pi-LAN-IP> [port]"
        )
    wanted_id, listen_address = sys.argv[1:3]
    port = int(sys.argv[3]) if len(sys.argv) == 4 else 18080
    volumes = [
        volume
        for volume in LinuxVolumeScanner().scan()
        if volume.persistent_id == wanted_id and volume.kind.value == "USB"
    ]
    if len(volumes) != 1:
        raise SystemExit(f"USB UUID is missing or ambiguous: {wanted_id}")
    volume = volumes[0]
    if volume.filesystem not in {"ext4", "exfat"} or volume.read_only:
        raise SystemExit("browser fixture requires a writable ext4/exFAT USB volume")
    if volume.mountpoint == Path("/"):
        raise SystemExit("refusing a root filesystem as the recording volume")

    temporary = tempfile.TemporaryDirectory(prefix="raspi-recorder-pi-browser-")
    environment = None
    server = None
    server_thread = None
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        environment = create_acceptance_environment(
            Path(temporary.name),
            real_camera_1=True,
            mounted_volume=volume,
            trusted_hosts=("localhost", listen_address),
        )
        service = environment.runtime.settings_service
        original_transition = service.transition_operation

        def traced_transition(
            operation_id: str,
            expected_stage: Any,
            next_stage: Any,
            *,
            error: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> Any:
            try:
                return original_transition(
                    operation_id,
                    expected_stage,
                    next_stage,
                    error=error,
                    recovery=recovery,
                )
            except Exception as exc:
                try:
                    current = service.load_operation(operation_id)
                    current_stage = None if current is None else current.stage.value
                except Exception:
                    current_stage = "unavailable"
                print(
                    json.dumps(
                        {
                            "event": "operation_transition_failed",
                            "operation_id": operation_id,
                            "expected_stage": getattr(
                                expected_stage, "value", str(expected_stage)
                            ),
                            "next_stage": getattr(next_stage, "value", str(next_stage)),
                            "current_stage": current_stage,
                            "error": repr(exc),
                            "traceback": traceback.format_exc(),
                        }
                    ),
                    flush=True,
                )
                raise

        service.transition_operation = traced_transition
        opt_root = Path(temporary.name) / "opt"
        state_root = Path(temporary.name) / "state"
        protected_root = Path(temporary.name) / "protected"
        for directory in (opt_root, protected_root):
            directory.mkdir(mode=0o700)
        environment.runtime.update_service = UpdateService(
            current_version="0.1.32",
            source=LocalReleaseSource(),
            data_directory=state_root,
            install_directory=opt_root,
            privileged_state_directory=protected_root,
        )
        server = make_server(listen_address, port, environment.app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(
            json.dumps(
                {
                    "host": socket.gethostname(),
                    "started_utc": datetime.now(UTC).isoformat(),
                    "url": f"http://{listen_address}:{port}/",
                    "volume_uuid": volume.persistent_id,
                    "volume_mount": str(volume.mountpoint),
                    "filesystem": volume.filesystem,
                    "physical_camera_port_key": environment.camera_1_port_key,
                    "camera_2": "virtual synthetic source",
                    "update_source": "local display-only fixture; no download/apply",
                    "test_root": temporary.name,
                    "recordings_root": str(environment.volume_root),
                },
                indent=2,
            ),
            flush=True,
        )
        stop.wait()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if environment is not None:
            environment.runtime.close()
        temporary.cleanup()


if __name__ == "__main__":
    main()

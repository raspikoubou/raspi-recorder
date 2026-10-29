from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from raspi_recorder.adapters.live_hls import LiveHlsPublisher
from raspi_recorder.domain.camera import OverlayCorner, Quality, RecordingMode
from raspi_recorder.domain.capacity import VolumeKind
from raspi_recorder.domain.errors import SettingsApplyError
from raspi_recorder.domain.frames import FrameBufferFullError, FrameEnvelope
from raspi_recorder.domain.operations import OperationStage
from raspi_recorder.domain.settings import ApplicationSettings
from raspi_recorder.engine.runtime import (
    _FRAME_PAYLOAD_BUDGET_BYTES,
    CameraCaptureWorker,
    CameraWorkerStopTimeout,
    EngineControlHandler,
    RecorderRuntime,
    RuntimeTransitionBusy,
    _effective_camera_settings,
    _parse_range,
    _worker_error_blocker,
)
from raspi_recorder.ports.engine import EngineResponse, EngineStreamResponse
from tests.fakes import FakeClock, FakeClockSetter


class EngineRangeTests(unittest.TestCase):
    def test_unsupported_camera_profile_is_rejected_before_save_or_worker_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            current = runtime.settings_service.initialize()
            runtime._ports = (
                SimpleNamespace(
                    port_key="usb-test",
                    profiles=(SimpleNamespace(width=640, height=480, fps=(10,)),),
                ),
            )
            worker = SimpleNamespace(stop=Mock())
            runtime._cameras[1].worker = worker
            runtime._refresh_once = Mock()
            for width, height, fps in [(1280, 720, 15), (640, 480, 30)]:
                candidate = replace(
                    current,
                    cameras=(
                        replace(
                            current.cameras[0],
                            usb_port_key="usb-test",
                            width=width,
                            height=height,
                            fps=fps,
                        ),
                        current.cameras[1],
                    ),
                )
                with self.subTest(profile=(width, height, fps)):
                    with self.assertRaisesRegex(
                        ValueError, "解像度・fpsに対応"
                    ) as raised:
                        runtime.apply_settings(
                            candidate, expected_revision=current.revision
                        )
                    self.assertEqual(runtime.settings(), current)
                    self.assertEqual(
                        raised.exception.code, "unsupported_camera_profile"
                    )
                    self.assertEqual(
                        raised.exception.recovery["state"],
                        "previous_settings_unchanged",
                    )
                    worker.stop.assert_not_called()

    def test_recording_waits_for_update_commit_but_not_dependency_build(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            current = runtime.settings_service.initialize()
            configured = replace(
                current,
                power_saving_enabled=False,
                cameras=tuple(
                    replace(camera, usb_port_key=f"camera-{camera_id}")
                    for camera_id, camera in enumerate(current.cameras, 1)
                ),
            )
            runtime.settings_service.compare_and_swap(configured, current.revision)
            runtime._ports = tuple(
                SimpleNamespace(
                    port_key=f"camera-{camera_id}",
                    profiles=(
                        SimpleNamespace(
                            width=camera.width, height=camera.height, fps=(camera.fps,)
                        ),
                    ),
                )
                for camera_id, camera in enumerate(configured.cameras, 1)
            )
            runtime._selected = (SimpleNamespace(), Path(directory), "volume-1")
            operation = runtime.settings_service.create_operation(
                operation_id="update-recording-gate",
                request_id="update-recording-gate",
                kind="update",
                target={},
                previous_settings={},
                requested_settings={},
            )
            for stage in (
                OperationStage.VALIDATING,
                OperationStage.FINALIZING,
                OperationStage.APPLYING,
                OperationStage.STARTING,
                OperationStage.SUCCEEDED,
            ):
                operation = runtime.settings_service.transition_operation(
                    operation.operation_id, operation.stage, stage
                )
                blocked = stage in {OperationStage.APPLYING, OperationStage.STARTING}
                for camera_id in (1, 2):
                    with self.subTest(stage=stage, camera_id=camera_id):
                        runtime._cameras[camera_id].worker = None
                        worker = SimpleNamespace(
                            start=Mock(),
                            snapshot={"capture_active": True},
                        )
                        with patch(
                            "raspi_recorder.engine.runtime.CameraCaptureWorker",
                            return_value=worker,
                        ) as create_worker:
                            runtime._reconcile_camera(camera_id)
                        self.assertEqual(
                            create_worker.call_args.kwargs["recording_enabled"],
                            not blocked,
                        )
                        self.assertEqual(
                            "update_in_progress"
                            in runtime._cameras[camera_id].blockers,
                            blocked,
                        )

    def test_frame_payload_budget_matches_single_camera_design_budget(self):
        self.assertEqual(_FRAME_PAYLOAD_BUDGET_BYTES, 384 * 1024 * 1024)

    def test_continuous_worker_accepts_a_frame_larger_than_two_mib(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = ApplicationSettings().camera(1)
            worker = CameraCaptureWorker(
                camera_id=1,
                generation=1,
                settings=settings,
                port=SimpleNamespace(),
                recording_enabled=True,
                clock=FakeClock(),
                timezone=SimpleNamespace(name=lambda: "UTC"),
                catalog=SimpleNamespace(),
                volume_store=None,
                volume=None,
                recordings_root=None,
                volume_id=None,
                engine_epoch="test-epoch",
                settings_revision=1,
                live_directory=Path(temp_dir) / "live",
            )
            session = worker._pipeline.start_continuous()
            frame = FrameEnvelope(
                sequence=1,
                capture_mono_ns=1,
                capture_utc_ns=1,
                timezone="UTC",
                generation=1,
                jpeg_bytes=b"x" * (2 * 1024 * 1024 + 1),
            )

            worker._pipeline.push(frame)
            batch = session.take_batch(max_frames=1)

            self.assertIsNotNone(batch)
            assert batch is not None
            self.assertEqual(batch.size_bytes, len(frame.jpeg_bytes))
            batch.acknowledge()
            worker._pipeline.clear("test_complete")
            self.assertEqual(worker._pipeline.ring.retained_bytes, 0)

    def test_single_byte_ranges_and_suffix_ranges(self):
        self.assertEqual(_parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(_parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(_parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(_parse_range("bytes=98-120", 100), (98, 99))

    def test_invalid_and_multi_ranges_are_rejected(self):
        self.assertIsNone(_parse_range("bytes=100-", 100))
        self.assertIsNone(_parse_range("bytes=5-4", 100))
        self.assertIsNone(_parse_range("bytes=0-1,4-5", 100))
        self.assertIsNone(_parse_range("bytes=-0", 100))
        self.assertIsNone(_parse_range("bytes=0-0", 0))

    def test_worker_overload_is_not_reported_as_generic_io_error(self):
        self.assertEqual(_worker_error_blocker("FrameQueueError"), "overload")
        self.assertEqual(_worker_error_blocker("FrameBufferFullError"), "overload")
        self.assertEqual(_worker_error_blocker("OSError"), "io_error")


class RuntimeStateTests(unittest.TestCase):
    def test_failed_worker_shutdown_keeps_engine_lock_until_cleanup_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            replacement = RecorderRuntime(directory, clock=FakeClock())
            runtime._acquire_engine_lock()
            stuck = Mock()
            stuck.stop.side_effect = CameraWorkerStopTimeout(1, ("capture",))
            other = Mock()
            runtime._cameras[1].worker = stuck
            runtime._cameras[2].worker = other
            try:
                with self.assertRaises(CameraWorkerStopTimeout):
                    runtime.close(timeout_seconds=0.1)
                other.stop.assert_called_once()
                self.assertIsNotNone(runtime._engine_lock_fd)
                with self.assertRaisesRegex(RuntimeError, "別のengine"):
                    replacement._acquire_engine_lock()
                stuck.stop.side_effect = None
                runtime.close(timeout_seconds=0.1)
                replacement._acquire_engine_lock()
            finally:
                stuck.stop.side_effect = None
                runtime.close(timeout_seconds=0.1)
                replacement.close(timeout_seconds=0.1)

    def test_uncertain_update_reconciliation_retries_failed_journal_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            service = Mock()
            service.load_operation.return_value = SimpleNamespace(
                stage=OperationStage.VALIDATING
            )
            service.transition_operation.side_effect = [OSError("busy"), None]
            runtime.settings_service = service
            runtime.update_service = Mock()
            runtime._uncertain_update_operations["pending"] = 0
            runtime._reconcile_uncertain_update_operations()
            self.assertIn("pending", runtime._uncertain_update_operations)
            runtime.update_service.cleanup.assert_not_called()
            runtime._reconcile_uncertain_update_operations()
            self.assertNotIn("pending", runtime._uncertain_update_operations)
            runtime.update_service.cleanup.assert_called_once_with("pending")

    def test_second_engine_is_rejected_before_recovering_a_live_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            first = RecorderRuntime(directory, clock=FakeClock())
            second = RecorderRuntime(directory, clock=FakeClock())
            for runtime in (first, second):
                runtime._supervise = lambda owner=runtime: owner._stop.wait()
                runtime._supervise_capacity = lambda owner=runtime: owner._stop.wait()
            with patch("raspi_recorder.engine.runtime.EngineSocketServer"):
                try:
                    first.start()
                    first.catalog.upsert_volume(
                        volume_id="v",
                        persistent_id="sd",
                        kind=VolumeKind.MICROSD,
                        fs_type="ext4",
                        marker_id="v",
                        connected=True,
                        selected=True,
                        last_seen_utc_ns=1,
                    )
                    first.catalog.create_recording(
                        mode=RecordingMode.CONTINUOUS,
                        recording_id="live",
                        volume_id="v",
                        relative_path="live.mp4",
                        camera_id=1,
                        logical_event_id=None,
                        part_index=1,
                        boot_id="test",
                        writer_epoch=first.epoch,
                        created_utc_ns=1,
                        settings_snapshot={},
                    )
                    with self.assertRaisesRegex(RuntimeError, "別のengine"):
                        second.start()
                    self.assertEqual(
                        first.catalog.get_recording("live").state.value, "writing"
                    )
                    first.close(timeout_seconds=2)
                    second.start()
                    self.assertEqual(
                        second.catalog.get_recording("live").state.value, "abnormal"
                    )
                finally:
                    first.close(timeout_seconds=2)
                    second.close(timeout_seconds=2)

    def test_failed_startup_releases_the_state_directory_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            with (
                patch.object(
                    runtime.catalog,
                    "initialize",
                    side_effect=RuntimeError("bad database"),
                ),
                self.assertRaisesRegex(RuntimeError, "bad database"),
            ):
                runtime.start()
            self.assertIsNone(runtime._engine_lock_fd)
            replacement = RecorderRuntime(directory, clock=FakeClock())
            replacement._acquire_engine_lock()
            replacement.close(timeout_seconds=2)

    def test_power_saving_worker_uses_derived_values_and_preserves_saved_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            current = runtime.settings_service.initialize()
            normal_camera = replace(
                current.camera(1),
                usb_port_key="usb-preview",
                width=1280,
                height=720,
                fps=30,
                audio_enabled=True,
            )
            configured = replace(
                current,
                power_saving_enabled=True,
                cameras=(normal_camera, current.camera(2)),
            )
            runtime.settings_service.compare_and_swap(configured, current.revision)
            runtime._ports = (
                SimpleNamespace(
                    port_key="usb-preview",
                    device_path="/dev/video-preview-test",
                    profiles=(
                        SimpleNamespace(width=1280, height=720, fps=(30,)),
                        SimpleNamespace(width=640, height=480, fps=(15,)),
                    ),
                ),
            )
            runtime._cameras[1].desired_running = False
            preview_worker = SimpleNamespace(
                start=Mock(),
                stop=Mock(),
                snapshot={
                    "fatal_error": False,
                    "capture_active": False,
                    "recording_active": False,
                },
            )

            with patch(
                "raspi_recorder.engine.runtime.CameraCaptureWorker",
                return_value=preview_worker,
            ) as create_worker:
                runtime._reconcile_camera(1)

            effective = create_worker.call_args.kwargs["settings"]
            self.assertEqual(
                (effective.width, effective.height, effective.fps), (640, 480, 15)
            )
            self.assertFalse(effective.audio_enabled)
            self.assertEqual(runtime.settings().camera(1), normal_camera)
            status_camera = runtime.status()["cameras"][0]
            self.assertEqual(
                status_camera["effective_settings"],
                {
                    "width": 640,
                    "height": 480,
                    "fps": 15,
                    "audio_enabled": False,
                    "profile_reduced": True,
                },
            )

    def test_default_unregistered_camera_does_not_start_capture_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()

            runtime._reconcile_camera(1)

            camera = runtime.status()["cameras"][0]
            self.assertIsNone(runtime._cameras[1].worker)
            self.assertEqual(camera["state"], "blocked")
            self.assertFalse(camera["capture_active"])
            self.assertIn("invalid_config", camera["blockers"])

    def test_manually_stopped_configured_camera_keeps_preview_only_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            current = runtime.settings_service.current()
            configured = replace(
                current,
                cameras=(
                    replace(current.cameras[0], usb_port_key="usb-preview"),
                    current.cameras[1],
                ),
            )
            runtime.settings_service.compare_and_swap(configured, current.revision)
            runtime._ports = (
                SimpleNamespace(
                    port_key="usb-preview",
                    device_path="/dev/video-preview-test",
                    profiles=(
                        SimpleNamespace(
                            width=configured.cameras[0].width,
                            height=configured.cameras[0].height,
                            fps=(configured.cameras[0].fps,),
                        ),
                    ),
                ),
            )
            runtime._cameras[1].desired_running = False
            preview_worker = SimpleNamespace(
                start=Mock(),
                stop=Mock(),
                snapshot={
                    "fatal_error": False,
                    "capture_active": False,
                    "recording_active": False,
                },
            )

            with patch(
                "raspi_recorder.engine.runtime.CameraCaptureWorker",
                return_value=preview_worker,
            ) as create_worker:
                runtime._reconcile_camera(1)

            create_worker.assert_called_once()
            self.assertFalse(create_worker.call_args.kwargs["recording_enabled"])
            preview_worker.start.assert_called_once()
            self.assertIs(runtime._cameras[1].worker, preview_worker)
            self.assertEqual(runtime.status()["cameras"][0]["state"], "stopped")

    def test_worker_stop_error_is_not_hidden_by_manual_stopped_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            runtime._cameras[1].desired_running = False
            runtime._cameras[1].state = "error"
            runtime._cameras[1].worker = SimpleNamespace(
                snapshot={
                    "fatal_error": True,
                    "capture_active": True,
                    "recording_active": False,
                }
            )

            camera = runtime.status()["cameras"][0]

            self.assertEqual(camera["state"], "error")
            self.assertTrue(camera["capture_active"])

    def test_status_exposes_only_a_valid_system_iana_timezone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            runtime.timezone = SimpleNamespace(name=lambda: "Asia/Tokyo")
            self.assertEqual(runtime.status()["time"]["timezone"], "Asia/Tokyo")

            runtime.timezone = SimpleNamespace(name=lambda: "not/a-timezone")
            self.assertIsNone(runtime.status()["time"]["timezone"])

    def test_time_warning_tracks_explicit_synchronization_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = FakeClock()
            setter = FakeClockSetter(clock, synchronized=True)
            runtime = RecorderRuntime(temp_dir, clock=clock, clock_setter=setter)
            runtime.settings_service.initialize()
            runtime.timezone = SimpleNamespace(name=lambda: "Asia/Tokyo")

            confirmed = runtime.status()

            self.assertEqual(confirmed["time"]["quality"], "synchronized")
            self.assertNotIn("time_synchronization_not_verified", confirmed["warnings"])
            setter.synchronized = False
            runtime.time_status()
            unverified = runtime.status()
            self.assertEqual(unverified["time"]["quality"], "unverified")
            self.assertIn("time_synchronization_not_verified", unverified["warnings"])

    def test_manual_stop_survives_engine_restart_but_not_pi_reboot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            clock = FakeClock()
            first = RecorderRuntime(directory, clock=clock)
            first._cameras[1].desired_running = False
            first._save_desired_state()

            restarted = RecorderRuntime(directory, clock=clock)
            restarted._load_desired_state()
            self.assertFalse(restarted._cameras[1].desired_running)
            self.assertTrue(restarted._cameras[2].desired_running)

            clock.reboot("boot-test-2")
            rebooted = RecorderRuntime(directory, clock=clock)
            rebooted._load_desired_state()
            self.assertTrue(rebooted._cameras[1].desired_running)
            self.assertTrue(rebooted._cameras[2].desired_running)

    def test_engine_idempotency_response_cache_is_bounded(self):
        handler = EngineControlHandler(SimpleNamespace())
        with handler._lock:
            for index in range(1_025):
                handler._remember_request_locked(
                    f"request-{index}", "fingerprint", EngineResponse(200, {})
                )

        self.assertEqual(len(handler._requests), 1_024)
        self.assertNotIn("request-0", handler._requests)
        self.assertIn("request-1024", handler._requests)


class EngineSettingsValidationTests(unittest.TestCase):
    def test_power_saving_selects_the_lowest_supported_nonincreasing_profile(self):
        camera = replace(
            ApplicationSettings().camera(1),
            width=1920,
            height=1080,
            fps=30,
            audio_enabled=True,
        )
        port = SimpleNamespace(
            profiles=(
                SimpleNamespace(width=1920, height=1080, fps=(30,)),
                SimpleNamespace(width=1280, height=720, fps=(30, 15)),
                SimpleNamespace(width=640, height=480, fps=(30, 15)),
            )
        )

        effective, reduced = _effective_camera_settings(camera, True, port)

        self.assertTrue(reduced)
        self.assertEqual(
            (effective.width, effective.height, effective.fps), (640, 480, 15)
        )
        self.assertFalse(effective.audio_enabled)
        self.assertEqual((camera.width, camera.height, camera.fps), (1920, 1080, 30))
        self.assertTrue(camera.audio_enabled)

    def test_power_saving_without_a_lower_profile_keeps_dimensions_and_mutes_audio(
        self,
    ):
        camera = replace(
            ApplicationSettings().camera(1),
            width=1280,
            height=720,
            fps=15,
            audio_enabled=True,
        )
        port = SimpleNamespace(
            profiles=(SimpleNamespace(width=1280, height=720, fps=(15,)),)
        )

        effective, reduced = _effective_camera_settings(camera, True, port)

        self.assertFalse(reduced)
        self.assertEqual(
            (effective.width, effective.height, effective.fps), (1280, 720, 15)
        )
        self.assertFalse(effective.audio_enabled)
        self.assertIs(_effective_camera_settings(camera, False, port)[0], camera)

    def test_audio_setting_can_stay_enabled_without_a_current_microphone(self):
        current = ApplicationSettings()
        candidate = current.to_payload()
        cameras = candidate["cameras"]
        assert isinstance(cameras, list)
        cameras[0]["audio_enabled"] = True
        loaded = ApplicationSettings.from_payload(candidate)

        self.assertTrue(loaded.camera(1).audio_enabled)
        self.assertIsNone(loaded.camera(1).audio_interface_key)

    def test_audio_setting_is_accepted_and_persisted_by_the_settings_api(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            current = runtime.settings_service.initialize()
            requested = current.to_payload()
            cameras = requested["cameras"]
            assert isinstance(cameras, list)
            cameras[0]["audio_enabled"] = True
            runtime._refresh_once = Mock()
            runtime._validate_candidate = Mock()
            runtime._reconcile_camera = Mock()

            def run_inline(*_args, **kwargs):
                kwargs["target"](*kwargs["args"])
                return SimpleNamespace(start=Mock())

            handler = EngineControlHandler(runtime)
            with patch(
                "raspi_recorder.engine.runtime.threading.Thread",
                side_effect=run_inline,
            ):
                result = handler.handle(
                    "PATCH",
                    "/api/v1/settings",
                    {
                        "expected_revision": current.revision,
                        "request_id": "persist-audio-enabled",
                        "settings": requested,
                    },
                )

            self.assertEqual(result.status_code, 202)
            self.assertTrue(runtime.settings().camera(1).audio_enabled)

    def test_update_endpoints_report_missing_source_without_claiming_a_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "raspi_recorder.engine.runtime.release_source_from_environment",
                return_value=None,
            ):
                runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            handler = EngineControlHandler(runtime)

            status = handler.handle("GET", "/api/v1/update/status", None)
            check = handler.handle(
                "POST",
                "/api/v1/update/check",
                {"request_id": "update-check-without-source"},
            )
            invalid_apply = handler.handle("POST", "/api/v1/update/apply", {})
            apply = handler.handle(
                "POST",
                "/api/v1/update/apply",
                {
                    "request_id": "update-apply-without-source",
                    "version": "1.2.3",
                    "confirmed": True,
                },
            )

            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.body["source_configured"])
            self.assertIsNone(status.body["candidate"])
            self.assertEqual(check.status_code, 503)
            self.assertEqual(check.body["error"]["code"], "update_source_unconfigured")
            self.assertEqual(invalid_apply.status_code, 400)
            self.assertEqual(apply.status_code, 503)
            self.assertEqual(apply.body["error"]["code"], "update_source_unconfigured")

    def test_update_check_operation_can_complete_from_validating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            runtime.update_service = SimpleNamespace(
                source_configured=True,
                check=Mock(return_value={"candidate": {"version": "1.2.3"}}),
            )
            handler = EngineControlHandler(runtime)

            def run_inline(*_args, **kwargs):
                kwargs["target"](*kwargs["args"])
                return SimpleNamespace(start=Mock())

            with patch(
                "raspi_recorder.engine.runtime.threading.Thread",
                side_effect=run_inline,
            ):
                accepted = handler.handle(
                    "POST",
                    "/api/v1/update/check",
                    {"request_id": "update-check-succeeds"},
                )

            self.assertEqual(accepted.status_code, 202)
            operation = runtime.settings_service.load_operation(
                accepted.body["operation_id"]
            )
            self.assertIsNotNone(operation)
            assert operation is not None
            self.assertEqual(operation.stage, OperationStage.SUCCEEDED)
            self.assertEqual(
                operation.recovery,
                {
                    "state": "update_check_complete",
                    "candidate_available": True,
                },
            )


class PersistentSettingsOperationTests(unittest.TestCase):
    def test_runtime_marks_interrupted_operation_for_state_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            previous = runtime.settings_service.initialize()
            operation = runtime.settings_service.create_operation(
                operation_id="interrupted-settings-operation",
                request_id="interrupted-settings-request",
                kind="settings",
                target={"scope": "settings", "base_revision": previous.revision},
                previous_settings=previous.to_payload(),
                requested_settings=previous.to_payload(),
            )

            runtime._fail_interrupted_operations()

            recovered = runtime.settings_service.load_operation(operation.operation_id)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.stage, OperationStage.FAILED)
            self.assertEqual(
                recovered.error,
                {"code": "engine_restarted_during_settings_operation"},
            )
            self.assertEqual(recovered.recovery["current_revision"], previous.revision)

    def test_settings_operation_survives_handler_recreation_and_duplicate_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            current = runtime.settings_service.initialize()
            requested = current.to_payload()
            cameras = requested["cameras"]
            assert isinstance(cameras, list)
            cameras[0]["overlay_corner"] = OverlayCorner.TOP_RIGHT.value
            runtime._refresh_once = Mock()
            runtime._validate_candidate = Mock()
            runtime._reconcile_camera = Mock()

            first_handler = EngineControlHandler(runtime)
            body = {
                "expected_revision": current.revision,
                "request_id": "persistent-settings-request-1",
                "settings": requested,
            }

            def run_inline(*_args, **kwargs):
                kwargs["target"](*kwargs["args"])
                return SimpleNamespace(start=Mock())

            with patch(
                "raspi_recorder.engine.runtime.threading.Thread",
                side_effect=run_inline,
            ):
                accepted = first_handler.handle("PATCH", "/api/v1/settings", body)

            self.assertEqual(accepted.status_code, 202)
            operation_id = accepted.body["operation_id"]
            second_handler = EngineControlHandler(runtime)
            recovered = second_handler.handle(
                "GET", f"/api/v1/operations/{operation_id}", None
            )
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.body["state"], "succeeded")
            self.assertEqual(recovered.body["result"]["revision"], current.revision + 1)

            with patch(
                "raspi_recorder.engine.runtime.threading.Thread",
                side_effect=run_inline,
            ) as thread_factory:
                retried = second_handler.handle("PATCH", "/api/v1/settings", body)
            self.assertEqual(retried.body, accepted.body)
            thread_factory.assert_not_called()
            self.assertEqual(runtime.settings().revision, current.revision + 1)

    def test_corrupt_manual_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "runtime-state.json").write_text("{broken", encoding="utf-8")
            runtime = RecorderRuntime(directory, clock=FakeClock())
            runtime._load_desired_state()
            self.assertFalse(runtime._cameras[1].desired_running)
            self.assertFalse(runtime._cameras[2].desired_running)
            self.assertEqual(runtime._runtime_state_error, "runtime_state_invalid")

    def test_per_camera_settings_restart_only_the_changed_camera(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            current = ApplicationSettings()
            candidate = replace(
                current,
                cameras=(replace(current.cameras[0], width=1920), current.cameras[1]),
            )
            worker_one = SimpleNamespace(stop=Mock())
            worker_two = SimpleNamespace(stop=Mock())
            runtime._cameras[1].worker = worker_one
            runtime._cameras[2].worker = worker_two
            runtime.settings_service = SimpleNamespace(
                current=lambda: current,
                compare_and_swap=lambda value, expected: replace(
                    value, revision=expected + 1
                ),
            )
            runtime._refresh_once = unittest.mock.Mock()
            runtime._validate_candidate = unittest.mock.Mock()
            runtime._reconcile_camera = unittest.mock.Mock()

            runtime.apply_settings(candidate, expected_revision=0)

            worker_one.stop.assert_called_once_with()
            worker_two.stop.assert_not_called()
            self.assertIsNone(runtime._cameras[1].worker)
            self.assertIs(runtime._cameras[2].worker, worker_two)

    def test_overlay_only_settings_do_not_stop_recording_workers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            current = ApplicationSettings()
            candidate = replace(
                current,
                cameras=(
                    replace(current.cameras[0], overlay_corner=OverlayCorner.TOP_RIGHT),
                    current.cameras[1],
                ),
            )
            worker_one = SimpleNamespace(stop=Mock())
            worker_two = SimpleNamespace(stop=Mock())
            runtime._cameras[1].worker = worker_one
            runtime._cameras[2].worker = worker_two
            runtime.settings_service = SimpleNamespace(
                current=lambda: current,
                compare_and_swap=lambda value, expected: replace(
                    value, revision=expected + 1
                ),
            )
            runtime._refresh_once = unittest.mock.Mock()
            runtime._validate_candidate = unittest.mock.Mock()
            runtime._reconcile_camera = unittest.mock.Mock()

            runtime.apply_settings(candidate, expected_revision=0)

            worker_one.stop.assert_not_called()
            worker_two.stop.assert_not_called()
            self.assertIs(runtime._cameras[1].worker, worker_one)
            self.assertIs(runtime._cameras[2].worker, worker_two)

    def test_settings_worker_start_failure_rolls_back_saved_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            initial = runtime.settings_service.current()
            bound = replace(
                initial,
                cameras=(
                    replace(initial.cameras[0], usb_port_key="usb-test"),
                    initial.cameras[1],
                ),
            )
            current = runtime.settings_service.compare_and_swap(bound, initial.revision)
            candidate = replace(
                current,
                cameras=(
                    replace(current.cameras[0], quality=Quality.HIGH),
                    current.cameras[1],
                ),
            )
            runtime._ports = (
                SimpleNamespace(
                    port_key="usb-test",
                    profiles=(SimpleNamespace(width=1280, height=720, fps=(15,)),),
                ),
            )
            old_worker = SimpleNamespace(stop=Mock())
            restored_worker = SimpleNamespace(stop=Mock())
            runtime._cameras[1].worker = old_worker
            runtime._refresh_once = Mock()
            runtime._validate_candidate = Mock()

            def reconcile(camera_id: int) -> None:
                if camera_id != 1:
                    return
                if runtime.settings().camera(1).quality is Quality.HIGH:
                    runtime._cameras[1].worker = None
                else:
                    runtime._cameras[1].worker = restored_worker

            runtime._reconcile_camera = Mock(side_effect=reconcile)

            with self.assertRaises(SettingsApplyError) as raised:
                runtime.apply_settings(candidate, expected_revision=current.revision)

            saved = runtime.settings()
            self.assertEqual(saved.camera(1).quality, Quality.STANDARD)
            self.assertEqual(saved.revision, current.revision + 2)
            self.assertIs(runtime._cameras[1].worker, restored_worker)
            self.assertEqual(raised.exception.code, "settings_apply_failed_rolled_back")
            self.assertEqual(
                raised.exception.recovery["state"], "previous_settings_restored"
            )

    def test_live_settings_update_keeps_worker_and_updates_future_writer_state(self):
        # Construct the real worker: inventing a retired coordinator attribute
        # in a __new__ fixture hid the production overlay-save failure.
        with tempfile.TemporaryDirectory() as directory:
            settings = ApplicationSettings().camera(1)
            worker = CameraCaptureWorker(
                camera_id=1,
                generation=1,
                settings=settings,
                port=SimpleNamespace(),
                recording_enabled=True,
                clock=FakeClock(),
                timezone=SimpleNamespace(name=lambda: "UTC"),
                catalog=SimpleNamespace(),
                volume_store=None,
                volume=None,
                recordings_root=None,
                volume_id=None,
                engine_epoch="test-epoch",
                settings_revision=3,
                live_directory=Path(directory) / "live",
            )
            worker._live = SimpleNamespace(update_settings=Mock())
            worker._preview = SimpleNamespace(update_settings=Mock())
            worker._sync_audio_capture = Mock()
            factory = Mock()
            worker._writer_factories = {factory}
            candidate = replace(settings, overlay_enabled=False)
            worker.apply_live_settings(candidate, revision=4)
            self.assertEqual(worker._desired_writer_settings, candidate)
            self.assertEqual(worker.settings_revision, 4)
            worker._live.update_settings.assert_called_once_with(candidate)
            worker._preview.update_settings.assert_called_once_with(candidate)
            factory.update_settings.assert_called_once_with(candidate)


class LiveStreamTests(unittest.TestCase):
    def test_authenticated_live_demand_route_updates_only_requested_camera(self):
        runtime = SimpleNamespace(
            request_live_demand=Mock(return_value=True),
            release_live_demand=Mock(),
        )
        handler = EngineControlHandler(runtime)

        response = handler.handle("POST", "/api/v1/live/2/demand", {"active": True})
        released = handler.handle("POST", "/api/v1/live/2/demand", {"active": False})
        invalid = handler.handle("POST", "/api/v1/live/3/demand", {"active": True})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, {"accepted": True})
        runtime.request_live_demand.assert_called_once_with(2)
        self.assertEqual(released.status_code, 200)
        runtime.release_live_demand.assert_called_once_with(2)
        self.assertEqual(invalid.status_code, 404)

    def test_preview_demand_and_jpeg_stream_use_the_selected_camera(self):
        image = b"jpeg-frame"
        preview = SimpleNamespace(
            fresh_image=Mock(return_value=image),
            request_viewer=Mock(return_value=True),
            release_viewer=Mock(),
        )
        worker = SimpleNamespace(
            _preview=preview,
            request_preview_demand=Mock(return_value=True),
            release_preview_demand=Mock(),
        )
        runtime = SimpleNamespace(
            request_preview_demand=Mock(return_value=True),
            release_preview_demand=Mock(),
            _lock=threading.RLock(),
            _cameras={
                1: SimpleNamespace(worker=worker),
                2: SimpleNamespace(worker=None),
            },
        )
        handler = EngineControlHandler(runtime)
        demand = handler.handle(
            "POST", "/api/v1/cameras/1/preview/demand", {"active": True}
        )
        released = handler.handle(
            "POST", "/api/v1/cameras/1/preview/demand", {"active": False}
        )
        stream = handler.stream("GET", "/api/v1/cameras/1/preview.jpg")

        self.assertEqual(demand.body, {"accepted": True})
        self.assertEqual(released.body, {"accepted": True})
        self.assertEqual(runtime.request_preview_demand.call_args.args, (1,))
        self.assertEqual(runtime.release_preview_demand.call_args.args, (1,))
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.headers["Content-Type"], "image/jpeg")
        self.assertEqual(stream.headers["Content-Length"], str(len(image)))
        self.assertEqual(stream.handle.read(), image)

    def test_live_stream_confines_names_and_rewrites_playlist_segments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "camera-1" / "generation-7"
            directory.mkdir(parents=True)
            (directory / "index.m3u8").write_text(
                "#EXTM3U\n#EXTINF:1.0,\nsegment000008.ts\n",
                encoding="ascii",
            )
            (directory / "segment000008.ts").write_bytes(b"transport-stream")
            request_viewer = Mock(return_value=True)
            publisher = SimpleNamespace(
                directory=directory,
                request_viewer=request_viewer,
            )
            worker = SimpleNamespace(generation=7, _live=publisher)
            runtime = SimpleNamespace(
                _lock=threading.RLock(),
                _cameras={1: SimpleNamespace(worker=worker)},
                live_root=root,
            )
            handler = EngineControlHandler(runtime)
            with patch("raspi_recorder.engine.runtime._LIVE_ROOT", root):
                result = handler._live_stream("1", "7", "index.m3u8")
                self.assertIsInstance(result, EngineStreamResponse)
                assert isinstance(result, EngineStreamResponse)
                try:
                    playlist = result.handle.read().decode("ascii")
                    self.assertIn("/api/v1/live/1/7/segment000008.ts", playlist)
                finally:
                    result.close()
                segment = handler._live_stream("1", "7", "segment000008.ts")
                self.assertIsInstance(segment, EngineStreamResponse)
                assert isinstance(segment, EngineStreamResponse)
                try:
                    self.assertEqual(segment.handle.read(), b"transport-stream")
                finally:
                    segment.close()
                self.assertEqual(request_viewer.call_count, 2)
                traversal = handler._live_stream("1", "7", "../secret")
                self.assertEqual(traversal.status_code, 404)


class CameraWorkerShutdownTests(unittest.TestCase):
    def test_failed_audio_cleanup_does_not_open_another_source(self):
        for enabled in (False, True):
            with self.subTest(audio_needed=enabled):
                worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
                worker._lock = threading.RLock()
                worker._audio_lifecycle_lock = threading.RLock()
                worker._stop = threading.Event()
                worker._desired_writer_settings = replace(
                    ApplicationSettings().camera(1), audio_enabled=enabled
                )
                worker.recording_enabled = True
                source = Mock()
                source.close.side_effect = TimeoutError("not reaped")
                worker._audio_source = source
                worker._audio_thread = Mock()
                worker._audio_thread.is_alive.return_value = False
                worker._audio_stop_event = threading.Event()
                worker._audio_error = None
                worker._audio_buffer = SimpleNamespace(clear=Mock())
                with patch("raspi_recorder.engine.runtime.AlsaCaptureSource") as create:
                    worker._sync_audio_capture()
                create.assert_not_called()
                self.assertIs(worker._audio_source, source)
                self.assertEqual(worker._audio_error, "TimeoutError")

    def test_audio_capture_follows_live_viewer_demand_and_expiry(self):
        clock = FakeClock()
        settings = replace(
            ApplicationSettings().camera(1),
            audio_enabled=True,
            usb_port_key="synthetic-usb",
            audio_interface_key="synthetic-usb/1/capture/0",
        )
        audio_sources = []

        class FakeAudioSource:
            def __init__(self, *_args):
                self.ready = True
                self.last_error = None
                self.closed = threading.Event()
                audio_sources.append(self)

            def frames(self, stop_requested):
                try:
                    while not stop_requested.is_set():
                        time.sleep(0.001)
                        yield from ()
                finally:
                    self.closed.set()

            def request_stop(self):
                return None

            def close(self, _timeout):
                self.ready = False
                self.closed.set()

        worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
        worker._lock = threading.RLock()
        worker._audio_lifecycle_lock = threading.RLock()
        worker._stop = threading.Event()
        worker._desired_writer_settings = settings
        worker.settings = settings
        worker.recording_enabled = False
        with tempfile.TemporaryDirectory() as temp_dir:
            publisher = LiveHlsPublisher(
                Path(temp_dir) / "live",
                generation=1,
                settings=settings,
                clock=clock,
            )
            publisher._started = True
            worker._live = publisher
            worker.port = SimpleNamespace(port_key="synthetic-usb")
            worker.generation = 1
            worker.camera_id = 1
            worker._clock = clock
            worker._timezone = SimpleNamespace(name=lambda: "UTC")
            worker._audio_buffer = SimpleNamespace(clear=Mock())
            worker._audio_source = None
            worker._audio_thread = None
            worker._audio_stop_event = None
            worker._audio_error = None

            with patch(
                "raspi_recorder.engine.runtime.AlsaCaptureSource", FakeAudioSource
            ):
                worker._sync_audio_capture()
                self.assertEqual(audio_sources, [])

                self.assertTrue(publisher.request_viewer())
                worker._sync_audio_capture()
                self.assertEqual(len(audio_sources), 1)
                assert worker._audio_thread is not None
                self.assertTrue(worker._audio_thread.is_alive())

                clears_before_expiry = worker._audio_buffer.clear.call_count
                clock.advance(10_000_000_001)
                self.assertFalse(publisher.viewer_demand_active)
                worker._sync_audio_capture()
                worker._audio_thread.join(1)
                self.assertFalse(worker._audio_thread.is_alive())
                self.assertTrue(audio_sources[0].closed.is_set())
                self.assertGreater(
                    worker._audio_buffer.clear.call_count, clears_before_expiry
                )
                worker._sync_audio_capture()

            self.assertEqual(audio_sources[0].ready, False)
            self.assertIsNone(worker._audio_source)

    def test_two_synthetic_camera_streams_run_independently_after_one_ends(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sources = {}
            sources_lock = threading.Lock()
            profiles = (
                SimpleNamespace(
                    input_format="mjpeg", width=1280, height=720, fps=(15,)
                ),
            )
            port = SimpleNamespace(
                device_path="/dev/v4l/by-path/synthetic-video-index0",
                port_key="synthetic-usb",
                profiles=profiles,
            )

            class SyntheticSource:
                def __init__(self, config, _clock, _timezone):
                    self.config = config
                    self.stats = SimpleNamespace(dropped_frames=0, last_error=None)
                    self.frames_seen = 0
                    self.closed = threading.Event()
                    with sources_lock:
                        sources[config.camera_id] = self

                def frames(self, stop_requested):
                    try:
                        while not stop_requested.is_set():
                            if self.config.camera_id == 1 and self.frames_seen == 25:
                                return
                            self.frames_seen += 1
                            timestamp_ns = self.frames_seen * 66_666_667
                            yield FrameEnvelope(
                                sequence=self.frames_seen,
                                capture_mono_ns=timestamp_ns,
                                capture_utc_ns=timestamp_ns,
                                timezone="UTC",
                                generation=self.config.generation,
                                jpeg_bytes=b"synthetic-jpeg",
                            )
                            time.sleep(0.001)
                    finally:
                        self.closed.set()

                def request_stop(self):
                    return None

            workers = []
            timezone = SimpleNamespace(name=lambda: "UTC")
            for camera_id in (1, 2):
                worker = CameraCaptureWorker(
                    camera_id=camera_id,
                    generation=1,
                    settings=ApplicationSettings().camera(camera_id),
                    port=port,
                    recording_enabled=False,
                    clock=FakeClock(),
                    timezone=timezone,
                    catalog=SimpleNamespace(),
                    volume_store=None,
                    volume=None,
                    recordings_root=None,
                    volume_id=None,
                    engine_epoch="two-stream-test",
                    settings_revision=1,
                    live_directory=Path(temp_dir) / f"live-{camera_id}",
                    source_factory=SyntheticSource,
                )
                workers.append(worker)

            try:
                for worker in workers:
                    worker.start()
                for worker in workers:
                    self.assertTrue(worker.request_preview_demand())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with sources_lock:
                        source_one = sources.get(1)
                        source_two = sources.get(2)
                    if (
                        source_one is not None
                        and source_two is not None
                        and source_one.closed.is_set()
                        and source_two.frames_seen >= 40
                    ):
                        break
                    time.sleep(0.005)
                self.assertIsNotNone(source_one)
                self.assertIsNotNone(source_two)
                assert source_one is not None and source_two is not None
                self.assertTrue(source_one.closed.is_set())
                self.assertEqual(workers[0].snapshot["last_error"], "EOFError")
                self.assertTrue(workers[1].snapshot["capture_active"])
                previous_frames = source_two.frames_seen
                time.sleep(0.05)
                self.assertGreater(source_two.frames_seen, previous_frames)
                self.assertFalse(workers[1].snapshot["fatal_error"])
            finally:
                for worker in workers:
                    worker.stop(timeout_seconds=2)

    def test_capture_closes_frame_iterator_after_pipeline_failure(self):
        source_closed = threading.Event()
        frame = FrameEnvelope(
            sequence=1,
            capture_mono_ns=1,
            capture_utc_ns=1,
            timezone="UTC",
            generation=1,
            jpeg_bytes=b"jpeg",
        )

        class Source:
            stats = SimpleNamespace(dropped_frames=0, last_error=None)

            def frames(self, _stop_event):
                try:
                    yield frame
                finally:
                    source_closed.set()

        worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
        worker._source = Source()
        worker._stop = threading.Event()
        worker._lock = threading.RLock()
        worker._capture_open = False
        worker._capture_finished = threading.Event()
        worker._capture_ready = threading.Event()
        worker._capture_condition = threading.Condition(worker._lock)
        worker._audio_buffer = SimpleNamespace(take_before=lambda _mono_ns: ())
        worker._audio_source = None
        worker._frame_count = 0
        worker._last_frame_utc_ns = None
        worker._live = SimpleNamespace(submit=Mock())
        worker._preview = SimpleNamespace(submit=Mock())
        worker._recording_delay_for_live = lambda: None
        worker.recording_enabled = True
        worker._pipeline = SimpleNamespace(
            push=Mock(side_effect=FrameBufferFullError("bounded test")),
            finish_continuous=Mock(),
        )
        worker._continuous_session = None
        worker._recording_error = None

        worker._capture()

        self.assertTrue(source_closed.is_set())
        self.assertTrue(worker._stop.is_set())
        self.assertTrue(worker._capture_finished.is_set())
        self.assertEqual(worker._recording_error, "FrameBufferFullError")
        worker._pipeline.push.assert_called_once_with(frame)

    def test_stop_timeout_is_reported_and_retry_waits_for_old_generation(self):
        release = threading.Event()
        capture = threading.Thread(target=release.wait, name="blocked-capture")
        capture.start()
        pipeline = SimpleNamespace(clear=Mock())
        live = SimpleNamespace(close=Mock())
        worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
        worker.camera_id = 1
        worker._stop = threading.Event()
        worker._capture_wake = threading.Event()
        worker._thread = capture
        worker._audio_thread = None
        worker._gpio_thread = None
        worker._coordinator = None
        worker._event_worker_thread = None
        worker._event_jobs_ready = threading.Event()
        worker._writer_thread = None
        worker._pipeline = pipeline
        worker._live = live
        worker._lock = threading.RLock()
        worker._audio_lifecycle_lock = threading.Lock()
        worker._audio_stop_event = None
        worker._audio_buffer = SimpleNamespace(clear=Mock())
        worker._recording_error = None

        with self.assertRaises(CameraWorkerStopTimeout) as caught:
            worker.stop(timeout_seconds=0.02)

        self.assertEqual(caught.exception.workers, ("capture",))
        self.assertTrue(capture.is_alive())
        pipeline.clear.assert_not_called()

        release.set()
        capture.join(1)
        worker.stop(timeout_seconds=0.2)
        self.assertFalse(capture.is_alive())
        pipeline.clear.assert_called_once_with("worker_stopped")

    def test_all_worker_threads_share_one_stop_deadline(self):
        release = threading.Event()
        stop_event = threading.Event()
        threads = {
            name: threading.Thread(target=release.wait, name=f"blocked-{name}")
            for name in ("capture", "audio", "writer")
        }
        worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
        worker.camera_id = 1
        worker._stop = stop_event
        worker._capture_wake = threading.Event()
        worker._thread = threads["capture"]
        worker._audio_thread = threads["audio"]
        worker._coordinator = None
        worker._event_jobs_ready = threading.Event()
        worker._writer_thread = threads["writer"]
        worker._pipeline = SimpleNamespace(clear=Mock())
        worker._live = SimpleNamespace(close=Mock())
        worker._preview = SimpleNamespace(close=Mock())
        worker._lock = threading.RLock()
        worker._audio_lifecycle_lock = threading.Lock()
        worker._audio_stop_event = None
        worker._audio_buffer = SimpleNamespace(clear=Mock())
        worker._recording_error = None
        for thread in threads.values():
            thread.start()

        started = time.monotonic()
        with self.assertRaises(CameraWorkerStopTimeout) as caught:
            worker.stop(timeout_seconds=0.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(
            set(caught.exception.workers),
            {"capture", "audio", "writer"},
        )
        self.assertTrue(all(thread.is_alive() for thread in threads.values()))
        worker._pipeline.clear.assert_not_called()
        release.set()
        for thread in threads.values():
            thread.join(1)

    def test_stop_fails_if_source_child_process_is_still_active(self):
        worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
        worker.camera_id = 1
        worker._stop = threading.Event()
        worker._thread = None
        worker._audio_thread = None
        worker._gpio_thread = None
        worker._coordinator = None
        worker._event_worker_thread = None
        worker._event_jobs_ready = threading.Event()
        worker._writer_thread = None
        worker._source = SimpleNamespace(
            active_process_pid=1234,
            request_stop=Mock(),
            close=Mock(),
        )
        worker._audio_source = None
        worker._pipeline = SimpleNamespace(clear=Mock())
        worker._live = SimpleNamespace(close=Mock())
        worker._lock = threading.RLock()
        worker._audio_lifecycle_lock = threading.Lock()
        worker._audio_stop_event = None
        worker._audio_buffer = SimpleNamespace(clear=Mock())
        worker._recording_error = None

        with self.assertRaises(CameraWorkerStopTimeout) as caught:
            worker.stop(timeout_seconds=0.1)

        self.assertEqual(caught.exception.workers, ("capture_process",))
        worker._source.request_stop.assert_called_once_with()
        worker._source.close.assert_called_once()
        worker._pipeline.clear.assert_not_called()

    def test_fifty_quick_generations_leave_no_worker_threads(self):
        baseline = threading.active_count()
        for generation in range(1, 51):
            stop_event = threading.Event()
            capture = threading.Thread(
                target=stop_event.wait, name=f"recorder-capture-cam1-g{generation}"
            )
            worker = CameraCaptureWorker.__new__(CameraCaptureWorker)
            worker.camera_id = 1
            worker._stop = stop_event
            worker._thread = capture
            worker._audio_thread = None
            worker._gpio_thread = None
            worker._coordinator = None
            worker._event_worker_thread = None
            worker._event_jobs_ready = threading.Event()
            worker._writer_thread = None
            worker._pipeline = SimpleNamespace(clear=Mock())
            worker._live = SimpleNamespace(close=Mock())
            worker._lock = threading.RLock()
            worker._audio_lifecycle_lock = threading.Lock()
            worker._audio_stop_event = None
            worker._audio_buffer = SimpleNamespace(clear=Mock())
            worker._recording_error = None
            capture.start()
            worker.stop(timeout_seconds=0.2)
            self.assertFalse(capture.is_alive())

        alive_recorder_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("recorder-") and thread.is_alive()
        ]
        self.assertEqual(alive_recorder_threads, [])
        self.assertLessEqual(threading.active_count(), baseline)


class RuntimeSupervisorIsolationTests(unittest.TestCase):
    def test_capacity_and_status_continue_while_camera_reconcile_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            entered_reconcile = threading.Event()
            release_reconcile = threading.Event()
            capacity_called = threading.Event()
            calls_lock = threading.Lock()
            capacity_calls = 0

            def reconcile(camera_id):
                if camera_id == 1:
                    entered_reconcile.set()
                    release_reconcile.wait()

            def maintain_capacity():
                nonlocal capacity_calls
                with calls_lock:
                    capacity_calls += 1
                capacity_called.set()

            runtime._refresh_once = Mock()
            runtime._reconcile_camera = reconcile
            runtime._maintain_capacity = maintain_capacity
            runtime.export_jobs.expire = Mock()
            runtime._stop.clear()
            with patch(
                "raspi_recorder.engine.runtime._SUPERVISION_INTERVAL_SECONDS", 0.01
            ):
                supervisor = threading.Thread(target=runtime._supervise)
                capacity_supervisor = threading.Thread(
                    target=runtime._supervise_capacity
                )
                supervisor.start()
                self.assertTrue(entered_reconcile.wait(1))
                capacity_supervisor.start()
                capacity_called.clear()
                self.assertTrue(capacity_called.wait(1))
                self.assertTrue(runtime._configuration_lock.acquire(timeout=0.05))
                runtime._configuration_lock.release()
                with self.assertRaises(RuntimeTransitionBusy):
                    with runtime._configuration_transition(
                        operation="test_probe", wait_seconds=0.0
                    ):
                        self.fail("busy transition unexpectedly acquired a token")

                status_finished = threading.Event()
                status_thread = threading.Thread(
                    target=lambda: (runtime.status(), status_finished.set())
                )
                status_thread.start()
                self.assertTrue(status_finished.wait(0.5))
                with calls_lock:
                    self.assertGreaterEqual(capacity_calls, 1)

                runtime._stop.set()
                release_reconcile.set()
                supervisor.join(1)
                capacity_supervisor.join(1)
                status_thread.join(1)

            self.assertFalse(supervisor.is_alive())
            self.assertFalse(capacity_supervisor.is_alive())
            self.assertFalse(status_thread.is_alive())


class ReconcileGenerationTests(unittest.TestCase):
    def test_stop_failure_keeps_old_worker_and_does_not_advance_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            runtime._cameras[1].generation = 7

            class StuckWorker:
                recording_enabled = True
                settings = runtime.settings().camera(1)
                _volume_id = "old-volume"
                _thread = SimpleNamespace(is_alive=lambda: False)

                @property
                def snapshot(self):
                    return {"fatal_error": False, "last_error": None}

                def stop(self):
                    raise CameraWorkerStopTimeout(1, ("writer",))

            worker = StuckWorker()
            runtime._cameras[1].worker = worker

            runtime._reconcile_camera(1)

            self.assertIs(runtime._cameras[1].worker, worker)
            self.assertEqual(runtime._cameras[1].generation, 7)
            self.assertEqual(runtime._cameras[1].state, "error")
            self.assertIn("worker_stop_failed", runtime._cameras[1].blockers)

    def test_manual_camera_transition_propagates_worker_stop_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = RecorderRuntime(temp_dir, clock=FakeClock())
            runtime.settings_service.initialize()
            runtime._ports = ()

            class StuckWorker:
                recording_enabled = True
                settings = runtime.settings().camera(1)
                _volume_id = "old-volume"
                _thread = SimpleNamespace(is_alive=lambda: False)

                @property
                def snapshot(self):
                    return {"fatal_error": False, "last_error": None}

                def stop(self):
                    raise CameraWorkerStopTimeout(1, ("audio_process",))

            worker = StuckWorker()
            runtime._cameras[1].worker = worker
            runtime._cameras[1].desired_running = False

            with self.assertRaises(CameraWorkerStopTimeout):
                runtime._reconcile_camera(1, raise_on_stop_failure=True)

            self.assertIs(runtime._cameras[1].worker, worker)
            self.assertEqual(runtime._cameras[1].state, "error")


class EngineCameraStopResponseTests(unittest.TestCase):
    def test_camera_stop_failure_returns_conflict_instead_of_success(self):
        class Runtime:
            settings_service = SimpleNamespace(load_pending_operations=lambda: ())

            def set_camera_running(self, camera_id: int, running: bool) -> None:
                raise CameraWorkerStopTimeout(camera_id, ("capture_process",))

        response = EngineControlHandler(Runtime())._camera_command("1", "stop")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.body, {"error": {"code": "CameraWorkerStopTimeout"}})


class CapacityInitializationTests(unittest.TestCase):
    def test_unconfigured_cameras_wait_for_recordings_root_before_capacity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RecorderRuntime(directory, clock=FakeClock())
            runtime.settings_service.initialize()
            root = Path(directory) / "not-created" / "recordings"
            runtime._selected = (
                SimpleNamespace(
                    persistent_id="test-volume",
                    filesystem_bytes=16_000_000_000,
                ),
                root,
                "test-volume-id",
            )
            runtime._capacity_error = "ValueError"

            with patch.object(runtime._capacity_manager, "maintain") as maintain:
                runtime._maintain_capacity()

            self.assertIsNone(runtime._capacity_error)
            maintain.assert_not_called()
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()

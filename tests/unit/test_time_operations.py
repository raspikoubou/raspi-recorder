from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from raspi_recorder.domain.errors import ClockOperationError
from raspi_recorder.engine.runtime import (
    EngineControlHandler,
    RecorderRuntime,
    _local_datetime_to_utc_ns,
)
from raspi_recorder.ports.clock import ClockSetError
from tests.fakes import FakeClock, FakeClockSetter


class LocalWallTimeTests(unittest.TestCase):
    def test_tokyo_local_time_maps_to_the_expected_utc_nanoseconds(self):
        value = _local_datetime_to_utc_ns("2026-10-16T12:34:56", "Asia/Tokyo", 0)
        expected = (
            int(datetime(2026, 10, 16, 3, 34, 56, tzinfo=UTC).timestamp())
            * 1_000_000_000
        )
        self.assertEqual(value, expected)

    def test_dst_gap_is_rejected_and_ambiguous_fold_is_explicit(self):
        with self.assertRaises(ValueError):
            _local_datetime_to_utc_ns("2026-03-08T02:30:00", "America/New_York", 0)

        first = _local_datetime_to_utc_ns("2026-11-01T01:30:00", "America/New_York", 0)
        second = _local_datetime_to_utc_ns("2026-11-01T01:30:00", "America/New_York", 1)
        self.assertEqual(second - first, 3_600_000_000_000)

    def test_nonambiguous_time_rejects_fold_one(self):
        with self.assertRaises(ValueError):
            _local_datetime_to_utc_ns("2026-10-16T12:34:56", "Asia/Tokyo", 1)


class ManualTimeOperationTests(unittest.TestCase):
    def _runtime(self, temp_dir: str):
        clock = FakeClock(wall_utc_ns=1_800_000_000_000_000_000)
        setter = FakeClockSetter(clock)
        runtime = RecorderRuntime(temp_dir, clock=clock, clock_setter=setter)
        current = runtime.settings_service.initialize()
        runtime.timezone = SimpleNamespace(name=lambda: "Asia/Tokyo")
        runtime._refresh_once = Mock()
        runtime._reconcile_camera = Mock()
        return runtime, clock, setter, current

    def test_finalize_all_existing_workers_before_setting_and_restore_workers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, clock, setter, current = self._runtime(temp_dir)
            events: list[str] = []
            worker = Mock()
            worker.stop.side_effect = lambda: events.append("finalize")
            runtime._cameras[1].worker = worker

            def reconcile(camera_id: int) -> None:
                events.append(f"resume-{camera_id}")
                if camera_id == 1:
                    runtime._cameras[1].worker = object()

            runtime._reconcile_camera = Mock(side_effect=reconcile)
            original_set = setter.set_utc_ns

            def set_time(utc_ns: int, policy: str) -> None:
                events.append("set-clock")
                original_set(utc_ns, policy)

            setter.set_utc_ns = set_time
            target = 1_900_000_000_000_000_000

            runtime.apply_system_time(target, "Asia/Tokyo", current.revision, "keep")

            self.assertEqual(setter.calls, [(target, "keep")])
            self.assertEqual(clock.utc_ns(), target)
            self.assertEqual(events[0:2], ["finalize", "set-clock"])
            self.assertEqual(events[2:], ["resume-1", "resume-2"])
            worker.stop.assert_called_once()

    def test_failed_clock_set_restores_approximate_prior_time_and_workers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, clock, setter, current = self._runtime(temp_dir)
            prior = clock.utc_ns()
            setter.failure = ClockSetError("clock_write_failed", changed=True)
            runtime._cameras[1].desired_running = False

            with self.assertRaises(ClockOperationError) as raised:
                runtime.apply_system_time(
                    prior + 10_000_000_000, "Asia/Tokyo", current.revision, "keep"
                )

            self.assertEqual(raised.exception.code, "clock_write_failed")
            self.assertEqual(raised.exception.recovery["state"], "clock_restored")
            self.assertEqual(raised.exception.recovery["clock_changed"], False)
            self.assertEqual(len(setter.calls), 2)
            self.assertGreaterEqual(clock.utc_ns(), prior)

    def test_active_ntp_keep_is_rejected_before_finalizing_recording(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _clock, setter, current = self._runtime(temp_dir)
            setter.ntp_enabled = True
            worker = Mock()
            runtime._cameras[1].worker = worker

            with self.assertRaises(ClockOperationError) as raised:
                runtime.apply_system_time(
                    1_900_000_000_000_000_000,
                    "Asia/Tokyo",
                    current.revision,
                    "keep",
                )

            self.assertEqual(raised.exception.code, "ntp_active_time_change_rejected")
            self.assertEqual(raised.exception.recovery["state"], "recording_unchanged")
            worker.stop.assert_not_called()
            self.assertEqual(setter.calls, [])

    def test_unknown_ntp_state_is_rejected_before_finalizing_recording(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _clock, setter, current = self._runtime(temp_dir)
            setter.ntp_enabled = None
            worker = Mock()
            runtime._cameras[1].worker = worker

            with self.assertRaises(ClockOperationError) as raised:
                runtime.apply_system_time(
                    1_900_000_000_000_000_000,
                    "Asia/Tokyo",
                    current.revision,
                    "keep",
                )

            self.assertEqual(raised.exception.code, "ntp_state_unknown")
            self.assertEqual(raised.exception.recovery["state"], "recording_unchanged")
            self.assertTrue(raised.exception.recovery["ntp_state_uncertain"])
            worker.stop.assert_not_called()
            self.assertEqual(setter.calls, [])

    def test_unknown_ntp_state_blocks_pause_resume_before_finalizing_recording(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _clock, setter, current = self._runtime(temp_dir)
            setter.ntp_enabled = None
            worker = Mock()
            runtime._cameras[1].worker = worker

            with self.assertRaises(ClockOperationError) as raised:
                runtime.apply_system_time(
                    1_900_000_000_000_000_000,
                    "Asia/Tokyo",
                    current.revision,
                    "pause_and_resume",
                )

            self.assertEqual(raised.exception.code, "ntp_state_unknown")
            self.assertEqual(raised.exception.recovery["state"], "recording_unchanged")
            self.assertTrue(raised.exception.recovery["ntp_state_uncertain"])
            worker.stop.assert_not_called()
            self.assertEqual(setter.calls, [])

    def test_clock_rollback_uses_pause_resume_when_ntp_is_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, clock, setter, current = self._runtime(temp_dir)
            setter.ntp_enabled = True
            prior = clock.utc_ns()
            setter.failure = ClockSetError("clock_write_failed", changed=True)

            with self.assertRaises(ClockOperationError) as raised:
                runtime.apply_system_time(
                    prior + 10_000_000_000,
                    "Asia/Tokyo",
                    current.revision,
                    "pause_and_resume",
                )

            self.assertEqual(raised.exception.recovery["state"], "clock_restored")
            self.assertEqual(
                [policy for _, policy in setter.calls],
                [
                    "pause_and_resume",
                    "pause_and_resume",
                ],
            )
            self.assertGreaterEqual(clock.utc_ns(), prior)

    def test_time_api_queues_idempotent_operation_and_reports_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, clock, setter, current = self._runtime(temp_dir)
            handler = EngineControlHandler(runtime)
            body = {
                "expected_revision": current.revision,
                "request_id": "clock-operation-1",
                "datetime": "2026-10-16T12:34:56",
                "timezone": "Asia/Tokyo",
                "fold": 0,
                "ntp_policy": "keep",
                "confirmed": True,
            }
            with patch("raspi_recorder.engine.runtime.threading.Thread") as thread:
                accepted = handler.handle("POST", "/api/v1/time", body)
                runner = thread.call_args.kwargs["target"]
                runner(*thread.call_args.kwargs["args"])

            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(
                handler.handle("POST", "/api/v1/time", body).body,
                accepted.body,
            )
            operation = handler.handle(
                "GET", f"/api/v1/operations/{accepted.body['operation_id']}", None
            )
            self.assertEqual(operation.body["state"], "succeeded")
            self.assertEqual(
                operation.body["result"]["utc_ns"],
                _local_datetime_to_utc_ns("2026-10-16T12:34:56", "Asia/Tokyo", 0),
            )
            self.assertEqual(setter.calls[-1][1], "keep")
            self.assertEqual(clock.utc_ns(), setter.calls[-1][0])

    def test_time_api_rejects_unconfirmed_and_dst_gap_before_queueing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _clock, setter, current = self._runtime(temp_dir)
            runtime.timezone = SimpleNamespace(name=lambda: "America/New_York")
            setter.timezone = "America/New_York"
            handler = EngineControlHandler(runtime)
            body = {
                "expected_revision": current.revision,
                "request_id": "clock-operation-invalid",
                "datetime": "2026-03-08T02:30:00",
                "timezone": "America/New_York",
                "fold": 0,
                "ntp_policy": "keep",
                "confirmed": False,
            }

            response = handler.handle("POST", "/api/v1/time", body)

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.body["error"]["code"], "invalid_time_request")
            body["confirmed"] = True
            gap = handler.handle("POST", "/api/v1/time", body)
            self.assertEqual(gap.status_code, 400)
            self.assertEqual(gap.body["error"]["code"], "invalid_local_datetime")
            self.assertEqual(runtime.settings_service.load_pending_operations(), ())


if __name__ == "__main__":
    unittest.main()

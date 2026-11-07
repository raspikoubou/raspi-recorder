from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock

from raspi_recorder.adapters.clock_helper import TimedateHelper, handle_request
from raspi_recorder.ports.clock import ClockSetError


class TimedateHelperTests(unittest.TestCase):
    @staticmethod
    def _result(stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )

    @classmethod
    def _status(cls, ntp_enabled: bool) -> subprocess.CompletedProcess[str]:
        return cls._result(
            "Timezone=Asia/Tokyo\n"
            f"NTP={'yes' if ntp_enabled else 'no'}\n"
            "NTPSynchronized=no\n"
        )

    def test_status_reads_only_named_timedated_properties(self):
        runner = Mock(return_value=self._status(True))
        helper = TimedateHelper(runner=runner)

        status = helper.status()

        self.assertEqual(status.timezone, "Asia/Tokyo")
        self.assertIs(status.ntp_enabled, True)
        self.assertIs(status.synchronized, False)
        args = runner.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/timedatectl")
        self.assertEqual(args[1], "show")
        self.assertIn("--property=NTPSynchronized", args)
        self.assertEqual(runner.call_args.kwargs["timeout"], 8)

    def test_pause_set_and_resume_calls_fixed_systemd_methods(self):
        runner = Mock(
            side_effect=[
                self._status(True),
                self._result(),  # SetNTP(false)
                self._status(False),
                self._result(),  # SetTime
                self._result(),  # SetNTP(true)
                self._status(True),
            ]
        )
        helper = TimedateHelper(runner=runner, utc_now_ns=lambda: 1_000_000_000)
        target = 1_700_000_000_123_456_789

        helper.set_utc_ns(target, "pause_and_resume")

        calls = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(calls[1][-4:], ["SetNTP", "bb", "false", "false"])
        self.assertEqual(
            calls[2][0],
            "/usr/bin/timedatectl",
        )
        self.assertEqual(
            calls[3][-5:],
            ["SetTime", "xbb", "1700000000123456", "false", "false"],
        )
        self.assertEqual(calls[4][-4:], ["SetNTP", "bb", "true", "false"])
        self.assertEqual(calls[5][0], "/usr/bin/timedatectl")
        self.assertTrue(all(call.kwargs["check"] for call in runner.call_args_list))

    def test_keep_policy_does_not_change_ntp_setting_when_disabled(self):
        runner = Mock(
            side_effect=[
                self._result("Timezone=UTC\nNTP=no\nNTPSynchronized=yes\n"),
                self._result(),
            ]
        )
        TimedateHelper(runner=runner).set_utc_ns(1_700_000_000_000_000_000, "keep")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(
            runner.call_args.args[0][-5:],
            ["SetTime", "xbb", "1700000000000000", "false", "false"],
        )

    def test_keep_policy_rejects_active_ntp_before_calling_timedated(self):
        runner = Mock(return_value=self._status(True))
        with self.assertRaises(ClockSetError) as raised:
            TimedateHelper(runner=runner).set_utc_ns(1_700_000_000_000_000_000, "keep")
        self.assertEqual(raised.exception.code, "ntp_active_time_change_rejected")
        self.assertEqual(runner.call_count, 1)

    def test_unknown_ntp_state_rejects_before_calling_timedated(self):
        for policy in ("keep", "pause_and_resume"):
            with self.subTest(policy=policy):
                runner = Mock(
                    return_value=self._result(
                        "Timezone=UTC\nNTP=unknown\nNTPSynchronized=no\n"
                    )
                )
                with self.assertRaises(ClockSetError) as raised:
                    TimedateHelper(runner=runner).set_utc_ns(
                        1_700_000_000_000_000_000, policy
                    )
                self.assertEqual(raised.exception.code, "ntp_state_unknown")
                self.assertTrue(raised.exception.ntp_state_uncertain)
                self.assertEqual(runner.call_count, 1)

    def test_timedated_ntp_refusal_is_reported_without_clock_change(self):
        runner = Mock(
            side_effect=[
                self._result("Timezone=UTC\nNTP=no\nNTPSynchronized=no\n"),
                subprocess.CalledProcessError(
                    1,
                    ["busctl"],
                    stderr=(
                        "org.freedesktop.timedate1.AutomaticTimeSyncEnabled: "
                        "Automatic time synchronization is enabled"
                    ),
                ),
            ]
        )
        with self.assertRaises(ClockSetError) as raised:
            TimedateHelper(runner=runner).set_utc_ns(1_700_000_000_000_000_000, "keep")
        self.assertEqual(raised.exception.code, "ntp_active_time_change_rejected")
        self.assertFalse(raised.exception.changed)

    def test_ntp_state_is_polled_until_job_transition_completes(self):
        runner = Mock(
            side_effect=[
                self._status(True),
                self._result(),  # SetNTP(false)
                self._status(True),
                self._status(False),
                self._result(),  # SetTime
                self._result(),  # SetNTP(true)
                self._status(False),
                self._status(True),
            ]
        )
        helper = TimedateHelper(runner=runner, sleep=lambda _seconds: None)

        helper.set_utc_ns(1_700_000_000_000_000_000, "pause_and_resume")

        self.assertEqual(runner.call_count, 8)
        self.assertEqual(runner.call_args_list[2].args[0][0], "/usr/bin/timedatectl")
        self.assertEqual(runner.call_args_list[3].args[0][0], "/usr/bin/timedatectl")
        self.assertEqual(runner.call_args_list[6].args[0][0], "/usr/bin/timedatectl")
        self.assertEqual(runner.call_args_list[7].args[0][0], "/usr/bin/timedatectl")

    def test_failed_set_restores_ntp_after_explicit_pause(self):
        runner = Mock(
            side_effect=[
                self._result("Timezone=UTC\nNTP=yes\nNTPSynchronized=no\n"),
                self._result(),  # SetNTP(false)
                self._status(False),
                subprocess.CalledProcessError(1, ["busctl"]),
                self._status(False),
                self._result(),  # SetNTP(true)
                self._status(True),
                self._status(True),  # confirm restored state
            ]
        )

        with self.assertRaises(ClockSetError) as raised:
            TimedateHelper(runner=runner).set_utc_ns(10_000_000_000, "pause_and_resume")

        self.assertEqual(raised.exception.code, "timedate_service_failed")
        self.assertEqual(runner.call_count, 8)
        self.assertEqual(
            runner.call_args_list[5].args[0][-4:],
            ["SetNTP", "bb", "true", "false"],
        )

    def test_request_schema_does_not_accept_unknown_fields_or_boolean_time(self):
        helper = TimedateHelper()
        self.assertEqual(
            handle_request({"action": "status", "extra": True}, helper)["error"][
                "code"
            ],
            "invalid_request",
        )
        self.assertEqual(
            handle_request(
                {"action": "set", "utc_ns": True, "ntp_policy": "keep"}, helper
            )["error"]["code"],
            "invalid_request",
        )


if __name__ == "__main__":
    unittest.main()

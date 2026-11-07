import unittest

from raspi_recorder.domain.camera import (
    CameraBlocker,
    CameraRunState,
    CameraRuntime,
)


class CameraRuntimeTests(unittest.TestCase):
    def test_blockers_are_independent_and_only_explicitly_cleared(self):
        runtime = CameraRuntime(capture_active=True, physical_file_writing=True)
        runtime.add_blocker(CameraBlocker.LOW_SPACE)
        runtime.add_blocker(CameraBlocker.IO_ERROR)
        self.assertEqual(runtime.state, CameraRunState.BLOCKED)

        runtime.remove_blocker(CameraBlocker.LOW_SPACE)
        self.assertEqual(runtime.blockers, {CameraBlocker.IO_ERROR})
        self.assertEqual(runtime.state, CameraRunState.BLOCKED)

    def test_desired_stop_prevents_automatic_recovery_from_blocker(self):
        runtime = CameraRuntime(desired_running=False)
        runtime.add_blocker(CameraBlocker.CAMERA_MISSING)
        runtime.remove_blocker(CameraBlocker.CAMERA_MISSING)
        self.assertEqual(runtime.state, CameraRunState.STOPPED)

    def test_state_reports_finalizing_before_stopped_or_blocked(self):
        runtime = CameraRuntime(desired_running=False, finalizing=True)
        self.assertEqual(runtime.state, CameraRunState.FINALIZING)


if __name__ == "__main__":
    unittest.main()

import unittest

from raspi_recorder.domain.capacity import (
    BYTES_PER_GB,
    CapacityAction,
    CapacityThresholds,
    DeletionCandidate,
    RecordingFileState,
    VolumeKind,
    choose_capacity_action,
    oldest_deletion_candidate,
)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = CapacityThresholds(
            volume_kind=VolumeKind.MICROSD,
            volume_bytes=16 * BYTES_PER_GB,
        )

    def test_start_threshold_is_strict_and_recovery_is_inclusive(self):
        self.assertEqual(
            choose_capacity_action(5 * BYTES_PER_GB, self.thresholds, False),
            CapacityAction.CONTINUE,
        )
        self.assertEqual(
            choose_capacity_action(5 * BYTES_PER_GB - 1, self.thresholds, False),
            CapacityAction.DELETE_OLDEST,
        )
        self.assertEqual(
            choose_capacity_action(7 * BYTES_PER_GB, self.thresholds, True),
            CapacityAction.CONTINUE,
        )
        self.assertEqual(
            choose_capacity_action(7 * BYTES_PER_GB - 1, self.thresholds, True),
            CapacityAction.DELETE_OLDEST,
        )

    def test_media_specific_stop_threshold_is_inclusive(self):
        usb = CapacityThresholds(VolumeKind.USB, 16 * BYTES_PER_GB)
        self.assertEqual(
            choose_capacity_action(2 * BYTES_PER_GB, self.thresholds, True),
            CapacityAction.STOP_RECORDING,
        )
        self.assertEqual(
            choose_capacity_action(BYTES_PER_GB, usb, True),
            CapacityAction.STOP_RECORDING,
        )

    def test_invalid_threshold_relationships_and_small_capacity_are_rejected(self):
        with self.assertRaises(ValueError):
            CapacityThresholds(VolumeKind.MICROSD, 16 * BYTES_PER_GB, 20, 70)
        with self.assertRaises(ValueError):
            CapacityThresholds(VolumeKind.USB, 16 * BYTES_PER_GB, 70, 70)
        with self.assertRaises(ValueError):
            CapacityThresholds(VolumeKind.USB, 6 * BYTES_PER_GB, 50, 70)

    def test_free_bytes_cannot_exceed_volume_size(self):
        with self.assertRaises(ValueError):
            choose_capacity_action(17 * BYTES_PER_GB, self.thresholds, False)

    def test_deletion_picks_oldest_eligible_current_managed_file(self):
        candidates = [
            DeletionCandidate(
                "new", "vol-1", RecordingFileState.FINALIZED, True, 20, 2
            ),
            DeletionCandidate("old", "vol-1", RecordingFileState.ABNORMAL, True, 10, 1),
            DeletionCandidate(
                "writing", "vol-1", RecordingFileState.WRITING, True, 1, 0
            ),
            DeletionCandidate(
                "other-volume", "vol-2", RecordingFileState.FINALIZED, True, 0, 0
            ),
            DeletionCandidate(
                "external", "vol-1", RecordingFileState.FINALIZED, False, 0, 0
            ),
            DeletionCandidate(
                "leased", "vol-1", RecordingFileState.FINALIZED, True, 0, 0, 1
            ),
        ]
        selected = oldest_deletion_candidate(candidates, "vol-1")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.recording_id, "old")

    def test_no_eligible_file_returns_none(self):
        candidate = DeletionCandidate(
            "writing", "vol-1", RecordingFileState.WRITING, True, 1, 0
        )
        self.assertIsNone(oldest_deletion_candidate([candidate], "vol-1"))


if __name__ == "__main__":
    unittest.main()

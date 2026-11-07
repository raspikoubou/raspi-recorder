from __future__ import annotations

import unittest

from raspi_recorder.application.capacity_manager import (
    CapacityManager,
    CapacitySnapshot,
)
from raspi_recorder.domain.capacity import (
    CapacityAction,
    CapacityThresholds,
    VolumeKind,
)


class FakeIndex:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def oldest_deletion_candidate(self, volume_id: str, *, excluded_ids=()):
        del volume_id
        return next((item for item in self.ids if item not in excluded_ids), None)


class FakeAccess:
    def __init__(self, index: FakeIndex, reader: FakeReader, recovered_per_delete: int):
        self.index = index
        self.reader = reader
        self.recovered_per_delete = recovered_per_delete
        self.deleted: list[str] = []

    def delete_automatic(self, recording_id: str):
        from raspi_recorder.application.recording_access import DeleteItemResult

        self.index.ids.remove(recording_id)
        self.deleted.append(recording_id)
        self.reader.snapshot = CapacitySnapshot(
            volume_id=self.reader.snapshot.volume_id,
            persistent_id=self.reader.snapshot.persistent_id,
            total_bytes=self.reader.snapshot.total_bytes,
            available_bytes=(
                self.reader.snapshot.available_bytes + self.recovered_per_delete
            ),
        )
        return DeleteItemResult(recording_id, True, None)


class FakeReader:
    def __init__(self, snapshot: CapacitySnapshot) -> None:
        self.snapshot = snapshot

    def refresh(self, volume_id: str) -> CapacitySnapshot:
        del volume_id
        return self.snapshot


class FakeBlocker:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, str | None]] = []

    def set_low_space_blocked(self, blocked: bool, reason: str | None = None) -> None:
        self.calls.append((blocked, reason))


class CapacityManagerTests(unittest.TestCase):
    def make_manager(self, free_bytes: int, candidate_ids: list[str], recovered: int):
        snapshot = CapacitySnapshot("vol-1", "uuid-1", 15_000_000_000, free_bytes)
        index = FakeIndex(candidate_ids.copy())
        reader = FakeReader(snapshot)
        access = FakeAccess(index, reader, recovered)
        blocker = FakeBlocker()
        manager = CapacityManager(index, access, reader, blocker)
        thresholds = CapacityThresholds(VolumeKind.MICROSD, snapshot.total_bytes)
        return manager, access, blocker, thresholds

    def test_stop_threshold_blocks_then_deletes_until_recovery(self):
        manager, access, blocker, thresholds = self.make_manager(
            1_000_000_000, ["old-1", "old-2", "old-3"], 2_000_000_000
        )
        result = manager.maintain("vol-1", "uuid-1", thresholds)
        self.assertEqual(result.action, CapacityAction.CONTINUE)
        self.assertEqual(access.deleted, ["old-1", "old-2", "old-3"])
        self.assertEqual(result.available_bytes, 7_000_000_000)
        self.assertEqual(
            blocker.calls,
            [(True, "low_space_stop_threshold"), (False, None)],
        )

    def test_candidate_exhaustion_above_stop_does_not_unblock_a_stopped_recorder(self):
        manager, access, blocker, thresholds = self.make_manager(
            1_000_000_000, ["old"], 1_000_000_000
        )
        result = manager.maintain("vol-1", "uuid-1", thresholds)
        self.assertEqual(result.action, CapacityAction.STOP_RECORDING)
        self.assertEqual(result.warning, "deletion_candidates_exhausted")
        self.assertEqual(access.deleted, ["old"])
        self.assertEqual(blocker.calls, [(True, "low_space_stop_threshold")])

    def test_candidate_exhaustion_above_stop_warns_without_blocking(self):
        manager, access, blocker, thresholds = self.make_manager(
            4_000_000_000, [], 1_000_000_000
        )
        result = manager.maintain("vol-1", "uuid-1", thresholds)
        self.assertEqual(result.action, CapacityAction.DELETE_OLDEST)
        self.assertEqual(result.warning, "deletion_candidates_exhausted")
        self.assertEqual(access.deleted, [])
        self.assertEqual(blocker.calls, [])

    def test_identity_change_rejects_capacity_update(self):
        manager, _, _, thresholds = self.make_manager(8_000_000_000, [], 0)
        with self.assertRaisesRegex(RuntimeError, "identity_changed"):
            manager.maintain("vol-1", "different-uuid", thresholds)


if __name__ == "__main__":
    unittest.main()

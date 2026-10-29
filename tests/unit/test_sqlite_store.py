import tempfile
import unittest
from pathlib import Path

from raspi_recorder.adapters.sqlite_store import (
    OperationConflict,
    RequestIdConflict,
    RevisionConflict,
    SqliteStore,
)
from raspi_recorder.domain.operations import OperationStage


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.store = SqliteStore(Path(self.temp_directory.name) / "state.sqlite3")
        self.store.initialize(applied_utc_ns=1)

    def test_settings_use_revision_compare_and_swap(self):
        self.assertIsNone(self.store.load_settings())
        saved = self.store.save_settings(
            expected_revision=0,
            schema_version=1,
            payload={"mode": "continuous", "message": "常時録画"},
            updated_utc_ns=10,
        )
        self.assertEqual(saved.revision, 1)
        self.assertEqual(saved.payload["revision"], 1)
        self.assertEqual(saved.payload["schema_version"], 1)
        self.assertEqual(self.store.load_settings(), saved)

        with self.assertRaises(RevisionConflict) as context:
            self.store.save_settings(0, 1, {"mode": "event"}, 11)
        self.assertEqual(context.exception.actual_revision, 1)

    def test_settings_reject_nan_json_values(self):
        with self.assertRaises(ValueError):
            self.store.save_settings(0, 1, {"value": float("nan")}, 10)

    def test_same_request_id_and_payload_is_idempotent(self):
        first = self._create_operation()
        repeated = self.store.create_operation(
            operation_id="operation-retry",
            request_id="request-1",
            kind="mode-change",
            target={"mode": "event"},
            previous_settings={"mode": "settings_changed_after_original_request"},
            requested_settings={"mode": "event"},
            created_utc_ns=100,
        )
        self.assertEqual(repeated.operation_id, first.operation_id)
        self.assertEqual(repeated.stage, OperationStage.QUEUED)

    def test_reusing_request_id_for_different_payload_is_rejected(self):
        self._create_operation()
        with self.assertRaises(RequestIdConflict):
            self.store.create_operation(
                operation_id="operation-2",
                request_id="request-1",
                kind="mode-change",
                target={"mode": "continuous"},
                previous_settings={"mode": "continuous"},
                requested_settings={"mode": "event"},
                created_utc_ns=100,
            )

    def test_operation_stage_is_compare_and_swap_and_records_recovery(self):
        operation = self._create_operation()
        with self.assertRaises(OperationConflict):
            self.store.transition_operation(
                operation.operation_id,
                OperationStage.QUEUED,
                OperationStage.FINALIZING,
                110,
            )

        current = self.store.transition_operation(
            operation.operation_id,
            OperationStage.QUEUED,
            OperationStage.VALIDATING,
            110,
        )
        self.assertEqual(current.stage, OperationStage.VALIDATING)
        self.assertEqual(current.status, "running")
        current = self.store.transition_operation(
            operation.operation_id,
            OperationStage.VALIDATING,
            OperationStage.FINALIZING,
            120,
        )
        current = self.store.transition_operation(
            operation.operation_id,
            OperationStage.FINALIZING,
            OperationStage.FAILED,
            130,
            error={"code": "finalize_failed"},
            recovery={"old_mode_resumed": True},
        )
        self.assertEqual(current.stage, OperationStage.FAILED)
        self.assertEqual(current.status, "failed")
        self.assertEqual(current.error, {"code": "finalize_failed"})
        self.assertEqual(current.recovery, {"old_mode_resumed": True})
        self.assertEqual(self.store.load_operation(operation.operation_id), current)

    def test_validation_only_operation_can_succeed_without_applying_changes(self):
        operation = self._create_operation()
        operation = self.store.transition_operation(
            operation.operation_id,
            OperationStage.QUEUED,
            OperationStage.VALIDATING,
            110,
        )
        operation = self.store.transition_operation(
            operation.operation_id,
            OperationStage.VALIDATING,
            OperationStage.SUCCEEDED,
            120,
            recovery={"state": "check_complete"},
        )
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.stage, OperationStage.SUCCEEDED)
        self.assertEqual(operation.recovery, {"state": "check_complete"})

    def test_pending_operation_journal_fails_closed_above_limit(self):
        self.store._MAX_PENDING_OPERATIONS = 2
        for index in range(3):
            self.store.create_operation(
                operation_id=f"operation-{index}",
                request_id=f"request-{index}",
                kind="settings",
                target={"index": index},
                previous_settings={},
                requested_settings={"index": index},
                created_utc_ns=index,
            )

        with self.assertRaisesRegex(RuntimeError, "exceeds safety limit"):
            self.store.load_pending_operations()

    def _create_operation(self):
        return self.store.create_operation(
            operation_id="operation-1",
            request_id="request-1",
            kind="mode-change",
            target={"mode": "event"},
            previous_settings={"mode": "continuous"},
            requested_settings={"mode": "event"},
            created_utc_ns=100,
        )


if __name__ == "__main__":
    unittest.main()

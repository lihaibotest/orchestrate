from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from tests.p0_12.p0_e2e_acceptance import P0E2EAcceptance, P0E2EStatus  # noqa: E402


class P012E2ETests(unittest.TestCase):
    def test_happy_path_has_deterministic_evidence_and_sent_outbox(self) -> None:
        result = P0E2EAcceptance().run_happy_path()
        self.assertEqual(result.status, P0E2EStatus.PASS)
        self.assertEqual(result.execution_status, "SUCCEEDED")
        self.assertEqual(result.artifact_validation, "PASS")
        self.assertEqual(result.outbox_status, "SENT")
        self.assertEqual(result.tenant_id, "tenant-a")
        self.assertEqual(result.correlation_id, "corr-v3")
        self.assertTrue(result.workflow_family_id)
        self.assertEqual(result.workflow_version, 1)
        self.assertTrue(result.registry_snapshot_id)
        self.assertTrue(result.ir_hash)
        self.assertTrue(result.workflow_hash)
        self.assertTrue(result.execution_id)
        self.assertEqual(result.node_statuses, {"select-cases": "SUCCEEDED", "execute-cases": "SUCCEEDED"})
        self.assertEqual(result.attempt_numbers, {"select-cases": 1, "execute-cases": 1})
        self.assertEqual(len(result.artifact_refs), 2)
        self.assertEqual(len(result.runtime_event_ids), 7)
        self.assertEqual(len(result.cmm_event_ids), 5)
        self.assertEqual(result.cmm_delivery_count, 5)
        self.assertEqual(result.execution_history_terminal, "SUCCEEDED")
        self.assertEqual(
            result.evidence_steps,
            ("plan", "snapshot", "ir", "s0", "s1", "s2", "s3", "s4",
             "publish", "execute", "runtime_events", "succeeded", "artifact_ref", "cmm_outbox"),
        )

    def test_happy_path_identity_is_stable_across_replay(self) -> None:
        first = P0E2EAcceptance().run_happy_path().as_evidence()
        second = P0E2EAcceptance().run_happy_path().as_evidence()
        for key in (
            "workflow_family_id", "workflow_version", "registry_snapshot_id", "ir_hash",
            "workflow_hash", "execution_id", "artifact_refs", "runtime_event_ids", "cmm_event_ids",
        ):
            self.assertEqual(first[key], second[key], key)

    def test_agent_not_ready_is_rejected_before_publish(self) -> None:
        result = P0E2EAcceptance(agent_ready=False).run()
        self.assertEqual(result.status, P0E2EStatus.AGENT_NOT_READY)
        self.assertFalse(result.published)

    def test_n8n_import_failure_is_rejected_before_publish(self) -> None:
        result = P0E2EAcceptance(n8n_import_failure=True).run()
        self.assertEqual(result.status, P0E2EStatus.N8N_IMPORT_FAILED)
        self.assertFalse(result.published)

    def test_blueprint_and_execute_replay_have_one_business_identity(self) -> None:
        result = P0E2EAcceptance().run_blueprint_execute_replay()
        self.assertEqual(result.status, P0E2EStatus.PASS)
        self.assertTrue(result.plan_replayed)
        self.assertTrue(result.execution_replayed)
        self.assertEqual(result.workflow_versions, 1)
        self.assertEqual(result.engine_executions, 1)

    def test_runtime_duplicate_and_missing_event_have_evidence(self) -> None:
        result = P0E2EAcceptance().run_runtime_event_replay_and_missing()
        self.assertEqual(result.status, P0E2EStatus.EVENT_MISSING)
        self.assertEqual(result.duplicate_event_count, 1)
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(result.reconciliation_status, "EVENT_MISSING")
        self.assertTrue(result.raw_evidence)

    def test_agent_stall_has_retry_and_reconciliation_evidence(self) -> None:
        result = P0E2EAcceptance().run_agent_stalled()
        self.assertEqual(result.status, P0E2EStatus.STALLED)
        self.assertEqual(result.reconciliation_status, "STALLED")
        self.assertEqual(result.attempt_numbers, {"select-cases": 1})

    def test_artifact_hash_mismatch_is_not_trusted(self) -> None:
        result = P0E2EAcceptance().run_artifact_mismatch()
        self.assertEqual(result.status, P0E2EStatus.ARTIFACT_MISMATCH)
        self.assertEqual(result.reconciliation_status, "ARTIFACT_MISMATCH")
        self.assertFalse(result.execution_succeeded)

    def test_missing_event_reconciliation_keeps_explicit_evidence(self) -> None:
        result = P0E2EAcceptance(missing_runtime_event=True).run()
        self.assertEqual(result.status, P0E2EStatus.EVENT_MISSING)
        self.assertEqual(result.reconciliation_status, "EVENT_MISSING")
        self.assertTrue(result.raw_evidence)

    def test_cmm_disconnect_is_degraded_but_gate_is_not_skipped(self) -> None:
        result = P0E2EAcceptance(cmm_available=False).run()
        self.assertEqual(result.status, P0E2EStatus.CMM_DEGRADED)
        self.assertTrue(result.self_test_passed)
        self.assertEqual(result.outbox_status, "PENDING")

    def test_cmm_publisher_crash_recovers_without_duplicate_delivery(self) -> None:
        result = P0E2EAcceptance().run_cmm_crash_recovery()
        self.assertEqual(result.status, P0E2EStatus.PASS)
        self.assertEqual(result.outbox_status, "SENT")
        self.assertEqual(result.cmm_delivery_count, 1)

    def test_plan_replay_is_idempotent(self) -> None:
        result = P0E2EAcceptance().run_plan_replay()
        self.assertEqual(result.status, P0E2EStatus.PASS)
        self.assertTrue(result.plan_replayed)
        self.assertEqual(result.workflow_versions, 1)

    def test_execution_http_retry_reuses_execution_identity(self) -> None:
        result = P0E2EAcceptance(execution_response_lost=True).run_execution_retry()
        self.assertEqual(result.status, P0E2EStatus.PASS)
        self.assertTrue(result.execution_retried)
        self.assertEqual(result.engine_executions, 1)


if __name__ == "__main__":
    unittest.main()

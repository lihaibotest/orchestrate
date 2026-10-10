from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_planning_generation import FakePlanningBlueprintGenerationAdapter  # noqa: E402
from adapters.planning_blueprint_v3_adapter import PlanningBlueprintV3Adapter  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from domain.agent_preparation import AgentBinding, build_registry_snapshot  # noqa: E402
from tests.planning_blueprint_v3.test_inbound_adapter import payload  # noqa: E402
from tests.p0_06.test_workflow_compiler import REQUEST_SCHEMA, RESULT_SCHEMA  # noqa: E402


class GenerationPipelineTests(unittest.TestCase):
    def test_v3_to_internal_plan_to_ir_validation_seam(self) -> None:
        snapshot = build_registry_snapshot(
            "wf-tenant-a-plan-v3",
            (
                AgentBinding(
                    "task-a", "agent-a", "1.0.0", "https://agents.example.test/a",
                    "capability-agent-a", REQUEST_SCHEMA, RESULT_SCHEMA,
                    "credential-ref-a", {"mode": "request_response"},
                ),
            ),
            snapshot_id="snapshot-generation-v3",
            created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )
        snapshots = InMemoryRegistrySnapshotRepository()
        snapshots.save_registry_snapshot(snapshot)
        validator = ExecutionIRValidationService(snapshots)
        generation = FakePlanningBlueprintGenerationAdapter(
            lambda _plan, _tenant: snapshot,
            validator,
            workflow_version=1,
            workflow_family_id=lambda _plan, _tenant: "wf-tenant-a-plan-v3",
        )
        service = PlanningBlueprintIngestionService(
            repository=__import__(
                "adapters.fake_planning_blueprint",
                fromlist=["InMemoryPlanningBlueprintRepository"],
            ).InMemoryPlanningBlueprintRepository(),
            adapter=PlanningBlueprintV3Adapter(),
            generation=generation,
        )
        result = service.ingest(payload(), tenant_id="tenant-a")
        self.assertEqual(result.workflow_version, 1)
        self.assertEqual(result.workflow_family_id, "wf-tenant-a-plan-v3")
        self.assertEqual(result.workflow_reference["stage"], "IR_VALIDATED")
        self.assertEqual(result.workflow_reference["registry_snapshot_id"], snapshot.snapshot_id)


if __name__ == "__main__":
    unittest.main()

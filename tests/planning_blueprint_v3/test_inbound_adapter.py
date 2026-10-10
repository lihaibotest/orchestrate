from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.fake_planning_blueprint import InMemoryPlanningBlueprintRepository  # noqa: E402
from adapters.planning_blueprint_v3_adapter import PlanningBlueprintV3Adapter  # noqa: E402
from api.planning_blueprints import create_planning_blueprint_router  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from application.planning_blueprint_ir import build_execution_ir  # noqa: E402
from domain.agent_preparation import build_registry_snapshot  # noqa: E402
from domain.planning_blueprint import (  # noqa: E402
    BlueprintIdempotencyConflict,
    BlueprintSecretPresent,
    BlueprintSchemaInvalid,
    PlanDagInvalid,
)
from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from tests.p0_06.test_workflow_compiler import REQUEST_SCHEMA, RESULT_SCHEMA  # noqa: E402
from domain.agent_preparation import AgentBinding  # noqa: E402


def payload() -> dict[str, object]:
    return {
        "schema_version": "planning-blueprint.v3",
        "correlation_id": "corr-v3",
        "session_id": "session-v3",
        "plan_id": "plan-v3",
        "plan_version": 0,
        "workflow_version": 0,
        "idempotency_key": "plan-v3-key",
        "inputs": {"request": "generate"},
        "outputs": {"result": {"schema": RESULT_SCHEMA, "media_type": "application/json"}},
        "completion_criteria": {"status": "completed"},
        "retry_policy": {"max_retries": 2, "backoff_seconds": 1, "timeout_seconds": 30},
        "execution_policy": {"max_parallelism": 1},
        "constraints": {"artifact_mode": "WHOLE_ARTIFACT"},
        "tasks": [
            {
                "task_id": "task-a",
                "objective": "run agent",
                "capabilities": ["capability-agent-a"],
                "depends_on": [],
                "inputs": {"request": "blueprint.inputs.request"},
                "outputs": {"result": {"schema": RESULT_SCHEMA}},
                "completion_criteria": {"status": "completed"},
            }
        ],
    }


def upstream_payload() -> dict[str, object]:
    return {
        "schema_version": "planning-blueprint.v3",
        "contract_version": "orchestration.v1.1",
        "created_reason": "INITIAL",
        "session_id": "session-upstream",
        "intent_id": "intent-upstream",
        "intent_revision": 1,
        "plan_id": "plan-upstream",
        "plan_version": 1,
        "workflow_version": 0,
        "idempotency_key": "plan-upstream:v1",
        "goal": {"objective": "generate report", "criteria": ["TEST_REPORT"]},
        "retry_policy": {"max_attempts": 3, "backoff_seconds": 1, "timeout_seconds": 30},
        "execution_policy": {"allow_replan": True},
        "constraints": {"max_parallelism": 1},
        "timeout_policy": {"seconds": 300},
        "tasks": [
            {
                "task_id": "select-cases", "objective": "select cases",
                "capabilities": ["case-select"], "depends_on": [],
                "input_bindings": [], "completion_criteria": [],
                "iteration": {"enabled": False, "max_iterations": 0},
                "side_effect": "read_only",
                "outputs": [{"output_id": "selected-cases", "artifact_type": "FILE",
                             "media_type": "application/json", "required": True}],
            },
            {
                "task_id": "execute-cases", "objective": "execute cases",
                "capabilities": ["case-execute"], "depends_on": ["select-cases"],
                "input_bindings": [{"binding_mode": "WHOLE_ARTIFACT", "source": {
                    "source_type": "TASK_OUTPUT", "source_task_id": "select-cases",
                    "output_id": "selected-cases"}}],
                "completion_criteria": [], "iteration": {"enabled": False},
                "side_effect": "read_only",
                "outputs": [{"output_id": "test-cases", "artifact_type": "FILE",
                             "media_type": "application/json", "required": True}],
            },
        ],
    }


def snapshot():
    binding = AgentBinding(
        "requirement-a", "agent-a", "1.0.0", "https://agents.example.test/a",
        "capability-agent-a", REQUEST_SCHEMA, RESULT_SCHEMA, "credential-ref-a",
        {"mode": "request_response"},
    )
    return build_registry_snapshot(
        "wf-family-v3", (binding,), snapshot_id="snapshot-v3",
        created_at=__import__("datetime").datetime(2026, 9, 23, tzinfo=__import__("datetime").timezone.utc),
    )


class InboundAdapterTests(unittest.TestCase):
    def test_real_upstream_shape_maps_lists_and_request_correlation(self) -> None:
        plan = PlanningBlueprintV3Adapter().adapt(
            upstream_payload(), tenant_id="tenant-a", correlation_id="corr-upstream"
        )
        execute = next(task for task in plan.tasks if task.task_id == "execute-cases")
        self.assertEqual(plan.correlation_id, "corr-upstream")
        self.assertEqual(execute.completion_criteria, {"criteria": ()})
        self.assertIn("test-cases", execute.outputs)
        self.assertEqual(
            execute.inputs["artifact_binding_0"]["source_task_id"],
            "select-cases",
        )

    def test_real_upstream_binding_requires_existing_output(self) -> None:
        body = upstream_payload()
        body["tasks"][1]["input_bindings"][0]["source"]["output_id"] = "missing"
        with self.assertRaises(PlanDagInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")

    def test_valid_v3_maps_to_internal_model_and_preserves_identity(self) -> None:
        plan = PlanningBlueprintV3Adapter().adapt(payload(), tenant_id="tenant-a")
        self.assertEqual(plan.schema_version, "planning-blueprint.v3")
        self.assertEqual((plan.plan_id, plan.plan_version), ("plan-v3", 0))
        self.assertEqual(plan.requested_workflow_version, 0)
        self.assertEqual(plan.tasks[0].task_id, "task-a")

    def test_required_schema_and_unknown_critical_fields_are_structured(self) -> None:
        body = payload()
        body.pop("tasks")
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["unexpected_critical_field"] = True
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")

    def test_wrong_version_and_dag_errors_are_rejected(self) -> None:
        body = payload()
        body["schema_version"] = "planning-blueprint.v2"
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["tasks"] = [
            {**body["tasks"][0], "depends_on": ["missing-task"]},
        ]
        with self.assertRaises(PlanDagInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["tasks"] = [
            {**body["tasks"][0], "depends_on": ["task-a"]},
        ]
        with self.assertRaises(PlanDagInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")

    def test_secret_and_field_mapping_are_rejected(self) -> None:
        body = payload()
        body["inputs"] = {"password": "plaintext"}
        with self.assertRaises(BlueprintSecretPresent):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["constraints"] = {"mapping_mode": "FIELD_MAPPING"}
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["constraints"] = {"artifact_mode": "FIELD_MAPPING"}
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")

    def test_physical_agent_binding_is_rejected_by_closed_contract(self) -> None:
        body = payload()
        body["tasks"][0]["agent_id"] = "agent-a"
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV3Adapter().adapt(body, tenant_id="tenant-a")

    def test_idempotency_and_conflict(self) -> None:
        repository = InMemoryPlanningBlueprintRepository()
        service = PlanningBlueprintIngestionService(repository)
        first = service.ingest(payload(), tenant_id="tenant-a")
        replay = service.ingest(payload(), tenant_id="tenant-a")
        self.assertEqual(first.blueprint_id, replay.blueprint_id)
        self.assertTrue(replay.idempotent_replay)
        changed = payload()
        changed["inputs"] = {"request": "changed"}
        with self.assertRaises(BlueprintIdempotencyConflict):
            service.ingest(changed, tenant_id="tenant-a")

    def test_plan_version_is_not_workflow_version_and_ir_validates(self) -> None:
        plan = PlanningBlueprintV3Adapter().adapt(payload(), tenant_id="tenant-a")
        ir = build_execution_ir(
            plan, workflow_family_id="wf-family-v3", workflow_version=1, snapshot=snapshot()
        )
        self.assertEqual(plan.plan_version, 0)
        self.assertEqual(ir.workflow_version, 1)
        snapshots = InMemoryRegistrySnapshotRepository()
        snapshots.save_registry_snapshot(snapshot())
        result = ExecutionIRValidationService(snapshots).validate(ir)
        self.assertTrue(result.report.ok, result.report.issues)

    def test_api_returns_structured_error_and_success(self) -> None:
        from fastapi import FastAPI
        import httpx

        app = FastAPI()
        app.include_router(create_planning_blueprint_router(PlanningBlueprintIngestionService(InMemoryPlanningBlueprintRepository())))

        async def run() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                success = await client.post(
                    "/api/v1/planning-blueprints", json=payload(),
                    headers={"X-Tenant-Id": "tenant-a", "X-Correlation-Id": "corr-v3", "Idempotency-Key": "plan-v3-key"},
                )
                invalid = await client.post(
                    "/api/v1/planning-blueprints", json={"schema_version": "wrong"},
                    headers={"X-Tenant-Id": "tenant-a"},
                )
                return success, invalid

        success, invalid = asyncio.run(run())
        self.assertEqual(success.status_code, 202)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["code"], "BLUEPRINT_SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

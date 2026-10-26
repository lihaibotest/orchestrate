from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).parents[2]
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.planning_blueprint_v4_adapter import PlanningBlueprintV4Adapter  # noqa: E402
from adapters.fake_planning_blueprint import InMemoryPlanningBlueprintRepository  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from application.planning_blueprint_ir import build_execution_ir  # noqa: E402
from domain.agent_preparation import AgentBinding, build_registry_snapshot  # noqa: E402
from domain.planning_blueprint import BlueprintSchemaInvalid, PlanDagInvalid  # noqa: E402


def payload() -> dict[str, object]:
    return {
        "schema_version": "planning-blueprint.v4",
        "contract_version": "orchestration.v2.0",
        "session_id": "ses_0199a111-1111-7111-8111-111111111400",
        "intent_id": "int_0199a111-1111-7111-8111-111111111401",
        "intent_revision": 1,
        "project_id": "smart-test-platform",
        "plan_id": "pln_0199a111-1111-7111-8111-111111111402",
        "plan_version": 1,
        "created_reason": "INITIAL",
        "workflow_version": 0,
        "idempotency_key": "pln_0199a111-1111-7111-8111-111111111402:v1",
        "registration_snapshot": {
            "ref": "agent-registry-v12",
            "digest": "sha256:2f6d7c9d5f38c9b2f45f2be73380bd75a95143464a47e61f6a35ed637f8a45a1",
        },
        "timeout_policy": {"seconds": 3600},
        "retry_policy": {"max_attempts": 3, "backoff_seconds": 1},
        "goal": {"goal_id": "goal-1", "objective": "run smoke", "criteria": ["report"]},
        "tasks": [
            {
                "task_id": "case-select",
                "objective": "select cases",
                "capability_ref": "case-select-agent",
                "depends_on": [],
                "input_paths": {},
                "output_paths": {"selected": "/runtime/case-select/outputs/selected.json"},
                "log_path": "/runtime/case-select/logs",
                "artifact_path": "/runtime/case-select/artifacts",
                "completion_criteria": [],
                "iteration": {"enabled": False, "max_iterations": 0},
                "side_effect": "read_only",
            },
            {
                "task_id": "case-execute",
                "objective": "execute cases",
                "capability_ref": "case-execute-agent",
                "depends_on": ["case-select"],
                "input_paths": {"selected": "/runtime/case-execute/inputs/selected.json"},
                "output_paths": {"report": "/runtime/case-execute/outputs/report.json"},
                "log_path": "/runtime/case-execute/logs",
                "artifact_path": "/runtime/case-execute/artifacts",
                "completion_criteria": [],
                "iteration": {"enabled": False, "max_iterations": 0},
                "side_effect": "idempotent_write",
            },
        ],
        "constraints": {"max_parallelism": 1},
        "execution_policy": {"allow_replan": True, "allow_rebinding": False},
    }


class PlanningBlueprintV4AdapterTests(unittest.TestCase):
    def test_accepts_path_only_v4_and_preserves_identity(self) -> None:
        plan = PlanningBlueprintV4Adapter().adapt(payload(), tenant_id="tenant-a", traceparent="00-abc")
        self.assertEqual(plan.schema_version, "planning-blueprint.v4")
        self.assertEqual(plan.contract_version, "orchestration.v2.0")
        self.assertEqual(plan.project_id, "smart-test-platform")
        self.assertEqual(plan.correlation_id, "00-abc")
        execute = next(task for task in plan.tasks if task.task_id == "case-execute")
        self.assertEqual(execute.capability_ref, "case-execute-agent")
        self.assertEqual(execute.input_paths["selected"], "/runtime/case-execute/inputs/selected.json")

    def test_fixed_idempotency_key_and_positive_versions(self) -> None:
        body = payload()
        body["idempotency_key"] = "wrong"
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["plan_version"] = 0
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")

    def test_rejects_v11_and_runtime_or_schema_leakage(self) -> None:
        for field in ("correlation_id", "run_id", "evaluation_id", "schema_ref", "outputs", "input_bindings"):
            body = payload()
            body["tasks"][0][field] = {}  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(BlueprintSchemaInvalid):
                PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")

    def test_rejects_nested_endpoint_and_secret(self) -> None:
        body = payload()
        body["goal"] = {"description": {"endpoint": "http://agent"}}
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")

    def test_validates_snapshot_digest_and_paths(self) -> None:
        body = payload()
        body["registration_snapshot"]["digest"] = "sha256:bad"  # type: ignore[index]
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["tasks"][0]["log_path"] = ""  # type: ignore[index]
        with self.assertRaises(BlueprintSchemaInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")

    def test_dag_and_replan_lineage(self) -> None:
        body = payload()
        body["tasks"][1]["depends_on"] = ["missing"]  # type: ignore[index]
        with self.assertRaises(PlanDagInvalid):
            PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")
        body = payload()
        body["created_reason"] = "REPLAN"
        body["base_plan_version"] = 1
        body["plan_version"] = 2
        body["idempotency_key"] = body["plan_id"] + ":v2"
        body["replan_context"] = {
            "trigger_run_id": "run-1",
            "replan_trigger": "EXECUTION_FAILURE",
            "replan_reason": "timeout",
        }
        plan = PlanningBlueprintV4Adapter().adapt(body, tenant_id="tenant-a")
        self.assertEqual(plan.base_plan_version, 1)

    def test_ingestion_replay_uses_v4_immutable_identity(self) -> None:
        repository = InMemoryPlanningBlueprintRepository()
        service = PlanningBlueprintIngestionService(repository)
        first = service.ingest(payload(), tenant_id="tenant-a", traceparent="00-abc")
        replay = service.ingest(payload(), tenant_id="tenant-a", traceparent="00-abc")
        self.assertEqual(first.blueprint_id, replay.blueprint_id)
        self.assertTrue(replay.idempotent_replay)
        stored = repository.records[first.blueprint_id][1]
        self.assertEqual(stored.idempotency_key, payload()["idempotency_key"])

    def test_api_dispatches_v4_and_does_not_require_correlation_body(self) -> None:
        import asyncio
        import httpx
        from fastapi import FastAPI
        from api.planning_blueprints import create_planning_blueprint_router

        app = FastAPI()
        app.include_router(create_planning_blueprint_router(PlanningBlueprintIngestionService(InMemoryPlanningBlueprintRepository())))

        async def invoke() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v1/planning-blueprints",
                    json=payload(),
                    headers={
                        "X-Tenant-Id": "tenant-a",
                        "traceparent": "00-abc",
                        "Idempotency-Key": payload()["idempotency_key"],
                    },
                )

        response = asyncio.run(invoke())
        self.assertEqual(response.status_code, 202, response.text)

    def test_execution_submit_wrapper_accepts_exec_plan_and_rejects_evaluation(self) -> None:
        import asyncio
        import httpx
        from fastapi import FastAPI
        from api.planning_blueprints import create_execution_submit_router

        app = FastAPI()
        app.include_router(create_execution_submit_router(PlanningBlueprintIngestionService(InMemoryPlanningBlueprintRepository())))

        async def invoke(body: dict[str, object]) -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/execution/v1/runs",
                    json=body,
                    headers={
                        "X-Tenant-Id": "tenant-a",
                        "traceparent": "00-abc",
                        "Idempotency-Key": payload()["idempotency_key"],
                    },
                )

        accepted = asyncio.run(invoke({"exec_plan": payload()}))
        self.assertEqual(accepted.status_code, 202, accepted.text)
        rejected = asyncio.run(invoke({"exec_plan": payload(), "launch_context": {"evaluation_id": "eval-1"}}))
        self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_task_orchestration_v2_path_accepts_v4_plan(self) -> None:
        import asyncio
        import httpx
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import FastAPI
        from api.v2 import create_v2_router
        from security.hmac_auth import AuthenticatedRequest

        service = PlanningBlueprintIngestionService(
            InMemoryPlanningBlueprintRepository()
        )
        repository = SimpleNamespace(database_path=Path(tempfile.mktemp()))

        async def fake_auth(request: object, **_kwargs: object) -> AuthenticatedRequest:
            body = await request.body()  # type: ignore[attr-defined]
            return AuthenticatedRequest("planning-agent", "nonce-v4", "corr-v4", 0, body)

        app = FastAPI()
        app.include_router(
            create_v2_router(
                repository,
                settings=SimpleNamespace(),
                gateway_ready=False,
                planning_blueprint_service=service,
            )
        )

        async def invoke() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v2/blueprints",
                    json=payload(),
                    headers={"X-Correlation-ID": "corr-v4", "X-Tenant-Id": "tenant-a"},
                )

        with patch("api.v2.authenticate", new=fake_auth):
            response = asyncio.run(invoke())
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["plan_id"], payload()["plan_id"])

    def test_path_only_plan_can_enter_existing_ir_boundary(self) -> None:
        plan = PlanningBlueprintV4Adapter().adapt(payload(), tenant_id="tenant-a")
        bindings = tuple(
            AgentBinding(
                task.task_id, "agent-" + task.capability_ref, "1.0.0",
                "https://agents.example.test/" + task.task_id, task.capability_ref,
                {"type": "object"}, {"type": "object"}, "credential-ref",
                {"mode": "request_response"},
            )
            for task in plan.tasks
        )
        snapshot = build_registry_snapshot(
            "wf-v4", bindings, snapshot_id="snapshot-v4",
            created_at=__import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").timezone.utc),
        )
        ir = build_execution_ir(plan, workflow_family_id="wf-v4", workflow_version=1, snapshot=snapshot)
        self.assertEqual(ir.registry_snapshot_id, "snapshot-v4")
        select = next(node for node in ir.nodes if node.node_id == "case-select")
        self.assertEqual(select.agent_binding.capability, "case-select-agent")


if __name__ == "__main__":
    unittest.main()

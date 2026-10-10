from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
from pathlib import Path
import sys
import unittest
from fastapi import FastAPI

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_runtime_events import InMemoryRuntimeEventRepository  # noqa: E402
from adapters.runtime_event_sources import (  # noqa: E402
    AgentRuntimeAdapter,
    ArtifactRuntimeAdapter,
    N8nRuntimeAdapter,
)
from application.runtime_event_service import (  # noqa: E402
    ReconciliationService,
    RuntimeEventService,
)
from application.state_transition_service import StateTransitionService  # noqa: E402
from api.runtime_events import create_runtime_event_router  # noqa: E402
from domain.artifact import ArtifactPutRequest  # noqa: E402
from domain.runtime_events import RuntimeEvent, hash_payload  # noqa: E402
from tests.p0_08.test_execution_controller import Fixture as ExecutionFixture  # noqa: E402
from tests.p0_09.test_whole_artifact_handoff import ArtifactDagFixture  # noqa: E402


class RuntimeFixture:
    def __init__(self) -> None:
        self.fixture = ExecutionFixture()
        self.view = self.fixture.controller.start(self.fixture.command())
        self.execution_id = self.view.execution.execution_id
        self.fixture.controller.mark_node_ready(self.execution_id, "agent-a")
        self.fixture.controller.start_next_attempt(self.execution_id, "agent-a")
        self.repo = InMemoryRuntimeEventRepository()
        self.service = RuntimeEventService(
            self.repo,
            ReconciliationService(
                self.repo,
                self.fixture.repository,
                StateTransitionService(self.fixture.repository),
            ),
        )
        self.now = datetime.now(timezone.utc)

    def event(
        self,
        adapter: object,
        event_type: str,
        event_id: str,
        *,
        occurred_at: datetime | None = None,
        **values: object,
    ) -> RuntimeEvent:
        return adapter.to_event(  # type: ignore[attr-defined]
            {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": occurred_at or self.now,
                "payload": values,
                **values,
            },
            tenant_id="tenant-1",
            correlation_id=self.view.execution.correlation_id or "correlation-1",
            execution_id=self.execution_id,
            workflow_version=1,
        )


class RuntimeEventReconciliationTests(unittest.TestCase):
    def test_planning_blueprint_to_runtime_reconciliation_chain(self) -> None:
        fixture = ArtifactDagFixture()
        attempt = fixture.start_a()
        repo = InMemoryRuntimeEventRepository()
        service = RuntimeEventService(
            repo,
            ReconciliationService(
                repo,
                fixture.composition.repository,
                StateTransitionService(fixture.composition.repository),
                fixture.manager,
            ),
        )
        now = datetime.now(timezone.utc)
        correlation = fixture.view.execution.correlation_id or "session-upstream"
        common = {
            "tenant_id": "tenant-a",
            "correlation_id": correlation,
            "execution_id": fixture.execution_id,
            "workflow_version": 1,
        }
        for adapter, event_type, event_id in (
            (AgentRuntimeAdapter(), "agent.completed", "bp-agent-completed"),
            (N8nRuntimeAdapter(), "node.completed", "bp-engine-completed"),
        ):
            service.ingest(
                adapter.to_event(
                    {"event_id": event_id, "event_type": event_type, "node_id": "select-cases", "attempt_no": attempt.attempt_no, "occurred_at": now, "payload": {}},
                    **common,
                )
            )
        definition = fixture.composition.repository.get_runtime_definition(
            fixture.view.execution.workflow_family_id,
            fixture.view.execution.workflow_version,
            fixture.view.execution.ir_hash,
        )
        expected = next(
            item for item in definition.nodes if item.node_id == "select-cases"
        ).expected_outputs[0]
        artifact = fixture.manager.put_artifact(
            ArtifactPutRequest(
                "tenant-a", fixture.view.execution.workflow_family_id, 1,
                fixture.execution_id, "PRODUCTION", "select-cases", attempt.attempt_no,
                fixture.output(), expected,
            )
        )
        fixture.manager.validate_artifact("tenant-a", artifact.artifact_id, expected)
        result = service.ingest(
            ArtifactRuntimeAdapter().from_artifact(
                artifact, "artifact.validated", correlation_id=correlation
            )
        )
        self.assertIn("NODE:select-cases:SUCCEEDED", result.applied_transitions)
        self.assertEqual(
            fixture.composition.repository.nodes[(fixture.execution_id, "select-cases")].status,
            "SUCCEEDED",
        )

    def test_receive_and_query_api_are_observation_only_and_tenant_scoped(self) -> None:
        import httpx

        fixture = RuntimeFixture()
        app = FastAPI()
        app.include_router(create_runtime_event_router(fixture.service))
        payload = {
            "event_id": "api-event",
            "source": "AGENT_RUNTIME",
            "event_type": "agent.started",
            "execution_id": fixture.execution_id,
            "workflow_version": 1,
            "node_id": "agent-a",
            "attempt_no": 1,
            "occurred_at": fixture.now.isoformat(),
            "payload": {"token": "should-not-persist"},
        }

        async def request() -> tuple[int, dict[str, object], dict[str, object]]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/runtime-events",
                    json=payload,
                    headers={"X-Tenant-Id": "tenant-1", "X-Correlation-Id": fixture.view.execution.correlation_id or "correlation-1"},
                )
                query = await client.get(
                    f"/api/v1/executions/{fixture.execution_id}/runtime-events",
                    headers={"X-Tenant-Id": "tenant-1"},
                )
            return response.status_code, response.json(), query.json()

        code, accepted, queried = asyncio.run(request())
        self.assertEqual(code, 202)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(queried["observations"]), 0)
        self.assertEqual(fixture.fixture.repository.nodes[(fixture.execution_id, "agent-a")].status, "RUNNING")

    def test_proof_a_duplicate_event_creates_one_observation(self) -> None:
        fixture = RuntimeFixture()
        event = fixture.event(
            AgentRuntimeAdapter(),
            "agent.started",
            "duplicate-event",
            node_id="agent-a",
            attempt_no=1,
        )
        first = fixture.service.ingest(event)
        second = fixture.service.ingest(event)
        self.assertFalse(first.ignored)
        self.assertTrue(second.ignored)
        self.assertEqual(len(fixture.repo.events), 1)
        self.assertEqual(len(fixture.repo.observations), 1)

    def test_proof_b_out_of_order_success_then_running_does_not_regress(self) -> None:
        fixture = RuntimeFixture()
        agent_done = fixture.event(
            AgentRuntimeAdapter(),
            "agent.completed",
            "agent-success",
            node_id="agent-a",
            attempt_no=1,
        )
        engine_done = fixture.event(
            N8nRuntimeAdapter(),
            "node.completed",
            "engine-success",
            node_id="agent-a",
            attempt_no=1,
        )
        fixture.service.ingest(agent_done)
        fixture.service.ingest(engine_done)
        self.assertEqual(
            fixture.fixture.repository.nodes[(fixture.execution_id, "agent-a")].status,
            "SUCCEEDED",
        )
        late_running = fixture.event(
            N8nRuntimeAdapter(),
            "node.started",
            "late-running",
            occurred_at=fixture.now - timedelta(seconds=10),
            node_id="agent-a",
            attempt_no=1,
        )
        fixture.service.ingest(late_running)
        self.assertEqual(
            fixture.fixture.repository.nodes[(fixture.execution_id, "agent-a")].status,
            "SUCCEEDED",
        )

    def test_proof_c_multi_source_artifact_invalid_wins(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        repo = InMemoryRuntimeEventRepository()
        service = RuntimeEventService(
            repo,
            ReconciliationService(
                repo,
                fixture.composition.repository,
                StateTransitionService(fixture.composition.repository),
                fixture.manager,
            ),
        )
        now = datetime.now(timezone.utc)
        common = {
            "tenant_id": "tenant-a",
            "correlation_id": fixture.view.execution.correlation_id or "session-upstream",
            "execution_id": fixture.execution_id,
            "workflow_version": 1,
        }
        agent = AgentRuntimeAdapter().to_event(
            {"event_id": "agent-ok", "event_type": "agent.completed", "node_id": "select-cases", "attempt_no": 1, "occurred_at": now, "payload": {}}, **common
        )
        engine = N8nRuntimeAdapter().to_event(
            {"event_id": "n8n-ok", "event_type": "node.completed", "node_id": "select-cases", "attempt_no": 1, "occurred_at": now, "payload": {}}, **common
        )
        invalid = ArtifactRuntimeAdapter().to_event(
            {"event_id": "artifact-bad", "event_type": "artifact.invalid", "node_id": "select-cases", "attempt_no": 1, "external_reference": "missing-artifact", "occurred_at": now, "payload": {"reason": "missing"}}, **common
        )
        service.ingest(agent)
        service.ingest(engine)
        result = service.ingest(invalid)
        self.assertIn("NODE:select-cases:FAILED", result.applied_transitions)
        self.assertEqual(
            fixture.composition.repository.nodes[(fixture.execution_id, "select-cases")].status,
            "FAILED",
        )
        self.assertNotEqual(
            fixture.composition.repository.executions[fixture.execution_id].status,
            "SUCCEEDED",
        )

    def test_agent_success_requires_artifact_validation(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        repo = InMemoryRuntimeEventRepository()
        service = RuntimeEventService(
            repo,
            ReconciliationService(
                repo,
                fixture.composition.repository,
                StateTransitionService(fixture.composition.repository),
                fixture.manager,
            ),
        )
        for adapter, typ, eid in (
            (AgentRuntimeAdapter(), "agent.completed", "agent-ok"),
            (N8nRuntimeAdapter(), "node.completed", "engine-ok"),
        ):
            service.ingest(
                adapter.to_event(
                    {"event_id": eid, "event_type": typ, "node_id": "select-cases", "attempt_no": 1, "occurred_at": datetime.now(timezone.utc), "payload": {}},
                    tenant_id="tenant-a", correlation_id=fixture.view.execution.correlation_id or "session-upstream", execution_id=fixture.execution_id, workflow_version=1,
                )
            )
        self.assertEqual(
            fixture.composition.repository.nodes[(fixture.execution_id, "select-cases")].status,
            "RUNNING",
        )

    def test_event_processing_retry_is_not_business_attempt_retry(self) -> None:
        fixture = RuntimeFixture()
        bad = RuntimeEvent(
            "bad-event",
            fixture.execution_id,
            "unknown.event",
            1,
            fixture.now,
            node_id="agent-a",
            attempt_no=1,
            payload={},
            source="AGENT_RUNTIME",
            tenant_id="tenant-1",
            correlation_id=fixture.view.execution.correlation_id or "correlation-1",
            received_at=fixture.now,
            payload_hash=hash_payload({}),
        )
        with self.assertRaises(ValueError):
            fixture.service.ingest(bad)
        with self.assertRaises(ValueError):
            fixture.service.retry_failed("tenant-1", "bad-event")
        stored = fixture.repo.get_event("tenant-1", "bad-event")
        self.assertEqual(stored.processing_retry_count, 2)
        self.assertEqual(len(fixture.fixture.repository.list_attempts(fixture.execution_id, "agent-a")), 1)

    def test_tenant_isolation_and_correlation_lineage(self) -> None:
        fixture = RuntimeFixture()
        event = fixture.event(AgentRuntimeAdapter(), "agent.started", "tenant-event", node_id="agent-a", attempt_no=1)
        fixture.service.receive(event)
        with self.assertRaises(KeyError):
            fixture.repo.get_event("tenant-2", event.event_id)
        with self.assertRaises(ValueError):
            fixture.service.receive(
                AgentRuntimeAdapter().to_event(
                    {"event_id": "wrong-correlation", "event_type": "agent.started", "node_id": "agent-a", "attempt_no": 1, "occurred_at": fixture.now, "payload": {}},
                    tenant_id="tenant-1", correlation_id="wrong", execution_id=fixture.execution_id, workflow_version=1,
                )
            )


if __name__ == "__main__":
    unittest.main()

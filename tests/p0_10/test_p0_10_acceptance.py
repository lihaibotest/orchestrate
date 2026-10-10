from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_runtime_events import (  # noqa: E402
    FakeAgentFactsPort,
    FakeArtifactFactsPort,
    FakeN8nFactsPort,
    InMemoryRuntimeEventRepository,
)
from adapters.runtime_event_sources import AgentRuntimeAdapter  # noqa: E402
from application.runtime_event_service import (  # noqa: E402
    ReconciliationService,
    RuntimeEventService,
)
from application.state_transition_service import StateTransitionService  # noqa: E402
from api.runtime_events import create_runtime_event_router  # noqa: E402
from domain.runtime_events import ReconciliationStatus, RuntimeFact  # noqa: E402
from tests.p0_08.test_execution_controller import Fixture as ExecutionFixture  # noqa: E402


class AcceptanceFixture:
    def __init__(self) -> None:
        self.execution = ExecutionFixture()
        view = self.execution.controller.start(self.execution.command())
        self.execution_id = view.execution.execution_id
        self.execution.controller.mark_node_ready(self.execution_id, "agent-a")
        self.execution.controller.start_next_attempt(self.execution_id, "agent-a")
        self.repo = InMemoryRuntimeEventRepository()
        self.n8n = FakeN8nFactsPort()
        self.agent = FakeAgentFactsPort()
        self.artifact = FakeArtifactFactsPort()
        self.service = RuntimeEventService(
            self.repo,
            ReconciliationService(
                self.repo,
                self.execution.repository,
                StateTransitionService(self.execution.repository),
                facts_ports=(self.n8n, self.agent, self.artifact),
            ),
        )
        self.correlation_id = view.execution.correlation_id or "corr-acceptance"
        self.now = datetime.now(timezone.utc)

    def fact(self, source: str, state: str, evidence: dict[str, object] | None = None) -> RuntimeFact:
        return RuntimeFact(
            source=source,
            execution_id=self.execution_id,
            node_id="agent-a",
            attempt_no=1,
            state=state,
            occurred_at=self.now,
            evidence=evidence or {"source_fact": state},
        )

    def matched(self) -> None:
        self.n8n.facts = [self.fact("WORKFLOW_ENGINE", "SUCCEEDED")]
        self.agent.facts = [self.fact("AGENT_RUNTIME", "SUCCEEDED")]
        self.artifact.facts = [self.fact("ARTIFACT_STORE", "VALIDATED", {"content_hash": "hash-1"})]


class P010AcceptanceTests(unittest.TestCase):
    def test_ingest_normalizes_deduplicates_and_rejects_evaluator_fields(self) -> None:
        fixture = AcceptanceFixture()
        app = FastAPI()
        app.include_router(create_runtime_event_router(fixture.service))

        async def run() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"X-Tenant-Id": "tenant-1", "X-Correlation-Id": fixture.correlation_id}
                payload = {
                    "event_id": "ingest-1",
                    "source": "AGENT_RUNTIME",
                    "event_type": "success",
                    "execution_id": fixture.execution_id,
                    "workflow_version": 1,
                    "node_id": "agent-a",
                    "attempt_no": 1,
                    "occurred_at": fixture.now.isoformat(),
                    "payload": {"token": "must-redact"},
                }
                first = await client.post("/api/v1/runtime-events", json=payload, headers=headers)
                duplicate = await client.post("/api/v1/runtime-events", json=payload, headers=headers)
                rejected = await client.post(
                    "/api/v1/runtime-events",
                    json={**payload, "event_id": "evaluator-1", "score": 0.9},
                    headers=headers,
                )
                return first, duplicate, rejected

        first, duplicate, rejected = asyncio.run(run())
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["event"]["event_type"], "agent.completed")
        self.assertEqual(duplicate.status_code, 200)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(len(fixture.repo.events), 1)

    def test_reconcile_matched_preserves_three_source_evidence(self) -> None:
        fixture = AcceptanceFixture()
        fixture.matched()
        result = fixture.service.reconcile(fixture.execution_id, tenant_id="tenant-1")
        self.assertEqual(result.status, ReconciliationStatus.MATCHED)
        self.assertEqual({item["source"] for item in result.evidence}, {"WORKFLOW_ENGINE", "AGENT_RUNTIME", "ARTIFACT_STORE"})
        self.assertEqual(len(result.raw_facts), 3)

    def test_fault_injection_event_missing(self) -> None:
        fixture = AcceptanceFixture()
        fixture.agent.facts = [fixture.fact("AGENT_RUNTIME", "SUCCEEDED")]
        fixture.artifact.facts = [fixture.fact("ARTIFACT_STORE", "VALIDATED")]
        result = fixture.service.reconcile(fixture.execution_id, tenant_id="tenant-1")
        self.assertEqual(result.status, ReconciliationStatus.EVENT_MISSING)

    def test_fault_injection_stalled(self) -> None:
        fixture = AcceptanceFixture()
        fixture.n8n.facts = [fixture.fact("WORKFLOW_ENGINE", "RUNNING")]
        fixture.agent.facts = [fixture.fact("AGENT_RUNTIME", "RUNNING")]
        fixture.artifact.facts = []
        result = fixture.service.reconcile(fixture.execution_id, tenant_id="tenant-1")
        self.assertEqual(result.status, ReconciliationStatus.STALLED)

    def test_fault_injection_artifact_mismatch(self) -> None:
        fixture = AcceptanceFixture()
        fixture.n8n.facts = [fixture.fact("WORKFLOW_ENGINE", "SUCCEEDED")]
        fixture.agent.facts = [fixture.fact("AGENT_RUNTIME", "SUCCEEDED")]
        fixture.artifact.facts = [fixture.fact("ARTIFACT_STORE", "INVALID", {"reason": "hash mismatch"})]
        result = fixture.service.reconcile(fixture.execution_id, tenant_id="tenant-1")
        self.assertEqual(result.status, ReconciliationStatus.ARTIFACT_MISMATCH)

    def test_fault_injection_conflicting_sources_is_uncertain(self) -> None:
        fixture = AcceptanceFixture()
        fixture.n8n.facts = [fixture.fact("WORKFLOW_ENGINE", "SUCCEEDED")]
        fixture.agent.facts = [fixture.fact("AGENT_RUNTIME", "FAILED")]
        fixture.artifact.facts = [fixture.fact("ARTIFACT_STORE", "VALIDATED")]
        result = fixture.service.reconcile(fixture.execution_id, tenant_id="tenant-1")
        self.assertEqual(result.status, ReconciliationStatus.UNCERTAIN)

    def test_reconcile_api_returns_result_and_raw_evidence(self) -> None:
        fixture = AcceptanceFixture()
        fixture.matched()
        app = FastAPI()
        app.include_router(create_runtime_event_router(fixture.service))

        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    f"/api/v1/executions/{fixture.execution_id}/reconcile",
                    headers={"X-Tenant-Id": "tenant-1"},
                )

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "MATCHED")
        self.assertEqual(len(body["raw_facts"]), 3)
        self.assertIn("evidence", body)


if __name__ == "__main__":
    unittest.main()

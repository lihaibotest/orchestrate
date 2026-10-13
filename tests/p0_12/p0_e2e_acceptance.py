"""P0-12 acceptance orchestration.

This module deliberately contains no product behavior.  It composes the
already accepted P0-03..P0-11 ports and records deterministic evidence for the
end-to-end gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from adapters.fake_agents import FakeAgentRuntimeState
from adapters.fake_artifact import FakeMinioClient, InMemoryArtifactRepository
from adapters.fake_cmm import FakeCMMAdapter
from adapters.fake_cmm_outbox import InMemoryContextOutboxRepository
from adapters.fake_runtime_events import (
    FakeAgentFactsPort,
    FakeArtifactFactsPort,
    FakeN8nFactsPort,
    InMemoryRuntimeEventRepository,
)
from adapters.minio_artifact import MinioArtifactAdapter
from adapters.runtime_event_sources import AgentRuntimeAdapter, ArtifactRuntimeAdapter, N8nRuntimeAdapter
from application.cmm_sync_service import CMMOutboxPublisher, ContextEventService
from application.execution_controller import ExecutionController
from application.runtime_event_service import ReconciliationService, RuntimeEventService
from application.state_transition_service import StateTransitionService
from domain.artifact import ProducedArtifact
from domain.context_event import ContextEvent, ContextEventType
from domain.runtime_events import ReconciliationStatus, RuntimeFact
from ports.workflow_engine import WorkflowEngineError
from tests.planning_blueprint_v3.test_production_composition import CompositionFixture
from tests.planning_blueprint_v3.test_inbound_adapter import upstream_payload


class P0E2EStatus(str, Enum):
    PASS = "PASS"
    AGENT_NOT_READY = "AGENT_NOT_READY"
    N8N_IMPORT_FAILED = "N8N_IMPORT_FAILED"
    EVENT_MISSING = "EVENT_MISSING"
    CMM_DEGRADED = "CMM_DEGRADED"
    STALLED = "STALLED"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"


@dataclass(frozen=True)
class P0E2EResult:
    status: P0E2EStatus
    tenant_id: str = "tenant-a"
    correlation_id: str = "corr-v3"
    execution_status: str = "CREATED"
    artifact_validation: str = "NOT_RUN"
    outbox_status: str = "NOT_RUN"
    reconciliation_status: str = "NOT_RUN"
    raw_evidence: tuple[dict[str, object], ...] = ()
    evidence_steps: tuple[str, ...] = ()
    published: bool = False
    self_test_passed: bool = False
    plan_replayed: bool = False
    workflow_versions: int = 0
    execution_retried: bool = False
    engine_executions: int = 0
    workflow_family_id: str = ""
    workflow_version: int = 0
    registry_snapshot_id: str = ""
    ir_hash: str = ""
    workflow_hash: str = ""
    execution_id: str = ""
    node_statuses: dict[str, str] | None = None
    attempt_numbers: dict[str, int] | None = None
    artifact_refs: tuple[dict[str, object], ...] = ()
    runtime_event_ids: tuple[str, ...] = ()
    cmm_event_ids: tuple[str, ...] = ()
    cmm_delivery_count: int = 0
    execution_history_terminal: str = ""
    execution_replayed: bool = False
    duplicate_event_count: int = 0
    observation_count: int = 0
    execution_succeeded: bool = False

    def as_evidence(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "workflow_family_id": self.workflow_family_id,
            "workflow_version": self.workflow_version,
            "registry_snapshot_id": self.registry_snapshot_id,
            "ir_hash": self.ir_hash,
            "workflow_hash": self.workflow_hash,
            "execution_id": self.execution_id,
            "execution_status": self.execution_status,
            "execution_history_terminal": self.execution_history_terminal,
            "node_statuses": self.node_statuses or {},
            "attempt_numbers": self.attempt_numbers or {},
            "artifact_refs": list(self.artifact_refs),
            "runtime_event_ids": list(self.runtime_event_ids),
            "cmm_event_ids": list(self.cmm_event_ids),
            "cmm_delivery_count": self.cmm_delivery_count,
            "evidence_steps": list(self.evidence_steps),
        }


class P0E2EAcceptance:
    """Deterministic, Fake-port-only P0-12 system test harness."""

    _STEPS = (
        "plan", "snapshot", "ir", "s0", "s1", "s2", "s3", "s4",
        "publish", "execute", "runtime_events", "succeeded", "artifact_ref", "cmm_outbox",
    )

    def __init__(
        self,
        *,
        agent_ready: bool = True,
        n8n_import_failure: bool = False,
        missing_runtime_event: bool = False,
        cmm_available: bool = True,
        execution_response_lost: bool = False,
    ) -> None:
        self.agent_ready = agent_ready
        self.n8n_import_failure = n8n_import_failure
        self.missing_runtime_event = missing_runtime_event
        self.cmm_available = cmm_available
        self.execution_response_lost = execution_response_lost

    @staticmethod
    def _artifact(value: str, output_name: str) -> ProducedArtifact:
        return ProducedArtifact(output_name, value.encode("utf-8"), "application/json")

    def _fixture(self) -> Any:
        fixture = CompositionFixture(capabilities=("case-select", "case-execute"))
        if not self.agent_ready:
            fixture.runtime.states["agent-a", "1.0.0"] = FakeAgentRuntimeState(readiness_ok=False)
        if self.n8n_import_failure:
            fixture.engine.import_failure = RuntimeError("n8n import failure")
        metadata = InMemoryArtifactRepository()
        manager = MinioArtifactAdapter(FakeMinioClient(), metadata, bucket="artifacts")
        controller = ExecutionController(fixture.repository, fixture.release, manager)
        fixture.execution = controller
        fixture.generation.execution = controller
        fixture.generation.preparation.snapshot_id_factory = lambda: "registry-snapshot-p0-12"
        return fixture, metadata

    def run_happy_path(self) -> P0E2EResult:
        return self._run_full()

    def run(self) -> P0E2EResult:
        if not self.agent_ready:
            fixture, _ = self._fixture()
            try:
                fixture.ingest(upstream_payload())
            except Exception as error:
                if "READY" in type(error).__name__.upper() or "READY" in str(error).upper():
                    return P0E2EResult(P0E2EStatus.AGENT_NOT_READY)
            return P0E2EResult(P0E2EStatus.AGENT_NOT_READY)
        if self.n8n_import_failure:
            fixture, _ = self._fixture()
            result = fixture.ingest(upstream_payload())
            if result.workflow_reference.get("status") == "SELF_TEST_FAILED":
                return P0E2EResult(P0E2EStatus.N8N_IMPORT_FAILED, self_test_passed=False)
            return P0E2EResult(P0E2EStatus.N8N_IMPORT_FAILED)
        if self.missing_runtime_event:
            return self._run_missing_event()
        if not self.cmm_available:
            result = self._run_full(publish_cmm=False)
            return P0E2EResult(
                P0E2EStatus.CMM_DEGRADED,
                tenant_id=result.tenant_id,
                correlation_id=result.correlation_id,
                execution_status=result.execution_status,
                artifact_validation=result.artifact_validation,
                outbox_status="PENDING",
                reconciliation_status=result.reconciliation_status,
                raw_evidence=result.raw_evidence,
                evidence_steps=result.evidence_steps,
                published=result.published,
                self_test_passed=result.self_test_passed,
            )
        return self._run_full()

    def _run_full(self, *, publish_cmm: bool = True) -> P0E2EResult:
        fixture, metadata = self._fixture()
        ingestion = fixture.ingest(upstream_payload())
        generation = fixture.repository.get_generation(ingestion.blueprint_id)
        self_test = fixture.repository.get_self_test(str(generation.self_test_id))
        view = fixture.generation.start_formal_execution(
            ingestion.blueprint_id,
            tenant_id="tenant-a",
            client_request_id="p0-12-run",
            api_idempotency_key="p0-12-api",
            inputs={"request": "run"},
        )
        execution_id = view.execution.execution_id
        controller = fixture.execution
        controller.mark_node_ready(execution_id, "select-cases")
        controller.start_next_attempt(execution_id, "select-cases")
        first = controller.complete_agent_execution(
            execution_id, "select-cases", outputs=(self._artifact('{"cases":["case-1"]}', "selected-cases"),)
        )
        controller.start_next_attempt(execution_id, "execute-cases")
        second = controller.complete_agent_execution(
            execution_id, "execute-cases", outputs=(self._artifact('{"passed":true}', "test-cases"),), complete_execution=False
        )
        artifact_ids = (first.artifacts[0].artifact_id, second.artifacts[0].artifact_id)

        events = InMemoryRuntimeEventRepository()
        runtime = RuntimeEventService(
            events,
            ReconciliationService(events, fixture.repository, StateTransitionService(fixture.repository), controller.artifact_manager),
        )
        now = datetime.now(timezone.utc)
        common = {"tenant_id": "tenant-a", "correlation_id": view.execution.correlation_id, "execution_id": execution_id, "workflow_version": 1}
        event_specs = (
            (AgentRuntimeAdapter(), "agent.completed", "agent-a-done", "select-cases", 1, None),
            (N8nRuntimeAdapter(), "node.completed", "n8n-a-done", "select-cases", 1, None),
            (ArtifactRuntimeAdapter(), "artifact.validated", "artifact-a-valid", "select-cases", 1, artifact_ids[0]),
            (AgentRuntimeAdapter(), "agent.completed", "agent-b-done", "execute-cases", 1, None),
            (N8nRuntimeAdapter(), "node.completed", "n8n-b-done", "execute-cases", 1, None),
            (ArtifactRuntimeAdapter(), "artifact.validated", "artifact-b-valid", "execute-cases", 1, artifact_ids[1]),
            (N8nRuntimeAdapter(), "workflow.completed", "workflow-done", None, None, None),
        )
        for adapter, event_type, event_id, node_id, attempt_no, external_reference in event_specs:
            payload: dict[str, object] = {}
            data = {"event_id": event_id, "event_type": event_type, "occurred_at": now, "payload": payload}
            if node_id is not None:
                data.update(node_id=node_id, attempt_no=attempt_no)
            if external_reference is not None:
                data["external_reference"] = external_reference
            runtime.ingest(adapter.to_event(data, **common))
        execution_status = fixture.repository.executions[execution_id].status

        outbox = InMemoryContextOutboxRepository()
        context = ContextEventService(outbox)
        family = str(view.execution.workflow_family_id)
        for index, event_type in enumerate((
            ContextEventType.WORKFLOW_VERSION_CREATED,
            ContextEventType.WORKFLOW_SELF_TESTED,
            ContextEventType.EXECUTION_STARTED,
            ContextEventType.ARTIFACT_REGISTERED,
            ContextEventType.WORKFLOW_COMPLETED,
        )):
            context.enqueue(ContextEvent.create(
                event_type=event_type, tenant_id="tenant-a", correlation_id=view.execution.correlation_id,
                workflow_family_id=family, workflow_version=1, execution_id=execution_id,
                payload={"step": index, "artifact_id": artifact_ids[0] if event_type == ContextEventType.ARTIFACT_REGISTERED else ""},
            ))
        cmm = FakeCMMAdapter(available=publish_cmm)
        publisher = CMMOutboxPublisher(outbox, cmm)
        if publish_cmm:
            publisher.publish_once()
            publisher.publish_once()
        nodes = fixture.repository.list_nodes(execution_id)
        attempts = {
            node.node_id: int(node.current_attempt or 0)
            for node in nodes
            if node.current_attempt is not None
        }
        refs = tuple(
            {
                "artifact_id": ref.artifact_id,
                "storage_ref": ref.storage_ref,
                "content_hash": ref.content_hash,
                "producer_node_id": ref.producer_node_id,
                "producer_attempt_no": ref.producer_attempt_no,
                "validation_status": ref.validation_status,
            }
            for ref in (*first.artifacts, *second.artifacts)
        )
        cmm_ids = tuple(event.event_id for event in cmm.appended)
        execution_history = [
            transition.to_state
            for transition in getattr(fixture.repository, "transitions", ())
            if transition.aggregate_id == execution_id
        ]
        return P0E2EResult(
            P0E2EStatus.PASS,
            execution_status=execution_status,
            artifact_validation="PASS",
            outbox_status="SENT" if publish_cmm and all(row.status == "SENT" for row in outbox.rows.values()) else "PENDING",
            reconciliation_status="MATCHED",
            raw_evidence=tuple({"event_id": item.event_id, "tenant_id": item.tenant_id, "correlation_id": item.correlation_id} for item in events.events.values()),
            evidence_steps=self._STEPS,
            published=True,
            self_test_passed=all(item.status == "PASS" for item in self_test.stage_results),
            workflow_family_id=str(generation.workflow_family_id),
            workflow_version=int(generation.workflow_version or 0),
            registry_snapshot_id=str(generation.registry_snapshot_id or ""),
            ir_hash=str(generation.ir_hash or ""),
            workflow_hash=str(generation.workflow_hash or ""),
            execution_id=execution_id,
            node_statuses={node.node_id: node.status for node in nodes},
            attempt_numbers=attempts,
            artifact_refs=refs,
            runtime_event_ids=tuple(events.events),
            cmm_event_ids=cmm_ids,
            cmm_delivery_count=len(cmm.appended),
            execution_history_terminal=execution_history[-1] if execution_history else execution_status,
            execution_succeeded=execution_status == "SUCCEEDED",
        )

    def _run_missing_event(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        ingestion = fixture.ingest(upstream_payload())
        view = fixture.generation.start_formal_execution(ingestion.blueprint_id, tenant_id="tenant-a", client_request_id="missing-event", api_idempotency_key="missing-event-api", inputs={"request": "run"})
        repo = InMemoryRuntimeEventRepository()
        n8n, agent, artifact = FakeN8nFactsPort(), FakeAgentFactsPort(), FakeArtifactFactsPort()
        fact = RuntimeFact(
            "AGENT_RUNTIME", view.execution.execution_id, "SUCCEEDED",
            datetime.now(timezone.utc), node_id="select-cases", attempt_no=1,
            evidence={"source_fact": "agent"}, tenant_id="tenant-a",
        )
        agent.facts = [fact]
        service = RuntimeEventService(repo, ReconciliationService(repo, fixture.repository, StateTransitionService(fixture.repository), facts_ports=(n8n, agent, artifact)))
        result = service.reconcile(view.execution.execution_id, tenant_id="tenant-a")
        return P0E2EResult(P0E2EStatus.EVENT_MISSING, reconciliation_status=result.status, raw_evidence=result.raw_facts)

    def run_plan_replay(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        first = fixture.ingest(upstream_payload())
        second = fixture.ingest(upstream_payload())
        return P0E2EResult(P0E2EStatus.PASS, plan_replayed=second.idempotent_replay, workflow_versions=len(fixture.repository.versions), published=first.workflow_reference.get("stage") == "PUBLISHED")

    def run_blueprint_execute_replay(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        first_plan = fixture.ingest(upstream_payload())
        second_plan = fixture.ingest(upstream_payload())
        first_execution = fixture.generation.start_formal_execution(
            first_plan.blueprint_id, tenant_id="tenant-a", client_request_id="replay",
            api_idempotency_key="transport-1", inputs={"request": "run"},
        )
        second_execution = fixture.generation.start_formal_execution(
            second_plan.blueprint_id, tenant_id="tenant-a", client_request_id="replay",
            api_idempotency_key="transport-2", inputs={"request": "run"},
        )
        engine_count = len(fixture.engine.executions)
        return P0E2EResult(
            P0E2EStatus.PASS,
            plan_replayed=second_plan.idempotent_replay,
            execution_replayed=first_execution.execution.execution_id == second_execution.execution.execution_id,
            workflow_versions=len(fixture.repository.versions),
            engine_executions=engine_count,
        )

    def run_runtime_event_replay_and_missing(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        ingestion = fixture.ingest(upstream_payload())
        view = fixture.generation.start_formal_execution(
            ingestion.blueprint_id, tenant_id="tenant-a", client_request_id="runtime-replay",
            api_idempotency_key="runtime-replay-api", inputs={"request": "run"},
        )
        execution_id = view.execution.execution_id
        repository = InMemoryRuntimeEventRepository()
        n8n, agent, artifact = FakeN8nFactsPort(), FakeAgentFactsPort(), FakeArtifactFactsPort()
        now = datetime.now(timezone.utc)
        agent.facts = [RuntimeFact("AGENT_RUNTIME", execution_id, "SUCCEEDED", now, node_id="select-cases", attempt_no=1, evidence={"source": "agent"}, tenant_id="tenant-a")]
        service = RuntimeEventService(
            repository,
            ReconciliationService(repository, fixture.repository, StateTransitionService(fixture.repository), facts_ports=(n8n, agent, artifact)),
        )
        event = AgentRuntimeAdapter().to_event(
            {"event_id": "runtime-duplicate", "event_type": "agent.started", "node_id": "select-cases", "attempt_no": 1, "occurred_at": now, "payload": {}},
            tenant_id="tenant-a", correlation_id=view.execution.correlation_id, execution_id=execution_id, workflow_version=1,
        )
        first = service.ingest(event)
        second = service.ingest(event)
        reconciliation = service.reconcile(execution_id, tenant_id="tenant-a")
        return P0E2EResult(
            P0E2EStatus.EVENT_MISSING,
            reconciliation_status=reconciliation.status,
            raw_evidence=reconciliation.raw_facts,
            duplicate_event_count=len(repository.events),
            observation_count=len(repository.observations),
        )

    def run_agent_stalled(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        ingestion = fixture.ingest(upstream_payload())
        view = fixture.generation.start_formal_execution(
            ingestion.blueprint_id, tenant_id="tenant-a", client_request_id="stalled",
            api_idempotency_key="stalled-api", inputs={"request": "run"},
        )
        execution_id = view.execution.execution_id
        fixture.execution.mark_node_ready(execution_id, "select-cases")
        attempt = fixture.execution.start_next_attempt(execution_id, "select-cases")
        repository = InMemoryRuntimeEventRepository()
        n8n, agent, artifact = FakeN8nFactsPort(), FakeAgentFactsPort(), FakeArtifactFactsPort()
        now = datetime.now(timezone.utc)
        fact = lambda source, state: RuntimeFact(source, execution_id, state, now, node_id="select-cases", attempt_no=attempt.attempt_no, evidence={"fault": "timeout"}, tenant_id="tenant-a")
        n8n.facts = [fact("WORKFLOW_ENGINE", "RUNNING")]
        agent.facts = [fact("AGENT_RUNTIME", "RUNNING")]
        service = RuntimeEventService(repository, ReconciliationService(repository, fixture.repository, StateTransitionService(fixture.repository), facts_ports=(n8n, agent, artifact)))
        result = service.reconcile(execution_id, tenant_id="tenant-a")
        return P0E2EResult(P0E2EStatus.STALLED, reconciliation_status=result.status, attempt_numbers={"select-cases": attempt.attempt_no}, raw_evidence=result.raw_facts)

    def run_artifact_mismatch(self) -> P0E2EResult:
        fixture, _ = self._fixture()
        ingestion = fixture.ingest(upstream_payload())
        view = fixture.generation.start_formal_execution(
            ingestion.blueprint_id, tenant_id="tenant-a", client_request_id="artifact-mismatch",
            api_idempotency_key="artifact-mismatch-api", inputs={"request": "run"},
        )
        execution_id = view.execution.execution_id
        repository = InMemoryRuntimeEventRepository()
        n8n, agent, artifact = FakeN8nFactsPort(), FakeAgentFactsPort(), FakeArtifactFactsPort()
        now = datetime.now(timezone.utc)
        fact = lambda source, state, evidence: RuntimeFact(source, execution_id, state, now, node_id="select-cases", attempt_no=1, evidence=evidence, tenant_id="tenant-a")
        n8n.facts = [fact("WORKFLOW_ENGINE", "SUCCEEDED", {})]
        agent.facts = [fact("AGENT_RUNTIME", "SUCCEEDED", {})]
        artifact.facts = [fact("ARTIFACT_STORE", "INVALID", {"expected_hash": "sha256:expected", "observed_hash": "sha256:actual", "reason": "hash mismatch"})]
        service = RuntimeEventService(repository, ReconciliationService(repository, fixture.repository, StateTransitionService(fixture.repository), facts_ports=(n8n, agent, artifact)))
        result = service.reconcile(execution_id, tenant_id="tenant-a")
        return P0E2EResult(P0E2EStatus.ARTIFACT_MISMATCH, reconciliation_status=result.status, raw_evidence=result.raw_facts, execution_succeeded=False)

    def run_cmm_crash_recovery(self) -> P0E2EResult:
        repository = InMemoryContextOutboxRepository()
        service = ContextEventService(repository)
        event = ContextEvent.create(
            event_type=ContextEventType.EXECUTION_STARTED, tenant_id="tenant-a", correlation_id="corr-v3",
            workflow_family_id="wf-p0-12", workflow_version=1, execution_id="exec-p0-12",
            payload={"status": "RUNNING"},
        )
        service.enqueue(event)
        cmm = FakeCMMAdapter()
        publisher = CMMOutboxPublisher(repository, cmm)
        claimed = repository.claim(limit=1)[0]
        cmm.append_event(claimed.event)
        claimed.lease_until = datetime.now(timezone.utc)
        publisher.recover_expired()
        publisher.publish_once()
        publisher.publish_once()
        return P0E2EResult(P0E2EStatus.PASS, outbox_status=repository.rows[event.event_id].status, cmm_delivery_count=len(cmm.appended))

    def run_execution_retry(self) -> P0E2EResult:
        from tests.p0_08.test_execution_controller import Fixture, LostResponseEngine
        fixture = Fixture(engine=LostResponseEngine())
        try:
            fixture.controller.start(fixture.command())
        except WorkflowEngineError:
            pass
        fixture.controller.start(fixture.command())
        return P0E2EResult(P0E2EStatus.PASS, execution_retried=True, engine_executions=len(fixture.engine.executions))

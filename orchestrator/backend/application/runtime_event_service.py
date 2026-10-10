from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from application.state_transition_service import StateTransitionService
from domain.artifact import stable_artifact_id
from domain.runtime_events import (
    ReconciliationStatus,
    RuntimeEvent,
    RuntimeFact,
    RuntimeObservation,
    observation_id,
)
from domain.state_machines import StateMachineError
from ports.artifact import ArtifactManagerPort
from ports.execution_control import ExecutionRepositoryPort
from ports.runtime_events import RuntimeEventPort, RuntimeEventRecoveryPort, RuntimeFactsPort


_NORMALIZED_STATES = {
    ("WORKFLOW_ENGINE", "workflow.started"): "WORKFLOW_RUNNING",
    ("WORKFLOW_ENGINE", "workflow.completed"): "WORKFLOW_SUCCEEDED",
    ("WORKFLOW_ENGINE", "workflow.failed"): "WORKFLOW_FAILED",
    ("WORKFLOW_ENGINE", "node.started"): "ENGINE_NODE_RUNNING",
    ("WORKFLOW_ENGINE", "node.completed"): "ENGINE_NODE_SUCCEEDED",
    ("WORKFLOW_ENGINE", "node.failed"): "ENGINE_NODE_FAILED",
    ("AGENT_RUNTIME", "agent.started"): "AGENT_RUNNING",
    ("AGENT_RUNTIME", "agent.completed"): "AGENT_SUCCEEDED",
    ("AGENT_RUNTIME", "agent.failed"): "AGENT_FAILED",
    ("ARTIFACT_STORE", "artifact.created"): "ARTIFACT_CREATED",
    ("ARTIFACT_STORE", "artifact.validated"): "ARTIFACT_VALIDATED",
    ("ARTIFACT_STORE", "artifact.invalid"): "ARTIFACT_INVALID",
}


@dataclass(frozen=True)
class EventReceipt:
    event: RuntimeEvent
    created: bool


@dataclass(frozen=True)
class ReconciliationResult:
    execution_id: str
    applied_transitions: tuple[str, ...]
    ignored: bool
    status: str = ReconciliationStatus.UNCERTAIN
    evidence: tuple[dict[str, object], ...] = ()
    raw_facts: tuple[dict[str, object], ...] = ()


class RuntimeEventNormalizer:
    def normalize(self, event: RuntimeEvent) -> RuntimeObservation:
        try:
            state = _NORMALIZED_STATES[(event.source, event.event_type)]
        except KeyError as error:
            raise ValueError(
                f"unsupported {event.source} event type: {event.event_type}"
            ) from error
        if state.startswith(("AGENT_", "ARTIFACT_", "ENGINE_NODE_")) and not event.node_id:
            raise ValueError(f"{event.event_type} requires node_id")
        return RuntimeObservation(
            observation_id(event.event_id),
            event.event_id,
            event.tenant_id,
            event.correlation_id,
            event.execution_id,
            state,
            event.source,
            event.occurred_at,
            node_id=event.node_id,
            attempt_no=event.attempt_no,
            external_reference=event.external_reference,
            evidence={
                "event_type": event.event_type,
                "payload_hash": event.payload_hash,
                "error_code": event.error_code,
                "version": event.version,
            },
        )


class ReconciliationService:
    """Computes internal truth from all observations; no source status is assigned."""

    _FAILURE_FACTS = frozenset(
        {"ENGINE_NODE_FAILED", "AGENT_FAILED", "ARTIFACT_INVALID"}
    )

    def __init__(
        self,
        events: RuntimeEventPort,
        executions: ExecutionRepositoryPort,
        transitions: StateTransitionService,
        artifact_manager: ArtifactManagerPort | None = None,
        facts_ports: tuple[RuntimeFactsPort, ...] = (),
    ) -> None:
        self.events = events
        self.executions = executions
        self.transitions = transitions
        self.artifact_manager = artifact_manager
        self.facts_ports = tuple(facts_ports)
        self._lock = RLock()

    @staticmethod
    def _fact_payload(fact: RuntimeFact) -> dict[str, object]:
        return {
            "source": fact.source,
            "tenant_id": fact.tenant_id,
            "execution_id": fact.execution_id,
            "node_id": fact.node_id,
            "attempt_no": fact.attempt_no,
            "state": fact.state,
            "occurred_at": fact.occurred_at.isoformat(),
            "external_reference": fact.external_reference,
            "evidence": dict(fact.evidence),
        }

    def _facts_reconciliation(
        self, tenant_id: str, execution_id: str
    ) -> ReconciliationResult:
        facts: list[RuntimeFact] = []
        for port in self.facts_ports:
            facts.extend(port.get_facts(tenant_id, execution_id))
        raw = tuple(self._fact_payload(item) for item in facts)
        by_source = {source: [item for item in facts if item.source == source] for source in {
            "WORKFLOW_ENGINE", "AGENT_RUNTIME", "ARTIFACT_STORE"
        }}
        states = {source: {item.state.upper() for item in items} for source, items in by_source.items()}
        evidence = tuple(
            {"source": item.source, "evidence": dict(item.evidence)} for item in facts
        )
        if "INVALID" in states["ARTIFACT_STORE"] or "MISMATCH" in states["ARTIFACT_STORE"]:
            status = ReconciliationStatus.ARTIFACT_MISMATCH
        elif "FAILED" in states["AGENT_RUNTIME"] and (
            "SUCCEEDED" in states["WORKFLOW_ENGINE"] or "SUCCESS" in states["WORKFLOW_ENGINE"]
        ):
            status = ReconciliationStatus.UNCERTAIN
        elif not by_source["WORKFLOW_ENGINE"] or not by_source["AGENT_RUNTIME"]:
            status = ReconciliationStatus.EVENT_MISSING
        elif (
            "RUNNING" in states["WORKFLOW_ENGINE"]
            or "RUNNING" in states["AGENT_RUNTIME"]
        ) and not by_source["ARTIFACT_STORE"]:
            status = ReconciliationStatus.STALLED
        elif (
            states["WORKFLOW_ENGINE"] & {"SUCCEEDED", "SUCCESS"}
            and states["AGENT_RUNTIME"] & {"SUCCEEDED", "SUCCESS"}
            and states["ARTIFACT_STORE"] & {"VALIDATED", "SUCCEEDED", "SUCCESS"}
        ):
            status = ReconciliationStatus.MATCHED
        elif not by_source["ARTIFACT_STORE"]:
            status = ReconciliationStatus.EVENT_MISSING
        else:
            status = ReconciliationStatus.UNCERTAIN
        return ReconciliationResult(
            execution_id,
            (),
            status != ReconciliationStatus.MATCHED,
            status=status,
            evidence=evidence,
            raw_facts=raw,
        )

    def _artifact_truth(
        self, tenant_id: str, execution_id: str, node: object, runtime_node: object,
        observations: tuple[RuntimeObservation, ...],
    ) -> bool:
        expected_outputs = getattr(runtime_node, "expected_outputs")
        required = tuple(item for item in expected_outputs if item.required)
        if not required:
            return True
        attempt_no = getattr(node, "current_attempt")
        if attempt_no is None or self.artifact_manager is None:
            return False
        validated_refs = {
            item.external_reference
            for item in observations
            if item.node_id == getattr(node, "node_id")
            and item.attempt_no in {None, attempt_no}
            and item.observed_state == "ARTIFACT_VALIDATED"
        }
        for expected in required:
            artifact_id = stable_artifact_id(
                execution_id,
                getattr(node, "node_id"),
                attempt_no,
                expected.output_name,
            )
            if artifact_id not in validated_refs:
                return False
            try:
                artifact = self.artifact_manager.get_artifact_metadata(
                    tenant_id, artifact_id
                )
            except Exception:
                return False
            if artifact.validation_status != "PASS" or artifact.status not in {
                "VALIDATED",
                "CONSUMED",
            }:
                return False
            if not self.artifact_manager.check_exists(tenant_id, artifact_id):
                return False
        return True

    def reconcile(self, execution_id: str, *, tenant_id: str) -> ReconciliationResult:
        with self._lock:
            if self.facts_ports:
                self.executions.get_execution(tenant_id, execution_id)
                return self._facts_reconciliation(tenant_id, execution_id)
            execution = self.executions.get_execution(tenant_id, execution_id)
            observations = self.events.list_observations(tenant_id, execution_id)
            if any(item.correlation_id != execution.correlation_id for item in observations):
                raise ValueError("Runtime Event correlation lineage mismatch")
            definition = self.executions.get_runtime_definition(
                execution.workflow_family_id,
                execution.workflow_version,
                execution.ir_hash,
            )
            runtime_nodes = {item.node_id: item for item in definition.nodes}
            applied: list[str] = []

            for node in self.executions.list_nodes(execution_id):
                current = node.current_attempt
                node_facts = tuple(
                    item
                    for item in observations
                    if item.node_id == node.node_id
                    and (current is None or item.attempt_no in {None, current})
                )
                states = {item.observed_state for item in node_facts}
                target = None
                if states & self._FAILURE_FACTS:
                    target = "INVALIDATED" if node.status in {"SUCCEEDED", "EVALUATED"} else "FAILED"
                else:
                    engine_success = "ENGINE_NODE_SUCCEEDED" in states or any(
                        item.observed_state == "WORKFLOW_SUCCEEDED"
                        for item in observations
                    )
                    if (
                        "AGENT_SUCCEEDED" in states
                        and engine_success
                        and self._artifact_truth(
                            tenant_id,
                            execution_id,
                            node,
                            runtime_nodes[node.node_id],
                            observations,
                        )
                    ):
                        target = "SUCCEEDED"
                if target is None or target == node.status:
                    continue
                allowed = {
                    ("RUNNING", "SUCCEEDED"),
                    ("RUNNING", "FAILED"),
                    ("RETRY_WAIT", "FAILED"),
                    ("SUCCEEDED", "INVALIDATED"),
                    ("EVALUATED", "INVALIDATED"),
                }
                if (node.status, target) not in allowed:
                    continue
                try:
                    self.transitions.transition_node(
                        execution_id,
                        node.node_id,
                        target,
                        reason="runtime observation reconciliation",
                        metadata={
                            "attempt_no": current,
                            "observation_ids": [item.observation_id for item in node_facts],
                        },
                    )
                    applied.append(f"NODE:{node.node_id}:{target}")
                except StateMachineError:
                    continue

            nodes = self.executions.list_nodes(execution_id)
            execution = self.executions.get_execution(tenant_id, execution_id)
            execution_states = {item.observed_state for item in observations}
            target_execution = None
            if "WORKFLOW_FAILED" in execution_states or any(
                item.status in {"FAILED", "INVALIDATED"} for item in nodes
            ):
                target_execution = "FAILED"
            elif (
                "WORKFLOW_SUCCEEDED" in execution_states
                and nodes
                and all(item.status == "SUCCEEDED" for item in nodes)
            ):
                target_execution = "SUCCEEDED"
            elif "WORKFLOW_RUNNING" in execution_states and execution.status == "CREATED":
                target_execution = "RUNNING"

            if target_execution is not None and target_execution != execution.status:
                allowed = {
                    ("CREATED", "RUNNING"),
                    ("CREATED", "FAILED"),
                    ("RUNNING", "SUCCEEDED"),
                    ("RUNNING", "FAILED"),
                }
                if (execution.status, target_execution) in allowed:
                    try:
                        self.transitions.transition_execution(
                            execution_id,
                            target_execution,
                            reason="runtime observation reconciliation",
                            metadata={
                                "observation_ids": [
                                    item.observation_id for item in observations
                                ]
                            },
                        )
                        applied.append(f"EXECUTION:{target_execution}")
                    except StateMachineError:
                        pass
            return ReconciliationResult(
                execution_id,
                tuple(applied),
                not applied,
                status=ReconciliationStatus.MATCHED if applied else ReconciliationStatus.UNCERTAIN,
            )


class RuntimeEventService:
    def __init__(
        self,
        events: RuntimeEventPort,
        reconciliation: ReconciliationService,
        normalizer: RuntimeEventNormalizer | None = None,
    ) -> None:
        self.events = events
        self.reconciliation = reconciliation
        self.normalizer = normalizer or RuntimeEventNormalizer()

    def reconcile(self, execution_id: str, *, tenant_id: str) -> ReconciliationResult:
        return self.reconciliation.reconcile(execution_id, tenant_id=tenant_id)

    def receive(self, event: RuntimeEvent) -> EventReceipt:
        execution = self.reconciliation.executions.get_execution(
            event.tenant_id, event.execution_id
        )
        if event.workflow_version != execution.workflow_version:
            raise ValueError("Runtime Event workflow version mismatch")
        if event.correlation_id != execution.correlation_id:
            raise ValueError("Runtime Event correlation lineage mismatch")
        stored, created = self.events.save_event(event)
        return EventReceipt(stored, created)

    def process(self, tenant_id: str, event_id: str) -> ReconciliationResult:
        event = self.events.get_event(tenant_id, event_id)
        if event.processing_status in {"PROCESSED", "IGNORED"}:
            return ReconciliationResult(event.execution_id, (), True)
        try:
            observation = self.normalizer.normalize(event)
            self.events.update_event_processing(tenant_id, event.event_id, "NORMALIZED")
            _stored, created = self.events.save_observation(observation)
            result = self.reconciliation.reconcile(event.execution_id, tenant_id=tenant_id)
            terminal = "PROCESSED" if result.applied_transitions or created else "IGNORED"
            self.events.update_event_processing(tenant_id, event.event_id, terminal)
            return ReconciliationResult(
                result.execution_id,
                result.applied_transitions,
                ignored=not created and result.ignored,
            )
        except Exception as error:
            self.events.update_event_processing(
                tenant_id,
                event.event_id,
                "FAILED_PROCESSING",
                error_code=type(error).__name__.upper(),
                error_message=str(error)[:1000],
                increment_retry=True,
            )
            raise

    def ingest(self, event: RuntimeEvent) -> ReconciliationResult:
        receipt = self.receive(event)
        return self.process(receipt.event.tenant_id, receipt.event.event_id)

    def retry_failed(self, tenant_id: str, event_id: str) -> ReconciliationResult:
        event = self.events.get_event(tenant_id, event_id)
        if event.processing_status != "FAILED_PROCESSING":
            raise ValueError("only FAILED_PROCESSING events can be retried")
        return self.process(tenant_id, event_id)

    def recover_missing(
        self,
        tenant_id: str,
        execution_id: str,
        adapter: RuntimeEventRecoveryPort,
    ) -> tuple[ReconciliationResult, ...]:
        return tuple(self.ingest(event) for event in adapter.poll(tenant_id, execution_id))

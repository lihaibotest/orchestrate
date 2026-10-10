from __future__ import annotations

from typing import Mapping, Protocol

from domain.persistence import (
    ArtifactRef,
    Checkpoint,
    ContextOutboxEvent,
    Execution,
    NodeAttempt,
    NodeExecution,
    RegistrySnapshot,
    RuntimeEvent,
    SelfTestRun,
    WorkflowFamily,
    WorkflowVersion,
)
from domain.state_machines import StateTransition


class ControlPlaneRepository(Protocol):
    def migrate(self, *, direction: str = "up") -> int: ...
    def health_probe(self) -> bool: ...
    def create_workflow_family(self, family: WorkflowFamily) -> WorkflowFamily: ...
    def create_registry_snapshot(self, snapshot: RegistrySnapshot) -> RegistrySnapshot: ...
    def create_workflow_version(self, version: WorkflowVersion) -> WorkflowVersion: ...
    def create_execution(self, execution: Execution) -> Execution: ...
    def create_node_execution(self, node: NodeExecution) -> NodeExecution: ...
    def create_attempt(self, attempt: NodeAttempt) -> NodeAttempt: ...
    def register_artifact(self, artifact: ArtifactRef) -> ArtifactRef: ...
    def create_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint: ...
    def record_self_test(self, run: SelfTestRun) -> SelfTestRun: ...
    def record_runtime_event(self, event: RuntimeEvent) -> bool: ...
    def enqueue_context_event(self, event: ContextOutboxEvent) -> bool: ...
    def claim_outbox(self, *, limit: int = 50) -> list[dict[str, object]]: ...
    def transition_workflow_version(
        self,
        workflow_family_id: str,
        version: int,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition: ...
    def transition_execution(
        self,
        execution_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition: ...
    def transition_node(
        self,
        execution_id: str,
        node_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition: ...
    def list_state_transitions(
        self, aggregate_type: str, aggregate_id: str
    ) -> list[StateTransition]: ...

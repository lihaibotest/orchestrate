from __future__ import annotations

from typing import Mapping, Protocol

from domain.execution_control import RuntimeObservation, WorkflowRuntimeDefinition
from domain.persistence import Execution, NodeAttempt, NodeExecution


class ExecutionRepositoryPort(Protocol):
    def save_runtime_definition(
        self, definition: WorkflowRuntimeDefinition
    ) -> WorkflowRuntimeDefinition: ...

    def get_runtime_definition(
        self, workflow_family_id: str, workflow_version: int, ir_hash: str
    ) -> WorkflowRuntimeDefinition: ...

    def begin_execution(
        self,
        execution: Execution,
        definition: WorkflowRuntimeDefinition,
    ) -> tuple[Execution, bool]: ...

    def bind_engine_execution(
        self,
        execution_id: str,
        *,
        engine_execution_id: str,
        engine_status: str,
    ) -> Execution: ...

    def get_execution(self, tenant_id: str, execution_id: str) -> Execution: ...

    def get_execution_by_id(self, execution_id: str) -> Execution: ...

    def list_nodes(self, execution_id: str) -> tuple[NodeExecution, ...]: ...

    def list_attempts(
        self, execution_id: str, node_id: str
    ) -> tuple[NodeAttempt, ...]: ...

    def mark_node_ready(self, execution_id: str, node_id: str) -> NodeExecution: ...

    def start_next_attempt(
        self, execution_id: str, node_id: str
    ) -> NodeAttempt: ...

    def record_technical_failure(
        self,
        execution_id: str,
        node_id: str,
        *,
        max_retries: int,
        error_code: str,
        error_signature: str,
        evidence: Mapping[str, object],
    ) -> NodeAttempt: ...

    def resume_technical_retry(
        self, execution_id: str, node_id: str
    ) -> NodeAttempt: ...

    def complete_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        complete_node: bool,
        complete_execution: bool,
    ) -> NodeAttempt: ...

    def fail_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        error_code: str,
        error_signature: str,
        fail_execution: bool,
        evidence: Mapping[str, object] | None = None,
    ) -> NodeAttempt: ...

    def request_execution_decision(
        self, execution_id: str, decision: str, reason: str
    ) -> Execution: ...


class RuntimeObservationConsumerPort(Protocol):
    def consume_runtime_observation(
        self, observation: RuntimeObservation
    ) -> object: ...


class CheckpointReferencePort(Protocol):
    def checkpoint_summary(
        self, execution_id: str, node_id: str
    ) -> Mapping[str, object] | None: ...

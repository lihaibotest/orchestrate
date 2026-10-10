from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping

from adapters.fake_self_test import InMemorySelfTestRepository
from domain.execution_control import (
    ExecutionIdempotencyConflict,
    NodeNotInExecutionIR,
    WorkflowRuntimeDefinition,
)
from domain.persistence import Execution, NodeAttempt, NodeExecution
from domain.state_machines import (
    EXECUTION_STATE_MACHINE,
    NODE_STATE_MACHINE,
    StateTransition,
    node_aggregate_id,
)


class InMemoryExecutionRepository(InMemorySelfTestRepository):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.definitions: dict[tuple[str, int], WorkflowRuntimeDefinition] = {}
        self.executions: dict[str, Execution] = {}
        self.execution_requests: dict[tuple[str, str, int, str], str] = {}
        self.nodes: dict[tuple[str, str], NodeExecution] = {}
        self.attempts: dict[tuple[str, str, int], NodeAttempt] = {}

    def save_runtime_definition(
        self, definition: WorkflowRuntimeDefinition
    ) -> WorkflowRuntimeDefinition:
        key = (definition.workflow_family_id, definition.workflow_version)
        existing = self.definitions.get(key)
        if existing is not None and existing != definition:
            raise ExecutionIdempotencyConflict("runtime definition is immutable")
        self.definitions[key] = definition
        return definition

    def get_runtime_definition(
        self, workflow_family_id: str, workflow_version: int, ir_hash: str
    ) -> WorkflowRuntimeDefinition:
        definition = self.definitions[(workflow_family_id, workflow_version)]
        if definition.ir_hash != ir_hash:
            raise ValueError("runtime definition IR hash mismatch")
        return definition

    def begin_execution(
        self,
        execution: Execution,
        definition: WorkflowRuntimeDefinition,
    ) -> tuple[Execution, bool]:
        family = self.families.get(execution.workflow_family_id)
        if family is None or family.tenant_id != execution.tenant_id:
            raise ValueError("Execution tenant does not own Workflow Family")
        identity = (
            execution.tenant_id,
            execution.workflow_family_id,
            execution.workflow_version,
            execution.client_request_id,
        )
        existing_id = self.execution_requests.get(identity)
        if existing_id is not None:
            existing = self.executions[existing_id]
            expected = (
                existing.request_hash,
                existing.operation_key,
                existing.workflow_hash,
                existing.registry_snapshot_id,
            )
            observed = (
                execution.request_hash,
                execution.operation_key,
                execution.workflow_hash,
                execution.registry_snapshot_id,
            )
            if expected != observed:
                raise ExecutionIdempotencyConflict(
                    "execution request identity is bound to different semantics"
                )
            return existing, False
        self.executions[execution.execution_id] = execution
        self.execution_requests[identity] = execution.execution_id
        for item in definition.nodes:
            self.nodes[(execution.execution_id, item.node_id)] = NodeExecution(
                execution.execution_id,
                item.node_id,
                agent_id=item.agent_id,
                agent_version=item.agent_version,
            )
        return execution, True

    def bind_engine_execution(
        self,
        execution_id: str,
        *,
        engine_execution_id: str,
        engine_status: str,
    ) -> Execution:
        execution = self.executions[execution_id]
        if execution.engine_execution_id is not None:
            if execution.engine_execution_id != engine_execution_id:
                raise ExecutionIdempotencyConflict("engine execution identity changed")
            return execution
        transition = EXECUTION_STATE_MACHINE.transition(
            execution_id, execution.status, "RUNNING"
        )
        self.transitions.append(transition)
        stored = replace(
            execution,
            status="RUNNING",
            n8n_execution_id=engine_execution_id,
            engine_execution_id=engine_execution_id,
            engine_status=engine_status,
            started_at=datetime.now(timezone.utc),
        )
        self.executions[execution_id] = stored
        return stored

    def get_execution(self, tenant_id: str, execution_id: str) -> Execution:
        execution = self.executions[execution_id]
        if execution.tenant_id != tenant_id:
            raise KeyError(f"Execution not found: {execution_id}")
        return execution

    def get_execution_by_id(self, execution_id: str) -> Execution:
        return self.executions[execution_id]

    def transition_execution(
        self,
        execution_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        execution = self.executions[execution_id]
        transition = EXECUTION_STATE_MACHINE.transition(
            execution_id,
            execution.status,
            to_state,
            reason=reason,
            metadata=metadata,
        )
        now = datetime.now(timezone.utc)
        self.executions[execution_id] = replace(
            execution,
            status=to_state,
            started_at=now if to_state == "RUNNING" and execution.started_at is None else execution.started_at,
            ended_at=now if to_state in {"SUCCEEDED", "FAILED"} else execution.ended_at,
        )
        self.transitions.append(transition)
        return transition

    def transition_node(
        self,
        execution_id: str,
        node_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        node = self._node(execution_id, node_id)
        transition = NODE_STATE_MACHINE.transition(
            node_aggregate_id(execution_id, node_id),
            node.status,
            to_state,
            reason=reason,
            metadata=metadata,
        )
        self.nodes[(execution_id, node_id)] = replace(node, status=to_state)
        self.transitions.append(transition)
        return transition

    def list_nodes(self, execution_id: str) -> tuple[NodeExecution, ...]:
        return tuple(
            sorted(
                (item for item in self.nodes.values() if item.execution_id == execution_id),
                key=lambda item: item.node_id,
            )
        )

    def list_attempts(
        self, execution_id: str, node_id: str
    ) -> tuple[NodeAttempt, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.attempts.values()
                    if item.execution_id == execution_id and item.node_id == node_id
                ),
                key=lambda item: item.attempt_no,
            )
        )

    def _node(self, execution_id: str, node_id: str) -> NodeExecution:
        try:
            return self.nodes[(execution_id, node_id)]
        except KeyError as error:
            raise NodeNotInExecutionIR(node_id) from error

    def _transition_node(self, node: NodeExecution, to_state: str) -> NodeExecution:
        transition = NODE_STATE_MACHINE.transition(
            node_aggregate_id(node.execution_id, node.node_id),
            node.status,
            to_state,
        )
        self.transitions.append(transition)
        changed = replace(node, status=to_state)
        self.nodes[(node.execution_id, node.node_id)] = changed
        return changed

    def mark_node_ready(self, execution_id: str, node_id: str) -> NodeExecution:
        return self._transition_node(self._node(execution_id, node_id), "READY")

    def start_next_attempt(self, execution_id: str, node_id: str) -> NodeAttempt:
        node = self._node(execution_id, node_id)
        if node.status not in {"READY", "RUNNING"}:
            raise ValueError("business Attempt requires READY or RUNNING Node")
        if node.current_attempt is not None:
            previous = self.attempts[(execution_id, node_id, node.current_attempt)]
            if previous.status not in {"SUCCEEDED", "FAILED"}:
                raise ValueError("previous Business Attempt is not terminal")
        attempt_no = (node.current_attempt or 0) + 1
        key = f"{execution_id}:{node_id}:{attempt_no}:invoke"
        attempt = NodeAttempt(
            execution_id,
            node_id,
            attempt_no,
            key,
            "RUNNING",
            started_at=datetime.now(timezone.utc),
        )
        self.attempts[(execution_id, node_id, attempt_no)] = attempt
        changed = replace(node, current_attempt=attempt_no)
        self.nodes[(execution_id, node_id)] = changed
        if node.status == "READY":
            self._transition_node(changed, "RUNNING")
        return attempt

    def _attempt(self, node: NodeExecution) -> NodeAttempt:
        if node.current_attempt is None:
            raise ValueError("Node has no current Business Attempt")
        return self.attempts[(node.execution_id, node.node_id, node.current_attempt)]

    def record_technical_failure(
        self,
        execution_id: str,
        node_id: str,
        *,
        max_retries: int,
        error_code: str,
        error_signature: str,
        evidence: Mapping[str, object],
    ) -> NodeAttempt:
        node = self._node(execution_id, node_id)
        attempt = self._attempt(node)
        if node.status != "RUNNING" or attempt.status != "RUNNING":
            raise ValueError("technical failure requires RUNNING facts")
        if attempt.retry_count >= max_retries:
            return self.fail_attempt(
                execution_id,
                node_id,
                error_code=error_code,
                error_signature=error_signature,
                fail_execution=True,
            )
        changed = replace(
            attempt,
            status="RETRY_WAIT",
            retry_count=attempt.retry_count + 1,
            error_code=error_code,
            error_signature=error_signature,
            retry_evidence=dict(evidence),
        )
        self.attempts[(execution_id, node_id, attempt.attempt_no)] = changed
        self._transition_node(node, "RETRY_WAIT")
        return changed

    def resume_technical_retry(
        self, execution_id: str, node_id: str
    ) -> NodeAttempt:
        node = self._node(execution_id, node_id)
        attempt = self._attempt(node)
        if node.status != "RETRY_WAIT" or attempt.status != "RETRY_WAIT":
            raise ValueError("resume requires RETRY_WAIT facts")
        changed = replace(attempt, status="RUNNING")
        self.attempts[(execution_id, node_id, attempt.attempt_no)] = changed
        self._transition_node(node, "RUNNING")
        return changed

    def complete_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        complete_node: bool,
        complete_execution: bool,
    ) -> NodeAttempt:
        node = self._node(execution_id, node_id)
        attempt = self._attempt(node)
        if node.status != "RUNNING" or attempt.status != "RUNNING":
            raise ValueError("completion requires RUNNING facts")
        changed = replace(
            attempt, status="SUCCEEDED", ended_at=datetime.now(timezone.utc)
        )
        self.attempts[(execution_id, node_id, attempt.attempt_no)] = changed
        if complete_node:
            self._transition_node(node, "SUCCEEDED")
        if complete_execution:
            execution = self.executions[execution_id]
            transition = EXECUTION_STATE_MACHINE.transition(
                execution_id, execution.status, "SUCCEEDED"
            )
            self.transitions.append(transition)
            self.executions[execution_id] = replace(
                execution,
                status="SUCCEEDED",
                ended_at=datetime.now(timezone.utc),
            )
        return changed

    def fail_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        error_code: str,
        error_signature: str,
        fail_execution: bool,
        evidence: Mapping[str, object] | None = None,
    ) -> NodeAttempt:
        node = self._node(execution_id, node_id)
        attempt = self._attempt(node)
        changed = replace(
            attempt,
            status="FAILED",
            error_code=error_code,
            error_signature=error_signature,
            retry_evidence=dict(evidence or {}),
            ended_at=datetime.now(timezone.utc),
        )
        self.attempts[(execution_id, node_id, attempt.attempt_no)] = changed
        self._transition_node(node, "FAILED")
        if fail_execution:
            execution = self.executions[execution_id]
            transition = EXECUTION_STATE_MACHINE.transition(
                execution_id, execution.status, "FAILED"
            )
            self.transitions.append(transition)
            self.executions[execution_id] = replace(
                execution, status="FAILED", ended_at=datetime.now(timezone.utc)
            )
        return changed

    def request_execution_decision(
        self, execution_id: str, decision: str, reason: str
    ) -> Execution:
        target = {"PATCH": "PATCHING", "REPLAN": "REPLANNING", "FAIL": "FAILED"}.get(decision)
        if target is None:
            raise ValueError("unsupported decision")
        execution = self.executions[execution_id]
        transition = EXECUTION_STATE_MACHINE.transition(
            execution_id, execution.status, target, reason=reason
        )
        self.transitions.append(transition)
        changed = replace(execution, status=target)
        self.executions[execution_id] = changed
        return changed

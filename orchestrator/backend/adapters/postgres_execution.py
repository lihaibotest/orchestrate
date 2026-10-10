from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from adapters.postgres_self_test import PostgresSelfTestRepository
from domain.artifact import ExpectedArtifactOutput, WholeArtifactBinding
from domain.execution_control import (
    ExecutionIdempotencyConflict,
    NodeNotInExecutionIR,
    RetryLimitExceeded,
    RuntimeNodeDefinition,
    WorkflowRuntimeDefinition,
)
from domain.execution_ir import thaw_json
from domain.persistence import Execution, NodeAttempt, NodeExecution
from domain.state_machines import (
    EXECUTION_STATE_MACHINE,
    NODE_STATE_MACHINE,
    StateTransition,
    node_aggregate_id,
)


class PostgresExecutionRepository(PostgresSelfTestRepository):
    """PostgreSQL authority for formal Execution, Node, and Attempt facts."""

    @staticmethod
    def _definition_payload(definition: WorkflowRuntimeDefinition) -> list[dict[str, object]]:
        return [
            {
                "node_id": item.node_id,
                "agent_id": item.agent_id,
                "agent_version": item.agent_version,
                "credential_ref": item.credential_ref,
                "max_retries": item.max_retries,
                "backoff_seconds": item.backoff_seconds,
                "timeout_seconds": item.timeout_seconds,
                "depends_on": list(item.depends_on),
                "expected_outputs": [
                    {
                        "output_name": output.output_name,
                        "artifact_type": output.artifact_type,
                        "media_type": output.media_type,
                        "schema_ref": output.schema_ref,
                        "schema": thaw_json(output.schema),
                        "required": output.required,
                        "min_size_bytes": output.min_size_bytes,
                        "max_size_bytes": output.max_size_bytes,
                    }
                    for output in item.expected_outputs
                ],
                "input_bindings": [
                    {
                        "input_name": binding.input_name,
                        "source_node_id": binding.source_node_id,
                        "source_output_name": binding.source_output_name,
                        "binding_mode": binding.binding_mode,
                    }
                    for binding in item.input_bindings
                ],
            }
            for item in definition.nodes
        ]

    def save_runtime_definition(
        self, definition: WorkflowRuntimeDefinition
    ) -> WorkflowRuntimeDefinition:
        payload = self._definition_payload(definition)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash,nodes FROM workflow_runtime_definition "
                "WHERE workflow_family_id=%s AND workflow_version=%s FOR UPDATE",
                (definition.workflow_family_id, definition.workflow_version),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if (str(existing[0]), str(existing[1]), existing[2]) != (
                    definition.registry_snapshot_id,
                    definition.ir_hash,
                    payload,
                ):
                    raise ExecutionIdempotencyConflict(
                        "Workflow runtime definition is immutable"
                    )
                return definition
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash FROM workflow_version "
                "WHERE workflow_family_id=%s AND version=%s",
                (definition.workflow_family_id, definition.workflow_version),
            )
            version = cursor.fetchone()
            if version is None:
                raise KeyError("Workflow Version not found")
            if (str(version[0]), str(version[1])) != (
                definition.registry_snapshot_id,
                definition.ir_hash,
            ):
                raise ValueError("runtime definition differs from Workflow Version")
            cursor.execute(
                "INSERT INTO workflow_runtime_definition("
                "workflow_family_id,workflow_version,registry_snapshot_id,ir_hash,nodes"
                ") VALUES (%s,%s,%s,%s,%s::jsonb)",
                (
                    definition.workflow_family_id,
                    definition.workflow_version,
                    definition.registry_snapshot_id,
                    definition.ir_hash,
                    self._json(payload),
                ),
            )
        return definition

    def get_runtime_definition(
        self, workflow_family_id: str, workflow_version: int, ir_hash: str
    ) -> WorkflowRuntimeDefinition:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash,nodes FROM workflow_runtime_definition "
                "WHERE workflow_family_id=%s AND workflow_version=%s",
                (workflow_family_id, workflow_version),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError("Workflow runtime definition not found")
            if str(row[1]) != ir_hash:
                raise ValueError("runtime definition IR hash mismatch")
            return WorkflowRuntimeDefinition(
                workflow_family_id,
                workflow_version,
                str(row[0]),
                str(row[1]),
                tuple(
                    RuntimeNodeDefinition(
                        str(item["node_id"]),
                        None if item.get("agent_id") is None else str(item["agent_id"]),
                        None if item.get("agent_version") is None else str(item["agent_version"]),
                        None if item.get("credential_ref") is None else str(item["credential_ref"]),
                        int(item.get("max_retries", 0)),
                        int(item.get("backoff_seconds", 0)),
                        None if item.get("timeout_seconds") is None else int(item["timeout_seconds"]),
                        tuple(str(value) for value in item.get("depends_on", ())),
                        tuple(
                            ExpectedArtifactOutput(
                                output_name=str(output["output_name"]),
                                artifact_type=str(output.get("artifact_type", "FILE")),
                                media_type=str(output.get("media_type", "application/octet-stream")),
                                schema_ref=None if output.get("schema_ref") is None else str(output["schema_ref"]),
                                schema=output.get("schema", {}),
                                required=bool(output.get("required", True)),
                                min_size_bytes=None if output.get("min_size_bytes") is None else int(output["min_size_bytes"]),
                                max_size_bytes=None if output.get("max_size_bytes") is None else int(output["max_size_bytes"]),
                            )
                            for output in item.get("expected_outputs", ())
                        ),
                        tuple(
                            WholeArtifactBinding(
                                input_name=str(binding["input_name"]),
                                source_node_id=str(binding["source_node_id"]),
                                source_output_name=str(binding["source_output_name"]),
                                binding_mode=str(binding.get("binding_mode", "WHOLE_ARTIFACT")),
                            )
                            for binding in item.get("input_bindings", ())
                        ),
                    )
                    for item in row[2]
                ),
            )

    @staticmethod
    def _execution_from_row(row: tuple[object, ...]) -> Execution:
        return Execution(
            execution_id=str(row[0]),
            workflow_family_id=str(row[1]),
            workflow_version=int(row[2]),
            tenant_id=str(row[3]),
            status=str(row[4]),
            correlation_id=None if row[5] is None else str(row[5]),
            client_request_id=str(row[6]),
            n8n_execution_id=None if row[7] is None else str(row[7]),
            registry_snapshot_id=str(row[8]),
            ir_hash=str(row[9]),
            workflow_hash=str(row[10]),
            execution_mode=str(row[11]),
            request_hash=str(row[12]),
            operation_key=str(row[13]),
            engine=str(row[14]),
            engine_workflow_id=str(row[15]),
            engine_execution_id=None if row[16] is None else str(row[16]),
            engine_status=None if row[17] is None else str(row[17]),
            started_at=row[18],  # type: ignore[arg-type]
            ended_at=row[19],  # type: ignore[arg-type]
        )

    @staticmethod
    def _execution_select() -> str:
        return (
            "SELECT execution_id,workflow_family_id,workflow_version,tenant_id,status,"
            "correlation_id,client_request_id,n8n_execution_id,registry_snapshot_id,"
            "ir_hash,workflow_hash,execution_mode,request_hash,operation_key,engine,"
            "engine_workflow_id,engine_execution_id,engine_status,started_at,ended_at "
            "FROM execution "
        )

    def _load_execution(self, cursor: object, execution_id: str, *, lock: bool) -> Execution:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(  # type: ignore[attr-defined]
            self._execution_select() + "WHERE execution_id=%s" + suffix,
            (execution_id,),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise KeyError(f"Execution not found: {execution_id}")
        return self._execution_from_row(row)

    def begin_execution(
        self,
        execution: Execution,
        definition: WorkflowRuntimeDefinition,
    ) -> tuple[Execution, bool]:
        if execution.execution_mode != "PRODUCTION":
            raise ValueError("formal Execution must use PRODUCTION mode")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT tenant_id FROM workflow_family WHERE workflow_family_id=%s",
                (execution.workflow_family_id,),
            )
            family = cursor.fetchone()
            if family is None:
                raise KeyError(f"Workflow Family not found: {execution.workflow_family_id}")
            if str(family[0]) != execution.tenant_id:
                raise ValueError("Execution tenant does not own Workflow Family")
            cursor.execute(
                "INSERT INTO execution("
                "execution_id,workflow_family_id,workflow_version,tenant_id,correlation_id,"
                "client_request_id,status,registry_snapshot_id,ir_hash,workflow_hash,"
                "execution_mode,request_hash,operation_key,engine,engine_workflow_id"
                ") VALUES (%s,%s,%s,%s,%s,%s,'CREATED',%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    execution.execution_id,
                    execution.workflow_family_id,
                    execution.workflow_version,
                    execution.tenant_id,
                    execution.correlation_id,
                    execution.client_request_id,
                    execution.registry_snapshot_id,
                    execution.ir_hash,
                    execution.workflow_hash,
                    execution.execution_mode,
                    execution.request_hash,
                    execution.operation_key,
                    execution.engine,
                    execution.engine_workflow_id,
                ),
            )
            created = cursor.rowcount == 1
            stored = self._load_execution(cursor, execution.execution_id, lock=True)
            immutable = (
                stored.tenant_id,
                stored.workflow_family_id,
                stored.workflow_version,
                stored.client_request_id,
                stored.registry_snapshot_id,
                stored.ir_hash,
                stored.workflow_hash,
                stored.execution_mode,
                stored.request_hash,
                stored.operation_key,
                stored.engine,
                stored.engine_workflow_id,
            )
            observed = (
                execution.tenant_id,
                execution.workflow_family_id,
                execution.workflow_version,
                execution.client_request_id,
                execution.registry_snapshot_id,
                execution.ir_hash,
                execution.workflow_hash,
                execution.execution_mode,
                execution.request_hash,
                execution.operation_key,
                execution.engine,
                execution.engine_workflow_id,
            )
            if immutable != observed:
                raise ExecutionIdempotencyConflict(
                    "execution request identity is bound to different semantics"
                )
            if created:
                for node in definition.nodes:
                    cursor.execute(
                        "INSERT INTO execution_node_definition("
                        "execution_id,node_id,agent_id,agent_version,credential_ref,"
                        "max_retries,backoff_seconds,timeout_seconds"
                        ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            execution.execution_id,
                            node.node_id,
                            node.agent_id,
                            node.agent_version,
                            node.credential_ref,
                            node.max_retries,
                            node.backoff_seconds,
                            node.timeout_seconds,
                        ),
                    )
                    cursor.execute(
                        "INSERT INTO node_execution("
                        "execution_id,node_id,status,agent_id,agent_version"
                        ") VALUES (%s,%s,'PENDING',%s,%s)",
                        (
                            execution.execution_id,
                            node.node_id,
                            node.agent_id,
                            node.agent_version,
                        ),
                    )
            return stored, created

    def bind_engine_execution(
        self,
        execution_id: str,
        *,
        engine_execution_id: str,
        engine_status: str,
    ) -> Execution:
        with self._transaction() as connection:
            cursor = connection.cursor()
            execution = self._load_execution(cursor, execution_id, lock=True)
            if execution.engine_execution_id is not None:
                if execution.engine_execution_id != engine_execution_id:
                    raise ExecutionIdempotencyConflict(
                        "Execution is already bound to another engine execution"
                    )
                return execution
            transition = EXECUTION_STATE_MACHINE.transition(
                execution_id,
                execution.status,
                "RUNNING",
                reason="workflow engine execution accepted",
                metadata={"engine": execution.engine},
            )
            cursor.execute(
                "UPDATE execution SET engine_execution_id=%s,n8n_execution_id=%s,"
                "engine_status=%s,status='RUNNING',started_at=COALESCE(started_at,now()),"
                "updated_at=now() WHERE execution_id=%s",
                (engine_execution_id, engine_execution_id, engine_status, execution_id),
            )
            self._record_transition(cursor, transition)
            return replace(
                execution,
                status="RUNNING",
                n8n_execution_id=engine_execution_id,
                engine_execution_id=engine_execution_id,
                engine_status=engine_status,
            )

    def get_execution(self, tenant_id: str, execution_id: str) -> Execution:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                self._execution_select() + "WHERE tenant_id=%s AND execution_id=%s",
                (tenant_id, execution_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Execution not found: {execution_id}")
            return self._execution_from_row(row)

    def get_execution_by_id(self, execution_id: str) -> Execution:
        with self._transaction() as connection:
            return self._load_execution(connection.cursor(), execution_id, lock=False)

    @staticmethod
    def _node_from_row(row: tuple[object, ...]) -> NodeExecution:
        return NodeExecution(
            str(row[0]), str(row[1]), str(row[2]),
            None if row[3] is None else str(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else int(row[5]),
            None if row[6] is None else str(row[6]),
        )

    def list_nodes(self, execution_id: str) -> tuple[NodeExecution, ...]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT execution_id,node_id,status,agent_id,agent_version,current_attempt,"
                "checkpoint_id FROM node_execution WHERE execution_id=%s ORDER BY node_id",
                (execution_id,),
            )
            return tuple(self._node_from_row(row) for row in cursor.fetchall())

    @staticmethod
    def _attempt_from_row(row: tuple[object, ...]) -> NodeAttempt:
        evidence = thaw_json(row[11])
        if not isinstance(evidence, dict):
            raise TypeError("retry evidence must be an object")
        return NodeAttempt(
            str(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4]), int(row[5]),
            None if row[6] is None else str(row[6]),
            None if row[7] is None else str(row[7]),
            row[8], row[9], row[10], evidence,  # type: ignore[arg-type]
        )

    @staticmethod
    def _attempt_select() -> str:
        return (
            "SELECT execution_id,node_id,attempt_no,idempotency_key,status,retry_count,"
            "error_code,error_signature,started_at,ended_at,next_retry_at,retry_evidence "
            "FROM node_attempt "
        )

    def list_attempts(
        self, execution_id: str, node_id: str
    ) -> tuple[NodeAttempt, ...]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                self._attempt_select()
                + "WHERE execution_id=%s AND node_id=%s ORDER BY attempt_no",
                (execution_id, node_id),
            )
            return tuple(self._attempt_from_row(row) for row in cursor.fetchall())

    def _load_node(self, cursor: object, execution_id: str, node_id: str) -> NodeExecution:
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT execution_id,node_id,status,agent_id,agent_version,current_attempt,"
            "checkpoint_id FROM node_execution WHERE execution_id=%s AND node_id=%s FOR UPDATE",
            (execution_id, node_id),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise NodeNotInExecutionIR(f"Node is not part of this Execution: {node_id}")
        return self._node_from_row(row)

    def _transition_node_tx(
        self, cursor: object, node: NodeExecution, to_state: str, reason: str
    ) -> NodeExecution:
        transition = NODE_STATE_MACHINE.transition(
            node_aggregate_id(node.execution_id, node.node_id),
            node.status,
            to_state,
            reason=reason,
            metadata={"attempt_no": node.current_attempt},
        )
        cursor.execute(  # type: ignore[attr-defined]
            "UPDATE node_execution SET status=%s,updated_at=now() "
            "WHERE execution_id=%s AND node_id=%s",
            (to_state, node.execution_id, node.node_id),
        )
        self._record_transition(cursor, transition)
        return replace(node, status=to_state)

    def mark_node_ready(self, execution_id: str, node_id: str) -> NodeExecution:
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            return self._transition_node_tx(cursor, node, "READY", "Node dependencies satisfied")

    def start_next_attempt(self, execution_id: str, node_id: str) -> NodeAttempt:
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            if node.status not in {"READY", "RUNNING"}:
                raise ValueError("business Attempt requires READY or RUNNING Node")
            if node.current_attempt is not None:
                cursor.execute(
                    "SELECT status FROM node_attempt WHERE execution_id=%s AND node_id=%s "
                    "AND attempt_no=%s",
                    (execution_id, node_id, node.current_attempt),
                )
                previous = cursor.fetchone()
                if previous is None or str(previous[0]) not in {"SUCCEEDED", "FAILED"}:
                    raise ValueError("previous Business Attempt is not terminal")
            attempt_no = (node.current_attempt or 0) + 1
            idempotency_key = f"{execution_id}:{node_id}:{attempt_no}:invoke"
            cursor.execute(
                "INSERT INTO node_attempt("
                "execution_id,node_id,attempt_no,idempotency_key,status,retry_count,started_at"
                ") VALUES (%s,%s,%s,%s,'RUNNING',0,now())",
                (execution_id, node_id, attempt_no, idempotency_key),
            )
            cursor.execute(
                "UPDATE node_execution SET current_attempt=%s,updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s",
                (attempt_no, execution_id, node_id),
            )
            if node.status == "READY":
                self._transition_node_tx(
                    cursor,
                    replace(node, current_attempt=attempt_no),
                    "RUNNING",
                    "Business Attempt started",
                )
            return NodeAttempt(
                execution_id, node_id, attempt_no, idempotency_key, "RUNNING", 0
            )

    def _current_attempt(
        self, cursor: object, node: NodeExecution
    ) -> NodeAttempt:
        if node.current_attempt is None:
            raise ValueError("Node has no current Business Attempt")
        cursor.execute(  # type: ignore[attr-defined]
            self._attempt_select()
            + "WHERE execution_id=%s AND node_id=%s AND attempt_no=%s FOR UPDATE",
            (node.execution_id, node.node_id, node.current_attempt),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise ValueError("Node current_attempt has no matching Attempt")
        return self._attempt_from_row(row)

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
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            attempt = self._current_attempt(cursor, node)
            if node.status != "RUNNING" or attempt.status != "RUNNING":
                raise ValueError("technical failure requires RUNNING Node and Attempt")
            if attempt.retry_count >= max_retries:
                return self._fail_attempt_tx(
                    cursor,
                    node,
                    attempt,
                    error_code,
                    error_signature,
                    fail_execution=True,
                )
            next_count = attempt.retry_count + 1
            cursor.execute(
                "UPDATE node_attempt SET status='RETRY_WAIT',retry_count=%s,error_code=%s,"
                "error_signature=%s,retry_evidence=%s::jsonb,updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s AND attempt_no=%s",
                (
                    next_count,
                    error_code,
                    error_signature,
                    self._json(dict(evidence)),
                    execution_id,
                    node_id,
                    attempt.attempt_no,
                ),
            )
            self._transition_node_tx(cursor, node, "RETRY_WAIT", "technical retry scheduled")
            return replace(
                attempt,
                status="RETRY_WAIT",
                retry_count=next_count,
                error_code=error_code,
                error_signature=error_signature,
                retry_evidence=dict(evidence),
            )

    def resume_technical_retry(
        self, execution_id: str, node_id: str
    ) -> NodeAttempt:
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            attempt = self._current_attempt(cursor, node)
            if node.status != "RETRY_WAIT" or attempt.status != "RETRY_WAIT":
                raise ValueError("resume requires RETRY_WAIT Node and Attempt")
            cursor.execute(
                "UPDATE node_attempt SET status='RUNNING',updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s AND attempt_no=%s",
                (execution_id, node_id, attempt.attempt_no),
            )
            self._transition_node_tx(cursor, node, "RUNNING", "technical retry resumed")
            return replace(attempt, status="RUNNING")

    def _transition_execution_tx(
        self, cursor: object, execution: Execution, to_state: str, reason: str
    ) -> Execution:
        transition = EXECUTION_STATE_MACHINE.transition(
            execution.execution_id,
            execution.status,
            to_state,
            reason=reason,
        )
        cursor.execute(  # type: ignore[attr-defined]
            "UPDATE execution SET status=%s,updated_at=now(),"
            "ended_at=CASE WHEN %s IN ('SUCCEEDED','FAILED') THEN now() ELSE ended_at END "
            "WHERE execution_id=%s",
            (to_state, to_state, execution.execution_id),
        )
        self._record_transition(cursor, transition)
        return replace(execution, status=to_state)

    def complete_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        complete_node: bool,
        complete_execution: bool,
    ) -> NodeAttempt:
        if complete_execution and not complete_node:
            raise ValueError("Execution completion requires Node completion")
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            attempt = self._current_attempt(cursor, node)
            if node.status != "RUNNING" or attempt.status != "RUNNING":
                raise ValueError("completion requires RUNNING Node and Attempt")
            cursor.execute(
                "UPDATE node_attempt SET status='SUCCEEDED',ended_at=now(),updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s AND attempt_no=%s",
                (execution_id, node_id, attempt.attempt_no),
            )
            if complete_node:
                self._transition_node_tx(cursor, node, "SUCCEEDED", "Business Attempt succeeded")
            if complete_execution:
                execution = self._load_execution(cursor, execution_id, lock=True)
                self._transition_execution_tx(
                    cursor, execution, "SUCCEEDED", "Execution completion criteria satisfied"
                )
            return replace(attempt, status="SUCCEEDED")

    def _fail_attempt_tx(
        self,
        cursor: object,
        node: NodeExecution,
        attempt: NodeAttempt,
        error_code: str,
        error_signature: str,
        *,
        fail_execution: bool,
        evidence: Mapping[str, object] | None = None,
    ) -> NodeAttempt:
        cursor.execute(  # type: ignore[attr-defined]
            "UPDATE node_attempt SET status='FAILED',error_code=%s,error_signature=%s,retry_evidence=%s::jsonb,"
            "ended_at=now(),updated_at=now() WHERE execution_id=%s AND node_id=%s "
            "AND attempt_no=%s",
            (error_code, error_signature, self._json(dict(evidence or {})), node.execution_id, node.node_id, attempt.attempt_no),
        )
        self._transition_node_tx(cursor, node, "FAILED", "Business Attempt failed")
        if fail_execution:
            execution = self._load_execution(cursor, node.execution_id, lock=True)
            self._transition_execution_tx(
                cursor, execution, "FAILED", "Node execution failed"
            )
        return replace(
            attempt,
            status="FAILED",
            error_code=error_code,
            error_signature=error_signature,
            retry_evidence=dict(evidence or {}),
        )

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
        with self._transaction() as connection:
            cursor = connection.cursor()
            node = self._load_node(cursor, execution_id, node_id)
            attempt = self._current_attempt(cursor, node)
            if node.status != "RUNNING" or attempt.status != "RUNNING":
                raise ValueError("failure requires RUNNING Node and Attempt")
            return self._fail_attempt_tx(
                cursor,
                node,
                attempt,
                error_code,
                error_signature,
                fail_execution=fail_execution,
                evidence=evidence,
            )

    def request_execution_decision(
        self, execution_id: str, decision: str, reason: str
    ) -> Execution:
        target = {"PATCH": "PATCHING", "REPLAN": "REPLANNING", "FAIL": "FAILED"}.get(decision)
        if target is None:
            raise ValueError("decision must be PATCH, REPLAN, or FAIL")
        with self._transaction() as connection:
            cursor = connection.cursor()
            execution = self._load_execution(cursor, execution_id, lock=True)
            return self._transition_execution_tx(cursor, execution, target, reason)

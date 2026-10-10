from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterator, Mapping

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
from domain.runtime_events import RuntimeEventConflict, RuntimeObservation
from domain.agent_preparation import (
    AgentBinding as PreparedAgentBinding,
    RegistrySnapshot as PreparedRegistrySnapshot,
    SnapshotCreationFailed,
    thaw_json,
)
from infrastructure.migrations import PostgresMigrationRunner, validate_schema_name
from domain.state_machines import (
    AggregateNotFoundError,
    EXECUTION_STATE_MACHINE,
    NODE_STATE_MACHINE,
    WORKFLOW_VERSION_STATE_MACHINE,
    StateTransition,
    node_aggregate_id,
    workflow_version_aggregate_id,
)


class PostgresUnavailable(RuntimeError):
    pass


class PostgresControlPlaneRepository:
    """PostgreSQL adapter for control-plane facts; no PostgreSQL types leak to Domain."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "orchestrator",
        migrations_dir: Path | None = None,
        connection_factory: Any | None = None,
    ) -> None:
        if not dsn.strip() and connection_factory is None:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.schema = validate_schema_name(schema)
        self.migrations_dir = migrations_dir or Path(__file__).resolve().parents[2] / "migrations"
        self._connection_factory = connection_factory

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg2  # type: ignore
        except ImportError as error:
            raise PostgresUnavailable(
                "psycopg2-binary is required for PostgreSQL persistence"
            ) from error
        connection = psycopg2.connect(self.dsn)
        cursor = connection.cursor()
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        cursor.execute(f'SET search_path TO "{self.schema}"')
        cursor.close()
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{self.schema}"')
            cursor.close()
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(cursor: Any) -> dict[str, object] | None:
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
        return dict(zip(columns, row))

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def migrate(self, *, direction: str = "up") -> int:
        return PostgresMigrationRunner(
            self._connect, self.migrations_dir, self.schema
        ).migrate(direction=direction)

    def health_probe(self) -> bool:
        try:
            with self._transaction() as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
        except Exception:
            return False

    def create_workflow_family(self, family: WorkflowFamily) -> WorkflowFamily:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO workflow_family(workflow_family_id,tenant_id,task_id,cmm_context_id,status) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING workflow_family_id",
                (family.workflow_family_id, family.tenant_id, family.task_id, family.cmm_context_id, family.status),
            )
            cursor.fetchone()
        return family

    def create_registry_snapshot(self, snapshot: RegistrySnapshot) -> RegistrySnapshot:
        if snapshot.payload_ref is None and snapshot.payload is None:
            raise ValueError("registry snapshot requires payload_ref or payload")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO registry_snapshot(snapshot_id,workflow_family_id,snapshot_hash,payload_ref,payload_json) "
                "VALUES (%s,%s,%s,%s,%s::jsonb)",
                (snapshot.snapshot_id, snapshot.workflow_family_id, snapshot.snapshot_hash,
                 snapshot.payload_ref, self._json(snapshot.payload) if snapshot.payload is not None else None),
            )
            cursor.fetchone() if cursor.description else None
        return snapshot

    def save_registry_snapshot(
        self, snapshot: PreparedRegistrySnapshot
    ) -> PreparedRegistrySnapshot:
        payload = snapshot.content_payload()
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT snapshot_id,payload_json,hash_algorithm,created_at "
                "FROM registry_snapshot WHERE workflow_family_id=%s AND snapshot_hash=%s FOR UPDATE",
                (snapshot.workflow_family_id, snapshot.snapshot_hash),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing[1] != payload or existing[2] != snapshot.hash_algorithm:
                    raise SnapshotCreationFailed("Registry Snapshot hash collision")
                return replace(snapshot, snapshot_id=str(existing[0]), created_at=existing[3])
            cursor.execute(
                "INSERT INTO registry_snapshot("
                "snapshot_id,workflow_family_id,snapshot_hash,payload_json,hash_algorithm"
                ") VALUES (%s,%s,%s,%s::jsonb,%s) RETURNING created_at",
                (
                    snapshot.snapshot_id,
                    snapshot.workflow_family_id,
                    snapshot.snapshot_hash,
                    self._json(payload),
                    snapshot.hash_algorithm,
                ),
            )
            created_at = cursor.fetchone()[0]
            for binding in snapshot.bindings:
                cursor.execute(
                    "INSERT INTO agent_snapshot_binding("
                    "snapshot_id,requirement_id,agent_id,version,endpoint,capability,"
                    "input_schema,output_schema,credential_ref,invocation_policy"
                    ") VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)",
                    (
                        snapshot.snapshot_id,
                        binding.requirement_id,
                        binding.agent_id,
                        binding.version,
                        binding.endpoint,
                        binding.capability,
                        self._json(thaw_json(binding.input_schema)),
                        self._json(thaw_json(binding.output_schema)),
                        binding.credential_ref,
                        self._json(thaw_json(binding.invocation_policy)),
                    ),
                )
            return replace(snapshot, created_at=created_at)

    def get_registry_snapshot(self, snapshot_id: str) -> PreparedRegistrySnapshot:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT workflow_family_id,snapshot_hash,hash_algorithm,created_at "
                "FROM registry_snapshot WHERE snapshot_id=%s",
                (snapshot_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Registry Snapshot not found: {snapshot_id}")
            cursor.execute(
                "SELECT requirement_id,agent_id,version,endpoint,capability,input_schema,"
                "output_schema,credential_ref,invocation_policy "
                "FROM agent_snapshot_binding WHERE snapshot_id=%s ORDER BY requirement_id",
                (snapshot_id,),
            )
            bindings = tuple(
                PreparedAgentBinding(
                    requirement_id=str(item[0]),
                    agent_id=str(item[1]),
                    version=str(item[2]),
                    endpoint=str(item[3]),
                    capability=str(item[4]),
                    input_schema=item[5],
                    output_schema=item[6],
                    credential_ref=str(item[7]),
                    invocation_policy=item[8],
                )
                for item in cursor.fetchall()
            )
            if not bindings:
                raise SnapshotCreationFailed(
                    f"Registry Snapshot has no bindings: {snapshot_id}"
                )
            return PreparedRegistrySnapshot(
                snapshot_id=snapshot_id,
                workflow_family_id=str(row[0]),
                bindings=bindings,
                snapshot_hash=str(row[1]),
                hash_algorithm=str(row[2]),
                created_at=row[3],
            )

    def create_workflow_version(self, version: WorkflowVersion) -> WorkflowVersion:
        if version.version < 1:
            raise ValueError("workflow version must be positive")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO workflow_version(workflow_family_id,version,parent_version,registry_snapshot_id,ir_hash,structure_ref,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (version.workflow_family_id, version.version, version.parent_version,
                 version.registry_snapshot_id, version.ir_hash, version.structure_ref, version.status),
            )
        return version

    def create_execution(self, execution: Execution) -> Execution:
        if not execution.client_request_id:
            raise ValueError("client_request_id is required for execution idempotency")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT execution_id,workflow_family_id,workflow_version,tenant_id,status,correlation_id,client_request_id,n8n_execution_id "
                "FROM execution WHERE tenant_id=%s AND workflow_family_id=%s AND workflow_version=%s AND client_request_id=%s",
                (execution.tenant_id, execution.workflow_family_id, execution.workflow_version, execution.client_request_id),
            )
            existing = self._row(cursor)
            if existing is not None:
                immutable = (existing["execution_id"], existing["workflow_family_id"], existing["workflow_version"], existing["tenant_id"], existing["client_request_id"])
                observed = (execution.execution_id, execution.workflow_family_id, execution.workflow_version, execution.tenant_id, execution.client_request_id)
                if immutable != observed:
                    raise ValueError("idempotency key is bound to a different execution")
                return execution
            cursor.execute(
                "INSERT INTO execution("
                "execution_id,workflow_family_id,workflow_version,tenant_id,correlation_id,"
                "client_request_id,n8n_execution_id,status,registry_snapshot_id,ir_hash,"
                "workflow_hash,execution_mode,request_hash,operation_key,engine,engine_workflow_id"
                ") SELECT %s,%s,%s,%s,%s,%s,%s,%s,version.registry_snapshot_id,version.ir_hash,"
                "COALESCE(publish.workflow_hash,version.ir_hash),'PRODUCTION',%s,%s,"
                "COALESCE(publish.engine,'legacy'),COALESCE(publish.engine_workflow_id,'legacy:unbound') "
                "FROM workflow_version AS version LEFT JOIN workflow_publish_record AS publish "
                "ON publish.workflow_family_id=version.workflow_family_id "
                "AND publish.workflow_version=version.version "
                "WHERE version.workflow_family_id=%s AND version.version=%s",
                (execution.execution_id, execution.workflow_family_id, execution.workflow_version, execution.tenant_id,
                 execution.correlation_id, execution.client_request_id, execution.n8n_execution_id, execution.status,
                 execution.request_hash or f"legacy:{execution.execution_id}",
                 execution.operation_key or f"legacy:{execution.execution_id}",
                 execution.workflow_family_id, execution.workflow_version),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"Workflow Version not found: {execution.workflow_family_id}/{execution.workflow_version}"
                )
        return execution

    def create_node_execution(self, node: NodeExecution) -> NodeExecution:
        with self._transaction() as connection:
            cursor = connection.cursor()
            agent_version = node.agent_version
            if node.agent_id is not None and agent_version is None:
                agent_version = "legacy:unknown"
            cursor.execute(
                "INSERT INTO execution_node_definition("
                "execution_id,node_id,agent_id,agent_version"
                ") VALUES (%s,%s,%s,%s)",
                (node.execution_id, node.node_id, node.agent_id, agent_version),
            )
            cursor.execute(
                "INSERT INTO node_execution("
                "execution_id,node_id,status,agent_id,agent_version,current_attempt,checkpoint_id"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    node.execution_id,
                    node.node_id,
                    node.status,
                    node.agent_id,
                    agent_version,
                    node.current_attempt,
                    node.checkpoint_id,
                ),
            )
        return node

    def create_attempt(self, attempt: NodeAttempt) -> NodeAttempt:
        if attempt.attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT current_attempt FROM node_execution "
                "WHERE execution_id=%s AND node_id=%s FOR UPDATE",
                (attempt.execution_id, attempt.node_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError("NodeExecution not found")
            expected = (0 if row[0] is None else int(row[0])) + 1
            if attempt.attempt_no != expected:
                raise ValueError("attempt_no must increase monotonically")
            cursor.execute(
                "INSERT INTO node_attempt("
                "execution_id,node_id,attempt_no,idempotency_key,status,retry_count,error_code,"
                "error_signature,started_at,ended_at,next_retry_at,retry_evidence"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    attempt.execution_id,
                    attempt.node_id,
                    attempt.attempt_no,
                    attempt.idempotency_key,
                    attempt.status,
                    attempt.retry_count,
                    attempt.error_code,
                    attempt.error_signature,
                    attempt.started_at,
                    attempt.ended_at,
                    attempt.next_retry_at,
                    self._json(attempt.retry_evidence),
                ),
            )
            cursor.execute(
                "UPDATE node_execution SET current_attempt=%s,updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s",
                (attempt.attempt_no, attempt.execution_id, attempt.node_id),
            )
        return attempt

    def register_artifact(self, artifact: ArtifactRef) -> ArtifactRef:
        if artifact.uri.strip().lower().startswith(("data:", "inline:")):
            raise ValueError("artifact body must be stored in object storage, not PostgreSQL")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO artifact_ref(artifact_id,execution_id,node_id,attempt_no,uri,content_hash,schema_id,schema_version,quality_status,reusable,input_fingerprint,permission_scope,metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (artifact.artifact_id, artifact.execution_id, artifact.node_id, artifact.attempt_no, artifact.uri,
                 artifact.content_hash, artifact.schema_id, artifact.schema_version, artifact.quality_status,
                 artifact.reusable, artifact.input_fingerprint, self._json(artifact.permission_scope or {}), self._json(artifact.metadata)),
            )
        return artifact

    def create_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO checkpoint(checkpoint_id,execution_id,node_id,artifact_refs,input_fingerprint,trusted) VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                (checkpoint.checkpoint_id, checkpoint.execution_id, checkpoint.node_id, self._json(checkpoint.artifact_refs), checkpoint.input_fingerprint, checkpoint.trusted),
            )
        return checkpoint

    def record_self_test(self, run: SelfTestRun) -> SelfTestRun:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO self_test_run("
                "self_test_id,workflow_family_id,workflow_version,attempt_no,level,passed,"
                "failure_stage,error_signature,repair_action,evidence,tenant_id,"
                "registry_snapshot_id,ir_hash,workflow_hash,status,current_stage,"
                "repair_attempt,idempotency_key,started_at,completed_at,evidence_refs"
                ") SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,"
                "family.tenant_id,version.registry_snapshot_id,version.ir_hash,version.ir_hash,"
                "%s,%s,%s,%s,now(),now(),'[]'::jsonb "
                "FROM workflow_version AS version JOIN workflow_family AS family "
                "ON family.workflow_family_id=version.workflow_family_id "
                "WHERE version.workflow_family_id=%s AND version.version=%s",
                (run.self_test_id, run.workflow_family_id, run.workflow_version, run.attempt_no, run.level, run.passed,
                 run.failure_stage, run.error_signature, self._json(run.repair_action or {}), self._json(run.evidence),
                 "PASSED" if run.passed else "FAILED", run.level, max(0, run.attempt_no - 1),
                 f"legacy:{run.self_test_id}", run.workflow_family_id, run.workflow_version),
            )
            if cursor.rowcount != 1:
                raise KeyError(
                    f"Workflow Version not found: {run.workflow_family_id}/{run.workflow_version}"
                )
        return run

    def record_runtime_event(self, event: RuntimeEvent) -> bool:
        with self._transaction() as connection:
            cursor = connection.cursor()
            payload_hash = event.payload_hash or __import__("hashlib").md5(
                self._json(event.payload).encode("utf-8")
            ).hexdigest()
            cursor.execute(
                "INSERT INTO runtime_event(event_id,execution_id,event_type,workflow_version,node_id,agent_id,attempt_no,trace_id,request_id,error_code,payload,occurred_at,tenant_id,source,correlation_id,external_reference,received_at,payload_hash,event_version,processing_status,processing_retry_count,processing_error_code,processing_error_message) "
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,COALESCE(%s,execution.tenant_id),%s,COALESCE(%s,execution.correlation_id),%s,%s,%s,%s,%s,%s,%s,%s "
                "FROM execution WHERE execution_id=%s ON CONFLICT DO NOTHING",
                (event.event_id, event.execution_id, event.event_type, event.workflow_version, event.node_id, event.agent_id,
                 event.attempt_no, event.trace_id, event.request_id, event.error_code, self._json(event.payload), event.occurred_at,
                 event.tenant_id or None, event.source, event.correlation_id or None, event.external_reference,
                 event.received_at, payload_hash,
                 event.version, event.processing_status, event.processing_retry_count, event.processing_error_code, event.processing_error_message,
                 event.execution_id),
            )
            return cursor.rowcount == 1

    def save_event(self, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]:
        created = self.record_runtime_event(event)
        if created:
            return event, True
        try:
            existing = self.get_event(event.tenant_id, event.event_id)
        except KeyError:
            if not event.external_reference:
                raise
            with self._transaction() as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT event_id FROM runtime_event WHERE tenant_id=%s AND source=%s AND external_reference=%s AND payload_hash=%s",
                    (event.tenant_id, event.source, event.external_reference, event.payload_hash),
                )
                row = cursor.fetchone()
            if row is None:
                raise RuntimeEventConflict("Runtime Event identity conflict")
            existing = self.get_event(event.tenant_id, str(row[0]))
        identity = (existing.execution_id, existing.event_type, existing.node_id, existing.attempt_no, existing.payload_hash)
        observed = (event.execution_id, event.event_type, event.node_id, event.attempt_no, event.payload_hash)
        if identity != observed:
            raise RuntimeEventConflict("event_id is bound to different semantics")
        return existing, False

    def get_event(self, tenant_id: str, event_id: str) -> RuntimeEvent:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT event_id,execution_id,event_type,workflow_version,occurred_at,node_id,agent_id,attempt_no,trace_id,request_id,error_code,payload,source,tenant_id,correlation_id,external_reference,received_at,payload_hash,event_version,processing_status,processing_retry_count,processing_error_code,processing_error_message "
                "FROM runtime_event WHERE tenant_id=%s AND event_id=%s",
                (tenant_id, event_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Runtime Event not found: {event_id}")
            return RuntimeEvent(*row[:12], source=row[12], tenant_id=row[13], correlation_id=row[14] or "",
                                external_reference=row[15], received_at=row[16], payload_hash=row[17],
                                version=int(row[18]), processing_status=row[19], processing_retry_count=int(row[20]),
                                processing_error_code=row[21], processing_error_message=row[22])

    def update_event_processing(
        self,
        tenant_id: str,
        event_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_retry: bool = False,
    ) -> RuntimeEvent:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE runtime_event SET processing_status=%s,processing_error_code=%s,processing_error_message=%s,processing_retry_count=processing_retry_count+%s "
                "WHERE tenant_id=%s AND event_id=%s",
                (status, error_code, error_message, int(increment_retry), tenant_id, event_id),
            )
        return self.get_event(tenant_id, event_id)

    def save_observation(self, observation: RuntimeObservation) -> tuple[RuntimeObservation, bool]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO runtime_observation(observation_id,event_id,tenant_id,correlation_id,execution_id,node_id,attempt_no,observed_state,source,confidence,external_reference,occurred_at,evidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (event_id,observed_state) DO NOTHING",
                (observation.observation_id, observation.event_id, observation.tenant_id, observation.correlation_id, observation.execution_id,
                 observation.node_id, observation.attempt_no, observation.observed_state, observation.source,
                 observation.confidence, observation.external_reference, observation.occurred_at,
                 self._json(observation.evidence)),
            )
            created = cursor.rowcount == 1
        return observation, created

    def list_events(self, tenant_id: str, execution_id: str) -> tuple[RuntimeEvent, ...]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT event_id,execution_id,event_type,workflow_version,occurred_at,node_id,agent_id,attempt_no,trace_id,request_id,error_code,payload,source,tenant_id,correlation_id,external_reference,received_at,payload_hash,event_version,processing_status,processing_retry_count,processing_error_code,processing_error_message FROM runtime_event WHERE tenant_id=%s AND execution_id=%s ORDER BY occurred_at,received_at,event_id",
                (tenant_id, execution_id),
            )
            return tuple(RuntimeEvent(*row[:12], source=row[12], tenant_id=row[13], correlation_id=row[14] or "",
                                      external_reference=row[15], received_at=row[16], payload_hash=row[17], version=int(row[18]),
                                      processing_status=row[19], processing_retry_count=int(row[20]), processing_error_code=row[21], processing_error_message=row[22]) for row in cursor.fetchall())

    def list_observations(self, tenant_id: str, execution_id: str) -> tuple[RuntimeObservation, ...]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT observation_id,event_id,tenant_id,correlation_id,execution_id,observed_state,source,occurred_at,node_id,attempt_no,external_reference,confidence,evidence,created_at FROM runtime_observation WHERE tenant_id=%s AND execution_id=%s ORDER BY occurred_at,created_at,observation_id",
                (tenant_id, execution_id),
            )
            return tuple(RuntimeObservation(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], node_id=row[8], attempt_no=row[9], external_reference=row[10], confidence=row[11], evidence=row[12], created_at=row[13]) for row in cursor.fetchall())

    def enqueue_context_event(self, event: ContextOutboxEvent) -> bool:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO context_outbox(event_id,event_type,idempotency_key,payload,status) VALUES (%s,%s,%s,%s::jsonb,%s) ON CONFLICT (idempotency_key) DO NOTHING",
                (event.event_id, event.event_type, event.idempotency_key, self._json(event.payload), event.status),
            )
            return cursor.rowcount == 1

    def claim_outbox(self, *, limit: int = 50) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("outbox limit must be positive")
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT event_id,event_type,idempotency_key,payload FROM context_outbox "
                "WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= %s) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s",
                (now, limit),
            )
            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append({"event_id": row[0], "event_type": row[1], "idempotency_key": row[2], "payload": row[3]})
                cursor.execute(
                    "UPDATE context_outbox SET status='IN_FLIGHT',attempts=attempts+1,lease_until=%s WHERE event_id=%s",
                    (now, row[0]),
                )
            return result

    @staticmethod
    def _record_transition(cursor: Any, transition: StateTransition) -> StateTransition:
        cursor.execute(
            "INSERT INTO state_transition_history(aggregate_type,aggregate_id,from_state,to_state,reason,metadata) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb) RETURNING transition_id,created_at",
            (
                transition.aggregate_type,
                transition.aggregate_id,
                transition.from_state,
                transition.to_state,
                transition.reason,
                PostgresControlPlaneRepository._json(transition.metadata),
            ),
        )
        transition_id, created_at = cursor.fetchone()
        return StateTransition(
            aggregate_type=transition.aggregate_type,
            aggregate_id=transition.aggregate_id,
            from_state=transition.from_state,
            to_state=transition.to_state,
            reason=transition.reason,
            metadata=dict(transition.metadata),
            transition_id=int(transition_id),
            created_at=created_at,
        )

    def transition_workflow_version(
        self,
        workflow_family_id: str,
        version: int,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        aggregate_id = workflow_version_aggregate_id(workflow_family_id, version)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT status FROM workflow_version "
                "WHERE workflow_family_id=%s AND version=%s FOR UPDATE",
                (workflow_family_id, version),
            )
            row = cursor.fetchone()
            if row is None:
                raise AggregateNotFoundError("WORKFLOW_VERSION", aggregate_id)
            transition = WORKFLOW_VERSION_STATE_MACHINE.transition(
                aggregate_id,
                str(row[0]),
                to_state,
                reason=reason,
                metadata=metadata,
            )
            cursor.execute(
                "UPDATE workflow_version SET status=%s "
                "WHERE workflow_family_id=%s AND version=%s",
                (to_state, workflow_family_id, version),
            )
            return self._record_transition(cursor, transition)

    def transition_execution(
        self,
        execution_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT status FROM execution WHERE execution_id=%s FOR UPDATE",
                (execution_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AggregateNotFoundError("EXECUTION", execution_id)
            transition = EXECUTION_STATE_MACHINE.transition(
                execution_id,
                str(row[0]),
                to_state,
                reason=reason,
                metadata=metadata,
            )
            cursor.execute(
                "UPDATE execution SET status=%s,updated_at=now(),"
                "started_at=CASE WHEN %s='RUNNING' THEN COALESCE(started_at,now()) ELSE started_at END,"
                "ended_at=CASE WHEN %s IN ('SUCCEEDED','FAILED') THEN now() ELSE ended_at END "
                "WHERE execution_id=%s",
                (to_state, to_state, to_state, execution_id),
            )
            return self._record_transition(cursor, transition)

    def transition_node(
        self,
        execution_id: str,
        node_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        aggregate_id = node_aggregate_id(execution_id, node_id)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT status FROM node_execution "
                "WHERE execution_id=%s AND node_id=%s FOR UPDATE",
                (execution_id, node_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise AggregateNotFoundError("NODE", aggregate_id)
            transition = NODE_STATE_MACHINE.transition(
                aggregate_id,
                str(row[0]),
                to_state,
                reason=reason,
                metadata=metadata,
            )
            cursor.execute(
                "UPDATE node_execution SET status=%s,updated_at=now() "
                "WHERE execution_id=%s AND node_id=%s",
                (to_state, execution_id, node_id),
            )
            return self._record_transition(cursor, transition)

    def list_state_transitions(
        self, aggregate_type: str, aggregate_id: str
    ) -> list[StateTransition]:
        if aggregate_type not in {"WORKFLOW_VERSION", "EXECUTION", "NODE"}:
            raise ValueError("unsupported aggregate_type")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT transition_id,from_state,to_state,reason,metadata,created_at "
                "FROM state_transition_history "
                "WHERE aggregate_type=%s AND aggregate_id=%s ORDER BY transition_id",
                (aggregate_type, aggregate_id),
            )
            return [
                StateTransition(
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    from_state=str(row[1]),
                    to_state=str(row[2]),
                    reason=row[3],
                    metadata=row[4],
                    transition_id=int(row[0]),
                    created_at=row[5],
                )
                for row in cursor.fetchall()
            ]

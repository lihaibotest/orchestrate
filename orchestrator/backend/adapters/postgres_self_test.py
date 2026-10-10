from __future__ import annotations

from dataclasses import replace
import json

from adapters.postgres import PostgresControlPlaneRepository
from domain.execution_ir import thaw_json
from domain.persistence import WorkflowVersion
from domain.self_test import (
    ExecuteGateError,
    PublishGateError,
    PublishReservation,
    RepairRecord,
    SELF_TEST_STAGES,
    SelfTestRunResult,
    SelfTestStageResult,
    WorkflowTestIdentity,
)
from domain.state_machines import (
    WORKFLOW_VERSION_STATE_MACHINE,
    workflow_version_aggregate_id,
)
from ports.workflow_engine import CompiledWorkflow, WorkflowReference


class PostgresSelfTestRepository(PostgresControlPlaneRepository):
    """PostgreSQL release-gate adapter with transactional state transitions."""

    @staticmethod
    def _identity_from_row(row: tuple[object, ...]) -> WorkflowTestIdentity:
        return WorkflowTestIdentity(
            tenant_id=str(row[0]),
            workflow_family_id=str(row[1]),
            workflow_version=int(row[2]),
            registry_snapshot_id=str(row[3]),
            ir_hash=str(row[4]),
            status=str(row[5]),
            parent_version=None if row[6] is None else int(row[6]),
        )

    def _get_identity(self, cursor: object, family_id: str, version: int, *, lock: bool) -> WorkflowTestIdentity:
        suffix = " FOR UPDATE OF version" if lock else ""
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT family.tenant_id,version.workflow_family_id,version.version,"
            "version.registry_snapshot_id,version.ir_hash,version.status,version.parent_version "
            "FROM workflow_version AS version "
            "JOIN workflow_family AS family ON family.workflow_family_id=version.workflow_family_id "
            "WHERE version.workflow_family_id=%s AND version.version=%s" + suffix,
            (family_id, version),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise KeyError(f"Workflow Version not found: {family_id}/{version}")
        return self._identity_from_row(row)

    def get_workflow_identity(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> WorkflowTestIdentity:
        with self._transaction() as connection:
            cursor = connection.cursor()
            return self._get_identity(
                cursor,
                workflow_family_id,
                workflow_version,
                lock=False,
            )

    def _transition_workflow(
        self,
        cursor: object,
        identity: WorkflowTestIdentity,
        to_state: str,
        reason: str,
        metadata: dict[str, object],
    ) -> WorkflowTestIdentity:
        transition = WORKFLOW_VERSION_STATE_MACHINE.transition(
            workflow_version_aggregate_id(
                identity.workflow_family_id,
                identity.workflow_version,
            ),
            identity.status,
            to_state,
            reason=reason,
            metadata=metadata,
        )
        cursor.execute(  # type: ignore[attr-defined]
            "UPDATE workflow_version SET status=%s "
            "WHERE workflow_family_id=%s AND version=%s",
            (to_state, identity.workflow_family_id, identity.workflow_version),
        )
        cursor.execute(  # type: ignore[attr-defined]
            "INSERT INTO state_transition_history("
            "aggregate_type,aggregate_id,from_state,to_state,reason,metadata"
            ") VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (
                transition.aggregate_type,
                transition.aggregate_id,
                transition.from_state,
                transition.to_state,
                transition.reason,
                self._json(dict(transition.metadata)),
            ),
        )
        return replace(identity, status=to_state)

    @staticmethod
    def _stage_from_row(row: tuple[object, ...]) -> SelfTestStageResult:
        return SelfTestStageResult(
            stage=str(row[0]),
            status=str(row[1]),
            started_at=row[2],  # type: ignore[arg-type]
            completed_at=row[3],  # type: ignore[arg-type]
            duration_ms=int(row[4]),
            error_code=None if row[5] is None else str(row[5]),
            message=None if row[6] is None else str(row[6]),
            evidence=row[7],  # type: ignore[arg-type]
            evidence_refs=tuple(row[8]),  # type: ignore[arg-type]
            failure_signature=None if row[9] is None else str(row[9]),
        )

    def _load_run(self, cursor: object, self_test_id: str, *, lock: bool) -> SelfTestRunResult:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT self_test_id,tenant_id,workflow_family_id,workflow_version,"
            "registry_snapshot_id,ir_hash,workflow_hash,idempotency_key,started_at,status,"
            "current_stage,repair_attempt,completed_at,error_signature,evidence_refs "
            "FROM self_test_run WHERE self_test_id=%s" + suffix,
            (self_test_id,),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise KeyError(f"Self-Test not found: {self_test_id}")
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT stage,status,started_at,completed_at,duration_ms,error_code,message,"
            "evidence,evidence_refs,failure_signature FROM self_test_stage_result "
            "WHERE self_test_id=%s ORDER BY stage",
            (self_test_id,),
        )
        stages = tuple(self._stage_from_row(item) for item in cursor.fetchall())  # type: ignore[attr-defined]
        return SelfTestRunResult(
            self_test_id=str(row[0]),
            tenant_id=str(row[1]),
            workflow_family_id=str(row[2]),
            workflow_version=int(row[3]),
            registry_snapshot_id=str(row[4]),
            ir_hash=str(row[5]),
            workflow_hash=str(row[6]),
            idempotency_key=str(row[7]),
            started_at=row[8],  # type: ignore[arg-type]
            status=str(row[9]),
            current_stage=str(row[10]),
            repair_attempt=int(row[11]),
            completed_at=row[12],  # type: ignore[arg-type]
            failure_signature=None if row[13] is None else str(row[13]),
            stage_results=stages,
            evidence_refs=tuple(row[14]),  # type: ignore[arg-type]
        )

    def begin_self_test(
        self,
        run: SelfTestRunResult,
    ) -> tuple[SelfTestRunResult, bool]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT self_test_id FROM self_test_run "
                "WHERE tenant_id=%s AND idempotency_key=%s",
                (run.tenant_id, run.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                return self._load_run(cursor, str(existing[0]), lock=False), False

            identity = self._get_identity(
                cursor,
                run.workflow_family_id,
                run.workflow_version,
                lock=True,
            )
            expected = (
                identity.tenant_id,
                identity.registry_snapshot_id,
                identity.ir_hash,
            )
            observed = (run.tenant_id, run.registry_snapshot_id, run.ir_hash)
            if observed != expected:
                raise ValueError("Self-Test identity differs from Workflow Version")
            cursor.execute(
                "SELECT self_test_id FROM self_test_run "
                "WHERE workflow_family_id=%s AND workflow_version=%s AND status='RUNNING'",
                (run.workflow_family_id, run.workflow_version),
            )
            active = cursor.fetchone()
            if active is not None:
                return self._load_run(cursor, str(active[0]), lock=False), False
            metadata = {"self_test_id": run.self_test_id}
            if identity.status == "DRAFT":
                identity = self._transition_workflow(
                    cursor,
                    identity,
                    "VALIDATING",
                    "self-test validation started",
                    metadata,
                )
                identity = self._transition_workflow(
                    cursor,
                    identity,
                    "SELF_TESTING",
                    "self-test started",
                    metadata,
                )
            elif identity.status == "VALIDATING":
                identity = self._transition_workflow(
                    cursor,
                    identity,
                    "SELF_TESTING",
                    "self-test started",
                    metadata,
                )
            elif identity.status == "SELF_TEST_FAILED":
                cursor.execute(
                    "SELECT 1 FROM workflow_repair_record WHERE workflow_family_id=%s "
                    "AND source_workflow_version=%s AND repair_attempt=%s "
                    "AND status='RETRY_UNCHANGED'",
                    (
                        run.workflow_family_id,
                        run.workflow_version,
                        run.repair_attempt,
                    ),
                )
                if run.repair_attempt < 1 or cursor.fetchone() is None:
                    raise ValueError(
                        "failed Workflow Version requires an authorized repair retry"
                    )
                identity = self._transition_workflow(
                    cursor,
                    identity,
                    "SELF_TESTING",
                    "authorized self-test retry started",
                    metadata,
                )
            else:
                raise ValueError(
                    f"Workflow Version cannot start Self-Test: {identity.status}"
                )
            cursor.execute(
                "INSERT INTO self_test_run("
                "self_test_id,workflow_family_id,workflow_version,attempt_no,level,passed,"
                "evidence,tenant_id,registry_snapshot_id,ir_hash,workflow_hash,status,"
                "current_stage,repair_attempt,idempotency_key,started_at,evidence_refs"
                ") VALUES (%s,%s,%s,%s,%s,FALSE,'{}'::jsonb,%s,%s,%s,%s,'RUNNING',%s,%s,%s,%s,%s::jsonb)",
                (
                    run.self_test_id,
                    run.workflow_family_id,
                    run.workflow_version,
                    run.repair_attempt + 1,
                    run.current_stage,
                    run.tenant_id,
                    run.registry_snapshot_id,
                    run.ir_hash,
                    run.workflow_hash,
                    run.current_stage,
                    run.repair_attempt,
                    run.idempotency_key,
                    run.started_at,
                    self._json(list(run.evidence_refs)),
                ),
            )
            return run, True

    def complete_self_test(
        self,
        run: SelfTestRunResult,
        stage_results: tuple[SelfTestStageResult, ...],
    ) -> SelfTestRunResult:
        if tuple(run.stage_results) != tuple(stage_results):
            raise ValueError("stage result identity mismatch")
        if tuple(item.stage for item in stage_results) != SELF_TEST_STAGES:
            raise ValueError("completion requires exactly S0-S4 results")
        with self._transaction() as connection:
            cursor = connection.cursor()
            existing = self._load_run(cursor, run.self_test_id, lock=True)
            if existing.status != "RUNNING":
                return existing
            identity = self._get_identity(
                cursor,
                run.workflow_family_id,
                run.workflow_version,
                lock=True,
            )
            if identity.status != "SELF_TESTING":
                raise ValueError("Workflow Version left SELF_TESTING before completion")
            for item in stage_results:
                cursor.execute(
                    "INSERT INTO self_test_stage_result("
                    "self_test_id,stage,status,started_at,completed_at,duration_ms,error_code,"
                    "message,evidence,evidence_refs,failure_signature"
                    ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
                    (
                        run.self_test_id,
                        item.stage,
                        item.status,
                        item.started_at,
                        item.completed_at,
                        item.duration_ms,
                        item.error_code,
                        item.message,
                        self._json(item.evidence_dict()),
                        self._json(list(item.evidence_refs)),
                        item.failure_signature,
                    ),
                )
            summary = {
                "stages": [
                    {"stage": item.stage, "status": item.status, "error_code": item.error_code}
                    for item in stage_results
                ]
            }
            cursor.execute(
                "UPDATE self_test_run SET level=%s,passed=%s,failure_stage=%s,"
                "error_signature=%s,evidence=%s::jsonb,status=%s,current_stage=%s,"
                "completed_at=%s,evidence_refs=%s::jsonb WHERE self_test_id=%s",
                (
                    run.current_stage,
                    run.status == "PASSED",
                    run.current_stage if run.status == "FAILED" else None,
                    run.failure_signature,
                    self._json(summary),
                    run.status,
                    run.current_stage,
                    run.completed_at,
                    self._json(list(run.evidence_refs)),
                    run.self_test_id,
                ),
            )
            self._transition_workflow(
                cursor,
                identity,
                "VERIFIED" if run.status == "PASSED" else "SELF_TEST_FAILED",
                "self-test completed",
                {
                    "self_test_id": run.self_test_id,
                    "failure_signature": run.failure_signature,
                },
            )
            return run

    def get_self_test(self, self_test_id: str) -> SelfTestRunResult:
        with self._transaction() as connection:
            return self._load_run(connection.cursor(), self_test_id, lock=False)

    def latest_successful_self_test(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> SelfTestRunResult | None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT self_test_id FROM self_test_run WHERE workflow_family_id=%s "
                "AND workflow_version=%s AND status='PASSED' "
                "ORDER BY completed_at DESC,self_test_id DESC LIMIT 1",
                (workflow_family_id, workflow_version),
            )
            row = cursor.fetchone()
            return None if row is None else self._load_run(cursor, str(row[0]), lock=False)

    def record_repair(self, record: RepairRecord) -> RepairRecord:
        evidence = thaw_json(record.evidence)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT repair_id,status,target_workflow_version FROM workflow_repair_record "
                "WHERE self_test_id=%s AND repair_attempt=%s",
                (record.self_test_id, record.repair_attempt),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != record.repair_id
                    or str(existing[1]) != record.status
                    or existing[2] != record.target_workflow_version
                ):
                    raise ValueError("repair attempt is bound to different content")
                return record
            cursor.execute(
                "INSERT INTO workflow_repair_record("
                "repair_id,self_test_id,workflow_family_id,source_workflow_version,"
                "target_workflow_version,repair_attempt,failure_signature,status,reason,evidence,created_at"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    record.repair_id,
                    record.self_test_id,
                    record.workflow_family_id,
                    record.source_workflow_version,
                    record.target_workflow_version,
                    record.repair_attempt,
                    record.failure_signature,
                    record.status,
                    record.reason,
                    self._json(evidence),
                    record.created_at,
                ),
            )
        return record

    def create_repair_version(self, version: WorkflowVersion) -> WorkflowVersion:
        if version.parent_version is None:
            raise ValueError("repair Workflow Version requires parent_version")
        with self._transaction() as connection:
            cursor = connection.cursor()
            parent = self._get_identity(
                cursor,
                version.workflow_family_id,
                version.parent_version,
                lock=True,
            )
            if parent.status != "SELF_TEST_FAILED":
                raise ValueError("repair parent must be SELF_TEST_FAILED")
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash,status,parent_version FROM workflow_version "
                "WHERE workflow_family_id=%s AND version=%s",
                (version.workflow_family_id, version.version),
            )
            existing = cursor.fetchone()
            if existing is not None:
                observed = (str(existing[0]), str(existing[1]), str(existing[2]), existing[3])
                expected = (
                    version.registry_snapshot_id,
                    version.ir_hash,
                    version.status,
                    version.parent_version,
                )
                if observed != expected:
                    raise ValueError("repair Workflow Version already exists with different content")
                return version
            cursor.execute(
                "INSERT INTO workflow_version("
                "workflow_family_id,version,parent_version,registry_snapshot_id,ir_hash,structure_ref,status"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    version.workflow_family_id,
                    version.version,
                    version.parent_version,
                    version.registry_snapshot_id,
                    version.ir_hash,
                    version.structure_ref,
                    version.status,
                ),
            )
        return version

    @staticmethod
    def _reservation(row: tuple[object, ...]) -> PublishReservation:
        return PublishReservation(
            str(row[0]),
            int(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]) == "PUBLISHED",
        )

    def reserve_publish(
        self,
        identity: WorkflowTestIdentity,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
        *,
        operation_key: str,
    ) -> PublishReservation:
        with self._transaction() as connection:
            cursor = connection.cursor()
            current = self._get_identity(
                cursor,
                identity.workflow_family_id,
                identity.workflow_version,
                lock=True,
            )
            cursor.execute(
                "SELECT workflow_family_id,workflow_version,registry_snapshot_id,ir_hash,"
                "workflow_hash,engine,engine_workflow_id,operation_key,status "
                "FROM workflow_publish_record WHERE workflow_family_id=%s AND workflow_version=%s",
                (identity.workflow_family_id, identity.workflow_version),
            )
            row = cursor.fetchone()
            existing = None if row is None else self._reservation(row)
            if current.status == "PUBLISHED":
                if existing is None or not existing.published:
                    raise PublishGateError("published Workflow has no completed publish record")
                if (
                    existing.workflow_hash != workflow.workflow_hash
                    or existing.engine_workflow_id != reference.workflow_id
                ):
                    raise PublishGateError("repeat publish differs from published identity")
                return existing
            if current.status != "VERIFIED":
                raise PublishGateError(f"Workflow status is not VERIFIED: {current.status}")
            cursor.execute(
                "SELECT self_test_id,registry_snapshot_id,ir_hash,workflow_hash "
                "FROM self_test_run WHERE workflow_family_id=%s AND workflow_version=%s "
                "AND status='PASSED' ORDER BY completed_at DESC,self_test_id DESC LIMIT 1",
                (current.workflow_family_id, current.workflow_version),
            )
            run = cursor.fetchone()
            if run is None:
                raise PublishGateError("latest successful Self-Test is missing")
            cursor.execute(
                "SELECT stage,status FROM self_test_stage_result WHERE self_test_id=%s ORDER BY stage",
                (run[0],),
            )
            stages = tuple((str(item[0]), str(item[1])) for item in cursor.fetchall())
            if stages != tuple((stage, "PASS") for stage in SELF_TEST_STAGES):
                raise PublishGateError("Self-Test does not contain S0-S4 PASS")
            if (
                str(run[1]) != current.registry_snapshot_id
                or str(run[2]) != current.ir_hash
                or str(run[3]) != workflow.workflow_hash
            ):
                raise PublishGateError("Self-Test identity is stale")
            reservation = PublishReservation(
                current.workflow_family_id,
                current.workflow_version,
                current.registry_snapshot_id,
                current.ir_hash,
                workflow.workflow_hash,
                reference.engine,
                reference.workflow_id,
                operation_key,
            )
            if existing is not None:
                if existing != reservation:
                    raise PublishGateError("publish is already reserved with different identity")
                return existing
            cursor.execute(
                "INSERT INTO workflow_publish_record("
                "workflow_family_id,workflow_version,registry_snapshot_id,ir_hash,workflow_hash,"
                "engine,engine_workflow_id,operation_key,status"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')",
                (
                    reservation.workflow_family_id,
                    reservation.workflow_version,
                    reservation.registry_snapshot_id,
                    reservation.ir_hash,
                    reservation.workflow_hash,
                    reservation.engine,
                    reservation.engine_workflow_id,
                    reservation.operation_key,
                ),
            )
            return reservation

    def complete_publish(self, reservation: PublishReservation) -> PublishReservation:
        with self._transaction() as connection:
            cursor = connection.cursor()
            identity = self._get_identity(
                cursor,
                reservation.workflow_family_id,
                reservation.workflow_version,
                lock=True,
            )
            cursor.execute(
                "SELECT workflow_family_id,workflow_version,registry_snapshot_id,ir_hash,"
                "workflow_hash,engine,engine_workflow_id,operation_key,status "
                "FROM workflow_publish_record WHERE workflow_family_id=%s AND workflow_version=%s FOR UPDATE",
                (reservation.workflow_family_id, reservation.workflow_version),
            )
            row = cursor.fetchone()
            if row is None:
                raise PublishGateError("publish reservation is missing")
            stored = self._reservation(row)
            if stored.published and identity.status == "PUBLISHED":
                return stored
            if stored != reservation or identity.status != "VERIFIED":
                raise PublishGateError("publish reservation or Workflow status changed")
            self._transition_workflow(
                cursor,
                identity,
                "PUBLISHED",
                "release gate passed",
                {"workflow_hash": reservation.workflow_hash},
            )
            cursor.execute(
                "UPDATE workflow_publish_record SET status='PUBLISHED',published_at=now() "
                "WHERE workflow_family_id=%s AND workflow_version=%s",
                (reservation.workflow_family_id, reservation.workflow_version),
            )
            return replace(reservation, published=True)

    def assert_execution_allowed(
        self,
        workflow_family_id: str,
        workflow_version: int,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            identity = self._get_identity(
                cursor,
                workflow_family_id,
                workflow_version,
                lock=False,
            )
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash,workflow_hash,engine,engine_workflow_id,status "
                "FROM workflow_publish_record WHERE workflow_family_id=%s AND workflow_version=%s",
                (workflow_family_id, workflow_version),
            )
            row = cursor.fetchone()
            if identity.status != "PUBLISHED" or row is None or str(row[5]) != "PUBLISHED":
                raise ExecuteGateError("formal execution requires a published Workflow Version")
            if (
                str(row[0]) != identity.registry_snapshot_id
                or str(row[1]) != workflow.source_ir_hash
                or str(row[2]) != workflow.workflow_hash
                or str(row[3]) != reference.engine
                or str(row[4]) != reference.workflow_id
                or not reference.published
            ):
                raise ExecuteGateError("formal execution identity differs from published Workflow")

    def get_published_release(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> PublishReservation:
        with self._transaction() as connection:
            cursor = connection.cursor()
            identity = self._get_identity(
                cursor,
                workflow_family_id,
                workflow_version,
                lock=False,
            )
            cursor.execute(
                "SELECT registry_snapshot_id,ir_hash,workflow_hash,engine,"
                "engine_workflow_id,operation_key,status FROM workflow_publish_record "
                "WHERE workflow_family_id=%s AND workflow_version=%s",
                (workflow_family_id, workflow_version),
            )
            row = cursor.fetchone()
            if identity.status != "PUBLISHED" or row is None or str(row[6]) != "PUBLISHED":
                raise ExecuteGateError(
                    "formal execution requires a completed matching publish record"
                )
            if str(row[0]) != identity.registry_snapshot_id or str(row[1]) != identity.ir_hash:
                raise ExecuteGateError("published release identity is stale")
            return PublishReservation(
                workflow_family_id,
                workflow_version,
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                published=True,
            )

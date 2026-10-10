from __future__ import annotations

from typing import Mapping

from adapters.postgres_execution import PostgresExecutionRepository
from domain.planning_blueprint import (
    BlueprintIdempotencyConflict,
    BlueprintIngestionResult,
    BlueprintWorkflowGeneration,
    PlanningBlueprintPlan,
)
from domain.persistence import WorkflowFamily


class PostgresPlanningBlueprintRepository(PostgresExecutionRepository):
    """Durable inbound-contract facts after Adapter validation."""

    def ensure_workflow_family(self, family: WorkflowFamily) -> WorkflowFamily:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO workflow_family(workflow_family_id,tenant_id,task_id,status) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (workflow_family_id) DO NOTHING",
                (family.workflow_family_id, family.tenant_id, family.task_id, family.status),
            )
            cursor.execute(
                "SELECT tenant_id,task_id,status FROM workflow_family WHERE workflow_family_id=%s",
                (family.workflow_family_id,),
            )
            row = cursor.fetchone()
            if row is None or str(row[0]) != family.tenant_id or str(row[1]) != family.task_id:
                raise ValueError("workflow family identity conflict")
        return family

    @staticmethod
    def _result(row: tuple[object, ...], *, replay: bool = False) -> BlueprintIngestionResult:
        reference = row[12] if isinstance(row[12], dict) else {}
        return BlueprintIngestionResult(
            status=str(row[10]), blueprint_id=str(row[0]), plan_id=str(row[2]),
            plan_version=int(row[3]), session_id=str(row[5]), correlation_id=str(row[6]),
            idempotency_key=str(row[7]), blueprint_hash=str(row[8]),
            workflow_family_id=None if row[9] is None else str(row[9]),
            workflow_version=None if row[11] is None else int(row[11]),
            workflow_reference=reference, idempotent_replay=replay,
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT blueprint_id,tenant_id,plan_id,plan_version,schema_version,session_id,"
            "correlation_id,idempotency_key,blueprint_hash,workflow_family_id,validation_status,"
            "workflow_version,workflow_reference FROM planning_blueprint_ingestion "
        )

    def save_or_replay_blueprint(
        self, plan: PlanningBlueprintPlan, *, tenant_id: str, blueprint_id: str
    ) -> tuple[bool, BlueprintIngestionResult | None]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(self._select() + "WHERE tenant_id=%s AND idempotency_key=%s", (tenant_id, plan.idempotency_key))
            existing = cursor.fetchone()
            if existing is not None:
                result = self._result(existing, replay=True)
                if result.blueprint_hash != plan.blueprint_hash:
                    raise BlueprintIdempotencyConflict("idempotency key is bound to different blueprint content")
                return False, result
            cursor.execute(
                "INSERT INTO planning_blueprint_ingestion(blueprint_id,tenant_id,plan_id,plan_version,"
                "schema_version,session_id,correlation_id,idempotency_key,blueprint_hash,payload_json,validation_status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'ACCEPTED')",
                (blueprint_id, tenant_id, plan.plan_id, plan.plan_version, plan.schema_version,
                 plan.session_id, plan.correlation_id, plan.idempotency_key, plan.blueprint_hash,
                 self._json(plan.as_dict())),
            )
        return True, None

    def update_blueprint_result(
        self, blueprint_id: str, *, workflow_family_id: str | None,
        workflow_version: int | None, workflow_reference: Mapping[str, object]
    ) -> BlueprintIngestionResult:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE planning_blueprint_ingestion SET workflow_family_id=%s,workflow_version=%s,"
                "workflow_reference=%s::jsonb WHERE blueprint_id=%s",
                (workflow_family_id, workflow_version, self._json(dict(workflow_reference)), blueprint_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Blueprint not found: {blueprint_id}")
            cursor.execute(self._select() + "WHERE blueprint_id=%s", (blueprint_id,))
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Blueprint not found: {blueprint_id}")
            return self._result(row)

    def reserve_generation(
        self,
        *,
        blueprint_id: str,
        plan: PlanningBlueprintPlan,
        tenant_id: str,
        workflow_family_id: str,
        registry_snapshot_id: str,
    ) -> BlueprintWorkflowGeneration:
        """Atomically allocate the next immutable Workflow Version for a blueprint."""
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT blueprint_id,tenant_id,plan_id,plan_version,blueprint_hash,"
                "workflow_family_id,workflow_version,status,registry_snapshot_id,ir_hash,"
                "workflow_hash,workflow_reference,self_test_id,execution_id "
                "FROM planning_blueprint_generation WHERE blueprint_id=%s FOR UPDATE",
                (blueprint_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[4]) != plan.blueprint_hash or str(existing[1]) != tenant_id:
                    raise BlueprintIdempotencyConflict("blueprint is bound to different generation content")
                return BlueprintWorkflowGeneration(
                    blueprint_id=str(existing[0]), tenant_id=str(existing[1]), plan_id=str(existing[2]),
                    plan_version=int(existing[3]), blueprint_hash=str(existing[4]),
                    workflow_family_id=str(existing[5]), workflow_version=int(existing[6]),
                    status=str(existing[7]), registry_snapshot_id=None if existing[8] is None else str(existing[8]),
                    ir_hash=None if existing[9] is None else str(existing[9]),
                    workflow_hash=None if existing[10] is None else str(existing[10]),
                    workflow_reference=existing[11] or {}, self_test_id=None if existing[12] is None else str(existing[12]),
                    execution_id=None if existing[13] is None else str(existing[13]),
                )
            cursor.execute(
                "INSERT INTO workflow_family(workflow_family_id,tenant_id,task_id,status) "
                "VALUES (%s,%s,%s,'ACTIVE') ON CONFLICT (workflow_family_id) DO NOTHING",
                (workflow_family_id, tenant_id, plan.plan_id),
            )
            cursor.execute(
                "SELECT tenant_id FROM workflow_family WHERE workflow_family_id=%s FOR UPDATE",
                (workflow_family_id,),
            )
            family = cursor.fetchone()
            if family is None or str(family[0]) != tenant_id:
                raise ValueError("workflow family belongs to a different tenant")
            cursor.execute(
                "SELECT GREATEST("
                "COALESCE((SELECT MAX(version) FROM workflow_version WHERE workflow_family_id=%s),0),"
                "COALESCE((SELECT MAX(workflow_version) FROM planning_blueprint_generation "
                "WHERE workflow_family_id=%s),0))+1",
                (workflow_family_id, workflow_family_id),
            )
            version = int(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO planning_blueprint_generation(blueprint_id,tenant_id,plan_id,plan_version,"
                "blueprint_hash,workflow_family_id,workflow_version,status,registry_snapshot_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'RESERVED',%s)",
                (blueprint_id, tenant_id, plan.plan_id, plan.plan_version, plan.blueprint_hash,
                 workflow_family_id, version, registry_snapshot_id),
            )
        return BlueprintWorkflowGeneration(
            blueprint_id, tenant_id, plan.plan_id, plan.plan_version, plan.blueprint_hash,
            workflow_family_id, version, registry_snapshot_id=registry_snapshot_id,
        )

    def save_generation(self, generation: BlueprintWorkflowGeneration) -> BlueprintWorkflowGeneration:
        with self._transaction() as connection:
            cursor = connection.cursor()
            if generation.ir_hash is not None and generation.registry_snapshot_id is not None:
                cursor.execute(
                    "SELECT registry_snapshot_id,ir_hash,status FROM workflow_version "
                    "WHERE workflow_family_id=%s AND version=%s FOR UPDATE",
                    (generation.workflow_family_id, generation.workflow_version),
                )
                version = cursor.fetchone()
                if version is None:
                    cursor.execute(
                        "SELECT MAX(version) FROM workflow_version WHERE workflow_family_id=%s",
                        (generation.workflow_family_id,),
                    )
                    previous = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT INTO workflow_version(workflow_family_id,version,parent_version,"
                        "registry_snapshot_id,ir_hash,structure_ref,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'DRAFT')",
                        (generation.workflow_family_id, generation.workflow_version, previous,
                         generation.registry_snapshot_id, generation.ir_hash,
                         "blueprint:" + generation.blueprint_id),
                    )
                elif (str(version[0]), str(version[1])) != (
                    generation.registry_snapshot_id, generation.ir_hash
                ):
                    raise BlueprintIdempotencyConflict(
                        "reserved Workflow Version is bound to different immutable content"
                    )
            cursor.execute(
                "UPDATE planning_blueprint_generation SET status=%s,registry_snapshot_id=%s,"
                "ir_hash=%s,workflow_hash=%s,workflow_reference=%s::jsonb,self_test_id=%s,execution_id=%s "
                "WHERE blueprint_id=%s",
                (generation.status, generation.registry_snapshot_id, generation.ir_hash,
                 generation.workflow_hash, self._json(dict(generation.workflow_reference)),
                 generation.self_test_id, generation.execution_id, generation.blueprint_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Generation not found: {generation.blueprint_id}")
        return generation

    def get_generation(self, blueprint_id: str) -> BlueprintWorkflowGeneration:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT blueprint_id,tenant_id,plan_id,plan_version,blueprint_hash,workflow_family_id,"
                "workflow_version,status,registry_snapshot_id,ir_hash,workflow_hash,workflow_reference,"
                "self_test_id,execution_id FROM planning_blueprint_generation WHERE blueprint_id=%s",
                (blueprint_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"Generation not found: {blueprint_id}")
            return BlueprintWorkflowGeneration(
                str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]), str(row[5]), int(row[6]),
                str(row[7]), None if row[8] is None else str(row[8]), None if row[9] is None else str(row[9]),
                None if row[10] is None else str(row[10]), row[11] or {},
                None if row[12] is None else str(row[12]), None if row[13] is None else str(row[13]),
            )

    def link_execution(
        self,
        blueprint_id: str,
        *,
        tenant_id: str,
        execution_id: str,
    ) -> BlueprintWorkflowGeneration:
        """Persist the first formal execution reference without allowing lineage drift."""
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT g.workflow_family_id,g.workflow_version,g.status,g.execution_id,"
                "e.workflow_family_id,e.workflow_version "
                "FROM planning_blueprint_generation g JOIN execution e ON e.execution_id=%s "
                "WHERE g.blueprint_id=%s AND g.tenant_id=%s AND e.tenant_id=%s FOR UPDATE OF g",
                (execution_id, blueprint_id, tenant_id, tenant_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError("blueprint generation or tenant-scoped execution not found")
            if str(row[2]) != "PUBLISHED":
                raise ValueError("only a published blueprint generation can link an execution")
            if (str(row[0]), int(row[1])) != (str(row[4]), int(row[5])):
                raise ValueError("execution does not belong to the blueprint workflow")
            if row[3] is not None and str(row[3]) != execution_id:
                raise ValueError("blueprint generation is already linked to another execution")
            cursor.execute(
                "UPDATE planning_blueprint_generation SET execution_id=%s WHERE blueprint_id=%s",
                (execution_id, blueprint_id),
            )
        return self.get_generation(blueprint_id)

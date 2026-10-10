from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from domain.persistence import WorkflowFamily, WorkflowVersion
from domain.planning_blueprint import (
    BlueprintIngestionResult,
    BlueprintWorkflowGeneration,
    PlanningBlueprintPlan,
)
from adapters.fake_execution import InMemoryExecutionRepository


class InMemoryPlanningBlueprintRepository:
    def __init__(self) -> None:
        self.records: dict[str, tuple[str, BlueprintIngestionResult]] = {}
        self.by_key: dict[tuple[str, str], str] = {}

    def save_or_replay_blueprint(
        self,
        plan: PlanningBlueprintPlan,
        *,
        tenant_id: str,
        blueprint_id: str,
    ) -> tuple[bool, BlueprintIngestionResult | None]:
        key = (tenant_id, plan.idempotency_key)
        existing_id = self.by_key.get(key)
        if existing_id is not None:
            stored_hash, result = self.records[existing_id]
            if stored_hash != plan.blueprint_hash:
                from domain.planning_blueprint import BlueprintIdempotencyConflict
                raise BlueprintIdempotencyConflict("idempotency key is bound to different blueprint content")
            return False, replace(result, idempotent_replay=True)
        result = BlueprintIngestionResult(
            status="ACCEPTED",
            blueprint_id=blueprint_id,
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            session_id=plan.session_id,
            correlation_id=plan.correlation_id,
            idempotency_key=plan.idempotency_key,
            blueprint_hash=plan.blueprint_hash,
        )
        self.by_key[key] = blueprint_id
        self.records[blueprint_id] = (plan.blueprint_hash, result)
        return True, None

    def update_blueprint_result(
        self,
        blueprint_id: str,
        *,
        workflow_family_id: str | None,
        workflow_version: int | None,
        workflow_reference: Mapping[str, object],
    ) -> BlueprintIngestionResult:
        stored_hash, result = self.records[blueprint_id]
        updated = replace(
            result,
            workflow_family_id=workflow_family_id,
            workflow_version=workflow_version,
            workflow_reference=dict(workflow_reference),
        )
        self.records[blueprint_id] = (stored_hash, updated)
        return updated


class InMemoryProductionBlueprintRepository(InMemoryPlanningBlueprintRepository, InMemoryExecutionRepository):
    """Non-Fake-generation persistence adapter for production-composition tests."""

    def __init__(self) -> None:
        super().__init__()
        InMemoryExecutionRepository.__init__(self)
        self.families: dict[str, WorkflowFamily] = {}
        self.versions: dict[tuple[str, int], WorkflowVersion] = {}
        self.generations: dict[str, BlueprintWorkflowGeneration] = {}

    def ensure_workflow_family(self, family: WorkflowFamily) -> WorkflowFamily:
        existing = self.families.get(family.workflow_family_id)
        if existing is not None and existing != family:
            raise ValueError("workflow family identity conflict")
        self.families[family.workflow_family_id] = existing or family
        return self.families[family.workflow_family_id]

    def reserve_generation(self, *, blueprint_id: str, plan: PlanningBlueprintPlan,
                           tenant_id: str, workflow_family_id: str,
                           registry_snapshot_id: str) -> BlueprintWorkflowGeneration:
        existing = self.generations.get(blueprint_id)
        if existing is not None:
            if existing.blueprint_hash != plan.blueprint_hash:
                from domain.planning_blueprint import BlueprintIdempotencyConflict
                raise BlueprintIdempotencyConflict("blueprint is bound to different generation content")
            return existing
        version = max((item[1] for item in self.versions if item[0] == workflow_family_id), default=0) + 1
        generation = BlueprintWorkflowGeneration(
            blueprint_id, tenant_id, plan.plan_id, plan.plan_version, plan.blueprint_hash,
            workflow_family_id, version, registry_snapshot_id=registry_snapshot_id,
        )
        self.generations[blueprint_id] = generation
        return generation

    def save_generation(self, generation: BlueprintWorkflowGeneration) -> BlueprintWorkflowGeneration:
        existing = self.generations.get(generation.blueprint_id)
        if existing is not None and existing.blueprint_hash != generation.blueprint_hash:
            raise ValueError("generation identity is immutable")
        self.generations[generation.blueprint_id] = generation
        if generation.ir_hash and generation.registry_snapshot_id and (
            generation.workflow_family_id, generation.workflow_version
        ) not in self.versions:
            previous = max(
                (version for family, version in self.versions if family == generation.workflow_family_id),
                default=0,
            )
            self.versions[(generation.workflow_family_id, generation.workflow_version)] = WorkflowVersion(
                generation.workflow_family_id, generation.workflow_version,
                generation.registry_snapshot_id, generation.ir_hash, "DRAFT",
                parent_version=previous or None, structure_ref="blueprint:" + generation.blueprint_id,
            )
        return generation

    def get_generation(self, blueprint_id: str) -> BlueprintWorkflowGeneration:
        try:
            return self.generations[blueprint_id]
        except KeyError as error:
            raise KeyError(f"Generation not found: {blueprint_id}") from error

    def link_execution(
        self,
        blueprint_id: str,
        *,
        tenant_id: str,
        execution_id: str,
    ) -> BlueprintWorkflowGeneration:
        generation = self.get_generation(blueprint_id)
        execution = self.get_execution(tenant_id, execution_id)
        if generation.status != "PUBLISHED":
            raise ValueError("only a published blueprint generation can link an execution")
        if (execution.workflow_family_id, execution.workflow_version) != (
            generation.workflow_family_id,
            generation.workflow_version,
        ):
            raise ValueError("execution does not belong to the blueprint workflow")
        if generation.execution_id not in {None, execution_id}:
            raise ValueError("blueprint generation is already linked to another execution")
        linked = replace(generation, execution_id=execution_id)
        self.generations[blueprint_id] = linked
        return linked

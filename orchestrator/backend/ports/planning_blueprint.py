from __future__ import annotations

from typing import Mapping, Protocol

from domain.planning_blueprint import (
    BlueprintIngestionResult,
    BlueprintWorkflowGeneration,
    PlanningBlueprintPlan,
)
from domain.persistence import WorkflowFamily


class PlanningBlueprintInboundPort(Protocol):
    def ingest(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: str,
        correlation_id: str | None = None,
        traceparent: str | None = None,
        tracestate: str | None = None,
        idempotency_key: str | None = None,
    ) -> BlueprintIngestionResult: ...


class PlanningBlueprintRepositoryPort(Protocol):
    def save_or_replay_blueprint(
        self,
        plan: PlanningBlueprintPlan,
        *,
        tenant_id: str,
        blueprint_id: str,
    ) -> tuple[bool, BlueprintIngestionResult | None]: ...

    def update_blueprint_result(
        self,
        blueprint_id: str,
        *,
        workflow_family_id: str | None,
        workflow_version: int | None,
        workflow_reference: Mapping[str, object],
    ) -> BlueprintIngestionResult: ...


class PlanningBlueprintGenerationPort(Protocol):
    def generate(
        self,
        plan: PlanningBlueprintPlan,
        *,
        tenant_id: str,
    ) -> Mapping[str, object]: ...


class ProductionPlanningBlueprintRepositoryPort(Protocol):
    def ensure_workflow_family(self, family: WorkflowFamily) -> WorkflowFamily: ...

    def reserve_generation(
        self,
        *,
        blueprint_id: str,
        plan: PlanningBlueprintPlan,
        tenant_id: str,
        workflow_family_id: str,
        registry_snapshot_id: str,
    ) -> BlueprintWorkflowGeneration: ...

    def save_generation(self, generation: BlueprintWorkflowGeneration) -> BlueprintWorkflowGeneration: ...

    def get_generation(self, blueprint_id: str) -> BlueprintWorkflowGeneration: ...

    def link_execution(
        self,
        blueprint_id: str,
        *,
        tenant_id: str,
        execution_id: str,
    ) -> BlueprintWorkflowGeneration: ...

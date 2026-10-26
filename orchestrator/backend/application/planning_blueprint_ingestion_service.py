from __future__ import annotations

import hashlib
from typing import Mapping

from adapters.planning_blueprint_v3_adapter import PlanningBlueprintV3Adapter
from adapters.planning_blueprint_v4_adapter import PlanningBlueprintV4Adapter
from domain.planning_blueprint import (
    BlueprintIdempotencyConflict,
    BlueprintIngestionResult,
    PlanningBlueprintPlan,
)
from ports.planning_blueprint import (
    PlanningBlueprintGenerationPort,
    PlanningBlueprintRepositoryPort,
)


class PlanningBlueprintIngestionService:
    def __init__(
        self,
        repository: PlanningBlueprintRepositoryPort,
        *,
        adapter: PlanningBlueprintV3Adapter | None = None,
        v4_adapter: PlanningBlueprintV4Adapter | None = None,
        generation: PlanningBlueprintGenerationPort | None = None,
    ) -> None:
        self.repository = repository
        self.adapter = adapter or PlanningBlueprintV3Adapter()
        self.v4_adapter = v4_adapter or PlanningBlueprintV4Adapter()
        self.generation = generation

    @staticmethod
    def blueprint_id(plan: PlanningBlueprintPlan, tenant_id: str) -> str:
        identity = f"{tenant_id}:{plan.plan_id}:{plan.plan_version}:{plan.idempotency_key}"
        return "blueprint-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def workflow_family_id(plan: PlanningBlueprintPlan, tenant_id: str) -> str:
        return "wf-" + hashlib.sha256(f"{tenant_id}:{plan.plan_id}".encode("utf-8")).hexdigest()[:24]

    def ingest(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: str,
        correlation_id: str | None = None,
        traceparent: str | None = None,
        tracestate: str | None = None,
        idempotency_key: str | None = None,
    ) -> BlueprintIngestionResult:
        if payload.get("schema_version") == "planning-blueprint.v4":
            plan = self.v4_adapter.adapt(
                payload,
                tenant_id=tenant_id,
                traceparent=traceparent,
                tracestate=tracestate,
                idempotency_key=idempotency_key,
            )
        else:
            plan = self.adapter.adapt(
                payload,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        blueprint_id = self.blueprint_id(plan, tenant_id)
        created, existing = self.repository.save_or_replay_blueprint(
            plan, tenant_id=tenant_id, blueprint_id=blueprint_id
        )
        if not created:
            if existing is None:
                raise BlueprintIdempotencyConflict("existing blueprint result is unavailable")
            # A published reference is terminal. Failed generation results
            # remain replayable so a corrected external engine can complete
            # the same immutable PlanVersion without changing its idempotency
            # identity.
            if self.generation is None or existing.workflow_reference.get("stage") == "PUBLISHED":
                return existing
        family_id: str | None = None
        workflow_version: int | None = None
        reference: Mapping[str, object] = {}
        if self.generation is not None:
            generated = self.generation.generate(plan, tenant_id=tenant_id)
            family_id = str(generated.get("workflow_family_id")) if generated.get("workflow_family_id") else self.workflow_family_id(plan, tenant_id)
            value = generated.get("workflow_version")
            workflow_version = int(value) if value is not None else None
            candidate_reference = generated.get("workflow_reference")
            reference = candidate_reference if isinstance(candidate_reference, Mapping) else generated
        return self.repository.update_blueprint_result(
            blueprint_id,
            workflow_family_id=family_id,
            workflow_version=workflow_version,
            workflow_reference=reference,
        )

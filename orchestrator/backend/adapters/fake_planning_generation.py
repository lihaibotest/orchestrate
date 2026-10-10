from __future__ import annotations

from collections.abc import Callable
from typing import Mapping

from application.execution_ir_service import ExecutionIRValidationService
from application.planning_blueprint_ir import build_execution_ir
from domain.agent_preparation import RegistrySnapshot
from domain.planning_blueprint import PlanningBlueprintPlan


class FakePlanningBlueprintGenerationAdapter:
    """Deterministic generation seam used by contract tests and local integration.

    Production generation remains an injected port.  This adapter deliberately
    stops after IR validation and returns references; it never calls n8n,
    Self-Test, MinIO, or Runtime Event APIs.
    """

    def __init__(
        self,
        snapshot_for: Callable[[PlanningBlueprintPlan, str], RegistrySnapshot],
        validator: ExecutionIRValidationService,
        *,
        workflow_version: int = 1,
        workflow_family_id: Callable[[PlanningBlueprintPlan, str], str] | None = None,
    ) -> None:
        self.snapshot_for = snapshot_for
        self.validator = validator
        self.workflow_version = workflow_version
        self.workflow_family_id = workflow_family_id or (
            lambda plan, tenant: f"wf-{tenant}-{plan.plan_id}"
        )

    def generate(
        self,
        plan: PlanningBlueprintPlan,
        *,
        tenant_id: str,
    ) -> Mapping[str, object]:
        snapshot = self.snapshot_for(plan, tenant_id)
        family_id = self.workflow_family_id(plan, tenant_id)
        ir = build_execution_ir(
            plan,
            workflow_family_id=family_id,
            workflow_version=self.workflow_version,
            snapshot=snapshot,
        )
        validation = self.validator.validate_or_raise(ir)
        return {
            "workflow_family_id": family_id,
            "workflow_version": self.workflow_version,
            "registry_snapshot_id": snapshot.snapshot_id,
            "ir_hash": validation.ir_hash,
            "workflow_reference": {
                "stage": "IR_VALIDATED",
                "workflow_family_id": family_id,
                "workflow_version": self.workflow_version,
                "registry_snapshot_id": snapshot.snapshot_id,
                "ir_hash": validation.ir_hash,
            },
        }

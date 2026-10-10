from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Mapping

from application.agent_preparation_service import AgentPreparationService
from application.execution_controller import ExecutionController, ExecutionView, StartExecutionCommand
from application.execution_ir_service import ExecutionIRValidationService
from application.planning_blueprint_ir import build_execution_ir
from application.self_test_controller import SelfTestController, StartSelfTestRequest
from application.workflow_compilation_service import WorkflowCompilationService
from application.workflow_release_service import WorkflowReleaseService
from domain.agent_preparation import AgentRequirement, RegistrySnapshot
from domain.execution_control import TenantContext
from domain.execution_ir_validator import ExecutionIRValidationError
from domain.planning_blueprint import (
    BlueprintCompilationFailed,
    BlueprintIRValidationFailed,
    BlueprintWorkflowGeneration,
    PlanningBlueprintPlan,
)
from domain.persistence import WorkflowFamily
from ports.agent_preparation import AgentRegistryPort, AgentRuntimePort, RegistrySnapshotRepositoryPort
from ports.planning_blueprint import PlanningBlueprintGenerationPort, ProductionPlanningBlueprintRepositoryPort
from ports.workflow_engine import WorkflowCompilationError, WorkflowEnginePort


class ProductionPlanningBlueprintGenerationService(PlanningBlueprintGenerationPort):
    """Compose the existing P0-04..P0-08 services for v3 inbound plans."""

    def __init__(
        self,
        repository: ProductionPlanningBlueprintRepositoryPort,
        snapshots: RegistrySnapshotRepositoryPort,
        registry: AgentRegistryPort,
        runtime: AgentRuntimePort,
        validation: ExecutionIRValidationService,
        compilation: WorkflowCompilationService,
        engine: WorkflowEnginePort,
        self_test: SelfTestController,
        release: WorkflowReleaseService,
        execution: ExecutionController | None = None,
    ) -> None:
        self.repository = repository
        self.snapshots = snapshots
        self.preparation = AgentPreparationService(registry, runtime, snapshots)
        self.validation = validation
        self.compilation = compilation
        self.engine = engine
        self.self_test = self_test
        self.release = release
        self.execution = execution

    @staticmethod
    def _requirements(plan: PlanningBlueprintPlan) -> tuple[AgentRequirement, ...]:
        # A task is one physical binding requirement; capability alternatives are
        # resolved by AgentPreparationService without accepting a physical ID.
        return tuple(
            AgentRequirement(task.task_id, sorted(task.capabilities)[0])
            for task in plan.tasks
        )

    @staticmethod
    def _operation(blueprint_id: str, suffix: str) -> str:
        return f"blueprint:{blueprint_id}:{suffix}"

    def _snapshot(self, plan: PlanningBlueprintPlan, family_id: str) -> RegistrySnapshot:
        prepared = self.preparation.prepare_agents(family_id, self._requirements(plan))
        return prepared.snapshot

    def generate(self, plan: PlanningBlueprintPlan, *, tenant_id: str) -> Mapping[str, object]:
        blueprint_id = "blueprint-" + hashlib.sha256(
            f"{tenant_id}:{plan.plan_id}:{plan.plan_version}:{plan.idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        family_id = "wf-" + hashlib.sha256(
            f"{tenant_id}:{plan.plan_id}".encode("utf-8")
        ).hexdigest()[:24]
        try:
            existing = self.repository.get_generation(blueprint_id)
        except KeyError:
            existing = None
        if existing is not None and existing.status in {"PUBLISHED", "FAILED"}:
            return dict(existing.workflow_reference) | {
                "status": existing.status,
                "workflow_family_id": existing.workflow_family_id,
                "workflow_version": existing.workflow_version,
                "registry_snapshot_id": existing.registry_snapshot_id,
                "ir_hash": existing.ir_hash,
                "workflow_hash": existing.workflow_hash,
                "self_test_id": existing.self_test_id,
            }

        self.repository.ensure_workflow_family(WorkflowFamily(family_id, tenant_id, plan.plan_id))
        snapshot = self._snapshot(plan, family_id)
        generation = self.repository.reserve_generation(
            blueprint_id=blueprint_id,
            plan=plan,
            tenant_id=tenant_id,
            workflow_family_id=family_id,
            registry_snapshot_id=snapshot.snapshot_id,
        )
        ir = build_execution_ir(
            plan,
            workflow_family_id=family_id,
            workflow_version=generation.workflow_version,
            snapshot=snapshot,
        )
        try:
            validated = self.validation.validate_or_raise(ir)
        except ExecutionIRValidationError as error:
            raise BlueprintIRValidationFailed(str(error)) from error
        generation = replace(
            generation,
            status="GENERATED",
            registry_snapshot_id=snapshot.snapshot_id,
            ir_hash=validated.ir_hash,
        )
        self.repository.save_generation(generation)
        try:
            compiled = self.compilation.compile(ir).workflow
        except (ExecutionIRValidationError, WorkflowCompilationError) as error:
            raise BlueprintCompilationFailed(str(error)) from error
        generation = replace(
            generation,
            workflow_hash=compiled.workflow_hash,
            workflow_reference={
                "stage": "COMPILED",
                "workflow_family_id": family_id,
                "workflow_version": generation.workflow_version,
                "registry_snapshot_id": snapshot.snapshot_id,
                "ir_hash": validated.ir_hash,
                "workflow_hash": compiled.workflow_hash,
            },
        )
        self.repository.save_generation(generation)

        test_request = StartSelfTestRequest(
            tenant_id=tenant_id,
            workflow_family_id=family_id,
            workflow_version=generation.workflow_version,
            execution_ir=ir,
            workflow=compiled,
            operation_key=self._operation(blueprint_id, "self-test"),
        )
        result = self.self_test.start_self_test(test_request)
        if result.status != "PASSED":
            failed = replace(generation, status="FAILED", self_test_id=result.self_test_id)
            self.repository.save_generation(failed)
            return {
                "status": "SELF_TEST_FAILED",
                "workflow_family_id": family_id,
                "workflow_version": generation.workflow_version,
                "registry_snapshot_id": snapshot.snapshot_id,
                "ir_hash": validated.ir_hash,
                "workflow_hash": compiled.workflow_hash,
                "self_test_id": result.self_test_id,
            }

        imported = self.engine.import_workflow(
            compiled,
            operation_key=self._operation(blueprint_id, "self-test:S2:import"),
        )
        published = self.release.publish(
            family_id,
            generation.workflow_version,
            compiled,
            imported,
            operation_key=self._operation(blueprint_id, "publish"),
        )
        final = replace(
            generation,
            status="PUBLISHED",
            self_test_id=result.self_test_id,
            workflow_reference={
                "stage": "PUBLISHED",
                "engine": published.engine,
                "workflow_id": published.workflow_id,
                "published": published.published,
                "workflow_family_id": family_id,
                "workflow_version": generation.workflow_version,
                "registry_snapshot_id": snapshot.snapshot_id,
                "ir_hash": validated.ir_hash,
                "workflow_hash": compiled.workflow_hash,
                "correlation_id": plan.correlation_id,
                "self_test_id": result.self_test_id,
            },
        )
        self.repository.save_generation(final)
        return dict(final.workflow_reference) | {
            "status": final.status,
            "workflow_family_id": final.workflow_family_id,
            "workflow_version": final.workflow_version,
            "registry_snapshot_id": final.registry_snapshot_id,
            "ir_hash": final.ir_hash,
            "workflow_hash": final.workflow_hash,
            "self_test_id": final.self_test_id,
        }

    def start_formal_execution(
        self,
        blueprint_id: str,
        *,
        tenant_id: str,
        client_request_id: str,
        api_idempotency_key: str,
        inputs: Mapping[str, object],
    ) -> ExecutionView:
        """Run the published generation through the existing P0-08 formal gate."""
        if self.execution is None:
            raise RuntimeError("formal execution composition is not configured")
        generation = self.repository.get_generation(blueprint_id)
        if generation.tenant_id != tenant_id:
            raise KeyError(f"Generation not found: {blueprint_id}")
        correlation_id = str(generation.workflow_reference.get("correlation_id") or "")
        view = self.execution.start(
            StartExecutionCommand(
                TenantContext(tenant_id),
                generation.workflow_family_id,
                generation.workflow_version,
                client_request_id,
                api_idempotency_key,
                inputs,
                correlation_id,
            )
        )
        self.repository.link_execution(
            blueprint_id,
            tenant_id=tenant_id,
            execution_id=view.execution.execution_id,
        )
        return view

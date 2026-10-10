from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from domain.execution_ir import freeze_mapping, thaw_json


class PlanningBlueprintError(ValueError):
    """Base error for the planning-blueprint.v3 inbound boundary."""

    error_code = "BLUEPRINT_INVALID"


class BlueprintSchemaInvalid(PlanningBlueprintError):
    error_code = "BLUEPRINT_SCHEMA_INVALID"


class PlanDagInvalid(PlanningBlueprintError):
    error_code = "PLAN_DAG_INVALID"


class CapabilityInvalid(PlanningBlueprintError):
    error_code = "CAPABILITY_INVALID"


class BlueprintSecretPresent(PlanningBlueprintError):
    error_code = "SECRET_PRESENT"


class BlueprintVersionConflict(PlanningBlueprintError):
    error_code = "VERSION_CONFLICT"


class BlueprintIdempotencyConflict(PlanningBlueprintError):
    error_code = "IDEMPOTENCY_CONFLICT"


class BlueprintIRValidationFailed(PlanningBlueprintError):
    error_code = "IR_VALIDATION_FAILED"


class BlueprintCompilationFailed(PlanningBlueprintError):
    error_code = "WORKFLOW_COMPILATION_FAILED"


@dataclass(frozen=True)
class BlueprintTaskPlan:
    task_id: str
    objective: str
    capabilities: tuple[str, ...]
    depends_on: tuple[str, ...]
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    completion_criteria: Mapping[str, object]
    retry_policy: Mapping[str, object]
    execution_policy: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.objective.strip():
            raise ValueError("task_id and objective must not be empty")
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise CapabilityInvalid("task capabilities must be unique and non-empty")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise PlanDagInvalid("task dependencies must be unique")
        for name in ("inputs", "outputs", "completion_criteria", "retry_policy", "execution_policy"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name)))

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "capabilities": list(self.capabilities),
            "depends_on": list(self.depends_on),
            "inputs": thaw_json(self.inputs),
            "outputs": thaw_json(self.outputs),
            "completion_criteria": thaw_json(self.completion_criteria),
            "retry_policy": thaw_json(self.retry_policy),
            "execution_policy": thaw_json(self.execution_policy),
        }


@dataclass(frozen=True)
class PlanningBlueprintPlan:
    schema_version: str
    plan_id: str
    plan_version: int
    session_id: str
    correlation_id: str
    idempotency_key: str
    requested_workflow_version: int
    tasks: tuple[BlueprintTaskPlan, ...]
    inputs: Mapping[str, object]
    outputs: Mapping[str, object]
    completion_criteria: Mapping[str, object]
    retry_policy: Mapping[str, object]
    execution_policy: Mapping[str, object]
    constraints: Mapping[str, object]
    blueprint_hash: str
    tenant_id: str | None = None
    workflow_family_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "planning-blueprint.v3":
            raise BlueprintSchemaInvalid("schema_version must be planning-blueprint.v3")
        if self.plan_version < 0 or self.requested_workflow_version < 0:
            raise BlueprintSchemaInvalid("plan and requested workflow versions must be non-negative")
        task_ids = [item.task_id for item in self.tasks]
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise PlanDagInvalid("task_id must be unique and tasks must not be empty")
        known = set(task_ids)
        for task in self.tasks:
            dangling = sorted(set(task.depends_on) - known)
            if dangling:
                raise PlanDagInvalid(f"dependency does not exist: {dangling[0]}")
        for name in ("inputs", "outputs", "completion_criteria", "retry_policy", "execution_policy", "constraints"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name)))
        object.__setattr__(self, "tasks", tuple(sorted(self.tasks, key=lambda item: item.task_id)))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "workflow_version": self.requested_workflow_version,
            "tasks": [item.as_dict() for item in self.tasks],
            "inputs": thaw_json(self.inputs),
            "outputs": thaw_json(self.outputs),
            "completion_criteria": thaw_json(self.completion_criteria),
            "retry_policy": thaw_json(self.retry_policy),
            "execution_policy": thaw_json(self.execution_policy),
            "constraints": thaw_json(self.constraints),
        }


@dataclass(frozen=True)
class BlueprintIngestionResult:
    status: str
    blueprint_id: str
    plan_id: str
    plan_version: int
    session_id: str
    correlation_id: str
    idempotency_key: str
    blueprint_hash: str
    workflow_family_id: str | None = None
    workflow_version: int | None = None
    workflow_reference: Mapping[str, object] = field(default_factory=dict)
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workflow_reference", freeze_mapping(self.workflow_reference))

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.status == "ACCEPTED",
            "status": self.status,
            "blueprint_id": self.blueprint_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "blueprint_hash": self.blueprint_hash,
            "workflow_family_id": self.workflow_family_id,
            "workflow_version": self.workflow_version,
            "workflow_reference": thaw_json(self.workflow_reference),
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class BlueprintWorkflowGeneration:
    """Durable production-generation reservation and lifecycle reference."""

    blueprint_id: str
    tenant_id: str
    plan_id: str
    plan_version: int
    blueprint_hash: str
    workflow_family_id: str
    workflow_version: int
    status: str = "RESERVED"
    registry_snapshot_id: str | None = None
    ir_hash: str | None = None
    workflow_hash: str | None = None
    workflow_reference: Mapping[str, object] = field(default_factory=dict)
    self_test_id: str | None = None
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if self.workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        if self.status not in {"RESERVED", "GENERATED", "VERIFIED", "PUBLISHED", "FAILED"}:
            raise ValueError(f"unsupported generation status: {self.status}")
        object.__setattr__(self, "workflow_reference", freeze_mapping(self.workflow_reference))

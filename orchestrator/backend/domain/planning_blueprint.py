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
    # V2.0 path-only binding fields.  The legacy capability/input/output
    # members remain as an internal compatibility representation for the
    # existing IR and V1.1 ingestion path.
    capability_ref: str | None = None
    input_paths: Mapping[str, str] = field(default_factory=dict)
    output_paths: Mapping[str, str] = field(default_factory=dict)
    log_path: str | None = None
    artifact_path: str | None = None
    iteration: Mapping[str, object] = field(default_factory=dict)
    side_effect: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.objective.strip():
            raise ValueError("task_id and objective must not be empty")
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise CapabilityInvalid("task capabilities must be unique and non-empty")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise PlanDagInvalid("task dependencies must be unique")
        for name in ("inputs", "outputs", "completion_criteria", "retry_policy", "execution_policy", "input_paths", "output_paths", "iteration"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name)))
        if self.capability_ref is not None and not self.capability_ref.strip():
            raise CapabilityInvalid("capability_ref must not be empty")
        for name in ("log_path", "artifact_path"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be empty")

    def as_dict(self, *, v4: bool = False) -> dict[str, object]:
        if v4:
            return {
                "task_id": self.task_id,
                "objective": self.objective,
                "capability_ref": self.capability_ref or self.capabilities[0],
                "depends_on": list(self.depends_on),
                "input_paths": thaw_json(self.input_paths),
                "output_paths": thaw_json(self.output_paths),
                "log_path": self.log_path or "",
                "artifact_path": self.artifact_path or "",
                "completion_criteria": thaw_json(self.completion_criteria).get("criteria", []) if isinstance(thaw_json(self.completion_criteria), dict) else thaw_json(self.completion_criteria),
                "iteration": thaw_json(self.iteration),
                "side_effect": self.side_effect or "read_only",
            }
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
    contract_version: str = "orchestration.v1.1"
    project_id: str | None = None
    intent_id: str | None = None
    intent_revision: int | None = None
    created_reason: str | None = None
    base_plan_version: int | None = None
    replan_context: Mapping[str, object] = field(default_factory=dict)
    registration_snapshot: Mapping[str, object] = field(default_factory=dict)
    timeout_policy: Mapping[str, object] = field(default_factory=dict)
    goal: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version not in {"planning-blueprint.v3", "planning-blueprint.v4"}:
            raise BlueprintSchemaInvalid("unsupported planning blueprint schema_version")
        if self.schema_version == "planning-blueprint.v4" and self.contract_version != "orchestration.v2.0":
            raise BlueprintSchemaInvalid("planning-blueprint.v4 requires orchestration.v2.0")
        minimum_plan_version = 1 if self.schema_version == "planning-blueprint.v4" else 0
        if self.plan_version < minimum_plan_version or self.requested_workflow_version < 0:
            raise BlueprintSchemaInvalid("plan version is invalid for blueprint schema")
        task_ids = [item.task_id for item in self.tasks]
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise PlanDagInvalid("task_id must be unique and tasks must not be empty")
        known = set(task_ids)
        for task in self.tasks:
            dangling = sorted(set(task.depends_on) - known)
            if dangling:
                raise PlanDagInvalid(f"dependency does not exist: {dangling[0]}")
        for name in ("inputs", "outputs", "completion_criteria", "retry_policy", "execution_policy", "constraints", "replan_context", "registration_snapshot", "timeout_policy", "goal"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name)))
        object.__setattr__(self, "tasks", tuple(sorted(self.tasks, key=lambda item: item.task_id)))

    def as_dict(self) -> dict[str, object]:
        if self.schema_version == "planning-blueprint.v4":
            payload: dict[str, object] = {
                "schema_version": self.schema_version,
                "contract_version": self.contract_version,
                "session_id": self.session_id,
                "intent_id": self.intent_id,
                "intent_revision": self.intent_revision,
                "project_id": self.project_id,
                "plan_id": self.plan_id,
                "plan_version": self.plan_version,
                "created_reason": self.created_reason,
                "workflow_version": self.requested_workflow_version,
                "idempotency_key": self.idempotency_key,
                "registration_snapshot": thaw_json(self.registration_snapshot),
                "timeout_policy": thaw_json(self.timeout_policy),
                "retry_policy": thaw_json(self.retry_policy),
                "goal": thaw_json(self.goal),
                "tasks": [item.as_dict(v4=True) for item in self.tasks],
                "constraints": thaw_json(self.constraints),
                "execution_policy": thaw_json(self.execution_policy),
            }
            if self.created_reason == "REPLAN":
                payload["base_plan_version"] = self.base_plan_version
                payload["replan_context"] = thaw_json(self.replan_context)
            return payload
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

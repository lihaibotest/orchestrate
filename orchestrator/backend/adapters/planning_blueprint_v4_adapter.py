from __future__ import annotations

import hashlib
import re
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from domain.execution_control import reject_sensitive_data
from domain.execution_ir import canonical_json
from domain.planning_blueprint import (
    BlueprintSecretPresent,
    BlueprintSchemaInvalid,
    PlanDagInvalid,
    BlueprintTaskPlan,
    PlanningBlueprintPlan,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SESSION_ID = re.compile(r"^ses_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_INTENT_ID = re.compile(r"^int_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_PLAN_ID = re.compile(r"^pln_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPLAN_TRIGGERS = {"USER_INPUT", "EVALUATION", "EXECUTION_FAILURE", "REGISTRY_CHANGE", "POLICY_CHANGE", "MANUAL"}
_FORBIDDEN_V4_KEYS = {
    "run_id", "attempt_id", "parent_run_id", "run_reason", "evaluation_id", "schema_ref",
    "schema_digest", "input_schema", "output_schema", "outputs", "input_bindings", "capabilities",
    "binding_mode", "source_type", "source_task_id", "output_id", "source_path", "target_path",
    "endpoint", "access_url", "credential", "credential_ref", "worker", "queue", "instance",
    "service", "pod", "artifact_type", "storage", "media_type",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Snapshot(_Strict):
    ref: str = Field(min_length=1, max_length=256)
    digest: str

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registration_snapshot.ref must not be empty")
        return value

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("registration_snapshot.digest must be sha256:<64 lowercase hex>")
        return value


class _Iteration(_Strict):
    enabled: bool
    max_iterations: int = Field(ge=0)

    @model_validator(mode="after")
    def bounded(self) -> "_Iteration":
        if not self.enabled and self.max_iterations != 0:
            raise ValueError("disabled iteration must have max_iterations=0")
        if self.enabled and self.max_iterations < 1:
            raise ValueError("enabled iteration must have max_iterations>=1")
        return self


class _Task(_Strict):
    task_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=4096)
    capability_ref: str = Field(min_length=1, max_length=256)
    depends_on: list[str] = Field(default_factory=list, max_length=256)
    input_paths: dict[str, str]
    output_paths: dict[str, str]
    log_path: str = Field(min_length=1, max_length=2048)
    artifact_path: str = Field(min_length=1, max_length=2048)
    completion_criteria: list[object]
    iteration: _Iteration
    side_effect: str

    @field_validator("task_id", "capability_ref")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier format is invalid")
        return value

    @field_validator("depends_on")
    @classmethod
    def valid_dependencies(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not _IDENTIFIER.fullmatch(item) for item in value):
            raise ValueError("dependency identifiers are invalid or duplicated")
        return value

    @staticmethod
    def _paths(value: dict[str, str], name: str) -> dict[str, str]:
        if any(not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip() for key, item in value.items()):
            raise ValueError(f"{name} must be a string map with non-empty keys and values")
        return value

    @field_validator("input_paths")
    @classmethod
    def valid_input_paths(cls, value: dict[str, str]) -> dict[str, str]:
        return cls._paths(value, "input_paths")

    @field_validator("output_paths")
    @classmethod
    def valid_output_paths(cls, value: dict[str, str]) -> dict[str, str]:
        return cls._paths(value, "output_paths")

    @field_validator("side_effect")
    @classmethod
    def valid_side_effect(cls, value: str) -> str:
        if value not in {"read_only", "idempotent_write", "non_idempotent_write", "external_transaction"}:
            raise ValueError("unsupported side_effect")
        return value


class _Blueprint(_Strict):
    schema_version: str
    contract_version: str
    session_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    intent_revision: int = Field(gt=0)
    project_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(gt=0)
    base_plan_version: int | None = Field(default=None, gt=0)
    created_reason: str
    replan_context: dict[str, object] | None = None
    workflow_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    registration_snapshot: _Snapshot
    timeout_policy: dict[str, object]
    retry_policy: dict[str, object]
    goal: dict[str, object]
    tasks: list[_Task] = Field(min_length=1, max_length=512)
    constraints: dict[str, object]
    execution_policy: dict[str, object]

    @field_validator("schema_version")
    @classmethod
    def valid_schema(cls, value: str) -> str:
        if value != "planning-blueprint.v4":
            raise ValueError("schema_version must be planning-blueprint.v4")
        return value

    @field_validator("contract_version")
    @classmethod
    def valid_contract(cls, value: str) -> str:
        if value != "orchestration.v2.0":
            raise ValueError("contract_version must be orchestration.v2.0")
        return value

    @model_validator(mode="after")
    def validate_identity_and_dag(self) -> "_Blueprint":
        for field_name, pattern in (("session_id", _SESSION_ID), ("intent_id", _INTENT_ID), ("plan_id", _PLAN_ID)):
            if not pattern.fullmatch(getattr(self, field_name)):
                raise ValueError(f"{field_name} must use the required UUIDv7 format")
        expected_key = f"{self.plan_id}:v{self.plan_version}"
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency_key must equal {plan_id}:v{plan_version}")
        if self.created_reason not in {"INITIAL", "REPLAN"}:
            raise ValueError("created_reason must be INITIAL or REPLAN")
        if self.created_reason == "INITIAL" and (self.base_plan_version is not None or self.replan_context is not None):
            raise ValueError("INITIAL plan must not contain replan lineage")
        if self.created_reason == "REPLAN":
            if self.base_plan_version is None or self.base_plan_version >= self.plan_version or not isinstance(self.replan_context, dict):
                raise ValueError("REPLAN requires a prior base_plan_version and replan_context")
            required = {"trigger_run_id", "replan_trigger", "replan_reason"}
            if not required.issubset(self.replan_context) or self.replan_context["replan_trigger"] not in _REPLAN_TRIGGERS:
                raise ValueError("replan_context is incomplete")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("duplicate task_id")
        known = set(task_ids)
        if any(dep not in known or dep == task.task_id for task in self.tasks for dep in task.depends_on):
            raise ValueError("dependency does not exist or is self-referential")
        indegree = {task_id: 0 for task_id in task_ids}
        adjacency = {task_id: [] for task_id in task_ids}
        for task in self.tasks:
            for dep in task.depends_on:
                indegree[task.task_id] += 1
                adjacency[dep].append(task.task_id)
        ready = sorted(item for item, degree in indegree.items() if degree == 0)
        visited = 0
        while ready:
            current = ready.pop(0)
            visited += 1
            for target in sorted(adjacency[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if visited != len(task_ids):
            raise ValueError("dependency cycle")
        return self


class PlanningBlueprintV4Adapter:
    """Strict adapter for the path-only planning-blueprint.v4 contract."""

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: str,
        traceparent: str | None = None,
        tracestate: str | None = None,
        idempotency_key: str | None = None,
    ) -> PlanningBlueprintPlan:
        if not tenant_id.strip():
            raise BlueprintSchemaInvalid("tenant context is required")
        try:
            model = _Blueprint.model_validate(dict(payload))
        except ValidationError as error:
            message = self._safe_message(error)
            if "dependency" in message or "cycle" in message:
                raise PlanDagInvalid(message) from error
            raise BlueprintSchemaInvalid(message) from error
        document = model.model_dump(mode="json")
        if self._contains_forbidden_key(document):
            raise BlueprintSchemaInvalid("V2.0 request contains a forbidden V1.1/runtime/schema field")
        try:
            reject_sensitive_data(document)
        except Exception as error:
            raise BlueprintSecretPresent("blueprint contains forbidden secret material") from error
        if idempotency_key is not None and idempotency_key != model.idempotency_key:
            raise BlueprintSchemaInvalid("idempotency_key does not match request header")
        canonical = canonical_json(document)
        blueprint_hash = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        # W3C traceparent is transport metadata, not part of the V2.0 body.
        # Direct callers without an HTTP envelope still get a stable internal
        # correlation value from the immutable session identity.
        correlation = traceparent.strip() if traceparent and traceparent.strip() else model.session_id
        return PlanningBlueprintPlan(
            schema_version=model.schema_version,
            plan_id=model.plan_id,
            plan_version=model.plan_version,
            session_id=model.session_id,
            correlation_id=correlation,
            idempotency_key=model.idempotency_key,
            requested_workflow_version=model.workflow_version,
            tasks=tuple(
                BlueprintTaskPlan(
                    task_id=item.task_id,
                    objective=item.objective,
                    capabilities=(item.capability_ref,),
                    depends_on=tuple(item.depends_on),
                    # Keep only path values in the internal compatibility map;
                    # no V1.1 schema or artifact binding is synthesized.
                    inputs=dict(item.input_paths),
                    outputs={name: {"path": path} for name, path in item.output_paths.items()},
                    completion_criteria={"criteria": item.completion_criteria},
                    retry_policy=model.retry_policy,
                    execution_policy=model.execution_policy,
                    capability_ref=item.capability_ref,
                    input_paths=item.input_paths,
                    output_paths=item.output_paths,
                    log_path=item.log_path,
                    artifact_path=item.artifact_path,
                    iteration=item.iteration.model_dump(mode="json"),
                    side_effect=item.side_effect,
                )
                for item in model.tasks
            ),
            inputs={},
            outputs={},
            completion_criteria={"criteria": model.goal.get("criteria", [])},
            retry_policy=model.retry_policy,
            execution_policy=model.execution_policy,
            constraints=model.constraints,
            blueprint_hash=blueprint_hash,
            tenant_id=tenant_id,
            contract_version=model.contract_version,
            project_id=model.project_id,
            intent_id=model.intent_id,
            intent_revision=model.intent_revision,
            created_reason=model.created_reason,
            base_plan_version=model.base_plan_version,
            replan_context=model.replan_context or {},
            registration_snapshot=model.registration_snapshot.model_dump(mode="json"),
            timeout_policy=model.timeout_policy,
            goal=model.goal,
        )

    @classmethod
    def _contains_forbidden_key(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            if any(str(key).lower() in _FORBIDDEN_V4_KEYS for key in value):
                return True
            return any(cls._contains_forbidden_key(nested) for nested in value.values())
        if isinstance(value, list):
            return any(cls._contains_forbidden_key(item) for item in value)
        return False

    @staticmethod
    def _safe_message(error: ValidationError) -> str:
        first = error.errors()[0] if error.errors() else {}
        location = ".".join(str(item) for item in first.get("loc", ()))
        message = str(first.get("msg", "invalid blueprint"))
        return f"{location}: {message}" if location else message

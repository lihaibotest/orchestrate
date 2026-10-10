from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from domain.execution_control import reject_sensitive_data
from domain.planning_blueprint import (
    BlueprintSecretPresent,
    BlueprintSchemaInvalid,
    CapabilityInvalid,
    PlanDagInvalid,
    PlanningBlueprintPlan,
    BlueprintTaskPlan,
)
from domain.execution_ir import canonical_json


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Task(_Strict):
    task_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=4096)
    capabilities: list[str] = Field(min_length=1, max_length=64)
    depends_on: list[str] = Field(default_factory=list, max_length=256)
    inputs: dict[str, object] = Field(default_factory=dict)
    input_bindings: list[dict[str, object]] = Field(default_factory=list)
    outputs: list[dict[str, object]] | dict[str, object] = Field(default_factory=dict)
    completion_criteria: list[object] | dict[str, object] = Field(default_factory=dict)
    retry_policy: dict[str, object] = Field(default_factory=dict)
    execution_policy: dict[str, object] = Field(default_factory=dict)
    iteration: dict[str, object] = Field(default_factory=dict)
    side_effect: str | None = None

    @field_validator("task_id", "capabilities", "depends_on")
    @classmethod
    def valid_identifiers(cls, value: object) -> object:
        values = value if isinstance(value, list) else [value]
        if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in values):
            raise ValueError("identifier format is invalid")
        if isinstance(value, list) and len(value) != len(set(value)):
            raise ValueError("identifier list contains duplicates")
        return value

    @field_validator("objective")
    @classmethod
    def non_blank_objective(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must not be blank")
        return value


class _Blueprint(_Strict):
    schema_version: str
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=0)
    workflow_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    tasks: list[_Task] = Field(min_length=1, max_length=512)
    inputs: dict[str, object] = Field(default_factory=dict)
    outputs: dict[str, object] = Field(default_factory=dict)
    completion_criteria: dict[str, object] = Field(default_factory=dict)
    retry_policy: dict[str, object] = Field(default_factory=dict)
    execution_policy: dict[str, object] = Field(default_factory=dict)
    constraints: dict[str, object] = Field(default_factory=dict)
    contract_version: str | None = None
    created_reason: str | None = None
    intent_id: str | None = None
    intent_revision: int | None = Field(default=None, ge=0)
    goal: dict[str, object] | None = None
    timeout_policy: dict[str, object] | None = None

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "planning-blueprint.v3":
            raise ValueError("unsupported schema_version")
        return value

    @field_validator("correlation_id", "session_id", "plan_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier format is invalid")
        return value

    @model_validator(mode="after")
    def validate_dag(self) -> "_Blueprint":
        task_ids = [item.task_id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("duplicate task_id")
        known = set(task_ids)
        if any(dependency not in known for task in self.tasks for dependency in task.depends_on):
            raise ValueError("dependency does not exist")
        indegree = {item: 0 for item in task_ids}
        adjacency = {item: [] for item in task_ids}
        for task in self.tasks:
            for dependency in task.depends_on:
                indegree[task.task_id] += 1
                adjacency[dependency].append(task.task_id)
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


class PlanningBlueprintV3Adapter:
    """Translate the external v3 contract into an internal immutable plan DTO."""

    def adapt(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: str,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PlanningBlueprintPlan:
        if not tenant_id.strip():
            raise BlueprintSchemaInvalid("tenant context is required")
        try:
            normalized_payload = dict(payload)
            if normalized_payload.get("correlation_id") is None and correlation_id:
                normalized_payload["correlation_id"] = correlation_id
            if normalized_payload.get("correlation_id") is None:
                session_id = normalized_payload.get("session_id")
                normalized_payload["correlation_id"] = str(session_id) if session_id else None
            model = _Blueprint.model_validate(normalized_payload)
        except ValidationError as error:
            message = self._safe_message(error)
            if "dependency" in message or "cycle" in message:
                raise PlanDagInvalid(message) from error
            raise BlueprintSchemaInvalid(message) from error
        try:
            reject_sensitive_data(model.model_dump(mode="json"))
        except Exception as error:
            raise BlueprintSecretPresent("blueprint contains forbidden secret material") from error
        if correlation_id is not None and correlation_id != model.correlation_id:
            raise BlueprintSchemaInvalid("correlation_id does not match request context")
        if idempotency_key is not None and idempotency_key != model.idempotency_key:
            raise BlueprintSchemaInvalid("idempotency_key does not match request header")
        if any(not task.capabilities for task in model.tasks):
            raise CapabilityInvalid("every task requires at least one capability")
        self._validate_task_bindings(model.tasks)
        if self._contains_field_mapping(model.model_dump(mode="json")):
            raise BlueprintSchemaInvalid("FIELD_MAPPING is not supported; only WHOLE_ARTIFACT is accepted")
        if self._contains_unsupported_artifact_mode(model.model_dump(mode="json")):
            raise BlueprintSchemaInvalid("only WHOLE_ARTIFACT artifact mode is supported")
        canonical = canonical_json(model.model_dump(mode="json"))
        blueprint_hash = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        if model.correlation_id is None:
            raise BlueprintSchemaInvalid("correlation_id or session_id is required")
        return PlanningBlueprintPlan(
            schema_version=model.schema_version,
            plan_id=model.plan_id,
            plan_version=model.plan_version,
            session_id=model.session_id,
            correlation_id=model.correlation_id,
            idempotency_key=model.idempotency_key,
            requested_workflow_version=model.workflow_version,
            tasks=tuple(
                BlueprintTaskPlan(
                    task_id=item.task_id,
                    objective=item.objective,
                    capabilities=tuple(item.capabilities),
                    depends_on=tuple(item.depends_on),
                    inputs=self._task_inputs(item),
                    outputs=self._task_outputs(item),
                    completion_criteria=(
                        {"criteria": item.completion_criteria}
                        if isinstance(item.completion_criteria, list)
                        else item.completion_criteria
                    ),
                    retry_policy=item.retry_policy or model.retry_policy,
                    execution_policy=item.execution_policy or model.execution_policy,
                )
                for item in model.tasks
            ),
            inputs=model.inputs,
            outputs=model.outputs,
            completion_criteria=model.completion_criteria,
            retry_policy=model.retry_policy,
            execution_policy=model.execution_policy,
            constraints=model.constraints,
            blueprint_hash=blueprint_hash,
            tenant_id=tenant_id,
        )

    @staticmethod
    def _task_outputs(task: _Task) -> dict[str, object]:
        if isinstance(task.outputs, dict):
            return dict(task.outputs)
        outputs: dict[str, object] = {}
        for output in task.outputs:
            output_id = output.get("output_id")
            if not isinstance(output_id, str) or not output_id.strip():
                raise BlueprintSchemaInvalid("task output_id must be a non-empty string")
            if output_id in outputs:
                raise BlueprintSchemaInvalid("task output_id must be unique")
            outputs[output_id] = dict(output)
        return outputs

    @staticmethod
    def _task_inputs(task: _Task) -> dict[str, object]:
        inputs = dict(task.inputs)
        for index, binding in enumerate(task.input_bindings):
            source = binding.get("source")
            if isinstance(source, Mapping):
                inputs[f"artifact_binding_{index}"] = {
                    "binding_mode": binding.get("binding_mode", "WHOLE_ARTIFACT"),
                    "source_task_id": source.get("source_task_id"),
                    "output_id": source.get("output_id"),
                }
        return inputs

    @classmethod
    def _contains_field_mapping(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"mapping_mode", "mode"} and str(nested).upper() == "FIELD_MAPPING":
                    return True
                if cls._contains_field_mapping(nested):
                    return True
        if isinstance(value, list):
            return any(cls._contains_field_mapping(item) for item in value)
        return False

    @classmethod
    def _contains_unsupported_artifact_mode(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in {"artifact_mode", "artifact_mapping_mode"}:
                    if str(nested).upper() != "WHOLE_ARTIFACT":
                        return True
                if cls._contains_unsupported_artifact_mode(nested):
                    return True
        if isinstance(value, list):
            return any(cls._contains_unsupported_artifact_mode(item) for item in value)
        return False

    @staticmethod
    def _validate_task_bindings(tasks: list[_Task]) -> None:
        known_tasks = {task.task_id for task in tasks}
        output_ids = {
            (task.task_id, str(output.get("output_id")))
            for task in tasks
            for output in (task.outputs if isinstance(task.outputs, list) else [])
            if isinstance(output, Mapping) and output.get("output_id") is not None
        }
        for task in tasks:
            for binding in task.input_bindings:
                mode = str(binding.get("binding_mode", "WHOLE_ARTIFACT")).upper()
                if mode != "WHOLE_ARTIFACT":
                    raise BlueprintSchemaInvalid("only WHOLE_ARTIFACT input bindings are supported")
                source = binding.get("source")
                if not isinstance(source, Mapping):
                    raise BlueprintSchemaInvalid("input binding source must be an object")
                source_task = source.get("source_task_id")
                output_id = source.get("output_id")
                if source_task not in known_tasks or (str(source_task), str(output_id)) not in output_ids:
                    raise PlanDagInvalid("input binding references an unknown task output")

    @staticmethod
    def _safe_message(error: ValidationError) -> str:
        first = error.errors()[0] if error.errors() else {}
        location = ".".join(str(item) for item in first.get("loc", ()))
        message = str(first.get("msg", "invalid blueprint"))
        return f"{location}: {message}" if location else message

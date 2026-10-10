from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from contracts.orchestration import N8NEvent


_EVALUATOR_OWNED_KEYS = {
    "evaluation",
    "evaluationid",
    "evaluator",
    "evaluatorid",
    "evaluatoropinion",
    "quality",
    "qualityscore",
    "qualityverdict",
    "recommendation",
    "verdict",
}


def _contains_evaluator_owned_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in _EVALUATOR_OWNED_KEYS or _contains_evaluator_owned_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_evaluator_owned_key(item) for item in value)
    return False


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageDelta(StrictModel):
    calls_delta: StrictInt = Field(ge=0, le=100000)
    duration_ms_delta: StrictInt = Field(ge=0, le=86400000)
    cost_microusd: StrictInt = Field(ge=0, le=9223372036854775807)


class RuntimeTelemetryV1(StrictModel):
    """Canonical internal runtime facts adapted from the public v1/v2 contracts."""

    source_contract: Literal["runtime-event.v2", "orchestration.v1"]
    correlation_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    plan_version: StrictInt = Field(ge=1)
    workflow_version: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    node_id: str = Field(min_length=1, max_length=256)
    attempt: StrictInt = Field(ge=1, le=100)
    event_type: Literal["attempt_started", "attempt_completed", "attempt_failed", "checkpoint"]
    reservation_id: str | None = Field(default=None, min_length=1, max_length=256)
    usage: UsageDelta
    checkpoint: dict[str, object]
    failure: dict[str, object] | None = None
    metrics: dict[str, object] | None = None


class RuntimeEventV2(StrictModel):
    schema_version: Literal["runtime-event.v2"]
    correlation_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    plan_version: StrictInt = Field(ge=1)
    workflow_version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    node_id: str = Field(min_length=1, max_length=256)
    attempt: StrictInt = Field(ge=1, le=100)
    event_type: Literal["attempt_started", "attempt_completed", "attempt_failed", "checkpoint"]
    reservation_id: str | None = Field(default=None, min_length=1, max_length=256)
    usage: UsageDelta
    checkpoint: dict[str, object]
    failure: dict[str, object] | None = None
    metrics: dict[str, object] | None = None

    @field_validator("metrics")
    @classmethod
    def metrics_are_bounded(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is not None and len(value) > 64:
            raise ValueError("metrics exceeds 64 entries")
        if value is not None and _contains_evaluator_owned_key(value):
            raise ValueError("runtime metrics cannot contain evaluator-owned fields")
        return value

    @field_validator("checkpoint")
    @classmethod
    def checkpoint_is_bounded(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > 256:
            raise ValueError("checkpoint exceeds 256 entries")
        if _contains_evaluator_owned_key(value):
            raise ValueError("runtime checkpoint cannot contain evaluator-owned fields")
        return value

    @field_validator("failure")
    @classmethod
    def failure_is_bounded(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is not None and len(value) > 64:
            raise ValueError("failure exceeds 64 entries")
        if value is not None and _contains_evaluator_owned_key(value):
            raise ValueError("runtime failure cannot contain evaluator-owned fields")
        return value

    @model_validator(mode="after")
    def serialized_event_is_bounded(self) -> "RuntimeEventV2":
        if len(json.dumps(self.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")) > 65536:
            raise ValueError("runtime event payload exceeds 65536 bytes")
        return self


class RuntimeTerminalEventV2(StrictModel):
    schema_version: Literal["runtime-terminal-event.v2"]
    correlation_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    plan_version: StrictInt = Field(ge=1)
    workflow_version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    terminal_status: Literal["completed", "failed", "cancelled"]
    node_execution_states: list[dict[str, object]] = Field(default_factory=list, max_length=1024)
    output_refs: list[str] = Field(default_factory=list, max_length=256)
    artifact_refs: list[str] = Field(default_factory=list, max_length=256)
    log_refs: list[str] = Field(default_factory=list, max_length=256)
    checkpoint_refs: list[str] = Field(default_factory=list, max_length=256)
    input_summary: dict[str, object] = Field(default_factory=dict)
    retry_repair_history: list[dict[str, object]] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def terminal_facts_are_bounded_and_evaluator_free(self) -> "RuntimeTerminalEventV2":
        payload = self.model_dump(mode="json")
        if _contains_evaluator_owned_key(payload):
            raise ValueError("terminal runtime event cannot contain evaluator-owned fields")
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > 65536:
            raise ValueError("terminal runtime event payload exceeds 65536 bytes")
        return self


def runtime_event_v2_to_telemetry(event: RuntimeEventV2) -> RuntimeTelemetryV1:
    return RuntimeTelemetryV1.model_validate(
        {
            "source_contract": event.schema_version,
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "plan_version": event.plan_version,
            "workflow_version": event.workflow_version,
            "idempotency_key": event.idempotency_key,
            "event_id": event.event_id,
            "execution_id": event.execution_id,
            "node_id": event.node_id,
            "attempt": event.attempt,
            "event_type": event.event_type,
            "reservation_id": event.reservation_id,
            "usage": event.usage,
            "checkpoint": event.checkpoint,
            "failure": event.failure,
            "metrics": event.metrics,
        }
    )


def n8n_event_to_runtime_telemetry(event: N8NEvent) -> RuntimeTelemetryV1:
    checkpoint = dict(event.checkpoint or {})
    execution_id = checkpoint.pop("execution_id", None)
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("legacy N8N event checkpoint requires execution_id")
    usage = checkpoint.pop("usage", None)
    if usage is None:
        raise ValueError("legacy N8N event checkpoint requires usage")
    if event.node_id is None:
        raise ValueError("legacy N8N event requires node_id")
    if event.attempt is None:
        raise ValueError("legacy N8N event requires attempt")
    return RuntimeTelemetryV1.model_validate(
        {
            "source_contract": event.contract_version,
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "plan_version": event.plan_version,
            "workflow_version": event.workflow_version,
            "idempotency_key": event.idempotency_key,
            "event_id": event.event_id,
            "execution_id": execution_id,
            "node_id": event.node_id,
            "attempt": event.attempt,
            "event_type": event.event_type,
            "usage": usage,
            "checkpoint": checkpoint,
            "failure": event.failure,
        }
    )


class RuntimeDecisionV2(StrictModel):
    schema_version: Literal["runtime-decision.v2"] = "runtime-decision.v2"
    correlation_id: str
    session_id: str
    plan_version: StrictInt
    workflow_version: StrictInt
    idempotency_key: str
    decision_id: str
    event_id: str
    execution_id: str
    node_id: str
    action: Literal["CONTINUE", "RETRY", "WAIT", "FAIL", "REQUEST_EVALUATION", "PATCH", "REPLAN", "ABORT"]
    reason_code: str = Field(min_length=1, max_length=128)
    route: str | None = Field(default=None, max_length=128)
    wait_seconds: StrictInt | None = Field(default=None, ge=0, le=86400)


class RuntimeAuthorizeV2(StrictModel):
    schema_version: Literal["runtime-authorize.v2"]
    correlation_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    plan_version: StrictInt = Field(ge=1)
    workflow_version: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    node_id: str = Field(min_length=1, max_length=256)
    attempt: StrictInt = Field(ge=1, le=100)
    reservation_id: str = Field(min_length=1, max_length=256)
    calls: StrictInt = Field(ge=1, le=100000)
    cost_microusd: StrictInt = Field(ge=0, le=9223372036854775807)
    duration_seconds: StrictInt = Field(ge=1, le=86400)
    lease_until: float

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SUPPORTED_CONTRACT_VERSIONS = frozenset({"orchestration.v1"})
ESCALATION_PATH = "/api/v2/workflow-escalations"
FINAL_SUMMARY_PATH = "/api/v2/workflow-final-summaries"

_EVALUATOR_OWNED_KEYS = {
    "evaluation",
    "evaluationid",
    "evaluator",
    "evaluatorid",
    "evaluatoropinion",
    "opinion",
    "quality",
    "qualityscore",
    "qualityverdict",
    "recommendation",
    "verdict",
}


def _contains_evaluator_owned_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in _EVALUATOR_OWNED_KEYS or _contains_evaluator_owned_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_evaluator_owned_field(item) for item in value)
    return False


class TimeoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=3600)


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(ge=1, le=10)
    backoff_seconds: int = Field(default=1, ge=0, le=300)
    max_backoff_seconds: int = Field(default=30, ge=0, le=3600)
    timeout_seconds: int = Field(default=30, ge=1)
    retry_on: list[Literal["timeout", "network", "http_429", "http_5xx"]] = Field(
        default_factory=lambda: ["timeout", "network", "http_429", "http_5xx"]
    )


FailureClass = Literal[
    "timeout",
    "network",
    "http_429",
    "http_5xx",
    "business",
    "quality",
    "validation",
    "unknown",
]


class FailureDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=128)
    error_class: FailureClass
    error_code: str = Field(min_length=1, max_length=128)
    http_status: int | None = Field(default=None, ge=100, le=599)
    stderr: str = Field(default="", max_length=8192)
    input_summary: dict[str, object] = Field(default_factory=dict)
    diagnostic: dict[str, object] = Field(default_factory=dict)
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1, le=10)
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    workflow_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ContractEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["orchestration.v1"]
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    workflow_version: int = Field(ge=0)
    timeout_policy: TimeoutPolicy
    retry_policy: RetryPolicy
    idempotency_key: str = Field(min_length=1, max_length=256)


class BlueprintIngress(ContractEnvelope):
    blueprint: dict[str, object]


class N8NEvent(ContractEnvelope):
    event_id: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=128)
    checkpoint: dict[str, object] | None = None
    failure: dict[str, object] | None = None
    node_id: str | None = Field(default=None, min_length=1, max_length=128)
    attempt: int | None = Field(default=None, ge=1, le=10)

    @field_validator("checkpoint", "failure")
    @classmethod
    def reject_evaluator_owned_runtime_data(
        cls, value: dict[str, object] | None
    ) -> dict[str, object] | None:
        if value is not None and _contains_evaluator_owned_field(value):
            raise ValueError("n8n runtime event cannot contain evaluator-owned fields")
        return value


class EvaluationRelay(ContractEnvelope):
    evaluation_id: str = Field(min_length=1, max_length=256)
    result: dict[str, object]


class PlannerDirective(ContractEnvelope):
    directive_id: str = Field(min_length=1, max_length=256)
    directive: Literal["retry", "patch", "replan"]
    patch: dict[str, object] | None = None


class EscalationEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=2048)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=2048)


class WorkflowEscalationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workflow-escalation.v1"]
    contract_version: Literal["workflow-escalation.v1"]
    caller_identity: Literal["orchestrator-escalation"]
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    workflow_version: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=128)
    required_decision: str = Field(min_length=1, max_length=256)
    missing_capabilities: list[str] = Field(default_factory=list, max_length=64)
    external_dependency: str | None = Field(default=None, max_length=256)
    repair_attempts: int = Field(ge=0, le=100)
    evidence: list[EscalationEvidenceV1] = Field(default_factory=list, max_length=64)
    operation_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def reject_evaluator_owned_data(self) -> "WorkflowEscalationV1":
        if _contains_evaluator_owned_field(self.model_dump(mode="json")):
            raise ValueError("workflow escalation cannot contain evaluator-owned fields")
        return self


class WorkflowFinalSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workflow-final-summary.v1"]
    contract_version: Literal["workflow-final-summary.v1"]
    caller_identity: Literal["orchestrator-final-summary"]
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    workflow_version: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=256)
    terminal_status: Literal["completed", "failed", "cancelled"]
    monitoring_mode: Literal["planner_final_summary_fallback"]
    node_execution_states: list[dict[str, object]] = Field(default_factory=list, max_length=1024)
    output_refs: list[str] = Field(default_factory=list, max_length=256)
    artifact_refs: list[str] = Field(default_factory=list, max_length=256)
    log_refs: list[str] = Field(default_factory=list, max_length=256)
    checkpoint_refs: list[str] = Field(default_factory=list, max_length=256)
    input_summary: dict[str, object] = Field(default_factory=dict)
    retry_repair_history: list[dict[str, object]] = Field(default_factory=list, max_length=256)
    operation_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def reject_evaluator_owned_data(self) -> "WorkflowFinalSummaryV1":
        if _contains_evaluator_owned_field(self.model_dump(mode="json")):
            raise ValueError("workflow final summary cannot contain evaluator-owned fields")
        return self


class WorkflowRepairDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["workflow-repair-decision.v1"] = "workflow-repair-decision.v1"
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    workflow_version: int = Field(ge=1)
    node_id: str = Field(min_length=1, max_length=128)
    classification: Literal["workflow_local", "external_boundary", "uncertain"]
    action: Literal["retry_same_version", "repair_new_version", "escalate", "stop"]
    reason_code: str = Field(min_length=1, max_length=128)
    repair_attempt: int = Field(ge=0, le=100)
    max_repair_attempts: int = Field(ge=1, le=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def reject_evaluator_owned_data(self) -> "WorkflowRepairDecisionV1":
        if _contains_evaluator_owned_field(self.model_dump(mode="json")):
            raise ValueError("workflow repair decision cannot contain evaluator-owned fields")
        return self

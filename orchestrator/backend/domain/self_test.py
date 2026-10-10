from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import re
from typing import Mapping

from domain.execution_ir import canonical_json, freeze_mapping, thaw_json


SELF_TEST_STAGES = ("S0", "S1", "S2", "S3", "S4")
STAGE_STATUSES = frozenset({"PASS", "FAIL", "SKIPPED"})
RUN_STATUSES = frozenset({"RUNNING", "PASSED", "FAILED"})
REPAIR_STATUSES = frozenset(
    {"RETRY_UNCHANGED", "NEW_VERSION", "NO_REPAIR", "FAILED", "REPAIR_LIMIT_EXCEEDED"}
)


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


class SelfTestError(RuntimeError):
    """Base error for self-test and release-gate failures."""


class SelfTestIdentityError(SelfTestError):
    """The supplied IR/workflow does not match the persisted Workflow Version."""


class PublishGateError(SelfTestError):
    """Publication was rejected because the complete gate is not satisfied."""


class ExecuteGateError(SelfTestError):
    """Formal execution was rejected because the Workflow is not published."""


class RepairError(SelfTestError):
    """A repair request is invalid or cannot be applied safely."""


@dataclass(frozen=True)
class SelfTestPolicy:
    max_repair_attempts: int

    def __post_init__(self) -> None:
        if self.max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must not be negative")


@dataclass(frozen=True)
class FailureDescriptor:
    stage: str
    error_code: str
    rule_id: str | None = None
    node_id: str | None = None
    agent_id: str | None = None
    normalized_error_class: str = "validation"

    def __post_init__(self) -> None:
        if self.stage not in SELF_TEST_STAGES:
            raise ValueError(f"unsupported self-test stage: {self.stage}")
        object.__setattr__(self, "error_code", _text(self.error_code, "error_code"))
        object.__setattr__(self, "rule_id", _optional_text(self.rule_id, "rule_id"))
        object.__setattr__(self, "node_id", _optional_text(self.node_id, "node_id"))
        object.__setattr__(self, "agent_id", _optional_text(self.agent_id, "agent_id"))
        normalized = re.sub(
            r"[^a-z0-9_.-]+",
            "_",
            _text(self.normalized_error_class, "normalized_error_class").lower(),
        ).strip("_")
        object.__setattr__(self, "normalized_error_class", normalized or "unknown")

    def signature(self) -> str:
        payload = {
            "stage": self.stage,
            "error_code": self.error_code,
            "rule_id": self.rule_id,
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "normalized_error_class": self.normalized_error_class,
        }
        return hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class SelfTestStageResult:
    stage: str
    status: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    error_code: str | None = None
    message: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    failure_signature: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in SELF_TEST_STAGES:
            raise ValueError(f"unsupported self-test stage: {self.stage}")
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"unsupported stage status: {self.status}")
        if self.completed_at < self.started_at:
            raise ValueError("stage completed_at precedes started_at")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        object.__setattr__(self, "error_code", _optional_text(self.error_code, "error_code"))
        object.__setattr__(self, "message", _optional_text(self.message, "message"))
        object.__setattr__(
            self,
            "failure_signature",
            _optional_text(self.failure_signature, "failure_signature"),
        )
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence))
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(sorted(_text(item, "evidence_ref") for item in self.evidence_refs)),
        )
        if self.status == "FAIL" and (
            self.error_code is None or self.failure_signature is None
        ):
            raise ValueError("failed stage requires error_code and failure_signature")
        if self.status != "FAIL" and self.failure_signature is not None:
            raise ValueError("only failed stages may carry failure_signature")

    def evidence_dict(self) -> dict[str, object]:
        value = thaw_json(self.evidence)
        if not isinstance(value, dict):
            raise TypeError("stage evidence must be an object")
        return value


@dataclass(frozen=True)
class SelfTestRunResult:
    self_test_id: str
    tenant_id: str
    workflow_family_id: str
    workflow_version: int
    registry_snapshot_id: str
    ir_hash: str
    workflow_hash: str
    idempotency_key: str
    started_at: datetime
    status: str
    current_stage: str
    repair_attempt: int
    completed_at: datetime | None = None
    failure_signature: str | None = None
    stage_results: tuple[SelfTestStageResult, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "self_test_id",
            "tenant_id",
            "workflow_family_id",
            "registry_snapshot_id",
            "ir_hash",
            "workflow_hash",
            "idempotency_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        if self.status not in RUN_STATUSES:
            raise ValueError(f"unsupported self-test status: {self.status}")
        if self.current_stage not in SELF_TEST_STAGES:
            raise ValueError(f"unsupported current_stage: {self.current_stage}")
        if self.repair_attempt < 0:
            raise ValueError("repair_attempt must not be negative")
        object.__setattr__(
            self,
            "failure_signature",
            _optional_text(self.failure_signature, "failure_signature"),
        )
        object.__setattr__(
            self,
            "stage_results",
            tuple(sorted(self.stage_results, key=lambda item: SELF_TEST_STAGES.index(item.stage))),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(sorted(_text(item, "evidence_ref") for item in self.evidence_refs)),
        )
        if self.status == "RUNNING" and self.completed_at is not None:
            raise ValueError("running self-test cannot have completed_at")
        if self.status != "RUNNING" and self.completed_at is None:
            raise ValueError("completed self-test requires completed_at")
        if self.status == "PASSED" and (
            len(self.stage_results) != len(SELF_TEST_STAGES)
            or any(item.status != "PASS" for item in self.stage_results)
        ):
            raise ValueError("passed self-test requires S0-S4 PASS")
        if self.status == "FAILED" and self.failure_signature is None:
            raise ValueError("failed self-test requires failure_signature")


@dataclass(frozen=True)
class WorkflowTestIdentity:
    tenant_id: str
    workflow_family_id: str
    workflow_version: int
    registry_snapshot_id: str
    ir_hash: str
    status: str
    parent_version: int | None = None


@dataclass(frozen=True)
class RepairRecord:
    repair_id: str
    self_test_id: str
    workflow_family_id: str
    source_workflow_version: int
    repair_attempt: int
    failure_signature: str
    status: str
    created_at: datetime
    target_workflow_version: int | None = None
    reason: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "repair_id",
            "self_test_id",
            "workflow_family_id",
            "failure_signature",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))
        if self.status not in REPAIR_STATUSES:
            raise ValueError(f"unsupported repair status: {self.status}")
        if self.repair_attempt < 1:
            raise ValueError("repair_attempt must be positive")
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence))


@dataclass(frozen=True)
class PublishReservation:
    workflow_family_id: str
    workflow_version: int
    registry_snapshot_id: str
    ir_hash: str
    workflow_hash: str
    engine: str
    engine_workflow_id: str
    operation_key: str
    published: bool = False

    def __post_init__(self) -> None:
        for name in (
            "workflow_family_id",
            "registry_snapshot_id",
            "ir_hash",
            "workflow_hash",
            "engine",
            "engine_workflow_id",
            "operation_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.workflow_version < 1:
            raise ValueError("workflow_version must be positive")


def self_test_idempotent_id(
    tenant_id: str,
    workflow_family_id: str,
    workflow_version: int,
    workflow_hash: str,
    operation_key: str,
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "workflow_family_id": workflow_family_id,
        "workflow_version": workflow_version,
        "workflow_hash": workflow_hash,
        "operation": "self_test",
        "operation_key": operation_key,
    }
    return "self-test-" + hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()

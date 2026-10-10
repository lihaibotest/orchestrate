from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from domain.artifact import ArtifactRef
from domain.runtime_events import RuntimeEvent, RuntimeObservation


@dataclass(frozen=True)
class WorkflowFamily:
    workflow_family_id: str
    tenant_id: str
    task_id: str
    cmm_context_id: str | None = None
    status: str = "ACTIVE"


@dataclass(frozen=True)
class RegistrySnapshot:
    snapshot_id: str
    workflow_family_id: str
    snapshot_hash: str
    payload_ref: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowVersion:
    workflow_family_id: str
    version: int
    registry_snapshot_id: str
    ir_hash: str
    status: str
    parent_version: int | None = None
    structure_ref: str | None = None


@dataclass(frozen=True)
class Execution:
    execution_id: str
    workflow_family_id: str
    workflow_version: int
    tenant_id: str
    status: str
    correlation_id: str | None = None
    client_request_id: str = ""
    n8n_execution_id: str | None = None
    registry_snapshot_id: str = ""
    ir_hash: str = ""
    workflow_hash: str = ""
    execution_mode: str = "PRODUCTION"
    request_hash: str = ""
    operation_key: str = ""
    engine: str = ""
    engine_workflow_id: str = ""
    engine_execution_id: str | None = None
    engine_status: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class NodeExecution:
    execution_id: str
    node_id: str
    status: str = "PENDING"
    agent_id: str | None = None
    agent_version: str | None = None
    current_attempt: int | None = None
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class NodeAttempt:
    execution_id: str
    node_id: str
    attempt_no: int
    idempotency_key: str
    status: str = "PENDING"
    retry_count: int = 0
    error_code: str | None = None
    error_signature: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    next_retry_at: datetime | None = None
    retry_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    execution_id: str
    node_id: str
    artifact_refs: list[str] = field(default_factory=list)
    input_fingerprint: str | None = None
    trusted: bool = False


@dataclass(frozen=True)
class SelfTestRun:
    self_test_id: str
    workflow_family_id: str
    workflow_version: int
    attempt_no: int
    level: str
    passed: bool
    failure_stage: str | None = None
    error_signature: str | None = None
    repair_action: dict[str, Any] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextOutboxEvent:
    event_id: str
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    status: str = "PENDING"

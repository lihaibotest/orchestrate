from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


class ContextEventValidationError(ValueError):
    pass


class ContextEventType:
    WORKFLOW_VERSION_CREATED = "WORKFLOW_VERSION_CREATED"
    WORKFLOW_SELF_TESTED = "WORKFLOW_SELF_TESTED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    NODE_STATE_CHANGED = "NODE_STATE_CHANGED"
    NODE_EVALUATED = "NODE_EVALUATED"
    ARTIFACT_REGISTERED = "ARTIFACT_REGISTERED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    WORKFLOW_REPAIR_APPLIED = "WORKFLOW_REPAIR_APPLIED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    MEMORY_SUMMARY_UPDATED = "MEMORY_SUMMARY_UPDATED"


CONTEXT_EVENT_TYPES = (
    ContextEventType.WORKFLOW_VERSION_CREATED,
    ContextEventType.WORKFLOW_SELF_TESTED,
    ContextEventType.EXECUTION_STARTED,
    ContextEventType.NODE_STATE_CHANGED,
    ContextEventType.NODE_EVALUATED,
    ContextEventType.ARTIFACT_REGISTERED,
    ContextEventType.CHECKPOINT_CREATED,
    ContextEventType.WORKFLOW_REPAIR_APPLIED,
    ContextEventType.WORKFLOW_COMPLETED,
    ContextEventType.MEMORY_SUMMARY_UPDATED,
)

_SENSITIVE = frozenset({
    "password", "token", "api_key", "apikey", "access_key", "secret",
    "private_key", "authorization", "cookie", "credential", "credential_value",
})
_EVALUATOR = frozenset({
    "evaluator", "evaluation", "evaluation_id", "quality", "quality_score",
    "qualityscore", "quality_verdict", "verdict", "opinion", "recommendation",
    "reusable", "reuse_decision", "semantic_score",
})
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?<![a-z0-9])sk-[a-z0-9_-]{8,})"
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item))


def _validate_payload(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE and not normalized.endswith("_ref"):
                raise ContextEventValidationError("ContextEvent payload contains a Secret field")
            if normalized in _EVALUATOR:
                raise ContextEventValidationError("ContextEvent payload contains evaluator-owned fields")
            _validate_payload(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_payload(nested)
    elif isinstance(value, str) and _SENSITIVE_TEXT.search(value):
        raise ContextEventValidationError("ContextEvent payload contains a credential value")


def stable_context_event_id(
    event_type: str,
    tenant_id: str,
    workflow_family_id: str,
    workflow_version: int,
    execution_id: str | None,
    node_id: str | None,
    payload: Mapping[str, object],
) -> str:
    material = _canonical({
        "event_type": event_type,
        "tenant_id": tenant_id,
        "workflow_family_id": workflow_family_id,
        "workflow_version": workflow_version,
        "execution_id": execution_id,
        "node_id": node_id,
        "payload": payload,
    })
    return "context-event-" + hashlib.sha256(material.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ContextEvent:
    event_id: str
    event_type: str
    tenant_id: str
    correlation_id: str
    workflow_family_id: str
    workflow_version: int
    execution_id: str | None = None
    node_id: str | None = None
    attempt_no: int | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        tenant_id: str,
        correlation_id: str,
        workflow_family_id: str,
        workflow_version: int,
        execution_id: str | None = None,
        node_id: str | None = None,
        attempt_no: int | None = None,
        payload: Mapping[str, object] | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> "ContextEvent":
        safe_payload = dict(payload or {})
        resolved_id = event_id or stable_context_event_id(
            event_type, tenant_id, workflow_family_id, workflow_version,
            execution_id, node_id, safe_payload,
        )
        return cls(
            resolved_id, event_type, tenant_id, correlation_id, workflow_family_id,
            workflow_version, execution_id, node_id, attempt_no, safe_payload,
            occurred_at or datetime.now(timezone.utc),
        )

    def __post_init__(self) -> None:
        if self.event_type not in CONTEXT_EVENT_TYPES:
            raise ContextEventValidationError(f"unsupported ContextEvent type: {self.event_type}")
        for name in ("event_id", "tenant_id", "correlation_id", "workflow_family_id"):
            if not str(getattr(self, name)).strip():
                raise ContextEventValidationError(f"{name} must not be empty")
        if self.workflow_version < 1 or (self.attempt_no is not None and self.attempt_no < 1):
            raise ContextEventValidationError("invalid ContextEvent version or attempt")
        _validate_payload(self.payload)
        object.__setattr__(self, "payload", dict(self.payload))

    def as_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "workflow_family_id": self.workflow_family_id,
            "workflow_version": self.workflow_version,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "attempt_no": self.attempt_no,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": dict(self.payload),
        }

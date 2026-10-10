from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


RUNTIME_EVENT_SOURCES = frozenset(
    {"WORKFLOW_ENGINE", "AGENT_RUNTIME", "ARTIFACT_STORE", "LEGACY"}
)
RUNTIME_EVENT_PROCESSING_STATES = frozenset(
    {"RECEIVED", "NORMALIZED", "PROCESSED", "IGNORED", "FAILED_PROCESSING"}
)
OBSERVED_STATES = frozenset(
    {
        "WORKFLOW_RUNNING",
        "WORKFLOW_SUCCEEDED",
        "WORKFLOW_FAILED",
        "ENGINE_NODE_RUNNING",
        "ENGINE_NODE_SUCCEEDED",
        "ENGINE_NODE_FAILED",
        "AGENT_RUNNING",
        "AGENT_SUCCEEDED",
        "AGENT_FAILED",
        "ARTIFACT_CREATED",
        "ARTIFACT_VALIDATED",
        "ARTIFACT_INVALID",
    }
)


class ReconciliationStatus:
    MATCHED = "MATCHED"
    EVENT_MISSING = "EVENT_MISSING"
    STALLED = "STALLED"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    UNCERTAIN = "UNCERTAIN"
    ALL = frozenset({MATCHED, EVENT_MISSING, STALLED, ARTIFACT_MISMATCH, UNCERTAIN})


class RuntimeEventError(RuntimeError):
    pass


class RuntimeEventConflict(RuntimeEventError):
    pass


class RuntimeEventNotFound(RuntimeEventError, KeyError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_payload(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    )


def hash_payload(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_payload(value).encode("ascii")).hexdigest()


_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "authorization",
        "cookie",
        "secret",
        "credential",
        "credential_value",
    }
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]+|(?<![a-z0-9])sk-[a-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|([?&])(token|api_key|secret|password)=([^&\s]+))"
)


def redact_runtime_payload(value: object) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, nested in value.items():
            name = str(key)
            normalized = name.strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS and not normalized.endswith("_ref"):
                redacted[name] = "[REDACTED]"
            else:
                redacted[name] = redact_runtime_payload(nested)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_runtime_payload(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub("[REDACTED]", value)
    return value


@dataclass(frozen=True)
class RuntimeEvent:
    """One immutable external fact. The first fields preserve the P0-02 API."""

    event_id: str
    execution_id: str
    event_type: str
    workflow_version: int
    occurred_at: datetime
    node_id: str | None = None
    agent_id: str | None = None
    attempt_no: int | None = None
    trace_id: str | None = None
    request_id: str | None = None
    error_code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "LEGACY"
    tenant_id: str = ""
    correlation_id: str = ""
    external_reference: str | None = None
    received_at: datetime = field(default_factory=utc_now)
    payload_hash: str = ""
    version: int = 1
    processing_status: str = "RECEIVED"
    processing_retry_count: int = 0
    processing_error_code: str | None = None
    processing_error_message: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "execution_id", "event_type"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        if self.workflow_version < 1 or self.version < 1:
            raise ValueError("Runtime Event versions must be positive")
        if self.attempt_no is not None and self.attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        if self.source not in RUNTIME_EVENT_SOURCES:
            raise ValueError(f"unsupported Runtime Event source: {self.source}")
        if self.processing_status not in RUNTIME_EVENT_PROCESSING_STATES:
            raise ValueError("unsupported Runtime Event processing status")
        if self.processing_retry_count < 0:
            raise ValueError("processing_retry_count must not be negative")
        if self.source != "LEGACY":
            if not self.tenant_id.strip() or not self.correlation_id.strip():
                raise ValueError("tenant_id and correlation_id are required")
            if not self.payload_hash.strip():
                raise ValueError("payload_hash is required")
        object.__setattr__(self, "payload", dict(self.payload))

    def with_processing(
        self,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_retry: bool = False,
    ) -> "RuntimeEvent":
        return replace(
            self,
            processing_status=status,
            processing_retry_count=self.processing_retry_count + int(increment_retry),
            processing_error_code=error_code,
            processing_error_message=error_message,
        )


@dataclass(frozen=True)
class RuntimeObservation:
    observation_id: str
    event_id: str
    tenant_id: str
    correlation_id: str
    execution_id: str
    observed_state: str
    source: str
    occurred_at: datetime
    node_id: str | None = None
    attempt_no: int | None = None
    external_reference: str | None = None
    confidence: str = "AUTHORITATIVE_SOURCE"
    evidence: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.observed_state not in OBSERVED_STATES:
            raise ValueError(f"unsupported observed state: {self.observed_state}")
        if self.source not in RUNTIME_EVENT_SOURCES - {"LEGACY"}:
            raise ValueError(f"unsupported observation source: {self.source}")
        if self.attempt_no is not None and self.attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def kind(self) -> str:
        return self.observed_state

    @property
    def error_code(self) -> str | None:
        value = self.evidence.get("error_code")
        return None if value is None else str(value)

    @property
    def error_signature(self) -> str | None:
        value = self.evidence.get("error_signature")
        return None if value is None else str(value)

    def evidence_dict(self) -> dict[str, object]:
        return dict(self.evidence)


@dataclass(frozen=True)
class RuntimeFact:
    """A read-only fact supplied by a runtime source during reconciliation."""

    source: str
    execution_id: str
    state: str
    occurred_at: datetime
    node_id: str | None = None
    attempt_no: int | None = None
    external_reference: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)
    tenant_id: str = ""

    def __post_init__(self) -> None:
        if self.source not in RUNTIME_EVENT_SOURCES - {"LEGACY"}:
            raise ValueError(f"unsupported Runtime Fact source: {self.source}")
        if not self.execution_id.strip() or not self.state.strip():
            raise ValueError("Runtime Fact execution_id and state are required")
        if self.tenant_id and not self.tenant_id.strip():
            raise ValueError("tenant_id must not be blank when supplied")
        if self.attempt_no is not None and self.attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        object.__setattr__(self, "evidence", dict(self.evidence))


def observation_id(event_id: str) -> str:
    return "observation-" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()

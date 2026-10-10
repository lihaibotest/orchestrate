from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from typing import Mapping

from domain.execution_ir import freeze_mapping, thaw_json


ARTIFACT_STATES = frozenset(
    {"CREATED", "VALIDATING", "VALIDATED", "CONSUMED", "INVALID", "DELETED"}
)
VALIDATION_STATUSES = frozenset({"PENDING", "PASS", "FAIL"})
EXECUTION_MODES = frozenset({"PRODUCTION", "SELF_TEST"})


class ArtifactError(RuntimeError):
    """Base error for Artifact management."""


class ArtifactNotFound(ArtifactError):
    """The tenant-scoped Artifact does not exist."""


class ArtifactIdentityConflict(ArtifactError):
    """A stable Artifact identity was reused for different content."""


class ArtifactValidationFailed(ArtifactError):
    def __init__(self, result: "ArtifactValidationResult") -> None:
        self.result = result
        super().__init__(result.failure_reason or "artifact validation failed")


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _optional(value: str | None, name: str) -> str | None:
    return None if value is None else _required(value, name)


def stable_artifact_id(
    execution_id: str, node_id: str, attempt_no: int, output_name: str
) -> str:
    if attempt_no < 1:
        raise ValueError("attempt_no must be positive")
    material = "\x1f".join(
        (
            _required(execution_id, "execution_id"),
            _required(node_id, "node_id"),
            str(attempt_no),
            _required(output_name, "output_name"),
        )
    )
    return "artifact-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_storage_segment(value: str, name: str) -> str:
    normalized = _required(value, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
        raise ValueError(f"{name} is not safe for an Artifact namespace")
    return normalized


def reject_sensitive_metadata(value: Mapping[str, object]) -> None:
    forbidden = {
        "password",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "secret",
        "credential_value",
        "authorization",
    }

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in forbidden and not normalized.endswith("_ref"):
                    raise ValueError("Artifact metadata must not contain credential values")
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)


@dataclass(frozen=True)
class ExpectedArtifactOutput:
    output_name: str
    artifact_type: str = "FILE"
    media_type: str = "application/octet-stream"
    schema_ref: str | None = None
    schema: Mapping[str, object] = field(default_factory=dict)
    required: bool = True
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_name", _required(self.output_name, "output_name"))
        object.__setattr__(self, "artifact_type", _required(self.artifact_type, "artifact_type"))
        object.__setattr__(self, "media_type", _required(self.media_type, "media_type").lower())
        object.__setattr__(self, "schema_ref", _optional(self.schema_ref, "schema_ref"))
        object.__setattr__(self, "schema", freeze_mapping(self.schema))
        if self.min_size_bytes is not None and self.min_size_bytes < 0:
            raise ValueError("min_size_bytes must not be negative")
        if self.max_size_bytes is not None and self.max_size_bytes < 0:
            raise ValueError("max_size_bytes must not be negative")
        if (
            self.min_size_bytes is not None
            and self.max_size_bytes is not None
            and self.min_size_bytes > self.max_size_bytes
        ):
            raise ValueError("min_size_bytes must not exceed max_size_bytes")


@dataclass(frozen=True)
class WholeArtifactBinding:
    input_name: str
    source_node_id: str
    source_output_name: str
    binding_mode: str = "WHOLE_ARTIFACT"

    def __post_init__(self) -> None:
        for name in ("input_name", "source_node_id", "source_output_name"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.binding_mode != "WHOLE_ARTIFACT":
            raise ValueError("only WHOLE_ARTIFACT bindings are supported")


@dataclass(frozen=True)
class ProducedArtifact:
    output_name: str
    content: bytes
    media_type: str
    schema_ref: str | None = None
    content_hash: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_name", _required(self.output_name, "output_name"))
        object.__setattr__(self, "media_type", _required(self.media_type, "media_type").lower())
        object.__setattr__(self, "schema_ref", _optional(self.schema_ref, "schema_ref"))
        if not isinstance(self.content, bytes):
            raise TypeError("Artifact content must be bytes")
        digest = content_sha256(self.content)
        if self.content_hash is not None and self.content_hash.lower() != digest:
            raise ValueError("produced Artifact content_hash does not match content")
        object.__setattr__(self, "content_hash", digest)
        reject_sensitive_metadata(self.metadata)
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class ArtifactPutRequest:
    tenant_id: str
    workflow_family_id: str
    workflow_version: int
    execution_id: str
    execution_mode: str
    node_id: str
    attempt_no: int
    output: ProducedArtifact
    expected: ExpectedArtifactOutput

    def __post_init__(self) -> None:
        for name in ("tenant_id", "workflow_family_id", "execution_id", "node_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.workflow_version < 1 or self.attempt_no < 1:
            raise ValueError("workflow_version and attempt_no must be positive")
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be PRODUCTION or SELF_TEST")
        if self.output.output_name != self.expected.output_name:
            raise ValueError("produced output name differs from its expected output")


@dataclass(frozen=True)
class ArtifactIdentity:
    artifact_id: str
    artifact_type: str
    media_type: str
    schema_ref: str | None
    content_hash: str
    size_bytes: int
    storage_ref: str
    tenant_id: str
    workflow_family_id: str
    workflow_version: int
    execution_mode: str
    output_name: str
    producer_execution_id: str
    producer_node_id: str
    producer_attempt_no: int
    status: str = "CREATED"
    validation_status: str = "PENDING"
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "artifact_type",
            "media_type",
            "content_hash",
            "storage_ref",
            "tenant_id",
            "workflow_family_id",
            "output_name",
            "producer_execution_id",
            "producer_node_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "schema_ref", _optional(self.schema_ref, "schema_ref"))
        if self.workflow_version < 1 or self.producer_attempt_no < 1 or self.size_bytes < 0:
            raise ValueError("Artifact numeric identity fields are invalid")
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError("invalid Artifact execution_mode")
        if self.status not in ARTIFACT_STATES or self.validation_status not in VALIDATION_STATUSES:
            raise ValueError("invalid Artifact lifecycle state")
        reject_sensitive_metadata(self.metadata)
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def metadata_dict(self) -> dict[str, object]:
        result = thaw_json(self.metadata)
        if not isinstance(result, dict):
            raise TypeError("Artifact metadata must be an object")
        return result


@dataclass(frozen=True, init=False)
class ArtifactRef:
    artifact_id: str
    storage_ref: str
    content_hash: str
    producer_execution_id: str
    producer_node_id: str
    producer_attempt_no: int
    validation_status: str
    media_type: str
    schema_ref: str | None = None
    size_bytes: int = 0
    schema_id: str | None = None
    schema_version: str | None = None
    quality_status: str | None = None
    reusable: bool = False
    input_fingerprint: str | None = None
    permission_scope: Mapping[str, object] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __init__(self, *args: object, **kwargs: object) -> None:
        # P0-02 compatibility: (id, execution, node, attempt, uri, hash, ...).
        if len(args) >= 6 and isinstance(args[3], int):
            artifact_id, execution_id, node_id, attempt_no, uri, digest = args[:6]
            values = {
                "artifact_id": artifact_id,
                "storage_ref": uri,
                "content_hash": digest,
                "producer_execution_id": execution_id,
                "producer_node_id": node_id,
                "producer_attempt_no": attempt_no,
                "validation_status": kwargs.pop("validation_status", "PENDING"),
                "media_type": kwargs.pop("media_type", "application/octet-stream"),
                "schema_ref": kwargs.pop("schema_ref", None),
                "size_bytes": kwargs.pop("size_bytes", 0),
            }
        else:
            names = (
                "artifact_id",
                "storage_ref",
                "content_hash",
                "producer_execution_id",
                "producer_node_id",
                "producer_attempt_no",
                "validation_status",
                "media_type",
                "schema_ref",
                "size_bytes",
            )
            values = {name: value for name, value in zip(names, args)}
            for name in names[len(args) :]:
                if name in kwargs:
                    values[name] = kwargs.pop(name)
            values.setdefault("schema_ref", None)
            values.setdefault("size_bytes", 0)
        legacy_defaults = {
            "schema_id": None,
            "schema_version": None,
            "quality_status": None,
            "reusable": False,
            "input_fingerprint": None,
            "permission_scope": None,
            "metadata": {},
        }
        for name, default in legacy_defaults.items():
            values[name] = kwargs.pop(name, default)
        if kwargs:
            raise TypeError(f"unexpected ArtifactRef arguments: {', '.join(sorted(kwargs))}")
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def execution_id(self) -> str:
        return str(self.producer_execution_id)

    @property
    def node_id(self) -> str:
        return str(self.producer_node_id)

    @property
    def attempt_no(self) -> int:
        return int(self.producer_attempt_no)

    @property
    def uri(self) -> str:
        return str(self.storage_ref)

    @classmethod
    def from_identity(cls, identity: ArtifactIdentity) -> "ArtifactRef":
        return cls(
            artifact_id=identity.artifact_id,
            storage_ref=identity.storage_ref,
            content_hash=identity.content_hash,
            producer_execution_id=identity.producer_execution_id,
            producer_node_id=identity.producer_node_id,
            producer_attempt_no=identity.producer_attempt_no,
            validation_status=identity.validation_status,
            media_type=identity.media_type,
            schema_ref=identity.schema_ref,
            size_bytes=identity.size_bytes,
        )


@dataclass(frozen=True)
class ArtifactValidationCheck:
    validation_type: str
    passed: bool
    error_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ArtifactValidationResult:
    artifact_id: str
    passed: bool
    checks: tuple[ArtifactValidationCheck, ...]
    error_code: str | None = None
    failure_reason: str | None = None

    @classmethod
    def from_checks(
        cls, artifact_id: str, checks: tuple[ArtifactValidationCheck, ...]
    ) -> "ArtifactValidationResult":
        failed = next((check for check in checks if not check.passed), None)
        return cls(
            artifact_id,
            failed is None,
            checks,
            None if failed is None else failed.error_code,
            None if failed is None else failed.reason,
        )

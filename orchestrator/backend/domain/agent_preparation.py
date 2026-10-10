from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from types import MappingProxyType
from typing import Mapping


class AgentPreparationError(RuntimeError):
    error_code = "AGENT_PREPARATION_FAILED"


class AgentNotFound(AgentPreparationError):
    error_code = "AGENT_NOT_FOUND"


class AgentRegistrationFailed(AgentPreparationError):
    error_code = "AGENT_REGISTRATION_FAILED"


class AgentStartFailed(AgentPreparationError):
    error_code = "AGENT_START_FAILED"


class AgentNotHealthy(AgentPreparationError):
    error_code = "AGENT_NOT_HEALTHY"


class AgentNotReady(AgentPreparationError):
    error_code = "AGENT_NOT_READY"


class AgentUnreachable(AgentPreparationError):
    error_code = "AGENT_UNREACHABLE"


class InvalidAgentSchema(AgentPreparationError):
    error_code = "INVALID_AGENT_SCHEMA"


class MissingCredentialRef(AgentPreparationError):
    error_code = "MISSING_CREDENTIAL_REF"


class InvalidInvocationPolicy(AgentPreparationError):
    error_code = "INVALID_INVOCATION_POLICY"


class SnapshotCreationFailed(AgentPreparationError):
    error_code = "SNAPSHOT_CREATION_FAILED"


class InvalidAgentRequirement(AgentPreparationError):
    error_code = "INVALID_AGENT_REQUIREMENT"


def _require_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("value must be a mapping")
    return frozen


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        thaw_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


@dataclass(frozen=True)
class AgentRequirement:
    requirement_id: str
    capability: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _require_text(self.requirement_id, "requirement_id"))
        object.__setattr__(self, "capability", _require_text(self.capability, "capability"))


@dataclass(frozen=True)
class AgentCandidate:
    agent_id: str
    version: str
    capabilities: tuple[str, ...]
    endpoint: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    credential_ref: str
    invocation_policy: Mapping[str, object]
    registered: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _require_text(self.agent_id, "agent_id"))
        object.__setattr__(self, "version", _require_text(self.version, "version"))
        capabilities = tuple(sorted({_require_text(item, "capability") for item in self.capabilities}))
        if not capabilities:
            raise ValueError("capabilities must not be empty")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "endpoint", self.endpoint.strip())
        object.__setattr__(self, "input_schema", _freeze_mapping(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze_mapping(self.output_schema))
        object.__setattr__(self, "credential_ref", self.credential_ref.strip())
        object.__setattr__(self, "invocation_policy", _freeze_mapping(self.invocation_policy))

    @property
    def identity(self) -> tuple[str, str]:
        return (self.agent_id, self.version)


@dataclass(frozen=True)
class AgentBinding:
    requirement_id: str
    agent_id: str
    version: str
    endpoint: str
    capability: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    credential_ref: str
    invocation_policy: Mapping[str, object]

    def __post_init__(self) -> None:
        for field_name in (
            "requirement_id",
            "agent_id",
            "version",
            "endpoint",
            "capability",
            "credential_ref",
        ):
            object.__setattr__(self, field_name, _require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "input_schema", _freeze_mapping(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze_mapping(self.output_schema))
        object.__setattr__(self, "invocation_policy", _freeze_mapping(self.invocation_policy))

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "agent_id": self.agent_id,
            "version": self.version,
            "endpoint": self.endpoint,
            "capability": self.capability,
            "input_schema": thaw_json(self.input_schema),
            "output_schema": thaw_json(self.output_schema),
            "credential_ref": self.credential_ref,
            "invocation_policy": thaw_json(self.invocation_policy),
        }


@dataclass(frozen=True)
class RegistrySnapshot:
    snapshot_id: str
    workflow_family_id: str
    bindings: tuple[AgentBinding, ...]
    snapshot_hash: str
    created_at: datetime
    hash_algorithm: str = "sha256"

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _require_text(self.snapshot_id, "snapshot_id"))
        object.__setattr__(
            self,
            "workflow_family_id",
            _require_text(self.workflow_family_id, "workflow_family_id"),
        )
        bindings = tuple(sorted(self.bindings, key=lambda item: item.requirement_id))
        requirement_ids = [item.requirement_id for item in bindings]
        if not bindings or len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("snapshot bindings must have unique requirement IDs")
        if self.hash_algorithm != "sha256":
            raise ValueError("unsupported snapshot hash algorithm")
        if len(self.snapshot_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.snapshot_hash
        ):
            raise ValueError("snapshot_hash must be a SHA-256 hex digest")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "bindings", bindings)

    def content_payload(self) -> dict[str, object]:
        return {
            "format_version": "registry-snapshot.v1",
            "workflow_family_id": self.workflow_family_id,
            "hash_algorithm": self.hash_algorithm,
            "bindings": [binding.as_dict() for binding in self.bindings],
        }


@dataclass(frozen=True)
class CandidateRejection:
    agent_id: str
    version: str
    error_code: str


@dataclass(frozen=True)
class PreparationResult:
    snapshot: RegistrySnapshot
    rejections: tuple[CandidateRejection, ...] = field(default_factory=tuple)


def validate_json_schema(schema: Mapping[str, object], field_name: str) -> None:
    document = thaw_json(schema)
    if not isinstance(document, dict) or not document:
        raise InvalidAgentSchema(f"{field_name} must be a non-empty JSON Schema object")
    try:
        canonical_json(document)
    except (TypeError, ValueError) as error:
        raise InvalidAgentSchema(f"{field_name} is not canonical JSON") from error
    schema_type = document.get("type")
    if schema_type is not None and schema_type not in {
        "object", "array", "string", "number", "integer", "boolean", "null"
    }:
        raise InvalidAgentSchema(f"{field_name}.type is invalid")
    if schema_type is None and not any(
        key in document for key in ("$ref", "oneOf", "anyOf", "allOf", "const", "enum")
    ):
        raise InvalidAgentSchema(f"{field_name} has no schema type or composition")
    if schema_type == "object":
        properties = document.get("properties", {})
        required = document.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise InvalidAgentSchema(f"{field_name} object keywords are invalid")
        if any(not isinstance(item, str) or item not in properties for item in required):
            raise InvalidAgentSchema(f"{field_name}.required references an undefined property")


def validate_candidate_contract(candidate: AgentCandidate) -> None:
    validate_json_schema(candidate.input_schema, "input_schema")
    validate_json_schema(candidate.output_schema, "output_schema")
    if not candidate.credential_ref:
        raise MissingCredentialRef(f"credential_ref is missing for {candidate.agent_id}")
    if not candidate.endpoint:
        raise AgentUnreachable(f"endpoint is missing for {candidate.agent_id}")
    if not candidate.invocation_policy:
        raise InvalidInvocationPolicy(f"invocation_policy is missing for {candidate.agent_id}")
    try:
        canonical_json(candidate.invocation_policy)
    except (TypeError, ValueError) as error:
        raise InvalidInvocationPolicy(
            f"invocation_policy is invalid for {candidate.agent_id}"
        ) from error


def binding_from_candidate(
    requirement: AgentRequirement, candidate: AgentCandidate
) -> AgentBinding:
    return AgentBinding(
        requirement_id=requirement.requirement_id,
        agent_id=candidate.agent_id,
        version=candidate.version,
        endpoint=candidate.endpoint,
        capability=requirement.capability,
        input_schema=candidate.input_schema,
        output_schema=candidate.output_schema,
        credential_ref=candidate.credential_ref,
        invocation_policy=candidate.invocation_policy,
    )


def build_registry_snapshot(
    workflow_family_id: str,
    bindings: tuple[AgentBinding, ...],
    *,
    snapshot_id: str,
    created_at: datetime,
) -> RegistrySnapshot:
    family_id = _require_text(workflow_family_id, "workflow_family_id")
    ordered = tuple(sorted(bindings, key=lambda item: item.requirement_id))
    payload = {
        "format_version": "registry-snapshot.v1",
        "workflow_family_id": family_id,
        "hash_algorithm": "sha256",
        "bindings": [binding.as_dict() for binding in ordered],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return RegistrySnapshot(
        snapshot_id=snapshot_id,
        workflow_family_id=family_id,
        bindings=ordered,
        snapshot_hash=digest,
        created_at=created_at,
    )

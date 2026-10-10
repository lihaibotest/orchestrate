from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Mapping


IR_NODE_TYPES = frozenset(
    {
        "invoke_agent",
        "load_artifact",
        "store_artifact",
        "evaluate",
        "checkpoint",
        "decision",
        "join",
        "wait",
        "end",
    }
)


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen = freeze_json(value)
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
class AgentBindingRef:
    registry_snapshot_id: str
    requirement_id: str
    agent_id: str
    version: str
    endpoint: str
    capability: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    credential_ref: str

    def __post_init__(self) -> None:
        for name in (
            "registry_snapshot_id",
            "requirement_id",
            "agent_id",
            "version",
            "endpoint",
            "capability",
            "credential_ref",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "input_schema", freeze_mapping(self.input_schema))
        object.__setattr__(self, "output_schema", freeze_mapping(self.output_schema))

    def as_dict(self) -> dict[str, object]:
        return {
            "registry_snapshot_id": self.registry_snapshot_id,
            "requirement_id": self.requirement_id,
            "agent_id": self.agent_id,
            "version": self.version,
            "endpoint": self.endpoint,
            "capability": self.capability,
            "input_schema": thaw_json(self.input_schema),
            "output_schema": thaw_json(self.output_schema),
            "credential_ref": self.credential_ref,
        }


@dataclass(frozen=True)
class DataContract:
    schema: Mapping[str, object]
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", freeze_mapping(self.schema))
        normalized_sources = {
            _text(str(key), "source field"): _text(str(value), "source reference")
            for key, value in self.sources.items()
        }
        object.__setattr__(self, "sources", MappingProxyType(normalized_sources))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": thaw_json(self.schema),
            "sources": dict(self.sources),
        }


@dataclass(frozen=True)
class ArtifactPolicy:
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    registration_required: bool = True
    reuse_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "produces",
            tuple(sorted(_text(item, "produced artifact ID") for item in self.produces)),
        )
        object.__setattr__(
            self,
            "consumes",
            tuple(sorted(_text(item, "consumed artifact ID") for item in self.consumes)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "produces": list(self.produces),
            "consumes": list(self.consumes),
            "registration_required": self.registration_required,
            "reuse_allowed": self.reuse_allowed,
        }


@dataclass(frozen=True)
class CheckpointPolicy:
    enabled: bool = False
    required_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_artifacts",
            tuple(
                sorted(
                    _text(item, "checkpoint required artifact")
                    for item in self.required_artifacts
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "required_artifacts": list(self.required_artifacts),
        }


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: int = 0
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.backoff_seconds < 0:
            raise ValueError("retry values must not be negative")
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "max_retries": self.max_retries,
            "backoff_seconds": self.backoff_seconds,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class MonitorPolicy:
    heartbeat_required: bool = False
    runtime_events_required: bool = True
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("monitor timeout_seconds must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "heartbeat_required": self.heartbeat_required,
            "runtime_events_required": self.runtime_events_required,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class IRNode:
    node_id: str
    name: str
    node_type: str
    depends_on: tuple[str, ...]
    input_contract: DataContract
    output_contract: DataContract
    completion_criteria: Mapping[str, object]
    artifact_policy: ArtifactPolicy
    agent_binding: AgentBindingRef | None = None
    checkpoint_policy: CheckpointPolicy | None = None
    retry_policy: RetryPolicy | None = None
    monitor_policy: MonitorPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _text(self.node_id, "node_id"))
        object.__setattr__(self, "name", _text(self.name, "name"))
        node_type = _text(self.node_type, "node_type")
        if node_type not in IR_NODE_TYPES:
            raise ValueError(f"unsupported IR node type: {node_type}")
        object.__setattr__(self, "node_type", node_type)
        object.__setattr__(
            self,
            "depends_on",
            tuple(sorted(_text(item, "dependency") for item in self.depends_on)),
        )
        object.__setattr__(
            self,
            "completion_criteria",
            freeze_mapping(self.completion_criteria),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "type": self.node_type,
            "agent_binding": (
                None if self.agent_binding is None else self.agent_binding.as_dict()
            ),
            "depends_on": list(self.depends_on),
            "input_contract": self.input_contract.as_dict(),
            "output_contract": self.output_contract.as_dict(),
            "completion_criteria": thaw_json(self.completion_criteria),
            "artifact_policy": self.artifact_policy.as_dict(),
            "checkpoint_policy": (
                None
                if self.checkpoint_policy is None
                else self.checkpoint_policy.as_dict()
            ),
            "retry_policy": (
                None if self.retry_policy is None else self.retry_policy.as_dict()
            ),
            "monitor_policy": (
                None if self.monitor_policy is None else self.monitor_policy.as_dict()
            ),
        }


@dataclass(frozen=True)
class IREdge:
    source: str
    target: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "edge source"))
        object.__setattr__(self, "target", _text(self.target, "edge target"))

    def as_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target}


@dataclass(frozen=True)
class ArtifactDeclaration:
    artifact_id: str
    producer_node_id: str | None
    schema: Mapping[str, object]
    reusable: bool
    output_name: str | None = None
    artifact_type: str = "FILE"
    media_type: str = "application/octet-stream"
    schema_ref: str | None = None
    required: bool = True
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    uri: str | None = None
    content_hash: str | None = None
    input_fingerprint: str | None = None
    permission_scope: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, "artifact_id"))
        object.__setattr__(
            self,
            "producer_node_id",
            _optional_text(self.producer_node_id, "producer_node_id"),
        )
        object.__setattr__(self, "schema", freeze_mapping(self.schema))
        derived_name = self.artifact_id.split(":", 1)[-1]
        object.__setattr__(
            self,
            "output_name",
            _text(self.output_name or derived_name, "output_name"),
        )
        object.__setattr__(self, "artifact_type", _text(self.artifact_type, "artifact_type"))
        object.__setattr__(self, "media_type", _text(self.media_type, "media_type").lower())
        for name in ("schema_ref", "uri", "content_hash", "input_fingerprint", "permission_scope"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
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

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "producer_node_id": self.producer_node_id,
            "output_name": self.output_name,
            "artifact_type": self.artifact_type,
            "media_type": self.media_type,
            "schema_ref": self.schema_ref,
            "schema": thaw_json(self.schema),
            "required": self.required,
            "min_size_bytes": self.min_size_bytes,
            "max_size_bytes": self.max_size_bytes,
            "reusable": self.reusable,
            "uri": self.uri,
            "content_hash": self.content_hash,
            "input_fingerprint": self.input_fingerprint,
            "permission_scope": self.permission_scope,
        }


@dataclass(frozen=True)
class CheckpointDeclaration:
    checkpoint_id: str
    node_id: str
    artifact_ids: tuple[str, ...]
    trusted: bool
    input_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "checkpoint_id", _text(self.checkpoint_id, "checkpoint_id")
        )
        object.__setattr__(self, "node_id", _text(self.node_id, "checkpoint node_id"))
        object.__setattr__(
            self,
            "artifact_ids",
            tuple(sorted(_text(item, "checkpoint artifact ID") for item in self.artifact_ids)),
        )
        object.__setattr__(
            self,
            "input_fingerprint",
            _optional_text(self.input_fingerprint, "checkpoint input_fingerprint"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "node_id": self.node_id,
            "artifact_ids": list(self.artifact_ids),
            "trusted": self.trusted,
            "input_fingerprint": self.input_fingerprint,
        }


@dataclass(frozen=True)
class RecoveryMetadata:
    resume_root: str | None = None
    reused_nodes: tuple[str, ...] = ()
    invalidated_nodes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resume_root", _optional_text(self.resume_root, "resume_root")
        )
        object.__setattr__(
            self,
            "reused_nodes",
            tuple(sorted(_text(item, "reused node") for item in self.reused_nodes)),
        )
        object.__setattr__(
            self,
            "invalidated_nodes",
            tuple(
                sorted(_text(item, "invalidated node") for item in self.invalidated_nodes)
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "resume_root": self.resume_root,
            "reused_nodes": list(self.reused_nodes),
            "invalidated_nodes": list(self.invalidated_nodes),
        }


@dataclass(frozen=True)
class ExecutionIR:
    ir_version: str
    workflow_family_id: str
    workflow_version: int
    task_id: str
    registry_snapshot_id: str
    nodes: tuple[IRNode, ...]
    edges: tuple[IREdge, ...]
    completion_criteria: Mapping[str, object]
    context_view_id: str | None = None
    artifacts: tuple[ArtifactDeclaration, ...] = ()
    checkpoints: tuple[CheckpointDeclaration, ...] = ()
    recovery: RecoveryMetadata = field(default_factory=RecoveryMetadata)

    def __post_init__(self) -> None:
        for name in ("ir_version", "workflow_family_id", "task_id", "registry_snapshot_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.ir_version != "1.0":
            raise ValueError("ir_version must be 1.0")
        if self.workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        object.__setattr__(
            self, "context_view_id", _optional_text(self.context_view_id, "context_view_id")
        )
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda item: item.node_id)))
        object.__setattr__(
            self,
            "edges",
            tuple(sorted(self.edges, key=lambda item: (item.source, item.target))),
        )
        object.__setattr__(
            self,
            "artifacts",
            tuple(sorted(self.artifacts, key=lambda item: item.artifact_id)),
        )
        object.__setattr__(
            self,
            "checkpoints",
            tuple(sorted(self.checkpoints, key=lambda item: item.checkpoint_id)),
        )
        object.__setattr__(
            self,
            "completion_criteria",
            freeze_mapping(self.completion_criteria),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ir_version": self.ir_version,
            "workflow_family_id": self.workflow_family_id,
            "workflow_version": self.workflow_version,
            "task_id": self.task_id,
            "registry_snapshot_id": self.registry_snapshot_id,
            "context_view_id": self.context_view_id,
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
            "artifacts": [item.as_dict() for item in self.artifacts],
            "checkpoints": [item.as_dict() for item in self.checkpoints],
            "recovery": self.recovery.as_dict(),
            "completion_criteria": thaw_json(self.completion_criteria),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.as_dict()).encode("ascii")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Mapping

from domain.artifact import ExpectedArtifactOutput, WholeArtifactBinding
from domain.execution_ir import ExecutionIR, canonical_json, freeze_mapping, thaw_json
from domain.runtime_events import RuntimeObservation


EXECUTION_MODE_PRODUCTION = "PRODUCTION"
NORMALIZED_RUNTIME_FACTS = frozenset(
    {"NODE_SUCCEEDED", "TECHNICAL_FAILURE", "NODE_FAILED", "EXECUTION_FAILED"}
)


class ExecutionControlError(RuntimeError):
    """Base error for formal execution control."""


class ExecutionIdempotencyConflict(ExecutionControlError):
    """A stable request identity was reused with different semantics."""


class ExecutionSecurityError(ExecutionControlError):
    """Runtime input or metadata contained forbidden credential material."""


class RetryLimitExceeded(ExecutionControlError):
    """The current Business Attempt exhausted its configured technical retries."""


class NodeNotInExecutionIR(ExecutionControlError):
    """A Node does not belong to the immutable IR bound to the Execution."""


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    principal_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")


@dataclass(frozen=True)
class RuntimeNodeDefinition:
    node_id: str
    agent_id: str | None = None
    agent_version: str | None = None
    credential_ref: str | None = None
    max_retries: int = 0
    backoff_seconds: int = 0
    timeout_seconds: int | None = None
    depends_on: tuple[str, ...] = ()
    expected_outputs: tuple[ExpectedArtifactOutput, ...] = ()
    input_bindings: tuple[WholeArtifactBinding, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("node_id must not be empty")
        if (self.agent_id is None) != (self.agent_version is None):
            raise ValueError("agent_id and agent_version must be provided together")
        if self.credential_ref is not None and not self.credential_ref.strip():
            raise ValueError("credential_ref must not be empty")
        if self.max_retries < 0 or self.backoff_seconds < 0:
            raise ValueError("retry configuration must not be negative")
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "depends_on", tuple(sorted(self.depends_on)))
        object.__setattr__(
            self,
            "expected_outputs",
            tuple(sorted(self.expected_outputs, key=lambda item: item.output_name)),
        )
        object.__setattr__(
            self,
            "input_bindings",
            tuple(sorted(self.input_bindings, key=lambda item: item.input_name)),
        )


@dataclass(frozen=True)
class WorkflowRuntimeDefinition:
    workflow_family_id: str
    workflow_version: int
    registry_snapshot_id: str
    ir_hash: str
    nodes: tuple[RuntimeNodeDefinition, ...]

    def __post_init__(self) -> None:
        for name in ("workflow_family_id", "registry_snapshot_id", "ir_hash"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        if self.workflow_version < 1:
            raise ValueError("workflow_version must be positive")
        ordered = tuple(sorted(self.nodes, key=lambda item: item.node_id))
        if not ordered or len({item.node_id for item in ordered}) != len(ordered):
            raise ValueError("runtime definition requires unique Nodes")
        known_nodes = {item.node_id for item in ordered}
        for item in ordered:
            if any(dependency not in known_nodes for dependency in item.depends_on):
                raise ValueError("runtime definition contains an unknown dependency")
            if any(binding.source_node_id not in known_nodes for binding in item.input_bindings):
                raise ValueError("runtime definition contains an unknown Artifact source")
        object.__setattr__(self, "nodes", ordered)


def scoped_operation_key(
    tenant_id: str,
    operation: str,
    workflow_family_id: str,
    workflow_version: int,
    request_identity: str,
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "operation": operation,
        "workflow_family_id": workflow_family_id,
        "workflow_version": workflow_version,
        "request_identity": request_identity,
    }
    return f"{operation}-" + hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


def deterministic_execution_id(
    tenant_id: str,
    workflow_family_id: str,
    workflow_version: int,
    client_request_id: str,
) -> str:
    return scoped_operation_key(
        tenant_id,
        "execution",
        workflow_family_id,
        workflow_version,
        client_request_id,
    )


def request_hash(inputs: Mapping[str, object]) -> str:
    reject_sensitive_data(inputs)
    return hashlib.sha256(canonical_json(inputs).encode("ascii")).hexdigest()


def agent_operation_key(execution_id: str, node_id: str, attempt_no: int) -> str:
    if attempt_no < 1:
        raise ValueError("attempt_no must be positive")
    return f"{execution_id}:{node_id}:{attempt_no}:invoke"


def failure_signature(
    error_code: str,
    *,
    node_id: str,
    normalized_error_class: str,
) -> str:
    payload = {
        "error_code": error_code.strip(),
        "node_id": node_id.strip(),
        "normalized_error_class": normalized_error_class.strip().lower(),
    }
    return hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


def reject_sensitive_data(value: object) -> None:
    forbidden = {
        "secret",
        "password",
        "api_key",
        "access_key",
        "private_key",
        "authorization",
        "cookie",
        "token",
        "raw_token",
        "auth_token",
    }
    pattern = re.compile(
        r"(?i)(bearer\s+[a-z0-9._-]+|(?<![a-z0-9])sk-[a-z0-9_-]{8,}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|[?&](token|api_key|secret|password)=)"
    )

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in forbidden and not normalized.endswith("_ref"):
                    raise ExecutionSecurityError(
                        "runtime data contains forbidden credential material"
                    )
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and pattern.search(item):
            raise ExecutionSecurityError(
                "runtime data contains forbidden credential material"
            )

    visit(value)


def runtime_definition_from_ir(execution_ir: ExecutionIR) -> WorkflowRuntimeDefinition:
    nodes = []
    for node in execution_ir.nodes:
        binding = node.agent_binding
        retry = node.retry_policy
        expected_outputs = tuple(
            ExpectedArtifactOutput(
                output_name=str(artifact.output_name),
                artifact_type=artifact.artifact_type,
                media_type=artifact.media_type,
                schema_ref=artifact.schema_ref,
                schema=artifact.schema,
                required=artifact.required,
                min_size_bytes=artifact.min_size_bytes,
                max_size_bytes=artifact.max_size_bytes,
            )
            for artifact in execution_ir.artifacts
            if artifact.producer_node_id == node.node_id
        )
        artifact_bindings = []
        for input_name, source in node.input_contract.sources.items():
            if not source.startswith("artifact."):
                continue
            source_identity = source.removeprefix("artifact.")
            if ":" not in source_identity:
                continue
            source_node_id, source_output_name = source_identity.split(":", 1)
            artifact_bindings.append(
                WholeArtifactBinding(input_name, source_node_id, source_output_name)
            )
        nodes.append(
            RuntimeNodeDefinition(
                node_id=node.node_id,
                agent_id=None if binding is None else binding.agent_id,
                agent_version=None if binding is None else binding.version,
                credential_ref=None if binding is None else binding.credential_ref,
                max_retries=0 if retry is None else retry.max_retries,
                backoff_seconds=0 if retry is None else retry.backoff_seconds,
                timeout_seconds=None if retry is None else retry.timeout_seconds,
                depends_on=node.depends_on,
                expected_outputs=expected_outputs,
                input_bindings=tuple(artifact_bindings),
            )
        )
    return WorkflowRuntimeDefinition(
        execution_ir.workflow_family_id,
        execution_ir.workflow_version,
        execution_ir.registry_snapshot_id,
        execution_ir.content_hash(),
        tuple(nodes),
    )

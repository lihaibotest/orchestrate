from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Mapping, Protocol

from domain.execution_ir import ExecutionIR, canonical_json, freeze_mapping, thaw_json


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def require_operation_key(operation_key: str) -> str:
    return _required_text(operation_key, "operation_key")


@dataclass(frozen=True, order=True)
class WorkflowIssue:
    code: str
    path: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, "issue code"))
        object.__setattr__(self, "path", _required_text(self.path, "issue path"))
        object.__setattr__(self, "message", _required_text(self.message, "issue message"))


class WorkflowCompilationError(ValueError):
    def __init__(self, issues: tuple[WorkflowIssue, ...]) -> None:
        if not issues:
            raise ValueError("WorkflowCompilationError requires at least one issue")
        self.issues = tuple(sorted(issues))
        super().__init__("; ".join(f"{item.code}@{item.path}: {item.message}" for item in self.issues))


class WorkflowEngineError(RuntimeError):
    """Base error for workflow engine port failures."""


class WorkflowEngineResponseError(WorkflowEngineError):
    """The engine response did not satisfy the adapter contract."""


class WorkflowEngineOperationConflict(WorkflowEngineError):
    """An idempotency key was reused for a different operation input."""


@dataclass(frozen=True)
class CompiledWorkflow:
    target: str
    source_ir_hash: str
    document: Mapping[str, object]
    workflow_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", _required_text(self.target, "target"))
        object.__setattr__(
            self,
            "source_ir_hash",
            _required_text(self.source_ir_hash, "source_ir_hash"),
        )
        frozen = freeze_mapping(self.document)
        object.__setattr__(self, "document", frozen)
        digest = hashlib.sha256(canonical_json(frozen).encode("ascii")).hexdigest()
        object.__setattr__(self, "workflow_hash", digest)

    def mutable_document(self) -> dict[str, object]:
        document = thaw_json(self.document)
        if not isinstance(document, dict):
            raise TypeError("compiled workflow document must be an object")
        return document


@dataclass(frozen=True)
class WorkflowValidationResult:
    valid: bool
    issues: tuple[WorkflowIssue, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        issues = tuple(sorted(self.issues))
        if self.valid and issues:
            raise ValueError("valid workflow validation cannot contain issues")
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence))


@dataclass(frozen=True)
class WorkflowReference:
    engine: str
    workflow_id: str
    source_ir_hash: str
    workflow_hash: str
    published: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("engine", "workflow_id", "source_ir_hash", "workflow_hash"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class WorkflowExecution:
    engine: str
    execution_id: str
    workflow_id: str
    status: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("engine", "execution_id", "workflow_id", "status"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


class WorkflowCompilerPort(Protocol):
    def compile(self, execution_ir: ExecutionIR) -> CompiledWorkflow: ...


class WorkflowEnginePort(Protocol):
    def validate_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult: ...

    def import_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowReference: ...

    def preflight_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult: ...

    def publish_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowReference: ...

    def execute_workflow(
        self,
        workflow: WorkflowReference,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution: ...

    def get_execution(self, execution_id: str) -> WorkflowExecution: ...

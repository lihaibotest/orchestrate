from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Mapping

from domain.execution_ir import ExecutionIR, canonical_json, thaw_json
from ports.workflow_engine import (
    CompiledWorkflow,
    WorkflowCompilerPort,
    WorkflowEngineError,
    WorkflowEngineOperationConflict,
    WorkflowEnginePort,
    WorkflowExecution,
    WorkflowIssue,
    WorkflowReference,
    WorkflowValidationResult,
    require_operation_key,
)


class FakeWorkflowCompilerAdapter(WorkflowCompilerPort):
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def compile(self, execution_ir: ExecutionIR) -> CompiledWorkflow:
        self.calls.append(execution_ir.content_hash())
        if self.failure is not None:
            raise self.failure
        return CompiledWorkflow(
            target="fake",
            source_ir_hash=execution_ir.content_hash(),
            document={"execution_ir": execution_ir.as_dict()},
        )


class FakeWorkflowEngineAdapter(WorkflowEnginePort):
    """Deterministic in-memory engine used by application and contract tests."""

    def __init__(
        self,
        *,
        validation_issues: tuple[WorkflowIssue, ...] = (),
        preflight_issues: tuple[WorkflowIssue, ...] = (),
        import_failure: Exception | None = None,
    ) -> None:
        self.validation_issues = validation_issues
        self.preflight_issues = preflight_issues
        self.import_failure = import_failure
        self.workflows: dict[str, tuple[CompiledWorkflow, WorkflowReference]] = {}
        self.executions: dict[str, WorkflowExecution] = {}
        self.calls: list[tuple[str, str]] = []
        self._operations: dict[str, tuple[str, str, object]] = {}
        self._lock = RLock()

    def _idempotent(
        self,
        operation_key: str,
        operation: str,
        fingerprint: str,
    ) -> object | None:
        key = require_operation_key(operation_key)
        existing = self._operations.get(key)
        if existing is None:
            return None
        existing_operation, existing_fingerprint, result = existing
        if (existing_operation, existing_fingerprint) != (operation, fingerprint):
            raise WorkflowEngineOperationConflict(
                f"operation key {key!r} is already bound to different input"
            )
        return result

    def _record(
        self,
        operation_key: str,
        operation: str,
        fingerprint: str,
        result: object,
    ) -> None:
        key = require_operation_key(operation_key)
        self._operations[key] = (operation, fingerprint, result)
        self.calls.append((operation, key))

    def validate_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult:
        cached = self._idempotent(
            operation_key,
            "validate",
            workflow.workflow_hash,
        )
        if cached is not None:
            if not isinstance(cached, WorkflowValidationResult):
                raise TypeError("cached validation result has an invalid type")
            return cached
        result = WorkflowValidationResult(
            valid=not self.validation_issues,
            issues=self.validation_issues,
            evidence={"target": workflow.target},
        )
        self._record(operation_key, "validate", workflow.workflow_hash, result)
        return result

    def import_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowReference:
        if self.import_failure is not None:
            raise self.import_failure
        cached = self._idempotent(operation_key, "import", workflow.workflow_hash)
        if cached is not None:
            if not isinstance(cached, WorkflowReference):
                raise TypeError("cached workflow reference has an invalid type")
            return cached
        workflow_id = f"fake-workflow-{len(self.workflows) + 1}"
        reference = WorkflowReference(
            engine="fake",
            workflow_id=workflow_id,
            source_ir_hash=workflow.source_ir_hash,
            workflow_hash=workflow.workflow_hash,
            metadata={"status": "imported"},
        )
        self.workflows[workflow_id] = (workflow, reference)
        self._record(operation_key, "import", workflow.workflow_hash, reference)
        return reference

    def preflight_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult:
        cached = self._idempotent(operation_key, "preflight", workflow.workflow_id)
        if cached is not None:
            if not isinstance(cached, WorkflowValidationResult):
                raise TypeError("cached preflight result has an invalid type")
            return cached
        if workflow.workflow_id not in self.workflows:
            raise WorkflowEngineError(f"unknown workflow: {workflow.workflow_id}")
        result = WorkflowValidationResult(
            valid=not self.preflight_issues,
            issues=self.preflight_issues,
            evidence={"workflow_id": workflow.workflow_id, "triggerable": True},
        )
        self._record(operation_key, "preflight", workflow.workflow_id, result)
        return result

    def publish_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowReference:
        cached = self._idempotent(operation_key, "publish", workflow.workflow_id)
        if cached is not None:
            if not isinstance(cached, WorkflowReference):
                raise TypeError("cached workflow reference has an invalid type")
            return cached
        stored = self.workflows.get(workflow.workflow_id)
        if stored is None:
            raise WorkflowEngineError(f"unknown workflow: {workflow.workflow_id}")
        published = replace(workflow, published=True, metadata={"status": "published"})
        self.workflows[workflow.workflow_id] = (stored[0], published)
        self._record(operation_key, "publish", workflow.workflow_id, published)
        return published

    def execute_workflow(
        self,
        workflow: WorkflowReference,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution:
        with self._lock:
            return self._execute_workflow_locked(
                workflow,
                inputs,
                operation_key=operation_key,
            )

    def _execute_workflow_locked(
        self,
        workflow: WorkflowReference,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution:
        payload = thaw_json(inputs)
        fingerprint = f"{workflow.workflow_id}:{canonical_json(payload)}"
        cached = self._idempotent(operation_key, "execute", fingerprint)
        if cached is not None:
            if not isinstance(cached, WorkflowExecution):
                raise TypeError("cached workflow execution has an invalid type")
            return cached
        stored = self.workflows.get(workflow.workflow_id)
        if stored is None or not stored[1].published:
            raise WorkflowEngineError("workflow must be imported and published before execution")
        execution_id = f"fake-execution-{len(self.executions) + 1}"
        execution = WorkflowExecution(
            engine="fake",
            execution_id=execution_id,
            workflow_id=workflow.workflow_id,
            status="RUNNING",
            metadata={"inputs": payload},
        )
        self.executions[execution_id] = execution
        self._record(operation_key, "execute", fingerprint, execution)
        return execution

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        normalized = execution_id.strip()
        if not normalized:
            raise ValueError("execution_id must not be empty")
        self.calls.append(("get_execution", normalized))
        try:
            return self.executions[normalized]
        except KeyError as error:
            raise WorkflowEngineError(f"unknown execution: {normalized}") from error

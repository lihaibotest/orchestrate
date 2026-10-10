from __future__ import annotations

from typing import Mapping, Protocol

from domain.execution_ir import thaw_json
from ports.workflow_engine import (
    CompiledWorkflow,
    WorkflowEnginePort,
    WorkflowEngineResponseError,
    WorkflowExecution,
    WorkflowIssue,
    WorkflowReference,
    WorkflowValidationResult,
    require_operation_key,
)


class N8nGatewayPort(Protocol):
    def validate(
        self,
        artifact: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]: ...

    def create_inactive(
        self,
        artifact: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]: ...

    def activate(
        self,
        workflow_id: str,
        *,
        operation_key: str,
    ) -> dict[str, object]: ...

    def canary(
        self,
        workflow_id: str,
        *,
        operation_key: str,
    ) -> dict[str, object]: ...

    def automatic_start(
        self,
        workflow_id: str,
        payload: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]: ...

    def status(self, execution_id: str) -> dict[str, object]: ...


class N8nWorkflowEngineAdapter(WorkflowEnginePort):
    def __init__(self, gateway: N8nGatewayPort) -> None:
        self.gateway = gateway
        self._execution_workflows: dict[str, str] = {}

    @staticmethod
    def _require_n8n(workflow: CompiledWorkflow) -> None:
        if workflow.target != "n8n":
            raise ValueError("N8nWorkflowEngineAdapter only accepts target='n8n'")

    @staticmethod
    def _identifier(
        response: Mapping[str, object],
        names: tuple[str, ...],
        label: str,
    ) -> str:
        for name in names:
            value = response.get(name)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        raise WorkflowEngineResponseError(f"n8n response is missing {label}")

    @staticmethod
    def _status(response: Mapping[str, object], default: str) -> str:
        value = response.get("status")
        return str(value).strip() if value is not None and str(value).strip() else default

    @staticmethod
    def _validation_issues(response: Mapping[str, object]) -> tuple[WorkflowIssue, ...]:
        raw_issues = response.get("issues", response.get("errors", ()))
        if not isinstance(raw_issues, (list, tuple)):
            return ()
        issues: list[WorkflowIssue] = []
        for index, item in enumerate(raw_issues):
            if isinstance(item, Mapping):
                issues.append(
                    WorkflowIssue(
                        str(item.get("code", "N8N-VALIDATION-001")),
                        str(item.get("path", "workflow")),
                        str(item.get("message", "n8n validation failed")),
                    )
                )
            else:
                issues.append(
                    WorkflowIssue(
                        "N8N-VALIDATION-001",
                        f"workflow.issues.{index}",
                        str(item),
                    )
                )
        return tuple(issues)

    def validate_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult:
        self._require_n8n(workflow)
        key = require_operation_key(operation_key)
        response = self.gateway.validate(
            workflow.mutable_document(),
            operation_key=key,
        )
        issues = self._validation_issues(response)
        raw_valid = response.get("valid", response.get("ok", not issues))
        valid = bool(raw_valid) and not issues
        if not valid and not issues:
            issues = (
                WorkflowIssue(
                    "N8N-VALIDATION-001",
                    "workflow",
                    "n8n rejected the workflow without structured issues",
                ),
            )
        return WorkflowValidationResult(valid, issues, {"response": response})

    def import_workflow(
        self,
        workflow: CompiledWorkflow,
        *,
        operation_key: str,
    ) -> WorkflowReference:
        self._require_n8n(workflow)
        key = require_operation_key(operation_key)
        response = self.gateway.create_inactive(
            workflow.mutable_document(),
            operation_key=key,
        )
        workflow_id = self._identifier(response, ("workflow_id", "id"), "workflow ID")
        return WorkflowReference(
            engine="n8n",
            workflow_id=workflow_id,
            source_ir_hash=workflow.source_ir_hash,
            workflow_hash=workflow.workflow_hash,
            published=False,
            metadata={"response": response},
        )

    def publish_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowReference:
        if workflow.engine != "n8n":
            raise ValueError("workflow reference does not belong to n8n")
        key = require_operation_key(operation_key)
        response = self.gateway.activate(workflow.workflow_id, operation_key=key)
        response_id = response.get("workflow_id", response.get("id"))
        if response_id is not None and str(response_id) != workflow.workflow_id:
            raise WorkflowEngineResponseError("n8n publish response changed the workflow ID")
        return WorkflowReference(
            engine="n8n",
            workflow_id=workflow.workflow_id,
            source_ir_hash=workflow.source_ir_hash,
            workflow_hash=workflow.workflow_hash,
            published=True,
            metadata={"response": response},
        )

    def preflight_workflow(
        self,
        workflow: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowValidationResult:
        if workflow.engine != "n8n":
            raise ValueError("workflow reference does not belong to n8n")
        key = require_operation_key(operation_key)
        response = self.gateway.canary(workflow.workflow_id, operation_key=key)
        issues = self._validation_issues(response)
        raw_valid = response.get("valid", response.get("ok", not issues))
        valid = bool(raw_valid) and not issues
        if not valid and not issues:
            issues = (
                WorkflowIssue(
                    "N8N-PREFLIGHT-001",
                    "workflow.trigger",
                    "n8n trigger preflight failed without structured issues",
                ),
            )
        return WorkflowValidationResult(valid, issues, {"response": response})

    def execute_workflow(
        self,
        workflow: WorkflowReference,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution:
        if workflow.engine != "n8n" or not workflow.published:
            raise ValueError("a published n8n workflow reference is required")
        payload = thaw_json(inputs)
        if not isinstance(payload, dict):
            raise TypeError("workflow inputs must be an object")
        key = require_operation_key(operation_key)
        response = self.gateway.automatic_start(
            workflow.workflow_id,
            payload,
            operation_key=key,
        )
        execution_id = self._identifier(
            response,
            ("execution_id", "id"),
            "execution ID",
        )
        self._execution_workflows[execution_id] = workflow.workflow_id
        return WorkflowExecution(
            engine="n8n",
            execution_id=execution_id,
            workflow_id=workflow.workflow_id,
            status=self._status(response, "QUEUED"),
            metadata={"response": response},
        )

    def get_execution(self, execution_id: str) -> WorkflowExecution:
        normalized = execution_id.strip()
        if not normalized:
            raise ValueError("execution_id must not be empty")
        response = self.gateway.status(normalized)
        response_id = self._identifier(
            response,
            ("execution_id", "id"),
            "execution ID",
        )
        if response_id != normalized:
            raise WorkflowEngineResponseError("n8n status response changed the execution ID")
        workflow_id_value = response.get("workflow_id", response.get("workflowId"))
        workflow_id = (
            str(workflow_id_value).strip()
            if workflow_id_value is not None and str(workflow_id_value).strip()
            else self._execution_workflows.get(normalized, "")
        )
        if not workflow_id:
            raise WorkflowEngineResponseError("n8n status response is missing workflow ID")
        return WorkflowExecution(
            engine="n8n",
            execution_id=normalized,
            workflow_id=workflow_id,
            status=self._status(response, "UNKNOWN"),
            metadata={"response": response},
        )

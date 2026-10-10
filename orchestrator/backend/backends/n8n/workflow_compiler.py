from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable
from urllib.parse import urlparse

from domain.execution_ir import ExecutionIR, IRNode, thaw_json
from ports.workflow_engine import (
    CompiledWorkflow,
    WorkflowCompilationError,
    WorkflowCompilerPort,
    WorkflowIssue,
)


SUPPORTED_NODE_TYPES = frozenset({"invoke_agent", "join", "end"})


def _identity(value: str) -> str:
    return value


@dataclass(frozen=True)
class N8nCompilerConfig:
    webhook_credential_ref: str
    webhook_path_prefix: str = "execution-orchestrator"

    def __post_init__(self) -> None:
        if not self.webhook_credential_ref.strip():
            raise ValueError("webhook_credential_ref must not be empty")
        if not self.webhook_path_prefix.strip().strip("/"):
            raise ValueError("webhook_path_prefix must not be empty")


class N8nWorkflowCompilerAdapter(WorkflowCompilerPort):
    """Compile the stable P0-05 IR into deterministic n8n workflow JSON."""

    def __init__(
        self,
        config: N8nCompilerConfig,
        *,
        credential_id_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.credential_id_resolver = credential_id_resolver or _identity

    @staticmethod
    def _node_name(node_id: str) -> str:
        return f"ir::{node_id}"

    @staticmethod
    def _idempotency_expression(node_id: str) -> str:
        encoded_node = json.dumps(node_id, ensure_ascii=True)
        return (
            "={{ String($json.execution_id || $execution.id) + ':' + "
            f"{encoded_node} + ':' + String($json.attempt || 1) + ':invoke' }}"
        )

    @classmethod
    def _invocation_body(cls, execution_ir: ExecutionIR, node: IRNode) -> str:
        if node.agent_binding is None:
            raise ValueError("invoke_agent node is missing a binding")
        fields = {
            "workflow_family_id": execution_ir.workflow_family_id,
            "workflow_version": execution_ir.workflow_version,
            "node_id": node.node_id,
            "agent_id": node.agent_binding.agent_id,
            "agent_version": node.agent_binding.version,
            "output_contract": thaw_json(node.output_contract.schema),
        }
        constants = ", ".join(
            f"{json.dumps(key)}: {json.dumps(value, sort_keys=True, separators=(',', ':'))}"
            for key, value in fields.items()
        )
        encoded_node = json.dumps(node.node_id, ensure_ascii=True)
        return (
            "={{ { ...$json, "
            f"{constants}, "
            '"session_id": $json.session_id, '
            '"correlation_id": $json.correlation_id, '
            '"execution_id": String($json.execution_id || $execution.id), '
            '"attempt": Number($json.attempt || 1), '
            '"input_refs": ($json.input_refs || []), '
            '"idempotency_key": String($json.execution_id || $execution.id) + ":" + '
            f"{encoded_node} + ':' + String($json.attempt || 1) + ':invoke' "
            "} }}"
        )

    def _issues(self, execution_ir: ExecutionIR) -> tuple[WorkflowIssue, ...]:
        issues: list[WorkflowIssue] = []
        for node in execution_ir.nodes:
            path = f"nodes.{node.node_id}"
            if node.node_type not in SUPPORTED_NODE_TYPES:
                issues.append(
                    WorkflowIssue(
                        "N8N-NODE-001",
                        path,
                        f"{node.node_type} compiler semantics are not defined by the design baseline",
                    )
                )
                continue
            if node.node_type == "invoke_agent" and node.agent_binding is not None:
                endpoint = urlparse(node.agent_binding.endpoint)
                if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
                    issues.append(
                        WorkflowIssue(
                            "N8N-ENDPOINT-001",
                            f"{path}.agent_binding.endpoint",
                            "n8n HTTP Request nodes require an absolute HTTP(S) endpoint",
                        )
                    )
                credential_id = self.credential_id_resolver(
                    node.agent_binding.credential_ref
                ).strip()
                if not credential_id:
                    issues.append(
                        WorkflowIssue(
                            "N8N-CREDENTIAL-001",
                            f"{path}.agent_binding.credential_ref",
                            "credential reference did not resolve to an n8n credential ID",
                        )
                    )
            if node.node_type == "join" and len(node.depends_on) < 2:
                issues.append(
                    WorkflowIssue(
                        "N8N-JOIN-001",
                        f"{path}.depends_on",
                        "join requires at least two dependencies",
                    )
                )
        return tuple(sorted(issues))

    def _compile_node(self, execution_ir: ExecutionIR, node: IRNode) -> dict[str, object]:
        name = self._node_name(node.node_id)
        if node.node_type == "invoke_agent":
            if node.agent_binding is None:
                raise ValueError("validated invoke_agent node is missing a binding")
            retry = node.retry_policy
            timeout_ms = 30000 if retry is None or retry.timeout_seconds is None else retry.timeout_seconds * 1000
            max_tries = 1 if retry is None else retry.max_retries + 1
            wait_between_tries = 0 if retry is None else retry.backoff_seconds * 1000
            credential_id = self.credential_id_resolver(
                node.agent_binding.credential_ref
            ).strip()
            return {
                "id": node.node_id,
                "name": name,
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "parameters": {
                    "method": "POST",
                    "url": node.agent_binding.endpoint,
                    "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": self._invocation_body(execution_ir, node),
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [
                            {
                                "name": "X-Orchestrator-Idempotency-Key",
                                "value": self._idempotency_expression(node.node_id),
                            }
                        ]
                    },
                    "options": {"timeout": timeout_ms},
                },
                "credentials": {"httpHeaderAuth": {"id": credential_id}},
                "retryOnFail": max_tries > 1,
                "maxTries": max_tries,
                "waitBetweenTries": wait_between_tries,
            }
        if node.node_type == "join":
            return {
                "id": node.node_id,
                "name": name,
                "type": "n8n-nodes-base.merge",
                "typeVersion": 3,
                "parameters": {
                    "mode": "append",
                    "numberInputs": len(node.depends_on),
                },
            }
        return {
            "id": node.node_id,
            "name": name,
            "type": "n8n-nodes-base.noOp",
            "typeVersion": 1,
            "parameters": {},
        }

    @staticmethod
    def _levels(execution_ir: ExecutionIR) -> dict[str, int]:
        levels: dict[str, int] = {}
        remaining = {node.node_id: node for node in execution_ir.nodes}
        while remaining:
            progressed = False
            for node_id in sorted(tuple(remaining)):
                node = remaining[node_id]
                if all(dependency in levels for dependency in node.depends_on):
                    levels[node_id] = (
                        1 + max((levels[item] for item in node.depends_on), default=-1)
                    )
                    del remaining[node_id]
                    progressed = True
            if not progressed:
                raise ValueError("validated Execution IR unexpectedly contains a cycle")
        return levels

    def compile(self, execution_ir: ExecutionIR) -> CompiledWorkflow:
        issues = self._issues(execution_ir)
        if issues:
            raise WorkflowCompilationError(issues)
        ir_hash = execution_ir.content_hash()
        trigger_name = "orchestrator::start"
        path_prefix = self.config.webhook_path_prefix.strip().strip("/")
        nodes: list[dict[str, object]] = [
            {
                "id": "orchestrator-start",
                "name": trigger_name,
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                "parameters": {
                    "path": f"{path_prefix}/{ir_hash[:32]}/start",
                    "httpMethod": "POST",
                    "responseMode": "onReceived",
                    "authentication": "headerAuth",
                    "options": {},
                },
                "credentials": {
                    "httpHeaderAuth": {"id": self.config.webhook_credential_ref}
                },
                "webhookId": f"execution-orchestrator-{ir_hash[:32]}",
            }
        ]
        levels = self._levels(execution_ir)
        rows: dict[int, int] = {}
        for node in execution_ir.nodes:
            compiled_node = self._compile_node(execution_ir, node)
            level = levels[node.node_id]
            row = rows.get(level, 0)
            rows[level] = row + 1
            compiled_node["position"] = [320 + level * 320, row * 220]
            nodes.append(compiled_node)

        connections: dict[str, dict[str, list[list[dict[str, object]]]]] = {}

        def connect(source: str, target: str, target_index: int = 0) -> None:
            output = connections.setdefault(source, {"main": [[]]})["main"][0]
            connection = {"node": target, "type": "main", "index": target_index}
            if connection not in output:
                output.append(connection)

        by_id = {node.node_id: node for node in execution_ir.nodes}
        for node in execution_ir.nodes:
            target = self._node_name(node.node_id)
            if not node.depends_on:
                connect(trigger_name, target)
                continue
            for index, dependency in enumerate(node.depends_on):
                target_index = index if node.node_type == "join" else 0
                connect(self._node_name(by_id[dependency].node_id), target, target_index)

        document = {
            "name": (
                f"execution-orchestrator-{execution_ir.workflow_family_id}"
                f"-v{execution_ir.workflow_version}"
            ),
            "active": False,
            "nodes": nodes,
            "connections": connections,
            "settings": {"executionOrder": "v1"},
        }
        return CompiledWorkflow(
            target="n8n",
            source_ir_hash=ir_hash,
            document=document,
        )

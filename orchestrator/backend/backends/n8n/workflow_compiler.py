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
    # Monitoring is an external service.  Keep the endpoint configurable so
    # compiled workflows can use the deployment-local address while retaining
    # the Docker integration-stack default.
    monitoring_endpoint: str = "http://172.26.0.1:8080"
    # The planning/orchestration layer may register the agent tree before
    # publishing a workflow.  In that mode the workflow must emit only
    # agent-status callbacks and must not overwrite the externally-owned tree.
    register_monitor_tree: bool = False

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
        return "={{ $execution.id }}"

    @staticmethod
    def _artifact_agent_name(agent_id: str) -> str:
        aliases = {
            "agent_wordagent": "word_agent",
            "agent_pytestproject": "worker",
            "agent_reportagent": "report_agent",
            "wordagent": "word_agent",
            "pytestproject": "worker",
            "reportagent": "report_agent",
        }
        normalized = str(agent_id).strip()
        fallback = normalized[6:] if normalized.startswith("agent_") else normalized
        return aliases.get(normalized, fallback or "agent")

    @classmethod
    def _invocation_body(cls, execution_ir: ExecutionIR, node: IRNode) -> str:
        if node.agent_binding is None:
            raise ValueError("invoke_agent node is missing a binding")
        fields = {
            "workflow_family_id": execution_ir.workflow_family_id,
            "workflow_version": execution_ir.workflow_version,
            # Legacy IR stores the upstream Plan ID in task_id.  Preserve it
            # as a first-class field for Agent artifact namespacing.
            "plan_id": execution_ir.task_id,
            "plan_version": execution_ir.plan_version,
            "node_id": node.node_id,
            "agent_id": node.agent_binding.agent_id,
            "agent_version": node.agent_binding.version,
            "output_contract": thaw_json(node.output_contract.schema),
        }
        constants = ", ".join(
            f"{json.dumps(key)}: {json.dumps(value, sort_keys=True, separators=(',', ':'))}"
            for key, value in fields.items()
        )
        return (
            # Select the payload from whichever trigger actually ran. n8n
            # evaluates the unselected branch lazily, so manual executions do
            # not dereference the webhook node and webhook executions do not
            # dereference the manual trigger. Keeping this rooted at the
            # trigger also preserves the original Plan through later agents.
            "={{ { ...$if($(\"orchestrator::start\").isExecuted, "
            "$(\"orchestrator::start\").item.json.body, "
            '{"test_path":"/app/pytestproject/cases/test_TP82_test/test_TP82_test.py", '
            '"timeout":300,"generate_allure":true,"use_xvfb":false}), '
            f"{constants}, "
            '"session_id": $json.session_id, '
            '"correlation_id": $json.correlation_id, '
            '"execution_id": $execution.id, '
            '"run_id": $execution.id, '
            '"request_id": $execution.id, '
            '"artifact_namespace": ' + json.dumps(execution_ir.task_id) + ' + "_" + '
            f'{execution_ir.plan_version} + "_" + $execution.id, '
            '"artifact_root": "ai-agents/" + '
            f'{json.dumps(cls._artifact_agent_name(node.agent_binding.agent_id))} + "/" + '
            f'{json.dumps(execution_ir.task_id)} + "_" + {execution_ir.plan_version} + "_" + $execution.id + "/", '
            '"artifact_path": "ai-agents/" + '
            f'{json.dumps(cls._artifact_agent_name(node.agent_binding.agent_id))} + "/" + '
            f'{json.dumps(execution_ir.task_id)} + "_" + {execution_ir.plan_version} + "_" + $execution.id + "/", '
            '"input_artifact_root": $json.artifact_root || $json.input_artifact_root || null, '
            '"word_artifact_root": $json.word_artifact_root || $json.input_artifact_root || $json.artifact_root || null, '
            '"attempt": 1, '
            '"input_refs": $json.input_refs, '
            '"run_manifest_path": $json.run_manifest_path || null, '
            '"idempotency_key": $execution.id '
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
            # Word generation performs several LLM and document-rendering
            # steps; a 30s HTTP timeout can expire after the Agent has already
            # generated its output and cause n8n to invoke it again.
            if node.node_id == "word_agent":
                timeout_ms = max(timeout_ms, 120000)
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
                # Agent failures are part of the workflow result, not a reason
                # to skip the terminal monitoring callback.  Preserve the
                # response/error item so downstream agents and the final
                # workflow status node can still run.
                "continueOnFail": True,
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
    def _monitor_body(execution_ir: ExecutionIR, *, agentname: str, status: str) -> str:
        """Build the monitoring agent-status payload for one workflow round."""
        return (
            "={{ { "
            f"agentname: {json.dumps(agentname)}, "
            f"status: {json.dumps(status)}, "
            f"plan_id: {json.dumps(execution_ir.task_id)}, "
            f"plan_version: String({json.dumps(execution_ir.plan_version)}), "
            "task_id: " + json.dumps(execution_ir.task_id) + ", "
            # Monitoring 3.1 identifies a run by task_id/run_id.  The
            # submitted ExecPlan execution_id is the stable run id; retain
            # the n8n execution id separately as the requested round id.
            "run_id: String($json.execution_id || $execution.id), "
            "round_id: String($execution.id) "
            "} }}"
        )

    @staticmethod
    def _monitor_tree_body(execution_ir: ExecutionIR) -> str:
        children: list[dict[str, object]] = []
        for node in execution_ir.nodes:
            if node.node_type != "invoke_agent" or node.agent_binding is None:
                continue
            name = N8nWorkflowCompilerAdapter._artifact_agent_name(
                node.agent_binding.agent_id
            )
            if name in {item["name"] for item in children}:
                continue
            children.append({"name": name, "status": "waiting", "flag": 0, "children": []})
        root = execution_ir.objective or execution_ir.task_id
        payload = {
            "nameroot": root,
            "task_id": execution_ir.task_id,
            "run_id": "__RUN_ID__",
            "status": "running",
            "children": children,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return (
            "={{ (() => { const p = "
            + encoded.replace('"__RUN_ID__"', "String($json.execution_id || $execution.id)")
            + "; return p; })() }}"
        )

    def _monitor_node(
        self,
        execution_ir: ExecutionIR,
        *,
        node_id: str,
        agentname: str,
        status: str,
        position: list[int],
    ) -> dict[str, object]:
        endpoint = self.config.monitoring_endpoint.rstrip("/")
        return {
            "id": node_id,
            "name": node_id,
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": position,
            "parameters": {
                "method": "POST",
                "url": f"{endpoint}/api/monitoring/agent-status/",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": self._monitor_body(
                    execution_ir, agentname=agentname, status=status
                ),
                "options": {"timeout": 10000},
            },
            "retryOnFail": True,
            "maxTries": 3,
            "waitBetweenTries": 1000,
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
            },
            # Keep the authenticated webhook for automation and expose a
            # native n8n trigger so an operator can run the published workflow
            # directly from the editor. Both triggers feed the same immutable
            # Execution IR graph.
            {
                "id": "orchestrator-manual-start",
                "name": "orchestrator::manual-start",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 180],
                "parameters": {},
            },
        ]
        monitor_tree_id = "monitor::agent-tree"
        if self.config.register_monitor_tree:
            nodes.append(
                {
                    "id": monitor_tree_id,
                    "name": monitor_tree_id,
                    "type": "n8n-nodes-base.httpRequest",
                    "typeVersion": 4.2,
                    "position": [160, 0],
                    "parameters": {
                        "method": "POST",
                        "url": f"{self.config.monitoring_endpoint.rstrip('/')}/api/monitoring/agent-tree/",
                        "sendBody": True,
                        "specifyBody": "json",
                        "jsonBody": self._monitor_tree_body(execution_ir),
                        "options": {"timeout": 10000},
                    },
                    "retryOnFail": True,
                    "maxTries": 3,
                    "waitBetweenTries": 1000,
                }
            )
        levels = self._levels(execution_ir)
        rows: dict[int, int] = {}
        for node in execution_ir.nodes:
            compiled_node = self._compile_node(execution_ir, node)
            level = levels[node.node_id]
            row = rows.get(level, 0)
            rows[level] = row + 1
            compiled_node["position"] = [320 + level * 320, row * 220]
            nodes.append(compiled_node)

        # Emit explicit monitoring calls immediately before and after every
        # business Agent.  The status payload intentionally uses the
        # immutable ExecPlan identity and the n8n execution ID as the round
        # ID.  Keep the standard node IDs stable for the three registered
        # agents while avoiding collisions if a plan invokes one agent more
        # than once.
        monitor_entries: dict[str, str] = {}
        monitor_exits: dict[str, str] = {}
        for node in execution_ir.nodes:
            if node.node_type != "invoke_agent" or node.agent_binding is None:
                continue
            agentname = self._artifact_agent_name(node.agent_binding.agent_id)
            # Monitoring names are part of the external real-agent contract;
            # leave synthetic compiler fixtures (agent-a/agent-b) untouched.
            if agentname not in {"word_agent", "worker", "report_agent"}:
                continue
            node_name = self._node_name(node.node_id)
            base_start_id = f"monitor::{agentname}::running"
            base_end_id = f"monitor::{agentname}::end"
            start_id = base_start_id
            end_id = base_end_id
            if start_id in monitor_entries.values() or end_id in monitor_exits.values():
                start_id = f"{base_start_id}::{node.node_id}"
                end_id = f"{base_end_id}::{node.node_id}"
            base_position = next(
                item.get("position", [320, 0])
                for item in nodes
                if item.get("id") == node.node_id
            )
            nodes.append(
                self._monitor_node(
                    execution_ir,
                    node_id=start_id,
                    agentname=agentname,
                    status="running",
                    position=[int(base_position[0]) - 160, int(base_position[1])],
                )
            )
            nodes.append(
                self._monitor_node(
                    execution_ir,
                    node_id=end_id,
                    agentname=agentname,
                    status="end",
                    position=[int(base_position[0]) + 160, int(base_position[1])],
                )
            )
            monitor_entries[node_name] = start_id
            monitor_exits[node_name] = end_id

        # A workflow-level terminal status is emitted only after all terminal
        # IR paths have completed.  Use the immutable ExecPlan objective as
        # the root agent identity expected by the monitoring service.
        workflow_end_id = "monitor::workflow::end"
        workflow_agentname = execution_ir.objective or execution_ir.task_id
        terminal_nodes = [node for node in execution_ir.nodes if node.node_type == "end"]
        if not terminal_nodes:
            referenced = {
                dependency for node in execution_ir.nodes for dependency in node.depends_on
            }
            terminal_nodes = [
                node for node in execution_ir.nodes if node.node_id not in referenced
            ]
        workflow_position = [640, 0]
        if terminal_nodes:
            workflow_position = [
                640 + levels[terminal_nodes[0].node_id] * 320,
                0,
            ]
        workflow_join_id: str | None = None
        if len(terminal_nodes) > 1:
            workflow_join_id = "monitor::workflow::join"
            nodes.append(
                {
                    "id": workflow_join_id,
                    "name": workflow_join_id,
                    "type": "n8n-nodes-base.merge",
                    "typeVersion": 3,
                    "position": [workflow_position[0] - 160, workflow_position[1]],
                    "parameters": {"mode": "append", "numberInputs": len(terminal_nodes)},
                }
            )
        nodes.append(
            self._monitor_node(
                execution_ir,
                node_id=workflow_end_id,
                agentname=workflow_agentname,
                status="end",
                position=workflow_position,
            )
        )

        connections: dict[str, dict[str, list[list[dict[str, object]]]]] = {}

        def connect(source: str, target: str, target_index: int = 0) -> None:
            output = connections.setdefault(source, {"main": [[]]})["main"][0]
            connection = {"node": target, "type": "main", "index": target_index}
            if connection not in output:
                output.append(connection)

        by_id = {node.node_id: node for node in execution_ir.nodes}
        for node in execution_ir.nodes:
            target = self._node_name(node.node_id)
            target_entry = monitor_entries.get(target, target)
            if not node.depends_on:
                if self.config.register_monitor_tree:
                    connect(trigger_name, monitor_tree_id)
                    connect("orchestrator::manual-start", monitor_tree_id)
                continue
            for index, dependency in enumerate(node.depends_on):
                target_index = index if node.node_type == "join" else 0
                dependency_name = self._node_name(by_id[dependency].node_id)
                connect(monitor_exits.get(dependency_name, dependency_name), target_entry, target_index)

        # The optional registration node is the single entry point for root tasks.
        for node in execution_ir.nodes:
            if not node.depends_on:
                target = self._node_name(node.node_id)
                if self.config.register_monitor_tree:
                    connect(monitor_tree_id, monitor_entries.get(target, target))
                else:
                    connect(trigger_name, monitor_entries.get(target, target))
                    connect("orchestrator::manual-start", monitor_entries.get(target, target))

        for node_name, start_id in monitor_entries.items():
            end_id = monitor_exits[node_name]
            connect(start_id, node_name)
            connect(node_name, end_id)

        if workflow_join_id is not None:
            for index, terminal in enumerate(terminal_nodes):
                terminal_name = self._node_name(terminal.node_id)
                connect(
                    monitor_exits.get(terminal_name, terminal_name),
                    workflow_join_id,
                    index,
                )
            connect(workflow_join_id, workflow_end_id)
        elif terminal_nodes:
            terminal = terminal_nodes[0]
            terminal_name = self._node_name(terminal.node_id)
            connect(monitor_exits.get(terminal_name, terminal_name), workflow_end_id)
        else:
            connect(trigger_name, workflow_end_id)
            connect("orchestrator::manual-start", workflow_end_id)

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

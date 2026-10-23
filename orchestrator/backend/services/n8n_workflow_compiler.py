from __future__ import annotations

import hashlib
import hmac
import json

from services.registry_snapshot import RegistryCandidateSnapshot, optional_evaluator, resolve_agent
from services.workflow_spec_validator import validate_workflow_spec


def _start_webhook_path(session_id: str, workflow_version: int, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{session_id}:{workflow_version}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"orchestrator/{digest}/start"


def _retry_classifier_code(*, max_attempts: int, backoff_seconds: int) -> str:
    return """const item = $json;
const status = Number(item.http_status || item.statusCode || item.status || 0);
const code = String(item.error_code || item.code || '').toLowerCase();
const message = String(item.error || item.message || '').toLowerCase();
const attempt = Number(item.__orchestrator_attempt || 1);
const declaredClass = String(item.error_class || '').toLowerCase();
const technical = status === 429 || (status >= 500 && status <= 599) ||
  code.includes('timeout') || message.includes('timeout') ||
  code.includes('network') || message.includes('network') ||
  code.includes('connection') || message.includes('connection');
const error_class = status === 429 ? 'http_429' : (status >= 500 && status <= 599 ? 'http_5xx' :
  (code.includes('timeout') || message.includes('timeout') ? 'timeout' :
  ((code.includes('network') || message.includes('network') || code.includes('connection') || message.includes('connection')) ? 'network' :
  (declaredClass === 'business' ? 'business' : String(item.error_class || 'unknown')))));
return [{ json: { ...item, __orchestrator_attempt: attempt, __orchestrator_error_class: error_class,
  __orchestrator_should_retry: technical && attempt < %d,
  __orchestrator_backoff_seconds: Math.min(%d * Math.pow(2, Math.max(0, attempt - 1)), 300) } }];""" % (max_attempts, backoff_seconds)


def _evaluation_body_expression(*, stage: str, node_id: str | None = None) -> str:
    """Keep evaluator input stable while allowing agents to return inline or referenced evidence."""
    node_field = "" if node_id is None else f', "node_id": "{node_id}"'
    return (
        '={{ { "schema_version": "evaluation-input.v1", '
        f'"stage": "{stage}"{node_field}, '
        '"execution_status": $json.execution_status || $json.status || ($json.error ? "failed" : "completed"), '
        '"artifact": $json.artifact || $json.output || $json.result || null, '
        '"artifact_ref": $json.artifact_ref || $json.artifact_url || $json.output_url || null, '
        '"logs": $json.logs || null, "log_ref": $json.log_ref || $json.log_url || null, '
        '"error": $json.error || null, "diagnostic": $json.diagnostic || null, '
        '"correlation_id": $json.correlation_id, "session_id": $json.session_id, '
        '"plan_version": $json.plan_version, "workflow_version": $json.workflow_version, '
        '"idempotency_key": $json.idempotency_key, "payload": $json } }}'
    )


def _artifact_agent_name(agent_id: str) -> str:
    """Map deployment registry IDs to the stable MinIO Agent namespace."""
    aliases = {
        "agent_wordagent": "word_agent",
        "agent_pytestproject": "worker",
        "agent_reportagent": "report_agent",
        "wordagent": "word_agent",
        "pytestproject": "worker",
        "reportagent": "report_agent",
    }
    normalized = str(agent_id).strip()
    return aliases.get(normalized, normalized.removeprefix("agent_") or "agent")


def _agent_body_expression(*, node: dict[str, object], spec: dict[str, object]) -> str:
    """Decorate the legacy WorkflowSpec payload with execution context.

    The V1 compiler is still used by the compatibility ingress.  Keeping the
    context here ensures those workflows follow the same object-store layout
    as V2-generated workflows.
    """
    node_id = str(node["node_id"])
    agent_id = str(node.get("agent_id") or node_id)
    agent_name = _artifact_agent_name(agent_id)
    plan_id = str(spec.get("plan_id") or "")
    plan_literal = json.dumps(plan_id)
    incoming_plan = "($json.plan_id || ($json.blueprint && $json.blueprint.plan_id))"
    agent_literal = json.dumps(agent_name)
    # $json is intentionally spread first: existing blueprint inputs and
    # whole-artifact bindings remain available to the Agent endpoint.
    return (
        '={{ { ...$json, "plan_id": '
        f'{plan_literal} || {incoming_plan}, "plan_version": {int(spec["plan_version"])}, '
        '"workflow_version": ' + str(int(spec["workflow_version"])) + ', '
        '"execution_id": String($execution.id), "run_id": String($execution.id), '
        '"request_id": String($execution.id), "node_id": ' + json.dumps(node_id) + ', '
        '"agent_id": ' + json.dumps(agent_id) + ', "artifact_namespace": '
        f'({plan_literal} || {incoming_plan}) + "_" + String({int(spec["plan_version"])}) + "_" + String($execution.id), '
        '"artifact_root": "ai-agents/" + ' + agent_literal + ' + "/" + '
        f'({plan_literal} || {incoming_plan}) + "_" + String({int(spec["plan_version"])}) + "_" + String($execution.id) + "/", '
        '"artifact_path": "ai-agents/" + ' + agent_literal + ' + "/" + '
            f'({plan_literal} || {incoming_plan}) + "_" + String({int(spec["plan_version"])}) + "_" + String($execution.id) + "/"'
        ', "input_artifact_root": $json.artifact_root || $json.input_artifact_root || null, '
        '"word_artifact_root": $json.word_artifact_root || $json.input_artifact_root || $json.artifact_root || null } }}'
    )


def compile_workflow_spec(
    spec: dict[str, object],
    *,
    agents: list[RegistryCandidateSnapshot],
    webhook_path_secret: str = "development-webhook-path-secret",
    webhook_credential_ref: str = "orchestrator-webhook",
) -> dict[str, object]:
    validate_workflow_spec(spec, agents=agents)
    evaluator = optional_evaluator(agents)
    webhook_name = "orchestrator-start"
    webhook_path = _start_webhook_path(str(spec["session_id"]), int(spec["workflow_version"]), webhook_path_secret)
    compiled_nodes: list[dict[str, object]] = [
        {
            "parameters": {
                "path": webhook_path,
                "httpMethod": "POST",
                "responseMode": "onReceived",
                "authentication": "headerAuth",
                "options": {},
            },
            "credentials": {"httpHeaderAuth": {"id": webhook_credential_ref}},
            "webhookId": f"{webhook_name}-{webhook_path.rsplit('/', 2)[1]}",
            "id": webhook_name,
            "name": webhook_name,
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [0, 0],
        }
    ]
    node_entries: dict[str, str] = {}
    node_exits: dict[str, str] = {}
    node_exit_outputs: dict[str, int] = {}
    active_nodes: list[dict[str, object]] = []
    ordered = list(spec["nodes"])
    for index, node in enumerate(ordered):
        if node.get("kind") == "end":
            continue
        active_nodes.append(node)
        node_id = str(node["node_id"])
        retry = node.get("retry", {"max_attempts": 1})
        max_attempts = int(retry.get("max_attempts", 1))
        backoff_seconds = int(retry.get("backoff_seconds", 1))
        position = [240 + index * 300, 300]
        if node.get("kind") == "wait":
            compiled_nodes.append({"id": node_id, "name": node_id, "type": "n8n-nodes-base.wait", "typeVersion": 1.1, "position": position, "parameters": {"resume": "timeInterval", "amount": int(node.get("seconds", 1)), "unit": "seconds"}})
            node_entries[node_id] = node_id
            node_exits[node_id] = node_id
            node_exit_outputs[node_id] = 0
            continue
        if node.get("kind") == "decision":
            compiled_nodes.append({"id": node_id, "name": node_id, "type": "n8n-nodes-base.if", "typeVersion": 2, "position": position, "parameters": {"conditions": {"boolean": [{"value1": "={{ $json.decision }}", "operation": "true"}]}}})
            node_entries[node_id] = node_id
            node_exits[node_id] = node_id
            node_exit_outputs[node_id] = 0
            continue
        agent = resolve_agent(agents, str(node["agent_id"]), str(node["capability"]), run_mode=str(spec["run_mode"]))
        compiled_nodes.append({
            "id": node_id,
            "name": node_id,
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": position,
            "parameters": {
                "method": "POST",
                "url": agent.endpoint,
                "jsonBody": _agent_body_expression(node=node, spec=spec),
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "X-Orchestrator-Idempotency-Key", "value": "={{ $json.idempotency_key }}"}]},
                "options": {"timeout": max(int(retry.get("timeout_seconds", 30)) * 1000, 120000) if node_id == "word_agent" else int(retry.get("timeout_seconds", 30)) * 1000, "response": {"response": {"neverError": True}}},
            },
            "credentials": {"httpHeaderAuth": {"id": agent.n8n_credential_ref}},
        })
        classifier_id = f"classify-{node_id}"
        branch_id = f"retry-{node_id}"
        wait_id = f"wait-{node_id}"
        evaluation_id = f"evaluate-{node_id}"
        compiled_nodes.extend([
            {"id": classifier_id, "name": classifier_id, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [position[0] + 120, position[1]], "parameters": {"jsCode": _retry_classifier_code(max_attempts=max_attempts, backoff_seconds=backoff_seconds)}},
            {"id": branch_id, "name": branch_id, "type": "n8n-nodes-base.if", "typeVersion": 2, "position": [position[0] + 240, position[1]], "parameters": {"conditions": {"boolean": [{"value1": "={{ $json.__orchestrator_should_retry }}", "operation": "true"}]}}},
            {"id": wait_id, "name": wait_id, "type": "n8n-nodes-base.wait", "typeVersion": 1.1, "position": [position[0] + 360, position[1]], "parameters": {"resume": "timeInterval", "amount": "={{ $json.__orchestrator_backoff_seconds }}", "unit": "seconds"}},
        ])
        if evaluator is not None:
            compiled_nodes.append(
                {"id": evaluation_id, "name": evaluation_id, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [position[0] + 480, position[1]], "parameters": {"method": "POST", "url": evaluator.endpoint, "jsonBody": _evaluation_body_expression(stage="node", node_id=node_id), "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Orchestrator-Idempotency-Key", "value": "={{ $json.idempotency_key }}"}]}}, "credentials": {"httpHeaderAuth": {"id": evaluator.n8n_credential_ref}}}
            )
        node_entries[node_id] = node_id
        node_exits[node_id] = evaluation_id if evaluator is not None else branch_id
        node_exit_outputs[node_id] = 0 if evaluator is not None else 1

    if evaluator is not None:
        compiled_nodes.append({"id": "final-evaluation", "name": "final-evaluation", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [240 + len(ordered) * 300, 300], "parameters": {"method": "POST", "url": evaluator.endpoint, "jsonBody": _evaluation_body_expression(stage="flow"), "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-Orchestrator-Idempotency-Key", "value": "={{ $json.idempotency_key }}"}]}}, "credentials": {"httpHeaderAuth": {"id": evaluator.n8n_credential_ref}}})
    connections: dict[str, dict[str, list[list[dict[str, object]]]]] = {}

    def connect(source: str, target: str, target_index: int = 0, source_index: int = 0) -> None:
        main = connections.setdefault(source, {"main": [[]]})["main"]
        while len(main) <= source_index:
            main.append([])
        outputs = main[source_index]
        if not any(item["node"] == target and item["index"] == target_index for item in outputs):
            outputs.append({"node": target, "type": "main", "index": target_index})

    active_ids = {str(node["node_id"]) for node in active_nodes}
    for node in active_nodes:
        node_id = str(node["node_id"])
        dependencies = [str(item) for item in node.get("depends_on", []) if str(item) in active_ids]
        if dependencies:
            for dependency in dependencies:
                connect(node_exits[dependency], node_entries[node_id], source_index=node_exit_outputs[dependency])
        else:
            connect(webhook_name, node_entries[node_id])
    for node in active_nodes:
        if node.get("kind") == "business_agent":
            node_id = str(node["node_id"])
            connect(node_id, f"classify-{node_id}")
            connect(f"classify-{node_id}", f"retry-{node_id}")
            connect(f"retry-{node_id}", f"wait-{node_id}", 0)
            if evaluator is not None:
                connect(f"retry-{node_id}", f"evaluate-{node_id}", 1)
            connect(f"wait-{node_id}", node_id)
    terminal_ids = [str(dependency) for node in ordered if node.get("kind") == "end" for dependency in node.get("depends_on", []) if str(dependency) in active_ids]
    if not terminal_ids:
        referenced = {str(dependency) for node in active_nodes for dependency in node.get("depends_on", [])}
        terminal_ids = [node_id for node_id in active_ids if node_id not in referenced]
    if evaluator is not None and len(terminal_ids) > 1:
        compiled_nodes.append({"id": "final-join", "name": "final-join", "type": "n8n-nodes-base.merge", "typeVersion": 3, "position": [240 + len(ordered) * 300, 180], "parameters": {"mode": "append", "numberInputs": len(terminal_ids)}})
        for index, node_id in enumerate(terminal_ids):
            connect(node_exits[node_id], "final-join", index)
        connect("final-join", "final-evaluation")
    elif evaluator is not None and terminal_ids:
        for node_id in terminal_ids:
            connect(node_exits[node_id], "final-evaluation")
    elif evaluator is not None:
        connect(webhook_name, "final-evaluation")
    return {
        "name": f"orchestrator-{spec['session_id']}-v{spec['workflow_version']}",
        "session_id": spec["session_id"],
        "plan_id": spec.get("plan_id"),
        "plan_version": spec["plan_version"],
        "workflow_version": spec["workflow_version"],
        "run_mode": spec["run_mode"],
        "metadata": {
            "artifact_namespace_template": (
                f"{spec.get('plan_id') or '<plan_id>'}_{spec['plan_version']}_<execution_id>"
            ),
            "artifact_root_template": "ai-agents/{agent}/<plan_id>_<plan_version>_<execution_id>/",
        },
        "spec": json.loads(json.dumps(spec, sort_keys=True)),
        "nodes": compiled_nodes,
        "connections": connections,
    }

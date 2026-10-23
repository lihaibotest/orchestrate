from __future__ import annotations

import json

from contracts.execution_ir_v2 import ExecutionIRV2, IRNode
from backends.n8n.capabilities import compiler_capabilities, N8N_V2_NODE_KINDS


class N8nV2Compiler:
    def capabilities(self) -> set[str]:
        return compiler_capabilities()

    def validate_ir(self, execution_ir: dict[str, object] | ExecutionIRV2) -> list[dict[str, object]]:
        try:
            ir = execution_ir if isinstance(execution_ir, ExecutionIRV2) else ExecutionIRV2.model_validate(execution_ir)
        except Exception as error:
            return [{"code": "IR_SCHEMA_INVALID", "path": "ir", "message": str(error)}]
        return [
            {"code": "BACKEND_NODE_UNSUPPORTED", "path": f"nodes.{node.node_id}", "message": node.kind}
            for node in ir.nodes if node.kind not in N8N_V2_NODE_KINDS
        ]

    @staticmethod
    def _http_node(node_id: str, url: str, credential_ref: str, body: str) -> dict[str, object]:
        return {
            "id": node_id, "name": node_id, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": [0, 0],
            "parameters": {
                "method": "POST", "url": url, "sendBody": True, "specifyBody": "json",
                "jsonBody": body,
                "sendHeaders": True,
                "headerParameters": {"parameters": [
                    {"name": "X-Orchestrator-Idempotency-Key", "value": "={{ $json.idempotency_key }}"},
                ]},
                "options": {"timeout": 120000 if node_id == "word_agent" else 30000},
            },
            "credentials": {"httpHeaderAuth": {"id": credential_ref}},
        }

    @staticmethod
    def _artifact_agent_name(agent_id: str) -> str:
        """Return the stable MinIO namespace for a registered Agent.

        Registry IDs are deployment identities (``agent_pytestproject``),
        while the object-store contract intentionally uses the business
        names (``worker``).  Keep this mapping in the compiler so every
        workflow version emits the same path regardless of endpoint host.
        Unknown IDs are still safe: the ``agent_`` prefix is removed and the
        resulting identifier is used as-is.
        """
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

    @classmethod
    def _invocation_body_expression(
        cls,
        capability: str,
        *,
        plan_id: str,
        plan_version: int,
        workflow_version: int,
        correlation_id: str,
        session_id: str,
        idempotency_key: str,
        node_id: str,
        agent_id: str,
    ) -> str:
        bodies = {
            "choose_cases": (
                '={{ { "query": $("orchestrator-start").item.json.body.query '
                '|| $("orchestrator-start").item.json.query } }}'
            ),
            "generate_test_document": (
                '={{ { "user_intent": $("orchestrator-start").item.json.user_intent '
                '|| "生成标准测试项和测试用例", '
                '"item_filepath": "/app/wordagent/data/input/test_items.json", '
                '"case_filepath": "/app/wordagent/data/input/test_cases.json", '
                '"filter_items": true } }}'
            ),
            "execute_cases": (
                '={{ { "test_path": "runtime/test_login.py", "timeout": 60, '
                '"generate_allure": true, "use_xvfb": true } }}'
            ),
            "generate_report": "={{ {} }}",
        }
        payload = bodies.get(capability, "={{ $json }}")
        # The endpoint-specific fields above are deliberately retained, but
        # all Agent calls also receive one immutable execution context.  The
        # namespace is evaluated by n8n at run time because only n8n knows the
        # actual execution identity.
        agent_name = cls._artifact_agent_name(agent_id)
        plan_literal = json.dumps(plan_id)
        correlation_literal = json.dumps(correlation_id)
        session_literal = json.dumps(session_id)
        idempotency_literal = json.dumps(idempotency_key)
        agent_literal = json.dumps(agent_name)
        node_literal = json.dumps(node_id)
        if payload == "={{ $json }}":
            base = "$json"
        else:
            base = payload.removeprefix("={{ ").removesuffix(" }}")
        return (
            '={{ { ...'
            f'{base}, "plan_id": {plan_literal}, "plan_version": {plan_version}, '
            f'"workflow_version": {workflow_version}, "correlation_id": {correlation_literal}, '
            f'"session_id": {session_literal}, "idempotency_key": {idempotency_literal}, '
            '"execution_id": String($execution.id), "run_id": String($execution.id), '
            '"request_id": String($execution.id), "node_id": '
            f'{node_literal}, "agent_id": {json.dumps(agent_id)}, '
            '"artifact_namespace": '
            f'{plan_literal} + "_" + String({plan_version}) + "_" + String($execution.id), '
            '"artifact_root": "ai-agents/" + '
            f'{agent_literal} + "/" + {plan_literal} + "_" + String({plan_version}) + "_" + String($execution.id) + "/", '
            '"artifact_path": "ai-agents/" + '
            f'{agent_literal} + "/" + {plan_literal} + "_" + String({plan_version}) + "_" + String($execution.id) + "/"'
            ', "input_artifact_root": $json.artifact_root || $json.input_artifact_root || null, '
            '"word_artifact_root": $json.word_artifact_root || $json.input_artifact_root || $json.artifact_root || null } }}'
        )

    @staticmethod
    def _authorize_body_expression(ir: ExecutionIRV2, node: IRNode) -> str:
        correlation_id = json.dumps(ir.correlation_id)
        session_id = json.dumps(ir.session_id)
        node_id = json.dumps(node.node_id)
        idempotency_key = json.dumps(ir.idempotency_key)
        reservation_prefix = json.dumps(f"reservation:{ir.session_id}:v{ir.workflow_version}:{node.node_id}:")
        return (
            '={{ { "schema_version": "runtime-authorize.v2", '
            f'"correlation_id": {correlation_id}, "session_id": {session_id}, '
            f'"plan_version": {ir.plan_version}, "workflow_version": {ir.workflow_version}, '
            f'"idempotency_key": {idempotency_key}, "execution_id": String($execution.id), '
            f'"node_id": {node_id}, "attempt": 1, '
            f'"reservation_id": {reservation_prefix} + String($execution.id), '
            '"calls": 1, "cost_microusd": 0, "duration_seconds": 60, '
            '"lease_until": (Date.now() / 1000) + 120 } }}'
        )

    @staticmethod
    def _runtime_event_body_expression(ir: ExecutionIRV2, node: IRNode) -> str:
        correlation_id = json.dumps(ir.correlation_id)
        session_id = json.dumps(ir.session_id)
        node_id = json.dumps(node.node_id)
        idempotency_key = json.dumps(ir.idempotency_key)
        event_prefix = json.dumps(
            f"event:{ir.session_id}:v{ir.workflow_version}:{node.node_id}:"
        )
        reservation_prefix = json.dumps(
            f"reservation:{ir.session_id}:v{ir.workflow_version}:{node.node_id}:"
        )
        return (
            '={{ { "schema_version": "runtime-event.v2", '
            f'"correlation_id": {correlation_id}, "session_id": {session_id}, '
            f'"plan_version": {ir.plan_version}, "workflow_version": {ir.workflow_version}, '
            f'"idempotency_key": {idempotency_key}, '
            f'"event_id": {event_prefix} + String($execution.id), '
            '"execution_id": String($execution.id), '
            f'"node_id": {node_id}, "attempt": 1, "event_type": "attempt_completed", '
            f'"reservation_id": {reservation_prefix} + String($execution.id), '
            '"usage": { "calls_delta": 1, "duration_ms_delta": 0, "cost_microusd": 0 }, '
            '"checkpoint": { "agent_completed": true } } }}'
        )

    @staticmethod
    def _terminal_body_expression(ir: ExecutionIRV2) -> str:
        correlation_id = json.dumps(ir.correlation_id)
        session_id = json.dumps(ir.session_id)
        final_node_id = json.dumps(ir.final_node_id)
        event_prefix = json.dumps(
            f"terminal:{ir.session_id}:v{ir.workflow_version}:"
        )
        return (
            '={{ { "schema_version": "runtime-terminal-event.v2", '
            f'"correlation_id": {correlation_id}, "session_id": {session_id}, '
            f'"plan_version": {ir.plan_version}, "workflow_version": {ir.workflow_version}, '
            f'"idempotency_key": {event_prefix} + String($execution.id), '
            f'"event_id": {event_prefix} + String($execution.id), '
            '"execution_id": String($execution.id), '
            '"terminal_status": ($json.error ? "failed" : "completed"), '
            f'"node_execution_states": [{{ "node_id": {final_node_id}, '
            '"status": ($json.error ? "failed" : "completed") }], '
            '"output_refs": ($json.output_ref ? [String($json.output_ref)] : []), '
            '"artifact_refs": ($json.artifact_ref ? [String($json.artifact_ref)] : []), '
            '"log_refs": ($json.log_ref ? [String($json.log_ref)] : []), '
            '"checkpoint_refs": ($json.checkpoint_ref ? [String($json.checkpoint_ref)] : []), '
            '"input_summary": ($json.input_ref ? { "input_ref": String($json.input_ref) } : {}), '
            '"retry_repair_history": [] } }}'
        )

    def compile(self, execution_ir: dict[str, object] | ExecutionIRV2, context: dict[str, object]) -> dict[str, object]:
        errors = self.validate_ir(execution_ir)
        if errors:
            raise ValueError(errors)
        ir = execution_ir if isinstance(execution_ir, ExecutionIRV2) else ExecutionIRV2.model_validate(execution_ir)
        gateway = str(context["gateway_base_url"]).rstrip("/")
        gateway_credential = str(context["gateway_credential_ref"])
        webhook_credential = str(context["webhook_credential_ref"])
        evaluation_enabled = bool(context.get("evaluation_enabled", True))
        plan_id = str(context.get("plan_id") or f"plan-{ir.session_id}").strip()
        if not plan_id:
            raise ValueError("plan_id must not be empty")
        bindings = context.get("bindings", {})
        if not isinstance(bindings, dict):
            raise ValueError("bindings context must be an object")
        evaluator_binding = context.get("evaluator_binding")
        if evaluation_enabled:
            if not isinstance(evaluator_binding, dict):
                raise ValueError("evaluator binding is required when evaluation is enabled")
            evaluator_endpoint = str(evaluator_binding.get("endpoint", ""))
            evaluator_credential = str(evaluator_binding.get("credential_ref", ""))
            if not evaluator_endpoint or not evaluator_credential:
                raise ValueError("evaluator binding must contain endpoint and credential_ref")

        trigger_id = "orchestrator-start"
        nodes: list[dict[str, object]] = [{
            "id": trigger_id, "name": trigger_id, "type": "n8n-nodes-base.webhook", "typeVersion": 2,
            "position": [0, 0],
            "parameters": {
                "path": f"orchestrator/v2/{ir.metadata.ir_hash or ir.metadata.blueprint_hash}/start",
                "httpMethod": "POST", "responseMode": "onReceived", "authentication": "headerAuth", "options": {},
            },
            "credentials": {"httpHeaderAuth": {"id": webhook_credential}},
        }]
        nodes[0]["webhookId"] = f"orchestrator-v2-{(ir.metadata.ir_hash or ir.metadata.blueprint_hash)[:32]}"
        entries: dict[str, str] = {}
        exits: dict[str, str] = {}

        for index, node in enumerate(ir.nodes, start=1):
            position = [index * 300, 200]
            if node.kind == "invoke":
                binding = bindings.get(node.binding_id or "")
                if not isinstance(binding, dict):
                    raise ValueError(f"missing binding context: {node.binding_id}")
                endpoint = str(binding.get("endpoint", ""))
                credential_ref = str(binding.get("credential_ref", ""))
                if not endpoint or not credential_ref:
                    raise ValueError(f"incomplete binding context: {node.binding_id}")
                authorize_id = f"authorize-{node.node_id}"
                event_id = f"event-{node.node_id}"
                nodes.extend([
                    self._http_node(
                        authorize_id, f"{gateway}/api/v2/runtime/authorize", gateway_credential,
                        self._authorize_body_expression(ir, node),
                    ),
                    self._http_node(
                        node.node_id, endpoint, credential_ref,
                        self._invocation_body_expression(
                            str(node.capability),
                            plan_id=plan_id,
                            plan_version=ir.plan_version,
                            workflow_version=ir.workflow_version,
                            correlation_id=ir.correlation_id,
                            session_id=ir.session_id,
                            idempotency_key=ir.idempotency_key,
                            node_id=node.node_id,
                            agent_id=str(binding.get("agent_id") or node.binding_id or node.node_id),
                        ),
                    ),
                    self._http_node(
                        event_id, f"{gateway}/api/v2/runtime/events", gateway_credential,
                        self._runtime_event_body_expression(ir, node),
                    ),
                ])
                entries[node.node_id], exits[node.node_id] = authorize_id, event_id
            elif node.kind == "evaluate":
                if not evaluation_enabled:
                    continue
                evaluator_node = self._http_node(
                    node.node_id, evaluator_endpoint, evaluator_credential, "={{ $json }}"
                )
                evaluator_node["metadata"] = {
                    "orchestrator_monitoring_node": True,
                    "monitoring_kind": "direct_evaluator",
                }
                nodes.append(evaluator_node)
                entries[node.node_id] = exits[node.node_id] = node.node_id
            elif node.kind == "decision":
                decide_id = f"decide-{node.node_id}"
                nodes.extend([
                    self._http_node(decide_id, f"{gateway}/api/v2/runtime/decide", gateway_credential, "={{ $json }}"),
                    {"id": node.node_id, "name": node.node_id, "type": "n8n-nodes-base.switch", "typeVersion": 3,
                     "position": position, "parameters": {"rules": {"values": []}}},
                ])
                entries[node.node_id], exits[node.node_id] = decide_id, node.node_id
            elif node.kind == "wait":
                nodes.append({"id": node.node_id, "name": node.node_id, "type": "n8n-nodes-base.wait", "typeVersion": 1.1,
                              "position": position, "parameters": {"resume": "timeInterval", "amount": node.duration_seconds, "unit": "seconds"}})
                entries[node.node_id] = exits[node.node_id] = node.node_id
            elif node.kind == "join":
                nodes.append({"id": node.node_id, "name": node.node_id, "type": "n8n-nodes-base.merge", "typeVersion": 3,
                              "position": position, "parameters": {"mode": "append"}})
                entries[node.node_id] = exits[node.node_id] = node.node_id
            elif node.kind in {"parallel", "bounded_loop"}:
                control_id = node.node_id
                nodes.append(self._http_node(control_id, f"{gateway}/api/v2/runtime/{node.kind}", gateway_credential, "={{ $json }}"))
                entries[node.node_id] = exits[node.node_id] = control_id
            elif node.kind == "end":
                entries[node.node_id] = (
                    "final-evaluation" if evaluation_enabled else "terminal-runtime-event"
                )
                exits[node.node_id] = "terminal-runtime-event"

        if evaluation_enabled:
            final_evaluator = self._http_node(
                "final-evaluation",
                evaluator_endpoint,
                evaluator_credential,
                "={{ $json }}",
            )
            final_evaluator["metadata"] = {
                "orchestrator_monitoring_node": True,
                "monitoring_kind": "final_evaluator",
            }
            nodes.append(final_evaluator)
        terminal_node = self._http_node(
            "terminal-runtime-event",
            f"{gateway}/api/v2/runtime/terminal",
            gateway_credential,
            self._terminal_body_expression(ir),
        )
        terminal_node["metadata"] = {"orchestrator_terminal_telemetry": True}
        nodes.append(terminal_node)
        connections: dict[str, dict[str, list[list[dict[str, object]]]]] = {}

        def connect(source: str, target: str) -> None:
            output = connections.setdefault(source, {"main": [[]]})["main"][0]
            item = {"node": target, "type": "main", "index": 0}
            if item not in output:
                output.append(item)

        for node in ir.nodes:
            if node.kind == "invoke":
                connect(entries[node.node_id], node.node_id)
                connect(node.node_id, exits[node.node_id])
            elif node.kind == "decision":
                connect(entries[node.node_id], node.node_id)
        for entry in ir.entry_nodes:
            connect(trigger_id, entries[entry])
        for edge in ir.edges:
            connect(exits[edge.source], entries[edge.target])

        if evaluation_enabled:
            connect("final-evaluation", "terminal-runtime-event")

        monitoring_node_count = sum(
            bool(node.get("metadata", {}).get("orchestrator_monitoring_node"))
            for node in nodes
            if isinstance(node.get("metadata"), dict)
        )
        monitoring_mode = (
            "evaluator_direct"
            if evaluation_enabled and monitoring_node_count > 0
            else "planner_final_summary_fallback"
        )

        return {
            "name": f"orchestrator-v2-{ir.session_id}-v{ir.workflow_version}",
            "active": False,
            "nodes": nodes,
            "connections": connections,
            "settings": {"executionOrder": "v1"},
            "metadata": {
                "session_id": ir.session_id, "plan_version": ir.plan_version,
                "plan_id": plan_id,
                "artifact_namespace_template": f"{plan_id}_{ir.plan_version}_<execution_id>",
                "workflow_version": ir.workflow_version, "ir_hash": ir.metadata.ir_hash,
                "canary_policy": ir.canary_policy,
                "monitoring_mode": monitoring_mode,
                "monitoring_node_count": monitoring_node_count,
            },
        }

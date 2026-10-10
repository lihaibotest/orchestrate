from __future__ import annotations

import json
import socket
import time
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from backends.n8n.gateway import N8nGateway
from infrastructure.config import OrchestratorSettings
from services.planning_agent_delivery import PlanningAgentDelivery


def secret_file_value(path: str, *, setting_name: str) -> str:
    return OrchestratorSettings.read_secret(path, setting_name=setting_name)


class JsonHttpAdapter:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        mutation_timeout_seconds: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.mutation_timeout_seconds = mutation_timeout_seconds

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        return self.request_with_headers(method, path, payload)

    def request_with_headers(self, method: str, path: str, payload: dict[str, object] | None = None, extra_headers: dict[str, str] | None = None) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key
        headers.update(extra_headers or {})
        timeout = 10.0
        if method.upper() == "POST" and path.startswith("/api/v2/orchestrator/"):
            timeout = self.mutation_timeout_seconds
        response = urlopen(Request(f"{self.base_url}{path}", data=body, headers=headers, method=method), timeout=timeout)
        return json.loads(response.read() or b"{}")


class DeepSeekChatAdapter:
    """Constrained JSON-only transport for the DeepSeek Workflow LLM."""

    _SYSTEM_PROMPT = (
        "You are the Workflow Reasoner for orchestrator-agent. Return one JSON object only; "
        "never use Markdown fences or prose. Return exactly a reasoner-proposal.v1 object with "
        "these top-level fields: {\"schema_version\":\"reasoner-proposal.v1\","
        "\"correlation_id\":string,\"session_id\":string,\"plan_version\":integer,"
        "\"workflow_version\":0,\"idempotency_key\":string,\"proposal_id\":string,"
        "\"task_groups\":[{\"group_id\":string,\"task_ids\":[string],"
        "\"mode\":\"sequence|parallel\"}],\"data_bindings\":[{\"source_ref\":string,"
        "\"target_ref\":string}],\"control_structures\":[{\"structure_id\":string,"
        "\"kind\":\"decision|bounded_loop|wait\",\"task_ids\":[string]}],"
        "\"evaluation_gates\":[{\"gate_id\":string,\"after_task_id\":string,"
        "\"metric\":string}]}. Copy correlation_id, session_id, plan_version, "
        "workflow_version, and idempotency_key from request identity into those top-level fields. "
        "Include every blueprint task_id exactly once in task_groups and honor depends_on order. "
        "Do not return an \"identity\" object. Do not return \"bounded_retry\", "
        "\"proposal_type\", or any other fields. Empty arrays are allowed where appropriate. "
        "You may produce task grouping, data bindings, control structures, and evaluation gates only. Do not invent "
        "or return endpoint, credential, executor ID, n8n JSON, code, shell commands, or secrets; "
        "those are supplied by the immutable registry and compiler."
    )

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
        network_attempts: int = 3,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key must not be empty")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip() or "deepseek-v4-flash"
        self.timeout_seconds = timeout_seconds
        self.network_attempts = network_attempts

    def chat_json(
        self,
        payload: dict[str, object],
        *,
        operation_key: str,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or self._SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        body = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        request.headers["Authorization"] = f"Bearer {self.api_key}"
        # urllib canonicalizes header names; preserve this exact operation header for gateways.
        request.headers["X-Operation-Key"] = operation_key
        for attempt in range(self.network_attempts):
            try:
                response = urlopen(request, timeout=self.timeout_seconds)
                break
            except (socket.gaierror, TimeoutError, socket.timeout, ConnectionError):
                if attempt + 1 >= self.network_attempts:
                    raise
                time.sleep(min(2 ** attempt, 5))
        document = json.loads(response.read() or b"{}")
        try:
            content = document["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("DeepSeek response is missing choices[0].message.content") from error
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("DeepSeek response content must be a non-empty JSON string")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError("DeepSeek response content is not valid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("DeepSeek response JSON must be an object")
        return result

    def generate_reasoner_proposal(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]:
        return self.chat_json(payload, operation_key=operation_key)

    def repair_reasoner_proposal(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]:
        return self.chat_json(payload, operation_key=operation_key)

    def generate_spec(self, payload: dict[str, object]) -> dict[str, object]:
        return self.chat_json(
            payload,
            operation_key="v1-workflow-spec",
            system_prompt=(
                "Return one JSON object only, conforming to workflow-spec.v1. Generate only "
                "workflow structure and bounded retry settings from the supplied blueprint. "
                "Never invent endpoint URLs, credentials, secrets, code, or executor IDs."
            ),
        )

    def repair_spec(self, payload: dict[str, object]) -> dict[str, object]:
        return self.chat_json(
            payload,
            operation_key="v1-workflow-spec-repair",
            system_prompt=(
                "Return one JSON object only, conforming to workflow-spec.v1. Repair the supplied "
                "workflow specification using the structured diagnosis. Never return endpoint URLs, "
                "credentials, secrets, code, or executor IDs."
            ),
        )


class ExternalControlPlane:
    def __init__(self, settings: OrchestratorSettings) -> None:
        self.settings = settings
        n8n_key = secret_file_value(
            settings.n8n_api_key_file, setting_name="N8N_API_KEY_FILE"
        )
        self.n8n = JsonHttpAdapter(
            settings.n8n_base_url,
            api_key=n8n_key,
            mutation_timeout_seconds=settings.gateway_mutation_timeout_seconds,
        )
        self.gateway = N8nGateway(self.n8n)
        self.planner = JsonHttpAdapter(settings.planning_agent_base_url)
        llm_url = settings.workflow_llm_base_url
        if urlparse(llm_url).hostname == "api.deepseek.com":
            self.llm = DeepSeekChatAdapter(
                llm_url,
                api_key=secret_file_value(
                    settings.workflow_llm_api_key_file,
                    setting_name="ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE",
                ),
                model=settings.workflow_llm_model,
                timeout_seconds=settings.workflow_llm_timeout_seconds,
                network_attempts=settings.workflow_llm_network_attempts,
            )
        else:
            self.llm = JsonHttpAdapter(llm_url)
        self.webhook_auth_header = settings.n8n_webhook_auth_header
        self.webhook_auth_value = secret_file_value(
            settings.n8n_webhook_auth_value_file,
            setting_name="N8N_WEBHOOK_AUTH_VALUE_FILE",
        )

    @classmethod
    def configured(cls, settings: OrchestratorSettings) -> bool:
        return settings.external_control_configured

    def registry(self) -> dict[str, object]:
        return self.n8n.request("GET", "/api/v1/registry-candidates")

    def probe(self) -> bool:
        try:
            self.gateway.capabilities()
            return True
        except Exception:
            return False

    def gateway_capabilities(self):
        return self.gateway.capabilities()

    def require_v2_gateway(self):
        return self.gateway.require_v2_ready()

    def generate_spec(self, payload: dict[str, object]) -> dict[str, object]:
        if isinstance(self.llm, DeepSeekChatAdapter):
            return self.llm.generate_spec(payload)
        return self.llm.request("POST", "/api/v1/workflow-spec", payload)

    def generate_reasoner_proposal(self, payload: dict[str, object], *, operation_key: str) -> dict[str, object]:
        """Versioned v2 Reasoner transport; the legacy v1 endpoint remains untouched."""
        if isinstance(self.llm, DeepSeekChatAdapter):
            return self.llm.generate_reasoner_proposal(payload, operation_key=operation_key)
        return self.llm.request_with_headers(
            "POST",
            "/api/v2/reasoner-proposals",
            payload,
            {"X-Operation-Key": operation_key},
        )

    def create_workflow(self, workflow: dict[str, object]) -> dict[str, object]:
        payload = {
            "name": workflow["name"],
            "nodes": workflow["nodes"],
            "connections": workflow["connections"],
            "settings": workflow.get("settings", {}),
            "active": bool(workflow.get("active", False)),
        }
        return self.n8n.request("POST", "/api/v1/workflows", payload)

    def static_validate(self, workflow: dict[str, object]) -> None:
        self.n8n.request("POST", "/api/v1/workflows/validate", {"nodes": workflow["nodes"], "connections": workflow["connections"]})

    def create_inactive(self, workflow: dict[str, object]) -> dict[str, object]:
        return self.create_workflow({**workflow, "active": False})

    def run_canary(self, workflow_id: str, workflow: dict[str, object]) -> dict[str, object]:
        return self.n8n.request("POST", f"/api/v1/workflows/{workflow_id}/canary", {"workflow_version": workflow.get("workflow_version")})

    def activate_workflow(self, workflow_id: str) -> None:
        self.n8n.request("POST", f"/api/v1/workflows/{workflow_id}/activate", {})

    def activate(self, workflow_id: str) -> None:
        self.activate_workflow(workflow_id)

    def start_workflow(self, workflow: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
        webhook_path = ""
        for node in workflow.get("nodes", []):
            if isinstance(node, dict) and node.get("type") == "n8n-nodes-base.webhook":
                parameters = node.get("parameters", {})
                if isinstance(parameters, dict):
                    webhook_path = str(parameters.get("path", "")).strip()
                break
        if not webhook_path:
            raise RuntimeError("workflow has no webhook trigger for automatic start")
        normalized_path = webhook_path.lstrip("/")
        if not normalized_path.startswith("webhook/"):
            normalized_path = f"webhook/{normalized_path}"
        header_name = getattr(self, "webhook_auth_header", "X-Orchestrator-Webhook")
        header_value = getattr(self, "webhook_auth_value", "")
        if header_value and hasattr(self.n8n, "request_with_headers"):
            return self.n8n.request_with_headers("POST", f"/{normalized_path}", payload, {header_name: header_value})
        return self.n8n.request("POST", f"/{normalized_path}", payload)

    def _planning_agent_delivery(self) -> PlanningAgentDelivery:
        return PlanningAgentDelivery(
            planning_agent_base_url=self.planner.base_url,
            secret=secret_file_value(
                self.settings.planning_agent_hmac_secret_file,
                setting_name="ORCHESTRATOR_PLANNING_AGENT_HMAC_SECRET_FILE",
            ),
        )

    def send_workflow_escalation(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]:
        if payload.get("operation_key") != operation_key:
            raise ValueError("workflow escalation operation key mismatch")
        return self._planning_agent_delivery().send_workflow_escalation(payload)

    def send_workflow_final_summary(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]:
        if payload.get("operation_key") != operation_key:
            raise ValueError("workflow final-summary operation key mismatch")
        return self._planning_agent_delivery().send_workflow_final_summary(payload)

    def repair_spec(self, payload: dict[str, object]) -> dict[str, object]:
        if isinstance(self.llm, DeepSeekChatAdapter):
            return self.llm.repair_spec(payload)
        return self.llm.request("POST", "/api/v1/workflow-spec/repair", payload)

    def repair_reasoner_proposal(self, payload: dict[str, object], *, operation_key: str) -> dict[str, object]:
        if isinstance(self.llm, DeepSeekChatAdapter):
            return self.llm.repair_reasoner_proposal(payload, operation_key=operation_key)
        return self.llm.request_with_headers(
            "POST", "/api/v2/reasoner-proposals/repair", payload, {"X-Operation-Key": operation_key}
        )

    def resume(self, payload: dict[str, object]) -> None:
        self.n8n.request("POST", "/api/v1/executions/resume", payload)

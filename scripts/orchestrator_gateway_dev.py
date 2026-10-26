#!/usr/bin/env python3
"""Offline orchestrator-gateway.v1 adapter for the isolated n8n stack.

The adapter imports Orchestrator-compiled artifacts with n8n's supported CLI,
publishes their generated webhook, discovers real n8n execution identities,
and signs runtime facts before forwarding them to Orchestrator.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


REGISTRY_FILE = os.getenv(
    "REGISTRY_FILE",
    "/bundle/agent-registry/registration/agents.json",
)
# When the bundled registry contains Docker-only service names, a local
# gateway can project them onto the host address without mutating the
# authoritative registry file. This keeps the snapshot input immutable while
# allowing n8n (running in Docker) to call host-run development Agents.
REGISTRY_ENDPOINT_HOST = os.getenv("REGISTRY_ENDPOINT_HOST", "").strip()
N8N_CONTAINER = os.getenv("N8N_CONTAINER", "n8n")
N8N_POSTGRES_CONTAINER = os.getenv("N8N_POSTGRES_CONTAINER", "postgres")
N8N_INTERNAL_URL = os.getenv("N8N_INTERNAL_URL", "http://n8n:5678").rstrip("/")
N8N_PROJECT_ID = os.getenv("N8N_PROJECT_ID", "orchestrator-gateway")
ORCHESTRATOR_BASE_URL = os.getenv("ORCHESTRATOR_BASE_URL", "http://orchestrator-agent:8020").rstrip("/")
N8N_HMAC_SECRET_FILE = os.getenv(
    "N8N_HMAC_SECRET_FILE",
    os.getenv("ORCHESTRATOR_N8N_HMAC_SECRET_FILE", ""),
)
N8N_WEBHOOK_AUTH_VALUE_FILE = os.getenv("N8N_WEBHOOK_AUTH_VALUE_FILE", "")
N8N_WEBHOOK_AUTH_HEADER = os.getenv("N8N_WEBHOOK_AUTH_HEADER", "X-Orchestrator-Webhook")
N8N_WEBHOOK_START_TIMEOUT_SECONDS = float(
    os.getenv("N8N_WEBHOOK_START_TIMEOUT_SECONDS", "90")
)
GATEWAY_API_KEY_FILE = os.getenv("GATEWAY_API_KEY_FILE", "")
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
WORKFLOW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

OPS = (
    "registry_v2", "validate", "create_inactive", "canary", "activate",
    "automatic_start", "resume", "status", "retire", "live_reference_check",
    "signed_runtime_event_delivery", "operation_idempotency", "operation_lookup",
)
EVALUATOR_OWNED_KEYS = {
    "evaluation", "evaluationid", "evaluator", "evaluatorid", "evaluatoropinion",
    "quality", "qualityscore", "qualityverdict", "recommendation", "verdict",
}
# The task-orchestration Plan uses dashboard agent names as capability refs,
# while the legacy registration file exposes contract capability names. Keep
# both names in the gateway projection so the binding remains deterministic.
CAPABILITY_ALIASES = {
    "agent_wordagent": "word_agent",
    "agent_pytestproject": "worker",
    "agent_reportagent": "report_agent",
}
state_lock = threading.RLock()
start_lock = threading.Lock()
workflows: dict[str, dict] = {}
operations: dict[str, dict] = {}
executions: dict[str, dict] = {}
runtime_events: list[dict] = []


def read_secret(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    value = json.loads(handler.rfile.read(length).decode("utf-8"))
    return value if isinstance(value, dict) else {}


def contains_evaluator_owned_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            "".join(character for character in str(key).lower() if character.isalnum())
            in EVALUATOR_OWNED_KEYS
            or contains_evaluator_owned_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_evaluator_owned_key(item) for item in value)
    return False


def workflow_import_document(artifact: dict) -> tuple[str, list[dict]]:
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    workflow_id = "orchv2" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:18]
    document = {
        "id": workflow_id,
        "name": str(artifact.get("name") or workflow_id),
        "active": False,
        "nodes": artifact.get("nodes", []),
        "connections": artifact.get("connections", {}),
        "settings": artifact.get("settings", {"executionOrder": "v1"}),
    }
    return workflow_id, [document]


def artifact_webhook_path(artifact: dict) -> str:
    for node in artifact.get("nodes", []):
        if isinstance(node, dict) and node.get("type") == "n8n-nodes-base.webhook":
            parameters = node.get("parameters", {})
            if isinstance(parameters, dict):
                path = str(parameters.get("path", "")).strip().lstrip("/")
                if path:
                    return path
    raise RuntimeError("compiled workflow has no webhook path")


def signed_runtime_headers(
    body: bytes,
    correlation_id: str,
    secret: str,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()) if now is None else now)
    nonce_value = nonce or uuid.uuid4().hex
    message = b"\n".join(
        (b"n8n", timestamp.encode(), nonce_value.encode(), correlation_id.encode(), body)
    )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Orchestrator-Caller": "n8n",
        "X-Orchestrator-Timestamp": timestamp,
        "X-Orchestrator-Nonce": nonce_value,
        "X-Correlation-ID": correlation_id,
        "X-Orchestrator-Signature": hmac.new(
            secret.encode(), message, hashlib.sha256
        ).hexdigest(),
    }


def run_checked(command: list[str], *, timeout: int = 120) -> str:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=timeout
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise RuntimeError(detail[-1000:])
    return completed.stdout


def docker_json_import(document: object, *, kind: str) -> None:
    if kind not in {"workflow", "credentials"}:
        raise ValueError("unsupported n8n import kind")
    remote_path = f"/tmp/orchestrator-gateway-{kind}-{uuid.uuid4().hex}.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
        json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        # n8n runs as a non-root user in its container.  docker cp preserves
        # the source mode, while NamedTemporaryFile defaults to 0600, so make
        # the short-lived import document readable before copying it across.
        os.chmod(handle.name, 0o644)
        run_checked(["docker", "cp", handle.name, f"{N8N_CONTAINER}:{remote_path}"])
    try:
        command = [
            "docker", "exec", N8N_CONTAINER, "n8n", f"import:{kind}",
            f"--input={remote_path}",
        ]
        # Older/self-hosted n8n instances may not have the project provisioned.
        # An explicit project remains supported, while the empty value lets
        # n8n attach the import to its default owner project.
        if N8N_PROJECT_ID:
            command.append(f"--projectId={N8N_PROJECT_ID}")
        run_checked(command)
    finally:
        subprocess.run(
            ["docker", "exec", N8N_CONTAINER, "rm", "-f", remote_path],
            check=False, capture_output=True,
        )


def ensure_credentials() -> None:
    existing = {
        row.split("|", 2)[0]
        for row in psql_rows("SELECT id,name,type FROM credentials_entity")
    }
    webhook_value = read_secret(N8N_WEBHOOK_AUTH_VALUE_FILE)
    credentials = [
        {
            "id": "orchestrator-webhook", "name": "Orchestrator Webhook",
            "type": "httpHeaderAuth",
            "data": {"name": N8N_WEBHOOK_AUTH_HEADER, "value": webhook_value},
        },
        {
            "id": "orchestrator-gateway", "name": "Orchestrator Gateway",
            "type": "httpHeaderAuth",
            "data": {"name": "X-Orchestrator-Gateway", "value": "gateway-dev"},
        },
        {
            "id": "agent-registry-dev", "name": "Agent Registry Development",
            "type": "httpHeaderAuth",
            "data": {"name": "X-Orchestrator-Agent", "value": "registry-dev"},
        },
    ]
    missing = [item for item in credentials if str(item["id"]) not in existing]
    if missing:
        docker_json_import(missing, kind="credentials")


def import_workflow(artifact: dict) -> str:
    workflow_id, document = workflow_import_document(artifact)
    if psql_rows(f"SELECT id FROM workflow_entity WHERE id='{workflow_id}'"):
        return workflow_id
    ensure_credentials()
    subprocess.run(
        ["docker", "exec", N8N_CONTAINER, "n8n", "unpublish:workflow", f"--id={workflow_id}"],
        check=False, capture_output=True, text=True, timeout=30,
    )
    docker_json_import(document, kind="workflow")
    return workflow_id


def publish_workflow(workflow_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow_id):
        raise ValueError("invalid workflow identity")
    run_checked([
        "docker", "exec", N8N_CONTAINER, "n8n", "publish:workflow", f"--id={workflow_id}"
    ])
    run_checked(["docker", "restart", N8N_CONTAINER])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{N8N_INTERNAL_URL}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("n8n did not become healthy after workflow activation")


def unpublish_workflow(workflow_id: str) -> None:
    run_checked([
        "docker", "exec", N8N_CONTAINER, "n8n", "unpublish:workflow", f"--id={workflow_id}"
    ])
    run_checked(["docker", "restart", N8N_CONTAINER])


def psql_rows(sql: str, parameters: dict[str, str] | None = None) -> list[str]:
    parameters = parameters or {}
    variable_args = [item for name, value in parameters.items() for item in ("--set", f"{name}={value}")]
    output = run_checked([
        "docker", "exec", N8N_POSTGRES_CONTAINER,
        "psql", "-U", "n8n", "-d", "n8n", "-At", *variable_args, "-c", sql,
    ])
    return [line for line in output.splitlines() if line.strip()]


def latest_execution_id(workflow_id: str) -> int:
    if not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
        raise ValueError("invalid workflow identity")
    rows = psql_rows(
        # The identifier is constrained above to a conservative ASCII token.
        # Use a literal here because the bundled psql client does not expand
        # ``:'workflow_id'`` consistently when invoked through docker exec.
        f'''SELECT COALESCE(MAX(id),0) FROM execution_entity WHERE "workflowId" = '{workflow_id}' ''',
    )
    return int(rows[0]) if rows else 0


def execution_status(execution_id: str) -> dict:
    with state_lock:
        local = executions.get(str(execution_id))
    if isinstance(local, dict) and local.get("terminal"):
        return dict(local)
    if not execution_id.isdigit():
        return {"execution_id": execution_id, "status": "unknown"}
    rows = psql_rows(
        f'SELECT id,status,"workflowId" FROM execution_entity WHERE id={int(execution_id)}'
    )
    if not rows:
        return {"execution_id": execution_id, "status": "unknown"}
    observed_id, status, workflow_id = rows[0].split("|", 2)
    return {"execution_id": observed_id, "workflow_id": workflow_id, "status": status}


def n8n_start(workflow_id: str, artifact: dict, payload: dict) -> dict:
    webhook_path = artifact_webhook_path(artifact)
    with start_lock:
        previous_id = latest_execution_id(workflow_id)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        webhook_value = read_secret(N8N_WEBHOOK_AUTH_VALUE_FILE)
        if webhook_value:
            headers[N8N_WEBHOOK_AUTH_HEADER] = webhook_value
        request = Request(
            f"{N8N_INTERNAL_URL}/webhook/{webhook_path}", data=body,
            method="POST", headers=headers,
        )
        webhook_deadline = time.monotonic() + N8N_WEBHOOK_START_TIMEOUT_SECONDS
        while True:
            try:
                with urlopen(request, timeout=30) as response:
                    response.read()
                break
            except HTTPError as error:
                if error.code != 404 or time.monotonic() >= webhook_deadline:
                    raise
                time.sleep(0.5)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current_id = latest_execution_id(workflow_id)
            if current_id > previous_id:
                return {
                    "workflow_id": workflow_id,
                    "execution_id": str(current_id),
                    "status": "started",
                }
            time.sleep(0.2)
    raise RuntimeError("n8n accepted webhook but no execution identity was created")


def forward_runtime(path: str, payload: dict) -> tuple[int, dict]:
    if contains_evaluator_owned_key(payload):
        return 422, {"error": "runtime facts contain evaluator-owned fields"}
    correlation_id = str(payload.get("correlation_id", ""))
    if not correlation_id:
        return 422, {"error": "correlation_id is required"}
    secret = read_secret(N8N_HMAC_SECRET_FILE)
    if not secret:
        return 503, {"error": "n8n runtime signing secret is unavailable"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    deadline = time.monotonic() + 20
    while True:
        headers = signed_runtime_headers(body, correlation_id, secret)
        request = Request(
            f"{ORCHESTRATOR_BASE_URL}{path}", data=body, method="POST", headers=headers
        )
        try:
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read() or b"{}")
                if path.endswith("/authorize") and result.get("allow") is False and result.get("reason") == "BUDGET_UNAVAILABLE" and time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                return response.status, result
        except HTTPError as error:
            raw = error.read()
            result = json.loads(raw or b"{}")
            if error.code == 409 and time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            return error.code, result


def operation(_handler: BaseHTTPRequestHandler | None, key: str, result: dict) -> dict:
    with state_lock:
        existing = operations.get(key)
        if existing is not None:
            existing_result = existing.get("result", {})
            return existing_result if isinstance(existing_result, dict) else {}
        operations[key] = {"operation_key": key, "status": "committed", "result": result}
        return result


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "orchestrator-gateway.v1"

    def log_message(self, fmt, *args):
        print("gateway", fmt % args, flush=True)

    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        expected = GATEWAY_API_KEY
        if GATEWAY_API_KEY_FILE:
            expected = read_secret(GATEWAY_API_KEY_FILE)
        provided = self.headers.get("Authorization", "")
        if not provided:
            # The production JsonHttpAdapter uses n8n's API-key header while
            # local operators commonly use the bearer form.
            provided = self.headers.get("X-N8N-API-KEY", "")
            return bool(expected) and hmac.compare_digest(provided, expected)
        return bool(expected) and hmac.compare_digest(provided, f"Bearer {expected}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"status": "ok"})
        if path == "/api/v2/orchestrator/capabilities":
            operations_doc = {
                name: {"available": True, "api_version": "orchestrator-gateway.v1"}
                for name in OPS
            }
            return self.send_json(200, {
                "schema_version": "n8n-gateway-capabilities.v1",
                "gateway_id": "n8n-orchestrator-gateway-dev-20260821",
                "api_version": "orchestrator-gateway.v1",
                "authentication": {"scheme": "bearer", "control_plane": "required"},
                "operations": operations_doc,
            })
        if path.startswith("/api/v2/orchestrator/") and not self.authorized():
            return self.send_json(401, {"error": "gateway_auth_required"})
        if path == "/api/v2/orchestrator/registry":
            try:
                with open(REGISTRY_FILE, encoding="utf-8") as handle:
                    registry = json.load(handle)
            except Exception:
                registry = []
            candidates = []
            for item in registry:
                endpoint = str(item.get("endpoint", "http://127.0.0.1/"))
                if REGISTRY_ENDPOINT_HOST:
                    parsed = urlparse(endpoint)
                    if parsed.hostname in {"wordagent", "pytestproject", "reportagent"}:
                        endpoint = parsed._replace(netloc=f"{REGISTRY_ENDPOINT_HOST}:{parsed.port or 80}").geturl()
                capabilities = list(item.get("capabilities") or [])
                alias = CAPABILITY_ALIASES.get(str(item.get("agent_id", "")))
                if alias and alias not in capabilities:
                    capabilities.append(alias)
                candidates.append({
                    "agent_id": item.get("agent_id", "unknown"),
                    "role": "business",
                    "capabilities": capabilities,
                    "health": "healthy" if item.get("status") == "active" else "degraded",
                    "endpoint": endpoint,
                    "input_schema": {"type": "object", "additionalProperties": True},
                    "output_schema": {"type": "object", "additionalProperties": True},
                    "contract_version": "agent-contract.v1",
                    "n8n_credential_ref": "agent-registry-dev",
                    "dry_run_supported": True,
                    "side_effect": "read_only" if item.get("agent_id") == "agent_filter_library" else "idempotent_write",
                    "idempotency_supported": True, "usage_reporting": True,
                    "security_scopes": [], "backends": ["n8n"],
                    "cost_microusd": 0, "latency_ms": 1000,
                })
            return self.send_json(200, {
                "schema_version": "capability-registry.v2",
                "snapshot_id": "registry-snapshot-20260821", "snapshot_version": 1,
                "candidates": candidates,
            })
        if path.startswith("/api/v2/orchestrator/operations/"):
            key = unquote(path.rsplit("/", 1)[-1])
            with state_lock:
                return self.send_json(200, operations.get(key, {"operation_key": key, "status": "unknown"}))
        if path.startswith("/api/v2/orchestrator/executions/"):
            execution_id = unquote(path.rsplit("/", 1)[-1])
            return self.send_json(200, execution_status(execution_id))
        if path.startswith("/api/v2/orchestrator/workflows/") and path.endswith("/live-references"):
            workflow_id = unquote(path.split("/")[5])
            if not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
                return self.send_json(400, {"error": "invalid workflow identity"})
            rows = psql_rows(
                f'''SELECT COUNT(*) FROM execution_entity WHERE "workflowId" = '{workflow_id}' AND status IN ('new','running','waiting')''',
            )
            return self.send_json(200, {"workflow_id": workflow_id, "live": bool(rows and int(rows[0]))})
        return self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/v2/orchestrator/") and not self.authorized():
            return self.send_json(401, {"error": "gateway_auth_required"})
        payload = read_json(self)
        key = self.headers.get("Idempotency-Key", "")
        if path in {"/api/v2/runtime/authorize", "/api/v2/runtime/events", "/api/v2/runtime/terminal"}:
            status, result = forward_runtime(path, payload)
            return self.send_json(status, result)
        if path == "/api/v2/orchestrator/workflows/validate":
            workflow = payload.get("workflow", {})
            valid = isinstance(workflow, dict) and bool(workflow.get("nodes")) and workflow.get("connections") is not None
            result = {"status": "valid" if valid else "invalid", "valid": valid, "workflow_version": workflow.get("workflow_version", 1)}
            return self.send_json(200 if valid else 422, operation(self, key, result) if key else result)
        if path == "/api/v2/orchestrator/workflows":
            workflow = payload.get("workflow", {})
            try:
                workflow_id = import_workflow(workflow)
                with state_lock:
                    workflows[workflow_id] = {"workflow": workflow, "active": False}
                result = {"id": workflow_id, "workflow_id": workflow_id, "active": False, "status": "created"}
            except Exception as error:
                result = {"status": "error", "error": str(error)[:1000]}
            return self.send_json(200, operation(self, key, result) if key else result)
        if path.startswith("/api/v2/orchestrator/workflows/"):
            parts = path.strip("/").split("/")
            workflow_id = unquote(parts[4]) if len(parts) > 4 else ""
            action = parts[5] if len(parts) > 5 else ""
            try:
                if action == "canary":
                    workflow = workflows.get(workflow_id, {}).get("workflow", {})
                    valid = bool(workflow.get("nodes")) and bool(artifact_webhook_path(workflow))
                    canary_execution_id = f"canary-{workflow_id}"
                    # The development gateway has no separate canary runtime.
                    # Record a terminal metadata-only observation so the
                    # existing S3/S4 contracts remain explicit; production
                    # execution still goes through n8n_start after publish.
                    if valid:
                        with state_lock:
                            executions[canary_execution_id] = {
                                "execution_id": canary_execution_id,
                                "workflow_id": workflow_id,
                                "status": "success",
                                "terminal": True,
                                "completed": True,
                            }
                    result = {
                        "workflow_id": workflow_id,
                        "status": "passed" if valid else "failed",
                        "passed": valid,
                        "canary": True,
                        "execution_id": canary_execution_id if valid else None,
                        "artifact": {
                            "write_ok": valid,
                            "mode": "metadata-only",
                            "workflow_id": workflow_id,
                        },
                    }
                elif action == "activate":
                    publish_workflow(workflow_id)
                    with state_lock:
                        workflows.setdefault(workflow_id, {})["active"] = True
                    result = {"workflow_id": workflow_id, "status": "activated", "active": True}
                elif action == "retire":
                    unpublish_workflow(workflow_id)
                    with state_lock:
                        workflows.setdefault(workflow_id, {})["active"] = False
                    result = {"workflow_id": workflow_id, "status": "retired", "active": False}
                elif action == "start":
                    workflow = workflows.get(workflow_id, {}).get("workflow")
                    if not isinstance(workflow, dict):
                        raise RuntimeError("gateway has no compiled artifact for workflow")
                    result = n8n_start(workflow_id, workflow, payload)
                    with state_lock:
                        executions[str(result["execution_id"])] = result
                else:
                    return self.send_json(404, {"error": "unknown_workflow_operation"})
            except Exception as error:
                result = {"workflow_id": workflow_id, "status": "error", "error": str(error)[:1000]}
            return self.send_json(200, operation(self, key, result) if key else result)
        if path.startswith("/api/v2/orchestrator/executions/") and path.endswith("/resume"):
            execution_id = unquote(path.split("/")[5])
            result = {"execution_id": execution_id, "status": "resumed"}
            return self.send_json(200, operation(self, key, result) if key else result)
        if path == "/api/v2/orchestrator/runtime-events":
            if contains_evaluator_owned_key(payload):
                return self.send_json(422, {"error": "runtime facts contain evaluator-owned fields"})
            with state_lock:
                runtime_events.append(payload)
            result = {"accepted": True, "event_id": payload.get("event_id"), "status": "recorded"}
            return self.send_json(202, operation(self, key, result) if key else result)
        return self.send_json(404, {"error": "not_found"})


if __name__ == "__main__":
    port = int(os.getenv("GATEWAY_PORT", "27880"))
    server = ThreadingHTTPServer(("0.0.0.0", port), GatewayHandler)
    print(f"orchestrator-gateway.v1 listening on {port}", flush=True)
    server.serve_forever()

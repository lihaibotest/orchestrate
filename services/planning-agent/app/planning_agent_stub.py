#!/usr/bin/env python3
"""Small offline Planning Agent substitute with durable, bounded input."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STATE_FILE = Path(os.getenv("PLANNING_AGENT_STATE_FILE", "/state/planning-agent-last.json"))
MAX_REQUEST_BYTES = int(os.getenv("PLANNING_AGENT_MAX_REQUEST_BYTES", str(1024 * 1024)))
HMAC_SECRET_FILE = os.getenv("PLANNING_AGENT_HMAC_SECRET_FILE", "")
HMAC_REQUIRED = os.getenv("PLANNING_AGENT_HMAC_REQUIRED", "false").lower() == "true"
DEV_HMAC_BYPASS = os.getenv("PLANNING_AGENT_DEV_HMAC_BYPASS", "true").lower() == "true"

EVALUATOR_KEYS = {
    "evaluation", "evaluationid", "evaluator", "evaluatorid", "evaluatoropinion",
    "quality", "qualityscore", "qualityverdict", "recommendation", "verdict",
}


def contains_evaluator_owned_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            "".join(ch for ch in str(key).lower() if ch.isalnum()) in EVALUATOR_KEYS
            or contains_evaluator_owned_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(contains_evaluator_owned_key(item) for item in value)
    return False


def secret_value() -> str:
    if not HMAC_SECRET_FILE:
        return ""
    try:
        return Path(HMAC_SECRET_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def valid_signature(handler: BaseHTTPRequestHandler, body: bytes) -> bool:
    secret = secret_value()
    if not secret:
        return DEV_HMAC_BYPASS and not HMAC_REQUIRED
    provided = handler.headers.get("X-Planning-Agent-Signature", "")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def persist(payload: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{STATE_FILE.name}.", dir=STATE_FILE.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "planning-agent-stub/1"

    def send_json(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            return self.send_json(200, {"status": "ok"})
        if self.path == "/last":
            try:
                return self.send_json(200, json.loads(STATE_FILE.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return self.send_json(200, {})
        return self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json(400, {"error": "invalid_content_length"})
        if length <= 0:
            return self.send_json(400, {"error": "JSON object required"})
        if length > MAX_REQUEST_BYTES:
            return self.send_json(413, {"error": "request_too_large", "max_bytes": MAX_REQUEST_BYTES})
        body = self.rfile.read(length)
        if not valid_signature(self, body):
            return self.send_json(401, {"error": "planning_agent_auth_required"})
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "invalid_json"})
        if not isinstance(payload, dict):
            return self.send_json(400, {"error": "JSON object required"})
        if contains_evaluator_owned_key(payload):
            return self.send_json(422, {"error": "evaluator opinion is not accepted"})
        persist(payload)
        return self.send_json(202, {"accepted": True, "state_file": str(STATE_FILE)})

    def log_message(self, fmt: str, *args: object) -> None:
        print("planning", fmt % args, flush=True)


if __name__ == "__main__":
    port = int(os.getenv("PLANNING_AGENT_PORT", "18000"))
    ThreadingHTTPServer((os.getenv("PLANNING_AGENT_BIND", "0.0.0.0"), port), Handler).serve_forever()

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import time

from fastapi import HTTPException, Request, status


CALLER_ENV = {
    "planning-agent": "ORCHESTRATOR_PLANNING_AGENT_HMAC_SECRET",
    "n8n": "ORCHESTRATOR_N8N_HMAC_SECRET",
}
ENDPOINT_CALLERS = {
    "blueprint": "planning-agent",
    "blueprint-status": "planning-agent",
    "v2-audit": "planning-agent",
    "runtime-event": "n8n",
    "runtime-authorize": "n8n",
    "n8n-event": "n8n",
    "directive": "planning-agent",
}


def secret_value(secret_file: str, *, setting_name: str) -> str:
    if not secret_file:
        raise RuntimeError(f"mounted secret file required for {setting_name}")
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"cannot read mounted secret for {setting_name}") from error


@dataclass(frozen=True)
class AuthenticatedRequest:
    caller: str
    nonce: str
    correlation_id: str
    timestamp: int
    body: bytes


async def authenticate(
    request: Request,
    *,
    endpoint_permission: str,
    repository: object,
    settings: object,
) -> AuthenticatedRequest:
    caller = request.headers.get("X-Orchestrator-Caller", "").strip()
    timestamp_text = request.headers.get("X-Orchestrator-Timestamp", "").strip()
    nonce = request.headers.get("X-Orchestrator-Nonce", "").strip()
    correlation_id = request.headers.get("X-Correlation-ID", "").strip()
    supplied = request.headers.get("X-Orchestrator-Signature", "").strip().lower()
    if not all((caller, timestamp_text, nonce, correlation_id, supplied)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="signed request headers required")
    expected_caller = ENDPOINT_CALLERS[endpoint_permission]
    if caller != expected_caller:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="caller is not permitted for endpoint")
    try:
        timestamp = int(timestamp_text)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid timestamp") from error
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="request timestamp expired")
    setting = CALLER_ENV[caller]
    path_attribute = {
        "ORCHESTRATOR_PLANNING_AGENT_HMAC_SECRET": "planning_agent_hmac_secret_file",
        "ORCHESTRATOR_N8N_HMAC_SECRET": "n8n_hmac_secret_file",
    }[setting]
    secret = secret_value(str(getattr(settings, path_attribute, "")), setting_name=setting)
    if not secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="caller secret unavailable")
    body = await request.body()
    message = b"\n".join(
        [caller.encode(), timestamp_text.encode(), nonce.encode(), correlation_id.encode(), body]
    )
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid body signature")
    if not repository.claim_nonce(caller=caller, nonce=nonce, timestamp=timestamp):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="nonce already used")
    return AuthenticatedRequest(caller, nonce, correlation_id, timestamp, body)

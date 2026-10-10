from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Callable, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


EVALUATOR_PLANNING_AGENT_PATH = "/api/v2/evaluator-results"
EVALUATOR_CALLER_IDENTITY = "evaluator"


class EvaluatorRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: int = Field(default=1, ge=0, le=300)
    max_backoff_seconds: int = Field(default=10, ge=0, le=300)
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    retry_on: list[Literal["timeout", "network", "http_429", "http_5xx"]] = Field(
        default_factory=lambda: ["timeout", "network", "http_429", "http_5xx"]
    )


class EvaluatorTimeoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(default=10, ge=1, le=300)


class EvaluatorPlanningAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_agent_base_url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://")
    path: Literal["/api/v2/evaluator-results"] = EVALUATOR_PLANNING_AGENT_PATH
    caller_identity: Literal["evaluator"] = EVALUATOR_CALLER_IDENTITY
    auth_scheme: Literal["hmac-sha256"] = "hmac-sha256"
    hmac_secret_ref: str = Field(min_length=1, max_length=256)
    retry_policy: EvaluatorRetryPolicy = Field(default_factory=EvaluatorRetryPolicy)
    timeout_policy: EvaluatorTimeoutPolicy = Field(default_factory=EvaluatorTimeoutPolicy)
    failure_owner: Literal["evaluator"] = "evaluator"
    persist_failed_deliveries: Literal[True] = True


class EvaluatorPlanningAgentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["evaluator-planning-agent.v1"]
    contract_version: Literal["evaluator-planning-agent.v1"]
    caller_identity: Literal["evaluator"]
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    workflow_version: int = Field(ge=1)
    evaluation_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    evaluation: dict[str, object]


DirectTransport = Callable[
    [str, str, dict[str, object], dict[str, str], int],
    dict[str, object],
]
FailureRecorder = Callable[[dict[str, object], str], None]


def canonical_evaluator_body(payload: EvaluatorPlanningAgentV1 | dict[str, object]) -> bytes:
    validated = (
        payload
        if isinstance(payload, EvaluatorPlanningAgentV1)
        else EvaluatorPlanningAgentV1.model_validate(payload)
    )
    return json.dumps(
        validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def build_evaluator_hmac_headers(
    payload: EvaluatorPlanningAgentV1 | dict[str, object],
    *,
    secret: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    if not secret:
        raise ValueError("evaluator direct-channel secret must not be empty")
    validated = (
        payload
        if isinstance(payload, EvaluatorPlanningAgentV1)
        else EvaluatorPlanningAgentV1.model_validate(payload)
    )
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce_value = nonce or uuid4().hex
    body = canonical_evaluator_body(validated)
    message = b"\n".join(
        [
            EVALUATOR_CALLER_IDENTITY.encode(),
            timestamp_text.encode(),
            nonce_value.encode(),
            validated.correlation_id.encode(),
            body,
        ]
    )
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {
        "X-Evaluator-Caller": EVALUATOR_CALLER_IDENTITY,
        "X-Evaluator-Timestamp": timestamp_text,
        "X-Evaluator-Nonce": nonce_value,
        "X-Correlation-ID": validated.correlation_id,
        "X-Evaluator-Signature": signature,
        "X-Idempotency-Key": validated.idempotency_key,
    }


def verify_evaluator_hmac_headers(
    payload: EvaluatorPlanningAgentV1 | dict[str, object],
    headers: dict[str, str],
    *,
    secret: str,
    now: int | None = None,
) -> None:
    validated = (
        payload
        if isinstance(payload, EvaluatorPlanningAgentV1)
        else EvaluatorPlanningAgentV1.model_validate(payload)
    )
    if headers.get("X-Evaluator-Caller") != EVALUATOR_CALLER_IDENTITY:
        raise ValueError("invalid evaluator caller identity")
    if headers.get("X-Correlation-ID") != validated.correlation_id:
        raise ValueError("evaluator correlation identity mismatch")
    if headers.get("X-Idempotency-Key") != validated.idempotency_key:
        raise ValueError("evaluator idempotency identity mismatch")
    try:
        timestamp = int(headers["X-Evaluator-Timestamp"])
        nonce = headers["X-Evaluator-Nonce"]
        supplied = headers["X-Evaluator-Signature"]
    except (KeyError, ValueError) as error:
        raise ValueError("incomplete evaluator HMAC headers") from error
    if abs((int(time.time()) if now is None else now) - timestamp) > 300:
        raise ValueError("evaluator request timestamp expired")
    expected = build_evaluator_hmac_headers(
        validated,
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
    )["X-Evaluator-Signature"]
    if not hmac.compare_digest(supplied.lower(), expected):
        raise ValueError("invalid evaluator body signature")


def _http_transport(
    method: str,
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, object]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        method=method,
    )
    response = urlopen(request, timeout=timeout_seconds)
    document = json.loads(response.read() or b"{}")
    if not isinstance(document, dict):
        raise RuntimeError("Planning Agent direct-channel response must be an object")
    return document


class EvaluatorPlanningAgentDirectAdapter:
    """Evaluator-owned transport. Orchestrator control flow never instantiates this adapter."""

    def __init__(
        self,
        config: EvaluatorPlanningAgentConfig,
        *,
        secret: str,
        transport: DirectTransport | None = None,
        record_failure: FailureRecorder | None = None,
    ) -> None:
        self.config = config
        self.secret = secret
        self.transport = transport or _http_transport
        self.record_failure = record_failure

    def send(
        self, payload: EvaluatorPlanningAgentV1 | dict[str, object]
    ) -> dict[str, object]:
        message = (
            payload
            if isinstance(payload, EvaluatorPlanningAgentV1)
            else EvaluatorPlanningAgentV1.model_validate(payload)
        )
        document = message.model_dump(mode="json")
        url = f"{self.config.planning_agent_base_url.rstrip('/')}{self.config.path}"
        last_error: Exception | None = None
        for attempt in range(1, self.config.retry_policy.max_attempts + 1):
            headers = build_evaluator_hmac_headers(message, secret=self.secret)
            try:
                return self.transport(
                    "POST",
                    url,
                    document,
                    headers,
                    self.config.timeout_policy.seconds,
                )
            except HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise
                last_error = error
            except (OSError, TimeoutError) as error:
                last_error = error
            if attempt == self.config.retry_policy.max_attempts:
                break
        assert last_error is not None
        if self.record_failure is not None:
            self.record_failure(document, str(last_error))
        raise last_error

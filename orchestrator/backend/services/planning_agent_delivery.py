from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from contracts.orchestration import (
    ESCALATION_PATH,
    FINAL_SUMMARY_PATH,
    WorkflowEscalationV1,
    WorkflowFinalSummaryV1,
)


ESCALATION_OUTBOX_KIND = "planning.workflow_escalation"
FINAL_SUMMARY_OUTBOX_KIND = "planning.workflow_final_summary"


@dataclass(frozen=True)
class DeliveryPolicy:
    path: str
    caller_identity: str
    outbox_kind: str
    max_attempts: int = 5
    timeout_seconds: int = 10
    backoff_seconds: int = 2
    retry_on: tuple[str, ...] = ("timeout", "network", "http_429", "http_5xx")


ESCALATION_POLICY = DeliveryPolicy(
    path=ESCALATION_PATH,
    caller_identity="orchestrator-escalation",
    outbox_kind=ESCALATION_OUTBOX_KIND,
)
FINAL_SUMMARY_POLICY = DeliveryPolicy(
    path=FINAL_SUMMARY_PATH,
    caller_identity="orchestrator-final-summary",
    outbox_kind=FINAL_SUMMARY_OUTBOX_KIND,
)

DeliveryTransport = Callable[
    [str, str, dict[str, object], dict[str, str], int],
    dict[str, object],
]


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
    result = json.loads(response.read() or b"{}")
    if not isinstance(result, dict):
        raise RuntimeError("Planning Agent delivery response must be an object")
    return result


class PlanningAgentDelivery:
    def __init__(
        self,
        *,
        planning_agent_base_url: str,
        secret: str,
        transport: DeliveryTransport | None = None,
    ) -> None:
        if not planning_agent_base_url.startswith(("http://", "https://")):
            raise ValueError("Planning Agent base URL must use HTTP or HTTPS")
        if not secret:
            raise ValueError("Planning Agent delivery secret must not be empty")
        self.base_url = planning_agent_base_url.rstrip("/")
        self.secret = secret
        self.transport = transport or _http_transport

    def _send(
        self,
        payload: dict[str, object],
        *,
        policy: DeliveryPolicy,
    ) -> dict[str, object]:
        timestamp = str(int(time.time()))
        nonce = uuid4().hex
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        correlation_id = str(payload["correlation_id"])
        message = b"\n".join(
            [policy.caller_identity.encode(), timestamp.encode(), nonce.encode(), correlation_id.encode(), body]
        )
        headers = {
            "X-Orchestrator-Caller": policy.caller_identity,
            "X-Orchestrator-Channel": policy.outbox_kind.removeprefix("planning.").replace("_", "-"),
            "X-Orchestrator-Timestamp": timestamp,
            "X-Orchestrator-Nonce": nonce,
            "X-Correlation-ID": correlation_id,
            "X-Operation-Key": str(payload["operation_key"]),
            "X-Orchestrator-Signature": hmac.new(
                self.secret.encode(), message, hashlib.sha256
            ).hexdigest(),
        }
        return self.transport(
            "POST",
            f"{self.base_url}{policy.path}",
            payload,
            headers,
            policy.timeout_seconds,
        )

    def send_workflow_escalation(self, payload: dict[str, object]) -> dict[str, object]:
        validated = WorkflowEscalationV1.model_validate(payload)
        return self._send(validated.model_dump(mode="json"), policy=ESCALATION_POLICY)

    def send_workflow_final_summary(self, payload: dict[str, object]) -> dict[str, object]:
        validated = WorkflowFinalSummaryV1.model_validate(payload)
        return self._send(validated.model_dump(mode="json"), policy=FINAL_SUMMARY_POLICY)


def planning_agent_outbox_handlers(control: object) -> dict[str, Callable[[dict[str, object], str], object]]:
    return {
        ESCALATION_OUTBOX_KIND: lambda payload, operation_key: control.send_workflow_escalation(
            payload, operation_key=operation_key
        ),
        FINAL_SUMMARY_OUTBOX_KIND: lambda payload, operation_key: control.send_workflow_final_summary(
            payload, operation_key=operation_key
        ),
    }

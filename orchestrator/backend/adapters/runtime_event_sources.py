from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Callable, Iterable, Mapping

from domain.artifact import ArtifactIdentity
from domain.runtime_events import (
    RuntimeEvent,
    hash_payload,
    redact_runtime_payload,
    utc_now,
)


def _time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_now()


class RuntimeSourceAdapter:
    source = ""
    event_aliases: Mapping[str, str] = {}

    def to_event(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: str,
        correlation_id: str,
        execution_id: str,
        workflow_version: int,
    ) -> RuntimeEvent:
        raw = dict(payload)
        raw_type = str(raw.get("event_type", "")).strip()
        event_type = self.event_aliases.get(raw_type.lower(), raw_type.lower())
        if not event_type:
            raise ValueError("event_type is required")
        digest = hash_payload(raw)
        external_reference = raw.get("external_reference")
        external = None if external_reference is None else str(external_reference)
        supplied_id = raw.get("event_id")
        event_id = str(supplied_id).strip() if supplied_id is not None else ""
        if not event_id:
            event_id = "runtime-event-" + hashlib.sha256(
                f"{self.source}:{external or execution_id}:{digest}".encode("utf-8")
            ).hexdigest()
        safe_payload = redact_runtime_payload(raw.get("payload", raw))
        if not isinstance(safe_payload, dict):
            raise ValueError("Runtime Event payload must be an object")
        return RuntimeEvent(
            event_id,
            execution_id,
            event_type,
            workflow_version,
            _time(raw.get("occurred_at")),
            node_id=None if raw.get("node_id") is None else str(raw["node_id"]),
            agent_id=None if raw.get("agent_id") is None else str(raw["agent_id"]),
            attempt_no=None
            if raw.get("attempt_no") is None
            else int(raw["attempt_no"]),
            error_code=None
            if raw.get("error_code") is None
            else str(raw["error_code"]),
            payload=safe_payload,
            source=self.source,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            external_reference=external,
            received_at=utc_now(),
            payload_hash=digest,
            version=int(raw.get("version", 1)),
        )


class N8nRuntimeAdapter(RuntimeSourceAdapter):
    source = "WORKFLOW_ENGINE"
    event_aliases = {
        "success": "workflow.completed",
        "completed": "workflow.completed",
        "failed": "workflow.failed",
        "running": "workflow.started",
        "execution.started": "workflow.started",
        "execution.completed": "workflow.completed",
        "execution.failed": "workflow.failed",
        "node.success": "node.completed",
        "node.running": "node.started",
        "node.failure": "node.failed",
    }

    def __init__(
        self,
        poller: Callable[[str, str], Iterable[Mapping[str, object]]] | None = None,
    ) -> None:
        self.poller = poller

    def poll(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeEvent, ...]:
        if self.poller is None:
            return ()
        events = []
        for raw in self.poller(tenant_id, execution_id):
            events.append(
                self.to_event(
                    raw,
                    tenant_id=tenant_id,
                    correlation_id=str(raw["correlation_id"]),
                    execution_id=execution_id,
                    workflow_version=int(raw["workflow_version"]),
                )
            )
        return tuple(events)


class AgentRuntimeAdapter(RuntimeSourceAdapter):
    source = "AGENT_RUNTIME"
    event_aliases = {
        "started": "agent.started",
        "completed": "agent.completed",
        "success": "agent.completed",
        "failed": "agent.failed",
        "failure": "agent.failed",
        "agent.output.created": "agent.completed",
    }


class ArtifactRuntimeAdapter(RuntimeSourceAdapter):
    source = "ARTIFACT_STORE"
    event_aliases = {
        "created": "artifact.created",
        "validated": "artifact.validated",
        "invalid": "artifact.invalid",
        "validation_failed": "artifact.invalid",
    }

    def from_artifact(
        self,
        artifact: ArtifactIdentity,
        event_type: str,
        *,
        correlation_id: str,
    ) -> RuntimeEvent:
        return self.to_event(
            {
                "event_type": event_type,
                "event_id": f"{artifact.artifact_id}:{event_type}",
                "external_reference": artifact.artifact_id,
                "node_id": artifact.producer_node_id,
                "attempt_no": artifact.producer_attempt_no,
                "occurred_at": artifact.created_at.isoformat(),
                "payload": {
                    "artifact_id": artifact.artifact_id,
                    "content_hash": artifact.content_hash,
                    "validation_status": artifact.validation_status,
                },
            },
            tenant_id=artifact.tenant_id,
            correlation_id=correlation_id,
            execution_id=artifact.producer_execution_id,
            workflow_version=artifact.workflow_version,
        )


# Explicit names used by integration code and external adapter wiring.
N8nRuntimeEventAdapter = N8nRuntimeAdapter
AgentRuntimeEventAdapter = AgentRuntimeAdapter
ArtifactRuntimeEventAdapter = ArtifactRuntimeAdapter

from __future__ import annotations

from datetime import datetime
from typing import Literal, Mapping

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from adapters.runtime_event_sources import (
    AgentRuntimeAdapter,
    ArtifactRuntimeAdapter,
    N8nRuntimeAdapter,
)
from application.runtime_event_service import RuntimeEventService
from domain.runtime_events import RuntimeEventConflict, RuntimeEventNotFound


class RuntimeEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str | None = Field(default=None, min_length=1)
    source: Literal["WORKFLOW_ENGINE", "AGENT_RUNTIME", "ARTIFACT_STORE"]
    event_type: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    workflow_version: int = Field(ge=1)
    node_id: str | None = None
    agent_id: str | None = None
    attempt_no: int | None = Field(default=None, ge=1)
    external_reference: str | None = None
    occurred_at: datetime
    version: int = Field(default=1, ge=1)
    error_code: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


_EVALUATOR_FIELDS = frozenset(
    {"evaluator", "evaluation", "score", "quality_score", "semantic_score", "reusable"}
)


def _reject_evaluator_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in _EVALUATOR_FIELDS:
                raise ValueError("evaluator fields are not accepted by Runtime Event ingest")
            _reject_evaluator_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_evaluator_fields(nested)


def _event_payload(event) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "event_id": event.event_id,
        "source": event.source,
        "event_type": event.event_type,
        "execution_id": event.execution_id,
        "node_id": event.node_id,
        "attempt_no": event.attempt_no,
        "external_reference": event.external_reference,
        "occurred_at": event.occurred_at.isoformat(),
        "received_at": event.received_at.isoformat(),
        "payload_hash": event.payload_hash,
        "processing_status": event.processing_status,
        "processing_retry_count": event.processing_retry_count,
        "processing_error_code": event.processing_error_code,
        "processing_error_message": event.processing_error_message,
    }


def create_runtime_event_router(service: RuntimeEventService) -> APIRouter:
    router = APIRouter(tags=["runtime-events"])
    adapters = {
        "WORKFLOW_ENGINE": N8nRuntimeAdapter(),
        "AGENT_RUNTIME": AgentRuntimeAdapter(),
        "ARTIFACT_STORE": ArtifactRuntimeAdapter(),
    }

    @router.post("/api/v1/runtime-events", status_code=status.HTTP_202_ACCEPTED)
    async def receive_runtime_event(
        body: RuntimeEventBody,
        response: Response,
        tenant_id: str = Header(alias="X-Tenant-Id"),
        correlation_id: str = Header(alias="X-Correlation-Id"),
    ) -> Mapping[str, object]:
        raw = body.model_dump(mode="json", exclude={"source", "execution_id", "workflow_version"})
        try:
            _reject_evaluator_fields(raw)
            event = adapters[body.source].to_event(
                raw,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                execution_id=body.execution_id,
                workflow_version=body.workflow_version,
            )
            receipt = service.receive(event)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Execution not found") from error
        except RuntimeEventConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not receipt.created:
            response.status_code = status.HTTP_200_OK
        return {
            "accepted": True,
            "created": receipt.created,
            "event": _event_payload(receipt.event),
        }

    @router.get("/api/v1/executions/{execution_id}/runtime-events")
    async def list_runtime_events(
        execution_id: str,
        tenant_id: str = Header(alias="X-Tenant-Id"),
    ) -> Mapping[str, object]:
        try:
            service.reconciliation.executions.get_execution(tenant_id, execution_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Execution not found") from error
        events = service.events.list_events(tenant_id, execution_id)
        observations = service.events.list_observations(tenant_id, execution_id)
        return {
            "execution_id": execution_id,
            "events": [_event_payload(item) for item in events],
            "observations": [
                {
                    "observation_id": item.observation_id,
                    "event_id": item.event_id,
                    "observed_state": item.observed_state,
                    "source": item.source,
                    "node_id": item.node_id,
                    "attempt_no": item.attempt_no,
                    "external_reference": item.external_reference,
                    "confidence": item.confidence,
                    "occurred_at": item.occurred_at.isoformat(),
                    "created_at": item.created_at.isoformat(),
                }
                for item in observations
            ],
        }

    @router.post("/api/v1/executions/{execution_id}/reconcile")
    async def reconcile_execution(
        execution_id: str,
        tenant_id: str = Header(alias="X-Tenant-Id"),
    ) -> Mapping[str, object]:
        try:
            result = service.reconciliation.reconcile(execution_id, tenant_id=tenant_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Execution not found") from error
        return {
            "execution_id": result.execution_id,
            "status": result.status,
            "applied_transitions": list(result.applied_transitions),
            "evidence": list(result.evidence),
            "raw_facts": list(result.raw_facts),
        }

    return router

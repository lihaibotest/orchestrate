from __future__ import annotations

from datetime import datetime
from typing import Mapping

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from application.execution_controller import (
    ExecutionController,
    ExecutionView,
    StartExecutionCommand,
)
from domain.execution_control import (
    ExecutionControlError,
    ExecutionIdempotencyConflict,
    ExecutionSecurityError,
    TenantContext,
)
from domain.self_test import ExecuteGateError


class CreateExecutionBody(BaseModel):
    workflow_family_id: str = Field(min_length=1)
    workflow_version: int = Field(ge=1)
    client_request_id: str | None = None
    inputs: dict[str, object] = Field(default_factory=dict)


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def execution_view_payload(view: ExecutionView) -> dict[str, object]:
    execution = view.execution
    return {
        "execution_id": execution.execution_id,
        "tenant_id": execution.tenant_id,
        "workflow_family_id": execution.workflow_family_id,
        "workflow_version": execution.workflow_version,
        "registry_snapshot_id": execution.registry_snapshot_id,
        "ir_hash": execution.ir_hash,
        "workflow_hash": execution.workflow_hash,
        "execution_mode": execution.execution_mode,
        "status": execution.status,
        "correlation_id": execution.correlation_id,
        "engine": execution.engine,
        "engine_workflow_id": execution.engine_workflow_id,
        "engine_execution_id": execution.engine_execution_id,
        "started_at": _time(execution.started_at),
        "ended_at": _time(execution.ended_at),
        "idempotent_replay": view.idempotent_replay,
        "nodes": [
            {
                "node_id": node.node_id,
                "status": node.status,
                "agent_id": node.agent_id,
                "agent_version": node.agent_version,
                "current_attempt": node.current_attempt,
                "checkpoint_id": node.checkpoint_id,
                "attempts": [
                    {
                        "attempt_no": attempt.attempt_no,
                        "status": attempt.status,
                        "retry_count": attempt.retry_count,
                        "error_code": attempt.error_code,
                        "started_at": _time(attempt.started_at),
                        "ended_at": _time(attempt.ended_at),
                    }
                    for attempt in view.attempts
                    if attempt.node_id == node.node_id
                ],
            }
            for node in view.nodes
        ],
    }


def create_execution_router(controller: ExecutionController) -> APIRouter:
    router = APIRouter(prefix="/api/v1/executions", tags=["executions"])

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_execution(
        body: CreateExecutionBody,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        tenant_id: str = Header(alias="X-Tenant-Id"),
        correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Mapping[str, object]:
        try:
            view = controller.start(
                StartExecutionCommand(
                    TenantContext(tenant_id),
                    body.workflow_family_id,
                    body.workflow_version,
                    body.client_request_id or idempotency_key,
                    idempotency_key,
                    body.inputs,
                    correlation_id,
                )
            )
        except (ExecuteGateError, ExecutionIdempotencyConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ExecutionSecurityError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ExecutionControlError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if view.idempotent_replay:
            response.status_code = status.HTTP_200_OK
        return execution_view_payload(view)

    @router.get("/{execution_id}")
    async def get_execution(
        execution_id: str,
        tenant_id: str = Header(alias="X-Tenant-Id"),
    ) -> Mapping[str, object]:
        try:
            return execution_view_payload(
                controller.get(TenantContext(tenant_id), execution_id)
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Execution not found") from error

    return router

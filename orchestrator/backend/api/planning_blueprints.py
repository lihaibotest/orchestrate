from __future__ import annotations

import hmac
from typing import Mapping

from fastapi import APIRouter, Header, HTTPException, status

from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService
from domain.planning_blueprint import (
    BlueprintCompilationFailed, BlueprintIdempotencyConflict,
    BlueprintIRValidationFailed, BlueprintSecretPresent, BlueprintSchemaInvalid,
    CapabilityInvalid, PlanDagInvalid, PlanningBlueprintError,
)


def create_planning_blueprint_router(service: PlanningBlueprintIngestionService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/planning-blueprints", tags=["planning-blueprints"])

    @router.post("", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_planning_blueprint(
        payload: dict[str, object],
        tenant_id: str = Header(alias="X-Tenant-Id"),
        correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
        traceparent: str | None = Header(default=None, alias="traceparent"),
        tracestate: str | None = Header(default=None, alias="tracestate"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Mapping[str, object]:
        try:
            return service.ingest(
                payload, tenant_id=tenant_id, correlation_id=correlation_id,
                traceparent=traceparent, tracestate=tracestate,
                idempotency_key=idempotency_key,
            ).as_dict()
        except BlueprintIdempotencyConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.error_code, "message": str(error)}) from error
        except (BlueprintSecretPresent, BlueprintSchemaInvalid, CapabilityInvalid, PlanDagInvalid,
                BlueprintIRValidationFailed, BlueprintCompilationFailed) as error:
            raise HTTPException(status_code=422, detail={"code": error.error_code, "message": str(error)}) from error
        except PlanningBlueprintError as error:
            raise HTTPException(status_code=409, detail={"code": error.error_code, "message": str(error)}) from error

    return router


def create_execution_submit_router(
    service: PlanningBlueprintIngestionService,
    *,
    bearer_token: str | None = None,
) -> APIRouter:
    """V2.0 Execution Submit wire adapter.

    The transport wrapper is deliberately kept separate from the historical
    planning-blueprints endpoint.  The execution contract receives a complete
    ``exec_plan`` and never accepts runtime or evaluator launch context.
    """
    router = APIRouter(prefix="/api/execution/v1", tags=["execution-submit"])

    @router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def submit_execution_run(
        payload: dict[str, object],
        tenant_id: str = Header(alias="X-Tenant-Id"),
        authorization: str | None = Header(default=None, alias="Authorization"),
        traceparent: str | None = Header(default=None, alias="traceparent"),
        tracestate: str | None = Header(default=None, alias="tracestate"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Mapping[str, object]:
        if bearer_token is not None:
            scheme, _, presented = (authorization or "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(presented, bearer_token):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid execution submit token")
        unknown = set(payload) - {"exec_plan", "launch_context"}
        if unknown:
            raise HTTPException(status_code=422, detail={"code": "BLUEPRINT_SCHEMA_INVALID", "message": "unknown submit fields: " + ", ".join(sorted(unknown))})
        exec_plan = payload.get("exec_plan")
        if not isinstance(exec_plan, dict):
            raise HTTPException(status_code=422, detail={"code": "BLUEPRINT_SCHEMA_INVALID", "message": "exec_plan must be a complete object"})
        launch_context = payload.get("launch_context")
        if launch_context is not None and not isinstance(launch_context, dict):
            raise HTTPException(status_code=422, detail={"code": "BLUEPRINT_SCHEMA_INVALID", "message": "launch_context must be an object"})
        if isinstance(launch_context, dict) and "evaluation_id" in launch_context:
            raise HTTPException(status_code=422, detail={"code": "BLUEPRINT_SCHEMA_INVALID", "message": "evaluation_id is not allowed for V2.0 Monitoring submit"})
        try:
            return service.ingest(
                exec_plan,
                tenant_id=tenant_id,
                traceparent=traceparent,
                tracestate=tracestate,
                idempotency_key=idempotency_key,
            ).as_dict()
        except BlueprintIdempotencyConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.error_code, "message": str(error)}) from error
        except (BlueprintSecretPresent, BlueprintSchemaInvalid, CapabilityInvalid, PlanDagInvalid,
                BlueprintIRValidationFailed, BlueprintCompilationFailed) as error:
            raise HTTPException(status_code=422, detail={"code": error.error_code, "message": str(error)}) from error
        except PlanningBlueprintError as error:
            raise HTTPException(status_code=409, detail={"code": error.error_code, "message": str(error)}) from error

    return router

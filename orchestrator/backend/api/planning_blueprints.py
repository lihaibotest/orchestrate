from __future__ import annotations

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
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Mapping[str, object]:
        try:
            return service.ingest(
                payload, tenant_id=tenant_id, correlation_id=correlation_id,
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

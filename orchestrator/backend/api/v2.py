from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, status

from blueprint.normalizer import NormalizationError, normalize_blueprint
from contracts.runtime_event import RuntimeAuthorizeV2, RuntimeEventV2, RuntimeTerminalEventV2
from persistence.execution_plan_repository import ExecutionPlanRepository
from persistence.runtime_repository import RuntimeRepository
from security.hmac_auth import authenticate
from services.blueprint_v2_service import BlueprintV2Service
from services.ir_repair_service import redact
from runtime.supervisor import RuntimeSupervisor
from runtime.budget import BudgetManager
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService
from domain.planning_blueprint import (
    BlueprintCompilationFailed,
    BlueprintIdempotencyConflict,
    BlueprintIRValidationFailed,
    BlueprintSchemaInvalid,
    BlueprintSecretPresent,
    CapabilityInvalid,
    PlanDagInvalid,
    PlanningBlueprintError,
)


def create_v2_router(
    repository: object,
    *,
    settings: object,
    gateway_ready: bool,
    on_terminal_event: Callable[[], None] | None = None,
    planning_blueprint_service: PlanningBlueprintIngestionService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2")
    plans = ExecutionPlanRepository(repository.database_path)
    runtime = RuntimeRepository(repository.database_path)
    service = BlueprintV2Service(plans)

    @router.post("/blueprints", status_code=status.HTTP_202_ACCEPTED)
    async def submit_blueprint(request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request, endpoint_permission="blueprint", repository=repository, settings=settings
        )
        try:
            raw = json.loads(authenticated.body.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("blueprint body must be an object")

            # The task-orchestration service uses this endpoint as its wire
            # boundary.  Accept the current planning-blueprint.v3/v4 artifact
            # here, while retaining the original planning-blueprint.v2 queue
            # contract for existing callers.
            submitted = raw.get("exec_plan") if "exec_plan" in raw else raw
            is_exec_plan = isinstance(submitted, dict) and submitted.get("schema_version") in {
                "planning-blueprint.v3",
                "planning-blueprint.v4",
            }
            if is_exec_plan:
                if planning_blueprint_service is None:
                    raise HTTPException(
                        status_code=503,
                        detail="PLANNING_BLUEPRINT_NOT_READY: durable planning blueprint service is unavailable",
                    )
                if "exec_plan" in raw:
                    unknown = set(raw) - {"exec_plan", "launch_context"}
                    if unknown:
                        raise ValueError(
                            "unknown submit fields: " + ", ".join(sorted(unknown))
                        )
                    launch_context = raw.get("launch_context")
                    if launch_context is not None and not isinstance(launch_context, dict):
                        raise ValueError("launch_context must be an object")
                    if isinstance(launch_context, dict) and "evaluation_id" in launch_context:
                        raise ValueError(
                            "evaluation_id is not allowed for V2.0 Monitoring submit"
                        )
                # The signed v2 planning-agent contract predates the tenant
                # header.  Preserve that wire contract and use the explicit
                # header when a deployment provides tenant scoping.
                tenant_id = request.headers.get("X-Tenant-Id", "default").strip() or "default"
                result = planning_blueprint_service.ingest(
                    submitted,
                    tenant_id=tenant_id,
                    correlation_id=authenticated.correlation_id,
                    traceparent=request.headers.get("traceparent")
                    or authenticated.correlation_id,
                    tracestate=request.headers.get("tracestate"),
                    idempotency_key=request.headers.get("Idempotency-Key"),
                )
                return result.as_dict()

            if not gateway_ready:
                raise HTTPException(status_code=503, detail="V2_NOT_READY: compatible n8n gateway capability contract is unavailable")
            normalized = normalize_blueprint(raw)
        except BlueprintIdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except (
            BlueprintSecretPresent,
            BlueprintSchemaInvalid,
            CapabilityInvalid,
            PlanDagInvalid,
            BlueprintIRValidationFailed,
            BlueprintCompilationFailed,
        ) as error:
            raise HTTPException(
                status_code=422,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except PlanningBlueprintError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, NormalizationError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if authenticated.correlation_id != normalized.blueprint.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        try:
            return service.enqueue(normalized)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/planning-jobs/{job_id}")
    async def planning_job(job_id: str, request: Request) -> dict[str, object]:
        await authenticate(
            request,
            endpoint_permission="blueprint-status",
            repository=repository,
            settings=settings,
        )
        job = plans.get_planning_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="planning job not found")
        return job

    @router.post("/runtime/events")
    async def runtime_event(request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request,
            endpoint_permission="runtime-event",
            repository=repository,
            settings=settings,
        )
        try:
            raw = json.loads(authenticated.body.decode("utf-8"))
            event = RuntimeEventV2.model_validate(raw)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if authenticated.correlation_id != event.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        execution = runtime.get_backend_execution(event.execution_id)
        identity = (event.session_id, event.plan_version, event.workflow_version)
        if execution is None or identity != (
            execution["session_id"], execution["plan_version"], execution["workflow_version"]
        ):
            raise HTTPException(status_code=409, detail="cross-session/version backend execution identity")
        decision = RuntimeSupervisor().decide(event, budget_available=True)
        safe_event = redact(event.model_dump(mode="json"))
        assert isinstance(safe_event, dict)
        stored, _created = runtime.record_runtime_decision(
            event=safe_event, decision=decision.model_dump(mode="json")
        )
        return stored

    @router.post("/runtime/terminal")
    async def runtime_terminal(request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request,
            endpoint_permission="runtime-event",
            repository=repository,
            settings=settings,
        )
        try:
            raw = json.loads(authenticated.body.decode("utf-8"))
            terminal = RuntimeTerminalEventV2.model_validate(raw)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if authenticated.correlation_id != terminal.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        execution = runtime.get_backend_execution(terminal.execution_id)
        identity = (terminal.session_id, terminal.plan_version, terminal.workflow_version)
        if execution is None or identity != (
            execution["session_id"], execution["plan_version"], execution["workflow_version"]
        ):
            raise HTTPException(
                status_code=409,
                detail="cross-session/version backend execution identity",
            )
        try:
            result = runtime.record_terminal_event(terminal.model_dump(mode="json"))
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if on_terminal_event is not None:
            on_terminal_event()
        return result

    @router.post("/runtime/authorize")
    async def runtime_authorize(request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request,
            endpoint_permission="runtime-authorize",
            repository=repository,
            settings=settings,
        )
        try:
            raw = json.loads(authenticated.body.decode("utf-8"))
            authorization = RuntimeAuthorizeV2.model_validate(raw)
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if authenticated.correlation_id != authorization.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        execution = runtime.get_backend_execution(authorization.execution_id)
        if execution is None or (authorization.session_id, authorization.plan_version, authorization.workflow_version) != (
            execution["session_id"], execution["plan_version"], execution["workflow_version"]
        ):
            raise HTTPException(status_code=409, detail="cross-session/version backend execution identity")
        try:
            allowed = BudgetManager(runtime).authorize(
                authorization.reservation_id, authorization.execution_id, authorization.node_id, authorization.attempt,
                calls=authorization.calls, cost_microusd=authorization.cost_microusd,
                duration_seconds=authorization.duration_seconds, lease_until=authorization.lease_until,
            )
            state = runtime.budget_state(authorization.execution_id)
        except KeyError:
            allowed = False
            state = {}
        return {
            "allow": allowed, "reservation_id": authorization.reservation_id if allowed else None,
            "remaining_calls": max(0, int(state.get("max_calls", 0)) - int(state.get("reserved_calls", 0)) - int(state.get("committed_calls", 0))),
            "remaining_cost_microusd": max(0, int(state.get("max_cost_microusd", 0)) - int(state.get("reserved_cost_microusd", 0)) - int(state.get("committed_cost_microusd", 0))),
            "reason": "AUTHORIZED" if allowed else "BUDGET_UNAVAILABLE",
        }

    @router.get("/execution-plans/{session_id}/{workflow_version}")
    async def execution_plan(session_id: str, workflow_version: int, request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request, endpoint_permission="v2-audit", repository=repository, settings=settings
        )
        plan = plans.get_execution_plan_audit(session_id, workflow_version)
        if plan is None:
            raise HTTPException(status_code=404, detail="execution plan not found")
        if authenticated.correlation_id != plan.pop("correlation_id"):
            raise HTTPException(status_code=409, detail="cross-session audit identity")
        safe = redact(plan)
        assert isinstance(safe, dict)
        return safe

    @router.get("/executions/{execution_id}/decisions")
    async def execution_decisions(execution_id: str, request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request, endpoint_permission="v2-audit", repository=repository, settings=settings
        )
        identity = runtime.get_execution_audit_identity(execution_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="execution not found")
        if authenticated.correlation_id != identity["correlation_id"]:
            raise HTTPException(status_code=409, detail="cross-session audit identity")
        safe = redact({"execution_id": execution_id, "decisions": runtime.decisions(execution_id)})
        assert isinstance(safe, dict)
        return safe

    @router.get("/executions/{execution_id}/budget")
    async def execution_budget(execution_id: str, request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request, endpoint_permission="v2-audit", repository=repository, settings=settings
        )
        identity = runtime.get_execution_audit_identity(execution_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="execution not found")
        if authenticated.correlation_id != identity["correlation_id"]:
            raise HTTPException(status_code=409, detail="cross-session audit identity")
        try:
            safe = redact(runtime.budget_audit(execution_id))
            assert isinstance(safe, dict)
            return safe
        except KeyError as error:
            raise HTTPException(status_code=404, detail="budget not found") from error

    return router

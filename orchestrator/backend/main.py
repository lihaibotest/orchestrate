from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import ValidationError

from contracts.orchestration import BlueprintIngress, FailureDiagnosis, N8NEvent, PlannerDirective
from api.v2 import create_v2_router
from security.hmac_auth import authenticate
from services.n8n_workflow_compiler import compile_workflow_spec
from services.registry_snapshot import RegistryCandidateSnapshot
from services.retry_runtime import choose_failure_action
from services.workflow_repair_service import WorkflowRepairService
from application.container import ApplicationContainer, build_container
from api.executions import create_execution_router
from api.artifacts import create_artifact_router
from api.runtime_events import create_runtime_event_router
from api.planning_blueprints import create_execution_submit_router, create_planning_blueprint_router
from infrastructure.config import OrchestratorSettings
from infrastructure.logging import configure_logging


LOGGER = logging.getLogger(__name__)


def create_app(
    settings: OrchestratorSettings | None = None,
    container: ApplicationContainer | None = None,
) -> FastAPI:
    resolved_settings = settings or (
        container.settings if container is not None else OrchestratorSettings.from_environment()
    )
    if container is not None and container.settings != resolved_settings:
        raise ValueError("injected container settings do not match create_app settings")
    configure_logging(resolved_settings.log_level)
    app_container = container or build_container(resolved_settings)
    repository = app_container.repository
    app = FastAPI(title="Standalone Orchestrator Agent", version="1.1.0")
    app.state.settings = resolved_settings
    app.state.container = app_container
    app.state.repository = repository
    app.state.metrics = app_container.metrics
    app.state.tracer = app_container.tracer
    LOGGER.info("orchestrator application initialized", extra={"service_name": resolved_settings.service_name})
    control = app_container.external_control()
    v2_ready = app_container.v2_gateway_ready()
    planning_blueprint_service = app_container.planning_blueprint_service()
    app.include_router(
        create_v2_router(
            repository,
            settings=resolved_settings,
            gateway_ready=v2_ready,
            on_terminal_event=app_container.drain_outbox,
            planning_blueprint_service=planning_blueprint_service,
        )
    )
    execution_controller = app_container.execution_controller()
    if execution_controller is not None:
        app.include_router(create_execution_router(execution_controller))
    artifact_manager = app_container.artifact_manager()
    if artifact_manager is not None:
        app.include_router(create_artifact_router(artifact_manager))
    runtime_events = app_container.runtime_event_service()
    if runtime_events is not None:
        app.include_router(create_runtime_event_router(runtime_events))
    if planning_blueprint_service is not None:
        execution_submit_token = (
            resolved_settings.read_secret(
                resolved_settings.execution_submit_token_file,
                setting_name="ORCHESTRATOR_EXECUTION_SUBMIT_TOKEN_FILE",
            )
            if resolved_settings.execution_submit_token_file
            else None
        )
        app.include_router(create_planning_blueprint_router(planning_blueprint_service))
        app.include_router(
            create_execution_submit_router(
                planning_blueprint_service,
                bearer_token=execution_submit_token,
            )
        )
    publisher = app_container.workflow_publisher()
    planning_worker = app_container.planning_worker(gateway_ready=v2_ready)

    worker_task: asyncio.Task[None] | None = None

    async def planning_worker_loop() -> None:
        assert planning_worker is not None
        while True:
            await asyncio.to_thread(planning_worker.run_once)
            await asyncio.sleep(resolved_settings.planning_worker_poll_seconds)

    async def start_planning_worker() -> None:
        nonlocal worker_task
        if planning_worker is not None:
            planning_worker.recover_incomplete_jobs()
            worker_task = asyncio.create_task(planning_worker_loop())

    async def stop_planning_worker() -> None:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        app_container.shutdown()

    app.router.add_event_handler("startup", start_planning_worker)
    app.router.add_event_handler("shutdown", stop_planning_worker)

    def validate_spec_identity(spec: dict[str, object], *, session_id: str, plan_version: int, workflow_version: int) -> None:
        for field, expected in (("session_id", session_id), ("plan_version", plan_version), ("workflow_version", workflow_version)):
            if spec.get(field) != expected:
                raise HTTPException(status_code=502, detail=f"workflow spec identity mismatch: {field}")

    def compile_kwargs() -> dict[str, str]:
        return app_container.workflow_compile_kwargs()

    def drain_outbox() -> None:
        app_container.drain_outbox()

    drain_outbox()

    @app.middleware("http")
    async def request_observability(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(resolved_settings.request_id_header) or uuid4().hex
        correlation_id = request.headers.get("X-Correlation-ID", "")
        started = time.monotonic()
        request.state.request_id = request_id
        with app_container.tracer.start_as_current_span(
            f"{request.method} {request.url.path}"
        ) as span:
            set_attribute = getattr(span, "set_attribute", None)
            if callable(set_attribute):
                set_attribute("http.request.method", request.method)
                set_attribute("url.path", request.url.path)
                set_attribute("request.id", request_id)
                if correlation_id:
                    set_attribute("correlation.id", correlation_id)
            try:
                response = await call_next(request)
            except Exception:
                app_container.metrics.inc("orchestrator_http_errors_total")
                LOGGER.exception(
                    "request failed",
                    extra={"request_id": request_id, "correlation_id": correlation_id},
                )
                raise
        elapsed = time.monotonic() - started
        app_container.metrics.inc("orchestrator_http_requests_total")
        app_container.metrics.observe("orchestrator_http_request_duration_seconds", elapsed)
        response.headers[resolved_settings.request_id_header] = request_id
        LOGGER.info(
            "request completed",
            extra={"request_id": request_id, "correlation_id": correlation_id},
        )
        return response

    async def payload(request: Request, model: type) -> tuple[dict[str, object], object]:
        try:
            raw = json.loads((await request.body()).decode("utf-8"))
            validated = model.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return raw, validated

    def validate_session(raw: dict[str, object]) -> dict[str, object]:
        session = repository.get_session(str(raw["session_id"]))
        if session is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session not found")
        for field in ("correlation_id", "plan_version", "workflow_version"):
            if session[field] != raw[field]:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"cross-session/version {field}")
        return session

    @app.post("/api/v1/blueprints", status_code=202)
    async def blueprint_ingress(request: Request) -> dict[str, object]:
        authenticated = await authenticate(
            request,
            endpoint_permission="blueprint",
            repository=repository,
            settings=resolved_settings,
        )
        raw, validated = await payload(request, BlueprintIngress)
        if authenticated.correlation_id != validated.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        session = repository.create_session(raw)
        repository.record_blueprint_version(validated.session_id, validated.plan_version, validated.blueprint)
        if control is None:
            return {"accepted": True, "session": session}
        registry_payload = control.registry()
        candidates = [RegistryCandidateSnapshot.model_validate(item) for item in registry_payload.get("items", [])]
        snapshot_version = int(registry_payload.get("snapshot_version", 1))
        repository.record_registry_snapshot(validated.session_id, snapshot_version, [candidate.model_dump() for candidate in candidates])
        spec = control.generate_spec({"blueprint": validated.blueprint, "session_id": validated.session_id, "plan_version": validated.plan_version, "workflow_version": 1})
        validate_spec_identity(spec, session_id=validated.session_id, plan_version=validated.plan_version, workflow_version=1)
        workflow = compile_workflow_spec(spec, agents=candidates, **compile_kwargs())
        published = publisher.publish(validated.session_id, 1, workflow, canary=spec.get("run_mode") == "canary", start_payload=raw) if publisher else {}
        workflow_id = str(published["workflow_id"])
        trigger_result = published.get("trigger_result")
        repository.record_generation_attempt(
            validated.session_id,
            1,
            1,
            "started",
            {"workflow_id": workflow_id, "trigger_result": trigger_result},
        )
        repository.record_workflow_version(validated.session_id, 1, workflow, status="active")
        repository.set_session_workflow_version(validated.session_id, 1, status="workflow_active")
        return {"accepted": True, "session_id": validated.session_id, "workflow_version": 1, "workflow_id": workflow_id, "trigger_result": trigger_result}

    @app.post("/api/v1/events/n8n", status_code=202)
    async def n8n_event(request: Request) -> dict[str, bool]:
        authenticated = await authenticate(
            request,
            endpoint_permission="n8n-event",
            repository=repository,
            settings=resolved_settings,
        )
        raw, validated = await payload(request, N8NEvent)
        if authenticated.correlation_id != validated.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        validate_session(raw)
        if not repository.claim_callback(validated.event_id, validated.session_id, "n8n-event", raw):
            return {"accepted": True}
        if validated.event_type in {"workflow.technical_failure", "workflow.failure"}:
            failure = dict(validated.failure or {})
            checkpoint = validated.checkpoint or {}
            node_id = validated.node_id or str(failure.get("node_id") or checkpoint.get("node_id") or "unknown-node")
            failure.update({
                "node_id": node_id,
                "attempt": validated.attempt or int(failure.get("attempt", 1)),
                "max_attempts": int(failure.get("max_attempts", validated.retry_policy.max_attempts)),
            })
            action = choose_failure_action(
                failure,
                workflow_version=validated.workflow_version,
                idempotency_key=validated.idempotency_key,
            )
            diagnosis = FailureDiagnosis(
                node_id=node_id,
                error_class=action.error_class,
                error_code=str(failure.get("error_code", failure.get("code", action.error_class))).upper()[:128],
                http_status=failure.get("http_status") if isinstance(failure.get("http_status"), int) else None,
                stderr=str(failure.get("stderr", ""))[:8192],
                input_summary=dict(failure.get("input_summary", {})) if isinstance(failure.get("input_summary"), dict) else {},
                diagnostic={key: value for key, value in failure.items() if key not in {"stderr", "input_summary"}},
                attempt=action.attempt if action.action == "evaluate" else action.attempt - 1,
                max_attempts=action.max_attempts,
                correlation_id=validated.correlation_id,
                session_id=validated.session_id,
                plan_version=validated.plan_version,
                workflow_version=validated.workflow_version,
                idempotency_key=validated.idempotency_key,
            )
            diagnosis_payload = diagnosis.model_dump()
            repository.record_node_attempt(
                session_id=validated.session_id,
                workflow_version=validated.workflow_version,
                node_id=node_id,
                attempt=diagnosis.attempt,
                idempotency_key=validated.idempotency_key,
                status="technical_retry_pending" if action.action == "retry" else "evaluation_pending",
                metadata=diagnosis_payload,
            )
            repository.record_failure_diagnosis(
                diagnosis_id=validated.event_id,
                session_id=validated.session_id,
                workflow_version=validated.workflow_version,
                node_id=node_id,
                idempotency_key=validated.idempotency_key,
                error_class=action.error_class,
                error_code=diagnosis.error_code,
                diagnosis=diagnosis_payload,
            )
            if action.action == "retry":
                repository.record_outbox(
                    outbox_id=f"retry:{validated.event_id}",
                    session_id=validated.session_id,
                    kind="n8n.retry",
                    payload={**raw, "attempt": action.attempt, "workflow_version": action.workflow_version, "idempotency_key": action.idempotency_key},
                )
                drain_outbox()
            return {"accepted": True}
        return {"accepted": True}

    @app.post("/api/v1/evaluations", status_code=status.HTTP_410_GONE)
    async def evaluation() -> None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="EVALUATOR_DIRECT_CHANNEL_REQUIRED",
        )

    @app.post("/api/v1/directives", status_code=202)
    async def directive(request: Request) -> dict[str, bool]:
        authenticated = await authenticate(
            request,
            endpoint_permission="directive",
            repository=repository,
            settings=resolved_settings,
        )
        raw, validated = await payload(request, PlannerDirective)
        if authenticated.correlation_id != validated.correlation_id:
            raise HTTPException(status_code=409, detail="correlation ID mismatch")
        validate_session(raw)
        if not repository.claim_callback(validated.directive_id, validated.session_id, "directive", raw):
            return {"accepted": True}
        if validated.directive in {"patch", "replan"}:
            if control is None or publisher is None:
                return {"accepted": True}
            history = repository.session_history(validated.session_id)
            current_row = next(
                (row for row in history["workflow_versions"] if int(row["workflow_version"]) == validated.workflow_version),
                None,
            )
            if current_row is None:
                raise HTTPException(status_code=409, detail="workflow version not found")
            current_workflow = json.loads(current_row["workflow_json"])
            current_spec = current_workflow.get("spec")
            if not isinstance(current_spec, dict):
                raise HTTPException(status_code=409, detail="stored workflow has no WorkflowSpec")
            repaired = WorkflowRepairService().regenerate(
                current_spec,
                diagnosis=validated.patch or {"directive_id": validated.directive_id},
                directive=validated.directive,
                generate=lambda repair_request: control.repair_spec(repair_request),
            )
            next_version = validated.workflow_version + 1
            repaired["workflow_version"] = next_version
            validate_spec_identity(
                repaired,
                session_id=validated.session_id,
                plan_version=validated.plan_version,
                workflow_version=next_version,
            )
            registry_payload = control.registry()
            candidates = [RegistryCandidateSnapshot.model_validate(item) for item in registry_payload.get("items", [])]
            repository.record_registry_snapshot(
                validated.session_id,
                int(registry_payload.get("snapshot_version", 1)),
                [candidate.model_dump() for candidate in candidates],
            )
            workflow = compile_workflow_spec(repaired, agents=candidates, **compile_kwargs())
            published = publisher.publish(
                validated.session_id,
                next_version,
                workflow,
                canary=repaired.get("run_mode") == "canary",
                start_payload={**raw, "workflow_version": next_version},
            )
            repository.record_generation_attempt(
                validated.session_id,
                next_version,
                1,
                "started",
                {"workflow_id": published["workflow_id"], "trigger_result": published.get("trigger_result"), "directive_id": validated.directive_id},
            )
            repository.record_workflow_version(validated.session_id, next_version, workflow, status="active")
            repository.set_session_workflow_version(validated.session_id, next_version, status="workflow_active")
            return {"accepted": True, "workflow_version": next_version, "workflow_id": published["workflow_id"]}
        repository.record_directive_with_outbox(
            directive_id=validated.directive_id,
            session_id=validated.session_id,
            workflow_version=validated.workflow_version,
            directive=validated.directive,
            patch=validated.patch or {},
            outbox_id=f"resume:{validated.directive_id}",
            payload=raw,
        )
        drain_outbox()
        return {"accepted": True}

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        session = repository.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        session.pop("blueprint_json", None)
        return session

    @app.get("/api/v1/sessions/{session_id}/history")
    async def get_session_history(session_id: str) -> dict[str, object]:
        if repository.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        return repository.session_history(session_id)

    @app.get("/health")
    async def health() -> dict[str, str]:
        app.state.metrics.inc("orchestrator_health_checks_total")
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, object]:
        """Readiness is distinct from liveness and remains dependency-aware."""
        checks = [item.as_dict() for item in app_container.health()]
        ready_state = all(item["status"] == "ok" for item in checks)
        if not ready_state:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        app.state.metrics.inc("orchestrator_readiness_checks_total")
        return {"status": "ready" if ready_state else "not_ready", "checks": checks}

    @app.get("/metrics", response_class=Response)
    async def metrics() -> Response:
        return Response(
            app.state.metrics.exposition(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


app = create_app()

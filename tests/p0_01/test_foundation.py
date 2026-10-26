from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
from unittest.mock import patch


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from application.container import build_container  # noqa: E402
from domain.health import ComponentHealth  # noqa: E402
from history.orchestration_repository import OrchestrationRepository  # noqa: E402
from infrastructure.config import OrchestratorSettings  # noqa: E402
from infrastructure.logging import JsonFormatter  # noqa: E402
from infrastructure.observability import (  # noqa: E402
    NoOpTracer,
    PrometheusMetrics,
    configure_otel,
)
from main import create_app  # noqa: E402


@dataclass(frozen=True)
class AsgiResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


async def asgi_get(
    app: object, path: str, *, headers: dict[str, str] | None = None
) -> AsgiResponse:
    messages: list[dict[str, object]] = []
    sent_request = False

    async def receive() -> dict[str, object]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[operator]
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")  # type: ignore[arg-type]
        for item in messages
        if item["type"] == "http.response.body"
    )
    response_headers = {
        name.decode().lower(): value.decode()
        for name, value in start.get("headers", [])  # type: ignore[union-attr]
    }
    return AsgiResponse(int(start["status"]), response_headers, body)


class FakeControl:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.gateway = object()
        self.closed = False

    def probe(self) -> bool:
        return self.healthy

    def close(self) -> None:
        self.closed = True


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[str] = []

    @contextmanager
    def start_as_current_span(self, name: str):
        self.spans.append(name)
        yield object()


class FoundationTests(unittest.TestCase):
    def test_settings_map_all_main_runtime_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = {
                "ORCHESTRATOR_DATABASE_PATH": str(Path(directory) / "state.sqlite3"),
                "ORCHESTRATOR_POSTGRES_DSN": "postgresql://orchestrator@postgres/orchestrator",
                "ORCHESTRATOR_POSTGRES_SCHEMA": "control_plane",
                "ORCHESTRATOR_REDIS_URL": "redis://redis:6379/2",
                "ORCHESTRATOR_REDIS_REQUIRED": "true",
                "ORCHESTRATOR_V2_ENABLED": "true",
                "ORCHESTRATOR_V2_AGENT_ALLOWED_HOSTS": "Agent.EXAMPLE,worker.local",
                "ORCHESTRATOR_V2_ALLOWED_PRIVATE_CIDRS": "10.0.0.0/8,192.168.0.0/16",
                "ORCHESTRATOR_REASONER_DEADLINE_SECONDS": "42.5",
                "N8N_BASE_URL": "http://n8n:5678",
                "N8N_GATEWAY_CREDENTIAL_REF": "gateway-ref",
                "N8N_WEBHOOK_CREDENTIAL_REF": "webhook-ref",
                "ORCHESTRATOR_MONITORING_ENDPOINT": "http://monitoring.internal:8080",
                "ORCHESTRATOR_TASK_ORCHESTRATION_ENDPOINT": "http://task.internal:7000",
                "ORCHESTRATOR_RANK_COST_WEIGHT": "2",
                "ORCHESTRATOR_RANK_LATENCY_WEIGHT": "3",
                "ORCHESTRATOR_PLANNING_AGENT_HMAC_SECRET_FILE": "/secrets/planning",
                "ORCHESTRATOR_N8N_HMAC_SECRET_FILE": "/secrets/n8n",
            }
            with patch.dict(os.environ, values, clear=True):
                settings = OrchestratorSettings.from_environment()
        self.assertEqual(settings.v2_agent_allowed_hosts, ("agent.example", "worker.local"))
        self.assertEqual(settings.postgres_dsn, "postgresql://orchestrator@postgres/orchestrator")
        self.assertEqual(settings.postgres_schema, "control_plane")
        self.assertEqual(settings.redis_url, "redis://redis:6379/2")
        self.assertTrue(settings.redis_required)
        self.assertEqual(settings.v2_allowed_private_cidrs, ("10.0.0.0/8", "192.168.0.0/16"))
        self.assertEqual(settings.reasoner_deadline_seconds, 42.5)
        self.assertEqual(settings.n8n_gateway_credential_ref, "gateway-ref")
        self.assertEqual(settings.n8n_webhook_credential_ref, "webhook-ref")
        self.assertEqual(settings.monitoring_endpoint, "http://monitoring.internal:8080")
        self.assertEqual(
            settings.task_orchestration_endpoint, "http://task.internal:7000"
        )
        self.assertEqual((settings.rank_cost_weight, settings.rank_latency_weight), (2, 3))
        self.assertEqual(settings.planning_agent_hmac_secret_file, "/secrets/planning")
        self.assertEqual(settings.n8n_hmac_secret_file, "/secrets/n8n")

    def test_settings_reject_invalid_boolean(self) -> None:
        with patch.dict(os.environ, {"ORCHESTRATOR_V2_ENABLED": "sometimes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                OrchestratorSettings.from_environment()

    def test_health_is_serializable(self) -> None:
        self.assertEqual(ComponentHealth("api", "ok").as_dict(), {"name": "api", "status": "ok"})

    def test_prometheus_metrics_expose_counters_and_observations(self) -> None:
        metrics = PrometheusMetrics()
        metrics.inc("requests_total", 3)
        metrics.observe("request_duration_seconds", 0.25)
        value = metrics.exposition()
        self.assertIn("# TYPE requests_total counter", value)
        self.assertIn("requests_total 3", value)
        self.assertIn("request_duration_seconds_count 1", value)
        self.assertIn("request_duration_seconds_sum 0.25", value)

    def test_metrics_can_be_disabled(self) -> None:
        metrics = PrometheusMetrics(enabled=False)
        metrics.inc("requests_total")
        self.assertEqual(metrics.exposition(), "")

    def test_otel_returns_safe_noop_tracer_when_disabled(self) -> None:
        tracer = configure_otel(False)
        self.assertIsInstance(tracer, NoOpTracer)
        with tracer.start_as_current_span("test") as span:
            span.set_attribute("key", "value")

    def test_json_formatter_emits_structured_record(self) -> None:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
        record.request_id = "request-1"
        value = json.loads(JsonFormatter().format(record))
        self.assertEqual(value["message"], "hello")
        self.assertEqual(value["level"], "INFO")
        self.assertEqual(value["request_id"], "request-1")

    def test_container_is_lazy_and_injectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = OrchestratorSettings(
                database_path=str(Path(directory) / "state.sqlite3"),
                readiness_require_external_control=True,
            )
            calls = 0
            control = FakeControl()

            def factory() -> FakeControl:
                nonlocal calls
                calls += 1
                return control

            tracer = FakeTracer()
            metrics = PrometheusMetrics()
            container = build_container(
                settings,
                metrics=metrics,
                tracer=tracer,
                external_control_factory=factory,
            )
            self.assertEqual(calls, 0)
            self.assertIs(container.external_control(), control)
            self.assertIs(container.external_control(), control)
            self.assertEqual(calls, 1)
            self.assertIs(container.metrics, metrics)
            self.assertIs(container.tracer, tracer)
            self.assertTrue(all(item.status == "ok" for item in container.health()))
            container.shutdown()
            self.assertTrue(control.closed)

    def test_repository_probe_checks_real_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = OrchestrationRepository(Path(directory) / "state.sqlite3")
            self.assertTrue(repository.probe())

    def test_required_unconfigured_external_control_fails_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = OrchestratorSettings(
                database_path=str(Path(directory) / "state.sqlite3"),
                readiness_require_external_control=True,
            )
            container = build_container(settings)
            values = {item.name: item.status for item in container.health()}
        self.assertEqual(values["persistence"], "ok")
        self.assertEqual(values["external_control"], "not_configured")

    def test_health_readiness_metrics_and_request_id_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = OrchestratorSettings(
                database_path=str(Path(directory) / "state.sqlite3")
            )
            app = create_app(settings)
            health = asyncio.run(
                asgi_get(app, "/health", headers={"X-Request-ID": "request-1"})
            )
            ready = asyncio.run(asgi_get(app, "/ready"))
            metrics = asyncio.run(asgi_get(app, "/metrics"))
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.headers["x-request-id"], "request-1")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")  # type: ignore[index]
        self.assertIn("orchestrator_http_requests_total", metrics.text)
        self.assertTrue(metrics.headers["content-type"].startswith("text/plain"))

    def test_readiness_returns_503_for_required_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = OrchestratorSettings(
                database_path=str(Path(directory) / "state.sqlite3"),
                readiness_require_external_control=True,
            )
            response = asyncio.run(asgi_get(create_app(settings), "/ready"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()

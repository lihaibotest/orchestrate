from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.fake_execution import InMemoryExecutionRepository  # noqa: E402
from adapters.fake_workflow_engine import (  # noqa: E402
    FakeWorkflowCompilerAdapter,
    FakeWorkflowEngineAdapter,
)
from api.executions import create_execution_router  # noqa: E402
from application.execution_controller import (  # noqa: E402
    ExecutionController,
    StartExecutionCommand,
)
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from domain.execution_control import (  # noqa: E402
    ExecutionIdempotencyConflict,
    ExecutionSecurityError,
    NodeNotInExecutionIR,
    TenantContext,
    runtime_definition_from_ir,
)
from domain.persistence import WorkflowFamily, WorkflowVersion  # noqa: E402
from domain.self_test import ExecuteGateError, PublishReservation  # noqa: E402
from ports.workflow_engine import WorkflowEngineError  # noqa: E402
from tests.p0_06.test_workflow_compiler import _valid_fixture  # noqa: E402


class LostResponseEngine(FakeWorkflowEngineAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.lose_once = True

    def execute_workflow(self, workflow, inputs, *, operation_key):  # type: ignore[no-untyped-def]
        result = super().execute_workflow(
            workflow, inputs, operation_key=operation_key
        )
        if self.lose_once:
            self.lose_once = False
            raise WorkflowEngineError("response lost after side effect")
        return result


class Fixture:
    def __init__(
        self,
        *,
        status: str = "PUBLISHED",
        with_publish_record: bool = True,
        engine: FakeWorkflowEngineAdapter | None = None,
    ) -> None:
        self.execution_ir, _snapshot = _valid_fixture()
        self.workflow = FakeWorkflowCompilerAdapter().compile(self.execution_ir)
        self.engine = engine or FakeWorkflowEngineAdapter()
        imported = self.engine.import_workflow(self.workflow, operation_key="fixture-import")
        self.published = self.engine.publish_workflow(
            imported, operation_key="fixture-publish"
        )
        family = WorkflowFamily("family-compiler", "tenant-1", "task-1")
        version = WorkflowVersion(
            "family-compiler",
            1,
            self.execution_ir.registry_snapshot_id,
            self.execution_ir.content_hash(),
            status,
        )
        self.repository = InMemoryExecutionRepository((family,), (version,))
        self.repository.save_runtime_definition(
            runtime_definition_from_ir(self.execution_ir)
        )
        if with_publish_record:
            self.repository.publish_reservations[("family-compiler", 1)] = PublishReservation(
                "family-compiler",
                1,
                self.execution_ir.registry_snapshot_id,
                self.execution_ir.content_hash(),
                self.workflow.workflow_hash,
                self.published.engine,
                self.published.workflow_id,
                "fixture-release",
                published=True,
            )
        self.release = WorkflowReleaseService(self.repository, self.engine)
        self.controller = ExecutionController(self.repository, self.release)

    def command(
        self,
        *,
        request_id: str = "request-1",
        api_key: str = "api-key-1",
        inputs: dict[str, object] | None = None,
        tenant: str = "tenant-1",
        correlation: str | None = "correlation-1",
    ) -> StartExecutionCommand:
        return StartExecutionCommand(
            TenantContext(tenant),
            "family-compiler",
            1,
            request_id,
            api_key,
            inputs or {"request": "run"},
            correlation,
        )


class FormalExecutionTests(unittest.TestCase):
    def test_published_release_starts_production_execution_and_freezes_identity(self) -> None:
        fixture = Fixture()
        view = fixture.controller.start(fixture.command())
        execution = view.execution
        self.assertEqual(execution.status, "RUNNING")
        self.assertEqual(execution.execution_mode, "PRODUCTION")
        self.assertEqual(execution.workflow_version, 1)
        self.assertEqual(execution.registry_snapshot_id, fixture.execution_ir.registry_snapshot_id)
        self.assertEqual(execution.ir_hash, fixture.execution_ir.content_hash())
        self.assertEqual(execution.workflow_hash, fixture.workflow.workflow_hash)
        self.assertEqual(len(view.nodes), len(fixture.execution_ir.nodes))
        engine_inputs = next(iter(fixture.engine.executions.values())).metadata["inputs"]
        self.assertEqual(engine_inputs["execution_id"], execution.execution_id)  # type: ignore[index]
        self.assertEqual(engine_inputs["correlation_id"], "correlation-1")  # type: ignore[index]
        self.assertEqual(engine_inputs["execution_mode"], "PRODUCTION")  # type: ignore[index]

    def test_all_non_published_states_and_missing_publish_record_are_rejected(self) -> None:
        for state in ("DRAFT", "VALIDATING", "SELF_TESTING", "SELF_TEST_FAILED", "VERIFIED"):
            with self.subTest(state=state):
                with self.assertRaises(ExecuteGateError):
                    Fixture(status=state).controller.start(Fixture(status=state).command())
        fixture = Fixture(with_publish_record=False)
        with self.assertRaises(ExecuteGateError):
            fixture.controller.start(fixture.command())

    def test_execution_request_is_idempotent_and_conflicts_on_changed_semantics(self) -> None:
        fixture = Fixture()
        first = fixture.controller.start(fixture.command())
        second = fixture.controller.start(fixture.command())
        self.assertEqual(first.execution.execution_id, second.execution.execution_id)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(len(fixture.repository.executions), 1)
        self.assertEqual(len(fixture.engine.executions), 1)
        with self.assertRaises(ExecutionIdempotencyConflict):
            fixture.controller.start(
                fixture.command(inputs={"request": "different"})
            )
        replay_with_new_transport_key = fixture.controller.start(
            fixture.command(api_key="different-api-key")
        )
        self.assertTrue(replay_with_new_transport_key.idempotent_replay)

    def test_lost_engine_response_retry_reuses_same_external_execution(self) -> None:
        fixture = Fixture(engine=LostResponseEngine())
        with self.assertRaises(WorkflowEngineError):
            fixture.controller.start(fixture.command())
        self.assertEqual(len(fixture.engine.executions), 1)
        replay = fixture.controller.start(fixture.command())
        self.assertEqual(replay.execution.status, "RUNNING")
        self.assertEqual(len(fixture.engine.executions), 1)

    def test_tenant_scope_and_sensitive_runtime_inputs_are_enforced(self) -> None:
        fixture = Fixture()
        with self.assertRaises(ValueError):
            fixture.controller.start(fixture.command(tenant="tenant-2"))
        view = fixture.controller.start(fixture.command())
        with self.assertRaises(KeyError):
            fixture.controller.get(TenantContext("tenant-2"), view.execution.execution_id)
        for value in (
            {"password": "plaintext"},
            {"api_key": "plaintext"},
            {"token": "plaintext"},
            {"token": "Bearer raw-token-value"},
            {"private_key": "-----BEGIN PRIVATE KEY-----abc"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ExecutionSecurityError):
                    Fixture().controller.start(Fixture().command(inputs=value))


class NodeAttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.view = self.fixture.controller.start(self.fixture.command())
        self.execution_id = self.view.execution.execution_id
        self.node_id = "agent-a"

    def _start_attempt(self):
        self.fixture.controller.mark_node_ready(self.execution_id, self.node_id)
        return self.fixture.controller.start_next_attempt(self.execution_id, self.node_id)

    def test_node_state_path_and_unknown_node_rejection(self) -> None:
        attempt = self._start_attempt()
        self.assertEqual(attempt.attempt_no, 1)
        self.assertEqual(
            self.fixture.repository.nodes[(self.execution_id, self.node_id)].status,
            "RUNNING",
        )
        completed = self.fixture.controller.complete_attempt(
            self.execution_id, self.node_id
        )
        self.assertEqual(completed.status, "SUCCEEDED")
        self.assertEqual(
            self.fixture.repository.nodes[(self.execution_id, self.node_id)].status,
            "SUCCEEDED",
        )
        with self.assertRaises(Exception):
            self.fixture.controller.mark_node_ready(self.execution_id, self.node_id)
        with self.assertRaises(NodeNotInExecutionIR):
            self.fixture.controller.mark_node_ready(self.execution_id, "v4-only-node")

    def test_proof_a_technical_retries_keep_attempt_one(self) -> None:
        attempt = self._start_attempt()
        self.assertEqual((attempt.attempt_no, attempt.retry_count), (1, 0))
        for expected_retry in (1, 2):
            failed = self.fixture.controller.record_technical_failure(
                self.execution_id,
                self.node_id,
                error_code="EO-EXEC-001",
                normalized_error_class="transient_network",
            )
            self.assertEqual((failed.attempt_no, failed.retry_count), (1, expected_retry))
            self.fixture.controller.resume_technical_retry(
                self.execution_id, self.node_id
            )
        completed = self.fixture.controller.complete_attempt(
            self.execution_id, self.node_id
        )
        self.assertEqual((completed.attempt_no, completed.retry_count), (1, 2))
        attempts = self.fixture.repository.list_attempts(self.execution_id, self.node_id)
        self.assertEqual([(item.attempt_no, item.retry_count) for item in attempts], [(1, 2)])

    def test_proof_b_only_explicit_business_reexecution_creates_attempt_two(self) -> None:
        first = self._start_attempt()
        self.fixture.controller.complete_attempt(
            self.execution_id,
            self.node_id,
            complete_node=False,
        )
        second = self.fixture.controller.start_next_attempt(
            self.execution_id, self.node_id
        )
        self.assertEqual((first.attempt_no, second.attempt_no), (1, 2))
        node = self.fixture.repository.nodes[(self.execution_id, self.node_id)]
        self.assertEqual(node.current_attempt, 2)
        self.assertEqual(len(self.fixture.repository.list_attempts(self.execution_id, self.node_id)), 2)

    def test_retry_exhaustion_fails_same_attempt_node_and_execution(self) -> None:
        self._start_attempt()
        for _ in range(2):
            self.fixture.controller.record_technical_failure(
                self.execution_id,
                self.node_id,
                error_code="EO-EXEC-001",
                normalized_error_class="timeout",
            )
            self.fixture.controller.resume_technical_retry(self.execution_id, self.node_id)
        exhausted = self.fixture.controller.record_technical_failure(
            self.execution_id,
            self.node_id,
            error_code="EO-EXEC-001",
            normalized_error_class="timeout",
        )
        self.assertEqual((exhausted.attempt_no, exhausted.retry_count, exhausted.status), (1, 2, "FAILED"))
        self.assertEqual(self.fixture.repository.nodes[(self.execution_id, self.node_id)].status, "FAILED")
        self.assertEqual(self.fixture.repository.executions[self.execution_id].status, "FAILED")
        self.assertEqual(len(self.fixture.repository.list_attempts(self.execution_id, self.node_id)), 1)

    def test_patch_replan_are_explicit_state_boundaries(self) -> None:
        patched = self.fixture.controller.request_recovery_decision(
            self.execution_id, "PATCH", "explicit decision"
        )
        self.assertEqual(patched.status, "PATCHING")
        with self.assertRaises(Exception):
            self.fixture.controller.request_recovery_decision(
                self.execution_id, "REPLAN", "cannot skip state machine"
            )


async def _asgi_request(app, method: str, path: str, body=None, headers=None):  # type: ignore[no-untyped-def]
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.request(method, path, json=body, headers=headers)
    return response.status_code, response.json()


class ExecutionApiTests(unittest.TestCase):
    def test_versioned_api_uses_headers_and_returns_read_model(self) -> None:
        from fastapi import FastAPI

        fixture = Fixture()
        app = FastAPI()
        app.include_router(create_execution_router(fixture.controller))
        headers = {
            "Idempotency-Key": "http-key-1",
            "X-Tenant-Id": "tenant-1",
            "X-Correlation-Id": "http-correlation",
        }
        body = {
            "workflow_family_id": "family-compiler",
            "workflow_version": 1,
            "client_request_id": "http-request-1",
            "inputs": {"request": "api"},
        }
        code, created = asyncio.run(_asgi_request(app, "POST", "/api/v1/executions", body, headers))
        self.assertEqual(code, 201)
        self.assertEqual(created["correlation_id"], "http-correlation")
        code, replay = asyncio.run(_asgi_request(app, "POST", "/api/v1/executions", body, headers))
        self.assertEqual(code, 200)
        self.assertTrue(replay["idempotent_replay"])
        code, loaded = asyncio.run(
            _asgi_request(
                app,
                "GET",
                f"/api/v1/executions/{created['execution_id']}",
                headers={"X-Tenant-Id": "tenant-1"},
            )
        )
        self.assertEqual(code, 200)
        self.assertEqual(loaded["execution_id"], created["execution_id"])


if __name__ == "__main__":
    unittest.main()

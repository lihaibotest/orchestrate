from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_agents import FakeAgentRegistryAdapter, FakeAgentRuntimeAdapter, InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_planning_blueprint import InMemoryProductionBlueprintRepository  # noqa: E402
from adapters.fake_self_test import FakeAgentBindingProbe, FakeArtifactProbe, FakeCanaryProbe, FakeObservabilityProbe  # noqa: E402
from adapters.fake_workflow_engine import FakeWorkflowEngineAdapter  # noqa: E402
from api.planning_blueprints import create_planning_blueprint_router  # noqa: E402
from application.execution_controller import ExecutionController, StartExecutionCommand  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from application.production_planning_blueprint_generation import ProductionPlanningBlueprintGenerationService  # noqa: E402
from application.self_test_controller import SelfTestController  # noqa: E402
from application.self_test_stages import S0ValidationStage, S1BindingStage, S2EngineStage, S3CanaryArtifactStage, S4ObservabilityStage  # noqa: E402
from application.workflow_compilation_service import WorkflowCompilationService  # noqa: E402
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from application.container import build_container  # noqa: E402
from backends.n8n.workflow_compiler import N8nCompilerConfig, N8nWorkflowCompilerAdapter  # noqa: E402
from domain.agent_preparation import AgentCandidate  # noqa: E402
from domain.execution_control import TenantContext  # noqa: E402
from domain.execution_ir_validator import (  # noqa: E402
    ExecutionIRValidationError,
    IRValidationIssue,
    IRValidationReport,
)
from domain.planning_blueprint import BlueprintIRValidationFailed  # noqa: E402
from domain.self_test import PublishGateError  # noqa: E402
from infrastructure.config import OrchestratorSettings  # noqa: E402
from ports.self_test import ProbeResult  # noqa: E402
from tests.planning_blueprint_v3.test_inbound_adapter import payload, upstream_payload  # noqa: E402
from tests.p0_06.test_workflow_compiler import REQUEST_SCHEMA, RESULT_SCHEMA  # noqa: E402


class CompositionFixture:
    def __init__(
        self,
        *,
        self_test_failure: bool = False,
        capabilities: tuple[str, ...] | None = None,
    ) -> None:
        self.repository = InMemoryProductionBlueprintRepository()
        self.snapshots = InMemoryRegistrySnapshotRepository()
        requested = capabilities or ("capability-agent-a",)
        input_schema = REQUEST_SCHEMA if capabilities is None else {"type": "object"}
        output_schema = RESULT_SCHEMA if capabilities is None else {"type": "object"}
        self.registry = FakeAgentRegistryAdapter(tuple(
            AgentCandidate(
                "agent-a" if len(requested) == 1 else f"agent-{index}",
                "1.0.0", (capability,), f"https://agents.example.test/{index}",
                input_schema, output_schema, f"credential-ref-{index}",
                {"mode": "request_response"},
            )
            for index, capability in enumerate(requested)
        ))
        self.runtime = FakeAgentRuntimeAdapter()
        self.engine = FakeWorkflowEngineAdapter()
        self.validation = ExecutionIRValidationService(self.snapshots)
        self.compiler = N8nWorkflowCompilerAdapter(
            N8nCompilerConfig(webhook_credential_ref="credential-ref-webhook")
        )
        self.compilation = WorkflowCompilationService(
            self.validation, self.compiler, self.repository
        )
        binding = FakeAgentBindingProbe(
            ProbeResult(False, "BINDING_MISMATCH") if self_test_failure else None
        )
        self.self_test = SelfTestController(
            self.repository,
            (
                S0ValidationStage(self.validation),
                S1BindingStage(binding),
                S2EngineStage(self.engine),
                S3CanaryArtifactStage(FakeCanaryProbe(), FakeArtifactProbe()),
                S4ObservabilityStage(FakeObservabilityProbe()),
            ),
        )
        self.release = WorkflowReleaseService(self.repository, self.engine)
        self.execution = ExecutionController(self.repository, self.release)
        self.generation = ProductionPlanningBlueprintGenerationService(
            self.repository, self.snapshots, self.registry, self.runtime,
            self.validation, self.compilation, self.engine, self.self_test, self.release,
            self.execution,
        )
        self.ingestion = PlanningBlueprintIngestionService(
            self.repository, generation=self.generation
        )

    def ingest(self, body: dict[str, object] | None = None):
        return self.ingestion.ingest(body or payload(), tenant_id="tenant-a")


class ProductionCompositionTests(unittest.TestCase):
    def test_real_upstream_shape_generates_compiled_workflow(self) -> None:
        fixture = CompositionFixture(capabilities=("case-select", "case-execute"))
        result = fixture.ingest(upstream_payload())
        self.assertEqual(result.workflow_reference["stage"], "PUBLISHED")
        self.assertEqual(result.correlation_id, "session-upstream")
        self.assertEqual(len(fixture.engine.workflows), 1)
        definition = fixture.repository.get_runtime_definition(
            str(result.workflow_family_id),
            int(result.workflow_version or 0),
            str(result.workflow_reference["ir_hash"]),
        )
        self.assertEqual(tuple(node.node_id for node in definition.nodes), (
            "execute-cases", "select-cases"
        ))

    def test_composition_root_resolves_production_generation_not_fake(self) -> None:
        class Control:
            def __init__(self) -> None:
                self.gateway = object()

        with TemporaryDirectory() as directory:
            settings = OrchestratorSettings(
                database_path=str(Path(directory) / "state.sqlite3"),
                postgres_dsn="postgresql://configured-but-not-opened/db",
            )
            container = build_container(settings, external_control_factory=Control)
            service = container.planning_blueprint_service()
            self.assertIsNotNone(service)
            assert service is not None
            self.assertIsInstance(service.generation, ProductionPlanningBlueprintGenerationService)
            self.assertNotEqual(type(service.generation).__name__, "FakePlanningBlueprintGenerationAdapter")

    def test_production_generation_is_not_fake_and_compiles_n8n(self) -> None:
        fixture = CompositionFixture()
        self.assertIsInstance(fixture.generation, ProductionPlanningBlueprintGenerationService)
        self.assertNotEqual(type(fixture.generation).__name__, "FakePlanningBlueprintGenerationAdapter")
        result = fixture.ingest()
        self.assertEqual(result.workflow_reference["stage"], "PUBLISHED")
        self.assertEqual(result.workflow_reference["engine"], "fake")
        workflow = next(iter(fixture.engine.workflows.values()))[0]
        self.assertEqual(workflow.target, "n8n")
        self.assertTrue(any(node["type"] == "n8n-nodes-base.httpRequest" for node in workflow.mutable_document()["nodes"]))

    def test_proof_b_self_test_publish_identity_and_record(self) -> None:
        fixture = CompositionFixture()
        result = fixture.ingest()
        family = str(result.workflow_family_id)
        version = int(result.workflow_version or 0)
        generation = next(iter(fixture.repository.generations.values()))
        self.assertEqual(generation.status, "PUBLISHED")
        self.assertIsNotNone(generation.ir_hash)
        self.assertIsNotNone(generation.workflow_hash)
        self.assertIsNotNone(generation.registry_snapshot_id)
        run = fixture.repository.get_self_test(str(generation.self_test_id))
        self.assertEqual([item.status for item in run.stage_results], ["PASS"] * 5)
        self.assertEqual(fixture.repository.versions[(family, version)].status, "PUBLISHED")
        publish = fixture.repository.get_published_release(family, version)
        self.assertEqual((publish.ir_hash, publish.workflow_hash, publish.registry_snapshot_id),
                         (generation.ir_hash, generation.workflow_hash, generation.registry_snapshot_id))

    def test_proof_c_formal_execution_preserves_correlation_and_identity(self) -> None:
        fixture = CompositionFixture()
        result = fixture.ingest()
        view = fixture.generation.start_formal_execution(
            result.blueprint_id,
            tenant_id="tenant-a",
            client_request_id="request-blueprint-1",
            api_idempotency_key="api-key-1",
            inputs={"request": "run"},
        )
        self.assertEqual(view.execution.status, "RUNNING")
        self.assertEqual(view.execution.correlation_id, "corr-v3")
        self.assertEqual(view.execution.workflow_family_id, result.workflow_family_id)
        self.assertEqual(view.execution.workflow_version, result.workflow_version)
        generation = fixture.repository.get_generation(result.blueprint_id)
        self.assertEqual(generation.execution_id, view.execution.execution_id)

    def test_repeat_blueprint_replays_same_workflow_without_v2(self) -> None:
        fixture = CompositionFixture()
        first = fixture.ingest()
        second = fixture.ingest()
        self.assertEqual((first.workflow_family_id, first.workflow_version),
                         (second.workflow_family_id, second.workflow_version))
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(len(fixture.repository.versions), 1)
        self.assertEqual(len(fixture.engine.workflows), 1)

    def test_plan_version_cannot_force_internal_workflow_version(self) -> None:
        fixture = CompositionFixture()
        body = payload()
        body["plan_version"] = 41
        body["workflow_version"] = 999
        result = fixture.ingestion.ingest(body, tenant_id="tenant-a")
        self.assertEqual(result.plan_version, 41)
        self.assertEqual(result.workflow_version, 1)
        self.assertNotEqual(result.workflow_version, body["workflow_version"])

    def test_self_test_failure_does_not_publish_or_formally_execute(self) -> None:
        fixture = CompositionFixture(self_test_failure=True)
        result = fixture.ingest()
        self.assertEqual(result.workflow_reference["status"], "SELF_TEST_FAILED")
        generation = next(iter(fixture.repository.generations.values()))
        self.assertEqual(generation.status, "FAILED")
        self.assertEqual(fixture.repository.publish_reservations, {})
        with self.assertRaises(Exception):
            ExecutionController(fixture.repository, fixture.release).start(
                StartExecutionCommand(TenantContext("tenant-a"), generation.workflow_family_id,
                    generation.workflow_version, "blocked", "blocked-key", {}, "corr-v3")
            )

    def test_api_enters_production_composition(self) -> None:
        from fastapi import FastAPI
        import httpx

        fixture = CompositionFixture()
        app = FastAPI()
        app.include_router(create_planning_blueprint_router(fixture.ingestion))

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/api/v1/planning-blueprints", json=payload(),
                    headers={"X-Tenant-Id": "tenant-a", "X-Correlation-Id": "corr-v3",
                             "Idempotency-Key": "plan-v3-key"})

        response = asyncio.run(request())
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["workflow_reference"]["stage"], "PUBLISHED")

    def test_response_loss_after_generation_recovers_persisted_reference(self) -> None:
        fixture = CompositionFixture()
        original = fixture.repository.update_blueprint_result
        failed = False

        def lose_response(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal failed
            if not failed:
                failed = True
                raise ConnectionError("response lost after generation")
            return original(*args, **kwargs)

        fixture.repository.update_blueprint_result = lose_response  # type: ignore[method-assign]
        with self.assertRaises(ConnectionError):
            fixture.ingest()
        self.assertEqual(len(fixture.repository.versions), 1)
        self.assertEqual(len(fixture.engine.workflows), 1)
        recovered = fixture.ingest()
        self.assertEqual(recovered.workflow_reference["stage"], "PUBLISHED")
        self.assertEqual(len(fixture.repository.versions), 1)
        self.assertEqual(len(fixture.engine.workflows), 1)

    def test_invalid_blueprint_never_creates_workflow(self) -> None:
        fixture = CompositionFixture()
        invalid = payload()
        invalid["tasks"] = [{**invalid["tasks"][0], "depends_on": ["missing"]}]
        with self.assertRaises(Exception):
            fixture.ingestion.ingest(invalid, tenant_id="tenant-a")
        self.assertEqual(fixture.repository.versions, {})
        self.assertEqual(fixture.engine.workflows, {})

    def test_ir_validation_failure_never_invokes_compiler_or_engine(self) -> None:
        fixture = CompositionFixture()
        issue = IRValidationIssue("IR-TEST-001", "nodes", "injected invalid IR")
        fixture.validation.validate_or_raise = Mock(  # type: ignore[method-assign]
            side_effect=ExecutionIRValidationError(IRValidationReport((issue,)))
        )
        fixture.compiler.compile = Mock(wraps=fixture.compiler.compile)  # type: ignore[method-assign]
        with self.assertRaises(BlueprintIRValidationFailed) as raised:
            fixture.ingest()
        self.assertEqual(raised.exception.error_code, "IR_VALIDATION_FAILED")
        fixture.compiler.compile.assert_not_called()
        self.assertEqual(fixture.engine.workflows, {})
        self.assertEqual(fixture.repository.versions, {})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_agents import FakeAgentRegistryAdapter, FakeAgentRuntimeAdapter  # noqa: E402
from adapters.fake_self_test import FakeAgentBindingProbe, FakeArtifactProbe, FakeCanaryProbe, FakeObservabilityProbe  # noqa: E402
from adapters.fake_workflow_engine import FakeWorkflowEngineAdapter  # noqa: E402
from adapters.postgres_planning_blueprint import PostgresPlanningBlueprintRepository  # noqa: E402
from application.execution_controller import ExecutionController  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from application.production_planning_blueprint_generation import ProductionPlanningBlueprintGenerationService  # noqa: E402
from application.self_test_controller import SelfTestController  # noqa: E402
from application.self_test_stages import S0ValidationStage, S1BindingStage, S2EngineStage, S3CanaryArtifactStage, S4ObservabilityStage  # noqa: E402
from application.workflow_compilation_service import WorkflowCompilationService  # noqa: E402
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from backends.n8n.workflow_compiler import N8nCompilerConfig, N8nWorkflowCompilerAdapter  # noqa: E402
from domain.agent_preparation import AgentCandidate  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402
from tests.planning_blueprint_v3.test_inbound_adapter import payload  # noqa: E402
from tests.p0_06.test_workflow_compiler import REQUEST_SCHEMA, RESULT_SCHEMA  # noqa: E402


@unittest.skipUnless(os.getenv("P0_BLUEPRINT_POSTGRES_DSN"), "set P0_BLUEPRINT_POSTGRES_DSN")
class PostgresProductionCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["P0_BLUEPRINT_POSTGRES_DSN"]
        self.schema = f"bpv3prod_{uuid4().hex[:10]}"
        self.repository = PostgresPlanningBlueprintRepository(self.dsn, schema=self.schema)
        self.assertEqual(PostgresMigrationRunner(
            self.repository._connect, ROOT / "orchestrator/migrations", self.schema
        ).migrate(), 9)
        self.registry = FakeAgentRegistryAdapter((AgentCandidate(
            "agent-a", "1.0.0", ("capability-agent-a",), "https://agents.example.test/a",
            REQUEST_SCHEMA, RESULT_SCHEMA, "credential-ref-a", {"mode": "request_response"},
        ),))
        self.runtime = FakeAgentRuntimeAdapter()
        self.engine = FakeWorkflowEngineAdapter()
        validation = ExecutionIRValidationService(self.repository)
        compilation = WorkflowCompilationService(
            validation,
            N8nWorkflowCompilerAdapter(N8nCompilerConfig(webhook_credential_ref="credential-ref-webhook")),
            self.repository,
        )
        self.self_test = SelfTestController(
            self.repository,
            (
                S0ValidationStage(validation), S1BindingStage(FakeAgentBindingProbe()),
                S2EngineStage(self.engine),
                S3CanaryArtifactStage(FakeCanaryProbe(), FakeArtifactProbe()),
                S4ObservabilityStage(FakeObservabilityProbe()),
            ),
        )
        self.release = WorkflowReleaseService(self.repository, self.engine)
        self.execution = ExecutionController(self.repository, self.release)
        generation = ProductionPlanningBlueprintGenerationService(
            self.repository, self.repository, self.registry, self.runtime,
            validation, compilation, self.engine, self.self_test, self.release,
            self.execution,
        )
        self.service = PlanningBlueprintIngestionService(self.repository, generation=generation)

    def tearDown(self) -> None:
        import psycopg2
        connection = psycopg2.connect(self.dsn)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        connection.close()

    def test_postgres_production_lineage_and_formal_execution(self) -> None:
        result = self.service.ingest(payload(), tenant_id="tenant-a")
        self.assertEqual(result.workflow_reference["stage"], "PUBLISHED")
        self.assertEqual(result.plan_version, 0)
        self.assertEqual(result.workflow_version, 1)
        generation = self.repository.get_generation(result.blueprint_id)
        self.assertEqual(generation.status, "PUBLISHED")
        view = self.service.generation.start_formal_execution(
            result.blueprint_id,
            tenant_id="tenant-a",
            client_request_id="request-pg-1",
            api_idempotency_key="api-pg-1",
            inputs={"request": "run"},
        )
        self.assertEqual(view.execution.status, "RUNNING")
        self.assertEqual(view.execution.correlation_id, "corr-v3")
        self.assertEqual(
            self.repository.get_generation(result.blueprint_id).execution_id,
            view.execution.execution_id,
        )
        replay = self.service.ingest(payload(), tenant_id="tenant-a")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.workflow_version, result.workflow_version)


if __name__ == "__main__":
    unittest.main()

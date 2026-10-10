from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_self_test import (  # noqa: E402
    FakeAgentBindingProbe,
    FakeArtifactProbe,
    FakeCanaryProbe,
    FakeObservabilityProbe,
)
from adapters.fake_workflow_engine import (  # noqa: E402
    FakeWorkflowCompilerAdapter,
    FakeWorkflowEngineAdapter,
)
from adapters.postgres_self_test import PostgresSelfTestRepository  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.self_test_controller import SelfTestController, StartSelfTestRequest  # noqa: E402
from application.self_test_stages import (  # noqa: E402
    S0ValidationStage,
    S1BindingStage,
    S2EngineStage,
    S3CanaryArtifactStage,
    S4ObservabilityStage,
)
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from domain.persistence import RegistrySnapshot, WorkflowFamily, WorkflowVersion  # noqa: E402
from domain.self_test import SelfTestRunResult, self_test_idempotent_id  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402
from tests.p0_06.test_workflow_compiler import _valid_fixture  # noqa: E402


@unittest.skipUnless(
    os.getenv("P0_07_POSTGRES_DSN"),
    "set P0_07_POSTGRES_DSN to run PostgreSQL Self-Test integration tests",
)
class PostgresSelfTestTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        self.dsn = os.environ["P0_07_POSTGRES_DSN"]
        self.schema = f"p007_{uuid4().hex[:12]}"
        self.repository = PostgresSelfTestRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(
            self.repository._connect,
            PROJECT_ROOT / "orchestrator" / "migrations",
            self.schema,
        )
        self.assertEqual(self.runner.migrate(), 9)

    def tearDown(self) -> None:
        connection = self.psycopg2.connect(self.dsn)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            connection.close()

    def _application(self):
        execution_ir, snapshot = _valid_fixture()
        snapshot_repository = InMemoryRegistrySnapshotRepository()
        snapshot_repository.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
        validation = ExecutionIRValidationService(snapshot_repository)
        workflow = FakeWorkflowCompilerAdapter().compile(execution_ir)
        self.repository.create_workflow_family(
            WorkflowFamily(execution_ir.workflow_family_id, "tenant-1", "task-1")
        )
        self.repository.create_registry_snapshot(
            RegistrySnapshot(
                snapshot.snapshot_id,
                execution_ir.workflow_family_id,
                snapshot.snapshot_hash,
                payload={"bindings": []},
            )
        )
        self.repository.create_workflow_version(
            WorkflowVersion(
                execution_ir.workflow_family_id,
                execution_ir.workflow_version,
                execution_ir.registry_snapshot_id,
                execution_ir.content_hash(),
                "DRAFT",
            )
        )
        engine = FakeWorkflowEngineAdapter()
        controller = SelfTestController(
            self.repository,
            (
                S0ValidationStage(validation),
                S1BindingStage(FakeAgentBindingProbe()),
                S2EngineStage(engine),
                S3CanaryArtifactStage(FakeCanaryProbe(), FakeArtifactProbe()),
                S4ObservabilityStage(FakeObservabilityProbe()),
            ),
        )
        request = StartSelfTestRequest(
            "tenant-1",
            execution_ir.workflow_family_id,
            execution_ir.workflow_version,
            execution_ir,
            workflow,
            "integration-self-test",
        )
        return execution_ir, workflow, engine, controller, request

    def test_migration_persistence_publish_execute_and_down(self) -> None:
        _ir, workflow, engine, controller, request = self._application()
        result = controller.start_self_test(request)
        self.assertEqual(result.status, "PASSED")
        loaded = self.repository.get_self_test(result.self_test_id)
        self.assertEqual(loaded, result)
        self.assertEqual(len(loaded.stage_results), 5)
        reference = next(iter(engine.workflows.values()))[1]
        release = WorkflowReleaseService(self.repository, engine)
        published = release.publish(
            request.workflow_family_id,
            request.workflow_version,
            workflow,
            reference,
            operation_key="integration-publish",
        )
        execution = release.execute(
            request.workflow_family_id,
            request.workflow_version,
            workflow,
            published,
            {"request": "integration"},
            operation_key="integration-execute",
        )
        self.assertEqual(execution.status, "RUNNING")

        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute("SELECT count(*) FROM self_test_stage_result")
                self.assertEqual(cursor.fetchone()[0], 5)
                cursor.execute("SELECT status FROM workflow_publish_record")
                self.assertEqual(cursor.fetchone()[0], "PUBLISHED")
                with self.assertRaises(Exception):
                    cursor.execute(
                        "INSERT INTO self_test_stage_result("
                        "self_test_id,stage,status,started_at,completed_at,duration_ms,evidence,evidence_refs) "
                        "VALUES (%s,'S0','FAIL',now(),now(),0,'{}','[]')",
                        (result.self_test_id,),
                    )
            connection.rollback()
        finally:
            connection.close()

        self.assertEqual(self.runner.migrate(direction="down"), 9)

    def test_concurrent_start_has_one_authoritative_run(self) -> None:
        execution_ir, workflow, _engine, _controller, _request = self._application()

        def pending(index: int) -> SelfTestRunResult:
            key = f"concurrent-{index}"
            return SelfTestRunResult(
                self_test_idempotent_id(
                    "tenant-1", "family-compiler", 1, workflow.workflow_hash, key
                ),
                "tenant-1",
                "family-compiler",
                1,
                execution_ir.registry_snapshot_id,
                execution_ir.content_hash(),
                workflow.workflow_hash,
                key,
                datetime.now(timezone.utc),
                "RUNNING",
                "S0",
                0,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda item: self.repository.begin_self_test(pending(item)), (1, 2)))
        self.assertEqual(sum(1 for _run, created in outcomes if created), 1)
        self.assertEqual(outcomes[0][0].self_test_id, outcomes[1][0].self_test_id)

    def test_stage_write_failure_rolls_back_completion_and_transition(self) -> None:
        _ir, _workflow, _engine, controller, request = self._application()
        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "CREATE FUNCTION reject_stage_result() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected stage failure'; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_stage_result BEFORE INSERT ON self_test_stage_result "
                    "FOR EACH ROW EXECUTE FUNCTION reject_stage_result()"
                )
            connection.commit()
            with self.assertRaisesRegex(Exception, "injected stage failure"):
                controller.start_self_test(request)
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute("SELECT status FROM self_test_run")
                self.assertEqual(cursor.fetchone()[0], "RUNNING")
                cursor.execute("SELECT status FROM workflow_version")
                self.assertEqual(cursor.fetchone()[0], "SELF_TESTING")
                cursor.execute("SELECT count(*) FROM self_test_stage_result")
                self.assertEqual(cursor.fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

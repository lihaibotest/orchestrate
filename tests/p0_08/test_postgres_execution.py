from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from adapters.postgres_execution import PostgresExecutionRepository  # noqa: E402
from application.execution_controller import (  # noqa: E402
    ExecutionController,
    StartExecutionCommand,
)
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
from domain.execution_control import TenantContext, runtime_definition_from_ir  # noqa: E402
from domain.persistence import WorkflowFamily, WorkflowVersion  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402
from tests.p0_06.test_workflow_compiler import _valid_fixture  # noqa: E402


@unittest.skipUnless(
    os.getenv("P0_08_POSTGRES_DSN"),
    "set P0_08_POSTGRES_DSN to run PostgreSQL Execution tests",
)
class PostgresExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        self.dsn = os.environ["P0_08_POSTGRES_DSN"]
        self.schema = f"p008_{uuid4().hex[:12]}"
        self.repository = PostgresExecutionRepository(self.dsn, schema=self.schema)
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

    def _published_controller(self):
        execution_ir, snapshot = _valid_fixture()
        snapshots = InMemoryRegistrySnapshotRepository()
        snapshots.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
        validation = ExecutionIRValidationService(snapshots)
        workflow = FakeWorkflowCompilerAdapter().compile(execution_ir)
        engine = FakeWorkflowEngineAdapter()
        self.repository.create_workflow_family(
            WorkflowFamily(execution_ir.workflow_family_id, "tenant-1", "task-1")
        )
        self.repository.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
        self.repository.create_workflow_version(
            WorkflowVersion(
                execution_ir.workflow_family_id,
                execution_ir.workflow_version,
                execution_ir.registry_snapshot_id,
                execution_ir.content_hash(),
                "DRAFT",
            )
        )
        self.repository.save_runtime_definition(runtime_definition_from_ir(execution_ir))
        self_test = SelfTestController(
            self.repository,
            (
                S0ValidationStage(validation),
                S1BindingStage(FakeAgentBindingProbe()),
                S2EngineStage(engine),
                S3CanaryArtifactStage(FakeCanaryProbe(), FakeArtifactProbe()),
                S4ObservabilityStage(FakeObservabilityProbe()),
            ),
        )
        self_test.start_self_test(
            StartSelfTestRequest(
                "tenant-1",
                execution_ir.workflow_family_id,
                execution_ir.workflow_version,
                execution_ir,
                workflow,
                "p008-self-test",
            )
        )
        imported = next(iter(engine.workflows.values()))[1]
        release = WorkflowReleaseService(self.repository, engine)
        release.publish(
            execution_ir.workflow_family_id,
            execution_ir.workflow_version,
            workflow,
            imported,
            operation_key="p008-publish",
        )
        return execution_ir, engine, ExecutionController(self.repository, release)

    @staticmethod
    def _command(request_id: str = "request-1") -> StartExecutionCommand:
        return StartExecutionCommand(
            TenantContext("tenant-1"),
            "family-compiler",
            1,
            request_id,
            f"api-{request_id}",
            {"request": "postgres"},
            "correlation-postgres",
        )

    def test_migration_formal_execution_persistence_and_down(self) -> None:
        _ir, engine, controller = self._published_controller()
        view = controller.start(self._command())
        loaded = controller.get(TenantContext("tenant-1"), view.execution.execution_id)
        self.assertEqual(loaded.execution.execution_mode, "PRODUCTION")
        self.assertEqual(loaded.execution.status, "RUNNING")
        self.assertIsNotNone(loaded.execution.engine_execution_id)
        self.assertEqual(len(engine.executions), 1)

        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "SELECT execution_mode,registry_snapshot_id,ir_hash,workflow_hash "
                    "FROM execution WHERE execution_id=%s",
                    (view.execution.execution_id,),
                )
                row = cursor.fetchone()
                self.assertEqual(row[0], "PRODUCTION")
                self.assertTrue(all(row[index] for index in range(1, 4)))
                with self.assertRaises(Exception):
                    cursor.execute(
                        "UPDATE execution SET workflow_version=2 WHERE execution_id=%s",
                        (view.execution.execution_id,),
                    )
            connection.rollback()
        finally:
            connection.close()

        self.assertEqual(self.runner.migrate(direction="down"), 9)

    def test_concurrent_execution_and_attempt_have_one_authoritative_fact(self) -> None:
        _ir, engine, controller = self._published_controller()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _item: controller.start(self._command()), (1, 2)))
        self.assertEqual(results[0].execution.execution_id, results[1].execution.execution_id)
        self.assertEqual(len(engine.executions), 1)

        execution_id = results[0].execution.execution_id
        controller.mark_node_ready(execution_id, "agent-a")

        def start_attempt(_item: int):
            try:
                return controller.start_next_attempt(execution_id, "agent-a")
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(start_attempt, (1, 2)))
        successful = [item for item in attempts if not isinstance(item, Exception)]
        self.assertEqual(len(successful), 1)
        persisted = self.repository.list_attempts(execution_id, "agent-a")
        self.assertEqual([(item.attempt_no, item.retry_count) for item in persisted], [(1, 0)])
        node = next(item for item in self.repository.list_nodes(execution_id) if item.node_id == "agent-a")
        self.assertEqual(node.current_attempt, 1)

    def test_attempt_and_history_failures_roll_back_atomically(self) -> None:
        _ir, _engine, controller = self._published_controller()
        execution_id = controller.start(self._command()).execution.execution_id
        controller.mark_node_ready(execution_id, "agent-a")
        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "CREATE FUNCTION reject_node_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected node update failure'; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_node_update BEFORE UPDATE ON node_execution "
                    "FOR EACH ROW EXECUTE FUNCTION reject_node_update()"
                )
            connection.commit()
            with self.assertRaisesRegex(Exception, "injected node update failure"):
                controller.start_next_attempt(execution_id, "agent-a")
            self.assertEqual(self.repository.list_attempts(execution_id, "agent-a"), ())
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute("DROP TRIGGER reject_node_update ON node_execution")
                cursor.execute("DROP FUNCTION reject_node_update()")
            connection.commit()

            controller.start_next_attempt(execution_id, "agent-a")
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "CREATE FUNCTION reject_history() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected history failure'; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_history BEFORE INSERT ON state_transition_history "
                    "FOR EACH ROW EXECUTE FUNCTION reject_history()"
                )
            connection.commit()
            with self.assertRaisesRegex(Exception, "injected history failure"):
                controller.record_technical_failure(
                    execution_id,
                    "agent-a",
                    error_code="EO-EXEC-001",
                    normalized_error_class="timeout",
                )
            attempt = self.repository.list_attempts(execution_id, "agent-a")[0]
            node = next(item for item in self.repository.list_nodes(execution_id) if item.node_id == "agent-a")
            self.assertEqual((attempt.status, attempt.retry_count, node.status), ("RUNNING", 0, "RUNNING"))
        finally:
            connection.close()

    def test_execution_create_and_engine_reference_failures_are_recoverable(self) -> None:
        _ir, engine, controller = self._published_controller()
        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "CREATE FUNCTION reject_node_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected node insert failure'; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_node_insert BEFORE INSERT ON node_execution "
                    "FOR EACH ROW EXECUTE FUNCTION reject_node_insert()"
                )
            connection.commit()
            with self.assertRaisesRegex(Exception, "injected node insert failure"):
                controller.start(self._command("create-rollback"))
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute("SELECT count(*) FROM execution")
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("DROP TRIGGER reject_node_insert ON node_execution")
                cursor.execute("DROP FUNCTION reject_node_insert()")
            connection.commit()

            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE FUNCTION reject_engine_ref() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN IF NEW.engine_execution_id IS NOT NULL THEN "
                    "RAISE EXCEPTION 'injected engine ref failure'; END IF; RETURN NEW; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_engine_ref BEFORE UPDATE ON execution "
                    "FOR EACH ROW EXECUTE FUNCTION reject_engine_ref()"
                )
            connection.commit()
            command = self._command("engine-ref-rollback")
            with self.assertRaisesRegex(Exception, "injected engine ref failure"):
                controller.start(command)
            self.assertEqual(len(engine.executions), 1)
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER reject_engine_ref ON execution")
                cursor.execute("DROP FUNCTION reject_engine_ref()")
            connection.commit()
            recovered = controller.start(command)
            self.assertEqual(recovered.execution.status, "RUNNING")
            self.assertEqual(len(engine.executions), 1)
        finally:
            connection.close()

    def test_execution_tenant_must_match_workflow_family(self) -> None:
        _ir, _engine, controller = self._published_controller()
        with self.assertRaises(ValueError):
            controller.start(
                StartExecutionCommand(
                    TenantContext("tenant-2"),
                    "family-compiler",
                    1,
                    "tenant-mismatch",
                    "api-tenant-mismatch",
                    {"request": "postgres"},
                    "correlation-tenant-mismatch",
                )
            )


if __name__ == "__main__":
    unittest.main()

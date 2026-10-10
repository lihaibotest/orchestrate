from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4


ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_artifact import FakeMinioClient  # noqa: E402
from adapters.fake_self_test import FakeAgentBindingProbe, FakeArtifactProbe, FakeCanaryProbe, FakeObservabilityProbe  # noqa: E402
from adapters.fake_workflow_engine import FakeWorkflowCompilerAdapter, FakeWorkflowEngineAdapter  # noqa: E402
from adapters.minio_artifact import MinioArtifactAdapter  # noqa: E402
from adapters.postgres_artifact import PostgresArtifactRepository  # noqa: E402
from application.execution_controller import ExecutionController, StartExecutionCommand  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.self_test_controller import SelfTestController, StartSelfTestRequest  # noqa: E402
from application.self_test_stages import S0ValidationStage, S1BindingStage, S2EngineStage, S3CanaryArtifactStage, S4ObservabilityStage  # noqa: E402
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from domain.artifact import ArtifactNotFound, ArtifactPutRequest, ExpectedArtifactOutput, ProducedArtifact  # noqa: E402
from domain.execution_control import TenantContext, runtime_definition_from_ir  # noqa: E402
from domain.persistence import WorkflowFamily, WorkflowVersion  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402
from tests.p0_06.test_workflow_compiler import _valid_fixture  # noqa: E402


@unittest.skipUnless(
    os.getenv("P0_09_POSTGRES_DSN"),
    "set P0_09_POSTGRES_DSN to run PostgreSQL Artifact tests",
)
class PostgresArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        self.dsn = os.environ["P0_09_POSTGRES_DSN"]
        self.schema = f"p009_{uuid4().hex[:12]}"
        self.repository = PostgresArtifactRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(
            self.repository._connect, ROOT / "orchestrator" / "migrations", self.schema
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

    def _controller(self):  # type: ignore[no-untyped-def]
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
                "p009-self-test",
            )
        )
        imported = next(iter(engine.workflows.values()))[1]
        release = WorkflowReleaseService(self.repository, engine)
        release.publish(
            execution_ir.workflow_family_id,
            execution_ir.workflow_version,
            workflow,
            imported,
            operation_key="p009-publish",
        )
        manager = MinioArtifactAdapter(
            FakeMinioClient(), self.repository, bucket="artifacts"
        )
        controller = ExecutionController(self.repository, release, manager)
        view = controller.start(
            StartExecutionCommand(
                TenantContext("tenant-1"),
                execution_ir.workflow_family_id,
                execution_ir.workflow_version,
                "artifact-postgres",
                "artifact-api-key",
                {"request": "run"},
                "artifact-correlation",
            )
        )
        controller.mark_node_ready(view.execution.execution_id, "agent-a")
        controller.start_next_attempt(view.execution.execution_id, "agent-a")
        return view.execution, controller, manager

    def test_persistence_constraints_indexes_tenant_and_attempt_lineage(self) -> None:
        execution, controller, manager = self._controller()
        expected = ExpectedArtifactOutput(
            "report", media_type="application/json", schema={"type": "object"}
        )

        def put(attempt: int, value: str):  # type: ignore[no-untyped-def]
            return manager.put_artifact(
                ArtifactPutRequest(
                    "tenant-1",
                    execution.workflow_family_id,
                    execution.workflow_version,
                    execution.execution_id,
                    execution.execution_mode,
                    "agent-a",
                    attempt,
                    ProducedArtifact(
                        "report",
                        (f'{{"value":"{value}"}}').encode(),
                        "application/json",
                    ),
                    expected,
                )
            )

        first = put(1, "one")
        self.assertTrue(
            manager.validate_artifact("tenant-1", first.artifact_id, expected).passed
        )
        with self.assertRaises(ArtifactNotFound):
            manager.get_artifact_metadata("tenant-2", first.artifact_id)
        controller.complete_attempt(
            execution.execution_id, "agent-a", complete_node=False
        )
        controller.start_next_attempt(execution.execution_id, "agent-a")
        second = put(2, "two")
        self.assertNotEqual(first.artifact_id, second.artifact_id)

        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "SELECT attempt_no,status,validation_status FROM artifact_ref "
                    "ORDER BY attempt_no"
                )
                self.assertEqual(cursor.fetchall(), [(1, "VALIDATED", "PASS"), (2, "CREATED", "PENDING")])
                cursor.execute("SELECT count(*) FROM artifact_validation")
                self.assertGreaterEqual(cursor.fetchone()[0], 6)
                cursor.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename='artifact_ref'",
                    (self.schema,),
                )
                indexes = {row[0] for row in cursor.fetchall()}
                self.assertIn("idx_artifact_ref_tenant_execution", indexes)
                with self.assertRaises(Exception):
                    cursor.execute(
                        "INSERT INTO artifact_ref(artifact_id,execution_id,node_id,attempt_no,uri,content_hash) "
                        "VALUES ('bad','missing','node',1,'s3://bad','hash')"
                    )
            connection.rollback()
        finally:
            connection.close()

    def test_0008_down_restores_legacy_artifact_shape(self) -> None:
        self.assertEqual(self.runner.migrate(direction="down"), 9)
        self.assertEqual(self.runner.migrate(direction="down"), 8)
        connection = self.psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute(
                    "SELECT to_regclass('artifact_validation'),"
                    "EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='artifact_ref' AND column_name='uri'),"
                    "EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='artifact_ref' AND column_name='storage_ref')",
                    (self.schema, self.schema),
                )
                self.assertEqual(cursor.fetchone(), (None, True, False))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

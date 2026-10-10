from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from uuid import uuid4


PROJECT_ROOT = Path(__file__).parents[2]
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from domain.persistence import (  # noqa: E402
    ArtifactRef,
    Checkpoint,
    ContextOutboxEvent,
    Execution,
    NodeAttempt,
    NodeExecution,
    RegistrySnapshot,
    RuntimeEvent,
    SelfTestRun,
    WorkflowFamily,
    WorkflowVersion,
)
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


class FakeCursor:
    description = None

    def execute(self, statement: str, parameters: object = None) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


class FailingConnection(FakeConnection):
    def cursor(self) -> FakeCursor:
        raise RuntimeError("database unavailable")


class PostgresPersistenceTests(unittest.TestCase):
    def test_migration_runner_rejects_invalid_direction_and_schema(self) -> None:
        migrations = PROJECT_ROOT / "orchestrator/migrations"
        with self.assertRaises(ValueError):
            PostgresMigrationRunner(lambda: FakeConnection(), migrations, "bad-schema-name")
        runner = PostgresMigrationRunner(lambda: FakeConnection(), migrations)
        with self.assertRaises(ValueError):
            runner.migrate(direction="sideways")

    def test_migration_runner_rolls_back_when_connection_fails(self) -> None:
        runner = PostgresMigrationRunner(lambda: FailingConnection(), PROJECT_ROOT / "orchestrator/migrations")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            runner.migrate()

    def test_migration_files_contain_required_facts_and_constraints(self) -> None:
        sql = (PROJECT_ROOT / "orchestrator/migrations/0001_initial.up.sql").read_text(encoding="utf-8")
        for table in (
            "workflow_family", "registry_snapshot", "workflow_version", "execution",
            "node_execution", "node_attempt", "artifact_ref", "checkpoint",
            "self_test_run", "runtime_event", "context_outbox", "state_transition_history",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("UNIQUE (tenant_id, workflow_family_id, workflow_version, client_request_id)", sql)
        self.assertIn("FOREIGN KEY (workflow_family_id, registry_snapshot_id)", sql)
        self.assertIn("reject_immutable_row_update", sql)
        self.assertIn("btrim(uri) !~* '^(data:|inline:)'", sql)

    def test_domain_models_require_explicit_execution_idempotency(self) -> None:
        with self.assertRaises(ValueError):
            PostgresControlPlaneRepository("")
        with self.assertRaisesRegex(ValueError, "schema"):
            PostgresControlPlaneRepository("postgresql://db", schema='bad"schema')
        execution = Execution(
            execution_id="exec-1", workflow_family_id="family-1", workflow_version=1,
            tenant_id="tenant-1", status="CREATED", client_request_id="request-1",
        )
        self.assertEqual(execution.client_request_id, "request-1")

    def test_down_migration_is_explicit_and_recoverable(self) -> None:
        down = (PROJECT_ROOT / "orchestrator/migrations/0001_initial.down.sql").read_text(encoding="utf-8")
        self.assertIn("DROP TABLE IF EXISTS workflow_family", down)
        self.assertIn("DROP TABLE IF EXISTS context_outbox", down)

    @unittest.skipUnless(
        os.getenv("P0_02_POSTGRES_DSN"),
        "set P0_02_POSTGRES_DSN to run PostgreSQL integration tests",
    )
    def test_postgres_migration_constraints_idempotency_and_rollback(self) -> None:
        import psycopg2

        dsn = os.environ["P0_02_POSTGRES_DSN"]
        schema = f"p002_{uuid4().hex[:12]}"
        repository = PostgresControlPlaneRepository(dsn, schema=schema)
        connection = psycopg2.connect(dsn)
        try:
            runner = PostgresMigrationRunner(repository._connect, PROJECT_ROOT / "orchestrator/migrations", schema)
            self.assertEqual(runner.migrate(), 9)
            self.assertTrue(repository.health_probe())
            family = WorkflowFamily("family-1", "tenant-1", "task-1")
            snapshot = RegistrySnapshot("snapshot-1", family.workflow_family_id, "hash-1", payload={"agents": []})
            version = WorkflowVersion("family-1", 1, "snapshot-1", "ir-1", "VERIFIED")
            repository.create_workflow_family(family)
            repository.create_registry_snapshot(snapshot)
            repository.create_workflow_version(version)
            execution = Execution("exec-1", "family-1", 1, "tenant-1", "CREATED", client_request_id="request-1")
            self.assertEqual(repository.create_execution(execution), execution)
            self.assertEqual(repository.create_execution(execution), execution)
            with self.assertRaises(Exception):
                repository.create_execution(Execution("other", "family-1", 1, "tenant-1", "CREATED", client_request_id="request-1"))

            node = NodeExecution("exec-1", "node-1", agent_id="agent-1")
            repository.create_node_execution(node)
            with self.assertRaises(Exception):
                repository.create_node_execution(NodeExecution("missing-execution", "node-1"))

            attempt = NodeAttempt("exec-1", "node-1", 1, "attempt-key-1")
            repository.create_attempt(attempt)
            with self.assertRaises(Exception):
                repository.create_attempt(NodeAttempt("exec-1", "node-1", 1, "attempt-key-2"))
            with self.assertRaises(Exception):
                repository.create_attempt(NodeAttempt("exec-1", "node-1", 2, "attempt-key-1"))

            artifact = ArtifactRef(
                "artifact-1", "exec-1", "node-1", 1,
                "s3://artifacts/result.json", "sha256:abc",
                schema_id="result", schema_version="1", reusable=True,
            )
            repository.register_artifact(artifact)
            with self.assertRaisesRegex(ValueError, "object storage"):
                repository.register_artifact(
                    ArtifactRef(
                        "artifact-inline", "exec-1", "node-1", 1,
                        "  DATA:application/json,{}", "sha256:inline",
                    )
                )
            repository.create_checkpoint(
                Checkpoint(
                    "checkpoint-1", "exec-1", "node-1",
                    artifact_refs=["artifact-1"], input_fingerprint="input-1", trusted=True,
                )
            )
            with self.assertRaises(Exception):
                repository.create_checkpoint(
                    Checkpoint("checkpoint-bad", "exec-1", "missing-node")
                )
            repository.record_self_test(
                SelfTestRun(
                    "self-test-1", "family-1", 1, 1, "S0", True,
                    evidence={"validator": "passed"},
                )
            )

            event = RuntimeEvent(
                "runtime-event-1", "exec-1", "execution.created", 1,
                datetime.now(timezone.utc), node_id="node-1", attempt_no=1,
            )
            self.assertTrue(repository.record_runtime_event(event))
            self.assertFalse(repository.record_runtime_event(event))
            with self.assertRaises(Exception):
                repository.record_runtime_event(
                    RuntimeEvent(
                        "runtime-event-bad", "exec-1", "execution.created", 2,
                        datetime.now(timezone.utc),
                    )
                )

            self.assertTrue(repository.enqueue_context_event(ContextOutboxEvent("event-1", "terminal", {"execution_id": "exec-1"}, "op-1")))
            self.assertFalse(repository.enqueue_context_event(ContextOutboxEvent("event-2", "terminal", {}, "op-1")))
            claimed = repository.claim_outbox()
            self.assertEqual(claimed[0]["event_id"], "event-1")
            with self.assertRaises(Exception):
                repository.create_execution(Execution("bad", "missing-family", 1, "tenant-1", "CREATED", client_request_id="request-bad"))

            other_family = WorkflowFamily("family-2", "tenant-2", "task-2")
            repository.create_workflow_family(other_family)
            repository.create_registry_snapshot(
                RegistrySnapshot("snapshot-2", "family-2", "hash-2", payload={"agents": []})
            )
            repository.create_registry_snapshot(
                RegistrySnapshot("snapshot-unbound", "family-2", "hash-unbound", payload={"agents": []})
            )
            repository.create_workflow_version(
                WorkflowVersion("family-2", 1, "snapshot-2", "ir-2", "DRAFT")
            )
            with self.assertRaises(Exception):
                repository.create_workflow_version(
                    WorkflowVersion("family-1", 2, "snapshot-2", "ir-cross-family", "DRAFT", parent_version=1)
                )
            third_family = WorkflowFamily("family-3", "tenant-3", "task-3")
            repository.create_workflow_family(third_family)
            repository.create_registry_snapshot(
                RegistrySnapshot("snapshot-3", "family-3", "hash-3", payload={"agents": []})
            )
            with self.assertRaises(Exception):
                repository.create_workflow_version(
                    WorkflowVersion("family-3", 2, "snapshot-3", "ir-3", "DRAFT", parent_version=1)
                )

            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute("SELECT count(*) FROM execution")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM node_execution")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM node_attempt")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM artifact_ref")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM checkpoint")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM self_test_run")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT count(*) FROM runtime_event")
                self.assertEqual(cursor.fetchone()[0], 1)
            connection.commit()

            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO artifact_ref(artifact_id,execution_id,node_id,attempt_no,uri,content_hash) "
                        "VALUES ('artifact-inline-db','exec-1','node-1',1,'  INLINE:body','sha256:inline-db')"
                    )
            connection.rollback()

            for statement in (
                "UPDATE registry_snapshot SET snapshot_hash='changed' WHERE snapshot_id='snapshot-1'",
                "DELETE FROM registry_snapshot WHERE snapshot_id='snapshot-unbound'",
                "UPDATE workflow_version SET ir_hash='changed' WHERE workflow_family_id='family-1' AND version=1",
                "DELETE FROM workflow_version WHERE workflow_family_id='family-2' AND version=1",
            ):
                with self.assertRaises(Exception):
                    with connection.cursor() as cursor:
                        cursor.execute(statement)
                connection.rollback()

            for version in range(9, 0, -1):
                self.assertEqual(runner.migrate(direction="down"), version)
        finally:
            connection.close()
            cleanup = psycopg2.connect(dsn)
            cleanup.autocommit = True
            with cleanup.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cleanup.close()

    @unittest.skipUnless(
        os.getenv("P0_02_POSTGRES_DSN"),
        "set P0_02_POSTGRES_DSN to run PostgreSQL integration tests",
    )
    def test_postgres_migration_checksum_and_generic_down_name(self) -> None:
        import psycopg2

        dsn = os.environ["P0_02_POSTGRES_DSN"]
        schema = f"p002_checksum_{uuid4().hex[:8]}"
        source = PROJECT_ROOT / "orchestrator/migrations"
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory)
            up = migrations / "0001_control_plane.up.sql"
            down = migrations / "0001_control_plane.down.sql"
            shutil.copyfile(source / "0001_initial.up.sql", up)
            shutil.copyfile(source / "0001_initial.down.sql", down)
            runner = PostgresMigrationRunner(lambda: psycopg2.connect(dsn), migrations, schema)
            try:
                self.assertEqual(runner.migrate(), 1)
                original = up.read_text(encoding="utf-8")
                up.write_text(original + "\n-- unexpected mutation\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    runner.migrate()
                up.write_text(original, encoding="utf-8")
                self.assertEqual(runner.migrate(direction="down"), 1)
            finally:
                cleanup = psycopg2.connect(dsn)
                cleanup.autocommit = True
                with cleanup.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                cleanup.close()


if __name__ == "__main__":
    unittest.main()

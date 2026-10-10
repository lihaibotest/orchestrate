from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from domain.persistence import Execution, RegistrySnapshot, WorkflowFamily, WorkflowVersion  # noqa: E402
from domain.runtime_events import RuntimeEvent, RuntimeObservation, observation_id  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


@unittest.skipUnless(os.getenv("P0_10_POSTGRES_DSN"), "set P0_10_POSTGRES_DSN")
class PostgresRuntimeEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["P0_10_POSTGRES_DSN"]
        self.schema = f"p010_{uuid4().hex[:12]}"
        self.repository = PostgresControlPlaneRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(
            self.repository._connect, ROOT / "orchestrator/migrations", self.schema
        )
        self.assertEqual(self.runner.migrate(), 9)
        self.repository.create_workflow_family(WorkflowFamily("family-1", "tenant-a", "task-1"))
        self.repository.create_registry_snapshot(
            RegistrySnapshot("snapshot-1", "family-1", "hash-1", payload={"agents": []})
        )
        self.repository.create_workflow_version(
            WorkflowVersion("family-1", 1, "snapshot-1", "ir-1", "PUBLISHED")
        )
        self.repository.create_execution(
            Execution(
                "execution-1", "family-1", 1, "tenant-a", "RUNNING",
                correlation_id="corr-1", client_request_id="request-1",
            )
        )

    def tearDown(self) -> None:
        import psycopg2

        connection = psycopg2.connect(self.dsn)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        connection.close()

    def test_event_observation_idempotency_ordering_indexes_and_down(self) -> None:
        now = datetime.now(timezone.utc)
        event = RuntimeEvent(
            "event-1", "execution-1", "node.completed", 1, now,
            node_id="node-1", attempt_no=1, payload={"status": "success"},
            source="WORKFLOW_ENGINE", tenant_id="tenant-a", correlation_id="corr-1",
            external_reference="n8n-exec-1", received_at=now,
            payload_hash="hash-event-1",
        )
        self.assertTrue(self.repository.record_runtime_event(event))
        self.assertFalse(self.repository.record_runtime_event(event))
        loaded = self.repository.get_event("tenant-a", "event-1")
        self.assertEqual(loaded.occurred_at, now)
        observation = RuntimeObservation(
            observation_id("event-1"), "event-1", "tenant-a", "corr-1", "execution-1",
            "ENGINE_NODE_SUCCEEDED", "WORKFLOW_ENGINE", now,
            node_id="node-1", attempt_no=1, external_reference="n8n-exec-1",
        )
        self.assertTrue(self.repository.save_observation(observation)[1])
        self.assertFalse(self.repository.save_observation(observation)[1])
        self.assertEqual(len(self.repository.list_observations("tenant-a", "execution-1")), 1)
        with self.assertRaises(KeyError):
            self.repository.get_event("tenant-b", "event-1")
        import psycopg2

        connection = psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                cursor.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename='runtime_event'", (self.schema,))
                indexes = {row[0] for row in cursor.fetchall()}
                self.assertIn("idx_runtime_event_tenant_execution_time", indexes)
        finally:
            connection.close()
        self.assertEqual(self.runner.migrate(direction="down"), 9)

    def test_concurrent_event_idempotency_has_one_authoritative_row(self) -> None:
        now = datetime.now(timezone.utc)

        def submit(_: int) -> bool:
            repository = PostgresControlPlaneRepository(self.dsn, schema=self.schema)
            event = RuntimeEvent(
                "concurrent-event", "execution-1", "node.completed", 1, now,
                node_id="node-1", attempt_no=1, payload={"status": "success"},
                source="WORKFLOW_ENGINE", tenant_id="tenant-a", correlation_id="corr-1",
                external_reference="n8n-concurrent", received_at=now,
                payload_hash="hash-concurrent",
            )
            return repository.save_event(event)[1]

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.repository.list_events("tenant-a", "execution-1")), 1)

    def test_runtime_event_transaction_rolls_back_on_invalid_observation(self) -> None:
        now = datetime.now(timezone.utc)
        event = RuntimeEvent(
            "rollback-event", "execution-1", "node.completed", 1, now,
            node_id="node-1", attempt_no=1, payload={}, source="WORKFLOW_ENGINE",
            tenant_id="tenant-a", correlation_id="corr-1", received_at=now,
            payload_hash="hash-rollback",
        )
        self.assertTrue(self.repository.record_runtime_event(event))
        invalid = RuntimeObservation(
            "observation-rollback", "rollback-event", "tenant-a", "corr-1",
            "execution-1", "ENGINE_NODE_SUCCEEDED", "WORKFLOW_ENGINE", now,
            node_id="node-1", attempt_no=1,
        )
        self.assertTrue(self.repository.save_observation(invalid)[1])
        with self.assertRaises(Exception):
            self.repository.save_observation(invalid.__class__(
                "observation-rollback-2", "missing-event", "tenant-a", "corr-1",
                "execution-1", "ENGINE_NODE_SUCCEEDED", "WORKFLOW_ENGINE", now,
                node_id="node-1", attempt_no=1,
            ))
        self.assertEqual(len(self.repository.list_observations("tenant-a", "execution-1")), 1)


if __name__ == "__main__":
    unittest.main()

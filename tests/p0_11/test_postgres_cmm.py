from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from domain.context_event import ContextEvent, ContextEventType  # noqa: E402
from domain.persistence import Execution, RegistrySnapshot, WorkflowFamily, WorkflowVersion  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


@unittest.skipUnless(os.getenv("P0_11_POSTGRES_DSN"), "set P0_11_POSTGRES_DSN")
class PostgresCMMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["P0_11_POSTGRES_DSN"]
        self.schema = f"p011_{uuid4().hex[:12]}"
        self.repository = PostgresControlPlaneRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(self.repository._connect, ROOT / "orchestrator/migrations", self.schema)
        self.assertEqual(self.runner.migrate(), 10)
        self.repository.create_workflow_family(WorkflowFamily("family-1", "tenant-a", "task-1"))
        self.repository.create_registry_snapshot(RegistrySnapshot("snapshot-1", "family-1", "hash-1", payload={"agents": []}))
        self.repository.create_workflow_version(WorkflowVersion("family-1", 1, "snapshot-1", "ir-1", "PUBLISHED"))
        self.repository.create_execution(Execution("execution-1", "family-1", 1, "tenant-a", "RUNNING", client_request_id="request-1", correlation_id="corr-1"))

    def tearDown(self) -> None:
        import psycopg2
        connection = psycopg2.connect(self.dsn)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        connection.close()

    def event(self, event_id: str) -> ContextEvent:
        return ContextEvent.create(
            event_type=ContextEventType.EXECUTION_STARTED,
            tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
            workflow_version=1, execution_id="execution-1", payload={"status": "RUNNING"},
            event_id=event_id,
        )

    def test_migration_up_down_and_concurrent_idempotency(self) -> None:
        from adapters.postgres_cmm import PostgresContextOutboxRepository

        def enqueue(_: int) -> bool:
            repository = PostgresContextOutboxRepository(
                PostgresControlPlaneRepository(self.dsn, schema=self.schema)
            )
            return repository.enqueue(self.event("context-event-1"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(enqueue, range(16)))
        self.assertEqual(sum(results), 1)
        repository = PostgresContextOutboxRepository(self.repository)
        self.assertEqual(len(repository.claim(limit=10)), 1)
        self.assertEqual(self.runner.migrate(direction="down"), 10)

    def test_transaction_rollback_keeps_outbox_empty(self) -> None:
        from adapters.postgres_cmm import PostgresContextOutboxRepository
        repository = PostgresContextOutboxRepository(self.repository)
        with self.assertRaises(Exception):
            repository.enqueue(self.event("missing-event"), force_failure=True)
        self.assertEqual(repository.pending_count(), 0)

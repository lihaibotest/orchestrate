from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

PROJECT_ROOT = Path(__file__).parents[2]
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.postgres_planning_blueprint import PostgresPlanningBlueprintRepository  # noqa: E402
from application.planning_blueprint_ingestion_service import PlanningBlueprintIngestionService  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402
from tests.planning_blueprint_v3.test_inbound_adapter import payload  # noqa: E402


@unittest.skipUnless(os.getenv("P0_BLUEPRINT_POSTGRES_DSN"), "set P0_BLUEPRINT_POSTGRES_DSN")
class PostgresBlueprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["P0_BLUEPRINT_POSTGRES_DSN"]
        self.schema = f"bpv3_{uuid4().hex[:10]}"
        self.repository = PostgresPlanningBlueprintRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(self.repository._connect, PROJECT_ROOT / "orchestrator/migrations", self.schema)
        self.assertEqual(self.runner.migrate(), 9)

    def tearDown(self) -> None:
        import psycopg2
        connection = psycopg2.connect(self.dsn)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        connection.close()

    def test_persist_replay_conflict_and_immutable_payload(self) -> None:
        service = PlanningBlueprintIngestionService(self.repository)
        first = service.ingest(payload(), tenant_id="tenant-a")
        replay = service.ingest(payload(), tenant_id="tenant-a")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.blueprint_id, replay.blueprint_id)
        changed = payload()
        changed["inputs"] = {"request": "different"}
        with self.assertRaises(Exception):
            service.ingest(changed, tenant_id="tenant-a")
        import psycopg2
        connection = psycopg2.connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
                with self.assertRaises(Exception):
                    cursor.execute("UPDATE planning_blueprint_ingestion SET payload_json='{}'::jsonb")
                connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

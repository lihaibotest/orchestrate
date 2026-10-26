from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


@unittest.skipUnless(os.getenv("P0_12_POSTGRES_DSN"), "set P0_12_POSTGRES_DSN")
class P012PostgresMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connected = False
        self.dsn = os.environ["P0_12_POSTGRES_DSN"]
        self.schema = f"p012_{uuid4().hex[:12]}"
        self.repository = PostgresControlPlaneRepository(self.dsn, schema=self.schema)
        self.runner = PostgresMigrationRunner(
            self.repository._connect, ROOT / "orchestrator/migrations", self.schema
        )

    def tearDown(self) -> None:
        if not self.connected:
            return
        import psycopg2

        connection = psycopg2.connect(self.dsn)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            connection.close()

    def test_full_migration_up_down_isolated_schema(self) -> None:
        previous = os.environ.get("ORCHESTRATOR_ENABLE_P0_12")
        os.environ["ORCHESTRATOR_ENABLE_P0_12"] = "1"
        try:
            self.assertEqual(self.runner.migrate(), 11)
            self.connected = True
            self.assertEqual(self.runner.applied_versions(), list(range(1, 12)))
            self.assertEqual(self.runner.migrate(direction="down"), 11)
            self.assertEqual(self.runner.applied_versions(), list(range(1, 11)))
        finally:
            if previous is None:
                os.environ.pop("ORCHESTRATOR_ENABLE_P0_12", None)
            else:
                os.environ["ORCHESTRATOR_ENABLE_P0_12"] = previous

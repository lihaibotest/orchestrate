#!/usr/bin/env python3
"""Apply or roll back the PostgreSQL control-plane migration set."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("direction", choices=("up", "down"), nargs="?", default="up")
    parser.add_argument("--dsn", default=os.getenv("ORCHESTRATOR_POSTGRES_DSN", ""))
    parser.add_argument("--schema", default=os.getenv("ORCHESTRATOR_POSTGRES_SCHEMA", "orchestrator"))
    arguments = parser.parse_args()
    if not arguments.dsn:
        parser.error("--dsn or ORCHESTRATOR_POSTGRES_DSN is required")
    repository = PostgresControlPlaneRepository(
        arguments.dsn,
        schema=arguments.schema,
        migrations_dir=ROOT / "orchestrator" / "migrations",
    )
    print(repository.migrate(direction=arguments.direction))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

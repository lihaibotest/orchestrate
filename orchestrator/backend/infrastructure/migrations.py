from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Callable


MigrationConnection = Any
ConnectionFactory = Callable[[], MigrationConnection]
_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UP_MIGRATION = re.compile(r"^(?P<version>[0-9]+)_(?P<name>.+)\.up\.sql$")


def validate_schema_name(schema: str) -> str:
    if not _SAFE_SCHEMA.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    return schema


class PostgresMigrationRunner:
    """Minimal transactional migration runner with explicit up/down support."""

    def __init__(self, connection_factory: ConnectionFactory, migrations_dir: Path, schema: str = "orchestrator") -> None:
        validate_schema_name(schema)
        self.connection_factory = connection_factory
        self.migrations_dir = migrations_dir
        self.schema = schema

    def _connection(self) -> MigrationConnection:
        connection = self.connection_factory()
        cursor = connection.cursor()
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        cursor.execute(f'SET search_path TO "{self.schema}"')
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version integer primary key, checksum text not null, applied_at timestamptz not null default now())"
        )
        cursor.execute(
            "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text"
        )
        cursor.execute(
            "UPDATE schema_migrations SET checksum='legacy' WHERE checksum IS NULL"
        )
        cursor.execute(
            "ALTER TABLE schema_migrations ALTER COLUMN checksum SET NOT NULL"
        )
        cursor.close()
        return connection

    def applied_migrations(
        self, connection: MigrationConnection | None = None
    ) -> dict[int, str]:
        owns = connection is None
        connection = connection or self._connection()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            values = {int(row[0]): str(row[1]) for row in cursor.fetchall()}
            cursor.close()
            if owns:
                connection.commit()
            return values
        finally:
            if owns:
                connection.close()

    def applied_versions(self, connection: MigrationConnection | None = None) -> list[int]:
        return list(self.applied_migrations(connection))

    def _migration_files(self) -> dict[int, Path]:
        migrations: dict[int, Path] = {}
        for path in sorted(self.migrations_dir.glob("*.up.sql")):
            match = _UP_MIGRATION.fullmatch(path.name)
            if match is None:
                raise ValueError(f"invalid migration filename: {path.name}")
            version = int(match.group("version"))
            if version in migrations:
                raise ValueError(f"duplicate migration version: {version}")
            migrations[version] = path
        return migrations

    @staticmethod
    def _checksum(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _verify_applied(
        self, applied: dict[int, str], migrations: dict[int, Path]
    ) -> None:
        for version, recorded_checksum in applied.items():
            path = migrations.get(version)
            if path is None:
                raise RuntimeError(
                    f"applied migration {version} has no matching up file"
                )
            checksum = self._checksum(path)
            if recorded_checksum != checksum:
                raise RuntimeError(
                    f"checksum mismatch for applied migration {path.name}"
                )

    def migrate(self, *, direction: str = "up") -> int:
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        connection = self._connection()
        try:
            applied_migrations = self.applied_migrations(connection)
            migrations = self._migration_files()
            self._verify_applied(applied_migrations, migrations)
            applied = list(applied_migrations)
            if direction == "up":
                pending = [
                    (version, path)
                    for version, path in migrations.items()
                    if version not in applied_migrations
                ]
                for version, path in pending:
                    cursor = connection.cursor()
                    contents = path.read_text(encoding="utf-8")
                    cursor.execute(contents)
                    checksum = self._checksum(path)
                    cursor.execute("INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)", (version, checksum))
                    cursor.close()
                connection.commit()
                return pending[-1][0] if pending else (applied[-1] if applied else 0)
            if not applied:
                connection.commit()
                return 0
            version = applied[-1]
            up = migrations[version]
            down = up.with_name(up.name.removesuffix(".up.sql") + ".down.sql")
            if not down.is_file():
                raise FileNotFoundError(down)
            cursor = connection.cursor()
            cursor.execute(down.read_text(encoding="utf-8"))
            cursor.execute("DELETE FROM schema_migrations WHERE version=%s", (version,))
            cursor.close()
            connection.commit()
            return version
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

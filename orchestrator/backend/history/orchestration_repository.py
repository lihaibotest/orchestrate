from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrchestrationRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._callback_locks = tuple(threading.Lock() for _index in range(64))
        self._claimed_callback_ids: set[str] = set()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def probe(self) -> bool:
        """Check that the database can be opened and its migration state read."""
        try:
            with self.connection() as connection:
                result = connection.execute("SELECT 1").fetchone()
                migrations = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                if result is None or int(result[0]) != 1 or migrations is None:
                    return False
            return True
        except (OSError, sqlite3.Error):
            return False

    def _migrate(self) -> None:
        existed = self.database_path.exists()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current_version = self._current_schema_version()
        for version in range(current_version + 1, 11):
            backup = self.database_path.with_name(f"{self.database_path.name}.pre-migration-v{version}.bak")
            if existed or current_version > 0:
                self._backup_database(backup)
            try:
                with self.connection() as connection:
                    self._apply_migration(connection, version)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, utc_now()),
                    )
                self._assert_integrity(self.database_path)
            except Exception:
                if backup.exists():
                    self._restore_database(backup)
                raise
            existed = True
            current_version = version

    def _current_schema_version(self) -> int:
        if not self.database_path.exists():
            return 0
        with sqlite3.connect(self.database_path) as connection:
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not has_migrations:
                return 0
            return int(
                connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            )

    def _backup_database(self, backup: Path) -> None:
        backup.unlink(missing_ok=True)
        with sqlite3.connect(self.database_path) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        self._assert_integrity(backup)

    def _restore_database(self, backup: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
        with sqlite3.connect(backup) as source, sqlite3.connect(self.database_path) as target:
            source.backup(target)
        self._assert_integrity(self.database_path)

    @staticmethod
    def _assert_integrity(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")

    @staticmethod
    def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
        """Apply one ordered migration before recording its version."""
        statements: dict[int, tuple[str, ...]] = {
            1: (
                "CREATE TABLE IF NOT EXISTS nonce_ledger (caller TEXT NOT NULL, nonce TEXT NOT NULL, request_timestamp INTEGER NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY (caller, nonce))",
                "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, plan_version INTEGER NOT NULL, workflow_version INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, blueprint_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS callbacks (callback_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), callback_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS blueprint_versions (session_id TEXT NOT NULL REFERENCES sessions(session_id), plan_version INTEGER NOT NULL, blueprint_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (session_id, plan_version))",
                "CREATE TABLE IF NOT EXISTS workflow_versions (session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, workflow_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (session_id, workflow_version))",
                "CREATE TABLE IF NOT EXISTS generation_attempts (session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (session_id, workflow_version, attempt))",
                "CREATE TABLE IF NOT EXISTS registry_snapshots (session_id TEXT NOT NULL REFERENCES sessions(session_id), snapshot_version INTEGER NOT NULL, candidates_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (session_id, snapshot_version))",
                "CREATE TABLE IF NOT EXISTS evaluations (evaluation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS directives (directive_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, directive TEXT NOT NULL, patch_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            ),
            2: (
                "CREATE TABLE IF NOT EXISTS orchestration_events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), payload_json TEXT NOT NULL, status TEXT NOT NULL, lease_owner TEXT, lease_until REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS checkpoints (session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), checkpoint_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS outbox (outbox_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)",
            ),
            3: (
                "CREATE TABLE IF NOT EXISTS failure_diagnoses (diagnosis_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, node_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, error_class TEXT NOT NULL, error_code TEXT NOT NULL, diagnosis_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS node_attempts (session_id TEXT NOT NULL REFERENCES sessions(session_id), workflow_version INTEGER NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (session_id, workflow_version, node_id, attempt))",
            ),
            4: (
                "CREATE TABLE planning_jobs (job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, blueprint_hash TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', lease_owner TEXT, lease_until REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE execution_plans (plan_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, plan_version INTEGER NOT NULL, workflow_version INTEGER NOT NULL, blueprint_hash TEXT NOT NULL, snapshot_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL, ir_hash TEXT NOT NULL, ir_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, workflow_version))",
                "CREATE TABLE binding_decisions (plan_id TEXT NOT NULL REFERENCES execution_plans(plan_id), task_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL, agent_id TEXT NOT NULL, decision_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(plan_id, task_id))",
                "CREATE TABLE validation_reports (plan_id TEXT NOT NULL REFERENCES execution_plans(plan_id), dimension TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(plan_id, dimension))",
            ),
            5: (
                "CREATE TABLE backend_executions (execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, plan_version INTEGER NOT NULL, workflow_version INTEGER NOT NULL, backend TEXT NOT NULL, backend_workflow_id TEXT NOT NULL, backend_execution_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, plan_version, workflow_version, backend_workflow_id))",
                "CREATE TABLE publication_attempts (publication_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, operation_result_json TEXT NOT NULL DEFAULT '{}', attempt INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            ),
            6: (
                "CREATE TABLE runtime_decisions (decision_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, node_id TEXT NOT NULL, attempt INTEGER NOT NULL, action TEXT NOT NULL, reason_code TEXT NOT NULL, decision_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE budget_ledger (execution_id TEXT PRIMARY KEY, max_calls INTEGER NOT NULL, max_cost_microusd INTEGER NOT NULL, max_duration_seconds INTEGER NOT NULL, reserved_calls INTEGER NOT NULL DEFAULT 0, reserved_cost_microusd INTEGER NOT NULL DEFAULT 0, reserved_duration_seconds INTEGER NOT NULL DEFAULT 0, committed_calls INTEGER NOT NULL DEFAULT 0, committed_cost_microusd INTEGER NOT NULL DEFAULT 0, committed_duration_seconds INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
                "CREATE TABLE budget_reservations (reservation_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES budget_ledger(execution_id), node_id TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','committed','released','expired')), authorized_calls INTEGER NOT NULL, authorized_cost_microusd INTEGER NOT NULL, authorized_duration_seconds INTEGER NOT NULL, actual_calls INTEGER, actual_cost_microusd INTEGER, actual_duration_seconds INTEGER, lease_until REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(execution_id, node_id, attempt))",
                "CREATE TABLE progress_samples (sample_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, node_id TEXT NOT NULL, metric TEXT NOT NULL, value_json TEXT NOT NULL, sequence INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(execution_id, node_id, metric, sequence))",
            ),
            7: (
                "CREATE TABLE publication_sagas (job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), plan_version INTEGER NOT NULL, workflow_version INTEGER NOT NULL, artifact_json TEXT NOT NULL, start_payload_json TEXT NOT NULL, canary INTEGER NOT NULL, state TEXT NOT NULL, workflow_id TEXT, execution_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            ),
            8: (
                "CREATE TABLE workflow_repair_state (session_id TEXT NOT NULL REFERENCES sessions(session_id), lineage_id TEXT NOT NULL, max_attempts INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, escalated INTEGER NOT NULL DEFAULT 0, escalation_operation_key TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(session_id, lineage_id))",
                "CREATE TABLE workflow_repair_events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), lineage_id TEXT NOT NULL, action TEXT NOT NULL, repair_attempt INTEGER NOT NULL, created_at TEXT NOT NULL)",
            ),
            9: (
                "CREATE TABLE workflow_repair_resumes (operation_key TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), lineage_id TEXT NOT NULL, parent_execution_id TEXT NOT NULL UNIQUE REFERENCES backend_executions(execution_id), parent_workflow_version INTEGER NOT NULL, repaired_workflow_version INTEGER NOT NULL, checkpoint_json TEXT NOT NULL, start_payload_json TEXT NOT NULL, status TEXT NOT NULL, repaired_execution_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id,lineage_id,repaired_workflow_version))",
            ),
            10: (
                "CREATE TABLE workflow_terminal_events (execution_id TEXT PRIMARY KEY REFERENCES backend_executions(execution_id), event_id TEXT NOT NULL UNIQUE, terminal_status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            ),
        }
        if version == 3:
            # Historical v2 fixtures recorded migrations before all compatibility
            # tables existed. Reconcile those missing tables while applying v3.
            for prior_version in (1, 2):
                for statement in statements[prior_version]:
                    connection.execute(statement)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
            if "workflow_version" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN workflow_version INTEGER NOT NULL DEFAULT 0")
        for statement in statements.get(version, ()):
            connection.execute(statement)
        if version == 5:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)")}
            additions = {
                "attempt": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "next_attempt_at": "REAL",
                "operation_key": "TEXT",
                "operation_result_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE outbox ADD COLUMN {name} {declaration}")
        if version == 7:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(execution_plans)")}
            additions = {
                "workflow_spec_json": "TEXT NOT NULL DEFAULT '{}'",
                "compiled_artifact_json": "TEXT NOT NULL DEFAULT '{}'",
                "registry_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                "binding_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "monitoring_mode": "TEXT NOT NULL DEFAULT 'planner_final_summary_fallback'",
                "monitoring_node_count": "INTEGER NOT NULL DEFAULT 0",
                "final_summary_operation_key": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE execution_plans ADD COLUMN {name} {declaration}")

    def claim_nonce(self, *, caller: str, nonce: str, timestamp: int) -> bool:
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO nonce_ledger(caller, nonce, request_timestamp, received_at) VALUES (?, ?, ?, ?)",
                    (caller, nonce, timestamp, utc_now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def create_session(self, payload: dict[str, object]) -> dict[str, object]:
        with self.connection() as connection:
            existing = connection.execute("SELECT * FROM sessions WHERE session_id=?", (payload["session_id"],)).fetchone()
            blueprint_json = json.dumps(payload["blueprint"], sort_keys=True)
            if existing is not None:
                if any(existing[field] != payload[field] for field in ("correlation_id", "plan_version", "workflow_version")) or existing["blueprint_json"] != blueprint_json:
                    raise ValueError("session identity is immutable")
                return dict(existing)
            connection.execute(
                "INSERT INTO sessions(session_id, correlation_id, plan_version, workflow_version, status, blueprint_json, updated_at) VALUES (?, ?, ?, ?, 'blueprint_received', ?, ?)",
                (payload["session_id"], payload["correlation_id"], payload["plan_version"], payload["workflow_version"], blueprint_json, utc_now()),
            )
        return self.get_session(str(payload["session_id"])) or {}

    def get_session(self, session_id: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def record_callback(self, *, callback_id: str, callback_type: str, payload: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO callbacks(callback_id, session_id, callback_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    callback_id,
                    payload["session_id"],
                    callback_type,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                ),
            )

    def claim_callback(self, callback_id: str, session_id: str, callback_type: str, payload: dict[str, object]) -> bool:
        lock_index = hashlib.sha256(callback_id.encode("utf-8")).digest()[0] % len(self._callback_locks)
        with self._callback_locks[lock_index]:
            if callback_id in self._claimed_callback_ids:
                return False
            read_connection = sqlite3.connect(self.database_path, timeout=5)
            try:
                if read_connection.execute(
                    "SELECT 1 FROM callbacks WHERE callback_id=?", (callback_id,)
                ).fetchone() is not None:
                    self._claimed_callback_ids.add(callback_id)
                    return False
            finally:
                read_connection.close()
            with self.connection() as connection:
                changed = connection.execute(
                    "INSERT OR IGNORE INTO callbacks(callback_id, session_id, callback_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (callback_id, session_id, callback_type, json.dumps(payload, sort_keys=True), utc_now()),
                ).rowcount
            if changed:
                self._claimed_callback_ids.add(callback_id)
            return bool(changed)

    def record_blueprint_version(self, session_id: str, plan_version: int, blueprint: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO blueprint_versions VALUES (?, ?, ?, ?)",
                (session_id, plan_version, json.dumps(blueprint, sort_keys=True), utc_now()),
            )

    def record_workflow_version(self, session_id: str, workflow_version: int, workflow: dict[str, object], *, status: str) -> None:
        with self.connection() as connection:
            workflow_json = json.dumps(workflow, sort_keys=True)
            existing = connection.execute("SELECT workflow_json FROM workflow_versions WHERE session_id=? AND workflow_version=?", (session_id, workflow_version)).fetchone()
            if existing is not None:
                if existing[0] != workflow_json:
                    raise ValueError("workflow version is immutable")
                return
            connection.execute("INSERT INTO workflow_versions VALUES (?, ?, ?, ?, ?)", (session_id, workflow_version, workflow_json, status, utc_now()))

    def set_session_workflow_version(self, session_id: str, workflow_version: int, *, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE sessions SET workflow_version=?, status=?, updated_at=? WHERE session_id=?",
                (workflow_version, status, utc_now(), session_id),
            )

    def callback_exists(self, callback_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1 FROM callbacks WHERE callback_id=?", (callback_id,)).fetchone() is not None

    def record_generation_attempt(self, session_id: str, workflow_version: int, attempt: int, status: str, metadata: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO generation_attempts VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, workflow_version, attempt, status, json.dumps(metadata, sort_keys=True), utc_now()),
            )

    def record_registry_snapshot(self, session_id: str, snapshot_version: int, candidates: list[dict[str, object]]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO registry_snapshots VALUES (?, ?, ?, ?)",
                (session_id, snapshot_version, json.dumps(candidates, sort_keys=True), utc_now()),
            )

    def record_evaluation(self, evaluation_id: str, session_id: str, workflow_version: int, result: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO evaluations VALUES (?, ?, ?, ?, ?)",
                (evaluation_id, session_id, workflow_version, json.dumps(result, sort_keys=True), utc_now()),
            )

    def record_node_attempt(self, *, session_id: str, workflow_version: int, node_id: str, attempt: int, idempotency_key: str, status: str, metadata: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO node_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, workflow_version, node_id, attempt, idempotency_key, status, json.dumps(metadata, sort_keys=True), utc_now()),
            )

    def record_failure_diagnosis(self, *, diagnosis_id: str, session_id: str, workflow_version: int, node_id: str, idempotency_key: str, error_class: str, error_code: str, diagnosis: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO failure_diagnoses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (diagnosis_id, session_id, workflow_version, node_id, idempotency_key, error_class, error_code, json.dumps(diagnosis, sort_keys=True), utc_now()),
            )

    def record_directive(self, directive_id: str, session_id: str, workflow_version: int, directive: str, patch: dict[str, object]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO directives VALUES (?, ?, ?, ?, ?, ?)",
                (directive_id, session_id, workflow_version, directive, json.dumps(patch, sort_keys=True), utc_now()),
            )

    def record_directive_with_outbox(
        self,
        *,
        directive_id: str,
        session_id: str,
        workflow_version: int,
        directive: str,
        patch: dict[str, object],
        outbox_id: str,
        payload: dict[str, object],
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO directives VALUES (?, ?, ?, ?, ?, ?)",
                (directive_id, session_id, workflow_version, directive, json.dumps(patch, sort_keys=True), utc_now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(outbox_id, session_id, kind, payload_json, status, created_at, operation_key) VALUES (?, ?, 'n8n.resume', ?, 'pending', ?, ?)",
                (outbox_id, session_id, json.dumps(payload, sort_keys=True), utc_now(), outbox_id),
            )

    def record_outbox(
        self,
        *,
        outbox_id: str,
        session_id: str,
        kind: str,
        payload: dict[str, object],
        operation_key: str | None = None,
    ) -> bool:
        resolved_operation_key = operation_key or outbox_id
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM outbox WHERE operation_key=?", (resolved_operation_key,)
            ).fetchone() is not None:
                return False
            changed = connection.execute(
                "INSERT OR IGNORE INTO outbox(outbox_id, session_id, kind, payload_json, status, created_at, operation_key) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (outbox_id, session_id, kind, json.dumps(payload, sort_keys=True), utc_now(), resolved_operation_key),
            ).rowcount
        return bool(changed)

    def receive_event(self, event_id: str, session_id: str, payload: dict[str, object]) -> bool:
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO orchestration_events(event_id, session_id, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, 'received', ?, ?)",
                    (event_id, session_id, json.dumps(payload, sort_keys=True), utc_now(), utc_now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def lease_event(self, event_id: str, *, owner: str, lease_seconds: int) -> bool:
        import time
        with self.connection() as connection:
            changed = connection.execute(
                "UPDATE orchestration_events SET status='processing', lease_owner=?, lease_until=?, updated_at=? WHERE event_id=? AND status='received'",
                (owner, time.time() + lease_seconds, utc_now(), event_id),
            ).rowcount
        return bool(changed)

    def apply_event_transaction(self, *, event_id: str, checkpoint: dict[str, object], directive: dict[str, object], outbox: dict[str, object]) -> None:
        with self.connection() as connection:
            event = connection.execute("SELECT session_id FROM orchestration_events WHERE event_id=?", (event_id,)).fetchone()
            if event is None:
                raise KeyError(event_id)
            session_id = event["session_id"]
            connection.execute(
                "INSERT OR REPLACE INTO checkpoints(session_id, checkpoint_json, updated_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(checkpoint, sort_keys=True), utc_now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO directives(directive_id, session_id, workflow_version, directive, patch_json, created_at) VALUES (?, ?, 0, ?, ?, ?)",
                (directive["directive_id"], session_id, directive["directive"], json.dumps(directive.get("patch", {}), sort_keys=True), utc_now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(outbox_id, session_id, kind, payload_json, status, created_at, operation_key) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (outbox["outbox_id"], session_id, outbox["kind"], json.dumps(outbox["payload"], sort_keys=True), utc_now(), outbox["outbox_id"]),
            )
            connection.execute("UPDATE orchestration_events SET status='processed', updated_at=? WHERE event_id=?", (utc_now(), event_id))

    def event_state(self, event_id: str) -> dict[str, object]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM orchestration_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return dict(row)

    def pending_outbox(
        self, *, exclude_kinds: set[str] | None = None
    ) -> list[dict[str, object]]:
        excluded = sorted(exclude_kinds or set())
        query = "SELECT * FROM outbox WHERE status='pending'"
        parameters: tuple[object, ...] = ()
        if excluded:
            placeholders = ",".join("?" for _kind in excluded)
            query += f" AND kind NOT IN ({placeholders})"
            parameters = tuple(excluded)
        query += " ORDER BY created_at"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def mark_outbox_sent(self, outbox_id: str) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE outbox SET status='sent' WHERE outbox_id=?", (outbox_id,))

    def mark_outbox_failed(self, outbox_id: str, error: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE outbox SET status='pending',attempt=attempt+1,last_error=?,next_attempt_at=NULL WHERE outbox_id=?",
                (error[:2048], outbox_id),
            )

    def recover_incomplete_events(self) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute("SELECT event_id FROM orchestration_events WHERE status IN ('received', 'processing')").fetchall()
            ids = [row[0] for row in rows]
            connection.execute("UPDATE orchestration_events SET status='received', lease_owner=NULL, lease_until=NULL, updated_at=? WHERE status IN ('received','processing')", (utc_now(),))
        return ids

    def prune_ephemeral_records(
        self,
        *,
        nonce_before_timestamp: int,
        sent_outbox_before: str,
        limit: int = 100,
    ) -> dict[str, int]:
        if limit < 1 or limit > 1000:
            raise ValueError("retention batch limit must be between 1 and 1000")
        with self.connection() as connection:
            nonce_removed = connection.execute(
                "DELETE FROM nonce_ledger WHERE rowid IN ("
                "SELECT rowid FROM nonce_ledger WHERE request_timestamp<? "
                "ORDER BY request_timestamp LIMIT ?)",
                (nonce_before_timestamp, limit),
            ).rowcount
            outbox_removed = connection.execute(
                "DELETE FROM outbox WHERE rowid IN ("
                "SELECT rowid FROM outbox WHERE status='sent' AND created_at<? "
                "ORDER BY created_at LIMIT ?)",
                (sent_outbox_before, limit),
            ).rowcount
        return {"nonce": nonce_removed, "sent_outbox": outbox_removed}

    def event_count(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM orchestration_events").fetchone()[0])

    def session_history(self, session_id: str) -> dict[str, list[dict[str, object]]]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            tables = ["blueprint_versions", "workflow_versions", "generation_attempts", "registry_snapshots", "evaluations", "directives"]
            result: dict[str, list[dict[str, object]]] = {}
            for table in tables:
                rows = connection.execute(f"SELECT * FROM {table} WHERE session_id=? ORDER BY created_at", (session_id,)).fetchall()
                result[table] = [dict(row) for row in rows]
        finally:
            connection.close()
        return {
            "blueprint_versions": result["blueprint_versions"],
            "workflow_versions": result["workflow_versions"],
            "generation_attempts": result["generation_attempts"],
            "registry_snapshots": result["registry_snapshots"],
            "evaluations": result["evaluations"],
            "directives": result["directives"],
        }

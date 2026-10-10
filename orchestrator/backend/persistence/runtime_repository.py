from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from history.orchestration_repository import utc_now
from contracts.orchestration import WorkflowFinalSummaryV1
from contracts.runtime_event import RuntimeTerminalEventV2
from services.planning_agent_delivery import FINAL_SUMMARY_OUTBOX_KIND


class RuntimeRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def create_backend_execution(
        self, *, execution_id: str, session_id: str, plan_version: int, workflow_version: int,
        backend: str, backend_workflow_id: str, backend_execution_id: str | None,
    ) -> None:
        with self._connection() as connection:
            existing = connection.execute("SELECT * FROM backend_executions WHERE execution_id=?", (execution_id,)).fetchone()
            identity = (session_id, plan_version, workflow_version, backend, backend_workflow_id, backend_execution_id)
            if existing is not None:
                observed = tuple(existing[key] for key in ("session_id", "plan_version", "workflow_version", "backend", "backend_workflow_id", "backend_execution_id"))
                if observed != identity:
                    raise ValueError("backend execution identity is immutable")
                return
            now = utc_now()
            connection.execute(
                "INSERT INTO backend_executions VALUES (?,?,?,?,?,?,?,'running',?,?)",
                (execution_id, session_id, plan_version, workflow_version, backend, backend_workflow_id, backend_execution_id, now, now),
            )

    def get_backend_execution(self, execution_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM backend_executions WHERE execution_id=?", (execution_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_execution_audit_identity(self, execution_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT e.execution_id,e.session_id,e.plan_version,e.workflow_version,s.correlation_id "
                "FROM backend_executions e JOIN sessions s ON s.session_id=e.session_id "
                "WHERE e.execution_id=?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_terminal_event(self, event: dict[str, object]) -> dict[str, object]:
        terminal = RuntimeTerminalEventV2.model_validate(event)
        payload = terminal.model_dump(mode="json")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            execution = connection.execute(
                "SELECT session_id,plan_version,workflow_version FROM backend_executions "
                "WHERE execution_id=?",
                (terminal.execution_id,),
            ).fetchone()
            if execution is None:
                raise KeyError(terminal.execution_id)
            if (
                terminal.session_id,
                terminal.plan_version,
                terminal.workflow_version,
            ) != (
                str(execution["session_id"]),
                int(execution["plan_version"]),
                int(execution["workflow_version"]),
            ):
                raise ValueError("terminal event execution identity mismatch")
            plan = connection.execute(
                "SELECT monitoring_mode,monitoring_node_count,final_summary_operation_key "
                "FROM execution_plans WHERE session_id=? AND workflow_version=?",
                (terminal.session_id, terminal.workflow_version),
            ).fetchone()
            if plan is None:
                raise KeyError(
                    f"execution plan {terminal.session_id}:{terminal.workflow_version}"
                )

            existing = connection.execute(
                "SELECT terminal_status FROM workflow_terminal_events WHERE execution_id=?",
                (terminal.execution_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["terminal_status"]) != terminal.terminal_status:
                    raise ValueError("workflow terminal status is immutable")
                connection.commit()
                return {
                    "accepted": True,
                    "duplicate": True,
                    "final_summary_created": False,
                    "monitoring_mode": str(plan["monitoring_mode"]),
                }

            now = utc_now()
            connection.execute(
                "INSERT INTO workflow_terminal_events("
                "execution_id,event_id,terminal_status,payload_json,created_at"
                ") VALUES (?,?,?,?,?)",
                (
                    terminal.execution_id,
                    terminal.event_id,
                    terminal.terminal_status,
                    payload_json,
                    now,
                ),
            )
            connection.execute(
                "UPDATE backend_executions SET status=?,updated_at=? WHERE execution_id=?",
                (terminal.terminal_status, now, terminal.execution_id),
            )

            monitoring_mode = str(plan["monitoring_mode"])
            final_summary_created = False
            if monitoring_mode == "planner_final_summary_fallback":
                operation_key = plan["final_summary_operation_key"]
                if not isinstance(operation_key, str) or not operation_key:
                    raise ValueError("fallback execution plan lacks final-summary operation key")
                summary = WorkflowFinalSummaryV1(
                    schema_version="workflow-final-summary.v1",
                    contract_version="workflow-final-summary.v1",
                    caller_identity="orchestrator-final-summary",
                    correlation_id=terminal.correlation_id,
                    session_id=terminal.session_id,
                    plan_version=terminal.plan_version,
                    workflow_version=terminal.workflow_version,
                    execution_id=terminal.execution_id,
                    terminal_status=terminal.terminal_status,
                    monitoring_mode="planner_final_summary_fallback",
                    node_execution_states=terminal.node_execution_states,
                    output_refs=terminal.output_refs,
                    artifact_refs=terminal.artifact_refs,
                    log_refs=terminal.log_refs,
                    checkpoint_refs=terminal.checkpoint_refs,
                    input_summary=terminal.input_summary,
                    retry_repair_history=terminal.retry_repair_history,
                    operation_key=operation_key,
                ).model_dump(mode="json")
                final_summary_created = bool(
                    connection.execute(
                        "INSERT OR IGNORE INTO outbox("
                        "outbox_id,session_id,kind,payload_json,status,created_at,operation_key"
                        ") VALUES (?,?,?,?, 'pending',?,?)",
                        (
                            operation_key,
                            terminal.session_id,
                            FINAL_SUMMARY_OUTBOX_KIND,
                            json.dumps(summary, sort_keys=True),
                            now,
                            operation_key,
                        ),
                    ).rowcount
                )
            elif monitoring_mode != "evaluator_direct":
                raise ValueError("execution plan has invalid monitoring mode")

            connection.commit()
            return {
                "accepted": True,
                "duplicate": False,
                "final_summary_created": final_summary_created,
                "monitoring_mode": monitoring_mode,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_runtime_decision(self, *, event: dict[str, object], decision: dict[str, object]) -> tuple[dict[str, object], bool]:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT decision_json FROM runtime_decisions WHERE event_id=?", (event["event_id"],)).fetchone()
            if existing is not None:
                connection.commit()
                return json.loads(existing[0]), False
            now = utc_now()
            event_json = json.dumps(event, sort_keys=True, separators=(",", ":"))
            decision_json = json.dumps(decision, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO orchestration_events(event_id,session_id,payload_json,status,created_at,updated_at) VALUES (?,?,?,'processed',?,?)",
                (event["event_id"], event["session_id"], event_json, now, now),
            )
            connection.execute(
                "INSERT INTO runtime_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (decision["decision_id"], event["execution_id"], event["event_id"], event["node_id"], event["attempt"], decision["action"], decision["reason_code"], decision_json, now),
            )
            if decision["action"] in {"RETRY", "REQUEST_EVALUATION"}:
                kind = "runtime.retry" if decision["action"] == "RETRY" else "runtime.evaluate"
                outbox_id = f"{kind}:{event['event_id']}"
                connection.execute(
                    "INSERT INTO outbox(outbox_id,session_id,kind,payload_json,status,created_at,operation_key) VALUES (?,?,?,?, 'pending',?,?)",
                    (outbox_id, event["session_id"], kind, decision_json, now, outbox_id),
                )
            connection.commit()
            return decision, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_budget(self, execution_id: str, *, max_calls: int, max_cost_microusd: int, max_duration_seconds: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO budget_ledger(execution_id,max_calls,max_cost_microusd,max_duration_seconds,updated_at) VALUES (?,?,?,?,?)",
                (execution_id, max_calls, max_cost_microusd, max_duration_seconds, utc_now()),
            )

    def reserve_budget(self, *, reservation_id: str, execution_id: str, node_id: str, attempt: int, calls: int, cost_microusd: int, duration_seconds: int, lease_until: float) -> bool:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ledger = connection.execute("SELECT * FROM budget_ledger WHERE execution_id=?", (execution_id,)).fetchone()
            if ledger is None:
                raise KeyError(execution_id)
            allowed = (
                ledger["reserved_calls"] + ledger["committed_calls"] + calls <= ledger["max_calls"]
                and ledger["reserved_cost_microusd"] + ledger["committed_cost_microusd"] + cost_microusd <= ledger["max_cost_microusd"]
                and ledger["reserved_duration_seconds"] + ledger["committed_duration_seconds"] + duration_seconds <= ledger["max_duration_seconds"]
            )
            if not allowed:
                connection.rollback()
                return False
            now = utc_now()
            try:
                connection.execute(
                    "INSERT INTO budget_reservations VALUES (?,?,?,?, 'reserved', ?,?,?, NULL,NULL,NULL, ?,?,?)",
                    (reservation_id, execution_id, node_id, attempt, calls, cost_microusd, duration_seconds, lease_until, now, now),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE budget_ledger SET reserved_calls=reserved_calls+?, reserved_cost_microusd=reserved_cost_microusd+?, reserved_duration_seconds=reserved_duration_seconds+?, updated_at=? WHERE execution_id=?",
                (calls, cost_microusd, duration_seconds, now, execution_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def settle_reservation(self, reservation_id: str, *, actual_calls: int, actual_cost_microusd: int, actual_duration_seconds: int) -> bool:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None or row["status"] != "reserved":
                connection.rollback()
                return False
            now = utc_now()
            connection.execute(
                "UPDATE budget_reservations SET status='committed',actual_calls=?,actual_cost_microusd=?,actual_duration_seconds=?,updated_at=? WHERE reservation_id=? AND status='reserved'",
                (actual_calls, actual_cost_microusd, actual_duration_seconds, now, reservation_id),
            )
            connection.execute(
                "UPDATE budget_ledger SET reserved_calls=reserved_calls-?,reserved_cost_microusd=reserved_cost_microusd-?,reserved_duration_seconds=reserved_duration_seconds-?,committed_calls=committed_calls+?,committed_cost_microusd=committed_cost_microusd+?,committed_duration_seconds=committed_duration_seconds+?,updated_at=? WHERE execution_id=?",
                (row["authorized_calls"], row["authorized_cost_microusd"], row["authorized_duration_seconds"], actual_calls, actual_cost_microusd, actual_duration_seconds, now, row["execution_id"]),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def budget_state(self, execution_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM budget_ledger WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return dict(row)

    def budget_audit(self, execution_id: str) -> dict[str, object]:
        with self._connection() as connection:
            ledger = connection.execute(
                "SELECT * FROM budget_ledger WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if ledger is None:
                raise KeyError(execution_id)
            reservations = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM budget_reservations WHERE execution_id=? "
                    "ORDER BY created_at,reservation_id",
                    (execution_id,),
                )
            ]
            progress = []
            for row in connection.execute(
                "SELECT * FROM progress_samples WHERE execution_id=? "
                "ORDER BY sequence,sample_id",
                (execution_id,),
            ):
                sample = dict(row)
                sample["value"] = json.loads(sample.pop("value_json"))
                progress.append(sample)
        return {"ledger": dict(ledger), "reservations": reservations, "progress": progress}

    def decisions(self, execution_id: str) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_decisions WHERE execution_id=? ORDER BY created_at", (execution_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["decision"] = json.loads(item.pop("decision_json"))
            result.append(item)
        return result

    def release_reservation(self, reservation_id: str, *, status: str = "released") -> bool:
        if status not in {"released", "expired"}:
            raise ValueError("reservation release status is invalid")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None or row["status"] != "reserved":
                connection.rollback()
                return False
            now = utc_now()
            connection.execute(
                "UPDATE budget_reservations SET status=?,updated_at=? WHERE reservation_id=? AND status='reserved'",
                (status, now, reservation_id),
            )
            connection.execute(
                "UPDATE budget_ledger SET reserved_calls=reserved_calls-?,reserved_cost_microusd=reserved_cost_microusd-?,reserved_duration_seconds=reserved_duration_seconds-?,updated_at=? WHERE execution_id=?",
                (row["authorized_calls"], row["authorized_cost_microusd"], row["authorized_duration_seconds"], now, row["execution_id"]),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def expire_reservations(self, *, now: float, limit: int = 100) -> int:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT reservation_id FROM budget_reservations WHERE status='reserved' AND lease_until<? ORDER BY lease_until LIMIT ?",
                (now, limit),
            ).fetchall()
        return sum(self.release_reservation(str(row[0]), status="expired") for row in rows)

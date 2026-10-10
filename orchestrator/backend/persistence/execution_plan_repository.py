from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from history.orchestration_repository import utc_now


_PLAINTEXT_SECRET_KEYS = {
    "authorization",
    "apikey",
    "password",
    "privatekey",
    "secret",
    "token",
}


def _assert_no_plaintext_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if not normalized.endswith("ref") and any(
                marker in normalized for marker in _PLAINTEXT_SECRET_KEYS
            ):
                raise ValueError("execution recovery context cannot persist a plaintext secret")
            _assert_no_plaintext_secrets(nested)
    elif isinstance(value, list):
        for item in value:
            _assert_no_plaintext_secrets(item)


def _validate_monitoring_policy(
    monitoring_mode: str,
    monitoring_node_count: int,
    final_summary_operation_key: str | None,
) -> None:
    if not isinstance(monitoring_node_count, int) or isinstance(
        monitoring_node_count, bool
    ):
        raise ValueError("monitoring node count must be an integer")
    if monitoring_mode == "evaluator_direct":
        if monitoring_node_count < 1 or final_summary_operation_key is not None:
            raise ValueError("evaluator-direct monitoring policy is inconsistent")
        return
    if monitoring_mode == "planner_final_summary_fallback":
        if monitoring_node_count != 0 or not final_summary_operation_key:
            raise ValueError("fallback monitoring policy is inconsistent")
        return
    raise ValueError("monitoring mode is invalid")


class ExecutionPlanRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def claim_planning_job(self, *, job_id: str, session_id: str, idempotency_key: str, blueprint_hash: str) -> tuple[str, bool]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT job_id, session_id, blueprint_hash FROM planning_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing[1:] != (session_id, blueprint_hash):
                    raise ValueError("idempotency key is bound to a different planning request")
                connection.commit()
                return str(existing[0]), False
            now = utc_now()
            connection.execute(
                "INSERT INTO planning_jobs(job_id, session_id, idempotency_key, blueprint_hash, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (job_id, session_id, idempotency_key, blueprint_hash, now, now),
            )
            connection.commit()
            return job_id, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_outbox(
        self,
        *,
        outbox_id: str,
        session_id: str,
        kind: str,
        payload: dict[str, object],
        operation_key: str,
    ) -> bool:
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            if connection.execute(
                "SELECT 1 FROM outbox WHERE operation_key=?", (operation_key,)
            ).fetchone() is not None:
                return False
            changed = connection.execute(
                "INSERT INTO outbox("
                "outbox_id,session_id,kind,payload_json,status,created_at,operation_key"
                ") VALUES (?,?,?,?, 'pending',?,?)",
                (
                    outbox_id,
                    session_id,
                    kind,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                    operation_key,
                ),
            ).rowcount
        return bool(changed)

    def enqueue_planning_job(
        self,
        *,
        job_id: str,
        session_id: str,
        correlation_id: str,
        plan_version: int,
        idempotency_key: str,
        blueprint_hash: str,
        blueprint: dict[str, object],
    ) -> tuple[str, bool]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT job_id,session_id,blueprint_hash FROM planning_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["session_id"] != session_id or existing["blueprint_hash"] != blueprint_hash:
                    raise ValueError("idempotency key is bound to a different planning request")
                connection.commit()
                return str(existing["job_id"]), False
            now = utc_now()
            blueprint_json = json.dumps(blueprint, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO sessions(session_id,correlation_id,plan_version,workflow_version,status,blueprint_json,updated_at) VALUES (?,?,?,0,'planning_queued',?,?) ON CONFLICT(session_id) DO NOTHING",
                (session_id, correlation_id, plan_version, blueprint_json, now),
            )
            session = connection.execute("SELECT correlation_id,plan_version,blueprint_json FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is None or tuple(session) != (correlation_id, plan_version, blueprint_json):
                raise ValueError("session identity is immutable")
            payload = {"job_id": job_id, "session_id": session_id, "blueprint_hash": blueprint_hash}
            connection.execute(
                "INSERT INTO planning_jobs(job_id,session_id,idempotency_key,blueprint_hash,status,payload_json,created_at,updated_at) VALUES (?,?,?,?, 'queued',?,?,?)",
                (job_id, session_id, idempotency_key, blueprint_hash, json.dumps(payload, sort_keys=True), now, now),
            )
            connection.execute(
                "INSERT INTO outbox(outbox_id,session_id,kind,payload_json,status,created_at,operation_key) VALUES (?,?, 'planning.run',?, 'pending',?,?)",
                (f"planning:{job_id}", session_id, json.dumps(payload, sort_keys=True), now, f"planning:{job_id}"),
            )
            connection.commit()
            return job_id, True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_planning_job(self, job_id: str) -> dict[str, object] | None:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT job_id,session_id,blueprint_hash,status,payload_json,created_at,updated_at FROM planning_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            payload = json.loads(result.pop("payload_json"))
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                result["result"] = payload["result"]
            return result
        finally:
            connection.close()

    def get_execution_plan(self, session_id: str, workflow_version: int) -> dict[str, object] | None:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM execution_plans WHERE session_id=? AND workflow_version=?",
                (session_id, workflow_version),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["ir"] = json.loads(result.pop("ir_json"))
            for column, field in (
                ("workflow_spec_json", "workflow_spec"),
                ("compiled_artifact_json", "compiled_artifact"),
                ("registry_snapshot_json", "registry_snapshot"),
                ("binding_metadata_json", "binding_metadata"),
            ):
                raw = result.pop(column, "{}")
                result[field] = json.loads(raw)
            return result
        finally:
            connection.close()

    def get_execution_plan_audit(self, session_id: str, workflow_version: int) -> dict[str, object] | None:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT p.*,s.correlation_id FROM execution_plans p "
                "JOIN sessions s ON s.session_id=p.session_id "
                "WHERE p.session_id=? AND p.workflow_version=?",
                (session_id, workflow_version),
            ).fetchone()
            if row is None:
                return None
            plan = dict(row)
            plan_id = str(plan["plan_id"])
            bindings = []
            for binding_row in connection.execute(
                "SELECT task_id,agent_id,decision_json FROM binding_decisions "
                "WHERE plan_id=? ORDER BY task_id",
                (plan_id,),
            ):
                decision = json.loads(binding_row["decision_json"])
                bindings.append(
                    {
                        "task_id": binding_row["task_id"],
                        "selected_agent_id": binding_row["agent_id"],
                        "rejected": decision.get("rejected", []) if isinstance(decision, dict) else [],
                    }
                )
            reports = [
                {
                    "dimension": report_row["dimension"],
                    "report": json.loads(report_row["report_json"]),
                }
                for report_row in connection.execute(
                    "SELECT dimension,report_json FROM validation_reports "
                    "WHERE plan_id=? ORDER BY dimension",
                    (plan_id,),
                )
            ]
            job_id = None
            for job_row in connection.execute(
                "SELECT job_id,payload_json FROM planning_jobs WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ):
                payload = json.loads(job_row["payload_json"])
                if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                    if payload["result"].get("plan_id") == plan_id:
                        job_id = str(job_row["job_id"])
                        break
            publication = None
            if job_id is not None:
                publication_row = connection.execute(
                    "SELECT stage,status,attempt FROM publication_attempts WHERE job_id=? "
                    "ORDER BY CASE stage WHEN 'start' THEN 5 WHEN 'activate' THEN 4 "
                    "WHEN 'canary' THEN 3 WHEN 'create' THEN 2 WHEN 'validate' THEN 1 ELSE 0 END DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if publication_row is not None:
                    publication = dict(publication_row)
            return {
                "session_id": plan["session_id"],
                "correlation_id": plan["correlation_id"],
                "plan_id": plan_id,
                "plan_version": plan["plan_version"],
                "workflow_version": plan["workflow_version"],
                "normalized_blueprint_hash": plan["blueprint_hash"],
                "snapshot_hash": plan["snapshot_hash"],
                "proposal_hash": plan["proposal_hash"],
                "ir_hash": plan["ir_hash"],
                "bindings": bindings,
                "validation_reports": reports,
                "publication": publication,
                "created_at": plan["created_at"],
            }
        finally:
            connection.close()

    def load_job_blueprint(self, job_id: str) -> dict[str, object]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT s.blueprint_json FROM planning_jobs j JOIN sessions s ON s.session_id=j.session_id WHERE j.job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise ValueError("stored Blueprint is not an object")
            return value
        finally:
            connection.close()

    def mark_job_running(self, job_id: str) -> bool:
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            changed = connection.execute(
                "UPDATE planning_jobs SET status='running',updated_at=? WHERE job_id=? AND status='queued'",
                (utc_now(), job_id),
            ).rowcount
        return bool(changed)

    def fail_job(self, job_id: str, code: str, message: str) -> None:
        safe = {"error": {"code": code[:128], "message": message[:2048]}}
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            connection.execute(
                "UPDATE planning_jobs SET status='failed',payload_json=?,updated_at=? WHERE job_id=?",
                (json.dumps(safe, sort_keys=True), utc_now(), job_id),
            )

    def complete_job(
        self,
        *,
        job_id: str,
        plan_id: str,
        session_id: str,
        plan_version: int,
        workflow_version: int,
        blueprint_hash: str,
        snapshot_hash: str,
        proposal_hash: str,
        ir_hash: str,
        ir: dict[str, object],
        bindings: dict[str, dict[str, object]],
        validation_report: dict[str, object],
        result: dict[str, object],
        workflow_spec: dict[str, object] | None = None,
        compiled_artifact: dict[str, object] | None = None,
        registry_snapshot: dict[str, object] | None = None,
        binding_metadata: dict[str, object] | None = None,
        monitoring_mode: str = "planner_final_summary_fallback",
        monitoring_node_count: int = 0,
        final_summary_operation_key: str | None = None,
    ) -> None:
        _validate_monitoring_policy(
            monitoring_mode, monitoring_node_count, final_summary_operation_key
        )
        recovery_context = {
            "workflow_spec": workflow_spec or {},
            "compiled_artifact": compiled_artifact or {},
            "registry_snapshot": registry_snapshot or {},
            "binding_metadata": binding_metadata or {},
        }
        _assert_no_plaintext_secrets(recovery_context)
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            connection.execute(
                "INSERT OR IGNORE INTO execution_plans("
                "plan_id,session_id,plan_version,workflow_version,blueprint_hash,snapshot_hash,"
                "proposal_hash,ir_hash,ir_json,created_at,workflow_spec_json,compiled_artifact_json,"
                "registry_snapshot_json,binding_metadata_json,monitoring_mode,monitoring_node_count,"
                "final_summary_operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, session_id, plan_version, workflow_version, blueprint_hash, snapshot_hash,
                 proposal_hash, ir_hash, json.dumps(ir, sort_keys=True), now,
                 json.dumps(recovery_context["workflow_spec"], sort_keys=True),
                 json.dumps(recovery_context["compiled_artifact"], sort_keys=True),
                 json.dumps(recovery_context["registry_snapshot"], sort_keys=True),
                 json.dumps(recovery_context["binding_metadata"], sort_keys=True),
                 monitoring_mode, monitoring_node_count, final_summary_operation_key),
            )
            for task_id, decision in sorted(bindings.items()):
                connection.execute(
                    "INSERT OR IGNORE INTO binding_decisions VALUES (?,?,?,?,?,?)",
                    (plan_id, task_id, snapshot_hash, decision["agent_id"], json.dumps(decision, sort_keys=True), now),
                )
            connection.execute(
                "INSERT OR REPLACE INTO validation_reports VALUES (?,?,?,?)",
                (plan_id, "all", json.dumps(validation_report, sort_keys=True), now),
            )
            connection.execute(
                "UPDATE planning_jobs SET status='started',payload_json=?,updated_at=? WHERE job_id=?",
                (json.dumps({"result": result}, sort_keys=True), now, job_id),
            )
            connection.execute(
                "UPDATE sessions SET workflow_version=?,status='workflow_active',updated_at=? WHERE session_id=?",
                (workflow_version, now, session_id),
            )
            connection.execute(
                "UPDATE outbox SET status='sent',operation_result_json=?,attempt=attempt+1 WHERE outbox_id=?",
                (json.dumps(result, sort_keys=True), f"planning:{job_id}"),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_repaired_plan(
        self,
        *,
        plan_id: str,
        session_id: str,
        plan_version: int,
        workflow_version: int,
        blueprint_hash: str,
        snapshot_hash: str,
        proposal_hash: str,
        ir_hash: str,
        ir: dict[str, object],
        validation_report: dict[str, object],
        workflow_spec: dict[str, object] | None = None,
        compiled_artifact: dict[str, object] | None = None,
        registry_snapshot: dict[str, object] | None = None,
        binding_metadata: dict[str, object] | None = None,
        monitoring_mode: str = "planner_final_summary_fallback",
        monitoring_node_count: int = 0,
        final_summary_operation_key: str | None = None,
    ) -> None:
        _validate_monitoring_policy(
            monitoring_mode, monitoring_node_count, final_summary_operation_key
        )
        recovery_context = {
            "workflow_spec": workflow_spec or {},
            "compiled_artifact": compiled_artifact or {},
            "registry_snapshot": registry_snapshot or {},
            "binding_metadata": binding_metadata or {},
        }
        _assert_no_plaintext_secrets(recovery_context)
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT plan_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None or int(session[0]) != plan_version:
                raise ValueError("repair plan identity differs from the durable session")
            existing = connection.execute(
                "SELECT ir_hash FROM execution_plans WHERE session_id=? AND workflow_version=?",
                (session_id, workflow_version),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != ir_hash:
                    raise ValueError("workflow version is immutable")
                connection.commit()
                return
            previous = connection.execute(
                "SELECT ir_hash FROM execution_plans WHERE session_id=? ORDER BY workflow_version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if previous is not None and str(previous[0]) == ir_hash:
                raise ValueError("repair IR is not materially changed")
            now = utc_now()
            connection.execute(
                "INSERT INTO execution_plans("
                "plan_id,session_id,plan_version,workflow_version,blueprint_hash,snapshot_hash,"
                "proposal_hash,ir_hash,ir_json,created_at,workflow_spec_json,compiled_artifact_json,"
                "registry_snapshot_json,binding_metadata_json,monitoring_mode,monitoring_node_count,"
                "final_summary_operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    session_id,
                    plan_version,
                    workflow_version,
                    blueprint_hash,
                    snapshot_hash,
                    proposal_hash,
                    ir_hash,
                    json.dumps(ir, sort_keys=True),
                    now,
                    json.dumps(recovery_context["workflow_spec"], sort_keys=True),
                    json.dumps(recovery_context["compiled_artifact"], sort_keys=True),
                    json.dumps(recovery_context["registry_snapshot"], sort_keys=True),
                    json.dumps(recovery_context["binding_metadata"], sort_keys=True),
                    monitoring_mode,
                    monitoring_node_count,
                    final_summary_operation_key,
                ),
            )
            connection.execute(
                "INSERT INTO validation_reports VALUES (?,?,?,?)",
                (plan_id, "all", json.dumps(validation_report, sort_keys=True), now),
            )
            connection.execute(
                "UPDATE sessions SET workflow_version=?,status='workflow_active',updated_at=? "
                "WHERE session_id=?",
                (workflow_version, now, session_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

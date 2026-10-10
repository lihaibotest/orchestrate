from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3

from contracts.orchestration import WorkflowEscalationV1
from history.orchestration_repository import OrchestrationRepository, utc_now
from services.planning_agent_delivery import ESCALATION_OUTBOX_KIND


DEFAULT_MAX_SELF_REPAIRS = 2


class WorkflowRepairCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        max_repair_attempts: int = DEFAULT_MAX_SELF_REPAIRS,
    ) -> None:
        if max_repair_attempts < 1 or max_repair_attempts > 100:
            raise ValueError("max_repair_attempts must be between 1 and 100")
        self.database_path = database_path
        self.max_repair_attempts = max_repair_attempts
        OrchestrationRepository(database_path)

    def claim_repair(
        self,
        *,
        correlation_id: str,
        session_id: str,
        plan_version: int,
        workflow_version: int,
        lineage_id: str,
        event_id: str,
        node_id: str,
        reason_code: str,
    ) -> dict[str, object]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT action,repair_attempt FROM workflow_repair_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if duplicate is not None:
                state = connection.execute(
                    "SELECT max_attempts FROM workflow_repair_state WHERE session_id=? AND lineage_id=?",
                    (session_id, lineage_id),
                ).fetchone()
                connection.commit()
                return {
                    "action": str(duplicate["action"]),
                    "repair_attempt": int(duplicate["repair_attempt"]),
                    "max_repair_attempts": int(state["max_attempts"]),
                    "created": False,
                    "escalation_created": False,
                }

            now = utc_now()
            connection.execute(
                "INSERT OR IGNORE INTO workflow_repair_state("
                "session_id,lineage_id,max_attempts,attempts,escalated,updated_at"
                ") VALUES (?,?,?,0,0,?)",
                (session_id, lineage_id, self.max_repair_attempts, now),
            )
            state = connection.execute(
                "SELECT max_attempts,attempts,escalated FROM workflow_repair_state "
                "WHERE session_id=? AND lineage_id=?",
                (session_id, lineage_id),
            ).fetchone()
            if state is None:
                raise RuntimeError("workflow repair state was not created")

            maximum = int(state["max_attempts"])
            attempts = int(state["attempts"])
            escalation_created = False
            if attempts < maximum:
                attempts += 1
                action = "repair"
                connection.execute(
                    "UPDATE workflow_repair_state SET attempts=?,updated_at=? "
                    "WHERE session_id=? AND lineage_id=?",
                    (attempts, now, session_id, lineage_id),
                )
            else:
                action = "escalate"
                if not bool(state["escalated"]):
                    operation_key = (
                        f"escalation:{session_id}:{lineage_id}:REPAIR_ATTEMPTS_EXHAUSTED"
                    )
                    escalation = WorkflowEscalationV1(
                        schema_version="workflow-escalation.v1",
                        contract_version="workflow-escalation.v1",
                        caller_identity="orchestrator-escalation",
                        correlation_id=correlation_id,
                        session_id=session_id,
                        plan_version=plan_version,
                        workflow_version=workflow_version,
                        reason_code="REPAIR_ATTEMPTS_EXHAUSTED",
                        required_decision="revise the plan or capability boundary",
                        repair_attempts=attempts,
                        evidence=[
                            {
                                "ref": f"runtime-event://{event_id}",
                                "node_id": node_id,
                                "error_code": reason_code,
                                "detail": "self-repair attempt limit reached",
                            }
                        ],
                        operation_key=operation_key,
                    ).model_dump(mode="json")
                    connection.execute(
                        "INSERT INTO outbox("
                        "outbox_id,session_id,kind,payload_json,status,created_at,operation_key"
                        ") VALUES (?,?,?,?, 'pending',?,?)",
                        (
                            operation_key,
                            session_id,
                            ESCALATION_OUTBOX_KIND,
                            json.dumps(escalation, sort_keys=True),
                            now,
                            operation_key,
                        ),
                    )
                    connection.execute(
                        "UPDATE workflow_repair_state SET escalated=1,"
                        "escalation_operation_key=?,updated_at=? "
                        "WHERE session_id=? AND lineage_id=?",
                        (operation_key, now, session_id, lineage_id),
                    )
                    escalation_created = True

            connection.execute(
                "INSERT INTO workflow_repair_events("
                "event_id,session_id,lineage_id,action,repair_attempt,created_at"
                ") VALUES (?,?,?,?,?,?)",
                (event_id, session_id, lineage_id, action, attempts, now),
            )
            connection.commit()
            return {
                "action": action,
                "repair_attempt": attempts,
                "max_repair_attempts": maximum,
                "created": True,
                "escalation_created": escalation_created,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare_repaired_resume(
        self,
        *,
        session_id: str,
        lineage_id: str,
        parent_execution_id: str,
        parent_workflow_version: int,
        repaired_workflow_version: int,
        checkpoint: dict[str, object],
        original_idempotency_key: str,
    ) -> dict[str, object]:
        if repaired_workflow_version <= parent_workflow_version:
            raise ValueError("repaired workflow version must advance the parent version")
        checkpoint_json = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
        identity = (
            f"{session_id}:{lineage_id}:{parent_execution_id}:"
            f"{repaired_workflow_version}:{original_idempotency_key}"
        )
        resume_idempotency_key = "repair-resume-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:32]
        operation_key = (
            f"repair-start:{session_id}:{lineage_id}:{repaired_workflow_version}"
        )
        start_payload = {
            "idempotency_key": resume_idempotency_key,
            "parent_execution_id": parent_execution_id,
            "parent_workflow_version": parent_workflow_version,
            "workflow_version": repaired_workflow_version,
            "resume_from_checkpoint": json.loads(checkpoint_json),
        }
        start_payload_json = json.dumps(
            start_payload, sort_keys=True, separators=(",", ":")
        )

        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workflow_repair_resumes WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
            if existing is not None:
                expected = (
                    session_id,
                    lineage_id,
                    parent_execution_id,
                    parent_workflow_version,
                    repaired_workflow_version,
                    checkpoint_json,
                    start_payload_json,
                )
                observed = tuple(
                    existing[field]
                    for field in (
                        "session_id",
                        "lineage_id",
                        "parent_execution_id",
                        "parent_workflow_version",
                        "repaired_workflow_version",
                        "checkpoint_json",
                        "start_payload_json",
                    )
                )
                if observed != expected:
                    raise ValueError("repair resume operation identity is immutable")
                connection.commit()
                return {
                    "operation_key": operation_key,
                    "start_payload": json.loads(str(existing["start_payload_json"])),
                    "created": False,
                }

            parent = connection.execute(
                "SELECT session_id,workflow_version FROM backend_executions "
                "WHERE execution_id=?",
                (parent_execution_id,),
            ).fetchone()
            if parent is None:
                raise KeyError(parent_execution_id)
            if (str(parent["session_id"]), int(parent["workflow_version"])) != (
                session_id,
                parent_workflow_version,
            ):
                raise ValueError("parent execution identity does not match repair lineage")

            now = utc_now()
            connection.execute(
                "INSERT INTO workflow_repair_resumes("
                "operation_key,session_id,lineage_id,parent_execution_id,"
                "parent_workflow_version,repaired_workflow_version,checkpoint_json,"
                "start_payload_json,status,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?, 'prepared',?,?)",
                (
                    operation_key,
                    session_id,
                    lineage_id,
                    parent_execution_id,
                    parent_workflow_version,
                    repaired_workflow_version,
                    checkpoint_json,
                    start_payload_json,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE backend_executions SET status='superseded_for_repair',updated_at=? "
                "WHERE execution_id=?",
                (now, parent_execution_id),
            )
            connection.commit()
            return {
                "operation_key": operation_key,
                "start_payload": start_payload,
                "created": True,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_repaired_execution_started(
        self, *, operation_key: str, repaired_execution_id: str
    ) -> bool:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            resume = connection.execute(
                "SELECT repaired_execution_id FROM workflow_repair_resumes "
                "WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
            if resume is None:
                raise KeyError(operation_key)
            existing = resume["repaired_execution_id"]
            if existing is not None:
                if str(existing) != repaired_execution_id:
                    raise ValueError("repair start operation already has another execution")
                connection.commit()
                return False
            connection.execute(
                "UPDATE workflow_repair_resumes SET status='started',"
                "repaired_execution_id=?,updated_at=? WHERE operation_key=?",
                (repaired_execution_id, utc_now(), operation_key),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

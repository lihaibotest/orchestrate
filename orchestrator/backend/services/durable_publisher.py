from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Callable

from history.orchestration_repository import utc_now


class DurablePublisher:
    def __init__(self, database_path: Path, backend: object) -> None:
        self.database_path = database_path
        self.backend = backend

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _record_intent(
        self,
        *,
        job_id: str,
        session_id: str,
        stage: str,
        operation_key: str,
    ) -> sqlite3.Row | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, operation_result_json FROM publication_attempts WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
            if row is not None:
                return row
            now = utc_now()
            connection.execute(
                "INSERT INTO publication_attempts(publication_id,job_id,stage,operation_key,status,attempt,created_at,updated_at) VALUES (?,?,?,?, 'pending',0,?,?)",
                (operation_key, job_id, stage, operation_key, now, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(outbox_id,session_id,kind,payload_json,status,created_at,operation_key) VALUES (?,?, 'publication.advance',?, 'pending',?,?)",
                (
                    f"publication:{operation_key}",
                    session_id,
                    json.dumps({"job_id": job_id, "stage": stage}, sort_keys=True),
                    now,
                    operation_key,
                ),
            )
        return None

    def _commit_stage(
        self,
        *,
        job_id: str,
        stage: str,
        operation_key: str,
        result: dict[str, object],
        workflow_id: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute(
                "UPDATE publication_attempts SET status='committed',operation_result_json=?,last_error=NULL,updated_at=? WHERE operation_key=?",
                (json.dumps(result, sort_keys=True), utc_now(), operation_key),
            )
            connection.execute(
                "UPDATE publication_sagas SET state=?,workflow_id=COALESCE(?,workflow_id),"
                "execution_id=COALESCE(?,execution_id),updated_at=? WHERE job_id=?",
                (stage, workflow_id, execution_id, utc_now(), job_id),
            )
            connection.execute(
                "UPDATE outbox SET status='sent',operation_result_json=? WHERE outbox_id=?",
                (json.dumps(result, sort_keys=True), f"publication:{operation_key}"),
            )
        return result

    def _stage(
        self,
        *,
        job_id: str,
        session_id: str,
        stage: str,
        operation_key: str,
        action: Callable[[], dict[str, object]],
        workflow_id: str | None = None,
        validate_result: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        row = self._record_intent(
            job_id=job_id,
            session_id=session_id,
            stage=stage,
            operation_key=operation_key,
        )
        if row is not None and row["status"] == "committed":
            return json.loads(row["operation_result_json"])
        if row is not None:
            reconciliation = self.backend.lookup_operation(operation_key)
            if reconciliation.get("status") == "committed":
                candidate = reconciliation.get("result", {})
                result = candidate if isinstance(candidate, dict) else {}
                if validate_result is not None:
                    try:
                        validate_result(result)
                    except Exception as error:
                        self._fail_stage(operation_key, error)
                        raise
                return self._commit_stage(
                    job_id=job_id,
                    stage=stage,
                    operation_key=operation_key,
                    result=result,
                    workflow_id=(
                        str(result.get("id") or result.get("workflow_id") or "")
                        if stage == "inactive_created"
                        else workflow_id
                    ),
                    execution_id=(
                        str(result.get("execution_id") or result.get("executionId") or "")
                        if stage == "started"
                        else None
                    ),
                )
        with self._connection() as connection:
            connection.execute(
                "UPDATE publication_attempts SET attempt=attempt+1,status='pending',updated_at=? WHERE operation_key=?",
                (utc_now(), operation_key),
            )
        try:
            result = action()
        except Exception as error:
            reconciliation = self.backend.lookup_operation(operation_key)
            if reconciliation.get("status") != "committed":
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE publication_attempts SET status='failed',last_error=?,updated_at=? WHERE operation_key=?",
                        (str(error)[:2048], utc_now(), operation_key),
                    )
                    connection.execute(
                        "UPDATE outbox SET status='pending',attempt=attempt+1,last_error=? WHERE outbox_id=?",
                        (str(error)[:2048], f"publication:{operation_key}"),
                    )
                raise
            candidate = reconciliation.get("result", {})
            result = candidate if isinstance(candidate, dict) else {}
        if validate_result is not None:
            try:
                validate_result(result)
            except Exception as error:
                self._fail_stage(operation_key, error)
                raise
        return self._commit_stage(
            job_id=job_id,
            stage=stage,
            operation_key=operation_key,
            result=result,
            workflow_id=(
                str(result.get("id") or result.get("workflow_id") or "")
                if stage == "inactive_created"
                else workflow_id
            ),
            execution_id=(
                str(result.get("execution_id") or result.get("executionId") or "")
                if stage == "started"
                else None
            ),
        )

    def _fail_stage(self, operation_key: str, error: Exception) -> None:
        message = str(error)[:2048]
        with self._connection() as connection:
            connection.execute(
                "UPDATE publication_attempts SET status='failed',last_error=?,updated_at=? WHERE operation_key=?",
                (message, utc_now(), operation_key),
            )
            connection.execute(
                "UPDATE outbox SET status='pending',attempt=attempt+1,last_error=? WHERE outbox_id=?",
                (message, f"publication:{operation_key}"),
            )

    @staticmethod
    def _validate_started(result: dict[str, object]) -> None:
        status = str(result.get("status", "")).lower()
        accepted = result.get("accepted") is True
        execution_id = str(result.get("execution_id") or result.get("executionId") or "")
        if status not in {"started", "accepted", "running", "success", "completed"} and not accepted:
            raise RuntimeError(f"start operation failed: status={status or 'missing'}")
        if not execution_id:
            raise RuntimeError("start operation failed: execution identity is missing")

    def _local_stage(
        self,
        *,
        job_id: str,
        session_id: str,
        stage: str,
        operation_key: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        row = self._record_intent(
            job_id=job_id,
            session_id=session_id,
            stage=stage,
            operation_key=operation_key,
        )
        if row is not None and row["status"] == "committed":
            return json.loads(row["operation_result_json"])
        return self._commit_stage(
            job_id=job_id,
            stage=stage,
            operation_key=operation_key,
            result=result,
        )

    def _prepare(
        self,
        *,
        job_id: str,
        session_id: str,
        plan_version: int,
        workflow_version: int,
        artifact: dict[str, object],
        start_payload: dict[str, object],
        canary: bool,
        operation_key: str,
    ) -> None:
        artifact_json = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        start_json = json.dumps(start_payload, sort_keys=True, separators=(",", ":"))
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT session_id,plan_version,workflow_version,artifact_json,start_payload_json,canary FROM publication_sagas WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is None:
                now = utc_now()
                connection.execute(
                    "INSERT INTO publication_sagas(job_id,session_id,plan_version,workflow_version,artifact_json,start_payload_json,canary,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'prepared',?,?)",
                    (job_id, session_id, plan_version, workflow_version, artifact_json, start_json, int(canary), now, now),
                )
            elif tuple(existing) != (
                session_id,
                plan_version,
                workflow_version,
                artifact_json,
                start_json,
                int(canary),
            ):
                raise ValueError("publication saga identity is immutable")
        self._local_stage(
            job_id=job_id,
            session_id=session_id,
            stage="prepared",
            operation_key=operation_key,
            result={"status": "prepared"},
        )

    def publish(
        self,
        *,
        job_id: str,
        session_id: str,
        plan_version: int,
        workflow_version: int,
        artifact: dict[str, object],
        start_payload: dict[str, object],
        canary: bool,
    ) -> dict[str, object]:
        prefix = f"{job_id}:publish"
        self._prepare(
            job_id=job_id,
            session_id=session_id,
            plan_version=plan_version,
            workflow_version=workflow_version,
            artifact=artifact,
            start_payload=start_payload,
            canary=canary,
            operation_key=f"{prefix}:prepared",
        )
        self._stage(
            job_id=job_id,
            session_id=session_id,
            stage="validated",
            operation_key=f"{prefix}:validated",
            action=lambda: self.backend.validate(artifact, operation_key=f"{prefix}:validated"),
        )
        created = self._stage(
            job_id=job_id,
            session_id=session_id,
            stage="inactive_created",
            operation_key=f"{prefix}:inactive_created",
            action=lambda: self.backend.publish(artifact, operation_key=f"{prefix}:inactive_created"),
        )
        workflow_id = str(created.get("id") or created.get("workflow_id") or "")
        if not workflow_id:
            raise RuntimeError("published backend artifact has no workflow identity")
        if canary:
            result = self._stage(
                job_id=job_id,
                session_id=session_id,
                stage="canary_passed",
                operation_key=f"{prefix}:canary_passed",
                action=lambda: self.backend.canary(
                    workflow_id, operation_key=f"{prefix}:canary_passed"
                ),
                workflow_id=workflow_id,
            )
            if result.get("status") != "passed":
                raise RuntimeError("Canary failed; previous workflow remains active")
        else:
            self._local_stage(
                job_id=job_id,
                session_id=session_id,
                stage="canary_passed",
                operation_key=f"{prefix}:canary_passed",
                result={"status": "skipped"},
            )
        self._stage(
            job_id=job_id,
            session_id=session_id,
            stage="activated",
            operation_key=f"{prefix}:activated",
            action=lambda: self.backend.activate(
                workflow_id, operation_key=f"{prefix}:activated"
            ),
            workflow_id=workflow_id,
        )
        started = self._stage(
            job_id=job_id,
            session_id=session_id,
            stage="started",
            operation_key=f"{prefix}:started",
            action=lambda: self.backend.start(
                workflow_id,
                {**start_payload, "session_id": session_id, "plan_version": plan_version, "workflow_version": workflow_version},
                operation_key=f"{prefix}:started",
            ),
            workflow_id=workflow_id,
            validate_result=self._validate_started,
        )
        execution_id = str(started.get("execution_id") or started.get("executionId") or "")
        start_lookup = self.backend.lookup_operation(f"{prefix}:started")
        if start_lookup.get("status") != "committed":
            raise RuntimeError("started publication operation cannot be reconciled")
        self._local_stage(
            job_id=job_id,
            session_id=session_id,
            stage="reconciled",
            operation_key=f"{prefix}:reconciled",
            result={"status": "reconciled", "start_operation_key": f"{prefix}:started"},
        )
        return {"workflow_id": workflow_id, "execution_id": execution_id, "status": "started"}

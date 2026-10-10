from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from history.orchestration_repository import utc_now


class PlanningJobWorker:
    def __init__(self, database_path: Path, runner: object) -> None:
        self.database_path = database_path
        self.runner = runner

    def recover_incomplete_jobs(self) -> list[str]:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT job_id FROM planning_jobs "
                "WHERE status='running' AND lease_owner IS NULL ORDER BY created_at"
            ).fetchall()
            job_ids = [str(row[0]) for row in rows]
            if job_ids:
                connection.executemany(
                    "UPDATE planning_jobs SET status='queued',updated_at=? WHERE job_id=?",
                    [(utc_now(), job_id) for job_id in job_ids],
                )
            connection.commit()
            return job_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_job_ids(self, limit: int = 10) -> list[str]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT o.payload_json FROM outbox o "
                "JOIN planning_jobs j ON j.job_id=json_extract(o.payload_json, '$.job_id') "
                "WHERE o.kind='planning.run' AND o.status='pending' AND j.status='queued' "
                "ORDER BY o.created_at LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        job_ids: list[str] = []
        for row in rows:
            payload = json.loads(row[0])
            if isinstance(payload, dict) and payload.get("job_id"):
                job_ids.append(str(payload["job_id"]))
        return job_ids

    def run_once(self, limit: int = 10) -> int:
        processed = 0
        for job_id in self.pending_job_ids(limit):
            try:
                self.runner.run(job_id)
            except Exception:
                continue
            processed += 1
        return processed

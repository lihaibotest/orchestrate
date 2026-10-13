from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from domain.context_event import ContextEvent


class PostgresContextOutboxRepository:
    """PostgreSQL adapter for P0-11 ContextEvent + outbox facts."""

    def __init__(self, repository: Any, *, lease_seconds: int = 300) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds

    @staticmethod
    def _row_event(row: tuple[Any, ...]) -> ContextEvent:
        return ContextEvent(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]),
            row[6], row[7], row[8], row[9], row[10],
        )

    def enqueue(self, event: ContextEvent, *, force_failure: bool = False) -> bool:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO cmm_context_event(event_id,event_type,tenant_id,correlation_id,workflow_family_id,workflow_version,execution_id,node_id,attempt_no,payload,occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (event_id) DO NOTHING",
                (event.event_id, event.event_type, event.tenant_id, event.correlation_id,
                 event.workflow_family_id, event.workflow_version, event.execution_id,
                 event.node_id, event.attempt_no, self.repository._json(event.payload), event.occurred_at),
            )
            created = cursor.rowcount == 1
            if not created:
                return False
            if force_failure:
                raise RuntimeError("transaction rolled back")
            cursor.execute(
                "INSERT INTO cmm_outbox(event_id,event_type,idempotency_key,payload,status,tenant_id,correlation_id) VALUES (%s,%s,%s,%s::jsonb,'PENDING',%s,%s) ON CONFLICT (idempotency_key) DO NOTHING",
                (event.event_id, event.event_type, event.event_id, self.repository._json(event.as_payload()), event.tenant_id, event.correlation_id),
            )
            return True

    def claim(self, *, limit: int = 50) -> list[ContextEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = datetime.now(timezone.utc)
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT e.event_id,e.event_type,e.tenant_id,e.correlation_id,e.workflow_family_id,e.workflow_version,e.execution_id,e.node_id,e.attempt_no,e.payload,e.occurred_at FROM cmm_outbox o JOIN cmm_context_event e ON e.event_id=o.event_id WHERE o.status='PENDING' AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= %s) ORDER BY o.created_at FOR UPDATE OF o SKIP LOCKED LIMIT %s",
                (now, limit),
            )
            rows = cursor.fetchall()
            result = [self._row_event(row) for row in rows]
            for event in result:
                cursor.execute(
                    "UPDATE cmm_outbox SET status='IN_FLIGHT',attempts=attempts+1,lease_until=%s WHERE event_id=%s",
                    (now + timedelta(seconds=self.lease_seconds), event.event_id),
                )
            return result

    def mark_sent(self, event_id: str) -> None:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("UPDATE cmm_outbox SET status='SENT',sent_at=now(),lease_until=NULL WHERE event_id=%s", (event_id,))

    def mark_failed(self, event_id: str, error: str) -> None:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("UPDATE cmm_outbox SET status='PENDING',last_error=%s,lease_until=NULL,next_attempt_at=now() WHERE event_id=%s", (error[:2048], event_id))

    def recover_expired(self) -> int:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("UPDATE cmm_outbox SET status='PENDING',lease_until=NULL WHERE status='IN_FLIGHT' AND lease_until < now()")
            return int(cursor.rowcount)

    def pending_count(self) -> int:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT count(*) FROM cmm_outbox WHERE status IN ('PENDING','IN_FLIGHT')")
            return int(cursor.fetchone()[0])

    def lag_seconds(self) -> float:
        with self.repository._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT COALESCE(EXTRACT(EPOCH FROM (now()-min(created_at))),0) FROM cmm_outbox WHERE status IN ('PENDING','IN_FLIGHT')")
            return float(cursor.fetchone()[0])

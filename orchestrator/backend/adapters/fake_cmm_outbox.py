from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from domain.context_event import ContextEvent


@dataclass
class OutboxRow:
    event: ContextEvent
    status: str = "PENDING"
    attempts: int = 0
    lease_until: datetime | None = None
    last_error: str | None = None


class InMemoryContextOutboxRepository:
    def __init__(self) -> None:
        self.rows: dict[str, OutboxRow] = {}
        self._lock = RLock()

    def enqueue(self, event: ContextEvent, *, force_failure: bool = False) -> bool:
        with self._lock:
            if event.event_id in self.rows:
                return False
            self.rows[event.event_id] = OutboxRow(event)
            if force_failure:
                self.rows.pop(event.event_id, None)
                raise RuntimeError("transaction rolled back")
            return True

    def claim(self, *, limit: int = 50) -> list[OutboxRow]:
        with self._lock:
            now = datetime.now(timezone.utc)
            claimed: list[OutboxRow] = []
            for row in self.rows.values():
                if row.status != "PENDING" or (row.lease_until and row.lease_until > now):
                    continue
                row.status = "IN_FLIGHT"
                row.attempts += 1
                row.lease_until = now + timedelta(minutes=5)
                claimed.append(row)
                if len(claimed) >= limit:
                    break
            return claimed

    def mark_sent(self, event_id: str) -> None:
        with self._lock:
            row = self.rows[event_id]
            row.status = "SENT"
            row.lease_until = None

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._lock:
            row = self.rows[event_id]
            row.status = "PENDING"
            row.lease_until = None
            row.last_error = error[:2048]

    def recover_expired(self) -> int:
        with self._lock:
            now = datetime.now(timezone.utc)
            count = 0
            for row in self.rows.values():
                if row.status == "IN_FLIGHT" and row.lease_until and row.lease_until <= now:
                    row.status = "PENDING"
                    row.lease_until = None
                    count += 1
            return count

    def pending_count(self) -> int:
        with self._lock:
            return sum(row.status in {"PENDING", "IN_FLIGHT"} for row in self.rows.values())

    def lag_seconds(self) -> float:
        with self._lock:
            pending = [row for row in self.rows.values() if row.status in {"PENDING", "IN_FLIGHT"}]
            if not pending:
                return 0.0
            now = datetime.now(timezone.utc)
            return max(0.0, (now - min(row.event.occurred_at for row in pending)).total_seconds())

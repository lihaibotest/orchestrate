from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_cmm import FakeCMMAdapter  # noqa: E402
from adapters.fake_cmm_outbox import InMemoryContextOutboxRepository  # noqa: E402
from application.cmm_sync_service import CMMOutboxPublisher, ContextEventService  # noqa: E402
from domain.context_event import (  # noqa: E402
    CONTEXT_EVENT_TYPES,
    ContextEvent,
    ContextEventType,
    ContextEventValidationError,
)


class CMMEventTests(unittest.TestCase):
    def test_ten_context_event_types_have_stable_ids_and_safe_payloads(self) -> None:
        self.assertEqual(len(CONTEXT_EVENT_TYPES), 10)
        events = [
            ContextEvent.create(
                event_type=event_type,
                tenant_id="tenant-a",
                correlation_id="corr-1",
                workflow_family_id="family-1",
                workflow_version=1,
                execution_id="execution-1",
                payload={"fact": event_type},
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            for event_type in CONTEXT_EVENT_TYPES
        ]
        self.assertEqual(len({event.event_id for event in events}), 10)
        self.assertEqual(events[0].event_id, ContextEvent.create(
            event_type=events[0].event_type, tenant_id="tenant-a", correlation_id="corr-1",
            workflow_family_id="family-1", workflow_version=1, execution_id="execution-1",
            payload={"fact": events[0].event_type}, occurred_at=events[0].occurred_at,
        ).event_id)
        with self.assertRaises(ContextEventValidationError):
            ContextEvent.create(
                event_type=ContextEventType.EXECUTION_STARTED,
                tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
                workflow_version=1, execution_id="execution-1", payload={"token": "secret"},
            )
        with self.assertRaises(ContextEventValidationError):
            ContextEvent.create(
                event_type=ContextEventType.EXECUTION_STARTED,
                tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
                workflow_version=1, execution_id="execution-1", payload={"quality_score": 0.9},
            )

    def test_outbox_is_atomic_idempotent_and_crash_recoverable(self) -> None:
        repository = InMemoryContextOutboxRepository()
        service = ContextEventService(repository)
        event = ContextEvent.create(
            event_type=ContextEventType.EXECUTION_STARTED,
            tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
            workflow_version=1, execution_id="execution-1", payload={"status": "RUNNING"},
        )
        self.assertTrue(service.enqueue(event))
        self.assertFalse(service.enqueue(event))
        self.assertEqual(len(repository.rows), 1)
        publisher = CMMOutboxPublisher(repository, FakeCMMAdapter())
        publisher.publish_once()
        self.assertEqual(repository.rows[event.event_id].status, "SENT")
        self.assertEqual(len(publisher.cmm.appended), 1)
        repository.rows[event.event_id].status = "IN_FLIGHT"
        repository.rows[event.event_id].lease_until = None
        publisher.recover_expired()
        publisher.publish_once()
        self.assertEqual(len(publisher.cmm.appended), 1)

    def test_publisher_is_concurrently_idempotent(self) -> None:
        repository = InMemoryContextOutboxRepository()
        service = ContextEventService(repository)
        event = ContextEvent.create(
            event_type=ContextEventType.WORKFLOW_COMPLETED,
            tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
            workflow_version=1, execution_id="execution-1", payload={"status": "SUCCEEDED"},
        )
        service.enqueue(event)
        cmm = FakeCMMAdapter()
        publisher = CMMOutboxPublisher(repository, cmm)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: publisher.publish_once(), range(16)))
        self.assertEqual(len(cmm.appended), 1)

    def test_cmm_disconnect_degrades_without_skipping_gate(self) -> None:
        repository = InMemoryContextOutboxRepository()
        service = ContextEventService(repository)
        cmm = FakeCMMAdapter(available=False)
        view = service.get_view(cmm, "context-1")
        self.assertTrue(view.degraded)
        self.assertEqual(view.text, "")
        event = ContextEvent.create(
            event_type=ContextEventType.WORKFLOW_SELF_TESTED,
            tenant_id="tenant-a", correlation_id="corr-1", workflow_family_id="family-1",
            workflow_version=1, execution_id="execution-1", payload={"passed": True},
        )
        self.assertTrue(service.enqueue(event))
        self.assertEqual(repository.rows[event.event_id].status, "PENDING")

    def test_cmm_view_is_bounded_to_24000_characters(self) -> None:
        repository = InMemoryContextOutboxRepository()
        service = ContextEventService(repository)
        view = service.get_view(FakeCMMAdapter(view="x" * 30000), "context-1")
        self.assertEqual(len(view.text), 24000)
        self.assertFalse(view.degraded)

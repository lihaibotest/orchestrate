from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.context_event import ContextEvent
from ports.cmm import CMMPort, CMMView
from ports.context_outbox import ContextOutboxRepositoryPort


class CMMMetricsPort(Protocol):
    def inc(self, name: str, value: int = 1) -> None: ...


@dataclass(frozen=True)
class SafeCMMView:
    text: str
    degraded: bool
    source: str = "CMM"


class ContextEventService:
    def __init__(self, repository: ContextOutboxRepositoryPort, metrics: CMMMetricsPort | None = None) -> None:
        self.repository = repository
        self.metrics = metrics

    def enqueue(self, event: ContextEvent) -> bool:
        created = self.repository.enqueue(event)
        if self.metrics is not None:
            self.metrics.inc("context_events_enqueued_total" if created else "context_events_deduplicated_total")
        return created

    def get_view(self, cmm: CMMPort, context_id: str) -> SafeCMMView:
        try:
            view: CMMView = cmm.get_view(context_id)
        except Exception:
            if self.metrics is not None:
                self.metrics.inc("cmm_degraded_total")
            return SafeCMMView("", True, "DEGRADED")
        text = str(view.text)
        # TBD-03 does not define a tokenizer. Bound both common whitespace
        # tokens and pathological unbroken text without inventing wire fields.
        tokens = text.split()
        if len(tokens) > 24000:
            text = " ".join(tokens[:24000])
        if len(text) > 24000:
            text = text[:24000]
        return SafeCMMView(text, bool(view.degraded), view.source)


class CMMOutboxPublisher:
    def __init__(self, repository: ContextOutboxRepositoryPort, cmm: CMMPort, metrics: CMMMetricsPort | None = None) -> None:
        self.repository = repository
        self.cmm = cmm
        self.metrics = metrics

    def publish_once(self, *, limit: int = 50) -> int:
        published = 0
        for row in self.repository.claim(limit=limit):
            event = row if isinstance(row, ContextEvent) else row.event
            try:
                self.cmm.append_event(event)
            except Exception as error:
                self.repository.mark_failed(event.event_id, str(error))
                continue
            self.repository.mark_sent(event.event_id)
            published += 1
            if self.metrics is not None:
                self.metrics.inc("context_events_published_total")
        lag = getattr(self.repository, "lag_seconds", None)
        if self.metrics is not None and callable(lag):
            self.metrics.observe("cmm_outbox_lag", float(lag()))
        return published

    def recover_expired(self) -> int:
        recovered = self.repository.recover_expired()
        if self.metrics is not None:
            self.metrics.inc("cmm_outbox_recovered_total", recovered)
        return recovered


class ContextEventBridge:
    """Small seam for P0-03/07/08/10 facts; no external CMM schema is assumed."""

    def __init__(self, service: ContextEventService) -> None:
        self.service = service

    def emit(self, event: ContextEvent) -> bool:
        return self.service.enqueue(event)

    def from_fact(self, event_type: str, *, tenant_id: str, correlation_id: str,
                  workflow_family_id: str, workflow_version: int,
                  execution_id: str | None = None, node_id: str | None = None,
                  attempt_no: int | None = None, payload: dict[str, object] | None = None) -> bool:
        return self.emit(ContextEvent.create(
            event_type=event_type, tenant_id=tenant_id, correlation_id=correlation_id,
            workflow_family_id=workflow_family_id, workflow_version=workflow_version,
            execution_id=execution_id, node_id=node_id, attempt_no=attempt_no,
            payload=payload,
        ))

    def workflow_version_created(self, **kwargs: object) -> bool:
        return self.from_fact("WORKFLOW_VERSION_CREATED", **kwargs)  # type: ignore[arg-type]

    def workflow_self_tested(self, **kwargs: object) -> bool:
        return self.from_fact("WORKFLOW_SELF_TESTED", **kwargs)  # type: ignore[arg-type]

    def execution_started(self, **kwargs: object) -> bool:
        return self.from_fact("EXECUTION_STARTED", **kwargs)  # type: ignore[arg-type]

    def node_state_changed(self, **kwargs: object) -> bool:
        return self.from_fact("NODE_STATE_CHANGED", **kwargs)  # type: ignore[arg-type]

    def node_evaluated(self, **kwargs: object) -> bool:
        return self.from_fact("NODE_EVALUATED", **kwargs)  # type: ignore[arg-type]

    def artifact_registered(self, **kwargs: object) -> bool:
        return self.from_fact("ARTIFACT_REGISTERED", **kwargs)  # type: ignore[arg-type]

    def checkpoint_created(self, **kwargs: object) -> bool:
        return self.from_fact("CHECKPOINT_CREATED", **kwargs)  # type: ignore[arg-type]

    def workflow_repair_applied(self, **kwargs: object) -> bool:
        return self.from_fact("WORKFLOW_REPAIR_APPLIED", **kwargs)  # type: ignore[arg-type]

    def workflow_completed(self, **kwargs: object) -> bool:
        return self.from_fact("WORKFLOW_COMPLETED", **kwargs)  # type: ignore[arg-type]

    def memory_summary_updated(self, **kwargs: object) -> bool:
        return self.from_fact("MEMORY_SUMMARY_UPDATED", **kwargs)  # type: ignore[arg-type]

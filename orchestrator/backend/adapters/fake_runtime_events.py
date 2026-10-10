from __future__ import annotations

from dataclasses import replace
from threading import RLock

from domain.runtime_events import (
    RuntimeEvent,
    RuntimeEventConflict,
    RuntimeEventNotFound,
    RuntimeObservation,
    RuntimeFact,
)


class _FakeFactsPort:
    source = ""

    def __init__(self) -> None:
        self.facts: list[RuntimeFact] = []

    def get_facts(self, tenant_id: str, execution_id: str) -> tuple[RuntimeFact, ...]:
        return tuple(
            fact for fact in self.facts
            if fact.execution_id == execution_id
            and (not fact.tenant_id or fact.tenant_id == tenant_id)
        )


class FakeN8nFactsPort(_FakeFactsPort):
    source = "WORKFLOW_ENGINE"


class FakeAgentFactsPort(_FakeFactsPort):
    source = "AGENT_RUNTIME"


class FakeArtifactFactsPort(_FakeFactsPort):
    source = "ARTIFACT_STORE"


class FakeMinIOFactsPort(FakeArtifactFactsPort):
    """P0-10 fake for the artifact-store/MinIO observation source."""


FakeN8nRuntimePort = FakeN8nFactsPort
FakeAgentRuntimePort = FakeAgentFactsPort
FakeMinioRuntimePort = FakeMinIOFactsPort


class InMemoryRuntimeEventRepository:
    def __init__(self) -> None:
        self.events: dict[str, RuntimeEvent] = {}
        self.observations: dict[str, RuntimeObservation] = {}
        self._auxiliary: dict[tuple[str, str, str, str], str] = {}
        self._lock = RLock()

    @staticmethod
    def _identity(event: RuntimeEvent) -> tuple[object, ...]:
        return (
            event.tenant_id,
            event.source,
            event.event_type,
            event.execution_id,
            event.node_id,
            event.attempt_no,
            event.external_reference,
            event.payload_hash,
            event.occurred_at,
        )

    def save_event(self, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]:
        with self._lock:
            existing = self.events.get(event.event_id)
            if existing is not None:
                if self._identity(existing) != self._identity(event):
                    raise RuntimeEventConflict("event_id is bound to different semantics")
                return existing, False
            if event.external_reference:
                auxiliary = (
                    event.tenant_id,
                    event.source,
                    event.external_reference,
                    event.payload_hash,
                )
                duplicate_id = self._auxiliary.get(auxiliary)
                if duplicate_id is not None:
                    return self.events[duplicate_id], False
                self._auxiliary[auxiliary] = event.event_id
            self.events[event.event_id] = event
            return event, True

    def get_event(self, tenant_id: str, event_id: str) -> RuntimeEvent:
        with self._lock:
            event = self.events.get(event_id)
            if event is None or event.tenant_id != tenant_id:
                raise RuntimeEventNotFound(f"Runtime Event not found: {event_id}")
            return event

    def update_event_processing(
        self,
        tenant_id: str,
        event_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_retry: bool = False,
    ) -> RuntimeEvent:
        with self._lock:
            event = self.get_event(tenant_id, event_id)
            changed = event.with_processing(
                status,
                error_code=error_code,
                error_message=error_message,
                increment_retry=increment_retry,
            )
            self.events[event_id] = changed
            return changed

    def save_observation(
        self, observation: RuntimeObservation
    ) -> tuple[RuntimeObservation, bool]:
        with self._lock:
            existing = self.observations.get(observation.event_id)
            if existing is not None:
                if existing != observation:
                    raise RuntimeEventConflict(
                        "event_id is bound to a different Runtime Observation"
                    )
                return existing, False
            self.observations[observation.event_id] = observation
            return observation, True

    def list_events(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self.events.values()
                        if item.tenant_id == tenant_id
                        and item.execution_id == execution_id
                    ),
                    key=lambda item: (item.occurred_at, item.received_at, item.event_id),
                )
            )

    def list_observations(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeObservation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self.observations.values()
                        if item.tenant_id == tenant_id
                        and item.execution_id == execution_id
                    ),
                    key=lambda item: (item.occurred_at, item.created_at, item.observation_id),
                )
            )

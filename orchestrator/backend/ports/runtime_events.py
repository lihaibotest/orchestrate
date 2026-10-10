from __future__ import annotations

from typing import Protocol

from domain.runtime_events import RuntimeEvent, RuntimeObservation, RuntimeFact


class RuntimeEventPort(Protocol):
    def save_event(self, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]: ...

    def get_event(self, tenant_id: str, event_id: str) -> RuntimeEvent: ...

    def update_event_processing(
        self,
        tenant_id: str,
        event_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_retry: bool = False,
    ) -> RuntimeEvent: ...

    def save_observation(
        self, observation: RuntimeObservation
    ) -> tuple[RuntimeObservation, bool]: ...

    def list_events(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeEvent, ...]: ...

    def list_observations(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeObservation, ...]: ...


class RuntimeEventRecoveryPort(Protocol):
    def poll(
        self, tenant_id: str, execution_id: str
    ) -> tuple[RuntimeEvent, ...]: ...


class RuntimeFactsPort(Protocol):
    source: str

    def get_facts(self, tenant_id: str, execution_id: str) -> tuple[RuntimeFact, ...]: ...

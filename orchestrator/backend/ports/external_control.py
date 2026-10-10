from __future__ import annotations

from typing import Protocol


class ExternalControlPort(Protocol):
    gateway: object

    def require_v2_gateway(self) -> object: ...

    def registry(self) -> dict[str, object]: ...

    def generate_spec(self, payload: dict[str, object]) -> dict[str, object]: ...

    def generate_reasoner_proposal(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]: ...

    def repair_spec(self, payload: dict[str, object]) -> dict[str, object]: ...

    def resume(self, payload: dict[str, object]) -> None: ...

    def send_workflow_escalation(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]: ...

    def send_workflow_final_summary(
        self, payload: dict[str, object], *, operation_key: str
    ) -> dict[str, object]: ...

    def probe(self) -> bool: ...

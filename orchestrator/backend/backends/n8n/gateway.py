from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

from contracts.external_control import N8nGatewayCapabilities


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def request_with_headers(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]: ...


class N8nGateway:
    def __init__(self, transport: JsonTransport) -> None:
        self.transport = transport

    @staticmethod
    def _require_operation_key(operation_key: str) -> str:
        normalized = operation_key.strip()
        if not normalized:
            raise ValueError("operation_key is required for mutating gateway operations")
        return normalized

    def _mutate(
        self,
        path: str,
        payload: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        key = self._require_operation_key(operation_key)
        return self.transport.request_with_headers(
            "POST",
            path,
            payload,
            {"Idempotency-Key": key},
        )

    def capabilities(self) -> N8nGatewayCapabilities:
        document = self.transport.request("GET", "/api/v2/orchestrator/capabilities")
        return N8nGatewayCapabilities.model_validate(document)

    def require_v2_ready(self) -> N8nGatewayCapabilities:
        return self.capabilities().require_v2_ready()

    def registry_v2(self) -> dict[str, object]:
        return self.transport.request("GET", "/api/v2/orchestrator/registry")

    def validate(self, artifact: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self._mutate(
            "/api/v2/orchestrator/workflows/validate",
            {"workflow": artifact},
            operation_key=operation_key,
        )

    def create_inactive(self, artifact: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self._mutate(
            "/api/v2/orchestrator/workflows",
            {"workflow": artifact, "active": False},
            operation_key=operation_key,
        )

    def canary(self, workflow_id: str, *, operation_key: str) -> dict[str, object]:
        return self._mutate(
            f"/api/v2/orchestrator/workflows/{quote(workflow_id, safe='')}/canary",
            {},
            operation_key=operation_key,
        )

    def activate(self, workflow_id: str, *, operation_key: str) -> dict[str, object]:
        return self._mutate(
            f"/api/v2/orchestrator/workflows/{quote(workflow_id, safe='')}/activate",
            {},
            operation_key=operation_key,
        )

    def automatic_start(
        self,
        workflow_id: str,
        payload: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        return self._mutate(
            f"/api/v2/orchestrator/workflows/{quote(workflow_id, safe='')}/start",
            payload,
            operation_key=operation_key,
        )

    def resume(
        self,
        execution_id: str,
        payload: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        return self._mutate(
            f"/api/v2/orchestrator/executions/{quote(execution_id, safe='')}/resume",
            payload,
            operation_key=operation_key,
        )

    def status(self, execution_id: str) -> dict[str, object]:
        return self.transport.request(
            "GET",
            f"/api/v2/orchestrator/executions/{quote(execution_id, safe='')}",
        )

    def retire(self, workflow_id: str, *, operation_key: str) -> dict[str, object]:
        return self._mutate(
            f"/api/v2/orchestrator/workflows/{quote(workflow_id, safe='')}/retire",
            {},
            operation_key=operation_key,
        )

    def live_references(self, workflow_id: str) -> dict[str, object]:
        return self.transport.request(
            "GET",
            f"/api/v2/orchestrator/workflows/{quote(workflow_id, safe='')}/live-references",
        )

    def deliver_runtime_event(
        self,
        event: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        return self._mutate(
            "/api/v2/orchestrator/runtime-events",
            event,
            operation_key=operation_key,
        )

    def lookup_operation(self, operation_key: str) -> dict[str, object]:
        key = self._require_operation_key(operation_key)
        return self.transport.request(
            "GET",
            f"/api/v2/orchestrator/operations/{quote(key, safe='')}",
        )

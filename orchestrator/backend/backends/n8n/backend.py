from __future__ import annotations


class N8nExecutionBackend:
    """Thin ExecutionBackend mapping to the versioned n8n gateway contract."""

    def __init__(self, gateway: object) -> None:
        self.gateway = gateway

    def validate(self, artifact: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self.gateway.validate(artifact, operation_key=operation_key)

    def publish(self, artifact: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self.gateway.create_inactive(artifact, operation_key=operation_key)

    def activate(self, artifact_id: str, *, operation_key: str) -> dict[str, object]:
        return self.gateway.activate(artifact_id, operation_key=operation_key)

    def canary(self, artifact_id: str, *, operation_key: str) -> dict[str, object]:
        return self.gateway.canary(artifact_id, operation_key=operation_key)

    def start(self, artifact_id: str, payload: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self.gateway.automatic_start(artifact_id, payload, operation_key=operation_key)

    def resume(self, execution_id: str, payload: dict[str, object], *, operation_key: str) -> dict[str, object]:
        return self.gateway.resume(execution_id, payload, operation_key=operation_key)

    def status(self, execution_id: str) -> dict[str, object]:
        return self.gateway.status(execution_id)

    def lookup_operation(self, operation_key: str) -> dict[str, object]:
        return self.gateway.lookup_operation(operation_key)

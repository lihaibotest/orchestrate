from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GATEWAY_API_VERSION = "orchestrator-gateway.v1"
REQUIRED_GATEWAY_OPERATIONS = (
    "registry_v2",
    "validate",
    "create_inactive",
    "canary",
    "activate",
    "automatic_start",
    "resume",
    "status",
    "retire",
    "live_reference_check",
    "signed_runtime_event_delivery",
    "operation_idempotency",
    "operation_lookup",
)


class GatewayNotReady(RuntimeError):
    """The external gateway cannot satisfy the complete v2 control contract."""


class GatewayOperationCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    api_version: str = Field(min_length=1, max_length=64)


class N8nGatewayCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["n8n-gateway-capabilities.v1"]
    gateway_id: str = Field(min_length=1, max_length=128)
    api_version: str = Field(min_length=1, max_length=64)
    authentication: dict[str, str] = Field(default_factory=lambda: {"scheme": "bearer", "control_plane": "required"})
    operations: dict[str, GatewayOperationCapability]

    @model_validator(mode="after")
    def require_exact_operations(self) -> "N8nGatewayCapabilities":
        required = set(REQUIRED_GATEWAY_OPERATIONS)
        actual = set(self.operations)
        missing = sorted(required - actual)
        unexpected = sorted(actual - required)
        if missing or unexpected:
            raise ValueError(f"gateway operations mismatch: missing={missing}, unexpected={unexpected}")
        return self

    def require_v2_ready(self) -> "N8nGatewayCapabilities":
        if self.api_version != GATEWAY_API_VERSION:
            raise GatewayNotReady(
                f"gateway API version {self.api_version!r} is incompatible with {GATEWAY_API_VERSION!r}"
            )
        for name in REQUIRED_GATEWAY_OPERATIONS:
            operation = self.operations[name]
            if not operation.available:
                raise GatewayNotReady(f"gateway operation is unavailable: {name}")
            if operation.api_version != GATEWAY_API_VERSION:
                raise GatewayNotReady(
                    f"gateway operation {name} uses incompatible API version {operation.api_version!r}"
                )
        return self


GatewayRuntimeOperation = Literal["authorize", "runtime_event"]


class GatewayCredentialBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_reference: str = Field(min_length=1, max_length=256)
    workflow_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    allowed_operations: list[GatewayRuntimeOperation] = Field(min_length=1)

    def allows(
        self,
        *,
        credential_reference: str,
        workflow_id: str,
        session_id: str,
        node_id: str,
        operation: GatewayRuntimeOperation,
    ) -> bool:
        return (
            self.credential_reference == credential_reference
            and self.workflow_id == workflow_id
            and self.session_id == session_id
            and self.node_id == node_id
            and operation in self.allowed_operations
        )

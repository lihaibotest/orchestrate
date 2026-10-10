from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
JsonSchema = dict[str, object]


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentDescriptorV2(FrozenStrictModel):
    agent_id: Identifier
    role: Literal["business", "evaluator"]
    capabilities: tuple[Identifier, ...] = Field(min_length=1)
    health: Literal["healthy", "degraded", "unhealthy"]
    endpoint: str = Field(min_length=1, max_length=2048)
    input_schema: JsonSchema
    output_schema: JsonSchema
    contract_version: Identifier
    n8n_credential_ref: str = Field(min_length=1, max_length=256)
    dry_run_supported: bool
    side_effect: Literal[
        "read_only",
        "idempotent_write",
        "non_idempotent_write",
        "external_transaction",
    ]
    idempotency_supported: bool
    usage_reporting: bool
    security_scopes: tuple[Identifier, ...]
    backends: tuple[Identifier, ...] = Field(min_length=1)
    cost_microusd: Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
    latency_ms: Annotated[StrictInt, Field(ge=0, le=86400000)]


class CapabilityRegistryV2(FrozenStrictModel):
    schema_version: Literal["capability-registry.v2"]
    snapshot_id: Identifier
    snapshot_version: Annotated[StrictInt, Field(ge=1)]
    candidates: tuple[AgentDescriptorV2, ...] = Field(min_length=1, max_length=1024)


class CapabilityRequirement(FrozenStrictModel):
    capability: Identifier
    input_schema: JsonSchema
    output_schema: JsonSchema
    security_scopes: tuple[Identifier, ...]
    backend: Identifier
    side_effect: Literal["read_only", "idempotent_write"]
    canary: bool
    require_usage_reporting: bool

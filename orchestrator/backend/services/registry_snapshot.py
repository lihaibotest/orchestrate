from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def build_v2_registry_snapshot(payload, *, endpoint_policy):
    """Explicit adapter; v1 candidate parsing remains unchanged."""
    from capabilities.models import CapabilityRegistryV2
    from capabilities.registry import build_registry_snapshot

    return build_registry_snapshot(
        CapabilityRegistryV2.model_validate(payload),
        endpoint_policy=endpoint_policy,
    )


class RegistryCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    role: Literal["business", "evaluator"]
    capabilities: list[str] = Field(min_length=1)
    health: Literal["healthy", "degraded", "disabled"]
    endpoint: str = Field(min_length=1)
    input_contract_version: str = Field(min_length=1)
    output_contract_version: str = Field(min_length=1)
    n8n_credential_ref: str = Field(min_length=1)
    dry_run_supported: bool


def optional_evaluator(
    candidates: list[RegistryCandidateSnapshot], capability: str = "evaluate-workflow"
) -> RegistryCandidateSnapshot | None:
    eligible = sorted(
        [candidate for candidate in candidates if candidate.role == "evaluator" and candidate.health == "healthy" and capability in candidate.capabilities],
        key=lambda candidate: candidate.agent_id,
    )
    if not eligible:
        return None
    if len(eligible) > 1 and len({tuple(candidate.capabilities) for candidate in eligible}) > 1:
        raise ValueError("ambiguous evaluator candidates")
    return eligible[0]


def resolve_evaluator(candidates: list[RegistryCandidateSnapshot], capability: str = "evaluate-workflow") -> RegistryCandidateSnapshot:
    evaluator = optional_evaluator(candidates, capability)
    if evaluator is None:
        raise ValueError("no healthy evaluator candidate")
    return evaluator


def resolve_agent(candidates: list[RegistryCandidateSnapshot], agent_id: str, capability: str, *, run_mode: str) -> RegistryCandidateSnapshot:
    eligible = [candidate for candidate in candidates if candidate.agent_id == agent_id and candidate.role == "business" and candidate.health == "healthy" and capability in candidate.capabilities]
    if not eligible:
        raise ValueError(f"no healthy business candidate for {agent_id}/{capability}")
    candidate = eligible[0]
    if run_mode == "canary" and not candidate.dry_run_supported:
        raise ValueError(f"business candidate {agent_id} does not support dry_run")
    return candidate

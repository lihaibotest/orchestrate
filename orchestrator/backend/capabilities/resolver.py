from __future__ import annotations

from dataclasses import dataclass
import json

from capabilities.models import AgentDescriptorV2, CapabilityRequirement
from capabilities.ranking import RankingWeights
from capabilities.registry import RegistrySnapshot, RegistryValidationError


@dataclass(frozen=True)
class RejectedCandidate:
    agent_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BindingDecision:
    snapshot_id: str
    snapshot_hash: str
    selected: AgentDescriptorV2
    rejected: tuple[RejectedCandidate, ...]


def _properties(schema: dict[str, object]) -> dict[str, object]:
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _required(schema: dict[str, object]) -> set[str]:
    required = schema.get("required", [])
    return {str(item) for item in required} if isinstance(required, list) else set()


def _same_property_types(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("type") == right.get("type")


def input_schema_compatible(available: dict[str, object], accepted: dict[str, object]) -> bool:
    if available.get("type") != accepted.get("type"):
        return False
    available_properties = _properties(available)
    accepted_properties = _properties(accepted)
    if not _required(accepted) <= set(available_properties):
        return False
    return all(
        name in available_properties and _same_property_types(available_properties[name], schema)
        for name, schema in accepted_properties.items()
        if name in _required(accepted)
    )


def output_schema_compatible(produced: dict[str, object], expected: dict[str, object]) -> bool:
    if produced.get("type") != expected.get("type"):
        return False
    produced_properties = _properties(produced)
    expected_properties = _properties(expected)
    if not _required(expected) <= set(produced_properties):
        return False
    return all(
        name in produced_properties and _same_property_types(produced_properties[name], schema)
        for name, schema in expected_properties.items()
        if name in _required(expected)
    )


def _rejection_reasons(candidate: AgentDescriptorV2, requirement: CapabilityRequirement) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.health != "healthy":
        reasons.append("health")
    if requirement.capability not in candidate.capabilities:
        reasons.append("capability")
    if not input_schema_compatible(requirement.input_schema, candidate.input_schema):
        reasons.append("input_schema")
    if not output_schema_compatible(candidate.output_schema, requirement.output_schema):
        reasons.append("output_schema")
    if not set(requirement.security_scopes) <= set(candidate.security_scopes):
        reasons.append("security_scopes")
    if requirement.backend not in candidate.backends:
        reasons.append("backend")
    if candidate.side_effect != requirement.side_effect:
        reasons.append("side_effect")
    if requirement.side_effect == "idempotent_write" and not candidate.idempotency_supported:
        reasons.append("idempotency_supported")
    if requirement.canary and not candidate.dry_run_supported:
        reasons.append("dry_run_supported")
    if requirement.require_usage_reporting and not candidate.usage_reporting:
        reasons.append("usage_reporting")
    return tuple(reasons)


def resolve_capability(
    snapshot: RegistrySnapshot,
    requirement: CapabilityRequirement,
    *,
    weights: RankingWeights,
) -> BindingDecision:
    accepted: list[AgentDescriptorV2] = []
    rejected: list[RejectedCandidate] = []
    for candidate in snapshot.candidates:
        if candidate.role != "business":
            continue
        reasons = _rejection_reasons(candidate, requirement)
        if reasons:
            rejected.append(RejectedCandidate(candidate.agent_id, reasons))
        else:
            accepted.append(candidate)
    if not accepted:
        explanation = json.dumps(
            {item.agent_id: item.reasons for item in rejected},
            sort_keys=True,
        )
        raise RegistryValidationError(f"no compatible candidate: {explanation}")
    selected = min(accepted, key=weights.key)
    rejected.extend(
        RejectedCandidate(candidate.agent_id, ("ranking",))
        for candidate in accepted
        if candidate.agent_id != selected.agent_id
    )
    return BindingDecision(
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.snapshot_hash,
        selected=selected,
        rejected=tuple(sorted(rejected, key=lambda item: item.agent_id)),
    )

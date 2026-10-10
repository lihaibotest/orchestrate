from __future__ import annotations

import socket
from urllib.parse import urlsplit

from capabilities.models import CapabilityRegistryV2
from domain.agent_preparation import AgentCandidate, AgentRequirement


class GatewayAgentRegistryRuntimeAdapter:
    """Production P0-04 adapter over the versioned registry plus endpoint probe."""

    def __init__(self, gateway: object, *, connect_timeout_seconds: float = 3.0) -> None:
        self.gateway = gateway
        self.connect_timeout_seconds = connect_timeout_seconds
        self._health: dict[tuple[str, str], str] = {}

    def find_candidates(
        self, requirements: tuple[AgentRequirement, ...]
    ) -> tuple[AgentCandidate, ...]:
        loader = getattr(self.gateway, "registry_v2", None)
        if not callable(loader):
            raise RuntimeError("gateway does not expose registry_v2")
        registry = CapabilityRegistryV2.model_validate(loader())
        requested = {item.capability for item in requirements}
        candidates: list[AgentCandidate] = []
        for item in registry.candidates:
            if not requested.intersection(item.capabilities):
                continue
            identity = (item.agent_id, item.contract_version)
            self._health[identity] = item.health
            candidates.append(
                AgentCandidate(
                    item.agent_id,
                    item.contract_version,
                    tuple(item.capabilities),
                    item.endpoint,
                    item.input_schema,
                    item.output_schema,
                    item.n8n_credential_ref,
                    {
                        "side_effect": item.side_effect,
                        "idempotency_supported": item.idempotency_supported,
                        "dry_run_supported": item.dry_run_supported,
                        "security_scopes": list(item.security_scopes),
                    },
                    registered=item.health != "unhealthy",
                )
            )
        return tuple(candidates)

    def start(self, candidate: AgentCandidate) -> bool:
        # The registry owns lifecycle for managed agents; registration is the
        # explicit start/attach acknowledgement until a Runtime API is frozen.
        return candidate.registered

    def health(self, candidate: AgentCandidate) -> bool:
        return self._health.get(candidate.identity) == "healthy"

    def readiness(self, candidate: AgentCandidate) -> bool:
        return self.health(candidate) and bool(candidate.credential_ref)

    def reachability(self, candidate: AgentCandidate) -> bool:
        parsed = urlsplit(candidate.endpoint)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection(
                (parsed.hostname, port), timeout=self.connect_timeout_seconds
            ):
                return True
        except OSError:
            return False

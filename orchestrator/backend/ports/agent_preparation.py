from __future__ import annotations

from typing import Protocol

from domain.agent_preparation import (
    AgentCandidate,
    AgentRequirement,
    RegistrySnapshot,
)


class AgentRegistryPort(Protocol):
    def find_candidates(
        self, requirements: tuple[AgentRequirement, ...]
    ) -> tuple[AgentCandidate, ...]: ...


class AgentRuntimePort(Protocol):
    def start(self, candidate: AgentCandidate) -> bool: ...
    def health(self, candidate: AgentCandidate) -> bool: ...
    def readiness(self, candidate: AgentCandidate) -> bool: ...
    def reachability(self, candidate: AgentCandidate) -> bool: ...


class RegistrySnapshotRepositoryPort(Protocol):
    def save_registry_snapshot(self, snapshot: RegistrySnapshot) -> RegistrySnapshot: ...
    def get_registry_snapshot(self, snapshot_id: str) -> RegistrySnapshot: ...

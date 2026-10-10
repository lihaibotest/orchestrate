from __future__ import annotations

from dataclasses import dataclass

from domain.agent_preparation import (
    AgentCandidate,
    AgentRequirement,
    RegistrySnapshot,
    SnapshotCreationFailed,
)


class FakeAgentRegistryAdapter:
    def __init__(
        self,
        candidates: tuple[AgentCandidate, ...] = (),
        *,
        failure: Exception | None = None,
    ) -> None:
        self.candidates = candidates
        self.failure = failure
        self.queries: list[tuple[AgentRequirement, ...]] = []

    def find_candidates(
        self, requirements: tuple[AgentRequirement, ...]
    ) -> tuple[AgentCandidate, ...]:
        self.queries.append(requirements)
        if self.failure is not None:
            raise self.failure
        return self.candidates


@dataclass(frozen=True)
class FakeAgentRuntimeState:
    start_ok: bool = True
    health_ok: bool = True
    readiness_ok: bool = True
    reachability_ok: bool = True


class FakeAgentRuntimeAdapter:
    def __init__(
        self,
        states: dict[tuple[str, str], FakeAgentRuntimeState] | None = None,
    ) -> None:
        self.states = dict(states or {})
        self.calls: list[tuple[str, str, str]] = []

    def _state(self, candidate: AgentCandidate) -> FakeAgentRuntimeState:
        return self.states.get(candidate.identity, FakeAgentRuntimeState())

    def start(self, candidate: AgentCandidate) -> bool:
        self.calls.append(("start", candidate.agent_id, candidate.version))
        return self._state(candidate).start_ok

    def health(self, candidate: AgentCandidate) -> bool:
        self.calls.append(("health", candidate.agent_id, candidate.version))
        return self._state(candidate).health_ok

    def readiness(self, candidate: AgentCandidate) -> bool:
        self.calls.append(("readiness", candidate.agent_id, candidate.version))
        return self._state(candidate).readiness_ok

    def reachability(self, candidate: AgentCandidate) -> bool:
        self.calls.append(("reachability", candidate.agent_id, candidate.version))
        return self._state(candidate).reachability_ok


class InMemoryRegistrySnapshotRepository:
    def __init__(self) -> None:
        self.snapshots: dict[str, RegistrySnapshot] = {}
        self._by_content: dict[tuple[str, str], RegistrySnapshot] = {}

    def save_registry_snapshot(self, snapshot: RegistrySnapshot) -> RegistrySnapshot:
        content_key = (snapshot.workflow_family_id, snapshot.snapshot_hash)
        existing_content = self._by_content.get(content_key)
        if existing_content is not None:
            if existing_content.content_payload() != snapshot.content_payload():
                raise SnapshotCreationFailed("Registry Snapshot hash collision")
            return existing_content
        existing_id = self.snapshots.get(snapshot.snapshot_id)
        if existing_id is not None and existing_id != snapshot:
            raise SnapshotCreationFailed("snapshot_id is already bound to different content")
        self.snapshots[snapshot.snapshot_id] = snapshot
        self._by_content[content_key] = snapshot
        return snapshot

    def get_registry_snapshot(self, snapshot_id: str) -> RegistrySnapshot:
        try:
            return self.snapshots[snapshot_id]
        except KeyError as error:
            raise KeyError(f"Registry Snapshot not found: {snapshot_id}") from error

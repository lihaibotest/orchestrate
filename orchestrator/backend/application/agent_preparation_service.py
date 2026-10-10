from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from domain.agent_preparation import (
    AgentCandidate,
    AgentNotFound,
    AgentNotHealthy,
    AgentNotReady,
    AgentPreparationError,
    AgentRegistrationFailed,
    AgentRequirement,
    AgentStartFailed,
    AgentUnreachable,
    CandidateRejection,
    InvalidAgentRequirement,
    PreparationResult,
    SnapshotCreationFailed,
    binding_from_candidate,
    build_registry_snapshot,
    validate_candidate_contract,
)
from ports.agent_preparation import (
    AgentRegistryPort,
    AgentRuntimePort,
    RegistrySnapshotRepositoryPort,
)


Clock = Callable[[], datetime]
SnapshotIdFactory = Callable[[], str]


class AgentPreparationService:
    def __init__(
        self,
        registry: AgentRegistryPort,
        runtime: AgentRuntimePort,
        snapshots: RegistrySnapshotRepositoryPort,
        *,
        clock: Clock | None = None,
        snapshot_id_factory: SnapshotIdFactory | None = None,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.snapshots = snapshots
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.snapshot_id_factory = snapshot_id_factory or (
            lambda: f"registry-snapshot-{uuid4()}"
        )

    def prepare_agents(
        self,
        workflow_family_id: str,
        requirements: tuple[AgentRequirement, ...],
    ) -> PreparationResult:
        if not requirements:
            raise InvalidAgentRequirement("at least one Agent requirement is required")
        requirement_ids = [item.requirement_id for item in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise InvalidAgentRequirement("Agent requirement IDs must be unique")
        ordered_requirements = tuple(sorted(requirements, key=lambda item: item.requirement_id))
        try:
            candidates = self.registry.find_candidates(ordered_requirements)
        except AgentPreparationError:
            raise
        except Exception as error:
            raise AgentRegistrationFailed("Agent Registry query failed") from error

        prepared: dict[tuple[str, str], AgentPreparationError | None] = {}
        bindings = []
        rejections: list[CandidateRejection] = []
        for requirement in ordered_requirements:
            matches = sorted(
                (
                    candidate
                    for candidate in candidates
                    if requirement.capability in candidate.capabilities
                ),
                key=lambda item: item.identity,
            )
            if not matches:
                raise AgentNotFound(
                    f"no candidate provides capability {requirement.capability}"
                )
            registered = [candidate for candidate in matches if candidate.registered]
            if not registered:
                raise AgentRegistrationFailed(
                    f"no registered candidate provides capability {requirement.capability}"
                )

            selected: AgentCandidate | None = None
            last_error: AgentPreparationError | None = None
            for candidate in registered:
                state = prepared.get(candidate.identity)
                if candidate.identity not in prepared:
                    try:
                        self._prepare_candidate(candidate)
                    except AgentPreparationError as error:
                        state = error
                    else:
                        state = None
                    prepared[candidate.identity] = state
                if state is not None:
                    last_error = state
                    rejections.append(
                        CandidateRejection(
                            candidate.agent_id,
                            candidate.version,
                            state.error_code,
                        )
                    )
                    continue
                selected = candidate
                break
            if selected is None:
                if last_error is not None:
                    raise last_error
                raise AgentNotFound(
                    f"no eligible candidate provides capability {requirement.capability}"
                )
            bindings.append(binding_from_candidate(requirement, selected))

        try:
            snapshot = build_registry_snapshot(
                workflow_family_id,
                tuple(bindings),
                snapshot_id=self.snapshot_id_factory(),
                created_at=self.clock(),
            )
            saved = self.snapshots.save_registry_snapshot(snapshot)
        except AgentPreparationError:
            raise
        except Exception as error:
            raise SnapshotCreationFailed("Registry Snapshot persistence failed") from error
        if (
            saved.workflow_family_id != snapshot.workflow_family_id
            or saved.snapshot_hash != snapshot.snapshot_hash
        ):
            raise SnapshotCreationFailed("persisted Registry Snapshot identity mismatch")
        return PreparationResult(snapshot=saved, rejections=tuple(rejections))

    def _prepare_candidate(self, candidate: AgentCandidate) -> None:
        try:
            started = self.runtime.start(candidate)
        except Exception as error:
            raise AgentStartFailed(f"Agent start failed: {candidate.agent_id}") from error
        if not started:
            raise AgentStartFailed(f"Agent start failed: {candidate.agent_id}")
        try:
            healthy = self.runtime.health(candidate)
        except Exception as error:
            raise AgentNotHealthy(f"Agent health check failed: {candidate.agent_id}") from error
        if not healthy:
            raise AgentNotHealthy(f"Agent is unhealthy: {candidate.agent_id}")
        try:
            ready = self.runtime.readiness(candidate)
        except Exception as error:
            raise AgentNotReady(f"Agent readiness check failed: {candidate.agent_id}") from error
        if not ready:
            raise AgentNotReady(f"Agent is not ready: {candidate.agent_id}")
        try:
            reachable = self.runtime.reachability(candidate)
        except Exception as error:
            raise AgentUnreachable(f"Agent reachability check failed: {candidate.agent_id}") from error
        if not reachable:
            raise AgentUnreachable(f"Agent is unreachable: {candidate.agent_id}")
        validate_candidate_contract(candidate)

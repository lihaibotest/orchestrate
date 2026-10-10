from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from types import MappingProxyType
from typing import Mapping


class StateMachineError(ValueError):
    """Base error for rejected domain state changes."""


class UnknownStateError(StateMachineError):
    def __init__(self, aggregate_type: str, state: str) -> None:
        self.aggregate_type = aggregate_type
        self.state = state
        super().__init__(f"unknown {aggregate_type} state: {state}")


class InvalidTransitionError(StateMachineError):
    def __init__(self, aggregate_type: str, from_state: str, to_state: str) -> None:
        self.aggregate_type = aggregate_type
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"invalid {aggregate_type} transition: {from_state} -> {to_state}"
        )


class AggregateNotFoundError(StateMachineError):
    def __init__(self, aggregate_type: str, aggregate_id: str) -> None:
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        super().__init__(f"{aggregate_type} not found: {aggregate_id}")


@dataclass(frozen=True)
class StateTransition:
    aggregate_type: str
    aggregate_id: str
    from_state: str
    to_state: str
    reason: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    transition_id: int | None = None
    created_at: datetime | None = None


def workflow_version_aggregate_id(workflow_family_id: str, version: int) -> str:
    return json.dumps([workflow_family_id, version], separators=(",", ":"))


def node_aggregate_id(execution_id: str, node_id: str) -> str:
    return json.dumps([execution_id, node_id], separators=(",", ":"))


class StateMachine:
    def __init__(
        self,
        aggregate_type: str,
        transitions: Mapping[str, frozenset[str]],
    ) -> None:
        states = set(transitions)
        destinations = set().union(*transitions.values()) if transitions else set()
        missing = destinations - states
        if missing:
            raise ValueError(f"state matrix has undefined destinations: {sorted(missing)}")
        self.aggregate_type = aggregate_type
        self._transitions = MappingProxyType(dict(transitions))

    @property
    def transitions(self) -> Mapping[str, frozenset[str]]:
        return self._transitions

    @property
    def terminal_states(self) -> frozenset[str]:
        return frozenset(
            state for state, destinations in self._transitions.items() if not destinations
        )

    def can_transition(self, from_state: str, to_state: str) -> bool:
        self._require_state(from_state)
        self._require_state(to_state)
        return to_state in self._transitions[from_state]

    def is_terminal(self, state: str) -> bool:
        self._require_state(state)
        return not self._transitions[state]

    def transition(
        self,
        aggregate_id: str,
        from_state: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        if not aggregate_id:
            raise ValueError("aggregate_id must not be empty")
        if not self.can_transition(from_state, to_state):
            raise InvalidTransitionError(self.aggregate_type, from_state, to_state)
        return StateTransition(
            aggregate_type=self.aggregate_type,
            aggregate_id=aggregate_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            metadata=dict(metadata or {}),
        )

    def _require_state(self, state: str) -> None:
        if state not in self._transitions:
            raise UnknownStateError(self.aggregate_type, state)


WORKFLOW_VERSION_STATE_MACHINE = StateMachine(
    "WORKFLOW_VERSION",
    {
        "DRAFT": frozenset({"VALIDATING"}),
        "VALIDATING": frozenset({"SELF_TESTING", "SELF_TEST_FAILED"}),
        "SELF_TESTING": frozenset({"VERIFIED", "SELF_TEST_FAILED"}),
        "VERIFIED": frozenset({"PUBLISHED"}),
        "PUBLISHED": frozenset(),
        "SELF_TEST_FAILED": frozenset({"SELF_TESTING"}),
    },
)

EXECUTION_STATE_MACHINE = StateMachine(
    "EXECUTION",
    {
        "CREATED": frozenset({"RUNNING", "FAILED"}),
        "RUNNING": frozenset({"SUCCEEDED", "FAILED", "PATCHING", "REPLANNING"}),
        "PATCHING": frozenset({"RUNNING", "FAILED"}),
        "REPLANNING": frozenset({"RUNNING", "FAILED"}),
        "SUCCEEDED": frozenset(),
        "FAILED": frozenset(),
    },
)

NODE_STATE_MACHINE = StateMachine(
    "NODE",
    {
        "PENDING": frozenset({"READY"}),
        "READY": frozenset({"RUNNING"}),
        "RUNNING": frozenset({"SUCCEEDED", "RETRY_WAIT", "FAILED"}),
        "RETRY_WAIT": frozenset({"RUNNING", "FAILED"}),
        "SUCCEEDED": frozenset({"EVALUATED", "INVALIDATED"}),
        "EVALUATED": frozenset({"INVALIDATED"}),
        "FAILED": frozenset(),
        "INVALIDATED": frozenset(),
    },
)

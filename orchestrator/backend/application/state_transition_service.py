from __future__ import annotations

import logging
from typing import Mapping

from domain.state_machines import StateMachineError, StateTransition
from ports.control_plane_repository import ControlPlaneRepository


logger = logging.getLogger(__name__)


class StateTransitionService:
    """Application boundary for explicit, audited aggregate state changes."""

    def __init__(self, repository: ControlPlaneRepository, context_bridge: object | None = None) -> None:
        self.repository = repository
        self.context_bridge = context_bridge

    def _emit(self, event_type: str, *, workflow_family_id: str = "", workflow_version: int = 1,
              execution_id: str | None = None, node_id: str | None = None,
              tenant_id: str = "", correlation_id: str = "", payload: dict[str, object] | None = None) -> None:
        bridge = self.context_bridge
        if bridge is None or not tenant_id or not correlation_id or not workflow_family_id:
            return
        emit = getattr(bridge, "from_fact", None)
        if callable(emit):
            emit(event_type, tenant_id=tenant_id, correlation_id=correlation_id,
                 workflow_family_id=workflow_family_id, workflow_version=workflow_version,
                 execution_id=execution_id, node_id=node_id, payload=payload or {})

    def transition_workflow_version(
        self,
        workflow_family_id: str,
        version: int,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        try:
            result = self.repository.transition_workflow_version(
                workflow_family_id,
                version,
                to_state,
                reason=reason,
                metadata=metadata,
            )
            return result
        except StateMachineError as error:
            self._log_rejection(
                error,
                workflow_family_id=workflow_family_id,
                workflow_version=version,
                to_state=to_state,
                reason=reason,
            )
            raise

    def transition_execution(
        self,
        execution_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        try:
            result = self.repository.transition_execution(
                execution_id,
                to_state,
                reason=reason,
                metadata=metadata,
            )
            return result
        except StateMachineError as error:
            self._log_rejection(
                error,
                execution_id=execution_id,
                to_state=to_state,
                reason=reason,
            )
            raise

    def transition_node(
        self,
        execution_id: str,
        node_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StateTransition:
        try:
            result = self.repository.transition_node(
                execution_id,
                node_id,
                to_state,
                reason=reason,
                metadata=metadata,
            )
            return result
        except StateMachineError as error:
            self._log_rejection(
                error,
                execution_id=execution_id,
                node_id=node_id,
                to_state=to_state,
                reason=reason,
            )
            raise

    @staticmethod
    def _log_rejection(error: StateMachineError, **context: object) -> None:
        values = {
            "aggregate_type": getattr(error, "aggregate_type", "UNKNOWN"),
            "from_state": getattr(error, "from_state", None),
            **context,
        }
        logger.warning("state transition rejected", extra=values)

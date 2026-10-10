from __future__ import annotations

import logging
from typing import Mapping

from domain.state_machines import StateMachineError, StateTransition
from ports.control_plane_repository import ControlPlaneRepository


logger = logging.getLogger(__name__)


class StateTransitionService:
    """Application boundary for explicit, audited aggregate state changes."""

    def __init__(self, repository: ControlPlaneRepository) -> None:
        self.repository = repository

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
            return self.repository.transition_workflow_version(
                workflow_family_id,
                version,
                to_state,
                reason=reason,
                metadata=metadata,
            )
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
            return self.repository.transition_execution(
                execution_id,
                to_state,
                reason=reason,
                metadata=metadata,
            )
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
            return self.repository.transition_node(
                execution_id,
                node_id,
                to_state,
                reason=reason,
                metadata=metadata,
            )
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

from __future__ import annotations

import hashlib

from contracts.runtime_event import RuntimeDecisionV2, RuntimeEventV2
from services.workflow_failure_classifier import WorkflowFailureClassifier


class RuntimeSupervisor:
    def decide(
        self,
        event: RuntimeEventV2,
        *,
        budget_available: bool,
        repair_attempt: int = 0,
        max_repair_attempts: int = 2,
    ) -> RuntimeDecisionV2:
        if event.event_type == "attempt_completed":
            action, reason = "CONTINUE", "ATTEMPT_COMPLETED"
        elif event.event_type != "attempt_failed":
            action, reason = "CONTINUE", "EVENT_RECORDED"
        else:
            repair = WorkflowFailureClassifier().classify(
                event,
                repair_attempt=repair_attempt,
                max_repair_attempts=max_repair_attempts,
                budget_available=budget_available,
            )
            action = {
                "retry_same_version": "RETRY",
                "repair_new_version": "PATCH",
                "escalate": "REPLAN",
                "stop": "FAIL",
            }[repair.action]
            reason = repair.reason_code
        decision_id = "decision-" + hashlib.sha256(
            f"{event.execution_id}:{event.event_id}:{action}".encode("utf-8")
        ).hexdigest()[:24]
        return RuntimeDecisionV2(
            correlation_id=event.correlation_id,
            session_id=event.session_id,
            plan_version=event.plan_version,
            workflow_version=event.workflow_version,
            idempotency_key=event.idempotency_key,
            decision_id=decision_id,
            event_id=event.event_id,
            execution_id=event.execution_id,
            node_id=event.node_id,
            action=action,
            reason_code=reason,
            route=action.lower(),
        )

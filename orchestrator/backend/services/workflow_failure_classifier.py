from __future__ import annotations

from contracts.orchestration import WorkflowRepairDecisionV1
from contracts.runtime_event import RuntimeEventV2
from services.failure_classifier import classify_failure, is_technical_failure


WORKFLOW_LOCAL_CODES = {
    "INPUT_BINDING_MISMATCH",
    "OUTPUT_BINDING_MISMATCH",
    "OUTPUT_SCHEMA_MISMATCH",
    "INVALID_NODE_DEPENDENCY",
    "CHECKPOINT_MISMATCH",
}
EXTERNAL_BOUNDARY_CODES = {
    "CAPABILITY_UNAVAILABLE",
    "EVALUATOR_METRIC_UNAVAILABLE",
    "BACKEND_FEATURE_UNAVAILABLE",
    "EXTERNAL_DEPENDENCY_UNAVAILABLE",
}


class WorkflowFailureClassifier:
    def classify(
        self,
        event: RuntimeEventV2,
        *,
        repair_attempt: int,
        max_repair_attempts: int,
        budget_available: bool,
    ) -> WorkflowRepairDecisionV1:
        failure = event.failure or {}
        error_code = str(failure.get("error_code", failure.get("code", "UNKNOWN"))).upper()
        failure_class = classify_failure(error=failure)
        max_attempts = max(1, int(failure.get("max_attempts", 1)))
        evidence_refs = [
            str(value)
            for key, value in event.checkpoint.items()
            if str(key).endswith("_ref") and isinstance(value, str)
        ][:64]

        if not budget_available:
            classification, action, reason = (
                "external_boundary",
                "escalate",
                "BUDGET_UNAVAILABLE",
            )
        elif error_code in EXTERNAL_BOUNDARY_CODES:
            classification, action, reason = "external_boundary", "escalate", error_code
        elif is_technical_failure(failure_class) and event.attempt < max_attempts:
            classification, action, reason = (
                "workflow_local",
                "retry_same_version",
                "TECHNICAL_RETRY_AVAILABLE",
            )
        elif error_code in WORKFLOW_LOCAL_CODES:
            classification, action, reason = (
                "workflow_local",
                "repair_new_version",
                error_code,
            )
        elif is_technical_failure(failure_class):
            classification, action, reason = (
                "workflow_local",
                "repair_new_version",
                "TECHNICAL_RETRY_EXHAUSTED",
            )
        else:
            classification, action, reason = (
                "uncertain",
                "repair_new_version",
                "RUNTIME_FAILURE_UNCERTAIN",
            )

        if action == "repair_new_version" and repair_attempt >= max_repair_attempts:
            classification, action, reason = (
                "uncertain",
                "escalate",
                "REPAIR_ATTEMPTS_EXHAUSTED",
            )

        return WorkflowRepairDecisionV1(
            correlation_id=event.correlation_id,
            session_id=event.session_id,
            workflow_version=event.workflow_version,
            node_id=event.node_id,
            classification=classification,
            action=action,
            reason_code=reason,
            repair_attempt=repair_attempt,
            max_repair_attempts=max_repair_attempts,
            evidence_refs=evidence_refs,
        )

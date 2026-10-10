from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from services.failure_classifier import classify_failure, is_technical_failure


@dataclass(frozen=True)
class FailureAction:
    action: str
    error_class: str
    attempt: int
    max_attempts: int
    workflow_version: int
    idempotency_key: str


def choose_failure_action(
    error: Mapping[str, object], *, workflow_version: int, idempotency_key: str
) -> FailureAction:
    """Select the only runtime transition allowed for a failed node.

    ``max_attempts`` is inclusive: the first execution is attempt one. Technical
    failures can resume the same immutable version while all other failures, or an
    exhausted technical budget, must enter evaluation.
    """
    error_class = classify_failure(error=error)
    attempt = max(1, int(error.get("attempt", 1)))
    max_attempts = max(1, int(error.get("max_attempts", 1)))
    action = "retry" if is_technical_failure(error_class) and attempt < max_attempts else "evaluate"
    next_attempt = attempt + 1 if action == "retry" else attempt
    return FailureAction(action, error_class, next_attempt, max_attempts, workflow_version, idempotency_key)

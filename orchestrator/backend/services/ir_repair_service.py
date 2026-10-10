from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable


class RepairRejected(RuntimeError):
    pass


SENSITIVE_KEYS = (
    "authorization",
    "credential",
    "secret",
    "token",
    "api_key",
    "cookie",
    "private_key",
    "endpoint",
    "stderr",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)cookie\s*:\s*[^\s]+"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)
EVALUATOR_OWNED_KEYS = {
    "evaluation",
    "evaluationid",
    "evaluator",
    "evaluatorid",
    "evaluatoropinion",
    "quality",
    "qualityscore",
    "qualityverdict",
    "recommendation",
    "verdict",
}


def contains_evaluator_owned_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in EVALUATOR_OWNED_KEYS or contains_evaluator_owned_field(nested):
                return True
    elif isinstance(value, list):
        return any(contains_evaluator_owned_field(item) for item in value)
    return False


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(marker in str(key).lower() for marker in SENSITIVE_KEYS) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS):
            return "[REDACTED]"
        return value[:8192]
    return value


@dataclass(frozen=True)
class RepairResult:
    workflow_version: int
    ir_hash: str
    proposal: dict[str, object]


class IRRepairService:
    def repair(
        self,
        *,
        current_ir_hash: str,
        current_workflow_version: int,
        failure: dict[str, object],
        runtime_facts: dict[str, object],
        runtime_history: list[dict[str, object]],
        budget: dict[str, object],
        progress: dict[str, object],
        generate: Callable[[dict[str, object]], dict[str, object]],
        build_validate_publish: Callable[[dict[str, object], int], str],
    ) -> RepairResult:
        repair_input = {
            "schema_version": "ir-repair-request.v1",
            "current_ir_hash": current_ir_hash,
            "failure": failure,
            "runtime_facts": runtime_facts,
            "runtime_history": runtime_history,
            "budget": budget,
            "progress": progress,
        }
        if contains_evaluator_owned_field(repair_input):
            raise RepairRejected("repair input cannot contain evaluator-owned fields")
        request = redact(repair_input)
        assert isinstance(request, dict)
        proposal = generate(request)
        if not proposal:
            raise RepairRejected("repair proposal is not materially changed")
        next_version = current_workflow_version + 1
        ir_hash = build_validate_publish(proposal, next_version)
        if ir_hash == current_ir_hash:
            raise RepairRejected("repair IR is not materially changed")
        return RepairResult(next_version, ir_hash, proposal)

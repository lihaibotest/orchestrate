from __future__ import annotations

from contracts.execution_ir_v2 import ExecutionIRV2
from verification.types import ValidationIssue


def validate_policy(ir: ExecutionIRV2, bindings: dict[str, object]) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for node in ir.nodes:
        if node.kind != "invoke":
            continue
        if node.side_effect not in {"read_only", "idempotent_write"}:
            issues.append(ValidationIssue("UNSAFE_SIDE_EFFECT", f"nodes.{node.node_id}", "side effect is not allowed in v2"))
        decision = bindings.get(node.task_id or "")
        selected = getattr(decision, "selected", None)
        if node.side_effect == "idempotent_write" and selected is not None and not selected.idempotency_supported:
            issues.append(ValidationIssue("IDEMPOTENCY_UNSUPPORTED", f"nodes.{node.node_id}", "selected agent lacks idempotency"))
    return tuple(issues)


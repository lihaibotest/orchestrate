from __future__ import annotations

from contracts.execution_ir_v2 import ExecutionIRV2
from verification.types import ValidationIssue


def validate_loops(ir: ExecutionIRV2) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for node in ir.nodes:
        if node.kind != "bounded_loop" or node.loop is None:
            continue
        loop = node.loop
        path = f"nodes.{node.node_id}.loop"
        if loop.max_iterations < 1:
            issues.append(ValidationIssue("UNBOUNDED_LOOP", path, "loop must have a positive bound"))
        if loop.progress.minimum_delta <= 0 or loop.progress.patience < 1:
            issues.append(ValidationIssue("NO_PROGRESS_RULE", path, "loop progress requires positive delta and patience"))
        if loop.budget.max_calls > ir.constraints.max_agent_calls:
            issues.append(ValidationIssue("LOOP_BUDGET_EXCEEDS_GLOBAL", path, "loop call budget exceeds global budget"))
        if loop.budget.max_cost_microusd > ir.constraints.max_cost_microusd:
            issues.append(ValidationIssue("LOOP_BUDGET_EXCEEDS_GLOBAL", path, "loop cost budget exceeds global budget"))
        if loop.budget.max_duration_seconds > ir.constraints.max_duration_seconds:
            issues.append(ValidationIssue("LOOP_BUDGET_EXCEEDS_GLOBAL", path, "loop duration budget exceeds global budget"))
    return tuple(issues)


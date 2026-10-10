from __future__ import annotations

from contracts.execution_ir_v2 import ExecutionIRV2
from verification.types import ValidationIssue


def validate_graph(ir: ExecutionIRV2) -> tuple[ValidationIssue, ...]:
    adjacency = {node.node_id: [] for node in ir.nodes}
    indegree = {node.node_id: 0 for node in ir.nodes}
    for edge in ir.edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
    reachable: set[str] = set()
    stack = list(ir.entry_nodes)
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        stack.extend(adjacency.get(node_id, ()))
    issues = [
        ValidationIssue("UNREACHABLE_NODE", f"nodes.{node.node_id}", "node cannot be reached from an entry node")
        for node in ir.nodes
        if node.node_id not in reachable
    ]
    issues.extend(
        ValidationIssue("JOIN_ARITY_INVALID", f"nodes.{node.node_id}", "join requires at least two incoming branches")
        for node in ir.nodes
        if node.kind == "join" and indegree.get(node.node_id, 0) < 2
    )
    return tuple(issues)

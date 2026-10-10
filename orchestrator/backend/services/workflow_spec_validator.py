from __future__ import annotations

from collections import defaultdict, deque

from services.registry_snapshot import RegistryCandidateSnapshot, resolve_agent


class WorkflowSpecValidationError(ValueError):
    pass


ALLOWED_NODE_KINDS = {"business_agent", "wait", "decision", "end"}


def validate_workflow_spec(spec: dict[str, object], *, agents: list[RegistryCandidateSnapshot] | None = None) -> dict[str, object]:
    if spec.get("schema_version") != "workflow-spec.v1":
        raise WorkflowSpecValidationError("schema_version must be workflow-spec.v1")
    if spec.get("run_mode") not in {"canary", "production"}:
        raise WorkflowSpecValidationError("run_mode must be canary or production")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowSpecValidationError("nodes must be non-empty")
    if any(not isinstance(node, dict) for node in nodes):
        raise WorkflowSpecValidationError("nodes must contain objects")
    node_ids = [node.get("node_id") for node in nodes]
    if len(node_ids) != len(set(node_ids)) or any(not isinstance(node_id, str) or not node_id for node_id in node_ids):
        raise WorkflowSpecValidationError("node_id values must be unique")
    node_map = {node["node_id"]: node for node in nodes}
    graph: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in node_map}
    for node in nodes:
        if node.get("kind") not in ALLOWED_NODE_KINDS:
            raise WorkflowSpecValidationError("node kind is not supported")
        depends_on = node.get("depends_on", [])
        if not isinstance(depends_on, list) or any(dep not in node_map for dep in depends_on):
            raise WorkflowSpecValidationError("all dependencies must reference an existing node")
        for dependency in depends_on:
            graph[dependency].append(node["node_id"])
            indegree[node["node_id"]] += 1
        retry = node.get("retry", {"max_attempts": 1})
        if not isinstance(retry, dict) or not isinstance(retry.get("max_attempts", 1), int) or not 1 <= retry.get("max_attempts", 1) <= 5:
            raise WorkflowSpecValidationError("retry max_attempts must be total attempts between 1 and 5")
        if not isinstance(retry.get("backoff_seconds", 1), int) or not 0 <= retry.get("backoff_seconds", 1) <= 300:
            raise WorkflowSpecValidationError("retry backoff_seconds must be bounded")
        if not isinstance(retry.get("timeout_seconds", 30), int) or retry.get("timeout_seconds", 30) < 1:
            raise WorkflowSpecValidationError("retry timeout_seconds must be a positive integer")
        if node.get("kind") == "business_agent":
            if not node.get("agent_id") or not node.get("capability"):
                raise WorkflowSpecValidationError("business agent requires agent_id and capability")
            if not isinstance(node.get("input_bindings", {}), dict):
                raise WorkflowSpecValidationError("input_bindings must be an object")
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in graph[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(node_map):
        raise WorkflowSpecValidationError("workflow nodes must form a DAG")
    if agents is not None:
        for node in nodes:
            if node.get("kind") == "business_agent":
                try:
                    resolve_agent(agents, str(node["agent_id"]), str(node["capability"]), run_mode=str(spec["run_mode"]))
                except ValueError as error:
                    raise WorkflowSpecValidationError(str(error)) from error
    return spec

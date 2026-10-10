from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from blueprint.normalizer import NormalizedBlueprint
from capabilities.registry import RegistrySnapshot
from capabilities.resolver import BindingDecision
from contracts.execution_ir_v2 import (
    BoundedLoopSpec, ExecutionIRV2, IREdge, IRNode, IRConstraints, IRMetadata,
    LoopBudget, LoopEvaluationGate, LoopProgress, StructuredSubgraph,
)
from contracts.reasoner_proposal import ReasonerProposalV1
from ir.serializer import with_ir_hash
from verification.types import VerificationError, ValidationReport, verify_ir


@dataclass(frozen=True)
class BuildResult:
    ir: ExecutionIRV2
    report: ValidationReport


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _task_terminal(task_id: str, task) -> str:
    return f"loop-{task_id}" if task.iteration.enabled else f"invoke-{task_id}"


def _build_loop(task, binding: BindingDecision, metric: str) -> IRNode:
    invoke = IRNode(
        node_id=f"loop-invoke-{task.task_id}", kind="invoke", task_id=task.task_id,
        capability=task.capabilities[0], binding_id=f"binding-{task.task_id}",
        side_effect="idempotent_write" if task.side_effect == "idempotent_write" else "read_only",
        input_bindings={"input": "input.source"},
    )
    evaluate = IRNode(node_id=f"loop-evaluate-{task.task_id}", kind="evaluate", evaluation_metric=metric)
    body = StructuredSubgraph(
        nodes=(invoke, evaluate),
        edges=(IREdge(source=invoke.node_id, target=evaluate.node_id),),
        entry_nodes=(invoke.node_id,), exit_nodes=(evaluate.node_id,),
    )
    max_iterations = task.iteration.max_iterations
    return IRNode(
        node_id=f"loop-{task.task_id}", kind="bounded_loop", task_id=task.task_id,
        loop=BoundedLoopSpec(
            body=body,
            termination={"op": "gte", "left": {"op": "ref", "path": f"metrics.{metric}"}, "right": {"op": "const", "value": 0}},
            max_iterations=max_iterations,
            progress=LoopProgress(metric=metric, minimum_delta=1, patience=2),
            budget=LoopBudget(
                max_calls=max_iterations,
                max_cost_microusd=binding.selected.cost_microusd * max_iterations,
                max_duration_seconds=max(1, max_iterations * max(1, binding.selected.latency_ms // 1000)),
            ),
            evaluation_gate=LoopEvaluationGate(metric=metric),
        ),
    )


def build_execution_ir(
    normalized: NormalizedBlueprint,
    snapshot: RegistrySnapshot,
    proposal: ReasonerProposalV1,
    bindings: dict[str, BindingDecision],
    *,
    evaluation_enabled: bool = True,
) -> BuildResult:
    blueprint = normalized.blueprint
    expected_identity = (
        blueprint.correlation_id,
        blueprint.session_id,
        blueprint.plan_version,
        blueprint.workflow_version,
        blueprint.idempotency_key,
    )
    proposal_identity = (
        proposal.correlation_id,
        proposal.session_id,
        proposal.plan_version,
        proposal.workflow_version,
        proposal.idempotency_key,
    )
    if proposal_identity != expected_identity:
        raise VerificationError("PROPOSAL_IDENTITY_MISMATCH", "Reasoner proposal identity differs from Blueprint")
    task_by_id = {task.task_id: task for task in normalized.blueprint.tasks}
    proposal_tasks = {task_id for group in proposal.task_groups for task_id in group.task_ids}
    if proposal_tasks != set(task_by_id):
        missing = sorted(set(task_by_id) - proposal_tasks)
        if missing:
            raise VerificationError("PROPOSAL_TASK_MISMATCH", f"proposal omitted tasks: {missing}")
    for task in task_by_id.values():
        if task.side_effect in {"non_idempotent_write", "external_transaction"}:
            raise VerificationError("UNSAFE_SIDE_EFFECT", f"task {task.task_id} is not supported in v2")
        if task.task_id not in bindings:
            raise VerificationError("BINDING_MISSING", f"no immutable binding for {task.task_id}")
        if bindings[task.task_id].snapshot_hash != snapshot.snapshot_hash:
            raise VerificationError("BINDING_SNAPSHOT_MISMATCH", f"binding for {task.task_id} is not from the immutable snapshot")
        if task.side_effect == "idempotent_write" and not bindings[task.task_id].selected.idempotency_supported:
            raise VerificationError("IDEMPOTENCY_UNSUPPORTED", f"task {task.task_id} lacks idempotency support")
    for binding in proposal.data_bindings:
        source_parts = binding.source_ref.split(".")
        target_parts = binding.target_ref.split(".")
        if source_parts[0] == "outputs":
            if len(source_parts) != 3 or source_parts[1] not in task_by_id:
                raise VerificationError("DANGLING_OUTPUT_REF", f"unknown output ref {binding.source_ref}")
            source_schema = bindings[source_parts[1]].selected.output_schema
            source_property = source_schema.get("properties", {}).get(source_parts[2])
            if source_property is None:
                raise VerificationError("DANGLING_OUTPUT_REF", f"unknown output ref {binding.source_ref}")
            if len(target_parts) != 3 or target_parts[0] != "tasks" or target_parts[1] not in task_by_id:
                raise VerificationError("DANGLING_INPUT_REF", f"unknown input ref {binding.target_ref}")
            target_schema = bindings[target_parts[1]].selected.input_schema
            target_property = target_schema.get("properties", {}).get(target_parts[2])
            if target_property is None:
                raise VerificationError("DANGLING_INPUT_REF", f"unknown input ref {binding.target_ref}")
            if not isinstance(source_property, dict) or not isinstance(target_property, dict) or source_property.get("type") != target_property.get("type"):
                raise VerificationError("SCHEMA_INCOMPATIBLE", f"{binding.source_ref} cannot bind to {binding.target_ref}")

    metric_by_task = (
        {gate.after_task_id: gate.metric for gate in proposal.evaluation_gates}
        if evaluation_enabled
        else {}
    )
    nodes: list[IRNode] = []
    for task in sorted(task_by_id.values(), key=lambda item: item.task_id):
        binding = bindings[task.task_id]
        if task.iteration.enabled:
            nodes.append(_build_loop(task, binding, metric_by_task.get(task.task_id, "progress")))
        else:
            nodes.append(IRNode(
                node_id=f"invoke-{task.task_id}", kind="invoke", task_id=task.task_id,
                capability=task.capabilities[0], binding_id=f"binding-{task.task_id}",
                side_effect="idempotent_write" if task.side_effect == "idempotent_write" else "read_only",
                input_bindings={"input": "input.source"},
            ))
        if task.task_id in metric_by_task:
            nodes.append(IRNode(node_id=f"evaluate-{task.task_id}", kind="evaluate", evaluation_metric=metric_by_task[task.task_id]))
    nodes.append(IRNode(node_id="end", kind="end"))
    edges: list[IREdge] = []
    for task in task_by_id.values():
        target = _task_terminal(task.task_id, task)
        if not task.depends_on:
            edges.append(IREdge(source="__entry__", target=target))
        for dependency in task.depends_on:
            source = _task_terminal(dependency, task_by_id[dependency])
            if dependency in metric_by_task:
                source = f"evaluate-{dependency}"
            edges.append(IREdge(source=source, target=target))
        if task.task_id in metric_by_task:
            edges.append(IREdge(source=target, target=f"evaluate-{task.task_id}"))
    entry_nodes = tuple(edge.target for edge in edges if edge.source == "__entry__")
    edges = [edge for edge in edges if edge.source != "__entry__"]
    depended_on = {dependency for task in task_by_id.values() for dependency in task.depends_on}
    terminal_nodes = [
        _task_terminal(task.task_id, task) if task.task_id not in metric_by_task else f"evaluate-{task.task_id}"
        for task in task_by_id.values()
        if task.task_id not in depended_on
    ]
    edges.extend(IREdge(source=terminal, target="end") for terminal in terminal_nodes)
    ir = ExecutionIRV2(
        schema_version="execution-ir.v2", correlation_id=blueprint.correlation_id, session_id=blueprint.session_id,
        plan_version=blueprint.plan_version, workflow_version=1, idempotency_key=blueprint.idempotency_key,
        metadata=IRMetadata(
            blueprint_hash=normalized.blueprint_hash,
            snapshot_hash=snapshot.snapshot_hash,
            proposal_hash=_stable_hash(proposal.model_dump(mode="json")),
        ),
        constraints=IRConstraints(**blueprint.constraints.model_dump()), nodes=tuple(nodes), edges=tuple(edges),
        entry_nodes=entry_nodes, final_node_id="end",
    )
    report = verify_ir(ir, bindings=bindings, backend="n8n")
    if not report.ok:
        first = report.errors[0]
        raise VerificationError(first.code, first.message, path=first.path)
    return BuildResult(ir=with_ir_hash(ir), report=report)

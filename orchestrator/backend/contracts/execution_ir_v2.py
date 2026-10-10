from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from ir.expressions import ExpressionError, validate_expression


Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IRMetadata(FrozenStrictModel):
    blueprint_hash: str = Field(min_length=1, max_length=128)
    snapshot_hash: str = Field(min_length=1, max_length=128)
    proposal_hash: str = Field(min_length=1, max_length=128)
    ir_hash: str | None = Field(default=None, min_length=1, max_length=128)


class IRConstraints(FrozenStrictModel):
    max_duration_seconds: Annotated[StrictInt, Field(ge=1, le=604800)]
    max_cost_microusd: Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
    max_agent_calls: Annotated[StrictInt, Field(ge=1, le=100000)]
    max_parallelism: Annotated[StrictInt, Field(ge=1, le=256)]


class IREdge(FrozenStrictModel):
    source: Identifier
    target: Identifier
    source_output: str | None = Field(default=None, min_length=1, max_length=128)
    target_input: str | None = Field(default=None, min_length=1, max_length=128)


class LoopProgress(FrozenStrictModel):
    metric: Identifier
    minimum_delta: StrictInt | float
    patience: Annotated[StrictInt, Field(ge=1, le=100)]


class LoopBudget(FrozenStrictModel):
    max_calls: Annotated[StrictInt, Field(ge=1, le=100000)]
    max_cost_microusd: Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
    max_duration_seconds: Annotated[StrictInt, Field(ge=1, le=604800)]


class LoopEvaluationGate(FrozenStrictModel):
    metric: Identifier


class IRNode(FrozenStrictModel):
    node_id: Identifier
    kind: Literal["invoke", "evaluate", "decision", "parallel", "join", "bounded_loop", "wait", "end"]
    task_id: Identifier | None = None
    capability: Identifier | None = None
    binding_id: Identifier | None = None
    side_effect: Literal["read_only", "idempotent_write"] | None = None
    input_bindings: dict[str, str] | None = None
    expression: dict[str, object] | None = None
    duration_seconds: Annotated[StrictInt, Field(ge=1, le=86400)] | None = None
    evaluation_metric: Identifier | None = None
    loop: BoundedLoopSpec | None = None

    @field_validator("expression")
    @classmethod
    def expression_must_be_safe(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is not None:
            try:
                validate_expression(value)
            except ExpressionError as error:
                raise ValueError(f"invalid expression: {error}") from error
        return value

    @model_validator(mode="after")
    def required_fields_match_kind(self) -> "IRNode":
        if self.kind == "invoke":
            missing = [
                name
                for name in ("task_id", "capability", "binding_id", "side_effect", "input_bindings")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"invoke node missing required fields: {missing}")
        if self.kind == "evaluate" and self.evaluation_metric is None:
            raise ValueError("evaluate node requires evaluation_metric")
        if self.kind == "decision" and self.expression is None:
            raise ValueError("decision node requires expression")
        if self.kind == "wait" and self.duration_seconds is None:
            raise ValueError("wait node requires duration_seconds")
        if self.kind == "bounded_loop" and self.loop is None:
            raise ValueError("bounded_loop node requires loop")
        if self.kind != "bounded_loop" and self.loop is not None:
            raise ValueError("loop is only valid for bounded_loop")
        return self


class StructuredSubgraph(FrozenStrictModel):
    nodes: tuple[IRNode, ...] = Field(min_length=1, max_length=256)
    edges: tuple[IREdge, ...] = Field(max_length=2048)
    entry_nodes: tuple[Identifier, ...] = Field(min_length=1)
    exit_nodes: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "StructuredSubgraph":
        _validate_graph(self.nodes, self.edges, self.entry_nodes, self.exit_nodes)
        return self


class BoundedLoopSpec(FrozenStrictModel):
    body: StructuredSubgraph
    termination: dict[str, object]
    max_iterations: Annotated[StrictInt, Field(ge=1, le=100)]
    progress: LoopProgress
    budget: LoopBudget
    evaluation_gate: LoopEvaluationGate

    @field_validator("termination")
    @classmethod
    def termination_must_be_safe(cls, value: dict[str, object]) -> dict[str, object]:
        try:
            validate_expression(value)
        except ExpressionError as error:
            raise ValueError(f"invalid termination expression: {error}") from error
        return value


def _validate_graph(
    nodes: tuple[IRNode, ...],
    edges: tuple[IREdge, ...],
    entries: tuple[str, ...],
    exits: tuple[str, ...],
) -> None:
    node_ids = [node.node_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("duplicate node_id")
    known = set(node_ids)
    for edge in edges:
        if edge.source not in known or edge.target not in known:
            raise ValueError(f"dangling edge: {edge.source}->{edge.target}")
    if not set(entries) <= known or not set(exits) <= known:
        raise ValueError("entry or exit references an unknown node")
    adjacency = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in edges:
        adjacency[edge.source].append(edge.target)
        indegree[edge.target] += 1
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for target in sorted(adjacency[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if visited != len(node_ids):
        raise ValueError("top-level or structured subgraph contains a cycle")


class ExecutionIRV2(FrozenStrictModel):
    schema_version: Literal["execution-ir.v2"]
    correlation_id: Identifier
    session_id: Identifier
    plan_version: Annotated[StrictInt, Field(ge=1)]
    workflow_version: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: str = Field(min_length=1, max_length=256)
    canary_policy: Literal["disabled", "required"] = "disabled"
    metadata: IRMetadata
    constraints: IRConstraints
    nodes: tuple[IRNode, ...] = Field(min_length=1, max_length=1024)
    edges: tuple[IREdge, ...] = Field(max_length=8192)
    entry_nodes: tuple[Identifier, ...] = Field(min_length=1)
    final_node_id: Identifier

    @model_validator(mode="after")
    def validate_graph(self) -> "ExecutionIRV2":
        if self.workflow_version > 1 and self.canary_policy != "required":
            raise ValueError("repaired workflow requires Canary policy")
        _validate_graph(self.nodes, self.edges, self.entry_nodes, (self.final_node_id,))
        return self


IRNode.model_rebuild()

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictInt

from pydantic import BaseModel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskGroup(StrictModel):
    group_id: str = Field(min_length=1, max_length=128)
    task_ids: list[str] = Field(min_length=1, max_length=256)
    mode: Literal["sequence", "parallel"]


class DataBinding(StrictModel):
    source_ref: str = Field(min_length=1, max_length=256)
    target_ref: str = Field(min_length=1, max_length=256)


class ControlStructure(StrictModel):
    structure_id: str = Field(min_length=1, max_length=128)
    kind: Literal["decision", "bounded_loop", "wait"]
    task_ids: list[str] = Field(min_length=1, max_length=256)


class EvaluationGate(StrictModel):
    gate_id: str = Field(min_length=1, max_length=128)
    after_task_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)


class ReasonerProposalV1(StrictModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["reasoner-proposal.v1"]
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    plan_version: StrictInt = Field(ge=1)
    workflow_version: Literal[0]
    idempotency_key: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=128)
    task_groups: list[TaskGroup] = Field(max_length=256)
    data_bindings: list[DataBinding] = Field(max_length=1024)
    control_structures: list[ControlStructure] = Field(max_length=256)
    evaluation_gates: list[EvaluationGate] = Field(max_length=256)

    @staticmethod
    def _unique(values: list[str], label: str) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError(f"{label} contains duplicates")
        return values

    def model_post_init(self, __context: object) -> None:
        for group in self.task_groups:
            self._unique(group.task_ids, "task_ids")
        for structure in self.control_structures:
            self._unique(structure.task_ids, "task_ids")

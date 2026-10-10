from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from contracts.orchestration import ContractEnvelope


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuccessCriterion(StrictModel):
    criterion_id: Identifier
    metric: Identifier
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    target: bool | StrictInt | float | str


class Goal(StrictModel):
    goal_id: Identifier
    objective: str = Field(min_length=1, max_length=4096)
    criteria: list[SuccessCriterion] = Field(min_length=1, max_length=64)

    @field_validator("objective")
    @classmethod
    def objective_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must not be blank")
        return value


class IterationRequirement(StrictModel):
    enabled: bool
    max_iterations: Annotated[StrictInt, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def match_enabled_state(self) -> "IterationRequirement":
        if self.enabled and self.max_iterations < 1:
            raise ValueError("max_iterations must be positive when iteration is enabled")
        if not self.enabled and self.max_iterations != 0:
            raise ValueError("max_iterations must be zero when iteration is disabled")
        return self


class BlueprintTask(StrictModel):
    task_id: Identifier
    objective: str = Field(min_length=1, max_length=4096)
    capabilities: list[Identifier] = Field(min_length=1, max_length=32)
    depends_on: list[Identifier] = Field(max_length=256)
    completion_criteria: list[SuccessCriterion] = Field(min_length=1, max_length=64)
    iteration: IterationRequirement
    side_effect: Literal[
        "read_only",
        "idempotent_write",
        "non_idempotent_write",
        "external_transaction",
    ]

    @field_validator("objective")
    @classmethod
    def objective_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must not be blank")
        return value

    @field_validator("capabilities", "depends_on")
    @classmethod
    def identifiers_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("identifier list contains duplicates")
        return value


class GlobalConstraints(StrictModel):
    max_duration_seconds: Annotated[StrictInt, Field(ge=1, le=604800)]
    max_cost_microusd: Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
    max_agent_calls: Annotated[StrictInt, Field(ge=1, le=100000)]
    max_parallelism: Annotated[StrictInt, Field(ge=1, le=256)]


class ExecutionPolicy(StrictModel):
    allow_replan: bool
    allow_rebinding: bool
    side_effect_approval: Literal["reject_unsafe"]


class PlanningBlueprintV2(ContractEnvelope):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["planning-blueprint.v2"]
    workflow_version: Literal[0]
    goal: Goal
    tasks: list[BlueprintTask] = Field(min_length=1, max_length=256)
    constraints: GlobalConstraints
    execution_policy: ExecutionPolicy

    @model_validator(mode="after")
    def validate_task_references(self) -> "PlanningBlueprintV2":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("duplicate task_id")
        known = set(task_ids)
        dangling = sorted(
            {dependency for task in self.tasks for dependency in task.depends_on if dependency not in known}
        )
        if dangling:
            raise ValueError(f"dangling task dependency: {dangling}")
        return self

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from pydantic import ValidationError

from contracts.blueprint_v2 import PlanningBlueprintV2


class NormalizationError(ValueError):
    """A blueprint cannot be deterministically normalized."""


@dataclass(frozen=True)
class NormalizedBlueprint:
    blueprint: PlanningBlueprintV2
    blueprint_hash: str
    canonical_json: str


def _ensure_dependency_dag(blueprint: PlanningBlueprintV2) -> None:
    indegree = {task.task_id: len(task.depends_on) for task in blueprint.tasks}
    dependents = {task.task_id: [] for task in blueprint.tasks}
    for task in blueprint.tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.task_id)
    ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if visited != len(blueprint.tasks):
        raise NormalizationError("DEPENDENCY_CYCLE: tasks.depends_on contains a cycle")


def _canonicalize(model: PlanningBlueprintV2) -> PlanningBlueprintV2:
    payload = model.model_dump(mode="json")
    payload["goal"]["criteria"] = sorted(
        payload["goal"]["criteria"], key=lambda criterion: criterion["criterion_id"]
    )
    normalized_tasks = []
    for task in payload["tasks"]:
        task["capabilities"] = sorted(task["capabilities"])
        task["depends_on"] = sorted(task["depends_on"])
        task["completion_criteria"] = sorted(
            task["completion_criteria"], key=lambda criterion: criterion["criterion_id"]
        )
        normalized_tasks.append(task)
    payload["tasks"] = sorted(normalized_tasks, key=lambda task: task["task_id"])
    payload["retry_policy"]["retry_on"] = sorted(payload["retry_policy"]["retry_on"])
    return PlanningBlueprintV2.model_validate(payload)


def normalize_blueprint(raw: dict[str, object]) -> NormalizedBlueprint:
    payload = deepcopy(raw)
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and "iteration" not in task:
                task["iteration"] = {"enabled": False, "max_iterations": 0}
    try:
        validated = PlanningBlueprintV2.model_validate(payload)
    except ValidationError as error:
        raise NormalizationError(str(error)) from error
    _ensure_dependency_dag(validated)
    normalized = _canonicalize(validated)
    canonical_json = json.dumps(
        normalized.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return NormalizedBlueprint(
        blueprint=normalized,
        blueprint_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        canonical_json=canonical_json,
    )

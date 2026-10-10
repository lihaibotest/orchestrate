from __future__ import annotations

from dataclasses import dataclass

from blueprint.normalizer import NormalizedBlueprint
from capabilities.registry import RegistrySnapshot, RegistryValidationError


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class PreflightResult:
    ready: bool
    issues: tuple[PreflightIssue, ...]


def run_preflight(
    normalized: NormalizedBlueprint,
    snapshot: RegistrySnapshot,
    *,
    backend_features: set[str],
    evaluation_enabled: bool = True,
) -> PreflightResult:
    issues: list[PreflightIssue] = []
    blueprint = normalized.blueprint
    required_base_features = {"invoke", "evaluate", "end"}
    missing_base = sorted(required_base_features - backend_features)
    if missing_base:
        issues.append(
            PreflightIssue(
                "BACKEND_FEATURE_UNAVAILABLE",
                "backend.features",
                f"backend is missing required features: {missing_base}",
            )
        )

    business_candidates = [
        candidate
        for candidate in snapshot.candidates
        if candidate.role == "business" and candidate.health == "healthy"
    ]
    for task in blueprint.tasks:
        if not any(
            set(task.capabilities) & set(candidate.capabilities)
            for candidate in business_candidates
        ):
            issues.append(
                PreflightIssue(
                    "CAPABILITY_UNAVAILABLE",
                    f"tasks.{task.task_id}.capabilities",
                    f"no healthy business candidate provides any of {list(task.capabilities)}",
                )
            )
        if task.iteration.enabled and "bounded_loop" not in backend_features:
            issues.append(
                PreflightIssue(
                    "BACKEND_FEATURE_UNAVAILABLE",
                    f"tasks.{task.task_id}.iteration",
                    "backend does not support bounded_loop",
                )
            )

    metrics: list[tuple[str, str]] = []
    for index, criterion in enumerate(blueprint.goal.criteria):
        metrics.append((criterion.metric, f"goal.criteria.{index}.metric"))
    for task in blueprint.tasks:
        for index, criterion in enumerate(task.completion_criteria):
            metrics.append((criterion.metric, f"tasks.{task.task_id}.completion_criteria.{index}.metric"))
    if evaluation_enabled:
        checked_metrics: set[str] = set()
        for metric, path in metrics:
            if metric in checked_metrics:
                continue
            checked_metrics.add(metric)
            try:
                snapshot.default_evaluator(metric)
            except RegistryValidationError as error:
                issues.append(PreflightIssue("EVALUATOR_METRIC_UNAVAILABLE", path, str(error)))

    return PreflightResult(ready=not issues, issues=tuple(issues))

from __future__ import annotations

import hashlib

from blueprint.normalizer import NormalizedBlueprint
from persistence.execution_plan_repository import ExecutionPlanRepository


class BlueprintV2Service:
    def __init__(self, repository: ExecutionPlanRepository) -> None:
        self.repository = repository

    def enqueue(self, normalized: NormalizedBlueprint) -> dict[str, object]:
        blueprint = normalized.blueprint
        job_id = "job-" + hashlib.sha256(
            f"{blueprint.session_id}:{blueprint.idempotency_key}:{normalized.blueprint_hash}".encode("utf-8")
        ).hexdigest()[:24]
        claimed_job_id, created = self.repository.enqueue_planning_job(
            job_id=job_id,
            session_id=blueprint.session_id,
            correlation_id=blueprint.correlation_id,
            plan_version=blueprint.plan_version,
            idempotency_key=blueprint.idempotency_key,
            blueprint_hash=normalized.blueprint_hash,
            blueprint=blueprint.model_dump(mode="json"),
        )
        return {
            "job_id": claimed_job_id,
            "status": "queued",
            "created": created,
            "status_url": f"/api/v2/planning-jobs/{claimed_job_id}",
        }


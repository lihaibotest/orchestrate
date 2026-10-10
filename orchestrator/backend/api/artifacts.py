from __future__ import annotations

from typing import Mapping

from fastapi import APIRouter, Header, HTTPException

from domain.artifact import ArtifactIdentity, ArtifactNotFound
from ports.artifact import ArtifactManagerPort


def artifact_metadata_payload(artifact: ArtifactIdentity) -> dict[str, object]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "media_type": artifact.media_type,
        "schema_ref": artifact.schema_ref,
        "content_hash": artifact.content_hash,
        "size_bytes": artifact.size_bytes,
        "storage_ref": artifact.storage_ref,
        "tenant_id": artifact.tenant_id,
        "workflow_family_id": artifact.workflow_family_id,
        "workflow_version": artifact.workflow_version,
        "execution_mode": artifact.execution_mode,
        "output_name": artifact.output_name,
        "producer": {
            "execution_id": artifact.producer_execution_id,
            "node_id": artifact.producer_node_id,
            "attempt_no": artifact.producer_attempt_no,
        },
        "status": artifact.status,
        "validation_status": artifact.validation_status,
        "metadata": artifact.metadata_dict(),
        "created_at": artifact.created_at.isoformat(),
    }


def create_artifact_router(manager: ArtifactManagerPort) -> APIRouter:
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

    @router.get("/{artifact_id}")
    async def get_artifact_metadata(
        artifact_id: str,
        tenant_id: str = Header(alias="X-Tenant-Id"),
    ) -> Mapping[str, object]:
        try:
            return artifact_metadata_payload(
                manager.get_artifact_metadata(tenant_id, artifact_id)
            )
        except (ArtifactNotFound, KeyError) as error:
            raise HTTPException(status_code=404, detail="Artifact not found") from error

    return router

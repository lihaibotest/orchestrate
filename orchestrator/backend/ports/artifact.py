from __future__ import annotations

from typing import Protocol

from domain.artifact import (
    ArtifactIdentity,
    ArtifactPutRequest,
    ArtifactValidationCheck,
    ArtifactValidationResult,
    ExpectedArtifactOutput,
)


class ArtifactManagerPort(Protocol):
    def put_artifact(self, request: ArtifactPutRequest) -> ArtifactIdentity: ...

    def get_artifact_metadata(
        self, tenant_id: str, artifact_id: str
    ) -> ArtifactIdentity: ...

    def check_exists(self, tenant_id: str, artifact_id: str) -> bool: ...

    def validate_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
        expected: ExpectedArtifactOutput,
    ) -> ArtifactValidationResult: ...

    def mark_consumed(self, tenant_id: str, artifact_id: str) -> ArtifactIdentity: ...

    def delete_artifact(self, tenant_id: str, artifact_id: str) -> None: ...


class ArtifactMetadataRepositoryPort(Protocol):
    def save_artifact(self, artifact: ArtifactIdentity) -> ArtifactIdentity: ...

    def get_artifact(
        self, tenant_id: str, artifact_id: str
    ) -> ArtifactIdentity: ...

    def find_produced_artifact(
        self,
        tenant_id: str,
        execution_id: str,
        node_id: str,
        attempt_no: int,
        output_name: str,
    ) -> ArtifactIdentity | None: ...

    def update_artifact_state(
        self,
        tenant_id: str,
        artifact_id: str,
        *,
        status: str,
        validation_status: str,
    ) -> ArtifactIdentity: ...

    def record_artifact_validation(
        self, tenant_id: str, artifact_id: str, check: ArtifactValidationCheck
    ) -> None: ...

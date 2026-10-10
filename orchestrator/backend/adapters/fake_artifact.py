from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

from domain.artifact import (
    ArtifactIdentity,
    ArtifactIdentityConflict,
    ArtifactNotFound,
    ArtifactValidationCheck,
)


class InMemoryArtifactRepository:
    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str], ArtifactIdentity] = {}
        self.validations: list[tuple[str, str, ArtifactValidationCheck]] = []

    def save_artifact(self, artifact: ArtifactIdentity) -> ArtifactIdentity:
        key = (artifact.tenant_id, artifact.artifact_id)
        existing = self.artifacts.get(key)
        if existing is not None:
            comparable = (
                existing.content_hash,
                existing.storage_ref,
                existing.producer_execution_id,
                existing.producer_node_id,
                existing.producer_attempt_no,
                existing.output_name,
            )
            observed = (
                artifact.content_hash,
                artifact.storage_ref,
                artifact.producer_execution_id,
                artifact.producer_node_id,
                artifact.producer_attempt_no,
                artifact.output_name,
            )
            if comparable != observed:
                raise ArtifactIdentityConflict("Artifact identity conflict")
            return existing
        self.artifacts[key] = artifact
        return artifact

    def get_artifact(self, tenant_id: str, artifact_id: str) -> ArtifactIdentity:
        try:
            return self.artifacts[(tenant_id, artifact_id)]
        except KeyError as error:
            raise ArtifactNotFound(f"Artifact not found: {artifact_id}") from error

    def find_produced_artifact(
        self,
        tenant_id: str,
        execution_id: str,
        node_id: str,
        attempt_no: int,
        output_name: str,
    ) -> ArtifactIdentity | None:
        return next(
            (
                artifact
                for (scope, _artifact_id), artifact in self.artifacts.items()
                if scope == tenant_id
                and artifact.producer_execution_id == execution_id
                and artifact.producer_node_id == node_id
                and artifact.producer_attempt_no == attempt_no
                and artifact.output_name == output_name
            ),
            None,
        )

    def update_artifact_state(
        self,
        tenant_id: str,
        artifact_id: str,
        *,
        status: str,
        validation_status: str,
    ) -> ArtifactIdentity:
        artifact = self.get_artifact(tenant_id, artifact_id)
        changed = replace(
            artifact, status=status, validation_status=validation_status
        )
        self.artifacts[(tenant_id, artifact_id)] = changed
        return changed

    def record_artifact_validation(
        self, tenant_id: str, artifact_id: str, check: ArtifactValidationCheck
    ) -> None:
        self.get_artifact(tenant_id, artifact_id)
        self.validations.append((tenant_id, artifact_id, check))


class FakeMinioClient:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.lose_put_response_once = False

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(
        self,
        bucket: str,
        key: str,
        stream: object,
        length: int,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        content = stream.read(length)  # type: ignore[attr-defined]
        self.objects[(bucket, key)] = {
            "content": content,
            "content_type": content_type,
            "metadata": {f"x-amz-meta-{name}": value for name, value in metadata.items()},
        }
        if self.lose_put_response_once:
            self.lose_put_response_once = False
            raise ConnectionError("response lost after MinIO accepted the object")
        return SimpleNamespace(object_name=key)

    def stat_object(self, bucket: str, key: str) -> object:
        try:
            item = self.objects[(bucket, key)]
        except KeyError as error:
            raise FileNotFoundError(key) from error
        return SimpleNamespace(
            size=len(item["content"]),
            content_type=item["content_type"],
            metadata=dict(item["metadata"]),
        )

    def get_object(self, bucket: str, key: str) -> BytesIO:
        try:
            return BytesIO(self.objects[(bucket, key)]["content"])  # type: ignore[arg-type]
        except KeyError as error:
            raise FileNotFoundError(key) from error

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def corrupt(self, bucket: str, key: str, content: bytes) -> None:
        self.objects[(bucket, key)]["content"] = content

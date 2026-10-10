from __future__ import annotations

from io import BytesIO
import json
from typing import Mapping

from domain.artifact import (
    ArtifactIdentity,
    ArtifactIdentityConflict,
    ArtifactNotFound,
    ArtifactPutRequest,
    ArtifactValidationCheck,
    ArtifactValidationResult,
    ExpectedArtifactOutput,
    content_sha256,
    safe_storage_segment,
    stable_artifact_id,
)
from ports.artifact import ArtifactManagerPort, ArtifactMetadataRepositoryPort


def _schema_error(value: object, schema: Mapping[str, object], path: str = "$") -> str | None:
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = (expected_type,) if isinstance(expected_type, str) else tuple(expected_type)
        matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if not any(matches.get(str(item), False) for item in allowed):
            return f"{path} does not match schema type {expected_type}"
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        return f"{path} is not an allowed enum value"
    if isinstance(value, dict):
        required = schema.get("required", ())
        if isinstance(required, (list, tuple)):
            missing = sorted(str(item) for item in required if str(item) not in value)
            if missing:
                return f"{path} is missing required properties: {', '.join(missing)}"
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, nested_schema in properties.items():
                if key in value and isinstance(nested_schema, Mapping):
                    error = _schema_error(value[key], nested_schema, f"{path}.{key}")
                    if error:
                        return error
            if schema.get("additionalProperties") is False:
                unknown = sorted(str(key) for key in value if key not in properties)
                if unknown:
                    return f"{path} has unknown properties: {', '.join(unknown)}"
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            error = _schema_error(item, schema["items"], f"{path}[{index}]")  # type: ignore[arg-type]
            if error:
                return error
    return None


class MinioArtifactAdapter(ArtifactManagerPort):
    """Artifact manager backed by a MinIO-compatible object client."""

    storage_scheme = "artifact-store://"

    def __init__(
        self,
        client: object,
        repository: ArtifactMetadataRepositoryPort,
        *,
        bucket: str,
        ensure_bucket: bool = True,
    ) -> None:
        self.client = client
        self.repository = repository
        self.bucket = safe_storage_segment(bucket, "bucket")
        if ensure_bucket and not bool(self.client.bucket_exists(self.bucket)):  # type: ignore[attr-defined]
            self.client.make_bucket(self.bucket)  # type: ignore[attr-defined]

    @classmethod
    def connect(
        cls,
        endpoint: str,
        access_key: str,
        secret_key: str,
        repository: ArtifactMetadataRepositoryPort,
        *,
        bucket: str,
        secure: bool = True,
    ) -> "MinioArtifactAdapter":
        # Infrastructure dependency stays inside the Adapter boundary.
        from minio import Minio  # type: ignore[import-not-found]

        return cls(
            Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure),
            repository,
            bucket=bucket,
        )

    @staticmethod
    def _key(request: ArtifactPutRequest, artifact_id: str) -> str:
        parts = (
            safe_storage_segment(request.tenant_id, "tenant_id"),
            request.execution_mode.lower(),
            safe_storage_segment(request.workflow_family_id, "workflow_family_id"),
            f"v{request.workflow_version}",
            safe_storage_segment(request.execution_id, "execution_id"),
            safe_storage_segment(request.node_id, "node_id"),
            f"attempt-{request.attempt_no}",
            safe_storage_segment(request.output.output_name, "output_name"),
            artifact_id,
        )
        return "/".join(parts)

    def _storage_ref(self, key: str) -> str:
        return f"{self.storage_scheme}{self.bucket}/{key}"

    def _key_from_ref(self, storage_ref: str) -> str:
        prefix = f"{self.storage_scheme}{self.bucket}/"
        if not storage_ref.startswith(prefix):
            raise ArtifactIdentityConflict("Artifact storage reference belongs to another store")
        return storage_ref[len(prefix) :]

    @staticmethod
    def _metadata(stat: object) -> dict[str, str]:
        raw = getattr(stat, "metadata", {}) or {}
        return {
            str(key).lower().removeprefix("x-amz-meta-"): str(value)
            for key, value in raw.items()
        }

    def _stat(self, key: str) -> object | None:
        try:
            return self.client.stat_object(self.bucket, key)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _read(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, key)  # type: ignore[attr-defined]
        try:
            data = response.read()
            return data if isinstance(data, bytes) else bytes(data)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            release = getattr(response, "release_conn", None)
            if callable(release):
                release()

    def put_artifact(self, request: ArtifactPutRequest) -> ArtifactIdentity:
        artifact_id = stable_artifact_id(
            request.execution_id,
            request.node_id,
            request.attempt_no,
            request.output.output_name,
        )
        key = self._key(request, artifact_id)
        digest = str(request.output.content_hash)
        try:
            existing = self.repository.get_artifact(request.tenant_id, artifact_id)
        except (ArtifactNotFound, KeyError):
            existing = None
        if existing is not None:
            immutable = (
                existing.content_hash,
                existing.size_bytes,
                existing.media_type,
                existing.schema_ref,
                existing.producer_execution_id,
                existing.producer_node_id,
                existing.producer_attempt_no,
                existing.output_name,
            )
            observed = (
                digest,
                len(request.output.content),
                request.output.media_type,
                request.output.schema_ref,
                request.execution_id,
                request.node_id,
                request.attempt_no,
                request.output.output_name,
            )
            if immutable != observed:
                raise ArtifactIdentityConflict(
                    "stable Artifact identity is bound to different content or metadata"
                )
            stat = self._stat(key)
            if stat is not None and self._metadata(stat).get("content-sha256") == digest:
                return existing

        metadata = {
            "artifact-id": artifact_id,
            "content-sha256": digest,
            "media-type": request.output.media_type,
            "producer-execution-id": request.execution_id,
            "producer-node-id": request.node_id,
            "producer-attempt-no": str(request.attempt_no),
        }
        if request.output.schema_ref is not None:
            metadata["schema-ref"] = request.output.schema_ref
        try:
            self.client.put_object(  # type: ignore[attr-defined]
                self.bucket,
                key,
                BytesIO(request.output.content),
                len(request.output.content),
                content_type=request.output.media_type,
                metadata=metadata,
            )
        except Exception:
            # A lost response after a successful PUT is recovered by the stable key/hash.
            stat = self._stat(key)
            if stat is None or self._metadata(stat).get("content-sha256") != digest:
                raise
        identity = ArtifactIdentity(
            artifact_id=artifact_id,
            artifact_type=request.expected.artifact_type,
            media_type=request.output.media_type,
            schema_ref=request.output.schema_ref,
            content_hash=digest,
            size_bytes=len(request.output.content),
            storage_ref=self._storage_ref(key),
            tenant_id=request.tenant_id,
            workflow_family_id=request.workflow_family_id,
            workflow_version=request.workflow_version,
            execution_mode=request.execution_mode,
            output_name=request.output.output_name,
            producer_execution_id=request.execution_id,
            producer_node_id=request.node_id,
            producer_attempt_no=request.attempt_no,
            metadata=request.output.metadata,
        )
        return self.repository.save_artifact(identity)

    def get_artifact_metadata(
        self, tenant_id: str, artifact_id: str
    ) -> ArtifactIdentity:
        return self.repository.get_artifact(tenant_id, artifact_id)

    def check_exists(self, tenant_id: str, artifact_id: str) -> bool:
        try:
            artifact = self.repository.get_artifact(tenant_id, artifact_id)
        except (ArtifactNotFound, KeyError):
            return False
        if artifact.status == "DELETED":
            return False
        return self._stat(self._key_from_ref(artifact.storage_ref)) is not None

    def _persist_validation(
        self,
        artifact: ArtifactIdentity,
        checks: tuple[ArtifactValidationCheck, ...],
    ) -> ArtifactValidationResult:
        for check in checks:
            self.repository.record_artifact_validation(
                artifact.tenant_id, artifact.artifact_id, check
            )
        result = ArtifactValidationResult.from_checks(artifact.artifact_id, checks)
        self.repository.update_artifact_state(
            artifact.tenant_id,
            artifact.artifact_id,
            status="VALIDATED" if result.passed else "INVALID",
            validation_status="PASS" if result.passed else "FAIL",
        )
        return result

    def validate_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
        expected: ExpectedArtifactOutput,
    ) -> ArtifactValidationResult:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        self.repository.update_artifact_state(
            tenant_id, artifact_id, status="VALIDATING", validation_status="PENDING"
        )
        key = self._key_from_ref(artifact.storage_ref)
        stat = self._stat(key)
        if stat is None:
            return self._persist_validation(
                artifact,
                (
                    ArtifactValidationCheck(
                        "EXISTS", False, "ARTIFACT_MISSING", "Artifact object does not exist"
                    ),
                ),
            )

        stat_metadata = self._metadata(stat)
        observed_media = str(
            getattr(stat, "content_type", "") or stat_metadata.get("media-type", "")
        ).lower()
        observed_size = int(getattr(stat, "size", -1))
        checks: list[ArtifactValidationCheck] = [
            ArtifactValidationCheck("EXISTS", True),
            ArtifactValidationCheck(
                "MEDIA_TYPE",
                observed_media == expected.media_type,
                None if observed_media == expected.media_type else "ARTIFACT_MEDIA_TYPE_MISMATCH",
                None if observed_media == expected.media_type else f"expected {expected.media_type}, got {observed_media}",
            ),
            ArtifactValidationCheck(
                "SCHEMA_REF",
                expected.schema_ref is None or artifact.schema_ref == expected.schema_ref,
                None if expected.schema_ref is None or artifact.schema_ref == expected.schema_ref else "ARTIFACT_SCHEMA_MISMATCH",
                None if expected.schema_ref is None or artifact.schema_ref == expected.schema_ref else "Artifact schema_ref does not satisfy the output contract",
            ),
        ]
        content = self._read(key)
        observed_hash = content_sha256(content)
        hash_passed = (
            observed_hash == artifact.content_hash
            and stat_metadata.get("content-sha256") == artifact.content_hash
        )
        checks.append(
            ArtifactValidationCheck(
                "CONTENT_HASH",
                hash_passed,
                None if hash_passed else "ARTIFACT_HASH_MISMATCH",
                None if hash_passed else "Artifact content hash is inconsistent",
            )
        )
        size_passed = observed_size == artifact.size_bytes
        if expected.min_size_bytes is not None:
            size_passed = size_passed and observed_size >= expected.min_size_bytes
        if expected.max_size_bytes is not None:
            size_passed = size_passed and observed_size <= expected.max_size_bytes
        checks.append(
            ArtifactValidationCheck(
                "SIZE",
                size_passed,
                None if size_passed else "ARTIFACT_SIZE_MISMATCH",
                None if size_passed else "Artifact size does not satisfy the output contract",
            )
        )
        schema_passed = True
        schema_reason = None
        if expected.schema:
            if expected.media_type != "application/json":
                schema_passed = False
                schema_reason = "inline JSON schema requires application/json"
            else:
                try:
                    decoded = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    schema_passed = False
                    schema_reason = "Artifact is not valid JSON"
                else:
                    schema_reason = _schema_error(decoded, expected.schema)
                    schema_passed = schema_reason is None
        checks.append(
            ArtifactValidationCheck(
                "SCHEMA",
                schema_passed,
                None if schema_passed else "ARTIFACT_SCHEMA_MISMATCH",
                schema_reason,
            )
        )
        return self._persist_validation(artifact, tuple(checks))

    def mark_consumed(self, tenant_id: str, artifact_id: str) -> ArtifactIdentity:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        if artifact.validation_status != "PASS" or artifact.status not in {
            "VALIDATED",
            "CONSUMED",
        }:
            raise ArtifactIdentityConflict("unvalidated Artifact cannot be consumed")
        return self.repository.update_artifact_state(
            tenant_id,
            artifact_id,
            status="CONSUMED",
            validation_status="PASS",
        )

    def delete_artifact(self, tenant_id: str, artifact_id: str) -> None:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        key = self._key_from_ref(artifact.storage_ref)
        self.client.remove_object(self.bucket, key)  # type: ignore[attr-defined]
        self.repository.update_artifact_state(
            tenant_id, artifact_id, status="DELETED", validation_status=artifact.validation_status
        )

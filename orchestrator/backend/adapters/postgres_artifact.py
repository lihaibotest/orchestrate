from __future__ import annotations

from domain.artifact import (
    ArtifactIdentity,
    ArtifactIdentityConflict,
    ArtifactNotFound,
    ArtifactValidationCheck,
)
from adapters.postgres_planning_blueprint import PostgresPlanningBlueprintRepository


class PostgresArtifactRepository(PostgresPlanningBlueprintRepository):
    """PostgreSQL metadata authority for tenant-scoped Artifacts."""

    @staticmethod
    def _artifact_select() -> str:
        return (
            "SELECT artifact_id,artifact_type,media_type,schema_ref,content_hash,size_bytes,"
            "storage_ref,tenant_id,workflow_family_id,workflow_version,execution_mode,"
            "output_name,execution_id,node_id,attempt_no,status,validation_status,metadata,created_at "
            "FROM artifact_ref "
        )

    @staticmethod
    def _artifact_from_row(row: tuple[object, ...]) -> ArtifactIdentity:
        metadata = row[17] if isinstance(row[17], dict) else {}
        return ArtifactIdentity(
            artifact_id=str(row[0]),
            artifact_type=str(row[1]),
            media_type=str(row[2]),
            schema_ref=None if row[3] is None else str(row[3]),
            content_hash=str(row[4]),
            size_bytes=int(row[5]),
            storage_ref=str(row[6]),
            tenant_id=str(row[7]),
            workflow_family_id=str(row[8]),
            workflow_version=int(row[9]),
            execution_mode=str(row[10]),
            output_name=str(row[11]),
            producer_execution_id=str(row[12]),
            producer_node_id=str(row[13]),
            producer_attempt_no=int(row[14]),
            status=str(row[15]),
            validation_status=str(row[16]),
            metadata=metadata,
            created_at=row[18],  # type: ignore[arg-type]
        )

    def save_artifact(self, artifact: ArtifactIdentity) -> ArtifactIdentity:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO artifact_ref(artifact_id,artifact_type,media_type,schema_ref,"
                "content_hash,size_bytes,storage_ref,uri,tenant_id,workflow_family_id,workflow_version,"
                "execution_mode,output_name,execution_id,node_id,attempt_no,status,"
                "validation_status,metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (artifact_id) DO NOTHING",
                (
                    artifact.artifact_id,
                    artifact.artifact_type,
                    artifact.media_type,
                    artifact.schema_ref,
                    artifact.content_hash,
                    artifact.size_bytes,
                    artifact.storage_ref,
                    artifact.storage_ref,
                    artifact.tenant_id,
                    artifact.workflow_family_id,
                    artifact.workflow_version,
                    artifact.execution_mode,
                    artifact.output_name,
                    artifact.producer_execution_id,
                    artifact.producer_node_id,
                    artifact.producer_attempt_no,
                    artifact.status,
                    artifact.validation_status,
                    self._json(artifact.metadata_dict()),
                ),
            )
            cursor.execute(
                self._artifact_select() + "WHERE tenant_id=%s AND artifact_id=%s FOR UPDATE",
                (artifact.tenant_id, artifact.artifact_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ArtifactIdentityConflict("Artifact identity belongs to another tenant")
            stored = self._artifact_from_row(row)
            immutable = (
                stored.content_hash,
                stored.size_bytes,
                stored.storage_ref,
                stored.producer_execution_id,
                stored.producer_node_id,
                stored.producer_attempt_no,
                stored.output_name,
            )
            observed = (
                artifact.content_hash,
                artifact.size_bytes,
                artifact.storage_ref,
                artifact.producer_execution_id,
                artifact.producer_node_id,
                artifact.producer_attempt_no,
                artifact.output_name,
            )
            if immutable != observed:
                raise ArtifactIdentityConflict(
                    "stable Artifact identity is bound to different content"
                )
            return stored

    def get_artifact(self, tenant_id: str, artifact_id: str) -> ArtifactIdentity:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                self._artifact_select() + "WHERE tenant_id=%s AND artifact_id=%s",
                (tenant_id, artifact_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ArtifactNotFound(f"Artifact not found: {artifact_id}")
            return self._artifact_from_row(row)

    def find_produced_artifact(
        self,
        tenant_id: str,
        execution_id: str,
        node_id: str,
        attempt_no: int,
        output_name: str,
    ) -> ArtifactIdentity | None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                self._artifact_select()
                + "WHERE tenant_id=%s AND execution_id=%s AND node_id=%s "
                "AND attempt_no=%s AND output_name=%s",
                (tenant_id, execution_id, node_id, attempt_no, output_name),
            )
            row = cursor.fetchone()
            return None if row is None else self._artifact_from_row(row)

    def update_artifact_state(
        self,
        tenant_id: str,
        artifact_id: str,
        *,
        status: str,
        validation_status: str,
    ) -> ArtifactIdentity:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE artifact_ref SET status=%s,validation_status=%s,updated_at=now() "
                "WHERE tenant_id=%s AND artifact_id=%s",
                (status, validation_status, tenant_id, artifact_id),
            )
            if cursor.rowcount != 1:
                raise ArtifactNotFound(f"Artifact not found: {artifact_id}")
            cursor.execute(
                self._artifact_select() + "WHERE tenant_id=%s AND artifact_id=%s",
                (tenant_id, artifact_id),
            )
            return self._artifact_from_row(cursor.fetchone())

    def record_artifact_validation(
        self, tenant_id: str, artifact_id: str, check: ArtifactValidationCheck
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO artifact_validation(tenant_id,artifact_id,validation_type,result,"
                "error_code,failure_reason) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    artifact_id,
                    check.validation_type,
                    "PASS" if check.passed else "FAIL",
                    check.error_code,
                    check.reason,
                ),
            )

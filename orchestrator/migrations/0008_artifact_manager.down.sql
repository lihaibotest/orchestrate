DROP TRIGGER IF EXISTS artifact_state_guard ON artifact_ref;
DROP FUNCTION IF EXISTS guard_artifact_state();
DROP TRIGGER IF EXISTS artifact_identity_guard ON artifact_ref;
DROP FUNCTION IF EXISTS guard_artifact_identity();
DROP TRIGGER IF EXISTS artifact_manager_hydrate ON artifact_ref;
DROP FUNCTION IF EXISTS hydrate_artifact_manager_fields();

DROP TABLE IF EXISTS artifact_validation;
DROP INDEX IF EXISTS idx_artifact_ref_validation;
DROP INDEX IF EXISTS idx_artifact_ref_tenant_execution;

ALTER TABLE artifact_ref
    DROP CONSTRAINT IF EXISTS artifact_ref_stable_output_unique,
    DROP CONSTRAINT IF EXISTS artifact_ref_tenant_identity_unique,
    DROP CONSTRAINT IF EXISTS artifact_ref_validation_status_check,
    DROP CONSTRAINT IF EXISTS artifact_ref_status_check,
    DROP CONSTRAINT IF EXISTS artifact_ref_hash_check,
    DROP CONSTRAINT IF EXISTS artifact_ref_size_check,
    DROP CONSTRAINT IF EXISTS artifact_ref_mode_check,
    DROP CONSTRAINT IF EXISTS artifact_ref_workflow_fk,
    DROP CONSTRAINT IF EXISTS artifact_ref_tenant_fk;

ALTER TABLE artifact_ref
    ADD CONSTRAINT artifact_ref_execution_id_node_id_attempt_no_content_hash_key
        UNIQUE (execution_id,node_id,attempt_no,content_hash);

ALTER TABLE artifact_ref
    DROP COLUMN IF EXISTS updated_at,
    DROP COLUMN IF EXISTS validation_status,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS size_bytes,
    DROP COLUMN IF EXISTS schema_ref,
    DROP COLUMN IF EXISTS media_type,
    DROP COLUMN IF EXISTS artifact_type,
    DROP COLUMN IF EXISTS output_name,
    DROP COLUMN IF EXISTS execution_mode,
    DROP COLUMN IF EXISTS workflow_version,
    DROP COLUMN IF EXISTS workflow_family_id,
    DROP COLUMN IF EXISTS tenant_id,
    DROP COLUMN IF EXISTS storage_ref;

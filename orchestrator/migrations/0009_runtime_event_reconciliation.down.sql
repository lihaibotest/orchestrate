DROP INDEX IF EXISTS idx_runtime_observation_node_time;
DROP INDEX IF EXISTS idx_runtime_observation_execution_time;
DROP TABLE IF EXISTS runtime_observation;
DROP INDEX IF EXISTS uq_runtime_event_external_payload;
DROP INDEX IF EXISTS idx_runtime_event_node_time;
DROP INDEX IF EXISTS idx_runtime_event_tenant_execution_time;
ALTER TABLE runtime_event
    DROP CONSTRAINT IF EXISTS runtime_event_execution_tenant_fk,
    DROP CONSTRAINT IF EXISTS runtime_event_retry_count_check,
    DROP CONSTRAINT IF EXISTS runtime_event_version_check,
    DROP CONSTRAINT IF EXISTS runtime_event_processing_status_check,
    DROP CONSTRAINT IF EXISTS runtime_event_source_check,
    DROP COLUMN IF EXISTS processing_error_message,
    DROP COLUMN IF EXISTS processing_error_code,
    DROP COLUMN IF EXISTS processing_retry_count,
    DROP COLUMN IF EXISTS processing_status,
    DROP COLUMN IF EXISTS event_version,
    DROP COLUMN IF EXISTS payload_hash,
    DROP COLUMN IF EXISTS received_at,
    DROP COLUMN IF EXISTS external_reference,
    DROP COLUMN IF EXISTS correlation_id,
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE execution
    DROP CONSTRAINT IF EXISTS execution_runtime_tenant_unique;

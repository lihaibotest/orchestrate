DROP TRIGGER IF EXISTS execution_identity_guard ON execution;
DROP FUNCTION IF EXISTS guard_execution_identity();

ALTER TABLE node_execution
    DROP CONSTRAINT IF EXISTS node_execution_current_attempt_fk;

DROP INDEX IF EXISTS idx_node_attempt_status;
ALTER TABLE node_attempt
    DROP COLUMN IF EXISTS retry_evidence,
    DROP COLUMN IF EXISTS next_retry_at,
    DROP COLUMN IF EXISTS updated_at;

ALTER TABLE node_execution
    DROP CONSTRAINT IF EXISTS node_execution_agent_identity_check,
    DROP CONSTRAINT IF EXISTS node_execution_current_attempt_positive,
    DROP CONSTRAINT IF EXISTS node_execution_definition_fk,
    DROP COLUMN IF EXISTS checkpoint_id,
    DROP COLUMN IF EXISTS current_attempt,
    DROP COLUMN IF EXISTS agent_version;

DROP TABLE IF EXISTS execution_node_definition;
DROP INDEX IF EXISTS idx_execution_engine_ref;
DROP INDEX IF EXISTS idx_execution_correlation;

ALTER TABLE execution
    DROP CONSTRAINT IF EXISTS execution_snapshot_fk,
    DROP CONSTRAINT IF EXISTS execution_operation_key_unique,
    DROP CONSTRAINT IF EXISTS execution_request_hash_check,
    DROP CONSTRAINT IF EXISTS execution_mode_check,
    DROP COLUMN IF EXISTS engine_status,
    DROP COLUMN IF EXISTS engine_execution_id,
    DROP COLUMN IF EXISTS engine_workflow_id,
    DROP COLUMN IF EXISTS engine,
    DROP COLUMN IF EXISTS operation_key,
    DROP COLUMN IF EXISTS request_hash,
    DROP COLUMN IF EXISTS execution_mode,
    DROP COLUMN IF EXISTS workflow_hash,
    DROP COLUMN IF EXISTS ir_hash,
    DROP COLUMN IF EXISTS registry_snapshot_id;

DROP TABLE IF EXISTS workflow_runtime_definition;

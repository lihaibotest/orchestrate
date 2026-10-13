DROP INDEX IF EXISTS idx_context_outbox_lag;
DROP INDEX IF EXISTS idx_context_event_execution_time;
ALTER TABLE context_outbox
    DROP COLUMN IF EXISTS sent_at,
    DROP COLUMN IF EXISTS last_error,
    DROP COLUMN IF EXISTS lease_until,
    DROP COLUMN IF EXISTS correlation_id,
    DROP COLUMN IF EXISTS tenant_id;
DROP TABLE IF EXISTS cmm_outbox;
DROP TABLE IF EXISTS cmm_context_event;

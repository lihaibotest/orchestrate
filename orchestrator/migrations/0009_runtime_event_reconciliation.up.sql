ALTER TABLE runtime_event
    ADD COLUMN IF NOT EXISTS tenant_id TEXT,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'LEGACY',
    ADD COLUMN IF NOT EXISTS correlation_id TEXT,
    ADD COLUMN IF NOT EXISTS external_reference TEXT,
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS payload_hash TEXT,
    ADD COLUMN IF NOT EXISTS event_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
    ADD COLUMN IF NOT EXISTS processing_retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS processing_error_code TEXT,
    ADD COLUMN IF NOT EXISTS processing_error_message TEXT;

UPDATE runtime_event AS event
SET tenant_id = execution.tenant_id,
    correlation_id = COALESCE(event.correlation_id, execution.correlation_id),
    payload_hash = COALESCE(event.payload_hash, md5(event.payload::text))
FROM execution
WHERE execution.execution_id = event.execution_id;

ALTER TABLE runtime_event
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN payload_hash SET NOT NULL,
    ADD CONSTRAINT runtime_event_source_check
        CHECK (source IN ('WORKFLOW_ENGINE','AGENT_RUNTIME','ARTIFACT_STORE','LEGACY')),
    ADD CONSTRAINT runtime_event_processing_status_check
        CHECK (processing_status IN ('RECEIVED','NORMALIZED','PROCESSED','IGNORED','FAILED_PROCESSING')),
    ADD CONSTRAINT runtime_event_version_check CHECK (event_version > 0),
    ADD CONSTRAINT runtime_event_retry_count_check CHECK (processing_retry_count >= 0);

ALTER TABLE execution
    ADD CONSTRAINT execution_runtime_tenant_unique UNIQUE (execution_id,tenant_id);
ALTER TABLE runtime_event
    ADD CONSTRAINT runtime_event_execution_tenant_fk
        FOREIGN KEY (execution_id,tenant_id)
        REFERENCES execution(execution_id,tenant_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_tenant_execution_time
    ON runtime_event(tenant_id, execution_id, occurred_at, received_at);
CREATE INDEX IF NOT EXISTS idx_runtime_event_node_time
    ON runtime_event(tenant_id, execution_id, node_id, attempt_no, occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_event_external_payload
    ON runtime_event(tenant_id, source, external_reference, payload_hash)
    WHERE external_reference IS NOT NULL;

CREATE TABLE runtime_observation (
    observation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES runtime_event(event_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    execution_id TEXT NOT NULL REFERENCES execution(execution_id),
    node_id TEXT,
    attempt_no INTEGER CHECK (attempt_no IS NULL OR attempt_no > 0),
    observed_state TEXT NOT NULL CHECK (observed_state IN (
        'WORKFLOW_RUNNING','WORKFLOW_SUCCEEDED','WORKFLOW_FAILED',
        'ENGINE_NODE_RUNNING','ENGINE_NODE_SUCCEEDED','ENGINE_NODE_FAILED',
        'AGENT_RUNNING','AGENT_SUCCEEDED','AGENT_FAILED',
        'ARTIFACT_CREATED','ARTIFACT_VALIDATED','ARTIFACT_INVALID'
    )),
    source TEXT NOT NULL CHECK (source IN ('WORKFLOW_ENGINE','AGENT_RUNTIME','ARTIFACT_STORE')),
    confidence TEXT NOT NULL DEFAULT 'AUTHORITATIVE_SOURCE',
    external_reference TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(event_id, observed_state),
    FOREIGN KEY (execution_id,tenant_id)
        REFERENCES execution(execution_id,tenant_id)
);
CREATE INDEX idx_runtime_observation_execution_time
    ON runtime_observation(tenant_id, execution_id, occurred_at, created_at);
CREATE INDEX idx_runtime_observation_node_time
    ON runtime_observation(tenant_id, execution_id, node_id, attempt_no, occurred_at);

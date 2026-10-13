CREATE TABLE cmm_context_event (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'WORKFLOW_VERSION_CREATED','WORKFLOW_SELF_TESTED','EXECUTION_STARTED',
        'NODE_STATE_CHANGED','NODE_EVALUATED','ARTIFACT_REGISTERED',
        'CHECKPOINT_CREATED','WORKFLOW_REPAIR_APPLIED','WORKFLOW_COMPLETED',
        'MEMORY_SUMMARY_UPDATED'
    )),
    tenant_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    workflow_family_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version > 0),
    execution_id TEXT,
    node_id TEXT,
    attempt_no INTEGER CHECK (attempt_no IS NULL OR attempt_no > 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (execution_id) REFERENCES execution(execution_id)
);

CREATE TABLE cmm_outbox (
    event_id TEXT PRIMARY KEY REFERENCES cmm_context_event(event_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    tenant_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','IN_FLIGHT','SENT','FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);

CREATE INDEX idx_context_event_execution_time
    ON cmm_context_event(tenant_id, execution_id, occurred_at);
CREATE INDEX idx_context_outbox_lag
    ON cmm_outbox(status, created_at);

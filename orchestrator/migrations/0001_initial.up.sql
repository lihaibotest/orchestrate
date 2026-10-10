CREATE TABLE IF NOT EXISTS workflow_family (
    workflow_family_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    cmm_context_id TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_family_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_family_tenant_task ON workflow_family (tenant_id, task_id);

CREATE TABLE IF NOT EXISTS registry_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    workflow_family_id TEXT NOT NULL REFERENCES workflow_family(workflow_family_id),
    snapshot_hash TEXT NOT NULL,
    payload_ref TEXT,
    payload_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_family_id, snapshot_id),
    UNIQUE (workflow_family_id, snapshot_hash),
    CHECK (payload_ref IS NOT NULL OR payload_json IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS workflow_version (
    workflow_family_id TEXT NOT NULL REFERENCES workflow_family(workflow_family_id),
    version INTEGER NOT NULL CHECK (version > 0),
    parent_version INTEGER,
    registry_snapshot_id TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    structure_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'VALIDATING', 'SELF_TESTING', 'VERIFIED', 'PUBLISHED', 'SELF_TEST_FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_family_id, version),
    CHECK (parent_version IS NULL OR parent_version < version),
    FOREIGN KEY (workflow_family_id, parent_version) REFERENCES workflow_version(workflow_family_id, version),
    FOREIGN KEY (workflow_family_id, registry_snapshot_id) REFERENCES registry_snapshot(workflow_family_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS execution (
    execution_id TEXT PRIMARY KEY,
    workflow_family_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    correlation_id TEXT,
    client_request_id TEXT NOT NULL,
    n8n_execution_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'PATCHING', 'REPLANNING')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    UNIQUE (tenant_id, workflow_family_id, workflow_version, client_request_id),
    UNIQUE (execution_id, workflow_version),
    FOREIGN KEY (workflow_family_id, workflow_version) REFERENCES workflow_version(workflow_family_id, version),
    FOREIGN KEY (workflow_family_id, tenant_id) REFERENCES workflow_family(workflow_family_id, tenant_id),
    CHECK ((status IN ('SUCCEEDED', 'FAILED') AND ended_at IS NOT NULL) OR status NOT IN ('SUCCEEDED', 'FAILED'))
);
CREATE INDEX IF NOT EXISTS idx_execution_status ON execution(status);

CREATE TABLE IF NOT EXISTS node_execution (
    execution_id TEXT NOT NULL REFERENCES execution(execution_id),
    node_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'READY', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'EVALUATED', 'FAILED', 'INVALIDATED')),
    agent_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, node_id)
);

CREATE TABLE IF NOT EXISTS node_attempt (
    execution_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED')),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_code TEXT,
    error_signature TEXT,
    PRIMARY KEY (execution_id, node_id, attempt_no),
    FOREIGN KEY (execution_id, node_id) REFERENCES node_execution(execution_id, node_id),
    CHECK (ended_at IS NULL OR started_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS artifact_ref (
    artifact_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    uri TEXT NOT NULL CHECK (btrim(uri) !~* '^(data:|inline:)'),
    content_hash TEXT NOT NULL,
    schema_id TEXT,
    schema_version TEXT,
    quality_status TEXT,
    reusable BOOLEAN NOT NULL DEFAULT FALSE,
    input_fingerprint TEXT,
    permission_scope JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (execution_id, node_id, attempt_no, content_hash),
    FOREIGN KEY (execution_id, node_id, attempt_no) REFERENCES node_attempt(execution_id, node_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS checkpoint (
    checkpoint_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES execution(execution_id),
    node_id TEXT NOT NULL,
    artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(artifact_refs) = 'array'),
    input_fingerprint TEXT,
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (execution_id, node_id) REFERENCES node_execution(execution_id, node_id)
);

CREATE TABLE IF NOT EXISTS self_test_run (
    self_test_id TEXT PRIMARY KEY,
    workflow_family_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    level TEXT NOT NULL CHECK (level IN ('S0', 'S1', 'S2', 'S3', 'S4')),
    passed BOOLEAN NOT NULL,
    failure_stage TEXT,
    error_signature TEXT,
    repair_action JSONB,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (workflow_family_id, workflow_version) REFERENCES workflow_version(workflow_family_id, version)
);

CREATE TABLE IF NOT EXISTS runtime_event (
    event_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    node_id TEXT,
    agent_id TEXT,
    attempt_no INTEGER CHECK (attempt_no IS NULL OR attempt_no > 0),
    trace_id TEXT,
    request_id TEXT,
    error_code TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (execution_id) REFERENCES execution(execution_id),
    FOREIGN KEY (execution_id, workflow_version) REFERENCES execution(execution_id, workflow_version)
);
CREATE INDEX IF NOT EXISTS idx_runtime_event_execution_time ON runtime_event(execution_id, occurred_at);

CREATE TABLE IF NOT EXISTS context_outbox (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'IN_FLIGHT', 'SENT', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_context_outbox_pending ON context_outbox(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS state_transition_history (
    transition_id BIGSERIAL PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_immutable_row_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable after creation', TG_TABLE_NAME USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS registry_snapshot_immutable ON registry_snapshot;
CREATE TRIGGER registry_snapshot_immutable BEFORE UPDATE OR DELETE ON registry_snapshot FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();
DROP TRIGGER IF EXISTS workflow_version_immutable ON workflow_version;
CREATE TRIGGER workflow_version_immutable BEFORE UPDATE OR DELETE ON workflow_version FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();

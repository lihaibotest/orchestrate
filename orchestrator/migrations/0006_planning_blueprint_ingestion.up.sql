CREATE TABLE planning_blueprint_ingestion (
    blueprint_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    schema_version TEXT NOT NULL CHECK (schema_version = 'planning-blueprint.v3'),
    session_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    blueprint_hash TEXT NOT NULL,
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    validation_status TEXT NOT NULL CHECK (validation_status IN ('ACCEPTED', 'REJECTED')),
    validation_error JSONB,
    workflow_family_id TEXT,
    workflow_version INTEGER CHECK (workflow_version IS NULL OR workflow_version > 0),
    workflow_reference JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(workflow_reference) = 'object'),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX idx_planning_blueprint_plan ON planning_blueprint_ingestion(tenant_id, plan_id, plan_version);
CREATE INDEX idx_planning_blueprint_correlation ON planning_blueprint_ingestion(tenant_id, correlation_id);
CREATE TRIGGER planning_blueprint_immutable
BEFORE UPDATE OF blueprint_id,tenant_id,plan_id,plan_version,schema_version,session_id,correlation_id,
    idempotency_key,blueprint_hash,payload_json,received_at
ON planning_blueprint_ingestion FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();

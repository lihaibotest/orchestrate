CREATE TABLE workflow_runtime_definition (
    workflow_family_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    registry_snapshot_id TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    nodes JSONB NOT NULL CHECK (jsonb_typeof(nodes) = 'array' AND jsonb_array_length(nodes) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_family_id, workflow_version),
    FOREIGN KEY (workflow_family_id, workflow_version)
        REFERENCES workflow_version(workflow_family_id, version),
    FOREIGN KEY (workflow_family_id, registry_snapshot_id)
        REFERENCES registry_snapshot(workflow_family_id, snapshot_id)
);

CREATE TRIGGER workflow_runtime_definition_immutable
BEFORE UPDATE OR DELETE ON workflow_runtime_definition
FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();

ALTER TABLE execution
    ADD COLUMN registry_snapshot_id TEXT,
    ADD COLUMN ir_hash TEXT,
    ADD COLUMN workflow_hash TEXT,
    ADD COLUMN execution_mode TEXT,
    ADD COLUMN request_hash TEXT,
    ADD COLUMN operation_key TEXT,
    ADD COLUMN engine TEXT,
    ADD COLUMN engine_workflow_id TEXT,
    ADD COLUMN engine_execution_id TEXT,
    ADD COLUMN engine_status TEXT;

UPDATE execution AS execution_row
SET registry_snapshot_id = version.registry_snapshot_id,
    ir_hash = version.ir_hash,
    workflow_hash = COALESCE(publish.workflow_hash, version.ir_hash),
    execution_mode = 'PRODUCTION',
    request_hash = 'legacy:' || execution_row.execution_id,
    operation_key = 'legacy:' || execution_row.execution_id,
    engine = COALESCE(publish.engine, 'legacy'),
    engine_workflow_id = COALESCE(publish.engine_workflow_id, 'legacy:unbound'),
    engine_execution_id = execution_row.n8n_execution_id,
    engine_status = CASE WHEN execution_row.n8n_execution_id IS NULL THEN NULL ELSE 'LEGACY' END
FROM workflow_version AS version
LEFT JOIN workflow_publish_record AS publish
  ON publish.workflow_family_id = version.workflow_family_id
 AND publish.workflow_version = version.version
WHERE version.workflow_family_id = execution_row.workflow_family_id
  AND version.version = execution_row.workflow_version;

ALTER TABLE execution
    ALTER COLUMN registry_snapshot_id SET NOT NULL,
    ALTER COLUMN ir_hash SET NOT NULL,
    ALTER COLUMN workflow_hash SET NOT NULL,
    ALTER COLUMN execution_mode SET NOT NULL,
    ALTER COLUMN request_hash SET NOT NULL,
    ALTER COLUMN operation_key SET NOT NULL,
    ALTER COLUMN engine SET NOT NULL,
    ALTER COLUMN engine_workflow_id SET NOT NULL,
    ADD CONSTRAINT execution_mode_check CHECK (execution_mode = 'PRODUCTION'),
    ADD CONSTRAINT execution_request_hash_check CHECK (btrim(request_hash) <> ''),
    ADD CONSTRAINT execution_operation_key_unique UNIQUE (operation_key),
    ADD CONSTRAINT execution_snapshot_fk
        FOREIGN KEY (workflow_family_id, registry_snapshot_id)
        REFERENCES registry_snapshot(workflow_family_id, snapshot_id);

CREATE INDEX idx_execution_correlation ON execution(tenant_id, correlation_id);
CREATE INDEX idx_execution_engine_ref ON execution(engine, engine_execution_id)
WHERE engine_execution_id IS NOT NULL;

CREATE TABLE execution_node_definition (
    execution_id TEXT NOT NULL REFERENCES execution(execution_id),
    node_id TEXT NOT NULL,
    agent_id TEXT,
    agent_version TEXT,
    credential_ref TEXT,
    max_retries INTEGER NOT NULL DEFAULT 0 CHECK (max_retries >= 0),
    backoff_seconds INTEGER NOT NULL DEFAULT 0 CHECK (backoff_seconds >= 0),
    timeout_seconds INTEGER CHECK (timeout_seconds IS NULL OR timeout_seconds > 0),
    PRIMARY KEY (execution_id, node_id),
    CHECK ((agent_id IS NULL) = (agent_version IS NULL)),
    CHECK (credential_ref IS NULL OR btrim(credential_ref) <> '')
);

INSERT INTO execution_node_definition(execution_id,node_id,agent_id,agent_version)
SELECT execution_id,node_id,agent_id,
       CASE WHEN agent_id IS NULL THEN NULL ELSE 'legacy:unknown' END
FROM node_execution;

ALTER TABLE node_execution
    ADD COLUMN agent_version TEXT,
    ADD COLUMN current_attempt INTEGER,
    ADD COLUMN checkpoint_id TEXT;

UPDATE node_execution
SET agent_version = 'legacy:unknown'
WHERE agent_id IS NOT NULL;

ALTER TABLE node_execution
    ADD CONSTRAINT node_execution_definition_fk
        FOREIGN KEY (execution_id,node_id)
        REFERENCES execution_node_definition(execution_id,node_id),
    ADD CONSTRAINT node_execution_current_attempt_positive
        CHECK (current_attempt IS NULL OR current_attempt > 0),
    ADD CONSTRAINT node_execution_agent_identity_check
        CHECK ((agent_id IS NULL) = (agent_version IS NULL));

ALTER TABLE node_attempt
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN next_retry_at TIMESTAMPTZ,
    ADD COLUMN retry_evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(retry_evidence) = 'object');

ALTER TABLE node_execution
    ADD CONSTRAINT node_execution_current_attempt_fk
        FOREIGN KEY (execution_id,node_id,current_attempt)
        REFERENCES node_attempt(execution_id,node_id,attempt_no)
        DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX idx_node_attempt_status ON node_attempt(execution_id,status,updated_at);

CREATE OR REPLACE FUNCTION guard_execution_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.execution_id,OLD.workflow_family_id,OLD.workflow_version,OLD.tenant_id,
        OLD.correlation_id,OLD.client_request_id,OLD.registry_snapshot_id,OLD.ir_hash,
        OLD.workflow_hash,OLD.execution_mode,OLD.request_hash,OLD.operation_key,
        OLD.engine,OLD.engine_workflow_id,OLD.created_at)
       IS DISTINCT FROM
       (NEW.execution_id,NEW.workflow_family_id,NEW.workflow_version,NEW.tenant_id,
        NEW.correlation_id,NEW.client_request_id,NEW.registry_snapshot_id,NEW.ir_hash,
        NEW.workflow_hash,NEW.execution_mode,NEW.request_hash,NEW.operation_key,
        NEW.engine,NEW.engine_workflow_id,NEW.created_at) THEN
        RAISE EXCEPTION 'execution identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER execution_identity_guard
BEFORE UPDATE ON execution
FOR EACH ROW EXECUTE FUNCTION guard_execution_identity();

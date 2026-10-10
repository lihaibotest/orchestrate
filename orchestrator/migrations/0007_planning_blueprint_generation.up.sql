CREATE TABLE planning_blueprint_generation (
    blueprint_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    blueprint_hash TEXT NOT NULL,
    workflow_family_id TEXT NOT NULL REFERENCES workflow_family(workflow_family_id),
    workflow_version INTEGER NOT NULL CHECK (workflow_version > 0),
    status TEXT NOT NULL CHECK (status IN ('RESERVED','GENERATED','VERIFIED','PUBLISHED','FAILED')),
    registry_snapshot_id TEXT,
    ir_hash TEXT,
    workflow_hash TEXT,
    workflow_reference JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(workflow_reference) = 'object'),
    self_test_id TEXT,
    execution_id TEXT,
    generation_error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, blueprint_id),
    UNIQUE (workflow_family_id, workflow_version),
    UNIQUE (tenant_id, plan_id, plan_version),
    FOREIGN KEY (workflow_family_id, registry_snapshot_id)
        REFERENCES registry_snapshot(workflow_family_id, snapshot_id),
    FOREIGN KEY (self_test_id) REFERENCES self_test_run(self_test_id),
    FOREIGN KEY (execution_id) REFERENCES execution(execution_id)
);
CREATE INDEX idx_planning_blueprint_generation_lineage
    ON planning_blueprint_generation(tenant_id, plan_id, plan_version);
CREATE INDEX idx_planning_blueprint_generation_status
    ON planning_blueprint_generation(status, updated_at);

CREATE OR REPLACE FUNCTION guard_planning_blueprint_generation_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.blueprint_id, OLD.tenant_id, OLD.plan_id, OLD.plan_version, OLD.blueprint_hash,
        OLD.workflow_family_id, OLD.workflow_version, OLD.created_at)
       IS DISTINCT FROM
       (NEW.blueprint_id, NEW.tenant_id, NEW.plan_id, NEW.plan_version, NEW.blueprint_hash,
        NEW.workflow_family_id, NEW.workflow_version, NEW.created_at) THEN
        RAISE EXCEPTION 'planning blueprint generation identity is immutable' USING ERRCODE = '55000';
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER planning_blueprint_generation_identity_guard
BEFORE UPDATE ON planning_blueprint_generation
FOR EACH ROW EXECUTE FUNCTION guard_planning_blueprint_generation_identity();

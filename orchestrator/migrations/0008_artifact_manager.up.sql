ALTER TABLE artifact_ref
    ADD COLUMN storage_ref TEXT,
    ADD COLUMN tenant_id TEXT,
    ADD COLUMN workflow_family_id TEXT,
    ADD COLUMN workflow_version INTEGER,
    ADD COLUMN execution_mode TEXT,
    ADD COLUMN output_name TEXT,
    ADD COLUMN artifact_type TEXT,
    ADD COLUMN media_type TEXT,
    ADD COLUMN schema_ref TEXT,
    ADD COLUMN size_bytes BIGINT,
    ADD COLUMN status TEXT NOT NULL DEFAULT 'CREATED',
    ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'PENDING',
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE artifact_ref AS artifact
SET tenant_id = execution.tenant_id,
    workflow_family_id = execution.workflow_family_id,
    workflow_version = execution.workflow_version,
    execution_mode = execution.execution_mode,
    output_name = artifact.artifact_id,
    artifact_type = 'LEGACY',
    media_type = COALESCE(artifact.metadata->>'media_type', 'application/octet-stream'),
    schema_ref = COALESCE(artifact.schema_id, artifact.schema_version),
    storage_ref = artifact.uri,
    size_bytes = COALESCE((artifact.metadata->>'size_bytes')::bigint, 0),
    status = 'VALIDATED',
    validation_status = 'PASS'
FROM execution
WHERE execution.execution_id = artifact.execution_id;

ALTER TABLE artifact_ref
    ALTER COLUMN storage_ref SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN workflow_family_id SET NOT NULL,
    ALTER COLUMN workflow_version SET NOT NULL,
    ALTER COLUMN execution_mode SET NOT NULL,
    ALTER COLUMN output_name SET NOT NULL,
    ALTER COLUMN artifact_type SET NOT NULL,
    ALTER COLUMN media_type SET NOT NULL,
    ALTER COLUMN size_bytes SET NOT NULL,
    ADD CONSTRAINT artifact_ref_tenant_fk
        FOREIGN KEY (workflow_family_id,tenant_id)
        REFERENCES workflow_family(workflow_family_id,tenant_id),
    ADD CONSTRAINT artifact_ref_workflow_fk
        FOREIGN KEY (workflow_family_id,workflow_version)
        REFERENCES workflow_version(workflow_family_id,version),
    ADD CONSTRAINT artifact_ref_mode_check
        CHECK (execution_mode IN ('PRODUCTION','SELF_TEST')),
    ADD CONSTRAINT artifact_ref_size_check CHECK (size_bytes >= 0),
    ADD CONSTRAINT artifact_ref_hash_check CHECK (btrim(content_hash) <> ''),
    ADD CONSTRAINT artifact_ref_status_check
        CHECK (status IN ('CREATED','VALIDATING','VALIDATED','CONSUMED','INVALID','DELETED')),
    ADD CONSTRAINT artifact_ref_validation_status_check
        CHECK (validation_status IN ('PENDING','PASS','FAIL')),
    ADD CONSTRAINT artifact_ref_tenant_identity_unique UNIQUE (tenant_id,artifact_id);

ALTER TABLE artifact_ref
    DROP CONSTRAINT IF EXISTS artifact_ref_execution_id_node_id_attempt_no_content_hash_key;

ALTER TABLE artifact_ref
    ADD CONSTRAINT artifact_ref_stable_output_unique
        UNIQUE (execution_id,node_id,attempt_no,output_name);

CREATE INDEX idx_artifact_ref_tenant_execution
    ON artifact_ref(tenant_id,execution_id,node_id,attempt_no);
CREATE INDEX idx_artifact_ref_validation
    ON artifact_ref(tenant_id,validation_status,status);

CREATE OR REPLACE FUNCTION hydrate_artifact_manager_fields() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    execution_row execution%ROWTYPE;
BEGIN
    SELECT * INTO execution_row FROM execution WHERE execution_id = NEW.execution_id;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    NEW.tenant_id := COALESCE(NEW.tenant_id, execution_row.tenant_id);
    NEW.workflow_family_id := COALESCE(NEW.workflow_family_id, execution_row.workflow_family_id);
    NEW.workflow_version := COALESCE(NEW.workflow_version, execution_row.workflow_version);
    NEW.execution_mode := COALESCE(NEW.execution_mode, execution_row.execution_mode);
    NEW.output_name := COALESCE(NEW.output_name, NEW.artifact_id);
    NEW.artifact_type := COALESCE(NEW.artifact_type, 'LEGACY');
    NEW.media_type := COALESCE(NEW.media_type, NEW.metadata->>'media_type', 'application/octet-stream');
    NEW.schema_ref := COALESCE(NEW.schema_ref, NEW.schema_id, NEW.schema_version);
    NEW.size_bytes := COALESCE(NEW.size_bytes, (NEW.metadata->>'size_bytes')::bigint, 0);
    NEW.storage_ref := COALESCE(NEW.storage_ref, NEW.uri);
    NEW.uri := COALESCE(NEW.uri, NEW.storage_ref);
    RETURN NEW;
END;
$$;

CREATE TRIGGER artifact_manager_hydrate
BEFORE INSERT ON artifact_ref
FOR EACH ROW EXECUTE FUNCTION hydrate_artifact_manager_fields();

CREATE TABLE artifact_validation (
    validation_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    validation_type TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
    error_code TEXT,
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,artifact_id)
        REFERENCES artifact_ref(tenant_id,artifact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_artifact_validation_artifact
    ON artifact_validation(tenant_id,artifact_id,created_at);

CREATE OR REPLACE FUNCTION guard_artifact_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.artifact_id,OLD.tenant_id,OLD.workflow_family_id,OLD.workflow_version,
        OLD.execution_id,OLD.execution_mode,OLD.node_id,OLD.attempt_no,OLD.output_name,
        OLD.artifact_type,OLD.media_type,OLD.schema_ref,OLD.uri,OLD.storage_ref,OLD.content_hash,
        OLD.size_bytes,OLD.created_at)
       IS DISTINCT FROM
       (NEW.artifact_id,NEW.tenant_id,NEW.workflow_family_id,NEW.workflow_version,
        NEW.execution_id,NEW.execution_mode,NEW.node_id,NEW.attempt_no,NEW.output_name,
        NEW.artifact_type,NEW.media_type,NEW.schema_ref,NEW.uri,NEW.storage_ref,NEW.content_hash,
        NEW.size_bytes,NEW.created_at) THEN
        RAISE EXCEPTION 'artifact identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER artifact_identity_guard
BEFORE UPDATE ON artifact_ref
FOR EACH ROW EXECUTE FUNCTION guard_artifact_identity();

CREATE OR REPLACE FUNCTION guard_artifact_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = NEW.status AND OLD.validation_status = NEW.validation_status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status,NEW.status) NOT IN (
        ('CREATED','VALIDATING'),('CREATED','DELETED'),
        ('VALIDATING','VALIDATED'),('VALIDATING','INVALID'),
        ('VALIDATED','VALIDATING'),('VALIDATED','CONSUMED'),('VALIDATED','DELETED'),
        ('CONSUMED','VALIDATING'),('CONSUMED','CONSUMED'),('CONSUMED','DELETED'),
        ('INVALID','VALIDATING'),('INVALID','DELETED')
    ) THEN
        RAISE EXCEPTION 'illegal artifact transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER artifact_state_guard
BEFORE UPDATE OF status,validation_status ON artifact_ref
FOR EACH ROW EXECUTE FUNCTION guard_artifact_state();

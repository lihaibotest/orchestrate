ALTER TABLE self_test_run
    ADD COLUMN tenant_id TEXT,
    ADD COLUMN registry_snapshot_id TEXT,
    ADD COLUMN ir_hash TEXT,
    ADD COLUMN workflow_hash TEXT,
    ADD COLUMN status TEXT,
    ADD COLUMN current_stage TEXT,
    ADD COLUMN repair_attempt INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN idempotency_key TEXT,
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN completed_at TIMESTAMPTZ,
    ADD COLUMN evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE self_test_run AS run
SET tenant_id = family.tenant_id,
    registry_snapshot_id = version.registry_snapshot_id,
    ir_hash = version.ir_hash,
    workflow_hash = version.ir_hash,
    status = CASE WHEN run.passed THEN 'PASSED' ELSE 'FAILED' END,
    current_stage = run.level,
    idempotency_key = 'legacy:' || run.self_test_id,
    started_at = run.created_at,
    completed_at = run.created_at
FROM workflow_version AS version
JOIN workflow_family AS family
  ON family.workflow_family_id = version.workflow_family_id
WHERE version.workflow_family_id = run.workflow_family_id
  AND version.version = run.workflow_version;

ALTER TABLE self_test_run
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN registry_snapshot_id SET NOT NULL,
    ALTER COLUMN ir_hash SET NOT NULL,
    ALTER COLUMN workflow_hash SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN current_stage SET NOT NULL,
    ALTER COLUMN idempotency_key SET NOT NULL,
    ALTER COLUMN started_at SET NOT NULL;

ALTER TABLE self_test_run
    ADD CONSTRAINT self_test_run_tenant_fk
        FOREIGN KEY (workflow_family_id, tenant_id)
        REFERENCES workflow_family(workflow_family_id, tenant_id),
    ADD CONSTRAINT self_test_run_snapshot_fk
        FOREIGN KEY (workflow_family_id, registry_snapshot_id)
        REFERENCES registry_snapshot(workflow_family_id, snapshot_id),
    ADD CONSTRAINT self_test_run_status_check
        CHECK (status IN ('RUNNING', 'PASSED', 'FAILED')),
    ADD CONSTRAINT self_test_run_stage_check
        CHECK (current_stage IN ('S0', 'S1', 'S2', 'S3', 'S4')),
    ADD CONSTRAINT self_test_run_repair_attempt_check
        CHECK (repair_attempt >= 0),
    ADD CONSTRAINT self_test_run_completion_check
        CHECK (
            (status = 'RUNNING' AND completed_at IS NULL) OR
            (status IN ('PASSED', 'FAILED') AND completed_at IS NOT NULL)
        ),
    ADD CONSTRAINT self_test_run_failure_check
        CHECK (
            (status = 'FAILED' AND error_signature IS NOT NULL AND failure_stage IS NOT NULL) OR
            status <> 'FAILED'
        ),
    ADD CONSTRAINT self_test_run_evidence_refs_check
        CHECK (jsonb_typeof(evidence_refs) = 'array'),
    ADD CONSTRAINT self_test_run_idempotency_unique
        UNIQUE (tenant_id, idempotency_key);

CREATE UNIQUE INDEX idx_self_test_one_active_version
ON self_test_run(workflow_family_id, workflow_version)
WHERE status = 'RUNNING';

CREATE INDEX idx_self_test_family_version_status
ON self_test_run(workflow_family_id, workflow_version, status, created_at DESC);

CREATE INDEX idx_self_test_failure_signature
ON self_test_run(error_signature, created_at DESC)
WHERE error_signature IS NOT NULL;

CREATE TABLE self_test_stage_result (
    self_test_id TEXT NOT NULL REFERENCES self_test_run(self_test_id),
    stage TEXT NOT NULL CHECK (stage IN ('S0', 'S1', 'S2', 'S3', 'S4')),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'SKIPPED')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL CHECK (duration_ms >= 0),
    error_code TEXT,
    message TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array'),
    failure_signature TEXT,
    PRIMARY KEY (self_test_id, stage),
    CHECK (completed_at >= started_at),
    CHECK (
        (status = 'FAIL' AND error_code IS NOT NULL AND failure_signature IS NOT NULL) OR
        (status <> 'FAIL' AND failure_signature IS NULL)
    )
);

CREATE INDEX idx_self_test_stage_failure
ON self_test_stage_result(failure_signature)
WHERE failure_signature IS NOT NULL;

CREATE TABLE workflow_repair_record (
    repair_id TEXT PRIMARY KEY,
    self_test_id TEXT NOT NULL REFERENCES self_test_run(self_test_id),
    workflow_family_id TEXT NOT NULL,
    source_workflow_version INTEGER NOT NULL,
    target_workflow_version INTEGER,
    repair_attempt INTEGER NOT NULL CHECK (repair_attempt > 0),
    failure_signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('RETRY_UNCHANGED', 'NEW_VERSION', 'NO_REPAIR', 'FAILED', 'REPAIR_LIMIT_EXCEEDED')
    ),
    reason TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (self_test_id, repair_attempt),
    FOREIGN KEY (workflow_family_id, source_workflow_version)
        REFERENCES workflow_version(workflow_family_id, version),
    FOREIGN KEY (workflow_family_id, target_workflow_version)
        REFERENCES workflow_version(workflow_family_id, version),
    CHECK (
        (status = 'NEW_VERSION' AND target_workflow_version IS NOT NULL) OR
        (status <> 'NEW_VERSION' AND target_workflow_version IS NULL)
    )
);

CREATE INDEX idx_workflow_repair_failure
ON workflow_repair_record(failure_signature, created_at DESC);

CREATE TABLE workflow_publish_record (
    workflow_family_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    registry_snapshot_id TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    workflow_hash TEXT NOT NULL,
    engine TEXT NOT NULL,
    engine_workflow_id TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'PUBLISHED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    PRIMARY KEY (workflow_family_id, workflow_version),
    FOREIGN KEY (workflow_family_id, workflow_version)
        REFERENCES workflow_version(workflow_family_id, version),
    FOREIGN KEY (workflow_family_id, registry_snapshot_id)
        REFERENCES registry_snapshot(workflow_family_id, snapshot_id),
    CHECK (
        (status = 'PUBLISHED' AND published_at IS NOT NULL) OR
        (status = 'PENDING' AND published_at IS NULL)
    )
);

CREATE OR REPLACE FUNCTION guard_workflow_version_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workflow_version rows cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF (OLD.workflow_family_id, OLD.version, OLD.parent_version, OLD.registry_snapshot_id,
        OLD.ir_hash, OLD.structure_ref, OLD.created_at)
       IS DISTINCT FROM
       (NEW.workflow_family_id, NEW.version, NEW.parent_version, NEW.registry_snapshot_id,
        NEW.ir_hash, NEW.structure_ref, NEW.created_at) THEN
        RAISE EXCEPTION 'workflow_version structure is immutable' USING ERRCODE = '55000';
    END IF;
    IF NOT (
        (OLD.status = 'DRAFT' AND NEW.status = 'VALIDATING') OR
        (OLD.status = 'VALIDATING' AND NEW.status IN ('SELF_TESTING', 'SELF_TEST_FAILED')) OR
        (OLD.status = 'SELF_TESTING' AND NEW.status IN ('VERIFIED', 'SELF_TEST_FAILED')) OR
        (OLD.status = 'SELF_TEST_FAILED' AND NEW.status = 'SELF_TESTING') OR
        (OLD.status = 'VERIFIED' AND NEW.status = 'PUBLISHED')
    ) THEN
        RAISE EXCEPTION 'invalid workflow_version transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

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
        (OLD.status = 'VERIFIED' AND NEW.status = 'PUBLISHED')
    ) THEN
        RAISE EXCEPTION 'invalid workflow_version transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TABLE IF EXISTS workflow_publish_record;
DROP TABLE IF EXISTS workflow_repair_record;
DROP TABLE IF EXISTS self_test_stage_result;

DROP INDEX IF EXISTS idx_self_test_failure_signature;
DROP INDEX IF EXISTS idx_self_test_family_version_status;
DROP INDEX IF EXISTS idx_self_test_one_active_version;

ALTER TABLE self_test_run
    DROP CONSTRAINT IF EXISTS self_test_run_idempotency_unique,
    DROP CONSTRAINT IF EXISTS self_test_run_evidence_refs_check,
    DROP CONSTRAINT IF EXISTS self_test_run_failure_check,
    DROP CONSTRAINT IF EXISTS self_test_run_completion_check,
    DROP CONSTRAINT IF EXISTS self_test_run_repair_attempt_check,
    DROP CONSTRAINT IF EXISTS self_test_run_stage_check,
    DROP CONSTRAINT IF EXISTS self_test_run_status_check,
    DROP CONSTRAINT IF EXISTS self_test_run_snapshot_fk,
    DROP CONSTRAINT IF EXISTS self_test_run_tenant_fk;

ALTER TABLE self_test_run
    DROP COLUMN IF EXISTS evidence_refs,
    DROP COLUMN IF EXISTS completed_at,
    DROP COLUMN IF EXISTS started_at,
    DROP COLUMN IF EXISTS idempotency_key,
    DROP COLUMN IF EXISTS repair_attempt,
    DROP COLUMN IF EXISTS current_stage,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS workflow_hash,
    DROP COLUMN IF EXISTS ir_hash,
    DROP COLUMN IF EXISTS registry_snapshot_id,
    DROP COLUMN IF EXISTS tenant_id;

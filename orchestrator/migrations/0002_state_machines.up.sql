DROP TRIGGER IF EXISTS workflow_version_immutable ON workflow_version;

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

CREATE TRIGGER workflow_version_state_guard
BEFORE UPDATE OR DELETE ON workflow_version
FOR EACH ROW EXECUTE FUNCTION guard_workflow_version_transition();

CREATE OR REPLACE FUNCTION guard_execution_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (
        (OLD.status = 'CREATED' AND NEW.status IN ('RUNNING', 'FAILED')) OR
        (OLD.status = 'RUNNING' AND NEW.status IN ('SUCCEEDED', 'FAILED', 'PATCHING', 'REPLANNING')) OR
        (OLD.status = 'PATCHING' AND NEW.status IN ('RUNNING', 'FAILED')) OR
        (OLD.status = 'REPLANNING' AND NEW.status IN ('RUNNING', 'FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid execution transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS execution_state_guard ON execution;
CREATE TRIGGER execution_state_guard
BEFORE UPDATE OF status ON execution
FOR EACH ROW EXECUTE FUNCTION guard_execution_transition();

CREATE OR REPLACE FUNCTION guard_node_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (
        (OLD.status = 'PENDING' AND NEW.status = 'READY') OR
        (OLD.status = 'READY' AND NEW.status = 'RUNNING') OR
        (OLD.status = 'RUNNING' AND NEW.status IN ('SUCCEEDED', 'RETRY_WAIT', 'FAILED')) OR
        (OLD.status = 'RETRY_WAIT' AND NEW.status IN ('RUNNING', 'FAILED')) OR
        (OLD.status = 'SUCCEEDED' AND NEW.status IN ('EVALUATED', 'INVALIDATED')) OR
        (OLD.status = 'EVALUATED' AND NEW.status = 'INVALIDATED')
    ) THEN
        RAISE EXCEPTION 'invalid node transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS node_execution_state_guard ON node_execution;
CREATE TRIGGER node_execution_state_guard
BEFORE UPDATE OF status ON node_execution
FOR EACH ROW EXECUTE FUNCTION guard_node_transition();

CREATE INDEX IF NOT EXISTS idx_state_transition_aggregate
ON state_transition_history(aggregate_type, aggregate_id, transition_id);

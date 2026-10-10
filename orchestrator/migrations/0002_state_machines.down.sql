DROP INDEX IF EXISTS idx_state_transition_aggregate;

DROP TRIGGER IF EXISTS node_execution_state_guard ON node_execution;
DROP FUNCTION IF EXISTS guard_node_transition();

DROP TRIGGER IF EXISTS execution_state_guard ON execution;
DROP FUNCTION IF EXISTS guard_execution_transition();

DROP TRIGGER IF EXISTS workflow_version_state_guard ON workflow_version;
DROP FUNCTION IF EXISTS guard_workflow_version_transition();

CREATE TRIGGER workflow_version_immutable
BEFORE UPDATE OR DELETE ON workflow_version
FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();

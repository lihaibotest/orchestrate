ALTER TABLE registry_snapshot
ADD COLUMN hash_algorithm TEXT NOT NULL DEFAULT 'sha256'
CHECK (hash_algorithm = 'sha256');

CREATE TABLE agent_snapshot_binding (
    snapshot_id TEXT NOT NULL REFERENCES registry_snapshot(snapshot_id),
    requirement_id TEXT NOT NULL CHECK (btrim(requirement_id) <> ''),
    agent_id TEXT NOT NULL CHECK (btrim(agent_id) <> ''),
    version TEXT NOT NULL CHECK (btrim(version) <> ''),
    endpoint TEXT NOT NULL CHECK (btrim(endpoint) <> ''),
    capability TEXT NOT NULL CHECK (btrim(capability) <> ''),
    input_schema JSONB NOT NULL CHECK (jsonb_typeof(input_schema) = 'object'),
    output_schema JSONB NOT NULL CHECK (jsonb_typeof(output_schema) = 'object'),
    credential_ref TEXT NOT NULL CHECK (btrim(credential_ref) <> ''),
    invocation_policy JSONB NOT NULL CHECK (jsonb_typeof(invocation_policy) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id, requirement_id)
);

CREATE INDEX idx_agent_snapshot_binding_agent
ON agent_snapshot_binding(agent_id, version, capability);

CREATE TRIGGER agent_snapshot_binding_immutable
BEFORE UPDATE OR DELETE ON agent_snapshot_binding
FOR EACH ROW EXECUTE FUNCTION reject_immutable_row_update();

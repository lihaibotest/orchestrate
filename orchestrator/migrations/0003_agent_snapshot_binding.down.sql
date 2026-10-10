DROP TRIGGER IF EXISTS agent_snapshot_binding_immutable ON agent_snapshot_binding;
DROP TABLE IF EXISTS agent_snapshot_binding;
ALTER TABLE registry_snapshot DROP COLUMN IF EXISTS hash_algorithm;

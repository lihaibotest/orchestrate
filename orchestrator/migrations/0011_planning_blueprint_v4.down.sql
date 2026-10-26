DELETE FROM planning_blueprint_ingestion WHERE schema_version = 'planning-blueprint.v4';
ALTER TABLE planning_blueprint_ingestion
    DROP CONSTRAINT IF EXISTS planning_blueprint_ingestion_schema_version_check;
ALTER TABLE planning_blueprint_ingestion
    ADD CONSTRAINT planning_blueprint_ingestion_schema_version_check
    CHECK (schema_version = 'planning-blueprint.v3');

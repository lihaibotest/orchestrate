DROP TRIGGER IF EXISTS planning_blueprint_immutable ON planning_blueprint_ingestion;
DROP INDEX IF EXISTS idx_planning_blueprint_correlation;
DROP INDEX IF EXISTS idx_planning_blueprint_plan;
DROP TABLE IF EXISTS planning_blueprint_ingestion;

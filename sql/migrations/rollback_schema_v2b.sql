-- Phase 3B Rollback
-- Run: psql -U claude_agent -d agent_memory -f rollback_schema_v2b.sql

BEGIN;

ALTER TABLE memories DROP COLUMN IF EXISTS project_id;
ALTER TABLE sessions DROP COLUMN IF EXISTS project_id;
ALTER TABLE reflections DROP COLUMN IF EXISTS project_id;
ALTER TABLE error_solutions DROP COLUMN IF EXISTS project_id;
DROP TABLE IF EXISTS relationships CASCADE;
DROP TABLE IF EXISTS entities CASCADE;
DROP TABLE IF EXISTS project_artifacts CASCADE;
DROP TABLE IF EXISTS projects CASCADE;

COMMIT;

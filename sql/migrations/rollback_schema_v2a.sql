-- DOWN migration for Phase 3A (add_schema_v2a.sql)
-- Date: 2026-03-24
-- Purpose: Revert all changes made by add_schema_v2a.sql.
-- WARNING: This will DROP the error_solutions table and remove columns from memories/reflections.
-- Data in these columns/tables will be LOST.
-- Run: psql -U claude_agent -d agent_memory -f rollback_schema_v2a.sql

ALTER TABLE memories DROP COLUMN IF EXISTS retrieval_mode;
ALTER TABLE memories DROP COLUMN IF EXISTS tool_name;
ALTER TABLE reflections DROP COLUMN IF EXISTS task_category;
DROP TABLE IF EXISTS error_solutions CASCADE;
-- pg_trgm extension left in place (harmless, may be used by other tables)

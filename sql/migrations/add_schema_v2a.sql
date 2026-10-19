-- Phase 3A: Schema v2a — Error Solutions Table + Retrieval Mode Columns
-- Date: 2026-03-24
-- Purpose: Add error_solutions table, retrieval_mode/tool_name to memories,
--          task_category to reflections.
-- Prerequisite migrations: add_agent_identity.sql, add_injection_cooldown.sql
-- DO NOT RUN without first applying all prerequisite migrations.
-- Run: psql -U claude_agent -d agent_memory -f add_schema_v2a.sql

-- ============================================================
-- 1. EXTENSION: pg_trgm (trigram fuzzy search for error patterns)
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- 2. CREATE error_solutions table
-- ============================================================
CREATE TABLE IF NOT EXISTS error_solutions (
    id              SERIAL PRIMARY KEY,
    error_pattern   TEXT NOT NULL,
    error_example   TEXT,
    solution        TEXT NOT NULL,
    tool_name       TEXT,
    platform        TEXT,
    tags            TEXT[],
    verified        BOOLEAN DEFAULT FALSE,
    hit_count       INTEGER DEFAULT 0,
    last_hit        TIMESTAMPTZ,
    source_memory   INTEGER REFERENCES memories(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id)
);

CREATE INDEX IF NOT EXISTS idx_errors_tool    ON error_solutions(tool_name);
CREATE INDEX IF NOT EXISTS idx_errors_pattern ON error_solutions USING gin(error_pattern gin_trgm_ops);

-- ============================================================
-- 3. ALTER memories — add retrieval_mode and tool_name
--    NOTE: last_injected_at already added in add_injection_cooldown.sql — not repeated here.
--    NOTE: project_id deferred to Phase 3B (projects table not yet created).
-- ============================================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='memories' AND column_name='retrieval_mode') THEN
        ALTER TABLE memories ADD COLUMN retrieval_mode TEXT DEFAULT 'semantic';
        ALTER TABLE memories ADD CONSTRAINT chk_memories_retrieval_mode
            CHECK (retrieval_mode IN ('semantic', 'tool', 'manual'));
    END IF;
END $$;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tool_name TEXT;

-- ============================================================
-- 4. ALTER reflections — add task_category
-- ============================================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='reflections' AND column_name='task_category') THEN
        ALTER TABLE reflections ADD COLUMN task_category TEXT;
        ALTER TABLE reflections ADD CONSTRAINT chk_reflections_task_category
            CHECK (task_category IN (
                'implementation','debugging','research','design','testing',
                'deployment','configuration','documentation','review','ingestion',
                'automation','optimization','integration','maintenance'));
    END IF;
END $$;

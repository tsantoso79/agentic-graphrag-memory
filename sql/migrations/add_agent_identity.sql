-- Migration: Add Multi-Agent Identity
-- Adds agent provenance tracking to all agent-specific tables.
-- Run: psql -U claude_agent -d agent_memory -f add_agent_identity.sql
-- Prerequisite: Close all Claude Code sessions first.

BEGIN;

-- ============================================================
-- 1. CREATE agents table
-- ============================================================
CREATE TABLE IF NOT EXISTS agents (
    agent_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    agent_type      TEXT NOT NULL,       -- 'claude-code', 'openclaw', etc.
    environment     TEXT NOT NULL,       -- 'windows', 'wsl', 'docker'
    default_model   TEXT,                -- 'opus', 'sonnet', etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ
);

-- ============================================================
-- 2. Seed agent rows
-- ============================================================
INSERT INTO agents (agent_id, display_name, agent_type, environment, default_model)
VALUES
    ('claude-win',       'Claude Code (Windows)', 'claude-code', 'windows', 'opus'),
    ('claude-wsl',       'Claude Code (WSL)',     'claude-code', 'wsl',     'opus'),
    ('openclaw-docker',  'OpenClaw (Docker)',      'openclaw',   'docker',  'sonnet')
ON CONFLICT (agent_id) DO NOTHING;

-- ============================================================
-- 3. ADD COLUMN agent_id (nullable) to 6 tables
-- ============================================================
ALTER TABLE sessions            ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE memories            ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE reflections         ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE pending_transcripts ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE backtest_results    ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE strategy_evolution  ADD COLUMN IF NOT EXISTS agent_id TEXT;

-- ============================================================
-- 4. Backfill existing rows as 'claude-win'
-- ============================================================
UPDATE sessions            SET agent_id = 'claude-win' WHERE agent_id IS NULL;
UPDATE memories            SET agent_id = 'claude-win' WHERE agent_id IS NULL;
UPDATE reflections         SET agent_id = 'claude-win' WHERE agent_id IS NULL;
UPDATE pending_transcripts SET agent_id = 'claude-win' WHERE agent_id IS NULL;
UPDATE backtest_results    SET agent_id = 'claude-win' WHERE agent_id IS NULL;
UPDATE strategy_evolution  SET agent_id = 'claude-win' WHERE agent_id IS NULL;

-- ============================================================
-- 5. SET NOT NULL + FK constraints
-- ============================================================
ALTER TABLE sessions            ALTER COLUMN agent_id SET NOT NULL;
ALTER TABLE memories            ALTER COLUMN agent_id SET NOT NULL;
ALTER TABLE reflections         ALTER COLUMN agent_id SET NOT NULL;
ALTER TABLE pending_transcripts ALTER COLUMN agent_id SET NOT NULL;
ALTER TABLE backtest_results    ALTER COLUMN agent_id SET NOT NULL;
ALTER TABLE strategy_evolution  ALTER COLUMN agent_id SET NOT NULL;

-- strategy_evolution is written via MCP raw SQL (not memory_helper.py),
-- so it needs a DEFAULT to avoid NOT NULL violations from ad-hoc INSERTs.
ALTER TABLE strategy_evolution  ALTER COLUMN agent_id SET DEFAULT 'claude-win';

-- FK constraints (idempotent: skip if already exists)
DO $$ BEGIN ALTER TABLE sessions            ADD CONSTRAINT fk_sessions_agent            FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE memories            ADD CONSTRAINT fk_memories_agent            FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE reflections         ADD CONSTRAINT fk_reflections_agent         FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE pending_transcripts ADD CONSTRAINT fk_pending_transcripts_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE backtest_results    ADD CONSTRAINT fk_backtest_results_agent    FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE strategy_evolution  ADD CONSTRAINT fk_strategy_evolution_agent  FOREIGN KEY (agent_id) REFERENCES agents(agent_id); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- 6. CREATE INDEX on agent_id for all 6 tables
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_sessions_agent            ON sessions (agent_id);
CREATE INDEX IF NOT EXISTS idx_memories_agent            ON memories (agent_id);
CREATE INDEX IF NOT EXISTS idx_reflections_agent         ON reflections (agent_id);
CREATE INDEX IF NOT EXISTS idx_pending_transcripts_agent ON pending_transcripts (agent_id);
CREATE INDEX IF NOT EXISTS idx_backtest_results_agent    ON backtest_results (agent_id);
CREATE INDEX IF NOT EXISTS idx_strategy_evolution_agent  ON strategy_evolution (agent_id);

COMMIT;

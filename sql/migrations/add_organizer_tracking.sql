-- Migration: organizer_runs table for Phase 6 Memory Organizing Agent
-- Tracks each run of memory-organize with action counts and full action log

CREATE TABLE IF NOT EXISTS organizer_runs (
    id              SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    phase           TEXT NOT NULL CHECK (phase IN ('A', 'B', 'AB')),
    dry_run         BOOLEAN NOT NULL DEFAULT TRUE,
    dupes_merged    INTEGER DEFAULT 0,
    hot_suppressed  INTEGER DEFAULT 0,
    stale_decayed   INTEGER DEFAULT 0,
    ttl_applied     INTEGER DEFAULT 0,
    clusters_consolidated INTEGER DEFAULT 0,
    contradictions_resolved INTEGER DEFAULT 0,
    stale_assessed  INTEGER DEFAULT 0,
    total_actions   INTEGER DEFAULT 0,
    sonnet_cost_usd NUMERIC(6,4),
    actions_log     JSONB,
    error_message   TEXT,
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id)
);

BEGIN;

-- 1. Exposure tracking table
CREATE TABLE IF NOT EXISTS memory_exposures (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    memory_id       INTEGER NOT NULL REFERENCES memories(id),
    source          TEXT NOT NULL CHECK (source IN ('startup', 'inject', 'recall')),
    turn_number     INTEGER,
    similarity      NUMERIC(5,3),
    useful          BOOLEAN,
    scored_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exposures_session ON memory_exposures (session_id);
CREATE INDEX IF NOT EXISTS idx_exposures_memory  ON memory_exposures (memory_id);
CREATE INDEX IF NOT EXISTS idx_exposures_unscored ON memory_exposures (session_id) WHERE useful IS NULL;

-- 2. Three counter columns on memories + useful_pct
ALTER TABLE memories ADD COLUMN IF NOT EXISTS startup_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS inject_count  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS recall_count  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS useful_pct    NUMERIC(5,2);

-- 3. Backfill: assign existing recalled_count to inject_count
--    (known inaccuracy: some were manual recalls, but most were turn-recall)
UPDATE memories SET inject_count = recalled_count WHERE recalled_count > 0;

COMMIT;

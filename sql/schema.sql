-- Agent Memory Schema v1.0
-- PostgreSQL 18 + pgvector
-- Run: psql -U claude_agent -d agent_memory -f schema.sql

-- ============================================================
-- EXTENSIONS
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. SESSIONS - Track each Claude Code session
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    summary         TEXT,
    outcome         TEXT CHECK (outcome IN ('success', 'partial', 'failed', 'abandoned')),
    tasks_completed TEXT[],
    tags            TEXT[],
    token_estimate  INTEGER,
    project         TEXT                   -- working directory / project path
);

CREATE INDEX idx_sessions_started ON sessions (started_at DESC);
CREATE INDEX idx_sessions_tags ON sessions USING GIN (tags);
CREATE INDEX idx_sessions_project ON sessions (project);

-- ============================================================
-- 2. MEMORIES - Persistent facts, decisions, context, error fixes
-- ============================================================
CREATE TABLE IF NOT EXISTS memories (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content         TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN (
                        'fact', 'decision', 'preference', 'context',
                        'bug', 'config', 'pattern', 'tool_usage'
                    )),
    domain          TEXT NOT NULL DEFAULT 'general' CHECK (domain IN (
                        'backtesting', 'ea_development', 'optimization',
                        'tool_errors', 'environment', 'problem_solving',
                        'debugging', 'general'
                    )),
    importance      INTEGER NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    tags            TEXT[],
    source_session  TEXT,
    error_signature TEXT,              -- searchable error pattern string
    superseded_by   INTEGER REFERENCES memories(id),
    ttl_days        INTEGER,           -- NULL = permanent
    recalled_count  INTEGER NOT NULL DEFAULT 0,
    last_recalled   TIMESTAMPTZ,
    last_injected_at TIMESTAMPTZ,      -- cooldown: suppress re-injection within 2 hours
    search_text     TSVECTOR,          -- populated by trigger
    embedding       vector(512),       -- model2vec potion-base-32M (512-dim)
    project         TEXT                   -- working directory / project path
);

CREATE INDEX idx_memories_category ON memories (category);
CREATE INDEX idx_memories_domain ON memories (domain);
CREATE INDEX idx_memories_importance ON memories (importance DESC);
CREATE INDEX idx_memories_tags ON memories USING GIN (tags);
CREATE INDEX idx_memories_search ON memories USING GIN (search_text);
CREATE INDEX idx_memories_error_sig ON memories (error_signature) WHERE error_signature IS NOT NULL;
CREATE INDEX idx_memories_active ON memories (importance DESC, updated_at DESC)
    WHERE superseded_by IS NULL;
CREATE INDEX idx_memories_project ON memories (project);
CREATE INDEX idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops);

-- Auto-populate tsvector on insert/update
CREATE OR REPLACE FUNCTION memories_search_trigger() RETURNS trigger AS $$
BEGIN
    -- Only recompute tsvector and updated_at when content actually changes
    IF TG_OP = 'INSERT'
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.error_signature IS DISTINCT FROM OLD.error_signature
       OR NEW.tags IS DISTINCT FROM OLD.tags THEN
        NEW.search_text := to_tsvector('english',
            COALESCE(NEW.content, '') || ' ' ||
            COALESCE(NEW.error_signature, '') || ' ' ||
            COALESCE(array_to_string(NEW.tags, ' '), '')
        );
        NEW.updated_at := NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memories_search
    BEFORE INSERT OR UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_search_trigger();

-- ============================================================
-- 3. REFLECTIONS - Post-task self-evaluations
-- ============================================================
CREATE TABLE IF NOT EXISTS reflections (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT,
    task_type       TEXT NOT NULL,      -- 'backtest', 'debugging', 'coding', etc.
    domain          TEXT NOT NULL DEFAULT 'general' CHECK (domain IN (
                        'backtesting', 'ea_development', 'optimization',
                        'tool_errors', 'environment', 'problem_solving',
                        'debugging', 'general'
                    )),
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'partial', 'failed')),
    what_worked     TEXT,
    what_failed     TEXT,
    lessons         TEXT[],
    confidence      INTEGER CHECK (confidence BETWEEN 1 AND 10),
    tags            TEXT[],
    project         TEXT
);

CREATE INDEX idx_reflections_domain ON reflections (domain);
CREATE INDEX idx_reflections_task_type ON reflections (task_type);
CREATE INDEX idx_reflections_created ON reflections (created_at DESC);
CREATE INDEX idx_reflections_tags ON reflections USING GIN (tags);
CREATE INDEX idx_reflections_project ON reflections (project);

-- ============================================================
-- 4. BACKTEST_RESULTS - Structured MT4/Python backtest data
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_results (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT,
    ea_name         TEXT NOT NULL,
    ea_version      TEXT,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    date_from       DATE,
    date_to         DATE,
    source          TEXT CHECK (source IN ('mt4', 'python', 'manual')),
    -- Core metrics
    total_trades    INTEGER,
    profit_factor   NUMERIC(8,4),
    net_profit      NUMERIC(12,2),
    max_drawdown_pct NUMERIC(6,2),
    win_rate        NUMERIC(6,2),
    sharpe_ratio    NUMERIC(8,4),
    avg_trade       NUMERIC(10,2),
    max_consec_losses INTEGER,
    -- Flexible params
    parameters      JSONB,             -- EA input parameters
    extra_metrics   JSONB,             -- any additional metrics
    notes           TEXT,
    tags            TEXT[]
);

CREATE INDEX idx_bt_ea_name ON backtest_results (ea_name);
CREATE INDEX idx_bt_symbol ON backtest_results (symbol);
CREATE INDEX idx_bt_created ON backtest_results (created_at DESC);
CREATE INDEX idx_bt_params ON backtest_results USING GIN (parameters);
CREATE INDEX idx_bt_tags ON backtest_results USING GIN (tags);

-- ============================================================
-- 5. STRATEGIES - Trading strategy versions
-- ============================================================
CREATE TABLE IF NOT EXISTS strategies (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description     TEXT,
    entry_logic     TEXT,
    exit_logic      TEXT,
    risk_management TEXT,
    default_params  JSONB,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active', 'testing', 'retired', 'deprecated'
                    )),
    tags            TEXT[],
    UNIQUE(name, version)
);

CREATE INDEX idx_strategies_name ON strategies (name);
CREATE INDEX idx_strategies_status ON strategies (status);

-- ============================================================
-- 6. STRATEGY_EVOLUTION - Track strategy changes over time
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_evolution (
    id              SERIAL PRIMARY KEY,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      TEXT,
    change_type     TEXT NOT NULL CHECK (change_type IN (
                        'parameter_change', 'logic_change', 'new_version',
                        'status_change', 'risk_change'
                    )),
    old_values      JSONB,
    new_values      JSONB,
    rationale       TEXT,
    backtest_id     INTEGER REFERENCES backtest_results(id),
    tags            TEXT[]
);

CREATE INDEX idx_stratevo_strategy ON strategy_evolution (strategy_id, created_at DESC);

-- ============================================================
-- 7. LESSONS_LEARNED - Abstracted principles from experience
-- ============================================================
CREATE TABLE IF NOT EXISTS lessons_learned (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title           TEXT NOT NULL,
    principle       TEXT NOT NULL,
    domain          TEXT NOT NULL DEFAULT 'general' CHECK (domain IN (
                        'backtesting', 'ea_development', 'optimization',
                        'tool_errors', 'environment', 'problem_solving',
                        'debugging', 'general'
                    )),
    confidence      INTEGER NOT NULL DEFAULT 5 CHECK (confidence BETWEEN 1 AND 10),
    evidence        TEXT[],            -- references to reflections/memories
    error_signature TEXT,              -- for recurring error patterns
    applied_count   INTEGER NOT NULL DEFAULT 0,
    last_applied    TIMESTAMPTZ,
    tags            TEXT[],
    project         TEXT
);

CREATE INDEX idx_lessons_domain ON lessons_learned (domain);
CREATE INDEX idx_lessons_confidence ON lessons_learned (confidence DESC);
CREATE INDEX idx_lessons_error_sig ON lessons_learned (error_signature) WHERE error_signature IS NOT NULL;
CREATE INDEX idx_lessons_project ON lessons_learned (project);
CREATE INDEX idx_lessons_tags ON lessons_learned USING GIN (tags);

-- Auto-update updated_at on lessons_learned
CREATE OR REPLACE FUNCTION lessons_updated_trigger() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_lessons_updated
    BEFORE UPDATE ON lessons_learned
    FOR EACH ROW EXECUTE FUNCTION lessons_updated_trigger();

-- ============================================================
-- 8. PENDING_TRANSCRIPTS - Deferred transcript processing queue
-- ============================================================
CREATE TABLE IF NOT EXISTS pending_transcripts (
    id                  SERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id          TEXT NOT NULL,
    project             TEXT,
    compact_summary     TEXT,           -- NULL if no compaction happened
    raw_content         TEXT NOT NULL,  -- filtered transcript (full or tail-only)
    had_compaction      BOOLEAN NOT NULL DEFAULT FALSE,
    user_message_count  INTEGER NOT NULL DEFAULT 0,
    total_size_bytes    INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','completed','failed','skipped')),
    processed_at        TIMESTAMPTZ,
    processed_by        TEXT,           -- 'sonnet', 'skipped-trivial', 'skipped-stale'
    error_message       TEXT,
    memories_created    INTEGER DEFAULT 0
);

CREATE INDEX idx_pending_status ON pending_transcripts (status, created_at ASC);
CREATE INDEX idx_pending_session ON pending_transcripts (session_id);

-- ============================================================
-- 9. PAPERS - Document metadata (tags, author, type)
-- ============================================================
CREATE TABLE IF NOT EXISTS papers (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    source_file     TEXT UNIQUE,
    tags            TEXT[],
    author          TEXT,
    doc_type        TEXT,              -- textbook, paper, guide, reference
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_papers_tags ON papers USING GIN (tags);
CREATE INDEX idx_papers_title ON papers (title);

-- ============================================================
-- 10. PAPER_CHUNKS - Research paper knowledge base
-- ============================================================
CREATE TABLE IF NOT EXISTS paper_chunks (
    id              SERIAL PRIMARY KEY,
    paper_id        INTEGER REFERENCES papers(id),
    paper_title     TEXT NOT NULL,     -- denormalized for read performance
    section_header  TEXT,
    chunk_text      TEXT NOT NULL,
    source_file     TEXT,              -- denormalized for read performance
    embedding       vector(512),       -- model2vec potion-base-32M (512-dim)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_paper_chunks_embedding ON paper_chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_paper_chunks_paper_id ON paper_chunks (paper_id);

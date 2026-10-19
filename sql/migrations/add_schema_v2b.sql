-- Phase 3B Migration: Projects, Artifacts, Graph Memory
-- Run: psql -U claude_agent -d agent_memory -f add_schema_v2b.sql

BEGIN;

-- 3B.1: Projects table
CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    directory       TEXT,
    description     TEXT,
    status          TEXT DEFAULT 'active' CHECK (status IN ('active','paused','completed','archived')),
    tech_stack      TEXT[],
    goals           TEXT,
    milestones      TEXT[],
    last_handoff    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_active     TIMESTAMPTZ DEFAULT NOW(),
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id)
);

-- 3B.2: Project artifacts table
CREATE TABLE IF NOT EXISTS project_artifacts (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    file_path       TEXT NOT NULL,
    artifact_type   TEXT NOT NULL CHECK (artifact_type IN (
                        'plan','dashboard','script','ea','config',
                        'doc','skill','schema','hook','agent','test')),
    title           TEXT,
    description     TEXT,
    status          TEXT DEFAULT 'current' CHECK (status IN ('current','superseded','deleted')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by_session TEXT,
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id)
);

-- 3B.3: Entities table (graph nodes)
CREATE TABLE IF NOT EXISTS entities (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    entity_type     TEXT NOT NULL CHECK (entity_type IN (
                        'person','tool','concept','project','file',
                        'ea','symbol','platform','library','service')),
    description     TEXT,
    aliases         TEXT[],
    embedding       vector(512),
    mention_count   INTEGER DEFAULT 1,
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ DEFAULT NOW(),
    project_id      INTEGER REFERENCES projects(id),
    agent_id        TEXT NOT NULL REFERENCES agents(agent_id),
    UNIQUE(name, entity_type, agent_id)
);

-- 3B.3: Relationships table (graph edges)
CREATE TABLE IF NOT EXISTS relationships (
    id                  SERIAL PRIMARY KEY,
    source_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id           INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relationship_type   TEXT NOT NULL CHECK (relationship_type IN (
                            'uses','causes','fixes','depends_on','produces',
                            'trades','implements','contains','replaces','conflicts_with')),
    description         TEXT,
    confidence          SMALLINT DEFAULT 7 CHECK (confidence BETWEEN 1 AND 10),
    is_valid            BOOLEAN DEFAULT TRUE,
    mention_count       INTEGER DEFAULT 1,
    source_session      TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    agent_id            TEXT NOT NULL REFERENCES agents(agent_id),
    UNIQUE(source_id, target_id, relationship_type)
);

-- 3B.4: Add project_id FKs to existing tables (idempotent)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='memories' AND column_name='project_id') THEN
        ALTER TABLE memories ADD COLUMN project_id INTEGER REFERENCES projects(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sessions' AND column_name='project_id') THEN
        ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='reflections' AND column_name='project_id') THEN
        ALTER TABLE reflections ADD COLUMN project_id INTEGER REFERENCES projects(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='error_solutions' AND column_name='project_id') THEN
        ALTER TABLE error_solutions ADD COLUMN project_id INTEGER REFERENCES projects(id);
    END IF;
END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_projects_directory ON projects(directory);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON project_artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_project ON entities(project_id);
CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_memories_project_id ON memories(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project_id ON sessions(project_id);

COMMIT;

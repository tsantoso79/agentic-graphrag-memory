--
-- PostgreSQL database dump
--

\restrict Qbcazw7NaYTQiDRZuDNgWEqzcUEZeo0nv7suxLUIbPjshiOg0gCby8gjYYsXliW

-- Dumped from database version 18.2
-- Dumped by pg_dump version 18.2

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: lessons_updated_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lessons_updated_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;


--
-- Name: memories_search_trigger(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.memories_search_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
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
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    agent_id text NOT NULL,
    display_name text NOT NULL,
    agent_type text NOT NULL,
    environment text NOT NULL,
    default_model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone
);


--
-- Name: backtest_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_results (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id text,
    ea_name text NOT NULL,
    ea_version text,
    symbol text NOT NULL,
    timeframe text NOT NULL,
    date_from date,
    date_to date,
    source text,
    total_trades integer,
    profit_factor numeric(8,4),
    net_profit numeric(12,2),
    max_drawdown_pct numeric(6,2),
    win_rate numeric(6,2),
    sharpe_ratio numeric(8,4),
    avg_trade numeric(10,2),
    max_consec_losses integer,
    parameters jsonb,
    extra_metrics jsonb,
    notes text,
    tags text[],
    agent_id text NOT NULL,
    CONSTRAINT backtest_results_source_check CHECK ((source = ANY (ARRAY['mt4'::text, 'python'::text, 'manual'::text])))
);


--
-- Name: backtest_results_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_results_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_results_id_seq OWNED BY public.backtest_results.id;


--
-- Name: entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entities (
    id integer NOT NULL,
    name text NOT NULL,
    entity_type text NOT NULL,
    description text,
    aliases text[],
    embedding public.vector(512),
    mention_count integer DEFAULT 1,
    first_seen timestamp with time zone DEFAULT now(),
    last_seen timestamp with time zone DEFAULT now(),
    project_id integer,
    agent_id text NOT NULL,
    CONSTRAINT entities_entity_type_check CHECK ((entity_type = ANY (ARRAY['person'::text, 'tool'::text, 'concept'::text, 'project'::text, 'file'::text, 'ea'::text, 'symbol'::text, 'platform'::text, 'library'::text, 'service'::text])))
);


--
-- Name: entities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.entities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: entities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.entities_id_seq OWNED BY public.entities.id;


--
-- Name: entity_mentions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_mentions (
    id integer NOT NULL,
    entity_id integer NOT NULL,
    memory_id integer NOT NULL,
    confidence smallint DEFAULT 7,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT entity_mentions_confidence_check CHECK (((confidence >= 1) AND (confidence <= 10)))
);


--
-- Name: entity_mentions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.entity_mentions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: entity_mentions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.entity_mentions_id_seq OWNED BY public.entity_mentions.id;


--
-- Name: error_solutions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_solutions (
    id integer NOT NULL,
    error_pattern text NOT NULL,
    error_example text,
    solution text NOT NULL,
    tool_name text,
    platform text,
    tags text[],
    verified boolean DEFAULT false,
    hit_count integer DEFAULT 0,
    last_hit timestamp with time zone,
    source_memory integer,
    created_at timestamp with time zone DEFAULT now(),
    agent_id text NOT NULL,
    project_id integer
);


--
-- Name: error_solutions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.error_solutions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: error_solutions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.error_solutions_id_seq OWNED BY public.error_solutions.id;


--
-- Name: improvement_flag_memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.improvement_flag_memories (
    flag_id integer NOT NULL,
    memory_id integer NOT NULL
);


--
-- Name: improvement_flags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.improvement_flags (
    id integer NOT NULL,
    theme text NOT NULL,
    problem text NOT NULL,
    suggested_fix text NOT NULL,
    priority text DEFAULT 'medium'::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    organizer_run_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone
);


--
-- Name: improvement_flags_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.improvement_flags_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: improvement_flags_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.improvement_flags_id_seq OWNED BY public.improvement_flags.id;


--
-- Name: lessons_learned; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lessons_learned (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    title text NOT NULL,
    principle text NOT NULL,
    domain text DEFAULT 'general'::text NOT NULL,
    confidence integer DEFAULT 5 NOT NULL,
    evidence text[],
    error_signature text,
    applied_count integer DEFAULT 0 NOT NULL,
    last_applied timestamp with time zone,
    tags text[],
    project text,
    CONSTRAINT lessons_learned_confidence_check CHECK (((confidence >= 1) AND (confidence <= 10))),
    CONSTRAINT lessons_learned_domain_check CHECK ((domain = ANY (ARRAY['backtesting'::text, 'ea_development'::text, 'optimization'::text, 'tool_errors'::text, 'environment'::text, 'problem_solving'::text, 'debugging'::text, 'general'::text])))
);


--
-- Name: lessons_learned_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lessons_learned_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lessons_learned_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lessons_learned_id_seq OWNED BY public.lessons_learned.id;


--
-- Name: memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memories (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    content text NOT NULL,
    category text NOT NULL,
    domain text DEFAULT 'general'::text NOT NULL,
    importance integer DEFAULT 5 NOT NULL,
    tags text[],
    source_session text,
    error_signature text,
    superseded_by integer,
    ttl_days integer,
    recalled_count integer DEFAULT 0 NOT NULL,
    last_recalled timestamp with time zone,
    search_text tsvector,
    embedding public.vector(512),
    project text,
    agent_id text NOT NULL,
    startup_count integer DEFAULT 0 NOT NULL,
    inject_count integer DEFAULT 0 NOT NULL,
    recall_count integer DEFAULT 0 NOT NULL,
    useful_pct numeric(5,2),
    last_injected_at timestamp with time zone,
    retrieval_mode text DEFAULT 'semantic'::text,
    tool_name text,
    project_id integer,
    CONSTRAINT chk_memories_retrieval_mode CHECK ((retrieval_mode = ANY (ARRAY['semantic'::text, 'tool'::text, 'manual'::text]))),
    CONSTRAINT memories_category_check CHECK ((category = ANY (ARRAY['fact'::text, 'decision'::text, 'preference'::text, 'context'::text, 'bug'::text, 'config'::text, 'pattern'::text, 'tool_usage'::text]))),
    CONSTRAINT memories_domain_check CHECK ((domain = ANY (ARRAY['backtesting'::text, 'ea_development'::text, 'optimization'::text, 'tool_errors'::text, 'environment'::text, 'problem_solving'::text, 'debugging'::text, 'general'::text]))),
    CONSTRAINT memories_importance_check CHECK (((importance >= 1) AND (importance <= 10)))
);


--
-- Name: memories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.memories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.memories_id_seq OWNED BY public.memories.id;


--
-- Name: memory_exposures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_exposures (
    id integer NOT NULL,
    session_id text NOT NULL,
    memory_id integer NOT NULL,
    source text NOT NULL,
    turn_number integer,
    similarity numeric(5,3),
    useful boolean,
    scored_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_exposures_source_check CHECK ((source = ANY (ARRAY['startup'::text, 'inject'::text, 'recall'::text])))
);


--
-- Name: memory_exposures_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.memory_exposures_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: memory_exposures_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.memory_exposures_id_seq OWNED BY public.memory_exposures.id;


--
-- Name: organizer_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organizer_runs (
    id integer NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    phase text NOT NULL,
    dry_run boolean DEFAULT true NOT NULL,
    dupes_merged integer DEFAULT 0,
    hot_suppressed integer DEFAULT 0,
    stale_decayed integer DEFAULT 0,
    ttl_applied integer DEFAULT 0,
    clusters_consolidated integer DEFAULT 0,
    contradictions_resolved integer DEFAULT 0,
    stale_assessed integer DEFAULT 0,
    total_actions integer DEFAULT 0,
    sonnet_cost_usd numeric(6,4),
    actions_log jsonb,
    error_message text,
    agent_id text NOT NULL,
    CONSTRAINT organizer_runs_phase_check CHECK ((phase = ANY (ARRAY['A'::text, 'B'::text, 'AB'::text])))
);


--
-- Name: organizer_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.organizer_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: organizer_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.organizer_runs_id_seq OWNED BY public.organizer_runs.id;


--
-- Name: paper_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.paper_chunks (
    id integer NOT NULL,
    paper_title text NOT NULL,
    section_header text,
    chunk_text text NOT NULL,
    source_file text,
    embedding public.vector(512),
    created_at timestamp with time zone DEFAULT now(),
    paper_id integer
);


--
-- Name: paper_chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.paper_chunks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: paper_chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.paper_chunks_id_seq OWNED BY public.paper_chunks.id;


--
-- Name: papers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.papers (
    id integer NOT NULL,
    title text NOT NULL,
    source_file text,
    tags text[],
    author text,
    doc_type text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: papers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.papers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: papers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.papers_id_seq OWNED BY public.papers.id;


--
-- Name: pending_transcripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_transcripts (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id text NOT NULL,
    project text,
    compact_summary text,
    raw_content text NOT NULL,
    had_compaction boolean DEFAULT false NOT NULL,
    user_message_count integer DEFAULT 0 NOT NULL,
    total_size_bytes integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    processed_at timestamp with time zone,
    processed_by text,
    error_message text,
    memories_created integer DEFAULT 0,
    agent_id text NOT NULL,
    inject_log jsonb,
    supplementary_status text,
    CONSTRAINT pending_transcripts_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'completed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT pending_transcripts_supplementary_status_check CHECK ((supplementary_status = ANY (ARRAY['pending'::text, 'processing'::text, 'completed'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: pending_transcripts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pending_transcripts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pending_transcripts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pending_transcripts_id_seq OWNED BY public.pending_transcripts.id;


--
-- Name: project_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_artifacts (
    id integer NOT NULL,
    project_id integer NOT NULL,
    file_path text NOT NULL,
    artifact_type text NOT NULL,
    title text,
    description text,
    status text DEFAULT 'current'::text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by_session text,
    agent_id text NOT NULL,
    CONSTRAINT project_artifacts_artifact_type_check CHECK ((artifact_type = ANY (ARRAY['plan'::text, 'dashboard'::text, 'script'::text, 'ea'::text, 'config'::text, 'doc'::text, 'skill'::text, 'schema'::text, 'hook'::text, 'agent'::text, 'test'::text]))),
    CONSTRAINT project_artifacts_status_check CHECK ((status = ANY (ARRAY['current'::text, 'superseded'::text, 'deleted'::text])))
);


--
-- Name: project_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_artifacts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: project_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_artifacts_id_seq OWNED BY public.project_artifacts.id;


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id integer NOT NULL,
    name text NOT NULL,
    display_name text,
    directory text,
    description text,
    status text DEFAULT 'active'::text,
    tech_stack text[],
    goals text,
    milestones text[],
    last_handoff text,
    created_at timestamp with time zone DEFAULT now(),
    last_active timestamp with time zone DEFAULT now(),
    agent_id text NOT NULL,
    CONSTRAINT projects_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'completed'::text, 'archived'::text])))
);


--
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- Name: reflections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reflections (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id text,
    task_type text NOT NULL,
    domain text DEFAULT 'general'::text NOT NULL,
    outcome text NOT NULL,
    what_worked text,
    what_failed text,
    lessons text[],
    confidence integer,
    tags text[],
    project text,
    agent_id text NOT NULL,
    task_category text,
    project_id integer,
    CONSTRAINT chk_reflections_task_category CHECK ((task_category = ANY (ARRAY['implementation'::text, 'debugging'::text, 'research'::text, 'design'::text, 'testing'::text, 'deployment'::text, 'configuration'::text, 'documentation'::text, 'review'::text, 'ingestion'::text, 'automation'::text, 'optimization'::text, 'integration'::text, 'maintenance'::text]))),
    CONSTRAINT reflections_confidence_check CHECK (((confidence >= 1) AND (confidence <= 10))),
    CONSTRAINT reflections_domain_check CHECK ((domain = ANY (ARRAY['backtesting'::text, 'ea_development'::text, 'optimization'::text, 'tool_errors'::text, 'environment'::text, 'problem_solving'::text, 'debugging'::text, 'general'::text]))),
    CONSTRAINT reflections_outcome_check CHECK ((outcome = ANY (ARRAY['success'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: reflections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.reflections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reflections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.reflections_id_seq OWNED BY public.reflections.id;


--
-- Name: relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.relationships (
    id integer NOT NULL,
    source_id integer NOT NULL,
    target_id integer NOT NULL,
    relationship_type text NOT NULL,
    description text,
    confidence smallint DEFAULT 7,
    is_valid boolean DEFAULT true,
    mention_count integer DEFAULT 1,
    source_session text,
    created_at timestamp with time zone DEFAULT now(),
    agent_id text NOT NULL,
    CONSTRAINT relationships_confidence_check CHECK (((confidence >= 1) AND (confidence <= 10))),
    CONSTRAINT relationships_relationship_type_check CHECK ((relationship_type = ANY (ARRAY['uses'::text, 'causes'::text, 'fixes'::text, 'depends_on'::text, 'produces'::text, 'trades'::text, 'implements'::text, 'contains'::text, 'replaces'::text, 'conflicts_with'::text])))
);


--
-- Name: relationships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.relationships_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: relationships_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.relationships_id_seq OWNED BY public.relationships.id;


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    session_id text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    summary text,
    outcome text,
    tasks_completed text[],
    tags text[],
    token_estimate integer,
    project text,
    agent_id text NOT NULL,
    claude_session_id text,
    transcript_path text,
    conversation_id text,
    project_id integer,
    CONSTRAINT sessions_outcome_check CHECK ((outcome = ANY (ARRAY['success'::text, 'partial'::text, 'failed'::text, 'abandoned'::text])))
);


--
-- Name: strategies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategies (
    id integer NOT NULL,
    name text NOT NULL,
    version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    description text,
    entry_logic text,
    exit_logic text,
    risk_management text,
    default_params jsonb,
    status text DEFAULT 'active'::text NOT NULL,
    tags text[],
    CONSTRAINT strategies_status_check CHECK ((status = ANY (ARRAY['active'::text, 'testing'::text, 'retired'::text, 'deprecated'::text])))
);


--
-- Name: strategies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategies_id_seq OWNED BY public.strategies.id;


--
-- Name: strategy_evolution; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategy_evolution (
    id integer NOT NULL,
    strategy_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id text,
    change_type text NOT NULL,
    old_values jsonb,
    new_values jsonb,
    rationale text,
    backtest_id integer,
    tags text[],
    agent_id text DEFAULT 'claude-win'::text NOT NULL,
    CONSTRAINT strategy_evolution_change_type_check CHECK ((change_type = ANY (ARRAY['parameter_change'::text, 'logic_change'::text, 'new_version'::text, 'status_change'::text, 'risk_change'::text])))
);


--
-- Name: strategy_evolution_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_evolution_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_evolution_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_evolution_id_seq OWNED BY public.strategy_evolution.id;


--
-- Name: todo_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.todo_items (
    id integer NOT NULL,
    content text NOT NULL,
    completed boolean DEFAULT false,
    priority character varying(10) DEFAULT '''medium'''::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: todo_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.todo_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: todo_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.todo_items_id_seq OWNED BY public.todo_items.id;


--
-- Name: backtest_results id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results ALTER COLUMN id SET DEFAULT nextval('public.backtest_results_id_seq'::regclass);


--
-- Name: entities id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entities ALTER COLUMN id SET DEFAULT nextval('public.entities_id_seq'::regclass);


--
-- Name: entity_mentions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_mentions ALTER COLUMN id SET DEFAULT nextval('public.entity_mentions_id_seq'::regclass);


--
-- Name: error_solutions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_solutions ALTER COLUMN id SET DEFAULT nextval('public.error_solutions_id_seq'::regclass);


--
-- Name: improvement_flags id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.improvement_flags ALTER COLUMN id SET DEFAULT nextval('public.improvement_flags_id_seq'::regclass);


--
-- Name: lessons_learned id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons_learned ALTER COLUMN id SET DEFAULT nextval('public.lessons_learned_id_seq'::regclass);


--
-- Name: memories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories ALTER COLUMN id SET DEFAULT nextval('public.memories_id_seq'::regclass);


--
-- Name: memory_exposures id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_exposures ALTER COLUMN id SET DEFAULT nextval('public.memory_exposures_id_seq'::regclass);


--
-- Name: organizer_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizer_runs ALTER COLUMN id SET DEFAULT nextval('public.organizer_runs_id_seq'::regclass);


--
-- Name: paper_chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_chunks ALTER COLUMN id SET DEFAULT nextval('public.paper_chunks_id_seq'::regclass);


--
-- Name: papers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.papers ALTER COLUMN id SET DEFAULT nextval('public.papers_id_seq'::regclass);


--
-- Name: pending_transcripts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_transcripts ALTER COLUMN id SET DEFAULT nextval('public.pending_transcripts_id_seq'::regclass);


--
-- Name: project_artifacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_artifacts ALTER COLUMN id SET DEFAULT nextval('public.project_artifacts_id_seq'::regclass);


--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- Name: reflections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reflections ALTER COLUMN id SET DEFAULT nextval('public.reflections_id_seq'::regclass);


--
-- Name: relationships id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships ALTER COLUMN id SET DEFAULT nextval('public.relationships_id_seq'::regclass);


--
-- Name: strategies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies ALTER COLUMN id SET DEFAULT nextval('public.strategies_id_seq'::regclass);


--
-- Name: strategy_evolution id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_evolution ALTER COLUMN id SET DEFAULT nextval('public.strategy_evolution_id_seq'::regclass);


--
-- Name: todo_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.todo_items ALTER COLUMN id SET DEFAULT nextval('public.todo_items_id_seq'::regclass);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (agent_id);


--
-- Name: backtest_results backtest_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results
    ADD CONSTRAINT backtest_results_pkey PRIMARY KEY (id);


--
-- Name: entities entities_name_entity_type_agent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_name_entity_type_agent_id_key UNIQUE (name, entity_type, agent_id);


--
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (id);


--
-- Name: entity_mentions entity_mentions_entity_id_memory_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_mentions
    ADD CONSTRAINT entity_mentions_entity_id_memory_id_key UNIQUE (entity_id, memory_id);


--
-- Name: entity_mentions entity_mentions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_mentions
    ADD CONSTRAINT entity_mentions_pkey PRIMARY KEY (id);


--
-- Name: error_solutions error_solutions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_solutions
    ADD CONSTRAINT error_solutions_pkey PRIMARY KEY (id);


--
-- Name: improvement_flag_memories improvement_flag_memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.improvement_flag_memories
    ADD CONSTRAINT improvement_flag_memories_pkey PRIMARY KEY (flag_id, memory_id);


--
-- Name: improvement_flags improvement_flags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.improvement_flags
    ADD CONSTRAINT improvement_flags_pkey PRIMARY KEY (id);


--
-- Name: lessons_learned lessons_learned_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lessons_learned
    ADD CONSTRAINT lessons_learned_pkey PRIMARY KEY (id);


--
-- Name: memories memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_pkey PRIMARY KEY (id);


--
-- Name: memory_exposures memory_exposures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_exposures
    ADD CONSTRAINT memory_exposures_pkey PRIMARY KEY (id);


--
-- Name: organizer_runs organizer_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizer_runs
    ADD CONSTRAINT organizer_runs_pkey PRIMARY KEY (id);


--
-- Name: paper_chunks paper_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_pkey PRIMARY KEY (id);


--
-- Name: papers papers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.papers
    ADD CONSTRAINT papers_pkey PRIMARY KEY (id);


--
-- Name: papers papers_source_file_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.papers
    ADD CONSTRAINT papers_source_file_key UNIQUE (source_file);


--
-- Name: pending_transcripts pending_transcripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_transcripts
    ADD CONSTRAINT pending_transcripts_pkey PRIMARY KEY (id);


--
-- Name: project_artifacts project_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_artifacts
    ADD CONSTRAINT project_artifacts_pkey PRIMARY KEY (id);


--
-- Name: projects projects_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_name_key UNIQUE (name);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: reflections reflections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reflections
    ADD CONSTRAINT reflections_pkey PRIMARY KEY (id);


--
-- Name: relationships relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships
    ADD CONSTRAINT relationships_pkey PRIMARY KEY (id);


--
-- Name: relationships relationships_source_id_target_id_relationship_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships
    ADD CONSTRAINT relationships_source_id_target_id_relationship_type_key UNIQUE (source_id, target_id, relationship_type);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (session_id);


--
-- Name: strategies strategies_name_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_name_version_key UNIQUE (name, version);


--
-- Name: strategies strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_pkey PRIMARY KEY (id);


--
-- Name: strategy_evolution strategy_evolution_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_evolution
    ADD CONSTRAINT strategy_evolution_pkey PRIMARY KEY (id);


--
-- Name: todo_items todo_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.todo_items
    ADD CONSTRAINT todo_items_pkey PRIMARY KEY (id);


--
-- Name: idx_artifacts_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_artifacts_project ON public.project_artifacts USING btree (project_id);


--
-- Name: idx_backtest_results_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_results_agent ON public.backtest_results USING btree (agent_id);


--
-- Name: idx_bt_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_created ON public.backtest_results USING btree (created_at DESC);


--
-- Name: idx_bt_ea_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_ea_name ON public.backtest_results USING btree (ea_name);


--
-- Name: idx_bt_params; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_params ON public.backtest_results USING gin (parameters);


--
-- Name: idx_bt_symbol; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_symbol ON public.backtest_results USING btree (symbol);


--
-- Name: idx_bt_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bt_tags ON public.backtest_results USING gin (tags);


--
-- Name: idx_em_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_em_entity ON public.entity_mentions USING btree (entity_id);


--
-- Name: idx_em_memory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_em_memory ON public.entity_mentions USING btree (memory_id);


--
-- Name: idx_entities_aliases; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entities_aliases ON public.entities USING gin (aliases);


--
-- Name: idx_entities_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entities_embedding ON public.entities USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_entities_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entities_name ON public.entities USING btree (name);


--
-- Name: idx_entities_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entities_project ON public.entities USING btree (project_id);


--
-- Name: idx_entities_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entities_type ON public.entities USING btree (entity_type);


--
-- Name: idx_errors_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_errors_pattern ON public.error_solutions USING gin (error_pattern public.gin_trgm_ops);


--
-- Name: idx_errors_tool; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_errors_tool ON public.error_solutions USING btree (tool_name);


--
-- Name: idx_exposures_memory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exposures_memory ON public.memory_exposures USING btree (memory_id);


--
-- Name: idx_exposures_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exposures_session ON public.memory_exposures USING btree (session_id);


--
-- Name: idx_exposures_session_memory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exposures_session_memory ON public.memory_exposures USING btree (session_id, memory_id);


--
-- Name: idx_exposures_unscored; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exposures_unscored ON public.memory_exposures USING btree (session_id) WHERE (useful IS NULL);


--
-- Name: idx_lessons_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_confidence ON public.lessons_learned USING btree (confidence DESC);


--
-- Name: idx_lessons_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_domain ON public.lessons_learned USING btree (domain);


--
-- Name: idx_lessons_error_sig; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_error_sig ON public.lessons_learned USING btree (error_signature) WHERE (error_signature IS NOT NULL);


--
-- Name: idx_lessons_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lessons_tags ON public.lessons_learned USING gin (tags);


--
-- Name: idx_memories_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_active ON public.memories USING btree (importance DESC, updated_at DESC) WHERE (superseded_by IS NULL);


--
-- Name: idx_memories_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_agent ON public.memories USING btree (agent_id);


--
-- Name: idx_memories_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_category ON public.memories USING btree (category);


--
-- Name: idx_memories_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_domain ON public.memories USING btree (domain);


--
-- Name: idx_memories_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_embedding ON public.memories USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_memories_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_embedding_hnsw ON public.memories USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_memories_error_sig; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_error_sig ON public.memories USING btree (error_signature) WHERE (error_signature IS NOT NULL);


--
-- Name: idx_memories_importance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_importance ON public.memories USING btree (importance DESC);


--
-- Name: idx_memories_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project ON public.memories USING btree (project);


--
-- Name: idx_memories_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_project_id ON public.memories USING btree (project_id);


--
-- Name: idx_memories_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_search ON public.memories USING gin (search_text);


--
-- Name: idx_memories_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_tags ON public.memories USING gin (tags);


--
-- Name: idx_papers_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_papers_tags ON public.papers USING gin (tags);


--
-- Name: idx_papers_title; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_papers_title ON public.papers USING btree (title);


--
-- Name: idx_pending_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pending_session ON public.pending_transcripts USING btree (session_id);


--
-- Name: idx_pending_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pending_status ON public.pending_transcripts USING btree (status, created_at);


--
-- Name: idx_pending_transcripts_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pending_transcripts_agent ON public.pending_transcripts USING btree (agent_id);


--
-- Name: idx_projects_directory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_directory ON public.projects USING btree (directory);


--
-- Name: idx_reflections_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reflections_agent ON public.reflections USING btree (agent_id);


--
-- Name: idx_reflections_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reflections_created ON public.reflections USING btree (created_at DESC);


--
-- Name: idx_reflections_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reflections_domain ON public.reflections USING btree (domain);


--
-- Name: idx_reflections_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reflections_tags ON public.reflections USING gin (tags);


--
-- Name: idx_reflections_task_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reflections_task_type ON public.reflections USING btree (task_type);


--
-- Name: idx_relationships_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_relationships_source ON public.relationships USING btree (source_id);


--
-- Name: idx_relationships_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_relationships_target ON public.relationships USING btree (target_id);


--
-- Name: idx_sessions_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_agent ON public.sessions USING btree (agent_id);


--
-- Name: idx_sessions_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_project ON public.sessions USING btree (project);


--
-- Name: idx_sessions_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_project_id ON public.sessions USING btree (project_id);


--
-- Name: idx_sessions_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_started ON public.sessions USING btree (started_at DESC);


--
-- Name: idx_sessions_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_tags ON public.sessions USING gin (tags);


--
-- Name: idx_strategies_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strategies_name ON public.strategies USING btree (name);


--
-- Name: idx_strategies_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strategies_status ON public.strategies USING btree (status);


--
-- Name: idx_strategy_evolution_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strategy_evolution_agent ON public.strategy_evolution USING btree (agent_id);


--
-- Name: idx_stratevo_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stratevo_strategy ON public.strategy_evolution USING btree (strategy_id, created_at DESC);


--
-- Name: paper_chunks_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX paper_chunks_embedding_idx ON public.paper_chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: lessons_learned trg_lessons_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lessons_updated BEFORE UPDATE ON public.lessons_learned FOR EACH ROW EXECUTE FUNCTION public.lessons_updated_trigger();


--
-- Name: memories trg_memories_search; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_memories_search BEFORE INSERT OR UPDATE ON public.memories FOR EACH ROW EXECUTE FUNCTION public.memories_search_trigger();


--
-- Name: entities entities_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: entities entities_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: entity_mentions entity_mentions_entity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_mentions
    ADD CONSTRAINT entity_mentions_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entities(id) ON DELETE CASCADE;


--
-- Name: entity_mentions entity_mentions_memory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_mentions
    ADD CONSTRAINT entity_mentions_memory_id_fkey FOREIGN KEY (memory_id) REFERENCES public.memories(id) ON DELETE CASCADE;


--
-- Name: error_solutions error_solutions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_solutions
    ADD CONSTRAINT error_solutions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: error_solutions error_solutions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_solutions
    ADD CONSTRAINT error_solutions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: error_solutions error_solutions_source_memory_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_solutions
    ADD CONSTRAINT error_solutions_source_memory_fkey FOREIGN KEY (source_memory) REFERENCES public.memories(id);


--
-- Name: backtest_results fk_backtest_results_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_results
    ADD CONSTRAINT fk_backtest_results_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: memories fk_memories_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT fk_memories_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: pending_transcripts fk_pending_transcripts_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_transcripts
    ADD CONSTRAINT fk_pending_transcripts_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: reflections fk_reflections_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reflections
    ADD CONSTRAINT fk_reflections_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: sessions fk_sessions_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT fk_sessions_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: strategy_evolution fk_strategy_evolution_agent; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_evolution
    ADD CONSTRAINT fk_strategy_evolution_agent FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: improvement_flag_memories improvement_flag_memories_flag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.improvement_flag_memories
    ADD CONSTRAINT improvement_flag_memories_flag_id_fkey FOREIGN KEY (flag_id) REFERENCES public.improvement_flags(id) ON DELETE CASCADE;


--
-- Name: memories memories_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: memories memories_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memories
    ADD CONSTRAINT memories_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.memories(id);


--
-- Name: memory_exposures memory_exposures_memory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_exposures
    ADD CONSTRAINT memory_exposures_memory_id_fkey FOREIGN KEY (memory_id) REFERENCES public.memories(id);


--
-- Name: organizer_runs organizer_runs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizer_runs
    ADD CONSTRAINT organizer_runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: paper_chunks paper_chunks_paper_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paper_chunks
    ADD CONSTRAINT paper_chunks_paper_id_fkey FOREIGN KEY (paper_id) REFERENCES public.papers(id);


--
-- Name: project_artifacts project_artifacts_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_artifacts
    ADD CONSTRAINT project_artifacts_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: project_artifacts project_artifacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_artifacts
    ADD CONSTRAINT project_artifacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: projects projects_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: reflections reflections_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reflections
    ADD CONSTRAINT reflections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: relationships relationships_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships
    ADD CONSTRAINT relationships_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(agent_id);


--
-- Name: relationships relationships_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships
    ADD CONSTRAINT relationships_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.entities(id) ON DELETE CASCADE;


--
-- Name: relationships relationships_target_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relationships
    ADD CONSTRAINT relationships_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.entities(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: strategy_evolution strategy_evolution_backtest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_evolution
    ADD CONSTRAINT strategy_evolution_backtest_id_fkey FOREIGN KEY (backtest_id) REFERENCES public.backtest_results(id);


--
-- Name: strategy_evolution strategy_evolution_strategy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_evolution
    ADD CONSTRAINT strategy_evolution_strategy_id_fkey FOREIGN KEY (strategy_id) REFERENCES public.strategies(id);


--
-- PostgreSQL database dump complete
--

\unrestrict Qbcazw7NaYTQiDRZuDNgWEqzcUEZeo0nv7suxLUIbPjshiOg0gCby8gjYYsXliW


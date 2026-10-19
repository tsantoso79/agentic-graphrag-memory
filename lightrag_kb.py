"""
LightRAG Knowledge Base Configuration & Interface
Extracts entities and relationships from ingested papers.
Uses: Gemini Flash (LLM), EmbeddingGemma-300M ONNX (embeddings), NetworkX (graph).
Storage: PG-backed KV/vector/doc_status, file-based graph (NetworkX).

Post-extraction filters (Windows compatibility + quality):
- Sanitize graphml XML-unsafe chars in node/edge attributes
- Normalize entity names (case, plurals, whitespace, hyphens)
- Reject entities <= 2 chars (math symbol junk) unless allowlisted
- Constrain entity types to the 10 defined domain types
"""

import os
import re
import sys
import asyncio
import time
import hashlib
import logging
import unicodedata
import numpy as np
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & PG config
# ---------------------------------------------------------------------------
WORKDIR = Path(os.environ.get(
    "LIGHTRAG_WORKDIR",
    str(Path.home() / ".claude" / "memory" / "lightrag_workdir")
))
WORKDIR.mkdir(parents=True, exist_ok=True)

_kb_log = logging.getLogger("lightrag_kb")

# LightRAG PG backends read these env vars (set from AGENT_MEMORY_DB_URI)
_db_uri = os.environ.get("AGENT_MEMORY_DB_URI", "")
if _db_uri and not os.environ.get("POSTGRES_USER"):
    # Parse postgresql://user:pass@host:port/dbname
    from urllib.parse import urlparse, unquote
    _parsed = urlparse(_db_uri)
    os.environ.setdefault("POSTGRES_USER", unquote(_parsed.username or ""))
    os.environ.setdefault("POSTGRES_PASSWORD", unquote(_parsed.password or ""))
    os.environ.setdefault("POSTGRES_HOST", _parsed.hostname or "localhost")
    os.environ.setdefault("POSTGRES_PORT", str(_parsed.port or 5432))
    os.environ.setdefault("POSTGRES_DATABASE", _parsed.path.lstrip("/") or "agent_memory")

# ---------------------------------------------------------------------------
# Embedding: EmbeddingGemma-300M via ONNX (CPU, ~20ms single, ~4ms batched)
# ---------------------------------------------------------------------------
_embed_model = None

def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(
            "onnx-community/embeddinggemma-300m-ONNX",
            backend="onnx",
            model_kwargs={"file_name": "onnx/model.onnx"},
        )
    return _embed_model


async def embedding_func(texts: list[str]) -> np.ndarray:
    """EmbeddingGemma-300M ONNX — 768-dim, CPU."""
    model = _get_embed_model()
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.array(embeddings, dtype=np.float32)


# ---------------------------------------------------------------------------
# LLM: Gemini Flash primary, Claude Haiku fallback
# ---------------------------------------------------------------------------
async def llm_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict] | None = None,
    keyword_extraction: bool = False,
    **kwargs,
) -> str:
    """Gemini Flash with retry on 503, Haiku fallback (both async)."""
    import asyncio as _asyncio
    from lightrag.llm.gemini import gemini_model_complete

    # Retry Gemini up to 4 times with exponential backoff on 503/overload.
    # Per-call 90s hard timeout prevents a stuck socket from holding a keyed
    # lock in merge_nodes_and_edges and starving Phase 2 relation merge.
    gemini_error = None
    for attempt in range(4):
        try:
            result = await _asyncio.wait_for(
                gemini_model_complete(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    history_messages=history_messages,
                    keyword_extraction=keyword_extraction,
                    model_name="gemini-2.5-flash-lite",
                    **kwargs,
                ),
                timeout=90,
            )
            return result
        except _asyncio.TimeoutError as e:
            gemini_error = e
            print(f"[LightRAG] Gemini call exceeded 90s, falling back to Haiku...")
            break
        except Exception as e:
            gemini_error = e
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str or "overloaded" in err_str.lower():
                wait = (2 ** attempt) * 5  # 5s, 10s, 20s, 40s
                if attempt < 3:
                    print(f"[LightRAG] Gemini 503, retry {attempt+1}/3 in {wait}s...")
                    await _asyncio.sleep(wait)
                    continue
            # Non-503 error or final attempt — fall through to Haiku
            print(f"[LightRAG] Gemini failed ({e}), falling back to Haiku...")
            break

    # Fallback: Claude Haiku via async anthropic SDK
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"Gemini failed ({gemini_error}) and ANTHROPIC_API_KEY not set for Haiku fallback"
        )
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages = []
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=kwargs.get("max_tokens", 4096),
        system=system_prompt or "",
        messages=messages,
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Entity extraction tuning — domain-specific prompts for academic paper KB
# ---------------------------------------------------------------------------
ENTITY_TYPES = [
    "Method",        # algorithms, strategies, techniques, trading strategies
    "Framework",     # software frameworks, architectures, platforms
    "Model",         # ML models, mathematical models, language models
    "Concept",       # theoretical concepts, principles, theories
    "Person",        # researchers, authors, practitioners
    "Organization",  # companies, institutions, research labs
    "Metric",        # performance measures, benchmarks, indicators
    "Dataset",       # datasets, benchmarks, corpora
    "Tool",          # software tools, APIs, code interpreters, libraries
    "Task",          # benchmark tasks, problems, applications
]

# ---------------------------------------------------------------------------
# Fix 1: XML-safe graphml write (prevents [Errno 22] crash on Windows)
# ---------------------------------------------------------------------------
_XML_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")

def _sanitize_xml_string(s: str) -> str:
    """Remove chars that are invalid in XML 1.0 (graphml format)."""
    return _XML_UNSAFE_RE.sub("", s)

def _sanitize_graph_for_xml(graph):
    """Sanitize all node/edge string attributes to be XML-safe.
    Also fixes UNKNOWN entity types that slip through the merge path.
    """
    for node_id in list(graph.nodes):
        for key, val in graph.nodes[node_id].items():
            if isinstance(val, str):
                graph.nodes[node_id][key] = _sanitize_xml_string(val)
        # Fix UNKNOWN types that survive the merge majority-vote
        etype = graph.nodes[node_id].get("entity_type", "")
        if etype.lower() in ("unknown", "") and etype not in _VALID_TYPES:
            graph.nodes[node_id]["entity_type"] = "concept"
    for u, v in list(graph.edges):
        for key, val in graph.edges[u, v].items():
            if isinstance(val, str):
                graph.edges[u, v][key] = _sanitize_xml_string(val)

def _patch_networkx_write():
    """Monkey-patch NetworkXStorage.write_nx_graph to sanitize before writing.
    Also uses atomic temp-file-then-rename to prevent corruption on kill."""
    from lightrag.kg.networkx_impl import NetworkXStorage
    import networkx as nx
    from lightrag.utils import logger as _lg
    import tempfile
    import shutil

    def _safe_write(graph, file_name, workspace="_"):
        _sanitize_graph_for_xml(graph)
        _lg.info(
            f"[{workspace}] Writing graph with {graph.number_of_nodes()} nodes, "
            f"{graph.number_of_edges()} edges (XML-sanitized)"
        )
        # Atomic write: temp file then rename (prevents corruption on kill)
        dir_name = os.path.dirname(file_name) or "."
        fd, tmp_path = tempfile.mkstemp(suffix=".graphml", dir=dir_name)
        os.close(fd)
        try:
            nx.write_graphml(graph, tmp_path)
            shutil.move(tmp_path, file_name)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    NetworkXStorage.write_nx_graph = staticmethod(_safe_write)


def _patch_fault_tolerant_merging():
    """Patch LightRAG's merge_nodes_and_edges to be fault-tolerant.

    LightRAG uses asyncio.FIRST_EXCEPTION — one entity/edge failure cancels
    all pending tasks and raises, killing the entire paper. This patch changes
    it to ALL_COMPLETED with return_exceptions=True, so individual entity
    failures are logged and skipped instead of being fatal.
    """
    import lightrag.operate as _op
    import asyncio
    from lightrag.utils import logger as _lg

    _original_merge = _op.merge_nodes_and_edges

    async def _fault_tolerant_merge(*args, **kwargs):
        """Wrapper that catches FIRST_EXCEPTION propagation by patching asyncio.wait
        within the merge function to use ALL_COMPLETED instead."""
        _original_wait = asyncio.wait

        async def _tolerant_wait(fs, **wait_kwargs):
            # Override FIRST_EXCEPTION → ALL_COMPLETED
            if wait_kwargs.get("return_when") == asyncio.FIRST_EXCEPTION:
                wait_kwargs["return_when"] = asyncio.ALL_COMPLETED
            done, pending = await _original_wait(fs, **wait_kwargs)
            return done, pending

        # Temporarily replace asyncio.wait within the operate module
        _op.asyncio.wait = _tolerant_wait
        try:
            # Also suppress the re-raise of first_exception by patching
            # the function to catch and log instead
            result = await _original_merge(*args, **kwargs)
            return result
        except Exception as e:
            _lg.warning(f"[FaultTolerant] Entity/edge merge error (non-fatal): {e}")
            # Return empty result instead of crashing the paper
            return None
        finally:
            _op.asyncio.wait = _original_wait

    _op.merge_nodes_and_edges = _fault_tolerant_merge


# ---------------------------------------------------------------------------
# Fix 2: Entity name normalization (dedup "AI Agent" vs "AI Agents" etc.)
# ---------------------------------------------------------------------------
# Common plural suffixes to strip (conservative — only clear English plurals)
_PLURAL_SUFFIXES = [
    ("ies", "y"),     # strategies → strategy
    ("yses", "ysis"), # analyses → analysis, paralyses → paralysis
    ("ases", "ase"),  # databases → database, cases → case, phases → phase
    ("nses", "nse"),  # responses → response, licenses → license
    ("ches", "ch"),   # approaches → approach
    ("shes", "sh"),   # crashes → crash
    ("xes", "x"),     # indexes → index
]

def _normalize_entity_name(name: str) -> str:
    """Normalize entity name for dedup: title case, collapse whitespace, strip plurals."""
    # Unicode canonicalization: collapse NFD/NFC variants so "Café" (NFC) and
    # "Café" (NFD: e + combining acute) compare equal. Protects self-loop
    # detection and dedup for CJK/Vietnamese/Arabic content.
    name = unicodedata.normalize("NFKC", name)
    # Collapse whitespace and hyphens
    name = re.sub(r"[\s\-_]+", " ", name).strip()
    # Title case (preserves acronyms if all-caps and <=5 chars)
    words = name.split()
    normalized = []
    for w in words:
        if w.isupper() and len(w) <= 5:
            normalized.append(w)  # Keep acronyms: AI, ML, LLM, NLP
        else:
            normalized.append(w.title())
    name = " ".join(normalized)
    # Strip common plural suffixes (only if result is >= 3 chars)
    lower = name.lower()
    for suffix, replacement in _PLURAL_SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix) + 2:
            name = name[:-len(suffix)] + replacement
            break
    else:
        # No special plural matched — check generic trailing 's'
        # But not: analysis, loss, chaos, etc.
        if (lower.endswith("s") and not lower.endswith("ss")
                and len(name) > 3
                and not lower.endswith(("is", "us", "os"))):
            name = name[:-1]
    return name


# ---------------------------------------------------------------------------
# Fix 3: Short entity filter (reject math symbols like α, μ, d, y)
# ---------------------------------------------------------------------------
SHORT_ENTITY_ALLOWLIST = {
    "AI", "ML", "5G", "6G", "NLP", "RL", "CV", "DL", "RF", "GAN", "RNN",
    "CNN", "LLM", "GPU", "API", "RAG", "IoT", "QA", "UI", "UX", "OS",
    "SQL", "AWS", "GCP", "RPA", "ETL", "KNN", "SVM", "PCA", "MLP",
}

def _is_valid_entity_name(name: str) -> bool:
    """Reject entities that are too short, numeric, or math symbols."""
    stripped = name.strip()
    if len(stripped) <= 2 and stripped.upper() not in SHORT_ENTITY_ALLOWLIST:
        return False
    # Reject pure numeric or version-like strings
    if re.match(r"^[\d.,\-\s]+$", stripped):
        return False
    if re.match(r"^\d+\.\d+(\.\d+)?$", stripped):
        return False
    # Reject single Greek/math symbols
    if len(stripped) <= 3 and re.match(r"^[\u0370-\u03FF\u1D00-\u1DBF\u2200-\u22FF\U0001D400-\U0001D7FF_]+$", stripped):
        return False
    return True


# ---------------------------------------------------------------------------
# Fix 4: Entity type constraint (only allow the 10 defined types)
# ---------------------------------------------------------------------------
_VALID_TYPES = {t.lower() for t in ENTITY_TYPES}

_TYPE_MAPPING = {
    # Map off-spec types to valid ones
    "other": None,          # Too vague — reject
    "unknown": "concept",   # Fallback
    "publication": None,    # Paper title references — reject
    "book": None,           # Same
    "document": None,       # Same
    "section": None,        # Section headers leaked in — reject
    "figure": None,         # Figure captions leaked in — reject
    "location": None,       # Rarely useful
    "event": "concept",     # Close enough
    "standard": "concept",  # Close enough
    "project": "framework", # Close enough
}

def _constrain_entity_type(entity_type: str) -> str | None:
    """Map entity type to valid type. Returns None to reject the entity."""
    lower = entity_type.lower().strip()
    if lower in _VALID_TYPES:
        return lower
    return _TYPE_MAPPING.get(lower)  # Returns None for unmapped types


# ---------------------------------------------------------------------------
# Monkey-patch: wrap extraction handlers with filters
# ---------------------------------------------------------------------------
_extraction_patched = False

def _patch_extraction_filters():
    """Wrap entity/relationship extraction with quality filters."""
    global _extraction_patched
    if _extraction_patched:
        return
    _extraction_patched = True

    from lightrag import operate
    _orig_entity = operate._handle_single_entity_extraction
    _orig_relation = operate._handle_single_relationship_extraction

    async def _filtered_entity_extraction(record_attributes, chunk_key, timestamp, file_path="unknown_source"):
        result = await _orig_entity(record_attributes, chunk_key, timestamp, file_path)
        if result is None:
            return None

        entity_name = result.get("entity_name", "")
        entity_type = result.get("entity_type", "")

        # Fix 3: reject short/junk entities
        if not _is_valid_entity_name(entity_name):
            return None

        # Fix 4: constrain entity type
        constrained_type = _constrain_entity_type(entity_type)
        if constrained_type is None:
            return None
        result["entity_type"] = constrained_type

        # Fix 2: normalize entity name for dedup
        result["entity_name"] = _normalize_entity_name(entity_name)

        return result

    async def _filtered_relation_extraction(record_attributes, chunk_key, timestamp, file_path="unknown_source"):
        result = await _orig_relation(record_attributes, chunk_key, timestamp, file_path)
        if result is None:
            return None

        # Fix 2: normalize source/target names to match normalized entities
        source = result.get("src_id", "")
        target = result.get("tgt_id", "")

        if not _is_valid_entity_name(source) or not _is_valid_entity_name(target):
            return None

        src_norm = _normalize_entity_name(source)
        tgt_norm = _normalize_entity_name(target)

        # Fix 6: drop self-loop relations. A self-loop (src == tgt after
        # normalization) causes a deadlock inside LightRAG's _KeyedLockContext:
        # Phase 2 _locked_process_edges acquires the keyed lock with
        # sorted_edge_key = [src, tgt]; when src == tgt the context iterates
        # the same asyncio.Lock twice and the second acquire waits forever
        # on a lock it already holds (asyncio.Lock is not reentrant).
        # Confirmed 2026-04-11 via watchdog dump: `GraphDB:Yolov5` count=3 locked=True.
        if src_norm.strip().lower() == tgt_norm.strip().lower():
            return None

        result["src_id"] = src_norm
        result["tgt_id"] = tgt_norm

        return result

    operate._handle_single_entity_extraction = _filtered_entity_extraction
    operate._handle_single_relationship_extraction = _filtered_relation_extraction

    # Fix 6 (safety net): also patch _KeyedLockContext to dedupe keys.
    # If any future caller passes a list with duplicate keys, the lock
    # context deadlocks on itself (asyncio.Lock is not reentrant). Dedupe
    # the key list at __init__ so duplicates become no-ops.
    from lightrag.kg import shared_storage as _ss
    if not getattr(_ss._KeyedLockContext, "_dedupe_patched", False):
        _orig_kl_init = _ss._KeyedLockContext.__init__

        def _patched_kl_init(self, parent, namespace, keys, enable_logging):
            deduped = list(dict.fromkeys(keys))  # preserves order, drops dupes
            if len(deduped) != len(keys):
                # Escalated to ERROR + stack: any dedupe fire means the upstream
                # self-loop filter was bypassed (truncation collision, NFD/NFC
                # mismatch, stale persisted self-loop, or new code path). Needs
                # investigation — don't let it hide as a warning.
                _kb_log.error(
                    f"[lightrag_kb] _KeyedLockContext dedupe fired: {keys} -> {deduped} "
                    f"(self-loop would have deadlocked — upstream filter missed it)",
                    stack_info=True,
                )
            _orig_kl_init(self, parent, namespace, deduped, enable_logging)

        _ss._KeyedLockContext.__init__ = _patched_kl_init
        _ss._KeyedLockContext._dedupe_patched = True


# ---------------------------------------------------------------------------
# Fix 5: Cap entity/relation description lists before LLM merge
# Root cause of ingest hangs: common entities accumulate 50+ descriptions
# across papers, causing _handle_entity_relation_summary to trigger
# multi-round map-reduce LLM calls that timeout (360s+).
# Fix: keep the existing summary + only the N most recent new descriptions.
# ---------------------------------------------------------------------------
MAX_DESCRIPTIONS_FOR_MERGE = 6  # existing summary (1) + up to 5 new descriptions

def _patch_description_cap():
    """Cap description lists before LLM merge to prevent timeout on common entities.

    When entity "Neural Network" has been seen in 20 papers, the merge function
    receives 50+ descriptions and triggers multi-round LLM summarization that
    times out. This patch keeps the existing summary (first entry) plus only
    the most recent new descriptions, drastically reducing LLM input size.
    """
    from lightrag import operate

    _orig_summary = operate._handle_entity_relation_summary

    async def _capped_summary(
        description_type,
        entity_or_relation_name,
        description_list,
        separator,
        global_config,
        llm_response_cache=None,
    ):
        if len(description_list) > MAX_DESCRIPTIONS_FOR_MERGE:
            original_count = len(description_list)
            # Keep first entry (existing merged summary) + last N-1 (newest descriptions)
            keep_count = MAX_DESCRIPTIONS_FOR_MERGE
            description_list = [description_list[0]] + description_list[-(keep_count - 1):]
            print(
                f"[LightRAG] Capped `{entity_or_relation_name}` descriptions: "
                f"{original_count} → {len(description_list)}"
            )
        return await _orig_summary(
            description_type,
            entity_or_relation_name,
            description_list,
            separator,
            global_config,
            llm_response_cache,
        )

    operate._handle_entity_relation_summary = _capped_summary


def _patch_extraction_prompts():
    """Override LightRAG's default extraction prompts for academic paper domain."""
    from lightrag.prompt import PROMPTS

    # Patch system prompt: add negative instruction about numeric values
    original = PROMPTS["entity_extraction_system_prompt"]
    numeric_rule = """
9.  **Exclusions:** Do NOT extract:
    - Bare numeric values, percentages, or table cell values (e.g., '3.4%', '60.6', '$87.60')
    - Single letters or Greek/math symbols (e.g., 'α', 'd', 'μ', 'σ')
    - Section headers, figure captions, or page numbers (e.g., 'Section 4.2', 'Figure 5')
    - Paper titles or journal names as entities — focus on the concepts within papers
    Only extract named, meaningful concepts (3+ characters) that would be useful as knowledge graph nodes."""
    # Insert before the completion signal instruction (item 8)
    PROMPTS["entity_extraction_system_prompt"] = original.replace(
        "8.  **Completion Signal:**",
        numeric_rule + "\n\n10.  **Completion Signal:**",
    )

    # Replace few-shot examples with academic paper domain examples
    td = "{tuple_delimiter}"
    cd = "{completion_delimiter}"
    et = str(ENTITY_TYPES)
    PROMPTS["entity_extraction_examples"] = [
        f"""<Entity_types>
{et}

<Input Text>
```
Reinforcement learning has emerged as a powerful framework for automated trading. Deep Q-Networks (DQN) and Proximal Policy Optimization (PPO) are among the most widely used methods. FinRL, an open-source library built on PyTorch, provides standardized environments for training RL agents on financial data. Experiments on the Dow Jones 30 dataset show that PPO achieves a Sharpe ratio of 1.8, outperforming traditional mean-variance optimization by 23%.
```

<Output>
entity{td}Reinforcement Learning{td}concept{td}Reinforcement learning is a machine learning paradigm used as a framework for automated trading systems.
entity{td}Deep Q-Networks{td}model{td}Deep Q-Networks (DQN) is a deep reinforcement learning model widely used in automated trading.
entity{td}Proximal Policy Optimization{td}method{td}Proximal Policy Optimization (PPO) is an RL algorithm used for training trading agents, achieving a Sharpe ratio of 1.8 on Dow Jones 30.
entity{td}FinRL{td}framework{td}FinRL is an open-source library built on PyTorch that provides standardized environments for training RL agents on financial data.
entity{td}PyTorch{td}framework{td}PyTorch is a deep learning framework used as the foundation for FinRL.
entity{td}Dow Jones 30{td}dataset{td}Dow Jones 30 is a stock dataset used for evaluating RL trading agent performance.
entity{td}Sharpe Ratio{td}metric{td}Sharpe ratio is a risk-adjusted return metric used to evaluate trading strategy performance.
entity{td}Mean-Variance Optimization{td}method{td}Mean-variance optimization is a traditional portfolio allocation method used as a baseline for comparison.
entity{td}Automated Trading{td}task{td}Automated trading is the task of using algorithms to execute financial trades without human intervention.
relation{td}Proximal Policy Optimization{td}FinRL{td}implementation, training{td}PPO agents are trained using the FinRL library's standardized environments.
relation{td}FinRL{td}PyTorch{td}dependency, foundation{td}FinRL is built on PyTorch as its deep learning backend.
relation{td}Proximal Policy Optimization{td}Dow Jones 30{td}evaluation, benchmark{td}PPO was evaluated on the Dow Jones 30 dataset achieving a Sharpe ratio of 1.8.
relation{td}Proximal Policy Optimization{td}Mean-Variance Optimization{td}comparison, outperformance{td}PPO outperforms mean-variance optimization by 23% on the benchmark.
relation{td}Deep Q-Networks{td}Automated Trading{td}application, method{td}DQN is applied to the automated trading task as an RL-based approach.
{cd}

""",
        f"""<Entity_types>
{et}

<Input Text>
```
LangGraph enables developers to build multi-agent systems where a supervisor agent delegates tasks to specialized worker agents. ReAct (Reasoning + Acting) is a prompting pattern that interleaves chain-of-thought reasoning with tool use. The Voyager framework demonstrates that LLM agents can autonomously explore Minecraft environments by writing and executing code. Google DeepMind introduced Gemini, a multimodal model capable of processing text, images, and code.
```

<Output>
entity{td}LangGraph{td}framework{td}LangGraph is a framework for building multi-agent systems with supervisor-worker delegation patterns.
entity{td}ReAct{td}method{td}ReAct (Reasoning + Acting) is a prompting pattern that interleaves chain-of-thought reasoning with tool use for LLM agents.
entity{td}Voyager{td}framework{td}Voyager is a framework demonstrating autonomous LLM agents that explore Minecraft by writing and executing code.
entity{td}Chain-Of-Thought Reasoning{td}method{td}Chain-of-thought reasoning is a prompting technique where the model breaks down problems step by step.
entity{td}Google DeepMind{td}organization{td}Google DeepMind is an AI research lab that developed the Gemini multimodal model.
entity{td}Gemini{td}model{td}Gemini is a multimodal model by Google DeepMind capable of processing text, images, and code.
entity{td}Multi-Agent Systems{td}concept{td}Multi-agent systems are architectures where multiple AI agents collaborate or compete to accomplish tasks.
entity{td}Tool Use{td}concept{td}Tool use is the capability of LLM agents to interact with external tools and APIs during task execution.
entity{td}Code Execution{td}tool{td}Code execution is a tool capability allowing agents to write and run code autonomously.
relation{td}LangGraph{td}Multi-Agent Systems{td}implementation, architecture{td}LangGraph provides the framework for implementing multi-agent systems with supervisor-worker patterns.
relation{td}ReAct{td}Chain-Of-Thought Reasoning{td}combination, enhancement{td}ReAct combines chain-of-thought reasoning with tool use actions.
relation{td}Voyager{td}Code Execution{td}core capability, autonomy{td}Voyager agents autonomously explore environments by writing and executing code.
relation{td}Google DeepMind{td}Gemini{td}development, creation{td}Google DeepMind developed the Gemini multimodal model.
{cd}

""",
        f"""<Entity_types>
{et}

<Input Text>
```
Pairs trading is a market-neutral strategy that identifies co-moving stock pairs using cointegration tests. The Augmented Dickey-Fuller test is commonly used to verify stationarity of the spread.
```

<Output>
entity{td}Pairs Trading{td}method{td}Pairs trading is a market-neutral trading strategy that identifies co-moving stock pairs using cointegration analysis.
entity{td}Cointegration{td}concept{td}Cointegration is a statistical property indicating a long-run equilibrium relationship between time series.
entity{td}Augmented Dickey-Fuller Test{td}method{td}The Augmented Dickey-Fuller test is a statistical method used to verify stationarity of price spreads in pairs trading.
entity{td}Stationarity{td}concept{td}Stationarity is a statistical property where a time series has constant mean and variance over time.
entity{td}Market-Neutral Strategy{td}concept{td}A market-neutral strategy aims to generate returns independent of overall market direction.
relation{td}Pairs Trading{td}Cointegration{td}methodology, identification{td}Pairs trading uses cointegration tests to identify co-moving stock pairs.
relation{td}Augmented Dickey-Fuller Test{td}Stationarity{td}verification, testing{td}The ADF test is used to verify stationarity of the spread between paired stocks.
relation{td}Pairs Trading{td}Market-Neutral Strategy{td}classification, type{td}Pairs trading is a type of market-neutral trading strategy.
{cd}

""",
    ]

# ---------------------------------------------------------------------------
# LightRAG instance (lazy init)
# ---------------------------------------------------------------------------
_rag_instance = None
_rag_initialized = False

async def reset_rag():
    """Destroy and recreate LightRAG instance — clears internal locks/state.

    PG storage and graphml file persist. Call between papers to prevent
    deadlock accumulation from keyed locks in merge_nodes_and_edges.
    """
    global _rag_instance, _rag_initialized
    if _rag_instance is not None:
        try:
            await asyncio.wait_for(_rag_instance.finalize_storages(), timeout=60)
        except Exception:
            pass
    # Reset module-level keyed locks (survive instance destruction)
    try:
        from lightrag.kg.shared_storage import finalize_share_data
        finalize_share_data()
    except Exception:
        pass
    _rag_instance = None
    _rag_initialized = False


def _scrub_self_loops_from_graphml():
    """Drop self-loop edges from the persisted graphml file on startup.

    Belt-and-suspenders for the self-loop deadlock fix (see _patch_extraction_filters).
    If a self-loop exists in the graph from a pre-fix run, document deletion would
    trigger `_locked_rebuild_relationship` which passes sorted([src, tgt]) to the
    keyed lock — with src==tgt this deadlocks _KeyedLockContext unless Fix 2 catches
    it. Scrubbing on startup guarantees the persisted state is clean, so the fix
    doesn't depend on Fix 2 being present forever.

    Idempotent — no-op if graphml is missing or has zero self-loops.
    """
    graph_file = WORKDIR / "graph_chunk_entity_relation.graphml"
    if not graph_file.exists():
        return
    try:
        import networkx as nx
        G = nx.read_graphml(str(graph_file))
        self_loops = [(u, v) for u, v in G.edges() if u == v]
        if not self_loops:
            return
        _kb_log.warning(
            f"[lightrag_kb] Scrubbing {len(self_loops)} self-loop edges from graphml "
            f"(pre-fix stale data): {self_loops[:5]}{'...' if len(self_loops) > 5 else ''}"
        )
        G.remove_edges_from(self_loops)
        backup = graph_file.with_suffix(".graphml.preselflop")
        if not backup.exists():
            import shutil
            shutil.copy2(str(graph_file), str(backup))
        nx.write_graphml(G, str(graph_file))
        _kb_log.warning(f"[lightrag_kb] Self-loop scrub complete. Backup: {backup.name}")
    except Exception as e:
        _kb_log.error(f"[lightrag_kb] Self-loop scrub failed: {e}", exc_info=True)


async def get_rag() -> "LightRAG":
    """Get or create the LightRAG instance (async — requires initialization)."""
    global _rag_instance, _rag_initialized
    if _rag_instance is not None and _rag_initialized:
        return _rag_instance

    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc

    if _rag_instance is None:
        _patch_extraction_prompts()
        _patch_extraction_filters()
        _patch_networkx_write()
        _patch_description_cap()
        _scrub_self_loops_from_graphml()
        # _patch_fault_tolerant_merging()  # DISABLED — global asyncio.wait replacement causes deadlocks
        _rag_instance = LightRAG(
            working_dir=str(WORKDIR),
            # LLM
            llm_model_func=llm_func,
            llm_model_name="gemini-2.5-flash-lite",
            llm_model_max_async=1,  # MUST be 1 — 2+ deadlocks LightRAG's entity merge locks on Windows
            default_llm_timeout=600,  # 10 min → worker timeout = 1200s
            default_embedding_timeout=120,  # ONNX cold load needs >30s on busy system
            # Embeddings
            embedding_func=EmbeddingFunc(
                embedding_dim=768,
                max_token_size=2048,
                func=embedding_func,
                model_name="embeddinggemma-300m",
            ),
            # Storage — PG-backed KV/vector/doc_status, file-based graph
            kv_storage="PGKVStorage",
            vector_storage="PGVectorStorage",
            graph_storage="NetworkXStorage",
            doc_status_storage="PGDocStatusStorage",
            # Chunking
            chunk_token_size=1600,
            chunk_overlap_token_size=100,
            # Entity extraction
            entity_extract_max_gleaning=0,  # skip second pass
            addon_params={"entity_types": ENTITY_TYPES},
        )

    if not _rag_initialized:
        await _rag_instance.initialize_storages()
        _rag_initialized = True

    return _rag_instance


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------
async def ingest_text(text: str, doc_id: str | None = None):
    """Ingest a text document into the knowledge graph."""
    rag = await get_rag()
    await rag.ainsert(text)
    _kb_log.info(f"Ingested document{f' ({doc_id})' if doc_id else ''}")


async def query(question: str, mode: str = "hybrid") -> str:
    """Query the knowledge graph.
    Modes: naive, local, global, hybrid, mix
    """
    from lightrag import QueryParam
    rag = await get_rag()
    result = await rag.aquery(question, param=QueryParam(mode=mode))
    return result


MAX_CHUNKS_PER_BATCH = 5  # Split large papers into batches (executor patch fixes async deadlock)
CHUNKS_PER_VIRTUAL_BOOK = 50  # Large books split into virtual books of this size
MIN_CHUNK_LENGTH = 50  # Skip chunks shorter than this after cleaning (junk fragments)
MAX_DOTS_RATIO = 0.30  # Skip chunks where >30% of non-space chars are dots (TOC pages)


def _clean_chunk(text: str) -> str:
    """Clean a chunk of text before sending to LightRAG entity extraction.

    Fixes for broken PDF→markdown extraction:
    - Strip U+FFFD replacement chars (OCR failures)
    - Remove non-printable control chars (keep \\n, \\r, \\t)
    - Collapse runs of dots (TOC leaders) to a single '.'
    - Drop lone-character math-junk lines (common in mangled LaTeX)

    Returns cleaned text, possibly empty if nothing salvageable.
    """
    if not text:
        return ""

    # Strip U+FFFD replacement chars
    text = text.replace("\ufffd", "")

    # Remove other non-printable control chars, keep whitespace
    text = "".join(c for c in text if c >= " " or c in "\n\r\t")

    # Collapse TOC dot leaders: "......." → "."
    text = re.sub(r"\.{4,}", ".", text)

    # Drop lines that are just 1-3 chars of math junk (e.g. "_T L ._", "R1", "= 0.")
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        # Keep empty lines for paragraph breaks
        if not stripped:
            cleaned_lines.append(line)
            continue
        # Drop lines that are <= 5 chars and contain no alphanumeric word chars (≥3 letters)
        if len(stripped) <= 5 and not re.search(r"[A-Za-z]{3,}", stripped):
            continue
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def _should_skip_chunk(text: str) -> tuple[bool, str]:
    """Return (skip, reason). Called on the CLEANED chunk."""
    if len(text) < MIN_CHUNK_LENGTH:
        return True, f"too short ({len(text)} < {MIN_CHUNK_LENGTH} chars)"

    # TOC detection: ratio of dots to non-space chars
    non_space = [c for c in text if not c.isspace()]
    if non_space:
        dots_ratio = sum(1 for c in non_space if c == ".") / len(non_space)
        if dots_ratio > MAX_DOTS_RATIO:
            return True, f"TOC page ({dots_ratio:.0%} dots)"

    # Alpha ratio: if <40% of non-space chars are letters, it's binary/math junk
    if non_space:
        alpha_ratio = sum(1 for c in non_space if c.isalpha()) / len(non_space)
        if alpha_ratio < 0.40:
            return True, f"low alpha ratio ({alpha_ratio:.0%})"

    return False, ""


async def ingest_paper_from_db(paper_id: int, chunk_offset: int = 0, chunk_limit: int = None):
    """Pull a paper's chunks from paper_chunks table and ingest into LightRAG.

    chunk_offset/chunk_limit: process only a slice of chunks (virtual book).
    Papers with more than MAX_CHUNKS_PER_BATCH chunks are split into
    multiple batches to prevent asyncio deadlocks on large books.
    """
    import asyncpg
    conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])
    try:
        # Get title
        title_row = await conn.fetchrow(
            "SELECT title FROM papers WHERE id = $1", paper_id
        )
        title = title_row["title"] if title_row else None
        if not title:
            title_row2 = await conn.fetchrow(
                "SELECT paper_title FROM paper_chunks WHERE paper_id = $1 LIMIT 1", paper_id
            )
            title = title_row2["paper_title"] if title_row2 else f"paper_{paper_id}"

        # Get total chunk count for labeling
        total_chunks = await conn.fetchval(
            "SELECT count(*) FROM paper_chunks WHERE paper_id = $1", paper_id
        )

        # Fetch only the slice we need (avoids redundant full-table fetch per virtual book)
        if chunk_limit is not None:
            rows = await conn.fetch(
                "SELECT chunk_text FROM paper_chunks WHERE paper_id = $1 ORDER BY id OFFSET $2 LIMIT $3",
                paper_id, chunk_offset, chunk_limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT chunk_text FROM paper_chunks WHERE paper_id = $1 ORDER BY id",
                paper_id,
            )
    finally:
        await conn.close()

    if not rows:
        _kb_log.info(f"No chunks found for paper_id={paper_id} (offset={chunk_offset}, limit={chunk_limit})")
        return

    raw_chunks = [r["chunk_text"] for r in rows]

    # Pre-process: clean garbage (replacement chars, control bytes, TOC leaders)
    # and drop chunks that are too short, too dotty (TOC), or low-alpha (binary junk).
    chunks = []
    skipped_stats = {"too_short": 0, "toc": 0, "low_alpha": 0}
    for raw in raw_chunks:
        cleaned = _clean_chunk(raw)
        skip, reason = _should_skip_chunk(cleaned)
        if skip:
            if "too short" in reason:
                skipped_stats["too_short"] += 1
            elif "TOC" in reason:
                skipped_stats["toc"] += 1
            elif "alpha" in reason:
                skipped_stats["low_alpha"] += 1
            continue
        chunks.append(cleaned)

    if len(chunks) < len(raw_chunks):
        dropped = len(raw_chunks) - len(chunks)
        _kb_log.info(
            f"  Cleaned: kept {len(chunks)}/{len(raw_chunks)} chunks "
            f"(dropped {dropped}: {skipped_stats['too_short']} short, "
            f"{skipped_stats['toc']} TOC, {skipped_stats['low_alpha']} low-alpha)"
        )

    if not chunks:
        _kb_log.warning(f"All {len(raw_chunks)} chunks filtered out as garbage, skipping")
        return

    # Recompute slice_label with cleaned count for accurate logging
    slice_label = f" [chunks {chunk_offset}-{chunk_offset + len(raw_chunks) - 1}/{total_chunks}]" if chunk_limit else ""

    # Pre-flight dedup check — skip if all content already processed
    import asyncpg as _apg
    _pf_conn = await _apg.connect(os.environ["AGENT_MEMORY_DB_URI"])
    try:
        all_already_done = True
        for batch_start in range(0, len(chunks), MAX_CHUNKS_PER_BATCH):
            batch = chunks[batch_start : batch_start + MAX_CHUNKS_PER_BATCH]
            batch_text = "\n\n".join(batch)
            content_md5 = hashlib.md5(batch_text.encode()).hexdigest()
            doc_key = f"doc-{content_md5}"
            exists = await _pf_conn.fetchval(
                "SELECT 1 FROM lightrag_doc_status WHERE id = $1 AND status = 'processed'",
                doc_key,
            )
            if not exists:
                all_already_done = False
                break
        if all_already_done:
            _kb_log.info(f"'{title}'{slice_label} — all batches already processed, skipping")
            return  # Success — no error raised
    finally:
        await _pf_conn.close()

    batch_size = MAX_CHUNKS_PER_BATCH
    BATCH_TIMEOUT = 600  # 10 min timeout per batch — 300s was too short for dense academic papers

    if len(chunks) <= batch_size:
        full_text = "\n\n".join(chunks)
        doc_id = f"{title}{slice_label}" if slice_label else title
        _kb_log.info(f"Ingesting '{title}'{slice_label} ({len(chunks)} chunks, {len(full_text)} chars)...")
        await asyncio.wait_for(ingest_text(full_text, doc_id=doc_id), timeout=BATCH_TIMEOUT)
    else:
        # Split into batches
        n_batches = (len(chunks) + batch_size - 1) // batch_size
        _kb_log.info(f"'{title}'{slice_label} ({len(chunks)} chunks) → {n_batches} batches (size={batch_size})")
        batch_errors = 0
        failed_batch_nums = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            batch_num = i // batch_size + 1
            batch_text = "\n\n".join(batch)
            batch_id = f"{title}{slice_label} [Part {batch_num}/{n_batches}]"
            _kb_log.info(f"  Batch {batch_num}/{n_batches}: {len(batch)} chunks, {len(batch_text)} chars")
            try:
                await asyncio.wait_for(ingest_text(batch_text, doc_id=batch_id), timeout=BATCH_TIMEOUT)
            except (asyncio.TimeoutError, Exception) as e:
                batch_errors += 1
                failed_batch_nums.append(batch_num)
                _kb_log.error(f"  Batch {batch_num}/{n_batches} FAILED ({type(e).__name__}), skipping remaining batches")
                break  # Don't continue — next batch's ainsert() would reprocess this zombie
            if batch_num < n_batches:
                await asyncio.sleep(2)  # Brief pause between batches
        _kb_log.info(f"'{title}'{slice_label} done: {n_batches - batch_errors}/{n_batches} batches OK")
        if batch_errors > 0:
            raise RuntimeError(f"{batch_errors}/{n_batches} batches failed (batches: {failed_batch_nums})")


async def ingest_all_papers(delay_between: float = 1.0):
    """Bulk ingest all papers from the database.

    delay_between: seconds to wait between papers (default 1s).
    """
    import asyncpg
    conn = await asyncpg.connect(os.environ["AGENT_MEMORY_DB_URI"])
    try:
        papers = await conn.fetch("SELECT id, title FROM papers ORDER BY id")
        print(f"[LightRAG] Found {len(papers)} papers to ingest (delay={delay_between}s between)")
        for p in papers:
            print(f"  [{p['id']}] {p['title']}")
        for i, p in enumerate(papers):
            print(f"\n--- Paper {i+1}/{len(papers)} ---")
            try:
                await ingest_paper_from_db(p["id"])
            except Exception as e:
                print(f"[LightRAG] ERROR ingesting paper {p['id']} ({p['title']}): {e}")
                print("[LightRAG] Continuing to next paper...")
            if i < len(papers) - 1:
                print(f"[LightRAG] Rate limit pause ({delay_between}s)...")
                await asyncio.sleep(delay_between)
        print(f"\n[LightRAG] Bulk ingest complete. Processed {len(papers)} papers.")
    finally:
        await conn.close()


async def reset_doc(doc_id: str | None = None):
    """Reset stuck PROCESSING/FAILED documents to PENDING status."""
    from lightrag.base import DocStatus
    rag = await get_rag()
    doc_store = rag.doc_status
    if doc_id:
        record = await doc_store.get_by_id(doc_id)
        if record:
            record["status"] = "pending"
            record["error_msg"] = None
            await doc_store.upsert({doc_id: record})
            print(f"[LightRAG] Reset document {doc_id} to PENDING")
        else:
            print(f"[LightRAG] Document {doc_id} not found")
    else:
        # Only reset recently-stuck processing docs (<2 hours old).
        # Docs stuck longer are likely too large and would deadlock again.
        # Don't touch failed docs (may be force-completed or duplicates).
        import asyncpg as _apg
        conn = await _apg.connect(os.environ["AGENT_MEMORY_DB_URI"])
        result = await conn.execute("""
            UPDATE lightrag_doc_status
            SET status = 'pending', error_msg = NULL
            WHERE status = 'processing'
            AND updated_at > NOW() - INTERVAL '2 hours'
        """)
        await conn.close()
        print(f"[LightRAG] Reset recent stuck docs: {result}")


def main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="LightRAG Knowledge Base")
    sub = parser.add_subparsers(dest="command")

    # query
    q = sub.add_parser("query", help="Query the knowledge graph")
    q.add_argument("question", help="Your question")
    q.add_argument("--mode", default="hybrid", choices=["naive", "local", "global", "hybrid", "mix"])

    # ingest-paper
    ip = sub.add_parser("ingest-paper", help="Ingest a paper from DB")
    ip.add_argument("paper_id", type=int)

    # ingest-all
    sub.add_parser("ingest-all", help="Ingest all papers from DB")

    # stats
    sub.add_parser("stats", help="Show graph statistics")

    # reset-doc
    rd = sub.add_parser("reset-doc", help="Reset stuck PROCESSING/FAILED docs to PENDING")
    rd.add_argument("doc_id", nargs="?", default=None, help="Specific doc ID, or omit to reset all stuck")

    args = parser.parse_args()

    if args.command == "query":
        result = asyncio.run(query(args.question, args.mode))
        print(result)
    elif args.command == "ingest-paper":
        asyncio.run(ingest_paper_from_db(args.paper_id))
    elif args.command == "ingest-all":
        asyncio.run(ingest_all_papers())
    elif args.command == "stats":
        graph_file = WORKDIR / "graph_chunk_entity_relation.graphml"
        if graph_file.exists():
            import networkx as nx
            G = nx.read_graphml(graph_file)
            print(f"Nodes: {G.number_of_nodes()}")
            print(f"Edges: {G.number_of_edges()}")
        else:
            print("No graph file yet. Run ingest first.")
    elif args.command == "reset-doc":
        asyncio.run(reset_doc(args.doc_id))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

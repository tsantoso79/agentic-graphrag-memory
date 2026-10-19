"""
memory_helper.py - CLI tool for agent persistent memory operations.
Connects to PostgreSQL agent_memory database.

Usage:
    python memory_helper.py <command> [options]

Commands:
    startup             Query DB, write MEMORY.md, spawn Sonnet processor if pending
    save-session        Record session end with summary/outcome (+ optional reflection)
    save-context        Save important context before /compact
    store-memory        Store a new memory
    recall              Search memories by keyword and/or category
    store-reflection    Store a post-task reflection
    store-backtest      Store backtest results (from JSON file or args)
    get-strategy-history  Show evolution of a strategy
    promote-lessons     Check reflections for recurring patterns → lessons_learned
    auto-reflect        Auto-generate fallback reflection from session DB data
    store-transcript    Filter and store transcript for deferred Sonnet processing
    process-pending     Claim oldest pending transcript and output structured content
    mark-processed      Update transcript processing status
    backfill-embeddings Generate embeddings for memories missing them
    cleanup             Expire old memories, clean up transcripts, show stats
    get-exposures       List unscored memory exposures for a session
    score-exposure      Score a memory exposure as useful or not
"""

import argparse
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 output on Windows (console defaults to cp1252, breaks on Unicode)
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Trace relay (lazy — no cost if CLAUDE_TRACE_ENABLED not set)
# ---------------------------------------------------------------------------
_trace_enabled = os.environ.get("CLAUDE_TRACE_ENABLED", "0") in ("1", "true")
if _trace_enabled:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from tracer.trace_relay import emit_trace, ensure_server_running
    except Exception:
        def emit_trace(*a, **kw): pass
        def ensure_server_running(): pass
else:
    def emit_trace(*a, **kw): pass
    def ensure_server_running(): pass

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded)
# ---------------------------------------------------------------------------
_embed_model = None


def get_embed_model():
    """Lazy-load model2vec StaticModel. ~50ms first call, cached after."""
    global _embed_model
    if _embed_model is None:
        from model2vec import StaticModel
        _embed_model = StaticModel.from_pretrained("minishlab/potion-base-32M")
    return _embed_model


def embed_text(text):
    """Return a 512-dim numpy vector for the given text."""
    model = get_embed_model()
    return model.encode(text)


def _skip_memory_loop():
    """Return True if this process should not participate in the memory loop.

    Catches:
    - Our own claude -p spawns (MEMORY_HELPER_SUBPROCESS=1)
    - Any external script that opts out (MEMORY_LOOP_SKIP=1)
    """
    return bool(
        os.environ.get("MEMORY_HELPER_SUBPROCESS")
        or os.environ.get("MEMORY_LOOP_SKIP")
    )


def read_hook_stdin():
    """Read hook JSON from stdin (non-blocking). Returns dict or None."""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                return json.loads(raw)
    except Exception:
        pass
    return None


def find_last_compact_boundary(transcript_path):
    """Reverse byte-scan to find the last compact_boundary marker in a JSONL file.

    Instead of parsing every JSON line (slow on large files), reads 4MB chunks
    from the end and searches for the raw string "compact_boundary".  Only parses
    JSON on candidate lines.

    Returns the byte offset of the boundary line, or None if not found.
    """
    CHUNK = 4 * 1024 * 1024  # 4 MB
    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return None

    if file_size == 0:
        return None

    try:
        with open(transcript_path, "rb") as f:
            pos = file_size
            while pos > 0:
                read_start = max(0, pos - CHUNK)
                f.seek(read_start)
                chunk = f.read(pos - read_start)

                # Search backwards for the marker string
                search_from = len(chunk)
                while True:
                    idx = chunk.rfind(b'"compact_boundary"', 0, search_from)
                    if idx < 0:
                        break
                    # Find the full line containing this match
                    line_start = chunk.rfind(b'\n', 0, idx)
                    line_start = line_start + 1 if line_start >= 0 else 0
                    line_end = chunk.find(b'\n', idx)
                    if line_end < 0:
                        line_end = len(chunk)
                    line = chunk[line_start:line_end]
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                            return read_start + line_start
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # Keep searching earlier in this chunk
                    search_from = idx

                # Overlap by 1KB to avoid missing a line split across chunks
                pos = read_start + 1024 if read_start > 0 else 0
    except Exception:
        return None

    return None


def extract_transcript_summary(transcript_path):
    """Parse a JSONL transcript file and return a fixed-logic activity summary.
    Returns a string with user topics, files touched, and tool usage counts."""
    from collections import Counter

    if not transcript_path or not os.path.exists(transcript_path):
        return None

    user_messages = []
    tool_counts = Counter()
    files_modified = set()

    # Single pass: collect data (include all content, pre and post compact)
    # Pass 1 (boundary finding) is eliminated — boundary_offset is pre-computed
    # by find_last_compact_boundary() in cmd_session_end.
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Skip compact_boundary marker line (detected by content)
                if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                    continue

                msg_type = obj.get("type")

                # Collect user messages (skip meta/commands)
                if msg_type == "user" and not obj.get("isMeta"):
                    content = obj.get("message", {}).get("content", "")
                    if (isinstance(content, str) and len(content) > 5
                            and "<command-name>" not in content
                            and "<local-command" not in content
                            and "<system-reminder>" not in content):
                        user_messages.append(content[:150])

                # Collect tool usage from assistant messages
                elif msg_type == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                name = block.get("name", "")
                                tool_counts[name] += 1
                                inp = block.get("input", {})
                                fp = inp.get("file_path", "")
                                if fp and name in ("Edit", "Write"):
                                    files_modified.add(fp)
    except Exception as e:
        print(f"WARNING: transcript parse error: {e}", file=sys.stderr)
        return None

    if not user_messages and not tool_counts:
        return None

    parts = []

    # User topics (last 10 messages — most recent work is most relevant)
    if user_messages:
        topics = [m[:80] for m in user_messages[-10:]]
        parts.append("User topics:\n" + "\n".join(f"  - {t}" for t in topics))

    # Files modified
    if files_modified:
        # Show just filenames, not full paths
        short_names = sorted(os.path.basename(f) for f in files_modified)
        parts.append("Files modified: " + ", ".join(short_names))

    # Tool usage summary
    if tool_counts:
        top_tools = tool_counts.most_common(6)
        tool_str = ", ".join(f"{name}({cnt})" for name, cnt in top_tools)
        parts.append(f"Tool usage: {tool_str}")

    return "\n".join(parts)


def filter_transcript(transcript_path, started_at=None, boundary_offset=None):
    """Filter a JSONL transcript for deferred Sonnet processing.

    Single-pass approach (Pass 1 eliminated):
      boundary_offset is pre-computed by find_last_compact_boundary() in cmd_session_end.
      This pass filters entries and detects the boundary marker by content.

    Args:
      started_at: Optional datetime — skip JSONL entries with timestamp before this.
                  Used for claude -c to process only the current session's portion.
      boundary_offset: Byte offset of compact_boundary (from find_last_compact_boundary).
                       Used to set had_compaction flag. Boundary line detected by content.

    Returns dict:
      {compact_summary, raw_content, had_compaction, user_message_count, total_size_bytes}
    """
    MAX_RAW_BYTES = 2 * 1024 * 1024  # 2 MB safety cap

    if not transcript_path or not os.path.exists(transcript_path):
        return None

    # Pass 1 eliminated — boundary_offset pre-computed by find_last_compact_boundary()
    had_compaction = boundary_offset is not None
    found_boundary = False  # Track when we pass the boundary line in the stream

    # --- Single pass: filter entries, detect boundary by content ---
    compact_summary = None
    parts = []
    user_msg_count = 0
    seen_first_user_after_boundary = False

    # Key tool_use params to keep (truncated to 200 chars)
    KEEP_PARAMS = {"file_path", "command", "pattern", "query", "description",
                   "content", "url", "skill", "prompt"}

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):

                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    obj = json.loads(line_s)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Time-based filtering: skip entries from before this session started
                # Used for claude -c where transcript file contains previous sessions
                if started_at and obj.get("timestamp"):
                    try:
                        entry_time = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
                        sa = started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None else started_at
                        if entry_time < sa:
                            continue
                    except (ValueError, TypeError):
                        pass  # Unparseable timestamp — keep the entry

                msg_type = obj.get("type")

                # --- system: detect boundary by content, keep turn_duration ---
                if msg_type == "system":
                    if obj.get("subtype") == "compact_boundary":
                        parts.append("[--- COMPACT BOUNDARY ---]")
                        found_boundary = True
                        continue
                    if obj.get("subtype") == "turn_duration":
                        dur = obj.get("duration_seconds") or obj.get("duration", "?")
                        parts.append(f"[TURN_DURATION] {dur}s")
                    continue

                # --- progress, file-history-snapshot, queue-operation: discard ---
                if msg_type in ("progress", "file-history-snapshot", "queue-operation"):
                    continue

                # --- user messages ---
                if msg_type == "user":
                    # Skip meta messages (command outputs, system injections)
                    if obj.get("isMeta"):
                        continue

                    msg = obj.get("message", {})
                    content = msg.get("content", "")

                    # String content (direct user text)
                    if isinstance(content, str):
                        # Skip system-reminder injections, command outputs
                        if ("<system-reminder>" in content
                                or "<command-name>" in content
                                or "<local-command" in content):
                            continue

                        # First user message after boundary = compact summary
                        if found_boundary and not seen_first_user_after_boundary:
                            seen_first_user_after_boundary = True
                            if content.startswith("This session is being continued"):
                                compact_summary = content
                                continue

                        if len(content.strip()) > 3:
                            user_msg_count += 1
                            parts.append(f"[USER] {content.strip()}")

                    # List content (tool_result blocks from the user turn)
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "text":
                                text = (block.get("text") or "").strip()
                                # Skip system-reminder injections in text blocks
                                if text and "<system-reminder>" not in text:
                                    # Compact summary can also appear as a text block
                                    if (found_boundary and not seen_first_user_after_boundary
                                            and text.startswith("This session is being continued")):
                                        seen_first_user_after_boundary = True
                                        compact_summary = text
                                    elif block.get("isCompactSummary"):
                                        seen_first_user_after_boundary = True
                                        compact_summary = text
                                    # Don't emit tool_result text blocks — they're tool output
                            elif btype == "tool_result":
                                # Only keep error results
                                is_err = block.get("is_error", False)
                                if is_err:
                                    err_content = block.get("content", "")
                                    if isinstance(err_content, list):
                                        err_content = " ".join(
                                            b.get("text", "") for b in err_content
                                            if isinstance(b, dict) and b.get("type") == "text"
                                        )
                                    parts.append(f"[TOOL_ERROR] {str(err_content)[:500]}")
                    continue

                # --- assistant messages ---
                if msg_type == "assistant":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue

                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")

                        if btype == "thinking":
                            text = (block.get("thinking") or "").strip()
                            if text:
                                parts.append(f"[THINKING] {text}")

                        elif btype == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                parts.append(f"[ASSISTANT] {text}")

                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            # Extract only key params, truncated
                            key_params = {}
                            for k in KEEP_PARAMS:
                                if k in inp:
                                    val = str(inp[k])[:200]
                                    key_params[k] = val
                            parts.append(f"[TOOL] {name} {json.dumps(key_params)}")
                    continue

    except Exception as e:
        print(f"WARNING: filter_transcript pass 2 error: {e}", file=sys.stderr)
        return None

    raw_content = "\n".join(parts)
    total_size = len(raw_content.encode("utf-8"))

    # Size guard: truncate from beginning if over 2 MB
    if total_size > MAX_RAW_BYTES:
        # Binary search for the right split point
        encoded = raw_content.encode("utf-8")
        truncated = encoded[-MAX_RAW_BYTES:]
        # Find first newline to avoid partial line
        nl = truncated.find(b"\n")
        if nl >= 0:
            truncated = truncated[nl + 1:]
        raw_content = "[TRUNCATED]\n" + truncated.decode("utf-8", errors="replace")
        total_size = len(raw_content.encode("utf-8"))

    return {
        "compact_summary": compact_summary,
        "raw_content": raw_content,
        "had_compaction": had_compaction,
        "user_message_count": user_msg_count,
        "total_size_bytes": total_size,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URI = os.environ.get("AGENT_MEMORY_DB_URI")
if not DB_URI:
    print("ERROR: AGENT_MEMORY_DB_URI not set. Add it to ~/.claude/settings.json env block.",
          file=sys.stderr)
    sys.exit(1)

AGENT_ID = os.environ.get("AGENT_ID")
if not AGENT_ID:
    print("ERROR: AGENT_ID not set. Add it to ~/.claude/settings.json env block.",
          file=sys.stderr)
    sys.exit(1)

CHECKPOINT_GROWTH_BYTES = 1_000_000  # 1MB of JSONL growth triggers auto-checkpoint

# Paths are parameterized via env vars so the same script works on Windows and WSL.
# Windows sets these in settings.json env block; WSL sets them in its own settings.json.
# If no env var is set, derive from os.getcwd() using Claude Code's project dir naming.

def _cwd_to_project_dir():
    """Convert current working directory to Claude Code project directory name.

    Claude Code stores project config at ~/.claude/projects/<encoded-path>/.
    Encoding: replace : \\ / . space _ with dashes. No stripping.
    """
    result = []
    for ch in os.getcwd():
        if ch in (":", "\\", "/", ".", " ", "_"):
            result.append("-")
        else:
            result.append(ch)
    project_name = "".join(result)
    return Path.home() / ".claude" / "projects" / project_name


def _derive_memory_md_path():
    env_val = os.environ.get("MEMORY_MD_PATH")
    if env_val:
        return Path(env_val)
    return _cwd_to_project_dir() / "memory" / "MEMORY.md"


MEMORY_MD_PATH = _derive_memory_md_path()

# SESSION_ID_FILE sits next to MEMORY.md — per-project, not global.
SESSION_ID_FILE = Path(os.environ.get(
    "SESSION_ID_FILE",
    str(MEMORY_MD_PATH.parent / ".current_session_id")
))


def get_current_session_id():
    """Read the current session ID from the temp file, or return None.
    Returns None if the file is stale (older than 24 hours)."""
    if SESSION_ID_FILE.exists():
        # Staleness guard: if file is >24h old, likely from a crashed session
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                SESSION_ID_FILE.stat().st_mtime, tz=timezone.utc)
            if age > timedelta(hours=24):
                return None
        except Exception:
            pass  # If stat fails, proceed with reading
        sid = SESSION_ID_FILE.read_text(encoding="utf-8").strip()
        if sid:
            return sid
    return None


def get_project():
    """Return the project root (nearest ancestor with .claude/) or cwd as fallback."""
    cwd = os.getcwd()
    current = os.path.normpath(cwd)
    while current and current != os.path.dirname(current):
        if os.path.isdir(os.path.join(current, ".claude")):
            # Normalize drive letter to uppercase
            if len(current) >= 2 and current[1] == ":":
                current = current[0].upper() + current[1:]
            return current
        current = os.path.dirname(current)
    return cwd

# ---------------------------------------------------------------------------
# Transcript baseline (compaction-aware checkpointing)
# ---------------------------------------------------------------------------
def _write_transcript_baseline(transcript_path):
    """Write (or overwrite) the transcript baseline file for checkpoint delta tracking."""
    baseline_file = SESSION_ID_FILE.parent / ".transcript_baseline"
    initial_size = 0
    if transcript_path and os.path.exists(transcript_path):
        try:
            initial_size = os.path.getsize(transcript_path)
        except OSError:
            pass
    baseline_data = json.dumps({
        "size": initial_size,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "transcript_path": transcript_path or "",
    })
    try:
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(baseline_data, encoding="utf-8")
    except OSError:
        pass


def _extract_segment_summary(transcript_path, after_dt=None, seek_offset=0):
    """Extract a lightweight summary from JSONL entries after a given datetime.

    Similar to extract_transcript_summary but filters by timestamp and can seek
    past already-checkpointed content for performance.
    Returns None if no content found.
    """
    from collections import Counter

    if not transcript_path or not os.path.exists(transcript_path):
        return None

    user_messages = []
    tool_counts = Counter()
    files_modified = set()

    with open(transcript_path, encoding="utf-8") as f:
        if seek_offset > 0:
            f.seek(seek_offset)
            f.readline()  # skip partial line after seek
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # Filter by timestamp if provided
            if after_dt and obj.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(
                        obj["timestamp"].replace("Z", "+00:00"))
                    after_aware = (after_dt.replace(tzinfo=timezone.utc)
                                   if after_dt.tzinfo is None else after_dt)
                    if ts < after_aware:
                        continue
                except (ValueError, TypeError):
                    pass  # Unparseable timestamp — keep the entry

            msg_type = obj.get("type")

            if msg_type == "user" and not obj.get("isMeta"):
                content = obj.get("message", {}).get("content", "")
                if (isinstance(content, str) and len(content) > 5
                        and "<command-name>" not in content
                        and "<local-command" not in content
                        and "<system-reminder>" not in content):
                    user_messages.append(content[:150])

            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            tool_counts[name] += 1
                            inp = block.get("input", {})
                            fp = inp.get("file_path", "")
                            if fp and name in ("Edit", "Write"):
                                files_modified.add(fp)

    if not user_messages and not tool_counts:
        return None

    parts = []
    if user_messages:
        topics = [m[:80] for m in user_messages[-10:]]
        parts.append("User topics:\n" + "\n".join(f"  - {t}" for t in topics))
    if files_modified:
        short_names = sorted(os.path.basename(f) for f in files_modified)
        parts.append("Files modified: " + ", ".join(short_names))
    if tool_counts:
        top_tools = tool_counts.most_common(6)
        tool_str = ", ".join(f"{name}({cnt})" for name, cnt in top_tools)
        parts.append(f"Tool usage: {tool_str}")

    return "\n".join(parts)


def _auto_checkpoint(hook_input, conn):
    """Check transcript growth since last baseline. If exceeded, save checkpoint and reset.

    Returns a brief note string if checkpoint was saved, empty string otherwise.
    Designed to run within the 5-second hook timeout.
    Runs even when conn=None (DB down) — baseline check is file-based.
    """
    baseline_file = SESSION_ID_FILE.parent / ".transcript_baseline"
    if not baseline_file.exists():
        return ""

    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    tp = baseline.get("transcript_path") or (
        hook_input.get("transcript_path", "") if hook_input else "")
    if not tp or not os.path.exists(tp):
        return ""

    try:
        current_size = os.path.getsize(tp)
    except OSError:
        return ""

    baseline_size = baseline.get("size", 0)
    growth = current_size - baseline_size
    growth_kb = growth // 1024

    # Log growth to stderr for threshold calibration
    print(f"CHECKPOINT: growth={growth_kb}KB (current={current_size//1024}KB, "
          f"baseline={baseline_size//1024}KB)", file=sys.stderr)

    if growth < CHECKPOINT_GROWTH_BYTES:
        return ""

    # Threshold exceeded — extract summary of the new segment
    saved_at_str = baseline.get("saved_at", "")
    saved_at = None
    if saved_at_str:
        try:
            saved_at = datetime.fromisoformat(
                saved_at_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    summary = None
    try:
        summary = _extract_segment_summary(tp, saved_at, seek_offset=baseline_size)
    except Exception as e:
        print(f"CHECKPOINT: segment summary failed: {e}", file=sys.stderr)

    if not summary:
        summary = f"Auto-checkpoint at {growth_kb}KB growth (no extractable content)"

    # Store checkpoint in DB — only reset baseline if write succeeds
    db_saved = False
    session_id = get_current_session_id()
    if session_id and conn:
        try:
            safe_execute(conn, """
                UPDATE sessions
                SET summary = COALESCE(summary, '') || E'\n---checkpoint---\n' || %s
                WHERE session_id = %s
            """, (summary, session_id))
            db_saved = True
        except Exception as e:
            print(f"CHECKPOINT: DB write failed: {e}", file=sys.stderr)

    if not db_saved:
        print("CHECKPOINT: skipped baseline reset (DB write failed or unavailable)",
              file=sys.stderr)
        return (f"\n\nAuto-checkpoint: ~{growth_kb}KB of context since last save. "
                "DB unavailable — checkpoint NOT persisted. "
                "Save important decisions/findings with store-memory manually.")

    # Reset baseline only after successful DB write
    new_baseline = json.dumps({
        "size": current_size,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "transcript_path": tp,
    })
    try:
        baseline_file.write_text(new_baseline, encoding="utf-8")
    except OSError:
        pass

    print(f"CHECKPOINT: auto-saved at {growth_kb}KB growth, baseline reset",
          file=sys.stderr)
    return f"\n\nAuto-checkpoint saved ({growth_kb}KB of context). Baseline reset."


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------
def get_conn():
    conn = psycopg2.connect(DB_URI)
    conn.autocommit = True
    return conn


def safe_query(conn, sql, params=None):
    """Execute a query and return rows as dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if cur.description:
            return cur.fetchall()
        return []


def safe_execute(conn, sql, params=None):
    """Execute a statement (INSERT/UPDATE) without returning rows."""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())


def log_exposure(conn, session_id, memory_id, source, turn_number=None, similarity=None):
    """Log a memory exposure event and increment the corresponding counter (atomic)."""
    col = {"startup": "startup_count", "inject": "inject_count", "recall": "recall_count"}[source]
    safe_execute(conn, f"""
        WITH ins AS (
            INSERT INTO memory_exposures (session_id, memory_id, source, turn_number, similarity)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING memory_id
        )
        UPDATE memories SET {col} = {col} + 1
        FROM ins WHERE memories.id = ins.memory_id
    """, (session_id, memory_id, source, turn_number, similarity))


# ---------------------------------------------------------------------------
# Sonnet background processor - spawns claude -p to extract memories
# ---------------------------------------------------------------------------
PYTHON_EXE = os.environ.get("MEMORY_PYTHON", sys.executable)
HELPER_SCRIPT = str(Path(__file__).resolve())
LOG_DIR = Path(os.environ.get("MEMORY_LOG_DIR", Path(__file__).resolve().parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _build_sonnet_prompt():
    """Build the prompt for background Sonnet transcript processor."""
    py = PYTHON_EXE.replace("\\", "\\\\")
    helper = HELPER_SCRIPT.replace("\\", "\\\\")
    cmd_prefix = f'"{py}" "{helper}"'

    return f"""You are an automated memory extraction agent. Your job is to process session transcripts and extract structured memories and reflections for a persistent learning loop.

You have access to a CLI tool via Bash. The command prefix is:
{cmd_prefix}

## Quality Gates

Apply TWO gates to every candidate memory before storing:

1. **Discoverable?** Can a fresh agent find this by reading code, config files, or `--help`? Yes → SKIP.
2. **Novel + Actionable?** Does knowing this change what an agent would DO? No → SKIP.

## Memory Templates

Use the correct template for each type. Sonnet must match the phrasing:

**Structured types:**
- `decision`: "Chose [X] over [alternatives] because [rationale]."
- `bug`: "When [X], expect [Y] but actually [Z]. Fix: [workaround]." (or "Avoid [X] because [Y]" if no fix exists)
- `preference`: "User wants [X], not [Y]. Context: [when this applies]."
- `pattern` (failed approach): "[X] was tried and failed because [Y]. Don't revisit unless [condition]."

**Quality-gated free-form (no rigid template, but strict rules):**
- `fact`: Single declarative statement. Must contain a concrete noun (path, tool name, version, number). No changelog verbs.
- `pattern`: Must follow "When [trigger], [action] because [reason]."
- `fact` (domain knowledge): Quantitative finding + scope/evidence. E.g., "Model A outperforms baseline B (AUC 0.71 vs 0.65 on the 2022 holdout)."

Valid categories for --category: fact, decision, bug, pattern, preference, config, tool_usage
Valid domains for --domain: backtesting, ea_development, optimization, tool_errors, environment, problem_solving, debugging, general

## Banned Changelog Verbs

BANNED FIRST WORDS: "Integrated", "Created", "Built", "Added", "Implemented", "Fixed", "Designed", "Configured", "Set up", "Developed", "Established", "Introduced", "Migrated", "Refactored"

If your memory starts with or prominently features one of these, you are writing a changelog. Rephrase as what IS true now, not what was DONE.
  Bad:  "Added project filtering to MEMORY.md generation."
  Good: "MEMORY.md generation filters by project — cross-project memories excluded unless imp >= 9."
  Bad:  "Integrated systems thinking into problem-solver skill."
  Good: "problem-solver skill includes Systems Thinking lens (Template B Step C.5). Reference: systems-lens.md."

## Importance Anchors

4 = saves a few minutes of searching/debugging
5-6 = prevents a wrong tool choice or configuration error
7-8 = prevents a wrong architectural decision or repeating a failed experiment
9-10 = prevents data loss, security exposure, or major wasted effort
Never store below importance 4.

## Workflow

Loop until no more pending transcripts:

1. **Get next transcript:**
   ```
   {cmd_prefix} process-pending
   ```
   If output is "NO_PENDING", exit immediately (all done).

2. **Read the output** — it contains:
   - TRANSCRIPT_ID, SESSION_ID, PROJECT, HAD_COMPACTION
   - COMPACT_SUMMARY (between delimiters) — the Opus-generated session summary if compaction happened
   - RAW_CONTENT (between delimiters) — filtered transcript lines tagged as [USER], [ASSISTANT], [THINKING], [TOOL], [TOOL_ERROR], [TURN_DURATION]

3. **Phase 1 — TRIAGE:** Scan the entire transcript. List ALL candidate memories with:
   - Content (one line, using the correct template from above)
   - Template type (decision/bug/preference/pattern/fact)
   - Novel? (Y/N) — would a fresh agent find this in the codebase?
   - Actionable? (Y/N) — does knowing this change what an agent does?
   - Proposed importance (4-10, using anchors above)

   Only candidates with Novel=Y AND Actionable=Y proceed to Phase 2.

   **Don't even list as candidates:**
   - Bugs that were found AND fixed in the same session (the fix is in the code already)
   - Raw implementation details visible by reading current code
   - Generic lessons ("always test before deploying", "guard env vars are essential")
   - Historical decisions whose rationale is obvious from the code
   - Meta-observations about the memory system itself (unless truly novel)

4. **Phase 2 — DEDUP + STORE:** For each surviving candidate, check for existing similar memories:
   ```
   {cmd_prefix} recall --query "topic keywords" --limit 3 --semantic
   ```
   If a similar memory already exists, skip it (don't store duplicates).

5. **Store each unique memory** (always pass --project from step 2's PROJECT field):
   ```
   {cmd_prefix} store-memory --content "description" --category fact --domain general --importance 7 --project "PROJECT_VALUE" --tags tag1 tag2
   ```

   **Retrieval mode classification** — add these flags when applicable:
   - `--retrieval-mode tool --tool-name TOOL` for memories about a specific tool's behavior, quirks, or usage patterns.
     Tool names: Bash, Edit, Write, Read, Agent, WebFetch, Glob, Grep, chrome-mcp, playwright-mcp, slack-mcp, git, python, psql, npm, docker, claude-cli, hooks, settings, memory-system
   - `--retrieval-mode manual` for memories only useful when explicitly recalled (rare edge cases, historical context)
   - Default is `--retrieval-mode semantic` (omit the flag) — for general knowledge retrieved by topic similarity

   Examples:
   - Tool memory: `--retrieval-mode tool --tool-name chrome-mcp` (a quirk about Chrome MCP navigation)
   - Tool memory: `--retrieval-mode tool --tool-name git` (a git workflow pattern)
   - Semantic (default): no flag needed (a design decision, user preference, domain fact)

6. **Extract error→solution patterns** from failed tool calls in the transcript:
   Look for [TOOL_ERROR] entries or failed commands. For each distinct error pattern:
   ```
   {cmd_prefix} store-error-solution --error-pattern "the error message pattern" --solution "how to fix it" --tool-name TOOL --tags tag1 tag2
   ```
   - `--error-pattern`: The key error text (generalize paths/IDs where possible)
   - `--solution`: Concrete fix or workaround
   - `--tool-name`: Same tool names as retrieval mode above
   - Skip errors that were trivial typos or already have solutions in the transcript that are obvious from context

7. **Store one reflection for the session** (always pass --project from step 2's PROJECT field):
   ```
   {cmd_prefix} store-reflection --task-type "what was done" --domain general --outcome success --what-worked "description" --lessons "lesson1" "lesson2" --confidence 7 --project "PROJECT_VALUE"
   ```
   Outcome: success, partial, or failed. Confidence 1-10 based on how clear the lessons are.

8. **Mark processed AND write session handoff** (combined step — MANDATORY):
   ```
   {cmd_prefix} mark-processed --transcript-id N --status completed --memories-created M --session-id "SESSION_ID" --handoff-completed "what was accomplished" --handoff-next "most important next action" --handoff-pending "what remains open" --handoff-context "relevant context for next session"
   ```
   Where N is the TRANSCRIPT_ID, M is the count of memories stored, and SESSION_ID is from step 2.

   The four handoff fields are shown to the agent at the START of the next session in this project.
   Rules for handoff content:
   - **--handoff-completed**: Concrete deliverables or decisions. At least one specific noun (file, feature, fix).
     Good: "Implemented cmd_session_end combined hook, updated settings.json timeout to 60s."
     Bad: "Worked on memory system improvements."
   - **--handoff-next**: The single most important next action. What should the next session do FIRST?
     Good: "Live test session-end hook: exit and re-enter to verify MEMORY.md shows sync handoff."
     Bad: "Continue working on the project."
   - **--handoff-pending**: Other unfinished work or open questions (NOT the next action).
     Good: "Raise inject threshold from 0.3, test project-scoped retrieval."
     Bad: "Continue working on the project."
   - **--handoff-context**: User preferences, decisions, or constraints discovered during the session.
     Good: "User prefers single-command approach over dual-spawn. Rejected design team's option 2."
     Bad: "User was helpful."
   If the session was exploratory with no clear deliverables, state what was learned and what question remains open.

9. **Loop back to step 1** — call process-pending again to check for more.

## Error handling
- If any command fails, mark the transcript as failed:
  ```
  {cmd_prefix} mark-processed --transcript-id N --status failed --error-message "brief error description"
  ```
  Then continue to the next transcript (loop back to step 1).

## Important rules
- Be concise in memory content — no filler words, get to the point
- When HAD_COMPACTION is True, use the COMPACT_SUMMARY as primary context (it's an Opus-generated summary) and RAW_CONTENT as supplementary detail
- When HAD_COMPACTION is False, use RAW_CONTENT as the only source
- Track memory count accurately for mark-processed
- Exit cleanly when NO_PENDING is returned
- Do NOT store memories about the memory system itself unless something genuinely novel was discovered
- Apply the banned changelog verbs check to EVERY memory before storing
- Every memory must pass BOTH quality gates (Discoverable? + Novel+Actionable?)
"""


def _build_sonnet_supplementary_prompt():
    """Build the prompt for background Sonnet-Supplementary processor (entities, relationships, artifacts)."""
    py = PYTHON_EXE.replace("\\", "\\\\")
    helper = HELPER_SCRIPT.replace("\\", "\\\\")
    cmd_prefix = f'"{py}" "{helper}"'

    return f"""You are an automated graph extraction agent. Your job is to process session transcripts that have already been processed by Sonnet-Core (memories extracted) and extract entities, relationships, and artifacts for a knowledge graph.

You have access to a CLI tool via Bash. The command prefix is:
{cmd_prefix}

## Workflow

Loop until no more pending transcripts:

1. **Get next transcript:**
   ```
   {cmd_prefix} process-supplementary
   ```
   If output is "NO_PENDING", exit immediately (all done).

2. **Read the output** — it contains TRANSCRIPT_ID, SESSION_ID, PROJECT, and the transcript content.

3. **Extract entities** — scan the transcript for notable entities:

   Entity types: person, tool, concept, project, file, ea, symbol, platform, library, service

   For each entity found, store it:
   ```
   {cmd_prefix} store-entity --name "entity name" --entity-type TYPE --description "brief description"
   ```
   Add `--project-id N` if the entity belongs to a specific project (use the project list below).
   Add `--aliases alias1 alias2` if the entity has alternative names.

   **What to extract:**
   - Tools and libraries used (e.g., "psycopg2", "model2vec", "Plotly")
   - People mentioned (e.g., "Park et al.", user names)
   - Key concepts discussed (e.g., "Reciprocal Rank Fusion", "MMR diversity")
   - Files created or modified (e.g., "add_schema_v2b.sql")
   - EAs, symbols, platforms mentioned
   - Services and external systems (e.g., "Slack API", "Discord")

   **What NOT to extract:**
   - Generic programming concepts everyone knows (e.g., "Python", "function", "variable")
   - Temporary/throwaway items (test variables, debug prints)
   - Entities with no meaningful context in the transcript

   Aim for 5-15 entities per session. Quality over quantity.

4. **Extract relationships** — identify how entities relate to each other:

   Relationship types: uses, causes, fixes, depends_on, produces, trades, implements, contains, replaces, conflicts_with

   ```
   {cmd_prefix} store-relationship --source "entity A" --target "entity B" --relationship-type TYPE --description "how they relate" --confidence N
   ```

   Confidence scale: 5=mentioned together, 7=clearly related, 9=definitively proven in transcript.

   **Examples:**
   - memory_helper.py -uses-> pgvector
   - BM25 search -produces-> keyword matches
   - hybrid search -replaces-> pure vector search
   - error_solutions table -fixes-> tool errors

   Aim for 3-10 relationships per session. Only store relationships clearly supported by the transcript.

5. **Extract artifacts** — files created or significantly modified during the session:

   ```
   {cmd_prefix} store-artifact --file-path "FULL_PATH" --artifact-type TYPE --project "PROJECT_DIR" --title "display name" --description "what it does"
   ```

   Artifact types: plan, dashboard, script, ea, config, doc, skill, schema, hook, agent, test

   **Only store artifacts that are:**
   - Newly created files (not just read or trivially edited)
   - Significantly modified files (new features, not typo fixes)
   - Use the PROJECT directory from step 2

6. **Mark completed:**
   ```
   {cmd_prefix} mark-supplementary --transcript-id N --status completed
   ```

7. **Loop back to step 1** — call process-supplementary again to check for more.

## Error handling
- If any command fails, mark as failed:
  ```
  {cmd_prefix} mark-supplementary --transcript-id N --status failed
  ```
  Then continue to the next transcript.

## Project IDs for --project-id flag
Use these when storing entities with a clear project association:
IDs come from the `projects` table (e.g. 1=multi-agent, 2=memory-loop, 3=skill-building).
Run `SELECT id, name FROM projects ORDER BY id` to list them.

## Important rules
- Be concise in entity descriptions — one sentence max
- Don't create duplicate entities — the CLI handles upserts, but use consistent names
- Prefer specific names over generic ones (e.g., "model2vec" not "embedding model")
- Relationships must reference entities you've already stored in this session
- Store entities BEFORE relationships that reference them
- Skip trivial sessions (very short transcripts with no meaningful entities)
"""


SONNET_LOCK_FILE = LOG_DIR / "sonnet_processor.lock"
SONNET_LOCK_STALE_SECONDS = 15 * 60  # 15 minutes


def _acquire_processor_lock():
    """Acquire an atomic lock file for Sonnet processor spawning.

    Uses O_CREAT|O_EXCL for atomic creation (fails if file exists).
    Stale locks (>15 min) are removed and retried once.
    Returns the file descriptor on success, None if lock is held.
    """
    import time

    def _try_create():
        try:
            fd = os.open(str(SONNET_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    if _try_create():
        return True

    # Lock file exists — check staleness
    try:
        age = time.time() - os.path.getmtime(str(SONNET_LOCK_FILE))
        if age >= SONNET_LOCK_STALE_SECONDS:
            os.unlink(str(SONNET_LOCK_FILE))
            return _try_create()
    except OSError:
        pass

    return False


def spawn_sonnet_processor(conn):
    """Check for pending transcripts and spawn background Sonnet processor."""
    import shutil
    import subprocess

    # Check for pending transcripts
    pending = safe_query(conn, """
        SELECT COUNT(*) AS cnt FROM pending_transcripts WHERE status = 'pending'
    """)
    pending_count = pending[0]["cnt"] if pending else 0

    # Reset stale 'processing' entries (claimed > 1 hour ago) back to 'pending'
    stale = safe_query(conn, """
        UPDATE pending_transcripts
        SET status = 'pending', processed_at = NULL
        WHERE status = 'processing'
          AND processed_at < NOW() - INTERVAL '1 hour'
        RETURNING id
    """)
    if stale:
        pending_count += len(stale)
        print(f"Reset {len(stale)} stale processing transcripts to pending")

    if pending_count == 0:
        return

    # Atomic lock file: only one Sonnet processor at a time
    if not _acquire_processor_lock():
        print(f"Sonnet processor already running (lock: {SONNET_LOCK_FILE}), skipping spawn")
        return

    # Find claude CLI
    claude_path = shutil.which("claude")
    if not claude_path:
        print("WARNING: claude CLI not found in PATH, cannot spawn Sonnet processor",
              file=sys.stderr)
        return

    # Create log directory
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file_path = LOG_DIR / f"sonnet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    prompt = _build_sonnet_prompt()

    # Spawn detached background process
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Required: unset nested-session guard
    env["MEMORY_HELPER_SUBPROCESS"] = "1"  # Prevent recursive spawn cascade

    # Windows-specific: detach process and suppress console window
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    # Use file-based empty MCP config (inline JSON '{}' crashes claude -p on Windows)
    empty_mcp = str(LOG_DIR / "_empty_mcp.json")
    if not os.path.exists(empty_mcp):
        import json as _json
        with open(empty_mcp, "w") as _f:
            _json.dump({"mcpServers": {}}, _f)

    with open(log_file_path, "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [
                claude_path,
                "-p", prompt,
                "--model", "sonnet",
                "--allowedTools", "Bash",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                "--max-budget-usd", "5.00",
                "--mcp-config", empty_mcp,
                "--strict-mcp-config",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW,
            startupinfo=startupinfo,
            cwd=str(Path(HELPER_SCRIPT).parent),
        )

    print(f"Spawned Sonnet processor ({pending_count} pending) → {log_file_path}")


SUPPLEMENTARY_LOCK_FILE = LOG_DIR / "sonnet_supplementary.lock"


def spawn_sonnet_supplementary(conn):
    """Check for completed transcripts needing supplementary processing and spawn."""
    import shutil
    import subprocess

    # Reset stale 'processing' entries (claimed > 1 hour ago)
    stale = safe_query(conn, """
        UPDATE pending_transcripts
        SET supplementary_status = 'pending'
        WHERE supplementary_status = 'processing'
          AND processed_at < NOW() - INTERVAL '1 hour'
        RETURNING id
    """)
    if stale:
        print(f"Reset {len(stale)} stale supplementary-processing transcripts to pending")

    pending = safe_query(conn, """
        SELECT COUNT(*) AS cnt FROM pending_transcripts
        WHERE status = 'completed'
          AND (supplementary_status IS NULL OR supplementary_status = 'pending')
    """)
    pending_count = pending[0]["cnt"] if pending else 0
    if pending_count == 0:
        return

    # Atomic lock file (separate from Core processor)
    if not _acquire_processor_lock_for(SUPPLEMENTARY_LOCK_FILE):
        print(f"Supplementary processor already running, skipping spawn")
        return

    claude_path = shutil.which("claude")
    if not claude_path:
        print("WARNING: claude CLI not found, cannot spawn Supplementary", file=sys.stderr)
        SUPPLEMENTARY_LOCK_FILE.unlink(missing_ok=True)
        return

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file_path = LOG_DIR / f"supplementary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        prompt = _build_sonnet_supplementary_prompt()

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env["MEMORY_HELPER_SUBPROCESS"] = "1"

        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        empty_mcp = str(LOG_DIR / "_empty_mcp.json")
        if not os.path.exists(empty_mcp):
            import json as _json
            with open(empty_mcp, "w") as _f:
                _json.dump({"mcpServers": {}}, _f)

        # Write prompt to temp file (avoids cmd-line escaping issues on Windows)
        prompt_file = LOG_DIR / f"_supp_prompt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        # Use a wrapper Python script that runs claude synchronously and writes
        # output to the log file. Direct Popen(stdout=log_f) with DETACHED_PROCESS
        # on Windows produces empty log files due to stdout buffering issues.
        # Wrapper uses try/finally to ensure lock cleanup on all exit paths.
        _lock = repr(str(SUPPLEMENTARY_LOCK_FILE))
        _prompt = repr(str(prompt_file))
        _log = repr(str(log_file_path))
        wrapper_script = (
            f"import subprocess, os, pathlib\n"
            f"try:\n"
            f"  r = subprocess.run("
            f"[{repr(claude_path)}, '-p', open({_prompt}, encoding='utf-8').read(), "
            f"'--model', 'sonnet', '--allowedTools', 'Bash', "
            f"'--dangerously-skip-permissions', '--no-session-persistence', "
            f"'--max-budget-usd', '2.00', "
            f"'--mcp-config', {repr(empty_mcp)}, '--strict-mcp-config'], "
            f"capture_output=True, text=True, "
            f"cwd={repr(str(Path(HELPER_SCRIPT).parent))})\n"
            f"  open({_log}, 'w', encoding='utf-8').write(r.stdout or '')\n"
            f"  open({_log}, 'a', encoding='utf-8').write(r.stderr or '')\n"
            f"finally:\n"
            f"  pathlib.Path({_lock}).unlink(missing_ok=True)\n"
            f"  try:\n"
            f"    os.remove({_prompt})\n"
            f"  except OSError:\n"
            f"    pass\n"
        )

        subprocess.Popen(
            [PYTHON_EXE, "-c", wrapper_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW,
            startupinfo=startupinfo,
        )

        print(f"Spawned Supplementary processor ({pending_count} pending) → {log_file_path}")
    except Exception:
        SUPPLEMENTARY_LOCK_FILE.unlink(missing_ok=True)
        raise


def _acquire_processor_lock_for(lock_path, *, stale_seconds=None):
    """Generic lock acquisition for any lock file path."""
    import time
    if stale_seconds is None:
        stale_seconds = SONNET_LOCK_STALE_SECONDS
    lock_str = str(lock_path)

    def _try_create():
        try:
            fd = os.open(lock_str, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            return False

    if _try_create():
        return True

    try:
        age = time.time() - os.path.getmtime(lock_str)
        if age >= stale_seconds:
            os.unlink(lock_str)
            return _try_create()
    except OSError:
        pass

    return False


# ---------------------------------------------------------------------------
# startup - Write MEMORY.md from DB state
# ---------------------------------------------------------------------------
def cmd_startup(args):
    if _skip_memory_loop():
        return

    # Guard: Claude Code fires SessionStart hook twice (~30-350ms apart).
    # The second run overwrites SESSION_ID_FILE, orphaning the first session row.
    # Skip if SESSION_ID_FILE was written less than 2 seconds ago.
    import time
    if SESSION_ID_FILE.exists():
        try:
            age = time.time() - SESSION_ID_FILE.stat().st_mtime
            if age < 2:
                print("Startup hook already ran (duplicate fire), skipping")
                return
        except OSError:
            pass

    # Read hook input early — needed for baseline init (must work even when DB is down)
    hook_input = read_hook_stdin()
    claude_uuid = hook_input.get("session_id") if hook_input else None
    hook_transcript_path = hook_input.get("transcript_path") if hook_input else None

    # Write initial transcript baseline for compaction-aware checkpointing
    # Must be before DB gate so it works even when PostgreSQL is down
    _write_transcript_baseline(hook_transcript_path)

    try:
        conn = get_conn()
    except psycopg2.OperationalError as e:
        # DB unreachable — session still starts, turn-recall will handle context
        print(f"WARNING: DB unreachable at startup: {e}", file=sys.stderr)
        return

    # ── Conversation Chain Model ──
    # Always create a new session row. Close any orphans in the same project
    # and queue their transcripts for Sonnet. No orphan reuse — each startup
    # gets its own session row. Sessions sharing a conversation are linked
    # by claude_session_id (when available).

    # Step 1: Close orphaned sessions in same project, queue for Sonnet
    project = get_project()
    orphans = safe_query(conn, """
        SELECT session_id, transcript_path, started_at, claude_session_id
        FROM sessions
        WHERE ended_at IS NULL
          AND project = %s
          AND session_id NOT IN (SELECT session_id FROM pending_transcripts)
    """, (project,))
    for orph in (orphans or []):
        tp = orph.get("transcript_path")
        if tp and os.path.exists(tp):
            orph_boundary = find_last_compact_boundary(tp)
            orph_result = filter_transcript(tp, started_at=orph["started_at"], boundary_offset=orph_boundary)
            if orph_result and orph_result["user_message_count"] >= 3:
                safe_execute(conn, """
                    INSERT INTO pending_transcripts
                        (session_id, project, compact_summary, raw_content, had_compaction,
                         user_message_count, total_size_bytes, status, agent_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                    ON CONFLICT DO NOTHING
                """, (
                    orph["session_id"], project,
                    orph_result["compact_summary"], orph_result["raw_content"],
                    orph_result["had_compaction"], orph_result["user_message_count"],
                    orph_result["total_size_bytes"], AGENT_ID,
                ))
                print(f"Recovered orphan transcript: {orph['session_id']}")
        # Mark as ended regardless (even trivial sessions)
        safe_execute(conn, """
            UPDATE sessions SET ended_at = NOW(),
                summary = COALESCE(summary, 'Recovered: terminal closed without /exit')
            WHERE session_id = %s AND ended_at IS NULL
        """, (orph["session_id"],))

    # Read real conversation UUID from per-project statusline-written file
    conversation_id = None
    if hook_transcript_path:
        cid_file = Path(os.path.dirname(hook_transcript_path)) / ".conversation_id"
        if cid_file.exists():
            try:
                conversation_id = cid_file.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass

    # Step 2: Always create a new session row
    session_id = f"session-{uuid.uuid4().hex[:12]}"
    SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_ID_FILE.write_text(session_id, encoding="utf-8")

    # Write flag file as JSON — session row created on first user prompt (deferred INSERT)
    # Uses hook_transcript_path directly (correct per-session path from Claude Code).
    # conversation_id is stored as metadata only, NOT used to derive transcript path.
    flag_file = SESSION_ID_FILE.parent / ".session_needs_insert"
    flag_data = json.dumps({
        "session_id": session_id,
        "claude_session_id": claude_uuid,
        "transcript_path": hook_transcript_path,
        "conversation_id": conversation_id,
    })
    flag_file.write_text(flag_data, encoding="utf-8")

    # Update agent last_seen timestamp
    safe_execute(conn, """
        UPDATE agents SET last_seen = NOW() WHERE agent_id = %s
    """, (AGENT_ID,))

    # ── MEMORY.md is now managed by built-in auto-memory ──
    # Project context + session handoff are injected via first turn-recall instead.
    print(f"Startup complete: session={session_id}")

    # Trace: emit session_start and auto-start dashboard server
    emit_trace("session_start", {
        "claude_uuid": claude_uuid,
        "transcript_path": hook_transcript_path,
        "cwd": get_project(),
    }, session_id=session_id)
    ensure_server_running()

    # Recover any fallback transcripts saved when DB was down
    fallback_dir = LOG_DIR.parent / "pending_fallback"
    if fallback_dir.exists():
        for fb_file in fallback_dir.glob("*.json"):
            try:
                data = json.loads(fb_file.read_text(encoding="utf-8"))
                # Check idempotency
                existing = safe_query(conn, """
                    SELECT id FROM pending_transcripts WHERE session_id = %s LIMIT 1
                """, (data["session_id"],))
                if not existing:
                    safe_execute(conn, """
                        INSERT INTO pending_transcripts
                            (session_id, project, compact_summary, raw_content, had_compaction,
                             user_message_count, total_size_bytes, status, agent_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                    """, (
                        data["session_id"], data.get("project"),
                        data.get("compact_summary"), data["raw_content"],
                        data.get("had_compaction", False),
                        data.get("user_message_count", 0), data.get("total_size_bytes", 0),
                        data.get("agent_id", AGENT_ID),
                    ))
                    print(f"Recovered fallback transcript: {data['session_id']}")
                fb_file.unlink()
            except Exception as e:
                print(f"WARNING: failed to recover fallback {fb_file.name}: {e}",
                      file=sys.stderr)

    # Crash-recovery spawn: process stale pending transcripts from sessions
    # that ended abnormally or were recovered from fallback files.
    # Normal path: Sonnet spawns at SessionEnd via cmd_session_end().
    try:
        spawn_sonnet_processor(conn)
    except Exception as e:
        print(f"WARNING: spawn_sonnet_processor failed: {e}", file=sys.stderr)

    conn.close()


# ---------------------------------------------------------------------------
# session-end - Combined SessionEnd handler (save + store + spawn)
# ---------------------------------------------------------------------------
def cmd_session_end(args):
    """Combined SessionEnd: save session -> store transcript -> spawn Sonnet.
    Replaces the two separate parallel hooks with one sequential command.
    """
    if _skip_memory_loop():
        return

    hook_input = read_hook_stdin()

    # Debug: log hook input for diagnosing "tuple index out of range"
    _debug_log = LOG_DIR / "session_end_debug.log"
    try:
        import traceback as _tb
        _debug_log.write_text(json.dumps({
            "hook_input": hook_input,
            "timestamp": str(datetime.now()),
        }, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    session_id = get_current_session_id()
    if not session_id:
        session_id = f"session-{uuid.uuid4().hex[:12]}"

    # ── DB connection ──
    try:
        conn = get_conn()
    except psycopg2.OperationalError as e:
        print(f"WARNING: DB unreachable in session-end: {e}", file=sys.stderr)
        _store_transcript_fallback(hook_input, session_id)
        return

    # ── Step 1: Save session metadata (non-critical) ──
    # Wrapped in try/except: Sonnet overwrites this in its step 8 anyway.
    # A crash here must NOT prevent transcript storage (Step 2).
    transcript_path = hook_input.get("transcript_path") if hook_input else None
    claude_uuid = hook_input.get("session_id") if hook_input else None
    project = get_project()

    # Read real conversation UUID from per-project statusline-written file
    conversation_id = None
    if transcript_path:
        cid_file = Path(os.path.dirname(transcript_path)) / ".conversation_id"
        if cid_file.exists():
            try:
                conversation_id = cid_file.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass

    # Find compact boundary once (reverse byte-scan), share with both functions
    boundary_offset = find_last_compact_boundary(transcript_path) if transcript_path else None

    try:
        summary = None
        if transcript_path:
            summary = extract_transcript_summary(transcript_path)
        if not summary:
            summary = "Session ended (no summary provided)"

        safe_execute(conn, """
            INSERT INTO sessions (session_id, summary, outcome, ended_at, tags, project, agent_id,
                                  claude_session_id, transcript_path, conversation_id)
            VALUES (%s, %s, 'success', NOW(), '{}', %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE
            SET summary = CASE
                    WHEN sessions.summary LIKE '%%---checkpoint---%%'
                    THEN sessions.summary || E'\n---session-end---\n' || EXCLUDED.summary
                    ELSE EXCLUDED.summary
                END,
                outcome = EXCLUDED.outcome,
                ended_at = CASE WHEN sessions.ended_at IS NULL THEN NOW() ELSE sessions.ended_at END,
                project = COALESCE(sessions.project, EXCLUDED.project),
                claude_session_id = COALESCE(sessions.claude_session_id, EXCLUDED.claude_session_id),
                transcript_path = COALESCE(EXCLUDED.transcript_path, sessions.transcript_path),
                conversation_id = COALESCE(EXCLUDED.conversation_id, sessions.conversation_id)
        """, (session_id, summary, project, AGENT_ID, claude_uuid, transcript_path, conversation_id))
        print(f"Session saved: {session_id}")

        # Trace: emit session_end event
        emit_trace("session_end", {
            "summary": (summary or "")[:300],
            "outcome": "success",
        }, session_id=session_id)
    except Exception as e:
        import traceback as _tb
        print(f"WARNING: save-session failed, continuing to transcript: {e}", file=sys.stderr)
        # Write full traceback to debug log
        try:
            with open(LOG_DIR / "session_end_debug.log", "a", encoding="utf-8") as _f:
                _f.write(f"\n--- save-session traceback ---\n")
                _tb.print_exc(file=_f)
        except Exception:
            pass

    # ── Step 2: Store transcript ──
    if not transcript_path:
        print("No transcript_path in hook input, skipping transcript storage.", file=sys.stderr)
        conn.close()
        return

    # Look up started_at for time-based filtering (claude -c: skip previous session content)
    started_at = None
    try:
        sa_rows = safe_query(conn, "SELECT started_at FROM sessions WHERE session_id = %s", (session_id,))
        if sa_rows:
            started_at = sa_rows[0]["started_at"]
    except Exception:
        pass

    result = filter_transcript(transcript_path, started_at=started_at, boundary_offset=boundary_offset)
    if result is None:
        print("filter_transcript returned None, skipping.", file=sys.stderr)
        conn.close()
        return

    # Build inject_log
    inject_log = None
    try:
        inject_rows = safe_query(conn, """
            SELECT me.id as exposure_id, me.memory_id, m.content, m.category,
                   me.similarity, me.created_at
            FROM memory_exposures me
            JOIN memories m ON m.id = me.memory_id
            WHERE me.session_id = %s AND me.source = 'inject'
            ORDER BY me.created_at
        """, (session_id,))
        if inject_rows:
            inject_log = json.dumps([{
                "exposure_id": r["exposure_id"],
                "memory_id": r["memory_id"],
                "content": r["content"][:300],
                "category": r["category"],
                "similarity": float(r["similarity"]) if r["similarity"] else None,
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
            } for r in inject_rows])
    except Exception as e:
        print(f"WARNING: inject_log build failed: {e}", file=sys.stderr)

    # Idempotency check
    existing = safe_query(conn, """
        SELECT id FROM pending_transcripts WHERE session_id = %s LIMIT 1
    """, (session_id,))
    if existing:
        print(f"Transcript already stored for {session_id}, skipping.")
        conn.close()
        return

    # Trivial session check — skip only if BOTH few messages AND small size
    has_substantial_compact = (result["compact_summary"] and len(result["compact_summary"]) > 1000)
    has_substantial_raw = (result["raw_content"] and len(result["raw_content"]) > 2000)
    if not has_substantial_compact and not has_substantial_raw and (result["user_message_count"] < 3 and result["total_size_bytes"] < 500):
        safe_execute(conn, """
            INSERT INTO pending_transcripts
                (session_id, project, compact_summary, raw_content, had_compaction,
                 user_message_count, total_size_bytes, status, processed_by, agent_id, inject_log)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'skipped', 'skipped-trivial', %s, %s)
        """, (
            session_id, project,
            result["compact_summary"], result["raw_content"] or "",
            result["had_compaction"],
            result["user_message_count"], result["total_size_bytes"],
            AGENT_ID, inject_log,
        ))
        print(f"Trivial session ({result['user_message_count']} msgs, "
              f"{result['total_size_bytes']} bytes) — stored as skipped.")
        conn.close()
        return

    # Store as pending
    safe_execute(conn, """
        INSERT INTO pending_transcripts
            (session_id, project, compact_summary, raw_content, had_compaction,
             user_message_count, total_size_bytes, status, agent_id, inject_log)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
    """, (
        session_id, project,
        result["compact_summary"], result["raw_content"],
        result["had_compaction"],
        result["user_message_count"], result["total_size_bytes"],
        AGENT_ID, inject_log,
    ))
    print(f"Transcript stored for {session_id} "
          f"({result['user_message_count']} msgs, "
          f"{result['total_size_bytes']/1024:.1f} KB, "
          f"compacted={result['had_compaction']})")

    conn.close()

    # ── Step 2.5 + 3: Sync handoff + Sonnet spawn (detached background) ──
    # These are slow (LLM call + subprocess spawn) and must not block the hook.
    # Spawn a detached Python process to handle them so the hook returns fast.
    import subprocess as _sp
    _bg_script = (
        f"import sys; sys.path.insert(0, {repr(str(Path(__file__).parent))}); "
        f"import memory_helper as mh; "
        f"conn = mh.get_conn(); "
        f"_bo = mh.find_last_compact_boundary({repr(transcript_path)}); "
        f"result = mh.filter_transcript({repr(transcript_path)}, boundary_offset=_bo); "
        f"mh._generate_sync_handoff(conn, {repr(session_id)}, result) if result else None; "
        f"mh.spawn_sonnet_processor(conn); "
        f"mh.spawn_sonnet_supplementary(conn); "
        f"conn.close()"
    )
    try:
        _startupinfo = _sp.STARTUPINFO()
        _startupinfo.dwFlags |= _sp.STARTF_USESHOWWINDOW
        _startupinfo.wShowWindow = _sp.SW_HIDE
        _sp.Popen(
            [PYTHON_EXE, "-c", _bg_script],
            startupinfo=_startupinfo,
            creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NO_WINDOW,
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        print(f"Background tasks spawned for {session_id}")
    except Exception as e:
        print(f"WARNING: background spawn failed: {e}", file=sys.stderr)


def _store_transcript_fallback(hook_input, session_id):
    """Save transcript to local file when DB is unreachable."""
    transcript_path = hook_input.get("transcript_path") if hook_input else None
    if not transcript_path:
        return
    fb_boundary = find_last_compact_boundary(transcript_path)
    result = filter_transcript(transcript_path, boundary_offset=fb_boundary)
    if result is None:
        return
    fallback_dir = LOG_DIR.parent / "pending_fallback"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = fallback_dir / f"{session_id}.json"
    fallback_path.write_text(json.dumps({
        "session_id": session_id,
        "project": get_project(),
        "compact_summary": result["compact_summary"],
        "raw_content": result["raw_content"],
        "had_compaction": result["had_compaction"],
        "user_message_count": result["user_message_count"],
        "total_size_bytes": result["total_size_bytes"],
        "agent_id": AGENT_ID,
    }), encoding="utf-8")
    print(f"WARNING: DB unreachable, saved transcript fallback to {fallback_path}",
          file=sys.stderr)


def _generate_sync_handoff(conn, session_id, result):
    """Generate session handoff synchronously using quick LLM call.

    Runs at session end BEFORE Sonnet spawns, so the next session always
    finds a COMPLETED: handoff immediately on startup.
    """
    import shutil
    import subprocess

    claude_path = shutil.which("claude")
    if not claude_path:
        print("WARNING: claude CLI not found, skipping sync handoff", file=sys.stderr)
        return

    # Use compact_summary (Opus-generated) if available, else raw_content
    content = result.get("compact_summary") or result.get("raw_content") or ""
    if not content or len(content) < 100:
        return  # Too short to summarize meaningfully

    # Truncate to ~4000 chars to keep LLM call fast
    if len(content) > 4000:
        content = content[:4000] + "\n\n[TRUNCATED]"

    prompt = (
        "Summarize this development session transcript into exactly 3 fields. "
        "Be specific and concise.\n\n"
        "COMPLETED: What was accomplished (concrete deliverables, decisions, files changed). "
        "At least one specific noun.\n"
        "NEXT: The single most important next action or test to perform.\n"
        "PENDING: Other open items or unfinished work discussed in the session.\n\n"
        "Rules:\n"
        "- Each field is 1-2 sentences max\n"
        "- No filler words, no generic statements like 'worked on improvements'\n"
        "- If the session was exploratory, state what was learned\n"
        "- Output ONLY the 3 lines starting with COMPLETED:, NEXT:, PENDING: — nothing else\n\n"
        f"TRANSCRIPT:\n{content}"
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)
    env["MEMORY_HELPER_SUBPROCESS"] = "1"

    # Windows: suppress console window
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    # Ensure empty MCP config exists
    empty_mcp = str(LOG_DIR / "_empty_mcp.json")
    if not os.path.exists(empty_mcp):
        with open(empty_mcp, "w") as _f:
            json.dump({"mcpServers": {}}, _f)

    try:
        proc = subprocess.run(
            [
                claude_path, "-p", "-",
                "--output-format", "text",
                "--no-session-persistence",
                "--model", "haiku",
                "--dangerously-skip-permissions",
                "--mcp-config", empty_mcp,
                "--strict-mcp-config",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            startupinfo=startupinfo,
            env=env,
        )
        if proc.returncode != 0:
            print(f"WARNING: sync handoff LLM failed (rc={proc.returncode}): "
                  f"{proc.stderr[:200]}", file=sys.stderr)
            return

        response = proc.stdout.strip()
        if not response or "COMPLETED:" not in response:
            print("WARNING: sync handoff LLM returned unexpected format", file=sys.stderr)
            return

        # Extract from first COMPLETED: line (LLM may add preamble)
        lines = response.split("\n")
        cleaned = []
        started = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("COMPLETED:"):
                started = True
            if started and stripped:
                cleaned.append(stripped)

        if not cleaned:
            return

        summary = "\n".join(cleaned)
        safe_execute(conn, """
            UPDATE sessions SET summary = %s WHERE session_id = %s
        """, (summary, session_id))
        print(f"Sync handoff written for {session_id}")

    except subprocess.TimeoutExpired:
        print("WARNING: sync handoff LLM timed out (30s)", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: sync handoff failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# save-session - Record session end
# ---------------------------------------------------------------------------
def cmd_save_session(args):
    if _skip_memory_loop():
        return

    conn = get_conn()
    session_id = args.session_id or get_current_session_id() or f"session-{uuid.uuid4().hex[:12]}"
    hook_input = read_hook_stdin()

    # If no summary provided, try to extract one from transcript
    summary = args.summary
    if not summary:
        if hook_input and hook_input.get("transcript_path"):
            summary = extract_transcript_summary(hook_input["transcript_path"])
        if not summary:
            summary = "Session ended (no summary provided)"

    outcome = args.outcome or "success"

    # Estimate token count from transcript file size (1 token ≈ 4 chars)
    token_estimate = None
    tp = (hook_input or {}).get("transcript_path")
    if tp and os.path.exists(tp):
        token_estimate = os.path.getsize(tp) // 4

    # Extract Claude Code UUID and transcript_path from hook input
    claude_uuid = (hook_input or {}).get("session_id")
    hook_tp = (hook_input or {}).get("transcript_path")

    # Upsert session — only update fields that were explicitly provided
    project = get_project()
    safe_execute(conn, """
        INSERT INTO sessions (session_id, summary, outcome, ended_at, tags, project, agent_id,
                              token_estimate, claude_session_id, transcript_path)
        VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE
        SET summary = EXCLUDED.summary,
            outcome = EXCLUDED.outcome,
            ended_at = CASE WHEN sessions.ended_at IS NULL THEN NOW() ELSE sessions.ended_at END,
            tags = CASE WHEN %s THEN EXCLUDED.tags ELSE sessions.tags END,
            project = COALESCE(sessions.project, EXCLUDED.project),
            token_estimate = COALESCE(EXCLUDED.token_estimate, sessions.token_estimate),
            claude_session_id = COALESCE(sessions.claude_session_id, EXCLUDED.claude_session_id),
            transcript_path = COALESCE(EXCLUDED.transcript_path, sessions.transcript_path)
    """, (session_id, summary, outcome, args.tags or [], project, AGENT_ID,
          token_estimate, claude_uuid, hook_tp, bool(args.tags)))
    print(f"Session saved: {session_id} [{outcome}]")

    # If agent provided reflection data, bundle it into a reflection record
    lessons = args.lessons or []
    if lessons or args.what_worked or args.what_failed:
        # Derive task_type from tags or default
        task_type = args.tags[0] if args.tags else "session-work"
        domain = args.domain or "general"
        safe_execute(conn, """
            INSERT INTO reflections (session_id, task_type, domain, outcome,
                                     what_worked, what_failed, lessons, confidence, tags, agent_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            session_id, task_type, domain, outcome,
            args.what_worked, args.what_failed, lessons,
            args.confidence or 5, args.tags or [], AGENT_ID,
        ))
        print(f"Bundled reflection stored: [{outcome}] {task_type}")

    conn.close()


# ---------------------------------------------------------------------------
# save-context - Save current context before /compact
# ---------------------------------------------------------------------------
def cmd_save_context(args):
    conn = get_conn()
    session_id = get_current_session_id()

    # If no content provided, try to extract from transcript
    content = args.content
    if not content:
        hook_input = read_hook_stdin()
        if hook_input and hook_input.get("transcript_path"):
            content = extract_transcript_summary(hook_input["transcript_path"])
        if not content:
            content = "Context saved before compact (no details provided)"

    vec = None
    try:
        vec = embed_text(content)
    except Exception as e:
        print(f"WARNING: embedding failed for context: {e}", file=sys.stderr)

    project = get_project()
    if vec is not None:
        safe_execute(conn, """
            INSERT INTO memories (content, category, domain, importance, tags, ttl_days, source_session, project, embedding, agent_id)
            VALUES (%s, 'context', %s, %s, %s, 7, %s, %s, %s, %s)
        """, (content, args.domain or "general", args.importance or 7, args.tags or [], session_id, project, vec.tolist(), AGENT_ID))
    else:
        safe_execute(conn, """
            INSERT INTO memories (content, category, domain, importance, tags, ttl_days, source_session, project, agent_id)
            VALUES (%s, 'context', %s, %s, %s, 7, %s, %s, %s)
        """, (content, args.domain or "general", args.importance or 7, args.tags or [], session_id, project, AGENT_ID))
    print("Context saved as temporary memory (7-day TTL)")
    conn.close()


# ---------------------------------------------------------------------------
# store-transcript - Store filtered transcript for deferred processing
# ---------------------------------------------------------------------------
def cmd_store_transcript(args):
    if _skip_memory_loop():
        return

    hook_input = read_hook_stdin()
    transcript_path = None
    if hook_input:
        transcript_path = hook_input.get("transcript_path")
    if not transcript_path:
        print("No transcript_path in hook input, skipping.", file=sys.stderr)
        return

    session_id = get_current_session_id()
    if not session_id:
        print("No active session ID, skipping store-transcript.", file=sys.stderr)
        return

    # Look up started_at for time-based filtering (claude -c: skip previous session content)
    started_at = None
    try:
        _conn = get_conn()
        try:
            sa_rows = safe_query(_conn, "SELECT started_at FROM sessions WHERE session_id = %s", (session_id,))
            if sa_rows:
                started_at = sa_rows[0]["started_at"]
        finally:
            _conn.close()
    except Exception:
        pass  # DB down — filter full transcript (better to over-process than lose data)

    # Filter the transcript before DB connection (can succeed even if DB is down)
    st_boundary = find_last_compact_boundary(transcript_path)
    result = filter_transcript(transcript_path, started_at=started_at, boundary_offset=st_boundary)
    if result is None:
        print("filter_transcript returned None, skipping.", file=sys.stderr)
        return

    try:
        conn = get_conn()
    except psycopg2.OperationalError as e:
        # DB unreachable — save filtered transcript to local file as fallback
        fallback_dir = LOG_DIR.parent / "pending_fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = fallback_dir / f"{session_id}.json"
        fallback_path.write_text(json.dumps({
            "session_id": session_id,
            "project": get_project(),
            "compact_summary": result["compact_summary"],
            "raw_content": result["raw_content"],
            "had_compaction": result["had_compaction"],
            "user_message_count": result["user_message_count"],
            "total_size_bytes": result["total_size_bytes"],
            "agent_id": AGENT_ID,
        }), encoding="utf-8")
        print(f"WARNING: DB unreachable, saved transcript fallback to {fallback_path}: {e}",
              file=sys.stderr)
        return

    # Build inject_log: query memory_exposures for this session's inject events
    inject_log = None
    try:
        inject_rows = safe_query(conn, """
            SELECT me.id as exposure_id, me.memory_id, m.content, m.category,
                   me.similarity, me.created_at
            FROM memory_exposures me
            JOIN memories m ON m.id = me.memory_id
            WHERE me.session_id = %s AND me.source = 'inject'
            ORDER BY me.created_at
        """, (session_id,))
        if inject_rows:
            inject_log = json.dumps([{
                "exposure_id": r["exposure_id"],
                "memory_id": r["memory_id"],
                "content": r["content"][:300],
                "category": r["category"],
                "similarity": float(r["similarity"]) if r["similarity"] else None,
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
            } for r in inject_rows])
    except Exception as e:
        print(f"WARNING: inject_log build failed: {e}", file=sys.stderr)

    # Idempotency: skip if session already stored
    existing = safe_query(conn, """
        SELECT id FROM pending_transcripts WHERE session_id = %s LIMIT 1
    """, (session_id,))
    if existing:
        print(f"Transcript already stored for {session_id}, skipping.")
        conn.close()
        return

    # Trivial session check — skip only if BOTH few messages AND small size
    has_substantial_compact = (result["compact_summary"] and len(result["compact_summary"]) > 1000)
    has_substantial_raw = (result["raw_content"] and len(result["raw_content"]) > 2000)
    if not has_substantial_compact and not has_substantial_raw and (result["user_message_count"] < 3 and result["total_size_bytes"] < 500):
        safe_execute(conn, """
            INSERT INTO pending_transcripts
                (session_id, project, compact_summary, raw_content, had_compaction,
                 user_message_count, total_size_bytes, status, processed_by, agent_id, inject_log)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'skipped', 'skipped-trivial', %s, %s)
        """, (
            session_id, get_project(),
            result["compact_summary"], result["raw_content"] or "",
            result["had_compaction"],
            result["user_message_count"], result["total_size_bytes"],
            AGENT_ID, inject_log,
        ))
        print(f"Trivial session ({result['user_message_count']} msgs, "
              f"{result['total_size_bytes']} bytes) — stored as skipped.")
        conn.close()
        return

    # Store as pending
    safe_execute(conn, """
        INSERT INTO pending_transcripts
            (session_id, project, compact_summary, raw_content, had_compaction,
             user_message_count, total_size_bytes, status, agent_id, inject_log)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
    """, (
        session_id, get_project(),
        result["compact_summary"], result["raw_content"],
        result["had_compaction"],
        result["user_message_count"], result["total_size_bytes"],
        AGENT_ID, inject_log,
    ))
    print(f"Transcript stored for {session_id} "
          f"({result['user_message_count']} msgs, "
          f"{result['total_size_bytes']/1024:.1f} KB, "
          f"compacted={result['had_compaction']})")
    conn.close()


# ---------------------------------------------------------------------------
# process-pending - Claim and output the oldest pending transcript
# ---------------------------------------------------------------------------
def cmd_process_pending(args):
    conn = get_conn()

    # Atomically claim the oldest pending transcript (set processed_at for stale detection)
    rows = safe_query(conn, """
        UPDATE pending_transcripts
        SET status = 'processing', processed_at = NOW()
        WHERE id = (
            SELECT id FROM pending_transcripts
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, session_id, project, compact_summary, raw_content, had_compaction, inject_log
    """)

    if not rows:
        print("NO_PENDING")
        conn.close()
        return

    row = rows[0]
    # Output structured content with delimiters for Sonnet to parse
    print(f"TRANSCRIPT_ID={row['id']}")
    print(f"SESSION_ID={row['session_id']}")
    print(f"PROJECT={row['project'] or ''}")
    print(f"HAD_COMPACTION={row['had_compaction']}")
    print("---COMPACT_SUMMARY_START---")
    print(row["compact_summary"] or "(no compaction)")
    print("---COMPACT_SUMMARY_END---")
    print("---RAW_CONTENT_START---")
    print(row["raw_content"])
    print("---RAW_CONTENT_END---")
    if row.get("inject_log"):
        inject_data = row["inject_log"]
        if isinstance(inject_data, str):
            inject_data = json.loads(inject_data)
        print("---INJECT_LOG_START---")
        for entry in inject_data:
            eid = entry.get("exposure_id", "?")
            mid = entry.get("memory_id", "?")
            cat = entry.get("category", "?")
            sim = entry.get("similarity")
            sim_str = f"sim:{sim:.2f}" if sim else "sim:?"
            content = entry.get("content", "")[:200]
            print(f"[exposure_id={eid}] [memory_id={mid}] [{cat}] ({sim_str}) {content}")
        print("---INJECT_LOG_END---")

    conn.close()


# ---------------------------------------------------------------------------
# mark-processed - Update transcript processing status
# ---------------------------------------------------------------------------
def cmd_mark_processed(args):
    conn = get_conn()

    safe_execute(conn, """
        UPDATE pending_transcripts
        SET status = %s,
            processed_at = NOW(),
            processed_by = %s,
            memories_created = %s,
            error_message = %s
        WHERE id = %s
    """, (
        args.status,
        args.processed_by or "sonnet",
        args.memories_created or 0,
        args.error_message,
        args.transcript_id,
    ))
    print(f"Transcript {args.transcript_id} marked as {args.status}")

    # Write session handoff if provided (merged step 8+9)
    if args.status == "completed" and args.handoff_completed:
        session_id = args.session_id
        if not session_id:
            # Look up session_id from the transcript
            rows = safe_query(conn, """
                SELECT session_id FROM pending_transcripts WHERE id = %s
            """, (args.transcript_id,))
            session_id = rows[0]["session_id"] if rows else None

        if session_id:
            # Skip if sync handoff already wrote a COMPLETED: summary
            existing = safe_query(conn, """
                SELECT summary FROM sessions WHERE session_id = %s
            """, (session_id,))
            if (existing and existing[0]["summary"]
                    and existing[0]["summary"].startswith("COMPLETED:")):
                print(f"Sync handoff already exists for {session_id}, skipping Sonnet overwrite")
            else:
                summary = f"COMPLETED: {args.handoff_completed}"
                if args.handoff_next:
                    summary += f"\nNEXT: {args.handoff_next}"
                if args.handoff_pending:
                    summary += f"\nPENDING: {args.handoff_pending}"
                if args.handoff_context:
                    summary += f"\nCONTEXT: {args.handoff_context}"
                safe_execute(conn, """
                    UPDATE sessions SET summary = %s
                    WHERE session_id = %s
                """, (summary, session_id))
                print(f"Session handoff written for {session_id}")

    # Trigger Supplementary processor when Core completes a transcript
    if args.status == "completed":
        # Mark as ready for supplementary processing
        safe_execute(conn, """
            UPDATE pending_transcripts SET supplementary_status = 'pending'
            WHERE id = %s
        """, (args.transcript_id,))
        # Spawn supplementary via detached Python process (NOT directly from
        # within Core's claude -p, which causes empty logs on Windows due to
        # nested detached process stdout handle inheritance issues).
        try:
            import subprocess as _sp
            _bg = (
                f"import sys; sys.path.insert(0, {repr(str(Path(__file__).parent))}); "
                f"import memory_helper as mh; "
                f"conn = mh.get_conn(); "
                f"mh.spawn_sonnet_supplementary(conn); "
                f"conn.close()"
            )
            _si = _sp.STARTUPINFO()
            _si.dwFlags |= _sp.STARTF_USESHOWWINDOW
            _si.wShowWindow = _sp.SW_HIDE
            _sp.Popen(
                [PYTHON_EXE, "-c", _bg],
                startupinfo=_si,
                creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NO_WINDOW,
                stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            print("Supplementary spawn delegated to detached Python process")
        except Exception as e:
            print(f"WARNING: supplementary spawn delegation failed: {e}", file=sys.stderr)

    conn.close()


# ---------------------------------------------------------------------------
# store-memory - Store a new memory
# ---------------------------------------------------------------------------
def cmd_store_memory(args):
    conn = get_conn()
    session_id = args.session_id or get_current_session_id()
    project = args.project or get_project()

    # Generate embedding
    vec = None
    try:
        vec = embed_text(args.content)
    except Exception as e:
        print(f"WARNING: embedding failed, storing without vector: {e}", file=sys.stderr)

    retrieval_mode = getattr(args, 'retrieval_mode', None) or "semantic"
    tool_name = getattr(args, 'tool_name', None)

    # --- Write-time dedup gate ---
    if vec is not None:
        neighbors = safe_query(conn, """
            SELECT id, content, category, domain, importance, tags,
                   error_signature, retrieval_mode, tool_name,
                   1 - (embedding <=> %s::vector) as similarity
            FROM memories
            WHERE superseded_by IS NULL AND embedding IS NOT NULL
              AND (ttl_days IS NULL OR created_at + (ttl_days || ' days')::INTERVAL > NOW())
            ORDER BY embedding <=> %s::vector
            LIMIT 3
        """, (vec.tolist(), vec.tolist()))

        if neighbors:
            top = neighbors[0]
            top_sim = top["similarity"]

            if top_sim > 0.97:
                # Near-exact duplicate — auto-update existing memory
                new_imp = max(top["importance"], args.importance or 5)
                new_content = args.content if len(args.content) > len(top["content"]) else top["content"]
                new_vec = vec.tolist() if len(args.content) > len(top["content"]) else None
                updates = ["importance = %s", "updated_at = NOW()"]
                params = [new_imp]
                if len(args.content) > len(top["content"]):
                    updates.extend(["content = %s", "embedding = %s"])
                    params.extend([args.content, vec.tolist()])
                # Preserve incoming metadata if provided and different
                if args.category and args.category != top["category"]:
                    updates.append("category = %s")
                    params.append(args.category)
                if args.domain and args.domain != "general" and args.domain != top["domain"]:
                    updates.append("domain = %s")
                    params.append(args.domain)
                if getattr(args, 'error_signature', None) and not top["error_signature"]:
                    updates.append("error_signature = %s")
                    params.append(args.error_signature)
                params.append(top["id"])
                safe_execute(conn, f"UPDATE memories SET {', '.join(updates)} WHERE id = %s", params)
                print(f"DEDUP: Updated #{top['id']} (sim={top_sim:.3f}) instead of inserting duplicate: "
                      f"{top['content'][:60]}...")
                conn.close()
                return

            if top_sim > 0.85:
                print(f"DEDUP WARNING: Similar memory exists (#{top['id']}, sim={top_sim:.3f}): "
                      f"{top['content'][:80]}...", file=sys.stderr)
                # Proceed with insert but log the warning

    if vec is not None:
        safe_execute(conn, """
            INSERT INTO memories (content, category, domain, importance, tags,
                                  error_signature, source_session, ttl_days, project, embedding,
                                  retrieval_mode, tool_name, agent_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            args.content,
            args.category or "fact",
            args.domain or "general",
            args.importance or 5,
            args.tags or [],
            args.error_signature,
            session_id,
            args.ttl_days,
            project,
            vec.tolist(),
            retrieval_mode,
            tool_name,
            AGENT_ID,
        ))
    else:
        safe_execute(conn, """
            INSERT INTO memories (content, category, domain, importance, tags,
                                  error_signature, source_session, ttl_days, project,
                                  retrieval_mode, tool_name, agent_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            args.content,
            args.category or "fact",
            args.domain or "general",
            args.importance or 5,
            args.tags or [],
            args.error_signature,
            session_id,
            args.ttl_days,
            project,
            retrieval_mode,
            tool_name,
            AGENT_ID,
        ))
    print(f"Memory stored: [{args.category or 'fact'}] {args.content[:60]}...")
    conn.close()


# ---------------------------------------------------------------------------
# _recall_supplementary - Search error_solutions, entities, projects, artifacts
# ---------------------------------------------------------------------------
def _recall_supplementary(conn, query, limit=3, as_json=False):
    """Search supplementary tables alongside memories."""
    if not query:
        return
    sections = {}
    q_like = f"%{query}%"

    # Error solutions
    try:
        rows = safe_query(conn, """
            SELECT id, tool_name, error_pattern, solution, verified, hit_count
            FROM error_solutions
            WHERE error_pattern ILIKE %s OR solution ILIKE %s OR tool_name ILIKE %s
            ORDER BY hit_count DESC, verified DESC
            LIMIT %s
        """, (q_like, q_like, q_like, limit))
        if rows:
            sections['error_solutions'] = rows
    except Exception:
        pass

    # Entities
    try:
        rows = safe_query(conn, """
            SELECT id, name, entity_type, description, mention_count
            FROM entities
            WHERE name ILIKE %s OR description ILIKE %s
               OR array_to_string(COALESCE(aliases, '{}'), ' ') ILIKE %s
            ORDER BY mention_count DESC
            LIMIT %s
        """, (q_like, q_like, q_like, limit))
        if rows:
            sections['entities'] = rows
    except Exception:
        pass

    # Projects
    try:
        rows = safe_query(conn, """
            SELECT id, name, display_name, description, status, tech_stack
            FROM projects
            WHERE name ILIKE %s OR display_name ILIKE %s OR description ILIKE %s
            ORDER BY last_active DESC NULLS LAST
            LIMIT %s
        """, (q_like, q_like, q_like, limit))
        if rows:
            sections['projects'] = rows
    except Exception:
        pass

    # Project artifacts
    try:
        rows = safe_query(conn, """
            SELECT id, file_path, artifact_type, title, status
            FROM project_artifacts
            WHERE (title ILIKE %s OR file_path ILIKE %s OR description ILIKE %s)
              AND status = 'current'
            ORDER BY updated_at DESC
            LIMIT %s
        """, (q_like, q_like, q_like, limit))
        if rows:
            sections['artifacts'] = rows
    except Exception:
        pass

    if not sections:
        return sections if as_json else None

    if as_json:
        return sections
    else:
        print("--- RELATED ---")
        for section, rows in sections.items():
            label = section.upper().replace('_', ' ')
            print(f"\n  {label}:")
            for r in rows:
                if section == 'error_solutions':
                    v = " [verified]" if r.get("verified") else ""
                    tool = r.get('tool_name') or '?'
                    print(f"    [err:{r['id']}] [{tool}]{v} {r['error_pattern'][:80]}")
                    print(f"           → {r['solution'][:100]}")
                elif section == 'entities':
                    desc = f" — {r['description'][:80]}" if r.get('description') else ""
                    print(f"    [ent:{r['id']}] [{r['entity_type']}] {r['name']} ({r.get('mention_count', 0)} mentions){desc}")
                elif section == 'projects':
                    stack = ", ".join(r["tech_stack"]) if isinstance(r.get("tech_stack"), list) else ""
                    name = r.get('display_name') or r['name']
                    print(f"    [proj:{r['id']}] {name} [{r.get('status', '?')}] {stack}")
                    if r.get('description'):
                        print(f"           {r['description'][:100]}")
                elif section == 'artifacts':
                    title = r.get('title') or os.path.basename(r['file_path'])
                    print(f"    [art:{r['id']}] [{r['artifact_type']}] {title}")
                    print(f"           {r['file_path']}")
        print()


# ---------------------------------------------------------------------------
# recall - Search memories
# ---------------------------------------------------------------------------
def cmd_recall(args):
    conn = get_conn()

    # Semantic search mode: embed the query, use vector similarity
    if args.semantic and args.query:
        try:
            query_vec = embed_text(args.query)
        except Exception as e:
            print(f"ERROR: embedding failed: {e}", file=sys.stderr)
            conn.close()
            sys.exit(1)

        # Build optional filters
        filters = [
            "superseded_by IS NULL",
            "(ttl_days IS NULL OR created_at + (ttl_days || ' days')::INTERVAL > NOW())",
            "embedding IS NOT NULL",
        ]
        params = []
        if args.category:
            filters.append("category = %s")
            params.append(args.category)
        if args.domain:
            filters.append("domain = %s")
            params.append(args.domain)

        where = " AND ".join(filters)
        limit = args.limit or 10

        rows = safe_query(conn, f"""
            SELECT id, content, category, domain, importance, error_signature,
                   recalled_count, tags, created_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memories
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, [query_vec.tolist()] + params + [query_vec.tolist(), limit])

        if not rows:
            supp = _recall_supplementary(conn, args.query, as_json=args.json)
            if args.json:
                print(json.dumps({"memories": [], "supplementary": supp or {}}, indent=2, default=str))
            else:
                print("No memories found (semantic).")
            conn.close()
            return

        # Update recalled_count
        ids = [r["id"] for r in rows]
        safe_execute(conn, """
            UPDATE memories SET recalled_count = recalled_count + 1, last_recalled = NOW()
            WHERE id = ANY(%s)
        """, (ids,))

        # Log recall exposures
        try:
            sid = get_current_session_id()
            if sid:
                for r in rows:
                    log_exposure(conn, sid, r["id"], "recall",
                                 similarity=float(r.get("similarity", 0)) if r.get("similarity") else None)
        except Exception:
            pass  # Non-critical

        if args.json:
            supp = _recall_supplementary(conn, args.query, as_json=True)
            for r in rows:
                r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
                r["similarity"] = float(r["similarity"]) if r["similarity"] is not None else None
            print(json.dumps({"memories": rows, "supplementary": supp or {}}, indent=2, default=str))
        else:
            for r in rows:
                sim = f"{float(r['similarity']):.3f}" if r["similarity"] is not None else "?"
                print(f"[{r['id']}] (sim:{sim}) [{r['domain']}/{r['category']}|imp:{r['importance']}] "
                      f"{r['content'][:120]}")
                if r["error_signature"]:
                    print(f"     error_sig: {r['error_signature']}")
                print()
            _recall_supplementary(conn, args.query)

        conn.close()
        return

    # Keyword search mode (existing behavior)
    conditions = [
        "superseded_by IS NULL",
        "(ttl_days IS NULL OR created_at + (ttl_days || ' days')::INTERVAL > NOW())"
    ]
    params = []

    if args.query:
        conditions.append("search_text @@ plainto_tsquery('english', %s)")
        params.append(args.query)

    if args.category:
        conditions.append("category = %s")
        params.append(args.category)

    if args.domain:
        conditions.append("domain = %s")
        params.append(args.domain)

    if args.error_signature:
        conditions.append("error_signature ILIKE %s")
        params.append(f"%{args.error_signature}%")

    where = " AND ".join(conditions)
    limit = args.limit or 10

    rows = safe_query(conn, f"""
        SELECT id, content, category, domain, importance, error_signature,
               recalled_count, tags, created_at
        FROM memories
        WHERE {where}
        ORDER BY importance DESC, recalled_count DESC, updated_at DESC
        LIMIT %s
    """, params + [limit])

    if not rows:
        supp = _recall_supplementary(conn, args.query, as_json=args.json)
        if args.json:
            print(json.dumps({"memories": [], "supplementary": supp or {}}, indent=2, default=str))
        else:
            print("No memories found.")
        conn.close()
        return

    # Update recalled_count for returned memories
    ids = [r["id"] for r in rows]
    safe_execute(conn, """
        UPDATE memories SET recalled_count = recalled_count + 1, last_recalled = NOW()
        WHERE id = ANY(%s)
    """, (ids,))

    # Log recall exposures
    try:
        sid = get_current_session_id()
        if sid:
            for r in rows:
                log_exposure(conn, sid, r["id"], "recall")
    except Exception:
        pass  # Non-critical

    # Output as JSON for machine consumption, or formatted text
    if args.json:
        supp = _recall_supplementary(conn, args.query, as_json=True)
        for r in rows:
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        print(json.dumps({"memories": rows, "supplementary": supp or {}}, indent=2, default=str))
    else:
        for r in rows:
            print(f"[{r['id']}] [{r['domain']}/{r['category']}|imp:{r['importance']}] "
                  f"{r['content'][:120]}")
            if r["error_signature"]:
                print(f"     error_sig: {r['error_signature']}")
            print()
        _recall_supplementary(conn, args.query)

    conn.close()


# ---------------------------------------------------------------------------
# store-reflection - Store a post-task reflection
# ---------------------------------------------------------------------------
def cmd_store_reflection(args):
    conn = get_conn()
    session_id = args.session_id or get_current_session_id()
    project = args.project or get_project()
    safe_execute(conn, """
        INSERT INTO reflections (session_id, task_type, domain, outcome,
                                 what_worked, what_failed, lessons, confidence, tags, project, agent_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        session_id,
        args.task_type,
        args.domain or "general",
        args.outcome,
        args.what_worked,
        args.what_failed,
        args.lessons or [],
        args.confidence or 5,
        args.tags or [],
        project,
        AGENT_ID,
    ))
    print(f"Reflection stored: [{args.outcome}] {args.task_type}")
    conn.close()


# ---------------------------------------------------------------------------
# store-backtest - Store backtest results
# ---------------------------------------------------------------------------
def cmd_store_backtest(args):
    conn = get_conn()

    # Accept either a JSON file or individual args
    if args.json_file:
        with open(args.json_file, "r") as f:
            data = json.load(f)
    else:
        data = {
            "ea_name": args.ea_name,
            "ea_version": args.ea_version,
            "symbol": args.symbol,
            "timeframe": args.timeframe,
            "source": args.source or "mt4",
            "total_trades": args.total_trades,
            "profit_factor": args.profit_factor,
            "net_profit": args.net_profit,
            "max_drawdown_pct": args.max_drawdown_pct,
            "win_rate": args.win_rate,
            "parameters": json.loads(args.parameters) if args.parameters else None,
            "notes": args.notes,
        }

    # Validate required fields
    for field in ("ea_name", "symbol", "timeframe"):
        if not data.get(field):
            print(f"Error: '{field}' is required", file=sys.stderr)
            conn.close()
            sys.exit(1)

    # Merge session_id and tags from JSON if provided, CLI overrides
    session_id = args.session_id or data.get("session_id") or get_current_session_id()
    tags = args.tags or data.get("tags", [])

    safe_execute(conn, """
        INSERT INTO backtest_results
            (session_id, ea_name, ea_version, symbol, timeframe, date_from, date_to,
             source, total_trades, profit_factor, net_profit, max_drawdown_pct,
             win_rate, sharpe_ratio, avg_trade, max_consec_losses,
             parameters, extra_metrics, notes, tags, agent_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
    """, (
        session_id,
        data.get("ea_name"),
        data.get("ea_version"),
        data.get("symbol"),
        data.get("timeframe"),
        data.get("date_from"),
        data.get("date_to"),
        data.get("source", "mt4"),
        data.get("total_trades"),
        data.get("profit_factor"),
        data.get("net_profit"),
        data.get("max_drawdown_pct"),
        data.get("win_rate"),
        data.get("sharpe_ratio"),
        data.get("avg_trade"),
        data.get("max_consec_losses"),
        json.dumps(data.get("parameters")) if data.get("parameters") else None,
        json.dumps(data.get("extra_metrics")) if data.get("extra_metrics") else None,
        data.get("notes"),
        tags,
        AGENT_ID,
    ))
    print(f"Backtest stored: {data.get('ea_name')} {data.get('symbol')} {data.get('timeframe')}")
    conn.close()


# ---------------------------------------------------------------------------
# get-strategy-history - Show evolution of a strategy
# ---------------------------------------------------------------------------
def cmd_get_strategy_history(args):
    conn = get_conn()

    # Get strategy info
    strategies = safe_query(conn, """
        SELECT id, name, version, status, description, default_params
        FROM strategies
        WHERE name ILIKE %s
        ORDER BY version DESC
    """, (f"%{args.name}%",))

    if not strategies:
        print(f"No strategy found matching: {args.name}")
        conn.close()
        return

    for s in strategies:
        print(f"Strategy: {s['name']} v{s['version']} [{s['status']}]")
        if s["description"]:
            print(f"  {s['description'][:100]}")

        # Get evolution
        evos = safe_query(conn, """
            SELECT change_type, old_values, new_values, rationale, created_at
            FROM strategy_evolution
            WHERE strategy_id = %s
            ORDER BY created_at DESC LIMIT 20
        """, (s["id"],))

        if evos:
            print("  Evolution:")
            for e in evos:
                ts = e["created_at"].strftime("%m/%d %H:%M") if e["created_at"] else "?"
                print(f"    {ts} [{e['change_type']}] {(e['rationale'] or '')[:80]}")
        print()

    # Get related backtests
    bt = safe_query(conn, """
        SELECT ea_name, symbol, timeframe, profit_factor, max_drawdown_pct,
               total_trades, net_profit, created_at
        FROM backtest_results
        WHERE ea_name ILIKE %s
        ORDER BY created_at DESC LIMIT 10
    """, (f"%{args.name}%",))

    if bt:
        print("Recent Backtests:")
        for b in bt:
            ts = b["created_at"].strftime("%m/%d") if b["created_at"] else "?"
            print(f"  {ts} {b['symbol']} {b['timeframe']} | "
                  f"PF:{b['profit_factor']} DD:{b['max_drawdown_pct']}% "
                  f"Trades:{b['total_trades']} Net:{b['net_profit']}")

    conn.close()


# ---------------------------------------------------------------------------
# promote-lessons - Check reflections for recurring patterns → lessons_learned
# Uses semantic similarity (model2vec) to cluster lessons by meaning,
# not exact string match. Threshold: 2+ semantically similar lessons.
# ---------------------------------------------------------------------------
def cmd_promote_lessons(args):
    import numpy as np
    conn = get_conn()
    min_occ = args.min_occurrences or 2
    sim_threshold = args.similarity or 0.50

    # 1. Pull all lesson texts from reflections
    rows = safe_query(conn, """
        SELECT unnest(lessons) AS lesson_text, domain, task_type, confidence, project
        FROM reflections
        WHERE lessons IS NOT NULL AND array_length(lessons, 1) > 0
    """)

    if not rows:
        print("No reflection lessons found.")
        conn.close()
        return

    # 2. Embed all lesson texts
    lessons = []
    for r in rows:
        text = (r["lesson_text"] or "").strip()
        if len(text) < 10:
            continue
        lessons.append({
            "text": text,
            "domain": r["domain"],
            "task_type": r["task_type"],
            "confidence": r["confidence"] or 5,
            "project": r.get("project"),
        })

    if not lessons:
        print("No valid lesson texts found.")
        conn.close()
        return

    print(f"Embedding {len(lessons)} lesson texts...")
    embeddings = np.array([embed_text(l["text"]) for l in lessons])

    # 3. Compute cosine similarity matrix
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1  # avoid division by zero
    normed = embeddings / norms
    sim_matrix = normed @ normed.T

    # 4. Greedy clustering: group lessons with similarity >= threshold
    used = set()
    clusters = []
    for i in range(len(lessons)):
        if i in used:
            continue
        cluster = [i]
        used.add(i)
        for j in range(i + 1, len(lessons)):
            if j in used:
                continue
            if sim_matrix[i, j] >= sim_threshold:
                cluster.append(j)
                used.add(j)
        clusters.append(cluster)

    # 5. Filter clusters meeting min_occurrences threshold
    promotable = [c for c in clusters if len(c) >= min_occ]

    if not promotable:
        print(f"No lesson clusters with {min_occ}+ similar entries (similarity >= {sim_threshold}).")
        conn.close()
        return

    # 6. Load existing lessons_learned embeddings for dedup check
    existing = safe_query(conn, "SELECT id, principle, domain FROM lessons_learned")
    existing_embeddings = []
    if existing:
        existing_embeddings = [(r, embed_text(r["principle"])) for r in existing]

    def is_duplicate(text_embedding, domain):
        """Check if a lesson is semantically similar to an existing lessons_learned entry."""
        for r, emb in existing_embeddings:
            cos = float(np.dot(text_embedding, emb) / (np.linalg.norm(text_embedding) * np.linalg.norm(emb) + 1e-9))
            if cos >= sim_threshold and r["domain"] == domain:
                return True
        return False

    promoted = 0
    for cluster_indices in promotable:
        members = [lessons[i] for i in cluster_indices]
        # Pick the longest text as representative (usually most complete)
        representative = max(members, key=lambda m: len(m["text"]))
        text = representative["text"]
        domain = representative["domain"]
        task_types = list(set(m["task_type"] for m in members if m["task_type"]))
        avg_conf = sum(m["confidence"] for m in members) / len(members)
        conf = min(10, int(avg_conf) + (len(members) - 1))
        title = text[:60]

        rep_embedding = embeddings[cluster_indices[0]]
        if is_duplicate(rep_embedding, domain):
            print(f"  SKIP (already exists): [{domain}] {title}")
            continue

        if args.dry_run:
            similar_texts = [lessons[i]["text"][:80] for i in cluster_indices]
            print(f"  WOULD PROMOTE: [{domain}] ({len(members)}x, conf:{conf}) {title}")
            for st in similar_texts:
                print(f"    - {st}")
            continue

        # Use project from the representative reflection's source
        rep_project = representative.get("project")
        safe_execute(conn, """
            INSERT INTO lessons_learned (title, principle, domain, confidence, evidence, tags, project)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            title,
            text,
            domain,
            conf,
            [f"Seen in {len(members)} similar reflections across {task_types}"],
            [],
            rep_project,
        ))
        promoted += 1
        print(f"  PROMOTED: [{domain}] ({len(members)}x, conf:{conf}) {title}")

    if args.dry_run:
        print(f"\nDry run: {len(promotable)} clusters would be promoted")
    else:
        print(f"\nPromoted {promoted} new lessons")

    conn.close()


# ---------------------------------------------------------------------------
# auto-reflect - Generate fallback reflection from DB data if agent didn't
# ---------------------------------------------------------------------------
def cmd_auto_reflect(args):
    conn = get_conn()
    session_id = get_current_session_id()
    if not session_id:
        print("No active session ID, skipping auto-reflect.")
        conn.close()
        return

    # Check if agent already stored a reflection for this session
    existing = safe_query(conn, """
        SELECT id FROM reflections WHERE session_id = %s LIMIT 1
    """, (session_id,))
    if existing:
        print(f"Reflection already exists for {session_id}, skipping auto-reflect.")
        conn.close()
        return

    # Gather session data: what happened?
    session = safe_query(conn, """
        SELECT summary, outcome FROM sessions WHERE session_id = %s
    """, (session_id,))
    summary = session[0]["summary"] if session else None
    outcome = session[0]["outcome"] if session else None

    # Memories stored this session
    mem_stats = safe_query(conn, """
        SELECT category, domain, COUNT(*) as cnt
        FROM memories
        WHERE source_session = %s
        GROUP BY category, domain
        ORDER BY cnt DESC
    """, (session_id,))

    # Backtests stored this session
    bt_stats = safe_query(conn, """
        SELECT ea_name, symbol, timeframe, profit_factor
        FROM backtest_results
        WHERE session_id = %s
    """, (session_id,))

    # If nothing happened this session, skip
    if not mem_stats and not bt_stats and (not summary or summary == "Session ended (no summary provided)"):
        print(f"No meaningful activity in {session_id}, skipping auto-reflect.")
        conn.close()
        return

    # Build reflection from data
    what_worked_parts = []
    domains = set()
    task_types = set()

    if mem_stats:
        total_mems = sum(m["cnt"] for m in mem_stats)
        categories = [f"{m['cnt']} {m['category']}" for m in mem_stats]
        what_worked_parts.append(f"Stored {total_mems} memories ({', '.join(categories)})")
        for m in mem_stats:
            domains.add(m["domain"])
            task_types.add(m["category"])

    if bt_stats:
        bt_list = [f"{b['ea_name']} {b['symbol']} {b['timeframe']}" for b in bt_stats]
        what_worked_parts.append(f"Ran {len(bt_stats)} backtests: {'; '.join(bt_list)}")
        domains.add("backtesting")
        task_types.add("backtesting")

    if summary and summary != "Session ended (no summary provided)":
        what_worked_parts.append(f"Summary: {summary[:120]}")

    what_worked = ". ".join(what_worked_parts)
    domain = domains.pop() if len(domains) == 1 else "general"
    task_type = task_types.pop() if len(task_types) == 1 else "session-work"

    safe_execute(conn, """
        INSERT INTO reflections (session_id, task_type, domain, outcome,
                                 what_worked, what_failed, lessons, confidence, tags, agent_id)
        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
    """, (
        session_id,
        task_type,
        domain,
        outcome or "success",
        what_worked,
        [],  # no lessons - auto-reflect can't know what was learned
        3,   # low confidence - this is auto-generated, not agent-written
        ["auto-reflect"],
        AGENT_ID,
    ))
    print(f"Auto-reflection stored for {session_id}: [{outcome or 'success'}] {task_type}")
    conn.close()


# ---------------------------------------------------------------------------
# cleanup - Expire old memories, summarize old reflections
# ---------------------------------------------------------------------------
def cmd_backfill_embeddings(args):
    conn = get_conn()

    rows = safe_query(conn, """
        SELECT id, content FROM memories
        WHERE embedding IS NULL AND superseded_by IS NULL
        ORDER BY id
    """)

    if not rows:
        print("All memories already have embeddings.")
        conn.close()
        return

    print(f"Backfilling {len(rows)} memories...")
    batch_size = args.batch_size or 50
    model = get_embed_model()
    success = 0
    failed = 0

    # Process in batches for efficiency (model2vec.encode accepts lists)
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts = [r["content"] for r in batch]
        try:
            vectors = model.encode(texts)
            for r, vec in zip(batch, vectors):
                safe_execute(conn, """
                    UPDATE memories SET embedding = %s WHERE id = %s
                """, (vec.tolist(), r["id"]))
                success += 1
        except Exception as e:
            # Fall back to one-by-one for this batch
            print(f"  Batch {i//batch_size} failed, retrying individually: {e}", file=sys.stderr)
            for r in batch:
                try:
                    vec = embed_text(r["content"])
                    safe_execute(conn, """
                        UPDATE memories SET embedding = %s WHERE id = %s
                    """, (vec.tolist(), r["id"]))
                    success += 1
                except Exception as e2:
                    print(f"  FAILED id={r['id']}: {e2}", file=sys.stderr)
                    failed += 1

    print(f"Backfill complete: {success} embedded, {failed} failed")
    conn.close()


def cmd_cleanup(args):
    conn = get_conn()

    # Expire TTL memories
    expired = safe_query(conn, """
        DELETE FROM memories
        WHERE ttl_days IS NOT NULL
          AND created_at + (ttl_days || ' days')::INTERVAL < NOW()
        RETURNING id
    """)
    print(f"Expired {len(expired)} TTL memories")

    # Mark low-importance, never-recalled memories older than 90 days
    stale = safe_query(conn, """
        UPDATE memories
        SET ttl_days = 30
        WHERE importance <= 3
          AND recalled_count = 0
          AND created_at < NOW() - INTERVAL '90 days'
          AND ttl_days IS NULL
          AND superseded_by IS NULL
        RETURNING id
    """)
    print(f"Marked {len(stale)} stale memories for 30-day expiry")

    # Clean up old pending_transcripts (completed/skipped/failed > 30 days)
    old_transcripts = safe_query(conn, """
        DELETE FROM pending_transcripts
        WHERE status IN ('completed', 'skipped', 'failed')
          AND created_at < NOW() - INTERVAL '30 days'
        RETURNING id
    """)
    if old_transcripts:
        print(f"Cleaned up {len(old_transcripts)} old pending_transcripts")

    # Clean up old log files (> 30 days)
    if LOG_DIR.exists():
        cutoff = datetime.now().timestamp() - 30 * 86400
        removed = 0
        for log_file in LOG_DIR.glob("sonnet_*.log"):
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                removed += 1
        if removed:
            print(f"Cleaned up {removed} old Sonnet log files")

    # Count stats
    stats = safe_query(conn, """
        SELECT
            (SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL) AS active_memories,
            (SELECT COUNT(*) FROM reflections) AS total_reflections,
            (SELECT COUNT(*) FROM lessons_learned) AS total_lessons,
            (SELECT COUNT(*) FROM backtest_results) AS total_backtests,
            (SELECT COUNT(*) FROM sessions) AS total_sessions,
            (SELECT COUNT(*) FROM pending_transcripts WHERE status = 'pending') AS pending_transcripts
    """)
    if stats:
        s = stats[0]
        print(f"DB stats: {s['active_memories']} memories, {s['total_reflections']} reflections, "
              f"{s['total_lessons']} lessons, {s['total_backtests']} backtests, "
              f"{s['total_sessions']} sessions, {s['pending_transcripts']} pending transcripts")

    conn.close()


# ---------------------------------------------------------------------------
# turn-recall - Per-turn semantic memory retrieval (UserPromptSubmit hook)
# ---------------------------------------------------------------------------
def sanitize_tsquery_term(word):
    """Strip non-alphanumeric chars to prevent tsquery injection."""
    return ''.join(c for c in word if c.isalnum())


def extract_keywords(prompt):
    """Extract significant words from prompt for keyword fallback search.
    Returns up to 8 words that are >2 chars, alphanumeric only."""
    keywords = []
    for w in prompt.split():
        clean = sanitize_tsquery_term(w)
        if len(clean) > 2:
            keywords.append(clean)
        if len(keywords) >= 8:
            break
    return keywords


def _seeded_graph_walk(conn, seed_ids, query_vec, exclude_ids, _SELECT_COLS, session_id=None):
    """Seeded graph walk: from seed memory IDs, find linked entities,
    walk 1-hop relationships, return connected memories not in seeds.

    Flow: seed_ids → entity_mentions → entities → relationships
          → entities → entity_mentions → memories (LIMIT 5)

    Returns list of memory dicts compatible with turn-recall candidates.
    """
    import numpy as np

    if not seed_ids:
        return []

    seed_list = list(seed_ids)

    # Single query: seed memories → entity_mentions → relationships → entity_mentions → memories
    exclude_list = list(exclude_ids) if exclude_ids else []
    prefixed_cols = ", ".join(f"m.{c.strip()}" for c in _SELECT_COLS.split(","))

    extra_clauses = []
    extra_params = []
    if exclude_list:
        extra_clauses.append("AND m.id != ALL(%s)")
        extra_params.append(exclude_list)
    if session_id:
        extra_clauses.append("AND NOT EXISTS (SELECT 1 FROM memory_exposures me "
                             "WHERE me.session_id = %s AND me.memory_id = m.id)")
        extra_params.append(session_id)

    graph_memories = safe_query(conn, f"""
        SELECT DISTINCT ON (m.id) {prefixed_cols},
               0.40 AS similarity
        FROM entity_mentions em_seed
        JOIN relationships r ON (em_seed.entity_id = r.source_id OR em_seed.entity_id = r.target_id)
                             AND r.is_valid = TRUE
        JOIN entity_mentions em_walked ON em_walked.entity_id = CASE
            WHEN em_seed.entity_id = r.source_id THEN r.target_id
            ELSE r.source_id END
        JOIN memories m ON m.id = em_walked.memory_id
        WHERE em_seed.memory_id = ANY(%s)
          AND m.id != ALL(%s)
          AND m.superseded_by IS NULL
          AND (m.ttl_days IS NULL OR m.created_at + (m.ttl_days || ' days')::INTERVAL > NOW())
          AND (m.last_injected_at IS NULL OR m.last_injected_at < NOW() - INTERVAL '2 hours')
          {' '.join(extra_clauses)}
        ORDER BY m.id, m.importance DESC
        LIMIT 5
    """, [seed_list, seed_list] + extra_params)

    if not graph_memories:
        return []

    # Backfill similarity from embeddings
    for gm in graph_memories:
        if query_vec is not None and gm.get("embedding"):
            try:
                g_emb = np.array(
                    json.loads(gm["embedding"]) if isinstance(gm["embedding"], str) else gm["embedding"],
                    dtype=np.float32
                )
                gm["similarity"] = max(0.35, float(
                    np.dot(query_vec, g_emb) / (np.linalg.norm(query_vec) * np.linalg.norm(g_emb) + 1e-9)
                ))
            except Exception:
                gm["similarity"] = 0.40
        gm["_source"] = "graph"

    return graph_memories


def _format_age(ended_at):
    """Format a timestamp as a relative age string (Xm ago, Xh ago, Xd ago)."""
    age = datetime.now(timezone.utc) - (
        ended_at.replace(tzinfo=timezone.utc) if ended_at.tzinfo is None else ended_at
    )
    age_hours = age.total_seconds() / 3600
    if age_hours < 1:
        return f"{max(1, int(age.total_seconds() / 60))}m ago"
    elif age_hours < 24:
        return f"{int(age_hours)}h ago"
    else:
        return f"{int(age_hours / 24)}d ago"


def _build_first_turn_context(conn):
    """Build project context + session handoff text for first-turn injection.

    Returns a string to prepend to additionalContext, or empty string if nothing
    to inject. Queries the projects table and sessions table.
    """
    parts = []
    project = get_project()
    session_id = get_current_session_id()

    # ── Project context ──
    try:
        cwd = os.getcwd()
        proj_row = safe_query(conn, """
            SELECT id, name, display_name, description, status, tech_stack, goals,
                   milestones, last_handoff
            FROM projects
            WHERE directory = %s OR %s LIKE directory || '%%'
            LIMIT 1
        """, (cwd, cwd))
        if proj_row:
            p = proj_row[0]
            lines = []
            lines.append(f"## Project: {p['display_name'] or p['name']}")
            if p.get("description"):
                lines.append(p["description"])
            if p.get("goals"):
                lines.append(f"**Goals:** {p['goals']}")
            if p.get("tech_stack"):
                stack = ", ".join(p["tech_stack"]) if isinstance(p["tech_stack"], list) else str(p["tech_stack"])
                lines.append(f"**Stack:** {stack}")
            if p.get("status") and p["status"] != "active":
                lines.append(f"**Status:** {p['status']}")

            # Key artifacts
            artifacts = safe_query(conn, """
                SELECT file_path, artifact_type, title
                FROM project_artifacts
                WHERE project_id = %s AND status = 'current'
                ORDER BY updated_at DESC
                LIMIT 5
            """, (p["id"],))
            if artifacts:
                lines.append("**Key files:**")
                for a in artifacts:
                    label = a.get("title") or os.path.basename(a["file_path"])
                    lines.append(f"- {a['artifact_type']}: {label} (`{a['file_path']}`)")

            parts.append("--- Project Context ---\n" + "\n".join(lines))
    except Exception:
        pass  # Non-critical

    # ── Session handoff ──
    try:
        # Prefer COMPLETED: handoff, fall back to latest session
        prev_session = safe_query(conn, """
            SELECT session_id, summary, ended_at
            FROM sessions
            WHERE project = %s
              AND ended_at IS NOT NULL
              AND session_id != %s
              AND summary LIKE 'COMPLETED:%%'
            ORDER BY ended_at DESC
            LIMIT 1
        """, (project, session_id))
        if not prev_session:
            prev_session = safe_query(conn, """
                SELECT session_id, summary, ended_at
                FROM sessions
                WHERE project = %s
                  AND ended_at IS NOT NULL
                  AND session_id != %s
                ORDER BY ended_at DESC
                LIMIT 1
            """, (project, session_id))

        handoff_lines = []
        if prev_session and prev_session[0].get("summary"):
            row = prev_session[0]
            age_str = _format_age(row["ended_at"])
            proj_name = os.path.basename(project) if project else "unknown"
            handoff_lines.append(f"## Previous Session ({proj_name}, {age_str})")

            summary = row["summary"]
            if summary.startswith("COMPLETED:"):
                for line in summary.split("\n"):
                    handoff_lines.append(line)
            else:
                handoff_lines.append("_Sonnet has not yet processed this session._")
                for line in summary.split("\n")[:25]:
                    handoff_lines.append(line)
        else:
            handoff_lines.append("## Previous Session")
            handoff_lines.append("No previous session for this project.")

        handoff_lines.append("")
        handoff_lines.append(f"_Session: {session_id}_")

        # Truncate to budget
        if len(handoff_lines) > 30:
            handoff_lines = handoff_lines[:30]

        parts.append("--- Session Handoff ---\n" + "\n".join(handoff_lines))
    except Exception:
        pass  # Non-critical

    return "\n\n".join(parts)


def cmd_turn_recall(args):
    """Per-turn memory retrieval: read user prompt, search DB, inject as additionalContext."""
    if _skip_memory_loop():
        return

    hook_input = read_hook_stdin()
    if not hook_input:
        return

    # Deferred session insert: create DB row on first user prompt, not at startup
    # Also triggers first-turn project context + session handoff injection.
    flag_file = SESSION_ID_FILE.parent / ".session_needs_insert"
    is_first_turn = flag_file.exists()
    if is_first_turn:
        try:
            # Read flag file — JSON (new) or plain text (legacy)
            flag_raw = flag_file.read_text(encoding="utf-8").strip()
            try:
                flag_data = json.loads(flag_raw)
            except (json.JSONDecodeError, ValueError):
                flag_data = {"session_id": flag_raw}  # Legacy plain-text format

            conn = get_conn()
            session_id = flag_data.get("session_id") or get_current_session_id()
            if session_id:
                project = get_project()
                safe_execute(conn, """
                    INSERT INTO sessions (session_id, started_at, summary, outcome, project, agent_id,
                                          claude_session_id, transcript_path, conversation_id)
                    VALUES (%s, NOW(), NULL, NULL, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id) DO NOTHING
                """, (session_id, project, AGENT_ID,
                      flag_data.get("claude_session_id"),
                      flag_data.get("transcript_path"),
                      flag_data.get("conversation_id")))
            flag_file.unlink()
            conn.close()
        except Exception:
            pass  # Will retry next prompt

    prompt = hook_input.get("prompt", "")
    if len(prompt.strip()) < 10:
        return  # Skip trivial prompts ("yes", "ok", etc.)

    # Auto-checkpoint: check transcript growth (works even when DB is down)
    conn = None
    try:
        conn = get_conn()
    except Exception:
        pass  # DB down — continue to checkpoint check, skip memory search

    checkpoint_note = _auto_checkpoint(hook_input, conn)

    # Resolve session_id early — needed for session dedup in queries
    hook_session_id = get_current_session_id() or hook_input.get("session_id")

    if not conn:
        # DB is down — emit checkpoint note if we have one, otherwise exit
        if checkpoint_note:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": checkpoint_note.strip(),
                }
            }
            print(json.dumps(output))
        return

    results = []
    search_mode = "hybrid"

    # --- Hybrid BM25+Vector search with Reciprocal Rank Fusion ---
    # Run both searches, merge via RRF, then multi-factor rerank + MMR
    # Session dedup: exclude memories already injected in this session
    session_clause = ""
    session_params = []
    if hook_session_id:
        session_clause = ("AND NOT EXISTS (SELECT 1 FROM memory_exposures me "
                          "WHERE me.session_id = %s AND me.memory_id = memories.id)")
        session_params = [hook_session_id]

    _COMMON_FILTER = f"""
        WHERE superseded_by IS NULL
          AND (ttl_days IS NULL OR created_at + (ttl_days || ' days')::INTERVAL > NOW())
          AND (last_injected_at IS NULL OR last_injected_at < NOW() - INTERVAL '2 hours')
          {session_clause}
    """
    _SELECT_COLS = """id, content, category, domain, importance, project, tags,
                      created_at, useful_pct, embedding, project_id"""

    vec_results = []

    # Resolve current project_id early — used for project-scoped cosine + reranking
    current_project_id = None
    try:
        cwd = os.getcwd()
        proj_row = safe_query(conn, """
            SELECT id FROM projects WHERE directory = %s
            OR %s LIKE directory || '%%' LIMIT 1
        """, (cwd, cwd))
        if proj_row:
            current_project_id = proj_row[0]["id"]
    except Exception:
        pass

    # 1. Project-scoped vector search — fetch top 10, filter by similarity > 0.45
    # Project filter: same project, untagged (NULL), or critical (imp >= 9)
    project_clause = ""
    project_params = []
    if current_project_id:
        project_clause = "AND (project_id = %s OR project_id IS NULL OR importance >= 9)"
        project_params = [current_project_id]

    query_vec = None
    try:
        import numpy as np
        query_vec = embed_text(prompt)
        vec_params = [query_vec.tolist()] + session_params + project_params + [query_vec.tolist()]
        vec_results = safe_query(conn, f"""
            SELECT {_SELECT_COLS},
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memories
            {_COMMON_FILTER}
              AND embedding IS NOT NULL
              {project_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT 10
        """, tuple(vec_params))
        vec_results = [r for r in vec_results if float(r["similarity"]) > 0.45]
    except Exception:
        pass  # Vector search failed

    if not vec_results:
        search_mode = "none"
        candidates = []
    else:
        candidates = list(vec_results)

    # 2. Seeded graph walk — always run, using cosine results as seeds
    if vec_results and query_vec is not None:
        try:
            seed_ids = [r["id"] for r in vec_results]
            existing_ids = set(seed_ids)
            graph_results = _seeded_graph_walk(conn, seed_ids, query_vec, existing_ids,
                                               _SELECT_COLS, session_id=hook_session_id)
            for gr in graph_results:
                if gr["id"] not in existing_ids:
                    candidates.append(gr)
                    existing_ids.add(gr["id"])
        except Exception:
            pass  # Graph walk is non-critical

    # 3. Multi-factor reranking + MMR diversity

    if candidates:
        try:
            import numpy as np
            import math
            for c in candidates:
                relevance = float(c.get("similarity", 0.40))
                age_days = (datetime.now(timezone.utc) - c["created_at"]).total_seconds() / 86400
                imp = int(c.get("importance", 5) or 5)
                if imp >= 8:
                    recency = 1.0
                else:
                    recency = math.exp(-math.log(2) / 30.0 * age_days)
                project_boost = 0.10 if (current_project_id and c.get("project_id") == current_project_id) else 0.0
                graph_boost = 0.05 if c.get("_source") == "graph" else 0.0
                c["_composite"] = (0.50 * relevance
                                   + 0.15 * recency
                                   + 0.35 * (imp / 10.0)
                                   + project_boost
                                   + graph_boost)

            candidates.sort(key=lambda x: x["_composite"], reverse=True)

            # MMR diversity: select top 3 avoiding near-duplicates (λ=0.7)
            def _parse_embedding(raw):
                if isinstance(raw, (list, np.ndarray)):
                    return np.array(raw, dtype=np.float32)
                if isinstance(raw, str):
                    try:
                        return np.array(json.loads(raw), dtype=np.float32)
                    except (json.JSONDecodeError, ValueError):
                        return None
                return None

            selected = []
            remaining = list(candidates)
            for _ in range(min(3, len(remaining))):
                if not selected:
                    best = remaining[0]
                else:
                    best = None
                    best_mmr = -float("inf")
                    sel_embs = [_parse_embedding(s.get("embedding")) for s in selected]
                    for c in remaining:
                        c_emb = _parse_embedding(c.get("embedding"))
                        if c_emb is None or any(e is None for e in sel_embs):
                            max_sim_to_selected = 0.0
                        else:
                            sims = [float(np.dot(c_emb, e) / (np.linalg.norm(c_emb) * np.linalg.norm(e) + 1e-9))
                                    for e in sel_embs]
                            max_sim_to_selected = max(sims)
                        mmr = 0.7 * c["_composite"] - 0.3 * max_sim_to_selected
                        if mmr > best_mmr:
                            best_mmr = mmr
                            best = c
                if best:
                    selected.append(best)
                    remaining.remove(best)

            results = selected
        except Exception:
            import traceback
            print(f"turn-recall scoring error: {traceback.format_exc()}", file=sys.stderr)
            results = candidates[:3]

    # Update recalled_count + last_injected_at for matched memories
    if results:
        ids = [r["id"] for r in results]
        try:
            safe_execute(conn, """
                UPDATE memories SET recalled_count = recalled_count + 1,
                    last_recalled = NOW(), last_injected_at = NOW()
                WHERE id = ANY(%s)
            """, (ids,))
        except Exception:
            pass  # Non-critical

        # Log inject exposures (hook_session_id resolved earlier for session dedup)
        try:
            if hook_session_id:
                for r in results:
                    log_exposure(conn, hook_session_id, r["id"], "inject",
                                 similarity=float(r.get("similarity", 0)) if r.get("similarity") else None)
        except Exception:
            pass  # Non-critical

    # First-turn injection: project context + session handoff
    first_turn_context = ""
    if is_first_turn:
        try:
            first_turn_context = _build_first_turn_context(conn)
        except Exception:
            pass  # Non-critical

    if not results and not checkpoint_note and not first_turn_context:
        conn.close()
        return

    # Format output with metadata
    lines = []
    for r in results:
        if r.get("_composite") is not None:
            score_tag = f"score:{float(r['_composite']):.2f}"
        elif search_mode == "semantic" and r.get("similarity") is not None:
            score_tag = f"sim:{float(r['similarity']):.2f}"
        else:
            score_tag = f"rank:{float(r.get('rank', 0)):.2f}"
        project = r.get("project", "") or ""
        project_tag = f" @{os.path.basename(project)}" if project else ""
        imp = int(r.get("importance", 5) or 5)
        lines.append(f"- [ID {r['id']}] ({score_tag}{project_tag}) [{r['category']}|{r['domain']}|imp:{imp}] {r['content']}")

    context_parts = []
    if first_turn_context:
        context_parts.append(first_turn_context)
    if lines:
        context_parts.append("Relevant memories:\n" + "\n".join(lines))
    if checkpoint_note:
        context_parts.append(checkpoint_note)

    context_text = "\n\n".join(context_parts)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_text.strip(),
        }
    }

    print(json.dumps(output))

    # Trace: emit prompt_submit + memory_recall events
    emit_trace("prompt_submit", {
        "prompt_preview": prompt[:200],
        "prompt_length": len(prompt),
        "is_first_turn": is_first_turn,
    }, session_id=hook_session_id)
    if results:
        emit_trace("memory_recall", {
            "query_preview": prompt[:100],
            "results_count": len(results),
            "memories": [{"id": r["id"], "content_preview": (r["content"] or "")[:100],
                          "similarity": float(r.get("similarity") or r.get("_composite") or 0),
                          "category": r.get("category", ""),
                          "importance": int(r.get("importance", 5) or 5)} for r in results[:5]],
            "search_mode": search_mode,
        }, session_id=hook_session_id)

    conn.close()


# ---------------------------------------------------------------------------
# Tool-recall - PreToolUse hook memory injection
# ---------------------------------------------------------------------------
def _map_tool_name(raw_tool_name):
    """Map Claude Code tool names to error_solutions tool_name values."""
    if not raw_tool_name:
        return None
    # MCP tools: mcp__server__method → server-name
    if raw_tool_name.startswith("mcp__claude-in-chrome__") or raw_tool_name.startswith("mcp__claude_in_chrome__"):
        return "chrome-mcp"
    if raw_tool_name.startswith("mcp__playwright__"):
        return "playwright-mcp"
    if raw_tool_name.startswith("mcp__slack__"):
        return "slack-mcp"
    # Core tools: exact match
    if raw_tool_name in ("Bash", "Edit", "Write", "Read", "Agent", "WebFetch", "Glob", "Grep"):
        return raw_tool_name
    return raw_tool_name  # passthrough for unknown tools


def _classify_bash_command(command):
    """Classify a Bash command into a sub-type for targeted error_solutions lookup.

    Returns platform string matching error_solutions.platform, or None for generic.
    """
    if not command:
        return None
    # Extract first meaningful token (skip env vars, quotes, pipes-to)
    parts = command.strip().split()
    if not parts:
        return None
    first = parts[0].strip("\"'").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    # Remove .exe suffix
    if first.endswith(".exe"):
        first = first[:-4]

    _CMD_MAP = {
        "git": "git", "gh": "git",
        "python": "python", "python3": "python", "py": "python",
        "psql": "psql", "pg_dump": "psql", "pg_restore": "psql",
        "npm": "npm", "npx": "npm", "node": "npm", "yarn": "npm",
        "pip": "python", "pip3": "python", "uv": "python",
        "docker": "docker", "docker-compose": "docker",
        "claude": "claude-cli",
        "schtasks": "windows", "taskkill": "windows", "net": "windows", "sc": "windows",
    }
    sub = _CMD_MAP.get(first)
    if sub:
        return sub

    # Check second token for common wrappers: "cmd /c python ...", "bash -c git ..."
    if first in ("cmd", "bash", "sh", "powershell", "pwsh") and len(parts) >= 3:
        second = parts[2] if parts[1] in ("/c", "-c") else parts[1]
        second = second.strip("\"'").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if second.endswith(".exe"):
            second = second[:-4]
        return _CMD_MAP.get(second)

    return None


def cmd_tool_recall(args):
    """Per-tool memory retrieval: read PreToolUse hook JSON, inject tool notes and error solutions."""
    if _skip_memory_loop():
        return

    hook_input = read_hook_stdin()
    if not hook_input:
        return

    raw_tool_name = hook_input.get("tool_name", "")
    tool_name = _map_tool_name(raw_tool_name)
    if not tool_name:
        return

    # For Bash, sub-classify the command for targeted lookup
    bash_sub = None
    if tool_name == "Bash":
        tool_input = hook_input.get("tool_input", {})
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        bash_sub = _classify_bash_command(command)

    conn = None
    try:
        conn = get_conn()
    except Exception:
        return  # DB down — skip silently

    try:
        # Query memories — for Bash with sub-type, filter by matching tags
        if bash_sub:
            mem_rows = safe_query(conn, """
                SELECT id, content
                FROM memories
                WHERE retrieval_mode = 'tool'
                  AND tool_name = %s
                  AND superseded_by IS NULL
                  AND tags @> ARRAY[%s]
                ORDER BY importance DESC
                LIMIT 2
            """, (tool_name, bash_sub))
        else:
            mem_rows = safe_query(conn, """
                SELECT id, content
                FROM memories
                WHERE retrieval_mode = 'tool'
                  AND tool_name = %s
                  AND superseded_by IS NULL
                ORDER BY importance DESC
                LIMIT 2
            """, (tool_name,))

        # Query error_solutions — for Bash with sub-type, filter by platform
        if bash_sub:
            err_rows = safe_query(conn, """
                SELECT id, error_pattern, solution
                FROM error_solutions
                WHERE tool_name = %s AND platform = %s
                ORDER BY verified DESC, hit_count DESC
                LIMIT 3
            """, (tool_name, bash_sub))
        else:
            err_rows = safe_query(conn, """
                SELECT id, error_pattern, solution
                FROM error_solutions
                WHERE tool_name = %s
                ORDER BY verified DESC, hit_count DESC
                LIMIT 3
            """, (tool_name,))

        if not mem_rows and not err_rows:
            return

        # Update last_injected_at on retrieved tool memories (cooldown + analytics)
        if mem_rows:
            mem_ids = [r["id"] for r in mem_rows]
            try:
                safe_execute(conn, """
                    UPDATE memories SET recalled_count = recalled_count + 1,
                        last_recalled = NOW(), last_injected_at = NOW()
                    WHERE id = ANY(%s)
                """, (mem_ids,))
            except Exception:
                pass  # Non-critical

        label = f"{tool_name}:{bash_sub}" if bash_sub else tool_name
        lines = []
        for row in mem_rows:
            content = (row["content"] or "")[:100]
            lines.append(f"- [mem:{row['id']}] {content}")

        for row in err_rows:
            pattern = (row["error_pattern"] or "")[:60]  # shorter — just for identification
            solution = (row["solution"] or "")[:100]
            lines.append(f"- [err:{row['id']}] {pattern}: {solution}")

        context_text = f"Tool notes for {label}:\n" + "\n".join(lines)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context_text.strip(),
            }
        }
        print(json.dumps(output))

        # Trace: emit tool_recall event
        emit_trace("tool_recall", {
            "tool_name": tool_name,
            "bash_sub": bash_sub,
            "memories_count": len(mem_rows) if mem_rows else 0,
            "errors_count": len(err_rows) if err_rows else 0,
            "injected_context_preview": context_text[:300] if context_text else "",
        }, session_id=get_current_session_id())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-store - PostToolUse hook error capture (write-only)
# ---------------------------------------------------------------------------
# Note: each hook invocation spawns a new process, so module-level state
# does not persist across calls. Dedup relies on the trigram query below.


import re as _re

# Compiled regex for error detection — matches at line start or after whitespace
# to avoid false positives like "0 failed" or "BACKTEST_FAILED_AT"
_ERROR_RE = _re.compile(
    r"(?:^|\n)\s*(?:error:|Error:|ERROR:|Exception:|exception:|FAILED\b|"
    r"Traceback \(most recent call last\)|ModuleNotFoundError|ImportError|"
    r"SyntaxError|TypeError|ValueError|KeyError|AttributeError|"
    r"ConnectionRefusedError|TimeoutError|Permission denied|"
    r"No such file or directory|command not found|Exit code [12]\b)",
    _re.MULTILINE
)


def _detect_error(tool_response_str):
    """Return True if tool_response looks like an error (line-anchored regex)."""
    if not tool_response_str:
        return False
    return bool(_ERROR_RE.search(tool_response_str))


def cmd_error_store(args):
    """Capture tool errors from PostToolUse hook and update error_solutions. Write-only."""
    if _skip_memory_loop():
        return

    hook_input = read_hook_stdin()
    if not hook_input:
        return

    raw_tool_name = hook_input.get("tool_name", "")
    # PostToolUseFailure provides "error" field; PostToolUse provides "tool_response"
    tool_response = hook_input.get("error", "") or hook_input.get("tool_response", "")
    if not isinstance(tool_response, str):
        tool_response = json.dumps(tool_response)

    # PostToolUseFailure always has errors; PostToolUse needs detection
    is_failure_event = "error" in hook_input
    if not is_failure_event and not _detect_error(tool_response):
        return

    tool_name = _map_tool_name(raw_tool_name)
    if not tool_name:
        return

    conn = None
    try:
        conn = get_conn()
    except Exception:
        return  # DB down — skip silently

    try:
        # Check for trigram match in existing error_solutions
        # %% = literal % = pg_trgm similarity operator (threshold default 0.3)
        match_rows = safe_query(conn, """
            SELECT id
            FROM error_solutions
            WHERE tool_name = %s AND error_pattern %% %s
            ORDER BY similarity(error_pattern, %s) DESC
            LIMIT 1
        """, (tool_name, tool_response[:200], tool_response[:200]))

        if match_rows:
            matched_id = match_rows[0]["id"]
            safe_execute(conn, """
                UPDATE error_solutions
                SET hit_count = hit_count + 1, last_hit = NOW()
                WHERE id = %s
            """, (matched_id,))
        else:
            error_pattern = tool_response[:200]
            # Store tail of response as example — Python tracebacks have the
            # actual exception message at the end, not the beginning
            error_example = tool_response[-500:] if len(tool_response) > 500 else tool_response
            safe_execute(conn, """
                INSERT INTO error_solutions
                    (error_pattern, error_example, solution, tool_name, platform, tags, verified, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, false, %s)
            """, (
                error_pattern,
                error_example,
                "Pending Sonnet extraction",
                tool_name,
                "windows",
                ["auto-captured", tool_name],
                AGENT_ID,
            ))

        # Trace: emit tool_error or tool_success event
        emit_trace("tool_error" if (is_failure_event or _detect_error(tool_response)) else "tool_success", {
            "tool_name": tool_name,
            "error_preview": tool_response[:500] if is_failure_event else "",
            "response_preview": tool_response[:500] if not is_failure_event else "",
            "error_pattern": tool_response[:200] if is_failure_event else "",
            "matched_solution_id": match_rows[0]["id"] if match_rows else None,
            "is_new_error": not bool(match_rows),
        }, session_id=get_current_session_id())

    except Exception:
        pass  # DB write failure — skip silently, will retry next occurrence
    finally:
        conn.close()


def cmd_store_error_solution(args):
    """Store an error→solution pattern (used by Sonnet processor for transcript extraction)."""
    conn = get_conn()
    try:
        # Dedup: check if similar error_pattern already exists via trigram
        existing = safe_query(conn, """
            SELECT id, solution FROM error_solutions
            WHERE error_pattern %% %s AND tool_name = %s
            LIMIT 1
        """, (args.error_pattern, args.tool_name))

        if existing:
            # Update solution if the existing one is a placeholder
            row = existing[0]
            if row['solution'] == "Pending Sonnet extraction":
                safe_execute(conn, """
                    UPDATE error_solutions SET solution = %s, verified = true
                    WHERE id = %s
                """, (args.solution, row['id']))
                print(f"Updated error_solution [id:{row['id']}] with extracted solution")
            else:
                print(f"Similar error_solution already exists [id:{row['id']}], skipping")
        else:
            safe_execute(conn, """
                INSERT INTO error_solutions
                    (error_pattern, solution, tool_name, platform, tags, verified, agent_id)
                VALUES (%s, %s, %s, %s, %s, true, %s)
            """, (
                args.error_pattern,
                args.solution,
                args.tool_name,
                args.platform or "windows",
                args.tags or ["sonnet-extracted"],
                AGENT_ID,
            ))
            print(f"Stored error_solution: [{args.tool_name}] {args.error_pattern[:60]}...")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Supplementary processor CLI commands — entities, relationships, artifacts
# ---------------------------------------------------------------------------
def cmd_store_entity(args):
    """Upsert an entity into the graph (used by Sonnet-Supplementary)."""
    conn = get_conn()
    try:
        # Upsert: increment mention_count if exists, insert if new
        existing = safe_query(conn, """
            SELECT id FROM entities
            WHERE name = %s AND entity_type = %s AND agent_id = %s
            LIMIT 1
        """, (args.name, args.entity_type, AGENT_ID))
        if existing:
            eid = existing[0]["id"]
            safe_execute(conn, """
                UPDATE entities SET mention_count = mention_count + 1,
                    last_seen = NOW(),
                    description = COALESCE(%s, description),
                    aliases = CASE WHEN %s IS NOT NULL THEN
                        (SELECT array_agg(DISTINCT x) FROM unnest(aliases || %s::text[]) x)
                    ELSE aliases END
                WHERE id = %s
            """, (args.description, args.aliases, args.aliases, eid))
            print(f"Updated entity [{eid}] {args.name} ({args.entity_type})")
        else:
            vec = None
            try:
                vec = embed_text(f"{args.name}: {args.description or args.entity_type}")
            except Exception:
                pass
            if vec is not None:
                safe_execute(conn, """
                    INSERT INTO entities (name, entity_type, description, aliases, embedding, project_id, agent_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (args.name, args.entity_type, args.description, args.aliases, vec.tolist(), args.project_id, AGENT_ID))
            else:
                safe_execute(conn, """
                    INSERT INTO entities (name, entity_type, description, aliases, project_id, agent_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (args.name, args.entity_type, args.description, args.aliases, args.project_id, AGENT_ID))
            print(f"Stored entity: {args.name} ({args.entity_type})")
    finally:
        conn.close()


def cmd_store_relationship(args):
    """Upsert a relationship between two entities (used by Sonnet-Supplementary)."""
    conn = get_conn()
    try:
        # Resolve entity IDs by name
        src = safe_query(conn, "SELECT id FROM entities WHERE name = %s AND agent_id = %s LIMIT 1",
                         (args.source, AGENT_ID))
        tgt = safe_query(conn, "SELECT id FROM entities WHERE name = %s AND agent_id = %s LIMIT 1",
                         (args.target, AGENT_ID))
        if not src:
            print(f"Source entity '{args.source}' not found, skipping relationship")
            return
        if not tgt:
            print(f"Target entity '{args.target}' not found, skipping relationship")
            return
        src_id, tgt_id = src[0]["id"], tgt[0]["id"]

        existing = safe_query(conn, """
            SELECT id FROM relationships
            WHERE source_id = %s AND target_id = %s AND relationship_type = %s
            LIMIT 1
        """, (src_id, tgt_id, args.relationship_type))
        if existing:
            safe_execute(conn, """
                UPDATE relationships SET mention_count = mention_count + 1,
                    confidence = GREATEST(confidence, %s),
                    description = COALESCE(%s, description)
                WHERE id = %s
            """, (args.confidence or 7, args.description, existing[0]["id"]))
            print(f"Updated relationship: {args.source} -{args.relationship_type}-> {args.target}")
        else:
            session_id = get_current_session_id()
            safe_execute(conn, """
                INSERT INTO relationships (source_id, target_id, relationship_type, description,
                                           confidence, source_session, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (src_id, tgt_id, args.relationship_type, args.description,
                  args.confidence or 7, session_id, AGENT_ID))
            print(f"Stored relationship: {args.source} -{args.relationship_type}-> {args.target}")
    finally:
        conn.close()


def cmd_store_artifact(args):
    """Upsert a project artifact (used by Sonnet-Supplementary)."""
    conn = get_conn()
    try:
        # Look up project_id from project directory
        project_id = None
        if args.project:
            proj = safe_query(conn, """
                SELECT id FROM projects WHERE directory = %s OR %s LIKE directory || '%%' LIMIT 1
            """, (args.project, args.project))
            if proj:
                project_id = proj[0]["id"]
        if not project_id:
            print(f"Project not found for '{args.project}', skipping artifact")
            return

        # Upsert by file_path + project_id
        existing = safe_query(conn, """
            SELECT id FROM project_artifacts WHERE file_path = %s AND project_id = %s LIMIT 1
        """, (args.file_path, project_id))
        if existing:
            safe_execute(conn, """
                UPDATE project_artifacts SET updated_at = NOW(),
                    title = COALESCE(%s, title),
                    description = COALESCE(%s, description),
                    artifact_type = COALESCE(%s, artifact_type),
                    status = COALESCE(%s, status)
                WHERE id = %s
            """, (args.title, args.description, args.artifact_type, args.status, existing[0]["id"]))
            print(f"Updated artifact: {args.file_path}")
        else:
            session_id = get_current_session_id()
            safe_execute(conn, """
                INSERT INTO project_artifacts (project_id, file_path, artifact_type, title,
                                               description, status, created_by_session, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (project_id, args.file_path, args.artifact_type, args.title,
                  args.description, args.status or "current", session_id, AGENT_ID))
            print(f"Stored artifact: {args.file_path} ({args.artifact_type})")
    finally:
        conn.close()


def cmd_process_supplementary(args):
    """Claim oldest completed transcript for supplementary processing (entity/graph extraction)."""
    conn = get_conn()
    try:
        # Find transcripts completed by Core but not yet supplementary-processed
        row = safe_query(conn, """
            SELECT id, session_id, project, compact_summary, raw_content, had_compaction
            FROM pending_transcripts
            WHERE status = 'completed'
              AND (supplementary_status IS NULL OR supplementary_status = 'pending')
            ORDER BY created_at ASC
            LIMIT 1
        """)
        if not row:
            print("NO_PENDING")
            return

        t = row[0]
        tid = t["id"]
        # Mark as processing
        safe_execute(conn, """
            UPDATE pending_transcripts SET supplementary_status = 'processing'
            WHERE id = %s
        """, (tid,))

        # Output in same format as process-pending
        print(f"TRANSCRIPT_ID: {tid}")
        print(f"SESSION_ID: {t['session_id']}")
        print(f"PROJECT: {t['project']}")
        print(f"HAD_COMPACTION: {t['had_compaction']}")
        print("---COMPACT_SUMMARY_START---")
        print(t.get("compact_summary") or "(none)")
        print("---COMPACT_SUMMARY_END---")
        print("---RAW_CONTENT_START---")
        print(t.get("raw_content") or "(none)")
        print("---RAW_CONTENT_END---")
    finally:
        conn.close()


def cmd_mark_supplementary(args):
    """Mark supplementary processing status for a transcript."""
    conn = get_conn()
    safe_execute(conn, """
        UPDATE pending_transcripts
        SET supplementary_status = %s
        WHERE id = %s
    """, (args.status, args.transcript_id))
    print(f"Transcript {args.transcript_id} supplementary marked as {args.status}")
    conn.close()


# ---------------------------------------------------------------------------
# Exposure tracking - get-exposures and score-exposure
# ---------------------------------------------------------------------------
def cmd_get_exposures(args):
    """List unscored memory exposures for a session (used by Sonnet processor)."""
    conn = get_conn()
    rows = safe_query(conn, """
        SELECT me.id as exposure_id, me.memory_id, me.source,
               m.content, m.category, m.domain
        FROM memory_exposures me
        JOIN memories m ON m.id = me.memory_id
        WHERE me.session_id = %s AND me.useful IS NULL
        ORDER BY me.source, me.created_at
    """, (args.session_id,))
    conn.close()
    if not rows:
        print("No unscored exposures for this session.")
        return
    for r in rows:
        print(f"[exposure_id={r['exposure_id']}] [{r['source']}] "
              f"[{r['category']}|{r['domain']}] {r['content'][:200]}")


def cmd_score_exposure(args):
    """Score a memory exposure as useful or not, and update useful_pct."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE memory_exposures SET useful = %s, scored_at = NOW() WHERE id = %s
        """, (args.useful, args.exposure_id))
        if cur.rowcount == 0:
            conn.close()
            print(f"WARNING: exposure_id {args.exposure_id} not found, skipping.")
            return
    # Recalculate useful_pct for this memory
    safe_execute(conn, """
        UPDATE memories SET useful_pct = sub.pct
        FROM (
            SELECT memory_id,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE useful) / NULLIF(COUNT(*), 0), 1) as pct
            FROM memory_exposures
            WHERE memory_id = (SELECT memory_id FROM memory_exposures WHERE id = %s)
              AND useful IS NOT NULL
            GROUP BY memory_id
        ) sub
        WHERE memories.id = sub.memory_id
    """, (args.exposure_id,))
    conn.close()
    print(f"Scored exposure {args.exposure_id} as {'useful' if args.useful else 'not useful'}")


def cmd_batch_score_exposures(args):
    """Score multiple exposures in one call. Accepts JSON map from stdin or --scores arg.
    Format: {"exposure_id": true/false, ...}  (keys are strings, values are booleans)"""
    conn = get_conn()

    # Read scores from stdin (preferred for large payloads) or --scores arg
    scores_raw = None
    if args.scores:
        scores_raw = args.scores
    else:
        try:
            if not sys.stdin.isatty():
                scores_raw = sys.stdin.read().strip()
        except Exception:
            pass

    if not scores_raw:
        print("ERROR: No scores provided. Pass --scores '{...}' or pipe JSON to stdin.",
              file=sys.stderr)
        conn.close()
        sys.exit(1)

    try:
        scores = json.loads(scores_raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    if not isinstance(scores, dict):
        print("ERROR: Scores must be a JSON object {exposure_id: true/false}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    scored_count = 0
    skipped = 0
    memory_ids_to_update = set()

    with conn.cursor() as cur:
        for exp_id_str, useful in scores.items():
            try:
                exp_id = int(exp_id_str)
            except (ValueError, TypeError):
                skipped += 1
                continue
            cur.execute(
                "UPDATE memory_exposures SET useful = %s, scored_at = NOW() WHERE id = %s",
                (bool(useful), exp_id))
            if cur.rowcount > 0:
                scored_count += 1
                cur.execute("SELECT memory_id FROM memory_exposures WHERE id = %s", (exp_id,))
                row = cur.fetchone()
                if row:
                    memory_ids_to_update.add(row[0])
            else:
                skipped += 1

    # Batch recalculate useful_pct for all affected memories
    for mid in memory_ids_to_update:
        safe_execute(conn, """
            UPDATE memories SET useful_pct = sub.pct
            FROM (
                SELECT memory_id,
                       ROUND(100.0 * COUNT(*) FILTER (WHERE useful) / NULLIF(COUNT(*), 0), 1) as pct
                FROM memory_exposures
                WHERE memory_id = %s AND useful IS NOT NULL
                GROUP BY memory_id
            ) sub
            WHERE memories.id = sub.memory_id
        """, (mid,))

    conn.close()
    print(f"Batch scored {scored_count} exposures ({skipped} skipped). "
          f"Updated useful_pct for {len(memory_ids_to_update)} memories.")


# ---------------------------------------------------------------------------
# Paper knowledge base - ingest and recall
# ---------------------------------------------------------------------------
KNOWLEDGE_DIR = Path(os.environ.get("MEMORY_KNOWLEDGE_DIR", Path.home() / "knowledge"))


def chunk_markdown_by_section(md_text, paper_title, max_chars=2000):
    """Split markdown into chunks by ## headers. Prefix each with paper/section context."""
    import re
    # Split on ## headers (keep the header with its content)
    sections = re.split(r'(?=^## )', md_text, flags=re.MULTILINE)
    chunks = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract section header
        header_match = re.match(r'^## (.+)', section)
        header = header_match.group(1).strip() if header_match else "Preamble"

        # Split oversized sections on paragraph boundaries
        if len(section) > max_chars:
            paragraphs = section.split('\n\n')
            current_chunk = ""
            for para in paragraphs:
                # Split oversized single paragraphs on sentence boundaries
                if len(para) > max_chars:
                    if current_chunk.strip():
                        chunks.append({
                            "header": header,
                            "text": f"[Paper: \"{paper_title}\"] [Section: {header}]\n\n{current_chunk.strip()}"
                        })
                        current_chunk = ""
                    # Split long paragraph by sentences (period + space)
                    sentences = re.split(r'(?<=\. )', para)
                    sent_chunk = ""
                    for sent in sentences:
                        if len(sent_chunk) + len(sent) > max_chars and sent_chunk:
                            chunks.append({
                                "header": header,
                                "text": f"[Paper: \"{paper_title}\"] [Section: {header}]\n\n{sent_chunk.strip()}"
                            })
                            sent_chunk = sent
                        else:
                            sent_chunk += sent
                    if sent_chunk.strip():
                        current_chunk = sent_chunk
                elif len(current_chunk) + len(para) + 2 > max_chars and current_chunk:
                    chunks.append({
                        "header": header,
                        "text": f"[Paper: \"{paper_title}\"] [Section: {header}]\n\n{current_chunk.strip()}"
                    })
                    current_chunk = para
                else:
                    current_chunk += "\n\n" + para if current_chunk else para
            if current_chunk.strip():
                chunks.append({
                    "header": header,
                    "text": f"[Paper: \"{paper_title}\"] [Section: {header}]\n\n{current_chunk.strip()}"
                })
        else:
            chunks.append({
                "header": header,
                "text": f"[Paper: \"{paper_title}\"] [Section: {header}]\n\n{section}"
            })

    return chunks


def cmd_ingest_paper(args):
    """Ingest a markdown file into paper_chunks table with embeddings."""
    md_path = Path(args.file)
    if not md_path.exists():
        print(f"ERROR: File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    md_text = md_path.read_text(encoding="utf-8")
    title = args.title or md_path.stem.replace("_", " ").replace("-", " ")
    tags = args.tags or []

    conn = get_conn()

    # Check for existing paper record
    existing_paper = safe_query(conn,
        "SELECT id, title FROM papers WHERE source_file = %s", (str(md_path),))

    if existing_paper:
        paper_id = existing_paper[0]["id"]
        if args.replace:
            print(f"Replacing existing paper: {existing_paper[0]['title']}")
        else:
            chunk_count = safe_query(conn,
                "SELECT count(*) AS cnt FROM paper_chunks WHERE paper_id = %s", (paper_id,))
            cnt = chunk_count[0]["cnt"] if chunk_count else 0
            print(f"WARNING: '{existing_paper[0]['title']}' already ingested ({cnt} chunks). "
                  f"Use --replace to re-ingest.")
            conn.close()
            return
    else:
        paper_id = None  # will create below inside transaction

    chunks = chunk_markdown_by_section(md_text, title, max_chars=args.max_chars)
    if not chunks:
        print("No chunks generated — check markdown content.")
        conn.close()
        return

    print(f"Ingesting '{title}' from {md_path.name}: {len(chunks)} chunks")

    # Embed all chunks first (before touching DB), so failures don't leave partial state
    embedded = []
    for i, chunk in enumerate(chunks):
        try:
            vec = embed_text(chunk["text"])
        except Exception as e:
            print(f"  WARNING: embedding failed for chunk {i}: {e}", file=sys.stderr)
            vec = None
        embedded.append((chunk, vec))

    # Use explicit transaction for atomicity (delete old + insert new)
    conn.autocommit = False
    try:
        cur = conn.cursor()

        if existing_paper and args.replace:
            cur.execute("DELETE FROM paper_chunks WHERE paper_id = %s", (paper_id,))
            if tags:
                cur.execute("UPDATE papers SET tags = %s WHERE id = %s", (tags, paper_id))
            if args.author:
                cur.execute("UPDATE papers SET author = %s WHERE id = %s", (args.author, paper_id))
            if args.doc_type:
                cur.execute("UPDATE papers SET doc_type = %s WHERE id = %s", (args.doc_type, paper_id))
        elif paper_id is None:
            cur.execute("""
                INSERT INTO papers (title, source_file, tags, author, doc_type)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (title, str(md_path), tags or None, args.author, args.doc_type))
            paper_id = cur.fetchone()[0]

        for chunk, vec in embedded:
            cur.execute("""
                INSERT INTO paper_chunks (paper_title, section_header, chunk_text, source_file, embedding, paper_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (title, chunk["header"], chunk["text"],
                  str(md_path), vec.tolist() if vec is not None else None, paper_id))

        conn.commit()
        print(f"  Stored {len(embedded)} chunks in paper_chunks table.")
    except Exception as e:
        conn.rollback()
        print(f"  ERROR: ingestion failed, rolled back: {e}", file=sys.stderr)
    finally:
        conn.autocommit = True
        conn.close()


def _recall_papers_graph(query_text, mode="hybrid", json_output=False):
    """Query LightRAG knowledge graph for paper relationships."""
    import asyncio
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from lightrag_kb import query as lightrag_query
        result = asyncio.run(lightrag_query(query_text, mode=mode))
    except Exception as e:
        print(f"[graph] ERROR: {e}", file=sys.stderr)
        return

    if not result or "do not have enough" in result.lower():
        print("[graph] No graph results for this query.")
        return

    if json_output:
        print(json.dumps({"source": "lightrag_graph", "mode": mode, "response": result}, indent=2))
    else:
        print(f"--- Knowledge Graph ({mode}) ---")
        print(result)


def cmd_recall_papers(args):
    """Semantic search across paper_chunks with optional graph KB query."""
    # Graph-only mode
    if args.graph_only:
        _recall_papers_graph(args.query, mode=args.graph_mode, json_output=args.json)
        return

    # Vector search
    conn = get_conn()

    try:
        query_vec = embed_text(args.query)
    except Exception as e:
        print(f"ERROR: embedding failed: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    filters = ["pc.embedding IS NOT NULL"]
    params = []
    join_papers = False

    if args.paper:
        filters.append("pc.paper_title ILIKE %(paper)s")
        params.append(("paper", f"%{args.paper}%"))

    if args.tags:
        join_papers = True
        filters.append("p.tags @> %(tags)s::text[]")
        params.append(("tags", args.tags))

    where = " AND ".join(filters)
    limit = args.limit or 5

    # Build named params dict
    param_dict = {"vec": query_vec.tolist(), "limit": limit}
    for key, val in params:
        param_dict[key] = val

    join_clause = "JOIN papers p ON p.id = pc.paper_id" if join_papers else ""

    rows = safe_query(conn, f"""
        SELECT pc.id, pc.paper_title, pc.section_header, pc.chunk_text, pc.created_at,
               1 - (pc.embedding <=> %(vec)s::vector) AS similarity
        FROM paper_chunks pc
        {join_clause}
        WHERE {where}
        ORDER BY pc.embedding <=> %(vec)s::vector
        LIMIT %(limit)s
    """, param_dict)

    if not rows:
        print("No matching paper chunks found.")
    elif args.json:
        for r in rows:
            r["similarity"] = float(r["similarity"]) if r["similarity"] is not None else None
            r["created_at"] = r.get("created_at", "").isoformat() if r.get("created_at") else None
        print(json.dumps(rows, indent=2, default=str))
    else:
        for r in rows:
            sim = f"{float(r['similarity']):.3f}"
            title_short = r['paper_title'][:40]
            section = r['section_header'] or "?"
            print(f"[{r['id']}] (sim:{sim}) [{title_short}] §{section}")

    conn.close()

    # Append graph results if --graph flag is set
    if args.graph:
        print()
        _recall_papers_graph(args.query, mode=args.graph_mode, json_output=args.json)


# ---------------------------------------------------------------------------
# audit-memories - Dedup merge + stale assessment
# ---------------------------------------------------------------------------
def cmd_audit_memories(args):
    conn = get_conn()

    if args.rewrite_all:
        _audit_rewrite_all(conn, batch_size=args.batch_size, offset=args.offset)
    elif args.apply_rewrites:
        _audit_apply_rewrites(conn, args.apply_rewrites, dry_run=args.dry_run)
    elif args.merge_duplicates:
        _audit_merge_duplicates(conn, threshold=args.threshold, dry_run=args.dry_run)
    elif args.assess_stale:
        _audit_assess_stale(conn)
    elif args.delete_ids:
        _audit_delete_ids(conn, args.delete_ids, dry_run=args.dry_run)
    else:
        print("Specify --rewrite-all, --apply-rewrites, --merge-duplicates, --assess-stale, or --delete-ids")

    conn.close()


def _audit_merge_duplicates(conn, threshold=0.85, dry_run=False):
    # Step 1: find all duplicate pairs above threshold
    rows = safe_query(conn, """
        SELECT a.id as id1, b.id as id2,
               1 - (a.embedding <=> b.embedding) as similarity
        FROM memories a
        JOIN memories b ON a.id < b.id
        WHERE a.superseded_by IS NULL AND b.superseded_by IS NULL
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND 1 - (a.embedding <=> b.embedding) > %s
        ORDER BY similarity DESC
    """, (threshold,))

    if not rows:
        print(f"No duplicate pairs found above threshold {threshold}")
        return 0, []

    print(f"Found {len(rows)} duplicate pairs above {threshold} similarity\n")

    # Step 2: union-find clustering
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in rows:
        union(r["id1"], r["id2"])

    # build clusters
    clusters = {}
    all_ids = set()
    for r in rows:
        all_ids.add(r["id1"])
        all_ids.add(r["id2"])
    for mid in all_ids:
        root = find(mid)
        clusters.setdefault(root, set()).add(mid)

    # collect max similarity per pair for reporting
    pair_sims = {}
    for r in rows:
        pair_sims[(r["id1"], r["id2"])] = float(r["similarity"])

    print(f"Clustered into {len(clusters)} groups ({len(all_ids)} unique memories)\n")

    total_superseded = 0
    merge_actions = []
    for cluster_ids in clusters.values():
        ids_list = sorted(cluster_ids)

        # Step 3: pick winner
        winner_rows = safe_query(conn, """
            SELECT id, importance,
                   startup_count + inject_count + recall_count as total_exp,
                   updated_at, LEFT(content, 100) as preview
            FROM memories WHERE id = ANY(%s)
            ORDER BY importance DESC,
                     startup_count + inject_count + recall_count DESC,
                     updated_at DESC
        """, (ids_list,))

        winner = winner_rows[0]
        loser_ids = [r["id"] for r in winner_rows[1:]]

        # find max similarity in this cluster
        max_sim = 0.0
        for i, a in enumerate(ids_list):
            for b in ids_list[i + 1:]:
                key = (min(a, b), max(a, b))
                max_sim = max(max_sim, pair_sims.get(key, 0.0))

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}[MERGE] Kept #{winner['id']} (imp:{winner['importance']}), "
              f"superseded {['#' + str(x) for x in loser_ids]} (sim={max_sim:.3f})")
        print(f"  Winner: {winner['preview']}")
        for r in winner_rows[1:]:
            print(f"  Loser:  #{r['id']}: {r['preview']}")
        print()

        if not dry_run and loser_ids:
            # Step 4: supersede losers
            safe_execute(conn, """
                UPDATE memories SET superseded_by = %(winner)s
                WHERE id = ANY(%(losers)s)
            """, {"winner": winner["id"], "losers": loser_ids})
            # Step 5: fix dangling chains
            safe_execute(conn, """
                UPDATE memories SET superseded_by = %(winner)s
                WHERE superseded_by = ANY(%(losers)s)
            """, {"winner": winner["id"], "losers": loser_ids})
            total_superseded += len(loser_ids)

        merge_actions.append({
            "action": "MERGE", "winner_id": winner["id"],
            "loser_ids": loser_ids, "similarity": round(max_sim, 3)
        })

    if dry_run:
        print(f"DRY RUN: Would merge {len(clusters)} clusters, "
              f"superseding {sum(len(c) - 1 for c in clusters.values())} memories")
    else:
        print(f"Merged {len(clusters)} clusters, superseded {total_superseded} memories")

    return total_superseded, merge_actions


def _audit_assess_stale(conn):
    rows = safe_query(conn, """
        SELECT id, category, domain, importance, created_at,
               LEFT(content, 120) as preview
        FROM memories
        WHERE importance <= 3
          AND startup_count = 0 AND inject_count = 0 AND recall_count = 0
          AND created_at < NOW() - INTERVAL '14 days'
          AND superseded_by IS NULL
        ORDER BY importance ASC, created_at ASC
    """)

    if not rows:
        print("No stale low-value memories found.")
        return

    print("STALE MEMORY ASSESSMENT")
    print("=" * 50)
    for r in rows:
        date_str = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "?"
        print(f"[id={r['id']}] [{r['category']}|{r['domain']}|imp:{r['importance']}|{date_str}] "
              f"{r['preview']}")
    print(f"\nTotal: {len(rows)} memories for review.")


def _audit_delete_ids(conn, ids, dry_run=False):
    # Preview what will be deleted
    rows = safe_query(conn, """
        SELECT id, category, importance, LEFT(content, 100) as preview
        FROM memories WHERE id = ANY(%s) AND superseded_by IS NULL
    """, (ids,))

    if not rows:
        print("No active memories found with those IDs.")
        return

    prefix = "[DRY RUN] " if dry_run else ""
    for r in rows:
        print(f"{prefix}[DELETE] #{r['id']} [{r['category']}|imp:{r['importance']}] {r['preview']}")

    if not dry_run:
        matched_ids = [r["id"] for r in rows]
        # Clear FK references before deleting
        safe_execute(conn, "DELETE FROM memory_exposures WHERE memory_id = ANY(%s)", (matched_ids,))
        safe_execute(conn, "UPDATE memories SET superseded_by = NULL WHERE superseded_by = ANY(%s)", (matched_ids,))
        safe_execute(conn, "DELETE FROM memories WHERE id = ANY(%s)", (matched_ids,))
        print(f"\nDeleted {len(rows)} memories.")
    else:
        print(f"\nDRY RUN: Would delete {len(rows)} memories.")


def _audit_rewrite_all(conn, batch_size=50, offset=0):
    """Export a batch of memories for Sonnet rewriting."""
    rows = safe_query(conn, """
        SELECT id, content, category, domain, importance, tags
        FROM memories
        WHERE superseded_by IS NULL
        ORDER BY id ASC
        LIMIT %s OFFSET %s
    """, (batch_size, offset))

    if not rows:
        print("No memories found at this offset.")
        return

    print(f"REWRITE BATCH (offset={offset}, count={len(rows)})")
    print("=" * 60)
    for r in rows:
        tags_str = ", ".join(r["tags"]) if r.get("tags") else ""
        print(f"[ID={r['id']}] [cat={r['category']}|dom={r['domain']}|imp={r['importance']}]")
        if tags_str:
            print(f"TAGS: {tags_str}")
        print(f"CONTENT: {r['content']}")
        print(f"ACTION: ___")  # KEEP / REWRITE / DELETE
        print(f"NEW_CONTENT: ___")  # only if REWRITE
        print(f"NEW_IMP: ___")  # only if REWRITE
        print()
    print(f"Total in batch: {len(rows)}")
    if len(rows) == batch_size:
        print(f"More available. Run with --offset {offset + batch_size}")


def _audit_apply_rewrites(conn, json_file, dry_run=False):
    """Apply rewrite decisions from a JSON file."""
    with open(json_file, encoding="utf-8") as f:
        rewrites = json.load(f)

    prefix = "[DRY RUN] " if dry_run else ""
    counts = {"keep": 0, "rewrite": 0, "delete": 0, "error": 0}
    for entry in rewrites:
        mid = entry["id"]
        action = entry["action"]  # "keep", "rewrite", "delete"
        counts[action] = counts.get(action, 0) + 1

        if action == "delete":
            print(f"{prefix}[DELETE] #{mid}")
            if not dry_run:
                safe_execute(conn, "DELETE FROM memory_exposures WHERE memory_id = %s", (mid,))
                safe_execute(conn, "UPDATE memories SET superseded_by = NULL WHERE superseded_by = %s", (mid,))
                safe_execute(conn, "DELETE FROM memories WHERE id = %s", (mid,))

        elif action == "rewrite":
            new_content = entry["new_content"]
            new_imp = entry.get("new_importance")
            new_cat = entry.get("new_category")
            print(f"{prefix}[REWRITE] #{mid}: {new_content[:80]}...")
            if not dry_run:
                updates = ["content = %s"]
                params = [new_content]
                if new_imp is not None:
                    updates.append("importance = %s")
                    params.append(new_imp)
                if new_cat is not None:
                    updates.append("category = %s")
                    params.append(new_cat)
                # Re-embed with new content
                try:
                    vec = embed_text(new_content)
                    updates.append("embedding = %s")
                    params.append(vec.tolist())
                except Exception as e:
                    print(f"  WARNING: embedding failed for #{mid}, storing without vector: {e}",
                          file=sys.stderr)
                    counts["error"] = counts.get("error", 0) + 1
                params.append(mid)
                safe_execute(conn, f"""
                    UPDATE memories SET {', '.join(updates)} WHERE id = %s
                """, params)

        else:  # keep — still apply importance/category adjustments if present
            new_imp = entry.get("new_importance")
            new_cat = entry.get("new_category")
            if new_imp is not None or new_cat is not None:
                updates = []
                params = []
                if new_imp is not None:
                    updates.append("importance = %s")
                    params.append(new_imp)
                if new_cat is not None:
                    updates.append("category = %s")
                    params.append(new_cat)
                if updates and not dry_run:
                    params.append(mid)
                    safe_execute(conn, f"""
                        UPDATE memories SET {', '.join(updates)} WHERE id = %s
                    """, params)
                print(f"{prefix}[KEEP+UPDATE] #{mid}: imp={new_imp}, cat={new_cat}")

    verb = "Would apply" if dry_run else "Applied"
    summary = (f"\n{verb} {len(rewrites)} decisions: "
               f"{counts['keep']} kept, {counts['rewrite']} rewritten, {counts['delete']} deleted.")
    if counts["error"]:
        summary += f" ({counts['error']} embedding errors)"
    print(summary)


# ---------------------------------------------------------------------------
# Phase 6: Memory Organizing Agent
# ---------------------------------------------------------------------------

def _organize_decay_completed(conn, dry_run=False):
    """A3: Decay importance of memories from completed/archived projects."""
    rows = safe_query(conn, """
        SELECT m.id, m.importance, p.name, p.status
        FROM memories m
        JOIN projects p ON m.project_id = p.id
        WHERE m.superseded_by IS NULL
          AND p.status IN ('completed', 'archived')
          AND m.importance > 4
          AND m.importance < 9
          AND m.recall_count = 0
    """)
    actions = []
    for r in rows:
        old_imp = r["importance"]
        new_imp = max(old_imp - 2, 4)
        if new_imp == old_imp:
            continue
        actions.append({"type": "decay_completed", "id": r["id"],
                        "old_importance": old_imp, "new_importance": new_imp,
                        "project": r["name"], "project_status": r["status"]})
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}[DECAY] #{r['id']} imp:{old_imp}→{new_imp} "
              f"(project={r['name']}, status={r['status']})")
        if not dry_run:
            safe_execute(conn, "UPDATE memories SET importance = %s WHERE id = %s",
                         (new_imp, r["id"]))
    return actions


def _organize_find_topic_clusters(conn, lo=0.70, hi=0.85, max_clusters=20):
    """B1 prep: Find memory pairs with cosine 0.70-0.85 for topic consolidation.
    Uses HNSW-indexed LATERAL join (O(n*K)) instead of quadratic self-join."""
    rows = safe_query(conn, """
        SELECT DISTINCT ON (LEAST(a.id, b.id), GREATEST(a.id, b.id))
               LEAST(a.id, b.id) as id1,
               GREATEST(a.id, b.id) as id2,
               1 - (a.embedding <=> b.embedding) as similarity
        FROM memories a
        CROSS JOIN LATERAL (
            SELECT id, embedding FROM memories b
            WHERE b.id != a.id
              AND b.superseded_by IS NULL
              AND b.embedding IS NOT NULL
            ORDER BY b.embedding <=> a.embedding
            LIMIT 50
        ) b
        WHERE a.superseded_by IS NULL
          AND a.embedding IS NOT NULL
          AND 1 - (a.embedding <=> b.embedding) > %s
          AND 1 - (a.embedding <=> b.embedding) <= %s
        ORDER BY LEAST(a.id, b.id), GREATEST(a.id, b.id),
                 (1 - (a.embedding <=> b.embedding)) DESC
    """, (lo, hi))
    if not rows:
        return []

    # Union-find clustering
    parent = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in rows:
        union(r["id1"], r["id2"])

    clusters = {}
    all_ids = set()
    for r in rows:
        all_ids.add(r["id1"])
        all_ids.add(r["id2"])
    for mid in all_ids:
        root = find(mid)
        clusters.setdefault(root, set()).add(mid)

    # Cap at max_clusters, take largest first
    sorted_clusters = sorted(clusters.values(), key=len, reverse=True)[:max_clusters]
    return [sorted(c) for c in sorted_clusters if len(c) >= 2]


def _organize_find_contradictions(conn, sim_lo=0.80, sim_hi=0.95, max_pairs=20):
    """B2 prep: Find pairs with high embedding sim but different text content.
    Uses HNSW-indexed LATERAL join (O(n*K)) instead of quadratic self-join."""
    rows = safe_query(conn, """
        SELECT DISTINCT ON (LEAST(a.id, b.id), GREATEST(a.id, b.id))
               LEAST(a.id, b.id) as id1,
               GREATEST(a.id, b.id) as id2,
               1 - (a.embedding <=> b.embedding) as embedding_sim,
               similarity(a.content, b.content) as text_sim,
               a.content as content1, b.content as content2,
               a.created_at as created1, b.created_at as created2
        FROM memories a
        CROSS JOIN LATERAL (
            SELECT id, content, embedding, created_at FROM memories b
            WHERE b.id != a.id
              AND b.superseded_by IS NULL
              AND b.embedding IS NOT NULL
            ORDER BY b.embedding <=> a.embedding
            LIMIT 50
        ) b
        WHERE a.superseded_by IS NULL
          AND a.embedding IS NOT NULL
          AND 1 - (a.embedding <=> b.embedding) > %s
          AND 1 - (a.embedding <=> b.embedding) <= %s
          AND similarity(a.content, b.content) < 0.80
        ORDER BY LEAST(a.id, b.id), GREATEST(a.id, b.id),
                 (1 - (a.embedding <=> b.embedding)) DESC
        LIMIT %s
    """, (sim_lo, sim_hi, max_pairs))
    return rows or []


def _organize_find_stale(conn, age_days=60, max_imp=6, limit=50):
    """B3 prep: Find old, low-importance, rarely-used memories for staleness review."""
    rows = safe_query(conn, """
        SELECT id, content, category, domain, importance, created_at,
               inject_count, recall_count
        FROM memories
        WHERE superseded_by IS NULL
          AND importance <= %s
          AND recall_count = 0
          AND created_at < NOW() - (%s || ' days')::INTERVAL
        ORDER BY importance ASC, inject_count ASC
        LIMIT %s
    """, (max_imp, str(age_days), limit))
    return rows or []


def _organize_llm_call(prompt_text, json_schema=None):
    """Call Claude via claude -p for Phase B LLM operations. Returns parsed JSON or None.

    Args:
        prompt_text: The prompt to send to the model.
        json_schema: If provided (dict or JSON string), uses --output-format json
                     with --json-schema for constrained decoding. Otherwise falls
                     back to text mode with manual JSON extraction.
    """
    import shutil
    import time as _time
    claude_path = shutil.which("claude")
    if not claude_path:
        print("ERROR: claude CLI not found in PATH", file=sys.stderr)
        return None

    env = os.environ.copy()
    env["MEMORY_HELPER_SUBPROCESS"] = "1"

    empty_mcp = str(LOG_DIR / "_empty_mcp.json")
    if not os.path.exists(empty_mcp):
        with open(empty_mcp, "w") as _f:
            json.dump({"mcpServers": {}}, _f)

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    cmd = [claude_path, "-p", "-",
           "--no-session-persistence",
           "--model", "opus",
           "--dangerously-skip-permissions",
           "--mcp-config", empty_mcp,
           "--strict-mcp-config"]

    if json_schema:
        schema_str = json.dumps(json_schema) if isinstance(json_schema, dict) else json_schema
        cmd.extend(["--output-format", "json", "--json-schema", schema_str])
    else:
        cmd.extend(["--output-format", "text"])

    max_retries = 2
    for attempt in range(max_retries):
        try:
            proc = subprocess.run(
                cmd,
                input=prompt_text,
                capture_output=True, text=True, encoding="utf-8",
                timeout=600, startupinfo=startupinfo, env=env,
            )
            if proc.returncode != 0:
                print(f"WARNING: LLM call failed (rc={proc.returncode}, attempt {attempt+1}/{max_retries}): "
                      f"{proc.stderr[:500]}", file=sys.stderr)
                if attempt < max_retries - 1:
                    _time.sleep(10)
                    continue
                return None

            response = proc.stdout.strip()
            print(f"    [DIAG] Response length: {len(response)} chars (attempt {attempt+1})")

            parsed = _parse_llm_response(response, json_schema)
            if parsed is not None:
                return parsed

            print(f"    WARNING: Parse failed (attempt {attempt+1}/{max_retries})", file=sys.stderr)
            if attempt < max_retries - 1:
                _time.sleep(10)
                continue
            return None

        except subprocess.TimeoutExpired:
            print(f"WARNING: LLM call timed out after 600s (attempt {attempt+1}/{max_retries})",
                  file=sys.stderr)
            if attempt < max_retries - 1:
                _time.sleep(10)
                continue
            return None
        except Exception as e:
            print(f"WARNING: LLM call error (attempt {attempt+1}/{max_retries}): {e}",
                  file=sys.stderr)
            if attempt < max_retries - 1:
                _time.sleep(10)
                continue
            return None
    return None


def _parse_llm_response(response, json_schema=None):
    """Parse a claude -p response into a dict. Shared by single and parallel calls."""
    if not response:
        return None

    if json_schema:
        try:
            envelope = json.loads(response)
        except json.JSONDecodeError as e:
            print(f"    WARNING: JSON envelope parse failed: {e}\n"
                  f"      Head: {response[:500]}\n      Tail: {response[-500:]}", file=sys.stderr)
            return None

        if isinstance(envelope, dict) and "structured_output" in envelope:
            result = envelope["structured_output"]
            if isinstance(result, dict):
                return result
            if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
                print("    [DIAG] Unwrapped list-wrapped structured_output")
                return result[0]
            print(f"    WARNING: structured_output has unexpected type: {type(result).__name__}",
                  file=sys.stderr)

        # Fallback: envelope itself is the result
        if isinstance(envelope, dict) and any(k in envelope for k in
                                               ("merges", "contradictions", "stale", "improvements")):
            return envelope

        print(f"    WARNING: No structured_output in envelope. Keys: "
              f"{list(envelope.keys()) if isinstance(envelope, dict) else type(envelope).__name__}\n"
              f"      is_error: {envelope.get('is_error', 'N/A') if isinstance(envelope, dict) else 'N/A'}",
              file=sys.stderr)
        return None

    # Text mode: extract JSON
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        idx_start = response.find(start_char)
        idx_end = response.rfind(end_char)
        if idx_start >= 0 and idx_end > idx_start:
            try:
                parsed = json.loads(response[idx_start:idx_end + 1])
                if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                    return parsed[0]
                return parsed
            except json.JSONDecodeError:
                continue
    print(f"    WARNING: Could not extract JSON from text response.\n"
          f"      Length: {len(response)} chars\n"
          f"      Head: {response[:500]}\n      Tail: {response[-500:]}", file=sys.stderr)
    return None


def _kill_process_tree(proc):
    """Kill a process and its entire child tree on Windows."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=10,
        )
    except Exception:
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _organize_parallel_llm_calls(calls, max_parallel=10):
    """Spawn multiple claude -p subprocesses in parallel and collect results.

    Args:
        calls: list of dicts with keys:
            - label: str identifier for this call (e.g. "B1_batch0", "B2")
            - prompt: str prompt text
            - json_schema: dict or None
            - timeout: int seconds (default 600)
        max_parallel: cap on concurrent processes

    Returns:
        dict mapping label -> parsed JSON result (or None on failure)
    """
    import shutil
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    claude_path = shutil.which("claude")
    if not claude_path:
        print("ERROR: claude CLI not found in PATH", file=sys.stderr)
        return {c["label"]: None for c in calls}

    if not calls:
        return {}

    # Process in batches if exceeding max_parallel
    if len(calls) > max_parallel:
        print(f"    [PARALLEL] {len(calls)} calls exceed max_parallel={max_parallel}, "
              f"processing in batches")
        results = {}
        for batch_start in range(0, len(calls), max_parallel):
            batch = calls[batch_start:batch_start + max_parallel]
            batch_results = _organize_parallel_llm_calls(batch, max_parallel=max_parallel)
            results.update(batch_results)
        return results

    env = os.environ.copy()
    env["MEMORY_HELPER_SUBPROCESS"] = "1"

    empty_mcp = str(LOG_DIR / "_empty_mcp.json")
    if not os.path.exists(empty_mcp):
        with open(empty_mcp, "w") as _f:
            json.dump({"mcpServers": {}}, _f)

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    max_retries = 2

    def _run_single_call(call):
        """Run one LLM call with retry logic. Returns (label, result)."""
        label = call["label"]
        prompt = call["prompt"]
        schema = call.get("json_schema")
        timeout = call.get("timeout", 600)

        cmd = [claude_path, "-p", "-",
               "--no-session-persistence",
               "--model", "opus",
               "--dangerously-skip-permissions",
               "--mcp-config", empty_mcp,
               "--strict-mcp-config"]

        if schema:
            schema_str = json.dumps(schema) if isinstance(schema, dict) else schema
            cmd.extend(["--output-format", "json", "--json-schema", schema_str])
        else:
            cmd.extend(["--output-format", "text"])

        for attempt in range(max_retries):
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                    startupinfo=startupinfo, env=env,
                )
                proc.stdin.write(prompt)
                proc.stdin.close()
                if attempt == 0:
                    print(f"    [PARALLEL] Launched {label} ({len(prompt)} chars)")

                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill_process_tree(proc)
                    print(f"    WARNING: {label} timed out after {timeout}s "
                          f"(attempt {attempt+1}/{max_retries})", file=sys.stderr)
                    if attempt < max_retries - 1:
                        _time.sleep(10)
                        continue
                    return label, None

                if proc.returncode != 0:
                    print(f"    WARNING: {label} failed (rc={proc.returncode}, "
                          f"attempt {attempt+1}/{max_retries}): {stderr[:300]}", file=sys.stderr)
                    if attempt < max_retries - 1:
                        _time.sleep(10)
                        continue
                    return label, None

                response = stdout.strip()
                print(f"    [PARALLEL] {label} returned {len(response)} chars")

                parsed = _parse_llm_response(response, schema)
                if parsed is not None:
                    return label, parsed

                print(f"    WARNING: {label} parse failed (attempt {attempt+1}/{max_retries})",
                      file=sys.stderr)
                if attempt < max_retries - 1:
                    _time.sleep(10)
                    continue
                return label, None

            except Exception as e:
                print(f"    WARNING: {label} error (attempt {attempt+1}/{max_retries}): {e}",
                      file=sys.stderr)
                if attempt < max_retries - 1:
                    _time.sleep(10)
                    continue
                return label, None

        return label, None

    # Launch all calls in parallel using ThreadPoolExecutor
    live_count = len(calls)
    print(f"    [PARALLEL] Dispatching {live_count} processes...")

    results = {}
    with ThreadPoolExecutor(max_workers=live_count) as executor:
        futures = {executor.submit(_run_single_call, call): call["label"] for call in calls}
        for future in as_completed(futures):
            label, result = future.result()
            results[label] = result

    succeeded = sum(1 for v in results.values() if v is not None)
    print(f"    [PARALLEL] Done: {succeeded}/{live_count} succeeded")
    return results


def _organize_run_phase_b(conn, max_clusters=20, dry_run=False, enabled_steps=None, run_id=None):
    """Phase B: LLM-assisted consolidation — topic clusters, contradictions, staleness."""
    if enabled_steps is None:
        enabled_steps = {"B1", "B2", "B3", "B4"}
    actions = []

    # Gather data for all B steps, then dispatch parallel Opus calls
    clusters = _organize_find_topic_clusters(conn, max_clusters=max_clusters) if "B1" in enabled_steps else []
    contradictions = _organize_find_contradictions(conn) if "B2" in enabled_steps else []
    stale = _organize_find_stale(conn) if "B3" in enabled_steps else []
    improvement_candidates = []
    if "B4" in enabled_steps:
        improvement_candidates = safe_query(conn, """
            SELECT id, content, category, domain, importance, created_at,
                   inject_count, recall_count
            FROM memories
            WHERE superseded_by IS NULL
              AND category IN ('bug', 'pattern', 'tool_usage')
              AND importance >= 5
            ORDER BY created_at DESC
            LIMIT 80
        """) or []

    clusters_consolidated = 0
    contradictions_resolved = 0
    stale_assessed = 0
    improvements_flagged = 0

    # Build cluster data
    cluster_blocks = []
    valid_clusters = []
    if clusters:
        print(f"\n--- B1: Topic Consolidation ({len(clusters)} clusters) ---")
        for cluster_ids in clusters:
            mems = safe_query(conn, """
                SELECT id, content, category, domain, importance
                FROM memories WHERE id = ANY(%s) ORDER BY importance DESC
            """, (cluster_ids,))
            if not mems or len(mems) < 2:
                continue
            valid_clusters.append(cluster_ids)
            mems_text = "\n".join(
                f"  [ID={m['id']}] [cat={m['category']}|dom={m['domain']}|imp={m['importance']}] {m['content']}"
                for m in mems)
            cluster_blocks.append(f"CLUSTER {len(valid_clusters)} (IDs: {cluster_ids}):\n{mems_text}")
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"{prefix}  Cluster {len(valid_clusters)}: {cluster_ids}")

    if contradictions:
        print(f"\n--- B2: Contradiction Detection ({len(contradictions)} pairs) ---")
        for pair in contradictions:
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"{prefix}  #{pair['id1']} vs #{pair['id2']} "
                  f"(embed_sim={float(pair['embedding_sim']):.2f}, text_sim={float(pair['text_sim']):.2f})")

    if stale:
        print(f"\n--- B3: Staleness Assessment ({len(stale)} candidates) ---")
        if dry_run:
            for m in stale:
                print(f"[DRY RUN] [STALE?] #{m['id']} imp:{m['importance']} "
                      f"age={m['created_at'].strftime('%Y-%m-%d')} {m['content'][:80]}")

    if improvement_candidates:
        print(f"\n--- B4: Improvement Opportunities ({len(improvement_candidates)} candidates) ---")

    # Dry-run: just count, no LLM call
    if dry_run:
        for cids in valid_clusters:
            actions.append({"type": "cluster_preview", "ids": cids})
        clusters_consolidated = len(valid_clusters)
        for pair in contradictions:
            actions.append({"type": "contradiction_preview", "id1": pair["id1"], "id2": pair["id2"]})
        contradictions_resolved = len(contradictions)
        for m in stale:
            actions.append({"type": "stale_preview", "id": m["id"]})
        stale_assessed = len(stale)
        if improvement_candidates:
            actions.append({"type": "improvement_preview", "candidate_count": len(improvement_candidates)})
            improvements_flagged = len(improvement_candidates)
    else:
        # Build per-section prompts and dispatch in parallel
        B1_BATCH_SIZE = 7
        B4_BATCH_SIZE = 30

        parallel_calls = []

        # --- B1: Cluster merge prompts (batched) ---
        if valid_clusters:
            for batch_idx in range(0, len(cluster_blocks), B1_BATCH_SIZE):
                batch = cluster_blocks[batch_idx:batch_idx + B1_BATCH_SIZE]
                prompt = (
                    "You are a memory maintenance agent. Analyze these memory clusters.\n\n"
                    "## MERGE CLUSTERS\n"
                    "Each cluster contains semantically related memories. Within each cluster:\n"
                    "- Find groups of GENUINE DUPLICATES (same fact stated differently).\n"
                    "- For each duplicate group, pick one winner and mark the rest as losers.\n"
                    "- A cluster may yield MULTIPLE merge groups, or ZERO if no true duplicates exist.\n"
                    "- Do NOT force-merge memories that cover different aspects of the same topic.\n"
                    "- When in doubt, do NOT merge — keeping distinct memories is safer than losing info.\n\n"
                    + "\n\n".join(batch)
                    + '\n\n## OUTPUT FORMAT\n'
                    'Return a JSON object:\n'
                    '{"merges": [{"cluster": <n>, "winner_id": <id>, "loser_ids": [<ids>], '
                    '"new_content": "<merged text, max 100 words>", "new_importance": <1-10>, '
                    '"rationale": "<why>"}]}\n'
                    'Multiple entries per cluster OK. Omit cluster if no true dupes. '
                    'Return {"merges": []} if no duplicates found.'
                )
                parallel_calls.append({
                    "label": f"B1_batch{batch_idx // B1_BATCH_SIZE}",
                    "prompt": prompt,
                    "json_schema": {"type": "object", "properties": {
                        "merges": {"type": "array", "items": {"type": "object"}}}},
                    "timeout": 600,
                })

        # --- B2: Contradiction detection (single call) ---
        if contradictions:
            pairs_text = "\n\n".join(
                f"PAIR {i+1}:\n"
                f"  MEMORY A [ID={p['id1']}, created {p['created1'].strftime('%Y-%m-%d')}]: \"{p['content1']}\"\n"
                f"  MEMORY B [ID={p['id2']}, created {p['created2'].strftime('%Y-%m-%d')}]: \"{p['content2']}\""
                for i, p in enumerate(contradictions))
            prompt = (
                "You are a memory maintenance agent. Analyze these memory pairs for contradictions.\n\n"
                "## CONTRADICTION DETECTION\n"
                "For each pair, decide if they contradict each other.\n\n"
                + pairs_text
                + '\n\n## OUTPUT FORMAT\n'
                'Return a JSON object:\n'
                '{"contradictions": [{"pair": <n>, "contradicts": "YES|NO|PARTIAL", '
                '"keep_id": <id>, "supersede_id": <id or null>, "rationale": "<why>"}]}\n'
                'For NO contradictions, set supersede_id to null.'
            )
            parallel_calls.append({
                "label": "B2",
                "prompt": prompt,
                "json_schema": {"type": "object", "properties": {
                    "contradictions": {"type": "array", "items": {"type": "object"}}}},
                "timeout": 600,
            })

        # --- B3: Staleness assessment (single call) ---
        if stale:
            stale_text = "\n".join(
                f"[ID={m['id']}] [cat={m['category']}|dom={m['domain']}|imp={m['importance']}|"
                f"injects={m['inject_count']}|recalls={m['recall_count']}|"
                f"age={m['created_at'].strftime('%Y-%m-%d')}] {m['content']}"
                for m in stale)
            prompt = (
                "You are a memory maintenance agent. Assess these low-usage memories for staleness.\n\n"
                "## STALENESS ASSESSMENT\n"
                "Decide: KEEP, DEMOTE (reduce importance), or DELETE. "
                "When in doubt, DEMOTE. Never reduce importance below 3.\n\n"
                + stale_text
                + '\n\n## OUTPUT FORMAT\n'
                'Return a JSON object:\n'
                '{"stale": [{"id": <id>, "action": "KEEP|DEMOTE|DELETE", '
                '"new_importance": <int>, "rationale": "<why>"}]}'
            )
            parallel_calls.append({
                "label": "B3",
                "prompt": prompt,
                "json_schema": {"type": "object", "properties": {
                    "stale": {"type": "array", "items": {"type": "object"}}}},
                "timeout": 600,
            })

        # --- B4: Improvement opportunities (batched) ---
        if improvement_candidates:
            for batch_idx in range(0, len(improvement_candidates), B4_BATCH_SIZE):
                batch = improvement_candidates[batch_idx:batch_idx + B4_BATCH_SIZE]
                imp_text = "\n".join(
                    f"[ID={m['id']}] [cat={m['category']}|dom={m['domain']}|imp={m['importance']}|"
                    f"age={m['created_at'].strftime('%Y-%m-%d')}] {m['content']}"
                    for m in batch)
                prompt = (
                    "You are a memory maintenance agent. Analyze these memories for improvement patterns.\n\n"
                    "## IMPROVEMENT OPPORTUNITIES\n"
                    "Identify memories revealing RECURRING problems, persistent workarounds, "
                    "or patterns suggesting the agent's infrastructure could be improved.\n\n"
                    + imp_text
                    + '\n\n## OUTPUT FORMAT\n'
                    'Return a JSON object:\n'
                    '{"improvements": [{"memory_ids": [<ids>], "theme": "<name>", '
                    '"problem": "<issue>", "suggested_fix": "<fix>", "priority": "high|medium|low"}]}\n'
                    'Return {"improvements": []} if no patterns found.'
                )
                parallel_calls.append({
                    "label": f"B4_batch{batch_idx // B4_BATCH_SIZE}",
                    "prompt": prompt,
                    "json_schema": {"type": "object", "properties": {
                        "improvements": {"type": "array", "items": {"type": "object"}}}},
                    "timeout": 600,
                })

        # --- Dispatch all calls in parallel ---
        if parallel_calls:
            print(f"\n  Dispatching {len(parallel_calls)} parallel Opus calls...")
            all_results = _organize_parallel_llm_calls(parallel_calls)

            # --- Apply results with conflict resolution ---
            # Track IDs already superseded to prevent conflicting writes
            superseded_ids = set()

            # 1. Apply B1 merges first (destructive — sets superseded_by)
            for label in sorted(k for k in all_results if k.startswith("B1")):
                result = all_results[label]
                if not result or not isinstance(result, dict):
                    continue
                for entry in result.get("merges", []):
                    winner_id = entry.get("winner_id")
                    loser_ids = entry.get("loser_ids", [])
                    new_content = entry.get("new_content", "")
                    new_imp = entry.get("new_importance")
                    if not winner_id or not loser_ids or not new_content:
                        continue
                    # Skip if any involved ID was already superseded
                    if winner_id in superseded_ids or any(lid in superseded_ids for lid in loser_ids):
                        print(f"    [SKIP] Merge #{winner_id} ← {loser_ids} — ID conflict")
                        continue
                    try:
                        vec = embed_text(new_content)
                        updates = ["content = %s", "embedding = %s"]
                        params = [new_content, vec.tolist()]
                        if new_imp:
                            updates.append("importance = %s")
                            params.append(new_imp)
                        params.append(winner_id)
                        safe_execute(conn, f"UPDATE memories SET {', '.join(updates)} WHERE id = %s", params)
                    except Exception as e:
                        print(f"    WARNING: embed/update failed for #{winner_id}: {e}", file=sys.stderr)
                        continue
                    safe_execute(conn, "UPDATE memories SET superseded_by = %s WHERE id = ANY(%s)",
                                 (winner_id, loser_ids))
                    safe_execute(conn, "UPDATE memories SET superseded_by = %s WHERE superseded_by = ANY(%s)",
                                 (winner_id, loser_ids))
                    superseded_ids.update(loser_ids)
                    actions.append({"type": "cluster_merge", "winner_id": winner_id,
                                    "loser_ids": loser_ids, "rationale": entry.get("rationale", "")})
                    clusters_consolidated += 1
                    print(f"    [MERGE] #{winner_id} ← superseded {loser_ids}")

            # 2. Apply B2 contradictions (skip IDs already superseded by B1)
            b2_result = all_results.get("B2")
            if b2_result and isinstance(b2_result, dict):
                for entry in b2_result.get("contradictions", []):
                    if entry.get("contradicts") in ("YES", "PARTIAL") and entry.get("supersede_id"):
                        keep_id = entry["keep_id"]
                        sup_id = entry["supersede_id"]
                        if keep_id in superseded_ids or sup_id in superseded_ids:
                            print(f"    [SKIP] Contradiction #{keep_id} vs #{sup_id} — ID conflict")
                            continue
                        safe_execute(conn, "UPDATE memories SET superseded_by = %s WHERE id = %s",
                                     (keep_id, sup_id))
                        superseded_ids.add(sup_id)
                        actions.append({"type": "contradiction_resolved", "keep": keep_id,
                                        "supersede": sup_id,
                                        "rationale": entry.get("rationale", "")})
                        contradictions_resolved += 1
                        print(f"    [CONTRADICT] Keep #{keep_id}, supersede #{sup_id}")
                    else:
                        print(f"    [NO CONFLICT] Pair {entry.get('pair', '?')}")

            # 3. Apply B3 staleness (skip IDs already superseded)
            b3_result = all_results.get("B3")
            if b3_result and isinstance(b3_result, dict) and stale:
                for entry in b3_result.get("stale", []):
                    mid = entry.get("id")
                    if mid in superseded_ids:
                        print(f"    [SKIP] Stale #{mid} — already superseded")
                        continue
                    action_type = entry.get("action", "KEEP").upper()
                    if action_type == "DELETE":
                        safe_execute(conn, "UPDATE memories SET superseded_by = id WHERE id = %s AND superseded_by IS NULL", (mid,))
                        superseded_ids.add(mid)
                        print(f"    [DELETE] #{mid}: {entry.get('rationale', '')}")
                        actions.append({"type": "stale_delete", "id": mid, "rationale": entry.get("rationale", "")})
                    elif action_type == "DEMOTE":
                        new_imp = max(entry.get("new_importance", 3), 3)
                        safe_execute(conn, "UPDATE memories SET importance = %s WHERE id = %s", (new_imp, mid))
                        print(f"    [DEMOTE] #{mid} → imp:{new_imp}: {entry.get('rationale', '')}")
                        actions.append({"type": "stale_demote", "id": mid, "new_importance": new_imp,
                                        "rationale": entry.get("rationale", "")})
                    else:
                        print(f"    [KEEP] #{mid}")
                    stale_assessed += 1

            # 4. Apply B4 improvements (non-destructive — just inserts to improvement_flags)
            for label in sorted(k for k in all_results if k.startswith("B4")):
                result = all_results[label]
                if not result or not isinstance(result, dict):
                    continue
                for entry in result.get("improvements", []):
                    theme = entry.get("theme", "unknown")
                    problem = entry.get("problem", "")
                    suggested_fix = entry.get("suggested_fix", "")
                    priority = entry.get("priority", "medium")
                    memory_ids = entry.get("memory_ids", [])
                    print(f"  [{priority.upper()}] {theme}")
                    print(f"    Problem: {problem}")
                    print(f"    Suggested fix: {suggested_fix}")
                    print(f"    Related memories: {memory_ids}")
                    flag_row = safe_query(conn,
                        "INSERT INTO improvement_flags (theme, problem, suggested_fix, priority, organizer_run_id) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (theme, problem, suggested_fix, priority, run_id))
                    flag_id = flag_row[0]["id"] if flag_row else None
                    if flag_id and memory_ids:
                        for mid in memory_ids:
                            safe_execute(conn,
                                "INSERT INTO improvement_flag_memories (flag_id, memory_id) "
                                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                (flag_id, mid))
                    actions.append({"type": "improvement_flagged", "flag_id": flag_id, "theme": theme,
                                    "problem": problem, "suggested_fix": suggested_fix,
                                    "priority": priority, "memory_ids": memory_ids})
                    improvements_flagged += 1

    return actions, clusters_consolidated, contradictions_resolved, stale_assessed, improvements_flagged


def cmd_memory_organize(args):
    """Memory Organizing Agent — weekly Opus-powered maintenance."""
    # Parse steps
    valid_steps = {"A1", "A3", "B1", "B2", "B3", "B4"}
    if args.steps:
        steps = {s.strip().upper() for s in args.steps.split(",")}
        invalid = steps - valid_steps
        if invalid:
            print(f"ERROR: Invalid steps: {invalid}. Valid: {sorted(valid_steps)}")
            return
    else:
        steps = valid_steps  # all steps by default

    # Atomic lock — prevents concurrent organizer runs.
    # Assumes LOG_DIR is on local NTFS; O_EXCL is not reliable on SMB shares.
    # 90min stale TTL covers worst-case Phase B (8 parallel LLM calls x 600s + retries x serial phases).
    lock_path = LOG_DIR / "organizer.lock"
    if not _acquire_processor_lock_for(lock_path, stale_seconds=90 * 60):
        print("ERROR: Another organizer run is in progress (lock file exists). "
              "Wait or delete logs/organizer.lock", file=sys.stderr)
        return

    conn = get_conn()
    dry_run = args.dry_run
    verbose = args.verbose

    # Ensure improvement_flags tables exist
    safe_execute(conn, """
        CREATE TABLE IF NOT EXISTS improvement_flags (
            id SERIAL PRIMARY KEY,
            theme TEXT NOT NULL,
            problem TEXT NOT NULL,
            suggested_fix TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'open',
            organizer_run_id INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """)
    safe_execute(conn, """
        CREATE TABLE IF NOT EXISTS improvement_flag_memories (
            flag_id INTEGER REFERENCES improvement_flags(id) ON DELETE CASCADE,
            memory_id INTEGER,
            PRIMARY KEY (flag_id, memory_id)
        )
    """)

    # Force dry-run on first run
    first_run = not safe_query(conn, "SELECT 1 FROM organizer_runs LIMIT 1")
    if first_run and not dry_run:
        print("First run detected — forcing --dry-run for safety. Review output, then run again without --dry-run.")
        dry_run = True

    # Count active memories before
    before_count = safe_query(conn, "SELECT COUNT(*) as cnt FROM memories WHERE superseded_by IS NULL")
    before_count = before_count[0]["cnt"] if before_count else 0

    # Determine phase label for logging
    has_a = any(s.startswith("A") for s in steps)
    has_b = any(s.startswith("B") for s in steps)
    phase = "AB" if (has_a and has_b) else ("A" if has_a else "B")

    # Start run record
    run_row = safe_query(conn, """
        INSERT INTO organizer_runs (phase, dry_run, agent_id)
        VALUES (%s, %s, %s) RETURNING id
    """, (phase, dry_run, AGENT_ID))
    run_id = run_row[0]["id"] if run_row else None

    all_actions = []
    dupes_merged = 0
    stale_decayed = 0
    clusters_consolidated = 0
    contradictions_resolved = 0
    stale_assessed = 0
    improvements_flagged = 0
    error_msg = None

    try:
        prefix = "[DRY RUN] " if dry_run else ""
        steps_label = ",".join(sorted(steps))
        print(f"\n{'='*60}")
        print(f"{prefix}MEMORY ORGANIZER — Steps: {steps_label}")
        print(f"{'='*60}")
        print(f"Active memories: {before_count}")

        # Phase A steps
        if has_a:
            print(f"\n{'─'*40}")
            print(f"Phase A: Heuristic Pass")
            print(f"{'─'*40}")

            if "A1" in steps:
                print(f"\n--- A1: Merge Duplicates (threshold={args.merge_threshold}) ---")
                merge_result = _audit_merge_duplicates(conn, threshold=args.merge_threshold, dry_run=dry_run)
                if merge_result:
                    dupes_merged, merge_actions = merge_result
                    all_actions.extend(merge_actions)

            if "A3" in steps:
                print(f"\n--- A3: Decay Completed-Project Memories ---")
                decay_actions = _organize_decay_completed(conn, dry_run=dry_run)
                stale_decayed = len(decay_actions)
                all_actions.extend(decay_actions)

        # Phase B steps
        if has_b:
            print(f"\n{'─'*40}")
            print(f"Phase B: LLM Consolidation")
            print(f"{'─'*40}")
            b_steps = {s for s in steps if s.startswith("B")}
            b_actions, clusters_consolidated, contradictions_resolved, stale_assessed, improvements_flagged = \
                _organize_run_phase_b(conn, max_clusters=args.max_sonnet_clusters,
                                      dry_run=dry_run, enabled_steps=b_steps, run_id=run_id)
            all_actions.extend(b_actions)

        # Count active memories after
        after_count = safe_query(conn, "SELECT COUNT(*) as cnt FROM memories WHERE superseded_by IS NULL")
        after_count = after_count[0]["cnt"] if after_count else before_count

        total_actions = (dupes_merged + stale_decayed +
                         clusters_consolidated + contradictions_resolved +
                         stale_assessed + improvements_flagged)

        # Summary report
        print(f"\n{'='*60}")
        print(f"{prefix}MEMORY ORGANIZER REPORT (run #{run_id})")
        print(f"{'='*60}")
        if phase in ("A", "AB"):
            print(f"Phase A: {dupes_merged} dupes merged, {stale_decayed} project-decay")
        if phase in ("B", "AB"):
            print(f"Phase B: {clusters_consolidated} clusters consolidated, "
                  f"{contradictions_resolved} contradictions, {stale_assessed} stale assessed, "
                  f"{improvements_flagged} improvements flagged")
        print(f"Before: {before_count} active → After: {after_count} active "
              f"({after_count - before_count:+d})")

    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"\nERROR: {e}", file=sys.stderr)
    finally:
        # Update run record
        if run_id:
            try:
                safe_execute(conn, """
                    UPDATE organizer_runs SET
                        completed_at = NOW(),
                        dupes_merged = %s, stale_decayed = %s,
                        clusters_consolidated = %s,
                        contradictions_resolved = %s, stale_assessed = %s,
                        total_actions = %s, actions_log = %s, error_message = %s
                    WHERE id = %s
                """, (dupes_merged, stale_decayed,
                      clusters_consolidated, contradictions_resolved, stale_assessed,
                      total_actions,
                      json.dumps(all_actions) if all_actions else None,
                      error_msg, run_id))
            except Exception:
                pass

        # Store a reflection so headless (Task Scheduler) runs are still surfaced
        # in the reflections table — replaces the old wrapper-prompt's reflection step.
        try:
            outcome = "failure" if error_msg else ("success" if total_actions > 0 else "no_op")
            what_worked = (
                f"Phase A: {dupes_merged} dupes merged, {stale_decayed} project-decay. "
                f"Phase B: {clusters_consolidated} clusters, {contradictions_resolved} contradictions, "
                f"{stale_assessed} stale, {improvements_flagged} improvements flagged."
            )
            what_failed = error_msg[:500] if error_msg else None
            safe_execute(conn, """
                INSERT INTO reflections (session_id, task_type, domain, outcome,
                                         what_worked, what_failed, lessons, confidence,
                                         tags, project, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                f"organizer-run-{run_id}" if run_id else f"organizer-run-{uuid.uuid4().hex[:8]}",
                "memory_organize",
                "memory_system",
                outcome,
                what_worked,
                what_failed,
                [],
                3 if error_msg else 7,
                ["organizer", f"phase-{phase}", "scheduled" if not dry_run else "dry_run"],
                "memory-loop",
                AGENT_ID,
            ))
        except Exception:
            pass

        # Remove lock file
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

        conn.close()


def _build_rewrite_prompt(memories_text):
    """Build a prompt for Sonnet to evaluate and rewrite a batch of memories."""
    return f"""You are evaluating existing memories against updated quality rules. For each memory, decide: KEEP, REWRITE, or DELETE.

## Quality Gates (apply to every memory)

1. **Discoverable?** Can a fresh agent find this by reading code, config files, or `--help`? Yes → DELETE.
2. **Novel + Actionable?** Does knowing this change what an agent would DO? No → DELETE.

## Templates (memories must match these)

Valid categories: fact, decision, bug, pattern, preference, config, tool_usage

- **decision:** "Chose [X] over [alternatives] because [rationale]."
- **bug:** "When [X], expect [Y] but actually [Z]. Fix: [workaround]."
- **preference:** "User wants [X], not [Y]. Context: [when this applies]."
- **pattern** (failed approach): "[X] was tried and failed because [Y]. Don't revisit unless [condition]."
- **pattern:** "When [trigger], [action] because [reason]."
- **fact:** Single declarative statement with a concrete noun (path, tool, version, number).
- **fact** (domain knowledge): Quantitative finding + scope/evidence.

## Banned Changelog Verbs

BANNED FIRST WORDS: "Integrated", "Created", "Built", "Added", "Implemented", "Fixed", "Designed", "Configured", "Set up", "Developed", "Established", "Introduced", "Migrated", "Refactored"

If a memory starts with these, REWRITE as what IS true now, not what was DONE.

## Importance Anchors

4 = saves a few minutes of searching/debugging
5-6 = prevents a wrong tool choice or configuration error
7-8 = prevents a wrong architectural decision or repeating a failed experiment
9-10 = prevents data loss, security exposure, or major wasted effort
Anything below 4 → DELETE.

## DELETE if:
- Diary/narrative ("First real use of...", "Successfully completed...")
- Point-in-time snapshot that's likely stale ("Albert runs X model")
- Implementation detail discoverable from code
- Generic wisdom ("always test before deploying")
- Importance would be ≤ 3 under the new anchors

## REWRITE if:
- Good content but uses changelog verbs (rephrase as current state)
- Good content but doesn't match a template (restructure to fit)
- Importance needs adjusting under new anchors

## KEEP if:
- Already matches a template, passes both gates, correct importance

## Input memories:

{memories_text}

## Output format:

Return ONLY a JSON array. No markdown, no explanation, no preamble. Example:
[
  {{"id": 123, "action": "keep"}},
  {{"id": 124, "action": "rewrite", "new_content": "rewritten text here", "new_importance": 6}},
  {{"id": 125, "action": "rewrite", "new_content": "rewritten text", "new_category": "pattern"}},
  {{"id": 126, "action": "delete"}}
]

Only include new_importance if it should change. Only include new_category if the category should change.
Evaluate every memory in the batch. Output valid JSON only."""


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description="Agent memory helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # startup
    p = sub.add_parser("startup", help="Write MEMORY.md from DB state")
    p.add_argument("--output-path", help="Override MEMORY.md output path")

    # save-session
    p = sub.add_parser("save-session", help="Record session end")
    p.add_argument("--session-id")
    p.add_argument("--summary")
    p.add_argument("--outcome", choices=["success", "partial", "failed", "abandoned"])
    p.add_argument("--tags", nargs="*")
    # Optional bundled reflection (so agent can save session + reflect in one call)
    p.add_argument("--what-worked")
    p.add_argument("--what-failed")
    p.add_argument("--lessons", nargs="*")
    p.add_argument("--domain", default="general",
                   choices=["backtesting", "ea_development", "optimization",
                            "tool_errors", "environment", "problem_solving",
                            "debugging", "general"])
    p.add_argument("--confidence", type=int, default=5)

    # save-context
    p = sub.add_parser("save-context", help="Save context before compact")
    p.add_argument("--content")
    p.add_argument("--domain", default="general")
    p.add_argument("--importance", type=int, default=7)
    p.add_argument("--tags", nargs="*")

    # store-memory
    p = sub.add_parser("store-memory", help="Store a new memory")
    p.add_argument("--content", required=True)
    p.add_argument("--category", default="fact",
                   choices=["fact", "decision", "preference", "context",
                            "bug", "config", "pattern", "tool_usage"])
    p.add_argument("--domain", default="general",
                   choices=["backtesting", "ea_development", "optimization",
                            "tool_errors", "environment", "problem_solving",
                            "debugging", "general"])
    p.add_argument("--importance", type=int, default=5)
    p.add_argument("--tags", nargs="*")
    p.add_argument("--error-signature")
    p.add_argument("--session-id")
    p.add_argument("--project", help="Override project (default: os.getcwd())")
    p.add_argument("--ttl-days", type=int)
    p.add_argument("--retrieval-mode", choices=["semantic", "tool", "manual"],
                   default="semantic", help="How this memory should be retrieved")
    p.add_argument("--tool-name", help="Tool name for retrieval_mode=tool memories")

    # recall
    p = sub.add_parser("recall", help="Search memories")
    p.add_argument("--query")
    p.add_argument("--category")
    p.add_argument("--domain")
    p.add_argument("--error-signature")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.add_argument("--semantic", action="store_true",
                   help="Use vector similarity search instead of keyword search")

    # store-reflection
    p = sub.add_parser("store-reflection", help="Store a reflection")
    p.add_argument("--session-id")
    p.add_argument("--task-type", required=True)
    p.add_argument("--domain", default="general",
                   choices=["backtesting", "ea_development", "optimization",
                            "tool_errors", "environment", "problem_solving",
                            "debugging", "general"])
    p.add_argument("--outcome", required=True, choices=["success", "partial", "failed"])
    p.add_argument("--what-worked")
    p.add_argument("--what-failed")
    p.add_argument("--lessons", nargs="*")
    p.add_argument("--confidence", type=int, default=5)
    p.add_argument("--tags", nargs="*")
    p.add_argument("--project", help="Override project (default: os.getcwd())")

    # store-backtest
    p = sub.add_parser("store-backtest", help="Store backtest results")
    p.add_argument("--json-file", help="Path to JSON with all fields")
    p.add_argument("--session-id")
    p.add_argument("--ea-name")
    p.add_argument("--ea-version")
    p.add_argument("--symbol")
    p.add_argument("--timeframe")
    p.add_argument("--source", default="mt4")
    p.add_argument("--total-trades", type=int)
    p.add_argument("--profit-factor", type=float)
    p.add_argument("--net-profit", type=float)
    p.add_argument("--max-drawdown-pct", type=float)
    p.add_argument("--win-rate", type=float)
    p.add_argument("--parameters", help="JSON string of EA params")
    p.add_argument("--notes")
    p.add_argument("--tags", nargs="*")

    # get-strategy-history
    p = sub.add_parser("get-strategy-history", help="Strategy evolution")
    p.add_argument("--name", required=True)

    # promote-lessons
    p = sub.add_parser("promote-lessons", help="Promote recurring reflection lessons (semantic similarity)")
    p.add_argument("--min-occurrences", type=int, default=2,
                   help="Minimum similar lessons to trigger promotion (default: 2)")
    p.add_argument("--similarity", type=float, default=0.50,
                   help="Cosine similarity threshold for clustering (default: 0.50)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be promoted without writing")

    # auto-reflect
    sub.add_parser("auto-reflect",
                   help="Auto-generate fallback reflection from session DB data")

    # store-error-solution
    p = sub.add_parser("store-error-solution",
                       help="Store an error→solution pattern (used by Sonnet processor)")
    p.add_argument("--error-pattern", required=True, help="The error message pattern")
    p.add_argument("--solution", required=True, help="How to fix or avoid this error")
    p.add_argument("--tool-name", required=True, help="Which tool produces this error")
    p.add_argument("--platform", default="windows", help="Platform context (default: windows)")
    p.add_argument("--tags", nargs="*", help="Optional tags for categorization")

    # store-entity
    p = sub.add_parser("store-entity", help="Upsert entity into graph (Sonnet-Supplementary)")
    p.add_argument("--name", required=True, help="Entity name")
    p.add_argument("--entity-type", required=True,
                   choices=["person", "tool", "concept", "project", "file",
                            "ea", "symbol", "platform", "library", "service"])
    p.add_argument("--description", help="Brief description")
    p.add_argument("--aliases", nargs="*", help="Alternative names")
    p.add_argument("--project-id", type=int, help="Associated project ID")

    # store-relationship
    p = sub.add_parser("store-relationship", help="Upsert relationship between entities (Sonnet-Supplementary)")
    p.add_argument("--source", required=True, help="Source entity name")
    p.add_argument("--target", required=True, help="Target entity name")
    p.add_argument("--relationship-type", required=True,
                   choices=["uses", "causes", "fixes", "depends_on", "produces",
                            "trades", "implements", "contains", "replaces", "conflicts_with"])
    p.add_argument("--description", help="Relationship description")
    p.add_argument("--confidence", type=int, default=7, help="Confidence 1-10 (default: 7)")

    # store-artifact
    p = sub.add_parser("store-artifact", help="Upsert project artifact (Sonnet-Supplementary)")
    p.add_argument("--file-path", required=True, help="Full file path")
    p.add_argument("--artifact-type", required=True,
                   choices=["plan", "dashboard", "script", "ea", "config",
                            "doc", "skill", "schema", "hook", "agent", "test"])
    p.add_argument("--project", required=True, help="Project directory path")
    p.add_argument("--title", help="Display title")
    p.add_argument("--description", help="Brief description")
    p.add_argument("--status", default="current", choices=["current", "superseded", "deleted"])

    # process-supplementary
    sub.add_parser("process-supplementary",
                   help="Claim oldest completed transcript for supplementary processing")

    # mark-supplementary
    p = sub.add_parser("mark-supplementary", help="Mark supplementary processing status")
    p.add_argument("--transcript-id", required=True, type=int)
    p.add_argument("--status", required=True, choices=["completed", "failed", "skipped"])

    # store-transcript
    # session-end (combined SessionEnd handler)
    sub.add_parser("session-end",
                   help="Combined SessionEnd: save session + store transcript + spawn Sonnet")

    sub.add_parser("store-transcript",
                   help="Filter and store transcript for deferred Sonnet processing")

    # process-pending
    sub.add_parser("process-pending",
                   help="Claim oldest pending transcript and output structured content")

    # mark-processed
    p = sub.add_parser("mark-processed",
                       help="Update transcript processing status")
    p.add_argument("--transcript-id", required=True, type=int)
    p.add_argument("--status", required=True,
                   choices=["completed", "failed"])
    p.add_argument("--processed-by", default="sonnet")
    p.add_argument("--memories-created", type=int, default=0)
    p.add_argument("--error-message")
    p.add_argument("--session-id", help="Session ID (auto-detected from transcript if omitted)")
    p.add_argument("--handoff-completed", help="What was accomplished this session")
    p.add_argument("--handoff-next", help="Single most important next action")
    p.add_argument("--handoff-pending", help="What remains open or needs discussion")
    p.add_argument("--handoff-context", help="Relevant working context for next session")

    # backfill-embeddings
    p = sub.add_parser("backfill-embeddings",
                       help="Generate embeddings for memories that don't have one")
    p.add_argument("--batch-size", type=int, default=50)

    # cleanup
    sub.add_parser("cleanup", help="Expire old memories, clean up transcripts, show stats")

    # turn-recall
    sub.add_parser("turn-recall",
                   help="Per-turn semantic memory retrieval (UserPromptSubmit hook)")

    # tool-recall
    sub.add_parser("tool-recall",
                   help="Per-tool memory retrieval (PreToolUse hook)")

    # error-store
    sub.add_parser("error-store",
                   help="Capture tool errors (PostToolUse hook, write-only)")

    # ingest-paper
    p = sub.add_parser("ingest-paper", help="Ingest markdown paper into paper_chunks")
    p.add_argument("--file", required=True, help="Path to markdown file")
    p.add_argument("--title", help="Paper title (default: filename)")
    p.add_argument("--tags", nargs="+", help="Tags for the paper (e.g., trading ML)")
    p.add_argument("--author", help="Author name(s)")
    p.add_argument("--doc-type", dest="doc_type",
                   help="Document type (textbook, paper, guide, reference)")
    p.add_argument("--replace", action="store_true",
                   help="Replace existing chunks from same source file")
    p.add_argument("--max-chars", type=int, default=2000,
                   help="Max chars per chunk before splitting (default: 2000)")

    # recall-papers
    p = sub.add_parser("recall-papers", help="Semantic search across paper_chunks")
    p.add_argument("--query", required=True, help="Search query")
    p.add_argument("--paper", help="Filter by paper title (partial match)")
    p.add_argument("--tags", nargs="+", help="Filter by tags (all must match)")
    p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--graph", action="store_true", help="Also query LightRAG knowledge graph")
    p.add_argument("--graph-only", action="store_true", help="Only query graph (skip vector search)")
    p.add_argument("--graph-mode", default="hybrid", choices=["naive", "local", "global", "hybrid", "mix"],
                   help="LightRAG query mode (default: hybrid)")

    # get-exposures
    p = sub.add_parser("get-exposures", help="List unscored memory exposures for a session")
    p.add_argument("--session-id", required=True, help="Session ID to query exposures for")

    # score-exposure
    p = sub.add_parser("score-exposure", help="Score a memory exposure as useful or not")
    p.add_argument("--exposure-id", required=True, type=int, help="Exposure ID to score")
    p.add_argument("--useful", required=True, type=lambda v: v.lower() in ("true", "1", "yes"),
                   help="Whether the exposure was useful (true/false)")

    # batch-score-exposures
    p = sub.add_parser("batch-score-exposures", help="Score multiple exposures in one call (JSON map)")
    p.add_argument("--scores", help='JSON map: {"exposure_id": true/false, ...}. Or pipe via stdin.')

    # audit-memories
    p = sub.add_parser("memory-organize", help="Memory Organizing Agent: selective step-based maintenance")
    p.add_argument("--steps", type=str, default=None,
                   help="Comma-separated steps (default: all). Valid: A1,A3,B1,B2,B3,B4")
    p.add_argument("--dry-run", action="store_true", help="Preview without modifying (forced on first run)")
    p.add_argument("--merge-threshold", type=float, default=0.85, help="Cosine threshold for duplicate merge (default: 0.85)")
    p.add_argument("--max-sonnet-clusters", type=int, default=20, help="Budget cap for Phase B clusters (default: 20)")
    p.add_argument("--verbose", action="store_true", help="Detailed per-action output")

    p = sub.add_parser("audit-memories", help="Audit memory quality: merge duplicates, assess stale entries")
    p.add_argument("--merge-duplicates", action="store_true", help="Find and merge near-duplicate memories")
    p.add_argument("--assess-stale", action="store_true", help="List stale low-value memories for review")
    p.add_argument("--delete-ids", nargs="+", type=int, help="Delete specific memory IDs after assessment")
    p.add_argument("--rewrite-all", action="store_true",
                   help="Export all memories for batch rewriting")
    p.add_argument("--apply-rewrites", type=str,
                   help="Apply rewrite decisions from JSON file")
    p.add_argument("--batch-size", type=int, default=50,
                   help="Memories per batch for --rewrite-all (default: 50)")
    p.add_argument("--offset", type=int, default=0,
                   help="Start offset for --rewrite-all batches")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without modifying DB")
    p.add_argument("--threshold", type=float, default=0.85,
                   help="Cosine similarity threshold for duplicates (default: 0.85)")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "startup": cmd_startup,
        "save-session": cmd_save_session,
        "save-context": cmd_save_context,
        "store-memory": cmd_store_memory,
        "recall": cmd_recall,
        "store-reflection": cmd_store_reflection,
        "store-backtest": cmd_store_backtest,
        "get-strategy-history": cmd_get_strategy_history,
        "promote-lessons": cmd_promote_lessons,
        "auto-reflect": cmd_auto_reflect,
        "store-transcript": cmd_store_transcript,
        "session-end": cmd_session_end,
        "process-pending": cmd_process_pending,
        "mark-processed": cmd_mark_processed,
        "backfill-embeddings": cmd_backfill_embeddings,
        "cleanup": cmd_cleanup,
        "turn-recall": cmd_turn_recall,
        "tool-recall": cmd_tool_recall,
        "error-store": cmd_error_store,
        "store-error-solution": cmd_store_error_solution,
        "store-entity": cmd_store_entity,
        "store-relationship": cmd_store_relationship,
        "store-artifact": cmd_store_artifact,
        "process-supplementary": cmd_process_supplementary,
        "mark-supplementary": cmd_mark_supplementary,
        "ingest-paper": cmd_ingest_paper,
        "recall-papers": cmd_recall_papers,
        "get-exposures": cmd_get_exposures,
        "score-exposure": cmd_score_exposure,
        "batch-score-exposures": cmd_batch_score_exposures,
        "memory-organize": cmd_memory_organize,
        "audit-memories": cmd_audit_memories,
    }

    try:
        dispatch[args.command](args)
    except psycopg2.OperationalError as e:
        print(f"DB connection error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

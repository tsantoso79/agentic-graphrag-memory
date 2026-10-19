"""Real-time trace dashboard server for Claude Code CLI sessions.

Endpoints:
  GET  /            → redirect to /dashboard
  GET  /dashboard   → single-page HTML dashboard
  GET  /events      → SSE stream (replay last 50, then live)
  POST /api/ingest  → receive events from hook relay
  GET  /api/traces  → JSON array of recent events
  GET  /api/stats   → aggregate statistics
  GET  /healthz     → health check
  GET  /knowledge-graph → Knowledge Graph visualization
  GET  /api/kg/*        → KG data endpoints (graph, stats, search, query)
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid as uuid_mod
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from kg_dashboard import KG_DASHBOARD_HTML, handle_kg_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [trace-server] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("TRACE_SERVER_PORT", "7799"))
LOG_DIR = Path.home() / ".claude" / "memory" / "logs"
BUFFER_SIZE = 1000

# Event buffer and SSE subscribers
_buffer = deque(maxlen=BUFFER_SIZE)
_clients = []  # list of asyncio.Queue
_replay_date = None  # Track which date was last replayed


def _fan_out(event):
    """Push event to all connected SSE clients."""
    dead = []
    for q in _clients:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _clients.remove(q)


def _replay_jsonl():
    """Load today's JSONL file into the buffer on startup."""
    global _replay_date
    date_str = datetime.now().strftime("%Y-%m-%d")
    _replay_date = date_str
    jsonl_path = LOG_DIR / f"trace_events_{date_str}.jsonl"
    if not jsonl_path.exists():
        return
    try:
        count = 0
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    _buffer.append(event)
                    count += 1
                except json.JSONDecodeError:
                    continue
        logger.info("Replayed %d events from %s", count, jsonl_path.name)
    except Exception as e:
        logger.warning("Failed to replay JSONL: %s", e)


async def _date_check_loop():
    """Every 60s, check if the date rolled over. If so, clear buffer and replay new day."""
    global _replay_date
    while True:
        await asyncio.sleep(60)
        today = datetime.now().strftime("%Y-%m-%d")
        if today != _replay_date:
            logger.info("Date rolled over %s -> %s, replaying new JSONL", _replay_date, today)
            _buffer.clear()
            _replay_jsonl()


# ── Transcript Watcher ────────────────────────────────────────────────

# Global token accumulator per session
_session_tokens = {}  # session_id → {input: int, output: int, cache_read: int, cache_create: int}
_turn_counts = {}     # session_id → int
_context_ledger = {}  # session_id → {hook_injections: [], user_prompts: [], tool_results: [], assistant_text: [], tool_calls: []}


def _init_ledger(sid):
    if sid not in _context_ledger:
        _context_ledger[sid] = {
            "hook_injections": [],  # [{content, est_tokens}]
            "user_prompts": [],
            "tool_results": [],
            "assistant_text": [],
            "tool_calls": [],
        }
    return _context_ledger[sid]


def _est_tokens(text):
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(str(text)) // 4)


class TranscriptWatcher:
    """Tail transcript JSONL files and convert to dashboard events."""

    def __init__(self, buffer, fan_out_fn):
        self._watchers = {}  # session_id → {path, offset}
        self._buffer = buffer
        self._fan_out = fan_out_fn

    def watch(self, session_id, transcript_path):
        if session_id in self._watchers:
            return
        # Use text-mode tell() for correct offset (not byte-based getsize)
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                f.seek(0, 2)  # Seek to end
                offset = f.tell()
        except OSError:
            offset = 0
        self._watchers[session_id] = {"path": transcript_path, "offset": offset}
        logger.info("Watching transcript: %s (offset=%d)", transcript_path, offset)

    async def poll_loop(self):
        scan_counter = 0
        while True:
            for sid, info in list(self._watchers.items()):
                try:
                    self._poll_file(sid, info)
                except Exception as e:
                    logger.debug("Poll error for %s: %s", sid, e)
            # Every 10 polls (~5s), scan for new transcript files
            scan_counter += 1
            if scan_counter >= 10:
                scan_counter = 0
                self._scan_for_transcripts()
            await asyncio.sleep(0.5)

    def _scan_for_transcripts(self):
        """Auto-discover new transcript JSONL files in projects dir."""
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return
        watched_paths = {info["path"] for info in self._watchers.values()}
        now = time.time()
        for jsonl in projects_dir.rglob("*.jsonl"):
            path_str = str(jsonl)
            if path_str in watched_paths:
                continue
            # Only pick up files modified in last 5 minutes (active sessions)
            try:
                mtime = jsonl.stat().st_mtime
                if now - mtime > 300:
                    continue
            except OSError:
                continue
            # Use filename as pseudo session_id
            sid = jsonl.stem
            if sid in self._watchers:
                continue
            self.watch(sid, path_str)
            logger.info("Auto-discovered transcript: %s", jsonl.name)

    def _poll_file(self, session_id, info):
        path = info["path"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(info["offset"])
                new_data = f.read()
                if not new_data:
                    return
                info["offset"] = f.tell()
        except OSError:
            return
        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            for evt in self._parse_line(entry, session_id):
                # Light dedup against last 50 buffered events
                if not any(e.get("id") == evt["id"] for e in list(self._buffer)[-50:]):
                    self._buffer.append(evt)
                    self._fan_out(evt)

    def _parse_line(self, entry, session_id):
        evts = []
        etype = entry.get("type", "")
        ts = self._parse_ts(entry.get("timestamp", ""))

        if etype == "assistant":
            evts.extend(self._parse_assistant(entry, session_id, ts))
        elif etype == "user":
            evts.extend(self._parse_user(entry, session_id, ts))
        elif etype == "system":
            evts.extend(self._parse_system(entry, session_id, ts))
        elif etype == "queue-operation":
            op = entry.get("operation", "")
            content = entry.get("content", "")
            if content:
                evts.append(self._evt("subagent_task", {
                    "operation": op, "task_summary": str(content)[:200],
                }, session_id, ts))
        # Also handle compact_start events from hook relay to reset ledger
        if etype == "compact_start" or (etype == "system" and entry.get("subtype") == "compact"):
            ledger = _init_ledger(session_id)
            for items in ledger.values():
                items.clear()
        return evts

    def _parse_assistant(self, entry, session_id, ts):
        evts = []
        msg = entry.get("message", {})
        stop = msg.get("stop_reason")
        if stop is None:
            return evts  # Skip streaming chunks
        usage = msg.get("usage", {})
        model = msg.get("model", "")
        content = msg.get("content", [])

        tool_calls = []
        has_thinking = False
        thinking_preview = ""
        text_preview = ""

        for block in content:
            bt = block.get("type", "")
            if bt == "tool_use":
                name = block.get("name", "")
                tid = block.get("id", "")
                inp_full = json.dumps(block.get("input", {}), default=str)
                inp = inp_full[:200]
                tool_calls.append({"name": name, "id": tid, "input_preview": inp})
                evts.append(self._evt("tool_use", {
                    "tool_name": name, "tool_id": tid, "input_preview": inp,
                }, session_id, ts))
                _init_ledger(session_id)["tool_calls"].append({
                    "content": name + ": " + inp[:300], "est_tokens": _est_tokens(inp_full),
                })
                if name == "Skill":
                    skill_input = block.get("input", {})
                    evts.append(self._evt("skill_invoke", {
                        "skill_name": skill_input.get("skill", "?"),
                        "skill_args": str(skill_input.get("args", ""))[:200],
                    }, session_id, ts))
            elif bt == "thinking":
                has_thinking = True
                txt = block.get("thinking", "")
                thinking_preview = txt[:300]
                evts.append(self._evt("thinking", {
                    "thinking_preview": thinking_preview,
                    "thinking_length": len(txt),
                }, session_id, ts))
            elif bt == "text":
                txt = block.get("text", "")
                text_preview = txt[:200]
                if txt:
                    ledger = _init_ledger(session_id)
                    ledger["assistant_text"].append({
                        "content": txt[:500], "est_tokens": _est_tokens(txt),
                    })

        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)

        # Accumulate tokens
        if session_id not in _session_tokens:
            _session_tokens[session_id] = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
        st = _session_tokens[session_id]
        st["input"] += in_tok
        st["output"] += out_tok
        st["cache_read"] += cache_read
        st["cache_create"] += cache_create

        evts.append(self._evt("llm_response", {
            "model": model, "stop_reason": stop,
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cache_read_tokens": cache_read, "cache_creation_tokens": cache_create,
            "total_input": st["input"], "total_output": st["output"],
            "tool_calls": tool_calls, "tool_call_count": len(tool_calls),
            "has_thinking": has_thinking, "text_preview": text_preview,
        }, session_id, ts))
        return evts

    def _parse_user(self, entry, session_id, ts):
        evts = []
        ledger = _init_ledger(session_id)
        msg = entry.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    rc = str(block.get("content", ""))
                    evts.append(self._evt("tool_result", {
                        "tool_use_id": block.get("tool_use_id", ""),
                        "is_error": block.get("is_error", False),
                        "result_preview": rc[:200],
                        "result_length": len(rc),
                    }, session_id, ts))
                    ledger["tool_results"].append({
                        "content": rc[:500], "est_tokens": _est_tokens(rc),
                    })
        elif isinstance(content, str) and content:
            if "<system-reminder>" in content:
                ledger["hook_injections"].append({
                    "content": content[:500], "est_tokens": _est_tokens(content),
                })
            else:
                ledger["user_prompts"].append({
                    "content": content[:500], "est_tokens": _est_tokens(content),
                })
        return evts

    def _parse_system(self, entry, session_id, ts):
        evts = []
        sub = entry.get("subtype", "")
        if sub == "turn_duration":
            dur = entry.get("durationMs", 0)
            mc = entry.get("messageCount", 0)
            _turn_counts[session_id] = _turn_counts.get(session_id, 0) + 1
            evts.append(self._evt("turn_complete", {
                "duration_ms": dur, "message_count": mc,
                "turn_number": _turn_counts[session_id],
            }, session_id, ts))
            # Emit context breakdown from this turn's ledger
            ledger = _init_ledger(session_id)
            st = _session_tokens.get(session_id, {})
            categories = {}
            cat_total_est = 0
            for cat_name, items in ledger.items():
                est = sum(it["est_tokens"] for it in items)
                cat_total_est += est
                recent = [it["content"] for it in items[-5:]]
                categories[cat_name] = {"est_tokens": est, "count": len(items), "recent": recent}
            # Compute proportions relative to sum of all categories (not API tokens)
            if cat_total_est > 0:
                for cat in categories.values():
                    cat["proportion"] = round(cat["est_tokens"] / cat_total_est, 4)
            evts.append(self._evt("context_breakdown", {
                "categories": categories,
                "cat_total_est": cat_total_est,
                "cache_read_tokens": st.get("cache_read", 0),
                "cache_creation_tokens": st.get("cache_create", 0),
                "total_input": st.get("input", 0),
                "total_output": st.get("output", 0),
                "turn_number": _turn_counts[session_id],
            }, session_id, ts))
            # Reset ledger for next turn (keep structure, clear items)
            for items in ledger.values():
                items.clear()
        elif sub == "api_error":
            err = entry.get("error", {})
            evts.append(self._evt("api_error", {
                "status_code": err.get("status", 0),
                "retry_attempt": entry.get("retryAttempt", 0),
                "max_retries": entry.get("maxRetries", 0),
                "retry_in_ms": entry.get("retryInMs", 0),
                "error_message": str(err.get("message", ""))[:200],
            }, session_id, ts))
        return evts

    @staticmethod
    def _parse_ts(ts_str):
        if not ts_str:
            return time.time()
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return time.time()

    @staticmethod
    def _evt(event_type, data, session_id, ts):
        return {
            "id": "tw_" + uuid_mod.uuid4().hex[:10],
            "ts": ts, "type": event_type,
            "session_id": session_id,
            "project": "", "agent_id": "transcript",
            "data": data,
        }


_watcher = None  # Set in main()


# ── HTTP request handler ──────────────────────────────────────────────

async def handle_client(reader, writer):
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=10.0)
        request = data.decode("utf-8", errors="replace")
        first_line = request.split("\r\n")[0] if request else ""

        if "GET /healthz" in first_line:
            _write_response(writer, 200, "text/plain", "ok")

        elif "GET /dashboard" in first_line:
            _write_response(writer, 200, "text/html; charset=utf-8", DASHBOARD_HTML)

        elif "GET /events" in first_line:
            await _handle_sse(writer)
            return  # SSE keeps connection open

        elif "POST /api/ingest" in first_line:
            # Extract JSON body after headers
            body = ""
            if "\r\n\r\n" in request:
                body = request.split("\r\n\r\n", 1)[1]
            try:
                event = json.loads(body)
                # Deduplicate
                if not any(e.get("id") == event.get("id") for e in _buffer):
                    _buffer.append(event)
                    _fan_out(event)
                    # Auto-watch transcript on session_start
                    if event.get("type") == "session_start" and _watcher:
                        tp = (event.get("data") or {}).get("transcript_path", "")
                        sid = event.get("session_id", "")
                        if tp and sid:
                            _watcher.watch(sid, tp)
                _write_response(writer, 200, "application/json", '{"ok":true}')
            except json.JSONDecodeError:
                _write_response(writer, 400, "application/json", '{"error":"invalid json"}')

        elif "GET /api/traces" in first_line:
            # Parse query params for filtering
            traces = list(_buffer)
            # Support ?session_id=X and ?type=X filters
            if "session_id=" in first_line:
                sid = first_line.split("session_id=")[1].split("&")[0].split(" ")[0]
                traces = [e for e in traces if e.get("session_id") == sid]
            if "type=" in first_line:
                etype = first_line.split("type=")[1].split("&")[0].split(" ")[0]
                traces = [e for e in traces if e.get("type") == etype]
            _write_response(writer, 200, "application/json",
                            json.dumps(traces[-200:], default=str))

        elif "GET /api/stats" in first_line:
            stats = _compute_stats()
            _write_response(writer, 200, "application/json",
                            json.dumps(stats, default=str))

        elif "GET / " in first_line or "GET / HTTP" in first_line:
            writer.write(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /dashboard\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
        elif "GET /knowledge-graph" in first_line:
            _write_response(writer, 200, "text/html; charset=utf-8", KG_DASHBOARD_HTML)

        elif "GET /api/kg/" in first_line:
            result = await handle_kg_request(first_line)
            if result:
                status, ctype, body = result
                _write_response(writer, status, ctype, body)
            else:
                _write_response(writer, 404, "text/plain", "Not Found")

        else:
            _write_response(writer, 404, "text/plain", "Not Found")

        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _write_response(writer, status, content_type, body):
    status_text = {200: "OK", 302: "Found", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    encoded = body.encode("utf-8") if isinstance(body, str) else body
    header = (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        f"Connection: close\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        f"Access-Control-Allow-Headers: Content-Type\r\n"
        f"\r\n"
    )
    writer.write(header.encode("utf-8") + encoded)


async def _handle_sse(writer):
    """SSE endpoint: replay recent events then stream live."""
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: keep-alive\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
    )
    writer.write(header.encode())
    await writer.drain()

    # Replay last 50 events
    recent = list(_buffer)[-50:]
    for event in recent:
        line = f"data: {json.dumps(event, default=str)}\n\n"
        writer.write(line.encode())
    await writer.drain()

    # Subscribe and stream live
    queue = asyncio.Queue(maxsize=200)
    _clients.append(queue)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                line = f"data: {json.dumps(event, default=str)}\n\n"
                writer.write(line.encode())
                await writer.drain()
            except asyncio.TimeoutError:
                writer.write(b": keepalive\n\n")
                await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        if queue in _clients:
            _clients.remove(queue)
        try:
            writer.close()
        except Exception:
            pass


def _compute_stats():
    if not _buffer:
        return {"total_events": 0, "event_types": {}, "sessions": []}

    type_counts = {}
    sessions = {}
    for e in _buffer:
        t = e.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
        sid = e.get("session_id", "")
        if sid:
            if sid not in sessions:
                sessions[sid] = {"session_id": sid, "project": e.get("project", ""), "event_count": 0, "first_ts": e.get("ts"), "last_ts": e.get("ts")}
            sessions[sid]["event_count"] += 1
            sessions[sid]["last_ts"] = e.get("ts")

    return {
        "total_events": len(_buffer),
        "event_types": type_counts,
        "connected_clients": len(_clients),
        "sessions": list(sessions.values()),
        "oldest_ts": _buffer[0].get("ts") if _buffer else None,
        "newest_ts": _buffer[-1].get("ts") if _buffer else None,
    }


# ── Dashboard HTML ─────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Trace Dashboard</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --purple: #bc8cff;
  --orange: #d18616; --cyan: #39d353; --pink: #f778ba; --teal: #2ea9a1;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: 'Consolas','Monaco','Courier New',monospace; font-size: 13px; height: 100vh; overflow: hidden; }
#app { display: grid; grid-template-rows: auto auto 1fr auto auto auto; height: 100vh; }

/* Header */
#header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; align-items: center; gap: 16px; }
#header h1 { font-size: 15px; color: var(--accent); font-weight: 600; letter-spacing: 1px; }
.status { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; }
.status-dot.connected { background: var(--green); box-shadow: 0 0 6px var(--green); }
.status-dot.disconnected { background: var(--red); }
#event-count { color: var(--text-dim); font-size: 12px; }
.header-stats { margin-left: auto; display: flex; align-items: center; gap: 16px; font-size: 12px; }
.stat-item { display: flex; align-items: center; gap: 4px; }
.stat-label { color: var(--text-dim); }
.stat-val { color: var(--accent); font-weight: 600; }
.stat-val.tok { color: var(--yellow); }
.stat-val.turn { color: var(--purple); }
.header-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text-dim); padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; font-family: inherit; }
.header-btn:hover { border-color: var(--accent); color: var(--text); }
.header-btn.active { border-color: var(--yellow); color: var(--yellow); }

/* Pipeline bar */
#pipeline { background: var(--surface); border-bottom: 1px solid var(--border); padding: 10px 16px; display: flex; align-items: center; gap: 4px; font-size: 12px; overflow-x: auto; }
.pipe-step { padding: 4px 10px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); white-space: nowrap; cursor: pointer; transition: all 0.15s; }
.pipe-step:hover { border-color: var(--text-dim); color: var(--text); }
.pipe-step.active { border-color: var(--accent); color: var(--accent); background: rgba(88,166,255,0.1); animation: pulse 0.5s ease; }
.pipe-step.filtered { border-color: var(--green); color: var(--green); background: rgba(63,185,80,0.15); box-shadow: 0 0 6px rgba(63,185,80,0.3); }
.pipe-arrow { color: var(--text-dim); margin: 0 2px; }
.pipe-count { font-size: 10px; color: var(--text-dim); margin-left: 4px; }
@keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }

/* Main content */
#main { display: grid; grid-template-columns: 420px 1fr; overflow: hidden; }

/* Timeline */
#timeline { overflow-y: auto; flex: 1; }
.event-item { padding: 8px 12px; border-bottom: 1px solid var(--border); cursor: pointer; display: flex; gap: 8px; align-items: flex-start; transition: background 0.1s; }
.event-item:hover { background: rgba(88,166,255,0.05); }
.event-item.selected { background: rgba(88,166,255,0.1); border-left: 3px solid var(--accent); }
.event-item.new-event { animation: slideIn 0.3s ease; }
.event-time { color: var(--text-dim); font-size: 11px; white-space: nowrap; min-width: 55px; }
.event-badge { font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 600; white-space: nowrap; text-transform: uppercase; }
.badge-session_start, .badge-session_end { background: rgba(188,140,255,0.2); color: var(--purple); }
.badge-prompt_submit { background: rgba(88,166,255,0.2); color: var(--accent); }
.badge-memory_recall { background: rgba(63,185,80,0.2); color: var(--green); }
.badge-tool_recall { background: rgba(57,211,83,0.15); color: var(--cyan); }
.badge-tool_start { background: rgba(209,134,22,0.15); color: var(--orange); }
.badge-tool_success { background: rgba(209,134,22,0.2); color: var(--orange); }
.badge-tool_error { background: rgba(248,81,73,0.2); color: var(--red); }
.badge-checkpoint { background: rgba(210,153,34,0.2); color: var(--yellow); }
.badge-compact { background: rgba(210,153,34,0.2); color: var(--yellow); }
.badge-llm_response { background: rgba(88,166,255,0.15); color: var(--accent); }
.badge-tool_use { background: rgba(209,134,22,0.15); color: var(--orange); }
.badge-tool_result { background: rgba(209,134,22,0.25); color: var(--orange); }
.badge-thinking { background: rgba(247,120,186,0.15); color: var(--pink); }
.badge-turn_complete { background: rgba(188,140,255,0.15); color: var(--purple); }
.badge-api_error { background: rgba(248,81,73,0.2); color: var(--red); }
.badge-skill_invoke { background: rgba(46,169,161,0.2); color: var(--teal); }
.badge-subagent_task { background: rgba(188,140,255,0.15); color: var(--purple); }
.badge-stop { background: rgba(188,140,255,0.2); color: var(--purple); }
.badge-subagent_start { background: rgba(46,169,161,0.15); color: var(--teal); }
.badge-subagent_stop { background: rgba(46,169,161,0.25); color: var(--teal); }
.badge-compact_start { background: rgba(210,153,34,0.2); color: var(--yellow); }
.badge-context_breakdown { background: rgba(88,166,255,0.15); color: var(--accent); }
.event-summary { color: var(--text); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
@keyframes slideIn { from { opacity: 0; transform: translateX(-10px); } to { opacity: 1; transform: translateX(0); } }

/* Detail panel */
#detail { overflow-y: auto; padding: 16px; }
#detail-placeholder { color: var(--text-dim); text-align: center; padding-top: 100px; font-size: 14px; }
.detail-section { margin-bottom: 16px; }
.detail-section h3 { font-size: 13px; color: var(--accent); margin-bottom: 6px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
.detail-row { display: flex; padding: 3px 0; }
.detail-key { color: var(--text-dim); min-width: 140px; }
.detail-val { color: var(--text); word-break: break-all; }
.detail-val.good { color: var(--green); }
.detail-val.bad { color: var(--red); }
.detail-val.warn { color: var(--yellow); }
pre.detail-code { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 10px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; font-size: 12px; max-height: 400px; overflow-y: auto; color: var(--text); margin-top: 4px; }
.mem-item { padding: 6px 8px; margin: 4px 0; border-radius: 4px; border: 1px solid var(--border); background: var(--bg); }
.mem-id { color: var(--accent); font-weight: 600; font-size: 11px; }
.mem-score { color: var(--yellow); font-size: 11px; margin-left: 8px; }
.mem-cat { font-size: 10px; padding: 1px 5px; border-radius: 2px; background: rgba(188,140,255,0.15); color: var(--purple); margin-left: 6px; }
.mem-content { color: var(--text-dim); font-size: 12px; margin-top: 3px; }
.importance-bar { display: inline-flex; gap: 2px; }
.importance-pip { width: 10px; height: 10px; border-radius: 2px; border: 1px solid var(--border); }
.importance-pip.filled { background: var(--accent); border-color: var(--accent); }
.thinking-toggle { cursor: pointer; color: var(--pink); font-size: 12px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); margin-top: 4px; display: inline-block; }
.thinking-toggle:hover { border-color: var(--pink); }
.thinking-content { display: none; margin-top: 4px; }
.thinking-content.expanded { display: block; }
.tool-call-item { padding: 4px 8px; margin: 3px 0; border-left: 2px solid var(--orange); background: rgba(209,134,22,0.05); font-size: 12px; }
.tool-call-name { color: var(--orange); font-weight: 600; }

/* Context pressure bar */
#context-bar { background: var(--surface); border-top: 1px solid var(--border); padding: 6px 16px; font-size: 11px; }
#context-bar-header { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
#context-bar-track { flex: 1; height: 14px; background: var(--bg); border-radius: 4px; border: 1px solid var(--border); overflow: hidden; display: flex; cursor: pointer; }
.ctx-segment { height: 100%; transition: width 0.5s ease; min-width: 0; position: relative; }
.ctx-segment:hover { opacity: 0.8; }
.ctx-seg-cache { background: #30363d; }
.ctx-seg-hooks { background: var(--green); }
.ctx-seg-prompts { background: var(--accent); }
.ctx-seg-results { background: var(--orange); }
.ctx-seg-assistant { background: var(--purple); }
.ctx-seg-tools { background: var(--teal); }
#context-label { color: var(--text-dim); min-width: 200px; }
#context-legend { display: flex; gap: 12px; margin-top: 4px; flex-wrap: wrap; }
.legend-item { display: flex; align-items: center; gap: 4px; cursor: pointer; padding: 1px 4px; border-radius: 3px; }
.legend-item:hover { background: rgba(255,255,255,0.05); }
.legend-dot { width: 8px; height: 8px; border-radius: 2px; }
.legend-label { color: var(--text-dim); }
.legend-val { color: var(--text); font-weight: 600; }

/* Context drilldown panel */
#context-drilldown { display: none; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; margin-top: 6px; max-height: 200px; overflow-y: auto; padding: 8px; }
#context-drilldown.visible { display: block; }
#context-drilldown h4 { color: var(--accent); font-size: 12px; margin-bottom: 6px; }
.drilldown-item { padding: 4px 6px; margin: 3px 0; border-left: 2px solid var(--border); font-size: 11px; color: var(--text-dim); white-space: pre-wrap; word-break: break-all; max-height: 60px; overflow: hidden; }

/* Session tabs */
#sessions-bar { background: var(--surface); border-top: 1px solid var(--border); padding: 6px 16px; display: flex; align-items: center; gap: 8px; font-size: 12px; overflow-x: auto; }
.session-tab { padding: 4px 10px; border-radius: 4px; background: var(--bg); border: 1px solid var(--border); cursor: pointer; white-space: nowrap; display: flex; align-items: center; gap: 6px; }
.session-tab:hover { border-color: var(--text-dim); }
.session-tab.active { border-color: var(--accent); color: var(--accent); }
.session-count { font-size: 10px; background: var(--border); padding: 1px 5px; border-radius: 8px; color: var(--text-dim); }
#sessions-label { color: var(--text-dim); }

/* Search bar */
#timeline-col { display: flex; flex-direction: column; overflow: hidden; border-right: 1px solid var(--border); }
#search-bar { padding: 6px 8px; background: var(--surface); border-bottom: 1px solid var(--border); }
#search-input { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 5px 8px; border-radius: 4px; font-family: inherit; font-size: 12px; outline: none; }
#search-input:focus { border-color: var(--accent); box-shadow: 0 0 4px rgba(88,166,255,0.3); }
#search-input::placeholder { color: var(--text-dim); }
.event-item.search-dim { opacity: 0.25; }

/* Sparkline charts */
.sparkline { vertical-align: middle; margin-left: 4px; cursor: crosshair; }
.spark-tooltip { position: absolute; background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 2px 6px; border-radius: 3px; font-size: 10px; pointer-events: none; z-index: 100; display: none; }

/* Perf panel */
#perf-panel { display: none; background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 12px; margin: 8px 16px; max-height: 300px; overflow-y: auto; }
#perf-panel.visible { display: block; }
#perf-panel h3 { color: var(--accent); font-size: 13px; margin-bottom: 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
.perf-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.perf-table th { text-align: left; color: var(--text-dim); padding: 3px 6px; border-bottom: 1px solid var(--border); font-weight: 600; }
.perf-table td { padding: 3px 6px; border-bottom: 1px solid rgba(48,54,61,0.5); }
.perf-bar { display: inline-block; height: 10px; border-radius: 2px; min-width: 2px; }
.cost-badge { background: rgba(210,153,34,0.2); color: var(--yellow); padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }

/* Expand button */
.expand-btn { color: var(--accent); font-size: 11px; cursor: pointer; padding: 2px 6px; border: 1px solid var(--border); border-radius: 3px; background: var(--bg); display: inline-block; margin-top: 4px; }
.expand-btn:hover { border-color: var(--accent); }

/* Turn grouping */
.turn-group-header { padding: 6px 12px; background: rgba(188,140,255,0.08); border-bottom: 1px solid var(--border); cursor: pointer; display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--purple); }
.turn-group-header:hover { background: rgba(188,140,255,0.12); }
.turn-group-header .turn-toggle { color: var(--text-dim); font-size: 10px; }
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1>CLAUDE TRACE</h1>
    <div class="status">
      <div class="status-dot disconnected" id="status-dot"></div>
      <span id="status-text">Disconnected</span>
    </div>
    <span id="event-count">0 events</span>
    <div class="header-stats">
      <div class="stat-item"><span class="stat-label">Tokens:</span><span class="stat-val tok" id="token-gauge">0</span><canvas class="sparkline" id="spark-tokens" width="120" height="24"></canvas><span class="spark-tooltip" id="tip-tokens"></span></div>
      <div class="stat-item"><span class="stat-label">Turns:</span><span class="stat-val turn" id="turn-counter">0</span><canvas class="sparkline" id="spark-turns" width="120" height="24"></canvas><span class="spark-tooltip" id="tip-turns"></span></div>
      <div class="stat-item"><span class="cost-badge" id="cost-badge" title="Estimated session cost">$0.00</span></div>
    </div>
    <button class="header-btn" id="perf-btn" onclick="togglePerf()">Perf</button>
    <button class="header-btn" onclick="exportEvents()">Export</button>
    <button class="header-btn" id="time-btn" onclick="toggleTimeMode()">Rel</button>
    <button class="header-btn" id="pause-btn" onclick="togglePause()">Pause</button>
    <button class="header-btn" onclick="clearEvents()">Clear</button>
    <a href="/knowledge-graph" class="header-btn" style="text-decoration:none">KG</a>
  </div>
  <div id="pipeline">
    <div class="pipe-step filtered" id="pipe-all" onclick="filterPipeline('all')">All</div>
    <span class="pipe-arrow">|</span>
    <div class="pipe-step" id="pipe-prompt" onclick="filterPipeline('prompt')">Prompt<span class="pipe-count" id="cnt-prompt">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-memory" onclick="filterPipeline('memory')">Memory<span class="pipe-count" id="cnt-memory">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-llm" onclick="filterPipeline('llm')">LLM<span class="pipe-count" id="cnt-llm">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-tools" onclick="filterPipeline('tools')">Tools<span class="pipe-count" id="cnt-tools">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-error" onclick="filterPipeline('error')">Errors<span class="pipe-count" id="cnt-error">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-agents" onclick="filterPipeline('agents')">Agents<span class="pipe-count" id="cnt-agents">0</span></div>
    <span class="pipe-arrow">&rarr;</span>
    <div class="pipe-step" id="pipe-session" onclick="filterPipeline('session')">Session<span class="pipe-count" id="cnt-session">0</span></div>
  </div>
  <div id="main">
    <div id="timeline-col">
      <div id="search-bar"><input type="text" id="search-input" placeholder="Search events... (/ or Ctrl+K)" oninput="onSearch()"></div>
      <div id="timeline"></div>
    </div>
    <div id="detail">
      <div id="detail-placeholder">Select an event to view details</div>
    </div>
  </div>
  <div id="perf-panel"></div>
  <div id="context-bar">
    <div id="context-bar-header">
      <span id="context-label">Context: 0% (0 / ~200K tokens)</span>
      <div id="context-bar-track">
        <div class="ctx-segment ctx-seg-cache" id="seg-cache" onclick="drilldown('cache')" title="System/Cache"></div>
        <div class="ctx-segment ctx-seg-hooks" id="seg-hooks" onclick="drilldown('hook_injections')" title="Hook Injections"></div>
        <div class="ctx-segment ctx-seg-prompts" id="seg-prompts" onclick="drilldown('user_prompts')" title="User Prompts"></div>
        <div class="ctx-segment ctx-seg-results" id="seg-results" onclick="drilldown('tool_results')" title="Tool Results"></div>
        <div class="ctx-segment ctx-seg-assistant" id="seg-assistant" onclick="drilldown('assistant_text')" title="Assistant Text"></div>
        <div class="ctx-segment ctx-seg-tools" id="seg-tools" onclick="drilldown('tool_calls')" title="Tool Calls"></div>
      </div>
    </div>
    <div id="context-legend">
      <div class="legend-item" onclick="drilldown('cache')"><div class="legend-dot ctx-seg-cache"></div><span class="legend-label">Cache</span> <span class="legend-val" id="leg-cache">0</span></div>
      <div class="legend-item" onclick="drilldown('hook_injections')"><div class="legend-dot ctx-seg-hooks"></div><span class="legend-label">Hooks</span> <span class="legend-val" id="leg-hooks">0</span></div>
      <div class="legend-item" onclick="drilldown('user_prompts')"><div class="legend-dot ctx-seg-prompts"></div><span class="legend-label">Prompts</span> <span class="legend-val" id="leg-prompts">0</span></div>
      <div class="legend-item" onclick="drilldown('tool_results')"><div class="legend-dot ctx-seg-results"></div><span class="legend-label">Tool Results</span> <span class="legend-val" id="leg-results">0</span></div>
      <div class="legend-item" onclick="drilldown('assistant_text')"><div class="legend-dot ctx-seg-assistant"></div><span class="legend-label">Assistant</span> <span class="legend-val" id="leg-assistant">0</span></div>
      <div class="legend-item" onclick="drilldown('tool_calls')"><div class="legend-dot ctx-seg-tools"></div><span class="legend-label">Tool Calls</span> <span class="legend-val" id="leg-tools">0</span></div>
    </div>
    <div id="context-drilldown"></div>
  </div>
  <div id="sessions-bar">
    <span id="sessions-label">Sessions:</span>
    <div class="session-tab active" id="tab-all" onclick="filterSession('all')">All</div>
  </div>
</div>

<script>
const events = [];
let selectedId = null;
let evtSource = null;
let activeFilter = 'all';
let activeSession = 'all';
let paused = false;

let totalTokens = {input: 0, output: 0};  // Cumulative (billing)
let lastInputTokens = 0;  // Latest API call's input_tokens (= current context size)
let turnCount = 0;
const CTX_LIMIT = 200000;

const FILTER_MAP = {
  all: null,
  prompt: ['prompt_submit'],
  memory: ['memory_recall', 'tool_recall'],
  llm: ['llm_response', 'thinking'],
  tools: ['tool_use', 'tool_result', 'tool_start', 'tool_success', 'skill_invoke'],
  error: ['tool_error', 'api_error'],
  agents: ['subagent_task', 'subagent_start', 'subagent_stop'],
  session: ['session_start', 'session_end', 'checkpoint', 'compact', 'turn_complete', 'stop', 'compact_start', 'context_breakdown'],
};

const PIPE_COUNT_MAP = {
  prompt: ['prompt_submit'],
  memory: ['memory_recall', 'tool_recall'],
  llm: ['llm_response'],
  tools: ['tool_use', 'tool_result', 'tool_start', 'tool_success', 'skill_invoke'],
  error: ['tool_error', 'api_error'],
  agents: ['subagent_task', 'subagent_start', 'subagent_stop'],
  session: ['session_start', 'session_end', 'turn_complete'],
};

function filterPipeline(step) {
  if (activeFilter === step) step = 'all';
  activeFilter = step;
  document.querySelectorAll('.pipe-step').forEach(el => el.classList.remove('filtered'));
  document.getElementById('pipe-' + (step === 'all' ? 'all' : step)).classList.add('filtered');
  renderTimeline();
}

function filterSession(sid) {
  activeSession = sid;
  document.querySelectorAll('.session-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + (sid === 'all' ? 'all' : sid)).classList.add('active');
  renderTimeline();
}

function getFilteredEvents() {
  let filtered = events;
  if (activeSession !== 'all') {
    filtered = filtered.filter(e => e.session_id === activeSession);
  }
  if (activeFilter !== 'all' && FILTER_MAP[activeFilter]) {
    const types = FILTER_MAP[activeFilter];
    filtered = filtered.filter(e => types.includes(e.type));
  }
  return filtered;
}

function togglePause() {
  paused = !paused;
  const btn = document.getElementById('pause-btn');
  if (paused) {
    btn.textContent = 'Resume';
    btn.classList.add('active');
  } else {
    btn.textContent = 'Pause';
    btn.classList.remove('active');
    renderTimeline();
  }
}

function connect() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/events');
  evtSource.onopen = () => {
    document.getElementById('status-dot').className = 'status-dot connected';
    document.getElementById('status-text').textContent = 'Connected';
  };
  evtSource.onerror = () => {
    document.getElementById('status-dot').className = 'status-dot disconnected';
    document.getElementById('status-text').textContent = 'Reconnecting...';
    // EventSource auto-reconnects
  };
  evtSource.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      if (evt.error) return;
      if (events.find(x => x.id === evt.id)) return;
      events.unshift(evt);
      if (events.length > 1000) events.pop();
      // Track tokens and turns
      if (evt.type === 'llm_response') {
        const d = evt.data || {};
        totalTokens.input = d.total_input || (totalTokens.input + (d.input_tokens||0));
        totalTokens.output = d.total_output || (totalTokens.output + (d.output_tokens||0));
        lastInputTokens = d.input_tokens || lastInputTokens;
        updateTokenGauge();
        updateContextBar();
        pushSparkToken(d.output_tokens||0);
        updateCost();
        if (perfVisible) renderPerf();
      }
      if (evt.type === 'turn_complete') {
        turnCount = (evt.data||{}).turn_number || (turnCount + 1);
        document.getElementById('turn-counter').textContent = turnCount;
        pushSparkTurn((evt.data||{}).duration_ms||0);
        if (perfVisible) renderPerf();
      }
      if (evt.type === 'context_breakdown') {
        updateBreakdownBar(evt.data || {});
      }
      if (!paused) {
        renderTimeline();
        updatePipeline(evt);
        updateSessionTabs();
      }
      updateCounts();
      document.getElementById('event-count').textContent = events.length + ' events';
    } catch(err) {}
  };
}

function formatTime(ts) {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function formatSessionId(sid) {
  if (!sid) return '?';
  return sid.length > 16 ? sid.slice(0, 16) + '..' : sid;
}

function badgeClass(type) {
  return 'badge-' + (type || 'unknown');
}

function fmtTok(n) {
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return String(n);
}

function summarize(evt) {
  const d = evt.data || {};
  switch(evt.type) {
    case 'session_start':
      return 'Session started' + (d.cwd ? ' | ' + d.cwd.split(/[/\\\\]/).pop() : '');
    case 'session_end':
      return (d.outcome || 'ended') + (d.summary ? ' | ' + d.summary.slice(0, 60) : '');
    case 'prompt_submit':
      return (d.prompt_preview || '').slice(0, 80) || 'prompt (' + (d.prompt_length || 0) + ' chars)';
    case 'memory_recall':
      return (d.results_count || 0) + ' memories | ' + (d.search_mode || 'hybrid');
    case 'tool_recall':
      return (d.tool_name || '?') + (d.bash_sub ? ':' + d.bash_sub : '') + ' | ' + (d.memories_count || 0) + ' mem, ' + (d.errors_count || 0) + ' err';
    case 'tool_start':
      return (d.tool_name || '?') + ' | ' + (d.tool_input_preview || '').slice(0, 60);
    case 'tool_success':
      return (d.tool_name || '?') + ' OK | ' + ((d.response_preview || '').slice(0, 60));
    case 'tool_error':
      return (d.tool_name || '?') + ' FAIL' + (d.is_new_error ? ' [NEW]' : ' [KNOWN]') + ' | ' + (d.error_preview || '').slice(0, 50);
    case 'checkpoint':
      return 'Transcript checkpoint | ' + (d.transcript_growth_kb || 0) + ' KB';
    case 'compact':
      return 'Context compaction | ' + (d.reason || '');
    case 'llm_response':
      return (d.model || '?').split('-').slice(-2).join('-') + ' | ' + fmtTok(d.output_tokens||0) + ' out | ' + (d.tool_call_count||0) + ' tools' + (d.has_thinking ? ' | thinking' : '');
    case 'tool_use':
      return (d.tool_name || '?') + ' | ' + (d.input_preview || '').slice(0, 60);
    case 'tool_result':
      return (d.is_error ? 'ERROR | ' : 'OK | ') + (d.result_preview || '').slice(0, 60);
    case 'thinking':
      return (d.thinking_preview || '').slice(0, 80);
    case 'turn_complete':
      return 'Turn ' + (d.turn_number||'?') + ' | ' + ((d.duration_ms||0)/1000).toFixed(1) + 's | ' + (d.message_count||0) + ' msgs';
    case 'api_error':
      return 'HTTP ' + (d.status_code||'?') + ' | retry ' + (d.retry_attempt||0) + '/' + (d.max_retries||0);
    case 'skill_invoke':
      return '/' + (d.skill_name || '?') + (d.skill_args ? ' ' + d.skill_args.slice(0,40) : '');
    case 'subagent_task':
      return (d.operation || '?') + ' | ' + (d.task_summary || '').slice(0, 60);
    case 'stop':
      return 'Stop | ' + (d.stop_reason || '?');
    case 'subagent_start':
      return (d.agent_type || '?') + (d.agent_name ? ' | ' + d.agent_name : '');
    case 'subagent_stop':
      return (d.agent_type || '?') + ' | ' + ((d.duration_ms||0)/1000).toFixed(1) + 's';
    case 'compact_start':
      return 'Context compacting | ' + (d.reason || 'context_limit');
    case 'context_breakdown': {
      const cats = d.categories || {};
      const parts = [];
      for (const [k,v] of Object.entries(cats)) { if (v.est_tokens > 0) parts.push(k.replace(/_/g,' ') + ':' + fmtTok(v.est_tokens)); }
      return 'Turn ' + (d.turn_number||'?') + ' | ' + parts.join(', ');
    }
    default:
      return JSON.stringify(d).slice(0, 60);
  }
}

function renderTimeline() {
  const container = document.getElementById('timeline');
  const scrollTop = container.scrollTop;
  const filtered = getFilteredEvents();
  const countLabel = activeFilter === 'all' && activeSession === 'all'
    ? filtered.length + ' events'
    : filtered.length + '/' + events.length + ' events';
  document.getElementById('event-count').textContent = countLabel;
  container.innerHTML = filtered.map(evt => {
    const sel = evt.id === selectedId ? ' selected' : '';
    const dim = searchQuery && !matchesSearch(evt) ? ' search-dim' : '';
    const typeLabel = (evt.type || 'unknown').replace(/_/g, ' ');
    const timeStr = relativeTime ? formatRelTime(evt.ts) : formatTime(evt.ts);
    return '<div class="event-item' + sel + dim + '" onclick="selectEvent(\\''+evt.id+'\\')">'+
      '<span class="event-time">'+timeStr+'</span>'+
      '<span class="event-badge '+badgeClass(evt.type)+'">'+escapeHtml(typeLabel)+'</span>'+
      '<span class="event-summary">'+escapeHtml(summarize(evt))+'</span>'+
    '</div>';
  }).join('');
  container.scrollTop = scrollTop;
}

function selectEvent(id) {
  selectedId = id;
  renderTimeline();
  const evt = events.find(e => e.id === id);
  if (evt) renderDetail(evt);
}

function renderDetail(evt) {
  const panel = document.getElementById('detail');
  let html = '';
  const d = evt.data || {};

  // Header section
  html += '<div class="detail-section"><h3>' + escapeHtml((evt.type||'').toUpperCase().replace(/_/g,' ')) + ' &mdash; ' + formatTime(evt.ts) + ' <button class="expand-btn" id="copy-btn" onclick="copyDetail()" style="float:right">Copy</button></h3>';
  if (evt.session_id) html += row('Session', escapeHtml(formatSessionId(evt.session_id)));
  if (evt.project) html += row('Project', escapeHtml(evt.project.split(/[/\\\\]/).pop()));
  if (evt.agent_id) html += row('Agent', escapeHtml(evt.agent_id));
  html += '</div>';

  switch(evt.type) {
    case 'session_start':
      html += '<div class="detail-section"><h3>Session Start</h3>';
      if (d.claude_uuid) html += row('Claude UUID', escapeHtml(d.claude_uuid));
      if (d.cwd) html += row('Working Dir', escapeHtml(d.cwd));
      if (d.transcript_path) html += row('Transcript', escapeHtml(d.transcript_path));
      html += '</div>';
      break;

    case 'session_end':
      html += '<div class="detail-section"><h3>Session End</h3>';
      html += row('Outcome', '<span class="detail-val ' + (d.outcome === 'success' ? 'good' : 'bad') + '">' + escapeHtml(d.outcome || '?') + '</span>');
      if (d.summary) {
        html += '<h3 style="margin-top:12px">Summary</h3><pre class="detail-code">' + escapeHtml(d.summary) + '</pre>';
      }
      html += '</div>';
      break;

    case 'prompt_submit':
      html += '<div class="detail-section"><h3>User Prompt</h3>';
      html += row('Length', (d.prompt_length || 0) + ' chars');
      html += row('First Turn', d.is_first_turn ? '<span class="detail-val warn">Yes</span>' : 'No');
      if (d.prompt_preview) {
        html += '<h3 style="margin-top:12px">Prompt Text</h3><pre class="detail-code">' + escapeHtml(d.prompt_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'memory_recall':
      html += '<div class="detail-section"><h3>Memory Recall</h3>';
      html += row('Results', '<span style="color:var(--green);font-weight:600">' + (d.results_count || 0) + ' memories</span>');
      html += row('Search Mode', escapeHtml(d.search_mode || 'hybrid'));
      if (d.query_preview) html += row('Query', escapeHtml(d.query_preview));
      html += '</div>';
      if (d.memories && d.memories.length) {
        html += '<div class="detail-section"><h3>Injected Memories (' + d.memories.length + ')</h3>';
        d.memories.forEach(m => {
          html += '<div class="mem-item">';
          html += '<span class="mem-id">[mem:' + m.id + ']</span>';
          if (m.similarity != null) html += '<span class="mem-score">sim:' + Number(m.similarity).toFixed(2) + '</span>';
          if (m.category) html += '<span class="mem-cat">' + escapeHtml(m.category) + '</span>';
          html += ' ' + importanceBar(m.importance || 5);
          if (m.content_preview) html += '<div class="mem-content">' + escapeHtml(m.content_preview) + '</div>';
          html += '</div>';
        });
        html += '</div>';
      }
      break;

    case 'tool_recall':
      html += '<div class="detail-section"><h3>Tool Recall</h3>';
      html += row('Tool', '<span style="color:var(--orange);font-weight:600">' + escapeHtml(d.tool_name || '?') + '</span>');
      if (d.bash_sub) html += row('Bash Sub-type', escapeHtml(d.bash_sub));
      html += row('Memories Found', (d.memories_count || 0));
      html += row('Errors Found', (d.errors_count || 0));
      if (d.injected_context_preview) {
        html += '<h3 style="margin-top:12px">Injected Context</h3><pre class="detail-code">' + escapeHtml(d.injected_context_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'tool_success':
      html += '<div class="detail-section"><h3>Tool Success</h3>';
      html += row('Tool', '<span style="color:var(--orange);font-weight:600">' + escapeHtml(d.tool_name || '?') + '</span>');
      html += row('Status', '<span class="detail-val good">OK</span>');
      if (d.response_preview) {
        html += '<h3 style="margin-top:12px">Response</h3><pre class="detail-code">' + escapeHtml(d.response_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'tool_error':
      html += '<div class="detail-section"><h3>Tool Error</h3>';
      html += row('Tool', '<span style="color:var(--orange);font-weight:600">' + escapeHtml(d.tool_name || '?') + '</span>');
      html += row('Status', '<span class="detail-val bad">FAILED</span>');
      html += row('New Error', d.is_new_error ? '<span class="detail-val warn">Yes (new pattern)</span>' : '<span class="detail-val good">No (known pattern)</span>');
      if (d.matched_solution_id) html += row('Matched Solution', '[err:' + escapeHtml(d.matched_solution_id) + ']');
      if (d.error_preview) {
        html += '<h3 style="margin-top:12px">Error Output</h3><pre class="detail-code" style="color:var(--red)">' + escapeHtml(d.error_preview) + '</pre>';
      }
      if (d.error_pattern) {
        html += '<h3 style="margin-top:12px">Error Pattern</h3><pre class="detail-code">' + escapeHtml(d.error_pattern) + '</pre>';
      }
      html += '</div>';
      break;

    case 'checkpoint':
      html += '<div class="detail-section"><h3>Transcript Checkpoint</h3>';
      if (d.transcript_growth_kb) html += row('Growth', d.transcript_growth_kb + ' KB');
      if (d.segment_summary) {
        html += '<h3 style="margin-top:12px">Segment Summary</h3><pre class="detail-code">' + escapeHtml(d.segment_summary) + '</pre>';
      }
      html += '</div>';
      break;

    case 'llm_response':
      html += '<div class="detail-section"><h3>LLM Response</h3>';
      html += row('Model', '<span style="color:var(--accent);font-weight:600">' + escapeHtml(d.model||'?') + '</span>');
      html += row('Stop Reason', escapeHtml(d.stop_reason || '?'));
      html += row('Input Tokens', fmtTok(d.input_tokens||0));
      html += row('Output Tokens', '<span style="color:var(--yellow);font-weight:600">' + fmtTok(d.output_tokens||0) + '</span>');
      html += row('Cache Read', fmtTok(d.cache_read_tokens||0));
      html += row('Cache Created', fmtTok(d.cache_creation_tokens||0));
      html += row('Total In (session)', fmtTok(d.total_input||0));
      html += row('Total Out (session)', fmtTok(d.total_output||0));
      html += row('Has Thinking', d.has_thinking ? '<span class="detail-val warn">Yes</span>' : 'No');
      html += '</div>';
      if (d.tool_calls && d.tool_calls.length) {
        html += '<div class="detail-section"><h3>Tool Calls (' + d.tool_calls.length + ')</h3>';
        d.tool_calls.forEach(tc => {
          html += '<div class="tool-call-item"><span class="tool-call-name">' + escapeHtml(tc.name) + '</span>';
          html += '<div style="font-size:11px;color:var(--text-dim);margin-top:2px">' + escapeHtml(tc.input_preview||'') + '</div></div>';
        });
        html += '</div>';
      }
      if (d.text_preview) {
        html += '<div class="detail-section"><h3>Response Text</h3><pre class="detail-code">' + escapeHtml(d.text_preview) + '</pre></div>';
      }
      break;

    case 'tool_use':
      html += '<div class="detail-section"><h3>Tool Use</h3>';
      html += row('Tool', '<span style="color:var(--orange);font-weight:600">' + escapeHtml(d.tool_name||'?') + '</span>');
      html += row('Tool ID', '<span style="font-size:11px">' + escapeHtml(d.tool_id||'') + '</span>');
      if (d.input_preview) {
        html += '<h3 style="margin-top:12px">Input</h3><pre class="detail-code">' + escapeHtml(d.input_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'tool_result':
      html += '<div class="detail-section"><h3>Tool Result</h3>';
      html += row('Status', d.is_error ? '<span class="detail-val bad">ERROR</span>' : '<span class="detail-val good">OK</span>');
      html += row('Tool Use ID', '<span style="font-size:11px">' + escapeHtml(d.tool_use_id||'') + '</span>');
      html += row('Result Length', (d.result_length||0) + ' chars');
      if (d.result_preview) {
        html += '<h3 style="margin-top:12px">Result</h3><pre class="detail-code"' + (d.is_error ? ' style="color:var(--red)"' : '') + '>' + escapeHtml(d.result_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'thinking':
      html += '<div class="detail-section"><h3>Thinking Block</h3>';
      html += row('Length', (d.thinking_length||0) + ' chars');
      if (d.thinking_preview) {
        html += '<h3 style="margin-top:12px">Thinking Content</h3><pre class="detail-code" style="color:var(--pink);border-color:rgba(247,120,186,0.3)">' + escapeHtml(d.thinking_preview) + '</pre>';
      }
      html += '</div>';
      break;

    case 'turn_complete':
      html += '<div class="detail-section"><h3>Turn Complete</h3>';
      html += row('Turn Number', '<span style="color:var(--purple);font-weight:600">' + (d.turn_number||'?') + '</span>');
      html += row('Duration', ((d.duration_ms||0)/1000).toFixed(1) + 's (' + (d.duration_ms||0) + 'ms)');
      html += row('Messages (loops)', '<span style="color:var(--accent);font-weight:600">' + (d.message_count||0) + '</span>');
      html += '</div>';
      break;

    case 'api_error':
      html += '<div class="detail-section"><h3>API Error</h3>';
      html += row('Status', '<span class="detail-val bad">HTTP ' + (d.status_code||'?') + '</span>');
      html += row('Retry', (d.retry_attempt||0) + ' / ' + (d.max_retries||0));
      html += row('Retry In', (d.retry_in_ms||0) + 'ms');
      if (d.error_message) {
        html += '<h3 style="margin-top:12px">Error</h3><pre class="detail-code" style="color:var(--red)">' + escapeHtml(d.error_message) + '</pre>';
      }
      html += '</div>';
      break;

    case 'skill_invoke':
      html += '<div class="detail-section"><h3>Skill Invocation</h3>';
      html += row('Skill', '<span style="color:var(--teal);font-weight:600">/' + escapeHtml(d.skill_name||'?') + '</span>');
      if (d.skill_args) html += row('Args', escapeHtml(d.skill_args));
      html += '</div>';
      break;

    case 'subagent_task':
      html += '<div class="detail-section"><h3>Subagent Task</h3>';
      html += row('Operation', escapeHtml(d.operation || '?'));
      if (d.task_summary) {
        html += '<h3 style="margin-top:12px">Task</h3><pre class="detail-code">' + escapeHtml(d.task_summary) + '</pre>';
      }
      html += '</div>';
      break;

    case 'stop':
      html += '<div class="detail-section"><h3>Session Stop</h3>';
      html += row('Reason', escapeHtml(d.stop_reason || '?'));
      html += '</div>';
      break;

    case 'subagent_start':
      html += '<div class="detail-section"><h3>Subagent Start</h3>';
      html += row('Type', '<span style="color:var(--teal)">' + escapeHtml(d.agent_type||'?') + '</span>');
      if (d.agent_name) html += row('Name', escapeHtml(d.agent_name));
      html += '</div>';
      break;

    case 'subagent_stop':
      html += '<div class="detail-section"><h3>Subagent Stop</h3>';
      html += row('Type', escapeHtml(d.agent_type||'?'));
      html += row('Duration', ((d.duration_ms||0)/1000).toFixed(1) + 's');
      html += '</div>';
      break;

    case 'compact_start':
      html += '<div class="detail-section"><h3>Context Compaction</h3>';
      html += row('Reason', escapeHtml(d.reason || 'context_limit'));
      html += '</div>';
      break;

    case 'context_breakdown':
      html += '<div class="detail-section"><h3>Context Breakdown &mdash; Turn ' + (d.turn_number||'?') + '</h3>';
      html += row('Total Input', fmtTok(d.total_input||0));
      html += row('Total Output', fmtTok(d.total_output||0));
      html += row('Cache Read', fmtTok(d.cache_read_tokens||0));
      html += row('Cache Created', fmtTok(d.cache_creation_tokens||0));
      html += '</div>';
      if (d.categories) {
        html += '<div class="detail-section"><h3>Content Categories</h3>';
        const catNames = {hook_injections:'Hook Injections', user_prompts:'User Prompts', tool_results:'Tool Results', assistant_text:'Assistant Text', tool_calls:'Tool Calls'};
        for (const [key, label] of Object.entries(catNames)) {
          const cat = d.categories[key] || {};
          html += row(label, '<span style="color:var(--accent)">' + (cat.count||0) + ' items</span> &mdash; ~' + fmtTok(cat.est_tokens||0) + ' tokens');
          if (cat.recent && cat.recent.length) {
            cat.recent.forEach(item => {
              html += '<div style="margin-left:140px;padding:3px 6px;margin-top:2px;border-left:2px solid var(--border);font-size:11px;color:var(--text-dim);max-height:40px;overflow:hidden">' + escapeHtml(item).slice(0,200) + '</div>';
            });
          }
        }
        html += '</div>';
      }
      break;

    default:
      html += '<div class="detail-section"><h3>Raw Data</h3><pre class="detail-code">' + escapeHtml(JSON.stringify(d, null, 2)) + '</pre></div>';
  }

  panel.innerHTML = html;
}

function row(key, val) {
  return '<div class="detail-row"><span class="detail-key">' + key + '</span><span class="detail-val">' + val + '</span></div>';
}

function importanceBar(n) {
  let html = '<span class="importance-bar">';
  for (let i = 1; i <= 10; i++) {
    html += '<span class="importance-pip' + (i <= n ? ' filled' : '') + '"></span>';
  }
  html += '</span> ' + n + '/10';
  return html;
}

function escapeHtml(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function updatePipeline(evt) {
  const map = {
    prompt_submit: 'pipe-prompt',
    memory_recall: 'pipe-memory', tool_recall: 'pipe-memory',
    llm_response: 'pipe-llm', thinking: 'pipe-llm',
    tool_use: 'pipe-tools', tool_result: 'pipe-tools', tool_start: 'pipe-tools',
    tool_success: 'pipe-tools', skill_invoke: 'pipe-tools',
    tool_error: 'pipe-error', api_error: 'pipe-error',
    subagent_task: 'pipe-agents', subagent_start: 'pipe-agents', subagent_stop: 'pipe-agents',
    session_start: 'pipe-session', session_end: 'pipe-session',
    turn_complete: 'pipe-session', checkpoint: 'pipe-session',
    stop: 'pipe-session', compact_start: 'pipe-session',
  };
  document.querySelectorAll('.pipe-step').forEach(el => el.classList.remove('active'));
  const id = map[evt.type];
  if (id) document.getElementById(id).classList.add('active');
}

function updateTokenGauge() {
  const total = totalTokens.input + totalTokens.output;
  document.getElementById('token-gauge').textContent = fmtTok(total) + ' (' + fmtTok(totalTokens.input) + ' in / ' + fmtTok(totalTokens.output) + ' out)';
}

function updateContextBar() {
  const used = lastInputTokens;
  const pct = Math.min(100, (used / CTX_LIMIT) * 100);
  document.getElementById('context-label').textContent = 'Context: ~' + pct.toFixed(0) + '% (' + fmtTok(used) + ' / ~200K tokens)';
}

let latestBreakdown = null;

function updateBreakdownBar(bd) {
  latestBreakdown = bd;
  const cats = bd.categories || {};
  const catTotal = bd.cat_total_est || 0;
  const cache = bd.cache_read_tokens || 0;

  // Use server-computed proportions for segments (relative to category sum)
  // Cache proportion is estimated as cache / (cache + catTotal)
  const grandTotal = cache + catTotal || 1;

  const segs = {
    cache: {tokens: cache, pct: (cache / grandTotal) * 100},
    hook_injections: {tokens: (cats.hook_injections||{}).est_tokens||0, pct: ((cats.hook_injections||{}).proportion||0) * (catTotal/grandTotal) * 100},
    user_prompts: {tokens: (cats.user_prompts||{}).est_tokens||0, pct: ((cats.user_prompts||{}).proportion||0) * (catTotal/grandTotal) * 100},
    tool_results: {tokens: (cats.tool_results||{}).est_tokens||0, pct: ((cats.tool_results||{}).proportion||0) * (catTotal/grandTotal) * 100},
    assistant_text: {tokens: (cats.assistant_text||{}).est_tokens||0, pct: ((cats.assistant_text||{}).proportion||0) * (catTotal/grandTotal) * 100},
    tool_calls: {tokens: (cats.tool_calls||{}).est_tokens||0, pct: ((cats.tool_calls||{}).proportion||0) * (catTotal/grandTotal) * 100},
  };

  const segMap = {cache:'seg-cache', hook_injections:'seg-hooks', user_prompts:'seg-prompts',
                  tool_results:'seg-results', assistant_text:'seg-assistant', tool_calls:'seg-tools'};
  const legMap = {cache:'leg-cache', hook_injections:'leg-hooks', user_prompts:'leg-prompts',
                  tool_results:'leg-results', assistant_text:'leg-assistant', tool_calls:'leg-tools'};

  for (const [key, info] of Object.entries(segs)) {
    const el = document.getElementById(segMap[key]);
    if (el) el.style.width = Math.max(0, info.pct).toFixed(1) + '%';
    const leg = document.getElementById(legMap[key]);
    if (leg) leg.textContent = fmtTok(info.tokens);
  }
}

function drilldown(category) {
  const dd = document.getElementById('context-drilldown');
  if (!latestBreakdown) { dd.className = ''; return; }

  if (category === 'cache') {
    dd.className = 'visible';
    dd.innerHTML = '<h4>System / Cache (' + fmtTok(latestBreakdown.cache_read_tokens||0) + ' tokens)</h4>' +
      '<div class="drilldown-item">Cached content includes: system prompt, CLAUDE.md, conversation history from prior turns. Exact content not available from transcript.</div>';
    return;
  }

  const cat = (latestBreakdown.categories||{})[category];
  if (!cat || !cat.recent || cat.recent.length === 0) {
    dd.className = 'visible';
    dd.innerHTML = '<h4>' + escapeHtml(category.replace(/_/g,' ')) + '</h4><div class="drilldown-item">No items yet</div>';
    return;
  }

  dd.className = 'visible';
  let html = '<h4>' + escapeHtml(category.replace(/_/g,' ')) + ' (' + cat.count + ' items, ~' + fmtTok(cat.est_tokens) + ' tokens)</h4>';
  cat.recent.forEach((item, i) => {
    html += '<div class="drilldown-item" style="border-color:var(--border)">' + escapeHtml(item) + '</div>';
  });
  dd.innerHTML = html;
}

function updateCounts() {
  for (const [step, types] of Object.entries(PIPE_COUNT_MAP)) {
    const count = events.filter(e => types.includes(e.type)).length;
    const el = document.getElementById('cnt-' + step);
    if (el) el.textContent = count;
  }
}

function updateSessionTabs() {
  const sessions = {};
  events.forEach(e => {
    const sid = e.session_id;
    if (sid) {
      if (!sessions[sid]) sessions[sid] = {id: sid, count: 0, project: e.project || ''};
      sessions[sid].count++;
    }
  });
  const bar = document.getElementById('sessions-bar');
  let html = '<span id="sessions-label">Sessions:</span>';
  html += '<div class="session-tab' + (activeSession === 'all' ? ' active' : '') + '" id="tab-all" onclick="filterSession(\\'all\\')">All</div>';
  Object.values(sessions).forEach(s => {
    const label = escapeHtml(formatSessionId(s.id));
    const proj = s.project ? escapeHtml(s.project.split(/[/\\\\]/).pop()) : '';
    const isActive = activeSession === s.id ? ' active' : '';
    const safeSid = escapeHtml(s.id);
    html += '<div class="session-tab' + isActive + '" data-sid="' + safeSid + '" onclick="filterSession(\\''+safeSid+'\\')">'+
      (proj ? proj + ' | ' : '') + label +
      '<span class="session-count">' + s.count + '</span></div>';
  });
  bar.innerHTML = html;
}

function clearEvents() {
  events.length = 0;
  selectedId = null;
  sparkTokenData.length = 0;
  sparkTurnData.length = 0;
  totalTokens = {input: 0, output: 0};
  lastInputTokens = 0;
  turnCount = 0;
  latestBreakdown = null;
  renderTimeline();
  document.getElementById('detail').innerHTML = '<div id="detail-placeholder">Select an event to view details</div>';
  document.getElementById('event-count').textContent = '0 events';
  document.getElementById('token-gauge').textContent = '0';
  document.getElementById('turn-counter').textContent = '0';
  document.getElementById('cost-badge').textContent = '$0.00';
  updateCounts();
  updateSessionTabs();
  updateContextBar();
  const c1 = document.getElementById('spark-tokens'); if (c1) c1.getContext('2d').clearRect(0,0,c1.width,c1.height);
  const c2 = document.getElementById('spark-turns'); if (c2) c2.getContext('2d').clearRect(0,0,c2.width,c2.height);
}

// ── 3a: Search & Keyboard Navigation ──
let searchQuery = '';

function onSearch() {
  searchQuery = document.getElementById('search-input').value.toLowerCase();
  renderTimeline();
}

function matchesSearch(evt) {
  if (!searchQuery) return true;
  const hay = (evt.type + ' ' + summarize(evt) + ' ' + JSON.stringify(evt.data)).toLowerCase();
  return hay.includes(searchQuery);
}

document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement||{}).tagName;
  if (e.key === '/' && tag !== 'INPUT') { e.preventDefault(); document.getElementById('search-input').focus(); return; }
  if ((e.ctrlKey||e.metaKey) && e.key === 'k') { e.preventDefault(); document.getElementById('search-input').focus(); return; }
  if (e.key === 'Escape') {
    document.getElementById('search-input').value = '';
    searchQuery = '';
    document.getElementById('search-input').blur();
    selectedId = null;
    renderTimeline();
    document.getElementById('detail').innerHTML = '<div id="detail-placeholder">Select an event to view details</div>';
    return;
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const filtered = getFilteredEvents();
    if (!filtered.length) return;
    const curIdx = filtered.findIndex(ev => ev.id === selectedId);
    let ni = e.key === 'ArrowDown' ? (curIdx < filtered.length - 1 ? curIdx + 1 : 0) : (curIdx > 0 ? curIdx - 1 : filtered.length - 1);
    selectEvent(filtered[ni].id);
    const item = document.querySelector('.event-item.selected');
    if (item) item.scrollIntoView({block: 'nearest'});
    return;
  }
  if (tag === 'INPUT') return;
  const pipeSteps = ['prompt','memory','llm','tools','error','agents','session'];
  if (e.key >= '1' && e.key <= '7') { filterPipeline(pipeSteps[parseInt(e.key)-1]); return; }
  if (e.key === '0') filterPipeline('all');
});

// ── 3b: Sparkline Charts ──
const sparkTokenData = [];
const sparkTurnData = [];
const SPARK_MAX = 30;

function drawSparkline(canvasId, data, color, tipId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data.length) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(...data) || 1;
  const step = w / Math.max(data.length - 1, 1);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = i * step;
    const y = h - (v / max) * (h - 4) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
  // Fill area under line
  ctx.lineTo((data.length - 1) * step, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = color.replace(')', ',0.1)').replace('rgb', 'rgba');
  ctx.fill();

  // Tooltip on hover
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.min(data.length - 1, Math.max(0, Math.round(mx / step)));
    const tip = document.getElementById(tipId);
    if (tip) {
      tip.style.display = 'block';
      tip.style.left = (e.clientX + 8) + 'px';
      tip.style.top = (e.clientY - 20) + 'px';
      tip.style.position = 'fixed';
      tip.textContent = fmtTok(data[idx]);
    }
  };
  canvas.onmouseleave = () => {
    const tip = document.getElementById(tipId);
    if (tip) tip.style.display = 'none';
  };
}

function pushSparkToken(val) {
  sparkTokenData.push(val);
  if (sparkTokenData.length > SPARK_MAX) sparkTokenData.shift();
  drawSparkline('spark-tokens', sparkTokenData, 'rgb(210,153,34)', 'tip-tokens');
}

function pushSparkTurn(val) {
  sparkTurnData.push(val);
  if (sparkTurnData.length > SPARK_MAX) sparkTurnData.shift();
  drawSparkline('spark-turns', sparkTurnData, 'rgb(188,140,255)', 'tip-turns');
}

// ── Cost estimation (standalone, called on every llm_response) ──
const COST_PER_MTok = {input: 15, output: 75, cache_read: 1.5, cache_create: 18.75};  // Opus pricing $/1M tokens

function updateCost() {
  const llms = (activeSession !== 'all' ? events.filter(e => e.session_id === activeSession) : events).filter(e => e.type === 'llm_response');
  let cIn = 0, cOut = 0, cRead = 0, cCreate = 0;
  llms.forEach(e => {
    const d = e.data || {};
    cIn += d.input_tokens || 0;       // Non-cached input (already excludes cache)
    cOut += d.output_tokens || 0;
    cRead += d.cache_read_tokens || 0;
    cCreate += d.cache_creation_tokens || 0;
  });
  const total = (cIn * COST_PER_MTok.input + cOut * COST_PER_MTok.output + cRead * COST_PER_MTok.cache_read + cCreate * COST_PER_MTok.cache_create) / 1000000;
  document.getElementById('cost-badge').textContent = '$' + total.toFixed(2);
  return {costIn: cIn, costOut: cOut, costCache: cRead, costCreate: cCreate, totalCost: total};
}

// ── 3d: Performance Profiling ──
let perfVisible = false;

function togglePerf() {
  perfVisible = !perfVisible;
  const panel = document.getElementById('perf-panel');
  const btn = document.getElementById('perf-btn');
  if (perfVisible) {
    panel.className = 'visible';
    btn.classList.add('active');
    renderPerf();
  } else {
    panel.className = '';
    btn.classList.remove('active');
  }
}

function renderPerf() {
  const panel = document.getElementById('perf-panel');
  const filtered = activeSession !== 'all' ? events.filter(e => e.session_id === activeSession) : events;

  // Turn breakdown
  const turns = filtered.filter(e => e.type === 'turn_complete').sort((a,b) => (a.data.turn_number||0) - (b.data.turn_number||0));
  const llms = filtered.filter(e => e.type === 'llm_response');
  const tools = filtered.filter(e => e.type === 'tool_use');

  // Tool frequency
  const toolFreq = {};
  tools.forEach(e => { const n = (e.data||{}).tool_name||'?'; toolFreq[n] = (toolFreq[n]||0) + 1; });
  const toolSorted = Object.entries(toolFreq).sort((a,b) => b[1] - a[1]);
  const maxFreq = toolSorted.length ? toolSorted[0][1] : 1;

  // Cost estimate (reuse updateCost which has correct formula)
  const costs = updateCost();
  const totalCost = costs.totalCost;

  let html = '<h3>Performance Profile' + (activeSession !== 'all' ? ' (filtered session)' : ' (all sessions)') + '</h3>';

  // Cost summary
  html += '<div style="margin-bottom:12px"><span class="cost-badge">Est. Cost: $' + totalCost.toFixed(4) + '</span>';
  html += ' <span style="color:var(--text-dim);font-size:11px">(' + fmtTok(costs.costIn) + ' input + ' + fmtTok(costs.costOut) + ' output + ' + fmtTok(costs.costCache) + ' cache-read + ' + fmtTok(costs.costCreate) + ' cache-write @ Opus pricing)</span></div>';

  // Tool frequency chart
  if (toolSorted.length) {
    html += '<h3>Tool Usage (' + tools.length + ' calls)</h3><table class="perf-table">';
    html += '<tr><th>Tool</th><th>Count</th><th style="width:60%">Frequency</th></tr>';
    const colors = {Read:'var(--accent)',Edit:'var(--green)',Write:'var(--green)',Bash:'var(--orange)',Agent:'var(--teal)',Grep:'var(--cyan)',Glob:'var(--cyan)',Skill:'var(--teal)'};
    toolSorted.slice(0, 15).forEach(([name, count]) => {
      const pct = (count / maxFreq * 100).toFixed(0);
      const c = colors[name] || 'var(--text-dim)';
      html += '<tr><td style="color:' + c + ';font-weight:600">' + escapeHtml(name) + '</td><td>' + count + '</td>';
      html += '<td><span class="perf-bar" style="width:' + pct + '%;background:' + c + '"></span></td></tr>';
    });
    html += '</table>';
  }

  // Slowest turns
  if (turns.length) {
    const slowest = [...turns].sort((a,b) => (b.data.duration_ms||0) - (a.data.duration_ms||0)).slice(0, 5);
    html += '<h3 style="margin-top:12px">Slowest Turns</h3><table class="perf-table">';
    html += '<tr><th>Turn</th><th>Duration</th><th>Messages</th></tr>';
    slowest.forEach(t => {
      const d = t.data || {};
      html += '<tr><td style="color:var(--purple)">#' + (d.turn_number||'?') + '</td>';
      html += '<td style="color:var(--yellow)">' + ((d.duration_ms||0)/1000).toFixed(1) + 's</td>';
      html += '<td>' + (d.message_count||0) + '</td></tr>';
    });
    html += '</table>';
  }

  // Turn breakdown table
  if (turns.length) {
    html += '<h3 style="margin-top:12px">Turn Breakdown (' + turns.length + ' turns)</h3><table class="perf-table">';
    html += '<tr><th>#</th><th>Duration</th><th>Msgs</th></tr>';
    turns.forEach(t => {
      const d = t.data || {};
      html += '<tr><td>' + (d.turn_number||'?') + '</td>';
      html += '<td>' + ((d.duration_ms||0)/1000).toFixed(1) + 's</td>';
      html += '<td>' + (d.message_count||0) + '</td></tr>';
    });
    html += '</table>';
  }

  panel.innerHTML = html;
}

// ── 3e: Export & Clipboard ──
function exportEvents() {
  const filtered = getFilteredEvents();
  const blob = new Blob([JSON.stringify(filtered, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const sid = activeSession !== 'all' ? activeSession.slice(0,12) : 'all';
  a.href = url;
  a.download = 'claude-trace-' + sid + '-' + new Date().toISOString().slice(0,19).replace(/[:]/g,'-') + '.json';
  a.click();
  URL.revokeObjectURL(url);
}

function copyDetail() {
  const evt = events.find(e => e.id === selectedId);
  if (!evt) return;
  const text = evt.type.toUpperCase() + ' @ ' + formatTime(evt.ts) + '\\n' +
    'Session: ' + (evt.session_id||'?') + '\\n' +
    JSON.stringify(evt.data, null, 2);
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copy-btn');
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy', 1500); }
  });
}

// ── 3f: Enhanced Views — Relative time toggle ──
let relativeTime = false;

function toggleTimeMode() {
  relativeTime = !relativeTime;
  const btn = document.getElementById('time-btn');
  if (btn) btn.textContent = relativeTime ? 'Abs' : 'Rel';
  renderTimeline();
}

function formatRelTime(ts) {
  if (!ts) return '--:--';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

connect();
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────

async def main():
    global _watcher
    _replay_jsonl()

    # Start transcript watcher and date-rollover checker
    _watcher = TranscriptWatcher(_buffer, _fan_out)
    asyncio.ensure_future(_watcher.poll_loop())
    asyncio.ensure_future(_date_check_loop())
    logger.info("Transcript watcher started (500ms poll)")

    server = await asyncio.start_server(handle_client, "127.0.0.1", PORT)
    logger.info("Trace dashboard at http://localhost:%d/dashboard", PORT)

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            sig = getattr(signal, sig_name)
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(server)))
        except (AttributeError, NotImplementedError):
            pass  # Windows doesn't support add_signal_handler

    async with server:
        await server.serve_forever()


async def _shutdown(server):
    logger.info("Shutting down...")
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")

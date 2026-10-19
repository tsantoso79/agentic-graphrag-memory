"""Lightweight trace event emitter for Claude Code hooks.

Called from memory_helper.py hook functions. Each hook invocation is a fresh
process, so this module has no persistent state. Events are relayed via:
  1. JSONL file append (always, <1ms)
  2. HTTP POST to trace_server.py (fire-and-forget, 50ms timeout)
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

LOG_DIR = Path.home() / ".claude" / "memory" / "logs"
TRACER_DIR = Path.home() / ".claude" / "memory" / "tracer"
SERVER_PORT = 7799
INGEST_URL = f"http://localhost:{SERVER_PORT}/api/ingest"
AGENT_ID = os.environ.get("AGENT_ID", "claude-win")


def emit_trace(event_type, data, session_id=None):
    """Emit a trace event. Must never raise — tracing must not break hooks."""
    try:
        event = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "type": event_type,
            "session_id": session_id or "",
            "project": os.environ.get("CLAUDE_PROJECT", os.getcwd()),
            "agent_id": AGENT_ID,
            "data": data,
        }

        # 1. JSONL file append (primary — always works, <1ms)
        _append_jsonl(event)

        # 2. HTTP POST to dashboard server (best-effort, fire-and-forget thread)
        t = threading.Thread(target=_post_to_server, args=(event,), daemon=True)
        t.start()

    except Exception:
        pass  # Never break hooks


def _append_jsonl(event):
    """Append event as single JSON line to daily JSONL file."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        jsonl_path = LOG_DIR / f"trace_events_{date_str}.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _post_to_server(event):
    """Fire-and-forget HTTP POST to the trace server."""
    try:
        payload = json.dumps(event, default=str).encode("utf-8")
        req = Request(
            INGEST_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(req, timeout=0.05)  # 50ms timeout
    except (URLError, OSError, Exception):
        pass  # Server not running or slow — that's fine


def _is_port_in_use(port):
    """Check if a TCP port is in use on localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            result = s.connect_ex(("127.0.0.1", port))
            return result == 0
    except Exception:
        return False


def ensure_server_running():
    """Start trace_server.py as a detached background process if not running."""
    try:
        if _is_port_in_use(SERVER_PORT):
            return  # Already running

        python_exe = sys.executable or os.environ.get(
            "PYTHON_EXE", sys.executable
        )
        server_script = str(TRACER_DIR / "trace_server.py")

        # Windows: detached process with no console window
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        creation_flags = CREATE_NO_WINDOW | DETACHED_PROCESS

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Prevent nested session detection

        subprocess.Popen(
            [python_exe, server_script],
            creationflags=creation_flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception:
        pass  # Non-critical — dashboard is optional

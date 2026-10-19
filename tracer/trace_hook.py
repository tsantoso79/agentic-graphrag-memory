"""Trace-only hook handler for Stop, SubagentStart/Stop, PreCompact.

Reads hook stdin JSON, emits trace event, exits. No DB access needed.
Usage: python trace_hook.py <stop|subagent-start|subagent-stop|compact-start>
"""

import json
import os
import sys
from pathlib import Path

# Import emit_trace from sibling module
sys.path.insert(0, str(Path(__file__).parent.parent))
from tracer.trace_relay import emit_trace

EVENT_MAP = {
    "stop": (
        "stop",
        lambda d: {"stop_reason": d.get("stop_reason", "?")},
    ),
    "subagent-start": (
        "subagent_start",
        lambda d: {
            "agent_type": d.get("agent_type", ""),
            "agent_name": d.get("agent_name", ""),
        },
    ),
    "subagent-stop": (
        "subagent_stop",
        lambda d: {
            "agent_type": d.get("agent_type", ""),
            "duration_ms": d.get("duration_ms", 0),
        },
    ),
    "compact-start": (
        "compact_start",
        lambda d: {"reason": d.get("reason", "context_limit")},
    ),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in EVENT_MAP:
        sys.exit(0)

    cmd = sys.argv[1]
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, Exception):
        hook_input = {}

    event_type, data_fn = EVENT_MAP[cmd]
    session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))

    emit_trace(event_type, data_fn(hook_input), session_id=session_id)


if __name__ == "__main__":
    main()

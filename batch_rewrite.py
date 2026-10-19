"""
Batch rewrite pipeline: exports memory batches, sends each to claude -p with
the rewrite prompt, collects JSON decisions into a single output file.

Usage:
    python batch_rewrite.py [--batch-size 50] [--start-offset 0] [--output rewrites.json]

Must be run standalone (not from within Claude Code) since claude -p can't nest.
Uses detached Popen with output files to avoid subprocess blocking.
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_helper import get_conn, safe_query, _build_rewrite_prompt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Force UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def export_batch(conn, batch_size, offset):
    """Fetch a batch of memories and format as text for the rewrite prompt."""
    rows = safe_query(conn, """
        SELECT id, content, category, domain, importance, tags
        FROM memories
        WHERE superseded_by IS NULL
        ORDER BY id ASC
        LIMIT %s OFFSET %s
    """, (batch_size, offset))
    if not rows:
        return None, []

    lines = []
    for r in rows:
        lines.append(f"[ID={r['id']}] [cat={r['category']}|dom={r['domain']}|imp={r['importance']}]")
        lines.append(f"CONTENT: {r['content']}")
        lines.append("")

    return "\n".join(lines), rows


def call_sonnet(prompt_text, batch_num):
    """Call claude -p via stdin pipe, poll for completion, return stdout."""
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("claude CLI not found in PATH")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["MEMORY_HELPER_SUBPROCESS"] = "1"

    # Use file-based empty MCP config (inline JSON '{}' crashes claude -p on Windows)
    empty_mcp = os.path.join(SCRIPT_DIR, "_empty_mcp.json")
    if not os.path.exists(empty_mcp):
        with open(empty_mcp, "w") as _f:
            json.dump({"mcpServers": {}}, _f)

    output_file = os.path.join(SCRIPT_DIR, f"_batch_{batch_num}_output.txt")

    # Write prompt to file to avoid Windows 32KB command-line limit
    prompt_file = os.path.join(SCRIPT_DIR, f"_batch_{batch_num}_prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as pf:
        pf.write(prompt_text)

    out_f = open(output_file, "w", encoding="utf-8")

    # Use shell command to pipe prompt file into claude -p via stdin
    # claude -p - reads prompt from stdin
    shell_cmd = f'type "{prompt_file}" | "{claude_path}" -p - --model sonnet --no-session-persistence --max-budget-usd 0.50 --mcp-config "{empty_mcp}" --strict-mcp-config'

    proc = subprocess.Popen(
        shell_cmd,
        stdout=out_f,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=SCRIPT_DIR,
        shell=True,
    )

    # Poll for completion with timeout
    timeout = 300  # 5 minutes per batch
    t0 = time.time()
    while proc.poll() is None:
        if time.time() - t0 > timeout:
            proc.kill()
            break
        time.sleep(2)

    out_f.close()

    # Cleanup prompt file
    try:
        os.unlink(prompt_file)
    except OSError:
        pass

    if proc.returncode is None or (time.time() - t0 > timeout):
        # Clean up output file on timeout
        try:
            os.unlink(output_file)
        except OSError:
            pass
        raise TimeoutError(f"claude -p timed out after {timeout}s")

    # Read output
    with open(output_file, "r", encoding="utf-8") as f:
        result = f.read()

    # Cleanup
    try:
        os.unlink(output_file)
    except OSError:
        pass

    return result


def extract_json(text):
    """Extract JSON array from claude output (may have markdown fences)."""
    text = text.strip()
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Try extracting from markdown code fence
    match = re.search(r'```(?:json)?\s*\n(\[.*?\])\s*\n```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding the first [ ... ] block
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def main():
    parser = argparse.ArgumentParser(description="Batch rewrite pipeline")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--output", default=os.path.join(SCRIPT_DIR, "rewrites.json"))
    args = parser.parse_args()

    conn = get_conn()

    # Count total
    total_row = safe_query(conn, "SELECT COUNT(*) as n FROM memories WHERE superseded_by IS NULL")
    total = total_row[0]["n"]
    print(f"Total active memories: {total}")
    print(f"Batch size: {args.batch_size}, starting at offset {args.start_offset}")
    est_batches = (total - args.start_offset + args.batch_size - 1) // args.batch_size
    print(f"Estimated batches: {est_batches}")
    print()

    all_decisions = []
    offset = args.start_offset
    batch_num = 0

    while True:
        batch_num += 1
        print(f"--- Batch {batch_num} (offset={offset}) ---")

        memories_text, rows = export_batch(conn, args.batch_size, offset)
        if memories_text is None:
            print("No more memories. Done.")
            break

        print(f"  Exported {len(rows)} memories (IDs {rows[0]['id']}-{rows[-1]['id']})")

        # Build prompt and call Sonnet
        prompt = _build_rewrite_prompt(memories_text)
        print(f"  Calling claude -p (Sonnet)...", flush=True)
        t0 = time.time()
        try:
            raw_output = call_sonnet(prompt, batch_num)
        except (TimeoutError, Exception) as e:
            print(f"  ERROR: {e}")
            offset += args.batch_size
            continue
        elapsed = time.time() - t0
        print(f"  Response received ({elapsed:.1f}s, {len(raw_output)} chars)")

        # Parse JSON
        decisions = extract_json(raw_output)
        if decisions is None:
            print(f"  ERROR: Could not parse JSON from output. Saving raw to batch_{batch_num}_raw.txt")
            raw_path = os.path.join(SCRIPT_DIR, f"batch_{batch_num}_raw.txt")
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw_output)
            offset += args.batch_size
            continue

        # Validate: every ID in the batch should have a decision
        batch_ids = {r["id"] for r in rows}
        decision_ids = {d["id"] for d in decisions}
        missing = batch_ids - decision_ids
        if missing:
            print(f"  WARNING: {len(missing)} memories without decisions: {sorted(missing)[:10]}")

        # Tally
        actions = {}
        for d in decisions:
            actions[d["action"]] = actions.get(d["action"], 0) + 1
        print(f"  Decisions: {actions}")

        all_decisions.extend(decisions)
        offset += args.batch_size

        if len(rows) < args.batch_size:
            print("Last batch (fewer than batch_size). Done.")
            break

    # Save all decisions
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_decisions, f, indent=2)

    # Final summary
    total_actions = {}
    for d in all_decisions:
        total_actions[d["action"]] = total_actions.get(d["action"], 0) + 1
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(all_decisions)} decisions across {batch_num} batches")
    print(f"Summary: {total_actions}")
    print(f"Saved to: {args.output}")
    print(f"\nReview, then apply with:")
    print(f'  python memory_helper.py audit-memories --apply-rewrites "{args.output}" --dry-run')

    conn.close()


if __name__ == "__main__":
    main()

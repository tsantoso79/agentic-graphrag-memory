"""
Seed error_solutions table from existing bug memories.
Phase 3A prerequisite for Phase 4 PreToolUse hook.

Approach:
1. Fetch all active bug memories
2. Classify tool_name (matching PreToolUse hook tool names)
3. Extract error_pattern + solution from content
4. Insert into error_solutions with verified=false
5. Skip memories that are project-specific with no tool-actionable guidance

Run: python seed_error_solutions.py [--dry-run]
"""

import os
import re
import sys
import psycopg2

# Fix Windows console encoding
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URI = os.environ["AGENT_MEMORY_DB_URI"]

# Tool classification rules: (tag_or_keyword, tool_name, platform)
# tool_name should match what PreToolUse hook receives
TOOL_RULES = [
    # MCP tools - exact tool names
    (["chrome-mcp", "get_page_text", "claude-in-chrome"], "chrome-mcp", None),
    (["playwright", "browser_snapshot", "browser_click"], "playwright-mcp", None),
    (["slack", "openclaw"], "slack-mcp", None),
    # Core Claude Code tools
    (["edit-tool", "edit tool", "atomic-write", "Edit tool"], "Edit", None),
    (["read-tool", "read tool"], "Read", None),
    (["write-tool", "write tool"], "Write", None),
    (["webfetch", "web-fetch", "fetch_stealth"], "WebFetch", None),
    (["background-agent", "background_agent", "subagent", "agent-tool", "agent tool"], "Agent", None),
    # Bash sub-categories (tool_name = "Bash", use tags for sub-type)
    (["psql", "postgresql", "pgvector", "migration", "pg_trgm", "ivfflat"], "Bash", "psql"),
    (["docker", "container", "dockerfile", "daemon"], "Bash", "docker"),
    (["claude-cli", "claude -p", "max-budget", "arg-limit", "claude-json"], "Bash", "claude-cli"),
    (["schtasks", "taskkill", "installer"], "Bash", "windows"),
    (["git_bash", "git bash"], "Bash", "git"),
    (["npm", "npx", "node"], "Bash", "npm"),
    (["mt4", "metatrader", "terminal.exe", "wine"], "Bash", "mt4"),
    (["pip", "venv", "anaconda"], "Bash", "python"),
    # System-level
    (["hooks", "PreToolUse", "PostToolUse", "SessionEnd", "SessionStart", "hook"], "hooks", None),
    (["settings.json", "settings.local", "permissions"], "settings", None),
    (["memory-system", "memory-loop", "memory_helper", "sonnet-processor",
      "turn-recall", "MEMORY.md", "memory.md"], "memory-system", None),
    (["encoding", "cp1252", "charmap", "utf-8", "utf8", "ansi"], "Bash", "encoding"),
]


def classify_tool(content: str, tags: list, error_sig: str):
    """Return (tool_name, platform) based on content/tags/signature."""
    text = f"{content} {' '.join(tags or [])} {error_sig or ''}".lower()
    for keywords, tool_name, platform in TOOL_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return tool_name, platform
    return None, None


def extract_error_pattern(content: str, error_sig: str) -> str:
    """Extract a concise error pattern (what goes wrong)."""
    if error_sig:
        return error_sig.strip()

    # Look for explicit error messages in quotes
    for pat in [
        r"'([^']{15,120})'",
        r'"([^"]{15,120})"',
        r"(Error:?\s+[^\n.]{10,100})",
        r"([A-Z][a-z]+Error[:\s][^\n.]{10,100})",
    ]:
        m = re.search(pat, content)
        if m:
            return m.group(1).strip()

    # First clause up to first period or dash
    first = content.split(". ")[0].split(" — ")[0].split(" – ")[0]
    return first[:120].strip()


def extract_solution(content: str) -> str:
    """Extract the solution/fix/workaround from content."""
    # Look for explicit markers
    for marker in ["Fix:", "Workaround:", "Solution:", "Resolution:", "Always "]:
        idx = content.find(marker)
        if idx >= 0:
            rest = content[idx:].strip()
            # Take up to 2 sentences
            sentences = re.split(r"(?<=[.!])\s+", rest)
            result = " ".join(sentences[:2])
            return result[:400].strip()

    # Look for "Instead," or "Use X instead" patterns
    m = re.search(r"(Instead[,:]?\s+.+?)(?:\.|$)", content)
    if m:
        return m.group(1).strip()[:400]

    # Look for imperative advice after the problem description
    for pat in [
        r"(?:Must|Should|Need to|Configure|Set|Add|Run|Switch to|Use)\s+(.+?)(?:\.\s|$)",
    ]:
        m = re.search(pat, content)
        if m:
            return m.group(0).strip()[:400]

    # If no explicit solution found, use the full content as it's usually
    # self-contained (memory content is already distilled)
    return None


def is_tool_actionable(content: str, domain: str, tags: list) -> bool:
    """Filter out project-specific bugs that won't help PreToolUse."""
    # EA-specific bugs with no tool guidance
    if domain in ("ea_development", "backtesting") and not any(
        t in (tags or [])
        for t in [
            "plotly",
            "python",
            "pip",
            "encoding",
            "html",
            "dashboard",
            "flask",
            "docker",
        ]
    ):
        return False
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    conn = psycopg2.connect(DB_URI)
    cur = conn.cursor()

    # Get agent_id
    cur.execute("SELECT agent_id FROM agents LIMIT 1")
    agent_id = cur.fetchone()[0]

    # Fetch all active bug memories
    cur.execute("""
        SELECT id, content, error_signature, tags, domain, importance
        FROM memories
        WHERE category = 'bug' AND superseded_by IS NULL
        ORDER BY id
    """)
    rows = cur.fetchall()

    inserted = 0
    skipped_no_tool = 0
    skipped_no_solution = 0
    skipped_not_actionable = 0

    for mid, content, error_sig, tags, domain, importance in rows:
        # Filter non-actionable
        if not is_tool_actionable(content, domain, tags):
            skipped_not_actionable += 1
            continue

        # Classify tool
        tool_name, platform = classify_tool(content, tags, error_sig)
        if not tool_name:
            skipped_no_tool += 1
            continue

        # Extract pattern and solution
        error_pattern = extract_error_pattern(content, error_sig)
        solution = extract_solution(content)

        # If no explicit solution extracted, use full content as solution
        # (bug memories are already distilled knowledge)
        if not solution:
            solution = content

        # Build tags for the error_solution
        es_tags = list(tags or [])
        if platform and platform not in es_tags:
            es_tags.append(platform)

        if dry_run:
            print(f"[{mid:4d}] tool={tool_name:15s} platform={platform or '-':10s}")
            print(f"       pattern: {error_pattern[:80]}")
            print(f"       solution: {solution[:80]}")
            print()
        else:
            cur.execute(
                """
                INSERT INTO error_solutions
                    (error_pattern, error_example, solution, tool_name, platform, tags,
                     verified, source_memory, agent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    error_pattern,
                    content[:500],  # truncated example
                    solution,
                    tool_name,
                    platform or "windows",
                    es_tags,
                    False,
                    mid,
                    agent_id,
                ),
            )

        inserted += 1

    if not dry_run:
        conn.commit()

    print(f"\n{'DRY RUN - ' if dry_run else ''}SEED RESULTS")
    print(f"{'=' * 40}")
    print(f"Total bug memories:      {len(rows)}")
    print(f"Inserted:                {inserted}")
    print(f"Skipped (not actionable):{skipped_not_actionable}")
    print(f"Skipped (no tool match): {skipped_no_tool}")

    # Show distribution
    if not dry_run:
        cur.execute("""
            SELECT tool_name, COUNT(*) FROM error_solutions
            GROUP BY tool_name ORDER BY COUNT(*) DESC
        """)
        print(f"\nTool distribution:")
        for tool, count in cur.fetchall():
            print(f"  {tool:20s}: {count}")

    conn.close()


if __name__ == "__main__":
    main()

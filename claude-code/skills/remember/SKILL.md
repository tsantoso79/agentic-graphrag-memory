---
name: remember
description: >
  Use this skill when the user asks to remember, record, or save what was done in the current session
  to persistent memory. Also use when the user says "store this", "save learnings", "commit to memory",
  or similar phrases about persisting session knowledge.
  Keywords: remember, record, store memory, save learnings, commit to memory, session summary.
allowed-tools: Bash, Read
---

# Remember / Record Skill

Captures the current session's key work into persistent memory (PostgreSQL agent_memory database).

## When to Use

**USE when user says:**
- `/remember` or `/record`
- "remember what we did"
- "store this in memory"
- "save our learnings"
- "record this session"

**DO NOT use for:**
- Storing a single specific fact (just call `store-memory` directly)
- Saving session summary at end (SessionEnd hook handles that)

## Procedure

### Step 1: Review the conversation

Scan the current conversation for:

| Type | Look for | Memory category |
|------|----------|----------------|
| **Decisions made** | Architecture choices, tool selections, approach chosen over alternatives | `decision` |
| **Facts discovered** | New info about environment, tools, systems, behavior | `fact` |
| **Non-obvious bugs** | Unresolved bugs or gotchas that look right but fail | `bug` |
| **Patterns identified** | Reusable techniques, recurring solutions | `pattern` |
| **Config changes** | Settings modified, hooks added, schema changes | `config` |
| **Tool gotchas** | Non-obvious tool failures, workarounds, quirks | `tool_usage` |
| **User preferences** | Workflow preferences, communication style, tool preferences | `preference` |

### Step 2: Filter — apply the 30-day test

For each candidate, ask: **"If I erased this, would a future agent make a worse decision in 30 days?"**

Memory is a decision boundary database, not a diary. Only keep items that narrow the solution space for future work.

**DROP if any of these apply:**
- Resolved bug with no reusable lesson (the fix is already in the code)
- Fact that's easily discoverable by reading the file/config it describes
- Information already in CLAUDE.md, skill files, or code comments
- Session-specific context (what files were open, what order things were done)
- Meta-observations about the memory system itself (unless a genuine gotcha)

**KEEP if:**
- A future agent would waste real time without this knowledge
- The gotcha is non-obvious (can't be found by reading code/config)
- It captures a reusable pattern applicable beyond the original context
- It records a decision with trade-offs that aren't obvious from the code alone

**Compress to distilled principles, not raw details:**
- Bad: "In session X, we found that function Y had bug Z because of line N, fixed by changing A to B"
- Good: "Windows subprocess spawn requires both CREATE_NO_WINDOW and STARTUPINFO(SW_HIDE) — the flag alone is insufficient for Node.js executables"

### Step 3: Draft the memories

For each item that passes the 30-day test, draft a memory with:
- **content**: Self-contained knowledge that a future agent can understand without conversation context. Length should match complexity:
  - **Simple facts/configs**: 1 sentence is fine (e.g., "psql hangs without PGPASSWORD env var set")
  - **Decisions**: 2-3 sentences — state the decision, what was chosen over what, and why
  - **Bugs**: Only store if unresolved or a non-obvious gotcha. 2-3 sentences — the error pattern, root cause, and the reusable lesson (not the raw fix)
  - **Patterns/insights**: 2-4 sentences — the pattern, why it works, and when to apply it
  - **Architecture/design**: 3-5 sentences — the full context including alternatives considered and trade-offs
  - MEMORY.md displays full content in the system prompt (no truncation), so keep entries concise to stay within the 195-line budget. Detailed content is better stored in the DB and accessed via `recall`
- **category**: From table above
- **domain**: `backtesting`, `ea_development`, `optimization`, `tool_errors`, `environment`, `problem_solving`, `debugging`, `general`
- **importance**: 1-3 trivial, 4-6 useful, 7-8 important, 9-10 critical
- **tags**: 2-4 relevant keywords

### Step 4: Show table then store immediately

Present the table of what will be stored, then store all memories in the same turn — do NOT wait for user confirmation. The user wants to see what's being stored but does not want a permission gate.

```
| # | Category | Domain | Imp | Content (truncated) | Tags |
|---|----------|--------|-----|---------------------|------|
| 1 | decision | general | 8 | Use fastembed over PyTorch... | embeddings |
| 2 | bug | environment | 7 | psql hangs without PGPASSWORD... | psql, auth |
```

Then immediately proceed to store each one.

### Step 5: Store memories

```bash
python ~/.claude/memory/memory_helper.py store-memory \
  --content "<content>" \
  --category <category> \
  --domain <domain> \
  --importance <N> \
  --tags <tag1> <tag2>
```

### Step 6: Optionally store a reflection

If significant work was completed, also store a reflection:

```bash
python ~/.claude/memory/memory_helper.py store-reflection \
  --task-type "<type>" \
  --domain <domain> \
  --outcome <success|partial|failed> \
  --what-worked "<description>" \
  --what-failed "<description>" \
  --lessons "<lesson1>" "<lesson2>" \
  --confidence <1-10>
```

### Step 7: Report

```
MEMORIES RECORDED
=================
Stored: N memories, M reflections
Session: <session_id>

Items:
  [decision|imp:8] Use fastembed over PyTorch...
  [bug|imp:7] psql hangs without PGPASSWORD...
```

## Rules

- **Don't store trivial actions** (read a file, ran a command)
- **Don't store session-specific temp context** (use save-context for that)
- **Don't duplicate** — check recall first if unsure whether something is already stored
- **Match content depth to complexity** — simple facts get 1 sentence, decisions and patterns get 2-5 sentences with rationale and context. A future agent reading this memory should understand the what, why, and trade-offs without needing the original conversation.
- **Always show the user** what you plan to store before storing it
- **Include a reflection** if the session involved completing a significant task

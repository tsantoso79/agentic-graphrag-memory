---
name: recall-papers
description: Search the pgvector knowledge base for papers, books, and research documents by semantic query. Optionally query the LightRAG knowledge graph for relationship-based answers.
---

# Recall Papers

Search the knowledge base for relevant paper chunks using semantic similarity, and optionally query the LightRAG knowledge graph for cross-paper relationship discovery.

## Usage

```bash
# Vector search (default)
python ~/.claude/memory/memory_helper.py recall-papers --query "<QUERY>" --limit 5

# Vector search + graph KB
python ~/.claude/memory/memory_helper.py recall-papers --query "<QUERY>" --limit 5 --graph

# Graph KB only (skip vector search)
python ~/.claude/memory/memory_helper.py recall-papers --query "<QUERY>" --graph --graph-only
```

## Parameters

- `--query` — Natural language search query (required)
- `--limit` — Max vector search results (default 5)
- `--paper` — Filter by paper title (partial match)
- `--tags` — Filter by tags (all must match)
- `--json` — Output as JSON
- `--graph` — Also query LightRAG knowledge graph (appended after vector results)
- `--graph-only` — Only query graph, skip vector search
- `--graph-mode` — LightRAG query mode: naive, local, global, hybrid (default), mix

## When to use --graph

Use `--graph` when the question involves:
- **Relationships** between concepts across papers ("how does X relate to Y?")
- **Thematic synthesis** ("what are the main research themes?")
- **Entity-centric** questions ("what methods use reinforcement learning?")

Use plain vector search when looking for specific text passages or quotes.

## Examples

```bash
# Find specific text about pairs trading
python ~/.claude/memory/memory_helper.py recall-papers --query "pairs trading cointegration" --limit 5

# Find relationships across papers about AI agents
python ~/.claude/memory/memory_helper.py recall-papers --query "How do AI agents communicate in multi-agent systems?" --graph --graph-only

# Combined: vector chunks + graph synthesis
python ~/.claude/memory/memory_helper.py recall-papers --query "reinforcement learning for trading" --limit 3 --graph
```

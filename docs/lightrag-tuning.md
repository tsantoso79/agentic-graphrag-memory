LightRAG integrated as graph knowledge engine for paper_chunks KB (261 papers, 23K chunks). Separate from main memory system.

**Why:** Find relationships between concepts across papers. Vector search finds similar text; LightRAG extracts entities + relationships into a queryable knowledge graph.

**How to apply:** Use `lightrag_kb.py` CLI for KB graph queries. Main memory system (model2vec, memories table, turn-recall) unchanged.

## Stack

| Component | Choice |
|-----------|--------|
| KV/Vector/DocStatus | PGKVStorage + PGVectorStorage + PGDocStatusStorage (existing PG 18) |
| Graph | NetworkXStorage (graphml file, no Docker/AGE) |
| LLM | Gemini 2.5 Flash Lite primary, Haiku async fallback |
| Embeddings | EmbeddingGemma-300M ONNX fp32 CPU, 768-dim, ~20ms |

## Entity Extraction Tuning (2026-03-28)

Critic review identified: default few-shot examples contradict custom entity_types, numeric junk entities, llm_model_max_async=4 blows past 15 RPM. All fixed:

| Change | Before | After |
|--------|--------|-------|
| entity_types | 11 generic defaults | 10 domain-specific (Method, Framework, Model, Concept, Person, Organization, Metric, Dataset, Tool, Task) |
| Few-shot examples | Fiction/sports/finance defaults | 3 academic paper examples using our custom types |
| System prompt | No numeric exclusion | Added rule 9: no bare numeric values as entities |
| gleaning | 1 (two LLM passes) | 0 (single pass) |
| chunk_token_size | 1200 | 1600 |
| llm_model_max_async | 4 | 1 |

**Result:** 27% fewer nodes, 35% fewer edges, 64% fewer LLM calls, zero numeric junk.

## Bulk Ingest — IN PROGRESS

- Started: 2026-03-28 20:47
- Process: `nohup python ~/.claude/memory/run_bulk_ingest.py`
- Log: `~/.claude/memory/bulk_ingest.log`
- Monitor: `tail -f ~/.claude/memory/bulk_ingest.log`
- Expect: days to complete (Gemini free tier 1M tokens/day)

## Key Files

- Config/CLI: `~/.claude/memory/lightrag_kb.py`
- Bulk launcher: `~/.claude/memory/run_bulk_ingest.py`

## Pending

- Check bulk ingest progress
- Phase 4: Integrate with recall-papers skill + ingest skill
- Phase 5: Visualization (react-force-graph) — deferred

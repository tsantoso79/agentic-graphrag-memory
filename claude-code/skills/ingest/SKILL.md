---
name: ingest
description: >
  Use this skill when the user asks to ingest, import, or add documents to the knowledge base.
  Handles PDF, EPUB, XPS, MOBI, FB2, CBZ, SVG, TXT files, URLs, and scanned PDFs (OCR).
  Full pipeline: convert to markdown, promote section headers, check for duplicates, embed, store in pgvector.
  Keywords: ingest, import, knowledge base, add paper, add book, convert PDF, OCR, recall-papers.
allowed-tools: Bash, Read, Glob, Grep
---

# Ingest Skill

Ingests documents into the pgvector knowledge base. Full pipeline from source file to searchable chunks.

## When to Use

**USE when user says:**
- `/ingest <file_or_url>` or `/ingest`
- "ingest this PDF", "add this to the knowledge base"
- "import this book/paper/document"
- "convert and store this PDF"

**DO NOT use for:**
- Querying the knowledge base (use `recall-papers` directly)
- Converting without ingesting (use `pdf_to_markdown.py` directly)

## Paths

- **Converter:** `~/.claude/tools/pdf_to_markdown.py`
- **Memory helper:** `~/.claude/memory/memory_helper.py`
- **Python:** `python`
- **Markdown output:** `$KNOWLEDGE_DIR\markdown\`
- **PDF storage:** `$KNOWLEDGE_DIR\pdfs\`

## Procedure

### Step 1: Identify Input

From user's message, extract:
- **File path(s):** Single file, multiple files, or a folder
- **URL:** If starts with http:// or https://
- **OCR needed:** If user mentions "scanned", "scan", or "OCR"
- **Title:** If user provides one; otherwise derive from content (Step 4)

If user says `/ingest` with no arguments, ask what to ingest.

### Step 2: Check for Duplicates

**CRITICAL: Always check before ingesting.**

For each file, check if it's already in the knowledge base:

```bash
python ~/.claude/memory/memory_helper.py \
recall-papers --query "TITLE_OR_FILENAME" --limit 3
```

Also check by source_file in the `papers` metadata table:

```bash
python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['AGENT_MEMORY_DB_URI'])
cur = conn.cursor()
cur.execute('''
    SELECT p.id, p.title, p.tags, p.author, p.doc_type,
           (SELECT count(*) FROM paper_chunks pc WHERE pc.paper_id = p.id) as chunks
    FROM papers p WHERE p.source_file ILIKE %s
''', ('%FILENAME%',))
rows = cur.fetchall()
for r in rows:
    print(f'  Found: {r[1]} ({r[5]} chunks) tags={r[2]} author={r[3]} type={r[4]}')
if not rows:
    print('  Not found - safe to ingest')
conn.close()
"
```

**If already ingested:**
- Report to user: "This file has already been ingested as 'TITLE' (N chunks). Want me to re-ingest with --replace?"
- Only proceed if user confirms
- Use `--replace` flag when re-ingesting

### Step 3: Convert to Markdown

**For files:**
```bash
python ~/.claude/tools/pdf_to_markdown.py \
"<FILE_PATH>" [--ocr]
```

**For URLs (requires trafilatura):**
```bash
python ~/.claude/tools/pdf_to_markdown.py \
"<URL>"
```

**For long filenames (>200 chars):** The converter may fail on Windows. Use a Python one-liner with a shortened output name:
```bash
python -c "
import pymupdf4llm
from pathlib import Path
md = pymupdf4llm.to_markdown('<FULL_PATH>')
Path(r'<OUTPUT_DIR>\<SHORT_NAME>.md').write_text(md, encoding='utf-8')
print(f'Done: {len(md):,} chars')
"
```

### Step 4: Check Markdown Quality

After conversion, check the structure:

```bash
python -c "
import re
text = open(r'<MD_PATH>', encoding='utf-8').read()
headers = re.findall(r'^##+ .+', text, re.MULTILINE)
print(f'Chars: {len(text):,}')
print(f'## headers: {len(headers)}')
print()
# Show first 300 chars for title extraction
print(text[:300])
print()
# Check for promotable sections
sections = re.findall(r'^\d+(?:\.\d+)*\.\s+[A-Z]', text, re.MULTILINE)
print(f'Promotable numbered sections: {len(sections)}')
"
```

**Extract title** from the first ~300 chars of the markdown (paper title, book title, etc.).

### Step 5: Promote Section Headers (if needed)

If the file has few `##` headers but has numbered sections (e.g., `1. Introduction`, `3.2. Results`), promote them:

```bash
python -c "
import re
from pathlib import Path
path = Path(r'<MD_PATH>')
text = path.read_text(encoding='utf-8')

def promote_sections(text):
    lines = text.split('\n')
    result = []
    promoted = 0
    for line in lines:
        stripped = line.strip()
        m = re.match(r'^(\d+(?:\.\d+)*)\.\s+([A-Z_].*)', stripped)
        if m and len(stripped) < 100:
            num = m.group(1)
            title = m.group(2).strip()
            title = re.sub(r'\s+\d+$', '', title)
            if title and not title[0].isdigit():
                result.append(f'## {num}. {title}')
                promoted += 1
                continue
        result.append(line)
    return '\n'.join(result), promoted

text2, count = promote_sections(text)
path.write_text(text2, encoding='utf-8')
headers = len(re.findall(r'^## .+', text2, re.MULTILINE))
print(f'Promoted {count} sections -> {headers} total ## headers')
"
```

**Skip promotion if** the file already has 10+ `##` headers.

### Step 5.5: Determine Tags

Assign 2-5 tags based on the document's content. Use existing vocabulary where possible:

**Domain:** trading, AI, ML, math, finance, philosophy, programming, neuroscience
**Type (--doc-type):** textbook, paper, guide, reference
**Topic:** free-form lowercase (neural_networks, market_microstructure, backtesting, regime_detection, algorithmic_trading, technical_analysis, quant_finance, deep_learning, multi_agent_systems, game_theory, cognitive_science, etc.)

Check existing tags for consistency:
```bash
"/c/Program Files/PostgreSQL/18/bin/psql.exe" "$AGENT_MEMORY_DB_URI" -c "SELECT DISTINCT unnest(tags) as tag FROM papers ORDER BY tag;"
```

### Step 6: Ingest to pgvector

```bash
python ~/.claude/memory/memory_helper.py \
ingest-paper --file "<MD_PATH>" --title "<TITLE>" --tags <tag1> <tag2> [--author "<AUTHOR>"] [--doc-type <TYPE>] [--replace]
```

### Step 7: Verify with Test Query

Run a semantic search to confirm ingestion works:

```bash
python ~/.claude/memory/memory_helper.py \
recall-papers --query "<RELEVANT_TOPIC>" --limit 3
```

Pick a query related to the document's main topic.

### Step 8: Report Results

```
INGEST COMPLETE
===============
Source:   <filename or URL>
Title:   <extracted or user-provided title>
Author:  <author if known>
Type:    <doc_type>
Tags:    [tag1, tag2, tag3]
Format:  <PDF/EPUB/URL/etc>
OCR:     <yes/no>
Chars:   <N>
Headers: <N> (M promoted)
Chunks:  <N>

Test query: "<query>"
Top result: (sim:<N>) <section preview>
```

## Batch Ingestion

When multiple files are provided:
1. Check duplicates for ALL files first
2. Report which are new vs already ingested
3. Convert all in parallel where possible
4. Ingest sequentially (embedding is CPU-bound)
5. Report summary table at end

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Empty markdown output | Try with `--ocr` flag |
| No `##` headers | Run section promotion (Step 5) |
| Filename too long (Windows) | Use shortened output name |
| `trafilatura` not found | `pip install trafilatura` |
| OCR garbled text | Check if Tesseract is installed: `tesseract --version` |
| Duplicate chunks | Use `--replace` flag to re-ingest |

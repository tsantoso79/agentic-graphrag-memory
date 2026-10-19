"""Knowledge Graph dashboard — backend + HTML for trace_server integration.

Provides:
  KG_DASHBOARD_HTML  — inline HTML/CSS/JS string (force-graph 2D)
  handle_kg_request() — async route dispatcher for /api/kg/*
"""

import json
import logging
import os
import sys
import urllib.parse

import networkx as nx

logger = logging.getLogger(__name__)

# LightRAG query access
sys.path.insert(0, os.path.expanduser("~/.claude/memory"))

_GRAPH_PATH = os.path.expanduser(
    "~/.claude/memory/lightrag_workdir/graph_chunk_entity_relation.graphml"
)
_cache = {"mtime": 0, "data": None}


def _safe_float(val, default=1.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _load_graph():
    """Load graphml, cache by mtime. Returns {nodes: [...], edges: [...]}."""
    try:
        mt = os.path.getmtime(_GRAPH_PATH)
    except FileNotFoundError:
        return {"nodes": [], "edges": []}
    if mt == _cache["mtime"] and _cache["data"]:
        return _cache["data"]
    try:
        G = nx.read_graphml(_GRAPH_PATH)
    except Exception as e:
        logger.warning("Failed to parse graphml: %s", e)
        return {"nodes": [], "edges": []}
    nodes = []
    for nid, attrs in G.nodes(data=True):
        nodes.append({
            "id": nid,
            "label": nid,
            "type": attrs.get("entity_type", "Unknown"),
            "description": attrs.get("description", ""),
            "degree": G.degree(nid),
        })
    edges = []
    for src, tgt, attrs in G.edges(data=True):
        edges.append({
            "source": src,
            "target": tgt,
            "description": attrs.get("description", ""),
            "weight": _safe_float(attrs.get("weight", 1.0)),
        })
    result = {"nodes": nodes, "edges": edges}
    _cache["mtime"] = mt
    _cache["data"] = result
    return result


def _get_stats(graph_data):
    """Compute entity type counts, edge count, orphan count."""
    type_counts = {}
    orphans = 0
    for n in graph_data["nodes"]:
        t = n["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
        if n["degree"] == 0:
            orphans += 1
    return {
        "node_count": len(graph_data["nodes"]),
        "edge_count": len(graph_data["edges"]),
        "type_counts": type_counts,
        "orphan_count": orphans,
        "avg_degree": round(
            sum(n["degree"] for n in graph_data["nodes"])
            / max(len(graph_data["nodes"]), 1), 2
        ),
    }


def _search_entities(graph_data, query):
    """Case-insensitive substring search on node labels. Returns top 50."""
    q = query.lower()
    return [n for n in graph_data["nodes"] if q in n["label"].lower()][:50]


async def _query_rag(question, mode="hybrid"):
    """Async LightRAG query — must be awaited from the event loop."""
    from lightrag_kb import query as rag_query
    return await rag_query(question, mode=mode)


def _extract_param(first_line, name):
    """Extract a URL query parameter from an HTTP request line.
    E.g. 'GET /api/kg/search?q=hello HTTP/1.1' -> 'hello' for name='q'
    Returns None if param not found."""
    try:
        path = first_line.split(" ")[1]
        if "?" not in path:
            return None
        qs = path.split("?", 1)[1]
        params = urllib.parse.parse_qs(qs)
        values = params.get(name)
        return values[0] if values else None
    except (IndexError, KeyError):
        return None


async def handle_kg_request(first_line):
    """Handle /api/kg/* requests. Returns (status, content_type, body) or None.
    Async because /api/kg/query awaits LightRAG."""
    if "GET /api/kg/graph" in first_line:
        data = _load_graph()
        return (200, "application/json", json.dumps(data))

    elif "GET /api/kg/stats" in first_line:
        data = _load_graph()
        stats = _get_stats(data)
        return (200, "application/json", json.dumps(stats))

    elif "GET /api/kg/search" in first_line:
        q = _extract_param(first_line, "q")
        if not q:
            return (400, "application/json", '{"error":"missing q param"}')
        data = _load_graph()
        results = _search_entities(data, q)
        return (200, "application/json", json.dumps(results))

    elif "GET /api/kg/query" in first_line:
        q = _extract_param(first_line, "q")
        mode = _extract_param(first_line, "mode") or "hybrid"
        if not q:
            return (400, "application/json", '{"error":"missing q param"}')
        try:
            result = await _query_rag(q, mode)
            return (200, "application/json", json.dumps({"answer": result}))
        except Exception as e:
            return (500, "application/json", json.dumps({"error": str(e)}))

    return None


KG_DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Knowledge Graph &mdash; Claude Trace</title>
  <script src="https://unpkg.com/force-graph"></script>
  <style>
    :root {
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
      --green: #3fb950; --red: #f85149; --yellow: #d29922;
      --purple: #bc8cff; --orange: #d18616; --cyan: #39d353;
      --pink: #f778ba; --teal: #2ea9a1;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Consolas','Monaco','Courier New',monospace;
      font-size: 13px;
      height: 100vh;
      overflow: hidden;
    }
    #app {
      display: grid;
      grid-template-columns: 240px 1fr;
      grid-template-rows: auto 1fr auto;
      height: 100vh;
    }
    #header {
      grid-column: 1 / -1;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 10px 16px;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    #header h1 {
      font-size: 14px;
      font-weight: bold;
      letter-spacing: 0.1em;
      color: var(--accent);
    }
    #stats-summary {
      color: var(--text-dim);
      font-size: 12px;
      flex: 1;
    }
    .header-btn {
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--accent);
      padding: 4px 12px;
      text-decoration: none;
      font-family: inherit;
      font-size: 12px;
      cursor: pointer;
      border-radius: 3px;
    }
    .header-btn:hover { background: var(--border); }
    #sidebar {
      background: var(--surface);
      border-right: 1px solid var(--border);
      padding: 12px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    #sidebar label {
      display: block;
      font-size: 11px;
      color: var(--text-dim);
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    #search-input {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 8px;
      font-family: inherit;
      font-size: 12px;
      border-radius: 3px;
      outline: none;
    }
    #search-input:focus { border-color: var(--accent); }
    #type-filters {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    #type-filters input[type="checkbox"] { cursor: pointer; }
    .section-title {
      font-size: 11px;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      border-bottom: 1px solid var(--border);
      padding-bottom: 4px;
    }
    #stats-panel {
      background: var(--bg);
      border: 1px solid var(--border);
      padding: 8px;
      border-radius: 3px;
      line-height: 1.8;
      font-size: 12px;
      color: var(--text-dim);
    }
    #stats-panel b { color: var(--text); }
    #query-input {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 8px;
      font-family: inherit;
      font-size: 12px;
      border-radius: 3px;
      outline: none;
      resize: vertical;
      min-height: 60px;
    }
    #query-input:focus { border-color: var(--accent); }
    #query-mode {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 5px 8px;
      font-family: inherit;
      font-size: 12px;
      border-radius: 3px;
      outline: none;
      margin-top: 4px;
    }
    #query-btn {
      width: 100%;
      background: var(--accent);
      border: none;
      color: var(--bg);
      padding: 6px 12px;
      font-family: inherit;
      font-size: 12px;
      font-weight: bold;
      cursor: pointer;
      border-radius: 3px;
      margin-top: 6px;
    }
    #query-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    #query-btn:hover:not(:disabled) { background: #79b8ff; }
    #query-output {
      background: var(--bg);
      border: 1px solid var(--border);
      padding: 8px;
      border-radius: 3px;
      font-size: 11px;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 200px;
      overflow-y: auto;
      line-height: 1.5;
    }
    #graph-canvas {
      width: 100%;
      height: 100%;
      background: var(--bg);
    }
    #detail-panel {
      display: none;
      position: absolute;
      bottom: 16px;
      right: 16px;
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 12px 14px;
      max-width: 340px;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.6;
      z-index: 10;
      box-shadow: 0 4px 16px rgba(0,0,0,0.5);
    }
    #detail-close {
      float: right;
      cursor: pointer;
      color: var(--text-dim);
      font-size: 14px;
      line-height: 1;
      margin-left: 8px;
    }
    #detail-close:hover { color: var(--text); }
    #loading-overlay {
      position: absolute;
      inset: 0;
      background: rgba(13,17,23,0.85);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 20;
      flex-direction: column;
      gap: 12px;
    }
    .spinner {
      width: 32px;
      height: 32px;
      border: 3px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .loading-text { color: var(--text-dim); font-size: 13px; }
    #app-footer {
      grid-column: 1 / -1;
      background: var(--surface);
      border-top: 1px solid var(--border);
      padding: 5px 16px;
      display: flex;
      align-items: center;
      gap: 16px;
      font-size: 11px;
      color: var(--text-dim);
    }
  </style>
</head>
<body>
  <div id="app">
    <div id="header">
      <h1>KNOWLEDGE GRAPH</h1>
      <span id="stats-summary"></span>
      <a href="/dashboard" class="header-btn">Back to Trace</a>
    </div>

    <div id="sidebar">
      <div>
        <label>Search Nodes</label>
        <input type="text" id="search-input" placeholder="Filter by label...">
      </div>

      <div>
        <div class="section-title">Entity Types</div>
        <div id="type-filters" style="margin-top:8px"></div>
      </div>

      <div>
        <div class="section-title">Graph Stats</div>
        <div id="stats-panel" style="margin-top:8px">Loading...</div>
      </div>

      <div>
        <div class="section-title">LLM Query</div>
        <div style="margin-top:8px">
          <textarea id="query-input" placeholder="Ask a question about the knowledge graph..."></textarea>
          <select id="query-mode">
            <option value="hybrid">hybrid</option>
            <option value="local">local</option>
            <option value="global">global</option>
            <option value="naive">naive</option>
          </select>
          <button id="query-btn" onclick="askQuery()">Ask</button>
          <div id="query-output" style="margin-top:6px;display:none"></div>
        </div>
      </div>
    </div>

    <div style="position:relative;overflow:hidden">
      <div id="graph-canvas"></div>
      <div id="detail-panel">
        <span id="detail-close" onclick="closeDetail()">&times;</span>
        <div id="detail-content"></div>
      </div>
      <div id="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text">Loading knowledge graph...</div>
      </div>
    </div>

    <div id="app-footer">
      <span>Scroll to zoom &bull; Drag to pan &bull; Click node for details &bull; Double-click to focus</span>
    </div>
  </div>

  <script>
    const TYPE_COLORS = {
      concept: '#d29922', method: '#58a6ff', model: '#bc8cff',
      framework: '#3fb950', person: '#f778ba', organization: '#d18616',
      metric: '#2ea9a1', dataset: '#39d353', tool: '#f85149', task: '#8b949e'
    };

    let graphData = null;
    let activeTypes = new Set(Object.keys(TYPE_COLORS));
    let searchQuery = '';
    let selectedNode = null;

    async function loadGraph() {
      showLoading(true);
      try {
        const res = await fetch('/api/kg/graph');
        graphData = await res.json();
        if (!graphData.nodes.length) { showEmpty(); return; }
        initGraph();
        loadStats();
        buildTypeFilters();
        showLoading(false);
      } catch(e) {
        document.getElementById('loading-overlay').innerHTML =
          '<div style="color:var(--red);text-align:center">' +
          '<div style="font-size:16px;margin-bottom:8px">Failed to load graph</div>' +
          '<div style="color:var(--text-dim)">' + e.message + '</div></div>';
      }
    }

    function initGraph() {
      const container = document.getElementById('graph-canvas');
      const graph = ForceGraph()(container)
        .graphData(filterGraphData())
        .nodeId('id')
        .nodeLabel(n => n.label + ' (' + n.type + ')\\n' + n.description.slice(0, 100))
        .nodeColor(n => {
          if (selectedNode && n.id === selectedNode.id) return '#fff';
          if (searchQuery && !n.label.toLowerCase().includes(searchQuery)) return '#333';
          return TYPE_COLORS[n.type] || '#8b949e';
        })
        .nodeVal(n => Math.sqrt(n.degree) * 2 + 3)
        .linkColor(() => 'rgba(88,166,255,0.15)')
        .linkWidth(e => Math.min(e.weight, 3))
        .onNodeClick((node, event) => {
          if (node._lastClick && Date.now() - node._lastClick < 400) {
            window._graph.centerAt(node.x, node.y, 400);
            window._graph.zoom(4, 400);
            node._lastClick = 0;
          } else {
            node._lastClick = Date.now();
            onNodeClick(node);
          }
        })
        .onNodeHover(onNodeHover)
        .backgroundColor('#0d1117')
        .width(container.offsetWidth)
        .height(container.offsetHeight);
      window._graph = graph;
    }

    function edgeSourceId(e) { return typeof e.source === 'object' ? e.source.id : e.source; }
    function edgeTargetId(e) { return typeof e.target === 'object' ? e.target.id : e.target; }

    function filterGraphData() {
      if (!graphData) return {nodes:[], links:[]};
      const nodes = graphData.nodes.filter(n => activeTypes.has(n.type));
      const nodeIds = new Set(nodes.map(n => n.id));
      const links = graphData.edges
        .filter(e => nodeIds.has(edgeSourceId(e)) && nodeIds.has(edgeTargetId(e)))
        .map(e => ({...e, source: edgeSourceId(e), target: edgeTargetId(e)}));
      return {nodes, links};
    }

    function applyFilters() {
      if (window._graph) { window._graph.graphData(filterGraphData()); }
    }

    function onNodeClick(node) {
      selectedNode = node;
      const connected = graphData.edges
        .filter(e => edgeSourceId(e) === node.id || edgeTargetId(e) === node.id)
        .map(e => edgeSourceId(e) === node.id ? edgeTargetId(e) : edgeSourceId(e));
      const detail = document.getElementById('detail-panel');
      document.getElementById('detail-content').innerHTML =
        '<strong>' + escHtml(node.label) + '</strong> (' + escHtml(node.type) + ') &mdash; ' + node.degree + ' edges<br>' +
        '<span style="color:var(--text-dim)">' + escHtml(node.description) + '</span><br>' +
        'Connected to: ' + connected.slice(0, 20).map(escHtml).join(', ') +
        (connected.length > 20 ? '...' : '');
      detail.style.display = 'block';
      window._graph.nodeColor(window._graph.nodeColor());
    }

    function onNodeHover(node) {
      document.body.style.cursor = node ? 'pointer' : 'default';
    }

    function closeDetail() {
      document.getElementById('detail-panel').style.display = 'none';
      selectedNode = null;
      if (window._graph) window._graph.nodeColor(window._graph.nodeColor());
    }

    function escHtml(str) {
      return String(str)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    document.getElementById('search-input').addEventListener('input', e => {
      searchQuery = e.target.value.toLowerCase();
      if (window._graph) window._graph.nodeColor(window._graph.nodeColor());
    });

    function buildTypeFilters() {
      const container = document.getElementById('type-filters');
      const stats = {};
      graphData.nodes.forEach(n => { stats[n.type] = (stats[n.type]||0) + 1; });
      container.innerHTML = Object.entries(TYPE_COLORS).map(([type, color]) => {
        const count = stats[type] || 0;
        return '<label style="display:flex;align-items:center;gap:6px;cursor:pointer">' +
          '<input type="checkbox" checked data-type="' + type + '" onchange="toggleType(\\'' + type + '\\', this.checked)">' +
          '<span style="width:10px;height:10px;border-radius:50%;background:' + color + ';display:inline-block;flex-shrink:0"></span>' +
          type + ' <span style="color:var(--text-dim)">(' + count + ')</span>' +
          '</label>';
      }).join('');
    }

    function toggleType(type, on) {
      if (on) activeTypes.add(type); else activeTypes.delete(type);
      applyFilters();
    }

    async function loadStats() {
      try {
        const res = await fetch('/api/kg/stats');
        const s = await res.json();
        document.getElementById('stats-panel').innerHTML =
          'Nodes: <b>' + s.node_count.toLocaleString() + '</b><br>' +
          'Edges: <b>' + s.edge_count.toLocaleString() + '</b><br>' +
          'Orphans: <b>' + s.orphan_count + '</b><br>' +
          'Avg Degree: <b>' + s.avg_degree + '</b>';
        document.getElementById('stats-summary').textContent =
          s.node_count.toLocaleString() + ' nodes / ' + s.edge_count.toLocaleString() + ' edges';
      } catch(e) {
        document.getElementById('stats-panel').textContent = 'Stats unavailable';
      }
    }

    async function askQuery() {
      const input = document.getElementById('query-input');
      const q = input.value.trim();
      if (!q) return;
      const mode = document.getElementById('query-mode').value;
      const btn = document.getElementById('query-btn');
      const out = document.getElementById('query-output');
      btn.disabled = true; btn.textContent = 'Querying...';
      out.style.display = 'block';
      out.textContent = 'Waiting for LightRAG response...';
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60000);
        const res = await fetch('/api/kg/query?q=' + encodeURIComponent(q) + '&mode=' + mode, {signal: controller.signal});
        clearTimeout(timeout);
        const data = await res.json();
        out.textContent = data.answer || data.error || 'No response';
      } catch(e) {
        out.textContent = e.name === 'AbortError' ? 'Query timed out (60s)' : e.message;
      }
      btn.disabled = false; btn.textContent = 'Ask';
    }

    function showLoading(on) {
      document.getElementById('loading-overlay').style.display = on ? 'flex' : 'none';
    }

    function showEmpty() {
      document.getElementById('loading-overlay').innerHTML =
        '<div style="color:var(--text-dim);text-align:center">' +
        '<div style="font-size:18px;margin-bottom:8px">No graph data</div>' +
        '<div>Run bulk ingest first:<br><code style="color:var(--accent)">cd ~/.claude/memory && python -u run_bulk_ingest.py</code></div></div>';
    }

    window.addEventListener('load', loadGraph);
    window.addEventListener('resize', () => {
      if (window._graph) {
        const c = document.getElementById('graph-canvas');
        window._graph.width(c.offsetWidth).height(c.offsetHeight);
      }
    });
  </script>
</body>
</html>'''

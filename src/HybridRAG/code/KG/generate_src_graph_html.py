"""
Generate an interactive HTML visualization of the SRC_* source code graph from Neo4j.
Queries the 'mcal' database and produces a self-contained HTML file using vis-network.
"""
import json, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query_knowledge_graph import KnowledgeGraphQuerier


def main():
    q = KnowledgeGraphQuerier(profile="mcal")
    q.connect()

    # ── 1. Fetch all SRC_* nodes ─────────────────────────────────────────
    src_nodes_raw = q.execute_cypher(
        "MATCH (n) WHERE any(lbl IN labels(n) WHERE lbl STARTS WITH 'SRC_') "
        "RETURN id(n) AS nid, labels(n)[0] AS label, "
        "coalesce(n.name, n.file_name, '') AS name, "
        "coalesce(n.description, '') AS description, "
        "coalesce(n.signature, '') AS signature, "
        "coalesce(n.sync_async, '') AS sync_async, "
        "coalesce(n.reentrancy, '') AS reentrancy, "
        "coalesce(n.data_type, n.type_name, '') AS data_type, "
        "coalesce(n.register_access_count, 0) AS reg_count"
    )

    # ── 2. Fetch neighbouring non-SRC nodes (targets of SRC relationships) ──
    neighbour_nodes_raw = q.execute_cypher(
        "MATCH (s)-[r]->(t) "
        "WHERE any(lbl IN labels(s) WHERE lbl STARTS WITH 'SRC_') "
        "AND NOT any(lbl IN labels(t) WHERE lbl STARTS WITH 'SRC_') "
        "RETURN DISTINCT id(t) AS nid, labels(t)[0] AS label, "
        "coalesce(t.name, t.function_name, t.file_name, t.param_name, t.macro_name, '') AS name, "
        "coalesce(t.description, '') AS description"
    )

    # Also get incoming edges to SRC nodes
    neighbour_in_raw = q.execute_cypher(
        "MATCH (s)-[r]->(t) "
        "WHERE any(lbl IN labels(t) WHERE lbl STARTS WITH 'SRC_') "
        "AND NOT any(lbl IN labels(s) WHERE lbl STARTS WITH 'SRC_') "
        "RETURN DISTINCT id(s) AS nid, labels(s)[0] AS label, "
        "coalesce(s.name, s.function_name, s.file_name, '') AS name, "
        "coalesce(s.description, '') AS description"
    )

    # ── 3. Fetch all relationships involving SRC_* nodes ─────────────────
    rels_out = q.execute_cypher(
        "MATCH (s)-[r]->(t) "
        "WHERE any(lbl IN labels(s) WHERE lbl STARTS WITH 'SRC_') "
        "RETURN id(s) AS sid, id(t) AS tid, type(r) AS rel_type"
    )
    rels_in = q.execute_cypher(
        "MATCH (s)-[r]->(t) "
        "WHERE any(lbl IN labels(t) WHERE lbl STARTS WITH 'SRC_') "
        "AND NOT any(lbl IN labels(s) WHERE lbl STARTS WITH 'SRC_') "
        "RETURN id(s) AS sid, id(t) AS tid, type(r) AS rel_type"
    )

    q.close()

    # ── 4. Build node map ────────────────────────────────────────────────
    COLOR_MAP = {
        "SRC_Function":       "#E74C3C",
        "SRC_SourceFile":     "#3498DB",
        "SRC_DataType":       "#F39C12",
        "SRC_Macro":          "#E67E22",
        "SRC_GlobalVariable": "#9B59B6",
        "SRC_LocalVariable":  "#1ABC9C",
        "SWA_Function":       "#27AE60",
        "SWA_DataType":       "#27AE60",
        "SWA_Macro":          "#27AE60",
        "SWUD_Function":      "#8E44AD",
        "SWUD_DerivedConfigParam": "#8E44AD",
        "SWUD_TypeDefinition":"#8E44AD",
        "SWUD_CodeGenMacro":  "#8E44AD",
        "MCALModule":         "#E74C3C",
    }
    DEFAULT_COLOR = "#95A5A6"
    
    SHAPE_MAP = {
        "SRC_Function":       "dot",
        "SRC_SourceFile":     "square",
        "SRC_DataType":       "diamond",
        "SRC_Macro":          "triangle",
        "SRC_GlobalVariable": "star",
        "SRC_LocalVariable":  "triangleDown",
        "MCALModule":         "hexagon",
    }

    SIZE_MAP = {
        "SRC_Function":       22,
        "SRC_SourceFile":     28,
        "SRC_DataType":       18,
        "SRC_Macro":          14,
        "SRC_GlobalVariable": 16,
        "SRC_LocalVariable":  10,
        "MCALModule":         35,
    }

    # De-duplicate nodes
    node_map = {}  # nid -> dict
    for row in src_nodes_raw:
        nid = row["nid"]
        label = row["label"]
        color = COLOR_MAP.get(label, DEFAULT_COLOR)
        display = row["name"] or str(nid)
        # Truncate long names for display
        short = display if len(display) < 30 else display[:27] + "…"
        node_map[nid] = {
            "id": nid,
            "label": short,
            "fullName": display,
            "nodeType": label,
            "title": f"<b>{display}</b><br>{label}",
            "color": {"background": color, "border": color,
                      "highlight": {"background": "#fff", "border": color}},
            "shape": SHAPE_MAP.get(label, "dot"),
            "size": SIZE_MAP.get(label, 16),
            "font": {"size": 11, "color": "#e0e0e0", "face": "Arial"},
            "borderWidth": 2, "shadow": True,
            "props": {k: v for k, v in row.items()
                      if k not in ("nid",) and v},
        }

    for row in list(neighbour_nodes_raw) + list(neighbour_in_raw):
        nid = row["nid"]
        if nid in node_map:
            continue
        label = row["label"]
        color = COLOR_MAP.get(label, DEFAULT_COLOR)
        display = row["name"] or str(nid)
        short = display if len(display) < 30 else display[:27] + "…"
        node_map[nid] = {
            "id": nid,
            "label": short,
            "fullName": display,
            "nodeType": label,
            "title": f"<b>{display}</b><br>{label}",
            "color": {"background": color, "border": color,
                      "highlight": {"background": "#fff", "border": color}},
            "shape": SHAPE_MAP.get(label, "dot"),
            "size": SIZE_MAP.get(label, 20),
            "font": {"size": 11, "color": "#e0e0e0", "face": "Arial"},
            "borderWidth": 2, "shadow": True,
            "props": {k: v for k, v in row.items()
                      if k not in ("nid",) and v},
        }

    # ── 5. Build edge list ───────────────────────────────────────────────
    EDGE_COLOR_MAP = {
        "SRC_CALLS":              "#E74C3C",
        "SRC_DEFINED_IN":         "#3498DB",
        "SRC_BELONGS_TO_MODULE":  "#2ECC71",
        "SRC_HAS_LOCAL_VAR":      "#1ABC9C",
        "SRC_HAS_GLOBAL_VAR":     "#9B59B6",
        "SRC_USES_GLOBAL":        "#F39C12",
        "SRC_INCLUDES":           "#3498DB",
        "SRC_IMPLEMENTS_SWA":     "#27AE60",
        "SRC_IMPLEMENTS_SWUD":    "#8E44AD",
        "SRC_TRACES_TO":          "#FFD700",
    }
    DEFAULT_EDGE_COLOR = "#555"

    edge_list = []
    eid = 0
    for row in list(rels_out) + list(rels_in):
        sid, tid, rel = row["sid"], row["tid"], row["rel_type"]
        if sid not in node_map or tid not in node_map:
            continue
        color = EDGE_COLOR_MAP.get(rel, DEFAULT_EDGE_COLOR)
        edge_list.append({
            "id": eid, "from": sid, "to": tid,
            "label": rel,
            "color": {"color": color, "highlight": "#FFD700", "opacity": 0.7},
            "width": 2 if rel == "SRC_CALLS" else 1.5,
            "arrows": "to",
            "font": {"size": 8, "color": "#888", "strokeWidth": 0,
                     "align": "middle", "background": "rgba(26,26,46,0.8)"},
            "smooth": {"type": "continuous", "roundness": 0.15},
        })
        eid += 1

    # ── 6. Build categories and stats ────────────────────────────────────
    categories = {}
    for n in node_map.values():
        nt = n["nodeType"]
        if nt.startswith("SRC_"):
            cat = "SRC – Source Code"
        elif nt.startswith("SWA_"):
            cat = "SWA – Architecture"
        elif nt.startswith("SWUD_"):
            cat = "SWUD – Detailed Design"
        elif nt == "MCALModule":
            cat = "Module Hub"
        else:
            cat = "Other"
        categories.setdefault(cat, [])
        if nt not in [x["name"] for x in categories[cat]]:
            c = n["color"]["background"]
            categories[cat].append({"name": nt, "colour": c})

    rel_types = {}
    for e in edge_list:
        rt = e["label"]
        if rt not in rel_types:
            rel_types[rt] = {"name": rt, "colour": e["color"]["color"], "count": 0}
        rel_types[rt]["count"] += 1

    nodes_json = json.dumps(list(node_map.values()))
    edges_json = json.dumps(edge_list)
    categories_json = json.dumps(categories)
    rel_list_json = json.dumps(list(rel_types.values()))

    total_nodes = len(node_map)
    total_edges = len(edge_list)
    src_count = sum(1 for n in node_map.values() if n["nodeType"].startswith("SRC_"))

    print(f"Nodes: {total_nodes} ({src_count} SRC_*)  Edges: {total_edges}")

    # ── 7. Generate HTML ─────────────────────────────────────────────────
    html = HTML_TEMPLATE.replace("__NODES_JSON__", nodes_json)
    html = html.replace("__EDGES_JSON__", edges_json)
    html = html.replace("__CATEGORIES_JSON__", categories_json)
    html = html.replace("__REL_LIST_JSON__", rel_list_json)
    html = html.replace("__TOTAL_NODES__", str(total_nodes))
    html = html.replace("__TOTAL_EDGES__", str(total_edges))
    html = html.replace("__SRC_COUNT__", str(src_count))

    out_path = os.path.join(os.path.dirname(__file__), "..", "..", "references",
                            "src_code_graph_explorer.html")
    out_path = os.path.normpath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Written: {out_path}")


# ══════════════════════════════════════════════════════════════════════════
# HTML Template
# ══════════════════════════════════════════════════════════════════════════
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ADC Source Code Graph Explorer</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; overflow:hidden; font-family:'Segoe UI',system-ui,sans-serif; background:#0f0f23; color:#e0e0e0; }
body { display:flex; }

/* Sidebar */
.sidebar { width:340px; min-width:340px; background:#1a1a2e; display:flex; flex-direction:column; border-right:1px solid #2a2a4a; z-index:10; }
.sidebar-header { padding:16px 18px 10px; border-bottom:1px solid #2a2a4a; }
.sidebar-header h2 { font-size:16px; font-weight:700; letter-spacing:.3px; background:linear-gradient(90deg,#E74C3C,#F39C12); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.sidebar-header .stats { font-size:11px; color:#777; margin-top:4px; }

/* Tab bar */
.tab-bar { display:flex; border-bottom:1px solid #2a2a4a; }
.tab-bar button { flex:1; padding:8px 0; background:none; border:none; color:#888; font-size:11px; cursor:pointer; transition:all .2s; border-bottom:2px solid transparent; }
.tab-bar button:hover { color:#bbb; }
.tab-bar button.active { color:#E74C3C; border-bottom-color:#E74C3C; }

/* Panels */
.tab-panel { display:none; flex:1; overflow-y:auto; padding:8px 0; }
.tab-panel.active { display:block; }

/* Search */
.search-box { display:flex; align-items:center; gap:6px; padding:8px 14px; border-bottom:1px solid #2a2a4a; }
.search-box input { flex:1; background:#16213e; border:1px solid #2a2a4a; border-radius:6px; padding:6px 10px; color:#e0e0e0; font-size:12px; outline:none; }
.search-box input:focus { border-color:#E74C3C; }

/* Category groups */
.cat-group { border-bottom:1px solid #1f1f3a; }
.cat-header { display:flex; align-items:center; gap:8px; padding:9px 14px; cursor:pointer; font-size:12px; color:#ccc; user-select:none; transition:background .15s; }
.cat-header:hover { background:rgba(255,255,255,.04); }
.cat-header .dot { display:inline-block; width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.cat-header .cnt { margin-left:auto; font-size:10px; color:#666; }
.cat-header .arrow { font-size:8px; transition:transform .2s; margin-left:4px; }
.cat-header.collapsed .arrow { transform:rotate(-90deg); }
.cat-items.hidden { display:none; }

/* Node items */
.item { display:flex; align-items:center; gap:8px; padding:5px 14px 5px 28px; font-size:12px; cursor:pointer; transition:background .12s; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.item:hover { background:rgba(255,255,255,.05); }
.item.selected { background:rgba(231,76,60,.18); border-left:3px solid #E74C3C; padding-left:25px; }
.item .dot { display:inline-block; width:7px; height:7px; border-radius:50%; flex-shrink:0; }

/* Relationship items */
.rel-item { display:flex; align-items:center; gap:8px; padding:6px 14px; font-size:12px; cursor:pointer; transition:background .12s; }
.rel-item:hover { background:rgba(255,255,255,.05); }
.rel-item .cnt { margin-left:auto; font-size:10px; background:#2a2a4a; padding:1px 6px; border-radius:8px; }
.rel-item .bar { width:8px; height:8px; border-radius:2px; flex-shrink:0; }

/* Filter toggles */
.filter-section { padding:10px 14px; }
.filter-section h4 { font-size:11px; color:#888; margin-bottom:8px; text-transform:uppercase; letter-spacing:.5px; }
.filter-row { display:flex; align-items:center; gap:8px; padding:4px 0; font-size:12px; cursor:pointer; }
.filter-row input[type=checkbox] { accent-color:#E74C3C; }
.filter-row .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex-shrink:0; }

/* Graph area */
#graph-area { flex:1; position:relative; background:#0f0f23; }
#network { width:100%; height:100%; }

/* Detail panel */
#detail-panel { position:absolute; top:0; right:-420px; width:400px; height:100%; background:#16213e; border-left:1px solid #2a2a4a; transition:right .3s ease; overflow-y:auto; z-index:5; padding:20px 18px; }
#detail-panel.visible { right:0; }
#detail-panel .close-btn { position:absolute; top:10px; right:12px; background:none; border:none; color:#888; font-size:22px; cursor:pointer; }
#detail-panel .close-btn:hover { color:#fff; }
#detail-panel h3 { font-size:15px; color:#fff; margin-bottom:2px; padding-right:30px; word-break:break-word; }
#detail-panel .subtitle { font-size:11px; color:#888; margin-bottom:14px; }
#detail-panel .section-title { font-size:12px; font-weight:600; color:#aaa; margin:14px 0 6px; border-bottom:1px solid #2a2a4a; padding-bottom:4px; }
#detail-panel .prop-row { display:flex; padding:3px 0; font-size:12px; gap:8px; }
#detail-panel .prop-key { color:#888; min-width:100px; flex-shrink:0; }
#detail-panel .prop-val { color:#e0e0e0; word-break:break-all; }
#detail-panel .rel-entry { display:flex; align-items:center; gap:5px; padding:3px 0; font-size:12px; flex-wrap:wrap; }
#detail-panel .rel-entry .arrow-icon { font-size:14px; flex-shrink:0; }
#detail-panel .rel-entry .rel-name { color:#FFD700; font-weight:500; font-size:11px; }
#detail-panel .rel-entry .peer-node { color:#4A90D9; cursor:pointer; }
#detail-panel .rel-entry .peer-node:hover { text-decoration:underline; }

/* Toolbar */
.toolbar { position:absolute; top:12px; left:12px; display:flex; gap:8px; z-index:6; }
.toolbar button { padding:6px 14px; background:#1a1a2e; color:#ccc; border:1px solid #2a2a4a; border-radius:6px; cursor:pointer; font-size:11px; transition:all .15s; }
.toolbar button:hover { background:#2a2a4a; color:#fff; }
.toolbar button.active { background:#E74C3C; color:#fff; border-color:#E74C3C; }

/* Legend */
.legend { position:absolute; bottom:12px; left:12px; background:rgba(26,26,46,.92); border:1px solid #2a2a4a; border-radius:8px; padding:10px 14px; z-index:6; font-size:11px; }
.legend h4 { font-size:10px; color:#888; text-transform:uppercase; margin-bottom:6px; letter-spacing:.5px; }
.legend-row { display:flex; align-items:center; gap:6px; padding:2px 0; }
.legend-row .dot { display:inline-block; width:8px; height:8px; border-radius:50%; flex-shrink:0; }
.legend-row .shape { display:inline-block; width:8px; height:8px; flex-shrink:0; }

/* Reset button */
#reset-btn { position:absolute; bottom:20px; right:20px; display:none; padding:8px 20px; background:#E74C3C; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600; z-index:6; }
#reset-btn.visible { display:block; }

/* Scrollbar */
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-track { background:#1a1a2e; }
::-webkit-scrollbar-thumb { background:#2a2a4a; border-radius:3px; }
</style>
</head>
<body>

<!-- ═══ SIDEBAR ═══ -->
<div class="sidebar">
  <div class="sidebar-header">
    <h2>&#x1F4BB; ADC Source Code Graph</h2>
    <div class="stats">__SRC_COUNT__ SRC nodes &middot; __TOTAL_NODES__ total nodes &middot; __TOTAL_EDGES__ edges</div>
  </div>

  <div class="tab-bar">
    <button class="active" data-tab="tab-nodes">Nodes</button>
    <button data-tab="tab-rels">Relations</button>
    <button data-tab="tab-filters">Filters</button>
  </div>

  <div id="tab-nodes" class="tab-panel active">
    <div class="search-box"><span>&#128269;</span><input id="node-search" type="text" placeholder="Search nodes…"></div>
    <div id="node-list"></div>
  </div>

  <div id="tab-rels" class="tab-panel">
    <div class="search-box"><span>&#128269;</span><input id="rel-search" type="text" placeholder="Filter relationships…"></div>
    <div id="rel-list"></div>
  </div>

  <div id="tab-filters" class="tab-panel">
    <div class="filter-section">
      <h4>Node Types</h4>
      <div id="node-type-filters"></div>
    </div>
    <div class="filter-section">
      <h4>Relationship Types</h4>
      <div id="rel-type-filters"></div>
    </div>
  </div>
</div>

<!-- ═══ GRAPH AREA ═══ -->
<div id="graph-area">
  <div id="network"></div>

  <div class="toolbar">
    <button id="btn-physics" class="active" onclick="togglePhysics()">Physics ON</button>
    <button onclick="network.fit()">Fit View</button>
    <button id="btn-labels" class="active" onclick="toggleEdgeLabels()">Edge Labels</button>
    <button onclick="highlightCallGraph()">Call Graph Only</button>
    <button onclick="highlightTraceability()">Traceability</button>
  </div>

  <div class="legend">
    <h4>Node Types</h4>
    <div class="legend-row"><span class="dot" style="background:#E74C3C"></span> SRC_Function</div>
    <div class="legend-row"><span class="dot" style="background:#3498DB"></span> SRC_SourceFile</div>
    <div class="legend-row"><span class="dot" style="background:#F39C12"></span> SRC_DataType</div>
    <div class="legend-row"><span class="dot" style="background:#E67E22"></span> SRC_Macro</div>
    <div class="legend-row"><span class="dot" style="background:#9B59B6"></span> SRC_GlobalVariable</div>
    <div class="legend-row"><span class="dot" style="background:#1ABC9C"></span> SRC_LocalVariable</div>
    <div class="legend-row"><span class="dot" style="background:#27AE60"></span> SWA_*</div>
    <div class="legend-row"><span class="dot" style="background:#8E44AD"></span> SWUD_*</div>
    <h4 style="margin-top:8px">Key Edges</h4>
    <div class="legend-row"><span class="dot" style="background:#E74C3C"></span> SRC_CALLS</div>
    <div class="legend-row"><span class="dot" style="background:#FFD700"></span> SRC_TRACES_TO</div>
    <div class="legend-row"><span class="dot" style="background:#F39C12"></span> SRC_USES_GLOBAL</div>
    <div class="legend-row"><span class="dot" style="background:#27AE60"></span> SRC_IMPLEMENTS_*</div>
  </div>

  <div id="detail-panel">
    <button class="close-btn" onclick="closeDetail()">&times;</button>
    <h3 id="detail-title"></h3>
    <div id="detail-subtitle" class="subtitle"></div>
    <div id="detail-body"></div>
  </div>

  <button id="reset-btn" onclick="resetView()">Reset View</button>
</div>

<script>
// ══════════════════ DATA ══════════════════
const RAW_NODES = __NODES_JSON__;
const RAW_EDGES = __EDGES_JSON__;
const CATEGORIES = __CATEGORIES_JSON__;
const REL_TYPES = __REL_LIST_JSON__;

// ══════════════════ BUILD NODE/EDGE INDEX ══════════════════
const nodeIndex = {};
RAW_NODES.forEach(n => { nodeIndex[n.id] = n; });

const nodes = new vis.DataSet(RAW_NODES);
const edges = new vis.DataSet(RAW_EDGES);

// Store original colours
const origNodeColors = {};
RAW_NODES.forEach(n => { origNodeColors[n.id] = JSON.parse(JSON.stringify(n.color)); });
const origEdgeColors = {};
RAW_EDGES.forEach(e => { origEdgeColors[e.id] = JSON.parse(JSON.stringify(e.color)); });

// ══════════════════ VIS.JS NETWORK ══════════════════
const container = document.getElementById('network');
const network = new vis.Network(container, { nodes, edges }, {
  physics: {
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {
      gravitationalConstant: -80,
      centralGravity: 0.005,
      springLength: 140,
      springConstant: 0.04,
      damping: 0.6,
      avoidOverlap: 0.4
    },
    stabilization: { enabled: true, iterations: 600, updateInterval: 25 },
    maxVelocity: 35,
    minVelocity: 0.75
  },
  edges: {
    smooth: { type: 'continuous', roundness: 0.15 },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    font: { size: 8, color: '#888', strokeWidth: 0, align: 'middle',
            background: 'rgba(26,26,46,0.8)' }
  },
  nodes: {
    borderWidth: 2, borderWidthSelected: 4,
    font: { size: 11, color: '#e0e0e0', face: 'Arial' },
  },
  interaction: {
    hover: true, tooltipDelay: 200,
    navigationButtons: false,
    keyboard: { enabled: true }
  }
});

// ══════════════════ SIDEBAR: NODES TAB ══════════════════
function buildNodeList() {
  const container = document.getElementById('node-list');
  container.innerHTML = '';

  // Group by nodeType
  const groups = {};
  RAW_NODES.forEach(n => {
    const t = n.nodeType;
    if (!groups[t]) groups[t] = [];
    groups[t].push(n);
  });

  // Sort groups: SRC_* first, then others
  const sortedTypes = Object.keys(groups).sort((a, b) => {
    const aS = a.startsWith('SRC_') ? 0 : 1;
    const bS = b.startsWith('SRC_') ? 0 : 1;
    if (aS !== bS) return aS - bS;
    return a.localeCompare(b);
  });

  sortedTypes.forEach(type => {
    const items = groups[type].sort((a, b) => (a.fullName || '').localeCompare(b.fullName || ''));
    const color = items[0].color.background;

    const grp = document.createElement('div');
    grp.className = 'cat-group';

    const hdr = document.createElement('div');
    hdr.className = 'cat-header';
    hdr.innerHTML = `<span class="dot" style="background:${color}"></span>
      <span>${type}</span>
      <span class="cnt">${items.length}</span>
      <span class="arrow">&#9660;</span>`;

    const body = document.createElement('div');
    body.className = 'cat-items';

    items.forEach(n => {
      const item = document.createElement('div');
      item.className = 'item';
      item.dataset.nid = n.id;
      item.innerHTML = `<span class="dot" style="background:${color}"></span>${n.fullName || n.label}`;
      item.addEventListener('click', () => focusNode(n.id));
      body.appendChild(item);
    });

    hdr.addEventListener('click', () => {
      hdr.classList.toggle('collapsed');
      body.classList.toggle('hidden');
    });

    grp.appendChild(hdr);
    grp.appendChild(body);
    container.appendChild(grp);
  });
}

// ══════════════════ SIDEBAR: RELS TAB ══════════════════
function buildRelList() {
  const container = document.getElementById('rel-list');
  container.innerHTML = '';
  REL_TYPES.sort((a, b) => b.count - a.count).forEach(rt => {
    const item = document.createElement('div');
    item.className = 'rel-item';
    item.innerHTML = `<span class="bar" style="background:${rt.colour}"></span>
      <span>${rt.name}</span>
      <span class="cnt">${rt.count}</span>`;
    item.addEventListener('click', () => highlightRelType(rt.name));
    container.appendChild(item);
  });
}

// ══════════════════ SIDEBAR: FILTERS TAB ══════════════════
function buildFilters() {
  // Node type filters
  const ntf = document.getElementById('node-type-filters');
  ntf.innerHTML = '';
  const types = [...new Set(RAW_NODES.map(n => n.nodeType))].sort();
  types.forEach(t => {
    const color = (RAW_NODES.find(n => n.nodeType === t) || {}).color || {};
    const row = document.createElement('label');
    row.className = 'filter-row';
    row.innerHTML = `<input type="checkbox" checked data-ntype="${t}">
      <span class="dot" style="background:${color.background || '#888'}"></span>${t}`;
    row.querySelector('input').addEventListener('change', applyFilters);
    ntf.appendChild(row);
  });

  // Rel type filters
  const rtf = document.getElementById('rel-type-filters');
  rtf.innerHTML = '';
  REL_TYPES.forEach(rt => {
    const row = document.createElement('label');
    row.className = 'filter-row';
    row.innerHTML = `<input type="checkbox" checked data-rtype="${rt.name}">
      <span class="dot" style="background:${rt.colour}"></span>${rt.name} (${rt.count})`;
    row.querySelector('input').addEventListener('change', applyFilters);
    rtf.appendChild(row);
  });
}

function applyFilters() {
  // Get active node types
  const activeTypes = new Set();
  document.querySelectorAll('#node-type-filters input:checked').forEach(cb => {
    activeTypes.add(cb.dataset.ntype);
  });
  // Get active rel types
  const activeRels = new Set();
  document.querySelectorAll('#rel-type-filters input:checked').forEach(cb => {
    activeRels.add(cb.dataset.rtype);
  });

  // Update node visibility
  const nodeUpdates = RAW_NODES.map(n => ({
    id: n.id,
    hidden: !activeTypes.has(n.nodeType)
  }));
  nodes.update(nodeUpdates);

  // Update edge visibility
  const edgeUpdates = RAW_EDGES.map(e => ({
    id: e.id,
    hidden: !activeRels.has(e.label)
  }));
  edges.update(edgeUpdates);
}

// ══════════════════ NODE FOCUS ══════════════════
function focusNode(nid) {
  network.focus(nid, { scale: 1.5, animation: { duration: 500, easingFunction: 'easeInOutQuad' }});
  network.selectNodes([nid]);
  showDetail(nid);
}

// ══════════════════ DETAIL PANEL ══════════════════
function showDetail(nid) {
  const n = nodeIndex[nid];
  if (!n) return;

  document.getElementById('detail-title').textContent = n.fullName || n.label;
  document.getElementById('detail-subtitle').textContent = n.nodeType;

  let html = '';

  // Properties
  if (n.props && Object.keys(n.props).length > 0) {
    html += '<div class="section-title">Properties</div>';
    for (const [k, v] of Object.entries(n.props)) {
      if (k === 'label' || k === 'name' || k === 'nid') continue;
      const val = typeof v === 'string' && v.length > 120 ? v.substring(0, 120) + '…' : v;
      html += `<div class="prop-row"><span class="prop-key">${k}</span><span class="prop-val">${val}</span></div>`;
    }
  }

  // Outgoing relationships
  const outEdges = RAW_EDGES.filter(e => e.from === nid);
  if (outEdges.length > 0) {
    html += `<div class="section-title">Outgoing (${outEdges.length})</div>`;
    outEdges.forEach(e => {
      const peer = nodeIndex[e.to];
      const peerName = peer ? (peer.fullName || peer.label) : e.to;
      html += `<div class="rel-entry">
        <span class="arrow-icon">&#x2192;</span>
        <span class="rel-name">${e.label}</span>
        <span class="peer-node" onclick="focusNode(${e.to})">${peerName}</span>
      </div>`;
    });
  }

  // Incoming relationships
  const inEdges = RAW_EDGES.filter(e => e.to === nid);
  if (inEdges.length > 0) {
    html += `<div class="section-title">Incoming (${inEdges.length})</div>`;
    inEdges.forEach(e => {
      const peer = nodeIndex[e.from];
      const peerName = peer ? (peer.fullName || peer.label) : e.from;
      html += `<div class="rel-entry">
        <span class="arrow-icon">&#x2190;</span>
        <span class="rel-name">${e.label}</span>
        <span class="peer-node" onclick="focusNode(${e.from})">${peerName}</span>
      </div>`;
    });
  }

  document.getElementById('detail-body').innerHTML = html;
  document.getElementById('detail-panel').classList.add('visible');
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('visible');
}

// ══════════════════ HIGHLIGHTING ══════════════════
let highlighted = false;

function dimAll() {
  nodes.update(RAW_NODES.map(n => ({
    id: n.id,
    color: { background: '#2a2a3a', border: '#2a2a3a',
             highlight: { background: '#2a2a3a', border: '#2a2a3a' }},
    font: { color: '#444' }
  })));
  edges.update(RAW_EDGES.map(e => ({
    id: e.id,
    color: { color: '#1a1a2a', highlight: '#1a1a2a', opacity: 0.2 },
    font: { color: 'transparent' }
  })));
}

function restoreAll() {
  nodes.update(RAW_NODES.map(n => ({
    id: n.id,
    color: origNodeColors[n.id],
    font: { size: 11, color: '#e0e0e0', face: 'Arial' },
    hidden: false
  })));
  edges.update(RAW_EDGES.map(e => ({
    id: e.id,
    color: origEdgeColors[e.id],
    font: { size: 8, color: '#888', strokeWidth: 0, align: 'middle',
            background: 'rgba(26,26,46,0.8)' },
    hidden: false
  })));
  highlighted = false;
  document.getElementById('reset-btn').classList.remove('visible');
}

function highlightRelType(relType) {
  dimAll();
  const relevantEdges = RAW_EDGES.filter(e => e.label === relType);
  const relevantNodes = new Set();
  relevantEdges.forEach(e => { relevantNodes.add(e.from); relevantNodes.add(e.to); });

  nodes.update([...relevantNodes].map(nid => ({
    id: nid,
    color: origNodeColors[nid],
    font: { size: 11, color: '#e0e0e0', face: 'Arial' }
  })));
  edges.update(relevantEdges.map(e => ({
    id: e.id,
    color: origEdgeColors[e.id],
    font: { size: 9, color: '#ddd' },
    width: 3
  })));

  highlighted = true;
  document.getElementById('reset-btn').classList.add('visible');
}

function highlightCallGraph() {
  if (highlighted) { restoreAll(); return; }
  dimAll();
  const callEdges = RAW_EDGES.filter(e => e.label === 'SRC_CALLS');
  const callNodes = new Set();
  callEdges.forEach(e => { callNodes.add(e.from); callNodes.add(e.to); });

  nodes.update([...callNodes].map(nid => ({
    id: nid,
    color: origNodeColors[nid],
    font: { size: 12, color: '#fff', face: 'Arial' }
  })));
  edges.update(callEdges.map(e => ({
    id: e.id,
    color: { color: '#E74C3C', highlight: '#FFD700', opacity: 1 },
    font: { size: 0 },
    width: 2.5
  })));

  highlighted = true;
  document.getElementById('reset-btn').classList.add('visible');
}

function highlightTraceability() {
  if (highlighted) { restoreAll(); return; }
  dimAll();
  const traceRels = ['SRC_TRACES_TO', 'SRC_IMPLEMENTS_SWA', 'SRC_IMPLEMENTS_SWUD'];
  const traceEdges = RAW_EDGES.filter(e => traceRels.includes(e.label));
  const traceNodes = new Set();
  traceEdges.forEach(e => { traceNodes.add(e.from); traceNodes.add(e.to); });

  nodes.update([...traceNodes].map(nid => ({
    id: nid,
    color: origNodeColors[nid],
    font: { size: 12, color: '#fff', face: 'Arial' }
  })));
  edges.update(traceEdges.map(e => ({
    id: e.id,
    color: origEdgeColors[e.id],
    font: { size: 9, color: '#ddd' },
    width: 3
  })));

  highlighted = true;
  document.getElementById('reset-btn').classList.add('visible');
}

function resetView() {
  restoreAll();
  // Reset filter checkboxes
  document.querySelectorAll('#node-type-filters input, #rel-type-filters input').forEach(cb => {
    cb.checked = true;
  });
}

// ══════════════════ TOOLBAR ══════════════════
let physicsOn = true;
function togglePhysics() {
  physicsOn = !physicsOn;
  network.setOptions({ physics: { enabled: physicsOn }});
  const btn = document.getElementById('btn-physics');
  btn.textContent = physicsOn ? 'Physics ON' : 'Physics OFF';
  btn.classList.toggle('active', physicsOn);
}

let edgeLabelsOn = true;
function toggleEdgeLabels() {
  edgeLabelsOn = !edgeLabelsOn;
  edges.update(RAW_EDGES.map(e => ({
    id: e.id,
    font: { ...e.font, size: edgeLabelsOn ? 8 : 0 }
  })));
  const btn = document.getElementById('btn-labels');
  btn.textContent = edgeLabelsOn ? 'Edge Labels' : 'No Labels';
  btn.classList.toggle('active', edgeLabelsOn);
}

// ══════════════════ SEARCH ══════════════════
document.getElementById('node-search').addEventListener('input', function() {
  const q = this.value.toLowerCase();
  document.querySelectorAll('#node-list .item').forEach(el => {
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
  // Also show/hide cat-groups
  document.querySelectorAll('#node-list .cat-group').forEach(grp => {
    const visible = [...grp.querySelectorAll('.item')].some(i => i.style.display !== 'none');
    grp.style.display = visible ? '' : 'none';
  });
});

document.getElementById('rel-search').addEventListener('input', function() {
  const q = this.value.toLowerCase();
  document.querySelectorAll('#rel-list .rel-item').forEach(el => {
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});

// ══════════════════ TAB SWITCHING ══════════════════
document.querySelectorAll('.tab-bar button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});

// ══════════════════ NETWORK EVENTS ══════════════════
network.on('click', function(params) {
  if (params.nodes.length > 0) {
    showDetail(params.nodes[0]);
  } else {
    closeDetail();
  }
});

network.on('doubleClick', function(params) {
  if (params.nodes.length > 0) {
    const nid = params.nodes[0];
    // Highlight neighbourhood
    dimAll();
    const connEdges = RAW_EDGES.filter(e => e.from === nid || e.to === nid);
    const connNodes = new Set([nid]);
    connEdges.forEach(e => { connNodes.add(e.from); connNodes.add(e.to); });

    nodes.update([...connNodes].map(id => ({
      id,
      color: origNodeColors[id],
      font: { size: 12, color: '#fff', face: 'Arial' }
    })));
    edges.update(connEdges.map(e => ({
      id: e.id,
      color: origEdgeColors[e.id],
      font: { size: 9, color: '#ddd' },
      width: 2.5
    })));

    highlighted = true;
    document.getElementById('reset-btn').classList.add('visible');
  }
});

// ══════════════════ INIT ══════════════════
buildNodeList();
buildRelList();
buildFilters();
</script>
</body>
</html>'''


if __name__ == "__main__":
    main()

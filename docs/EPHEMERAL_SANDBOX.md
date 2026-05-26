# Ephemeral Sandbox — Implementation Guide

> **Branch**: `feature/illd-sandbox-integration` (based on `feature/memory-layer-v2`)
> **Last updated**: 2026-05-12

---

## What Is It?

The **Ephemeral Sandbox** is an in-memory workspace where users can upload files and instantly get a searchable knowledge graph + semantic search — without touching the production database.

Think of it like a **scratchpad**: you upload your code files, and immediately you can ask questions about them, find relationships between functions, and search for information — all in real-time, all in memory, all isolated to your session.

---

## Why Do We Need It?

| Problem | Sandbox Solution |
|---------|-----------------|
| Some modules have no production data yet | Users upload what they have → instant KG |
| Engineers want to test changes before committing | Upload modified files → query immediately |
| Production ingestion takes time (batch jobs) | Sandbox ingestion is instant (in-memory) |
| Users don't want to pollute the production graph | Sandbox is isolated — destroyed after session |

---

## Architecture Overview

```
User uploads file(s) via MCP tool (sandbox_upload)
         │
         ▼
   ┌─────────────────────────────────────────────┐
   │  SandboxParserDispatcher                     │
   │  Routes by extension + workspace_id:         │
   │    .c/.h → C Parser (clang/regex)            │
   │    _swa.h → iLLD SWA Parser                  │
   │    _regdef.h → SFR Parser                    │
   │    .xlsx → XLSX / iLLD Jama Parser            │
   │    .arxml → ARXML Parser                      │
   │    .puml → PlantUML Parser                    │
   │    .rst → RST Parser                          │
   │    .json → JSON / iLLD Requirements Parser    │
   │    .md/.txt/.csv → Text Parser                │
   └─────────────────────────────────────────────┘
         │
         ▼
   ┌─────────────────────────────────────────────┐
   │  SandboxAdapter                              │
   │  Converts parsed data → graph nodes + chunks │
   │  Applies shadow/override for prod conflicts  │
   │  iLLD-specific: semantic per-entity chunks   │
   └─────────────────────────────────────────────┘
         │
         ▼
   ┌──────────────────────────────────────────────────┐
   │  EphemeralSandbox (per session)                   │
   │                                                    │
   │  ┌──────────────────┐  ┌───────────────────────┐ │
   │  │ EphemeralGraph    │  │ EphemeralVectors      │ │
   │  │ (NetworkX DiGraph)│  │ (in-memory, 384-dim)  │ │
   │  │ + keyword index   │  │ + cosine similarity   │ │
   │  └──────────────────┘  └───────────────────────┘ │
   └──────────────────────────────────────────────────┘
         │
         ▼
   ┌──────────────────────────────────────────────────┐
   │  TraceabilityPuller (on upload)                   │
   │  Pulls ±N hop neighbors from prod Neo4j           │
   │  + Boundary resolution (Unknown → typed nodes)    │
   └──────────────────────────────────────────────────┘
         │
         ▼
   User queries via search_database(session_id=...)
         │
         ▼
   ┌──────────────────────────────────────────────────┐
   │  HybridGraphService                               │
   │  Merges: sandbox results (graph + vectors)        │
   │        + production Qdrant results                 │
   │  Shadow filter: excludes stale prod chunks for    │
   │  files that were re-uploaded to the sandbox       │
   └──────────────────────────────────────────────────┘
         │
         ▼
   ┌──────────────────────────────────────────────────┐
   │  HybridTraversal (for multi-hop queries)          │
   │  BFS in NetworkX → boundary leaf detection →      │
   │  prod Neo4j continuation → re-entry blocking      │
   │  + shortest_path spanning shadow + prod           │
   └──────────────────────────────────────────────────┘
         │
         ▼
   Combined results returned to user
```

---

## Key Features

### 1. File Type Support (Module-Agnostic)
| Extension | Parser Used | What Gets Extracted |
|-----------|------------|-------------------|
| `.c` | C Parser (clang or regex fallback) | Functions, internal calls, SFR accesses, global refs |
| `.h` | C Parser | Declarations, prototypes |
| `*_swa.h` | iLLD SWA Parser | Functions, structs, struct members, enums, enum values, typedefs, macros |
| `*_regdef.h` | SFR Parser | Registers, bitfields |
| `.xlsx` | XLSX / iLLD Jama Parser | Requirements (auto-detects Jama export format) |
| `.arxml` | ARXML Parser | AUTOSAR components, modules |
| `.puml` | PlantUML Parser | UML diagrams, relationships |
| `.rst` | RST Parser | Documentation sections |
| `.json` | JSON Parser | Requirements, structured data |
| `.md`, `.txt`, `.csv` | Text Parser | General text content |

### 2. Workspace-Aware Routing
The `SandboxParserDispatcher` adapts behavior based on `workspace_id`:
- **`illd`**: Routes `_swa.h` to SWA parser, `_regdef.h` to SFR parser, detects Jama xlsx exports, creates `Function` nodes with `CALLS_INTERNALLY` edges matching prod KG conventions
- **`mcal`** (default): Uses `SRC_Function` nodes with `SRC_CALLS` edges, supports `SRC_ACCESSES_SFR` and `SRC_USES_GLOBAL` when include paths are provided

### 3. Shadow/Override (Smart Merging)
When a user uploads a file that already exists in production:
- Production nodes for that file get **shadowed** (original properties preserved in `_original_prod_properties`)
- Stale outgoing edges of sandbox-detectable types are cleared:
  - Without include paths: `SRC_CALLS` / `CALLS_INTERNALLY` only
  - With include paths: adds `SRC_ACCESSES_SFR`, `SRC_USES_GLOBAL`
- Non-uploaded files remain accessible from production
- **No production data is ever modified**

### 4. Real Embeddings (384-dimensional)
- Uses `all-MiniLM-L6-v2` sentence transformer model via shared singleton (`src/Configuration/embedding_singleton.py`)
- Same model pre-downloaded in Docker image at build time
- Falls back to deterministic hash-based embedder for dev/test environments
- iLLD workspace: creates **semantic per-entity chunks** (one chunk per function/struct/enum/register) instead of generic text splitting

### 5. Hybrid Search (HybridGraphService)
When querying with a `session_id`, results come from:
- **Sandbox** — graph keyword search + vector cosine similarity
- **Production Qdrant** — additional context not in the sandbox

The shadow filter excludes production Qdrant results from files the user re-uploaded. Sandbox results get a slight priority boost; prod results are discounted by 0.9×.

Tool classification:
| Category | Tools | Routing |
|----------|-------|---------|
| Shallow | `search_database`, `search_nodes`, `get_node_by_id`, `get_neighbors`, `query_api_function`, `get_type_definition`, `query_dependencies`, `get_distribution` | Sandbox NetworkX + vectors |
| Deep | `execute_cypher`, `find_coverage_gaps`, `build_traceability_matrix`, `find_requirement_traces`, `shortest_path`, `analyze_hw_sw_links`, `detect_communities`, `get_ontology_compliance` | Prod Neo4j + patch with sandbox overrides |

### 6. Hybrid Traversal (HybridTraversal)
Multi-hop graph traversal seamlessly spans sandbox and production:
1. **Phase 1**: BFS in NetworkX from start node
2. **Boundary leaf detection**: Nodes with no further qualifying edges
3. **Phase 2**: Fire continuation queries to prod Neo4j from each boundary leaf
4. **Re-entry blocking**: If prod returns a node that has a sandbox override, that path is stopped (sandbox has the truth)
5. **Shortest path**: Works across shadow → prod boundary using stitched segments

### 7. Production Traceability Pull (TraceabilityPuller)
On upload, the system automatically:
1. Extracts node names from parsed data (before ingestion)
2. Pulls ±N hop neighbors from production Neo4j (configurable `trace_depth`, default=1)
3. Loads production nodes/relationships into the sandbox graph (marked `_origin: "production"`)
4. Resolves boundary nodes (Unknown stubs → typed production nodes, cross-module)
5. Safety cap: 500 nodes max per upload call

### 8. Auto-Include Path Discovery
For C files, `sandbox_upload` automatically discovers include paths from:
1. `INCLUDE_HEADERS_DIR` env var (default `/app/include_headers` in Docker)
2. Local `temporary_data/` directory
3. Module-specific headers: CfgMcal, MemMap, SchM, rc1_deps, platform, cross-module SSC

---

## MCP Tools (4 total)

### `sandbox_upload`
Upload files into a session's sandbox.

```json
{
  "session_id": "my-session",
  "documents": [
    {"filename": "IfxAdc.c", "content": "...", "encoding": "utf-8"}
  ],
  "workspace_id": "illd",
  "trace_depth": 1,
  "module": "ADC",
  "include_paths": ["/path/to/headers"]
}
```

Or upload from file paths (reads from filesystem):
```json
{
  "session_id": "my-session",
  "file_paths": ["/path/to/IfxAdc.c", "/path/to/IfxAdc_swa.h"],
  "workspace_id": "illd"
}
```

**Returns**: Ingestion stats (nodes, edges, chunks), boundary resolution stats, parser diagnostics, sandbox status.

### `sandbox_status`
Check what's in the sandbox:
```json
{"session_id": "my-session"}
```
**Returns**: Active state, files ingested, graph stats (node counts by type, edge count), vector stats (chunk count).

### `sandbox_diff`
See what changed vs production:
```json
{"session_id": "my-session"}
```
**Returns**: Nodes added (sandbox-only), nodes modified/shadowed (with original vs current properties), edges added.

### `sandbox_clear`
Destroy the sandbox explicitly (also auto-destroyed on session end):
```json
{"session_id": "my-session"}
```
**Returns**: Cleanup stats (nodes removed, edges removed, chunks removed, files cleared).

### Querying (No New Tool Needed!)
All 25 existing query tools work with sandbox when you pass `session_id`. The `@with_session_routing` decorator injects `HybridGraphService` and `HybridTraversal` into the tool function:

```python
search_database(query="ADC calibration", session_id="my-session")
get_node_by_id(node_id="Function:IfxAdc_Init:ADC", session_id="my-session")
get_neighbors(node_id="Function:IfxAdc_Init:ADC", session_id="my-session")
execute_cypher(query="MATCH (n:Function)...", session_id="my-session")
shortest_path(start="Function:A:ADC", end="Requirement:REQ1:ADC", session_id="my-session")
find_requirement_traces(requirement_id="AURC1-REQA-123", session_id="my-session")
build_traceability_matrix(module="ADC", session_id="my-session")
```

---

## Scenarios

### Scenario 1: Module with no production data
```
User: "I'm working on CPUB module, nothing is in the system yet"
→ sandbox_upload(session_id, documents=[{filename: "cpub.c", content: ...}])
→ search_database("CPUB initialization", session_id)
→ Gets results from uploaded files only (prod is empty, that's fine)
```

### Scenario 2: Modifying existing code (shadow/override)
```
User: "I added a new function to IfxAdc.c"
→ sandbox_upload(session_id, documents=[{filename: "IfxAdc.c", content: modified}])
→ TraceabilityPuller loads ±1 hop from prod → sandbox has full context
→ SandboxAdapter shadows existing prod nodes, clears stale CALLS_INTERNALLY edges
→ search_database("ADC calibration", session_id)
→ Gets: new function from sandbox + other ADC info from production
→ Old IfxAdc.c production Qdrant chunks are shadow-filtered (excluded)
```

### Scenario 3: Quick analysis without polluting production
```
User: "I want to understand this code but don't want to ingest it permanently"
→ sandbox_upload(session_id, documents=[{filename: "experimental_driver.c", content: ...}])
→ get_neighbors(node_id="Function:ExpDriver_Init:UNKNOWN", session_id)
→ Session ends → sandbox auto-destroyed → production untouched
```

### Scenario 4: iLLD SWA + SFR combined upload
```
User: "Analyze the ADC module architecture"
→ sandbox_upload(session_id, documents=[
    {filename: "IfxAdc_swa.h", content: ...},
    {filename: "IfxAdc_regdef.h", content: ...},
    {filename: "IfxAdc.c", content: ...}
  ], workspace_id="illd", module="ADC")
→ Gets: Function, Struct, Enum, Register, BitField nodes + call graph + per-entity embeddings
→ query_dependencies(name="IfxAdc_Init", session_id)
→ Shows: function calls + SFR accesses + struct usage all in one traversal
```

---

## Technical Details

### File Locations
| Component | Path |
|-----------|------|
| Core implementation (2737 lines) | `src/MemoryLayer/memory/ephemeral_sandbox.py` |
| MCP tool definitions (4 tools) | `mcp/core/mcp_server.py` (lines 2655–3240) |
| Session routing decorator | `mcp/core/mcp_server.py` (lines 288–340) |
| Tier registration | `mcp/core/tool_tiers.py` |
| Auth policy | `mcp/auth/policies/resource_mcp_tool.yaml` |
| Unit tests (36) | `tests/unit/memory/test_illd_sandbox_integration.py` |
| E2E tests (10 modules) | `tests/integration/sandbox/test_sandbox_e2e_full.py` |

### Classes & Components
| Class | Lines | Purpose |
|-------|-------|---------|
| `Chunk` | dataclass | Text chunk ready for embedding (text, chunk_id, metadata, source_file) |
| `SearchResult` | dataclass | Unified result from graph or vector search |
| `_FallbackEmbedder` | ~20 | Deterministic hash-based embedder for dev/test (NOT production) |
| `_SentenceTransformerEmbedder` | ~30 | Production embedder using shared singleton, auto-fallback |
| `EphemeralGraph` | ~300 | NetworkX DiGraph with keyword index, shadow support, boundary resolution, bulk ops |
| `EphemeralVectors` | ~80 | In-memory vector store with cosine similarity, metadata filtering |
| `EphemeralSandbox` | dataclass | Container: graph + vectors + files_ingested + active flag |
| `SandboxManager` | ~60 | Thread-safe lifecycle manager, one sandbox per session |
| `SandboxIngester` | ~150 | Legacy file parser (superseded by SandboxParserDispatcher for Plan 2) |
| `SandboxParserDispatcher` | ~200 | Routes files to IngestionPipeline parsers by extension + workspace |
| `SandboxAdapter` | ~500 | Transforms parser output → nodes/edges/chunks, shadow logic, iLLD-specific methods |
| `TraceabilityPuller` | ~150 | Read-only Neo4j queries for ±N hop neighbors + boundary resolution |
| `HybridGraphService` | ~250 | Shallow/deep tool routing, sandbox + prod Qdrant merge, shadow filter |
| `HybridTraversal` | ~350 | BFS in sandbox → boundary detection → prod continuation → re-entry blocking |
| `SandboxQuerier` | ~40 | Combined graph keyword + vector semantic search with score fusion |

### Node Types Created (iLLD workspace)
| Node Type | Source | ID Format |
|-----------|--------|-----------|
| `Function` | .c, _swa.h | `Function:{name}:{MODULE}` |
| `Struct` | _swa.h | `Struct:{name}:{MODULE}` |
| `StructMember` | _swa.h | `StructMember:MEMBER_{struct}_{field}:{MODULE}` |
| `Enum` | _swa.h | `Enum:{name}:{MODULE}` |
| `EnumValue` | _swa.h | `EnumValue:ENUMVAL_{name}:{MODULE}` |
| `Typedef` | _swa.h | `Typedef:{name}:{MODULE}` |
| `Register` | _regdef.h | `Register:{name}:{MODULE}` |
| `BitField` | _regdef.h | `BitField:BITFIELD_{reg}_{bf}:{MODULE}` |
| `Requirement` | .xlsx, .json | `Requirement:{req_id}:{MODULE}` |

### Edge Types
| Relationship | Between | Origin |
|-------------|---------|--------|
| `CALLS_INTERNALLY` | Function → Function | iLLD sandbox |
| `SRC_CALLS` | SRC_Function → SRC_Function | MCAL sandbox |
| `SRC_ACCESSES_SFR` | SRC_Function → SFR_Register | MCAL (with includes) |
| `SRC_USES_GLOBAL` | SRC_Function → SRC_GlobalVariable | MCAL (with includes) |
| `DEPENDS_ON` | Function → Function | SWA dependencies |
| `HAS_MEMBER` | Struct → StructMember | SWA |
| `HAS_VALUE` | Enum → EnumValue | SWA |
| `HAS_BITFIELD` | Register → BitField | SFR |

### Dependencies
- `networkx>=3.3` — In-memory directed graph
- `sentence-transformers>=2.2.0` — Real embeddings (all-MiniLM-L6-v2, 384-dim)
- `numpy>=2.0.0` — Vector operations
- `qdrant-client>=1.9.0` — Production vector store queries
- `openpyxl` — XLSX/Jama parsing (optional, graceful fallback)

### Access Control (Cerbos)
All 4 sandbox tools are **PUBLIC** tier — any authenticated user can use them.
Production-writing tools (`submit_human_feedback`, `process_results`, etc.) remain independently controlled by their respective tier policies.

---

## Testing Summary

| Test Suite | Count | Status |
|-----------|-------|--------|
| Unit tests (mocked, no network) | 36 | ALL PASS ✓ |
| E2E integration (10 modules) | 10 | ALL PASS ✓ |
| Module types tested | single-SFR (5) + multi-SFR/edge-case (5) | ALL PASS ✓ |
| Edge cases covered | cross-named SFR, multi-SFR, base prefix, 1000+ nodes | ALL PASS ✓ |
| Live demo (download → modify → ingest → query) | 5 checks | ALL PASS ✓ |

### Modules Validated (E2E)
| Module | Type | Noteworthy |
|--------|------|-----------|
| CXPI | Single SFR | Standard iLLD module |
| CMEM | Single SFR | Small module, shadow/override tested |
| LCSS | Single SFR | Compact module |
| SCB | Single SFR | Simple structure |
| CANXS | Cross-named SFR | `sfr_filename` parameter (SFR file name ≠ module name) |
| FRAY | Multi-SFR | Multiple register definition files |
| PMS | Multi-SFR | Power management, large register set |
| CLOCKSC | Base prefix | Module prefix differs from source prefix |
| NVMR | Single SFR | Non-volatile memory |
| SCU | Multi-SFR | System control, 1000+ nodes |

### Unit Test Coverage
- Parser routing (all file types)
- SWA ingestion (functions, structs, enums, typedefs, macros)
- SFR ingestion (registers, bitfields)
- C source ingestion (functions, call graph, SFR accesses, global refs)
- Shadow/override logic (prod node replacement, stale edge clearing)
- Hybrid search (sandbox + prod merge, shadow filter)
- Module detection from filename patterns
- Embedder (real + fallback)
- Lifecycle (create, status, clear, destroy)
- Boundary resolution
- Traceability pull

---

## Deployment Notes

1. **No new infrastructure needed** — runs in the same pod, same image
2. **Model pre-downloaded** — `all-MiniLM-L6-v2` is cached in Docker image at build time (Dockerfile `RUN python -c "from sentence_transformers import ..."`)
3. **Memory usage** — each sandbox is ~1-50MB depending on file count; auto-destroyed on session end
4. **No database changes** — sandbox never writes to Neo4j or Qdrant (read-only for TraceabilityPuller and HybridGraphService)
5. **Backward compatible** — all existing tools work exactly as before when no `session_id` is passed
6. **Thread-safe** — `SandboxManager` uses threading.Lock for concurrent session creation
7. **Default workspace** — `workspace_id` defaults to `"mcal"` in the decorator (`mcp_server.py` line 310); `sandbox_upload` defaults to `"illd"` for iLLD-first usage

---

## How to Use (Quick Start)

```python
# 1. Start a session
session_start()  # Returns session_id

# 2. Upload files
sandbox_upload(
    session_id="<session_id>",
    documents=[
        {"filename": "MyModule.c", "content": "<source code>"},
        {"filename": "MyModule_swa.h", "content": "<SWA header>"},
    ],
    workspace_id="illd",
    module="MYMOD",
    trace_depth=1,
)

# 3. Query (any existing tool works)
search_database(query="initialization sequence", session_id="<session_id>")
get_neighbors(node_id="Function:MyModule_Init:MYMOD", session_id="<session_id>")
find_requirement_traces(requirement_id="AURC1-REQA-001", session_id="<session_id>")

# 4. Check status / diff
sandbox_status(session_id="<session_id>")
sandbox_diff(session_id="<session_id>")

# 5. Done — clear or let session end auto-destroy it
sandbox_clear(session_id="<session_id>")
```

---

## Branch & Merge

**Branch**: `feature/illd-sandbox-integration` (based on `feature/memory-layer-v2`)

**Merge checklist**:
1. ✅ All tests pass (36 unit + 10 E2E)
2. ✅ Pre-deployment audit clear (tools, imports, auth, Dockerfile, syntax)
3. ✅ Module-agnostic design (works for any MCAL/iLLD module)
4. Merge → build updated Docker image → deploy

# Memory Layer Architecture

**Component**: `src/MemoryLayer/`
**Primary classes**: `WorkingMemoryManager`, `SandboxManager`, `ContextBuilder`, `NodeSetManager`
**Backing stores**: Redis (sessions), Neo4j (node sets), Qdrant (pattern store), in-memory (sandbox)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Working Memory (Sessions)](#2-working-memory-sessions)
3. [Ephemeral Sandbox](#3-ephemeral-sandbox)
4. [Semantic Memory (Pattern Store)](#4-semantic-memory-pattern-store)
5. [Node Sets](#5-node-sets)
6. [Context Builder](#6-context-builder)
7. [Ontology Loader](#7-ontology-loader)
8. [File Map](#8-file-map)

---

## 1. Overview

The Memory Layer provides stateful capabilities to an otherwise stateless MCP server. It manages four kinds of memory:

| Memory Type | Scope | Lifetime | Store | Purpose |
|-------------|-------|----------|-------|---------|
| **Working Memory** | Per-session | TTL-based (default 1h) | Redis / in-memory | DA session context, intermediate results |
| **Ephemeral Sandbox** | Per-session | Destroyed on session end | In-memory (NetworkX) | Temporary KG from user-uploaded docs |
| **Semantic Memory** | Global (persistent) | Indefinite | Qdrant | Approved patterns from feedback learning |
| **Node Sets** | Global (persistent) | Indefinite | Neo4j | Module isolation anchors |

```
                    Memory Layer
                         │
        ┌────────────────┼────────────────────────┐
        │                │                        │
   Working Memory    Ephemeral             Semantic Memory
   (per-session)     Sandbox               (global, persistent)
        │           (per-session)                 │
        │                │                        │
   ┌────┴────┐     ┌─────┴──────┐           ┌────┴─────┐
   │  Redis  │     │  NetworkX  │           │  Qdrant  │
   │ Backend │     │ in-memory  │           │  Pattern │
   │  (prod) │     │  KG + Vecs │           │   Store  │
   ├─────────┤     └────────────┘           └──────────┘
   │In-Memory│
   │ Backend │
   │  (dev)  │
   └─────────┘
```

---

## 2. Working Memory (Sessions)

### Design Pattern: Strategy

The session subsystem uses the **Strategy pattern** with a backend abstraction:

```
SessionBackend (ABC)
├── InMemoryBackend   ← local dev, tests
└── RedisBackend      ← production
```

**`SessionBackend`** defines 5 methods:
- `save(session_id, session_data)` — persist a session
- `load(session_id)` → session data or None
- `delete(session_id)` — remove a session
- `list_ids()` → all active session IDs
- `close()` — cleanup resources

### InMemoryBackend

- Thread-safe dict store (`threading.Lock`)
- Sessions lost on server restart
- Zero external dependencies — used for local development and testing

### RedisBackend

- Key prefix: `wm:session:`
- JSON serialization for session data
- Native Redis TTL via `setex()` for automatic expiry
- No manual expiry checking needed — Redis handles it

### Session Model

```python
@dataclass
class ContextEntry:
    node_type: str          # e.g., "APIFunction", "Register"
    node_id: str            # unique identifier
    data: dict              # node properties
    source: str             # "kg", "rag", "sandbox", "user"
    query_text: str         # the query that fetched this entry

@dataclass
class Session:
    session_id: str
    assistant_name: str     # e.g., "CIA", "GEST"
    module_context: str     # default module for queries
    ttl_seconds: int        # default: 3600
    created_at: datetime
    expires_at: datetime
    context_entries: List[ContextEntry]
```

**TTL enforcement**: `Session.is_expired()` checks `datetime.now() > expires_at`. `WorkingMemoryManager` auto-purges expired sessions on every operation.

### WorkingMemoryManager

Orchestrates session lifecycle:

1. **Create session**: validates project/module against `ontology.yaml`, selects correct profile (illd/mcal), creates `Session` object with TTL
2. **Store data**: accepts key-value pairs, wraps in `ContextEntry` with source tracking
3. **Retrieve data**: returns stored entries, checks session validity
4. **End session**: persists audit trail, removes from backend

Ontology validation ensures that sessions reference valid modules and node types — prevents data integrity issues from misconfigured DAs.

### Domain Session Adapter

`DomainSessionAdapter` (323 lines) provides a simplified API for MCP tool handlers. It maps MCP tool parameters to `WorkingMemoryManager` operations and handles type mappings:

- `RAG_TYPE_MAP` — maps search result types to session context entry types
- `KG_TYPE_MAP` — maps Neo4j node labels to session context entry types

---

## 3. Ephemeral Sandbox

`SandboxManager` (in `ephemeral_sandbox.py`, 3667 lines) provides per-session temporary knowledge stores for user-uploaded documents. This lets DAs explore documents that aren't in the main knowledge graph.

### Architecture

Each sandbox session gets:
- **`EphemeralGraph`** — a NetworkX `DiGraph` (in-memory) for graph queries
- **`EphemeralVectors`** — an in-memory keyword index for text search

```
sandbox_upload(file_path)
    │
    ├── Parse file (reuses ingestion parsers)
    ├── Add nodes/edges to EphemeralGraph (NetworkX)
    └── Index content in EphemeralVectors (keyword index)

sandbox_query(query)
    │
    ├── Search EphemeralGraph (property matching)
    ├── Search EphemeralVectors (keyword matching)
    └── Merge and return results
```

### Resource Limits

| Limit | Value |
|-------|-------|
| Max file size | 10 MB (per file, `MAX_FILE_SIZE`) |
| Max chunks per session | 5000 (`max_chunks`) |
| Max KG-pull nodes per upload | 500 (`MAX_PULL_NODES`) |
| Lifetime | Destroyed when session ends |

### Keyword Index

`EphemeralVectors` implements a keyword-based search with **camelCase splitting** — important for AUTOSAR naming conventions:

- `IfxCan_Node_initBitTiming` → `["Ifx", "Can", "Node", "init", "Bit", "Timing"]`
- Queries are also split and matched against the index
- No embedding model needed — lightweight for temporary use

---

## 4. Semantic Memory (Pattern Store)

`PatternStore` (485 lines) manages **approved patterns** — learned from feedback on DA responses. When a DA response is approved (via the Review Gate), successful patterns are extracted and stored for future reference.

### Storage

Qdrant collection with 384-dimensional embeddings (same model as main RAG). Each pattern is an `ApprovedPattern`:

```python
@dataclass
class ApprovedPattern:
    pattern_id: str
    description: str        # what the pattern does
    context: str            # when to apply it
    code_snippet: str       # the actual code/content
    module: str
    da_name: str            # which DA created it
    confidence: float       # confidence score when approved
    created_at: datetime
```

### Operations

- **Store**: embed pattern → upsert into Qdrant
- **Search**: embed query → cosine similarity → return top-k patterns above threshold
- **CRUD**: create, read, update, delete individual patterns
- **Similarity match**: used by `ConfidenceCalculator` — if a new response matches an approved pattern (similarity ≥ threshold), the `similar_approved` signal fires (+5 confidence)

### PatternIndex

`PatternIndex` is a backward-compatible wrapper around `PatternStore`, providing a simplified API for legacy code.

### Embedder

`Embedder` wraps `sentence-transformers` with the `all-MiniLM-L6-v2` model (384 dimensions). Loaded lazily to avoid startup cost when patterns aren't needed.

---

## 5. Node Sets

Node sets provide **module isolation** within a shared Neo4j database. See [NODE_SETS_ARCHITECTURE.md](../NODE_SETS_ARCHITECTURE.md) for the full design.

### NodeSetManager (372 lines)

Manages `:NodeSet` anchor node lifecycle:
- **Create**: `MERGE (:NodeSet {module: $module, project: $project, status: "active"})`
- **Freeze/Archive**: update status for version management
- **List**: enumerate all node sets for a project
- **Delete**: remove anchor and cascade (admin only)

### ScopedQuery (424 lines)

Query interface that always starts from a NodeSet anchor:

```python
scoped = ScopedQuery(neo4j_driver, project="proj_A", module="cxpi")

# These all implicitly scope through the NodeSet anchor
functions = scoped.get_functions()     # All functions in cxpi
registers = scoped.get_registers()     # All registers in cxpi
summary = scoped.get_summary()         # Node type distribution
```

Methods: `get_functions()`, `get_registers()`, `get_structs()`, `get_requirements()`, `get_summary()`, `get_neighbors()`.

> **Note**: Some property names in `node_set_manager.py` and `scoped_query.py` are marked as `PLACEHOLDER` pending confirmation from the Ingestion team on final property naming conventions.

---

## 6. Context Builder

Two context builder implementations exist:

### Querier ContextBuilder (`src/HybridRAG/code/querier/context_builder.py`)

The primary context assembler used by MCP tools. Implements a **10-slot token budget algorithm**:

**Slots and default budgets** (tokens):

| Slot | Budget | Priority |
|------|--------|----------|
| `API_FUNCTIONS` | 5000 | Highest |
| `REQUIREMENTS` | 3000 | High |
| `TESTS` | 3000 | High |
| `DEPENDENCIES` | 2500 | Medium |
| `RELATIONSHIPS` | 1500 | Medium |
| `SAFETY` | 1200 | Medium |
| `CUSTOM` | 1000 | Low |
| `CODE_EXAMPLES` | 500 | Low |
| `REGISTERS` | 500 | Low |
| `CONVERSATION` | 300 | Lowest |

**Algorithm**:
1. Group incoming candidates by slot type, sort by relevance descending
2. **First pass**: fill each slot up to its budget with highest-relevance items
3. **Redistribute**: slots using <30% of budget donate surplus to slots using ≥90%
4. **Second pass**: fill redistributed budget with previously-skipped items
5. **Hard cap**: if total exceeds `total_budget` (default 8000), trim lowest-relevance items globally

**Token estimation**: `len(text) // 4` — ~4 characters per token (M09 standardization).

**Output**: `render()` assembles slots into sections:
```
=== API_FUNCTIONS ===
[content]

=== REQUIREMENTS ===
[content]
...
```

### Memory Layer ContextBuilder (`src/MemoryLayer/memory/context_builder.py`)

A simpler "librarian" metaphor with fixed allocation:
- 20% → conversation history
- 5% → session state
- 75% → RAG/KG search results

Fills greedily with provenance tracking (source of each entry). Used for direct session-based context assembly.

---

## 7. Ontology Loader

`OntologyLoader` (335 lines) is a **singleton** that loads and provides typed access to `ontology.yaml` (7275 lines).

### Ontology Structure

```yaml
profiles:
  illd:
    description: "iLLD reference driver profile"
    node_types: [APIFunction, DataStructure, Register, ...]
    relationships: [CALLS_INTERNALLY, HAS_FIELD, ...]
  mcal:
    description: "MCAL productive software profile"
    node_types: [StakeholderRequirement, ProductRequirement, ...]
    relationships: [DERIVES_FROM, VERIFIED_BY, ...]

node_types:
  APIFunction:
    properties:
      name: {type: string, required: true}
      signature: {type: string}
      ...
    extraction_patterns: [...]
  Register:
    properties:
      name: {type: string, required: true}
      address: {type: string}
      ...
```

### Access Methods

- `get_profiles()` → list of profile names
- `get_node_types(profile)` → node types for a profile
- `get_relationships(profile)` → relationship types for a profile
- `get_property_schema(node_type)` → property definitions
- `validate_node(node_type, properties)` → check against schema

The singleton pattern ensures the 7275-line YAML is parsed only once and shared across all services.

---

## 8. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| **working_memory/** | | |
| `working_memory/session.py` | 225 | `Session` and `ContextEntry` dataclasses |
| `working_memory/manager.py` | 659 | `WorkingMemoryManager`, `SessionBackend` ABC, backends |
| **semantic_memory/** | | |
| `semantic_memory/pattern_store.py` | 485 | `PatternStore` — Qdrant-backed CRUD + similarity |
| `semantic_memory/pattern_index.py` | 98 | Backward-compatible `PatternIndex` wrapper |
| `semantic_memory/embedder.py` | 187 | sentence-transformers wrapper |
| **node_sets/** | | |
| `node_sets/node_set_manager.py` | 372 | `:NodeSet` anchor node CRUD |
| `node_sets/scoped_query.py` | 424 | NodeSet-scoped query methods |
| **Root files** | | |
| `ephemeral_sandbox.py` | 3667 | `SandboxManager`, `EphemeralSandbox`, `EphemeralGraph`, `EphemeralVectors` |
| `context_builder.py` | 233 | "Librarian" context builder |
| `ontology_loader.py` | 335 | Singleton `OntologyLoader` |
| `domain_session_adapter.py` | 323 | MCP-routed session adapter |
| `session_manager.py` | 224 | Sprint 2 lightweight compatibility layer |
| `few_shot_library.py` | — | Few-shot example store |

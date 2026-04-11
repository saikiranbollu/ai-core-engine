# Node Sets Architecture

**Date:** February 24, 2026
**Status:** Active Development

> **IMPORTANT CLARIFICATION:** Neo4j and Qdrant do **not** store "facts" as their primary content. They store the **core engineering input data** — parsed from source documents (hardware specs, API docs, requirements, register definitions, source code, pattern libraries, etc.) — that Domain Assistants use to do their work. "Facts" (learned patterns from user acceptances) are a **separate, additional layer** built on top of this core data.

---

## High Level Overview

The Memory Layer uses **two databases** working together:

- **Neo4j** → Knowledge Graph. Stores the **core engineering data** as structured nodes and typed relationships — Functions, Structs, Registers, Enums, Requirements, Hardware Specs, and all their interconnections (CALLS_INTERNALLY, HAS_FIELD, IMPLEMENTS, ACCESS_TYPE, etc.). Used for structured graph traversal.
- **Qdrant** → RAG / Vector Search. Stores the **same core engineering data** as vector embeddings — chunked and embedded from the same source documents. Used for semantic similarity search (find content that *means* something similar to the query).

Both databases are **single shared instances** — not one-per-module. Instead, data is logically separated inside them using **Node Sets** (in Neo4j) and **Collections + Filters** (in Qdrant).

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MEMORY LAYER                                 │
│                                                                     │
│   User Query                                                        │
│       │                                                             │
│       ├──────────────────────────────────────────────────────────►  │
│       │              NEO4J  (Knowledge Graph)                       │
│       │              NodeSet anchor → HAS_MODULE → Nodes            │
│       │              Returns: structured data + typed relationships  │
│       │                                                             │
│       └──────────────────────────────────────────────────────────►  │
│                      QDRANT  (RAG / Vector Search)                  │
│                      Collection per module → HNSW + Filters         │
│                      Returns: semantically similar content          │
│                                                                     │
│   Both results combined → ranked → returned to Domain Assistant     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 1. Neo4j — Node Sets via Subgraph Logic

### How It Works

In Neo4j, every module has its own **anchor node** called a `NodeSet`. This anchor node acts as the entry point — the root — for all engineering data nodes belonging to that module. Every node (Function, Register, Struct, Requirement, etc.) is linked to its anchor via a `HAS_MODULE` relationship.

When a query comes in, it **always starts from the anchor node** and traverses only downward through `HAS_MODULE` edges. This means data from other modules is physically unreachable unless you explicitly ask for it. No bleed. No pollution.

```
(:NodeSet {module: "cxpi", project: "proj_A"})
        │
        │  [:HAS_MODULE]
        │
        ├──► (:Function  {name: "IfxCxpi_initChannel",  source_file: "IfxCxpi.c"})
        ├──► (:Register  {name: "CLC",  address: "0xF0000000"})
        ├──► (:Struct    {name: "IfxCxpi_Config"})
        └──► (:Requirement {id: "CXPI_REQ_001", asil: "ASIL-B"})
                    │
                    │  [:IMPLEMENTS]
                    │
                    └──► (:Function {name: "IfxCxpi_initChannel"})
                                  (still inside cxpi — same subgraph)
```

The CAN module has its own completely separate anchor:

```
(:NodeSet {module: "can", project: "proj_A"})
        │
        │  [:HAS_MODULE]
        │
        ├──► (:Function  {name: "IfxCan_initNode"})
        └──► (:Register  {name: "CAN_NBTP"})
```

These two subgraphs exist inside the **same Neo4j database** but are completely isolated from each other unless a cross-module relationship is explicitly defined.

---

### Cypher Queries

**Standard scoped query — always starts from the anchor:**

```cypher
// Fetch all Function nodes for cxpi module in proj_A
MATCH (ns:NodeSet {project: "proj_A", module: "cxpi"})
-[:HAS_MODULE]-> (f:Function)
RETURN f.name, f.signature, f.source_file
ORDER BY f.name
```

**Filter by node type inside the same module:**

```cypher
// Only fetch Register nodes from cxpi
MATCH (ns:NodeSet {project: "proj_A", module: "cxpi"})
-[:HAS_MODULE]-> (r:Register)
RETURN r.name, r.address, r.description
```

**Traverse typed relationships between nodes — still inside the same subgraph:**

```cypher
// Find all functions that implement a given requirement inside cxpi
MATCH (ns:NodeSet {project: "proj_A", module: "cxpi"})
-[:HAS_MODULE]-> (req:Requirement {id: "CXPI_REQ_001"})
<-[:IMPLEMENTS]- (fn:Function)
RETURN req.id, fn.name, fn.source_file
```

**Why this is safe — the anchor enforces the scope boundary:**

```
Without anchor (WRONG — returns everything):
   MATCH (f:Function) RETURN f
   → returns cxpi functions, can functions, lin functions, proj_B functions — all mixed

With anchor (CORRECT — scoped to module):
   MATCH (ns:NodeSet {module: "cxpi"}) -[:HAS_MODULE]-> (f:Function)
   RETURN f
   → returns ONLY cxpi Function nodes from that project
```

---

### Node Set Schema (Neo4j)

```cypher
// Anchor node — one per module per project
(:NodeSet {
    id:         "ns_proj_a_cxpi",
    project:    "proj_A",
    module:     "cxpi",
    status:     "active",         // active | frozen | archived
    created_at: datetime()
})

// Function node — parsed from C source code
(:Function {
    id:           "fn_cxpi_001",
    name:         "IfxCxpi_initChannel",
    signature:    "void IfxCxpi_initChannel(Ifx_CXPI *cxpiSFR, const IfxCxpi_Config *config)",
    source_file:  "IfxCxpi.c",
    module:       "cxpi",
    project:      "proj_A",
    vector_id:    "qdrant_point_fn_001"   // cross-reference to Qdrant
})

// Register node — parsed from hardware spec / register definition docs
(:Register {
    id:           "reg_cxpi_001",
    name:         "CLC",
    address:      "0xF0000000",
    description:  "Clock Control Register",
    module:       "cxpi",
    project:      "proj_A",
    vector_id:    "qdrant_point_reg_001"
})

// Requirement node — parsed from requirements document
(:Requirement {
    id:           "CXPI_REQ_001",
    text:         "The module shall initialize within 10ms of power-on",
    asil:         "ASIL-B",
    module:       "cxpi",
    project:      "proj_A",
    vector_id:    "qdrant_point_req_001"
})

// Relationship — anchors a node to its module
(:NodeSet {module: "cxpi"}) -[:HAS_MODULE]-> (:Function {id: "fn_cxpi_001"})

// Relationships between nodes — parsed from source / docs
(:Function {name: "IfxCxpi_initChannel"}) -[:CALLS_INTERNALLY {order:0, line:326}]-> (:Function {name: "IfxCxpi_resetModule"})
(:Function {name: "IfxCxpi_initChannel"}) -[:IMPLEMENTS]-> (:Requirement {id: "CXPI_REQ_001"})
(:Function {name: "IfxCxpi_initChannel"}) -[:USES_TYPE]->   (:Struct {name: "IfxCxpi_Config"})
(:Register {name: "CLC"})                 -[:HAS_FIELD]->   (:RegisterField {name: "DISR"})
(:RegisterField {name: "DISR"})           -[:ACCESS_TYPE]-> (:AccessMode {mode: "RW"})
```

---

### Example — Full Picture for proj_A

```
NEO4J (single instance)
│
├── (:NodeSet {project:"proj_A", module:"cxpi"})
│       │ [:HAS_MODULE]
│       ├── (:Function    {name:"IfxCxpi_initChannel"})
│       │       │ [:CALLS_INTERNALLY]
│       │       └── (:Function {name:"IfxCxpi_resetModule"})
│       ├── (:Register    {name:"CLC", address:"0xF0000000"})
│       │       │ [:HAS_FIELD]
│       │       └── (:RegisterField {name:"DISR"})
│       ├── (:Struct      {name:"IfxCxpi_Config"})
│       └── (:Requirement {id:"CXPI_REQ_001", asil:"ASIL-B"})
│               │ [:IMPLEMENTS] (reverse: Function IMPLEMENTS Requirement)
│               └── (:Function {name:"IfxCxpi_initChannel"})
│
├── (:NodeSet {project:"proj_A", module:"can"})
│       │ [:HAS_MODULE]
│       ├── (:Function {name:"IfxCan_initNode"})
│       └── (:Register {name:"CAN_NBTP"})
│
└── (:NodeSet {project:"proj_A", module:"lin"})
        │ [:HAS_MODULE]
        ├── (:Function {name:"IfxLin_initMasterChannel"})
        └── (:Register {name:"LIN_CON"})
```

All three NodeSets live inside one Neo4j database. Each one is a completely isolated subgraph that can only be accessed through its own anchor node.

---

## 2. Qdrant — Collections, Filters, and HNSW

### How It Works

Qdrant is used for **semantic / RAG search** — finding engineering content that is *meaningfully similar* to the user's query, not just exact matches.

The structure is:

```
One Qdrant Instance (internally hosted)
│
└── One Collection per Module
        │
        └── Every piece of engineering data is stored as a Point
                - vector:  384-dimensional embedding of the content text
                - payload: metadata (data_type, name, module, project, source_file, ...)
```

When a query comes in:
1. The query text is embedded into a 384-dimensional vector
2. Qdrant's **HNSW index** finds the most semantically similar content vectors in O(log n) time
3. **Payload filters** are applied to ensure only the right module/project/type comes back
4. Results are returned ranked by similarity score

---

### Collection Structure

```
Qdrant Instance: internal.qdrant.company.local:6333
│
├── Collection: "proj_a_cxpi"     ← all engineering data for cxpi module, proj_A
│       ├── Point {id:"fn_001",  vector:[...384 dims...], payload:{data_type:"function",    name:"IfxCxpi_initChannel", module:"cxpi", ...}}
│       ├── Point {id:"reg_001", vector:[...384 dims...], payload:{data_type:"register",    name:"CLC",                 module:"cxpi", ...}}
│       ├── Point {id:"str_001", vector:[...384 dims...], payload:{data_type:"struct",      name:"IfxCxpi_Config",      module:"cxpi", ...}}
│       └── Point {id:"req_001", vector:[...384 dims...], payload:{data_type:"requirement", id:"CXPI_REQ_001",          module:"cxpi", ...}}
│
├── Collection: "proj_a_can"      ← all engineering data for can module, proj_A
│       ├── Point {id:"fn_101",  vector:[...384 dims...], payload:{data_type:"function",    name:"IfxCan_initNode", module:"can", ...}}
│       └── Point {id:"reg_101", vector:[...384 dims...], payload:{data_type:"register",    name:"CAN_NBTP",        module:"can", ...}}
│
└── Collection: "proj_a_lin"      ← all engineering data for lin module, proj_A
        └── Point {id:"fn_201",  vector:[...384 dims...], payload:{data_type:"function",    name:"IfxLin_initMasterChannel", module:"lin", ...}}
```

---

### HNSW Indexing

Every Qdrant collection is configured with **HNSW (Hierarchical Navigable Small Worlds)** indexing. HNSW builds a multi-layer graph over the vectors so that similarity search runs in O(log n) time instead of scanning every vector (O(n)).

```
HNSW Index Structure (built once, queried many times):

Layer 2 — sparse:   ●─────────────●──────────────●
                    │             │              │
                    (rough navigation, big jumps)

Layer 1 — medium:   ●───●────●──●────●──●──●───●
                    │   │    │  │    │  │  │   │
                    (refined navigation)

Layer 0 — dense:    ●─●─●─●─●─●─●─●─●─●─●─●─●─●
                    (all content points, fine-grained neighbors)

Query:
  1. Enter at Layer 2 → jump to rough region
  2. Drop to Layer 1  → narrow to nearby area
  3. Search Layer 0   → return exact top-K matches

Result: O(log n) time — stays fast even as content grows into the thousands
```

HNSW configuration used:

```python
hnsw_config = HnswConfigDiff(
    m=16,             # connections per node — higher = better recall, more memory
    ef_construct=200  # build quality — higher = better index, slower to build
)
```

---

### Scoped Qdrant Search with Advanced Filters

Qdrant's **payload filters** are what enforce the module scope — equivalent to what `HAS_MODULE` does in Neo4j.

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from sentence_transformers import SentenceTransformer

qdrant   = QdrantClient(url="http://internal.qdrant.company.local:6333")
embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

def search_module_content(project, module, query, data_type=None, top_k=10):

    query_vector = embedder.encode(query).tolist()

    # Build filter — this is the scope boundary
    must_conditions = [
        FieldCondition("module",  match=MatchValue(value=module)),
        FieldCondition("project", match=MatchValue(value=project)),
    ]

    # Optional: narrow down to a specific data type
    if data_type:
        must_conditions.append(
            FieldCondition("data_type", match=MatchValue(value=data_type))
        )

    results = qdrant.search(
        collection_name=f"{project}_{module}",   # e.g. "proj_a_cxpi"
        query_vector=query_vector,
        query_filter=Filter(must=must_conditions),
        limit=top_k
    )

    return [
        {
            "data_type":  r.payload["data_type"],
            "name":       r.payload.get("name") or r.payload.get("id"),
            "module":     r.payload["module"],
            "similarity": r.score,              # HNSW similarity score 0.0 – 1.0
            "neo4j_id":   r.id                  # same ID as the Neo4j node
        }
        for r in results
    ]
```

**Example call:**

```python
results = search_module_content(
    project="proj_a",
    module="cxpi",
    query="initialize CXPI module with struct configuration",
    data_type="function",   # optional: only return Function nodes
    top_k=10
)

# Returns only cxpi content from proj_a, data_type=function
# Ranked by semantic similarity to the query
# Latency: ~10-15ms (HNSW O(log n))
```

---

### Example — Full Picture for proj_A

```
QDRANT (single instance)

Collection: "proj_a_cxpi"
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ id       │ vector (384 dims) │ payload                                               │
├──────────┼───────────────────┼───────────────────────────────────────────────────────┤
│ fn_001   │ [0.12,-0.45,...]  │ data_type:function,    name:IfxCxpi_initChannel,      │
│          │                   │ module:cxpi, project:proj_a, source_file:IfxCxpi.c    │
├──────────┼───────────────────┼───────────────────────────────────────────────────────┤
│ reg_001  │ [0.33, 0.71,...]  │ data_type:register,    name:CLC,                     │
│          │                   │ address:0xF0000000,    module:cxpi, project:proj_a    │
├──────────┼───────────────────┼───────────────────────────────────────────────────────┤
│ req_001  │ [-0.21,0.55,...]  │ data_type:requirement, id:CXPI_REQ_001,               │
│          │                   │ asil:ASIL-B,           module:cxpi, project:proj_a    │
└──────────────────────────────────────────────────────────────────────────────────────┘

Query: "initialize CXPI with config struct"
  → embed query → [0.10, -0.42, ...]
  → HNSW search finds: fn_001 (score: 0.96), req_001 (score: 0.61), reg_001 (score: 0.44)
  → Filter: module=cxpi, project=proj_a → all 3 pass
  → Return top-2: fn_001, req_001

Collection: "proj_a_can"    ← completely separate, never touched by cxpi queries
Collection: "proj_a_lin"    ← completely separate, never touched by cxpi queries
```

---

## 3. Cross-Module Traversal

By default, **all queries are strictly scoped to one module**. A cxpi query never sees can data. A proj_A query never sees proj_B data.

However, there are real cases where cross-module access is needed:

| Scenario | Example |
|----------|---------|
| Protocol dependency | CXPI initialization calls a function defined in the SPI peripheral module |
| Shared hardware | CXPI and LIN both reference the same CLC register |
| Compliance audit | ASPICE check needs to trace requirements across all modules |
| Release management | Release notes must cover CXPI + CAN + LIN together |

Cross-module access is **always explicit and controlled** — it is never the default.

---

### In Neo4j — Cross-Module Relationship

A direct relationship between two `NodeSet` anchor nodes expresses that one module depends on another. Individual nodes that span modules are linked with an explicit `CROSS_REF` relationship.

```cypher
// Define the module-level dependency once
(:NodeSet {module: "cxpi"}) -[:DEPENDS_ON]-> (:NodeSet {module: "spi"})

// Cross-module query — explicit, follows the DEPENDS_ON edge
MATCH (ns_cxpi:NodeSet {project: "proj_A", module: "cxpi"})
-[:DEPENDS_ON]-> (ns_spi:NodeSet {project: "proj_A", module: "spi"})
MATCH (ns_cxpi) -[:HAS_MODULE]-> (fn_cxpi:Function)
MATCH (ns_spi)  -[:HAS_MODULE]-> (fn_spi:Function)
WHERE (fn_cxpi) -[:CROSS_REF]->  (fn_spi)
RETURN fn_cxpi.name, fn_spi.name
```

The `CROSS_REF` relationship between specific nodes must also be explicitly created — it is never auto-generated.

---

### In Qdrant — Multi-Collection Search

Cross-module in Qdrant means searching across multiple collections. This is only allowed when the query context explicitly declares the target modules:

```python
def search_cross_module(project, source_module, target_modules, query):

    query_vector = embedder.encode(query).tolist()
    all_results  = []

    for module in [source_module] + target_modules:
        results = qdrant.search(
            collection_name=f"{project}_{module}",
            query_vector=query_vector,
            query_filter=Filter(must=[
                FieldCondition("project", match=MatchValue(value=project)),
                FieldCondition("module",  match=MatchValue(value=module)),
            ]),
            limit=5   # fewer per module when crossing boundaries
        )
        # tag each result with its source module
        all_results.extend([(r, module) for r in results])

    # sort all results by similarity score across modules
    return sorted(all_results, key=lambda x: x[0].score, reverse=True)
```

**Example call:**

```python
# ASPICE audit: trace initialization requirements across cxpi, can, and lin
results = search_cross_module(
    project="proj_a",
    source_module="cxpi",
    target_modules=["can", "lin"],
    query="module initialization sequence requirements"
)

# Returns mixed results from cxpi + can + lin, ranked by similarity
# Each result is tagged with its source module so it is fully traceable
```

---

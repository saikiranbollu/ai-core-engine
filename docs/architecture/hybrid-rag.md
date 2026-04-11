# Hybrid RAG & Search Architecture

**Component**: `src/HybridRAG/`
**Primary class**: `SearchService` (1259 lines)
**Backing stores**: Neo4j (graph traversal) + Qdrant (vector similarity)

---

## Table of Contents

1. [Overview](#1-overview)
2. [SearchService Pipeline](#2-searchservice-pipeline)
3. [Graph Search (Neo4j)](#3-graph-search-neo4j)
4. [Entity-Targeted Lookup](#4-entity-targeted-lookup)
5. [Vector Search (Qdrant)](#5-vector-search-qdrant)
6. [Reciprocal Rank Fusion (RRF)](#6-reciprocal-rank-fusion-rrf)
7. [Alpha Blending](#7-alpha-blending)
8. [Label-Aware Search](#8-label-aware-search)
9. [Knowledge Intelligence Service](#9-knowledge-intelligence-service)
10. [KG Construction Pipeline](#10-kg-construction-pipeline)
11. [File Map](#11-file-map)

---

## 1. Overview

The Hybrid RAG engine is the core retrieval subsystem of AICE. It answers queries by searching **two complementary data stores** in parallel and merging results:

- **Neo4j Knowledge Graph** — for structured traversal: "find all functions that implement requirement X", "what does struct Y contain", "dependency chain from A to B"
- **Qdrant Vector Store** — for semantic similarity: "how to configure baud rate" matches content about `IfxCan_Node_initBitTiming` even though no keywords overlap

The merge strategy is **Reciprocal Rank Fusion (RRF)** with a configurable alpha parameter that lets callers control the graph-vs-vector weight.

```
              User Query
                  │
    ┌─────────────┼──────────────┐
    │             │              │
    ▼             ▼              ▼
 Graph       Entity-         Vector
 Search     Targeted         Search
 (Neo4j)     Lookup         (Qdrant)
    │         (Neo4j)           │
    │             │              │
    └─────────────┼──────────────┘
                  │
              RRF Merge
             (α blending)
                  │
                  ▼
           Ranked Results
```

---

## 2. SearchService Pipeline

`SearchService.search()` executes a **5-stage pipeline**:

### Stage 1: Query Analysis

Extracts structured signals from the natural language query:

- **Label inference**: Keywords like "function", "register", "requirement" map to Neo4j node labels (e.g., `APIFunction`, `Register`, `StakeholderRequirement`)
- **Keyword extraction**: Identifies potential entity names for exact-match lookup
- **Module detection**: Extracts module references (e.g., "ADC", "CAN") for scoped filtering

### Stage 2: Graph Search (Neo4j)

Executes Cypher queries against the knowledge graph:

- Full-text index search across node properties
- Property filter matching (label, module, workspace)
- NodeSet-scoped: all queries anchor through `:NodeSet` nodes to enforce module isolation

### Stage 3: Entity-Targeted Lookup

Exact-match search across **9 property fields** to catch specific entity references:

```
function_name, param_name, name, title, api_name,
test_case_id, requirement_id, decision_id, type_name
```

With optional module filtering. Results include **1-hop neighbor expansion** — for each matched entity, immediate neighbors (connected by any relationship) are also returned. This surfaces related context like "function X → calls → function Y" or "function X → implements → requirement Z".

### Stage 4: Vector Search (Qdrant)

Embeds the query using `all-MiniLM-L6-v2` (384 dimensions), then performs cosine similarity search against the appropriate Qdrant collection. Collection naming follows `{workspace}_{module}_embeddings`.

### Stage 5: RRF Merge

Combines all result sets using Reciprocal Rank Fusion (see [Section 6](#6-reciprocal-rank-fusion-rrf)), deduplicates by `node_id`, and applies `top_k` limit.

---

## 3. Graph Search (Neo4j)

### Query Construction

The graph search constructs Cypher queries dynamically based on the query analysis. Two patterns:

**Scoped search (default)** — always starts from a NodeSet anchor:

```cypher
MATCH (ns:NodeSet {module: $module, project: $project})
-[:HAS_MODULE]-> (n)
WHERE n.name CONTAINS $keyword OR n.description CONTAINS $keyword
RETURN n
ORDER BY n.name
LIMIT $limit
```

**Label-filtered search** — when the query analysis infers a specific label:

```cypher
MATCH (ns:NodeSet {module: $module})
-[:HAS_MODULE]-> (n:APIFunction)
WHERE n.function_name CONTAINS $keyword
RETURN n
```

### Database Resolution

`_db_for_workspace(workspace_id)` maps workspace identifiers to Neo4j database names. The `illd` and `mcal` workspaces use separate Neo4j databases for complete data isolation.

### Read-Only Enforcement

All Cypher execution goes through the `execute_cypher` tool, which rejects write operations. Regex checks block clauses: `CREATE`, `DELETE`, `SET`, `MERGE`, `DROP`, `REMOVE`.

---

## 4. Entity-Targeted Lookup

A targeted exact-match phase that complements fuzzy graph and vector search. The implementation searches **9 named property fields** in Neo4j using parameterized `OR` queries:

```cypher
MATCH (n)
WHERE n.function_name = $entity
   OR n.name = $entity
   OR n.api_name = $entity
   OR n.requirement_id = $entity
   OR ...
RETURN n
```

**1-hop expansion**: For each matched node, a second query fetches immediate neighbors:

```cypher
MATCH (n)-[r]-(neighbor)
WHERE elementId(n) = $node_id
RETURN n, r, neighbor
```

This is critical for domain queries like "tell me about `IfxCan_Node_init`" — the exact match finds the function, and the 1-hop expansion surfaces its parameters, return type, called functions, implementing requirements, and accessed registers.

---

## 5. Vector Search (Qdrant)

### Embedding

Queries are embedded using `all-MiniLM-L6-v2` via the sentence-transformers library. This produces a 384-dimensional dense vector.

### Collection Structure

Each module in each workspace has a dedicated Qdrant collection:

```
{workspace}_{module}_embeddings
```

Examples: `illd_adc_embeddings`, `mcal_can_embeddings`

Collection naming is managed by `collection_naming_unified.py` (255 lines) for consistency.

### ID Mapping

Qdrant uses UUID5 deterministic mapping from Neo4j node IDs to Qdrant point IDs. This is implemented in `vector_store_factory.py` and ensures that the same node always maps to the same Qdrant point, enabling idempotent upserts during ingestion.

### Similarity Metric

Cosine similarity. Qdrant's HNSW index provides approximate nearest-neighbor search with configurable `ef` parameter for recall-speed tradeoff.

---

## 6. Reciprocal Rank Fusion (RRF)

### Formula

```
RRF_score(document) = α × 1/(k + rank_graph + 1) + (1-α) × 1/(k + rank_vector + 1)
```

Where:
- `k = 60` (constant, standard value from Cormack et al.)
- `rank_graph` = position in graph search results (0-indexed)
- `rank_vector` = position in vector search results (0-indexed)
- `α` = alpha blending parameter (0.0 to 1.0)

### Why RRF

RRF is **score-agnostic**: it only uses rank positions, not raw scores. This is critical because:
- Neo4j returns relevance scores from full-text indexing (arbitrary scale)
- Qdrant returns cosine similarity scores (0.0 to 1.0)
- These scales are incomparable — raw score interpolation would be meaningless

RRF normalizes both into a shared rank-based scale.

### Implementation

```python
def _merge_results_rrf(self, graph_results, vector_results, alpha, k=60):
    scores = {}
    for rank, item in enumerate(graph_results):
        nid = item["node_id"]
        scores[nid] = scores.get(nid, 0) + alpha * (1 / (k + rank + 1))
    for rank, item in enumerate(vector_results):
        nid = item["node_id"]
        scores[nid] = scores.get(nid, 0) + (1 - alpha) * (1 / (k + rank + 1))
    # Deduplicate, sort descending, return
```

### Legacy Merge

A simpler linear interpolation (`_merge_results()`) is kept for backward compatibility but is not the primary merge path. It directly interpolates raw scores, which is less robust.

---

## 7. Alpha Blending

The `alpha` parameter controls the graph-vs-vector weight:

| Alpha | Behavior | Use Case |
|-------|----------|----------|
| `0.0` | Pure graph search | Exact structural queries ("what calls function X") |
| `0.3` | Graph-heavy | Code generation (need exact API signatures + some semantic) |
| `0.5` | Balanced | General-purpose queries |
| `0.8` | Vector-heavy | Structural lookups, struct definitions |
| `1.0` | Pure vector search | Broad semantic queries ("how to configure timing") |

DAs set alpha based on their task type. The `RLMOrchestrator` uses different alphas per sub-query based on the `RLMTaskType` — e.g., `code_generation` uses `α=0.8` for struct lookups and `α=0.3` for semantic context.

---

## 8. Label-Aware Search

When the query analysis detects a specific entity type (via keywords or DA hints), the search narrows to that label:

| Keyword Pattern | Inferred Label | Example Query |
|----------------|----------------|---------------|
| "function", "API", "init" | `APIFunction` | "IfxCan_Node_init function" |
| "register", "SFR", "bitfield" | `Register` | "CLC register address" |
| "requirement", "REQ", "shall" | `StakeholderRequirement` / `ProductRequirement` | "ADC_REQ_001" |
| "struct", "config", "typedef" | `DataStructure` | "IfxCan_Config struct" |
| "test", "test case" | `VerificationStep` | "test cases for ADC init" |

This significantly improves precision — instead of searching all node types, the graph search targets only the relevant label.

---

## 9. Knowledge Intelligence Service

`KnowledgeIntelligenceService` (683 lines) provides higher-level query operations that build on graph traversal:

### API Function Intelligence (`query_api_function`)

Returns **25+ fields** for a single API function:
- Signature, parameters (with types and descriptions), return type
- Module, file location, source
- Dependencies: functions it calls, functions that call it
- Traceability: linked requirements, test cases
- MISRA compliance notes
- Initialization sequence position (topological order)
- Register accesses (which SFRs it reads/writes)
- Safety criticality (ASIL level)

Works as: fuzzy search → pick best match → enrich via graph traversal.

### Type Definition Resolution (`get_type_definition`)

Returns struct/enum/typedef definitions with all fields, default values, and related functions. Same pattern: fuzzy match → enrich.

### Dependency Analysis (`query_dependencies`)

Resolves direct and transitive dependencies via graph traversal up to configurable depth. Returns:
- Dependency tree (who calls whom)
- **Topological init_sequence** — the correct initialization order
- Direct vs. transitive dependency counts

### API Usage Validation (`validate_api_usage`)

Takes an ordered list of function calls and checks them against the dependency graph. Reports violations where a function is called before its dependencies are initialized.

### Traceability (`find_requirement_traces`, `build_traceability_matrix`)

Traces V-Model chains: `Requirement → IMPLEMENTS → Function → TESTS → TestCase → YIELDS → TestResult`. Module-wide matrix generation in JSON/CSV/HTML formats. Gap detection for incomplete chains.

### HW-SW Link Analysis (`analyze_hw_sw_links`)

Maps register accesses to functions, detecting undocumented register accesses (functions touching registers without a documented `ACCESS_TYPE` relationship).

---

## 10. KG Construction Pipeline

`build_knowledge_graph.py` (4668 lines) is the largest file in the codebase. It handles the full pipeline from parsed source documents to Neo4j + Qdrant:

1. **Node creation**: MERGE nodes with labels and properties into Neo4j
2. **Relationship creation**: Create typed edges (CALLS_INTERNALLY, HAS_FIELD, IMPLEMENTS, ACCESS_TYPE, etc.)
3. **NodeSet linking**: Attach every node to its module's `:NodeSet` anchor
4. **Vector embedding**: Embed node content and upsert into Qdrant
5. **Index management**: Create Neo4j full-text and property indexes
6. **Validation**: Verify ontology compliance after construction

---

## 11. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `code/querier/search_service.py` | 1259 | Hybrid search pipeline, RRF, entity lookup |
| `code/querier/knowledge_intelligence.py` | 683 | API/dependency/traceability queries |
| `code/querier/rlm_orchestrator.py` | 712 | Multi-step retrieval (see [RLM doc](rlm-orchestrator.md)) |
| `code/querier/context_builder.py` | 236 | 10-slot token budget assembly |
| `code/querier/kg_node_utils.py` | 625 | Node display, scoring, classification |
| `code/KG/build_knowledge_graph.py` | 4668 | Full KG construction pipeline |
| `code/KG/query_knowledge_graph.py` | 1595 | Legacy query module |
| `code/RAG/hybrid_rag_unified.py` | 295 | Profile-agnostic RAG orchestrator |
| `code/RAG/rag_query_unified.py` | 404 | Unified RAG query engine |
| `code/RAG/vector_store_factory.py` | 266 | Qdrant client factory, UUID5 mapping |
| `code/RAG/collection_naming_unified.py` | 255 | Collection naming conventions |
| `code/neo4j_manager.py` | 536 | Connection management, config loading |
| `code/token_manager.py` | 338 | GPT4IFX JWT token lifecycle |
| `code/pdf_pipeline.py` | 957 | PDF processing with structure-aware chunking |
| `config/ontology.yaml` | 6166 | Full ontology: profiles, node types, relationships |
| `config/storage_config.yaml` | ~50 | Neo4j + Qdrant connection configuration |

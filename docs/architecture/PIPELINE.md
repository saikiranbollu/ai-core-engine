# Search Pipeline Architecture

The AI Core Engine search pipeline processes user queries through 8 sequential
stages. Each stage is optional and gated by configuration or query complexity.

---

## Pipeline Diagram

```
  User Query
      |
      v
 +--------------------------+
 | 1. Query Enhancement     |  GAP-A03 — rewrite / expand / decompose
 +--------------------------+
      |
      v
 +--------------------------+      +--------------------------+
 | 2. Graph Search (Neo4j)  |      | 3. Vector Search (Qdrant)|
 |    alpha -> 1.0          |      |    alpha -> 0.0          |
 +--------------------------+      +--------------------------+
      \                              /
       \                            /
        v                          v
     +-------------------------------+
     | 4. RRF Merge + Batch Enrich   |  Reciprocal Rank Fusion
     +-------------------------------+
                  |
                  v
     +-------------------------------+
     | 5. Cross-Encoder Reranking    |  GAP-A01 — FlashRank
     +-------------------------------+
                  |
                  v
     +-------------------------------+
     | 6. Context Compression        |  GAP-A04 — LLMLingua / Extractive
     +-------------------------------+
                  |
                  v
     +-------------------------------+
     | 7. Relevance Judging          |  GAP-A08 — DeepEval
     +-------------------------------+
                  |
                  v
     +-------------------------------+
     | 8. Context Refinement         |  GAP-A07 — CRAG + Self-RAG
     |    (complex queries only)     |
     +-------------------------------+
                  |
                  v
           Final Context
```

---

## Stage Details

### Stage 1 — Query Enhancement (GAP-A03)

Rewrites the raw user query into a form better suited for retrieval.
Operations include synonym expansion, sub-question decomposition for
multi-hop queries, and intent classification to select the downstream
alpha blend (vector vs. graph).

- **Module**: `src/HybridRAG/code/querier/query_enhancer.py`
- **Async**: Wrapped in `asyncio.to_thread` (H08 fix)

### Stage 2 — Graph Search (Neo4j)

Traverses the knowledge graph for structurally related nodes:
MISRA rules, iLLD API relationships, register hierarchies, and
requirement traceability edges.

- **Module**: `src/HybridRAG/code/querier/search_service.py` (the graph-search stage is implemented as `SearchService._graph_search()` and its consolidated UNWIND helper; there is **no** separate `graph_search.py` file)
- **Read mode**: `READ_ACCESS` (H14 fix)
- **Injection protection**: Label allowlist (H17 fix)
- **ID format**: `elementId()` (M07 migration)
- **Alpha = 1.0** selects pure graph search.

### Stage 3 — Vector Search (Qdrant)

Dense retrieval using 384-dimensional embeddings (all-MiniLM-L6-v2 via
FlashRank, replacing PyTorch/sentence-transformers per ADR-038).

- **Module**: `src/HybridRAG/code/querier/search_service.py` (implemented as `SearchService._vector_search()`; there is **no** separate `vector_search.py` file)
- **Fallback**: 384-dim zero-padded embeddings (H18 fix)
- **Alpha = 0.0** selects pure vector search.

### Stage 4 — RRF Merge + Batch Enrichment

Reciprocal Rank Fusion combines graph and vector result lists into a
single ranked set. Batch enrichment resolves cross-references and
attaches metadata (module, file path, confidence scores).

- **Module**: `src/HybridRAG/code/querier/search_service.py` (implemented as `SearchService._merge_results_rrf()`; there is **no** separate `rrf_merge.py` file). Batch enrichment is in `src/HybridRAG/code/querier/batch_graph_resolver.py`.
- **Token estimation**: `len(text) // 4` (M09 standardization)

### Stage 5 — Cross-Encoder Reranking (GAP-A01, FlashRank)

A lightweight cross-encoder rescores the merged results for semantic
relevance. FlashRank was adopted to eliminate the PyTorch dependency
(ADR-038).

- **Module**: `src/HybridRAG/code/querier/reranker.py`
- **Model**: FlashRank default (ms-marco-MiniLM-L-12-v2)

### Stage 6 — Context Compression (GAP-A04, LLMLingua/Extractive)

Reduces token count of the reranked context before it reaches the LLM.
Two strategies are available:

| Strategy    | When used                | Method                     |
|-------------|--------------------------|----------------------------|
| Extractive  | Short / factual queries  | Sentence scoring + pruning |
| LLMLingua   | Long / complex queries   | Token-level compression    |

- **Module**: `src/HybridRAG/code/querier/context_compressor.py`
- **LLM wiring**: `set_llm_fn()` connects GPT4IFX
- **Fix**: Added missing `import re` (C10)

### Stage 7 — Relevance Judging (GAP-A08, DeepEval)

Each context chunk is scored for faithfulness and relevance using
DeepEval metrics. Chunks below the threshold are dropped before
final generation.

- **Module**: `src/HybridRAG/code/querier/relevance_judge.py`
- **LLM wiring**: `set_llm_fn()` connects GPT4IFX

### Stage 8 — Context Refinement (GAP-A07, CRAG + Self-RAG)

Activated only for complex queries (multi-hop, ambiguous, or low
Stage-7 scores). Implements Corrective RAG (CRAG) and Self-RAG
patterns to iteratively improve context quality before generation.

- **Module**: `src/HybridRAG/code/querier/context_refiner.py`
- **LLM wiring**: `set_llm_fn()` connects GPT4IFX
- **Fix**: Added missing `import re` (C10)

---

## Alpha Semantics

The `alpha` parameter controls the blend between graph and vector search:

| Alpha | Behavior          |
|-------|--------------------|
| 0.0   | Pure vector search |
| 0.5   | Balanced (default) |
| 1.0   | Pure graph search  |

---

## Observability

- **OpenTelemetry** (`trace_tool` decorator) is wired to the top 5 MCP tools
  (ADR-036).
- **Prometheus** timing context vars are wired into `_authorize()` (C03).
- **MCP StreamNotifier** progress callbacks fire at each stage boundary.

---

## Related ADRs

| ADR     | Decision                                      |
|---------|-----------------------------------------------|
| ADR-036 | OpenTelemetry adopted for MCP layer only      |
| ADR-037 | asyncio.TaskGroup replaces Celery             |
| ADR-038 | FlashRank replaces PyTorch                    |
| ADR-040 | Rate limiting via slowapi                     |
| ADR-041 | CBMC/FMEA domain tools deferred               |


## Top Performance Bottlenecks (Ranked by Severity)

### 1. CRITICAL -- Graph Search N+1 Query Problem
**File:** `src/HybridRAG/code/querier/search_service.py:228-253`

The `_graph_search()` method runs a **nested loop** of Cypher queries:
```python
for label in labels:      # 5-20 labels
    for kw in keywords:   # 5-10 keywords
        session.run(cypher, ...)  # one round-trip per combo
```
This generates **50-200 individual Neo4j queries per search**, each doing `toLower(CONTAINS)` across 10 string properties (defeating index usage). This is your single biggest bottleneck.

**Fix:** Consolidate into a single Cypher query using `UNWIND` for labels/keywords, and create full-text indexes instead of `CONTAINS` scans.

### 2. HIGH -- Entirely Synchronous Backend on an Async Server
All backend I/O (Neo4j, Qdrant, LLM calls, PostgreSQL) is **synchronous**, called directly from `async` MCP tool handlers. This **blocks the uvicorn event loop** -- one slow query freezes all concurrent requests.

**Fix:** Wrap all sync I/O with `asyncio.to_thread()` at minimum, or migrate to async drivers (`neo4j` async driver, `qdrant-client` async mode, `httpx.AsyncClient`).

### 3. HIGH -- Sequential Hybrid Search Stages
Graph search (Neo4j) and vector search (Qdrant) run **sequentially** in `hybrid_search()`, but they're independent.

**Fix:** Run both searches concurrently with `asyncio.gather()`.

### 4. MEDIUM-HIGH -- Sequential Ingestion
`ingestion_service.py` processes files in a serial loop. A `thread_pool_config.yaml` exists with `max_workers: 4` but is **never used**.

**Fix:** Use `concurrent.futures.ThreadPoolExecutor` or `asyncio.gather` for parallel file ingestion.


### 6. MEDIUM -- httpx Client Recreation Per Request
`rlm_orchestrator.py:534` and `pdf_pipeline.py` create a **new httpx client** for every LLM call instead of reusing a connection pool.

**Fix:** Create a shared `httpx.Client` (or `AsyncClient`) singleton with connection pooling.

### 7. LOW -- Qdrant Using HTTP Instead of gRPC
Config sets `grpc: false`. gRPC is significantly faster for vector operations.

**Fix:** Set `grpc: true` in `storage_config.yaml`.

---


## Recommended Action Plan (Priority Order)

| Priority | Fix | Expected Impact | Effort |
|----------|-----|----------------|--------|
| P0 | Consolidate graph search into single Cypher + full-text indexes | **10-50x faster graph search** | 1-2 days |
| P0 | Wrap sync I/O in `asyncio.to_thread()` | **Unblocks concurrent requests** | 1-2 days |
| P1 | Parallelize Neo4j + Qdrant search with `asyncio.gather()` | **~2x faster hybrid search** | Hours |
| P1 | Reuse httpx client with connection pool | **Reduced LLM call latency** | Hours |
| P2 | Enable Qdrant gRPC transport | **~30% faster vector search** | Minutes |
| P2 | Use FAISS index for semantic cache | **O(1) vs O(n) cache lookup** | 1 day |
| P3 | Parallel file ingestion with ThreadPoolExecutor | **4x faster ingestion** | 1 day |

The P0 fixes alone should dramatically improve your Domain Assistant query latency. Would you like me to implement any of these?
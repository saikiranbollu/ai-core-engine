
## Performance Bottlenecks — Analysis & Fix Status (Sprint 9)

### 1. ~~CRITICAL~~ ✅ FIXED — Graph Search N+1 Query Problem
**File:** `src/HybridRAG/code/querier/search_service.py`

**Problem**: `_graph_search()` ran a nested loop generating 50–200 individual Neo4j queries per search, each doing `toLower(CONTAINS)` across 13 string properties.

**Fix (Sprint 9)**: Consolidated into a single Cypher query using `any(lbl IN labels(n) WHERE lbl IN $labels)` with `UNWIND` for keywords (pre-existing in main). This branch adds fulltext index fallback via `db.index.fulltext.queryNodes('aice_search_idx', ...)` and `ensure_fulltext_index()` to `Neo4jConnection` in `neo4j_manager.py` creating Lucene-backed index across 13 properties.

### 2. ~~HIGH~~ ✅ FIXED — Synchronous Backend on Async Server
**File:** `mcp/core/mcp_server.py`

**Problem**: All backend I/O called synchronously from `async` MCP tool handlers, blocking the uvicorn event loop.

**Fix (Sprint 9)**: Main branch had 29 `asyncio.to_thread()` wrappings. This branch extends coverage to 63 wrappings (all remaining tool handlers). Added `async def _warm_backends()` that parallel-initializes Neo4j (illd + mcal), Qdrant, Redis, PostgreSQL, CacheService via `asyncio.gather()` at startup (new in this branch).

### 3. ~~HIGH~~ ✅ ALREADY DONE (main) — Parallel Hybrid Search Stages
**File:** `src/HybridRAG/code/querier/search_service.py`

Graph search (Neo4j) and vector search (Qdrant) already run in **parallel** via `asyncio.gather()` in `hybrid_search_async()` on the main branch:
```python
graph_results, vector_results = await asyncio.gather(
    _graph_stage(), _vector_stage(),
)
```
Both stages use `asyncio.to_thread()` for non-blocking execution. **No additional changes needed — this was incorrectly listed as partial.**

### 4. ~~MEDIUM-HIGH~~ ✅ FIXED — Sequential Ingestion
**File:** `src/IngestionPipeline/ingestion_service.py`

**Problem**: `batch_ingest()` processed modules in a serial for-loop.

**Fix (Sprint 9)**: Replaced with `ThreadPoolExecutor(max_workers=4)` + `as_completed()` pattern. Added `update_progress()` to `IngestionJobTracker` for per-module progress tracking.

### 5. ~~MEDIUM~~ ✅ FIXED — SemanticCache O(n) Linear Scan
**File:** `src/Configuration/cache_service.py`

**Problem**: `SemanticCache.get()` computed 500 pure-Python dot products per lookup.

**Fix (Sprint 9)**: Replaced `_entries` list with `faiss.IndexFlatIP(384)` for SIMD-optimized sub-ms lookups. Added `RediSearchSemanticCache` as L2 shared cache (feature-flagged via `AICE_CACHE_L2_REDIS`). `CacheService` upgraded from 2-tier to 3-tier: LRU → FAISS L1 → RediSearch L2 → RAG.

### 6. ~~MEDIUM~~ ✅ FIXED — httpx Client Recreation Per Request
`rlm_orchestrator.py` and `pdf_pipeline.py` create a **new httpx client** for every LLM call.

**Fix (Sprint 9)**: Added shared `_shared_http_client` singleton in both files. `_get_shared_openai_client()` in `rlm_orchestrator.py` reuses a single `httpx.Client` with 60s timeout and optional CA bundle, only rebuilding when the auth token changes. `pdf_pipeline.py` uses a similar `_get_shared_http_client()` with thread-safe double-checked locking.

### 7. ~~LOW~~ ✅ ALREADY DONE (main) — Qdrant gRPC Auto-Detection
**File:** `mcp/core/mcp_server.py`

gRPC is **already implemented** with auto-detection in the main branch:
- **In-cluster K8s** (`KUBERNETES_SERVICE_HOST` set + `qdrant.grpc: true` in config): automatically connects via gRPC on port 6334
- **External HTTPS**: falls back to REST (OpenShift edge-terminated ingress only supports HTTP/1.1)

The `prefer_grpc` flag reads from `storage_config.yaml` → `qdrant.grpc`. For K8s deployments, set `grpc: true` and `in_cluster_url` in config. **No code change needed — this is a deployment configuration concern.**

---

## Additional Sprint 9 Improvements

### RLM Progress Reporting + SSE Streaming
**File:** `src/HybridRAG/code/querier/rlm_orchestrator.py`, `mcp/core/mcp_server.py`

Added `on_progress` callback to `RLMOrchestrator.run()` — invoked after planning, each sub-query, and synthesis. The `rlm_orchestrate` tool handler now accepts `ctx: Context` (FastMCP SDK) and bridges progress to `ctx.report_progress(step, total)` + `ctx.info(msg)` via `asyncio.run_coroutine_threadsafe()` for real-time SSE/streamable-HTTP streaming.

### APScheduler Background Jobs
**File:** `mcp/app.py`

Added `BackgroundScheduler` with health check (5 min) and cache stats (30 min) periodic jobs. Graceful shutdown on SIGTERM. Silently disabled if `apscheduler` not installed.

---

## Fix Status Summary

| Priority | Fix | Status | Where |
|----------|-----|--------|-------|
| P0 | Consolidated graph search (UNWIND) | ✅ Done | main |
| P0 | Fulltext index fallback + `ensure_fulltext_index()` | ✅ Done | **this branch** |
| P0 | Async-wrap tool handlers (29 → 63 `to_thread` calls) | ✅ Done | **this branch** |
| P0 | Parallel backend startup (`_warm_backends`) | ✅ Done | **this branch** |
| P1 | Parallel hybrid search (`asyncio.gather`) | ✅ Done | main (was incorrectly listed as partial) |
| P1 | Reuse httpx client pool | ✅ Done | **this branch** |
| P1 | Batch Qdrant upserts (512-size batches, indexing toggle) | ✅ Done | **this branch** |
| P1 | Batch Neo4j writes (UNWIND via Neo4jBatchWriter) | ✅ Done | **this branch** |
| P2 | Qdrant gRPC transport | ✅ Done (code) | main (config-dependent, not a code issue) |
| P2 | FAISS L1 semantic cache | ✅ Done | **this branch** |
| P2 | RediSearch L2 shared cache (feature-flagged) | ✅ Done | **this branch** |
| P2 | 3-tier CacheService (LRU → FAISS L1 → RediSearch L2) | ✅ Done | **this branch** |
| P3 | ThreadPoolExecutor batch ingestion | ✅ Done | **this branch** |
| P3 | Ingestion progress tracking (`update_progress`) | ✅ Done | **this branch** |
| Enh | RLM `on_progress` + SSE streaming via `ctx.report_progress` | ✅ Done | **this branch** |
| Enh | APScheduler background jobs (health + cache stats) | ✅ Done | **this branch** |
| Enh | `faiss-cpu` + `apscheduler` dependencies | ✅ Done | **this branch** |
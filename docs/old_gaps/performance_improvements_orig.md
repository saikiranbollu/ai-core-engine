
## Performance Bottlenecks — Analysis & Fix Status (Sprint 9)

### 1. ~~CRITICAL~~ ✅ FIXED — Graph Search N+1 Query Problem
**File:** `src/HybridRAG/code/querier/search_service.py`

**Problem**: `_graph_search()` ran a nested loop generating 50–200 individual Neo4j queries per search, each doing `toLower(CONTAINS)` across 13 string properties.

**Fix (Sprint 9)**: Consolidated into a single Cypher query using `any(lbl IN labels(n) WHERE lbl IN $labels)` with `UNWIND` for keywords. Added fulltext index fallback via `db.index.fulltext.queryNodes('aice_search_idx', ...)`. Added `ensure_fulltext_index()` to `Neo4jConnection` in `neo4j_manager.py` creating Lucene-backed index across 13 properties.

### 2. ~~HIGH~~ ✅ FIXED — Synchronous Backend on Async Server
**File:** `mcp/core/mcp_server.py`

**Problem**: All backend I/O called synchronously from `async` MCP tool handlers, blocking the uvicorn event loop.

**Fix (Sprint 9)**: Wrapped 26 sync tool handlers with `await asyncio.to_thread()`. Added `async def _warm_backends()` that parallel-initializes Neo4j (illd + mcal), Qdrant, Redis, PostgreSQL, CacheService via `asyncio.gather()` at startup.

### 3. HIGH — Sequential Hybrid Search Stages
Graph search (Neo4j) and vector search (Qdrant) run **sequentially** in `hybrid_search()`, but they're independent.

**Status**: Partially addressed — `hybrid_search_async()` already existed. Full `asyncio.gather()` parallelization deferred.

### 4. ~~MEDIUM-HIGH~~ ✅ FIXED — Sequential Ingestion
**File:** `src/IngestionPipeline/ingestion_service.py`

**Problem**: `batch_ingest()` processed modules in a serial for-loop.

**Fix (Sprint 9)**: Replaced with `ThreadPoolExecutor(max_workers=4)` + `as_completed()` pattern. Added `update_progress()` to `IngestionJobTracker` for per-module progress tracking.

### 5. ~~MEDIUM~~ ✅ FIXED — SemanticCache O(n) Linear Scan
**File:** `src/Configuration/cache_service.py`

**Problem**: `SemanticCache.get()` computed 500 pure-Python dot products per lookup.

**Fix (Sprint 9)**: Replaced `_entries` list with `faiss.IndexFlatIP(384)` for SIMD-optimized sub-ms lookups. Added `RediSearchSemanticCache` as L2 shared cache (feature-flagged via `AICE_CACHE_L2_REDIS`). `CacheService` upgraded from 2-tier to 3-tier: LRU → FAISS L1 → RediSearch L2 → RAG.

### 6. MEDIUM — httpx Client Recreation Per Request
`rlm_orchestrator.py` and `pdf_pipeline.py` create a **new httpx client** for every LLM call.

**Status**: Not yet fixed. Shared client singleton recommended.

### 7. LOW — Qdrant Using HTTP Instead of gRPC
Config sets `grpc: false`. gRPC is significantly faster for vector operations.

**Status**: Not yet fixed. Set `grpc: true` in `storage_config.yaml`.

---

## Additional Sprint 9 Improvements

### RLM Progress Reporting
**File:** `src/HybridRAG/code/querier/rlm_orchestrator.py`

Added `on_progress` callback to `RLMOrchestrator.run()` — invoked after planning, each sub-query, and synthesis. The `rlm_orchestrate` tool handler logs step-by-step progress.

### APScheduler Background Jobs
**File:** `mcp/app.py`

Added `BackgroundScheduler` with health check (5 min) and cache stats (30 min) periodic jobs. Graceful shutdown on SIGTERM. Silently disabled if `apscheduler` not installed.

---

## Fix Status Summary

| Priority | Fix | Status | Sprint |
|----------|-----|--------|--------|
| P0 | Consolidated graph search + fulltext index | ✅ Done | 9 |
| P0 | Async-wrap 26 tool handlers + parallel startup | ✅ Done | 9 |
| P1 | Parallel hybrid search (`asyncio.gather`) | ⏳ Partial | — |
| P1 | Reuse httpx client pool | ❌ Open | — |
| P2 | Qdrant gRPC transport | ❌ Open | — |
| P2 | FAISS L1 + RediSearch L2 cache | ✅ Done | 9 |
| P3 | ThreadPoolExecutor batch ingestion | ✅ Done | 9 |
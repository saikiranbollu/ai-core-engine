

# Plan: Adapt DocJockey Async/Performance Patterns to AICE

## TL;DR
Adapt 8 proven DocJockey patterns (parallel init, batch ingestion, UNWIND graph queries, semantic cache indexing, streaming responses, async DB pooling, scheduled jobs, batch embedding) into AICE to fix 4 confirmed bottlenecks (N+1 graph queries, sequential ingestion, O(n) cache scan, sync I/O blocking event loop).

> **Implementation Status** (branch `feature/perf_enhancements_cache_async_batch` vs `main`):
> ✅ = implemented in this branch | 🟡 = pre-existing in main | ❌ = not yet implemented

---

## Phase 1: Fix Critical Sync I/O Blocking (P0 — Foundation) ✅ IMPLEMENTED

### Step 1.1: Wrap MCP tool handlers with `asyncio.to_thread()` ✅
- **Problem**: MCP server tools call sync search directly, blocking the event loop
- **Template**: DocJockey's `async_init_*` pattern (docjockey-backend `src/pipeline/rag_pipeline.py` L112-132)
- **File**: `mcp/core/mcp_server.py` — all tool handler functions
- **Change**: Each tool that calls a sync backend (Neo4j, Qdrant) should wrap the call in `await asyncio.to_thread(sync_fn, *args)`. The `hybrid_search_async()` in `search_service.py` L250-303 already does this correctly for search — apply the same pattern at every tool entry point.
- **Status**: 🟡 Main had 29 `to_thread` calls. ✅ Branch extends to 63 (all remaining tool handlers wrapped).
- **Verification**: Run existing unit tests `tests/unit/test_sprint*.py`; add a simple async integration test that calls two tools concurrently to confirm no event-loop blocking.

### Step 1.2: Parallel service initialization at startup ✅
- **Problem**: AICE lazy-initializes Neo4j, Qdrant, Redis, Cerbos sequentially on first call
- **Template**: DocJockey `rag_pipeline.py` L112-132 — `asyncio.create_task()` + `asyncio.gather()` for 4 services
- **File**: `mcp/app.py` and `mcp/core/mcp_server.py` (lazy-init functions `_get_neo4j()`, `_get_qdrant()` at L200-265)
- **Change**: Add an `async def _warm_backends()` function that creates tasks for Neo4j driver, Qdrant client, Redis, and embedding model initialization — run them with `asyncio.gather()` during server startup in `app.py`. Keep lazy-init as fallback for graceful degradation.
- **Status**: ✅ `_warm_backends()` added to `mcp_server.py` — parallel-inits Neo4j (illd + mcal), Qdrant, Redis, PostgreSQL, CacheService with per-service error handling.
- **Verification**: Measure first-tool latency before/after; confirm backends are ready at startup via `health_check` tool.

---

## Phase 2: Fix Graph Search N+1 Queries (P0 — Biggest Latency Win) ✅ IMPLEMENTED

### Step 2.1: Consolidate per-label Cypher into single UNWIND query ✅
- **Problem**: `_graph_search()` in `search_service.py` L395-430 runs one Cypher query per label (5-20 labels) each scanning 13 properties with `toLower(CONTAINS)` — 50-200 queries per search
- **Template**: code-retrieval's `MemgraphIngestor._execute_batch()` uses `UNWIND $batch AS row` pattern (graph_service.py)
- **File**: `src/HybridRAG/code/querier/search_service.py` L395-430
- **Change**:
  1. Replace the `for label in labels` loop with a single Cypher using `UNWIND $labels AS lbl` and dynamic label matching via `any(l IN labels(n) WHERE l IN $labels)`
  2. Consolidate 13 `toLower(CONTAINS)` into a single full-text index query: `CALL db.index.fulltext.queryNodes('search_index', $query) YIELD node, score`
  3. If full-text index not feasible (Neo4j Community limitations), at minimum batch all labels into one query with `OR` over labels
- **Status**: 🟡 UNWIND consolidation with `any(lbl IN labels(n) WHERE lbl IN $labels)` pre-existing in main. ✅ Branch adds fulltext index fallback via `db.index.fulltext.queryNodes('aice_search_idx', ...)` with silent skip if index unavailable.
- **Prerequisite**: Create Neo4j full-text index on the 13 searchable properties (one-time admin operation)
- **Verification**: Measure query count per search before/after (should drop from ~50-200 to 1-3); run `tests/unit/hybrid_rag/` tests.

### Step 2.2: Add full-text index creation to ingestion pipeline ✅
- **File**: `src/IngestionPipeline/ingestion_service.py` or `src/HybridRAG/code/neo4j_manager.py`
- **Change**: Add `CREATE FULLTEXT INDEX search_index IF NOT EXISTS FOR (n:Function|Parameter|Register|...) ON EACH [n.name, n.function_name, n.description, n.api_name, n.type_name, n.module_name]` to a setup/migration function
- **Status**: ✅ `ensure_fulltext_index()` added to `Neo4jConnection` in `neo4j_manager.py` — creates `aice_search_idx` across all discovered node labels.
- **Verification**: Run index creation on test Neo4j instance; confirm `CALL db.index.fulltext.queryNodes()` returns results

---

## Phase 3: Async Batch Ingestion Pipeline (P3 → upgraded to P1 via DocJockey patterns) — Partially Implemented

### Step 3.1: Add ThreadPoolExecutor to `batch_ingest()` ✅
- **Problem**: `batch_ingest()` at L240-268 processes modules sequentially in a for-loop
- **Template**: DocJockey pipeline's batch processing with tqdm progress (data_pipeline `src/pipeline/stage2_pipeline.py` L798-899)
- **File**: `src/IngestionPipeline/ingestion_service.py` L240-268
- **Change**: Replace sequential `for mod in modules` with `concurrent.futures.ThreadPoolExecutor(max_workers=4)` + `executor.map()` or `executor.submit()`. Add tqdm progress bar around the loop. Update `JobTracker` progress percentage as modules complete.
- **Status**: ✅ Implemented with `ThreadPoolExecutor` + `as_completed()`. Added `update_progress()` to `IngestionJobTracker`.
- **Depends on**: Nothing (independent)
- **Verification**: Run `tests/integration/test_sprint*.py` ingestion tests; benchmark 10-module ingestion time before/after.

### Step 3.2: Batch Qdrant upserts during ingestion ✅
- **Problem**: Ingestion inserts vectors one-at-a-time into Qdrant
- **Template**: DocJockey's `build_quadrant_index()` in `src/embedding_generator/push_embedding.py` L59-118 — batch with `models.Batch()`, disable indexing during upload, retry failed batches
- **File**: `src/HybridRAG/code/RAG/vector_store_factory.py` — `_QdrantCollectionAdapter.upsert()` + `_batch_upsert()`
- **Change**: `upsert()` now accepts `batch_size` (default 512). When point count exceeds batch_size, `_batch_upsert()` disables Qdrant indexing (`indexing_threshold=0`), upserts in batches, then re-enables indexing (`indexing_threshold=20000`).
- **Status**: ✅ Implemented in `vector_store_factory.py`. All callers (`illd_rag_ingestion.py`, `rag_ingestion.py`) benefit automatically via the adapter.
- **Verification**: Ingest a test module; compare Qdrant document count before/after; benchmark upload time.

### Step 3.3: Batch Neo4j writes during ingestion with UNWIND ✅
- **Template**: code-retrieval's `MemgraphIngestor.flush_nodes()` — buffer nodes by label, batch `UNWIND $batch AS row MERGE`
- **File**: `src/IngestionPipeline/ingestion_service.py` — `Neo4jBatchWriter` class + `_write_to_kg()` refactored
- **Change**: Added `Neo4jBatchWriter` class (context manager) that buffers nodes by label and relationships by type, auto-flushes at threshold (default 500). Uses `UNWIND $rows AS item MERGE` pattern for both nodes and relationships. `_write_to_kg()` now uses batch writer instead of per-node `_merge_scoped_node()` calls.
- **Status**: ✅ Implemented. All c_header, json, pdf, xlsx, arxml, puml ingestion paths use batch writes.
- **Verification**: Ingest test module; verify node count matches; benchmark ingestion time.

---

## Phase 4: Hybrid Semantic Cache — FAISS L1 + RediSearch L2 (P2) ✅ IMPLEMENTED

**Context**: With 50+ modules × 2 workspaces, semantic cache scales to ~25K-50K entries (384-dim, ~73MB). Current pure-Python linear scan is O(n) — unusable at scale. Hybrid approach gives sub-millisecond in-process lookups + shared persistence across pods.

### Step 4.1: Add FAISS as in-process L1 semantic cache ✅
- **Problem**: `SemanticCache.get()` at `cache_service.py` L90-110 does O(n) dot products per lookup (pure Python `_cosine()`, not even NumPy)
- **File**: `src/Configuration/cache_service.py` L90-110
- **Change**:
  1. Add `faiss-cpu` to `requirements.txt`
  2. Replace `self._entries` list with `faiss.IndexFlatIP(384)` (inner product = cosine on normalized vectors, since `SentenceTransformer` already normalizes)
  3. Add `self._id_to_meta: Dict[int, Dict]` parallel dict mapping FAISS index position → `{query, value, metadata, ts}`
  4. On `put()`: `self._index.add(np.array([embedding], dtype='float32'))` + store metadata at position `self._index.ntotal - 1`
  5. On `get()`: `distances, indices = self._index.search(np.array([q_emb], dtype='float32'), k=1)` → check `distances[0][0] >= threshold` → return metadata
  6. On `invalidate_by_module()`: Rebuild index excluding invalidated entries (FAISS doesn't support deletion; use `IndexIDMap` wrapper if rebuild cost matters)
  7. On `clear()`: `self._index.reset()` + clear metadata dict
  8. Keep `self._lock` for thread safety
- **Performance**: <0.1ms for 50K vectors (SIMD-optimized brute-force). Only need `IndexIVFFlat` beyond ~1M vectors.
- **Limitation**: Cache lost on process restart — acceptable for single-instance; addressed by L2 in Step 4.2.

### Step 4.2: Add RediSearch VSS as shared L2 semantic cache *(depends on 4.1, targets multi-pod K8s)* ✅
- **Problem**: In K8s with multiple MCP pods, each pod has its own FAISS L1 = cold caches per pod, no sharing
- **Prerequisite**: Redis server must have the RediSearch module loaded (`redis-stack` Docker image or `LOADMODULE /path/to/redisearch.so`)
- **File**: `src/Configuration/cache_service.py` — add `RediSearchSemanticCache` class
- **Change**:
  1. Create a Redis HNSW vector index: `FT.CREATE cache_idx ON HASH PREFIX 1 "aice:semcache:" SCHEMA embedding VECTOR HNSW 6 TYPE FLOAT32 DIM 384 DISTANCE_METRIC IP query TEXT metadata TAG value TEXT`
  2. On `put()` (after FAISS L1 put): `HSET aice:semcache:{id} embedding {bytes} query {q} metadata {json} value {json}` with TTL via `EXPIRE`
  3. On `get()` (only on FAISS L1 miss): `FT.SEARCH cache_idx "*=>[KNN 1 @embedding $vec AS score]" PARAMS 2 vec {bytes} RETURN 4 query value metadata score` → check score ≥ threshold
  4. On L2 hit: backfill into FAISS L1 for future in-process hits (cache warming)
  5. On `invalidate_by_module()`: `FT.SEARCH` + `DEL` matching entries by metadata tag
  6. On `clear()`: `FT.DROPINDEX cache_idx DD` + recreate
- **Performance**: ~1-5ms per L2 lookup (network hop), still 100x faster than full RAG search (~100-500ms)
- **Note**: Can be feature-flagged (`AICE_CACHE_L2_REDIS=true`) — disabled by default for single-instance deployments

### Step 4.3: Update `CacheService` to 3-tier lookup ✅
- **File**: `src/Configuration/cache_service.py` — `CacheService.get()`
- **Change**: Update flow to: LRU (exact match) → FAISS L1 (in-process semantic) → RediSearch L2 (shared semantic, if enabled) → RAG
- **Cache put**: Write to all tiers (LRU + FAISS L1 + RediSearch L2)
- **Metrics**: Add `CACHE_REQUESTS_TOTAL` labels for `faiss_l1` and `redis_l2` tiers in `mcp_server.py`
- **Status**: ✅ `CacheService` upgraded to 3-tier: LRU → FAISS L1 → RediSearch L2 → RAG. `RediSearchSemanticCache` class added, feature-flagged via `AICE_CACHE_L2_REDIS`. `faiss-cpu>=1.7.0` added to `requirements.txt`.

**Verification**:
- Existing cache tests in `tests/unit/test_sprint8_fixes.py` must pass
- New benchmark: 50K-entry lookup target <0.1ms (FAISS L1), <5ms (RediSearch L2)
- Multi-pod test: Start 2 MCP instances sharing Redis → confirm L2 hit on pod B for query cached by pod A

---

## Phase 5: Streaming Responses for MCP (Enhancement) ✅ IMPLEMENTED

### Step 5.1: Add SSE streaming to long-running tools ✅
- **Template**: DocJockey's `combined_responses()` async generator in `rag_pipeline.py` L183-410 — SSE format with `"data: "` prefix
- **File**: `mcp/core/mcp_server.py` — `rlm_orchestrate` tool handler
- **Change**: `rlm_orchestrate` now accepts `ctx: Context` (FastMCP SDK injection). The sync `on_progress` callback bridges to async `ctx.report_progress(step, total)` and `ctx.info(msg)` via `asyncio.run_coroutine_threadsafe()` from the worker thread. Progress notifications stream over SSE/streamable-HTTP transports in real time.
- **Status**: ✅ Implemented. `Context` imported from FastMCP SDK alongside `FastMCP` in the import shim. Best-effort delivery — notification failures don't break RLM execution.
- **Depends on**: Phase 1 (async tools must be working first)
- **Verification**: Manual test with MCP client over streamable-http transport; confirm intermediate progress notifications arrive before final response.

---

## Phase 6: Scheduled Background Jobs (Enhancement) ✅ IMPLEMENTED

### Step 6.1: Add APScheduler for periodic health checks and cache warming ✅
- **Template**: DocJockey's `main.py` L291-308 — `BackgroundScheduler` with `CronTrigger` and `IntervalTrigger`
- **File**: `mcp/app.py`
- **Change**:
  1. Add `apscheduler` to `requirements.txt`
  2. Create scheduler in `app.py` startup:
     - `IntervalTrigger(minutes=5)` → health_check (Neo4j, Qdrant, Redis, Cerbos)
     - `IntervalTrigger(minutes=30)` → cache stats logging
     - `CronTrigger(hour=2)` → cache warming for common queries (optional)
  3. Shutdown scheduler in SIGTERM handler (already exists in `app.py`)
- **Status**: ✅ `BackgroundScheduler` with health check (5 min) and cache stats (30 min). Graceful shutdown via SIGTERM/SIGINT. Silently disabled if `apscheduler` not installed. `apscheduler>=3.10.0` added to `requirements.txt`.
- **Parallel with**: Any phase
- **Verification**: Check logs for periodic health check output; confirm scheduler shutdown on SIGTERM.

### Additional: RLM Progress Reporting ✅
- **File**: `src/HybridRAG/code/querier/rlm_orchestrator.py`
- **Status**: ✅ Added `on_progress` callback to `RLMOrchestrator.run()` — invoked after planning, each sub-query, and synthesis. The `rlm_orchestrate` tool handler logs step-by-step progress. (Prerequisite for Phase 5 streaming.)

---

## Relevant Files

| File | Phase | Change | Status |
|------|-------|--------|--------|
| `mcp/core/mcp_server.py` | 1 | Async tool wrappers (29→63), `_warm_backends()` | ✅ |
| `mcp/app.py` | 1, 6 | Startup warm-up, APScheduler | ✅ |
| `src/HybridRAG/code/querier/search_service.py` | 2 | UNWIND + fulltext fallback | ✅ (UNWIND 🟡, fulltext ✅) |
| `src/HybridRAG/code/neo4j_manager.py` | 2 | `ensure_fulltext_index()` | ✅ |
| `src/IngestionPipeline/ingestion_service.py` | 3 | ThreadPool + progress | ✅ (batch writes ❌) |
| `src/Configuration/cache_service.py` | 4 | FAISS L1 + RediSearch L2 + 3-tier | ✅ |
| `src/HybridRAG/code/querier/rlm_orchestrator.py` | Enh | `on_progress` callback | ✅ |
| `requirements.txt` | 3, 4, 6 | `faiss-cpu`, `apscheduler` | ✅ |

### Reference templates (DocJockey)
- `docjockey-backend/src/pipeline/rag_pipeline.py` L112-132 — Parallel init pattern
- `docjockey-backend/src/retriever/hybrid_search.py` L125-179 — Parallel search
- `docjockey-backend/src/pipeline/rag_pipeline.py` L183-410 — Streaming SSE
- `docjockey-backend/main.py` L291-308 — APScheduler
- `docjockey_data_pipeline-2/src/embedding_generator/push_embedding.py` L59-118 — Batch Qdrant upsert
- `docjockey_data_pipeline-2/src/embedding_generator/generate_embeddings.py` L32-65 — Batch embeddings with tqdm
- `code-retrieval/codebase_rag/services/graph_service.py` — UNWIND batch pattern

## Verification

1. **Unit tests**: Run `pytest tests/unit/` after each phase — all existing tests must pass
2. **Integration tests**: Run `pytest tests/integration/` after Phases 2-3
3. **Benchmarks** (new):
   - Phase 1: First-tool latency (target: <500ms vs current cold-start)
   - Phase 2: Query count per search (target: 1-3 vs current 50-200)
   - Phase 3: 10-module ingestion time (target: 4x speedup with 4 workers)
   - Phase 4: Cache lookup — FAISS L1 <0.1ms at 50K entries, RediSearch L2 <5ms, vs current ~5-10ms at 500 entries
4. **Health check**: `health_check(verbose=true)` after deployment
5. **Load test**: 10 concurrent MCP tool calls — confirm no event-loop blocking (Phase 1 validation)
6. **Multi-pod cache test** (Phase 4): 2 MCP instances sharing Redis → confirm L2 hit on pod B for query cached by pod A

## Decisions

- **Hybrid FAISS + RediSearch for semantic cache** — FAISS as L1 (in-process, <0.1ms) for hot path; RediSearch as L2 (shared, ~2ms) for multi-pod persistence. At 50K entries / 384 dims, FAISS `IndexFlatIP` is trivial (<0.1ms SIMD brute-force). RediSearch L2 is feature-flagged, disabled for single-instance. Only need ANN indices beyond ~1M vectors.
- **ThreadPoolExecutor over asyncio for ingestion** — Ingestion is CPU-bound (parsing) + I/O-bound (Neo4j/Qdrant writes); ThreadPool handles both; asyncio alone won't help with CPU work
- **Full-text index over dynamic Cypher** — Neo4j full-text indexes are Lucene-backed, orders of magnitude faster than `toLower(CONTAINS)` pattern matching
- **Phases are ordered by impact**: P0 fixes first (sync I/O, N+1 queries), then P1 (batch ingestion), P2 (cache), then enhancements (streaming, scheduler)

## Scope

- **Included**: Async patterns, batch processing, hybrid cache optimization (FAISS L1 + RediSearch L2), streaming, scheduling
- **Excluded**: DocJockey's aiomysql patterns (AICE uses PostgreSQL not MySQL), permission sync cron jobs (AICE uses Cerbos RBAC not source-level permissions), reranking service (AICE deferred cross-encoder per ADR-018)

-----------------------------

Usage of BM25? BM25 is a powerful text retrieval algorithm that could potentially improve keyword search performance in AICE. However, given that AICE already has a Neo4j full-text index using Lucene, which provides exact and fuzzy matching across 13 properties, the incremental benefit of adding a separate BM25-based search engine (like Elasticsearch) may not justify the operational complexity it introduces. BM25 excels in unstructured text retrieval, but AICE's data is mostly structured (function signatures, register names, requirement IDs) where exact match and graph traversal are more effective. Instead of adding BM25, AICE can focus on optimizing the existing Neo4j full-text search (e.g., adding a camelCase tokenizer for API names) and improving the fusion of existing signals (semantic vectors + graph traversal + keyword search) for better performance and relevance.

---

## Analysis 1: BM25 + Qdrant + Neo4j vs. Current Qdrant + Neo4j

### What AICE Currently Has (3 retrieval signals)

| Signal | Engine | What It Does |
|--------|--------|-------------|
| **Semantic vector** | Qdrant (all-MiniLM-L6-v2, 384d) | Cosine similarity against embedded chunks |
| **Graph traversal** | Neo4j Cypher | Typed relationship hops (CALLS, IMPLEMENTS, TRACES_TO…) |
| **Keyword/full-text** | Neo4j Lucene index (`aice_search_idx`) | Exact & fuzzy match on 13 property fields |

You already have a **text search** signal — it's Lucene inside Neo4j, not Elasticsearch BM25, but it covers the same role. The fusion uses two strategies: RRF for inter-collection fusion in `rag_query_unified.py` (`1/(k+rank)` with k=60) and alpha-blending for graph-vs-vector fusion in `hybrid_rag_unified.py` — a deliberate two-stage design.

### What Adding Standalone BM25 (e.g., Elasticsearch) Would Change

| Dimension | Current (Neo4j Lucene) | + Elasticsearch BM25 |
|-----------|----------------------|---------------------|
| **Scoring model** | Lucene TF-IDF (Neo4j's default) | BM25 with configurable k1/b saturation tuning |
| **Field boosting** | Single flat index across 13 fields | Per-field boost (phrase=20x, prefix=15x, AND=10x — DocJockey uses this) |
| **Query DSL** | `db.index.fulltext.queryNodes(...)` — limited | Full bool/should/must/phrase/fuzzy/wildcard |
| **Tokenization** | Neo4j's default analyzer | Custom analyzers (edge-ngram, stemming, synonyms) |
| **Scalability** | Shares Neo4j memory/compute | Independent horizontal shard scaling |
| **Operational cost** | Zero extra — already running Neo4j | New service to deploy, monitor, maintain |
| **Ingestion** | Same Neo4j write path | Dual-write: Neo4j + ES (DocJockey does this via separate pipeline) |

### Verdict: **Stay with current Qdrant + Neo4j** (with improvements)

**Why NOT to add BM25/Elasticsearch now:**

1. **You already have text search** — Neo4j full-text index on 13 properties with Lucene. It's not as feature-rich as ES, but for your domain (AURIX API names, register names, requirement IDs) exact/prefix matching is the dominant text search pattern, which Lucene handles well.

2. **Operational complexity** — Adding Elasticsearch means a 6th service (Neo4j, Qdrant, Redis, PostgreSQL, Cerbos, + ES). Your deployment is already 5 Docker services + K8s. For a team supporting 21 Domain Assistants, operational overhead matters.

3. **Diminishing returns for structured engineering data** — BM25 shines on unstructured prose (natural language documents). Your data is mostly structured: function signatures (`IfxCxpi_initChannel`), register names (`CLC`), requirement IDs (`CXPI_REQ_001`). These are best served by exact match + graph traversal, which you already have.

4. **DocJockey needs ES because it lacks a graph** — DocJockey uses ES BM25 to compensate for having no knowledge graph for docs. AICE has Neo4j with typed relationships and NodeSet isolation — your graph IS your structural search.

5. **Performance is already good** — Sprint 9 consolidated graph queries from 50-200 to 1-3, and total hybrid search is ~150-250ms.

**What to do instead (higher ROI):**

| Improvement | Effort | Impact | Status |
|-------------|--------|--------|--------|
| Qdrant gRPC | Config change | 30-50% vector search speedup | 🟡 Already implemented (auto-detects in-cluster K8s; config `qdrant.grpc: true`) |
| Parallel graph + vector search | Small code change | ~50% latency reduction | 🟡 Already implemented (`asyncio.gather()` in `search_service.py`) |
| RRF for inter-collection vector fusion | Medium | Robust rank-based merging | 🟡 Already implemented (`rag_query_unified.py`, k=60) |
| Tune Neo4j full-text analyzer (camelCase tokenizer) | Small | Better text hits on `IfxCxpi_initChannel` | ❌ Not yet done |

---

## Analysis 2: Hybrid RAG Strategy Comparison

From your comparison table in Sample Comparison with DocJockey.md:

### Hybrid RAG: Neo4j graph + Qdrant vector (AICE) vs. Elasticsearch full-text + Qdrant vector (DocJockey)

| Criterion | **AICE: Neo4j + Qdrant + RRF** | **DocJockey: ES + Qdrant + Reranking** | Winner |
|-----------|------|------|------|
| **Structural queries** ("what calls IfxCxpi_init?") | Neo4j multi-hop Cypher traversal — direct relationship query | ES keyword match only — no relationship awareness | **AICE** |
| **Semantic queries** ("code similar to DMA setup") | Qdrant cosine similarity (identical capability) | Qdrant cosine similarity | **Tie** |
| **Keyword/exact queries** ("find CLC register") | Neo4j Lucene index (adequate, 13 fields) | ES BM25 with field boosting + phrase/prefix/wildcard (more sophisticated) | **DocJockey** |
| **Traceability** ("trace requirement CXPI_REQ_001 to test") | Native: `TRACES_TO`, `IMPLEMENTS`, `VERIFIES` relationships | Impossible without a graph | **AICE** |
| **Cross-entity reasoning** ("what functions use struct X?") | `USES_TYPE` relationship — single Cypher query | Requires embedding proximity (unreliable for structural questions) | **AICE** |
| **Natural language doc search** ("how to configure watchdog") | Weak — not your primary data type | Strong — ES BM25 excels on prose | **DocJockey** |
| **Score fusion** | Two-stage: RRF for inter-collection (rag_query_unified.py) + alpha-blending for graph-vs-vector (hybrid_rag_unified.py) | Cross-encoder reranking (deferred, using RRF too) | **Tie** |
| **Operational overhead** | Neo4j + Qdrant (2 search services) | ES + Qdrant (2 search services) | **Tie** |
| **Domain fit** | Structured engineering data with typed relationships | 330+ heterogeneous document sources | **Each fits its domain** |

### Knowledge Graph: Neo4j with NodeSet isolation (AICE) vs. Memgraph code-only (DocJockey)

| Criterion | **AICE: Neo4j** | **DocJockey: Memgraph** | Winner |
|-----------|------|------|------|
| **Scope** | All data types (Functions, Registers, Requirements, Tests, HWSpecs…) | Code structure only (files, classes, functions, imports) | **AICE** |
| **Data isolation** | NodeSet anchors per module per project — enforced subgraph boundaries | Single flat graph per codebase | **AICE** |
| **Relationship richness** | 12+ typed relationships (TRACES_TO, IMPLEMENTS, CALLS, USES_TYPE, HAS_FIELD…) | Basic code relationships (IMPORTS, CONTAINS, CALLS) | **AICE** |
| **Multi-hop reasoning** | Up to 5-hop traversals for V-Model traceability chains | 1-2 hop code navigation | **AICE** |
| **Batch performance** | Fixed N+1 → UNWIND pattern (1-3 queries) | UNWIND batch from day one (code-retrieval was newer, learned from AICE's mistake) | **DocJockey** |
| **Maturity** | Neo4j Community (established, rich ecosystem, APOC library) | Memgraph (faster for simple traversals, smaller footprint) | **Tie** (different strengths) |
| **Missing** | No graph for non-AURIX content | No graph for documents — only code | **Both have gaps** |

### Bottom Line

**For AICE's domain (automotive embedded engineering):** The current **Neo4j + Qdrant** approach is the **correct architecture**. Your data is inherently graph-structured (Requirements → Architecture → Code → Tests → HW Specs). A knowledge graph is not optional here — it's the core value proposition. ES BM25 would add marginal text search improvement at significant operational cost.

**For DocJockey's domain (enterprise docs from 330+ sources):** The **ES + Qdrant** approach is the **correct architecture**. With heterogeneous prose documents and no structural relationships between them, BM25's sophisticated text matching is more valuable than a graph.

**They are solving different retrieval problems — neither approach is universally better.**

The one cross-pollination worth considering: if AICE ever needs to search **unstructured documentation** (user manuals, design documents, meeting notes) alongside structured engineering data, adding BM25 for *just that content type* via a dedicated query route would be valuable. But for your core use case, the current stack is optimal.

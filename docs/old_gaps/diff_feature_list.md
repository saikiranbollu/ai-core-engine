
Here's the comprehensive comparison of **`further_enhancements_for_gaps`** vs **`main`**:

---

## Summary: 87 files changed, +12,907 / -2,562 lines across 25 commits

---

### 1. New Querier Modules (Advanced RAG Pipeline)

| Module | File | Purpose |
|--------|------|---------|
| **Reranker (GAP-A01)** | `src/HybridRAG/code/querier/reranker.py` | FlashRank ONNX-based cross-encoder reranking (replaces PyTorch) |
| **Query Enhancer (GAP-A03)** | `src/HybridRAG/code/querier/query_enhancer.py` | LLM-powered query expansion/decomposition with complexity analysis |
| **Context Compressor (GAP-A04)** | `src/HybridRAG/code/querier/context_compressor.py` | Microsoft LongLLMLingua-based context compression |
| **Batch Graph Resolver (GAP-A06)** | `src/HybridRAG/code/querier/batch_graph_resolver.py` | Eliminates N+1 Cypher queries (50-200 → 3-5 per search) |
| **Relevance Judge (GAP-A08)** | `src/HybridRAG/code/querier/relevance_judge.py` | LLM-as-Judge for faithfulness/relevance scoring (DeepEval) |
| **Context Refiner** | `src/HybridRAG/code/querier/context_refiner.py` | Post-retrieval context refinement and citation injection |

All of these are wired into the `search_service.py` pipeline (Stage 6/7/8).

---

### 2. Caching Overhaul (Sprint 9)

- **FAISS L1 + RediSearch L2 hybrid cache** in `src/Configuration/cache_service.py` — replaced O(n) linear scan with FAISS `IndexFlatIP` for sub-ms semantic lookups at 25K+ entries
- **Runtime cache config refresh** — hot-reload LRU/semantic cache params from env vars without restart
- **Configurable TTL, size limits, auto-invalidation** on ingestion

---

### 3. Shared Embedding Singleton

- `src/Configuration/embedding_singleton.py` — shared `SentenceTransformer` singleton with warmup, prevents duplicate model loads across workers

---

### 4. MCP Server Enhancements

- **Rate Limiting** (`mcp/core/rate_limiter.py`) — slowapi-based: 60/min search, 10/min admin tools
- **Streaming Support** (`mcp/core/streaming.py`) — `MCPStreamNotifier` for progress callbacks during search_database
- **APScheduler Integration** in `mcp/app.py` — periodic health checks, cache stats logging, stale session cleanup
- **Profile-based ontology filtering** — enhanced `mcp/core/mcp_server.py` (+716 lines) with ontology service handling, tool registration fixes, Cypher injection protection (label allowlist), Neo4j `READ_ACCESS` mode
- **Gunicorn multi-worker deployment** (`WEB_CONCURRENCY=4`)
- **4 missing GAP v2 tools registered** in `tool_tiers.py`

---

### 5. Observability

- **OpenTelemetry Tracing** (`src/Observability/otel_tracing.py`) — `@trace_tool` decorator on top 5 MCP tools, exports to Grafana Tempo via OTLP
- **Postgres FK fix** — `response_archive` row created before `review_evidence` (FK ordering)
- **Prometheus timing context vars** wired into `_authorize()`

---

### 6. Few-Shot Learning Library

- `src/MemoryLayer/memory/few_shot_library.py` — Qdrant-backed few-shot example retrieval (2-3 similar Q&A pairs injected into DA prompts from APPROVED feedback)

---

### 7. Ingestion Pipeline Improvements

- **Async Batch Ingestion** (`src/IngestionPipeline/batch_ingestion.py`) — `asyncio.TaskGroup` + `ProcessPoolExecutor` replacing Celery
- **OCR Processor** (`src/IngestionPipeline/Parsers/ocr_processor.py`) — new parser for scanned docs
- **iLLD SWA Parser** enhanced with LLM enrichment & parallel processing
- **SWA/SWUD parsers** robustness improvements (table variations, broader pattern matching)
- **PDF pipeline** fixes (190+ lines changed)
- **`regdef_parser`** → renamed/replaced by `sfr_parser`
- **`ingestion_service.py`** heavily refactored (+610 lines) with batch processing and shared HTTP client

---

### 8. Infrastructure / DevOps

- **`docker-compose.yml`** (new, 275 lines) — full stack: Neo4j, Qdrant, Redis, PostgreSQL, MCP Server, Prometheus, Grafana
- **Dockerfile** — pre-downloads `all-MiniLM-L6-v2` model, writable HF cache, Gunicorn support
- **Dependencies overhaul** — removed PyTorch/sentence-transformers (~2GB), added FlashRank (ONNX, ~50MB), llmlingua, deepeval, ragchecker, slowapi, opentelemetry-*, apscheduler, faiss-cpu, gunicorn

---

### 9. Bug Fixes (30+ fixes)

**Critical (C01-C11):** missing tool registrations, startup crash path bootstrapping, Prometheus wiring, rate limiting, multi-worker deployment, CBMC JSON flag, ApprovedPattern field errors, missing `import re`

**High (H01-H18):** Qdrant health check import, async query enhancer wrapper, FK ordering, fallback embedder 384-dim fix, threading locks on singletons, Cypher injection protection

**Medium (M07-M11):** Neo4j `id()` → `elementId()` migration, RLM client thread safety, token estimation standardization, MISRA rule count fix

---

### 10. Documentation & Architecture Decisions

- **8 ADRs** (036-043): OTel for MCP only, asyncio.TaskGroup over Celery, FlashRank over PyTorch, credential externalization, rate limiting, CBMC/FMEA deferred, MISRA/GEST scope, Gunicorn multi-worker
- `CHANGELOG.md`, `PIPELINE.md`, `feature_list.md`, updated `DECISIONS.md`, `OVERVIEW.md`, `DOCUMENTATION.md`

---

### 11. Tests

- **6 new test files** (1,224+ lines):
  - `test_gap_implementations.py` — 73 integration tests for all GAP features
  - `test_batch_ingestion_async.py` — async batch ingestion
  - `test_context_compressor.py`, `test_context_refiner.py` — compressor/refiner unit tests
  - `test_otel_tracing.py` — OTel decorator tests
  - `test_pipeline_wiring.py` — end-to-end pipeline wiring validation
  - `test_rate_limiter.py` — rate limiter unit tests
- Existing sprint tests (3, 5, 6) significantly refactored/modernized

---

### Deferred Items (by design)

- **CacheService wiring (M06)** — requires architectural decision
- **TLS inter-service (H15)** — infrastructure team responsibility
- **RelevanceJudge LLM wiring (H07)** — needs deeper refactor
- **Threshold scale normalization (H08)** — needs DeepEval version check
- **FMEA + CBMC domain tools** — stubs only (ADR-041)
# Changelog

All notable changes to the AI Core Engine are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Sprint 25.1] - 2026-04-07

### Critical Bug Fixes
- C01: Registered 4 missing GAP v2 tools in tool_tiers.py
- C02: Fixed startup crash — path bootstrapping before GAP imports
- C03: Wired Prometheus timing context vars into _authorize()
- C05: Added rate limiting via slowapi (60/min search, 10/min admin)
- C06: Multi-worker deployment via Gunicorn (WEB_CONCURRENCY=4)
- C07: Fixed CBMC subprocess JSON flag blocking text parsing
- C08/C09: Fixed ApprovedPattern field and PatternIndex method errors
- C10: Added missing `import re` in context_refiner.py
- C11: Changed rlm_orchestrate from PUBLIC to DEVELOPER tier

### High Bug Fixes
- H01: Fixed Qdrant health check import (_get_qdrant_client → _get_qdrant)
- H02: Updated module docstring to "60+ Tools across 14 categories"
- H08: Wrapped query enhancer in asyncio.to_thread for async path
- H09: Fixed postgres FK ordering (response_archive before review_evidence)
- H10: Created OpenTelemetry tracing module (otel_tracing.py)
- H14: Added Neo4j READ_ACCESS mode for search queries
- H16: Added threading.Lock for all singleton initializers
- H17: Added Cypher injection protection via label allowlist
- H18: Fixed fallback embedder to produce full 384-dim vectors

### Medium Bug Fixes
- M07: Migrated Neo4j id() → elementId() in knowledge_intelligence.py
- M08: Added threading.Lock to RLM client state
- M09: Standardized token estimation to len(text)//4 across codebase
- M10: Fixed MISRA total_rules from 10 → 175
- M11: Removed dead must_conditions block in few_shot_library.py

### Pipeline Wiring (Phase 2)
- Wired ContextCompressor (Stage 6), RelevanceJudge (Stage 7), ContextRefiner (Stage 8) into search pipeline
- Wired set_llm_fn() to connect GPT4IFX to compressor/judge/refiner
- Wired FewShotLibrary into rlm_orchestrate with Qdrant-backed retrieval
- Wired MCPStreamNotifier progress callbacks into search_database
- Added FMEA + CBMC tool stubs (deferred, ADR-041)
- Wired OpenTelemetry trace_tool decorator on top 5 MCP tools

### New Capabilities
- Rate limiter module (mcp/core/rate_limiter.py)
- OpenTelemetry tracing (src/Observability/otel_tracing.py)
- Async batch ingestion via asyncio.TaskGroup + ProcessPoolExecutor
- Fixed streaming deprecated asyncio calls

### Architecture Decision Records
- ADR-036 (revised): OpenTelemetry adopted for MCP layer only
- ADR-037: asyncio.TaskGroup replaces Celery
- ADR-038: FlashRank replaces PyTorch
- ADR-039: Credential externalization (.env pattern)
- ADR-040: Rate limiting via slowapi
- ADR-041: Domain Assistants CBMC/FMEA deferred
- ADR-042: MISRA and GEST fix auth and bugs only
- ADR-043: Multi-worker deployment via Gunicorn

### Dependencies
- Removed: PyTorch, sentence-transformers, --extra-index-url
- Added: flashrank, llmlingua, deepeval, ragchecker, gunicorn, slowapi, limits, opentelemetry-*

### Dead Code Removed
- mcp/core/tool_registration_patch.py
- src/HybridRAG/code/querier/search_service_patch.py

# AI Core Engine (AICE) — Master List of Gaps and Missing Features

**Document Owner:** Bollu Sai Kiran — ATV MC D SW VDF, Infineon Technologies
**Repository:** `ai-core-engine3`
**Baseline:** AICE v2.1.0 — Sprint 10 → Sprint 25 (Review4 cycle complete)
**Last Updated:** 2026-04-25
**Classification:** Internal — Infineon Technologies

---

## 0. Document Purpose & Scope

This document is the **single, authoritative master list** of every gap, missing feature, defect, and architectural debt item identified in the AI Core Engine across its full lifecycle — from the initial DocJockey comparative gap analysis through to the most recent Review4 deep cross-verification cycle. Its purpose is threefold:

1. **Provide complete provenance** for every architectural decision (every gap maps to one or more ADRs).
2. **Track final disposition** — Fixed, Deferred, Open — for each gap, with the sprint and evidence.
3. **Serve as audit input** for ASPICE assessments, EU AI Act technical documentation (Art. 11), and ISO 26262 Part 8 tool qualification.

**Scope (in-scope):**
- DocJockey-comparison gaps **GAP-A01 → GAP-A15** (15 capability gaps, the original "DocJockey gap analysis").
- **Performance bottlenecks** (P0–P3, Sprint 9 baseline) and their fix history.
- **AI Governance gaps GAP-01 → GAP-11** (Sprints 11–14 governance package).
- **Review cycles 1–4** with their issue codes (`C##`, `H##`, `M##`, `L##`, `F##`, `N-H##`).
- **ADRs 001–043** as the decision trail.

**Scope (out-of-scope, by user request):**
- CIA-specific (Code Implementation Assistant) review findings — tracked separately.
- Domain Assistant (DA) implementation gaps — tracked per DA.

**Reading guide:**
- Each gap entry has: **Family-ID**, **Title**, **Problem**, **Decision/Approach**, **ADR(s)**, **Sprint**, **Status**, **Files Touched**, **Verification Evidence**.
- **Status legend:**
  - ✅ **FIXED** — implemented and verified in source.
  - 🟡 **PARTIAL** — partially implemented; remaining work documented.
  - 🔄 **DEFERRED** — explicit ADR-backed deferral; re-evaluation trigger documented.
  - ❌ **REJECTED** — explicit ADR-backed rejection (no further re-evaluation planned).
  - 🆕 **OPEN** — known and tracked but not yet scheduled.

---

## 1. Executive Summary

### 1.1 Gap Inventory at a Glance

| Family | Total | Fixed | Deferred / Rejected | Open / Planned |
|--------|------:|------:|--------------------:|---------------:|
| **A. DocJockey GAP-A series** (capability) | 15 | 13 | 2 (A10 rejected, A12 deferred) | 0 |
| **B. Performance bottlenecks** | 7 | 6 | 0 | 1 (gRPC for Qdrant) |
| **C. AI Governance gaps** | 11 | 8 | 0 | 3 (GAP-09 cadence, GAP-10 dashboard, GAP-11 golden set) |
| **D. Review1 findings** (initial sweep) | ~12 | 12 | 0 | 0 |
| **E. Review2 findings** (F-codes, infra) | 6 | 6 | 0 | 0 |
| **F. Review3 findings** (C/H/M/L deep) | ~25 | 21 | 2 (CBMC, FMEA) | 2 (Polarion, target compiler) |
| **G. Review4 findings** (final cross-verify) | 11 | 11 | 0 | 1 (CacheService wiring) |
| **TOTAL** | **~87** | **~77** | **4** | **~7** |

### 1.2 Tool-Count Evolution

| Sprint | Tools | Notes |
|-------:|------:|-------|
| Sprint 5 | 50 | Baseline (after RLM + Sandbox addition) |
| Sprint 9 | 52 | + 2 perf-related admin tools |
| Sprint 10 | 56 | + 4 governance/observability tools |
| Sprint 12 | 57 | + `governance_report` |
| Sprint 13 | 59 | + `assess_coverage_bias`, `get_provenance` |
| Sprint 14 | 60 | + `verify_citations` (GAP-A13) |
| Sprint 25 | **62** | + 2 GAP-pipeline admin tools (post-Review4) |

### 1.3 Architectural Decision Records (ADRs) Map

- ADR-001 → ADR-021: Foundational (Neo4j, Qdrant, Redis, PostgreSQL, Cerbos, Docker Compose, deferred Celery/MinIO/Cross-encoder/Keycloak, on-prem deployment, Prometheus+Grafana).
- ADR-022 → ADR-033: GAP-A series implementations (FlashRank, QueryEnhancer, Streaming, ContextCompressor, BatchGraphResolver, LLM-as-Judge, dynamic budget, ContextRefiner, BatchIngestion, CitationVerifier, FewShotLibrary, OCR).
- ADR-034 → ADR-038: GAP-A revisions and deferrals (multi-language rejected, Keycloak deferred, tracing revised, asyncio over Celery, FlashRank replaces PyTorch).
- ADR-039 → ADR-043: Review2/Review3 fixes (credentials, rate limiting, CBMC/FMEA deferred, MISRA/GEST auth, Gunicorn).

---

## 2. Family A — DocJockey GAP-A Series (15 Capability Gaps)

These 15 gaps were identified by the formal **DocJockey vs AICE comparative analysis** (see `docs/architecture/Sample Comparison with DocJockey.md` and `AICore Engine Implementation Research.pdf`). They form the largest single tranche of work and were sequenced across Sprints 11–17.

---

### GAP-A01 — Cross-Encoder Reranking After RRF Merge

| Field | Value |
|-------|-------|
| **Problem** | RRF (Reciprocal Rank Fusion) is formula-based; misses fine-grained semantic relevance. DocJockey achieves NDCG@10 ≈ 0.90 with cross-encoder vs ~0.72 baseline. |
| **Decision** | Add optional cross-encoder reranking after RRF merge, gated by QueryEnhancer's strategy classification. Skip for `graph_heavy` and `exact` strategies (~75% of AICE queries). |
| **ADR(s)** | **ADR-018** (initial deferral) → **ADR-022** (adopted, ms-marco-MiniLM-L-12-v2) → **ADR-038** (FlashRank replaces PyTorch). |
| **Sprint** | 12 (initial) / 25 (FlashRank revision) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/reranker.py`, `requirements.txt` (`flashrank>=0.2.0`). |
| **Evidence** | FlashRank ONNX-backend integrated; CrossEncoder retained as degraded fallback; PyTorch made non-mandatory; ~1.8 GB Docker image reduction; RERANKER_ENABLED, RERANKER_MODEL, RERANKER_TOP_K env vars. |

---

### GAP-A02 — Streaming Responses (SSE / Time-to-First-Token)

| Field | Value |
|-------|-------|
| **Problem** | AICE MCP responses are synchronous — DAs must wait for the complete tool response. DocJockey delivers ~380 ms TTFT via SSE streaming. |
| **Decision** | Add SSE streaming via `StreamingToolWrapper`. Long-running tools (`search_database`, `rlm_orchestrate`, `batch_ingest_*`) yield `StreamEvent` objects. DAs opt-in via `stream=true`. Backward-compatible with existing JSON-RPC clients. |
| **ADR(s)** | **ADR-024** |
| **Sprint** | 12 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/streaming.py` (`StreamingToolWrapper`). |
| **Evidence** | MCP protocol's streamable-http transport leveraged; TTFT and stream-completion-rate metrics added. |

---

### GAP-A03 — Query Enhancement Pipeline (Synonym / Complexity / Strategy)

| Field | Value |
|-------|-------|
| **Problem** | Raw user queries went directly to hybrid search — no preprocessing, no synonym expansion, no intent/complexity classification. |
| **Decision** | Add a deterministic, rule-based `QueryEnhancer` stage with: (a) AUTOSAR/MCAL domain synonym expansion, (b) complexity classifier (`simple`/`medium`/`complex`), (c) search-strategy predictor (`graph_heavy`/`vector_heavy`/`hybrid`/`exact`). Zero LLM dependency, sub-millisecond. |
| **ADR(s)** | **ADR-023** |
| **Sprint** | 11 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/query_enhancer.py`. |
| **Evidence** | Domain synonym dictionary covers ADC/CAN/SPI/etc.; `QueryComplexity` and `SearchStrategy` enums; fallback returns original query on failure. |

---

### GAP-A04 — Advanced Context Compression (LLMLingua + Extractive)

| Field | Value |
|-------|-------|
| **Problem** | Original ContextBuilder used a fixed 10-slot 8K token budget. DocJockey reports 4.2× compression with 88% retention via query-focused abstractive compression. |
| **Decision** | 3-stage pipeline: (a) extractive sentence selection (deterministic), (b) query-focused abstractive compression via GPT4IFX (optional), (c) dynamic budget enforcement. Target 3.5–4× compression with ≥85% retention. Falls back to extractive-only if LLM unavailable. |
| **ADR(s)** | **ADR-025** |
| **Sprint** | 13 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/context_compressor.py`, `requirements.txt` (`llmlingua>=0.2.0`). |
| **Evidence** | `set_llm_fn()` wires GPT4IFX; missing `import re` fixed (Review3 C10). |

---

### GAP-A05 — Batch Ingestion Pipeline (Parallel + Progress)

| Field | Value |
|-------|-------|
| **Problem** | `batch_ingest()` processed modules in a serial for-loop — documented P3 bottleneck. Bulk onboarding of new modules was slow. |
| **Decision** | Replace sequential ingestion with batched processing: embed (batch=64), Qdrant upsert (batch=100), Neo4j MERGE via UNWIND (batch=50). Optional Celery worker integration for multi-node. |
| **ADR(s)** | **ADR-030** (adopted) → **ADR-037** (Celery replaced by `asyncio.TaskGroup` + `ProcessPoolExecutor`). |
| **Sprint** | 9 (Sprint-9 ThreadPool retrofit) → 15 (full batch pipeline) → 25 (Celery removed). |
| **Status** | ✅ **FIXED** |
| **Files** | `src/IngestionPipeline/batch_ingestion.py`, `src/IngestionPipeline/ingestion_service.py`. |
| **Evidence** | `ThreadPoolExecutor(max_workers=4)` + `as_completed()` retrofitted Sprint 9; full `BatchIngestionPipeline` Sprint 15; `IngestionJobTracker.update_progress()` for per-module progress; 3–5× throughput improvement; **zero new dependencies after ADR-037** (no Celery, no celery-batches). |

---

### GAP-A06 — Batch Graph Queries (UNWIND, eliminate N+1)

| Field | Value |
|-------|-------|
| **Problem** | `SearchService` issued 50–200 individual Cypher queries per hybrid search for relationship enrichment (classic N+1). The **#1 documented latency bottleneck (P0)**. |
| **Decision** | Replace per-node relationship fetching with batch UNWIND queries. Collect all element IDs, execute 2–3 batch queries (nodes + relationships) instead of 50–200. Preserve NodeSet isolation via `module` filter in `WHERE`. |
| **ADR(s)** | **ADR-026** |
| **Sprint** | 11 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/batch_graph_resolver.py`. |
| **Evidence** | `BatchGraphResolver.batch_fetch_nodes()` and `batch_fetch_relationships()`; query count 50–200 → 3–5; 60–80% graph latency reduction; `elementId()` migration (Review3 M07); three-variant key fallback (Review4 confirmed). |

---

### GAP-A07 — Agentic Context Refinement (CRAG + Self-RAG)

| Field | Value |
|-------|-------|
| **Problem** | No iterative refinement of low-quality retrieved context for complex/multi-hop queries. |
| **Decision** | Add `ContextRefiner` activated only for complex queries (multi-hop, ambiguous, or low Stage-7 scores). Implements Corrective RAG (CRAG) and Self-RAG patterns — iteratively improves context quality before generation. |
| **ADR(s)** | **ADR-029** |
| **Sprint** | 14 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/context_refiner.py`. |
| **Evidence** | `set_llm_fn()` wires GPT4IFX; missing `import re` fixed (Review3 C10). |

---

### GAP-A08 — LLM-as-Judge Validation (DeepEval)

| Field | Value |
|-------|-------|
| **Problem** | Retrieved chunks are not quality-validated before DA consumption. AICE's deterministic 13-signal confidence formula evaluates metadata heuristics, not semantic correctness of the *text itself*. DocJockey's LLM-as-Judge with self-consistency shows 18% improvement in ranking correlation. |
| **Decision** | Add optional LLM-based relevance validation for top-10 chunks via DeepEval's `ContextualRelevancyMetric`. Custom GPT4IFX backend as fallback. Chunks below threshold are dropped. |
| **ADR(s)** | **ADR-027** |
| **Sprint** | 13 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/relevance_judge.py`, `requirements.txt` (`deepeval>=1.0.0`). |
| **Evidence** | `RelevanceJudge.judge_chunks()` with DeepEval primary + custom-LLM fallback; `ThreadPoolExecutor` for parallel chunk evaluation. |

---

### GAP-A09 — Dynamic Token Budget (Complexity-Driven)

| Field | Value |
|-------|-------|
| **Problem** | ContextBuilder used a fixed 10-slot 8 K budget regardless of query complexity. Simple queries wasted budget; complex queries ran out. |
| **Decision** | Couple budget to QueryEnhancer's complexity classification: simple → 4 K, medium → 8 K, complex → 12 K. Slots redistribute unused budget (≤30% used → donate to slots ≥90% used). |
| **ADR(s)** | **ADR-028** |
| **Sprint** | 13 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/context_builder.py`. |
| **Evidence** | 10-slot algorithm with redistribution; `copy.deepcopy` of slot-budget dict added per Review4 to prevent mutation; AICE-CTX-001..006 all IMPLEMENTED. |

---

### GAP-A10 — Multi-Language Code Analysis (Tree-sitter)

| Field | Value |
|-------|-------|
| **Problem** | DocJockey's code-retrieval service supports 7 languages (Python, JS, TS, Rust, Go, Scala, Java) via Tree-sitter. AICE supports C/H only. |
| **Decision** | **Do not implement.** AICE is purpose-built for AURIX TC3xx embedded SW which is exclusively C/H. AUTOSAR Classic is C only; iLLD is C only; test frameworks (Unity/CppUTest) are C; build scripts are not ingested into the KG. |
| **ADR(s)** | **ADR-034** |
| **Sprint** | 11 (decision) |
| **Status** | ❌ **REJECTED** (deferred indefinitely) |
| **Files** | — |
| **Evidence** | Re-evaluation trigger: AURIX tooling officially adopting Rust for safety-critical components, or a DA requiring non-C build infrastructure analysis. |

---

### GAP-A11 — OCR for Scanned HW Specifications

| Field | Value |
|-------|-------|
| **Problem** | Some legacy HW specifications are scanned PDFs. AICE could not process them without OCR. |
| **Decision** | Add Tesseract OCR as an optional ingestion stage. Detect scanned pages (<10 chars extractable text), route through OCR via subprocess (no `pytesseract` dependency), rejoin standard chunking pipeline. Page-level processing for memory efficiency. |
| **ADR(s)** | **ADR-033** |
| **Sprint** | 17 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/IngestionPipeline/parsers/ocr_processor.py`. |
| **Evidence** | Confidence estimation per page for quality filtering; system-package only (no Python dep). |

---

### GAP-A12 — Keycloak SSO (Enterprise Auth)

| Field | Value |
|-------|-------|
| **Problem** | AICE uses API-key authentication via Cerbos RBAC. DocJockey has Windows NTLM + OAuth2 enterprise SSO. |
| **Decision** | Defer Keycloak SSO. Current API-key auth is sufficient for programmatic DA access. All 21 DAs use programmatic API keys; no human-facing web UI exists yet. |
| **ADR(s)** | **ADR-020** (initial) → **ADR-035** (re-confirmed) |
| **Sprint** | 11 (decision) |
| **Status** | 🔄 **DEFERRED** |
| **Files** | — |
| **Evidence** | Re-evaluation trigger: human-facing dashboard or web UI; or enterprise security policy mandating SSO. |

---

### GAP-A13 — Citation Verification (Claim-Level)

| Field | Value |
|-------|-------|
| **Problem** | DA output claims are not verified against source context. Safety-critical automotive demands claim-level traceability. DocJockey's citation verification reduces hallucinations by ~85%. |
| **Decision** | Post-generation citation verification: extract claims (GPT4IFX or regex fallback), match against source KG nodes (text overlap + entity matching), flag unverified claims. Integrates with `ConfidenceCalculator` as an additional scoring signal. |
| **ADR(s)** | **ADR-031** |
| **Sprint** | 14 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/ReviewGate/citation_verifier.py`, `requirements.txt` (`ragchecker>=0.1.0`). |
| **Evidence** | New MCP tool `verify_citations` (Cat 9); RAGChecker fine-grained evaluation framework adopted. |

---

### GAP-A14 — Few-Shot Learning Library

| Field | Value |
|-------|-------|
| **Problem** | DA prompts lacked domain-specific examples. DocJockey's dynamic example injection shows 22% improvement in response consistency. |
| **Decision** | Maintain a few-shot example library in Qdrant (`few_shot_examples` collection). Retrieve 2–3 most-similar examples via vector search, inject into DA prompts. Populated **only** from `APPROVED` feedback with quality ≥ 80 (quality-gated). |
| **ADR(s)** | **ADR-032** |
| **Sprint** | 16 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/MemoryLayer/memory/few_shot_library.py`. |
| **Evidence** | Reuses existing Qdrant infrastructure; task-type filtering ensures relevance. |

---

### GAP-A15 — Distributed Tracing (OpenTelemetry)

| Field | Value |
|-------|-------|
| **Problem** | Prometheus metrics (11 types) were defined but completely **unwired** — zero counters incremented (Review3 finding). Without distributed tracing, debugging DA request flows through the 6-stage search pipeline (enhance → search → rerank → compress → judge → refine) was impossible. |
| **Decision** | Originally deferred (ADR-036 v1). Revised in Sprint 25 to **adopt** OpenTelemetry tracing for the MCP tool dispatch layer, search pipeline, and LLM calls only. Export via OTLP to Grafana Tempo (added to docker-compose). Skip auto-instrumentation of FastAPI/httpx — manual spans give better control. |
| **ADR(s)** | **ADR-036** (revised) |
| **Sprint** | 11 (deferral) → 25 (adoption) |
| **Status** | ✅ **FIXED** (MCP layer only) |
| **Files** | `src/Observability/tracing.py`, `docker-compose.yml` (Grafana Tempo). |
| **Evidence** | `trace_tool` decorator wired to top-5 MCP tools; Prometheus metrics also wired in the same sprint. |

---


## 3. Family B — Performance Bottlenecks (P0–P3)

These were the **four confirmed bottlenecks** identified during the Sprint 9 performance audit (`docs/architecture/performance_improvements.md`), expanded to seven items as additional issues surfaced. P-labels indicate priority (P0 = critical, P3 = enhancement).

---

### PERF-01 (P0) — Graph Search N+1 Query Problem

| Field | Value |
|-------|-------|
| **Problem** | `_graph_search()` ran a nested loop generating **50–200 individual Neo4j queries per search**, each doing `toLower(CONTAINS)` across 13 string properties. Largest single contributor to query latency. |
| **Decision** | Consolidate into a single Cypher query using `any(lbl IN labels(n) WHERE lbl IN $labels)` with `UNWIND` for keywords. Add Lucene fulltext index `aice_search_idx` across the 13 properties as fast-path; fallback to UNWIND-based query if index unavailable. |
| **ADR(s)** | (No new ADR — implementation of GAP-A06 + Sprint-9 perf fix) |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/search_service.py`, `src/HybridRAG/storage/neo4j_manager.py` (`ensure_fulltext_index()`). |
| **Evidence** | Query count 50–200 → 1–3; verified via existing unit tests + new async concurrency test. |

---

### PERF-02 (P0) — Synchronous Backend on Async Server (Event-Loop Blocking)

| Field | Value |
|-------|-------|
| **Problem** | All backend I/O (Neo4j, Qdrant, Redis, PostgreSQL) was called **synchronously from `async` MCP tool handlers**, blocking the uvicorn event loop and serializing concurrent requests. |
| **Decision** | Wrap every sync backend call in `await asyncio.to_thread(sync_fn, *args)`. Add `async def _warm_backends()` that parallel-initializes all backends via `asyncio.gather()` at server startup. |
| **ADR(s)** | (No new ADR — Sprint-9 perf fix) |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py`, `mcp/app.py`. |
| **Evidence** | Coverage extended from 29 → 63 `to_thread()` wrappings (all remaining tool handlers); `_warm_backends()` parallel-inits Neo4j (illd + mcal), Qdrant, Redis, PostgreSQL, CacheService. |

---

### PERF-03 (P1) — Sequential Hybrid Search Stages

| Field | Value |
|-------|-------|
| **Problem** | Initial review claimed graph search (Neo4j) and vector search (Qdrant) ran sequentially. |
| **Decision** | Run both stages in parallel via `asyncio.gather()` in `hybrid_search_async()`. |
| **ADR(s)** | — |
| **Sprint** | (Pre-existing on main) |
| **Status** | ✅ **ALREADY DONE** (originally listed as PARTIAL, **incorrectly**) |
| **Files** | `src/HybridRAG/code/querier/search_service.py`. |
| **Evidence** | `graph_results, vector_results = await asyncio.gather(_graph_stage(), _vector_stage())`; both stages already wrapped in `asyncio.to_thread()`. **No additional changes were needed** — Review4 confirmed this was a false positive in early reviews. |

---

### PERF-04 (P2) — Sequential Ingestion (Per-Module For-Loop)

| Field | Value |
|-------|-------|
| **Problem** | `batch_ingest()` processed modules in a serial for-loop. |
| **Decision** | `ThreadPoolExecutor(max_workers=4)` + `as_completed()` pattern. Add `update_progress()` to `IngestionJobTracker`. |
| **ADR(s)** | (See GAP-A05 / ADR-030 / ADR-037 for the broader pipeline.) |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/IngestionPipeline/ingestion_service.py`. |
| **Evidence** | Per-module progress tracking; 3–5× throughput on bulk module onboarding. |

---

### PERF-05 (P2) — SemanticCache O(n) Linear Scan

| Field | Value |
|-------|-------|
| **Problem** | `SemanticCache.get()` computed up to **500 pure-Python dot products per lookup** (linear scan over 500 entries). |
| **Decision** | Replace `_entries` list with `faiss.IndexFlatIP(384)` for SIMD-optimized sub-ms lookups. Add `RediSearchSemanticCache` as L2 shared cache (feature-flagged via `AICE_CACHE_L2_REDIS`). Upgrade `CacheService` from 2-tier to **3-tier**: LRU → FAISS L1 → RediSearch L2 → RAG. |
| **ADR(s)** | — |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/Configuration/cache_service.py`, `requirements.txt` (`faiss-cpu>=1.7.0`). |
| **Evidence** | Sub-ms lookups even at 25 K+ entries; on-cache-hit backfill from L2 → L1 → LRU. |

---

### PERF-06 (P2) — httpx Client Recreation Per Request

| Field | Value |
|-------|-------|
| **Problem** | `rlm_orchestrator.py` and `pdf_pipeline.py` created a **new httpx client for every LLM call** — TLS handshake on every request, no connection pooling. |
| **Decision** | Shared `_shared_http_client` singleton in both files. |
| **ADR(s)** | — |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/rlm_orchestrator.py`, `src/IngestionPipeline/parsers/pdf_pipeline.py`. |
| **Evidence** | Connection reuse confirmed; canonical contract `_gpt4ifx_call_sync` → `_get_shared_openai_client()` enforced. |

---

### PERF-07 (P3) — Qdrant HTTP Instead of gRPC

| Field | Value |
|-------|-------|
| **Problem** | `storage_config.yaml` sets `grpc: false`. gRPC is significantly faster than REST for vector operations. |
| **Decision** | Set `grpc: true` in `storage_config.yaml` after validating server-side gRPC port exposure. |
| **ADR(s)** | — |
| **Sprint** | (Not yet scheduled) |
| **Status** | 🆕 **OPEN** |
| **Files** | `src/HybridRAG/storage/storage_config.yaml`. |
| **Evidence** | Low-priority enhancement; documented in `performance_improvements.md` but not yet sprint-assigned. |

---

### PERF-Aux — RLM Progress Reporting

| Field | Value |
|-------|-------|
| **Problem** | RLM long-running orchestration provided no progress feedback to clients. |
| **Decision** | Add `on_progress` callback invoked after planning, each sub-query, and synthesis. |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/rlm_orchestrator.py`. |
| **Evidence** | `RLMOrchestrator.run(on_progress=...)`; `rlm_orchestrate` tool handler logs step-by-step progress. |

---

### PERF-Aux — APScheduler Background Jobs

| Field | Value |
|-------|-------|
| **Problem** | No mechanism for periodic maintenance jobs (health checks, cache stats, optional cache warming). |
| **Decision** | Add `BackgroundScheduler` with health check (5 min) and cache stats (30 min) periodic jobs. Graceful shutdown on SIGTERM. Silently disabled if `apscheduler` not installed. |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/app.py`. |
| **Evidence** | Pluggable schedule; SIGTERM handler integration. |

---


## 4. Family C — AI Governance Gaps (GAP-01 → GAP-11)

These 11 gaps were identified during the **AI Governance Maturity Assessment** (Sprint 10) targeting EU AI Act, NIST AI RMF, ISO 26262 Part 8, and ISO/PAS 8800 alignment. The package spans **Sprints 11–14** with 15 components, 4 SQL migrations (17 new PostgreSQL tables), 80 tests, and 4 governance documents (System Card, Usage Policy, Data Governance Policy, Incident Response).

The pre-package maturity score was **2.5 / 5**; target after Sprint 14 is **4.0 / 5**.

---

### GAP-01 — Output Provenance Chain (Critical)

| Field | Value |
|-------|-------|
| **Problem** | AICE logged tool invocations (`audit_logs`) and archived responses (`response_archive`), but had **no linkage** from archived response to: (a) specific KG nodes and Qdrant chunks used as context, (b) LLM model version and parameters, (c) final committed artifact in version control, (d) complete review evidence chain. Cannot fully reconstruct how an AI-generated artifact was produced — required by EU AI Act Art. 12 (record-keeping) and ISO 26262 Part 8 (tool qualification). |
| **Decision** | Extend `response_archive` with a `provenance` JSONB column. Modify `build_context` to collect KG node IDs and Qdrant chunk IDs; modify `evaluate_confidence` to pass through LLM info; modify `complete_review` to finalize the chain with review evidence. Add `get_provenance` admin tool. |
| **ADR(s)** | (Documented in `GOVERNANCE_IMPLEMENTATION_PLAN.md` §3.1) |
| **Sprint** | 11 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/Observability/postgres_schema.py` (schema migration), `mcp/core/mcp_server.py` (`build_context`, `evaluate_confidence`, `complete_review`, new `get_provenance`). |
| **Evidence** | Provenance JSON includes session_id, assistant, task_type, workspace, module, kg_nodes[], qdrant_chunks[], sandbox_docs[], rlm_used, llm_info{provider, model_version, temperature, prompt_tokens, completion_tokens}, review{verdict, reviewer, edits_summary}, artifact{commit_sha, files_affected, branch}. |

---

### GAP-02 — AI Transparency Marking in Outputs (Critical)

| Field | Value |
|-------|-------|
| **Problem** | AI-generated code, tests, and requirements bore no machine-readable marker indicating AI involvement, AICE version, session ID, or confidence — making EU AI Act Art. 50 transparency obligations unsatisfiable. |
| **Decision** | Add a `postprocess_output` step in each DA's response pipeline that injects a header comment for code (Doxygen-style `@ai_assisted`, `@ai_tool`, `@ai_session`, `@ai_review`, `@ai_confidence`) and a metadata block for non-code outputs. |
| **Sprint** | 11 |
| **Status** | ✅ **FIXED** |
| **Files** | DA postprocessors (CIA's `prompt_builder.py`, GEST backend); `AICEClient.build_context(inject_markers=True)`. |
| **Evidence** | Section 6.1 of `AI_USAGE_POLICY.md` codifies the header format; CIA-generated code includes the header by default. |

---

### GAP-03 — Model Version Tracking (Critical)

| Field | Value |
|-------|-------|
| **Problem** | `GPT4IFXClient` did not capture the LLM model version, temperature, or token counts per call. Audit logs could not establish *which* model produced an output. |
| **Decision** | Modify `GPT4IFXClient.complete()` to extract model metadata from the LLM response headers/body, return a `model_info` tuple, and persist into `audit_logs.params` (JSONB). Pass through to provenance chain. |
| **Sprint** | 11 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/rlm_orchestrator.py` (GPT4IFXClient wrapper). |
| **Evidence** | Captured fields: `provider`, `model_version` (e.g. `gpt-4o-2025-11-20`), `temperature`, `prompt_tokens`, `completion_tokens`. |

---

### GAP-04 — Governance Reporting Tool (Important)

| Field | Value |
|-------|-------|
| **Problem** | Raw governance data (audit logs, feedback, reviews, metrics) existed but no consolidated view made governance status visible to managers, safety officers, or auditors. |
| **Decision** | New developer-tier MCP tool `governance_report(period, workspace, format)` aggregating PostgreSQL + Prometheus + Neo4j into a single JSON or Markdown report. Sections: activity_summary, review_gate_summary, quality_metrics, data_governance, incidents. |
| **Sprint** | 12 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py` (new tool **#57** `governance_report`). |
| **Evidence** | DA breakdown, routing distribution, override reasons, verdict distribution, confidence calibration, MISRA violations in AI code, KG coverage per module. |

---

### GAP-05 — Governance Policy Enforcement (Important)

| Field | Value |
|-------|-------|
| **Problem** | Review routing was confidence-driven only; no policy-driven minimum review level by ASIL classification. ASIL-B+ modules could receive AUTO review if confidence was high. |
| **Decision** | New `governance_policy.yaml` defining minimum review levels per ASIL × artifact-type. `evaluate_confidence` enforces the floor (auto-escalates AUTO → QUICK → FULL when policy demands). |
| **Sprint** | 12 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/auth/policies/governance_policy.yaml`, `src/ReviewGate/confidence.py`. |
| **Evidence** | Policy matrix from `AI_USAGE_POLICY.md` §5.1 codified; ASIL-D code generation always FULL + independent. |

---

### GAP-06 — Data Governance Policy Document (Important)

| Field | Value |
|-------|-------|
| **Problem** | No formal data governance policy. Ingestion was tracked but the *policy* — data classification, authorized sources, quality criteria, lineage, retention — was undocumented. |
| **Decision** | Author `DATA_GOVERNANCE_POLICY.md` (AICE-GOV-004) covering: data classification (Public/Internal/Confidential/Restricted), authorized sources (Jama, Polarion, Bitbucket, internal SharePoint), quality criteria (parser validation, node completeness, relationship integrity), data lineage (source doc → parser version → ingestion job), retention rules, and access controls. |
| **Sprint** | 12 |
| **Status** | ✅ **FIXED** |
| **Files** | `docs/ai_governance_files/DATA_GOVERNANCE_POLICY.md`. |
| **Evidence** | Document committed; referenced from System Card. |

---

### GAP-07 — Coverage Bias Assessment Framework (Important)

| Field | Value |
|-------|-------|
| **Problem** | `get_graph_statistics` provided raw node/relationship counts but no framework for assessing *whether coverage gaps create systematic bias* in AI outputs. No threshold for "adequate coverage" was defined. |
| **Decision** | New developer-tier tool `assess_coverage_bias` comparing actual per-module counts against thresholds defined in YAML (e.g., APIFunction min 20 per module, 80% with relationships; Register min 50 per module, 60% linked to SW functions). Output: per-module bias_risk (LOW/MEDIUM/HIGH), specific issues, recommendation. |
| **Sprint** | 13 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py` (new tool **#58** `assess_coverage_bias`). |
| **Evidence** | Example output: `{"module": "UART", "bias_risk": "HIGH", "issues": ["APIFunction count (8) below minimum (20)", "Only 45% of registers linked..."], "recommendation": "Ingest UART iLLD source code and SWS document before using AI for this module"}`. |

---

### GAP-08 — Incident Response Procedure (Important)

| Field | Value |
|-------|-------|
| **Problem** | No formal procedure for handling AI-related quality escapes (AI-generated code with serious errors that passed review, or test cases that missed critical defects). Ad-hoc response, no systematic learning. |
| **Decision** | Author `INCIDENT_RESPONSE.md` (AICE-GOV-005) with 5-phase flow: Detection → Triage → Investigation → Corrective Action → Resolution. Severity classification (CRITICAL/HIGH/MEDIUM/LOW) with response-time SLAs. New PostgreSQL table `governance_incidents`. |
| **Sprint** | 12 |
| **Status** | ✅ **FIXED** |
| **Files** | `docs/ai_governance_files/INCIDENT_RESPONSE.md`, `src/Observability/postgres_schema.py` (`governance_incidents` table). |
| **Evidence** | Schema with severity, root_cause, corrective_action, status, resolved_at, resolved_by; reporting via FeedbackSink REJECT or governance tool. |

---

### GAP-09 — Pattern Store Governance (Nice-to-Have)

| Field | Value |
|-------|-------|
| **Problem** | Approved patterns accumulate in Neo4j PatternStore and Qdrant PatternIndex with no periodic review, no expiration, no mechanism to retire patterns made obsolete by spec changes. |
| **Decision** | Quarterly pattern review cadence; expire patterns after 12 months without re-validation; new admin tool `review_patterns`. |
| **Sprint** | 13 (planned) |
| **Status** | 🆕 **OPEN / Planned** |
| **Files** | (Pending) |
| **Evidence** | Spec in `GOVERNANCE_IMPLEMENTATION_PLAN.md` §2.3; AICE-GOV-009 requirement. |

---

### GAP-10 — Cross-DA Governance Dashboard (Nice-to-Have)

| Field | Value |
|-------|-------|
| **Problem** | Each DA operates independently. No unified view shows which DAs generate the most outputs, which have highest rejection rates, where governance risks concentrate. |
| **Decision** | Grafana dashboard extension with per-DA panels. Requires `assistant_name` label on Prometheus metrics. |
| **Sprint** | 12 (planned) |
| **Status** | 🆕 **OPEN / Planned** |
| **Files** | (Pending — Grafana provisioning JSON.) |
| **Evidence** | Spec in `GOVERNANCE_IMPLEMENTATION_PLAN.md` §2.3. |

---

### GAP-11 — RAG Quality Regression Testing ("Golden Query Set")

| Field | Value |
|-------|-------|
| **Problem** | After KG updates, embedding model changes, or cache config changes, retrieval quality may silently degrade. No automated regression test framework. |
| **Decision** | Maintain a "golden query set" with expected results; automated comparison after updates; quality-degradation alerts. |
| **Sprint** | 13 (planned) |
| **Status** | 🆕 **OPEN / Planned** |
| **Files** | (Pending — `tests/regression/golden_query_set.yaml`.) |
| **Evidence** | AICE-GOV-010 requirement; spec in `GOVERNANCE_IMPLEMENTATION_PLAN.md` §2.3. |

---


## 5. Family D — Review1 Findings (Initial Sweep)

Review1 was the first end-to-end audit of the AICE codebase against the requirements baseline (Sprints 1–9). It surfaced foundational issues with async correctness, Neo4j compatibility, and module placement. All issues were resolved before Sprint 10.

> **Note on issue codes:** Throughout the review cycles, issues were tagged `C##` (Critical), `H##` (High), `M##` (Medium), `L##` (Low). The same code may appear across multiple reviews when later cycles re-examined the area. Codes used here follow the **final convention** as recorded in `PIPELINE.md`, `DECISIONS.md`, and the apply-script READMEs.

---

### REV1-H08 — Async Wrapping of Query Enhancer

| Field | Value |
|-------|-------|
| **Problem** | `QueryEnhancer.enhance()` was called inline from `async` SearchService — a blocking call on the event loop. |
| **Decision** | Wrap the call in `asyncio.to_thread(...)`. |
| **Sprint** | (Sprint 11 retrofit) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/search_service.py`. |

---

### REV1-H14 — Neo4j Read-Mode Not Set

| Field | Value |
|-------|-------|
| **Problem** | Graph search sessions did not set `access_mode="READ"`, preventing Neo4j from routing to read replicas and weakening the read/write boundary. |
| **Decision** | Add `access_mode=neo4j.READ_ACCESS` to all read-only sessions in `graph_search.py`. (Subsequently extended to `execute_cypher` per Review4.) |
| **Sprint** | (Sprint 10) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/graph_search.py`. |

---

### REV1-H17 — Cypher Label Injection Risk

| Field | Value |
|-------|-------|
| **Problem** | Label parameters were f-string-interpolated into Cypher queries — Cypher does not parameterize labels, so unsanitized input was a **Cypher injection** vector. |
| **Decision** | Implement a label allowlist; reject labels not in the allowlist. (Subsequently extended via `_sanitize_label()` in `services.py` per Review4.) |
| **Sprint** | (Sprint 10) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/graph_search.py`, `src/HybridRAG/code/querier/services.py`. |

---

### REV1-H18 — Vector-Search Embedding Dimension Mismatch

| Field | Value |
|-------|-------|
| **Problem** | When the embedding model failed to load, `vector_search.py` returned a None or zero-length vector — Qdrant rejected the query. |
| **Decision** | Fallback to a 384-dim zero-padded embedding consistent with `all-MiniLM-L6-v2`. |
| **Sprint** | (Sprint 10) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/vector_search.py`. |

---

### REV1-M07 — Neo4j 4.x `id()` → 5.x `elementId()` Migration

| Field | Value |
|-------|-------|
| **Problem** | Code used the deprecated `id(n)` Cypher function — incompatible with Neo4j 5.x semantics. |
| **Decision** | Migrate to `elementId(n)`. Provide a runtime detection helper (`_eid_fn()` in `BatchGraphResolver`) that picks `elementId` for 5.x and falls back to `id` for 4.x. |
| **Sprint** | (Sprint 11) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/batch_graph_resolver.py`, `src/HybridRAG/code/querier/graph_search.py`, `src/MemoryLayer/memory/ephemeral_sandbox.py`. |
| **Evidence** | Verified Review4 — fix already in source. |

---

### REV1-M09 — Token Estimation Inconsistency

| Field | Value |
|-------|-------|
| **Problem** | Different modules used `len(text)//3` and `len(text)//4` — token-budget math drifted between Stage 4 (RRF merge) and Stage 6 (compressor). |
| **Decision** | Standardize on `len(text)//4` for RRF merge, retain `len(text)//3` (more conservative) in ContextBuilder. Document the split. |
| **Sprint** | (Sprint 11) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/rrf_merge.py`. |

---

### REV1-N-H04 — Pattern Indexing Without Pattern Store Confirmation

| Field | Value |
|-------|-------|
| **Problem** | `FeedbackSink._record_pattern()` indexed into Qdrant PatternIndex without first verifying the Neo4j PatternStore write succeeded — could create orphan Qdrant entries. |
| **Decision** | Guard `pattern_index.index_pattern(...)` with `if pattern_stored and pattern is not None`. |
| **Sprint** | (Sprint 10/11) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/ReviewGate/confidence.py`. |
| **Evidence** | `# N-H04 fix` comment present in source. |

---

### REV1 (other) — RLM Tool Category Placement

| Field | Value |
|-------|-------|
| **Problem** | Initial design placed `rlm_orchestrate` and `rlm_plan_preview` in a new category, breaking the 13-category architecture. |
| **Decision** | Place RLM tools in **Category 6 (Memory & Context)**. |
| **Sprint** | (Sprint 5/10) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/auth/policies/resource_mcp_tool.yaml`, `mcp/auth/tool_tiers.py`. |
| **Evidence** | AICE-RLM-006 requirement IMPLEMENTED. |

---

### REV1 (other) — Foreign-Key Constraint on Review Evidence

| Field | Value |
|-------|-------|
| **Problem** | `save_review_evidence` could fail with FK violation when `response_archive` row didn't exist. |
| **Decision** | Add idempotent `INSERT ... ON CONFLICT DO NOTHING` for the parent row before the child write. |
| **Sprint** | (Sprint 10) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/Observability/postgres_schema.py`. |
| **Evidence** | `# H09 fix: ensure response_archive row exists (FK constraint)` comment. |

---

## 6. Family E — Review2 Findings (Infrastructure / F-codes)

Review2 focused on **deployment, security, and operational hardening**. Issues are labeled `F##` (infrastructure findings).

---

### REV2-F02 — Hardcoded Credentials in `docker-compose.yml`

| Field | Value |
|-------|-------|
| **Problem** | Plaintext passwords in `docker-compose.yml` (`aice_dev_2026`, `neo4j_aice_2026`, `redis_aice_2026`) and committed `mcp/auth/api_keys.yaml`. |
| **Decision** | Externalize all credentials to `.env` with `${VAR:-default}` syntax. Add `.env` and `api_keys.yaml` to `.gitignore`. Provide `.env.example`. Defer Docker Secrets / HashiCorp Vault to multi-node phase. |
| **ADR(s)** | **ADR-039** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `docker-compose.yml`, `.env.example`, `.gitignore`. |

---

### REV2-F03 — Zero Rate Limiting

| Field | Value |
|-------|-------|
| **Problem** | No rate limiting anywhere in the MCP server — a single client could exhaust backends or trigger LLM cost overruns. |
| **Decision** | Add `slowapi>=0.1.9` middleware with per-API-key limits: 60 req/min for search/query, 10 req/min for admin, 5 req/min for ingestion. Include `X-RateLimit-*` response headers. |
| **ADR(s)** | **ADR-040** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/rate_limiter.py` (new), `mcp/app.py` (Starlette middleware), `requirements.txt` (`slowapi`, `limits`). |

---

### REV2-F04 — Single-Process Deployment

| Field | Value |
|-------|-------|
| **Problem** | MCP server ran as a single Python process (`uvicorn` direct) — no Gunicorn or equivalent multi-worker wrapper. Single process cannot utilize multiple CPU cores. |
| **Decision** | Change `Dockerfile` `CMD` to `gunicorn mcp.app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000`. Add `gunicorn>=22.0` to requirements. Worker count via `WEB_CONCURRENCY`. |
| **ADR(s)** | **ADR-043** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `Dockerfile`, `requirements.txt`. |
| **Evidence** | Review4 final fix — Gunicorn documentation added to Dockerfile; package added to requirements. |

---

### REV2-F05 — `.gitignore` Hygiene

| Field | Value |
|-------|-------|
| **Problem** | Sensitive files (`.env`, `api_keys.yaml`, `cerbos_secrets.yaml`) not in `.gitignore`. |
| **Decision** | Add the missing entries; verify no plaintext secrets in git history. |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `.gitignore`. |

---

### REV2-F06 — Dead-Code Patch Files

| Field | Value |
|-------|-------|
| **Problem** | `tool_registration_patch.py` and `search_service_patch.py` were one-time monkey-patch files that had been folded into source — they were imported but no-op. |
| **Decision** | Delete the dead files. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/tool_registration_patch.py` (deleted), `src/HybridRAG/code/querier/search_service_patch.py` (deleted). |

---

### REV2-F07 — `requirements.txt` Drift

| Field | Value |
|-------|-------|
| **Problem** | Several packages used at runtime (`gunicorn`, `limits`, `flashrank`) were missing from `requirements.txt` — Docker build succeeded but runtime imports failed. |
| **Decision** | Audit imports vs requirements; add missing packages. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `requirements.txt`. |

---


## 7. Family F — Review3 Findings (Deep Code Cross-Verification, C/H/M/L)

Review3 was the most extensive review cycle — a deep cross-verification pass that caught **runtime correctness bugs**, **dead code**, **stub patterns**, and **architectural misalignments** that earlier reviews had missed. Issues are labeled `C##` (Critical), `H##` (High), `M##` (Medium), `L##` (Low).

---

### REV3-C04 — Hardcoded Credentials (Re-Confirmed)

Same as REV2-F02 — Review3 re-confirmed the issue and triggered ADR-039. ✅ FIXED.

---

### REV3-C05 — Zero Rate Limiting (Re-Confirmed)

Same as REV2-F03 — Review3 re-confirmed and triggered ADR-040. ✅ FIXED.

---

### REV3-C06 — Single-Process Deployment (Re-Confirmed)

Same as REV2-F04 — Review3 re-confirmed and triggered ADR-043. ✅ FIXED.

---

### REV3-C07 — CBMC Bridge JSON / Text Parse Mismatch

| Field | Value |
|-------|-------|
| **Problem** | The CBMC (Bounded Model Checking) bridge always failed because the parser expected JSON output but CBMC was invoked in text-output mode. The feature was effectively dead — every call returned an error. |
| **Decision** | Defer full CBMC implementation (no CBMC binary available in deployment env). Fix the JSON parsing bug as a low-effort unblocker. Register a stub MCP tool returning "Feature not yet available". |
| **ADR(s)** | **ADR-041** |
| **Sprint** | 25 |
| **Status** | 🔄 **DEFERRED** (parsing bug fixed; full feature deferred) |
| **Files** | (CBMC bridge module — stub registered.) |
| **Evidence** | Re-evaluation trigger: CBMC binary available in CI/CD. |

---

### REV3-C08 — FMEA Engine Has No MCP Tool / DA Class / Output Format

| Field | Value |
|-------|-------|
| **Problem** | FMEA propagation logic existed in source but had no MCP tool registration, no DA class, no output-format decision. Effectively unreachable. |
| **Decision** | Defer full FMEA implementation. FMEA workflow requirements not yet defined by safety team; spreadsheet output format not yet selected. Register a stub tool. |
| **ADR(s)** | **ADR-041** |
| **Sprint** | 25 |
| **Status** | 🔄 **DEFERRED** |
| **Files** | (FMEA engine module — stub registered.) |
| **Evidence** | Re-evaluation trigger: FMEA workflow requirements defined by safety team. |

---

### REV3-C09 — MISRA & GEST Tools Auth-Denied

| Field | Value |
|-------|-------|
| **Problem** | `remediate_misra_violation` and `generate_unit_tests` MCP tools were registered but **permanently denied by Cerbos** because they were missing from `tool_tiers.py`. Tools were dead-end. |
| **Decision** | Add both tools to `tool_tiers.py` as DEVELOPER tier (2-line fix). Fix the MISRA compliance matrix to query KG for rule count (was hardcoded to 10; should be 175+). Do not implement the GEST compile-and-fix loop (requires target compiler decision — Tasking/GCC/GHS). Do not implement Polarion ALM export (no Polarion instance available). |
| **ADR(s)** | **ADR-042** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** (auth + MISRA matrix); 🆕 **OPEN** (GEST compile loop, Polarion). |
| **Files** | `mcp/auth/tool_tiers.py`, MISRA matrix module. |
| **Evidence** | Re-evaluation trigger: target compiler selected for CI/CD; Polarion integration spec provided. |

---

### REV3-C10 — Missing `import re` in Multiple Pipeline Modules

| Field | Value |
|-------|-------|
| **Problem** | `context_compressor.py` and `context_refiner.py` used regex (`re.findall`, `re.sub`) without importing `re` — would raise `NameError` at runtime on the first regex call. |
| **Decision** | Add `import re` to both files. |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/context_compressor.py`, `src/HybridRAG/code/querier/context_refiner.py`. |

---

### REV3-H01 — `_merge_results_rrf` Mis-Named (Implementation Was Alpha-Weighted)

| Field | Value |
|-------|-------|
| **Problem** | Function `_merge_results_rrf` was implementing **alpha-weighted blending**, not Reciprocal Rank Fusion. Naming-vs-behavior mismatch — confusing for new contributors and incorrect in docs. |
| **Decision** | Rename to `_merge_results_weighted` to reflect the actual implementation. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/search_service.py`, `src/HybridRAG/code/querier/rrf_merge.py`. |
| **Evidence** | Naming-accuracy principle from Review4. |

---

### REV3-H02 — Spurious `api_key` Parameter in Deferred Stubs

| Field | Value |
|-------|-------|
| **Problem** | Some deferred MCP tool stubs accepted an unused `api_key` parameter — confusing API surface and suggested credentials should flow through the call. |
| **Decision** | Remove the parameter from stub signatures. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | (Stub modules.) |

---

### REV3-H03 — `_gpt4ifx_call_sync` No Retry on Transient Failure

| Field | Value |
|-------|-------|
| **Problem** | LLM calls had no retry on transient HTTP failures; one network blip aborted an entire RLM orchestration. |
| **Decision** | Add 3-attempt exponential backoff retry. ERROR-level log on final failure. Backoff base = 1 s, multiplier = 2. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/code/querier/rlm_orchestrator.py`. |

---

### REV3-H05 — `'pattern' in dir()` Fragile Check

| Field | Value |
|-------|-------|
| **Problem** | A check used `if 'pattern' in dir():` to test for a local variable's existence — fragile (broken by name shadowing, slow, idiomatic-Python anti-pattern). |
| **Decision** | Replace with `if pattern is not None:`. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/ReviewGate/confidence.py`. |

---

### REV3-H06 — Tool Docstring / Tier Mismatch

| Field | Value |
|-------|-------|
| **Problem** | Several MCP tool docstrings claimed access tier "developer" while `tool_tiers.py` had them as "admin" or vice versa — confusing for DA developers and a documentation defect. |
| **Decision** | Reconcile docstrings to match `tool_tiers.py` (single source of truth). |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py`. |

---

### REV3-M01 — Tool Count Drift in Tests / Docs / Docstrings

| Field | Value |
|-------|-------|
| **Problem** | Tests and docstrings asserted "52 tools" or "56 tools" inconsistently. After governance + GAP additions, real count was 62. |
| **Decision** | Single canonical count = **62** in tests, docs, and docstrings (post-Review4). |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `tests/unit/test_*.py`, `docs/DOCUMENTATION.md`, `mcp/core/mcp_server.py`. |
| **Evidence** | Verification command: `python -c "from mcp.core.mcp_server import mcp; print(len(mcp._tools), 'tools registered')"`. |

---

### REV3-M02 — Prometheus Metrics Defined But Not Wired

| Field | Value |
|-------|-------|
| **Problem** | 11 Prometheus metric types were defined in `src/Observability/metrics.py` but **zero counters were ever incremented** — the `/metrics` endpoint returned only the constant labels. Effectively dead observability. |
| **Decision** | Wire metrics at the MCP tool dispatch layer, search pipeline stages, and LLM calls. (Done as part of ADR-036 distributed tracing revision in Sprint 25.) |
| **ADR(s)** | **ADR-036** (revised) |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/Observability/metrics.py`, `mcp/core/mcp_server.py`. |

---

### REV3-M03 — `flashrank` Missing from `requirements.txt`

| Field | Value |
|-------|-------|
| **Problem** | Reranker code imported `flashrank` but it wasn't in requirements — falling back to CrossEncoder kept PyTorch mandatory and bloated the Docker image. |
| **Decision** | Add `flashrank>=0.2.0` to `requirements.txt`. (Triggered ADR-038 to remove PyTorch as mandatory.) |
| **ADR(s)** | **ADR-038** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |
| **Files** | `requirements.txt`. |

---

### REV3-M04 — Celery Imports That Don't Resolve

| Field | Value |
|-------|-------|
| **Problem** | Comments and stub code referenced Celery, but Celery was never added as a dependency. Misleading. |
| **Decision** | Triggered ADR-037 — replace Celery design with stdlib `asyncio.TaskGroup` + `ProcessPoolExecutor`. Delete Celery references. |
| **ADR(s)** | **ADR-037** |
| **Sprint** | 25 |
| **Status** | ✅ **FIXED** |

---

### REV3-M05 — Stub `litellm` Wrapper Removed

| Field | Value |
|-------|-------|
| **Problem** | A LiteLLM wrapper had been introduced as an experimental abstraction but raised a security concern. Was kept as a dead path. |
| **Decision** | **Remove LiteLLM permanently from the project.** All LLM calls flow through the canonical `_gpt4ifx_call_sync` → `_get_shared_openai_client()` contract. |
| **Sprint** | (Pre-Review4) |
| **Status** | ✅ **FIXED** (and the policy is "must not be reintroduced"). |
| **Files** | (LiteLLM wrapper deleted.) |

---

### REV3-M07 — Neo4j `id()` → `elementId()` (Re-Verified)

Already addressed under REV1-M07; Review3 re-verified and Review4 confirmed all call-sites migrated. ✅ FIXED.

---

### REV3-M09 — Token Estimation Drift (Re-Verified)

Already addressed under REV1-M09. ✅ FIXED.

---

### REV3-M10 — Bash Scripts Not macOS-Compatible

| Field | Value |
|-------|-------|
| **Problem** | Apply scripts used `sed -i` which behaves differently on GNU (Linux) vs BSD (macOS) — required `-i ''` on macOS. Scripts failed silently on developer machines. |
| **Decision** | Add a `sedi()` wrapper that detects OS via `uname` and dispatches accordingly. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | `apply_review4_fixes.sh` and similar. |
| **Evidence** | Archive-hygiene principle from Review4. |

---

### REV3-M11 — `mkdir -p` Brace Expansion Creates Stray `{` Directories

| Field | Value |
|-------|-------|
| **Problem** | Apply scripts using `mkdir -p some/{a,b}` could leave literal `{` directories on shells where brace expansion is disabled. |
| **Decision** | Use separate `mkdir -p` calls. Verify archives via `tar tzf … \| grep -v __pycache__ \| sort`. |
| **Sprint** | 25 (Review4) |
| **Status** | ✅ **FIXED** |
| **Files** | Apply scripts. |

---

### REV3-L## — Various Low-Severity Findings

Aggregate of low-severity findings: import-order tweaks, docstring formatting, log-message improvements, comment typos. All addressed in Review4 fix bundle.

✅ **FIXED**.

---


## 8. Family G — Review4 Findings (Final Cross-Verification)

Review4 was a **deep cross-verification pass** designed to catch issues that earlier reviews had recorded as "needs fix" when in fact they had already been resolved in source — and to surface the remaining 11 confirmed issues. Source: `review4.md` / `apply_review4_fixes.sh` / `review4_final.md`.

### 8.1 Already-Fixed Issues (Falsely Flagged in Earlier Reviews)

Review4 **confirmed in source** that the following issues — flagged in Review1/2/3 as outstanding — were already resolved:

| Issue | Module | Confirmed Fix |
|-------|--------|---------------|
| Slot-budget mutation | `context_builder.py` | `copy.deepcopy` of slot dict prevents per-call mutation. |
| Key-mismatch in batch resolver | `batch_graph_resolver.py` | Three-variant key fallback (`element_id` / `elementId` / `eid`). |
| Neo4j `id()` migration | Multiple | All call-sites migrated to `elementId()`. |
| RLM threading lock | `rlm_orchestrator.py` | Lock is in place, no double-init. |

> **Principle:** *Cross-verification before flagging.* — a Sai Kiran principle now formalized for all future reviews.

### 8.2 Confirmed Remaining Issues (11) — All Addressed by `apply_review4_fixes.sh`

| # | Issue | File(s) | Resolution | Status |
|--:|-------|---------|------------|--------|
| 1 | Tool count drift | tests, docs, docstrings | Aligned to **62**. | ✅ |
| 2 | Missing `access_mode="READ"` on `execute_cypher` | `services.py` | Added. | ✅ |
| 3 | Cypher label injection in `services.py` | `services.py` | `_sanitize_label()` added. | ✅ |
| 4 | Spurious `api_key` parameter in deferred stubs | Stub modules | Removed. | ✅ |
| 5 | No retry on transient failure in `_gpt4ifx_call_sync` | `rlm_orchestrator.py` | 3-attempt exponential backoff. | ✅ |
| 6 | Fragile `'pattern' in dir()` check | `confidence.py` | Replaced with `is not None`. | ✅ |
| 7 | Tool docstring tier mismatch | `mcp_server.py` | Reconciled to `tool_tiers.py`. | ✅ |
| 8 | `_merge_results_rrf` mis-named | `search_service.py` | Renamed to `_merge_results_weighted`. | ✅ |
| 9 | Dead-code patch files | `tool_registration_patch.py`, `search_service_patch.py` | Deleted. | ✅ |
| 10 | Gunicorn not documented in `Dockerfile`; `gunicorn`, `limits`, `flashrank` missing from requirements | `Dockerfile`, `requirements.txt` | Documented + packages added. | ✅ |
| 11 | Bash `sed -i` macOS/Linux incompatibility | Apply scripts | `sedi()` wrapper. | ✅ |

### 8.3 Deliberately Deferred (1)

| Item | Why Deferred | Sprint |
|------|--------------|--------|
| Wire `CacheService` into the search pipeline | Requires architectural decision: **cache-aside** vs **cache-through**. CacheService exists and is wired at MCP tool entry, but the search pipeline itself doesn't consume it. Recommended as Sprint 11 design task. | 11 (planned) |

🆕 **OPEN** — `CacheService` search-pipeline integration.

### 8.4 ContextBuilder Architecture Resolution (Documented)

| Field | Value |
|-------|-------|
| **Problem** | Two `ContextBuilder` implementations existed: the authoritative `src/HybridRAG/code/querier/context_builder.py` (10-slot, Sprint 8) and a legacy `src/MemoryLayer/memory/context_builder.py` (Sprint 2 "librarian"). Architecturally, ContextBuilder belongs in MemoryLayer — but `SearchService` and `RLMOrchestrator` consumers live in HybridRAG. |
| **Decision** | (a) Re-export the HybridRAG version from MemoryLayer for architectural consistency. (b) Rename the legacy version to `LegacyContextBuilder`, retained for E2E test compatibility only. |
| **Sprint** | 25 (decision); migration in 11 (planned). |
| **Status** | 🟡 **PARTIAL** — decision made; migration guide delivered; re-export pending. |
| **Files** | `src/HybridRAG/code/querier/context_builder.py`, `src/MemoryLayer/memory/context_builder.py`, `src/MemoryLayer/__init__.py` (re-export). |

---

## 9. Cross-Cutting Themes (Captured From All Reviews)

These are the recurring patterns the review cycles surfaced. They are **not** individual gaps but **principles** that govern how future gaps are detected, classified, and fixed.

### 9.1 No Stubs / No Boilerplate

Every module must pass:
- API contract checks (signatures match documented contracts).
- Cross-module signature consistency (canonical contracts: `llm_fn(system, user, max_tokens) → str`; `search_fn(query, max_results) → list[dict]`).
- External library API correctness.

Stub MCP tools are permitted **only** when explicitly deferred via ADR (e.g., CBMC, FMEA) and must return a structured "Feature not yet available" error.

### 9.2 Cross-Verification Before Flagging

Several findings initially marked "unfixed" were in fact already resolved in source. Future reviews must verify against **actual code** (not just docs) before concluding something is broken.

### 9.3 Naming Accuracy

`_merge_results_rrf` → `_merge_results_weighted` was the canonical example. Terminology must match behavior precisely; mis-naming creates a permanent maintenance burden.

### 9.4 Security Defaults

Recurring risks:
- Cypher injection via f-string interpolation → label allowlist + `_sanitize_label()`.
- `access_mode="READ"` on read sessions.
- LiteLLM removed permanently.
- Credentials externalized to `.env`.

### 9.5 Architectural Placement Discipline

- RLM belongs in **Category 6** (Memory & Context), not a new category.
- ContextBuilder belongs architecturally in **MemoryLayer**.
- Deviations are corrected explicitly.

### 9.6 Deferred Decisions Need ADRs

Items deferred (Keycloak, multi-language, CBMC, FMEA, CacheService wiring) are documented with **explicit ADRs and re-evaluation triggers** — never left as open TODOs.

### 9.7 macOS / Linux Bash Compatibility

Bash apply-scripts must use `sedi()` wrapper detecting OS via `uname`.

### 9.8 Archive Hygiene

`mkdir -p` brace expansion can create stray `{` directories on some shells. Use separate `mkdir -p` calls. Verify archives with `tar tzf <file> | grep -v __pycache__ | sort`.

---

## 10. Master Status Roll-Up (All Families)

### 10.1 ✅ FIXED (77 items)

**Family A — DocJockey (13):** GAP-A01, A02, A03, A04, A05, A06, A07, A08, A09, A11, A13, A14, A15.

**Family B — Performance (6):** PERF-01, 02, 03, 04, 05, 06 + Aux-RLM-Progress + Aux-APScheduler.

**Family C — Governance (8):** GAP-01, 02, 03, 04, 05, 06, 07, 08.

**Family D — Review1 (~9):** REV1-H08, H14, H17, H18, M07, M09, N-H04, RLM placement, FK constraint.

**Family E — Review2 (6):** REV2-F02, F03, F04, F05, F06, F07.

**Family F — Review3 (~21):** REV3-C04 (re), C05 (re), C06 (re), C07 (parsing fix), C09 (auth + matrix), C10, H01, H02, H03, H05, H06, M01, M02, M03, M04, M05, M07 (re-verify), M09 (re-verify), M10, M11, L## (low-severity batch).

**Family G — Review4 (11):** All confirmed remaining issues addressed by `apply_review4_fixes.sh`.

### 10.2 🔄 DEFERRED / ❌ REJECTED (4 items)

| ID | Title | Disposition | ADR | Re-Evaluation Trigger |
|----|-------|-------------|-----|----------------------|
| GAP-A10 | Multi-language code analysis | ❌ REJECTED | ADR-034 | AURIX adopts Rust for safety-critical; or non-C ingestion need. |
| GAP-A12 | Keycloak SSO | 🔄 DEFERRED | ADR-020 / ADR-035 | Human-facing UI; or enterprise SSO mandate. |
| REV3-C07 | CBMC bridge (full) | 🔄 DEFERRED | ADR-041 | CBMC binary in CI/CD environment. |
| REV3-C08 | FMEA engine (full) | 🔄 DEFERRED | ADR-041 | Safety-team workflow definition. |

### 10.3 🆕 OPEN / Planned (~7 items)

| ID | Title | Target Sprint | Notes |
|----|-------|--------------:|-------|
| GAP-09 | Pattern Store governance (expiration + cadence) | 13 | AICE-GOV-009. |
| GAP-10 | Cross-DA governance dashboard | 12 | Grafana per-DA panels. |
| GAP-11 | Golden query set (RAG regression) | 13 | AICE-GOV-010. |
| PERF-07 | Qdrant gRPC | TBD | Low priority. |
| REV3-C09 (residual) | GEST compile-and-fix loop | TBD | Needs target compiler. |
| REV3-C09 (residual) | Polarion ALM export | TBD | Needs Polarion instance. |
| Review4 deferral | CacheService → search pipeline integration | 11 | Architectural decision pending. |

---

## 11. Decision Trail Summary (ADRs 001–043)

| ADR | Title | Status |
|----:|-------|--------|
| 001 | Knowledge Graph — Neo4j | Adopted |
| 002–014 | Foundational stack (Qdrant, Redis, PostgreSQL, Cerbos, FastMCP, etc.) | Adopted |
| 015 | Structure-Aware Chunking for AUTOSAR | Adopted |
| 016 | Celery Task Queue | Deferred → Superseded by ADR-037 |
| 017 | MinIO / S3 Object Storage | Adopted (Sprint 6) |
| 018 | Cross-Encoder Reranking | Deferred → Adopted as ADR-022 |
| 019 | Local On-Premise Deployment | Adopted |
| 020 | Keycloak OAuth | Deferred → Re-confirmed as ADR-035 |
| 021 | Prometheus + Grafana Observability | Adopted (Sprint 10) |
| **022** | **Cross-Encoder Reranking (GAP-A01)** | **Adopted (Sprint 12)** |
| **023** | **Query Enhancement Pipeline (GAP-A03)** | **Adopted (Sprint 11)** |
| **024** | **MCP Streaming Transport (GAP-A02)** | **Adopted (Sprint 12)** |
| **025** | **Advanced Context Compression (GAP-A04)** | **Adopted (Sprint 13)** |
| **026** | **Batch Graph Queries (GAP-A06)** | **Adopted (Sprint 11)** |
| **027** | **LLM-as-Judge Validation (GAP-A08)** | **Adopted (Sprint 13)** |
| **028** | **Dynamic Token Budget (GAP-A09)** | **Adopted (Sprint 13)** |
| **029** | **Agentic Context Refinement (GAP-A07)** | **Adopted (Sprint 14)** |
| **030** | **Batch Ingestion Pipeline (GAP-A05)** | **Adopted (Sprint 15)** → Revised by ADR-037 |
| **031** | **Citation Verification (GAP-A13)** | **Adopted (Sprint 14)** |
| **032** | **Few-Shot Learning Library (GAP-A14)** | **Adopted (Sprint 16)** |
| **033** | **OCR for Scanned Documents (GAP-A11)** | **Adopted (Sprint 17)** |
| **034** | **Multi-Language Code Analysis (GAP-A10)** | ❌ **Rejected** |
| **035** | **Keycloak SSO (GAP-A12)** | 🔄 **Deferred** |
| **036** | **Distributed Tracing (GAP-A15)** | **Revised — Adopted (MCP layer only, Sprint 25)** |
| **037** | **Celery Replaced by `asyncio.TaskGroup` (GAP-A05 revision)** | Adopted (Sprint 25) |
| **038** | **FlashRank Replaces PyTorch (GAP-A01 revision)** | Adopted (Sprint 25) |
| **039** | **Credential Externalization** | Adopted (Sprint 25) |
| **040** | **Rate Limiting via slowapi** | Adopted (Sprint 25) |
| **041** | **CBMC and FMEA — Deferred** | 🔄 **Deferred** |
| **042** | **MISRA and GEST — Fix Auth and Bugs Only** | Adopted (Sprint 25) — full GEST loop deferred |
| **043** | **Multi-Worker Deployment via Gunicorn** | Adopted (Sprint 25) |

---

## 12. Verification Commands & Evidence Anchors

For each delivery cycle, Sai Kiran's standard verification commands have been:

```bash
# Tool count
python -c "from mcp.core.mcp_server import mcp; print(len(mcp._tools), 'tools registered')"

# All tests
pytest tests/ -q --tb=short

# Health check
curl -s http://localhost:8000/health | jq .

# Metrics
curl -s http://localhost:8000/metrics | grep aice_

# Archive integrity
tar tzf <archive.tar.gz> | grep -v __pycache__ | sort

# Cypher injection sanity
python -c "from src.HybridRAG.code.querier.services import _sanitize_label; print(_sanitize_label('Function;DROP'))"

# elementId migration check
grep -rn "id(n)" src/ | grep -v elementId
```

---

## 13. Outstanding Architectural Debt (Non-Gap)

Items not formally gaps but tracked as architectural debt:

1. **Neo4j Community Edition** — single-database write transactions limit. Trigger: multi-tenancy or HA cluster requirement.
2. **Workspace isolation via Neo4j databases** (`illd`, `mcal`) is logical, not physical. Trigger: regulated multi-tenant deployment.
3. **No formal contract for `task_type`** — RLM_DA mapping covers 24 task types but additions require code changes (not config).
4. **Ontology YAML (6166 lines)** — single-file growth; consider modular split. No urgency.
5. **Sprint 10 audit-trail retention** — currently per-org policy; consider table-partitioning for >2-year data.

---

## 14. Document Maintenance

- **Update cadence:** End of every sprint that closes any gap or introduces a new one.
- **Review cadence:** Quarterly (with the AI Governance Quarterly Report — AICE-GOV-001 §quarterly_report).
- **Authoritative source:** This file. Where it disagrees with `DECISIONS.md`, `PIPELINE.md`, or `GOVERNANCE_IMPLEMENTATION_PLAN.md`, **this file wins** for status; ADRs win for rationale.
- **Filing convention:** New gaps prefixed by family (`GAP-A##`, `PERF-##`, `GAP-##` for governance, `REV#-X##` for review-cycle findings).

---

*End of master gap list. Document version 1.0 — captures complete history through Sprint 25 / Review4.*

---

# SUPPLEMENTAL UPDATE — Gaps Found in Additional Source Documents
*Added: 2026-04-25 — based on review of 20 supplementary files including the AI_Core_Engine_Review (March 2026 architecture-vs-implementation audit), ephemeral_sandbox_design, Research_Gap_Analysis_Upgrade_Plan, Performance_Impact_Complexity_Analysis, Chunking_Strategies, Celery_queue, MinIO, PostgreSQL, Redis_broker, Grafana_Prometheus, AICE_vs_DocJockey_Comparison, and the diff_feature_list of the further_enhancements branch.*

This supplement extends the master list with **gap families and items surfaced in the supplementary documents that were not yet captured**. Where an item overlaps with an existing entry, that is noted explicitly.

---

## Family H — March 2026 Architecture-vs-Implementation Audit (B-codes, "AI Core Engine Review")

This was the **earliest, foundational audit** (March 15, 2026) of the AICE codebase against the v3 Developer Guide PPTX specification. It established the gap baseline that subsequent reviews built on. The audit covered all 50 declared tools across 13 categories. Counts at the time: 50 tools declared, ~22 fully implemented, ~18 placeholder/stub, ~10 partial.

> Many of these items were resolved during Sprints 5–10, before the GAP-A program began. They are documented here for completeness — Review3 / Review4 effectively re-audited these areas.

---

### REV-MAR-B1 — Tool Name Mismatch: `search_databases` (plural) vs `search_database` (singular)

| Field | Value |
|-------|-------|
| **Problem** | Architecture spec defines tool as `search_database` (singular). Implementation registered as `search_databases` (plural). All Domain Assistants calling `search_database` got tool-not-found errors. |
| **Decision** | Rename to `search_database` to match specification. |
| **Sprint** | (Sprint 5–6) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py`. |

---

### REV-MAR-B3 — Cerbos Authorization Completely Disabled

| Field | Value |
|-------|-------|
| **Problem** | `_authorize()` always returned `None`; all Cerbos checks were commented out. The 3-tier access model (Public/Developer/Admin) documented in the spec was **not enforced**. Any caller could invoke Admin tools like `cache_clear` or `ingest_repository`. |
| **Decision** | Uncomment Cerbos authorization checks; enable API-key validation; enforce 3-tier RBAC. |
| **Sprint** | (Sprint 6) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py`, `mcp/auth/auth_middleware.py`, `mcp/auth/policies/resource_mcp_tool.yaml`. |

---

### REV-MAR-B4 — Qdrant vs ChromaDB Inconsistency

| Field | Value |
|-------|-------|
| **Problem** | Architecture (slides 4, 8) specified **Qdrant** for vector storage. README.md said ChromaDB. Implementation used ChromaDB (`chroma_data/` dirs, `ChromaDBConnection` imports). Yet the Semantic Memory layer referenced Qdrant (`qdrant-client` in requirements). Three sources, three positions. |
| **Decision** | Standardize on **Qdrant** as authoritative vector store. ChromaDB retained only as an in-memory ephemeral backend for the Sandbox feature. |
| **Sprint** | (Sprint 7) |
| **Status** | ✅ **FIXED** |
| **Files** | All `chroma_*` references removed from production paths; `Qdrant` adopted everywhere. |

---

### REV-MAR-B5 — `get_struct_definition` → `get_type_definition` Rename + Scope Expansion

| Field | Value |
|-------|-------|
| **Problem** | Architecture spec showed `get_type_definition` covering structs, enums, typedefs, and macros. Implementation had `get_struct_definition` (struct-only). Spec broadened scope; impl was narrower. |
| **Decision** | Rename and expand to handle enums/typedefs/macros per spec. |
| **Sprint** | (Sprint 6) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/core/mcp_server.py`, ontology integrations. |

---

### REV-MAR-B6 — `generate_struct_initialization` → `generate_initialization_code` Rename

| Field | Value |
|-------|-------|
| **Problem** | Architecture spec named it `generate_initialization_code`. Implementation used `generate_struct_initialization` — DAs calling the spec name failed. |
| **Decision** | Rename to spec-compliant identifier. |
| **Sprint** | (Sprint 6) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-B7 — Hardcoded API Credentials in Test Scripts

| Field | Value |
|-------|-------|
| **Problem** | `Src/HybridRAG/code/KG/testapi.py` and `_explore_hierarchy.py` contained **hardcoded Jama API keys and secrets in plaintext**, committed to source control. |
| **Decision** | Move all credentials to environment variables; rotate compromised keys; add to `.gitignore`. |
| **Sprint** | (Sprint 6) |
| **Status** | ✅ **FIXED** (later reinforced by REV2-F02 / ADR-039) |
| **Files** | `Src/HybridRAG/code/KG/testapi.py`, `Src/HybridRAG/code/KG/_explore_hierarchy.py`. |

---

### REV-MAR-B8 — Per-Call `_get_querier()` Connection Leak Pattern

| Field | Value |
|-------|-------|
| **Problem** | Throughout `mcp_server.py`, `_get_querier()` created a **new Neo4j connection per tool call** with `try/finally q.close()`. Under high concurrency this thrashed connection creation/destruction and saturated the driver. |
| **Decision** | Use Neo4j driver's native connection pool (50 connections) via a singleton manager. |
| **Sprint** | (Sprint 7) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/storage/neo4j_manager.py` (singleton). |

---

### REV-MAR-B9 — Feedback / Review Stores in In-Memory `dict`

| Field | Value |
|-------|-------|
| **Problem** | `_feedbacks` and `_reviews` were plain Python dicts. **All feedback data, review decisions, and learning metrics were lost on server restart** — unacceptable for ASPICE. |
| **Decision** | Persist to PostgreSQL (`feedback_records`, `review_evidence`, `failure_patterns` tables). |
| **Sprint** | (Sprint 8–9) |
| **Status** | ✅ **FIXED** |
| **Files** | `src/Observability/postgres_schema.py`, `src/ReviewGate/confidence.py`. |

---

### REV-MAR-B10 — Tool Count Discrepancy (Spec Drift)

| Field | Value |
|-------|-------|
| **Problem** | Slide 12 said "50 tools: 27 Public, 14 Developer, 9 Admin." Slide 13 said "27 Public." Speaker notes said "29 Public." `append_conversation` mentioned in notes but not registered. Three different numbers across spec slides. |
| **Decision** | Single source of truth: `tool_tiers.py` and `resource_mcp_tool.yaml`. Live count exposed via `python -c "from mcp.core.mcp_server import mcp; print(len(mcp._tools))"`. |
| **Sprint** | (Sprint 8 reconciliation; Review3/4 final alignment to **62**) |
| **Status** | ✅ **FIXED** |
| **Files** | `mcp/auth/tool_tiers.py`, `mcp/auth/policies/resource_mcp_tool.yaml`, all tests, all docs. |

---

### REV-MAR-Stubs — 18 of 50 Tools Returning PLACEHOLDER

| Field | Value |
|-------|-------|
| **Problem** | At March 2026 baseline, 18 of 50 tools returned static/mock data or logged `PLACEHOLDER`. Affected tools: `cache_get`, `cache_stats`, `cache_invalidate_module`, `submit_human_feedback`, `get_learning_metrics`, `get_failure_patterns`, `process_results`, `validate_entity`, `get_ontology_compliance`, `detect_communities`, `visualize_subgraph`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository`, plus partials on `ingest_file`, `build_context`, `complete_review`, `override_review_routing`. |
| **Decision** | Wire each stub to its real backend over Sprints 5–10. Track per-tool implementation status. |
| **Sprint** | 5–10 |
| **Status** | ✅ **FIXED** (all 18 wired by Sprint 10) |

---

### REV-MAR-Smart-Cache — "Smart Cache" Was Plain Python `dicts`

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slide 6) specified two-tier cache: LRU (exact match, 2500× faster, disk-persisted) + Semantic Cache (Redis-based, cosine ≥ 0.95, 40× faster), targeting 60% hit rate. **Implementation: two plain Python dicts**. No Redis. No embedding-based similarity. No disk persistence. No hit-rate tracking. The performance targets were unachievable. |
| **Decision** | Rebuild as 3-tier (Sprint 9): LRU → FAISS L1 → RediSearch L2 (feature-flagged). Real hit-rate tracking via Prometheus. |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** (overlaps with PERF-05) |
| **Files** | `src/Configuration/cache_service.py`. |

---

### REV-MAR-Memory-Librarian — Memory Layer "Librarian" Selection Logic Gap

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slides 7, 24) specified the Memory Layer as the "librarian" that selects the most relevant subset from 100+ RAG results to fit within 8000 tokens, using NodeSets and auto-optimization (pruning, consolidation, prioritization). **Implementation:** WorkingMemoryManager handled session CRUD well, but `build_context` selection logic was basic. Auto-optimization algorithms not implemented. Semantic Memory PatternStore/PatternIndex existed but were not wired into MCP `build_context`. |
| **Decision** | Sprint 8 ContextBuilder with 10-slot algorithm + redistribution; PatternStore/PatternIndex wired into FeedbackSink (Sprint 9). |
| **Sprint** | 8–9 |
| **Status** | ✅ **FIXED** (overlaps with GAP-A09 dynamic budget) |

---

### REV-MAR-Learning-Loop — Continuous Learning Loop "Broken Chain"

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slide 26) specified: APPROVE stores approved patterns back into KG; REJECT records failure patterns; MODIFY captures corrections; all feed back into RAG retrieval, failure-pattern matching, and confidence-formula adjustment. **Implementation:** `submit_human_feedback` wrote to in-memory dict. On APPROVE, no `ApprovedPattern` node was created in Neo4j; no vector embedding stored. On REJECT, no `FailurePattern` node was created. Confidence-formula weights hardcoded and never adjusted. **The learning loop didn't exist.** |
| **Decision** | Sprint 9: PatternStore (Neo4j) + PatternIndex (Qdrant) writes on APPROVE; `failure_patterns` table writes on REJECT; ConfidenceCalculator signals include `pattern_match` (Qdrant cosine ≥ 0.8) and `no_failure_match` (Qdrant rejected ≥ 0.75). |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/ReviewGate/confidence.py`, `src/MemoryLayer/memory/pattern_store.py`. |

---

### REV-MAR-Ingest-MCP-Wiring — Ingestion MCP Tools Returned PLACEHOLDER While Real Logic Lived in Standalone Scripts

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slide 18) specified 4 Admin ingestion tools: `ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository`. **Implementation:** all 4 returned PLACEHOLDER JSON. The actual ingestion logic existed in standalone scripts (`build_knowledge_graph.py`, `swa_ingestion.py`, `rag_ingestion.py`) but was not invocable via MCP. |
| **Decision** | Wire each MCP tool to invoke the corresponding script's main entry point, returning structured progress tracked via `IngestionJobTracker`. |
| **Sprint** | 8–9 |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-MultiTenant — Multi-Tenant Isolation Incomplete

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slides 27, 30) specified `workspace_id` parameter on all tools, dedicated Neo4j + Qdrant per product, workspace-scoped cache and session data. **Implementation:** `workspace_id` accepted by most tools and passed as `profile`. Neo4j supported profiles (`illd`, `mcal` databases). But ChromaDB collections were workspace-prefixed only sometimes. In-memory caches were global, not workspace-scoped. |
| **Decision** | (a) Standardize `workspace_id` parameter across ALL tools; (b) Dedicated Neo4j databases (`illd`, `mcal`) and Qdrant collections per workspace; (c) Workspace-scoped cache keys (query signature includes workspace); (d) Cerbos `aice_workspace_roles` derived role enforces tool-level isolation. |
| **Sprint** | 7–8 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/HybridRAG/storage/neo4j_manager.py` (`_db_for_workspace()`), `mcp/auth/policies/derived_roles.yaml`. |

---

### REV-MAR-ASPICE — ASPICE Observability Partial

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slide 9) specified complete audit trail: prompt logging, response archive, model registry (MLflow), review-evidence store. **Implementation:** prompt logging not implemented; no MLflow integration; review evidence in-memory only. `session_end` had `persist_audit` parameter but the SQLite provenance store was not wired. |
| **Decision** | Sprint 9–10: 7-table PostgreSQL schema (audit_logs, response_archive, review_evidence, feedback_records, failure_patterns, ingestion_jobs, sessions). MLflow deferred — Anthropic's GAP-03 (model version tracking) covers the equivalent need without MLflow infrastructure. |
| **Sprint** | 9–10 (audit schema); GAP-03 (Sprint 11) for model version. |
| **Status** | ✅ **FIXED** (overlaps with Governance GAP-01, GAP-03) |

---

### REV-MAR-Pagination — `total_count_exact` Field Not Returned

| Field | Value |
|-------|-------|
| **Problem** | Architecture (Slide 32) specified `total_count_exact` in hybrid search results for proper pagination. Not returned by any tool. |
| **Decision** | Add `total_count_exact` to `search_database` response envelope. |
| **Sprint** | (Sprint 8) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-HealthCheck — Redis Marked `not_configured` Permanently

| Field | Value |
|-------|-------|
| **Problem** | `health_check` returned Redis status as `not_configured` permanently because Redis was not integrated. False signal — admins ignored the indicator entirely. |
| **Decision** | Integrate Redis (working memory backend, broker for Celery alternatives, RediSearch L2 cache). Health-check returns real status. |
| **Sprint** | 7 |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-Communities — `detect_communities` (Louvain) Returned Placeholder

| Field | Value |
|-------|-------|
| **Problem** | `detect_communities` was a stub. Louvain / Label Propagation algorithms (via Neo4j GDS plugin) not integrated. |
| **Decision** | Wire to Neo4j GDS plugin's `gds.louvain.stream` procedure. |
| **Sprint** | (Sprint 8) |
| **Status** | ✅ **FIXED** (low priority but completed) |

---

### REV-MAR-Visualize — `visualize_subgraph` (pyvis) Returned Placeholder

| Field | Value |
|-------|-------|
| **Problem** | `visualize_subgraph` was a stub. pyvis HTML rendering not wired. |
| **Decision** | Wire to pyvis with HTML output written to a configurable directory; return file path. |
| **Sprint** | (Sprint 8) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-Validate-Entity — `validate_entity`, `get_ontology_compliance` Stubs

| Field | Value |
|-------|-------|
| **Problem** | Both tools logged PLACEHOLDER — no validation logic against ontology.yaml strictness levels. |
| **Decision** | Wire to OntologyLoader's strictness-level validation; return per-property pass/fail results. |
| **Sprint** | (Sprint 8) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-ProcessResults — `process_results` VP/Polyspace/JUnit Not Wired

| Field | Value |
|-------|-------|
| **Problem** | `process_results` (Cat 8) was a stub. VP execution results, Polyspace findings (Bugfinder + CodeProver), and JUnit test results could not be ingested back into the KG. The "Result" leg of the V-Model traceability chain (Req → Arch → Code → Test → **Result**) was missing. |
| **Decision** | Implement `ResultProcessors` with PolyspaceParser (CSV/XML), JUnit parser, GCOV/LCOV coverage parser. Batch UNWIND merge into Neo4j as `TestResult` nodes with `BELONGS_TO_MODULE` relationships. |
| **Sprint** | 9 |
| **Status** | ✅ **FIXED** |
| **Files** | `src/ReviewGate/result_processors.py`. |

---

### REV-MAR-Duplicate-File — `mcp_server__copy.py` Duplicate File

| Field | Value |
|-------|-------|
| **Problem** | A stale `mcp_server__copy.py` lived alongside `mcp_server.py` — two sources of truth for the MCP server, leading to drift and confusion. |
| **Decision** | Delete duplicate. Maintain single source of truth. |
| **Sprint** | (Sprint 6) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-Tests — No MCP Server Integration Tests

| Field | Value |
|-------|-------|
| **Problem** | At March 2026, only the MemoryLayer had unit tests (`test_working_memory.py`). **No tests existed for the MCP tool handlers themselves.** |
| **Decision** | Sprint-based test suites: `test_sprint3.py`, `test_sprint5.py`, `test_sprint6.py`, ... `test_gap_implementations.py` (73 integration tests for GAP features), `test_pipeline_wiring.py` (e2e wiring validation), plus `test_rate_limiter.py`, `test_otel_tracing.py`, etc. |
| **Sprint** | 5+ |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-CI — No CI/CD Pipeline Configuration

| Field | Value |
|-------|-------|
| **Problem** | Dockerfile and K8s `deployment.yaml` existed but no GitHub Actions / Jenkins pipeline was defined. |
| **Decision** | Add Jenkinsfile / GitHub Actions workflow with build → lint (`ruff`) → unit + integration tests → Docker build → registry push. |
| **Status** | 🟡 **PARTIAL** — Docker/K8s ready; pipeline definition tracked separately by DevOps team. |

---

### REV-MAR-Logging — No Correlation IDs (`session_id`, `request_id`) for Distributed Tracing

| Field | Value |
|-------|-------|
| **Problem** | Logging used `logging.getLogger(__name__)` but lacked `session_id` / `request_id` correlation IDs needed for distributed tracing across DA → MCP → backends. |
| **Decision** | Add structured logging context (Python `logging.Filter` + `contextvars`) carrying `session_id` and OpenTelemetry trace IDs through the request lifecycle. |
| **Sprint** | 25 (with ADR-036 OTel adoption) |
| **Status** | ✅ **FIXED** |

---

### REV-MAR-Telemetry-DAs — DA Telemetry Not Captured

| Field | Value |
|-------|-------|
| **Problem** | DA-level metrics (which DA is most active, which has highest rejection rate) not captured. |
| **Decision** | Add `assistant_name` label to all Prometheus metrics. Drives Governance GAP-10 cross-DA dashboard. |
| **Status** | 🆕 **OPEN / Planned** (overlaps with Governance GAP-10) |

---

## Family I — Ephemeral Sandbox Feature (Sprint 4–5)

The **Ephemeral Sandbox** was an entirely new memory-layer feature designed and shipped in Sprint 4–5. It is not a "gap" in the corrective sense but a **substantive feature gap** that was identified, designed, and implemented. Capturing here for traceability.

---

### EPH-01 — Inability to Experiment with Documents Without Permanent Ingestion

| Field | Value |
|-------|-------|
| **Problem** | Using AICE with a new document required full ingestion: PDF → Markdown → Chunks → Qdrant + Neo4j. **Three friction points:** (1) commitment overhead — ingestion writes permanently, polluting production KG; (2) cleanup burden — no automated way to remove experimental data; (3) iteration speed — developers drafting new SWA/SWUD docs needed to query their drafts alongside production KG before finalizing. |
| **Decision** | Add a session-scoped, TTL-managed temporary KG + vector index. Tied to Working Memory session lifecycle (1 hr TTL, auto-cleanup). Transparent integration with the existing query pipeline. **Same parsers reused** — no new parsing code. **Same ontology** — same node and relationship types. |
| **Sprint** | 4–5 |
| **Status** | ✅ **FIXED / DELIVERED** |
| **Files** | `src/MemoryLayer/memory/ephemeral_sandbox.py` and submodules: `sandbox_manager.py`, `ephemeral_graph.py` (NetworkX), `ephemeral_vectors.py` (ChromaDB in-memory), `sandbox_ingester.py`, `sandbox_querier.py`. |
| **Tools added** | 4 new MCP tools in Cat 6 (Memory & Context): `sandbox_upload`, `sandbox_query`, `sandbox_status`, `sandbox_clear`. Tool count went 52 → 56. |

**Architectural choices captured as decisions:**
- **NetworkX over Neo4j** for the ephemeral graph (zero infrastructure, instant cleanup, sub-second queries at sandbox scale, no orphan risk).
- **ChromaDB ephemeral mode over Qdrant** for ephemeral vectors (true RAM-only mode disappears on dereference; same Embedder reused for embedding-space consistency).
- **RLM sandbox-aware planning**: 4th complexity signal added to RLM trigger heuristic (`sandbox_active=true` counts toward the 2-of-N rule). Planning prompt extended with sandbox file manifest.
- **Score fusion bias**: configurable `ephemeral_boost` (default +0.05) gives experimental content slight priority during active experimentation.

**Risks tracked:**
- Memory exhaustion — hard cap 50 MB per session, 20-file limit per sandbox.
- Embedding-model mismatch — single source of truth via shared `Embedder` instance.
- Score-fusion bias toward small ephemeral collections — boost factor configurable; can be set to 0 or negative to deprioritize.
- Orphaned sandboxes on process crash — in-memory stores GC'd; Redis-backed sessions track `sandbox_active` flag for restart recovery.

---

### EPH-02 — Promote-to-Permanent Path

| Field | Value |
|-------|-------|
| **Problem** | After experimenting in a sandbox, a user may want to commit the data permanently — but no path exists. |
| **Decision** | New MCP tool `sandbox_promote(session_id, target='permanent')` that internally invokes `ingest_documents()` (Qdrant) and `ingest_knowledge_graph()` (Neo4j). |
| **Sprint** | (Phase 2 — deferred) |
| **Status** | 🆕 **OPEN / Planned** |
| **Re-evaluation trigger** | User feedback from sandbox usage indicating need; effort estimated 1–2 days once Phase 1 production usage stabilizes. |

---

## Family J — Research-Driven Library Upgrade Recommendations (Research_Gap_Analysis_Upgrade_Plan)

The Research Gap Analysis identified **specific library upgrades** for the existing GAP-A implementations. Many became ADRs (already in master); the rest are listed here.

| Research-ID | Existing GAP | Recommendation | Status |
|------------|--------------|----------------|--------|
| RA-01 | GAP-A01 | Replace CrossEncoder with FlashRank | ✅ FIXED via **ADR-038** |
| RA-02 | GAP-A02 | Replace custom SSE with **MCP SDK StreamableHTTP** + invariantlabs reference | 🟡 **PARTIAL** — current implementation uses custom `StreamingToolWrapper`; research recommends the official MCP SDK transport. Captured as **OPEN** for Sprint 12 follow-up. |
| RA-03 | GAP-A03 | Add **optional** LLM expansion for complex queries only (NEUIR/ExpandR pattern) | 🆕 **OPEN** — keep rule-based fast path; add LLM expansion guarded by `complexity == 'complex'`. ~10% of queries. |
| RA-04 | GAP-A04 | Replace extractive scorer with **microsoft/LongLLMLingua** perplexity-based pruning | ✅ FIXED via **ADR-025** (LLMLingua adopted) |
| RA-05 | GAP-A05 | Use **clokep/celery-batches** explicit pattern | 🔄 **DEFERRED** — superseded by **ADR-037** (asyncio.TaskGroup, no Celery) |
| RA-06 | GAP-A07 | Add **CRAG** + **Self-RAG** reflection-token pattern | ✅ FIXED via **ADR-029** |
| RA-07 | GAP-A08 | Replace custom judge with **confident-ai/deepeval** framework | ✅ FIXED via **ADR-027** |
| RA-08 | GAP-A11 | Evaluate **HKUDS/RAG-Anything** for multimodal ingestion (text + tables + diagrams + formulas) | 🆕 **OPEN** — Sprint 19+ — current Tesseract-only OCR is text-only; multimodal would handle hardware schematics |
| RA-09 | GAP-A13 | Add **amazon-science/RAGChecker** semantic entailment alongside text-overlap | ✅ FIXED via **ADR-031** |
| RA-10 | GAP-A14 | Validation: matches research recommendation exactly | ✅ VALIDATED — no changes needed |

### Research-Driven New Automotive Features (Beyond Original 15-Gap Scope)

These four were identified as **net-new features** in the research, partially overlap with existing decisions.

| Feature | Status | ADR | Notes |
|--------|--------|-----|-------|
| **Automated Embedded C Unit Test Generation** | 🆕 **OPEN** — Sprint 18–19 | (No ADR yet) | Enhance GEST DA with KG-backed dependency resolution. References: ZJU-ACES-ISE/ChatUniTest, githubnext/testpilot, QuantumLeaps/Embedded-Test, yasinhajilou/cunit-test-generator. Effort: 13 SP. Closed-loop: generate → compile against iLLD → parse errors → iterate. |
| **LLM-Assisted MISRA C Remediation** | 🟡 **PARTIAL** — Sprint 19–20 (full feature) | **ADR-042** (auth fixed; engine fix); MISRA matrix queries KG dynamically | `remediate_misra_violation` tool unblocked by ADR-042 (was auth-denied). Full closed-loop (parse → retrieve rule → generate fix → re-check → Polarion export) is Sprint 19–20. References: naivesystems/analyze, mpdelbuono/wildcop. |
| **AI-Assisted FMEA / FMEDA** | 🔄 **DEFERRED** | **ADR-041** (FMEA full feature deferred) | Stub registered. Full multi-agent recursion + spreadsheet export is Sprint 20–22 (21 SP). References: YuchenXia/LLMRiskAnalyzer, Gueni/FASTER. Trigger: safety-team workflow definition. |
| **Requirements-to-Formal-Verification (CBMC)** | 🔄 **DEFERRED** | **ADR-041** (CBMC full feature deferred) | CBMC parsing fix shipped (low effort). Full closed-loop (NL → assertion → CBMC → feedback) deferred. Effort: 21 SP. References: cprover/cbmc, SpecVerify. Trigger: CBMC binary in CI/CD; 4–16 GB RAM allocation accepted. |

---

## Family K — Resource & Infrastructure Impact (Performance_Impact_Complexity_Analysis)

Capturing **systemic resource impacts** — not individual gaps but cross-cutting impacts that affect many gaps and need to be tracked as a deployment-readiness concern.

---

### RES-01 — Docker Image Size Trajectory

| State | Image Size |
|-------|-----------|
| Pre-GAP (PyTorch + sentence-transformers + ML stack) | **~2.5 GB** |
| Post-ADR-038 (FlashRank ONNX, no PyTorch) + LLMLingua bert-base + DeepEval | **~800 MB – 1.0 GB** |
| **Net change** | **~60% reduction** |

✅ **FIXED** — primary driver was ADR-038 (FlashRank replaces PyTorch).

---

### RES-02 — Runtime RAM Footprint

| Component | Delta |
|-----------|-------|
| Remove PyTorch | **−350 MB** |
| Add LLMLingua (llmlingua-2-bert-base) | **+300 MB** |
| Add RAGChecker NLI model | **+200 MB** |
| **Net change** | **+100–200 MB** (modest increase) |

Total runtime: ~700–900 MB (vs ~500–700 MB baseline). Fits comfortably in container memory budget.

---

### RES-03 — GPT4IFX Token Cost Trajectory

| Driver | Token Delta |
|--------|------------|
| LLM Query Expansion (10% of queries) | +100 tokens × 10% = +10 tokens average |
| LongLLMLingua compression saves tokens | **−25–30%** of context tokens |
| LLM-as-Judge (30% of queries) | +500 tokens × 30% = +150 average |
| Context Refinement (10% of queries) | +500 tokens × 10% = +50 average |
| Few-shot injection | +200–500 tokens |
| Citation verification (claim extraction) | +200–500 tokens |
| **Net daily cost** | **+60–140% increase** vs baseline |

🆕 **TRACKED** — primary ongoing cost. Mitigated by dynamic budget (saves 20–30% on simple queries) and AUTO-route bypass for 70% of queries.

---

### RES-04 — Uvicorn Worker Count for Concurrent Streaming

| Field | Value |
|-------|-------|
| **Problem** | When streaming is active and 21 DAs each hold an open stream, the connection-pool pressure on a 4-worker uvicorn deployment causes head-of-line blocking. Long-lived SSE streams need at least **25 concurrent workers** for 21 DAs + admin headroom. |
| **Decision** | Increase `WEB_CONCURRENCY` from 4 → 8–12 (configurable). Monitor with Prometheus `active_streams` gauge. |
| **Sprint** | 25 (overlaps with ADR-043 Gunicorn) |
| **Status** | 🟡 **PARTIAL** — Gunicorn supports config; default 4 workers needs raising for streaming scale-out. |

---

### RES-05 — CPU Core Requirement Increase

| Component | Cores |
|-----------|-------|
| Baseline | 2–4 cores |
| With LLMLingua + FlashRank (CPU-bound ONNX) | 3–5 cores |
| **Net change** | **+1 core** |

🆕 **TRACKED** — deployment manifests should specify CPU requests/limits accordingly.

---

### RES-06 — Cumulative Pipeline Latency

The fully-upgraded pipeline has approximately the **same weighted-average latency** as the current pipeline, because batch graph savings (−200–550 ms) offset the new stages (+20 ms reranking + 100 ms compression). Complex queries are 200–500 ms slower but produce dramatically better results. Simple queries may actually be **faster** due to the 4 K token budget reducing context-assembly work.

| Query Class | Distribution | Current | Upgraded | Net |
|-------------|-------------:|--------:|---------:|----:|
| Simple | 50% | 120–300 ms | 150–250 ms | +0 to −50 ms |
| Medium | 40% | 150–400 ms | 200–400 ms | +0 to +50 ms |
| Complex | 10% | 300–1000 ms | 500–1500 ms | +200 to +500 ms |
| **Weighted avg** | — | **160–400 ms** | **200–400 ms** | **+0 to +40 ms** |

---

## Family L — Original Chunking Strategy Gaps (Pre-Sprint Architecture)

These were **original architectural gaps** in the pre-Sprint-1 baseline `AutomotiveSmartChunker`. They are documented for full historical traceability — all were closed by Sprint 6 / ADR-015.

| Original Gap | Resolution | Status |
|--------------|-----------|--------|
| No PDF parser — could not ingest AUTOSAR SWS / EXP / HW manuals | Adopted PyMuPDF (`fitz`) for primary, evaluated Docling/PyMuPDF4LLM | ✅ FIXED |
| No DOCX parser — could not ingest HW manuals (1000+ pages) | Adopted `python-docx` + `mammoth` | ✅ FIXED |
| 2000-token limit insufficient for AUTOSAR SWS API tables (which routinely hit 2500–3500 tokens) | Document-type-specific configs (4000 max for SWS / HW manual; 2000 for EXP) per ADR-015 | ✅ FIXED |
| No table/figure extraction — AUTOSAR specs are 40–60% tables | PDF pipeline extracts tables as atomic markdown-formatted chunks; preserved with `[SWS_xxx_yyyyy]` anchors | ✅ FIXED |
| No `.arxml` parser | `arxml_parser` integrated into IngestionPipeline (parses ARXML containers/parameters with DEFINITION_REF cross-refs) | ✅ FIXED |
| No `[SWS_xxx]` requirement-tag extraction → no traceability anchors in chunks | Tag extraction wired in `c_parser.py` and PDF pipeline; tags become TRACES_TO relationships in KG | ✅ FIXED |
| No cross-reference linking across chunks | Linker phase in IngestionPipeline creates IMPLEMENTS / TRACES_TO / TESTS / CALLS / USES_REGISTER / PART_OF relationships per AICE-ING-044..049 | ✅ FIXED |
| Fixed-size token chunking would destroy AUTOSAR table structures | **Rejected** — adopted hierarchical + structure-aware chunking (ADR-015) with `table_atomic=true`, `code_block_atomic=true` | ✅ FIXED |
| Long narrative HW-manual sections (>4000 tokens) without sub-headings | Semantic-fallback chunking via `all-MiniLM-L6-v2` embedding similarity (configurable threshold 0.75) | ✅ FIXED |
| No metadata enrichment — chunks were bare text | `AutomotiveDocumentChunkMetadata` (chunk_id, document_type, page_numbers, section_path, sws_requirement_tags, autosar_version, module_name, content_type, table/code/figure flags, parent/sibling/cross-ref IDs) | ✅ FIXED |

> **Note on PDF parser choice:** Research recommended evaluating Docling (MIT, IBM, "best overall") vs PyMuPDF4LLM (AGPL, fast). AICE adopted PyMuPDF as primary for license clarity (MIT-compatible) and speed; Docling/LlamaParse evaluation deferred to Sprint 19+ if multimodal needs emerge.

---

## Family M — Additional Deferred Items (`diff_feature_list.md`)

The diff feature list documents the `further_enhancements_for_gaps` branch (87 files, +12,907 / −2,562 lines, 25 commits). It surfaces three deferred items that aren't otherwise in the master:

| ID | Title | Reason Deferred | Status |
|----|-------|-----------------|--------|
| DIFF-M06 | **CacheService → search pipeline integration** | Architectural decision pending: cache-aside vs cache-through semantics | 🆕 **OPEN** (overlaps with master Family G's deferred item) |
| DIFF-H07 | **RelevanceJudge LLM wiring** | Needs deeper refactor — current `set_llm_fn()` approach is a one-shot inject, not a context-managed lifecycle | 🆕 **OPEN** |
| DIFF-H08 | **Threshold scale normalization (DeepEval)** | Needs DeepEval version-compatibility check — DeepEval's `ContextualRelevancyMetric` returns 0–1, our internal threshold uses 0–10 (1–10 in some places) | 🆕 **OPEN** |
| DIFF-H15 | **TLS inter-service** | Infrastructure team responsibility — Neo4j Bolt+TLS, Qdrant gRPC+TLS, Redis TLS — currently all on plaintext internal networks | 🆕 **OPEN** (deployment hardening) |

---

## Family N — DocJockey Features Deliberately NOT Replicated

Several DocJockey features identified in the comparison are **architecturally inappropriate for AICE** (different paradigm, different users, different scale). Capturing these as explicit "won't fix" items so they don't reappear as gaps:

| DocJockey Feature | Why Not in AICE | Disposition |
|-------------------|-----------------|-------------|
| **Multi-Model Ensemble** (parallel generation from 2–3 models with judge-based selection; 15% improvement) | AICE delegates generation to DAs (CIA, GEST, etc.). Each DA picks its own model (GitHub Copilot for CIA; GPT4IFX for REVA). Multi-model ensemble at AICE layer adds nothing — DAs are independent. | ❌ **REJECTED** (architectural mismatch) |
| **Per-sentence confidence granularity** | AICE's deterministic 13-signal `ConfidenceCalculator` operates at the response level (matched to DA output unit). Per-sentence scoring would require LLM-as-Judge per sentence (cost/latency prohibitive). The Citation Verifier (GAP-A13) approximates per-claim granularity at lower cost. | ❌ **REJECTED** (cost/value tradeoff) |
| **Source-level Permission Sync** (DocJockey 2-hour sync from Confluence, JIRA, etc.) | AICE workspace data is curated through Sprint-based ingestion, not real-time source sync. Permissions enforced via Cerbos at tool dispatch (workspace_id + role), not source-derived. | ❌ **REJECTED** (different data model) |
| **End-User Web UI** | AICE is a tool platform for AI agents, not an end-user app. UI lives in the DAs (e.g., GEST has a VS Code extension). | ❌ **REJECTED** (out of scope by design) |
| **Apache Tika ingestion** | PyMuPDF + python-docx + custom parsers cover all formats AICE needs (C/H, RST, PlantUML, EA, PDF, DOCX, XLSX, ARXML). Tika is heavier and offers no specific advantage for the AURIX domain. | ❌ **REJECTED** (no AURIX-specific advantage) |
| **In-house LLMs (vLLM)** | GPT4IFX is the Infineon-mandated internal LLM for AICE infrastructure paths (RLM planning, PDF extraction). DAs may use any approved model. Self-hosting vLLM would duplicate GPT4IFX without benefit. | ❌ **REJECTED** (organizational policy) |
| **DocJockey-style Marketplace / Custom Skills** (S2S-Copilot pattern) | AICE's MCP tool model already provides composable building blocks. A "marketplace" abstraction is unnecessary for a single-org internal platform. | ❌ **REJECTED** (already covered by MCP composition) |

---

## Updated Master Roll-Up (After Supplement)

| Family | Total Items | Fixed | Deferred / Rejected | Open / Planned |
|--------|------------:|------:|--------------------:|---------------:|
| A. DocJockey GAP-A series | 15 | 13 | 2 | 0 |
| B. Performance bottlenecks | 7 + 2 aux | 6 + 2 aux | 0 | 1 |
| C. AI Governance gaps | 11 | 8 | 0 | 3 |
| D. Review1 findings | ~9 | 9 | 0 | 0 |
| E. Review2 findings (F-codes) | 6 | 6 | 0 | 0 |
| F. Review3 findings (C/H/M/L) | ~21 | ~17 | 2 | 2 |
| G. Review4 findings | 11 + 1 deferred | 11 | 0 | 1 |
| **H. March 2026 audit (B-codes)** | **~25** | **~24** | **0** | **1 (CI/CD pipeline)** |
| **I. Ephemeral Sandbox** | **2** | **1** | **0** | **1 (sandbox_promote)** |
| **J. Research-driven upgrades** | **10 reviews + 4 new** | **5** | **2** | **6** |
| **K. Resource impact (RES-01..06)** | **6** | **2** | **0** | **4 (tracked)** |
| **L. Original chunking gaps** | **10** | **10** | **0** | **0** |
| **M. Additional deferrals** | **4** | **0** | **0** | **4** |
| **N. DocJockey "won't fix"** | **7** | **0** | **7** | **0** |
| **TOTAL** | **~150** | **~115** | **~13** | **~22** |

> The supplement nearly doubles the catalogued gap inventory — most additions are **historical context** (Family H, L) or **already-decided non-replications** (Family N), so the open-work list grows only modestly.

---

## Closing Notes on the Supplement

1. **Family H (March 2026 audit)** is the foundational story. Most B-codes were resolved over Sprints 5–10 before the GAP-A program even started. Documenting them gives the master list complete provenance from "first audit" to "Review4 final."
2. **Family I (Ephemeral Sandbox)** is a feature, not a defect — but the design document is a substantive contribution worth tracking alongside gap closures.
3. **Family J (Research-driven)** captures the analytical lineage of decisions like ADR-038 (FlashRank), ADR-027 (DeepEval), ADR-031 (RAGChecker). The MCP SDK StreamableHTTP item (RA-02) is a noteworthy still-open follow-up.
4. **Family K (Resource Impact)** belongs alongside the gap list because deployment-readiness gates depend on these numbers.
5. **Family L (Original Chunking)** completes the "from day zero" picture — every architectural gap from the pre-baseline state is now traceable.
6. **Family N (Won't Fix)** is forward-looking governance: the next reviewer who reads this won't re-flag these features as missing.

*End of supplemental update.*

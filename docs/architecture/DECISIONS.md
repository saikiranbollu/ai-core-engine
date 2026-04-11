# Architecture Decision Records (ADRs)

**AI Core Engine v2.1.0**

> Each ADR documents a significant architecture or technology choice. Status: **Adopted** = implemented in the codebase, **Deferred** = evaluated but not yet adopted, **Rejected** = evaluated and not adopted.

---

## Table of Contents

- [ADR-001: Knowledge Graph — Neo4j](#adr-001-knowledge-graph--neo4j)
- [ADR-002: Vector Store — Qdrant with 384-dim Embeddings](#adr-002-vector-store--qdrant-with-384-dim-embeddings)
- [ADR-003: Hybrid RAG with Reciprocal Rank Fusion](#adr-003-hybrid-rag-with-reciprocal-rank-fusion)
- [ADR-004: Deterministic Confidence Scoring (not LLM-based)](#adr-004-deterministic-confidence-scoring-not-llm-based)
- [ADR-005: Cerbos PDP for RBAC Authorization](#adr-005-cerbos-pdp-for-rbac-authorization)
- [ADR-006: Session Backend Strategy Pattern (Redis / In-Memory)](#adr-006-session-backend-strategy-pattern-redis--in-memory)
- [ADR-007: Two-Tier Caching (LRU + Semantic)](#adr-007-two-tier-caching-lru--semantic)
- [ADR-008: PostgreSQL for Administrative Data](#adr-008-postgresql-for-administrative-data)
- [ADR-009: RLM as Internal Context Orchestrator](#adr-009-rlm-as-internal-context-orchestrator)
- [ADR-010: NodeSet Anchor Pattern for Module Isolation](#adr-010-nodeset-anchor-pattern-for-module-isolation)
- [ADR-011: Token-Budget Context Assembly with 10 Slots](#adr-011-token-budget-context-assembly-with-10-slots)
- [ADR-012: MCP Protocol with streamable-http Transport](#adr-012-mcp-protocol-with-streamable-http-transport)
- [ADR-013: Lazy Singleton Service Factories](#adr-013-lazy-singleton-service-factories)
- [ADR-014: Docker Compose with 5 Services](#adr-014-docker-compose-with-5-services)
- [ADR-015: Structure-Aware Chunking for AUTOSAR Documents](#adr-015-structure-aware-chunking-for-autosar-documents)
- [ADR-016: Celery Task Queue — Deferred](#adr-016-celery-task-queue--deferred)
- [ADR-017: MinIO / S3 Object Storage — Deferred](#adr-017-minio--s3-object-storage--deferred)
- [ADR-018: Cross-Encoder Reranking — Deferred](#adr-018-cross-encoder-reranking--deferred)
- [ADR-019: Local On-Premise Deployment](#adr-019-local-on-premise-deployment)
- [ADR-020: Keycloak OAuth — Deferred](#adr-020-keycloak-oauth--deferred)
- [ADR-021: Prometheus + Grafana Observability](#adr-021-prometheus--grafana-observability)

---

## ADR-001: Knowledge Graph — Neo4j

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 1 |
| **Context** | Engineering data is inherently relational: functions call other functions, structs contain fields, requirements trace to test cases, registers belong to peripherals. A relational DB would require expensive JOINs; a document store would lose the traversal capabilities. |
| **Decision** | Use **Neo4j 5.26 Community** as the primary knowledge store, with APOC and GDS plugins for advanced graph algorithms. |
| **Rationale** | (1) Native graph traversal for dependency chains and traceability — one `MATCH` path query vs. recursive SQL CTEs. (2) Cypher is expressive for multi-hop patterns like `Requirement → IMPLEMENTS → Function → ACCESSES → Register`. (3) APOC provides import/export utilities; GDS provides centrality, community detection, and shortest-path algorithms needed for dependency analysis. (4) Label-property indexing supports fast exact lookups alongside traversal. |
| **Trade-offs** | No ACID transactions across multiple databases (illd + mcal are separate Neo4j databases). Community edition lacks advanced clustering. Acceptable because AICE is a read-heavy workload with batch-write ingestion phases. |
| **Implementation** | `neo4j_manager.py` manages connection lifecycle. Dual database (`illd`, `mcal`) resolved via `_db_for_workspace()`. Write operations restricted to ingestion tools (admin tier). |

---

## ADR-002: Vector Store — Qdrant with 384-dim Embeddings

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2 |
| **Context** | Keyword search against graph properties misses semantic meaning (e.g., "how to set baud rate" won't match a function named `IfxCan_Node_initBitTiming`). Need dense vector similarity search. |
| **Decision** | Use **Qdrant v1.12.1** with **all-MiniLM-L6-v2** embeddings (384 dimensions, cosine similarity). |
| **Rationale** | (1) all-MiniLM-L6-v2 is lightweight (~80MB), fast, and runs locally without GPU — critical for on-premise deployment at Infineon. (2) 384 dimensions provide sufficient quality for domain vocabulary while keeping index size manageable. (3) Qdrant provides HNSW indexing with payload filtering (used for module-scoped searches). (4) gRPC + REST dual-protocol support. (5) UUID5 deterministic ID mapping (from node_id to Qdrant point_id) ensures idempotent upserts. |
| **Alternatives considered** | Pinecone (rejected: cloud-only, data residency concerns), ChromaDB (rejected: less mature for production), FAISS (rejected: no built-in filtering). |
| **Implementation** | `vector_store_factory.py` provides `_QdrantClientAdapter` and `_QdrantCollectionAdapter`. Collection naming follows `{workspace}_{module}_embeddings` convention via `collection_naming_unified.py`. |

---

## ADR-003: Hybrid RAG with Reciprocal Rank Fusion

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2 |
| **Context** | Pure vector search returns semantically similar content but misses structurally connected data. Pure graph search finds exact property matches and traversals but misses paraphrased queries. Need both, merged intelligently. |
| **Decision** | Combine graph search (Neo4j) and vector search (Qdrant) results using **Reciprocal Rank Fusion (RRF)** with configurable **alpha blending**. |
| **Rationale** | (1) RRF is score-agnostic — it only uses rank positions, so scores from different retrievers (Cypher relevance vs. cosine similarity) are naturally comparable. (2) Alpha parameter (0.0–1.0) lets DAs control the blend: `α=0.0` = all graph, `α=1.0` = all vector. (3) Standard `k=60` parameter from literature (Cormack et al.) prevents early-position dominance. |
| **Alternatives considered** | Linear score interpolation (implemented as `_merge_results()`, kept for backward compatibility but RRF is the primary path). Cross-encoder reranking (see ADR-018 — deferred). Maximal Marginal Relevance (considered for diversity; not needed when RRF already deduplicates by `node_id`). |
| **Formula** | `RRF_score(d) = α × 1/(k + rank_graph) + (1-α) × 1/(k + rank_vector)` where `k=60` |
| **Implementation** | `SearchService._merge_results_rrf()` in `search_service.py`. 5-stage pipeline: query analysis → graph search → entity-targeted lookup → vector search → RRF merge. |

---

## ADR-004: Deterministic Confidence Scoring (not LLM-based)

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 4 |
| **Context** | DA responses need quality gates before delivery. LLM-based evaluation is slow, expensive, and non-reproducible. Automotive domain requires deterministic, auditable, repeatable scoring for ASPICE compliance. |
| **Decision** | Use a **deterministic weighted-signal formula** for confidence scoring. Base score of 50, with positive signals (quality indicators) adding points and negative signals (risk indicators) subtracting points. |
| **Rationale** | (1) Reproducibility: same signals → same score, always. (2) Explainability: breakdown shows exactly which signals contributed. (3) Speed: no LLM call, sub-millisecond. (4) Auditability: scoring formula is versioned code, not a prompt. |
| **Thresholds** | `AUTO ≥ 80` (auto-approve, ~5 min spot check), `QUICK 50–79` (~15-20 min review), `FULL < 50` (≥1 hr deep review) |
| **Key signals** | `has_kg_context` (+30), `high_relevance` (+20), `has_dependency_order` (+20), `missing_requirements` (-30), `is_safety_critical` (-15) — 13 signals total |
| **Implementation** | `ConfidenceCalculator` in `confidence.py`. Integrates with `PatternStore` for similarity-based approved pattern matching. |

---

## ADR-005: Cerbos PDP for RBAC Authorization

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 1, hardened Sprint 6 |
| **Context** | 56 tools with different sensitivity levels (read-only search vs. data mutation vs. admin operations). Need per-request authorization, not just per-session. API keys are DA-specific. |
| **Decision** | Use **Cerbos PDP** (Policy Decision Point) running as a sidecar subprocess. 3-tier RBAC: `public` (36 tools), `developer` (14 tools), `admin` (6 tools). Derived roles for tier inheritance. |
| **Rationale** | (1) Cerbos policies are declarative YAML with CEL expressions — no code changes to update access rules. (2) Derived roles handle tier inheritance (`admin` inherits `developer` inherits `public`) cleanly. (3) Sidecar model keeps PDP co-located with the MCP server for low-latency checks. (4) Graceful fallback: if Cerbos is unavailable, `auth_middleware.py` falls back to local tier-check from `tool_tiers.py`. |
| **Alternatives considered** | OPA/Rego (more complex expression language), custom middleware only (lacks policy-as-code separation), OAuth2 scopes (too coarse for 56 tools). |
| **Implementation** | `auth_middleware.py` → `check_authorization(api_key, tool_name, workspace)`. API keys defined in `api_keys.yaml`. Policies in `mcp/auth/policies/`. Cerbos binary bundled in Docker image (multi-stage build). |

---

## ADR-006: Session Backend Strategy Pattern (Redis / In-Memory)

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2, refactored Sprint 5 |
| **Context** | DA sessions need working memory (store intermediate results, context entries). Production requires persistence across server restarts; local dev should work without Redis. |
| **Decision** | Implement **Strategy pattern**: `SessionBackend` abstract base class with `InMemoryBackend` and `RedisBackend` implementations. Backend selected at startup based on Redis availability. |
| **Rationale** | (1) DAs don't care about the backend — identical API either way. (2) Redis provides native TTL (`setex`) for automatic session expiry. (3) In-memory backend is zero-dependency for local development. (4) Adding new backends (e.g., PostgreSQL) only requires implementing 5 methods. |
| **Implementation** | `WorkingMemoryManager` in `manager.py`. `RedisBackend` uses key prefix `wm:session:`, JSON serialization, configurable TTL. Validates project/module against `ontology.yaml`. Auto-purges expired sessions on every operation. |

---

## ADR-007: Two-Tier Caching (LRU + Semantic)

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 6 |
| **Context** | DAs often ask similar questions within and across sessions. Search queries are expensive (Neo4j + Qdrant + merge). Need caching, but exact-match caching alone has low hit rate because natural language queries vary. |
| **Decision** | **Two-tier cache**: Tier 1 is an **LRU cache** (exact key match, ~2500x speedup). Tier 2 is a **semantic cache** (embedding cosine similarity ≥ configurable threshold, ~40x speedup). |
| **Rationale** | (1) LRU handles repeated identical queries (common in batch mode and CI/CD). (2) Semantic cache handles paraphrased queries ("ADC init sequence" ≈ "initialization order for ADC"). (3) Expected ~60% combined hit rate under normal workloads. (4) Semantic cache degrades gracefully — if `sentence-transformers` is unavailable, only LRU operates. (5) Automatic cache invalidation on ingestion completion prevents stale results. |
| **Configuration** | LRU: `LRU_CACHE_SIZE` env var (default `10000`), `LRU_CACHE_TTL_HOURS` env var (default `24`). Semantic: `SEMANTIC_CACHE_MAX_SIZE` (default `500`), `SEMANTIC_CACHE_THRESHOLD` env var (default `0.95`), `SEMANTIC_CACHE_TTL_DAYS` env var (default `7`), model via `ST_CACHE_MODEL` env var (default `all-MiniLM-L6-v2`). Invalid env values fall back to defaults with a warning log. |
| **Implementation** | `CacheService` in `cache_service.py`. Thread-safe `OrderedDict`-based LRU. Manual cosine similarity for semantic tier. Module-scoped invalidation via `invalidate_by_module()`. Automatic post-ingestion invalidation via `IngestionService._fire_module_ingested()` callback. Runtime config refresh via `refresh_config()` — re-reads env vars and updates TTL/size/threshold in-place without clearing cached data (MEG_SW-108). |

---

## ADR-008: PostgreSQL for Administrative Data

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 8 |
| **Context** | Audit trails, feedback records, ingestion job tracking, and review evidence need durable relational storage. Neo4j is for engineering data; Redis is volatile. Need a proper RDBMS. |
| **Decision** | Use **PostgreSQL 16** for all administrative/operational data. 7-table schema with graceful degradation (if PostgreSQL is unavailable, operations continue without persistence). |
| **Rationale** | (1) ACID transactions for audit integrity (ASPICE requirement). (2) Rich querying for analytics (e.g., failure pattern aggregation, feedback trends). (3) Industry-standard, well-supported. (4) Graceful degradation ensures the MCP server never hard-fails due to PostgreSQL issues. |
| **Tables** | `audit_logs`, `response_archive`, `review_evidence`, `feedback_records`, `failure_patterns`, `ingestion_jobs`, `sessions_meta` |
| **Alternatives considered** | SQLite (acceptable for local dev only — no concurrent writers). MongoDB (unnecessary — data is structured and relational). |
| **Implementation** | `PostgresClient` in `postgres_schema.py`. Auto-creates tables on first connection. All writes are best-effort (exceptions caught, logged, not propagated). |

---

## ADR-009: RLM as Internal Context Orchestrator

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 5 |
| **Context** | Complex DA queries require multiple search passes (e.g., "generate init code for ADC" needs API signatures, struct definitions, dependency order, register maps, and MISRA rules). A single search call cannot gather all the needed context. |
| **Decision** | Implement **RLM (Retrieval-augmented Language Model) Orchestrator** as an **internal** Core Engine capability. It sits below the MCP interface, above Hybrid RAG, and is invisible to DAs. `build_context` is the public entry point; it may internally delegate to RLM. |
| **Rationale** | (1) DAs don't need to know about multi-step retrieval — their lifecycle (`session_start → search → build_context → session_end`) stays stable. (2) RLM plans up to 6 sub-queries using an LLM (GPT4IFX), each with its own 8K token budget, then synthesizes results. (3) Task-type-aware planning: 24 `RLMTaskType` values with DA-specific sub-query templates. (4) Preview mode lets operators inspect plans before execution. |
| **Trigger heuristic** | `build_context` delegates to RLM when the query complexity warrants it (e.g., multiple function references, register-level access, safety-critical context). |
| **Alternatives considered** | Exposing RLM as a public tool (rejected: leaks implementation detail, complicates DA lifecycle). Having DAs orchestrate their own multi-step retrieval (rejected: duplicates logic across 21 DAs). |
| **Implementation** | `RLMOrchestrator` in `rlm_orchestrator.py`. `DA_TASK_MAPPING` maps 21 DAs to their task types. Two MCP tools exposed: `rlm_orchestrate` and `rlm_preview` (developer tier). |

---

## ADR-010: NodeSet Anchor Pattern for Module Isolation

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2 |
| **Context** | Multiple modules (ADC, CAN, SPI, etc.) coexist in the same Neo4j database. Queries must be scoped to a single module to prevent cross-contamination (e.g., CAN register data leaking into an ADC query). |
| **Decision** | Every module has a `:NodeSet` anchor node. All data nodes link to their anchor via `[:HAS_MODULE]`. Queries always start from the anchor and traverse downward. |
| **Rationale** | (1) Simple and enforceable — no separate databases per module needed. (2) Cross-module queries are still possible when explicitly requested (traverse from one anchor to another). (3) Compatible with Neo4j indexing (index on `NodeSet.module` + `NodeSet.project`). (4) Works naturally with Cypher pattern matching. |
| **Implementation** | `NodeSetManager` in `node_set_manager.py`, `ScopedQuery` in `scoped_query.py`. See [NODE_SETS_ARCHITECTURE.md](../NODE_SETS_ARCHITECTURE.md) for full design. |

---

## ADR-011: Token-Budget Context Assembly with 10 Slots

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2, enhanced Sprint 8 |
| **Context** | LLMs have token limits. DAs need a systematic way to assemble context that fits within a budget while prioritizing the most relevant information. Random concatenation wastes tokens on low-value content. |
| **Decision** | Implement a **slot-based token budget algorithm** with 10 named `ContextSlot` types, each with a default budget. Unused budget is redistributed from underused to hungry slots. |
| **Slots** | `API_FUNCTIONS` (5000), `REQUIREMENTS` (3000), `TESTS` (3000), `DEPENDENCIES` (2500), `RELATIONSHIPS` (1500), `SAFETY` (1200), `CUSTOM` (1000), `CODE_EXAMPLES` (500), `REGISTERS` (500), `CONVERSATION` (300) |
| **Algorithm** | (1) Group candidates by slot, sort by relevance. (2) Fill each slot up to budget (best items first). (3) Redistribute surplus from slots using <30% budget to slots using ≥90%. (4) Second fill pass with redistributed budget. (5) Hard-cap: trim lowest-relevance globally if over total budget. |
| **Default total budget** | 8,000 tokens (configurable per request via `max_tokens`). Token estimation: `len(text) // 3`. |
| **Implementation** | `ContextBuilder` in `src/HybridRAG/code/querier/context_builder.py`. Memory layer also has a `ContextBuilder` in `src/MemoryLayer/memory/context_builder.py` with a "librarian" metaphor (20% conversation, 5% session, 75% RAG). |

---

## ADR-012: MCP Protocol with streamable-http Transport

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 1 |
| **Context** | DAs need a standardized protocol to discover and call AICE tools. DAs run in different environments: VS Code Copilot (IDE), CI/CD pipelines (CLI), LiteLLM proxy (batch). |
| **Decision** | Use the **Model Context Protocol (MCP)** with JSON-RPC 2.0 message format. Primary transport: **streamable-http** for production/Kubernetes. **stdio** for local development. |
| **Rationale** | (1) MCP is becoming the standard for LLM-tool interaction — VS Code Copilot, Claude, and other platforms support it natively. (2) JSON-RPC is well-specified and language-agnostic. (3) streamable-http works naturally with Kubernetes services and load balancers. (4) stdio enables simple local testing without network setup. |
| **Implementation** | `FastMCP` framework for tool registration. `main()` in `mcp_server.py` selects transport via `MCP_TRANSPORT` env var. ASGI middleware wraps HTTP transports for API key extraction. |

---

## ADR-013: Lazy Singleton Service Factories

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 2 |
| **Context** | MCP server has 14+ service objects that depend on external connections (Neo4j, Qdrant, Redis, PostgreSQL). Eagerly initializing all services at startup would slow boot time and fail if any backend is temporarily unavailable. |
| **Decision** | All service objects are created **lazily** via `_get_*()` module-level functions. Each function creates the service on first call and returns the cached instance thereafter. |
| **Rationale** | (1) Fast startup — server accepts connections immediately. (2) Fault tolerance — if a backend is down, only tools that need it fail; others work normally. (3) Simple implementation — no DI framework needed, just Python module-level variables. |
| **Pattern** | `_search_service = None; def _get_search_service(): global _search_service; if _search_service is None: _search_service = SearchService(...); return _search_service` |
| **Implementation** | `mcp_server.py` — every `_get_*()` function follows this pattern. Neo4j drivers are keyed by workspace profile in a dict. |

---

## ADR-014: Docker Compose with 5 Services

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 1 |
| **Context** | AICE requires 4 external services (Neo4j, Qdrant, Redis, PostgreSQL) plus the MCP server itself. Developers need a one-command setup. Production needs defined health checks and restart policies. |
| **Decision** | Use **Docker Compose** with 5 named services on a shared bridge network (`aice-net`). Named volumes for data persistence. Health checks on all services. MCP server depends on all 4 backends (`service_healthy`). |
| **Services** | `neo4j` (5.26, APOC+GDS, 512m heap), `qdrant` (v1.12.1), `redis` (7-alpine, 256mb maxmemory, allkeys-lru, AOF), `postgres` (16-alpine), `mcp-server` (built from Dockerfile) |
| **Ports** | Neo4j: 7474/7687, Qdrant: 6333/6334, Redis: 6379, PostgreSQL: 5432, MCP: 8000 + Cerbos: 3592/3593 |
| **Implementation** | `docker-compose.yml` in project root. Multi-stage Dockerfile bundles Cerbos PDP binary + Python 3.12 runtime. `mcp/` directory copied as `aice_mcp/` to avoid shadowing the `mcp` pip package. |

---

## ADR-015: Structure-Aware Chunking for AUTOSAR Documents

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 5 |
| **Context** | AUTOSAR specifications are 40-60% tables. Fixed-size token chunking destroys table structures, splitting multi-column tables mid-row. Standard markdown splitters don't understand AUTOSAR-specific markers like `[SWS_xxx]` tags. |
| **Decision** | Use **hierarchical, structure-aware chunking**: heading-based section splits, atomic table handling (never split a table), max chunk size 4000 tokens (raised from standard 2000 to accommodate large API tables), and `[SWS_xxx]` tag preservation. |
| **Rationale** | (1) Tables are the primary information carrier in SWS documents — splitting them loses the column-to-row association. (2) 4000-token max accommodates typical AUTOSAR API parameter tables (often 2000-3000 tokens). (3) `[SWS_xxx]` tags are the traceability anchors — they must remain with their content. |
| **Alternatives rejected** | Fixed-size token chunking (destroys tables). Sliding window with propositions (too expensive at ingestion time). Semantic/topic-based as primary (unreliable for structured specs). |
| **Implementation** | PDF pipeline in `pdf_pipeline.py` (957 lines). Document-type-specific configs for SWS, hardware manuals, EXP documents, and design docs. |

---

## ADR-016: Celery Task Queue — Deferred

| Field | Value |
|-------|-------|
| **Status** | Deferred |
| **Date** | Evaluated Sprint 5, deferred |
| **Context** | Long-running operations (batch ingestion, VP test execution, batch code generation) could benefit from async task execution. Redis is already in the stack as a potential broker. |
| **Decision** | **Defer Celery adoption.** Design the `IngestionService` with a Celery-compatible interface (task signatures, job tracking), but run synchronously for now. |
| **Rationale** | (1) Current workloads are request-response MCP tool calls — async is not needed for the query path. (2) Ingestion is batch-oriented and runs infrequently (module onboarding, not per-query). (3) Adding Celery increases operational complexity (workers, broker monitoring, task retry logic). (4) The sync-first design can be wrapped in Celery task decorators later with minimal changes. |
| **When to reconsider** | When batch ingestion takes >5 minutes and blocks the MCP server, or when DAs need fire-and-forget code generation tasks. |
| **Implementation status** | `IngestionService.ingest_file()` has comments marking Celery insertion points (`# Celery: sync for now`). `IngestionJobTracker` already provides the async job-status API that Celery would use. |

---

## ADR-017: MinIO / S3 Object Storage — Deferred

| Field | Value |
|-------|-------|
| **Status** | Deferred |
| **Date** | Evaluated Sprint 5, deferred |
| **Context** | Source documents (PDFs, ARXML files, Excel specs) are currently read from local filesystem paths during ingestion. For multi-node deployment or CI/CD pipelines, a shared object store would be needed. |
| **Decision** | **Defer MinIO/S3 adoption.** Ingestion reads from local paths. Corpus snapshots (if needed) would use S3 with date-stamped naming and 2-year retention. |
| **Rationale** | (1) Current deployment is single-node Docker Compose — local filesystem is sufficient. (2) Adding object storage increases infrastructure complexity and cost. (3) Ingestion is an admin-only operation, not a hot path. (4) The parsed knowledge lives in Neo4j + Qdrant after ingestion — the original files are not re-read at query time. |
| **When to reconsider** | When deploying to multi-node Kubernetes where ingestion workers don't share a filesystem, or when implementing KG backup/restore workflows. |

---

## ADR-018: Cross-Encoder Reranking — Deferred

| Field | Value |
|-------|-------|
| **Status** | Deferred |
| **Date** | Evaluated Sprint 6, deferred |
| **Context** | RRF merges results by rank position. A cross-encoder (BERT-style) model could provide more accurate relevance scoring by jointly encoding the query and each candidate. |
| **Decision** | **Defer cross-encoder reranking.** RRF is sufficient for current quality requirements. |
| **Rationale** | (1) Cross-encoders are significantly slower (~50-100ms per query-document pair vs. ~1ms for RRF). (2) With top_k typically 10-20, the additional latency is 0.5-2 seconds per search. (3) RRF quality has been validated as adequate through GEST E2E testing. (4) Cross-encoder models need GPU for acceptable latency — not available in current on-premise deployment. |
| **When to reconsider** | When search quality metrics show unacceptable precision@k, or when GPU inference is available in the deployment environment. A cross-encoder could be added as a second-pass refiner over the top 10-20 RRF results. |

---

## ADR-019: Local On-Premise Deployment

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 1 (foundational constraint) |
| **Context** | Infineon's engineering data (register maps, safety-critical AUTOSAR code, proprietary silicon IP) cannot leave the corporate network. Cloud-hosted KG/RAG solutions (e.g., Pinecone, managed Neo4j Aura) are not permitted for this data classification. |
| **Decision** | All backend services (Neo4j, Qdrant, Redis, PostgreSQL, Cerbos) run **on-premise** within Infineon's infrastructure. No external API calls for data storage or retrieval. The only external dependency is GPT4IFX (Infineon's own LLM proxy). |
| **Rationale** | (1) Data residency — proprietary silicon register maps and AUTOSAR SWS data must stay on-premise. (2) Embedding model (`all-MiniLM-L6-v2`, 384-dim) was chosen specifically because it runs locally without GPU (~80MB, CPU-only inference). (3) Neo4j Community Edition and Qdrant are self-hosted — no cloud vendor dependency. (4) Docker Compose stack allows single-command deployment on any server with Docker installed. (5) Air-gapped operation is possible (all container images can be pre-pulled). |
| **Trade-offs** | No managed service auto-scaling. Neo4j Community lacks clustering (acceptable for current data volumes). Qdrant runs single-node (snapshot-based backup). Operational burden on the platform team for upgrades and monitoring. |
| **Alternatives rejected** | Pinecone (cloud-only, data residency violation). Neo4j Aura (managed cloud, same concern). AWS/Azure managed services (not approved for this data classification). |
| **Implementation** | `docker-compose.yml` defines 7 services on a bridge network (5 core + Prometheus + Grafana). All services use named Docker volumes for persistence. No external network egress required for data operations. |

---

## ADR-020: Keycloak OAuth — Deferred

| Field | Value |
|-------|-------|
| **Status** | Deferred (P3 — Nice to have) |
| **Date** | Evaluated in competition analysis, deferred |
| **Context** | The H2Loop reference architecture uses Keycloak for full OAuth 2.0 flows, SSO, LDAP/AD integration, and group-based access control. Question: should AICE adopt Keycloak for enterprise-grade authentication? |
| **Decision** | **Defer Keycloak adoption.** Cerbos RBAC is sufficient for the current MCP-based architecture where primary consumers are VS Code extensions and GitHub Copilot — not browser-based users. |
| **Rationale** | (1) Cerbos PDP handles per-tool RBAC with 3-tier roles — this covers the current authorization model well. (2) MCP's transport is machine-to-machine (API keys), not browser-based — OAuth 2.0 flows (authorization code, PKCE) add no value. (3) No current requirement for SSO with Infineon's corporate identity provider. (4) No web UI exists yet that would need browser-based login. (5) Adding Keycloak increases operational complexity (another stateful service, DB migration, token lifecycle management). |
| **When to reconsider** | When AICE adds a **web UI** (dashboard, review portal), when Infineon requires **LDAP/Active Directory integration** for user management, or when **cross-department group management** is needed beyond the current 3-tier model. |
| **What was adopted instead** | Cerbos PDP as a co-located sidecar (see ADR-005). API key → principal mapping via `api_keys.yaml`. Derived roles for tier inheritance. Graceful fallback to local tier check. |

---

## ADR-021: Prometheus + Grafana Observability

| Field | Value |
|-------|-------|
| **Status** | Adopted |
| **Date** | Sprint 8 (implementation), Sprint 10 (integration into ai-core-engine) |
| **Context** | AICE already collects timing data (e.g., `_step_times` in handlers, audit log `duration_ms`) but discards it after the request completes. PostgreSQL audit logs provide post-hoc analysis but not real-time dashboards or alerting. Need time-series metrics and visualization. |
| **Decision** | Add **Prometheus** for metrics collection and **Grafana** for dashboard visualization. MCP server exposes a `/metrics` endpoint scraped by Prometheus at 15s intervals. |
| **Rationale** | (1) Prometheus is the de-facto standard for metrics in containerized deployments. (2) Data is already being collected (timing, success/failure, cache hits) — Prometheus captures it as time-series instead of discarding it. (3) Grafana provides real-time dashboards for operations (query latency percentiles, error rates, cache effectiveness). (4) Both are self-hosted and open-source — consistent with the on-premise deployment model (ADR-019). (5) Alerting rules can trigger on SLA violations (e.g., p95 latency > 2s). |
| **Metrics collected** | MCP server: query latency, tool success/failure counts, context assembly time, RLM sub-query count, token usage per LLM call. Neo4j: connection pool metrics. Qdrant: search performance. Redis: cache hit rates. |
| **Grafana dashboards** | Query latency percentiles (p50, p95, p99), search stage breakdown (graph vs. vector), cache effectiveness, error rate trends, system health overview. |
| **Configuration** | Prometheus: `prometheus.yml` with scrape targets (mcp-server:8000, neo4j:2004, redis-exporter:9121). Grafana: port 3000, provisioned datasource pointing to Prometheus. Both added to Docker Compose with named volumes (`prometheus_data`, `grafana_data`). |
| **Implementation status** | **Implemented (Sprint 10).** `src/Observability/metrics.py` defines 11 Prometheus metric types with graceful `_NoOp` fallback. The MCP server mounts `/metrics` via Starlette alongside FastMCP. `docker-compose.yml` includes Prometheus (v2.53.0, port 9090) and Grafana (v11.1.0, port 3000) services with auto-provisioned datasource and 10-panel overview dashboard. Dependencies: `prometheus_client>=0.21`, `starlette>=0.37`. |

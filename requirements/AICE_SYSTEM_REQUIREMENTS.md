# AI Core Engine (AICE) — System Requirements

**Version 2.1.0 | Sprint 10**
**MCP Interface for Automotive Embedded Software Development**

> This document captures all implemented features as formal requirements.
> For component-specific requirements, see:
> - [Ingestion Pipeline Requirements](Ingestion%20Pipeline.md)
> - [Memory Layer Requirements](MEMORY_LAYER_REQUIREMENTS.md)

---

## 1. MCP Server Foundation

### 1.1 Tool Registration & Protocol

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-SRV-001 | The MCP server shall expose exactly 56 tools across 13 categories via JSON-RPC 2.0 | Must | Tool inventory per PPTX v3 Developer Guide | IMPLEMENTED |
| AICE-SRV-002 | The MCP server shall support streamable-http transport for production/Kubernetes deployments | Must | Standard HTTP transport for load balancers and service mesh | IMPLEMENTED |
| AICE-SRV-003 | The MCP server shall support stdio transport for local development and testing | Should | Zero-network-config local development | IMPLEMENTED |
| AICE-SRV-004 | Transport mode shall be configurable via MCP_TRANSPORT environment variable | Must | Deployment flexibility | IMPLEMENTED |
| AICE-SRV-005 | All tools shall return responses in a standard JSON envelope: `{"error": false, "data": ...}` for success, `{"error": true, "error_code": "...", "message": "..."}` for failure | Must | Wire-compatible response contract for all DA clients | IMPLEMENTED |
| AICE-SRV-006 | The MCP server shall expose 12 MCP resources for static data retrieval | Should | Resource-based access for ontology, config, status | IMPLEMENTED |
| AICE-SRV-007 | The MCP server shall expose 8 MCP prompts for common DA workflows | Should | Reusable prompt templates for DAs | IMPLEMENTED |

### 1.2 Service Architecture

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-SRV-010 | All service objects shall be lazily instantiated via `_get_*()` singleton factory functions | Must | Fast startup; fault-tolerant (only failing backends affect their tools) | IMPLEMENTED |
| AICE-SRV-011 | The MCP server shall start and accept connections even if one or more backends are unavailable | Must | Graceful degradation: tools for healthy backends continue working | IMPLEMENTED |
| AICE-SRV-012 | No backend failure shall crash the MCP server process | Must | Production reliability | IMPLEMENTED |
| AICE-SRV-013 | Per-request API key shall be propagated via Python contextvars for HTTP transports | Must | Thread-safe request-scoped authorization | IMPLEMENTED |
| AICE-SRV-014 | The MCP server shall support dual workspace profiles: illd (reference drivers) and mcal (productive AUTOSAR MCAL) | Must | Workspace isolation for different software stacks | IMPLEMENTED |

---

## 2. Authentication & Authorization (Category 13 — 2 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-AUTH-001 | The system shall use Cerbos PDP as a sidecar process for policy-based RBAC authorization | Must | Declarative YAML policies with CEL expressions; no code changes for access rule updates | IMPLEMENTED |
| AICE-AUTH-002 | Tools shall be classified into three tiers: public (34), developer (14), admin (8) | Must | Least-privilege access model | IMPLEMENTED |
| AICE-AUTH-003 | Tier inheritance shall be enforced: admin inherits developer inherits public | Must | Role hierarchy via Cerbos derived roles | IMPLEMENTED |
| AICE-AUTH-004 | API keys shall be mapped to principals via `api_keys.yaml` with per-DA key support | Must | DA-specific authentication and audit | IMPLEMENTED |
| AICE-AUTH-005 | When Cerbos PDP is unavailable, the system shall fall back to local tier-check from `tool_tiers.py` | Must | Graceful degradation for authorization | IMPLEMENTED |
| AICE-AUTH-006 | No API credentials shall be hardcoded in source code; all credentials from environment variables | Must | Security best practice (Bug Fix #3) | IMPLEMENTED |
| AICE-AUTH-007 | The `ensure_valid_token` tool shall manage GPT4IFX JWT lifecycle (obtain, cache, refresh) | Must | Automated LLM authentication | IMPLEMENTED |
| AICE-AUTH-008 | The `whoami` tool shall return the current principal, role, and tier for the calling API key | Should | Debugging and audit | IMPLEMENTED |

---

## 3. Search & Hybrid RAG (Category 1 — 6 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-SEARCH-001 | The `search_database` tool shall perform hybrid search combining Neo4j graph traversal and Qdrant vector similarity | Must | Core retrieval capability | IMPLEMENTED |
| AICE-SEARCH-002 | Search results shall be merged via Reciprocal Rank Fusion (RRF) with configurable alpha blending (0.0=graph-only, 1.0=vector-only) | Must | Tunable retrieval for different query types | IMPLEMENTED |
| AICE-SEARCH-003 | The `search_database` tool shall accept: query, max_results (default 10), alpha (default 0.5), filters, workspace_id | Must | Flexible search parameterization | IMPLEMENTED |
| AICE-SEARCH-004 | The `search_by_node_type` tool shall restrict search to specific Neo4j node labels | Should | Targeted retrieval for type-specific queries | IMPLEMENTED |
| AICE-SEARCH-005 | The `search_with_context` tool shall return results enriched with neighboring graph context | Should | Context-aware retrieval | IMPLEMENTED |
| AICE-SEARCH-006 | The `execute_cypher` tool (developer tier) shall execute arbitrary Cypher queries with parameterization | Must | Advanced graph queries for power users | IMPLEMENTED |
| AICE-SEARCH-007 | The `get_node_by_id` tool shall retrieve a specific node by its unique identifier | Must | Direct node access | IMPLEMENTED |
| AICE-SEARCH-008 | The `get_node_neighbors` tool shall return all nodes connected to a given node within a specified depth | Should | Graph exploration | IMPLEMENTED |
| AICE-SEARCH-009 | Search shall support entity-targeted lookup when named entities are detected in the query | Should | Precision retrieval for known entities | IMPLEMENTED |
| AICE-SEARCH-010 | Search shall detect aggregation queries and route to appropriate graph-aggregation Cypher | Should | Module-wide queries (e.g., "all functions in CAN") | IMPLEMENTED |

---

## 4. API Intelligence (Category 2 — 3 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-API-001 | The `query_api_function` tool shall return structured information about an API function: signature, parameters, return type, description, preconditions, traceability links | Must | Core function lookup for CIA, GEST, ACRA | IMPLEMENTED |
| AICE-API-002 | The `get_type_definition` tool shall return struct/enum/typedef definitions with field details | Must | Type resolution for code generation | IMPLEMENTED |
| AICE-API-003 | The `generate_init_code` tool shall generate initialization code sequences for a given module using dependency-ordered function calls | Should | Init sequence generation for CIA | IMPLEMENTED |
| AICE-API-004 | API Intelligence shall degrade gracefully without Neo4j, returning empty result sets with `matches_found: 0` | Must | Offline resilience | IMPLEMENTED |

---

## 5. Dependency Analysis (Category 3 — 3 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-DEP-001 | The `query_dependencies` tool shall resolve transitive function dependencies with topological init ordering | Must | Correct initialization sequences | IMPLEMENTED |
| AICE-DEP-002 | The `validate_api_usage` tool shall validate a function call sequence against the dependency graph | Must | Detect missing init calls | IMPLEMENTED |
| AICE-DEP-003 | The `detect_polling_requirements` tool shall identify which APIs require status polling after invocation | Must | Async operation detection | IMPLEMENTED |
| AICE-DEP-004 | Dependency resolution shall support configurable max_depth and include_hardware flag | Should | Scope control for dependency traversal | IMPLEMENTED |

---

## 6. Traceability (Category 4 — 4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-TRACE-001 | The `find_requirement_traces` tool shall return the full V-Model chain: Requirement → Architecture → Code → Test → Result | Must | ASPICE traceability | IMPLEMENTED |
| AICE-TRACE-002 | The `build_traceability_matrix` tool shall generate a module-wide coverage matrix in JSON, CSV, or HTML format | Must | Coverage reporting | IMPLEMENTED |
| AICE-TRACE-003 | The `find_coverage_gaps` tool shall identify missing links in requirement-code-test chains by severity | Must | Gap analysis for ASPICE | IMPLEMENTED |
| AICE-TRACE-004 | The `analyze_hw_sw_links` tool shall map hardware register usage to software functions and detect undocumented accesses | Must | HW-SW interface analysis | IMPLEMENTED |
| AICE-TRACE-005 | Traceability queries shall use canonical requirement labels (SoftwareRequirement, ProductRequirement, StakeholderRequirement) and case-insensitive module matching | Must | Ontology alignment | IMPLEMENTED |

---

## 7. Memory & Context (Category 6 — 11 tools)

### 7.1 Session Management (5 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-SESSION-001 | The `session_start` tool shall create a new working memory session with assistant_name, module_context, and configurable TTL | Must | Session lifecycle entry point | IMPLEMENTED |
| AICE-MEM-SESSION-002 | The `session_end` tool shall close a session and persist audit trail to PostgreSQL | Must | Session lifecycle exit point | IMPLEMENTED |
| AICE-MEM-SESSION-003 | The `session_store` tool shall store key-value data in session working memory | Must | Intermediate result storage | IMPLEMENTED |
| AICE-MEM-SESSION-004 | The `session_retrieve` tool shall retrieve stored data from session working memory | Must | Context retrieval | IMPLEMENTED |
| AICE-MEM-SESSION-005 | The `build_context` tool shall assemble token-budget-aware context from RAG results using the 10-slot algorithm | Must | LLM prompt optimization | IMPLEMENTED |
| AICE-MEM-SESSION-006 | Sessions shall support dual backends: Redis (production, with native TTL) and in-memory (development) via Strategy pattern | Must | Deployment flexibility | IMPLEMENTED |
| AICE-MEM-SESSION-007 | Expired sessions (past TTL) shall raise SessionExpiredError on any access attempt | Must | Resource management | IMPLEMENTED |

### 7.2 Ephemeral Sandbox (4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-SANDBOX-001 | The `sandbox_upload` tool shall ingest files into a per-session in-memory knowledge graph for experimental content | Must | Ad-hoc document analysis | IMPLEMENTED |
| AICE-MEM-SANDBOX-002 | The `sandbox_query` tool shall search within the ephemeral sandbox graph | Must | Session-scoped retrieval | IMPLEMENTED |
| AICE-MEM-SANDBOX-003 | The `sandbox_status` tool shall return sandbox statistics (nodes, relationships, files) | Should | Sandbox monitoring | IMPLEMENTED |
| AICE-MEM-SANDBOX-004 | The `sandbox_clear` tool shall delete all sandbox content for a session | Should | Resource cleanup | IMPLEMENTED |
| AICE-MEM-SANDBOX-005 | Sandbox content shall be automatically destroyed when the parent session ends | Must | No data leakage between sessions | IMPLEMENTED |

### 7.3 RLM Orchestrator (2 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-RLM-001 | The `rlm_orchestrate` tool shall perform multi-step retrieval: LLM plans N sub-queries (max 6), each assembled with ContextBuilder at 8K token budget, then synthesized | Must | Complex multi-domain context assembly | IMPLEMENTED |
| AICE-RLM-002 | The `rlm_plan_preview` tool (developer tier) shall return the planned sub-queries without executing them | Should | Planning visibility and debugging | IMPLEMENTED |
| AICE-RLM-003 | RLM shall support 24 task types covering all 21 Domain Assistants with domain-specific Neo4j node/relationship guidance, alpha values, and synthesis instructions | Must | DA-specific planning quality | IMPLEMENTED |
| AICE-RLM-004 | DA_TASK_MAPPING shall map all 21 DAs to their appropriate task types | Must | Complete DA coverage without GENERIC fallback | IMPLEMENTED |
| AICE-RLM-005 | Complexity routing heuristic shall trigger RLM when 2+ of 3 signals fire: 3+ functions needed, register-level keywords present, ASIL-B/D requirements detected | Must | Automatic complexity routing | IMPLEMENTED |
| AICE-RLM-006 | `rlm_orchestrate` and `rlm_plan_preview` shall be placed in Category 6 (Memory & Context), not as a separate category | Must | Architecture discipline per ADR-009 | IMPLEMENTED |

---

## 8. Cache (Category 7 — 4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-CACHE-001 | The system shall provide a two-tier cache: LRU exact-match (fast, in-memory) and semantic similarity (sentence-transformers embedding comparison) | Must | Reduce redundant LLM calls and search operations | IMPLEMENTED |
| AICE-CACHE-002 | The `cache_get` tool shall check LRU first, then semantic cache, returning the closest match above threshold | Must | Cache lookup | IMPLEMENTED |
| AICE-CACHE-003 | The `cache_put` tool shall store results in both LRU and semantic cache tiers | Must | Cache population | IMPLEMENTED |
| AICE-CACHE-004 | The `cache_invalidate` tool (developer tier) shall remove specific cache entries | Should | Manual cache management | IMPLEMENTED |
| AICE-CACHE-005 | The `cache_stats` tool (developer tier) shall return hit/miss counts and cache size | Should | Cache monitoring | IMPLEMENTED |
| AICE-CACHE-006 | LRU cache shall enforce configurable TTL with automatic eviction of expired entries. Configurable via `LRU_CACHE_SIZE` (default 10000) and `LRU_CACHE_TTL_HOURS` (default 24) env vars | Must | Cache freshness | IMPLEMENTED |
| AICE-CACHE-007 | Semantic cache shall use all-MiniLM-L6-v2 model for embedding comparison. Threshold configurable via `SEMANTIC_CACHE_THRESHOLD` (default 0.95), TTL via `SEMANTIC_CACHE_TTL_DAYS` (default 7) | Should | Consistent embedding model across system | IMPLEMENTED |

---

## 9. Feedback (Category 8 — 4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-FB-001 | The `submit_human_feedback` tool shall record human review decisions (APPROVE, APPROVE_WITH_EDITS, REJECT) with optional correction notes | Must | Continuous learning input | IMPLEMENTED |
| AICE-FB-002 | The `get_learning_metrics` tool shall return approval rates, rejection counts, and pattern details | Should | Learning effectiveness monitoring | IMPLEMENTED |
| AICE-FB-003 | The `get_failure_patterns` tool shall return aggregated failure patterns from rejected results | Should | Root cause analysis | IMPLEMENTED |
| AICE-FB-004 | The `get_review_analytics` tool shall return comprehensive review statistics by decision type | Should | Review process metrics | IMPLEMENTED |
| AICE-FB-005 | The FeedbackSink shall persist feedback to PostgreSQL and wire learned patterns to PatternStore (Neo4j) + PatternIndex (Qdrant) | Must | Durable learning with cross-session pattern reuse | IMPLEMENTED |

---

## 10. Review Gate & Confidence Scoring (Category 9 — 4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-RG-001 | The `evaluate_confidence` tool shall compute a deterministic confidence score (0-100) using 13 weighted signals, without any LLM dependency | Must | Auditable, reproducible scoring | IMPLEMENTED |
| AICE-RG-002 | Scoring shall route to: AUTO (score ≥ 80, ~5 min spot check), QUICK (50-79, ~15-20 min review), FULL (< 50, ≥1 hr deep review) | Must | Risk-proportionate human review | IMPLEMENTED |
| AICE-RG-003 | The `complete_review` tool shall record the final review verdict with reviewer_id and rationale | Must | ASPICE review evidence | IMPLEMENTED |
| AICE-RG-004 | The `override_routing` tool shall allow manual override of review routing (e.g., escalate AUTO to FULL) | Should | Human override for safety-critical | IMPLEMENTED |
| AICE-RG-005 | The `process_results` tool shall parse test/analysis results from external tools: VP XML, Polyspace CSV/XML, JUnit XML, GCOV/LCOV coverage, GCC/Tasking compiler logs | Must | CI/CD result ingestion | IMPLEMENTED |
| AICE-RG-006 | Key scoring signals shall include: has_kg_context (+30), high_relevance (+20), has_dependency_order (+20), missing_requirements (-30), is_safety_critical (-15) — 13 signals total | Must | Comprehensive risk assessment | IMPLEMENTED |

---

## 11. Ontology (Category 10 — 4 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-ONT-001 | The system shall maintain a dual-profile ontology (illd and mcal) defined in a YAML file with 30+ node types and relationship types | Must | Schema-driven knowledge model | IMPLEMENTED |
| AICE-ONT-002 | The `get_ontology` tool shall return the full ontology definition for a given profile | Must | Schema discovery | IMPLEMENTED |
| AICE-ONT-003 | The `get_node_types` tool shall return all valid node types for a profile | Should | Schema introspection | IMPLEMENTED |
| AICE-ONT-004 | The `validate_schema` tool (developer tier) shall validate graph data against ontology constraints | Should | Data quality enforcement | IMPLEMENTED |
| AICE-ONT-005 | The `get_relationship_types` tool (developer tier) shall return all valid relationship types | Should | Schema introspection | IMPLEMENTED |

---

## 12. Observability (Category 11 — 6 tools)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-OBS-001 | The `health_check` tool shall ping all backends (Neo4j, Qdrant, Redis, PostgreSQL, Cerbos) and return status with latency | Must | Infrastructure monitoring | IMPLEMENTED |
| AICE-OBS-002 | The `graph_stats` tool shall return total nodes, relationships, and label distribution from Neo4j | Should | Knowledge graph monitoring | IMPLEMENTED |
| AICE-OBS-003 | The `module_list` tool shall return all modules with node counts per module | Should | Module inventory | IMPLEMENTED |
| AICE-OBS-004 | The `get_distribution` tool shall return node type distribution across the graph | Should | Data profile monitoring | IMPLEMENTED |
| AICE-OBS-005 | The `coverage_report` tool shall return requirement-to-test coverage percentages per module | Should | ASPICE coverage reporting | IMPLEMENTED |
| AICE-OBS-006 | The `metrics` tool (developer tier) shall return a combined metrics snapshot | Should | Operational overview | IMPLEMENTED |

---

## 13. Visualization (Category 12 — 1 tool)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-VIZ-001 | The `visualize_graph` tool (developer tier) shall generate graph visualizations for knowledge exploration | Should | Visual debugging and exploration | IMPLEMENTED |

---

## 14. Prometheus Metrics & Observability Infrastructure

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-PROM-001 | The MCP server shall expose a `/metrics` endpoint for Prometheus scraping via `prometheus_client` ASGI app | Must | Real-time time-series metrics | IMPLEMENTED |
| AICE-PROM-002 | All 56 tools shall be automatically instrumented via `_ok()`/`_err()` helpers (no per-tool code changes) | Must | Zero-effort metric collection | IMPLEMENTED |
| AICE-PROM-003 | The system shall expose 11 Prometheus metric types: tool_requests_total, tool_request_duration, search_requests_total, search_duration, cache_requests_total, active_sessions, rlm_requests_total, rlm_subqueries, ingestion_files_total, backend_up, review_routing_total | Must | Comprehensive operational metrics | IMPLEMENTED |
| AICE-PROM-004 | If `prometheus_client` is not installed, all metrics shall degrade to `_NoOp` stubs that silently ignore all method calls | Must | Graceful degradation without metrics dependency | IMPLEMENTED |
| AICE-PROM-005 | Metrics shall be opt-in via `ENABLE_METRICS` environment variable | Should | Resource savings in development | IMPLEMENTED |
| AICE-PROM-006 | Grafana shall be pre-provisioned with a Prometheus datasource and a 10-panel overview dashboard | Should | Zero-setup monitoring | IMPLEMENTED |
| AICE-PROM-007 | Prometheus shall scrape the MCP server at 15-second intervals with 15-day data retention | Should | Operational monitoring window | IMPLEMENTED |

---

## 15. PostgreSQL Audit & Persistence

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-PG-001 | The system shall maintain a 7-table PostgreSQL schema for ASPICE-compliant audit trails | Must | Regulatory compliance | IMPLEMENTED |
| AICE-PG-002 | Tables shall include: audit_logs, response_archive, review_evidence, feedback_records, failure_patterns, ingestion_jobs, sessions_meta | Must | Complete audit coverage | IMPLEMENTED |
| AICE-PG-003 | Tables shall be auto-created on first connection (no manual migration required) | Must | Zero-setup deployment | IMPLEMENTED |
| AICE-PG-004 | All PostgreSQL writes shall be non-blocking; failures shall be logged but never crash the server | Must | Graceful degradation | IMPLEMENTED |
| AICE-PG-005 | Every MCP tool invocation shall be logged to audit_logs with: tool, caller, workspace, session, params, status, duration_ms | Must | ASPICE prompt logging | IMPLEMENTED |

---

## 16. Deployment & Infrastructure

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-DEPLOY-001 | The system shall be deployable via Docker Compose with 5 core services (Neo4j, Qdrant, Redis, PostgreSQL, MCP Server) and 2 optional monitoring services (Prometheus, Grafana) | Must | Self-contained deployment | IMPLEMENTED |
| AICE-DEPLOY-002 | The Dockerfile shall use multi-stage build: Stage 1 copies Cerbos binary, Stage 2 builds Python app | Must | Reproducible authorization sidecar | IMPLEMENTED |
| AICE-DEPLOY-003 | All services shall have Docker health checks with appropriate intervals and retry counts | Must | Container orchestration readiness | IMPLEMENTED |
| AICE-DEPLOY-004 | The MCP server shall start only after all 4 backend services pass health checks (depends_on: service_healthy) | Must | Startup ordering | IMPLEMENTED |
| AICE-DEPLOY-005 | A Kubernetes deployment manifest (`mcp/k8s/deployment.yaml`) shall be provided | Should | Cloud-native deployment | IMPLEMENTED |
| AICE-DEPLOY-006 | All connection credentials shall be configurable via environment variables with development defaults | Must | Deployment flexibility | IMPLEMENTED |

---

## 17. Context Builder

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-CTX-001 | The authoritative ContextBuilder shall use a 10-slot token-budget algorithm with named ContextSlot types | Must | Structured context assembly per ADR-011 | IMPLEMENTED |
| AICE-CTX-002 | Default slot budgets shall be: API_FUNCTIONS (5000), REQUIREMENTS (3000), TESTS (3000), DEPENDENCIES (2500), RELATIONSHIPS (1500), SAFETY (1200), CUSTOM (1000), CODE_EXAMPLES (500), REGISTERS (500), CONVERSATION (300) | Must | Priority-weighted allocation | IMPLEMENTED |
| AICE-CTX-003 | The algorithm shall redistribute unused budget from slots using <30% to slots using ≥90% | Must | Adaptive budget rebalancing | IMPLEMENTED |
| AICE-CTX-004 | Total token budget shall default to 8000 tokens, configurable per request via max_tokens parameter | Must | Flexible budget per DA | IMPLEMENTED |
| AICE-CTX-005 | Token estimation shall use `len(text) // 3` as a conservative approximation | Should | Fast, dependency-free estimation | IMPLEMENTED |
| AICE-CTX-006 | ContextBuilder is architecturally part of the Memory Layer; `src/HybridRAG/code/querier/context_builder.py` is the primary implementation shared by SearchService and RLMOrchestrator | Must | Architectural clarity (see note below) | IMPLEMENTED |

> **Note:** `src/MemoryLayer/memory/context_builder.py` is the legacy Sprint 2 "librarian" builder with a simpler greedy algorithm. The HybridRAG version (Sprint 8) supersedes it and should be re-exported from MemoryLayer for architectural consistency.

---

## 18. Domain Assistant Integration

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-DA-001 | AICE shall serve 21+ Domain Assistants covering the full V-Model lifecycle | Must | Comprehensive lifecycle coverage | IMPLEMENTED |
| AICE-DA-002 | All DAs shall follow the 6-step session lifecycle: session_start → search/rlm_orchestrate → build_context → domain LLM call → evaluate_confidence → session_end | Must | Standardized workflow | IMPLEMENTED |
| AICE-DA-003 | DAs shall communicate with AICE exclusively via MCP (JSON-RPC 2.0 over HTTP) | Must | Protocol standardization | IMPLEMENTED |
| AICE-DA-004 | The CIA Domain Assistant shall be provided as a reference implementation with VS Code extension, FastAPI backend, 3 task handlers, and 5 composable skills | Should | Reference integration pattern | IMPLEMENTED |

---

*Document generated from Sprint 10 codebase. Version 2.1.0.*
*Status values: IMPLEMENTED (verified in code), DRAFT (defined but not verified), PLANNED (roadmap item).*

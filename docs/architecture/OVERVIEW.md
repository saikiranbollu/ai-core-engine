# AI Core Engine — System Architecture Overview

**Version 2.1.0 | Sprint 25**

> This document describes the implemented architecture of the AI Core Engine (AICE). For tool-level API details see [DOCUMENTATION.md](../DOCUMENTATION.md). For setup instructions see [MCP_QUICKSTART.md](../MCP_QUICKSTART.md).

---

## Table of Contents

- [AI Core Engine — System Architecture Overview](#ai-core-engine--system-architecture-overview)
  - [Table of Contents](#table-of-contents)
  - [1. System Purpose](#1-system-purpose)
  - [2. High-Level Architecture](#2-high-level-architecture)
  - [3. Component Map](#3-component-map)
  - [4. Service Layer](#4-service-layer)
    - [Service → Tool Category Mapping](#service--tool-category-mapping)
  - [5. Data Flow](#5-data-flow)
    - [5.1 Search Query (Primary Path)](#51-search-query-primary-path)
    - [5.2 Ingestion Path](#52-ingestion-path)
    - [5.3 DA Session Lifecycle](#53-da-session-lifecycle)
  - [6. Dual Workspace Model](#6-dual-workspace-model)
  - [7. Technology Stack](#7-technology-stack)
  - [8. Codebase Layout](#8-codebase-layout)
  - [9. Related Documents](#9-related-documents)

---

## 1. System Purpose

AICE is a **knowledge-graph-backed MCP (Model Context Protocol) server** purpose-built for Infineon AURIX TC3xx automotive embedded software development. It serves as the shared knowledge backbone for **21+ Domain Assistants** (DAs) — specialized LLM-based agents covering the full V-Model lifecycle from requirements through testing and safety analysis.

**Core capability**: Expose structured engineering knowledge (API functions, register maps, requirements, traceability chains, compliance rules) through a unified set of MCP tools, backed by a Hybrid RAG engine that combines graph traversal with vector similarity search.

**What AICE is NOT**: It is not an LLM. It is the retrieval and knowledge layer that feeds context to LLMs running inside each Domain Assistant.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Domain Assistants (DAs)                        │
│                                                                     │
│  CIA    GEST   ACRA   SAGA   REVA   PRQ   ATRA   GECA   ...  (21+) │
│  (Code) (Test) (Review)(Arch) (Req)  (Req) (Trace)(Config)         │
└────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬───────────────┘
     │      │      │      │      │      │      │      │
     └──────┴──────┴──────┴──────┼──────┴──────┴──────┘
                                 │
                    MCP (JSON-RPC 2.0 over HTTP)
                    Authorization: Bearer <api-key>
                                 │
┌────────────────────────────────┼────────────────────────────────────┐
│                         AICE MCP Server                             │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                 ASGI Middleware Layer                         │   │
│  │   _APIKeyMiddleware → contextvars per-request API key        │   │
│  │   Cerbos PDP → 3-tier RBAC (public/developer/admin)          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              FastMCP Tool Layer — 55 Tools                   │   │
│  │                                                              │   │
│  │  Cat 1: Search & Query (6)      Cat 8: Feedback (4)         │   │
│  │  Cat 2: API Intelligence (3)    Cat 9: Review Gate (4)      │   │
│  │  Cat 3: Dependencies (3)        Cat 10: Ontology (4)        │   │
│  │  Cat 4: Traceability (4)        Cat 11: Observability (6)   │   │
│  │  Cat 5: Ingestion (0-removed)   Cat 12: Visualization (1)   │   │
│  │  Cat 5b: HSI (1)                Cat 13: Authentication (2)  │   │
│  │  Cat 6: Memory (5+5+2=12)       Cat 14: GAP v2 (1)         │   │
│  │  Cat 7: Cache (5)                                           │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                   Service Layer                              │   │
│  │                                                              │   │
│  │  SearchService          KnowledgeIntelligenceService         │   │
│  │  RLMOrchestrator        IngestionService                     │   │
│  │  WorkingMemoryManager   SandboxManager                       │   │
│  │  CacheService           ConfidenceCalculator                 │   │
│  │  FeedbackSink           OntologyService                      │   │
│  │  ObservabilityService   ResultProcessor                      │   │
│  │  ContextBuilder         AuthService                          │   │
│  └──────────────────────────────────────────────────────────────┘   │
└────────┬──────────┬──────────┬──────────┬───────────────────────────┘
         │          │          │          │
    ┌────┴───┐ ┌────┴────┐ ┌──┴───┐ ┌────┴──────┐
    │ Neo4j  │ │ Qdrant  │ │Redis │ │PostgreSQL │
    │ 5.26   │ │ v1.12.1 │ │  7   │ │    16     │
    │        │ │         │ │      │ │           │
    │ KG     │ │ Vector  │ │Cache │ │ Audit     │
    │ (illd  │ │ 384-dim │ │Sess. │ │ Feedback  │
    │ + mcal)│ │ cosine  │ │W.Mem │ │ Ingestion │
    └────────┘ └─────────┘ └──────┘ └───────────┘
```

---

## 3. Component Map

| Component | Source Path | Responsibility | Backing Store |
|-----------|------------|----------------|---------------|
| **MCP Server** | `mcp/core/mcp_server.py` | 55 active tool handlers, ASGI middleware, singleton service factories | — |
| **Auth & RBAC** | `mcp/core/auth_middleware.py`, `mcp/auth/` | Cerbos PDP, API key → principal resolution, 3-tier RBAC | Cerbos PDP (subprocess) |
| **Hybrid RAG / Search** | `src/HybridRAG/code/querier/search_service.py` | Hybrid search pipeline: graph + vector → RRF merge | Neo4j + Qdrant |
| **Knowledge Intelligence** | `src/HybridRAG/code/querier/knowledge_intelligence.py` | API function lookup, dependency analysis, traceability | Neo4j |
| **RLM Orchestrator** | `src/HybridRAG/code/querier/rlm_orchestrator.py` | Multi-step retrieval: LLM plans → N sub-queries → synthesis | Neo4j + Qdrant + GPT4IFX |
| **Context Builder** | `src/HybridRAG/code/querier/context_builder.py` | Token-budget-aware context assembly with 10 priority slots | — (in-memory) |
| **Ingestion Pipeline** | `src/IngestionPipeline/` | 17 parsers, 3 connectors, incremental ingestion (platform-level; not MCP-exposed) | Neo4j + Qdrant + PostgreSQL |
| **Memory Layer** | `src/MemoryLayer/` | Sessions (Redis/in-memory), ephemeral sandbox, node sets | Redis + Neo4j |
| **Review Gate** | `src/ReviewGate/` | Deterministic confidence scoring, feedback loop, result processing | PostgreSQL + Qdrant |
| **Cache** | `src/Configuration/cache_service.py` | Two-tier cache: LRU exact match + SemanticCache (sentence-transformers cosine, in-process) | In-memory + sentence-transformers |
| **Ontology** | `src/Configuration/services.py` + `ontology.yaml` | Dual-profile ontology (illd/mcal), schema validation | YAML + Neo4j |
| **Observability** | `src/Observability/postgres_schema.py`, `src/Observability/metrics.py` | 7-table PostgreSQL audit schema, graph statistics, Prometheus metrics | PostgreSQL + Neo4j + Prometheus + Grafana |
| **KG Construction** | `src/HybridRAG/code/KG/build_knowledge_graph.py` | Full knowledge graph build pipeline (4668 lines) | Neo4j + Qdrant |

---

## 4. Service Layer

All service objects are constructed lazily via `_get_*()` singleton factory functions in `mcp_server.py`. Each MCP tool handler follows an identical pattern:

```
@mcp.tool()
async def tool_name(params...) → dict:
    _authorize("tool_name")           # Cerbos RBAC check
    svc = _get_service()              # Lazy singleton
    result = svc.method(params...)    # Delegate to service
    return _ok(result)                # {"error": false, "data": ...}
```

### Service → Tool Category Mapping

| Service Class | Categories Served | Key Methods |
|---------------|-------------------|-------------|
| `SearchService` | Cat 1 (Search) | `search()`, `search_nodes()`, `get_node_by_id()`, `get_neighbors()`, `shortest_path()`, `execute_cypher()` |
| `KnowledgeIntelligenceService` | Cat 2–4 (Intelligence, Dependencies, Traceability) | `query_api_function()`, `get_type_definition()`, `query_dependencies()`, `validate_api_usage()`, `find_requirement_traces()`, `build_traceability_matrix()` |
| `IngestionService` | Cat 5 (not MCP) | `ingest_file()`, `ingest_module()`, `batch_ingest()`, `ingest_repository()` — platform-level only |
| `WorkingMemorySessionAdapter` | Cat 6 (Sessions) | `start()`, `store()`, `retrieve()`, `end()` |
| `SandboxManager` | Cat 6 (Sandbox) | `upload()`, `status()`, `clear()`, `diff()` — `sandbox_query` deprecated (use `search_database` with `session_id`) |
| `RLMOrchestrator` | Cat 6 (RLM) | `orchestrate()`, `preview_plan()` |
| `ContextBuilder` | Cat 6 (Context) | `build()` with 10-slot token budgets |
| `CacheService` | Cat 7 (Cache) | `get()`, `put()`, `invalidate()`, `stats()`, `refresh_config()` |
| `FeedbackSink` + `ResultProcessor` | Cat 8 (Feedback) | `submit()`, `process_results()`, `get_history()`, `get_learning_summary()` |
| `ConfidenceCalculator` | Cat 9 (Review Gate) | `evaluate()`, `route()`, `explain()`, `submit_review()` |
| `OntologyService` | Cat 10 (Config) | `get_profile()`, `get_schema()`, `validate()`, `compliance_score()` |
| `ObservabilityService` | Cat 11 (Health) | `health_check()`, `graph_stats()`, `module_list()`, `distribution()`, `coverage_report()`, `metrics()` |

---

## 5. Data Flow

### 5.1 Search Query (Primary Path)

```
DA sends search_database(query, workspace, alpha, top_k)
    │
    ▼
┌─ _authorize("search_database") ─── Cerbos PDP check ──┐
│                                                         │
│  ┌─ CacheService.get(query, workspace) ────────────┐   │
│  │   1. LRU exact match → HIT? return cached       │   │
│  │   2. SemanticCache (sentence-transformers        │   │
│  │      cosine, ≥threshold) → HIT? return          │   │
│  │   3. MISS → proceed to search                   │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ SearchService.search() ────────────────────────┐   │
│  │   Stage 1: Query analysis                       │   │
│  │     • Label inference (keyword → node type)     │   │
│  │     • Keyword extraction for entity lookup      │   │
│  │                                                 │   │
│  │   Stage 2: Graph search (Neo4j)                 │   │
│  │     • Cypher full-text + property matching      │   │
│  │     • NodeSet-scoped (module isolation)          │   │
│  │                                                 │   │
│  │   Stage 3: Entity-targeted lookup               │   │
│  │     • Exact match across 9 property fields      │   │
│  │     • 1-hop neighbor expansion                  │   │
│  │                                                 │   │
│  │   Stage 4: Vector search (Qdrant)               │   │
│  │     • 384-dim cosine similarity                 │   │
│  │     • Collection per workspace                  │   │
│  │                                                 │   │
│  │   Stage 5: RRF merge                            │   │
│  │     • score = Σ 1/(k + rank + 1), k=60         │   │
│  │     • alpha weights graph; (1-alpha) vector     │   │
│  │     • Deduplicate by node_id                    │   │
│  │     • Sort descending, apply top_k              │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─ CacheService.put(query, result) ───────────────┐   │
│  │   Write to both tiers (LRU exact + Semantic)    │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
└──── return _ok(result) ────────────────────────────────┘
```

### 5.2 Ingestion Path

```
ingest_file(file_path, workspace, module)
    │
    ▼
┌─ IngestionService ───────────────────────────────────┐
│                                                       │
│  1. Route by extension → specialized parser           │
│     .c/.h  → c_parser (libclang)                     │
│     .arxml → arxml_parser                            │
│     .pdf   → pdf_parser (PyMuPDF / LLM-assisted)    │
│     .xlsx  → xlsx_parser (openpyxl)                  │
│     .puml  → puml_parser                             │
│     .rst   → rst_parser                              │
│     .json  → JSON loader                             │
│     .md/.txt/.csv → text extraction                  │
│                                                       │
│  2. Parser extracts nodes + relationships             │
│     Functions, Structs, Registers, Enums,             │
│     Requirements, Test Cases, etc.                    │
│                                                       │
│  3. Write to Knowledge Graph                          │
│     • MERGE nodes into Neo4j                         │
│     • Link to module NodeSet anchor                  │
│     • Create typed relationships                      │
│                                                       │
│  4. Write to Vector Store                             │
│     • Embed content → 384-dim vectors                │
│     • Upsert into Qdrant collection                   │
│                                                       │
│  5. Track progress → IngestionJobTracker              │
│     • PostgreSQL write-through (10→50→90→100%)       │
└───────────────────────────────────────────────────────┘
```

### 5.3 DA Session Lifecycle

```
session_start(session_id, assistant_name, module_context)
    │
    ├── search_database / query_api_function / ...  (repeated)
    │
    ├── session_store(key, value)    ← working memory
    │
    ├── build_context(query, search_results, max_tokens=8192)
    │       │
    │       ├── [Standard] → ContextBuilder 10-slot budget fill
    │       └── [Complex]  → RLMOrchestrator (max 6 sub-queries)
    │
    ├── [DA calls its LLM with assembled context]
    │
    ├── evaluate_confidence(signals, response_id)
    │       └── Deterministic scoring → AUTO/QUICK/FULL routing
    │
    ├── submit_human_feedback(response_id, verdict)
    │
    └── session_end(session_id) → audit trail persisted
```

---

## 6. Dual Workspace Model

AICE serves two product ecosystems through logically separated data stores:

| Aspect | `illd` Workspace | `mcal` Workspace |
|--------|-------------------|-------------------|
| **Product** | iLLD reference drivers | MCAL productive software |
| **Compliance** | Relaxed | Strict (MISRA C:2012, AUTOSAR 4.x) |
| **Typical modules** | ~12 (ADC, SPI, CAN, CXPI, etc.) | ~15+ (29 Jama modules) |
| **Neo4j database** | `illd` (dedicated DB) | `mcal` (dedicated DB) |
| **Qdrant collections** | Per-module (`illd_<module>_embeddings`) | Per-module (`mcal_<module>_embeddings`) |
| **Primary node types** | APIFunction, DataStructure, Register | StakeholderRequirement, ProductRequirement, VerificationStep |
| **Primary sources** | Code parsing + HW specs | Jama requirements + code + test results |

Both workspaces share the same MCP server instance and tool set. Data isolation is enforced by:
1. **Separate Neo4j databases** — one per workspace
2. **Separate Qdrant collections** — per module per workspace
3. **NodeSet anchors** — each module has an anchor node; queries always start from the anchor, preventing cross-module data bleed

See [Node Sets Architecture](../NODE_SETS_ARCHITECTURE.md) for the full NodeSet design.

---

## 7. Technology Stack

| Layer | Technology | Version | Role |
|-------|-----------|---------|------|
| **Protocol** | MCP (Model Context Protocol) | JSON-RPC 2.0 | DA ↔ AICE communication |
| **Transport** | streamable-http (production), stdio (local dev) | — | HTTP with ASGI middleware |
| **Framework** | FastMCP | — | Tool registration, MCP compliance |
| **Runtime** | Python | 3.12 | Server runtime |
| **Knowledge Graph** | Neo4j Community | 5.26.0 | Structured engineering data + relationships |
| **Vector Store** | Qdrant | 1.12.1 | 384-dimensional semantic embeddings |
| **Embedding Model** | all-MiniLM-L6-v2 | — | Local sentence-transformers, 384-dim output |
| **Session/Cache Store** | Redis | 7-alpine | Working memory, session TTL |
| **Semantic Cache** | sentence-transformers (all-MiniLM-L6-v2) | — | In-process cosine similarity for semantic cache; O(n) scan, suitable for ≤500 entries |
| **Relational DB** | PostgreSQL | 16-alpine | Audit logs, feedback, ingestion jobs |
| **Authorization** | Cerbos PDP | latest | 3-tier RBAC policy evaluation |
| **LLM Proxy** | GPT4IFX | — | Infineon LLM proxy (RLM planning + PDF extraction) |
| **HTTP Server** | Uvicorn | — | ASGI server for HTTP transport |
| **Container** | Docker Compose | — | 7-service orchestration |
| **Graph Algorithms** | Neo4j APOC + GDS | — | Path finding, community detection |
| **In-memory Graphs** | NetworkX | — | Ephemeral sandbox per-session KGs |

---

## 8. Codebase Layout

```
ai-core-engine/
├── mcp/                              # MCP server (entrypoint + tools)
│   ├── app.py                        #   K8s entrypoint, Cerbos lifecycle
│   ├── core/
│   │   ├── mcp_server.py             #   62 tool handlers, ASGI middleware (1800 lines)
│   │   ├── tool_tiers.py             #   Tool → tier mapping (public/developer/admin)
│   │   └── auth_middleware.py         #   Cerbos integration, API key resolution
│   ├── auth/
│   │   ├── api_keys.yaml             #   API key → principal mapping
│   │   ├── policies/                 #   Cerbos resource policies + derived roles
│   │   └── .cerbos.yaml              #   Cerbos PDP configuration
│   └── k8s/
│       └── deployment.yaml           #   Kubernetes deployment manifest
│
├── src/
│   ├── HybridRAG/                    # Search, KG, RAG, RLM
│   │   ├── code/
│   │   │   ├── querier/
│   │   │   │   ├── search_service.py       # Hybrid search + RRF (1259 lines)
│   │   │   │   ├── rlm_orchestrator.py     # Multi-step retrieval (712 lines)
│   │   │   │   ├── knowledge_intelligence.py # API/dep/trace queries (683 lines)
│   │   │   │   ├── context_builder.py      # Token-budget assembly (236 lines)
│   │   │   │   └── kg_node_utils.py        # Node utilities (625 lines)
│   │   │   ├── KG/
│   │   │   │   ├── build_knowledge_graph.py  # KG construction (4668 lines)
│   │   │   │   └── query_knowledge_graph.py  # Legacy query module (1595 lines)
│   │   │   ├── RAG/
│   │   │   │   ├── hybrid_rag_unified.py     # Profile-agnostic RAG
│   │   │   │   ├── rag_query_unified.py      # Unified query engine
│   │   │   │   ├── vector_store_factory.py   # Qdrant client factory
│   │   │   │   └── collection_naming_unified.py
│   │   │   ├── neo4j_manager.py        # Connection management + config
│   │   │   ├── token_manager.py        # GPT4IFX JWT lifecycle
│   │   │   └── pdf_pipeline.py         # PDF processing pipeline
│   │   └── config/
│   │       ├── ontology.yaml           # 6166-line ontology definition
│   │       └── storage_config.yaml     # Neo4j + Qdrant connection config
│   │
│   ├── IngestionPipeline/            # File parsing + KG ingestion
│   │   ├── ingestion_service.py      # Service + job tracker (670 lines)
│   │   ├── Parsers/                  # 14 specialized parsers
│   │   ├── Connectors/              # Jama, Jenkins, Polarion
│   │   ├── Incremental/             # Git + mtime change detection
│   │   └── config/                  # Parser configurations
│   │
│   ├── MemoryLayer/                  # Sessions + working memory
│   │   └── memory/
│   │       ├── working_memory/       # Session + Manager + backends
│   │       ├── semantic_memory/      # PatternStore (Qdrant-backed)
│   │       ├── node_sets/           # NodeSet manager + scoped queries
│   │       ├── ephemeral_sandbox.py # Per-session in-memory KG
│   │       ├── context_builder.py   # "Librarian" budget allocator
│   │       ├── ontology_loader.py   # Singleton YAML loader
│   │       └── domain_session_adapter.py
│   │
│   ├── ReviewGate/                   # Confidence + feedback + results
│   │   ├── confidence.py            # Deterministic scoring (436 lines)
│   │   └── result_processors.py     # JUnit/VP/Polyspace parsers (709 lines)
│   │
│   ├── Configuration/                # Cache + ontology services
│   │   ├── cache_service.py         # LRU exact + SemanticCache (2-tier)
│   │   └── services.py             # OntologyService + ObservabilityService
│   │
│   └── Observability/                # Audit persistence + metrics
│       ├── postgres_schema.py       # 7-table PostgreSQL schema
│       └── metrics.py               # Prometheus metrics (11 types + NoOp fallback)
│
├── docker-compose.yml                # 7-service orchestration
├── Dockerfile                        # Multi-stage build (Cerbos + Python 3.12)
├── requirements.txt                  # Python dependencies
└── docs/                             # Documentation
    ├── DOCUMENTATION.md              # Complete reference (tools, config, API)
    ├── MCP_QUICKSTART.md             # Setup and connection guide
    ├── NODE_SETS_ARCHITECTURE.md     # NodeSet design
    └── architecture/                 # ← You are here
```

**Scale**: ~25,000+ lines of Python, 6,166-line ontology YAML, 55 active MCP tools (4 ingestion tools removed from MCP), 17 parsers, 3 external connectors, 7 PostgreSQL tables.

---

## 9. Related Documents

| Document | Focus |
|----------|-------|
| [Architecture Decisions](DECISIONS.md) | ADRs — why each technology and pattern was chosen |
| [Hybrid RAG & Search](hybrid-rag.md) | SearchService internals, RRF, alpha blending |
| [RLM Orchestrator](rlm-orchestrator.md) | Multi-step retrieval, task types, planning |
| [Ingestion Pipeline](ingestion-pipeline.md) | Parsers, connectors, incremental ingestion |
| [Memory Layer](memory-layer.md) | Sessions, sandbox, node sets, context assembly |
| [Review Gate](review-gate.md) | Confidence scoring, feedback, result processing |
| [Auth & Security](auth-and-security.md) | Cerbos RBAC, API keys, tier model |
| [Observability](observability.md) | PostgreSQL audit, Prometheus metrics, Grafana dashboards, health checks |
| [Deployment](deployment.md) | Docker Compose, Kubernetes, Dockerfile |
| [DOCUMENTATION.md](../DOCUMENTATION.md) | Full MCP tool reference |
| [MCP_QUICKSTART.md](../MCP_QUICKSTART.md) | Client setup and usage guide |
| [NODE_SETS_ARCHITECTURE.md](../NODE_SETS_ARCHITECTURE.md) | NodeSet module isolation design |

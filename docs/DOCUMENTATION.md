# AI Core Engine (AICE) — Complete Documentation

**Version 2.1.0 | Sprint 25**
**MCP Interface for Automotive Embedded Software Development**

> **Getting started?** See [MCP_QUICKSTART.md](MCP_QUICKSTART.md) for a practical setup and configuration guide with examples.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Requirements & Features](#2-requirements--features)
3. [System Architecture](#3-system-architecture)
4. [MCP Tool Reference](#4-mcp-tool-reference)
5. [Authentication & Authorization](#5-authentication--authorization)
6. [Storage Backends](#6-storage-backends)
7. [Ingestion Pipeline](#7-ingestion-pipeline)
8. [Memory Layer](#8-memory-layer)
9. [Search & Hybrid RAG](#9-search--hybrid-rag)
10. [Review Gate & Confidence Scoring](#10-review-gate--confidence-scoring)
11. [Cache Service](#11-cache-service)
12. [RLM Orchestrator](#12-rlm-orchestrator)
13. [Observability & Monitoring](#13-observability--monitoring)
14. [User Guide](#14-user-guide)
15. [API Reference](#15-api-reference)
16. [Ontology Reference](#16-ontology-reference)
17. [Glossary](#17-glossary)

---

## 1. Introduction

### 1.1 What is AICE?

The **AI Core Engine (AICE)** is a knowledge-graph-backed MCP (Model Context Protocol) server purpose-built for **Infineon AURIX TC3xx** automotive embedded software development. It provides a unified AI-powered platform that serves multiple Domain Assistants (DAs) — specialized LLM-based agents — with structured knowledge about AUTOSAR MCAL drivers, iLLD reference software, hardware registers, requirements traceability, and compliance rules.

AICE exposes **55 tools across 14 categories** (including Ephemeral Sandbox, HSI, and RLM extensions in Category 6 area), backed by a **Hybrid RAG** engine that combines Neo4j Knowledge Graph traversal with Qdrant vector similarity search, delivering precise and contextually rich responses for automotive software engineering tasks.

> **Note:** Run `python -c "from mcp.core.mcp_server import mcp; print(len(mcp._tools), 'tools registered')"` to verify the exact tool count in your deployment. The count includes 2 RLM tools, 4 Sandbox tools + `sandbox_diff`, 1 HSI tool (`get_function_hsi`), 5 Cache tools, and 1 GAP v2 tool (`query_enhance`). The 4 direct ingestion tools (`ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository`) were removed from MCP registration in Sprint 17 (Plan 2 Phase 2) — ingestion is now a platform-level operation.

### 1.2 Target Domain

| Aspect | Detail |
|--------|--------|
| **Hardware** | Infineon AURIX TC3xx family (TC37x, TC38x, TC39x) |
| **Software Stacks** | AUTOSAR Classic MCAL, iLLD (Infineon Low-Level Drivers) |
| **Standards** | ASPICE, ISO 26262, MISRA C:2012, AUTOSAR 4.x |
| **Modules** | ADC, CAN, DIO, ETH, FLS, GPT, ICU, MCU, PWM, SPI, WDG, UART, and more |

### 1.3 Domain Assistants Served

AICE serves 21+ Domain Assistants, each specialized for a phase of the V-Model lifecycle:

| Assistant | Code | V-Model Phase | Purpose |
|-----------|------|---------------|---------|
| Requirements Reviewer | REVA | Requirements | Review requirements for completeness, ambiguity, testability |
| Requirements Drafter | PRQ | Requirements | Draft product requirements from stakeholder inputs |
| Requirements Manager | RMA | Requirements | Manage requirement lifecycles and relationships |
| Architecture Analyst | SAGA | Architecture | Analyze software architecture, detect design issues |
| Architecture Tracer | ATRA | Architecture | Trace architecture decisions to requirements |
| Code Generator | CIA | Implementation | Generate compliant C code from requirements/specs |
| Code Transformer | CTA | Implementation | Transform/refactor existing code |
| Code Reviewer | ACRA | Implementation | Review code for MISRA, AUTOSAR, functional correctness |
| Config Generator | GECA | Implementation | Generate AUTOSAR configuration code |
| Page Generator | PAGE | Implementation | Generate documentation pages |
| Test Generator | GEST | Testing | Generate test cases from requirements and code |
| Test Verifier | GEVT | Testing | Verify test case quality and coverage |
| Test Quality Analyst | ATQA | Testing | Analyze overall test quality metrics |
| Safety Validator | — | Safety | Validate ISO 26262 safety requirements |
| Safety Analyst | — | Safety | Perform safety analysis (FMEA, FTA) |
| HAZOP Analyst | — | Safety | Hazard and operability studies |
| Data Flow Analyst | — | Safety | Data flow analysis for safety |
| MISRA Reviewer | — | Quality | MISRA C:2012 compliance checking |
| Traceability Analyst | TripleA | Cross-cutting | V-Model traceability analysis |
| Debug Analyst | VoltAI | Maintenance | Debug analysis and root cause investigation |
| Knowledge Weaver | KW | Infrastructure | Knowledge ingestion and graph enrichment |

---

## 2. Requirements & Features

### 2.1 Functional Requirements

#### FR-1: Hybrid Knowledge Retrieval
- **FR-1.1**: Semantic vector search across 384-dimensional embeddings (Qdrant)
- **FR-1.2**: Structured graph traversal via Neo4j Cypher queries
- **FR-1.3**: Alpha-blending parameter (0.0–1.0) to control vector vs. graph weight
- **FR-1.4**: Reciprocal Rank Fusion (RRF) for merging multi-source results
- **FR-1.5**: Label-aware search with entity-targeted lookup and 1-hop graph expansion

#### FR-2: API Intelligence
- **FR-2.1**: Query API functions with 25+ enriched fields (signature, parameters, return type, dependencies, traceability, MISRA notes, initialization sequence)
- **FR-2.2**: Type definition resolution (structs, enums, typedefs) with field details and defaults
- **FR-2.3**: C initializer code generation merging KG defaults with user overrides

#### FR-3: Dependency Analysis
- **FR-3.1**: Direct and transitive dependency resolution with topological init_sequence
- **FR-3.2**: API usage validation against dependency graph ordering
- **FR-3.3**: Polling requirement detection for APIs needing status checking

#### FR-4: V-Model Traceability
- **FR-4.1**: Full V-Model trace chains: Requirement → Architecture → Code → Test → Result
- **FR-4.2**: Module-wide traceability matrix generation (JSON/CSV/HTML output)
- **FR-4.3**: Coverage gap detection for incomplete trace chains
- **FR-4.4**: Hardware-software link analysis (register usage mapping)

#### FR-5: Multi-Format Ingestion
- **FR-5.1**: 11 file types supported: `.c`, `.h`, `.json`, `.rst`, `.puml`, `.pdf`, `.xlsx`, `.arxml`, `.md`, `.txt`, `.csv`
- **FR-5.2**: 10 specialized parsers for structured extraction
- **FR-5.3**: 3 external connectors: Jama (requirements), Jenkins (CI/CD), Polarion (ALM)
- **FR-5.4**: Single-file, module-level, batch, and repository-wide ingestion modes
- **FR-5.5**: Incremental ingestion with change detection

#### FR-6: Session & Working Memory
- **FR-6.1**: Session lifecycle management with TTL-based expiration
- **FR-6.2**: Token-budget-aware context assembly (greedily fills ≤8K tokens)
- **FR-6.3**: Ephemeral sandbox for per-session document exploration
- **FR-6.4**: Redis-backed sessions with in-memory fallback

#### FR-7: Review Gate
- **FR-7.1**: Deterministic confidence scoring (not LLM-based)
- **FR-7.2**: Automatic routing: AUTO (≥80), QUICK (50–79), FULL (<50)
- **FR-7.3**: Human feedback collection (APPROVE/APPROVE_WITH_EDITS/REJECT/ESCALATE)
- **FR-7.4**: Learning from review patterns for continuous improvement

#### FR-8: Multi-Step Context Assembly (RLM)
- **FR-8.1**: Query decomposition into max 6 targeted sub-queries
- **FR-8.2**: 23 task-type-aware planning with domain-specific prompts
- **FR-8.3**: Preview mode for inspecting query plans before execution

#### FR-9: Cache Layer
- **FR-9.1**: Two-tier caching: LRU exact match + SemanticCache (sentence-transformers cosine similarity, in-process)
- **FR-9.2**: TTL-based expiration with configurable thresholds
- **FR-9.3**: Module-scoped and full cache invalidation
- **FR-9.4**: Cache stats and performance monitoring

#### FR-10: Ontology Management
- **FR-10.1**: Dual-profile ontology (illd, mcal) with versioned schemas
- **FR-10.2**: Entity validation against ontology rules
- **FR-10.3**: Module-level ontology compliance scoring

### 2.2 Non-Functional Requirements

#### NFR-1: Security
- **NFR-1.1**: 3-tier RBAC (public, developer, admin) with Cerbos PDP enforcement
- **NFR-1.2**: Per-request API key authentication via HTTP headers
- **NFR-1.3**: Workspace-scoped role resolution
- **NFR-1.4**: Read-only Cypher execution (write clauses rejected)
- **NFR-1.5**: No credentials in version-controlled files (env-var resolution for all secrets)

#### NFR-2: Compliance
- **NFR-2.1**: ASPICE-compliant audit trail (every tool invocation logged)
- **NFR-2.2**: Response archiving for reproducibility
- **NFR-2.3**: Review evidence as formal work products
- **NFR-2.4**: ISO 26262 safety-critical awareness in confidence scoring

#### NFR-3: Performance
- **NFR-3.1**: LRU cache ~2500x speedup for exact matches
- **NFR-3.2**: SemanticCache cosine similarity scan ~5-10ms at ≤500 entries (in-process, O(n) scan via sentence-transformers)
- **NFR-3.3**: Expected ~60% cache hit rate under normal usage patterns
- **NFR-3.4**: Configurable token budgets for context assembly (8K default)

#### NFR-4: Reliability
- **NFR-4.1**: Graceful degradation when backends are unavailable
- **NFR-4.2**: Health checks for all infrastructure components
- **NFR-4.3**: Docker health checks with restart policies
- **NFR-4.4**: Write-through persistence with fallback to in-memory

#### NFR-5: Observability
- **NFR-5.1**: PostgreSQL audit logging for all tool invocations
- **NFR-5.2**: Prometheus metrics collection
- **NFR-5.3**: Grafana dashboards for visualization
- **NFR-5.4**: Graph statistics and coverage reporting
- **NFR-5.5**: MLFlow model registry (planned — not yet implemented)

#### NFR-6: Scalability
- **NFR-6.1**: Dual workspace support (illd, mcal)
- **NFR-6.2**: Module-level isolation via NodeSet anchors
- **NFR-6.3**: Async-ready design with Celery task wrapper support
- **NFR-6.4**: Thread-pool configuration for ingestion parallelism

### 2.3 Feature Matrix

| Feature | Status | Sprint | Category |
|---------|--------|--------|----------|
| Hybrid Search (vector + graph) | ✅ Complete | 2 | Core |
| Structured Node Queries | ✅ Complete | 2 | Core |
| Graph Traversal (neighbors, paths) | ✅ Complete | 2 | Core |
| Cypher Query Interface | ✅ Complete | 2 | Core |
| API Function Intelligence | ✅ Complete | 7 | Intelligence |
| Type Definition Resolution | ✅ Complete | 7 | Intelligence |
| C Code Generation | ✅ Complete | 7 | Intelligence |
| Dependency Analysis (transitive) | ✅ Complete | 7 | Intelligence |
| API Usage Validation | ✅ Complete | 7 | Intelligence |
| Polling Detection | ✅ Complete | 7 | Intelligence |
| V-Model Traceability | ✅ Complete | 7 | Intelligence |
| Traceability Matrix | ✅ Complete | 7 | Intelligence |
| Coverage Gap Detection | ✅ Complete | 7 | Intelligence |
| HW-SW Link Analysis | ✅ Complete | 7 | Intelligence |
| Multi-Format Ingestion | ✅ Complete | 5 | Ingestion |
| Connector Integration (Jama/Jenkins/Polarion) | ✅ Complete | 5 | Ingestion |
| Session Management | ✅ Complete | 2 | Memory |
| Context Builder (token-budget) | ✅ Complete | 2, 8 | Memory |
| Ephemeral Sandbox | ✅ Complete | 3 | Memory |
| RLM Orchestrator | ✅ Complete | 5 | Memory |
| LRU + SemanticCache (2-tier) | ✅ Complete | 6, 9 | Performance |
| Confidence Scoring | ✅ Complete | 4 | Quality |
| Human Feedback Loop | ✅ Complete | 4 | Quality |
| Review Gate Routing | ✅ Complete | 4 | Quality |
| Ontology Profiles | ✅ Complete | 6 | Config |
| RBAC (Cerbos + Tiers) | ✅ Complete | 1, 6 | Security |
| PostgreSQL Audit Schema | ✅ Complete | 8 | Observability |
| Prometheus + Grafana | ✅ Complete | 10 | Observability |
| GEST E2E Test | ✅ Complete | 8 | Testing |
| Docker Orchestration | ✅ Complete | 1 | Infrastructure |
| FeedbackSink Learning Loop | ✅ Complete | 9 | Quality |
| ResultProcessor (CI/CD) | ✅ Complete | 9 | Quality |
| process_results (full impl) | ✅ Complete | 9 | Quality |

---

## 3. System Architecture

### 3.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Domain Assistants (DAs)                       │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌───────┐  │
│  │ GEST │  │ CIA  │  │ ACRA │  │ SAGA │  │ REVA │  │  ...  │  │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └───┬───┘  │
│     │         │         │         │         │           │       │
└─────┼─────────┼─────────┼─────────┼─────────┼───────────┼───────┘
      │         │         │         │         │           │
      ▼         ▼         ▼         ▼         ▼           ▼
┌─────────────────────────────────────────────────────────────────┐
│              MCP Protocol Layer (JSON-RPC)                       │
│          Transport: streamable-http (HTTP)                       │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              Authentication & Authorization                │  │
│  │    HTTP Header → API Key → Cerbos PDP → 3-Tier RBAC       │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │                  MCP Server (FastMCP)                        ││
│  │           56 Tools across 13 Categories                      ││
│  │                                                              ││
│  │  ┌─────────────────────────────────────────────────────┐     ││
│  │  │  Cat 1: Search & Query (6)                          │     ││
│  │  │  Cat 2: API Intelligence (3)                        │     ││
│  │  │  Cat 3: Dependency Analysis (3)                     │     ││
│  │  │  Cat 4: Traceability (4)                            │     ││
│  │  │  Cat 5: Ingestion Pipeline (4)                      │     ││
│  │  │  Cat 6: Memory & Context (5+4 Sandbox + 2 RLM)     │     ││
│  │  │  Cat 7: Cache Management (4)                        │     ││
│  │  │  Cat 8: Feedback & Learning (4)                     │     ││
│  │  │  Cat 9: Review Gate (4)                             │     ││
│  │  │  Cat 10: Ontology & Config (4)                      │     ││
│  │  │  Cat 11: Observability & Health (6)                 │     ││
│  │  │  Cat 12: Visualization (1)                          │     ││
│  │  │  Cat 13: Authentication (2)                         │     ││
│  │  └─────────────────────────────────────────────────────┘     ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────┬──────────────────────────────────────────────────────-┘
           │
    ┌──────┴───────┬──────────────┬──────────────┬─────────────┐
    ▼              ▼              ▼              ▼             ▼
┌────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐
│ Neo4j  │  │  Qdrant  │  │  Redis   │  │ PostgreSQL │
│5.26    │  │  1.12    │  │  7       │  │  16        │
│        │  │          │  │          │  │            │
│Graph   │  │Vector    │  │Sessions  │  │Audit logs  │
│KG (illd│  │Embeddings│  │LRU Cache │  │Feedback    │
│+ mcal) │  │384-dim   │  │Working   │  │Review      │  │         │
│        │  │          │  │Memory    │  │evidence    │  │         │
└────────┘  └──────────┘  └──────────┘  └────────────┘  └─────────┘
```

### 3.2 Service Layer Architecture

The MCP server constructs service objects lazily via `_get_*()` singleton helpers. Each service encapsulates a specific domain:

```
mcp_server.py
    │
    ├── SearchService          ← Category 1 backend
    │      │── neo4j_driver
    │      │── qdrant_client
    │      └── embedding_model
    │
    ├── KnowledgeIntelligence  ← Categories 2-4 backend
    │      └── neo4j_driver
    │
    ├── IngestionService       ← Category 5 backend
    │      │── neo4j_driver
    │      │── parsers (10 specialized)
    │      │── connectors (Jama, Jenkins, Polarion)
    │      └── postgres_client (job tracking)
    │
    ├── SessionManager         ← Category 6 backend
    │      │── redis_client
    │      └── postgres_client (session metadata)
    │
    ├── ContextBuilder         ← Category 6 backend
    │      └── token-budget algorithm
    │
    ├── SandboxManager         ← Category 6 (Sandbox) backend
    │      │── EphemeralGraph (NetworkX per session)
    │      └── EphemeralVectors (in-memory per session)
    │
    ├── RLMOrchestrator        ← Category 6 (RLM) backend
    │      │── SearchService (sub-queries)
    │      └── LLM client (planning)
    │
    ├── CacheService           ← Category 7 backend
    │      │── LRUCache (exact match)
    │      └── SemanticCache (embedding similarity)
    │
    ├── ConfidenceCalculator   ← Category 9 backend
    │      └── deterministic formula
    │
    ├── FeedbackSink           ← Category 8 backend
    │      └── postgres_client (learning data)
    │
    ├── OntologyService        ← Category 10 backend
    │      └── OntologyLoader (YAML profiles)
    │
    ├── ObservabilityService   ← Category 11 backend
    │      └── neo4j_driver (graph stats)
    │
    └── AuthService            ← Category 13 backend
           └── TokenManager (JWT)
```

### 3.3 Data Flow — Search Query

```
Client Request
     │
     ▼
┌────────────────┐
│ MCP Server     │──► _authorize(tool, api_key)
│ search_database│         │
└────┬───────────┘         ▼
     │              ┌─────────────────┐
     │              │ Auth Middleware  │──► Cerbos PDP check
     │              └─────────────────┘
     ▼
┌────────────────┐
│ CacheService   │──► LRU exact check ──► Semantic similarity check
│ (2-tier)       │         │ HIT → return cached
└────┬───────────┘         │ MISS ↓
     ▼
┌────────────────┐
│ SearchService  │──► 5-stage pipeline:
│ hybrid_search  │    1. Query analysis (label inference, keyword extraction)
│                │    2. Graph search (Neo4j Cypher, label-aware)
│                │    3. Vector search (Qdrant cosine similarity)
│                │    4. RRF merge (alpha-blending)
│                │    5. Pagination
└────┬───────────┘
     │
     ▼
┌────────────────┐
│ CacheService   │──► Write to both LRU + Semantic tiers
│ (write-back)   │
└────┬───────────┘
     │
     ▼
  _ok(result) ──► {"error": false, "data": {...}}
```

### 3.4 Data Flow — Ingestion

> Ingestion is a platform-level operation (not an MCP tool). The flow below shows how `IngestionService.ingest_file()` works internally.

```
IngestionService.ingest_file(path, workspace, module)
     │
     ▼
┌───────────────────┐
│ IngestionService  │
│ _parse_file()     │──► Router by file extension:
│                   │    .c/.h → c_parser / illd_swa_parser / sfr_parser
│                   │    .json → JSON loader
│                   │    .rst  → rst_parser
│                   │    .puml → puml_parser
│                   │    .pdf  → pdf_parser (LLM-assisted)
│                   │    .xlsx → xlsx_parser
│                   │    .arxml→ arxml_parser
│                   │    .md/.txt/.csv → text extraction
└────┬──────────────┘
     │
     ▼
┌───────────────────┐
│ _write_to_kg()    │──► MERGE into Neo4j
│                   │    Create/update nodes with labels
│                   │    Create relationships
│                   │    Link to module NodeSet
└────┬──────────────┘
     │
     ▼
┌───────────────────┐
│ JobTracker        │──► Update progress → PostgreSQL
│                   │    Status: queued → processing → completed/failed
└───────────────────┘
```

### 3.5 Dual Workspace Model

AICE supports two product workspaces with distinct characteristics:

| Aspect | illd | mcal |
|--------|------|------|
| **Product** | iLLD reference software | MCAL productive software |
| **Compliance** | Relaxed | Strict (MISRA C:2012 + AUTOSAR) |
| **Modules** | ~12 (ADC, SPI, CAN, etc.) | ~15 + extended (29 Jama modules) |
| **Neo4j Database** | `illd` | `mcal` |
| **Node Types** | APIFunction, DataStructure, Register, etc. | StakeholderRequirement, ProductRequirement, VerificationStep, etc. |
| **Source** | Code parsing + HW specs | Jama requirements + code + test results |

Both workspaces share the same MCP server instance and tool set but use separate Neo4j databases and vector collections, ensuring data isolation.

---

## 4. MCP Tool Reference

### 4.1 Category 1: Search & Query (6 tools)

#### `search_database` — Hybrid Search
- **Tier**: public
- **Purpose**: Primary search entry point combining semantic vector search with knowledge graph traversal
- **Parameters**:
  - `query` (str, required): Natural language search query
  - `workspace` (str): "illd" or "mcal" (default: active instance)
  - `module_filter` (str): Restrict results to a specific module
  - `node_types` (list[str]): Filter by node labels
  - `alpha` (float, 0.0–1.0): Vector vs. graph blend (0.0 = all graph, 1.0 = all vector)
  - `include_relationships` (bool): Include neighboring relationships
  - `top_k` (int): Maximum results (default: 10)
- **Returns**: Ranked list of nodes with relevance scores, provenance indicators, and optional relationship context

#### `search_nodes` — Structured Node Search
- **Tier**: public
- **Purpose**: Deterministic structured query by label, keyword, and property filters
- **Parameters**:
  - `label` (str): Node type (e.g., "APIFunction", "Register")
  - `keyword` (str): Search keyword
  - `filters` (dict): Property-based filters
  - `workspace` (str): Target workspace
  - `limit` (int): Max results
- **Returns**: Matching nodes with all properties

#### `get_node_by_id` — Exact Lookup
- **Tier**: public
- **Purpose**: Retrieve a single node by document ID or Jama item ID
- **Parameters**:
  - `node_id` (str, required): Unique identifier
  - `workspace` (str): Target workspace
- **Returns**: Complete node with all properties and relationships

#### `get_neighbors` — Graph Traversal
- **Tier**: developer
- **Purpose**: Find directly connected nodes for a given node
- **Parameters**:
  - `node_id` (str, required): Starting node ID
  - `relationship_types` (list[str]): Filter by relationship type
  - `direction` (str): "in", "out", or "both"
  - `limit` (int): Max results
- **Returns**: Neighbor nodes with relationship metadata

#### `shortest_path` — Path Analysis
- **Tier**: developer
- **Purpose**: Find shortest path between two nodes in the knowledge graph
- **Parameters**:
  - `source_id` (str, required): Starting node
  - `target_id` (str, required): Destination node
  - `max_depth` (int): Maximum traversal depth
- **Returns**: Path as ordered list of nodes and relationships

#### `execute_cypher` — Raw Cypher Query
- **Tier**: developer
- **Purpose**: Execute read-only Cypher queries directly against Neo4j
- **Parameters**:
  - `query` (str, required): Cypher query (write clauses are rejected)
  - `params` (dict): Query parameters
  - `workspace` (str): Target workspace
- **Returns**: Query results as list of records
- **Security**: Write operations (CREATE, DELETE, SET, MERGE, DROP, REMOVE) are blocked

### 4.2 Category 2: API Intelligence (3 tools)

#### `query_api_function` — Function Intelligence
- **Tier**: public
- **Purpose**: Retrieve comprehensive information about an API function with 25+ fields
- **Parameters**:
  - `function_name` (str, required): API function name
  - `workspace` (str): Target workspace
- **Returns**: Enriched function data including:
  - Signature, parameters, return type
  - Module, file location
  - Dependencies (calls, called-by)
  - Traceability (linked requirements, test cases)
  - MISRA compliance notes
  - Initialization sequence position
  - Register accesses
  - Safety criticality (ASIL level)

#### `get_type_definition` — Type Resolution
- **Tier**: public
- **Purpose**: Retrieve struct, enum, or typedef definitions with fields and defaults
- **Parameters**:
  - `type_name` (str, required): Type name
  - `workspace` (str): Target workspace
- **Returns**: Full type definition with fields, C declaration, default values

#### `generate_initialization_code` — Code Generation
- **Tier**: public
- **Purpose**: Generate C initialization code by merging KG-stored defaults with user overrides
- **Parameters**:
  - `type_name` (str, required): Type/struct to initialize
  - `overrides` (dict): Custom field values
  - `workspace` (str): Target workspace
- **Returns**: Generated C code block

### 4.3 Category 3: Dependency Analysis (3 tools)

#### `query_dependencies` — Dependency Graph
- **Tier**: public
- **Purpose**: Resolve direct and transitive dependencies with topological initialization ordering
- **Parameters**:
  - `function_name` (str, required): Starting function
  - `depth` (int): Max traversal depth (default: 3)
  - `workspace` (str): Target workspace
- **Returns**: Dependency tree, topological init_sequence, direct/transitive counts

#### `validate_api_usage` — Usage Validation
- **Tier**: public
- **Purpose**: Check whether a sequence of API calls follows the correct dependency ordering
- **Parameters**:
  - `call_sequence` (list[str], required): Ordered list of function calls
  - `workspace` (str): Target workspace
- **Returns**: Validation result with violations marked

#### `detect_polling_requirements` — Polling Detection
- **Tier**: public
- **Purpose**: Identify APIs that require status polling after invocation
- **Parameters**:
  - `function_name` (str, required): Function to analyze
  - `workspace` (str): Target workspace
- **Returns**: Polling requirements with recommended patterns

### 4.4 Category 4: Traceability (4 tools)

#### `find_requirement_traces` — V-Model Traces
- **Tier**: public
- **Purpose**: Trace complete V-Model chains from requirements through architecture, code, tests, to results
- **Parameters**:
  - `requirement_id` (str, required): Requirement identifier
  - `workspace` (str): Target workspace
- **Returns**: Full trace chain with link quality metadata

#### `build_traceability_matrix` — Matrix Generation
- **Tier**: public
- **Purpose**: Generate module-wide traceability matrix
- **Parameters**:
  - `module` (str, required): Module name
  - `format` (str): Output format — "json", "csv", or "html"
  - `workspace` (str): Target workspace
- **Returns**: Complete traceability matrix in requested format

#### `find_coverage_gaps` — Gap Detection
- **Tier**: public
- **Purpose**: Identify missing links in requirement-code-test chains
- **Parameters**:
  - `module` (str, required): Module name
  - `workspace` (str): Target workspace
- **Returns**: List of gaps with severity and suggested actions

#### `analyze_hw_sw_links` — HW-SW Analysis
- **Tier**: public
- **Purpose**: Map hardware register usage to software functions and detect undocumented accesses
- **Parameters**:
  - `module` (str, required): Module name
  - `workspace` (str): Target workspace
- **Returns**: Register-to-function mapping, undocumented access warnings

### 4.5 Category 5: Ingestion Pipeline (0 MCP tools — platform operation)

> **Important**: The four direct ingestion MCP tools (`ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository`) were **removed from MCP registration** in Sprint 17 (Plan 2, Phase 2). The underlying `IngestionService` and all 17 parsers remain as library code used by the platform team for batch ingestion operations.
>
> **For DA developers**: Use `sandbox_upload` (Category 6) to ingest user-provided documents into a per-session ephemeral store. For production knowledge base updates, contact the platform team.

#### Platform-Level Ingestion (not MCP tools)

The following functions exist in the codebase (`src/IngestionPipeline/ingestion_service.py`) and can be invoked by the platform team:

| Function | Purpose |
|----------|---------|
| `ingest_file(file_path, module, workspace)` | Parse and ingest a single file |
| `ingest_module(repo_root, module, workspace)` | Ingest all files for a module |
| `batch_ingest(lld_path, modules, workspace)` | Parallel multi-module ingestion |
| `ingest_repository(repo_path, modules, workspace)` | Full repository ingestion |

**Supported file types (17 parsers)**: `.c`, `.h`, `.arxml`, `.pdf`, `.xlsx`, `.puml`, `.rst`, `.json`, `.md`, `.txt`, `.csv`, `.ea` (Enterprise Architect), `.xml` (SFR/HW spec), `.swud` (SWUD docs), doxygen headers, HW spec PDFs, iLLD SWA docs

**External connectors (3)**: Jama (requirements), Jenkins (CI/CD results), Polarion (ALM)

### 4.6 Category 6: Memory & Context (5 Session + 5 Sandbox + 2 RLM = 12 tools) + Category 5b HSI (1 tool)

#### Session Lifecycle (5 tools)

#### `session_start` — Open Session
- **Tier**: public
- **Purpose**: Initialize a working-memory session for a Domain Assistant
- **Parameters**:
  - `session_id` (str, required): Unique session identifier (convention: `{DA}_{timestamp}`)
  - `assistant_name` (str, optional): Domain Assistant name
  - `module_context` (str, optional): Default module context (e.g. "Adc")
- **Returns**: Session confirmation with metadata including `store_type` (RedisBackend or InMemoryBackend) and fixed `ttl_seconds: 3600`

#### `session_store` — Store Data
- **Tier**: public
- **Purpose**: Store a key-value pair in the active session
- **Parameters**:
  - `session_id` (str, required): Active session
  - `key` (str, required): Storage key
  - `value` (any, required): Data to store
- **Returns**: Confirmation

#### `session_retrieve` — Retrieve Data
- **Tier**: public
- **Purpose**: Retrieve session-scoped data by key
- **Parameters**:
  - `session_id` (str, required): Active session
  - `key` (str, required): Storage key
- **Returns**: Stored value

#### `build_context` — Context Assembly
- **Tier**: public
- **Purpose**: Assemble a token-budget-aware context payload from RAG results and session state
- **Parameters**:
  - `session_id` (str, required): Active session
  - `query` (str, required): The user query for context assembly
  - `search_results` (list): Pre-fetched search results
  - `max_tokens` (int): Token budget (default: 8192)
- **Returns**: Assembled context with provenance tracking

#### `session_end` — Close Session
- **Tier**: public
- **Purpose**: Close the session and persist the audit trail
- **Parameters**:
  - `session_id` (str, required): Session to close
- **Returns**: Session summary with audit metadata

#### Ephemeral Sandbox (4 tools)

#### `sandbox_upload` — Upload Documents
- **Tier**: public
- **Purpose**: Parse user-provided documents into per-session ephemeral KG and vector stores
- **Parameters**:
  - `session_id` (str, required): Active session
  - `file_path` (str, required): Document to upload
- **Returns**: Upload summary with extracted node/relationship counts
- **Limits**: Max 20 files, 50MB total per session

#### `sandbox_query` — Query Sandbox
- **Tier**: public
- **Purpose**: Search within the session's ephemeral stores
- **Parameters**:
  - `session_id` (str, required): Active session
  - `query` (str, required): Search query
- **Returns**: Results from ephemeral graph + vector stores

#### `sandbox_status` — Sandbox Status
- **Tier**: public
- **Purpose**: Inspect loaded files, node counts, and storage stats
- **Parameters**:
  - `session_id` (str, required): Active session
- **Returns**: Status report

#### `sandbox_clear` — Clear Sandbox
- **Tier**: public
- **Purpose**: Explicitly release ephemeral storage before session TTL expires
- **Parameters**:
  - `session_id` (str, required): Active session
- **Returns**: Confirmation

#### `sandbox_diff` — Diff Sandbox vs Production
- **Tier**: public
- **Purpose**: Show what changed in the sandbox relative to production knowledge. Compares sandbox nodes against their production counterparts and reports nodes added, modified (with original properties), and unchanged production nodes.
- **Parameters**:
  - `session_id` (str, required): Active session with a sandbox
- **Returns**: `{ "nodes_added": [str], "nodes_modified": [{node_id, original, current}], "nodes_unchanged": int, "edges_added": int, "edges_total": int }`

### 4.5b Category 5b: HSI — Hardware-Software Interface (1 tool)

#### `get_function_hsi` — HSI Section Extraction
- **Tier**: public
- **Purpose**: Extract the Hardware-Software Interface (HSI) section for a function in SWUD format. Returns SFR registers accessed (with access type, trust zone, line numbers), global/shared variables used, and events.
- **Parameters**:
  - `function_name` (str, required): Exact function name (e.g. "Adc_Init", "Can_Write")
  - `module` (str): Module name (default: "Adc")
  - `profile` (str): Ontology profile — "mcal" (default) or "illd"
- **Returns**: `{ "function_name": str, "registers": [...], "global_variables": [...], "events": [], "summary_text": str }` — `summary_text` is a Markdown-formatted SWUD HSI section

#### RLM (2 tools)

#### `rlm_orchestrate` — Multi-Step Context
- **Tier**: public
- **Purpose**: Decompose complex queries into targeted sub-queries for richer context assembly
- **Parameters**:
  - `query` (str, required): Complex query
  - `task_type` (str): One of 23 task types (auto-detected if not specified)
  - `session_id` (str): Active session for context reuse
  - `workspace` (str): Target workspace
- **Returns**: Synthesized context from up to 6 sub-queries

#### `rlm_plan_preview` — Preview Plan
- **Tier**: public
- **Purpose**: Show planned sub-queries without executing them
- **Parameters**:
  - `query` (str, required): Query to plan
  - `task_type` (str): Task type hint
- **Returns**: Planned sub-queries with alpha values and expected targets

### 4.7 Category 7: Cache Management (5 tools)

#### `cache_get` — Inspect Cache
- **Tier**: developer
- **Purpose**: Check if a cache entry exists for a given query
- **Parameters**:
  - `query` (str, required): Query to check
- **Returns**: Cache hit/miss status with entry metadata if hit

#### `cache_stats` — Cache Metrics
- **Tier**: developer
- **Purpose**: Retrieve cache performance metrics
- **Returns**: LRU and SemanticCache stats (hit rate, size, semantic cache enabled status, total entries)

#### `cache_invalidate_module` — Module Invalidation
- **Tier**: admin
- **Purpose**: Invalidate all cache entries related to a specific module
- **Parameters**:
  - `module` (str, required): Module to invalidate
- **Returns**: Number of invalidated entries

#### `cache_clear` — Clear Cache
- **Tier**: admin
- **Purpose**: Clear entire cache or selected tiers
- **Parameters**:
  - `tier` (str): "lru", "semantic", or "all" (default: "all")
- **Returns**: Confirmation with cleared entry count

#### `cache_refresh_config` — Reload Cache Config
- **Tier**: admin
- **Purpose**: Reload cache configuration from environment variables without restarting the server. Re-reads `LRU_CACHE_SIZE`, `LRU_CACHE_TTL_HOURS`, `SEMANTIC_CACHE_MAX_SIZE`, `SEMANTIC_CACHE_THRESHOLD`, and `SEMANTIC_CACHE_TTL_DAYS`. Cached data is preserved; entries are only evicted if the new size is smaller.
- **Parameters**: None
- **Returns**: `{ "lru_max_size": {old, new}, "lru_default_ttl": {old, new}, "semantic_max_size": {old, new}, "semantic_threshold": {old, new}, "semantic_ttl_seconds": {old, new}, "evicted": int }`

### 4.8 Category 8: Feedback & Learning (4 tools)

#### `submit_human_feedback` — Record Feedback
- **Tier**: public
- **Purpose**: Record human review decision and feed into learning loop. APPROVE decisions are stored as ApprovedPattern nodes in Neo4j and indexed in Qdrant for future similarity matching (enables the confidence scorer's 'has_proven_patterns' +15 signal).
- **Parameters**:
  - `response_id` (str, required): Response being reviewed
  - `decision` (str, required): APPROVE, APPROVE_WITH_EDITS, REJECT, ESCALATE
  - `reviewer_id` (str): Reviewer identifier
  - `issues_found` (int): Number of issues found
  - `correction_notes` (str): Reviewer comments / correction details
  - `module` (str): MCAL module name for pattern scoping
  - `task_type` (str): Task type for pattern categorization
  - `response_context` (str): The actual response text to store as approved pattern
- **Returns**: Feedback ID + pattern_stored (bool) + pattern_indexed (bool)

#### `get_learning_metrics` — Learning Stats
- **Tier**: developer
- **Purpose**: Retrieve approval/rejection rates, pattern counts, and learning trends
- **Parameters**:
  - `module` (str): Filter by module
  - `time_range` (str): Time window
- **Returns**: Metrics summary

#### `get_failure_patterns` — Pattern Query
- **Tier**: developer
- **Purpose**: Query learned failure patterns for specific modules or categories
- **Parameters**:
  - `module` (str): Module filter
  - `category` (str): Category filter
- **Returns**: Ranked failure patterns with occurrence counts

#### `process_results` — Result Processing
- **Tier**: admin
- **Purpose**: Parse test/analysis results from external tools, create TestResult nodes in the knowledge graph, and feed failures into the learning loop
- **Parameters**:
  - `results_dir` (str, required): Path to result files (single file or directory)
  - `result_type` (str, required): "vp", "polyspace", "junit", "coverage", "compiler"
  - `module_name` (str): MCAL module name (e.g., "Adc", "Spi")
  - `learn_from_failures` (bool): Record failures in FeedbackSink (default: true)
  - `update_graph` (bool): Create TestResult nodes in Neo4j (default: true)
  - `workspace_id` (str): Target workspace (default: "illd")
- **Returns**: Processing summary with pass/fail counts, graph nodes created, failures learned
- **Supported formats**: JUnit XML, VP simulation XML, Polyspace CSV/XML/PSBF/PSCP, GCOV/LCOV/Cobertura, GCC/Tasking compiler logs

### 4.9 Category 9: Review Gate (4 tools)

#### `evaluate_confidence` — Confidence Scoring
- **Tier**: public
- **Purpose**: Compute deterministic confidence score for a DA response and determine review routing
- **Parameters**:
  - `response` (dict, required): DA response to evaluate
  - `context` (dict): Query context
  - `session_id` (str): Session for historical data
- **Returns**: Score (0–100), review type (AUTO/QUICK/FULL), signal breakdown

#### `complete_review` — Close Review Gate
- **Tier**: public
- **Purpose**: Record the final review outcome and close the gate
- **Parameters**:
  - `review_id` (str, required): Review to close
  - `outcome` (str, required): Final decision
  - `reviewer` (str): Reviewer identity
- **Returns**: Confirmation with archived evidence

#### `override_review_routing` — Routing Override
- **Tier**: developer
- **Purpose**: Override automatic review type routing (e.g., escalate AUTO to FULL)
- **Parameters**:
  - `review_id` (str, required): Active review
  - `new_type` (str, required): Target review type
  - `reason` (str, required): Escalation reason
- **Returns**: Updated routing

#### `get_review_analytics` — Review Metrics
- **Tier**: developer
- **Purpose**: Retrieve review gate performance and accuracy metrics
- **Returns**: Analytics including override rates, accuracy by routing type, average review times

### 4.10 Category 10: Ontology & Config (4 tools)

#### `list_ontology_profiles` — Profile Listing
- **Tier**: public
- **Purpose**: List available ontology profiles (illd, mcal)
- **Returns**: Profile names with metadata

#### `get_ontology_schema` — Schema Query
- **Tier**: public
- **Purpose**: Retrieve ontology schema for a profile, optionally enriched with live node counts
- **Parameters**:
  - `profile` (str, required): "illd" or "mcal"
  - `include_counts` (bool): Include live node counts from Neo4j
- **Returns**: Node types, relationship types, property schemas, node counts

#### `validate_entity` — Entity Validation
- **Tier**: developer
- **Purpose**: Validate an entity against ontology rules
- **Parameters**:
  - `entity` (dict, required): Entity data to validate
  - `profile` (str, required): Target ontology profile
- **Returns**: Validation result with violations

#### `get_ontology_compliance` — Compliance Scoring
- **Tier**: developer
- **Purpose**: Compute ontology compliance score for a module
- **Parameters**:
  - `module` (str, required): Module to evaluate
  - `profile` (str, required): Ontology profile
- **Returns**: Compliance percentage with violation details

### 4.11 Category 11: Observability & Health (6 tools)

#### `health_check` — System Health
- **Tier**: public
- **Purpose**: Check connectivity to Neo4j, Qdrant, Redis, GPT4IFX, and PostgreSQL
- **Parameters**:
  - `verbose` (bool): Include detailed diagnostics
- **Returns**: Service-by-service health status

#### `get_graph_statistics` — Graph Stats
- **Tier**: public
- **Purpose**: Get node and relationship counts per type from Neo4j
- **Parameters**:
  - `workspace` (str): Target workspace
- **Returns**: Count summaries by label and relationship type

#### `list_available_modules` — Module Listing
- **Tier**: public
- **Purpose**: List all modules known to the knowledge graph
- **Parameters**:
  - `workspace` (str): Target workspace
- **Returns**: Module names with node counts

#### `get_distribution` — Distribution Analysis
- **Tier**: public
- **Purpose**: Analyze distributions by key dimensions
- **Parameters**:
  - `dimension` (str, required): "status", "asil", "domain", or custom
  - `workspace` (str): Target workspace
- **Returns**: Distribution counts and percentages

#### `get_coverage_report` — Coverage Report
- **Tier**: public
- **Purpose**: Aggregate traceability coverage percentages across modules
- **Parameters**:
  - `module` (str): Specific module or all
  - `workspace` (str): Target workspace
- **Returns**: Coverage metrics per V-Model phase

#### `detect_communities` — Community Detection
- **Tier**: developer
- **Purpose**: Run graph community detection algorithms to find clusters
- **Parameters**:
  - `algorithm` (str): Detection algorithm
  - `workspace` (str): Target workspace
- **Returns**: Community memberships and metrics

### 4.12 Category 12: Visualization (1 tool)

#### `visualize_subgraph` — Subgraph Rendering
- **Tier**: developer
- **Purpose**: Render a subgraph as interactive pyvis HTML
- **Parameters**:
  - `center_node_id` (str, required): Central node
  - `depth` (int): Expansion depth
  - `workspace` (str): Target workspace
- **Returns**: Path to generated HTML file

### 4.13 Category 13: Authentication (2 tools)

#### `get_token_info` — Token Inspection
- **Tier**: developer
- **Purpose**: Inspect JWT token timing (issued-at, expires-at, expired status)
- **Returns**: Token metadata

#### `ensure_valid_token` — Token Refresh
- **Tier**: admin
- **Purpose**: Force-refresh the GPT4IFX JWT using configured credentials
- **Returns**: New token status

### 4.14 Category 14: GAP v2 Tools (1 tool)

#### `query_enhance` — Query Complexity Analysis
- **Tier**: developer
- **Purpose**: Classify query complexity and predict the optimal search strategy. Exposes the QueryEnhancer preprocessing stage for upstream analysis. Entirely rule-based with zero LLM dependency and sub-millisecond latency.
- **Parameters**:
  - `query` (str, required): Natural language query to analyze
  - `include_synonyms` (bool): Include expanded domain synonyms (default: false)
- **Returns**:
  ```json
  {
    "original_query": "str",
    "enhanced_query": "str",
    "complexity": "SIMPLE | MEDIUM | COMPLEX",
    "strategy": "GRAPH_HEAVY | VECTOR_HEAVY | HYBRID | EXACT",
    "suggested_alpha": 0.5,
    "suggested_max_results": 10,
    "detected_entities": ["str"],
    "detected_modules": ["str"],
    "is_aggregation": false,
    "token_budget_hint": 4096
  }
  ```

---

## 5. Authentication & Authorization

### 5.1 Architecture

AICE uses a layered auth model:

```
HTTP Request
     │
     ▼
┌─────────────────────────────┐
│ ASGI Middleware              │
│ Extract API Key from Header │
│ Authorization: Bearer <key> │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│ API Key Registry            │
│ (mcp/auth/api_keys.yaml)   │
│ key → principal_id + roles  │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│ Cerbos PDP                  │ ← Production authorization
│ Per-request RBAC decisions  │
│ Workspace-scoped roles      │
└─────────────┬───────────────┘
              │ (if Cerbos unavailable)
              ▼
┌─────────────────────────────┐
│ Local Fallback              │
│ Tool-tier hierarchy check   │
│ admin ⊃ developer ⊃ public  │
└─────────────────────────────┘
```

### 5.2 Tier Hierarchy

| Tier | Tool Count | Access Level |
|------|-----------|-------------|
| **public** | 34 | Any authenticated caller |
| **developer** | 16 | Developer + Admin API keys |
| **admin** | 5 | Admin API keys only |

Hierarchy: `admin` can invoke all tools, `developer` can invoke developer + public tools, `public` can invoke only public tools.

### 5.3 Cerbos Policies

Policy files in `mcp/auth/policies/`:

- **derived_roles.yaml**: Defines role inheritance (admin includes developer permissions, developer includes public)
- **resource_mcp_tool.yaml**: Per-tool access control for all 55 active tools across the 3 tiers

### 5.4 Transport Mode

| Transport | Auth Method | Use Case |
|-----------|-------------|----------|
| **streamable-http** | HTTP `Authorization` header | Production deployment (all environments) |

---

## 6. Storage Backends

### 6.1 Neo4j — Knowledge Graph

| Property | Value |
|----------|-------|
| **Version** | 5.26.0-community |
| **Plugins** | APOC + GDS (Graph Data Science) |
| **Databases** | `illd`, `mcal` (dual workspace) |
| **Embedding Dimension** | 384 |
| **Similarity Threshold** | 0.85 |
| **Port** | 7687 (Bolt), 7474 (HTTP) |

**Node Types (illd)**: APIFunction, DataStructure, Register, BitField, Requirement, TestCase, Module, File, etc.

**Node Types (mcal)**: StakeholderRequirement (SHRQ), ProductRequirement (PRQ), VerificationStep (PVS), VerificationReport (PVR), Component, TestCase, etc.

**Key Relationships**: IMPLEMENTS, TRACES_TO, CALLS, DEPENDS_ON, HAS_PARAMETER, ACCESSES_REGISTER, HAS_BITFIELD, HAS_MODULE, TESTED_BY, VERIFIED_BY

**Module Isolation**: Every ingested node is linked via `[:HAS_MODULE]` to a `NodeSet` anchor node, enabling module-scoped queries.

### 6.2 Qdrant — Vector Store

| Property | Value |
|----------|-------|
| **Version** | 1.12.1 |
| **Embedding Model** | all-MiniLM-L6-v2 (384-dim) |
| **Distance** | Cosine |
| **HNSW Config** | m=16, ef_construct=200 |
| **Port** | 6333 (REST), 6334 (gRPC) |
| **Collection Naming** | `{project}_{module}` |

Collections store semantic embeddings for each document type (functions, structs, enums, requirements, test cases, etc.).

### 6.3 Redis — Sessions & Cache

| Property | Value |
|----------|-------|
| **Version** | 7-alpine |
| **Max Memory** | 256MB |
| **Eviction Policy** | allkeys-lru |
| **Port** | 6379 |
| **Session TTL** | 3600s (configurable) |
| **Cache TTL** | 86400s (configurable) |

Used for:
- Session data storage (working memory)
- LRU cache tier (exact match queries)
- Temporary data with TTL management

### 6.4 PostgreSQL — Audit & Persistence

| Property | Value |
|----------|-------|
| **Version** | 16-alpine |
| **Port** | 5432 |
| **Database** | `aice_meta` |

7 tables for ASPICE compliance:

| Table | Purpose | Key Fields |
|-------|---------|-----------|
| `audit_logs` | Every MCP tool invocation | tool, caller, workspace, session, params, status, duration |
| `response_archive` | DA-generated outputs | response_id, content hash, full response, model used |
| `review_evidence` | Human review decisions | review_id, decision, reviewer, comments, evidence |
| `feedback_records` | Learning data from feedback | feedback_id, decision, response_id, module, patterns |
| `failure_patterns` | Learned failure patterns | pattern_id, category, module, frequency, root_cause |
| `ingestion_jobs` | Async ingestion tracking | job_id, status, progress%, files, created/updated times |
| `sessions_meta` | Cross-process session visibility | session_id, assistant, module, created, closed, summary |

**Graceful Degradation**: When PostgreSQL is unavailable, all writes become no-ops. The system operates normally with in-memory state only, losing cross-process persistence and audit trail.

### 6.5 GPT4IFX — LLM Endpoint

| Property | Value |
|----------|-------|
| **URL** | https://gpt4ifx.icp.infineon.com |
| **Auth** | JWT via token_manager (auto-refreshed from IFX credentials) |
| **Models** | gpt-4o, gpt-4o-mini, text-embedding-3-small |

GPT4IFX is Infineon's internal LLM endpoint. Authentication is handled
automatically by `token_manager.py` which obtains and refreshes JWT tokens
using `IFX_USERNAME` / `IFX_PASSWORD` credentials provided at container start.

Model assignments:
| Role | Model |
|------|-------|
| Default | gpt-4o |
| Fast/cheap | gpt-4o-mini |
| Embedding | text-embedding-3-small |

---

## 7. Ingestion Pipeline

### 7.1 Overview

The Ingestion Pipeline transforms raw artifacts (source code, requirements documents, test results, architecture diagrams) into structured knowledge within the Neo4j graph and Qdrant vector store.

### 7.2 Supported File Types & Parsers

| File Type | Parser | Key Extractions |
|-----------|--------|----------------|
| `.c` | `c_parser` | Functions, call graphs, register R/W patterns, switch-case blocks. Uses regex + optional clang AST. |
| `.h` (iLLD SWA) | `illd_swa_parser` | Macros, typedefs, enums, structs, prototypes. Optional LLM enrichment for descriptions. |
| `.h` (Registers) | `sfr_parser` | Register definitions, bitfields, bit ranges. |
| `.json` | JSON loader | Structured data direct import. |
| `.rst` | `rst_parser` | Sections with title, heading level, body text. |
| `.puml` | `puml_parser` | Sequence diagrams → function frequency, phase/loop/polling patterns, participants. |
| `.pdf` | `pdf_parser` | LLM-assisted (gpt-4o vision) page-by-page Markdown conversion with heading/section/table extraction. |
| `.xlsx` | `xlsx_parser` | Worksheets, merged cell handling, structured row-dict objects, header detection. |
| `.arxml` | `arxml_parser` | EB tresos macros, ECUC containers, module configurations, cross-references. |
| `.md` / `.txt` / `.csv` | Text parser | Generic text extraction with section detection. |

### 7.3 External Connectors

| Connector | System | Features |
|-----------|--------|----------|
| **JamaConnector** | Jama (Requirements Management) | REST API with API-key auth, pagination, incremental sync (`modifiedSince`), exponential backoff |
| **JenkinsConnector** | Jenkins (CI/CD) | JUnit XML result parsing, build log retrieval, `jenkinsapi` library |
| **PolarionConnector** | Polarion (ALM) | 10 REST endpoints, Bearer JWT auth, work items, baselines, releases, test cases |

### 7.4 Ingestion Flow

```
1. Platform-level invocation (IngestionService.ingest_file / .ingest_module / .batch_ingest / .ingest_repository — not MCP tools)
      │
      ▼
2. Job creation → IngestionJobTracker assigns ID, status = "queued"
      │
      ▼
3. File discovery (for module/batch/repo modes: scan directory tree)
      │
      ▼
4. Parse phase → Router dispatches by extension to appropriate parser
      │
      ▼
5. Normalization → Parser output → common intermediate structure
      │                              (nodes: [{label, properties}],
      │                               relationships: [{type, source, target}])
      │
      ▼
6. KG write → MERGE into Neo4j, link to module NodeSet
      │
      ▼
7. Vector write → Generate embeddings, upsert into Qdrant collection
      │
      ▼
8. Job update → Status = "completed" / "failed", write to PostgreSQL
```

### 7.5 Incremental Ingestion

The `/src/IngestionPipeline/Incremental/incremental_ingestion.py` module supports:
- Change detection based on file modification timestamps
- Re-ingestion of only modified files
- Connector-level incremental sync (e.g., Jama `modifiedSince` parameter)

---

## 8. Memory Layer

### 8.1 Overview

The Memory Layer is the "librarian" of AICE — it decides what knowledge reaches the LLM within a given token budget. It consists of five subsystems:

```
Memory Layer
├── SessionManager        ← Session lifecycle & data storage
├── ContextBuilder        ← Token-budget-aware context assembly
├── EphemeralSandbox     ← Per-session temporary stores
├── SemanticMemory       ← Approved pattern index & storage
└── WorkingMemory        ← Ontology-validated session state
```

### 8.2 Session Manager

**Backend options**:
- **RedisSessionBackend**: Production backend using Redis with TTL support
- **DictBackend**: In-memory fallback for development

**Session data model**:
```python
SessionData:
  session_id: str
  assistant_name: str
  module_context: str
  ttl_seconds: int
  created_at: datetime
  data: dict[str, any]
```

**PostgreSQL write-through**: Optional persistence to `sessions_meta` table for cross-process session visibility.

### 8.3 Context Builder

The Sprint 8 v2 Context Builder uses a **slot-based token-budget** algorithm:

**10 Context Slots** (ordered by priority):
1. System prompt (reserved)
2. Conversation history (20% budget)
3. Session context (5% budget)
4. Primary query results
5. Related API functions
6. Dependency chains
7. Requirements trace
8. Code examples
9. Approved patterns
10. Module overview

**Fill Algorithm** (5 phases):
1. Reserve fixed slots (system, conversation, session)
2. Compute remaining budget
3. Greedily fill slots by priority until budget exhausted
4. Redistribute unused budget from low-priority to high-priority
5. Render final context with provenance markers

### 8.4 Ephemeral Sandbox

A "third storage tier" for user-uploaded documents that shouldn't be persisted to the main knowledge graph:

- **EphemeralGraph**: NetworkX graph per session (temporary KG)
- **EphemeralVectors**: In-memory vector store per session
- **Safety limits**: 20 files max, 50MB total per session
- **Cleanup**: Automatic on session TTL expiry, or explicit via `sandbox_clear`

Use case: A Domain Assistant can upload customer-specific specs for the current session without polluting the shared knowledge base.

### 8.5 Semantic Memory

Stores and indexes **approved patterns** — DA responses that were approved by human reviewers:
- **PatternStore**: CRUD for `ApprovedPattern` nodes in Neo4j (MERGE-based, usage count tracking)
- **PatternIndex**: Qdrant-backed similarity search (threshold 0.8) for finding relevant approved patterns
- **Collection**: `{profile}_{module}` (e.g., `mcal_adc`)

### 8.6 Working Memory

Ontology-validated session state with:
- `Session` dataclass with `ContextEntry` list
- TTL enforcement on every read operation
- Redis or in-memory backend

### 8.7 Node Sets

Module isolation pattern using Neo4j anchor nodes:
- **NodeSetManager**: Creates `NodeSet` anchor nodes per module
- **CollectionManager**: Creates Qdrant vector collections per module (HNSW config: m=16, ef=200, 384-dim cosine)
- **ScopedQuery**: All queries automatically scoped through `MATCH (ns:NodeSet) -[:HAS_MODULE]-> (node)` pattern

---

## 9. Search & Hybrid RAG

### 9.1 Overview

The search subsystem implements a 5-stage hybrid retrieval pipeline combining structured graph queries with semantic vector search.

### 9.2 Search Pipeline

```
Stage 1: Query Analysis
  ├── Label inference from query text (NER + pattern matching)
  ├── Keyword extraction
  └── Entity-targeted lookup detection

Stage 2: Graph Search (Neo4j)
  ├── Label-aware Cypher queries
  ├── CONTAINS keyword filtering
  └── NodeSet-scoped module isolation

Stage 3: Vector Search (Qdrant)
  ├── Embed query using all-MiniLM-L6-v2
  ├── Cosine similarity search across relevant collections
  └── Top-k retrieval per collection

Stage 4: Result Fusion
  ├── Reciprocal Rank Fusion (RRF) with K=60
  ├── Alpha-blending (user-configurable weight)
  └── Deduplication by node ID

Stage 5: Post-Processing
  ├── Pagination
  ├── 1-hop graph expansion (optional)
  └── Relationship enrichment (optional)
```

### 9.3 Alpha Blending

The `alpha` parameter controls the balance between vector and graph search:

| Alpha | Behavior | Best For |
|-------|----------|----------|
| 0.0 | Pure graph search | Exact structural queries, relationship traversal |
| 0.3 | Graph-heavy hybrid | API lookups, dependency chains |
| 0.5 | Balanced | General queries |
| 0.7 | Vector-heavy hybrid | Natural language, concept search |
| 1.0 | Pure vector search | Semantic similarity, fuzzy matching |

### 9.4 Knowledge Intelligence

The Sprint 7 `KnowledgeIntelligenceService` provides enriched backends for Categories 2–4:

**API Intelligence**:
- `query_api_function()`: 25+ field enrichment via multi-hop graph traversal
- `get_type_definition()`: Struct/enum resolution with field details
- `generate_initialization_code()`: C code generation with KG defaults + user overrides

**Dependency Analysis**:
- `query_dependencies()`: Transitive closure with topological sort for init_sequence
- `validate_api_usage()`: Call sequence validation against dependency DAG
- `detect_polling_requirements()`: Pattern detection for status-polling APIs

**Traceability**:
- `find_requirement_traces()`: Full V-Model chain traversal
- `build_traceability_matrix()`: Module-wide matrix in JSON/CSV/HTML
- `find_coverage_gaps()`: Missing link detection
- `analyze_hw_sw_links()`: Register-to-function mapping

---

## 10. Review Gate & Confidence Scoring

### 10.1 Confidence Formula

The `ConfidenceCalculator` uses a **deterministic formula** (not LLM-based):

```
Base Score = 50

Quality Signals (add points):
  +30  has_kg_context        (response backed by KG data)
  +20  high_relevance        (search results >0.85 similarity)
  +15  has_proven_patterns   (matches approved patterns)
  +10  format_correct        (output matches expected format)
  +10  misra_compliant       (no MISRA violations detected)
  +20  has_dependency_order  (correct initialization order)

Risk Signals (subtract points):
  -30  missing_requirements  (no requirements trace found)
  -20  low_relevance         (search results <0.5 similarity)
  -15  novel_pattern         (no approved patterns matched)
  -20  compliance_warnings   (MISRA/AUTOSAR issues detected)
  -10  complex_logic         (high cyclomatic complexity)
  -15  is_safety_critical    (ASIL-rated component)

Final Score = clamp(base + sum(quality) - sum(risk), 0, 100)
```

### 10.2 Routing Thresholds

| Score Range | Review Type | Expected Duration | Description |
|-------------|------------|-------------------|-------------|
| **≥ 80** | AUTO | ~5 minutes | High confidence — automated approval with spot-check |
| **50 – 79** | QUICK | ~15–20 minutes | Moderate confidence — focused review on flagged concerns |
| **< 50** | FULL | ~1+ hour | Low confidence — comprehensive expert review required |

### 10.3 Feedback Loop

```
DA Response
    │
    ▼
evaluate_confidence() → Score + Routing
    │
    ├── AUTO (≥80) → Auto-approve or spot-check
    ├── QUICK (50-79) → Focused human review
    └── FULL (<50) → Full expert review
    │
    ▼
submit_human_feedback(decision, comments, edits)
    │
    ├── APPROVE → PatternStore (Neo4j) + PatternIndex (Qdrant)
    ├── APPROVE_WITH_EDITS → PatternStore (Neo4j, confidence=0.75) + PatternIndex (Qdrant)
    ├── REJECT → save_failure_pattern() → PostgreSQL (with module/task metadata)
    └── ESCALATE → reassign to senior reviewer
    │
    ▼
FeedbackSink → PostgreSQL (feedback_records, failure_patterns)
PatternStore → ApprovedPattern nodes in Neo4j (for confidence scorer's +15 signal)
PatternIndex → Qdrant semantic index (for future similarity matching)
```

---

## 11. Cache Service

### 11.1 Three-Tier Architecture

```
Query arrives
    │
    ▼
┌─────────────────────────┐
│ Tier 1: LRU Cache       │ ← Exact string match
│ Thread-safe OrderedDict  │
│ Max: 1000 entries       │
│ TTL: configurable       │
│ Speedup: ~2500x         │
└──────┬──────────────────┘
       │ MISS
       ▼
┌─────────────────────────┐
│ Tier 2: SemanticCache   │ ← In-process cosine similarity
│ Model: MiniLM-L6-v2     │   sentence-transformers O(n) scan
│ Max: 500 entries        │   cosine similarity ≥ 0.85
│ Latency: ~5-10ms        │
└──────┬──────────────────┘
       │ MISS
       ▼
  Full Hybrid RAG execution
       │
       ▼
  Write-through to both tiers
```

### 11.2 Cache Key Dimensions

Cache entries are keyed by the complete query signature:
- Query text
- Workspace (illd/mcal)
- Module filter
- Node type filters
- Alpha value
- Include relationships flag

Different parameter combinations produce distinct cache entries, preventing cross-workspace or cross-filter cache pollution.

### 11.3 Embedding Model

- **Model**: `all-MiniLM-L6-v2` (Sentence Transformers)
- **Dimension**: 384
- **Local cache**: `local_models/` directory
- **Fallback**: When `sentence-transformers` is unavailable, semantic cache is disabled (LRU-only mode).

---

## 12. RLM Orchestrator

### 12.1 Concept

The **Recursive Language Model (RLM) Orchestrator** is an internal Core Engine capability that decomposes complex queries into targeted sub-queries for richer context assembly. It operates below the MCP interface and above Hybrid RAG execution.

### 12.2 Three Context Assembly Strategies

| Strategy | Trigger | Behavior |
|----------|---------|----------|
| **Standard** | Simple, focused queries | Direct ranking + trimming |
| **Deterministic Expansion** | Structured queries (API lookups, traces) | Fixed rules for graph/API/traceability expansion |
| **RLM** | Complex, cross-domain, multi-concept queries | LLM-planned decomposition → sub-queries → synthesis |

### 12.3 Task Types

23 task types mapped to 21 Domain Assistants:

| Category | Task Types | DAs |
|----------|-----------|-----|
| Requirements | `requirement_review`, `requirement_drafting`, `requirement_management` | REVA, PRQ, RMA |
| Architecture | `architecture_analysis`, `architecture_traceability` | SAGA, ATRA |
| Design & Code | `code_generation`, `code_transformation`, `code_review`, `config_generation`, `page_generation` | CIA, CTA, ACRA, GECA, PAGE |
| Testing | `test_generation`, `test_verification`, `test_quality_analysis` | GEST, GEVT, ATQA |
| Safety | `misra_review`, `safety_validation`, `safety_analysis`, `hazop_analysis`, `data_flow_analysis` | Specialized safety DAs |
| Traceability | `traceability` | TripleA |
| Debug | `debug_analysis` | VoltAI |
| Infrastructure | `knowledge_ingestion` | KW |
| Utility | `stop_typing`, `generic` | All |

### 12.4 RLM Execution Flow

```
rlm_orchestrate(query, task_type)
    │
    ▼
1. Task type detection (auto or user-specified)
    │
    ▼
2. LLM Planning Phase
   - System prompt with task-specific planning instructions
   - Generates ≤6 sub-queries with individual alpha values
   - Each sub-query: {query, alpha, target_labels, purpose}
    │
    ▼
3. Sequential Sub-Query Execution
   For each sub-query:
   - SearchService.hybrid_search(sub_query, alpha=sub_alpha)
   - Budget: 8K tokens per sub-query
   - Results accumulated
    │
    ▼
4. Synthesis Phase
   - Merge sub-query results
   - Deduplicate by node ID
   - Rank by aggregate relevance
   - Trim to total token budget
    │
    ▼
5. Return synthesized context
```

### 12.5 Task-Specific Planning

Each task type has a tailored planning prompt. Example for `test_generation` (GEST):

> *Decompose the test generation query into sub-queries:*
> *1. Retrieve the requirement being tested (alpha=0.3, graph-heavy)*
> *2. Find the API functions under test (alpha=0.3)*
> *3. Look up function dependencies and init sequence (alpha=0.2)*
> *4. Find existing test patterns for similar modules (alpha=0.7, vector-heavy)*
> *5. Check register access patterns for HW-related tests (alpha=0.3)*
> *6. Find MISRA constraints relevant to test design (alpha=0.5)*

---

## 13. Observability & Monitoring

### 13.1 PostgreSQL Audit Trail

Every MCP tool invocation is logged to the `audit_logs` table:

```sql
CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT NOW(),
    tool        TEXT NOT NULL,
    caller      TEXT,
    workspace   TEXT,
    session_id  TEXT,
    params      JSONB,
    status      TEXT,       -- 'ok' or 'error'
    duration_ms INTEGER,
    error_code  TEXT
);

CREATE INDEX idx_audit_ts ON audit_logs (ts);
CREATE INDEX idx_audit_tool ON audit_logs (tool);
```

### 13.2 Prometheus Metrics

Prometheus scrapes metrics from:
| Target | Port | Metrics |
|--------|------|---------|
| mcp-server | 8000 | Tool call counts, latency, error rates |
| neo4j | 2004 | Query counts, heap usage, page cache hits |

### 13.3 Grafana Dashboards

Pre-configured dashboards for:
- **MCP Server Overview**: Tool call rates, error rates, latency percentiles
- **Knowledge Graph Health**: Node/relationship counts, query performance
- **Cache Performance**: Hit rates, eviction counts, size trends
- **LLM Usage**: Token consumption, model distribution, latency
- **Ingestion Pipeline**: Job throughput, failure rates, queue depth

### 13.4 Health Checks

The `health_check` tool provides real-time infrastructure status:

```json
{
  "error": false,
  "data": {
    "status": "healthy",
    "services": {
      "neo4j": {"status": "ok", "latency_ms": 12},
      "qdrant": {"status": "ok", "latency_ms": 5},
      "redis": {"status": "ok", "latency_ms": 2},
      "gpt4ifx": {"status": "ok", "latency_ms": 45},
      "postgres": {"status": "ok", "latency_ms": 8}
    },
    "uptime_seconds": 86400,
    "tool_count": 56
  }
}
```

---

## 14. User Guide

### 14.1 For Domain Assistant Developers

#### Connecting to AICE

The AICE MCP server is **already deployed and running** on the Infineon Cloud. Domain Assistants connect via HTTP with an API key — no server-side setup required.

> **See [MCP_QUICKSTART.md](MCP_QUICKSTART.md) for the full setup guide** with Python, VS Code, curl, and CI/CD examples.

**HTTP (Recommended — all environments)**:
```python
import httpx

AICE_URL = "https://<aice-host>/mcp"   # Get from your platform team
API_KEY  = "key-gest-001"              # Your assigned API key

client = httpx.Client(
    base_url=AICE_URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=60.0,
)

response = client.post("/", json={
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "search_database", "arguments": {"query": "ADC initialization", "workspace": "illd"}},
    "id": 1,
})
```

**VS Code / Copilot Chat** — add to `.vscode/mcp.json`:
```json
{
  "servers": {
    "aice": {
      "type": "http",
      "url": "https://<aice-host>/mcp",
      "headers": { "Authorization": "Bearer key-cia-001" }
    }
  }
}
```

#### Standard Session Lifecycle

Every Domain Assistant should follow this 6-step pattern:

```python
# Step 1: Start session
session_start(session_id="GEST_20260322_001", assistant_name="GEST",
              module_context="Adc", ttl_seconds=3600)

# Step 2: Search for relevant knowledge
results = search_database(query="ADC channel group conversion API",
                          workspace="illd", module_filter="Adc", alpha=0.5)

# Step 3: Optionally upload additional documents
sandbox_upload(session_id="GEST_20260322_001",
               file_path="/path/to/customer_spec.pdf")

# Step 4: Build context within token budget
context = build_context(session_id="GEST_20260322_001",
                        query="Generate tests for Adc_StartGroupConversion",
                        search_results=results, max_tokens=8192)

# Step 5: [DA performs its domain-specific work using the context]
# e.g., GEST generates test code, ACRA reviews code, CIA generates code

# Step 6: Evaluate confidence and complete review
evaluation = evaluate_confidence(response=da_output, context=context,
                                 session_id="GEST_20260322_001")

# Step 7: Submit human feedback (or auto-approve if score ≥ 80)
if evaluation["data"]["review_type"] == "AUTO":
    complete_review(review_id=evaluation["data"]["review_id"], outcome="approved")
else:
    # Human reviews the output
    submit_human_feedback(response_id=evaluation["data"]["response_id"],
                          decision="APPROVE",
                          correction_notes="Looks good",
                          module="Adc",
                          task_type="test_generation",
                          response_context=da_output_text)

# Step 8: Close session
session_end(session_id="GEST_20260322_001")
```

### 14.2 For Administrators

#### Ingesting New Knowledge

> **Note**: Direct ingestion MCP tools were removed in Sprint 17. Contact the platform team to trigger production ingestion runs. Platform team invokes `IngestionService` directly.

For DA developers who need to load user-provided documents into a session:

```python
# Upload user documents to your per-session sandbox (available as MCP tool)
sandbox_upload(session_id="YOUR_SESSION", file_path="/path/to/spec.pdf")

# Then query via search_database with session routing
search_database(query="ADC init sequence", session_id="YOUR_SESSION", workspace_id="illd")
```

#### Managing Cache

```python
# Check cache performance
stats = cache_stats()

# Invalidate cache after re-ingestion
cache_invalidate_module(module="Adc")

# Clear all caches
cache_clear(tier="all")
```

#### Monitoring System Health

```python
# Quick health check
health_check()

# Detailed health check
health_check(verbose=True)

# Graph statistics
get_graph_statistics(workspace="illd")

# Available modules
list_available_modules(workspace="illd")
```

### 14.3 For Developers

#### Using Advanced Search

```python
# Graph-heavy search (structural queries)
search_database(query="Adc_StartGroupConversion dependencies",
                alpha=0.2, workspace="illd")

# Vector-heavy search (conceptual queries)
search_database(query="how to configure ADC for continuous scanning",
                alpha=0.8, workspace="illd")

# Direct Cypher queries
execute_cypher(
    query="MATCH (f:APIFunction)-[:CALLS]->(g:APIFunction) "
          "WHERE f.name = $name RETURN g.name, g.module",
    params={"name": "Adc_StartGroupConversion"},
    workspace="illd"
)

# Subgraph visualization
visualize_subgraph(center_node_id="Adc_StartGroupConversion",
                   depth=2, workspace="illd")
```

#### API Intelligence

```python
# Get comprehensive function details
func = query_api_function(function_name="Adc_StartGroupConversion",
                          workspace="illd")
# Returns: signature, parameters, dependencies, traceability, MISRA notes

# Resolve dependencies
deps = query_dependencies(function_name="Adc_StartGroupConversion",
                          depth=3, workspace="illd")
# Returns: dependency tree, topological init_sequence

# Validate API call sequence
result = validate_api_usage(
    call_sequence=["Adc_Init", "Adc_SetupResultBuffer", "Adc_StartGroupConversion"],
    workspace="illd"
)
# Returns: validation result with any ordering violations
```

#### Traceability

```python
# Full V-Model trace
traces = find_requirement_traces(requirement_id="SHRQ-12345",
                                  workspace="mcal")

# Coverage matrix
matrix = build_traceability_matrix(module="Adc", format="html",
                                    workspace="mcal")

# Find gaps
gaps = find_coverage_gaps(module="Adc", workspace="mcal")
```

### 14.4 Multi-Step Queries with RLM

For complex queries that span multiple knowledge domains:

```python
# Preview the query plan
plan = rlm_plan_preview(
    query="Generate comprehensive test cases for Adc_StartGroupConversion "
          "covering all dependency initialization, register access patterns, "
          "and MISRA compliance requirements",
    task_type="test_generation"
)
# Returns: planned sub-queries with alpha values

# Execute multi-step context assembly
context = rlm_orchestrate(
    query="Generate comprehensive test cases for Adc_StartGroupConversion...",
    task_type="test_generation",
    session_id="GEST_20260322_001",
    workspace="illd"
)
# Returns: synthesized context from up to 6 targeted sub-queries
```

### 14.5 MCP Response Format

All tools return responses in a standard envelope:

**Success**:
```json
{
  "error": false,
  "data": {
    // Tool-specific response data
  }
}
```

**Error**:
```json
{
  "error": true,
  "error_code": "NOT_FOUND",
  "message": "Node with ID 'xyz' not found in workspace 'illd'"
}
```

Common error codes:
| Code | Meaning |
|------|---------|
| `NOT_FOUND` | Requested entity does not exist |
| `AUTH_FAILED` | Authentication failure |
| `PERMISSION_DENIED` | Insufficient permissions (tier mismatch) |
| `VALIDATION_ERROR` | Invalid input parameters |
| `BACKEND_ERROR` | Storage backend unavailable or error |
| `TIMEOUT` | Operation exceeded time limit |

---

## 15. API Reference

### 15.1 MCP Protocol

AICE implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) specification. All interactions use JSON-RPC 2.0 over the configured transport.

**Tool Call**:
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "search_database",
    "arguments": {
      "query": "ADC initialization sequence",
      "workspace": "illd",
      "alpha": 0.5,
      "top_k": 10
    }
  },
  "id": 1
}
```

**Tool List**:
```json
{
  "jsonrpc": "2.0",
  "method": "tools/list",
  "params": {},
  "id": 2
}
```

### 15.2 Response Envelope

All 55 tools return data wrapped in a consistent envelope:

```typescript
// Success
{
  error: false,
  data: {
    // Tool-specific payload
  }
}

// Error
{
  error: true,
  error_code: string,
  message: string
}
```

### 15.3 Common Parameter Patterns

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workspace` | string | Active instance | "illd" or "mcal" |
| `module` | string | — | Module name (e.g., "Adc", "Spi") |
| `session_id` | string | — | Active session identifier |
| `alpha` | float | 0.5 | Vector vs. graph search blend |
| `top_k` | int | 10 | Maximum result count |
| `verbose` | bool | false | Include extended diagnostics |

---

## 16. Ontology Reference

### 16.1 illd Profile — Node Types

| Label | Description | Key Properties |
|-------|-------------|---------------|
| `APIFunction` | C API function | name, module, signature, parameters, return_type, description |
| `DataStructure` | Struct/union | name, fields, c_definition, module |
| `Enum` | Enumeration | name, values, module |
| `Typedef` | Type alias | name, underlying_type, module |
| `Register` | HW register | name, address, size, module |
| `BitField` | Register field | name, bit_range, access, parent_register |
| `Requirement` | Technical requirement | id, title, description, status, priority |
| `TestCase` | Test case | id, name, type, linked_requirement |
| `Module` | Software module | name, version, workspace |
| `File` | Source file | path, type, module |

### 16.2 mcal Profile — Node Types

| Label | Description | Key Properties | Jama Count |
|-------|-------------|---------------|------------|
| `StakeholderRequirement` (SHRQ) | Jama AU3GM top-level requirement | jama_id, title, description, status, asil, domain, importance | ~9,036 |
| `ProductRequirement` (PRQ) | Derived product requirement | jama_id, title, description, status, asil, priority | ~6,055 |
| `VerificationStep` (PVS) | Verification procedure | jama_id, title, method, status | ~2,396 |
| `VerificationReport` (PVR) | Verification result | jama_id, title, result, status | — |
| `Component` | Software component | name, module, type | — |
| `TestCase` | Test case | id, name, type, result | — |

### 16.3 Key Relationship Types

| Relationship | Source → Target | Description |
|-------------|----------------|-------------|
| `IMPLEMENTS` | Code → Requirement | Code implements a requirement |
| `TRACES_TO` | Requirement → Requirement | Traceability between requirement levels |
| `CALLS` | Function → Function | Function call dependency |
| `DEPENDS_ON` | Function → Function | Initialization dependency |
| `HAS_PARAMETER` | Function → Parameter | Function parameter |
| `ACCESSES_REGISTER` | Function → Register | Register read/write |
| `HAS_BITFIELD` | Register → BitField | Register bitfield |
| `HAS_MODULE` | NodeSet → Node | Module membership |
| `TESTED_BY` | Requirement → TestCase | Test coverage |
| `VERIFIED_BY` | Requirement → VerificationStep | Verification coverage |
| `DERIVED_FROM` | PRQ → SHRQ | Requirement derivation |

### 16.4 MCAL Modules (Jama AU3GM)

ADC, CAN, Crypto, DIO, DMA, DMU, ETH, FLS, FlsLoader, GPT, GTM, I2C, ICU, IRQ, ISR, LIN, MCU, Ocu, PORT, PWM, Sent, SPI, STM, WDG, UART, and more.

### 16.5 Jama Field Value Maps

**Status**: Active, Approved, Deleted, Draft, Fulfilled, In Progress, Not Applicable, Rejected, Reviewed, Under Review

**ASIL**: QM, ASIL-A, ASIL-B, ASIL-C, ASIL-D

**Domain**: Application Software, Application_Hardware, Application_Mechanics, Basis Software, Complex Device Driver, Microcontroller Abstraction, Service Layer

**Importance**: Low, Medium, High, Mandatory

---

## 17. Glossary

| Term | Definition |
|------|------------|
| **AICE** | AI Core Engine — the MCP server platform |
| **ASPICE** | Automotive SPICE — process assessment model for automotive software |
| **ASIL** | Automotive Safety Integrity Level (QM, A, B, C, D) |
| **AUTOSAR** | AUTomotive Open System ARchitecture — standardized software architecture |
| **AURIX** | Infineon's multi-core microcontroller family for automotive |
| **Cerbos** | Open-source authorization engine (Policy Decision Point) |
| **DA** | Domain Assistant — specialized LLM-based agent |
| **FastMCP** | Python implementation of the MCP server |
| **GEST** | Test Generation Domain Assistant |
| **CIA** | Code Intelligence Assistant |
| **ACRA** | Automated Code Review Assistant |
| **SAGA** | Software Architecture Gap Analyst |
| **HW-SW** | Hardware-Software interface |
| **iLLD** | Infineon Low-Level Drivers |
| **ISO 26262** | International functional safety standard for automotive |
| **KG** | Knowledge Graph (Neo4j) |
| **MCAL** | Microcontroller Abstraction Layer (AUTOSAR) |
| **MCP** | Model Context Protocol — protocol for LLM tool interaction |
| **MISRA C** | Motor Industry Software Reliability Association — C coding standard |
| **NodeSet** | Graph design pattern for module-scoped data isolation |
| **RLM** | Recursive Language Model — multi-step context assembly strategy |
| **RRF** | Reciprocal Rank Fusion — score merging algorithm |
| **SHRQ** | Stakeholder Requirement (Jama item type) |
| **PRQ** | Product Requirement (Jama item type) |
| **PVS** | Product Verification Step |
| **PVR** | Product Verification Report |
| **TTL** | Time To Live — expiration duration |
| **V-Model** | Systems engineering model mapping requirements → tests |

---

*Document updated for Sprint 25 codebase. Version 2.1.0.*
*For questions or contributions, contact the AI Core Engine team.*

# Changelog — Workshop Open Points Implementation

**Branch:** `further_enhancements_for_gaps`
**Date:** 2026-04-13
**Base commit:** `7c898a3`

---

## Overview

Implements all 6 workshop open points across 4 phases:

| # | Open Point | Phase |
|---|-----------|-------|
| 1 | New document type ingestion (errata, datasheets, user manuals, releases) | 2A |
| 2 | MCP fully async + autoscaling | 4A–4C |
| 3 | Sandbox-session integration + mixed sandbox/DB RAG | 1B, 3A |
| 4 | Ingestion access control change (ADMIN → DEVELOPER + sandbox targeting) | 2B |
| 5 | EA direct query for MCAL with AI Governance audit trail | 3B |
| 6 | Multi-branch RAG isolation using TAG sets | 1A |

---

## Phase 1: Foundation

### 1A — Multi-Branch RAG with TAG Sets

**New files:**
- `src/HybridRAG/code/querier/branch_filter.py` — `BranchFilter` utility generating Neo4j WHERE clauses and Qdrant `FieldCondition` for branch-scoped queries. Supports `"common"` tag via `MatchAny`.
- `scripts/migrate_branch_tags.py` — Migration script to backfill `branch_tag = "main"` on existing Neo4j nodes and Qdrant payloads.

**Modified files:**
- `src/HybridRAG/config/ontology.yaml` — Added `branch_tag` (string, default `"main"`) as a universal property to all node types in both ILLD and MCAL profiles. Added 4 new node types: `ErrataItem`, `DatasheetSpec`, `UserManualSection`, `ReleaseInfo`. Added 4 new relationship types: `ERRATA_AFFECTS`, `SPEC_BELONGS_TO`, `UM_REFERENCES`, `RELEASE_CONTAINS`.
- `src/HybridRAG/code/querier/search_service.py` — `hybrid_search()`, `_vector_search()`, and `_graph_search()` now accept optional `branch_tag` parameter for filtered queries.
- `src/IngestionPipeline/ingestion_service.py` — `Neo4jBatchWriter` now accepts `branch_tag` and includes it in all MERGE/SET queries and vector point payloads.
- `src/MemoryLayer/memory/ontology_loader.py` — Updated to handle new node types during ontology validation.
- `mcp/core/mcp_server.py` — Added `branch_tag` parameter to all search tools (`search_database`, `search_nodes`, `get_neighbors`, `execute_cypher`) and ingestion tools. New tool: `list_branch_tags(workspace_id, module)`.

### 1B — Sandbox-Session Lifecycle Fix

**Modified files:**
- `mcp/core/mcp_server.py` — `session_end()` now destroys the associated sandbox via `SandboxManager.destroy_sandbox(session_id)` to prevent memory leaks.
- `src/MemoryLayer/memory/ephemeral_sandbox.py` — Added `created_at` timestamp to `EphemeralSandbox`. Added `SandboxManager.cleanup_stale(max_age_seconds=7200)` for periodic cleanup.
- `mcp/app.py` — Wired APScheduler job to call `cleanup_stale()` every 10 minutes.

---

## Phase 2: New Document Types + Ingestion Tier Changes

### 2A — New Document Type Ingestion

**New files (4 parsers):**
- `src/IngestionPipeline/Parsers/errata_parser.py` — Parses errata/advisory docs (.pdf, .xlsx, .csv). Extracts severity, affected modules, workarounds. Detection: "errata" or "advisory" in filename.
- `src/IngestionPipeline/Parsers/datasheet_parser.py` — Parses datasheets (.pdf, .xlsx). Structures parametric tables (min/typ/max). Detection: "datasheet" or "ds_" in filename.
- `src/IngestionPipeline/Parsers/user_manual_parser.py` — Parses user manuals (.pdf, .md, .rst). Hierarchical section extraction with parent-child tree. Detection: "user_manual", "um_", "usermanual" in filename.
- `src/IngestionPipeline/Parsers/release_parser.py` — Parses changelogs and release metadata (.md, .txt, .json). Detection: "release", "changelog", "version" in filename.

**Modified files:**
- `src/IngestionPipeline/Parsers/__init__.py` — Registered 4 new parsers in `__all__` with lazy imports.
- `src/IngestionPipeline/ingestion_service.py` — `_parse_file()` now checks filename patterns for the 4 new parsers before falling through to extension-based dispatch. `_write_to_kg()` routes `errata`, `datasheet`, `user_manual`, `release` parse types to new writer methods.

### 2B — Ingestion Access Control (ADMIN → DEVELOPER + Sandbox Targeting)

**Modified files:**
- `mcp/core/tool_tiers.py` — Changed `ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository` from `ADMIN` to `DEVELOPER`. Added `promote_sandbox_to_db` as `ADMIN`.
- `mcp/core/mcp_server.py`:
  - Added `_get_caller_role()` helper resolving API key → principal → highest role.
  - `ingest_file` + `ingest_module_from_repo`: Developer callers must provide `session_id`; parsed data routes to sandbox via `SandboxIngester.ingest_parsed()`. Admin callers write to production KG (or sandbox if `session_id` given).
  - `batch_ingest_modules` + `ingest_repository`: Admin-only for production writes. Developer callers get actionable error directing them to use `ingest_file`/`ingest_module_from_repo` with `session_id`.
  - New tool: `promote_sandbox_to_db` (ADMIN) — extracts sandbox data and writes to production KG with `branch_tag`.
- `src/IngestionPipeline/ingestion_service.py` — Added `parse_file()` public method (parse without KG write) for sandbox consumption.
- `src/MemoryLayer/memory/ephemeral_sandbox.py` — Added `SandboxIngester.ingest_parsed()` method routing by `parse_type` to 5 type-specific handlers (`_ingest_c_analysis`, `_ingest_errata`, `_ingest_datasheet`, `_ingest_user_manual`, `_ingest_release`).

---

## Phase 3: Hybrid Query + EA Direct Query

### 3A — Mixed-Mode Sandbox + DB Query

**New files:**
- `src/MemoryLayer/memory/hybrid_sandbox_querier.py` — `HybridSandboxQuerier` class merging sandbox (via `SandboxQuerier`) and production DB (via `SearchService.hybrid_search`) results with configurable weights (default 0.6/0.4), deduplication by `node_id`, and origin tracking (`"sandbox"` or `"db"`).

**Modified files:**
- `mcp/core/mcp_server.py` — New tool: `sandbox_hybrid_query(session_id, query, top_k, sandbox_weight, branch_tag, filter_by_module, workspace_id)`.
- `mcp/core/tool_tiers.py` — Registered `sandbox_hybrid_query` as `PUBLIC`.

### 3B — EA Direct Query for MCAL + AI Governance Audit

**New files:**
- `src/IngestionPipeline/ea_direct_query.py` — `EADirectQueryService` class providing on-the-fly EA model queries via `_EAModelExtractor`. Lazy model loading, name→element index, Redis caching (MD5-hashed keys, configurable TTL). Methods: `query_component()`, `search(query, scope)`, `get_architecture_tree(module)`, `get_statistics()`.

**Modified files:**
- `src/Observability/postgres_schema.py` — Added `ea_access_audit` table (id, timestamp, tool_name, ea_path, query_params, caller_id, result_count, latency_ms) with indexes. Added `PostgresClient.log_ea_access()` method.
- `mcp/core/mcp_server.py` — Added `_log_ea_audit()` helper and 3 new DEVELOPER tools:
  - `ea_query_component(ea_path, component_name, project, mode)` — Detailed component info
  - `ea_search(ea_path, query, scope, project, mode)` — Search across EA model
  - `ea_get_architecture(ea_path, module, project, mode)` — Architecture tree view
  - All tools log to `ea_access_audit` via Postgres for AI Governance traceability.
- `mcp/core/tool_tiers.py` — Registered `ea_query_component`, `ea_search`, `ea_get_architecture` as `DEVELOPER`.

---

## Phase 4: Fully Async MCP + Autoscaling

### 4A — Async Backend Pool

**New files:**
- `mcp/core/async_backends.py` — `AsyncBackendPool` class providing async connection-pooled clients:
  - `neo4j(profile)` → `neo4j.AsyncGraphDatabase` driver with verify_connectivity
  - `qdrant()` → `qdrant_client.AsyncQdrantClient` with in-cluster gRPC auto-detection
  - `redis()` → `redis.asyncio.Redis` with connection pooling (max 20)
  - `health()` → per-backend health check returning overall status
  - `close()` → graceful shutdown of all backends
  - Shared config helpers: `_load_neo4j_profile_config()`, `_load_neo4j_pool_config()`, `_resolve_qdrant_config()`

### 4B — Shared Cache Manager

**New files:**
- `mcp/core/shared_cache.py` — `SharedCacheManager` with two-tier caching:
  - L1: Process-local dict (30s TTL, 500 entry cap with LRU eviction)
  - L2: Redis (5min TTL, shared across all MCP replicas)
  - Serialization: `msgpack` (fast binary) with `json` fallback
  - `get(key)`, `set(key, value, ttl)`, `delete(key)`
  - `invalidate_module(module)` — Scans L1 + Redis SCAN for module-matching keys
  - `clear()` — Bulk clear both tiers
  - `stats()` — Hit rates, entry counts, Redis memory usage

**Modified files:**
- `requirements.txt` — Added `msgpack>=1.0`.

### 4C — Nginx Load Balancer + Docker Compose Multi-Replica

**New files:**
- `nginx/nginx.conf` — Reverse proxy configuration:
  - `upstream mcp_backend` with `least_conn` load balancing
  - Sticky sessions via `X-Session-Id` header (required for sandbox state)
  - Rate limiting: 30 req/s per IP, burst 50
  - SSE/streaming support (`proxy_buffering off`, HTTP/1.1, `Connection ""`)
  - 300s read/send timeouts for long-running tools (ingestion, RLM)
  - 100MB max request body for file ingestion
  - `/health`, `/metrics`, `/mcp` route separation

**Modified files:**
- `docker-compose.yml`:
  - `mcp-server`: Removed `container_name` (incompatible with replicas). Added `deploy.replicas: ${MCP_REPLICAS:-3}` with resource limits (2 CPU / 4G mem, reservations 0.5 CPU / 1G). Changed `ports` to `expose` (nginx handles external routing).
  - New service: `nginx` (nginx:1.25-alpine) — exposed on port 8000, mounts `nginx/nginx.conf`, health check via `/health`.
- `mcp/core/mcp_server.py` — Added `/health` JSON endpoint to ASGI app (both Prometheus-enabled and disabled paths). Returns `{"status": "healthy"|"degraded", "neo4j": bool, "qdrant": bool, "redis": bool}` with 200/503 status code.

---

## New MCP Tools Summary

| Tool | Tier | Category |
|------|------|----------|
| `list_branch_tags` | PUBLIC | Observability |
| `sandbox_hybrid_query` | PUBLIC | Ephemeral Sandbox |
| `promote_sandbox_to_db` | ADMIN | Ingestion |
| `ea_query_component` | DEVELOPER | EA Direct Query |
| `ea_search` | DEVELOPER | EA Direct Query |
| `ea_get_architecture` | DEVELOPER | EA Direct Query |

## Tier Changes

| Tool | Old Tier | New Tier |
|------|----------|----------|
| `ingest_file` | ADMIN | DEVELOPER |
| `ingest_module_from_repo` | ADMIN | DEVELOPER |
| `batch_ingest_modules` | ADMIN | DEVELOPER |
| `ingest_repository` | ADMIN | DEVELOPER |

## New Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `msgpack` | ≥1.0 | Fast binary serialization for Redis shared cache |

## File Statistics

- **New files:** 12
- **Modified files:** 14
- **Lines added:** ~1,416
- **Lines removed:** ~48

## Deferred (by design)

| Item | Reason |
|------|--------|
| CacheService wiring (M06) | Requires separate architectural decision |
| TLS inter-service (H15) | Infrastructure team responsibility |
| Kubernetes HPA autoscaling | Docker Compose first, K8s later |
| RelevanceJudge LLM wiring (H07) | Needs deeper refactor |

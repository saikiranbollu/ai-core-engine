# Observability Architecture

**Component**: `src/Observability/`, `src/Configuration/services.py`
**Primary classes**: `PostgresClient`, `ObservabilityService`, `metrics` module
**Backing store**: PostgreSQL 16, Prometheus, Grafana

---

## Table of Contents

1. [Overview](#1-overview)
2. [PostgreSQL Audit Schema](#2-postgresql-audit-schema)
3. [Audit Logging](#3-audit-logging)
4. [Graph Statistics](#4-graph-statistics)
5. [Prometheus Metrics](#5-prometheus-metrics)
6. [Grafana Dashboards](#6-grafana-dashboards)
7. [Health Checks](#7-health-checks)
8. [Graceful Degradation](#8-graceful-degradation)
9. [File Map](#9-file-map)

---

## 1. Overview

AICE observability is designed for **ASPICE compliance**: every tool invocation is logged, every DA response is archived, and every review decision is persisted as a formal work product. The observability stack consists of:

- **PostgreSQL 16** — durable storage for audit logs, feedback, and operational data (7 tables)
- **ObservabilityService** — graph statistics and health monitoring via Neo4j queries
- **Prometheus** — real-time time-series metrics collection (25 metrics, 15s scrape interval)
- **Grafana** — pre-provisioned datasource with a 10-panel overview dashboard plus 6 specialized dashboards
- **Health checks** — Kubernetes liveness/readiness probes and an in-process `health_check` tool for all backends
- **Best-effort persistence** — all PostgreSQL writes are non-blocking; failures are logged but never crash the server

```
MCP Tool Invocation
    │
    ├── _audit_log() ──────────► PostgreSQL: audit_logs
    │
    ├── Prometheus metrics ────► /metrics endpoint (scraped by Prometheus)
    │                            ├── tool request count + duration
    │                            ├── cache hit/miss ratio
    │                            ├── active sessions gauge
    │                            └── RLM sub-query histogram
    │
    ├── Response archived ─────► PostgreSQL: response_archive
    │
    ├── Review verdict ────────► PostgreSQL: review_evidence
    │                                        feedback_records
    │
    └── Ingestion progress ────► PostgreSQL: ingestion_jobs

Graph Stats (on-demand)
    │
    └── ObservabilityService ──► Neo4j: node/relationship counts,
                                        module distribution,
                                        coverage metrics
```

---

## 2. PostgreSQL Audit Schema

`PostgresClient` (479 lines) manages a 7-table schema (plus a `da_productivity` analytics view). Tables are auto-created on first connection.

### Table Definitions

#### `audit_logs` — Tool Invocation Audit Trail

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `timestamp` | TIMESTAMPTZ | When the tool was invoked |
| `api_key` | VARCHAR(100) | Which DA invoked it |
| `tool_name` | VARCHAR(100) | Which tool was called |
| `workspace_id` | VARCHAR(50) | Target workspace (illd/mcal) |
| `parameters` | JSONB | Tool input parameters |
| `duration_ms` | FLOAT | Execution time |
| `success` | BOOLEAN | Whether the call succeeded |
| `error_message` | TEXT | Error details if failed |

#### `response_archive` — DA Response Archive

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `response_id` | VARCHAR(100) UNIQUE | Unique response identifier |
| `session_id` | VARCHAR(100) | Owning session |
| `assistant_name` | VARCHAR(50) | DA that produced this response |
| `query` | TEXT | The original query |
| `response` | JSONB | Full response payload |
| `confidence_score` | FLOAT | Score from ConfidenceCalculator |
| `review_type` | VARCHAR(20) | AUTO/QUICK/FULL |
| `created_at` | TIMESTAMPTZ | Creation timestamp |

#### `review_evidence` — Review Work Products

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `response_id` | VARCHAR(100) | Links to response_archive |
| `reviewer` | VARCHAR(100) | Who reviewed |
| `verdict` | VARCHAR(30) | APPROVE/APPROVE_WITH_EDITS/REJECT/ESCALATE |
| `comments` | TEXT | Reviewer comments |
| `edits_applied` | JSONB | Diff of edits made |
| `reviewed_at` | TIMESTAMPTZ | Review timestamp |

#### `feedback_records` — Structured Feedback

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `response_id` | VARCHAR(100) | Links to response_archive |
| `feedback_type` | VARCHAR(30) | Classification |
| `content` | JSONB | Structured feedback data |
| `source` | VARCHAR(50) | Feedback source (human/automated) |
| `created_at` | TIMESTAMPTZ | Timestamp |

#### `failure_patterns` — Recurring Failure Tracking

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `pattern_signature` | VARCHAR(255) | Failure pattern identifier |
| `module` | VARCHAR(50) | Affected module |
| `occurrence_count` | INTEGER | How many times seen |
| `last_seen` | TIMESTAMPTZ | Most recent occurrence |
| `details` | JSONB | Aggregated failure details |

#### `ingestion_jobs` — Ingestion Job Tracking

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `job_id` | VARCHAR(100) UNIQUE | Job identifier |
| `file_path` | TEXT | Source file |
| `module` | VARCHAR(50) | Target module |
| `workspace_id` | VARCHAR(50) | Target workspace |
| `status` | VARCHAR(20) | queued/processing/completed/failed |
| `progress` | INTEGER | 0-100% |
| `node_count` | INTEGER | Nodes created |
| `relationship_count` | INTEGER | Relationships created |
| `error_message` | TEXT | Error if failed |
| `created_at` | TIMESTAMPTZ | Job start |
| `completed_at` | TIMESTAMPTZ | Job end |

#### `sessions_meta` — Session Metadata

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `session_id` | VARCHAR(100) UNIQUE | Session identifier |
| `assistant_name` | VARCHAR(50) | DA name |
| `module_context` | VARCHAR(50) | Default module |
| `started_at` | TIMESTAMPTZ | Session start |
| `ended_at` | TIMESTAMPTZ | Session end |
| `tool_calls` | INTEGER | Total tool invocations |
| `audit_summary` | JSONB | Session summary |

---

## 3. Audit Logging

Every MCP tool invocation triggers an audit log write:

```python
def _audit_log(api_key, tool_name, workspace_id, params, duration_ms, success, error=None):
    postgres = _get_postgres_client()
    if postgres:
        postgres.insert("audit_logs", {
            "api_key": api_key,
            "tool_name": tool_name,
            "workspace_id": workspace_id,
            "parameters": json.dumps(params),
            "duration_ms": duration_ms,
            "success": success,
            "error_message": error,
        })
```

The write is **best-effort**: wrapped in try/except, failures logged but not propagated. This ensures a PostgreSQL issue never blocks a tool invocation.

---

## 4. Graph Statistics

`ObservabilityService` (in `services.py`) provides Neo4j-based analytics:

### Available Metrics

| Tool | Method | Returns |
|------|--------|---------|
| `health_check` | Pings all backends | Backend status (up/down) for Neo4j, Qdrant, Redis, PostgreSQL, Cerbos |
| `graph_stats` | `get_graph_stats()` | Total nodes, relationships, label distribution |
| `module_list` | `list_modules()` | All modules with node counts per module |
| `distribution` | `get_distribution()` | Node type distribution (how many Functions, Registers, etc.) |
| `coverage_report` | `get_coverage()` | Requirement-to-test coverage percentages per module |
| `metrics` | `get_metrics()` | Combined metrics snapshot |

### Example: Graph Stats

```cypher
MATCH (n)
RETURN labels(n)[0] AS label, count(*) AS count
ORDER BY count DESC
```

### Example: Coverage Report

```cypher
MATCH (ns:NodeSet {module: $module})-[:HAS_MODULE]->(req)
WHERE req:StakeholderRequirement OR req:ProductRequirement
OPTIONAL MATCH (req)<-[:VERIFIED_BY]-(test)
RETURN req.requirement_id, count(test) > 0 AS has_test
```

---

## 5. Prometheus Metrics

**File**: `src/Observability/metrics.py`
**Endpoint**: `/metrics` (mounted alongside FastMCP ASGI app via Starlette)
**Dependency**: `prometheus_client>=0.21`

The MCP server exposes Prometheus metrics at `/metrics`. All 55 tools are automatically instrumented via the `_ok()` / `_err()` return helpers — no per-tool code changes needed.

### Metric Types

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `aice_tool_requests_total` | Counter | `tool`, `status` | Total tool invocations (ok/error/denied) |
| `aice_tool_request_duration_seconds` | Histogram | `tool` | Per-tool latency (buckets: 0.05s–30s) |
| `aice_search_requests_total` | Counter | `workspace` | Search invocations by workspace (illd/mcal) |
| `aice_search_duration_seconds` | Histogram | `stage` | Search latency by stage (graph/vector/rerank) |
| `aice_cache_requests_total` | Counter | `cache_type`, `result` | Cache hit/miss counts (semantic/session) |
| `aice_active_sessions` | Gauge | — | Currently active DA sessions |
| `aice_rlm_requests_total` | Counter | `task_type` | RLM orchestration invocations |
| `aice_rlm_subquery_count` | Histogram | — | Number of sub-queries per RLM run |
| `aice_ingestion_files_total` | Counter | `parser_type`, `status` | Files processed by ingestion pipeline |
| `aice_backend_up` | Gauge | `backend` | Backend health (1=up, 0=down) |
| `aice_review_routing_total` | Counter | `route` | Review gate routing decisions (AUTO/QUICK/FULL) |

The table above lists the core request/latency/cache/session metrics. `metrics.py` defines **25
metrics** in total. Additional metrics include `aice_cerbos_up` (PDP reachability),
`aice_rlm_planner_fallbacks_total`, `aice_query_total` / `aice_query_latency_seconds`,
`aice_cache_hit_rate` / `aice_cache_size`, `aice_error_total`, `aice_ingestion_duration_seconds`,
and the DA-productivity family (`aice_da_session_duration_seconds`, `aice_da_session_outcomes_total`,
`aice_da_context_tokens`, `aice_da_first_result_latency_seconds`, `aice_da_pattern_hits_total`,
`aice_da_llm_tokens_total`).

### Instrumentation Pattern

All tools flow through `_ok()` and `_err()` helpers, which call `_finish_tool()`:

```python
def _finish_tool(status: str):
    name = _tool_name_ctx.get("")
    t0 = _tool_start_time.get(0.0)
    if name:
        TOOL_REQUESTS_TOTAL.labels(tool=name, status=status).inc()
        TOOL_REQUEST_DURATION.labels(tool=name).observe(time.time() - t0)
```

Specific tools add domain metrics:
- `search_database`: Cache hit/miss, search request counts
- `rlm_orchestrate`: RLM request count, sub-query histogram
- `session_start`/`session_end`: Active sessions gauge
- `evaluate_confidence`: Review routing counter
- `health_check`: Backend health gauges

### Graceful Degradation

If `prometheus_client` is not installed, all metric objects are replaced with `_NoOp` stubs that silently ignore `.inc()`, `.observe()`, `.set()`, and `.labels()`. The `/metrics` endpoint is not mounted. The server operates identically but without metrics.

---

## 6. Grafana Dashboards

**Dashboard file**: `monitoring/grafana/dashboards/aice-overview.json`
**Provisioning**: Auto-configured via `monitoring/grafana/provisioning/`

Grafana is pre-provisioned with a Prometheus datasource and a 10-panel overview dashboard, plus six specialized dashboards (cache performance, DA productivity, error rate, ingestion, query latency, silent failures). Provisioning is driven by `monitoring/grafana/provisioning/`; the monitoring stack is deployed via the Kubernetes manifests.

### AICE Overview Dashboard Panels

| Panel | Type | Query |
|-------|------|-------|
| Tool Request Rate | Time series | `rate(aice_tool_requests_total[5m])` by status |
| Tool Error Rate | Time series | Error requests / total requests ratio |
| Tool Latency (p50/p95/p99) | Time series | `histogram_quantile` on `aice_tool_request_duration_seconds` |
| Cache Hit Rate | Gauge | Hit count / (hit + miss) percentage |
| Active Sessions | Stat | `aice_active_sessions` current value |
| Search Requests by Workspace | Time series | `rate(aice_search_requests_total[5m])` by workspace |
| RLM Orchestration | Time series | `rate(aice_rlm_requests_total[5m])` by task_type |
| Review Gate Routing | Pie chart | `aice_review_routing_total` by route |
| Backend Health | Stat | `aice_backend_up` with UP/DOWN color mapping |
| Ingestion Files Processed | Time series | `rate(aice_ingestion_files_total[5m])` by status |

### Prometheus Configuration

- Scrape interval: 15s
- Retention: 15d
- Scrape targets: `mcp-server:8000/metrics` (primary), `localhost:9090` (self-monitoring)

---

## 7. Health Checks

### APScheduler Periodic Health Checks (Sprint 9)

The Kubernetes entrypoint (`mcp/app.py`) runs an APScheduler `BackgroundScheduler` with three periodic jobs:

| Job | Interval | Description |
|-----|----------|-------------|
| `health_check` | 5 minutes | Pings Neo4j, Qdrant, Redis; logs up/down status |
| `cache_stats` | 30 minutes | Logs LRU and semantic cache hit rates |
| `session_reaper` | 2 minutes | Purges expired sessions and updates the active-sessions gauge |

The scheduler shuts down gracefully on SIGTERM/SIGINT. If `apscheduler` is not installed, periodic jobs are silently disabled.

### Service Health Checks

Each backing service has a health probe defined in the Kubernetes manifests under `mcp/k8s/`:

| Service | Health Check | Interval |
|---------|-------------|----------|
| Neo4j | `cypher-shell "RETURN 1"` | 30s |
| Qdrant | HTTP GET `/` | 30s |
| Redis | `redis-cli ping` | 30s |
| PostgreSQL | `pg_isready` | 30s |
| MCP Server | HTTP GET (validates Cerbos + MCP endpoints) | 30s |
| Prometheus | `wget --spider http://localhost:9090/-/healthy` | 30s |
| Grafana | `curl -f http://localhost:3000/api/health` | 30s |

### MCP Health Tool

The `health_check` tool (public tier) pings all backends and returns their status:

```json
{
  "status": "healthy",
  "backends": {
    "neo4j": {"status": "up", "latency_ms": 12},
    "qdrant": {"status": "up", "latency_ms": 8},
    "redis": {"status": "up", "latency_ms": 2},
    "postgres": {"status": "up", "latency_ms": 5},
    "cerbos": {"status": "up", "latency_ms": 3}
  },
  "tool_count": 55,
  "uptime_seconds": 86400
}
```

Verbose mode (`verbose=true`) includes additional details: version numbers, connection pool sizes, memory usage.

---

## 8. Graceful Degradation

A core design principle: **no single backend failure should crash the server**.

| Backend Down | Impact | Degradation |
|-------------|--------|-------------|
| **PostgreSQL** | Audit, feedback, jobs | Operations continue; audit logs lost silently |
| **Redis** | Sessions | Falls back to in-memory sessions (lost on restart) |
| **Qdrant** | Vector search | Only graph search available (effective alpha → 1.0) |
| **Neo4j** | Graph search | Only vector search available (effective alpha → 0.0) |
| **Cerbos** | RBAC | Falls back to local tier-check from `tool_tiers.py` |

`PostgresClient` wraps every operation in try/except:

```python
def insert(self, table, data):
    try:
        # actual insert
    except Exception as e:
        logger.warning(f"PostgreSQL write failed: {e}")
        # continue without persistence
```

---

## 9. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `src/Observability/metrics.py` | 299 | Prometheus metrics: 25 metrics, `_NoOp` fallback |
| `src/Observability/postgres_schema.py` | 479 | `PostgresClient` — 7-table schema + `da_productivity` view, auto-creation, CRUD |
| `src/Observability/otel_tracing.py` | — | OpenTelemetry tracing setup (`trace_tool` decorator) |
| `src/Observability/log_sanitizer.py` | — | Secret-scrubbing log filter |
| `src/Configuration/services.py` | 414 | `ObservabilityService` — graph stats, health, metrics |
| `monitoring/prometheus.yml` | ~20 | Prometheus scrape config (mcp-server + self-monitoring) |
| `monitoring/grafana/dashboards/*.json` | — | 7 dashboards (overview + cache, DA productivity, error rate, ingestion, query latency, silent failures) |
| `monitoring/grafana/provisioning/` | — | Grafana datasource + dashboard provisioning |

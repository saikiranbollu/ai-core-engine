# Deployment Architecture

**Files**: `docker-compose.yml`, `Dockerfile`, `mcp/app.py`, `mcp/k8s/deployment.yaml`

---

## Table of Contents

- [Deployment Architecture](#deployment-architecture)
  - [Table of Contents](#table-of-contents)
  - [1. Overview](#1-overview)
  - [2. Docker Compose Stack](#2-docker-compose-stack)
    - [Services (Core)](#services-core)
    - [Services (Observability)](#services-observability)
    - [Health Checks](#health-checks)
    - [Startup Order](#startup-order)
    - [Neo4j Configuration](#neo4j-configuration)
    - [Redis Configuration](#redis-configuration)
  - [3. Dockerfile](#3-dockerfile)
    - [Stage 1: Cerbos Binary](#stage-1-cerbos-binary)
    - [Stage 2: Application](#stage-2-application)
    - [System Dependencies](#system-dependencies)
  - [4. Application Entrypoint](#4-application-entrypoint)
    - [Startup Flow](#startup-flow)
    - [Signal Handling](#signal-handling)
  - [5. Kubernetes Deployment](#5-kubernetes-deployment)
  - [6. Environment Variables](#6-environment-variables)
  - [7. Transport Modes](#7-transport-modes)
    - [stdio Mode](#stdio-mode)
    - [streamable-http Mode (Production)](#streamable-http-mode-production)
  - [8. Volume Mounts](#8-volume-mounts)
  - [9. Network Topology](#9-network-topology)

---

## 1. Overview

AICE is deployed as a **Docker Compose stack** for development and single-node production. The **5 core services** (Neo4j, Qdrant, Redis, PostgreSQL, MCP server) start by default. **Prometheus and Grafana** are available via the `monitoring` profile and are disabled by default. Kubernetes manifests are provided for cluster deployment. The MCP server container bundles a Cerbos PDP sidecar process for co-located RBAC evaluation.

```bash
# Core services only (default)
docker compose up -d

# With Prometheus + Grafana monitoring
ENABLE_METRICS=true docker compose --profile monitoring up -d
```

```
┌──────────────────────────────────────────────────────────────┐
│                       Docker Host                            │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────┐  ┌────────────┐       │
│  │  Neo4j   │  │  Qdrant  │  │Redis │  │ PostgreSQL │       │
│  │  5.26    │  │  v1.12.1 │  │  7   │  │    16      │       │
│  │  :7474   │  │  :6333   │  │:6379 │  │   :5432    │       │
│  │  :7687   │  │  :6334   │  │      │  │            │       │
│  └────┬─────┘  └────┬─────┘  └──┬───┘  └─────┬──────┘       │
│       │              │           │             │              │
│       └──────────────┼───────────┼─────────────┘              │
│                      │           │                            │
│              ┌───────┴───────────┴───────┐                    │
│              │      MCP Server           │                    │
│              │  ┌─────────────────────┐  │                    │
│              │  │  Python 3.12        │  │                    │
│              │  │  FastMCP + Uvicorn  │  │                    │
│              │  │  :8000 (MCP+metrics)│  │                    │
│              │  └─────────────────────┘  │                    │
│              │  ┌─────────────────────┐  │                    │
│              │  │  Cerbos PDP         │  │                    │
│              │  │  :3592 (HTTP)       │  │                    │
│              │  │  :3593 (gRPC)       │  │                    │
│              │  └─────────────────────┘  │                    │
│              └───────────────────────────┘                    │
│                        │                                     │
│              ┌─────────┴──────────┐  ┌──────────────────┐    │
│              │   Prometheus       │  │    Grafana        │    │
│              │   v2.53.0          │──│    v11.1.0        │    │
│              │   :9090            │  │    :3000          │    │
│              └────────────────────┘  └──────────────────┘    │
│                                                              │
│              Network: aice-net (bridge)                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Docker Compose Stack

### Services (Core)

| Service | Image | Ports | Key Configuration |
|---------|-------|-------|-------------------|
| **neo4j** | `neo4j:5.26.0-community` | 7474 (browser), 7687 (bolt) | APOC + GDS plugins, 512m heap, 512m pagecache |
| **qdrant** | `qdrant/qdrant:v1.12.1` | 6333 (REST), 6334 (gRPC) | Default configuration |
| **redis** | `redis:7-alpine` | 6379 | 256mb maxmemory, allkeys-lru eviction, AOF persistence |
| **postgres** | `postgres:16-alpine` | 5432 | DB=`aice_meta`, user=`aice` |
| **mcp-server** | Built from Dockerfile | 8000 (MCP), 3592/3593 (Cerbos) | depends_on all 4 backends (service_healthy) |

### Services (Observability — `monitoring` profile)

These services are **opt-in** via `docker compose --profile monitoring up -d`. Set `ENABLE_METRICS=true` to activate the `/metrics` endpoint in the MCP server.

| Service | Image | Ports | Key Configuration |
|---------|-------|-------|-------------------|
| **prometheus** | `prom/prometheus:v2.53.0` | 9090 | 15s scrape interval, 15d retention, scrapes `mcp-server:8000/metrics` |
| **grafana** | `grafana/grafana:11.1.0` | 3000 | Auto-provisioned Prometheus datasource, 10-panel overview dashboard |

Prometheus scrapes the MCP server's `/metrics` endpoint (exposed via `prometheus_client` ASGI app mounted alongside FastMCP). Grafana is pre-configured with a provisioned datasource and dashboard — no manual setup required.

See [ADR-021](DECISIONS.md#adr-021-prometheus--grafana-observability) for design rationale.

### Health Checks

All services have Docker health checks:

```yaml
neo4j:
  healthcheck:
    test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "$$NEO4J_AUTH", "RETURN 1"]
    interval: 30s
    timeout: 10s
    retries: 5

qdrant:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:6333/"]
    interval: 30s
    timeout: 10s
    retries: 5

redis:
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 30s
    timeout: 5s
    retries: 5

postgres:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U aice"]
    interval: 30s
    timeout: 5s
    retries: 5

mcp-server:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 30s
    timeout: 10s
    retries: 5
```

### Startup Order

The MCP server uses `depends_on` with `condition: service_healthy` for all 4 backends. This ensures the server only starts after all backends pass health checks.

### Neo4j Configuration

```yaml
environment:
  - NEO4J_AUTH=neo4j/password
  - NEO4J_PLUGINS=["apoc", "graph-data-science"]
  - NEO4J_server_memory_heap_initial__size=512m
  - NEO4J_server_memory_heap_max__size=512m
  - NEO4J_server_memory_pagecache_size=512m
```

APOC plugin: provides import/export utilities, collection functions, and path expansion.
GDS plugin: provides graph algorithms (shortest path, community detection, centrality, etc.).

### Redis Configuration

```yaml
command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --appendonly yes
```

- `allkeys-lru`: evicts least-recently-used keys when memory limit is reached (suitable for cache workload)
- `appendonly yes`: AOF persistence for session durability across restarts

---

## 3. Dockerfile

Multi-stage build with Cerbos PDP bundling:

### Stage 1: Cerbos Binary

```dockerfile
FROM ghcr.io/cerbos/cerbos:latest AS cerbos
# Provides /cerbos binary
```

### Stage 2: Application

```dockerfile
FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential git curl libclang-dev

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Cerbos binary from Stage 1
COPY --from=cerbos /cerbos /usr/local/bin/cerbos

# Application code
# NOTE: mcp/ is copied as aice_mcp/ to avoid shadowing the 'mcp' pip package
COPY mcp/ /app/aice_mcp/
COPY src/ /app/src/

# Cerbos policies
COPY mcp/auth/ /policies/

# Python path setup
ENV PYTHONPATH="/app/src:/app/src/HybridRAG/code:/app/src/MemoryLayer:/app/aice_mcp"

# Expose ports
EXPOSE 8000 3592 3593

# Entrypoint
CMD ["python", "aice_mcp/app.py"]
```

**Key detail**: The `mcp/` source directory is renamed to `aice_mcp/` during the Docker build. This prevents the project's `mcp/` package from shadowing the `mcp` pip package (the MCP SDK), which would cause import errors.

### System Dependencies

- `build-essential` — for compiling Python C extensions
- `git` — for incremental ingestion (git hash tracking)
- `curl` — for health checks
- `libclang-dev` — for the C parser (libclang bindings)

---

## 4. Application Entrypoint

`mcp/app.py` (109 lines) handles the server startup sequence:

### Startup Flow

```
1. Start Cerbos PDP subprocess
   └── /usr/local/bin/cerbos server --config=/policies/.cerbos.yaml
   
2. Wait for Cerbos health check
   └── Poll http://localhost:3592/api/health (max 30 retries, 1s interval)
   
3. Register signal handlers
   └── SIGTERM, SIGINT → graceful shutdown (stop Cerbos, cleanup)
   
4. Start MCP server
   └── Import and call mcp_server.main()
```

### Signal Handling

For Kubernetes graceful shutdown:
- `SIGTERM` → stop accepting new requests, finish in-flight, kill Cerbos subprocess, exit
- `SIGINT` → same (for local dev with Ctrl+C)

---

## 5. Kubernetes Deployment

`mcp/k8s/deployment.yaml` provides a basic Kubernetes Deployment manifest:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aice-mcp-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aice-mcp-server
  template:
    spec:
      containers:
        - name: mcp-server
          image: aice-mcp-server:latest
          ports:
            - containerPort: 8000  # MCP
            - containerPort: 3592  # Cerbos HTTP
            - containerPort: 3593  # Cerbos gRPC
          env:
            - name: MCP_TRANSPORT
              value: "streamable-http"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
```

In Kubernetes, the Neo4j, Qdrant, Redis, and PostgreSQL services are assumed to be running as separate pods/services (not bundled in the same pod).

---

## 6. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | Transport mode: `stdio`, `sse`, `streamable-http` |
| `MCP_API_KEY` | — | API key for stdio transport (no HTTP headers) |
| `NEO4J_URI` | `bolt://neo4j:7687` | Neo4j connection URI |
| `NEO4J_PASSWORD` | — | Neo4j password |
| `QDRANT_HOST` | `qdrant` | Qdrant hostname |
| `QDRANT_PORT` | `6333` | Qdrant REST port |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `POSTGRES_DSN` | — | PostgreSQL connection string |
| `GPT4IFX_ENDPOINT` | — | Infineon LLM proxy URL |
| `GPT4IFX_API_KEY` | — | LLM proxy API key |
| `ST_CACHE_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model for semantic cache |
| `LRU_CACHE_SIZE` | `10000` | Maximum LRU cache entries |
| `LRU_CACHE_TTL_HOURS` | `24` | LRU entry time-to-live in hours |
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine similarity threshold for semantic cache hits (0.0–1.0) |
| `SEMANTIC_CACHE_TTL_DAYS` | `7` | Semantic cache entry time-to-live in days |
| `SEMANTIC_CACHE_MAX_SIZE` | `500` | Maximum semantic cache entries |

All cache env vars can be updated at runtime via the `cache_refresh_config` MCP tool (admin tier) — no restart required. Cached data is preserved; entries are only evicted if size limits shrink below current count.

| `CERBOS_HOST` | `localhost` | Cerbos PDP host (usually localhost in sidecar mode) |
| `CERBOS_HTTP_PORT` | `3592` | Cerbos HTTP port |
| `CERBOS_GRPC_PORT` | `3593` | Cerbos gRPC port |
| `ENABLE_METRICS` | `false` | Enable Prometheus metrics (`true`/`false`). Set to `true` when using `--profile monitoring` |

---

## 7. Transport Modes

| Mode | Config | Use Case |
|------|--------|----------|
| `stdio` | `MCP_TRANSPORT=stdio` | Local development, debugging, IDE integration |
| `sse` | `MCP_TRANSPORT=sse` | Legacy HTTP streaming |
| `streamable-http` | `MCP_TRANSPORT=streamable-http` | Production (Docker, K8s) |

### stdio Mode

MCP reads from stdin, writes to stdout. Used with:
- `python mcp_server.py` directly
- VS Code Copilot `mcp.json` with type `stdio`
- Testing tools that pipe JSON-RPC messages

### streamable-http Mode (Production)

Creates ASGI app → wraps with `_APIKeyMiddleware` → runs on Uvicorn:

```python
app = mcp.streamable_http_app()
app = _APIKeyMiddleware(app)
uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000)).run()
```

---

## 8. Volume Mounts

8 named Docker volumes for data persistence:

| Volume | Mount Point | Service | Purpose |
|--------|------------|---------|----------|
| `neo4j_data` | `/data` | neo4j | Graph database files |
| `neo4j_logs` | `/logs` | neo4j | Neo4j server logs |
| `qdrant_data` | `/qdrant/storage` | qdrant | Vector index and snapshots |
| `redis_data` | `/data` | redis | AOF persistence files |
| `postgres_data` | `/var/lib/postgresql/data` | postgres | Relational data |
| `model_cache` | `/root/.cache` | mcp-server | sentence-transformers model cache |
| `prometheus_data` | `/prometheus` | prometheus | Time-series metrics data (15d retention) |
| `grafana_data` | `/var/lib/grafana` | grafana | Dashboard definitions, user preferences |

The `model_cache` volume prevents re-downloading the `all-MiniLM-L6-v2` model on every container restart.

---

## 9. Network Topology

All services communicate over a single Docker bridge network (`aice-net`). Service discovery uses Docker DNS — services refer to each other by container name:

```
mcp-server → neo4j:7687     (Bolt protocol)
mcp-server → qdrant:6333    (HTTP REST) / qdrant:6334 (gRPC)
mcp-server → redis:6379     (Redis protocol)
mcp-server → postgres:5432  (PostgreSQL wire protocol)
mcp-server → localhost:3592 (Cerbos HTTP — sidecar in same container)
prometheus → mcp-server:8000 (scrapes /metrics endpoint)
grafana    → prometheus:9090  (queries time-series data)
```

**External access**: Only the MCP server port (8000) needs to be exposed externally. Prometheus (9090) and Grafana (3000) are exposed for operations dashboards but can be restricted to internal networks in production.

# Deployment Architecture

**Files**: `Dockerfile`, `mcp/app.py`, `mcp/k8s/test/` (Kustomize overlay), `mcp/k8s/mcp-hpa.yaml`, `mcp/k8s/pipeline-cronjob.yaml`, `.gitlab-ci.yml`

---

## Table of Contents

- [Deployment Architecture](#deployment-architecture)
  - [Table of Contents](#table-of-contents)
  - [1. Overview](#1-overview)
  - [2. Kubernetes / OpenShift Topology](#2-kubernetes--openshift-topology)
  - [3. Container Image (Dockerfile)](#3-container-image-dockerfile)
    - [Stage 1: Cerbos Binary](#stage-1-cerbos-binary)
    - [Stage 2: Application](#stage-2-application)
    - [System Dependencies](#system-dependencies)
  - [4. Application Entrypoint](#4-application-entrypoint)
    - [Startup Flow](#startup-flow)
    - [Signal Handling](#signal-handling)
  - [5. MCP Server Deployment](#5-mcp-server-deployment)
  - [6. Backing Services](#6-backing-services)
    - [Schema Init Jobs](#schema-init-jobs)
  - [7. Routes \& Network](#7-routes--network)
  - [8. Autoscaling (HPA)](#8-autoscaling-hpa)
  - [9. Nightly Ingestion CronJob](#9-nightly-ingestion-cronjob)
  - [10. CI/CD Pipeline](#10-cicd-pipeline)
  - [11. Environment Variables](#11-environment-variables)
  - [12. Transport Modes](#12-transport-modes)
    - [stdio Mode](#stdio-mode)
    - [streamable-http Mode (Production)](#streamable-http-mode-production)
  - [13. Storage \& Persistence](#13-storage--persistence)
  - [14. Monitoring Integration](#14-monitoring-integration)

---

## 1. Overview

AICE is deployed to **OpenShift** (Kubernetes) as a set of pods in the `ai-core-engine` namespace. The MCP server runs as a **single-container pod** that also launches a **Cerbos PDP subprocess** for co-located RBAC evaluation (see [§4](#4-application-entrypoint)). The four backends — **Neo4j, Qdrant, Redis, PostgreSQL** — each run as their own pod/service. There is **no Docker Compose stack**; the entire environment is described by Kubernetes/OpenShift manifests under `mcp/k8s/` and applied with Kustomize.

Two deployment paths exist:

- **Automated (GitLab CI)** — every merge request targeting `main` triggers `.gitlab-ci.yml`, which builds the image via an OpenShift `BuildConfig`, runs unit tests inside it, deploys the test environment, runs E2E tests against the live route, then tears the environment down. See [§10](#10-cicd-pipeline).
- **Manual (Kustomize)** — apply the full overlay directly:

```bash
# Deploy the complete test environment to the ai-core-engine namespace
kubectl apply -k mcp/k8s/test/

# Roll out a new image after a rebuild
oc set image deployment/test-aice-mcp-server \
  aice-mcp=image-registry.openshift-image-registry.svc:5000/mcswai/test-aice-mcp:latest \
  -n ai-core-engine
```

```
┌────────────────────────────────────────────────────────────────────┐
│  OpenShift namespace: ai-core-engine                                │
│                                                                     │
│   External access via OpenShift Routes (edge / passthrough TLS)     │
│        │                                                            │
│        ▼                                                            │
│  ┌──────────────────────────────┐    Service: test-mcp (ClusterIP) │
│  │  Pod: test-aice-mcp-server   │    :8000 ─► Route test-mcp …/mcp  │
│  │  ┌────────────────────────┐  │                                  │
│  │  │ container: aice-mcp    │  │    probes: /_cerbos/health :3592 │
│  │  │  python aice_mcp/app.py│  │                                  │
│  │  │   ├─ Cerbos PDP subproc│  │    resources: 0.5–2 CPU,         │
│  │  │   │    :3592 / :3593   │  │               1.5–4 Gi RAM       │
│  │  │   ├─ APScheduler (3)   │  │                                  │
│  │  │   └─ MCP server :8000  │  │                                  │
│  │  └────────────────────────┘  │                                  │
│  └──────────────┬───────────────┘                                  │
│                 │ in-cluster DNS                                    │
│    ┌────────────┼───────────────┬───────────────┐                  │
│    ▼            ▼               ▼               ▼                   │
│ ┌────────┐ ┌──────────┐  ┌──────────┐   ┌────────────┐            │
│ │test-   │ │test-     │  │test-     │   │test-       │            │
│ │neo4j   │ │qdrant    │  │redis     │   │postgres    │            │
│ │4.4.48  │ │v1.12.1   │  │7-alpine  │   │16-alpine   │            │
│ │:7687   │ │:6333/6334│  │:6379     │   │:5432       │            │
│ │(SS+PVC)│ │          │  │          │   │            │            │
│ └────────┘ └──────────┘  └──────────┘   └────────────┘            │
│                                                                     │
│  Init Jobs: test-init-postgres, test-init-neo4j                     │
│  CronJob:   nightly-ingestion (00:00 Europe/Berlin)                 │
│  HPA:       1 → 5 pods @ 70% CPU (ClientIP sticky sessions)         │
└────────────────────────────────────────────────────────────────────┘

Build / CI namespace: mcswai   (BuildConfig test-aice-mcp, in-cluster GitLab Runner)
Monitoring: Prometheus scrapes :8000/metrics (ENABLE_METRICS=true) → Grafana
```

---

## 2. Kubernetes / OpenShift Topology

The complete environment is defined by a **Kustomize overlay** at `mcp/k8s/test/`. Applying it (`kubectl apply -k mcp/k8s/test/`) creates all resources in the `ai-core-engine` namespace in dependency order:

| Phase | Resource(s) | Manifest |
|-------|-------------|----------|
| 1 — Secrets & Network | `test-aice-db-secrets`, `test-aice-external-secrets`, NetworkPolicy | `secrets.yaml`, `network-policy.yaml` |
| 2 — Databases | Neo4j (StatefulSet), Qdrant, Redis, PostgreSQL | `neo4j.yaml`, `qdrant.yaml`, `redis.yaml`, `postgres.yaml` |
| 3 — Configuration | `test-aice-storage-config`, `test-aice-api-keys` ConfigMaps | `configmap.yaml` |
| 4 — Schema Init Jobs | `test-init-postgres`, `test-init-neo4j` | `init-postgres.yaml`, `init-neo4j.yaml` |
| 5 — MCP Server | Deployment `test-aice-mcp-server` + Service `test-mcp` + Routes | `mcp-deployment.yaml`, `route.yaml` |

Additional manifests applied outside the base overlay:

| Manifest | Namespace | Purpose |
|----------|-----------|---------|
| `mcp/k8s/test/buildconfig.yaml` | `mcswai` | OpenShift `ImageStream` + `BuildConfig` that builds the image from the Dockerfile |
| `mcp/k8s/test/gitlab-runner.yaml` | `mcswai` | In-cluster GitLab Runner (Kubernetes executor) for CI |
| `mcp/k8s/mcp-hpa.yaml` | `ai-core-engine` | HorizontalPodAutoscaler + sticky-session config ([§8](#8-autoscaling-hpa)) |
| `mcp/k8s/pipeline-cronjob.yaml` | `ai-core-engine` | Nightly ingestion CronJob + PVC + script ConfigMap ([§9](#9-nightly-ingestion-cronjob)) |

All test resources carry the label `app.kubernetes.io/part-of=aice-test-env`, which the CI cleanup stage uses for teardown:

```bash
kubectl delete all,secret,configmap,pvc,networkpolicy,route,job \
  -l app.kubernetes.io/part-of=aice-test-env -n ai-core-engine
```

> **OpenShift specifics** — `route.yaml` (Route) and `buildconfig.yaml` (BuildConfig / ImageStream) are OpenShift CRDs. The image registry is the in-cluster `image-registry.openshift-image-registry.svc:5000`. Backing-service images, probes, and resource limits are covered in [§6](#6-backing-services).

---

## 3. Container Image (Dockerfile)

A single multi-stage `Dockerfile` at the repo root produces the MCP server image. It bundles the Cerbos PDP binary and pre-bakes the embedding + prompt-compression models so the first request doesn't block on a network download.

### Stage 1: Cerbos Binary

```dockerfile
FROM ghcr.io/cerbos/cerbos:latest AS cerbos
# Provides /cerbos binary, copied into the app image in stage 2
```

### Stage 2: Application

```dockerfile
FROM python:3.12-slim
WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl libclang-dev && rm -rf /var/lib/apt/lists/*

# Cerbos binary from stage 1
COPY --from=cerbos /cerbos /usr/local/bin/cerbos

# CPU-only PyTorch first (the CPU wheel has zero nvidia-* deps, so
# sentence-transformers won't pull ~2 GB of CUDA packages), then requirements.
COPY requirements.txt .
RUN pip install --no-cache-dir --no-deps torch==2.11.0 && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn uvicorn[standard]

# Pre-download embedding + LLMLingua models into the image cache (offline at runtime)
ENV HF_HOME=/app/.cache \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# Application code — mcp/ is copied as aice_mcp/ to avoid shadowing the 'mcp' pip package
COPY mcp/ ./aice_mcp/
COPY src/ ./src/

# Cerbos policies (matches storage.disk.directory in .cerbos.yaml)
COPY mcp/auth/policies/ /policies/

ENV PYTHONPATH="/app/src:/app/src/HybridRAG/code:/app/src/MemoryLayer:/app/aice_mcp" \
    MCP_TRANSPORT=streamable-http \
    FASTMCP_HOST=0.0.0.0 FASTMCP_PORT=8000 FASTMCP_STREAMABLE_HTTP_PATH=/mcp \
    CERBOS_HOST=localhost CERBOS_HTTP_PORT=3592 CERBOS_GRPC_PORT=3593 \
    WEB_CONCURRENCY=4

EXPOSE 8000 3592 3593
CMD ["python", "aice_mcp/app.py"]
```

**Key detail**: The `mcp/` source directory is renamed to `aice_mcp/` during the build. This prevents the project's `mcp/` package from shadowing the `mcp` pip package (the MCP SDK), which would cause import errors. The image is built inside the cluster by an OpenShift `BuildConfig` (`oc start-build test-aice-mcp`), not by a local `docker build`.

### System Dependencies

- `build-essential` — for compiling Python C extensions
- `git` — for incremental ingestion (git hash tracking) and pip VCS installs
- `curl` — for health checks
- `libclang-dev` — libclang bindings used by the tree-sitter / docling parsing stack

---

## 4. Application Entrypoint

`mcp/app.py` — the **Kubernetes entrypoint** — starts both the Cerbos PDP and the MCP server in the same pod and schedules background jobs:

### Startup Flow

```
1. Start Cerbos PDP subprocess
   └── $CERBOS_BIN server --config=$CERBOS_CONFIG
       (default /usr/local/bin/cerbos, /app/aice_mcp/auth/.cerbos.yaml)

2. Wait for Cerbos health check
   └── Poll http://localhost:3592/_cerbos/health (30 s deadline, 0.5 s interval)
       → abort the pod if Cerbos never becomes healthy

3. Register signal handlers
   └── SIGTERM, SIGINT → graceful shutdown; SIGCHLD → detect Cerbos crash

4. Start APScheduler (3 periodic jobs, if apscheduler is installed)
   ├── health_check    — every 5 min  (probe Neo4j / Qdrant / Redis)
   ├── cache_stats     — every 30 min (log cache hit rates)
   └── session_reaper  — every 2 min  (purge expired sessions, update gauge)

5. Start MCP server
   └── core.mcp_server.main()   (enforces auth-readiness, fails fast on misconfig)
```

### Signal Handling

For Kubernetes graceful shutdown:
- `SIGTERM` → stop the scheduler, terminate the Cerbos subprocess, exit 0
- `SIGINT` → same (local dev with Ctrl+C)
- `SIGCHLD` → if the Cerbos child exits unexpectedly, the pod exits 1 so Kubernetes restarts it

---

## 5. MCP Server Deployment

`mcp/k8s/test/mcp-deployment.yaml` defines the `test-aice-mcp-server` Deployment plus the `test-mcp` ClusterIP Service. The single container runs the Dockerfile `CMD` (`python aice_mcp/app.py`), which spawns Cerbos as a subprocess — so all three ports (8000 MCP, 3592/3593 Cerbos) belong to one container.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-aice-mcp-server
  namespace: ai-core-engine
spec:
  replicas: 1                       # scaled by the HPA (§8)
  template:
    spec:
      containers:
        - name: aice-mcp
          image: image-registry.openshift-image-registry.svc:5000/mcswai/test-aice-mcp:latest
          imagePullPolicy: Always
          ports:
            - { name: mcp-http,    containerPort: 8000 }
            - { name: cerbos-http, containerPort: 3592 }
            - { name: cerbos-grpc, containerPort: 3593 }
          env:
            - { name: ENABLE_METRICS,     value: "true" }
            - { name: HF_HUB_OFFLINE,     value: "1" }     # model baked into image
            - { name: TRANSFORMERS_OFFLINE, value: "1" }
            - { name: REDIS_URL,  value: "redis://test-redis:6379/0" }
            - { name: POSTGRES_DSN, value: "postgresql://aice:…@test-postgres:5432/aice_meta" }
            # NEO4J_URI / NEO4J_PASSWORD and IFX/JAMA creds come from Secrets
          volumeMounts:
            - { name: storage-config, mountPath: /app/src/HybridRAG/config/storage_config.yaml, subPath: storage_config.yaml }
            - { name: api-keys,       mountPath: /app/aice_mcp/auth/api_keys.yaml, subPath: api_keys.yaml }
          livenessProbe:
            httpGet: { path: /_cerbos/health, port: cerbos-http }
            initialDelaySeconds: 15
            periodSeconds: 15
          readinessProbe:
            httpGet: { path: /_cerbos/health, port: cerbos-http }
            initialDelaySeconds: 10
            periodSeconds: 10
          resources:
            requests: { cpu: "500m", memory: "1536Mi" }
            limits:   { cpu: "2",    memory: "4Gi" }
      volumes:
        - { name: storage-config, configMap: { name: test-aice-storage-config } }
        - { name: api-keys,       configMap: { name: test-aice-api-keys } }
```

Notes:
- **Probes hit `/_cerbos/health` on port 3592**, not an MCP `/health` endpoint. Because `app.py` only starts the MCP server *after* Cerbos is healthy, a healthy Cerbos is a reliable readiness signal for the whole pod.
- **ConfigMap overrides** — `storage_config.yaml` and `api_keys.yaml` are mounted over the in-image copies, so storage endpoints and API keys are environment-specific without rebuilding the image.
- **Offline models** — `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` force the embedder to load from the baked-in cache.

---

## 6. Backing Services

Each backend runs as its own workload in the `ai-core-engine` namespace. All are reachable via in-cluster DNS under their `test-` service names.

| Service | Image | Kind | Ports | Notes |
|---------|-------|------|-------|-------|
| **test-neo4j** | `neo4j:4.4.48` | StatefulSet (headless, 2 Gi PVC) | 7474 (http), 7473 (https), 7687 (bolt) | APOC plugin; 256 m heap/pagecache; init container generates self-signed TLS certs for bolt + HTTPS (mirrors the production Helm instance) |
| **test-qdrant** | `qdrant/qdrant:v1.12.1` | Deployment | 6333 (REST), 6334 (gRPC) | Pulled from the internal `dockerregistry-v2.vih.infineon.com` mirror |
| **test-redis** | `redis:7-alpine` | Deployment | 6379 | Session/cache store |
| **test-postgres** | `postgres:16-alpine` | Deployment | 5432 | DB `aice_meta`, user `aice` |

> The test Neo4j is **4.4.48** with the **APOC** plugin only. GDS graph algorithms (community detection, shortest path, centrality) used by the higher-level services rely on the production Neo4j instance where GDS is available.

### Schema Init Jobs

Two Kubernetes Jobs run once during a Kustomize apply, before/alongside the MCP server:

- **`test-init-postgres`** (`init-postgres.yaml`) — creates the PostgreSQL metadata schema (audit logs, response archive, review evidence, feedback, failure patterns, ingestion jobs, session metadata). CI re-applies this job on every deploy.
- **`test-init-neo4j`** (`init-neo4j.yaml`) — creates graph indexes/constraints. The `ONTOLOGY_PROFILE` env var selects the profile: `mcal`, `illd`, or `both` (default). Shared indexes (`NodeSet`, `ApprovedPattern`) are always created.

---

## 7. Routes & Network

External access is provided by **OpenShift Routes** (`route.yaml`) with edge- or passthrough-TLS termination:

| Route | Host | TLS | Target |
|-------|------|-----|--------|
| `test-mcp` | `test-mcp-ai-core-engine.eu-de-7.icp.infineon.com/mcp` | edge (redirect) | `test-mcp:8000` |
| `test-neo4j-ui-route` | `neo4j-ui-ai-core-engine-test.icp.infineon.com` | edge | `test-neo4j:7474` |
| `test-neo4j-bolt-edge-route` | `bolt-edge-neo4j-…` | edge | `test-neo4j:7687` |
| `test-neo4j-bolt-passthrough-route` | `bolt-passthrough-neo4j-…` | passthrough | `test-neo4j:7687` |
| `test-qdrant-route` | `qdrant-ai-core-engine-test.icp.infineon.com` | edge | `test-qdrant:6333` |

Redis and PostgreSQL have **no external route** — they are cluster-only. `network-policy.yaml` restricts pod-to-pod traffic within the namespace. In-cluster service discovery uses Kubernetes DNS:

```
test-aice-mcp-server → test-neo4j:7687     (Bolt)
test-aice-mcp-server → test-qdrant:6333    (REST) / :6334 (gRPC)
test-aice-mcp-server → test-redis:6379     (Redis protocol)
test-aice-mcp-server → test-postgres:5432  (PostgreSQL wire protocol)
test-aice-mcp-server → localhost:3592      (Cerbos HTTP — subprocess in same container)
```

---

## 8. Autoscaling (HPA)

`mcp/k8s/mcp-hpa.yaml` provides a `HorizontalPodAutoscaler` plus session-affinity configuration:

- **Scale**: 1 → 5 replicas on 70 % average CPU utilization.
- **Scale-up**: fast — up to 2 pods per 60 s (60 s stabilization window).
- **Scale-down**: slow — 1 pod per 120 s (300 s stabilization window) so active in-memory sessions aren't killed.
- **Sticky sessions**: Service `sessionAffinity: ClientIP` + a cookie-based sticky-session route annotation (HAProxy), so each user keeps hitting the pod that holds their in-memory session.

---

## 9. Nightly Ingestion CronJob

`mcp/k8s/pipeline-cronjob.yaml` defines the `nightly-ingestion` CronJob that refreshes the knowledge graph from upstream sources:

| Property | Value |
|----------|-------|
| Schedule | `0 0 * * *` (midnight, `Europe/Berlin`) |
| Concurrency | `Forbid` (never overlap runs) |
| Timeout | `activeDeadlineSeconds: 43200` (12 h hard limit) |
| Retries | `backoffLimit: 0` (pipeline handles partial failures itself) |
| Entrypoint | `/scripts/run_nightly.sh` (from the `pipeline-runner-script` ConfigMap) |
| Storage | `pipeline-workspace` PVC (`pipeline-pvc.yaml`) for temp/logs |
| Credentials | Neo4j / Qdrant in-cluster; Jama + Bitbucket (IFX) creds from Secrets |

The job connects to Neo4j and Qdrant directly in-cluster (`bolt://test-neo4j:7687`, `http://test-qdrant:6333`) and uses the OpenShift-injected CA bundle for outbound TLS to Infineon-internal services.

---

## 10. CI/CD Pipeline

`.gitlab-ci.yml` runs on merge-request events targeting `main`, on the **in-cluster GitLab Runner** (Kubernetes executor, `mcswai` namespace). Pods run as the `gitlab-ci` service account, which has `edit` in both `mcswai` and `ai-core-engine`, so `oc` uses the in-cluster token — no manual login.

| Stage | What it does |
|-------|--------------|
| **build** | `oc start-build test-aice-mcp` (OpenShift BuildConfig, Docker strategy) → tags the image with the branch slug |
| **unit-test** | Runs `pytest tests/unit` *inside* the freshly built image (validates the exact artifact) |
| **deploy** | `oc apply -k mcp/k8s/test/`, re-runs `init-postgres`, `oc set image` to the branch image, waits for the route to return HTTP < 500 |
| **e2e-test** | Runs `pytest tests/e2e` against the live route (`MCP_TEST_URL`) using an admin API key |
| **cleanup** | Always runs — deletes all `aice-test-env`-labeled resources and the branch image tag |

Kubernetes-executor resource hints (`KUBERNETES_MEMORY_LIMIT: 5Gi`, etc.) are set on the test stages because loading sentence-transformers + PyTorch needs ~2 GB RAM.

---

## 11. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `streamable-http` (image) / `stdio` (bare) | Transport mode: `stdio`, `sse`, `streamable-http` |
| `FASTMCP_HOST` | `0.0.0.0` | Bind address for HTTP transport |
| `FASTMCP_PORT` | `8000` | MCP HTTP port |
| `FASTMCP_STREAMABLE_HTTP_PATH` | `/mcp` | Path the MCP endpoint is served on |
| `NEO4J_URI` | `bolt://test-neo4j:7687` | Neo4j connection URI (from Secret in-cluster) |
| `NEO4J_PASSWORD` | — | Neo4j password (from Secret) |
| `QDRANT_HOST` | `test-qdrant` | Qdrant hostname |
| `QDRANT_PORT` | `6333` | Qdrant REST port |
| `REDIS_URL` | `redis://test-redis:6379/0` | Redis connection URL |
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

| Variable | Default | Description |
|----------|---------|-------------|
| `CERBOS_BIN` | `/usr/local/bin/cerbos` | Path to the Cerbos binary |
| `CERBOS_CONFIG` | `/app/aice_mcp/auth/.cerbos.yaml` | Cerbos config file |
| `CERBOS_HOST` | `localhost` | Cerbos PDP host (subprocess in the same container) |
| `CERBOS_HTTP_PORT` | `3592` | Cerbos HTTP port |
| `CERBOS_GRPC_PORT` | `3593` | Cerbos gRPC port |
| `ENABLE_METRICS` | `false` (`true` in the deployment) | Expose the Prometheus `/metrics` endpoint |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` (deployment) | Load models from the baked-in cache, no network |
| `WEB_CONCURRENCY` | `4` | Worker count for multi-worker HTTP serving |

---

## 12. Transport Modes

| Mode | Config | Use Case |
|------|--------|----------|
| `stdio` | `MCP_TRANSPORT=stdio` | Local development, debugging, IDE integration |
| `sse` | `MCP_TRANSPORT=sse` | Legacy HTTP streaming |
| `streamable-http` | `MCP_TRANSPORT=streamable-http` | Production (image default, K8s) |

### stdio Mode

MCP reads from stdin, writes to stdout. Used with:
- `python mcp_server.py` directly
- VS Code Copilot `mcp.json` with type `stdio`
- Testing tools that pipe JSON-RPC messages

### streamable-http Mode (Production)

Creates ASGI app → wraps with `_APIKeyMiddleware` → runs on Uvicorn, served at `FASTMCP_STREAMABLE_HTTP_PATH` (`/mcp`):

```python
app = mcp.streamable_http_app()
app = _APIKeyMiddleware(app)
uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000)).run()
```

---

## 13. Storage & Persistence

Persistence is provided by Kubernetes `PersistentVolumeClaim`s rather than Docker volumes:

| PVC | Workload | Purpose |
|-----|----------|---------|
| `test-neo4j-data` (2 Gi, from StatefulSet `volumeClaimTemplates`) | test-neo4j | Graph database files |
| `pipeline-workspace` (`pipeline-pvc.yaml`) | nightly-ingestion CronJob | Temp working dirs + logs for ingestion |

Qdrant, Redis, and PostgreSQL in the test overlay use pod-local storage (data is disposable and re-seeded by the init jobs / nightly pipeline). The embedding model is **baked into the image** at build time, so no model-cache volume is needed at runtime.

---

## 14. Monitoring Integration

There is no Prometheus/Grafana pod in the application overlay. Instead:

- The MCP server exposes Prometheus metrics on `:8000/metrics` when `ENABLE_METRICS=true` (set in the deployment). See [observability.md](observability.md).
- Scrape config and dashboards live under `monitoring/` in the repo: `monitoring/prometheus.yml` plus 7 Grafana dashboard JSON files under `monitoring/grafana/`, and an alert rule at `monitoring/prometheus/aice_silent_failures.yml`.
- These are consumed by the platform's Prometheus/Grafana stack; the AICE repo ships the config, not the monitoring workloads.

See [ADR-021](DECISIONS.md#adr-021-prometheus--grafana-observability) for design rationale.

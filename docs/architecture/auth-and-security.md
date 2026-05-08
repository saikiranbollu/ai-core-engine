# Authentication & Security Architecture

**Component**: `mcp/core/auth_middleware.py`, `mcp/auth/`
**Primary classes**: `_APIKeyMiddleware`, auth functions in `auth_middleware.py`
**Backing services**: Cerbos PDP (subprocess)

---

## Table of Contents

1. [Overview](#1-overview)
2. [3-Tier RBAC Model](#2-3-tier-rbac-model)
3. [API Key Authentication](#3-api-key-authentication)
4. [Cerbos PDP Integration](#4-cerbos-pdp-integration)
5. [ASGI Middleware](#5-asgi-middleware)
6. [Per-Request Authorization Flow](#6-per-request-authorization-flow)
7. [Graceful Fallback](#7-graceful-fallback)
8. [Security Boundaries](#8-security-boundaries)
9. [File Map](#9-file-map)

---

## 1. Overview

AICE implements **per-request authorization** using a 3-tier RBAC model enforced by a Cerbos Policy Decision Point (PDP). Every MCP tool call is authenticated (API key) and authorized (Cerbos policy check) before execution.

```
DA Request (HTTP)
    │
    │  Authorization: Bearer <api-key>
    ▼
┌─ ASGI Middleware ──────────────────────────┐
│  Extract API key from HTTP header          │
│  Store in contextvars (per-request)        │
└─────────────┬──────────────────────────────┘
              │
              ▼
┌─ MCP Tool Handler ─────────────────────────┐
│  _authorize("tool_name")                   │
│      │                                     │
│      ▼                                     │
│  ┌─ auth_middleware ────────────────────┐   │
│  │  1. Resolve API key → principal     │   │
│  │  2. Look up principal's roles       │   │
│  │  3. Check Cerbos PDP               │   │
│  │     policy: resource_mcp_tool       │   │
│  │     action: invoke                  │   │
│  │     role: public/developer/admin    │   │
│  │  4. ALLOW → proceed                │   │
│  │     DENY → raise AuthorizationError │   │
│  └─────────────────────────────────────┘   │
│                                             │
│  [Execute tool logic]                      │
└─────────────────────────────────────────────┘
```

---

## 2. 3-Tier RBAC Model

### Tier Definitions

| Tier | Tools | Typical User | Purpose |
|------|-------|-------------|---------|
| **public** | 34 tools | All Domain Assistants | Read-only search, sessions, basic operations |
| **developer** | 14 tools | DA developers, operators | Overrides, analytics, validation, RLM access |
| **admin** | 6 tools | Platform team | Ingestion, data mutation, token management |

### Tier Inheritance

Tiers are hierarchical — higher tiers inherit all permissions from lower tiers:

```
admin ⊃ developer ⊃ public
```

An admin API key can invoke all 56 tools. A developer key can invoke public + developer tools (50 tools). A public key is limited to 36 tools.

This inheritance is implemented via **Cerbos derived roles** in `policies/derived_roles.yaml`.

### Tool Tier Mapping

`tool_tiers.py` contains the complete mapping. Examples:

```python
TOOL_TIERS = {
    # Category 1: Search — all public
    "search_database": "public",
    "search_nodes": "public",
    "get_node_by_id": "public",
    "get_neighbors": "developer",    # graph traversal
    "shortest_path": "developer",    # path analysis
    "execute_cypher": "developer",   # raw Cypher

    # Category 5: Ingestion — all admin
    "ingest_file": "admin",
    "ingest_module_from_repo": "admin",
    "batch_ingest_modules": "admin",
    "ingest_repository": "admin",

    # Category 6: RLM — developer
    "rlm_orchestrate": "developer",
    "rlm_preview": "developer",
    ...
}
```

---

## 3. API Key Authentication

### Key Format

API keys follow the convention `key-{da_code}-{number}`:
- `key-cia-001` — CIA (Code Generator)
- `key-gest-001` — GEST (Test Generator)
- `key-admin-001` — Admin key

### Key → Principal Mapping

Defined in `mcp/auth/api_keys.yaml`:

```yaml
principals:
  - id: "key-cia-001"
    name: "CIA Domain Assistant"
    roles:
      illd: "public"
      mcal: "public"

  - id: "key-saga-001"
    name: "SAGA Architecture Analyst"
    roles:
      illd: "developer"
      mcal: "developer"

  - id: "key-admin-001"
    name: "Platform Admin"
    roles:
      illd: "admin"
      mcal: "admin"
```

**Workspace-scoped roles**: A principal can have different roles in different workspaces. For example, a DA might have `developer` access to `illd` but only `public` access to `mcal`.

### Key Resolution

`load_api_keys()` reads the YAML file at startup. `resolve_principal(api_key)` returns the principal with its workspace-scoped roles.

---

## 4. Cerbos PDP Integration

### Architecture

Cerbos runs as a **sidecar subprocess** within the MCP server container:

```
Docker Container
├── Python MCP Server (port 8000)
└── Cerbos PDP (port 3592 HTTP, 3593 gRPC)
```

The Dockerfile uses a multi-stage build to bundle the Cerbos binary:
```
Stage 1: FROM ghcr.io/cerbos/cerbos:latest → copy /cerbos binary
Stage 2: FROM python:3.12-slim → install Python deps + copy /cerbos
```

### Policy Structure

**Resource policy** (`policies/resource_mcp_tool.yaml`):

```yaml
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  resource: "mcp_tool"
  rules:
    - actions: ["invoke"]
      roles: ["admin"]
      effect: EFFECT_ALLOW
      # admin can invoke everything

    - actions: ["invoke"]
      roles: ["developer"]
      effect: EFFECT_ALLOW
      condition:
        match:
          expr: >
            request.resource.attr.tier in ["public", "developer"]

    - actions: ["invoke"]
      roles: ["public"]
      effect: EFFECT_ALLOW
      condition:
        match:
          expr: >
            request.resource.attr.tier == "public"
```

**Derived roles** (`policies/derived_roles.yaml`):

Implements tier inheritance so that `admin` inherits `developer` permissions and `developer` inherits `public` permissions.

### Check Authorization

```python
def check_authorization(api_key: str, tool_name: str, workspace_id: str) -> bool:
    principal = resolve_principal(api_key)
    role = principal.roles.get(workspace_id, "public")
    tier = TOOL_TIERS.get(tool_name, "admin")  # default-deny for unknown tools

    # Call Cerbos PDP
    response = cerbos_client.check_resource(
        principal={"id": api_key, "roles": [role]},
        resource={"kind": "mcp_tool", "id": tool_name, "attr": {"tier": tier}},
        actions=["invoke"],
    )
    return response.is_allowed("invoke")
```

---

## 5. ASGI Middleware

`_APIKeyMiddleware` wraps the ASGI application (Uvicorn + FastMCP):

```python
class _APIKeyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
                _current_api_key.set(api_key)  # contextvars
        await self.app(scope, receive, send)
```

**Why `contextvars`?**: ASGI is async — multiple requests are handled concurrently. `contextvars.ContextVar` provides per-task (per-request) isolation, ensuring API keys don't leak between concurrent requests. This is the standard Python approach for per-request context in async applications.

For `stdio` transport (local dev), the API key is read from `MCP_API_KEY` environment variable.

---

## 6. Per-Request Authorization Flow

Every MCP tool handler calls `_authorize()`:

```python
def _authorize(tool_name: str):
    api_key = _current_api_key.get(None)
    if api_key is None:
        api_key = os.environ.get("MCP_API_KEY", "")

    workspace_id = _resolve_workspace()  # from request context or default

    allowed = check_authorization(api_key, tool_name, workspace_id)
    if not allowed:
        raise AuthorizationError(f"Not authorized to invoke {tool_name}")

    # Best-effort audit logging
    _audit_log(api_key, tool_name, workspace_id)
```

Audit logging writes to PostgreSQL `audit_logs` table. The write is non-blocking and best-effort — auth failures are logged but don't prevent the auth check from completing.

---

## 7. Graceful Fallback

If Cerbos PDP is unavailable (startup delay, crash, network issue), the auth middleware falls back to a **local tier check**:

```python
def check_authorization_local(api_key, tool_name, workspace_id):
    principal = resolve_principal(api_key)
    role = principal.roles.get(workspace_id, "public")
    tier = TOOL_TIERS.get(tool_name, "admin")

    tier_hierarchy = {"public": 0, "developer": 1, "admin": 2}
    return tier_hierarchy.get(role, 0) >= tier_hierarchy.get(tier, 2)
```

This ensures the server remains operational during Cerbos restarts. The fallback provides the same authorization logic, just without the policy-as-code flexibility of Cerbos.

---

## 8. Security Boundaries

### Read-Only Cypher

The `execute_cypher` tool (developer tier) blocks write operations:

```python
WRITE_CLAUSES = {"CREATE", "DELETE", "SET", "MERGE", "DROP", "REMOVE"}

def validate_cypher(query: str):
    tokens = query.upper().split()
    for clause in WRITE_CLAUSES:
        if clause in tokens:
            raise SecurityError(f"Write clause '{clause}' not allowed")
```

### No Credentials in Code

All secrets are resolved from environment variables:
- Neo4j password: `NEO4J_PASSWORD`
- Redis URL: `REDIS_URL`
- PostgreSQL DSN: `POSTGRES_DSN`
- API keys: loaded from `api_keys.yaml` (not in version control in production — mounted as a Kubernetes secret)

### Default-Deny

Unknown tools default to `admin` tier in `TOOL_TIERS`. If a tool isn't explicitly mapped, only admin keys can invoke it.

---

## 9. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `mcp/core/auth_middleware.py` | 252 | Cerbos integration, key resolution, authorization check |
| `mcp/core/tool_tiers.py` | 82 | 56-tool → tier mapping, `role_may_invoke()` |
| `mcp/auth/api_keys.yaml` | ~40 | API key → principal mapping |
| `mcp/auth/policies/resource_mcp_tool.yaml` | ~30 | Cerbos resource policy for tools |
| `mcp/auth/policies/derived_roles.yaml` | ~20 | Tier inheritance via derived roles |
| `mcp/auth/.cerbos.yaml` | ~10 | Cerbos PDP configuration |

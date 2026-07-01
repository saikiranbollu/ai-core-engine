# Authentication & Security Architecture

**Component**: `mcp/core/auth_middleware.py` (ASGI key extraction), `mcp/core/auth/` (Cerbos client + tier logic), `mcp/auth/` (Cerbos policies + API keys)
**Primary pieces**: `_APIKeyMiddleware`, `check_authorization()` in `mcp/core/auth/cerbos_client.py`
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
| **developer** | 16 tools | DA developers, operators | Overrides, analytics, validation, RLM orchestration |
| **admin** | 5 tools | Platform team | Cache mutation, result processing, token refresh |

### Tier Inheritance

Tiers are hierarchical — higher tiers inherit all permissions from lower tiers:

```
admin ⊃ developer ⊃ public
```

An admin API key can invoke all 55 active tools. A developer key can invoke public + developer tools (50 tools). A public key is limited to 34 tools. Source of truth: [`mcp/core/tool_tiers.py`](../../mcp/core/tool_tiers.py).

This inheritance is implemented via **Cerbos derived roles** in `policies/derived_roles.yaml`.

### Tool Tier Mapping

`tool_tiers.py` contains the complete mapping. Examples:

```python
TOOL_TIERS = {
    # Category 1: Search — mostly public, advanced graph traversal is developer
    "search_database": "public",
    "search_nodes": "public",
    "get_node_by_id": "public",
    "get_neighbors": "developer",    # graph traversal
    "shortest_path": "developer",    # path analysis
    "execute_cypher": "developer",   # raw Cypher

    # Category 5: Ingestion (admin) — removed from MCP registration in Plan 2 Phase 2.
    # ingest_file / ingest_module_from_repo / batch_ingest_modules / ingest_repository
    # are no longer exposed as MCP tools; use sandbox_upload (public, per-session) or
    # invoke IngestionService directly from library code. process_results remains admin.
    "process_results": "admin",

    # Category 6: RLM — developer (orchestrate), public (plan preview)
    "rlm_orchestrate": "developer",
    "rlm_plan_preview": "public",
    ...
}
```

---

## 3. API Key Authentication

### Key Format

API keys follow the convention `key-{da_code}-{number}`:
- `key-eda-001` — EDA (Embedded Driver Assistant, iLLD workspace)
- `key-gest-001` — GEST (Test Generator, MCAL workspace)

### Key → Principal Mapping

Defined in `mcp/auth/api_keys.yaml`:

```yaml
keys:
  # EDA is the only assistant with iLLD access; every other DA is MCAL-only.
  "key-eda-001":
    principal_id: "eda_assistant"
    roles:
      illd: ["public", "developer"]

  "key-gest-001":
    principal_id: "gest_assistant"
    roles:
      mcal: ["public"]
```

**Workspace-scoped roles**: A principal can hold different roles per workspace. In this deployment EDA holds `public` + `developer` on `illd`, while all other Domain Assistants are scoped to the `mcal` workspace only.

### Key Resolution

`load_api_keys()` reads the YAML file at startup. `resolve_principal(api_key)` returns the principal with its workspace-scoped roles. The YAML schema is `keys: { "<api-key>": { principal_id, roles: { <workspace>: [<role>, ...] } } }` — each workspace maps to a **list** of roles. Use `"*"` as a wildcard workspace for global access.

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
def check_authorization(
    api_key: str, tool_name: str,
    workspace_id: str = "illd", module_name: str | None = None,
) -> tuple[bool, str]:
    principal = resolve_principal(api_key, workspace_id)
    if principal is None:
        return False, "Unknown or missing API key"

    tier = get_tool_tier(tool_name)            # from tool_tiers.py
    if tier is None:
        return False, f"Unknown tool: {tool_name}"

    if _CERBOS_SDK_AVAILABLE:
        resource = Resource(id=tool_name, kind="mcp_tool", attr={
            "tier": tier, "workspace_id": workspace_id,
            "module_scope": module_name or "", ...
        })
        try:
            resp = client.is_allowed("invoke", principal, resource)  # 1s timeout
            _set_cerbos_up(True)
            return (True, "allowed") if resp else (False, "Insufficient access tier ...")
        except Exception:
            _set_cerbos_up(False)              # PDP down → reconnect next call
            # fall through to local tier check

    return check_via_local_tiers(principal, tool_name, tier)
```

The check returns `(allowed, message)`. A 1-second Cerbos timeout (`_CERBOS_TIMEOUT_S`) prevents a
hung PDP from blocking tool dispatch, and the `aice_cerbos_up` gauge tracks PDP reachability.

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

    allowed, message = check_authorization(api_key, tool_name, workspace_id)
    if not allowed:
        raise AuthorizationError(message)

    # Best-effort audit logging (completion-time row records the outcome)
    _audit_log(api_key, tool_name, workspace_id)
```

Audit logging writes to PostgreSQL `audit_logs` table. The write is non-blocking and best-effort — auth failures are logged but don't prevent the auth check from completing.

---

## 7. Graceful Fallback

If Cerbos PDP is unavailable (startup delay, crash, network issue), the auth middleware falls back to a **local tier check**:

```python
def check_via_local_tiers(principal, tool_name, tier) -> tuple[bool, str]:
    # mcp/core/auth/local_fallback.py — uses TIER_HIERARCHY from tool_tiers.py
    for role in principal.roles:
        if tier in TIER_HIERARCHY.get(role, set()):
            return True, "allowed"
    return False, (f"Insufficient access tier for tool '{tool_name}'. "
                   f"Required: {tier}, your roles: {sorted(principal.roles)}")
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
| `mcp/core/auth_middleware.py` | 51 | ASGI middleware: extract Bearer key → `contextvars` |
| `mcp/core/tool_tiers.py` | 166 | 55-tool → tier mapping, `TIER_HIERARCHY`, `get_tool_tier()` |
| `mcp/core/auth/cerbos_client.py` | 186 | `check_authorization()`, Cerbos PDP call (1s timeout), DENY audit, `aice_cerbos_up` gauge |
| `mcp/core/auth/api_key_registry.py` | 97 | `load_api_keys()` — API key registry loader |
| `mcp/core/auth/principal.py` | 62 | `resolve_principal()` — workspace-scoped roles |
| `mcp/core/auth/local_fallback.py` | 23 | `check_via_local_tiers()` — local check when PDP unreachable |
| `mcp/auth/api_keys.yaml` | 118 | API key → principal mapping |
| `mcp/auth/policies/resource_mcp_tool.yaml` | 122 | Cerbos resource policy for tools |
| `mcp/auth/policies/derived_roles.yaml` | 24 | Tier inheritance via derived roles |
| `mcp/auth/.cerbos.yaml` | 21 | Cerbos PDP configuration |

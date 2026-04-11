# AICE MCP Server — Quick Start & Configuration Guide

**Companion to [DOCUMENTATION.md](DOCUMENTATION.md) — practical setup and usage reference for Domain Assistant developers.**

> **Deployment model:** The AICE MCP server, Neo4j, Qdrant, Redis, PostgreSQL, and Cerbos are **already deployed and running** on the Infineon Cloud. You do **not** need to install or configure any server-side infrastructure. This guide focuses on connecting your Domain Assistant (running locally or in CI/CD) to the cloud-hosted AICE server.

---

## Table of Contents

1. [Overview](#1-overview)
2. [What You Need (Client-Side)](#2-what-you-need-client-side)
3. [Connecting to the AICE Server](#3-connecting-to-the-aice-server)
4. [Authentication & API Keys](#4-authentication--api-keys)
5. [Tool Usage Examples](#5-tool-usage-examples)
6. [Session Lifecycle Walkthrough](#6-session-lifecycle-walkthrough)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Overview

The AI Core Engine (AICE) MCP server exposes **56 tools across 13 categories** for automotive embedded software development. Domain Assistants (DAs) connect via the Model Context Protocol (MCP) over HTTP (`streamable-http`).

```
┌─────────────────────────────────────────────┐
│   Your Machine (Local / CI/CD)              │
│                                             │
│   Domain Assistant (CIA, GEST, ACRA, …)     │
│            │                                │
└────────────┼────────────────────────────────┘
             │ MCP (JSON-RPC 2.0 over HTTP)
             │ Authorization: Bearer <api-key>
             ▼
┌─────────────────────────────────────────────┐
│   Infineon Cloud                            │
│                                             │
│   ┌─────────────────────────┐               │
│   │  AICE MCP Server        │               │
│   │  ├── Cerbos Auth (RBAC) │               │
│   │  └── 56 Tools           │               │
│   ├─────────┬───────┬───────┤               │
│   │ Neo4j   │Qdrant │ Redis │  PostgreSQL   │
│   │ (KG)    │(Vecs) │(Cache)│  (Audit)      │
│   └─────────┴───────┴───────┘               │
└─────────────────────────────────────────────┘
```

---

## 2. What You Need (Client-Side)

| Requirement | Details |
|-------------|---------|
| **API Key** | Obtain from the AICE platform team (e.g., `key-cia-001`) |
| **Server URL** | The AICE MCP endpoint URL provided by your team (e.g., `https://<aice-host>/mcp`) |
| **Python 3.11+** | If using the MCP Python SDK or running a DA locally |
| **Network access** | Connectivity to the Infineon Cloud AICE endpoint |

> **No server-side setup required.** Neo4j, Qdrant, Redis, PostgreSQL, and Cerbos are all managed and running on the Infineon Cloud.

---

## 3. Connecting to the AICE Server

### 3.1 HTTP (Recommended — Production & CI/CD)

The standard way to connect. Your DA sends MCP JSON-RPC requests over HTTP with an API key in the `Authorization` header.

```python
import httpx

AICE_URL = "https://<aice-host>/mcp"   # ← Get from your platform team
API_KEY  = "key-cia-001"               # ← Your assigned API key

client = httpx.Client(
    base_url=AICE_URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=60.0,
)

# Call any MCP tool
response = client.post("/", json={
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "search_database",
        "arguments": {
            "query": "ADC initialization sequence",
            "workspace": "illd",
            "alpha": 0.5,
            "top_k": 10,
        },
    },
    "id": 1,
})

result = response.json()
print(result)
```

### 3.2 VS Code / Copilot Chat (IDE Integration)

For local development with VS Code Copilot, you can connect to the cloud server via HTTP. Add to your project's `.vscode/mcp.json`:

```json
{
  "servers": {
    "aice": {
      "type": "http",
      "url": "https://<aice-host>/mcp",
      "headers": {
        "Authorization": "Bearer key-cia-001"
      }
    }
  }
}
```

### 3.3 curl (Quick Testing)

```bash
AICE_URL="https://<aice-host>/mcp"
API_KEY="key-gest-001"

# List all available tools
curl -X POST "$AICE_URL" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}'

# Call a specific tool
curl -X POST "$AICE_URL" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0",
    "method":"tools/call",
    "params":{
      "name":"health_check",
      "arguments":{"verbose":true}
    },
    "id":2
  }'
```

### 3.4 CI/CD Pipeline Integration

In Jenkins or GitHub Actions, set the API key as a secret and call the AICE server:

```yaml
# Example: GitHub Actions
env:
  AICE_URL: "https://<aice-host>/mcp"
  AICE_API_KEY: ${{ secrets.AICE_API_KEY }}

steps:
  - run: python my_da/run.py --aice-url "$AICE_URL" --api-key "$AICE_API_KEY"
```

### 3.5 Client-Side Environment Variables

These are the only environment variables you need on your local/CI machine:

| Variable | Required | Description |
|----------|----------|-------------|
| `AICE_URL` | Yes | AICE MCP server endpoint (e.g., `https://<aice-host>/mcp`) |
| `AICE_API_KEY` | Yes | Your API key (e.g., `key-cia-001`) |

> **You do NOT need** `NEO4J_URI`, `QDRANT_URL`, `REDIS_URL`, `POSTGRES_DSN`, or any other backend variables. Those are server-side configuration managed by the platform team.

---

## 4. Authentication & API Keys

### 4.1 Three-Tier RBAC

The AICE server uses a three-tier role-based access control system:

| Tier | Access | Tool Count |
|------|--------|-----------|
| **public** | Basic search, sessions, feedback | 34 |
| **developer** | + graph traversal, analytics, visualization | 14 |
| **admin** | + ingestion, cache management, token refresh | 8 |

Hierarchy: `admin ⊃ developer ⊃ public` — higher tiers inherit all lower-tier permissions.

### 4.2 How Auth Works

Every request to the AICE server must include the API key in the `Authorization` header:

```
Authorization: Bearer <KEY>
```

The server resolves the key → principal → roles, then checks Cerbos RBAC policies before granting tool access.

> **Need a new API key?** Contact the platform team. Key provisioning is handled server-side.

---

## 5. Tool Usage Examples

### 5.1 Search & Query (Category 1)

```python
# Hybrid search — balanced graph + vector
search_database(query="ADC channel group conversion",
                workspace="illd", alpha=0.5, top_k=10)

# Graph-heavy search — structural queries
search_database(query="Adc_StartGroupConversion dependencies",
                workspace="illd", alpha=0.2)

# Vector-heavy search — conceptual queries
search_database(query="how to configure ADC for continuous scanning",
                workspace="illd", alpha=0.8)

# Structured node search by label
search_nodes(label="APIFunction", keyword="Adc_Init",
             workspace="illd", limit=5)

# Get exact node
get_node_by_id(node_id="Adc_StartGroupConversion", workspace="illd")

# Graph traversal — find neighbors
get_neighbors(node_id="Adc_StartGroupConversion",
              relationship_types=["CALLS", "DEPENDS_ON"],
              direction="out", limit=20)

# Shortest path between nodes
shortest_path(source_id="Adc_Init", target_id="Adc_DeInit", max_depth=5)

# Raw Cypher (read-only)
execute_cypher(
    query="MATCH (f:APIFunction)-[:CALLS]->(g:APIFunction) "
          "WHERE f.name = $name RETURN g.name, g.module",
    params={"name": "Adc_StartGroupConversion"},
    workspace="illd"
)
```

### 5.2 API Intelligence (Category 2)

```python
# Comprehensive function details (25+ fields)
query_api_function(function_name="Adc_StartGroupConversion",
                   workspace="illd")

# Resolve struct/enum/typedef
get_type_definition(type_name="Adc_ConfigType", workspace="illd")

# Generate initialization code with custom overrides
generate_initialization_code(
    type_name="Adc_ConfigType",
    overrides={"AdcHwTriggerSource": "ADC_TRIG_TIMER"},
    workspace="illd"
)
```

### 5.3 Dependency Analysis (Category 3)

```python
# Dependency tree with init ordering
query_dependencies(function_name="Adc_StartGroupConversion",
                   depth=3, workspace="illd")

# Validate call sequence
validate_api_usage(
    call_sequence=["Adc_Init", "Adc_SetupResultBuffer",
                   "Adc_StartGroupConversion"],
    workspace="illd"
)

# Detect polling requirements
detect_polling_requirements(function_name="Adc_StartGroupConversion",
                            workspace="illd")
```

### 5.4 Traceability (Category 4)

```python
# Full V-Model trace chain
find_requirement_traces(requirement_id="SHRQ-12345", workspace="mcal")

# Module-wide traceability matrix
build_traceability_matrix(module="Adc", format="html", workspace="mcal")

# Find coverage gaps
find_coverage_gaps(module="Adc", workspace="mcal")

# HW-SW register mapping
analyze_hw_sw_links(module="Adc", workspace="illd")
```

### 5.5 Ingestion (Category 5 — Admin)

```python
# Single file
ingest_file(file_path="/repo/Adc/src/Adc.c",
            workspace="illd", module="Adc")

# Entire module
ingest_module_from_repo(repo_path="/repo", module="Adc",
                        workspace="illd")

# Multiple modules
batch_ingest_modules(repo_path="/repo",
                     modules=["Adc", "Spi", "Can"],
                     workspace="illd")

# Full repository
ingest_repository(repo_path="/repo", workspace="illd")
```

### 5.6 Session & Context (Category 6)

```python
# Start session
session_start(session_id="CIA_20260322_001",
              assistant_name="CIA", module_context="Adc",
              ttl_seconds=3600)

# Store data
session_store(session_id="CIA_20260322_001",
              key="user_requirements", value={"files": [...]})

# Retrieve data
session_retrieve(session_id="CIA_20260322_001",
                 key="user_requirements")

# Build context with token budget
build_context(session_id="CIA_20260322_001",
              query="Generate Adc_Init implementation",
              search_results=results, max_tokens=8192)

# End session
session_end(session_id="CIA_20260322_001")
```

### 5.7 Ephemeral Sandbox (Category 6+)

```python
# Upload user documents to per-session temporary store
sandbox_upload(session_id="CIA_20260322_001",
               file_path="/path/to/customer_spec.pdf")

# Search within uploaded documents
sandbox_query(session_id="CIA_20260322_001",
              query="ADC timing requirements")

# Check sandbox status
sandbox_status(session_id="CIA_20260322_001")

# Release sandbox storage
sandbox_clear(session_id="CIA_20260322_001")
```

### 5.8 RLM — Multi-Step Context (Category 6+)

```python
# Preview query decomposition plan
rlm_plan_preview(
    query="Generate tests for Adc_StartGroupConversion covering "
          "all dependencies, register patterns, and MISRA compliance",
    task_type="test_generation"
)

# Execute multi-step context assembly
rlm_orchestrate(
    query="Generate tests for Adc_StartGroupConversion...",
    task_type="test_generation",
    session_id="GEST_20260322_001",
    workspace="illd"
)
```

### 5.9 Cache (Category 7)

```python
# Check cache performance
cache_stats()

# Inspect cache for a query
cache_get(query="ADC initialization")

# Invalidate after re-ingestion (admin)
cache_invalidate_module(module="Adc")

# Clear all caches (admin)
cache_clear(tier="all")
```

### 5.10 Feedback & Learning (Category 8)

```python
# Submit approval with learning loop
submit_human_feedback(
    response_id="resp-001",
    decision="APPROVE",
    correction_notes="Code is correct",
    module="Adc",
    task_type="code_generation",
    response_context="<the actual generated code>"
)
# → Creates ApprovedPattern in Neo4j + indexes in Qdrant

# Submit rejection
submit_human_feedback(
    response_id="resp-002",
    decision="REJECT",
    issues_found=3,
    correction_notes="Missing error handling for NULL pointer",
    module="Spi",
    task_type="code_generation"
)
# → Records failure pattern in PostgreSQL

# View learning metrics (developer)
get_learning_metrics(module="Adc", time_range="7d")

# Query failure patterns (developer)
get_failure_patterns(module="Spi", category="missing_error_handling")

# Process CI/CD results (admin)
process_results(
    results_dir="/ci/output/junit",
    result_type="junit",
    module_name="Adc",
    learn_from_failures=True,
    update_graph=True,
    workspace_id="illd"
)
```

### 5.11 Review Gate (Category 9)

```python
# Evaluate confidence
evaluation = evaluate_confidence(
    response=da_output,
    context=search_context,
    session_id="GEST_20260322_001"
)
# Returns: score (0-100), review_type (AUTO/QUICK/FULL), signals

# Auto-approve if routed to AUTO
complete_review(review_id=evaluation["data"]["review_id"],
                outcome="approved")

# Override routing (developer)
override_review_routing(review_id="rev-001",
                        new_type="FULL",
                        reason="Safety-critical module")

# Review analytics (developer)
get_review_analytics()
```

### 5.12 Observability (Category 11)

```python
# System health
health_check(verbose=True)

# Graph statistics
get_graph_statistics(workspace="illd")

# List known modules
list_available_modules(workspace="illd")

# Distribution analysis
get_distribution(dimension="asil", workspace="mcal")

# Coverage report
get_coverage_report(module="Adc", workspace="mcal")
```

---

## 6. Session Lifecycle Walkthrough

Every Domain Assistant integration should follow this pattern:

```
┌────────────────────────────────────────────────────┐
│  1. session_start   → Open working memory session  │
│  2. search_database → Find relevant knowledge      │
│  3. sandbox_upload  → (Optional) Upload user docs  │
│  4. build_context   → Assemble LLM context         │
│  5. [DA work]       → Generate/review/analyze      │
│  6. evaluate_confidence → Score the output         │
│  7. submit_human_feedback → Record decision        │
│  8. session_end     → Close and persist audit      │
└────────────────────────────────────────────────────┘
```

**Complete Python example** (using the HTTP connection from Section 3):

```python
import httpx

# Configuration
AICE_URL = "https://<aice-host>/mcp"
API_KEY  = "key-cia-001"
SESSION_ID = "CIA_20260322_001"
WORKSPACE = "illd"
MODULE = "Adc"

client = httpx.Client(
    base_url=AICE_URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=60.0,
)

def call_tool(name: str, arguments: dict) -> dict:
    """Helper: call an MCP tool via HTTP."""
    resp = client.post("/", json={
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
        "id": 1,
    })
    return resp.json().get("result", resp.json())

# --- Step 1: Open session ---
call_tool("session_start", {
    "session_id": SESSION_ID,
    "assistant_name": "CIA",
    "module_context": MODULE,
    "ttl_seconds": 3600,
})

# --- Step 2: Search for knowledge ---
search_results = call_tool("search_database", {
    "query": "Adc_StartGroupConversion implementation requirements",
    "workspace": WORKSPACE,
    "module_filter": MODULE,
    "alpha": 0.5,
    "top_k": 10,
})

# --- Step 3: (Optional) Upload customer specification ---
call_tool("sandbox_upload", {
    "session_id": SESSION_ID,
    "file_path": "/path/to/customer_adc_spec.pdf",
})

# --- Step 4: Build token-budget-aware context ---
context = call_tool("build_context", {
    "session_id": SESSION_ID,
    "query": "Generate Adc_StartGroupConversion implementation",
    "search_results": search_results["data"]["results"],
    "max_tokens": 8192,
})

# --- Step 5: Domain Assistant performs its work ---
# CIA generates driver code using the assembled context
# (This is where the DA calls its LLM with the context)
generated_code = "..."  # DA output

# --- Step 6: Evaluate confidence ---
evaluation = call_tool("evaluate_confidence", {
    "response": {"code": generated_code, "module": MODULE},
    "context": context["data"],
    "session_id": SESSION_ID,
})

score = evaluation["data"]["score"]
review_type = evaluation["data"]["review_type"]

# --- Step 7: Handle review routing ---
if review_type == "AUTO":
    call_tool("complete_review", {
        "review_id": evaluation["data"]["review_id"],
        "outcome": "approved",
    })
    call_tool("submit_human_feedback", {
        "response_id": evaluation["data"]["response_id"],
        "decision": "APPROVE",
        "module": MODULE,
        "task_type": "code_generation",
        "response_context": generated_code,
    })
else:
    print(f"Review required ({review_type}), score: {score}")

# --- Step 8: Close session ---
call_tool("session_end", {"session_id": SESSION_ID})
```

---

## 7. Troubleshooting

### 7.1 Common Client-Side Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `PERMISSION_DENIED` error | Wrong API key or insufficient tier | Verify your API key and that it has the required role for your workspace |
| `No API key provided` | Missing `Authorization` header | Add `Authorization: Bearer <your-key>` header to every request |
| `Unknown tool: xxx` | Tool name typo or tool not in your tier | Call `tools/list` to see available tools for your API key |
| Connection refused / timeout | Network issue or wrong server URL | Verify `AICE_URL`, check network/VPN connectivity to Infineon Cloud |
| `BACKEND_ERROR` for Neo4j/Qdrant | Server-side backend issue | Contact the platform team — this is a server-side issue |
| SSL/TLS certificate error | Corporate proxy or missing CA cert | Configure your HTTP client to trust the Infineon CA certificate |
| Session expired | TTL exceeded | Increase `ttl_seconds` in `session_start` or start a new session |
| Slow responses | Network latency or large result set | Reduce `top_k`, add `module_filter`, or check VPN routing |

### 7.2 Diagnostic Commands

```bash
AICE_URL="https://<aice-host>/mcp"
API_KEY="key-gest-001"

# Test connectivity
curl -s -o /dev/null -w "%{http_code}" "$AICE_URL"
# Expected: 200 or 405

# Health check (shows backend status)
curl -X POST "$AICE_URL" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"health_check","arguments":{"verbose":true}},"id":1}'

# List tools (verify auth works)
curl -X POST "$AICE_URL" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}'
```

### 7.3 Getting Help

- **Server-side issues** (backends down, ingestion problems, new API key requests): Contact the **platform team**.
- **DA integration issues** (how to call tools, context assembly, session management): See [DOCUMENTATION.md](DOCUMENTATION.md) for the full technical reference.

---

## Quick Reference Card

| What | How |
|------|-----|
| Connect (Python) | `httpx.Client(base_url="https://<aice-host>/mcp", headers={"Authorization": "Bearer <key>"})` |
| Connect (curl) | `curl -X POST https://<aice-host>/mcp -H "Authorization: Bearer <key>" -H "Content-Type: application/json" -d '{...}'` |
| Connect (VS Code) | Add HTTP server to `.vscode/mcp.json` (see Section 3.2) |
| List tools | `{"method":"tools/list","params":{}}` |
| Call a tool | `{"method":"tools/call","params":{"name":"<tool>","arguments":{...}}}` |
| Auth | `Authorization: Bearer key-cia-001` header on every request |
| Workspaces | `illd` (iLLD drivers) or `mcal` (AUTOSAR MCAL) |
| Response format | `{"error": false, "data": {...}}` or `{"error": true, "error_code": "...", "message": "..."}` |
| Need help? | Platform issues → platform team · DA integration → [DOCUMENTATION.md](DOCUMENTATION.md) |

---

*See [DOCUMENTATION.md](DOCUMENTATION.md) for the complete technical reference.*

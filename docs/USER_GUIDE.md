# AICE User Guide

**Version 2.1.0 | Sprint 25**

> This guide is for Domain Assistant (DA) developers integrating with the AI Core Engine. For the complete API reference see [DOCUMENTATION.md](DOCUMENTATION.md). For initial connection setup see [MCP_QUICKSTART.md](MCP_QUICKSTART.md).

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Connection & Authentication](#2-connection--authentication)
3. [Core Workflows](#3-core-workflows)
   - [3.1 Basic Search Workflow](#31-basic-search-workflow)
   - [3.2 Session-Based Context Workflow](#32-session-based-context-workflow)
   - [3.3 Complex Query Workflow (RLM)](#33-complex-query-workflow-rlm)
   - [3.4 User Document Workflow (Sandbox)](#34-user-document-workflow-sandbox)
4. [Tool Selection Guide](#4-tool-selection-guide)
5. [Working with Workspaces](#5-working-with-workspaces)
6. [Confidence Scoring & Review Gate](#6-confidence-scoring--review-gate)
7. [DA-Specific Patterns](#7-da-specific-patterns)
8. [Performance Tips](#8-performance-tips)
9. [Troubleshooting](#9-troubleshooting)
10. [Tool Quick Reference](#10-tool-quick-reference)

---

## 1. Introduction

The AI Core Engine (AICE) is the shared knowledge backbone for all Domain Assistants. It exposes **55 MCP tools** covering:

- **Knowledge retrieval**: hybrid graph + vector search over structured automotive SW knowledge
- **API intelligence**: detailed function metadata, type resolution, code generation
- **Traceability**: V-Model chain from requirements → architecture → code → tests
- **Session management**: per-DA working memory and context assembly
- **Confidence scoring**: deterministic review routing (AUTO / QUICK / FULL)
- **Learning loop**: human feedback feeds back into future retrievals

**What AICE is NOT**: It does not generate code, answers, or analysis. It provides context to your DA's LLM.

### Workspaces

All tools take a `workspace_id` or `workspace` parameter (aliases for the same concept):

| Workspace | Product | Typical Use |
|-----------|---------|-------------|
| `illd` | iLLD Reference Drivers | Code generation, API lookup, register analysis |
| `mcal` | AUTOSAR MCAL Productive SW | Requirements traceability, ASPICE, MISRA compliance |

---

## 2. Connection & Authentication

### Requirements

- API key from the platform team (e.g., `key-cia-001`)
- Server URL: `https://<aice-host>/mcp` (provided by platform team)
- Python 3.11+ with `httpx` or the MCP Python SDK

### Python Client Setup

```python
import httpx

AICE_URL = "https://<aice-host>/mcp"
API_KEY  = "key-cia-001"

client = httpx.Client(
    base_url=AICE_URL,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=60.0,
)

def call_tool(name: str, arguments: dict) -> dict:
    resp = client.post("/", json={
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
        "id": 1,
    })
    result = resp.json()
    return result.get("result", result)
```

### API Key Tiers

| Tier | Typical Key | Access |
|------|------------|--------|
| `public` | `key-cia-001`, `key-gest-001` | Search, sessions, feedback, confidence |
| `developer` | `key-dev-001` | + Graph traversal, analytics, visualization, `query_enhance` |
| `admin` | `key-admin-001` | + Cache management, `cache_refresh_config` |

---

## 3. Core Workflows

### 3.1 Basic Search Workflow

Use this for simple single-turn lookups: "what does this function do?", "find all MISRA violations in module X".

```python
# Step 1: Search
results = call_tool("search_database", {
    "query": "ADC channel group initialization",
    "workspace_id": "illd",
    "module_filter": "Adc",
    "alpha": 0.5,          # 0.0=pure vector, 1.0=pure graph, 0.5=balanced
    "top_k": 10,
})

# Step 2 (optional): Look up a specific function
func = call_tool("query_api_function", {
    "function_name": "Adc_StartGroupConversion",
    "workspace_id": "illd",
})

# Step 3 (optional): Resolve a type
type_def = call_tool("get_type_definition", {
    "type_name": "Adc_ConfigType",
    "workspace_id": "illd",
})
```

**Alpha guidance:**

| Use Case | Alpha |
|----------|-------|
| Structural ("what calls X?", "find all registers in Adc") | 0.1–0.3 |
| Balanced (most queries) | 0.5 |
| Conceptual ("how to configure baud rate") | 0.7–0.9 |

**Tip**: Use `query_enhance` (developer tier) to get an auto-suggested `alpha` and strategy before searching:

```python
hint = call_tool("query_enhance", {
    "query": "Find all functions accessing SFR registers in ADC module",
})
# Returns: suggested_alpha, strategy, detected_modules, complexity
```

---

### 3.2 Session-Based Context Workflow

Use this for multi-turn DA sessions where context accumulates across tool calls.

```python
SESSION_ID = "CIA_20260502_001"
WORKSPACE  = "illd"
MODULE     = "Adc"

# 1. Open session
call_tool("session_start", {
    "session_id": SESSION_ID,
    "assistant_name": "CIA",
    "module_context": MODULE,
})

# 2. Retrieve knowledge
search_results = call_tool("search_database", {
    "query": "Adc_StartGroupConversion implementation",
    "workspace_id": WORKSPACE,
    "module_filter": MODULE,
    "alpha": 0.5,
    "top_k": 10,
})

# 3. Store intermediate results for later
call_tool("session_store", {
    "session_id": SESSION_ID,
    "key": "search_results",
    "value": search_results["data"]["results"],
})

# 4. Assemble context with token budget
context = call_tool("build_context", {
    "session_id": SESSION_ID,
    "query": "Generate Adc_StartGroupConversion implementation",
    "search_results": search_results["data"]["results"],
    "max_tokens": 8192,
})

# 5. Your DA calls its LLM with context["data"]
generated_output = your_llm(context["data"])

# 6. Evaluate confidence
evaluation = call_tool("evaluate_confidence", {
    "response": {"output": generated_output, "module": MODULE},
    "context": context["data"],
    "session_id": SESSION_ID,
})
review_type = evaluation["data"]["review_type"]  # AUTO | QUICK | FULL

# 7. Handle review and submit feedback
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
        "response_context": generated_output,
    })
else:
    # Route to human reviewer (QUICK = 15 min, FULL = formal review)
    print(f"Human review required: {review_type}, score: {evaluation['data']['score']}")

# 8. Close session
call_tool("session_end", {"session_id": SESSION_ID})
```

---

### 3.3 Complex Query Workflow (RLM)

Use the RLM Orchestrator for queries that require multiple knowledge sources. It decomposes your query into up to 6 targeted sub-queries and synthesizes the results.

When to use RLM:
- Multi-hop questions ("find all functions that implement requirement X AND access register Y")
- Task types that need broad context: test generation, architecture review, full traceability
- When `build_context` with a single search returns insufficient results

```python
# Preview what sub-queries would be generated
plan = call_tool("rlm_plan_preview", {
    "query": "Generate tests for Adc_StartGroupConversion covering all "
             "dependencies, register patterns, and MISRA compliance",
    "task_type": "test_generation",
})
print(plan["data"]["sub_queries"])  # See planned decomposition before running

# Execute the full RLM context assembly
rlm_result = call_tool("rlm_orchestrate", {
    "query": "Generate tests for Adc_StartGroupConversion...",
    "task_type": "test_generation",
    "session_id": SESSION_ID,
    "profile": "mcal",
    "module": "Adc",
})
# Use rlm_result["data"]["context"] as input to your LLM
```

**Supported task_types**: `generic`, `initialization`, `debugging`, `traceability`, `dependency`

---

### 3.4 User Document Workflow (Sandbox)

Use the sandbox when your DA needs to process user-provided documents (specs, PDFs, requirements drafts) that aren't in the production knowledge base.

```python
SESSION_ID = "CIA_20260502_002"

# 1. Start session
call_tool("session_start", {"session_id": SESSION_ID, "assistant_name": "CIA"})

# 2. Upload user documents
call_tool("sandbox_upload", {
    "session_id": SESSION_ID,
    "file_path": "/path/to/customer_adc_spec.pdf",
})

# 3. Check what was loaded
status = call_tool("sandbox_status", {"session_id": SESSION_ID})
# Returns: files loaded, node count, storage bytes

# 4. Search within the sandbox (via search_database with session routing)
results = call_tool("search_database", {
    "query": "ADC timing requirements",
    "session_id": SESSION_ID,  # Routes search through sandbox
    "workspace_id": "illd",
})

# 5. Compare sandbox contents against production knowledge
diff = call_tool("sandbox_diff", {"session_id": SESSION_ID})
# Returns: nodes added, nodes modified (original vs current), edges added

# 6. Clean up explicitly (or session TTL will handle it)
call_tool("sandbox_clear", {"session_id": SESSION_ID})
call_tool("session_end", {"session_id": SESSION_ID})
```

---

## 4. Tool Selection Guide

| Question | Use This Tool |
|----------|--------------|
| "Find information about X" | `search_database` |
| "What does function X do?" | `query_api_function` |
| "What is type/struct X?" | `get_type_definition` |
| "Generate init code for type X" | `generate_initialization_code` |
| "What does X call / depend on?" | `query_dependencies` |
| "Is this call sequence valid?" | `validate_api_usage` |
| "Does X need polling after call?" | `detect_polling_requirements` |
| "Trace requirement X through V-Model" | `find_requirement_traces` |
| "What test coverage gaps exist in module X?" | `find_coverage_gaps` |
| "What registers does module X access?" | `analyze_hw_sw_links` |
| "Get hardware interface (SFRs, vars) for function X" | `get_function_hsi` |
| "Complex multi-hop query" | `rlm_orchestrate` (or `rlm_plan_preview` first) |
| "Process user-provided documents" | `sandbox_upload` + `search_database(session_id=...)` |
| "Evaluate DA output quality" | `evaluate_confidence` |
| "Record human approval/rejection" | `submit_human_feedback` |
| "Check system health" | `health_check` |
| "List available modules" | `list_available_modules` |
| "Understand query complexity before searching" | `query_enhance` (developer tier) |

### Search Alpha Decision Tree

```
Is your query structural?
  YES → alpha = 0.1 (graph-heavy)
  NO  → Is your query conceptual/semantic?
           YES → alpha = 0.8 (vector-heavy)
           NO  → Use alpha = 0.5 (balanced, default)
```

Or use `query_enhance` to get an auto-suggested alpha.

---

## 5. Working with Workspaces

### illd Workspace (Reference Drivers)

Use `workspace_id="illd"` for:
- Looking up iLLD API functions (`IfxAdc_*`, `IfxCan_*`, etc.)
- Hardware register analysis
- Dependency chains and initialization sequences
- Code pattern generation from reference implementations

```python
# Example: Get ADC function details
call_tool("query_api_function", {
    "function_name": "IfxAdc_initModule",
    "workspace_id": "illd",
})
```

### mcal Workspace (AUTOSAR MCAL)

Use `workspace_id="mcal"` for:
- AUTOSAR requirement traceability (`SHRQ-*`, `PRQXX-*`)
- MISRA compliance checking
- Test coverage analysis
- Jama-sourced requirements

```python
# Example: Trace a requirement
call_tool("find_requirement_traces", {
    "requirement_id": "SHRQ-12345",
    "workspace_id": "mcal",
})
```

### Module Filtering

Always pass `module_filter` when you know which module you're working with — it dramatically reduces noise and improves performance:

```python
call_tool("search_database", {
    "query": "initialization sequence",
    "workspace_id": "illd",
    "module_filter": "Adc",  # Scope to Adc module only
    "top_k": 10,
})
```

Run `list_available_modules(workspace_id="illd")` to get valid module names.

---

## 6. Confidence Scoring & Review Gate

Every DA response should go through confidence evaluation to determine whether automatic approval or human review is needed.

### How It Works

```
evaluate_confidence(signals, response_id, session_id)
    │
    ▼
Deterministic scoring (formula-based, NOT LLM-based):
  • context_coverage_ratio (+20 pts max)
  • has_proven_patterns (+15 pts if similar approved patterns exist in KG)
  • traceability_linked (+10 pts)
  • source_freshness (+10 pts)
  • issues_found (-5 pts per issue)
  • ... (more signals)
    │
    ▼
Score → routing:
  0–59:   FULL review   (human must review before use)
  60–79:  QUICK review  (15-minute spot check)
  80–100: AUTO          (automatically approved, logged)
```

### Submitting Feedback

Always call `submit_human_feedback` after a review decision — it feeds the learning loop:

```python
# Approval (stores as ApprovedPattern in Neo4j + Qdrant for future pattern matching)
call_tool("submit_human_feedback", {
    "response_id": "resp-001",
    "decision": "APPROVE",
    "module": "Adc",
    "task_type": "code_generation",
    "response_context": generated_code,  # The actual output to learn from
})

# Rejection (records failure pattern in PostgreSQL)
call_tool("submit_human_feedback", {
    "response_id": "resp-002",
    "decision": "REJECT",
    "issues_found": 3,
    "correction_notes": "Missing NULL pointer check on Adc_Init parameter",
    "module": "Adc",
    "task_type": "code_generation",
})
```

**Why this matters**: Approved patterns improve future `has_proven_patterns` confidence signals. After enough approvals on a pattern, similar queries will automatically score higher and route to AUTO.

---

## 7. DA-Specific Patterns

### CIA (Code Implementation Assistant)

```python
# Recommended flow
session_start → search_database(alpha=0.3) → query_api_function →
  query_dependencies → validate_api_usage → build_context →
  [LLM generates code] → evaluate_confidence → submit_human_feedback
```

Key tools: `query_api_function`, `get_type_definition`, `generate_initialization_code`, `query_dependencies`, `validate_api_usage`, `detect_polling_requirements`

### GEST (Test Generator)

```python
# Recommended flow for complex tests
session_start → rlm_plan_preview(task_type="test_generation") →
  rlm_orchestrate → find_coverage_gaps → get_function_hsi →
  [LLM generates tests] → evaluate_confidence → process_results (after CI)
```

Key tools: `rlm_orchestrate`, `find_coverage_gaps`, `get_function_hsi`, `process_results`

### ACRA (Code Reviewer)

```python
# Recommended flow
session_start → search_database(alpha=0.2, workspace_id="mcal") →
  validate_api_usage → find_requirement_traces → analyze_hw_sw_links →
  [LLM reviews code] → evaluate_confidence → submit_human_feedback
```

Key tools: `validate_api_usage`, `analyze_hw_sw_links`, `get_function_hsi`

### REVA (Requirements Reviewer)

```python
# Recommended flow
session_start → sandbox_upload(requirements_draft) →
  search_database(workspace_id="mcal") → find_coverage_gaps →
  build_traceability_matrix → [LLM reviews requirements]
```

Key tools: `sandbox_upload`, `find_requirement_traces`, `find_coverage_gaps`, `build_traceability_matrix`

### ATRA (Architecture Tracer)

```python
# Recommended flow
session_start → search_database(alpha=0.2) → shortest_path →
  find_requirement_traces → build_traceability_matrix →
  [LLM performs trace analysis]
```

Key tools: `find_requirement_traces`, `build_traceability_matrix`, `find_coverage_gaps`, `get_neighbors`, `shortest_path`

---

## 8. Performance Tips

### Cache the First Hit

AICE has a 2-tier cache (LRU exact match → SemanticCache cosine similarity). The first call for a query pays the full retrieval cost; subsequent similar queries are fast. Structure your DA to:
1. Call `search_database` once per context-assembly session, not per LLM turn.
2. Use `session_store` / `session_retrieve` to persist search results within a session.

### Reduce `top_k` for Targeted Queries

Default `top_k=10` is appropriate for broad exploration. For targeted lookups (e.g., "find the exact signature of Adc_Init"), use `top_k=3` or switch to `query_api_function` for zero ambiguity.

### Use `module_filter` Always

Graph + vector search across the entire workspace is slower than scoped search. Passing `module_filter="Adc"` limits both Neo4j and Qdrant queries to that module's nodes.

### Use `query_enhance` Before Complex Searches

For queries whose ideal `alpha` isn't clear, `query_enhance` analyzes the query in < 1 ms (zero LLM cost) and returns `suggested_alpha`, `strategy`, `detected_modules`, and `token_budget_hint`:

```python
hint = call_tool("query_enhance", {"query": your_query})
results = call_tool("search_database", {
    "query": your_query,
    "alpha": hint["data"]["suggested_alpha"],
    "top_k": hint["data"]["suggested_max_results"],
    "module_filter": hint["data"]["detected_modules"][0] if hint["data"]["detected_modules"] else None,
})
```

### Warm Up with `health_check`

On DA startup, call `health_check(verbose=True)` to verify all backends (Neo4j, Qdrant, Redis, PostgreSQL) are available. This surfaces configuration issues early.

---

## 9. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `PERMISSION_DENIED` on tool call | API key tier insufficient | Verify your key; some tools are `developer` or `admin` only |
| `No API key provided` | Missing `Authorization` header | Add `Authorization: Bearer <key>` to every request |
| Empty search results | Wrong workspace or module filter | Check `workspace_id`; call `list_available_modules` to verify module names |
| `BACKEND_UNAVAILABLE` for Neo4j | Neo4j not connected | Contact platform team |
| Session expired | TTL (3600s) exceeded | Start a new session with a fresh `session_id` |
| `sandbox_query` unknown tool | Tool deprecated | Use `search_database(session_id=<id>)` instead |
| `ingest_file` unknown tool | Removed from MCP | Ingestion is a platform operation; contact platform team |
| Slow responses | Large result set or network | Reduce `top_k`, add `module_filter`, check VPN routing |
| Low confidence scores consistently | DA output consistently incomplete | Review `get_failure_patterns()` to identify recurring issues |

### Diagnostic Sequence

```python
# 1. Check all backends
health = call_tool("health_check", {"verbose": True})

# 2. Verify modules exist
modules = call_tool("list_available_modules", {"workspace_id": "illd"})

# 3. Verify search is working
test = call_tool("search_database", {
    "query": "test query",
    "workspace_id": "illd",
    "top_k": 1,
})

# 4. Check cache performance
stats = call_tool("cache_stats", {})
```

---

## 10. Tool Quick Reference

### By Category

| # | Category | Tools | Tier |
|---|----------|-------|------|
| 1 | Search & Query | `search_database`, `search_nodes`, `get_node_by_id`, `get_neighbors`, `shortest_path`, `execute_cypher` | public / developer |
| 2 | API Intelligence | `query_api_function`, `get_type_definition`, `generate_initialization_code` | public |
| 3 | Dependency Analysis | `query_dependencies`, `validate_api_usage`, `detect_polling_requirements` | public |
| 4 | Traceability | `find_requirement_traces`, `build_traceability_matrix`, `find_coverage_gaps`, `analyze_hw_sw_links` | public |
| 5b | HSI | `get_function_hsi` | public |
| 6 | Memory / Sessions | `session_start`, `session_store`, `session_retrieve`, `build_context`, `session_end` | public |
| 6+ | Sandbox | `sandbox_upload`, `sandbox_status`, `sandbox_clear`, `sandbox_diff` | public |
| 6+ | RLM | `rlm_orchestrate`, `rlm_plan_preview` | public |
| 7 | Cache | `cache_get`, `cache_stats` | developer |
| 7 | Cache (admin) | `cache_invalidate_module`, `cache_clear`, `cache_refresh_config` | admin |
| 8 | Feedback | `submit_human_feedback`, `get_learning_metrics`, `get_failure_patterns` | public / developer |
| 8 | Feedback (admin) | `process_results` | admin |
| 9 | Review Gate | `evaluate_confidence`, `complete_review` | public |
| 9 | Review Gate | `override_review_routing`, `get_review_analytics` | developer |
| 10 | Ontology | `list_ontology_profiles`, `get_ontology_schema`, `validate_entity`, `get_ontology_compliance` | public / developer |
| 11 | Observability | `health_check`, `get_graph_statistics`, `list_available_modules`, `get_distribution`, `get_coverage_report`, `detect_communities` | public / developer |
| 12 | Visualization | `visualize_subgraph` | developer |
| 13 | Authentication | `get_token_info`, `ensure_valid_token` | developer / admin |
| 14 | GAP v2 | `query_enhance` | developer |

### Response Format

All tools return JSON strings in this format:

```json
// Success
{"error": false, "data": { ... }}

// Error
{"error": true, "error_code": "PERMISSION_DENIED", "message": "..."}
```

Common error codes: `PERMISSION_DENIED`, `INVALID_INPUT`, `BACKEND_UNAVAILABLE`, `INTERNAL_ERROR`, `BACKEND_ERROR`

---

*For the complete tool parameter reference see [DOCUMENTATION.md](DOCUMENTATION.md). For system architecture see [architecture/OVERVIEW.md](architecture/OVERVIEW.md).*

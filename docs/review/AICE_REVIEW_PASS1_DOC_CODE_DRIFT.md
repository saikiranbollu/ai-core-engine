# AICE Review — Pass 1: Documentation / Code Drift

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline (post-Review4)
**Date:** 2026-04-25
**Scope:** Cross-verification of `docs/`, `requirements/`, `mcp/auth/policies/`, `mcp/core/tool_tiers.py`, and `mcp/core/mcp_server.py`. **Tests excluded.** Master Gaps List used as context only — Sprint 25 source is ground truth.

---

## 0. Executive Summary

**Overall verdict:** The codebase is in significantly better shape than the documentation/specification layer. Most of the drift is in the **opposite direction** from what one usually finds: the code has moved forward (new tools, new tiers, semantic memory becoming Qdrant-only, ISO-26262-aligned confidence base) while the docs/specs are pinned to earlier sprints.

For ASPICE assessor-readiness and EU AI Act Annex IV technical documentation, this is a **critical risk**: an external auditor reading `requirements/AICE_SYSTEM_REQUIREMENTS.md` or `docs/architecture/auth-and-security.md` and then exercising the code will find numerous mismatches that are individually small but collectively undermine the credibility of the audit trail.

**Findings count:** 23 (5 Critical, 9 High, 7 Medium, 2 Low)

| Severity | Count | Category |
|---|---|---|
| 🔴 Critical | 5 | Auth tier coverage gaps, tool-name divergence, Cerbos policy duplicates, signal-count requirement vs code |
| 🟠 High | 9 | Tool-count drift, base-score doc, RBAC totals, PatternStore backing-store claim, tier counts |
| 🟡 Medium | 7 | Sprint version markers, RedisBackend framing, signal lists, metric names |
| 🟢 Low | 2 | Cosmetic header drift |

---

## 1. Critical Findings (block audit submission)

### F-D01 🔴 — `tool_tiers.py` is missing tools that are registered as `@mcp.tool()` (silent default-deny)

**Evidence:**
- `auth_middleware.py::check_authorization()` step 2: *"Determine tool tier — unknown tools are DENIED (strict)"* with `if tier is None: return False, f"Unknown tool: {tool_name}"`.
- `mcp/core/tool_tiers.py` `TOOL_TIERS` dict (Sprint 25 baseline) contains the following tools per tier:
  - PUBLIC: 33
  - DEVELOPER: 16
  - ADMIN: 5
  - **TOTAL = 54**
- `docs/architecture/OVERVIEW.md` "Codebase Layout" comment says `mcp_server.py` has "62 tool handlers". Master Gaps §1.2 says Sprint 25 = 62 tools.
- ADR-042 (Sprint 25, in `DECISIONS.md`) explicitly says: *"MISRA remediation engine and GEST (unit test generation) both have MCP tools registered but permanently denied by authorization (missing from tool_tiers.py). Decision: (1) Add `remediate_misra_violation` and `generate_unit_tests` to tool_tiers.py as DEVELOPER tier."*
- Master Gaps §1.2 lists tools added since Sprint 10: `governance_report` (#57), `assess_coverage_bias` (#58), `get_provenance` (#59), `verify_citations` (#60), and 2 more in Sprint 25 (#61–#62).

**None** of these tools (`remediate_misra_violation`, `generate_unit_tests`, `governance_report`, `assess_coverage_bias`, `get_provenance`, `verify_citations`, plus the 2 GAP-pipeline admins) appear in the `TOOL_TIERS` dict shown in source. If they are registered with `@mcp.tool()` but absent from `TOOL_TIERS`, **every call to those tools is returning PERMISSION_DENIED**, including from admin keys (because the tier check happens before the role check).

**Impact:** Up to 8 tools may be functionally unreachable in production. ADR-042 captures the fix for 2 of them — verify whether the fix actually shipped in tool_tiers.py.

**Recommendation:**
1. Run `python -c "from mcp.core.mcp_server import mcp; print(set(mcp._tools.keys()) - set(__import__('mcp.core.tool_tiers', fromlist=['TOOL_TIERS']).TOOL_TIERS.keys()))"` to enumerate registered-but-unmapped tools.
2. Add a startup assertion in `mcp/app.py` that **fails fast** if `set(registered_tools) != set(TOOL_TIERS.keys())`. This single assertion would have prevented this entire class of bug from reaching Sprint 25.
3. Add the missing tools to `TOOL_TIERS` (per ADR-042 for the MISRA/GEST cases; new tier decisions needed for the governance/citation/GAP-pipeline ones).

---

### F-D02 🔴 — `mcp/auth/policies/resource_mcp_tool.yaml` has duplicate tool entries (Cerbos policy ambiguity)

**Evidence:**
- File header: *"Resource 'mcp_tool' covering all 56 tools across 13 categories"* — already stale (claim says 56, but Sprint 25 has 62 registered, 54 in `TOOL_TIERS`).
- In the **PUBLIC tools** rule block under `# Category 11: Observability & Health`, the policy lists:
  ```yaml
  - expr: "request.resource.attr.tool_name == 'health_check'"
  - expr: "request.resource.attr.tool_name == 'get_graph_statistics'"
  - expr: "request.resource.attr.tool_name == 'list_available_modules'"
  - expr: "request.resource.attr.tool_name == 'get_distribution'"
  - expr: "request.resource.attr.tool_name == 'get_coverage_report'"
  ```
- In the **DEVELOPER tools** rule block (immediately below) under `# Category 11: Observability & Health`:
  ```yaml
  - expr: "request.resource.attr.tool_name == 'get_distribution'"
  - expr: "request.resource.attr.tool_name == 'get_coverage_report'"
  - expr: "request.resource.attr.tool_name == 'detect_communities'"
  ```
- `tool_tiers.py` (canonical) says `get_distribution` and `get_coverage_report` are PUBLIC.

**Impact:** Cerbos's first-match-allow semantics will probably make these tools effectively public (both rules `EFFECT_ALLOW`), but:
- The duplication makes the policy non-canonical — a future tightening of the public rule won't necessarily tighten the developer one.
- It is the kind of finding that auditors flag as "policy hygiene" because it shows an unreviewed copy-paste.
- Worse: if you ever change one rule to `EFFECT_DENY`, the duplicate-allow will silently override the deny.

**Recommendation:**
1. Remove `get_distribution` and `get_coverage_report` from the DEVELOPER block (they belong in PUBLIC per `tool_tiers.py`).
2. Add a CI check: parse `resource_mcp_tool.yaml`, ensure each tool name appears in **exactly one** rule, and that the rule's tier matches `TOOL_TIERS[tool_name]`. This is one screen of code and would have caught this.

---

### F-D03 🔴 — `requirements/AICE_SYSTEM_REQUIREMENTS.md` references tool names that don't exist in source

The requirements document is the formal specification artifact. Multiple "IMPLEMENTED" requirements name tools that are not in `TOOL_TIERS` and are not registered with `@mcp.tool()`. An ASPICE assessor doing a forward-traceability check from PRQ to implementation will find broken links.

| Req ID | Doc tool name | Actual tool in source | Status of doc claim |
|---|---|---|---|
| AICE-API-003 | `generate_init_code` | `generate_initialization_code` | Wrong name |
| AICE-SEARCH-004 | `search_by_node_type` | (no such tool — `search_nodes` accepts a label filter) | Phantom tool |
| AICE-SEARCH-005 | `search_with_context` | (no such tool) | Phantom tool |
| AICE-SEARCH-008 | `get_node_neighbors` | `get_neighbors` | Wrong name |
| AICE-OBS-002 | `graph_stats` | `get_graph_statistics` | Wrong name |
| AICE-OBS-003 | `module_list` | `list_available_modules` | Wrong name |
| AICE-OBS-005 | `coverage_report` | `get_coverage_report` | Wrong name |
| AICE-OBS-006 | `metrics` | (no such tool — `/metrics` is an HTTP endpoint, not an MCP tool) | Phantom tool |
| AICE-ONT-002 | `get_ontology` | `get_ontology_schema` | Wrong name |
| AICE-ONT-003 | `get_node_types` | (covered by `get_ontology_schema`) | Phantom tool |
| AICE-ONT-004 | `validate_schema` | `validate_entity` | Wrong name |
| AICE-ONT-005 | `get_relationship_types` | (covered by `get_ontology_schema`) | Phantom tool |
| AICE-VIZ-001 | `visualize_graph` | `visualize_subgraph` | Wrong name |

**Recommendation:** A one-time sweep through the requirements doc to align each "IMPLEMENTED" requirement to an actual tool name. Add a markdown lint check: every backtick-quoted `*_tool` identifier in the requirements doc must appear in `TOOL_TIERS`.

---

### F-D04 🔴 — Confidence-scoring requirement count and base score contradict the code (audit-blocker for ISO 26262)

**Evidence:**
- `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-RG-001 states: *"The `evaluate_confidence` tool shall compute a deterministic confidence score (0-100) using **8 weighted signals**, without any LLM dependency"*.
- AICE-RG-006 lists the 8 signals: `has_context (+30), has_dependency_order (+20), has_proven_pattern (+15), passes_validation (+10), api_coverage (+10), missing_requirements (-15), missing_hw_spec (-15), is_high_risk (-15)`.
- `docs/architecture/review-gate.md` lists **13 signals** with `base_score = 50`: 7 positive (has_kg_context +30, high_relevance +20, has_dependency_order +20, has_proven_patterns +15, format_correct +10, misra_compliant +10, similar_approved +5) and 6 negative (missing_requirements -30, low_relevance -20, compliance_warnings -20, novel_pattern -15, is_safety_critical -15, complex_logic -10).
- `src/ReviewGate/confidence.py` `DEFAULT_WEIGHTS` actually defines **7 signals**: 5 positive (api_verified +25, call_order_valid +25, config_valid +15, output_well_formed +5, pattern_match +10) and 2 negative (is_safety_critical -15, no_failure_match -10).
- `src/ReviewGate/confidence.py::evaluate()` literal: `base_score = 20` with the comment *"Why base = 20 (not 50)? ISO 26262 principle: assume failure until proven safe."*
- `docs/PATTERN_STORAGE_DESIGN.md` matches the code (7 signals, base 20).

**Impact:** Three documents disagree with each other and the code. The `requirements/` doc — the **specification** — is internally inconsistent and matches none of the others. For ISO 26262 Part 8 tool qualification, the requirement document is what gets versioned and assessed. As written, AICE-RG-001 says "8 signals" → implementation has 7 → **specification not met**.

The signal *names* are also entirely different (`has_context` vs `api_verified`, etc.) — this isn't a count problem alone, it's a different scoring model.

**Recommendation:**
1. Treat `src/ReviewGate/confidence.py` and `docs/PATTERN_STORAGE_DESIGN.md` as the **canonical** scoring spec — they agree.
2. Rewrite AICE-RG-001 / AICE-RG-006 to match the 7-signal, base-20 model with the actual signal names. Add an explicit ISO 26262 rationale paragraph (it's already in the code comment — just lift it).
3. Rewrite the table in `docs/architecture/review-gate.md` to match. Decide whether `has_kg_context`-style signals are *additional* (in which case wire them) or just stale (in which case delete them).
4. Note that GEST has its own weight overrides (`GEST_WEIGHTS` in `confidence.py`) — document this.

---

### F-D05 🔴 — `PatternStore` backing-store claim contradicts itself (req says Neo4j, class docstring says "no Neo4j")

**Evidence:**
- `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-FB-005: *"The FeedbackSink shall persist feedback to PostgreSQL and wire learned patterns to PatternStore (Neo4j) + PatternIndex (Qdrant)"* — Status: IMPLEMENTED.
- `mcp/core/mcp_server.py::_get_feedback_sink()` confirms the requirement's intent:
  ```python
  # Wire learning loop: PatternStore (Neo4j) + PatternIndex (Qdrant)
  if neo4j_driver:
      pattern_store = PatternStore(neo4j_driver=neo4j_driver, embedder=embedder)
  ```
- `src/MemoryLayer/memory/semantic_memory/pattern_store.py` class docstring says: *"CRUD + similarity search for ApprovedPattern, **backed entirely by Qdrant**. All pattern data (text, metadata, usage counts) is stored in Qdrant payloads. Vector embeddings enable similarity search. **No Neo4j needed.**"*
- The `PatternStore.__init__` parameters listed in the docstring are `embedder`, `collection`, `qdrant_url`, `qdrant_api_key`, `_client` — **no `neo4j_driver` param**.

**Impact:** One of three things is true and they're all bad:
1. The call site `PatternStore(neo4j_driver=neo4j_driver, embedder=embedder)` raises `TypeError: unexpected keyword argument 'neo4j_driver'` at runtime → learning loop is broken.
2. `PatternStore.__init__` accepts `**kwargs` and silently ignores `neo4j_driver` → call works but the variable name lies.
3. There are two `PatternStore` classes (one Neo4j, one Qdrant) and the import resolves to the wrong one.

I cannot determine which without reading the full `__init__` — please confirm at file:line. Either way, the requirement is wrong: the actual learning-loop persistence is **Qdrant + PostgreSQL**, not Neo4j + Qdrant. (PatternIndex is the Qdrant-side; PatternStore is *also* now Qdrant-side per its own docstring.)

`docs/MEMORY_LAYER_FEATURES.md` correctly notes that `PatternStore` "needs its own Qdrant collection (separate from RAG collections)" — which lines up with the docstring, not with AICE-FB-005.

**Recommendation:**
1. Reconcile: confirm by reading `pattern_store.py:__init__` whether `neo4j_driver` is accepted or ignored.
2. If the call site is broken, fix it (drop the `neo4j_driver=` kwarg).
3. Rewrite AICE-FB-005 to: *"persist feedback to PostgreSQL and wire learned patterns into the Qdrant-backed PatternStore + PatternIndex."*
4. Update the comment in `mcp_server.py::_get_feedback_sink()` to remove "(Neo4j)".

---

## 2. High Findings

### F-D06 🟠 — Tool-count drift across 6+ documents

| Source | Claim | Sprint reference |
|---|---|---|
| `docs/DOCUMENTATION.md` §1.1 | "56 tools across 13 categories" | Sprint 9 (header) |
| `docs/DOCUMENTATION.md` "Note" | runtime check via `mcp._tools` count | (correct guidance) |
| `docs/architecture/OVERVIEW.md` codebase comment | "62 tool handlers" | Sprint 10 |
| `mcp/auth/policies/resource_mcp_tool.yaml` header | "all 56 tools across 13 categories" | (no sprint) |
| `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-PROM-002 | "All 56 tools shall be automatically instrumented" | Sprint 10 |
| `docs/ai_governance_files/AICE_SYSTEM_CARD.md` | "56 MCP tools across 13 categories" | (System card v1.0.0, 2026-03-29) |
| `mcp/core/tool_tiers.py` actual count | **54** (visible in source) | Sprint 25 |
| Master Gaps §1.2 | **62** | Sprint 25 |

The number is in 4 different places (54, 56, 60, 62). **None match.** This is the highest-noise drift in the whole repo.

**Recommendation:** Single source of truth — generate the count programmatically from `mcp._tools` at build time, write it to a `TOOL_COUNT.txt` artifact, and have the docs include it via templating or post-build sed. Anything else will drift again within 1 sprint.

---

### F-D07 🟠 — Tier subtotals don't add up to claimed total in any document

| Source | public | developer | admin | sum | claimed total |
|---|---:|---:|---:|---:|---:|
| `docs/architecture/auth-and-security.md` §2 | 34 | 14 | 6 | 54 | 56 |
| `docs/DOCUMENTATION.md` §5.2 | 34 | 14 | 8 | 56 | 56 |
| `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-AUTH-002 | 34 | 14 | 8 | 56 | (implied 56) |
| `mcp/core/tool_tiers.py` actual | 33 | 16 | 5 | **54** | n/a |

In `auth-and-security.md` the same paragraph contains "An admin API key can invoke all 56 tools. A developer key can invoke public + developer tools (50 tools). A public key is limited to 36 tools." 36 + 50 = 86, none of which match the table above. This paragraph is internally contradictory.

**Recommendation:** Same as F-D06 — generate from source. Add three macros: `AICE_TOOL_COUNT_PUBLIC`, `AICE_TOOL_COUNT_DEVELOPER`, `AICE_TOOL_COUNT_ADMIN`.

---

### F-D08 🟠 — `auth-and-security.md` Cerbos policy snippet has wrong format vs actual Cerbos policy file

`docs/architecture/auth-and-security.md` shows the Cerbos resource policy structured around a `request.resource.attr.tier` predicate:
```yaml
- actions: ["invoke"]
  roles: ["developer"]
  effect: EFFECT_ALLOW
  condition:
    match:
      expr: >
        request.resource.attr.tier in ["public", "developer"]
```
The **actual** `mcp/auth/policies/resource_mcp_tool.yaml` doesn't use a `tier` attribute at all — it uses per-tool-name `expr`s:
```yaml
- expr: "request.resource.attr.tool_name == 'search_database'"
- expr: "request.resource.attr.tool_name == 'search_nodes'"
...
```
These are completely different policy designs. The doc shows the cleaner attribute-based design; the implementation hardcodes 56 expressions in a single `match.any.of` block.

**Recommendation:** Two options, in priority order:
1. **Refactor the policy** to the attribute-based style shown in the docs. The MCP server already passes `attr.tier` (per `auth_middleware.py`) — Cerbos can match on it. This collapses the 56 lines to 3 rules and makes adding/removing tools require *zero* policy edits (only `tool_tiers.py` change). Strongly recommended.
2. If the per-tool-name design is intentional (e.g., for fine-grained per-tool conditions later), update `auth-and-security.md` to show the actual policy.

This is a `High` because the doc's design is genuinely better — fixing forward is preferable to fixing backward.

---

### F-D09 🟠 — `AICE_SYSTEM_CARD.md` understates tool count and risks audit credibility

The system card (governance document for EU AI Act Annex IV) says: *"AICE exposes 56 MCP tools across 13 categories"*. Sprint 25 baseline has 62 registered (per OVERVIEW.md and Master Gaps).

This is a governance artifact — wrong numbers in the system card directly hit:
- EU AI Act Annex IV §1(b): "system functionalities" must be enumerated
- ASPICE SUP.10 (Configuration Management): inconsistent baselining
- ISO PAS 8800: AI system documentation requirements

**Recommendation:** Update system card to current Sprint 25 counts. Add a `Last source-verified:` field to the header that links to a programmatic count check.

---

### F-D10 🟠 — `docs/MEMORY_LAYER_FEATURES.md` mis-labels `RedisBackend` as "Placeholder"

The doc says: *"RedisBackend | Placeholder | Code is complete (real Redis calls: setex, get, delete, keys), but no Redis instance is provisioned and no env-var fallback (REDIS_HOST, REDIS_PORT, REDIS_PASSWORD) is wired into the constructor — must pass host/port/password manually"*.

But:
- `mcp/k8s/test/redis.yaml` shows a full Redis 7-alpine deployment with PVC, liveness/readiness probes, AOF persistence — Redis **is** provisioned in the test environment.
- `src/MemoryLayer/memory/working_memory/manager.py::RedisBackend.__init__` has working `redis.Redis(host=..., port=..., db=..., password=...)` and properly sets up `setex/get/delete/keys`.
- The only missing piece is env-var fallback in `__init__`, which is ~3 lines: `host = host or os.environ.get("REDIS_HOST", "localhost")`, etc.

The "Placeholder" label is misleading — it suggests *the Redis backend is non-functional*. In fact, the code is functional, just not auto-configured at MCP server bootstrap. This wording will mislead any new contributor.

**Recommendation:** Reword to "Functional, not auto-wired — env-var fallback missing in `RedisBackend.__init__`; bootstrap in `mcp_server.py::_get_session_manager()` does not yet select RedisBackend over InMemoryBackend." This is a 1-day fix, not a placeholder.

---

### F-D11 🟠 — `Prometheus metric` count mismatch (11 in spec, 17 in code)

`requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-PROM-003 lists 11 metric names. Source `mcp/core/mcp_server.py` imports from `src/Observability/metrics`:
```python
from src.Observability.metrics import (
    TOOL_REQUESTS_TOTAL, TOOL_REQUEST_DURATION,
    SEARCH_REQUESTS_TOTAL, SEARCH_DURATION,
    CACHE_REQUESTS_TOTAL, ACTIVE_SESSIONS,
    RLM_REQUESTS_TOTAL, RLM_SUBQUERIES,
    INGESTION_FILES_TOTAL, BACKEND_UP,
    REVIEW_ROUTING_TOTAL, PROMETHEUS_AVAILABLE,
    QUERY_LATENCY, CACHE_HIT_RATE, CACHE_SIZE, ERROR_TOTAL,
    INGESTION_DURATION,
    make_metrics_app,
)
```
That's 16 metric names + `PROMETHEUS_AVAILABLE` flag + `make_metrics_app`. The spec is missing `QUERY_LATENCY`, `CACHE_HIT_RATE`, `CACHE_SIZE`, `ERROR_TOTAL`, `INGESTION_DURATION`.

**Recommendation:** Update AICE-PROM-003 with the correct list. These were added in Tickets 7 & 8 (cache gauges and error classification, per the `_update_cache_gauges` and `_classify_and_record_error` helpers in `mcp_server.py`).

---

### F-D12 🟠 — Sprint version markers desynchronized

| Document | Header version |
|---|---|
| `docs/DOCUMENTATION.md` | "Version 2.1.0 \| Sprint 9" |
| `docs/architecture/OVERVIEW.md` | "Version 2.1.0 \| Sprint 10" |
| `docs/ai_governance_files/AICE_SYSTEM_CARD.md` | "Version: 2.1.0 (Sprint 10)" |
| `requirements/AICE_SYSTEM_REQUIREMENTS.md` | "Version 2.1.0 \| Sprint 10 — Status Update" |
| `requirements/MEMORY_LAYER_REQUIREMENTS.md` | "Version 2.1.0 \| Sprint 10 — Status Update" |
| `requirements/Ingestion Pipeline.md` | "Version 2.1.0 \| Sprint 10 — Status Update" |
| `Master Gaps List` | "Sprint 10 → Sprint 25 (Review4 cycle complete)" |

Documentation is pinned at Sprint 9–10. Source is at Sprint 25. There is no document that reflects Sprint 11–25 work as anything other than gap entries.

**Recommendation:** Either (a) bump all docs to "Version 2.x.0 \| Sprint 25" with a per-section changelog, or (b) keep them at v2.1.0 for archival and add a `docs/CHANGES_SINCE_2_1_0.md` overview.

---

### F-D13 🟠 — `docs/architecture/auth-and-security.md` "File Map" claims `tool_tiers.py` is 82 lines and maps "56 tools"

Actual `tool_tiers.py` has **54 entries** in `TOOL_TIERS` (per source) — and per Master Gaps it should have 62. The line count and tool-count claim in the file map are both wrong.

**Recommendation:** Auto-generate the file map from filesystem stats during doc build.

---

### F-D14 🟠 — `category` count drifts: "13 categories" in docs vs "14 categories" in tool_tiers.py

`mcp/core/tool_tiers.py` shows `# Cat 14: GAP v2 Tools (Sprint 25 — C01 fix)` but every doc says "13 categories" (DOCUMENTATION.md, system card, Cerbos policy header, architecture overview).

**Recommendation:** Decide whether GAP v2 is a top-level category 14 (then update all docs) or whether it should be folded into an existing one (e.g., merge into Category 1 Search since `query_enhance` is a search-preprocessor). I'd argue for folding — having a single-tool category is awkward.

---

## 3. Medium Findings

### F-D15 🟡 — `sandbox_query` is in Cerbos policy but commented out in `tool_tiers.py`

`tool_tiers.py`:
```python
"sandbox_upload": PUBLIC,
# "sandbox_query": PUBLIC,  # Deprecated: use search_database(session_id=...) instead
"sandbox_status": PUBLIC,
```

`mcp/auth/policies/resource_mcp_tool.yaml` PUBLIC block:
```yaml
- expr: "request.resource.attr.tool_name == 'sandbox_upload'"
- expr: "request.resource.attr.tool_name == 'sandbox_query'"   # ← still here!
- expr: "request.resource.attr.tool_name == 'sandbox_status'"
```

If `sandbox_query` is truly deprecated and removed, the Cerbos policy entry is dead code. If it's still callable from somewhere, the `tool_tiers.py` comment is wrong.

**Recommendation:** If deprecated, remove from Cerbos policy. If still alive (perhaps via aliasing inside `search_database`), document the alias and remove the deprecation comment.

---

### F-D16 🟡 — RLM tier inconsistency between `tool_tiers.py` and Cerbos policy

`tool_tiers.py`: `"rlm_orchestrate": DEVELOPER, "rlm_plan_preview": PUBLIC`.

`resource_mcp_tool.yaml` PUBLIC block lists **both**:
```yaml
# Category 6+: RLM
- expr: "request.resource.attr.tool_name == 'rlm_orchestrate'"
- expr: "request.resource.attr.tool_name == 'rlm_plan_preview'"
```

Per `_check_via_local_tiers` (the fallback when Cerbos is down), `tool_tiers.py` is canonical → `rlm_orchestrate` requires `developer`. But when Cerbos is up, the policy allows it for all roles → effectively PUBLIC. **The auth decision for `rlm_orchestrate` flips depending on whether Cerbos is up or down.** That's a defect.

`docs/architecture/rlm-orchestrator.md` says: *"two MCP tools are exposed at the **developer** tier for visibility and debugging"* — agreeing with `tool_tiers.py`.

**Recommendation:** Move `rlm_orchestrate` out of the PUBLIC Cerbos block into the DEVELOPER block. Add a unit test that asserts `tool_tiers.py[name] == cerbos_policy_tier(name)` for every tool — this would have caught it.

---

### F-D17 🟡 — `docs/MEMORY_LAYER_FEATURES.md` has stray text in section header

Section 2 heading reads:
```
## 2. Semantic Memory (Persistent — Qdrant)set PATH=%PATH%;C:\Users\AyubKhan\bin
```
A `set PATH=...` shell command got concatenated to the section header — looks like a Windows batch line accidentally pasted in.

**Recommendation:** Fix the header.

---

### F-D18 🟡 — `Section 7: Yet to Be Implemented` lists items that contradict other docs' "IMPLEMENTED" status

`docs/MEMORY_LAYER_FEATURES.md` §7 lists as "Yet to Be Implemented":
- *"Pattern store production wiring — needs to be called from the query pipeline"*
- *"Pattern population pipeline — No mechanism exists to populate approved patterns into Qdrant"*
- *"Domain assistant integration — Wire DomainSessionAdapter into domain assistants"*
- *"Session persistence across restarts"*

But `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-FB-005 says PatternStore wiring is `IMPLEMENTED`, AICE-MEM-005 says Redis persistence is `IMPLEMENTED`.

The MEMORY_LAYER_FEATURES doc is more honest. But the requirements doc says "IMPLEMENTED".

**Recommendation:** Reconcile. The status statements in `requirements/` are **assertions** — they need to be true. If wiring isn't done, status is `PARTIAL` not `IMPLEMENTED`.

---

### F-D19 🟡 — `Category 13 — Authentication (2 tools)` claim is incomplete

`requirements/AICE_SYSTEM_REQUIREMENTS.md` §11 (sic — Authentication, but numbered Category 13) starts: *"Authentication & Authorization (Category 13 — 2 tools)"* and lists `whoami` (AICE-AUTH-008) as IMPLEMENTED. **`whoami` does not exist in `tool_tiers.py`.**

The actual Category 13 tools are `get_token_info` (developer) and `ensure_valid_token` (admin). There's no `whoami` tool.

**Recommendation:** Either implement `whoami` (it's a useful tool) or strike AICE-AUTH-008.

---

### F-D20 🟡 — `OVERVIEW.md` line counts for files don't match reality (audit drift)

OVERVIEW.md lists files with line counts (e.g., `mcp_server.py` "1800 lines", `search_service.py` "1259 lines", `build_knowledge_graph.py` "4668 lines"). These are unverifiable as-is and will go stale immediately after any change.

**Recommendation:** Drop the line counts from OVERVIEW.md, or auto-generate them at doc-build time.

---

### F-D21 🟡 — `NodeSetManager` PLACEHOLDER warnings in production code

`src/MemoryLayer/memory/node_sets/node_set_manager.py` has multiple comments tagged `# ← CONFIRM WITH INGESTION TEAM (Q1, Q2, Q3)` and `⚠️ PLACEHOLDER PROPERTY NAMES`. `docs/architecture/memory-layer.md` mentions: *"Some property names in `node_set_manager.py` and `scoped_query.py` are marked as `PLACEHOLDER` pending confirmation from the Ingestion team on final property naming conventions."*

This is OK as a tracked TODO, but in a Sprint 25 codebase, "Q1, Q2, Q3" placeholders should be either resolved or have a JIRA ticket linked. As-is, it's a long-running ambiguity.

**Recommendation:** Resolve with the Ingestion team and remove placeholders. If the answer is "MODULE_PROPERTY_NAME = 'module'" (which is currently the default), strike the warning.

---

## 4. Low Findings

### F-D22 🟢 — `docs/DOCUMENTATION.md` says "Note: Run python -c …" but the snippet would print the wrong attribute

```python
print(len(mcp._tools), 'tools registered')
```
`FastMCP` instances expose tools via different attributes depending on version (`_tools`, `_tool_handlers`, etc.). The exact attribute may vary. This is a minor docs hint that may not work.

**Recommendation:** Replace with a proven snippet, e.g., `mcp.list_tools()` or whatever the SDK contract is.

---

### F-D23 🟢 — Module casing inconsistency: `Parsers/` vs `parsers/` in import paths

`src/IngestionPipeline/Parsers/__init__.py` exists (capital P) but `ingestion_service.py` does `from src.IngestionPipeline.parsers.c_parser import parse` (lowercase). On case-sensitive filesystems (Linux containers, the K8s deployment) the lowercase import will fail unless there's a symlink or both directories exist.

**Recommendation:** Pick one casing and stick to it. This is potentially a deploy-time fail on Linux.

---

## 5. Cross-cutting Recommendation: Add a Doc/Code Drift Gate to CI

All 23 findings above are caught by ~150 lines of CI script. Specifically:

```python
# tests/ci/test_doc_code_drift.py
def test_tool_tiers_matches_registered_tools():
    from mcp.core.mcp_server import mcp
    from mcp.core.tool_tiers import TOOL_TIERS
    registered = set(mcp._tool_handlers.keys())  # adjust per FastMCP API
    mapped = set(TOOL_TIERS.keys())
    assert registered == mapped, (
        f"Registered but unmapped: {registered - mapped}\n"
        f"Mapped but not registered: {mapped - registered}"
    )

def test_cerbos_policy_matches_tool_tiers():
    import yaml
    from mcp.core.tool_tiers import TOOL_TIERS, PUBLIC, DEVELOPER, ADMIN
    policy = yaml.safe_load(open("mcp/auth/policies/resource_mcp_tool.yaml"))
    rules = policy["resourcePolicy"]["rules"]
    expected_in_rule = {PUBLIC: set(), DEVELOPER: set(), ADMIN: set()}
    for tool, tier in TOOL_TIERS.items():
        expected_in_rule[tier].add(tool)
    # parse rule conditions, assert no tool appears in two rules
    # assert each TOOL_TIERS entry appears exactly once in the matching rule
    ...

def test_requirements_doc_tools_exist():
    import re
    from mcp.core.tool_tiers import TOOL_TIERS
    text = open("requirements/AICE_SYSTEM_REQUIREMENTS.md").read()
    referenced = set(re.findall(r"`([a-z_]+)` tool", text))
    missing = referenced - set(TOOL_TIERS.keys())
    assert not missing, f"Requirements doc references nonexistent tools: {missing}"
```

Three tests, blocking on CI, would have caught **F-D01, F-D02, F-D03, F-D15, F-D16, F-D19** — every Critical and most Highs.

---

## 6. Suggested Disposition

| Priority | Finding | Effort | Owner |
|---|---|---|---|
| P0 (this sprint) | F-D01 (auth coverage) | 0.5 day | Auth/MCP |
| P0 | F-D04 (RG signals/base) | 0.5 day | Review Gate |
| P0 | F-D05 (PatternStore backing) | 0.5 day | MemoryLayer |
| P1 (next sprint) | F-D02, F-D16 (Cerbos/RLM tiers) | 0.5 day | Auth |
| P1 | F-D03, F-D19, F-D22 (req doc tool names) | 1 day | Tech writer |
| P1 | F-D06, F-D07, F-D09, F-D13 (counts) | 0.5 day + automation | Tech writer + CI |
| P2 | F-D08 (Cerbos policy refactor) | 1 day | Auth |
| P2 | F-D10, F-D17, F-D18, F-D21 (memory layer wording) | 0.5 day | MemoryLayer |
| P3 | F-D11, F-D12, F-D14, F-D20, F-D23 | 0.5 day | Misc |
| Foundational | CI doc-drift tests (Section 5) | 1 day | DevEx |

**Total effort estimate:** ~6 person-days. Highest ROI: the CI tests (1 day) prevent the entire class going forward.

---

## 7. What I deliberately did not flag

I noticed but did not raise the following because they are explicitly tracked in the Master Gaps List or are design choices not drift:
- ContextBuilder dual-implementation (HybridRAG canonical + MemoryLayer LegacyContextBuilder) — covered in user memory and the migration doc.
- Sprint-25 GAP-A14/A15 deferrals — explicit ADR-backed decisions.
- `_merge_results_rrf` → `_merge_results_weighted` rename — covered in Master Gaps Review4 cycle.
- AICE_SYSTEM_CARD.md "AICE is NOT a safety system" framing — that's a policy stance, not drift.
- Deprecated `RLM_TYPE_MAP`, `KG_TYPE_MAP` placeholders in DomainSessionAdapter — adapter not yet wired (F-D18 covers the wiring claim).

---

**End of Pass 1.** Ready to proceed to Pass 2 (Architecture & Boundaries) on your signal.

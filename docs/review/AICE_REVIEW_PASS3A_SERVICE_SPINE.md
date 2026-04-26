# AICE Review — Pass 3 / Cluster A: Service Spine

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:** `mcp/core/auth_middleware.py`, `src/HybridRAG/code/querier/search_service.py`, `src/IngestionPipeline/ingestion_service.py`, `src/ReviewGate/confidence.py`
**Excludes:** tests/. Findings already reported in Pass 1 / Pass 2 are referenced but not re-stated.

---

## 0. Summary

The four files in this cluster carry the most operational risk in AICE: every external request transits through `auth_middleware.py`, every retrieval call goes through `search_service.py`, every byte of training-data goes through `ingestion_service.py`, and every confidence routing decision goes through `confidence.py`.

Bottom line: the spine is **functionally solid and well-tested in production paths**, but has accumulated a number of **silent-failure surfaces** — places where a wrong configuration or input is logged-and-swallowed rather than refused. For an ASPICE/EU-AI-Act-aligned system, "best-effort" is not the same as "auditable," and a few of these need tightening.

**Findings count:** 27 (4 Critical, 9 High, 10 Medium, 4 Low)

| File | C | H | M | L | Total |
|---|---:|---:|---:|---:|---:|
| `auth_middleware.py` | 2 | 3 | 2 | 1 | 8 |
| `search_service.py` | 1 | 3 | 4 | 1 | 9 |
| `ingestion_service.py` | 1 | 2 | 2 | 1 | 6 |
| `confidence.py` | 0 | 1 | 2 | 1 | 4 |

Severity criteria:
- 🔴 **Critical** — exploitable, data-loss, audit-bypass, or correctness regression in a production path.
- 🟠 **High** — recurring runtime issue, silent-failure, or drift from documented contract.
- 🟡 **Medium** — code-quality issue with operational impact (perf, memory, maintainability).
- 🟢 **Low** — cosmetic / future-proofing.

---

## 1. `mcp/core/auth_middleware.py` (~252 LoC)

### F-CA-A01 🔴 — `_api_key_registry` reload race + no TTL: rotated keys take effect non-deterministically across workers

**Evidence:**
```python
# Line ~50
_api_key_registry: Dict[str, dict] | None = None

def load_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    ...
    global _api_key_registry
    if _api_key_registry is not None:
        return _api_key_registry
    ...
    _api_key_registry = data.get("keys", {})
```

`reload_api_keys()` exists but is never called from `app.py`, the warmup, the audit logger, or any scheduled job (verified by searching for `reload_api_keys`). The registry is loaded **once per process** and cached forever.

In production AICE runs **with Gunicorn** (per the Sprint 25 Dockerfile additions). Gunicorn forks N workers; each forks the parent's `_api_key_registry` via copy-on-write. After fork, every worker has its own registry. When the YAML is updated:
- Workers don't reload → keep stale registry indefinitely.
- A pod restart eventually picks up the change, **per worker, in unpredictable order**.

For a typical 4-worker pod under steady load, a key rotation has a window of seconds-to-minutes where some workers accept the new key, others reject it, and the rotated-out key may still be valid in some workers.

**Impact:**
- **Compromised key cannot be revoked promptly.** A leaked `key-admin-pipeline` continues to authorize against half the workers until pod restart. For an admin tier key (full ingestion + JWT lifecycle) this is the auth-equivalent of a P0.
- **Audit trail is non-deterministic.** Two adjacent calls from the same client may produce different deny/allow outcomes — a finding that fails ASPICE SUP.10 (Configuration Management of Operational Data).
- The `reload_api_keys()` function exists but is dead code without a trigger.

**Recommendation:**
1. **Add a watchdog reload.** `apscheduler` is already in `app.py` (Sprint 25 added it for health checks and cache stats). Add a job:
   ```python
   def _reload_api_keys_job():
       from mcp.core.auth_middleware import reload_api_keys
       reload_api_keys()
       logger.info("[Scheduler] API key registry reloaded")
   scheduler.add_job(_reload_api_keys_job, IntervalTrigger(seconds=60))
   ```
2. **Add a TTL check in `load_api_keys`.** Cache the file `mtime`. On each `load_api_keys()` call, if `mtime` changed, reload. Cost: one `os.stat()` per check, microseconds.
3. **Expose `/admin/reload-api-keys`** (admin-tier MCP tool) so SREs can force-reload without waiting for the watchdog.
4. **K8s secret rotation:** if `api_keys.yaml` is mounted as a K8s secret, file `mtime` changes on rotation — the TTL check picks it up automatically.

This is the single highest-priority fix in this cluster. Effort: 1 day.

---

### F-CA-A02 🔴 — Default-deny is by tier, but missing `principal.roles` produces a default-allow path

**Evidence:**
```python
# resolve_principal(), ~line 90
roles = workspace_roles.get(workspace_id, [])
if not roles:
    roles = workspace_roles.get("*", [])

# Cerbos requires at least one role — use a placeholder that
# will match nothing in the policies.
if not roles:
    roles = ["_none"]
```

Then in `_check_via_local_tiers`:
```python
def _check_via_local_tiers(principal, tool_name, tier):
    from .tool_tiers import TIER_HIERARCHY
    roles = principal.roles if hasattr(principal, "roles") else principal.get("roles", set())
    for role in roles:
        allowed_tiers = TIER_HIERARCHY.get(role, set())
        if tier in allowed_tiers:
            return True, "allowed"
    ...
    return False, msg
```

For the placeholder role `"_none"`, `TIER_HIERARCHY.get("_none", set())` returns `set()`, and `tier in set()` is `False`. So the local-tier check **does correctly deny** the placeholder.

However, when Cerbos *is* available, the path is:
```python
client.is_allowed("invoke", principal, resource)
```
The Cerbos resource policy file has a rule `roles: ["public", "developer", "admin"]` for PUBLIC tools. **`"_none"` is not in that list** → policy doesn't match → no `EFFECT_ALLOW` rule fires → Cerbos returns `EFFECT_DENY` (Cerbos default). Good.

**But there's a subtle bug:** the `derived_roles.yaml` defines `public_via_developer` and `public_via_admin` as `parentRoles: ["developer"]` and `["admin"]` respectively. If a **future change** to `derived_roles.yaml` ever adds an `_none` parent role (e.g., from a copy-paste error or a "default role" experiment), every tool would silently become accessible to all unauthenticated callers because `_none` would inherit all derived roles.

The placeholder pattern is **fragile**. The proper fix is:
- Return `None` (which `check_authorization` already handles as "Unknown or missing API key" → deny).
- Or raise a typed `MissingRoleError` and have the caller convert it to a `403`.

**Evidence this is reachable:** `api_keys.yaml` line 70 onward shows `key-admin-pipeline` with `roles: { "*": ["admin"] }` — the `*` wildcard. If a new key is added without `*` and without the requested workspace (e.g., a key with only `roles: { illd: ["public"] }` queried with `workspace_id="mcal"`), the placeholder path activates.

**Recommendation:**
1. Replace the placeholder with `return None`:
   ```python
   if not roles:
       logger.warning("Principal %s has no roles for workspace %s — denying", principal_id, workspace_id)
       return None
   ```
2. Update `check_authorization` to treat `None` from `resolve_principal` as a final deny (already does).
3. **Add a unit test** that asserts: principal with no roles for workspace → all tool invocations return `(False, ...)` with both Cerbos available and unavailable.

Effort: 30 minutes; this is one of the cleanest tightenings in the file.

---

### F-CA-A03 🟠 — `check_authorization` lacks structured logging fields needed for ASPICE audit

**Evidence:**
```python
# ~line 175
logger.warning(
    "DENY   principal=%s tool=%s workspace=%s — %s",
    principal.id, tool_name, workspace_id, msg,
)
```

Logs are formatted-string-style. The audit requirement (per `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-PG-005: *"Every MCP tool invocation shall be logged to audit_logs with: tool, caller, workspace, session, params, status, duration_ms"*) implies these should be structured. The current logger output goes to stderr; the *separate* PostgreSQL audit write happens elsewhere (in `mcp_server.py::_authorize`).

There's no correlation between the `auth_middleware.py` log entry and the PostgreSQL `audit_logs` row — the log says `principal=cia_assistant tool=search_database workspace=illd` but the audit row has its own row-id. Joining them after the fact requires timestamp matching, which is unreliable across log delays.

The DENY case has worse consequences: **denials are logged but never written to PostgreSQL** because `mcp_server.py::_audit_log()` runs after the auth check. A repeated brute-force on tool names from a low-tier key produces nothing in the audit trail visible to compliance, only stderr lines that get rotated.

**Recommendation:**
1. Pass an opaque `correlation_id` (uuid4 hex) through both the logger call and the audit write. Add it to the `audit_logs` table.
2. **Write DENIES to PostgreSQL too.** Add `pg.save_authz_decision(api_key_hash, tool, workspace, decision, reason, correlation_id)`. This is a critical audit trail entry — denies are far more security-relevant than allows.
3. Use structured logging: switch logger calls to `logger.warning("authz.deny", extra={"principal_id": ..., "tool": ..., ...})` or move to `structlog`.

Effort: 1 day.

---

### F-CA-A04 🟠 — Cerbos client lifecycle: no health-check; no reconnect; no timeout

**Evidence:**
```python
def _get_cerbos_client():
    global _cerbos_client
    if _cerbos_client is None and _CERBOS_SDK_AVAILABLE:
        try:
            _cerbos_client = CerbosClient(host=_get_cerbos_url())
        except Exception as exc:
            logger.warning("Failed to create reusable CerbosClient: %s", exc)
    return _cerbos_client
```

And in `check_authorization`:
```python
try:
    client = _get_cerbos_client()
    if client is None:
        with CerbosClient(host=_get_cerbos_url()) as client:
            resp = client.is_allowed("invoke", principal, resource)
    else:
        resp = client.is_allowed("invoke", principal, resource)
    ...
except Exception as exc:
    logger.exception("Cerbos check failed for tool=%s — falling back to local tier check", tool_name)
    # Fall through to local tier check below
```

Issues:
1. **No timeout on Cerbos calls.** `CerbosClient.is_allowed()` blocks indefinitely if the Cerbos PDP becomes unresponsive (network split, OOM, deadlocked). Under load this saturates worker threads → all auth checks queue → request timeout cascade.
2. **No reconnect.** Once `_cerbos_client` is set, it's reused forever. If the Cerbos sidecar restarts, the underlying connection is dead but the client object is cached. Every subsequent call goes through the `except Exception` → fallback path silently. Under steady-state, **all auth decisions are made by local-tier fallback after Cerbos restart, until pod restart.** No alarm.
3. **No success counter / freshness check.** No way to detect "Cerbos has been silently broken for 6 hours."

**Per Pass 1 F-D16:** when Cerbos is up vs. down, `rlm_orchestrate` flips between PUBLIC and DEVELOPER tier. Combined with this finding, you can have hours where every tier-decision is wrong.

**Recommendation:**
1. **Add a 1-second timeout** to Cerbos calls. The `cerbos-py` SDK accepts `timeout=` per request or at the client level. (verify against your installed SDK version.)
2. **Add a periodic health check** in the apscheduler block in `app.py`. If Cerbos is unreachable for >30s, set a Prometheus gauge `cerbos_up=0` and emit a structured warning. Operators see it immediately.
3. **Treat ConnectionError specifically** — recreate `_cerbos_client` rather than fall back. The current behavior (catch all exceptions, fall back) hides connection-level errors.
4. After Pass 1 F-D02 lands (Cerbos policy refactor to use `attr.tier`), the policy and fallback paths will be 1:1, so this divergence becomes much less critical — but you still want the timeout.

Effort: 2 days (the health-check telemetry is the bulk).

---

### F-CA-A05 🟠 — `api_key[:8] + "…"` partial-prefix is a leak vector for low-entropy keys

**Evidence:**
```python
# ~line 125
attr={
    "workspace_id": workspace_id,
    "api_key_hash": api_key[:8] + "…",  # partial for audit logs
},
```

The current keys (`api_keys.yaml`) are 14–20 character human-readable strings:
- `key-cia-001`, `key-gest-001`, …, `key-admin-pipeline`, `key-admin-ops`

8 characters of prefix is **the entire prefix structure** — `key-cia-`, `key-gest-`, etc. The remaining suffix is short (e.g., `001`, `pipeline`). With 8 characters logged, an attacker who reads the logs effectively has the full key for most of these.

The variable is named `api_key_hash` — which is misleading. It's not a hash; it's a partial-cleartext prefix.

**Recommendation:**
1. Use a real cryptographic hash:
   ```python
   import hashlib
   api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
   ```
   16 hex chars = 64 bits of entropy, enough to identify the key in audit logs without revealing it.
2. **Move to longer, random keys.** `key-cia-001` style is anti-pattern for a system that's about to face EU AI Act audit. Use `key-cia-{32 hex}` minimum. The DA name remains as a prefix (handy for grepping audit logs), but the suffix is a real secret.
3. Combine with F-CA-A01: at the same time, switch the registry to store `sha256(key)` instead of plaintext. Compare hashes at lookup time.

Effort: 1 day for hashing change. Key-rotation effort separate.

---

### F-CA-A06 🟡 — `os.environ.get("CERBOS_HTTP_PORT", "3592")` returns string but is implicitly used as int

**Evidence:**
```python
def _get_cerbos_url() -> str:
    host = os.environ.get("CERBOS_HOST", "localhost")
    port = os.environ.get("CERBOS_HTTP_PORT", "3592")
    return f"http://{host}:{port}"
```

This is fine because the result is interpolated into a URL. But `app.py::main` line 60 does:
```python
cerbos_http_port = int(os.environ.get("CERBOS_HTTP_PORT", "3592"))
```

Inconsistent — one place keeps it as a string, the other casts. If the env var is malformed (`CERBOS_HTTP_PORT=3592 `, trailing space), `app.py` raises `ValueError`, but `auth_middleware.py` silently produces `http://localhost:3592 /` and **all auth requests fail silently**.

**Recommendation:** Centralize port parsing in a single helper:
```python
def _env_port(name: str, default: int) -> int:
    val = os.environ.get(name, str(default)).strip()
    try:
        return int(val)
    except ValueError:
        logger.warning("%s=%r is not a valid port, using default %d", name, val, default)
        return default
```
Effort: 30 min.

---

### F-CA-A07 🟡 — Silent registry "denies all" when `api_keys.yaml` is missing

**Evidence:**
```python
if not path.is_file():
    logger.warning("API key registry not found at %s — all requests will be denied", path)
    _api_key_registry = {}
    return _api_key_registry
```

Logging `WARNING` for an event that means "the entire authentication subsystem is broken" is wrong severity. This should be:
- `CRITICAL` log entry.
- A Prometheus gauge `aice_auth_registry_loaded=0` so it's alertable.
- Optional: refuse to start if the env var `MCP_REQUIRE_AUTH=1` is set (production posture).

`load_api_keys` returning `{}` without making noise means: server starts, every request is denied, ops gets paged hours later when DAs report failures. A loud failure at startup is preferable.

**Recommendation:** Same as above — bump to CRITICAL, add gauge, gate on env var.

Effort: 30 min.

---

### F-CA-A08 🟢 — Cerbos `_FallbackPrincipal` mutability is OK but undocumented

`_FallbackPrincipal` is a `@dataclass` with `roles: set[str]`. Sets are mutable; nothing in the file mutates the principal but a future contributor might. Add `frozen=True`.

```python
@dataclass(frozen=True)
class _FallbackPrincipal:
    id: str
    roles: frozenset[str]
    attr: Mapping[str, Any]  # frozenset / FrozenDict not stdlib-typed; OK to leave
```

Effort: 5 min.

---

## 2. `src/HybridRAG/code/querier/search_service.py` (~1259 LoC)

### F-CA-S01 🔴 — `hybrid_search_async` may execute the search **twice** in some configurations (sync wrapper around `asyncio.gather`)

**Evidence:** the file has both:
- `hybrid_search()` — uses `ThreadPoolExecutor(max_workers=2)` for graph + vector parallelism.
- `hybrid_search_async()` — uses `asyncio.gather(_graph_stage(), _vector_stage())` over `asyncio.to_thread`.

Both call the same `_graph_search()` and `_vector_search()` underneath. The MCP tool handler is `async def search_database(...)` and calls `await asyncio.to_thread(svc.hybrid_search, ...)` (the sync version) — confirmed in `mcp_server.py`. So `hybrid_search_async()` is *not* wired into the production path right now.

**The risk:** if a future refactor (especially a Sprint-26 DA change to talk to SearchService directly without going through MCP) calls `hybrid_search_async()` while still wrapping it in `asyncio.to_thread`, you get **double-async** — `asyncio.to_thread` puts the async function on a thread, but `asyncio.gather` inside it tries to attach to a loop that doesn't exist on that thread. This produces:
- Either `RuntimeError: There is no current event loop in thread 'ThreadPoolExecutor-X'` (older Python),
- Or silent creation of a new loop per call (CPython 3.12+) — performance regression where every call spins up a loop.

**This is a footgun, not yet a live bug.** But the existence of two parallel APIs (`hybrid_search` sync + `hybrid_search_async` async) with the same name pattern, both legitimate-looking, is the classic setup for the bug to land in the next sprint.

**Recommendation:**
1. **Pick one.** The async version is the architecturally correct one (DocJockey Phase 1.1 plan recommends it; per `Perf_improvements.md` it's already partially implemented elsewhere).
2. Delete `hybrid_search()` (sync) and inline its logic into `hybrid_search_async()`. The MCP tool handler then does `await svc.hybrid_search_async(...)` instead of `await asyncio.to_thread(svc.hybrid_search, ...)`. Saves a thread per call and removes the two-API ambiguity.
3. **In the interim** (while you decide), add a docstring warning on both functions: "Do not use `hybrid_search_async()` from a sync context — wrap `hybrid_search()` instead."

Effort: 1 day for the migration; faster if `hybrid_search_async` already works in tests.

---

### F-CA-S02 🟠 — `_merge_results_rrf` function name still says "rrf" but body is alpha-blending; Master Gaps Review4 §8.2 #8 said this was renamed

**Evidence:**
```python
# ~line 870
def _merge_results_rrf(
    self, graph: List[Dict], vector: List[Dict], alpha: float,
    k: int = 60, workspace_id: str = "illd",
) -> List[Dict]:
    """
    Merge graph + vector results.

    ILLD: Reciprocal Rank Fusion — ``alpha/(k+rank+1)`` per list.
    MCAL: Global normalisation + alpha blending (preserves cross-source
    score differences and handles MCAL's multi-collection fan-out).
    """
    if workspace_id == "mcal":
        return self._merge_mcal(graph, vector, alpha)
    return self._merge_illd_rrf(graph, vector, alpha, k)
```

Master Gaps List §8.2 row 8 says: *"`_merge_results_rrf` mis-named → Renamed to `_merge_results_weighted` ✅"*. **The rename did not land in source.** The function is still `_merge_results_rrf` and the docstring says "Reciprocal Rank Fusion" — but the MCAL branch goes to `_merge_mcal` which is alpha-blending (not RRF), and even the ILLD branch uses an `alpha`-weighted RRF formula `rrf = alpha * (1.0 / (k + rank + 1))` (which is technically still RRF, but with a conditional weight that pure RRF doesn't have).

So Master Gaps says "fixed" but source says otherwise. Either:
1. The fix landed in a different file/function.
2. The fix was reverted.
3. Master Gaps is wrong.

**Impact:** terminology ambiguity in a doc-heavy codebase. Three different parts of the system call this "RRF" / "alpha-blending" / "weighted blending" depending on which file you read. For an automotive auditor this looks like uncoordinated change tracking.

**Recommendation:**
1. Verify with a `git log -p mcp/HybridRAG/code/querier/search_service.py | grep -B2 -A2 _merge_results_` whether the rename ever shipped.
2. If it didn't: rename now (`_merge_results_weighted` for the dispatcher, keep `_merge_illd_rrf` for the per-list one).
3. If it did but in a different file: align terminology in source comments, not just function names.
4. Update Master Gaps §8.2 row 8 status accordingly (currently shows ✅).

Effort: 30 min for the rename.

---

### F-CA-S03 🟠 — `extract_keywords` and `extract_named_entities` are imported from `kg_node_utils` but applied to user query without sanitization

**Evidence:**
```python
# ~line 250 in hybrid_search()
named_ents = extract_named_entities(query, module=filter_by_module or self.module)
...
# Used in graph_search:
keywords = extract_keywords(query)
```

The `query` parameter is user-controlled (comes straight from the MCP tool input). It then goes into:
- `_graph_search()` which builds Cypher with `toLower(CONTAINS)` clauses against multiple properties.
- `_entity_targeted_lookup()` which constructs further Cypher.

I cannot see the body of `extract_keywords` and `extract_named_entities` from project knowledge, but if they apply any regex without escaping special characters, **a query containing Cypher metacharacters** (single quotes, backticks, `'); DROP`, etc.) could land in a Cypher string. Even with parameterized queries (which the file mostly uses — verified in `execute_cypher`), `CONTAINS` substring matching doesn't escape the right-hand operand by default.

**This is not a confirmed SQL/Cypher injection** — the surrounding code uses parameter binding. But the fact that user input is being run through a keyword extractor and then fed back into a query is exactly the pattern where injection regressions appear. **Add a tripwire test:** unit test that calls `hybrid_search(query="' OR 1=1 //")` and asserts no error and no abnormal result count.

**Pass 4 will look at this in depth.** For Cluster A I'm flagging it as "looks safe but unverified."

**Recommendation:**
1. Add a unit test (Pass 4 will ship one) verifying that special-character queries are handled.
2. Make the parameter-binding pattern explicit in the search method's docstring: *"All user input is bound as Cypher parameters; substring/CONTAINS matches use parameter binding (`$query_substring`)."*

---

### F-CA-S04 🟠 — `embed_fn` injection point silently disables embedding model, no warning

**Evidence:**
```python
def __init__(self, ..., embed_fn=None, ...):
    self._embed_fn = embed_fn
    self._st_model = None  # lazy-init sentence-transformers
```

```python
def _embed_query(self, text: str) -> List[float]:
    if self._embed_fn:
        return self._embed_fn(text)
    if self._st_model is None:
        try:
            from src.Configuration.embedding_singleton import get_shared_model
            self._st_model = get_shared_model()
        except Exception:
            try:
                from sentence_transformers import SentenceTransformer
                model_name = os.environ.get("ST_MODEL", "all-MiniLM-L6-v2")
                self._st_model = SentenceTransformer(model_name)
                logger.info("SearchService: loaded embedding model '%s'", model_name)
            except ImportError:
                logger.error("sentence-transformers not installed — vector search unavailable")
                return []
    if self._st_model is None:
        return []
    return self._st_model.encode(text, normalize_embeddings=True).tolist()
```

Returning `[]` from `_embed_query` causes `_vector_search` to skip Qdrant entirely (no embedding → no vector results). The error log fires once per *call*, but vector search continues to be silently disabled. There's no Prometheus signal, no health-check failure, no surfacing in `health_check` MCP tool output.

**Operational impact:** if `sentence-transformers` is not installed (e.g., minimized container layer in dev), AICE's hybrid search **silently degrades to graph-only**. A DA running CIA (which prefers low-alpha = vector-heavy) gets dramatically worse results, but nothing alerts.

**Recommendation:**
1. Set a flag `self._vector_disabled = True` on the first `ImportError`.
2. Have `health_check` tool report `"vector_search": "unavailable"` if the flag is set.
3. Add a Prometheus gauge `aice_vector_search_available` (0 or 1).
4. **Fail fast** at SearchService construction if `sentence-transformers` is missing AND no `embed_fn` was injected — `__init__` should call `_embed_query("test")` once and refuse to construct if it returns `[]`.

Effort: 1 day.

---

### F-CA-S05 🟡 — `_collection_cache_ttl` default 5 min is fine, but cache key collision possible

**Evidence:**
```python
self._collection_cache: Dict[str, List[str]] = {}  # workspace → names
self._collection_cache_ts: Dict[str, float] = {}
self._collection_cache_ttl: float = float(os.environ.get("COLLECTION_CACHE_TTL", "300"))
```

The cache is keyed by `workspace`. But the `_resolve_collections` lookup also depends on `module` (filter_by_module). If two callers in quick succession hit the cache for `workspace="mcal"` — one with `module="adc"` and one with `module="can"` — both get the same cached list, then filter-fan-out runs per call. This is *correct*, but the cache name is misleading — it caches the **full list of collections**, not the resolved subset.

The risk is that a future contributor reads the variable name `_collection_cache` and assumes they can write `_collection_cache[f"{workspace}:{module}"] = filtered_list`, which would create a different bug.

**Recommendation:** rename `_collection_cache` → `_all_collections_by_workspace_cache`, or add a docstring clarifying it's the unfiltered list.

Effort: 5 min.

---

### F-CA-S06 🟡 — ThreadPoolExecutor instantiated per-call in `hybrid_search()` is a perf regression

**Evidence:**
```python
# ~line 270 in hybrid_search()
if do_graph and do_vector:
    with ThreadPoolExecutor(max_workers=2) as pool:
        graph_future = pool.submit(self._graph_search, ...)
        vector_future = pool.submit(self._vector_search, ...)
        graph_results = graph_future.result()
        vector_results = vector_future.result()
```

Creating a `ThreadPoolExecutor` per call has overhead:
- 2 thread creations per request (Linux pthread_create ≈ 50–100µs each).
- Context manager overhead.

For ~100 req/s on a hot pod, this is 200 thread creations per second. Not catastrophic, but unnecessary.

**Recommendation:** This is moot if F-CA-S01 lands (delete `hybrid_search()`, use `hybrid_search_async()`). If you keep both:
- Use a class-level shared `ThreadPoolExecutor(max_workers=4)` initialized in `__init__` and reused.
- Or use `concurrent.futures.thread._ThreadPoolExecutor` at module level (singleton).

Effort: 30 min if not deleting `hybrid_search()`.

---

### F-CA-S07 🟡 — `merged.sort(key=lambda r: r.get("score", 0), reverse=True)` with `score=0` for missing entries hides bugs

**Evidence:**
```python
# In hybrid_search() entity-injection block:
for gr in extra[:needed]:
    for idx in range(len(merged) - 1, -1, -1):
        if (merged[idx].get("source") != "neo4j"
                and not merged[idx].get("_must_include")):
            merged[idx] = gr
            break
merged.sort(key=lambda r: r.get("score", 0), reverse=True)
```

If a result has no `score` field (which would be a bug in the merge step), it silently sorts to the bottom. No error, no warning. In a system where ranking quality is important, **a missing score should be loud** — likely an upstream bug in `_merge_results_rrf` or one of the `_graph_search`/`_vector_search` paths.

**Recommendation:** assert `"score" in r` in a debug-mode path, or use `r.get("score", -1)` so the missing entries land at the *top* (visible) rather than the bottom (hidden).

Effort: 5 min.

---

### F-CA-S08 🟡 — Two parallel context-builder import patterns in the same module

**Evidence:**
```python
from .context_builder import (
    AssembledContext, ContextBuilder, ContextBudget, ContextItem, ContextSlot,
)
```

Then later:
```python
def _get_context_builder(self) -> ContextBuilder:
    """Lazy-init the ContextBuilder."""
    if self._context_builder is None:
        budget = ContextBudget(total_budget=self._context_budget)
        self._context_builder = ContextBuilder(budget=budget)
    return self._context_builder
```

The local relative import `.context_builder` is the HybridRAG querier copy (per Pass 2 F-A03 — the wrong location). When Pass 2 F-A03 lands (move to MemoryLayer), this import path changes.

This is ALREADY a known issue. Listed here only to flag that any Pass 2 F-A03 work will touch this file at this line.

**Recommendation:** see Pass 2 F-A03.

---

### F-CA-S09 🟢 — Module docstring says "Sprint 2 → Sprint 8"; we're at Sprint 25

**Evidence:**
```python
"""
Search Service — Sprint 2 → Sprint 8
======================================
...
Sprint 8: Full Qdrant vector search + Reciprocal Rank Fusion (RRF) merge.
         Detailed Neo4j search with label-specific property maps,
         entity-targeted lookup, aggregation queries, and 1-hop expansion.
         Token-budget ContextBuilder integration.
"""
```

Last update marker: Sprint 8. Reality: Sprint 25 (with GAP-pipeline modules `QueryEnhancer`, `CrossEncoderReranker`, `ContextCompressor`, `ContextRefiner`, `RelevanceJudge` all imported and wired in `__init__`). Module docstring should reflect.

**Recommendation:** Update docstring to "Sprint 2 → Sprint 25" with a note about GAP pipeline integration.

Effort: 5 min.

---

## 3. `src/IngestionPipeline/ingestion_service.py` (~670 LoC)

### F-CA-I01 🔴 — `ingest_file` accepts arbitrary `file_path` from admin caller without containment check (path traversal risk)

**Evidence:**
```python
def ingest_file(self, file_path: str, module_name: str,
                overwrite: bool = False, workspace_id: str = "illd") -> Dict[str, Any]:
    """Parse a single file and ingest into KG."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")
    ...
```

Then `_parse_file` is called with the raw `Path`. The MCP tool wrapper for `ingest_file` (admin tier) accepts a string from the API caller. There is no:
- Allowlist of accepted base directories.
- Symlink check (`p.resolve()` with verification it's within an allowed root).
- Containment check (caller could pass `/etc/shadow`, `/proc/self/environ`, …).

Since `ingest_file` is **admin-tier**, an attacker with an admin key could:
- Pass `/etc/passwd` → file gets parsed by `c_parser` (which won't recognize it but might error in noisy ways), but worse, a Markdown-style file (`/etc/motd`) would be **ingested into Neo4j as a Document node**, exposing OS information through future searches.
- Pass `/proc/self/environ` → environment variables (which include `GPT4IFX_PASSWORD`, `NEO4J_PASSWORD`) become content in the KG.
- Pass paths with symlinks pointing outside the allowed area.

This is a "trust the admin tier" pattern that the rest of the codebase (e.g., `execute_cypher` write-clause filter) explicitly rejects. The Cerbos hard-line is "admin can do anything," but admin actions should still be **bounded by the deployment posture** — admins don't legitimately need to ingest arbitrary OS files.

**Recommendation:**
1. Add an `INGEST_ALLOWED_ROOTS` env var (default: `/data,/repos`) and validate `p.resolve().is_relative_to(allowed_root)` for at least one root.
2. Reject symlinks: `if p.is_symlink(): raise ValueError("Symlinks not allowed")`.
3. Refuse to ingest files outside `SUPPORTED_EXTENSIONS` *before* path resolution (already done — keep).
4. Log every `ingest_file` call with the resolved absolute path in `audit_logs` (admin actions need post-hoc accountability).

This applies to `ingest_module_from_repo`, `batch_ingest_modules`, and `ingest_repository` equally.

Effort: 1 day.

---

### F-CA-I02 🟠 — `_discover_module_files` caps at 100 files silently — can drop legitimate files

**Evidence:**
```python
return sorted(set(files))[:100]  # Cap at 100 files
```

A single AURIX module (e.g., the SCU module with all its sub-headers, register definitions, and SWA documents) can easily exceed 100 files. The cap silently drops the rest.

**Impact:** Module ingestion claims success, but only the first 100 files (by sort order!) are ingested. Subsequent searches missing certain registers or functions get attributed to "the model is bad" when actually "the data isn't there." This is the most pernicious kind of bug — silent partial failure.

**Recommendation:**
1. Make the cap configurable: `_get_env_int("INGESTION_MAX_FILES_PER_MODULE", 1000)`.
2. **Warn loudly** if the cap is hit:
   ```python
   if len(files) > cap:
       logger.warning(
           "[Ingestion] Module '%s' has %d files; capping at %d. Increase INGESTION_MAX_FILES_PER_MODULE.",
           module_name, len(files), cap,
       )
   ```
3. Surface in the job result: `{"status": "completed", "files_capped": True, "files_total": 247, "files_ingested": 100, ...}`.

Effort: 30 min.

---

### F-CA-I03 🟠 — `batch_ingest` `effective_workers = min(max_workers, total)` defaults `max_workers` is undefined in shown code

**Evidence:**
```python
effective_workers = min(max_workers, total) if total > 1 else 1

with ThreadPoolExecutor(max_workers=effective_workers) as executor:
    ...
```

The `max_workers` parameter is referenced but I cannot see its declaration in the snippet. Going by `Perf_improvements.md` it should default to 4. If the parameter signature is `def batch_ingest(self, lld_path, modules=None, workspace_id="illd", max_workers=4)`, then good. If it's missing (relies on a `self.max_workers` from `__init__` which is also not in the snippets), then it's a `NameError` at runtime — but this method ships, so it must be there.

**The risk:** the value flows from caller → method default → ThreadPoolExecutor. There is no upper bound. A misconfigured caller passing `max_workers=128` will spawn 128 threads, each with its own Neo4j session pool (which itself has a default of 50 connections). 128 × 50 = 6400 connections to Neo4j. Neo4j Community Edition default `dbms.connector.bolt.thread_pool_max_size` is 400. **Connection pool exhaustion** on the server side, all subsequent ingestions fail.

**Recommendation:**
1. Hard-cap `effective_workers = min(max_workers, total, MAX_INGESTION_WORKERS)` where `MAX_INGESTION_WORKERS = 8` (env-tunable).
2. Document the relationship between `max_workers` and Neo4j connection pool sizing in the docstring.
3. Per-thread Neo4j session pool — verify a single driver is shared (it appears to be `self._neo4j` per `IngestionService.__init__`); if so, the 128 × 50 calculation is wrong, but verify.

Effort: 30 min.

---

### F-CA-I04 🟡 — `ingest_repository` falls through to `batch_ingest` on the wrong path semantics

**Evidence:**
```python
def ingest_repository(self, repo_path: str, modules=None, include_tests=False, workspace_id="illd"):
    root = Path(repo_path)
    if not root.exists():
        raise FileNotFoundError(f"Repo not found: {repo_path}")
    # For repository-wide, delegate to batch_ingest
    lld_path = root / "lld" if (root / "lld").exists() else root
    return self.batch_ingest(str(lld_path), modules, workspace_id)
```

The `include_tests` parameter is silently dropped — `batch_ingest` doesn't accept it. The caller's request to include or exclude tests is **completely ignored**, and the result still claims success. Per the docstring of the corresponding MCP tool: *"include_tests (bool): Also ingest test files and create TestCase nodes. Default False."* The tool's contract is broken at this layer.

**Recommendation:** Either:
1. Remove `include_tests` from the function signature (and the MCP tool param).
2. Plumb it into `batch_ingest` and the underlying `_discover_module_files` (which already includes test directories implicitly via `*test*` patterns — would need to filter them out when `include_tests=False`).

Effort: 30 min for option 1, 2 hours for option 2.

---

### F-CA-I05 🟡 — `_fire_module_ingested` callback exception is swallowed; no retry, no metric

**Evidence:**
```python
def _fire_module_ingested(self, module_name: str, workspace_id: str):
    """Best-effort callback after successful module ingestion."""
    logger.info(...)
    if self._on_module_ingested:
        try:
            self._on_module_ingested(module_name, workspace_id)
        except Exception:
            logger.warning(
                "[Ingestion] on_module_ingested callback failed for '%s' — ingestion unaffected",
                module_name, exc_info=True,
            )
```

The callback is used by MCP layer for cache invalidation post-ingestion. If it fails, **the cache holds stale data forever** (until manual `cache_invalidate_module` admin call or pod restart).

**Recommendation:**
1. Add a Prometheus counter `aice_post_ingest_callback_failures_total{module=...}` so SREs see the failure rate.
2. Add a one-retry: try it twice, then log warning.
3. Or: write the failure to PostgreSQL `failed_callbacks` table for batch-retry by a scheduler.

Effort: 1 hour.

---

### F-CA-I06 🟢 — Sprint markers in docstrings: `Sprint 8: delegates to dedicated parsers...`

Per F-CA-S09 — this file claims Sprint 8 in `_parse_file` docstring; current sprint is 25. Update.

Effort: 5 min.

---

## 4. `src/ReviewGate/confidence.py` (~436 LoC)

### F-CA-C01 🟠 — `complete_review` and `record_feedback` write to PostgreSQL **without** ensuring `response_archive` row exists for `record_feedback`

**Evidence:**
```python
# In FeedbackSink.complete_review (~line 280)
if self._pg:
    self._pg.save_review_evidence(
        review_id=review_id, response_id=response_id, ...
    )
```

And in `postgres_schema.py::save_review_evidence`:
```python
# H09 fix: ensure response_archive row exists (FK constraint)
cur.execute(
    "INSERT INTO response_archive (response_id) "
    "VALUES (%s) ON CONFLICT (response_id) DO NOTHING",
    (response_id,)
)
cur.execute(
    "INSERT INTO review_evidence (...) ...",
    (...)
)
```

Good — the FK constraint is handled in `save_review_evidence`.

But `save_feedback` (called from `record_feedback`) **does not** include the same insert-then-insert pattern:
```python
def save_feedback(self, feedback_id, response_id, decision, ...):
    if not self._available:
        return
    try:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback_records (feedback_id, response_id, ...) "
                "VALUES (...) ON CONFLICT (feedback_id) DO NOTHING",
                (...)
            )
    except Exception as e:
        logger.warning("[PostgreSQL] save_feedback failed: %s", e)
```

If `feedback_records` has a foreign key to `response_archive(response_id)` (likely — it's normal to have one), then `record_feedback` for a `response_id` that wasn't pre-archived will **fail the FK and be silently dropped** (the `except` catches it, logs WARNING, continues).

**Compliance impact:** *every feedback record is supposed to land in PostgreSQL for ASPICE audit*. If a DA submits feedback before the response was archived (ordering not guaranteed across processes), the feedback is lost.

**Recommendation:**
1. Apply the same `INSERT response_archive ... ON CONFLICT` pattern to `save_feedback`.
2. **Or** drop the FK constraint on `feedback_records` (less clean, but simpler).
3. Add a unit test asserting feedback is persisted even when the response archive doesn't yet have the row.
4. Add Prometheus counter `aice_feedback_persistence_failures_total` so silent FK drops get noticed.

Effort: 1 hour.

---

### F-CA-C02 🟡 — `evaluate()` doesn't validate the `signals` dict — unknown keys silently ignored

**Evidence:**
```python
for signal_name, value in mapped.items():
    weight = self._weights.get(signal_name)
    if weight is None:
        continue
    ...
```

If a DA passes `signals={"misra_compliant": True, "has_kg_context": True}` — both **keys from the doc** (per Pass 1 F-D04 — the docs disagree with code) — `_weights.get("misra_compliant")` returns `None`, the signal is silently dropped, score is computed only from the 7 actual signals.

**Operational impact:** A DA that hasn't been updated for the Sprint 25 weight schema produces *lower* confidence scores than expected (because the keys it's sending are ignored). The DA team thinks "the model is bad," not "I'm using the wrong signal names."

**Recommendation:**
1. Log unknown signal names at INFO level (once per startup, not per call):
   ```python
   unknown = set(mapped) - set(self._weights) - {"validation_score", "api_match_ratio", "config_match_ratio"}
   if unknown:
       logger.info("evaluate: ignoring unknown signal names %s — known signals: %s",
                   unknown, list(self._weights.keys()))
   ```
2. **Or** raise a `ValueError` if `strict_signals=True` is set in `__init__`. Default `False` for backward compat.
3. Even better: typed signal interface. `class Signals(TypedDict, total=False): api_verified: bool; ...`

Effort: 1 hour.

---

### F-CA-C03 🟡 — Docstring says "Sprint 9: APPROVE/APPROVE_WITH_EDITS now writes to PatternStore (Neo4j) and indexes in PatternIndex (Qdrant)" — wrong per F-D05 / Pass 2 §8

**Evidence:**
```python
"""
...
Sprint 9: APPROVE/APPROVE_WITH_EDITS now writes to PatternStore (Neo4j)
and indexes in PatternIndex (Qdrant) for future similarity matching.
This enables the confidence scorer's 'has_proven_pattern' signal.
"""
```

Per your decision (Qdrant-only PatternStore), update this docstring.

Also note: the comment says `'has_proven_pattern'` — but the actual signal in `DEFAULT_WEIGHTS` is `pattern_match`. So the docstring is doubly wrong (Neo4j claim + signal name).

**Recommendation:** rewrite to:
```
Sprint 9 (revised Sprint 25): APPROVE/APPROVE_WITH_EDITS writes to the Qdrant-backed
PatternStore + PatternIndex for future similarity matching. This enables the
confidence scorer's 'pattern_match' signal.
```

Effort: 5 min.

---

### F-CA-C04 🟢 — `THRESHOLD_AUTO=80` and `THRESHOLD_QUICK=50` are module-level constants but `__init__` exposes them as parameters

**Evidence:**
```python
THRESHOLD_AUTO = 80
THRESHOLD_QUICK = 50

class ConfidenceCalculator:
    def __init__(self, weights=None, auto_threshold=THRESHOLD_AUTO, quick_threshold=THRESHOLD_QUICK):
        ...
```

Fine for module-level defaults, but the env vars `CONFIDENCE_AUTO_THRESHOLD` and `CONFIDENCE_QUICK_THRESHOLD` would be valuable for ops-tuning without code change. Especially as the policy maturity rises (see ASIL governance overrides — GAP-05).

**Recommendation:** Read from env at module load time:
```python
THRESHOLD_AUTO = int(os.environ.get("CONFIDENCE_AUTO_THRESHOLD", "80"))
THRESHOLD_QUICK = int(os.environ.get("CONFIDENCE_QUICK_THRESHOLD", "50"))
```

Effort: 5 min.

---

## 5. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CA-A01 (key reload), F-CA-A02 (placeholder role), F-CA-S01 (dual sync/async), F-CA-I01 (path containment) | 4 days |
| **P1 (next sprint)** | F-CA-A03 (audit denies), F-CA-A04 (Cerbos lifecycle), F-CA-A05 (key hash), F-CA-S02 (RRF rename), F-CA-S03 (query sanitization tripwire), F-CA-S04 (vector availability gauge), F-CA-I02 (file-cap warn), F-CA-I03 (worker bound), F-CA-C01 (FK on save_feedback) | 5–6 days |
| **P2** | F-CA-A06–A07, F-CA-S05–S08, F-CA-I04–I05, F-CA-C02–C03 | 2 days, mostly cosmetic + 1 small fix each |
| **P3** | F-CA-A08, F-CA-S09, F-CA-I06, F-CA-C04 | 1 hour total |

**Total cluster effort:** ~12 person-days. P0 alone is 4 days and addresses the highest-risk items in the entire spine.

---

## 6. What I deliberately did not flag

- **Per-tool tier/policy alignment** — covered in Pass 1 F-D01, F-D02, F-D03, F-D14, F-D15, F-D16, F-D19.
- **Default `base_score=20` vs docs claim `50`** — covered in Pass 1 F-D04 (your decision: code is canonical).
- **`PatternStore(neo4j_driver=...)` call site** — covered in Pass 1 F-D05 & Pass 2 §8.
- **`_merge_results_rrf` semantics** — covered in F-CA-S02 (a partial overlap with Pass 1, but the source-vs-Master Gaps drift is a new finding here).
- **`hybrid_search` call sequencing of entity-targeted lookup, aggregation, and 1-hop expansion** — these are correctness questions for Pass 5 (LLD productivity alignment), not code quality.

---

**End of Cluster A.** Ready to proceed to Cluster B (MCP server + tool handlers) on your signal.

Cluster B will cover `mcp/core/mcp_server.py` (~1800 LoC) — singleton lifecycle, tool registration, error envelopes, sandbox routing, and the `_warmup` pattern. Estimate: 20–25 findings.

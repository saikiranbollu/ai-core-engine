# AICE Review — Pass 3 / Cluster B: MCP Server

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:** `mcp/core/mcp_server.py` (~1800 LoC) — the single entrypoint that registers all `@mcp.tool()` handlers, owns the lazy-init service registry, ASGI middleware, the SDK shadowing shim, the per-tool `_authorize` flow, the `_warmup` path, and `main()`.
**Excludes:** tests/. Findings already reported in Pass 1, Pass 2, or Cluster A are referenced (with their original ID) but not re-stated.

---

## 0. Summary

`mcp_server.py` is the file with the **largest blast radius** in AICE. It is also the file that has accumulated the most "this works, leave it alone" patterns. The big-picture architectural problem (one file doing three jobs) is already in Pass 2 F-A04. This cluster looks at *what's actually in the file* and identifies correctness, robustness, and security issues at line level.

Bottom line: the file is **functionally correct for the happy path** but has many places where:
- Errors propagate as `_err("INTERNAL_ERROR", str(exc))` and lose all classification.
- Singletons silently fail to initialize and don't tell you about it.
- Sandbox behavior depends on subtle invariants that aren't enforced.
- The sync/async boundary is inconsistent.

**Findings count:** 24 (3 Critical, 8 High, 9 Medium, 4 Low)

| Severity | Count |
|---|---|
| 🔴 Critical | 3 |
| 🟠 High | 8 |
| 🟡 Medium | 9 |
| 🟢 Low | 4 |

Severity criteria (as in Cluster A):
- 🔴 **Critical** — exploitable, data-loss, audit-bypass, or correctness regression in a production path.
- 🟠 **High** — recurring runtime issue, silent-failure, or drift from documented contract.
- 🟡 **Medium** — code-quality issue with operational impact (perf, memory, maintainability).
- 🟢 **Low** — cosmetic / future-proofing.

---

## 1. Critical Findings

### F-CB-01 🔴 — `TraceabilityPuller.pull_neighbors` Cypher uses parameterized variable-length path bound — Neo4j does not support this syntax

**Evidence:** in `src/MemoryLayer/memory/ephemeral_sandbox.py` (called from `mcp_server.py::sandbox_upload`):

```cypher
MATCH path = (n)-[*1..$depth]-(neighbor)
```

Neo4j's Cypher language **does not allow variable-length path bounds (`*1..N`) to be parameterized**. The bound must be a literal integer at query-compile time. This will produce:

```
Neo.ClientError.Statement.SyntaxError:
Variable length pattern with parameter for length is not supported
```

The `try/except Exception as e: logger.error(...)` block catches this and returns `[], []`, so `sandbox_upload` *appears to succeed* but **the production traceability is silently empty** — `prod_nodes_loaded: 0`, `prod_relationships_loaded: 0`.

**Trigger conditions:** `trace_depth > 0` (default = 1) AND any node names extracted from upload. So **every** real `sandbox_upload` call hits this path. The user uploads a file, sees a "success" response with `nodes_created: 5`, but the prod-overlay shadow detection is broken because no prod nodes were loaded.

**Cross-effect:** Pass 2 F-A03 mentions the sandbox/prod overlay logic relies on `_origin: "production"` markers being on the prod nodes pulled into the sandbox. With prod nodes never loaded, every sandbox node is treated as a fresh insertion (no shadowing). The whole `_add_node_with_shadow` mechanism in `ephemeral_sandbox.py` becomes dead logic.

**Fix:**
```cypher
// Inline the depth as a literal — depth comes from a controlled int (0/1/2 only)
MATCH path = (n)-[*1..%d]-(neighbor)
```
Build the Cypher with `cypher = f"MATCH path = (n)-[*1..{int(depth)}]-(neighbor) ..."` after strict integer validation (depth is already validated to be 0/1/2 at the MCP layer — `if trace_depth < 0 or trace_depth > 2: return _err("INVALID_INPUT", ...)`). String formatting is **safe** here because `depth` cannot be user-controlled past that validation.

**Verification:**
1. Add a unit test that mocks the Neo4j driver and asserts the Cypher passed to `session.run` contains `*1..1` literally (not `*1..$depth`).
2. After the fix, `sandbox_upload` with `trace_depth=1` and a known function name should return `prod_nodes_loaded > 0`.

**Impact:** Sprint 4–5 Ephemeral Sandbox feature has been **silently broken in its primary mode** since the TraceabilityPuller landed. The feature still works without prod overlay (just sandbox nodes), but the shadowing/override mechanism — the entire reason the feature was built — has never been exercised in production unless someone is testing with `trace_depth=0`.

Effort: 30 minutes for the fix; ~1 hour for the regression test.

---

### F-CB-02 🔴 — Lazy-init pattern silently returns `None` without surfacing the failure to callers; tools then return generic `INTERNAL_ERROR` with no remediation path

**Evidence:** the universal pattern across `mcp_server.py`:

```python
def _get_search_service(profile: str = "illd"):
    if profile in _search_services:
        return _search_services[profile]
    try:
        from src.HybridRAG.code.querier.search_service import SearchService
        ...
        svc = SearchService(...)
        _search_services[profile] = svc
        logger.info("[MCP] SearchService initialized for profile '%s' ...", profile)
        return svc
    except Exception as e:
        logger.warning("[MCP] SearchService init failed for profile '%s': %s — search tools will return errors", profile, e)
    return None
```

Then in every tool:
```python
svc = _get_search_service(workspace_id)
if not svc:
    return _err("INTERNAL_ERROR", "SearchService unavailable")
```

The `except Exception` block:
1. Catches the original exception (could be `ImportError`, `Neo4jError`, `ConfigError`, anything).
2. Logs it as **WARNING** (severity is wrong — service init failure is not a warning, it's at minimum an ERROR, arguably CRITICAL).
3. Returns `None`.
4. **Discards the original exception**. Every subsequent tool call returns the same opaque `"SearchService unavailable"` message.

**Operational impact:** SREs see "SearchService unavailable" returned to a DA. To find out *why*, they need to grep stderr logs for `[MCP] SearchService init failed`, find the original log line, parse it. If logs are rotated or noisy, root cause is lost. There's no:
- Prometheus signal indicating service init failed.
- Self-test endpoint.
- Health-check tool surfacing it.
- Retry mechanism (`_search_services[profile] = None` is never set, so each call re-runs the failing init — performance hit + spam in logs).

**Worse:** because `_get_search_service()` doesn't cache the failure (the `_search_services[profile] = svc` only runs on success), every single tool call retries the failed import and reproduces the warning. If `SearchService` import has a 100ms penalty (sentence-transformers download attempt, etc.), this is 100ms latency added to every search call **forever**.

**Recommendation:** introduce a `_ServiceInitState` tuple per service:
```python
@dataclass
class _ServiceState:
    instance: Optional[Any] = None
    last_error: Optional[Exception] = None
    last_attempt: float = 0.0
    retry_after: float = 60.0  # seconds
```
- On success: cache the instance.
- On failure: cache `(None, exc)` and the timestamp; only retry after `retry_after` seconds.
- `health_check` reports per-service state with the captured exception's class name and brief message.
- Add Prometheus gauge `aice_service_up{service="..."}` (0 or 1) and counter `aice_service_init_errors_total{service="...", error_type="..."}`.

This pattern applies to all 12+ `_get_*` functions in the file. It's a single helper that fixes them all.

Effort: 2 days (the helper + retrofit + health-check integration).

---

### F-CB-03 🔴 — `_get_neo4j` resolves connection per-call without driver pooling guarantees; failure produces silent stale-driver reuse

**Evidence:** I haven't seen the full body of `_get_neo4j()` in project knowledge, but the pattern from related code is:
```python
_neo4j_drivers: Dict[str, Any] = {}     # keyed by profile

def _get_neo4j(profile: str = "illd"):
    if profile in _neo4j_drivers:
        return _neo4j_drivers[profile]
    cfg = _load_neo4j_profile_config(profile)
    if not cfg:
        return None
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["username"], cfg["password"]))
        _neo4j_drivers[profile] = driver
        return driver
    except Exception:
        return None
```

Issues:

1. **No `verify_connectivity()` call at construction time.** A driver created against an unreachable Neo4j succeeds silently; the failure surfaces only when `session.run()` is called — by which time multiple tools have already used the broken driver. (Compare `Neo4jConnection.connect()` in `neo4j_manager.py` which DOES call `verify_connectivity()`.)

2. **No health refresh.** If Neo4j restarts or the network blips, the cached driver stays. The neo4j-python driver has built-in connection-pool retry, but only at the session level — driver-level health is not re-checked. After a long Neo4j outage the driver may be in an unrecoverable state and need recreation.

3. **No pool sizing.** Default `max_connection_pool_size=100` per driver. With two profiles (`illd`, `mcal`), that's potentially 200 connections from a single MCP pod. If you run 4 Gunicorn workers, that's 800. Neo4j Community Edition's default is ~400 — exhaustion becomes likely under load.

4. **Combined with F-CB-02:** when init fails, every tool retries (no negative caching), so the warning is logged once per request — a single broken Neo4j at startup pours WARNING into stderr at request rate.

**Recommendation:**
1. Use `Neo4jConnection.connect()` from `neo4j_manager.py` (which already calls `verify_connectivity()`) instead of constructing a raw driver.
2. Apply F-CB-02 negative-caching with a per-service `_ServiceState`.
3. Set `max_connection_pool_size` from `storage_config.yaml` (e.g., 25 per profile per worker = 100 total under 4 workers, well under Neo4j's 400 default).
4. Add a 5-minute `apscheduler` job in `app.py` that calls `driver.verify_connectivity()` and resets the cached driver on failure.

Effort: 1 day.

---

## 2. High Findings

### F-CB-04 🟠 — `_authorize` doesn't pass `correlation_id` through to the audit log; combined with Cluster A F-CA-A03 there's no way to join an MCP audit trail

**Evidence:**
```python
def _authorize(tool_name: str, **kw) -> Optional[str]:
    api_key = _current_api_key.get("") or os.environ.get("MCP_API_KEY", "")
    ...
    pg = _get_postgres_client()
    if pg and pg.available:
        try:
            pg.log_audit(
                tool_name=tool_name, workspace_id=ws,
                caller_api_key=api_key[:8] + "…",
                response_code="ok" if allowed else "denied",
            )
        except Exception:
            pass
```

This is the same `api_key[:8] + "…"` issue from Cluster A F-CA-A05 — applies here too, the partial key is logged to PostgreSQL with the misleading name `caller_api_key`. (Recommended fix: sha256 hash; see F-CA-A05.)

**Additional issue here:** `log_audit` is called with **no correlation/request ID**. The MCP request hits:
- ASGI middleware (`_APIKeyMiddleware`)
- `_authorize` → `pg.log_audit` (audit row #1)
- Tool handler executes
- `_ok` / `_err` → `_finish_tool` → Prometheus metric (no correlation in metric labels either)
- Nothing else writes the request to PG

So there's a single audit log row per request, **but it's written before the tool executes**. If the tool raises (caught by `except Exception`) the audit log says "ok" / "denied" based on auth, and *says nothing about whether the tool succeeded*. The audit row's `response_code` field is misleadingly named — it tracks the **authorization** decision, not the **tool execution** outcome.

For ASPICE compliance: an auditor asking "show me all denied calls and all errored calls in the last 30 days" would get only the denied ones. Errored calls look identical to successful ones in `audit_logs`.

**Recommendation:**
1. Generate a `request_id = uuid4().hex` at the start of every tool handler (or in the ASGI middleware once and propagate via contextvars).
2. Write to `audit_logs` **after** tool execution completes — `_ok` and `_err` should call `pg.log_audit(..., outcome=...)`.
3. Or, write twice: once at start (decision), once at end (outcome). Both rows share a `correlation_id`.
4. Add `correlation_id` to the JSON envelope returned by `_ok`/`_err` so a DA can quote it when reporting issues.

Effort: 1 day.

---

### F-CB-05 🟠 — `_finish_tool` reads `_tool_name_ctx` and `_tool_start_time` but I cannot find where they're set; the metric may always observe 0.0

**Evidence:**
```python
_tool_name_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_tool_name_ctx", default="")
_tool_start_time: contextvars.ContextVar[float] = contextvars.ContextVar("_tool_start_time", default=0.0)

def _finish_tool(status: str) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    name = _tool_name_ctx.get("")
    t0 = _tool_start_time.get(0.0)
    if name:
        TOOL_REQUESTS_TOTAL.labels(tool=name, status=status).inc()
        TOOL_REQUEST_DURATION.labels(tool=name).observe(time.time() - t0)
```

For `TOOL_REQUESTS_TOTAL` to fire, `_tool_name_ctx` must be set somewhere — the `_set_tool_name(...)` calls aren't visible in any of the snippets I've examined. If they're never set, `name == ""` and the `if name:` guard skips both metric writes. **This means the central `aice_tool_requests_total` and `aice_tool_request_duration_seconds` Prometheus metrics may be silently no-op-ing in production.**

The Grafana dashboard at `monitoring/grafana/dashboards/aice-overview.json` has a panel `"sum(rate(aice_tool_requests_total[5m])) by (tool)"`. If the metric is silent, the panel shows nothing. The deployment doc's Sprint 10 claim *"All 56 tools shall be automatically instrumented via _ok()/_err() helpers (no per-tool code changes)"* (AICE-PROM-002) would be **unmet**.

I cannot confirm without seeing the rest of the file. There may be a `_start_tool(tool_name)` call inside the `with_session_routing` decorator or somewhere I haven't searched for.

**Recommendation:**
1. Manually verify: `git grep "_tool_name_ctx.set" mcp/` should show one or more setters.
2. If no setter exists, add one — preferably as an outermost decorator applied to every `@mcp.tool()`:
   ```python
   def _instrument(tool_name: str):
       def deco(fn):
           @wraps(fn)
           async def wrapper(*a, **kw):
               _tool_name_ctx.set(tool_name)
               _tool_start_time.set(time.time())
               return await fn(*a, **kw)
           return wrapper
       return deco
   ```
3. Or restructure `_ok`/`_err` to take `tool_name` as a positional argument (most invasive but clearest).
4. **Add a smoke test** in CI that calls one tool and verifies `aice_tool_requests_total{tool="search_database"}` increments.

Effort: 30 min to verify, 2 hours to fix if broken.

---

### F-CB-06 🟠 — `with_session_routing` decorator silently ignores kwargs that don't match the wrapped function's signature

**Evidence:**
```python
def with_session_routing(tool_name: str):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, session_id: Optional[str] = None, **kwargs):
            if session_id:
                sm = _get_sandbox_manager()
                sandbox = sm.get_sandbox(session_id) if sm else None
                if sandbox:
                    ...
                    kwargs["graph_service"] = hybrid
                    kwargs["query_mode"] = classification
                    kwargs["sandbox_ctx"] = sandbox

            sig = inspect.signature(fn)
            has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if has_varkw:
                filtered_kwargs = kwargs
            else:
                filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

            if "session_id" in sig.parameters:
                return await fn(*args, session_id=session_id, **filtered_kwargs)
            return await fn(*args, **filtered_kwargs)
```

Two problems:

1. **`inspect.signature(fn)` is called on every request.** This is not cheap — it walks the function annotations. For a hot tool like `search_database` that's called 50 times per second, this adds measurable overhead. **Cache it** with `lru_cache` keyed on `id(fn)`, or compute it once at decoration time.

2. **Filtering silently drops unknown kwargs.** If a DA accidentally passes `workspace="illd"` instead of `workspace_id="illd"`, the kwarg is silently dropped — the tool runs with the default workspace, returns wrong data, no error. This is the kind of bug that wastes hours of debugging time.

   The fix is "strict-mode" filtering: emit a WARNING for any dropped kwarg.

**Recommendation:**
```python
def with_session_routing(tool_name: str):
    def decorator(fn):
        sig = inspect.signature(fn)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        accepted_params = set(sig.parameters)

        @wraps(fn)
        async def wrapper(*args, session_id=None, **kwargs):
            ... # session routing logic unchanged
            if not has_varkw:
                unknown = set(kwargs) - accepted_params
                if unknown:
                    logger.warning("Tool %s received unknown kwargs: %s — dropped", tool_name, unknown)
                kwargs = {k: v for k, v in kwargs.items() if k in accepted_params}
            ...
```

Effort: 30 min.

---

### F-CB-07 🟠 — `query_mode = query_mode` self-assignment and similar no-ops appear in many tool handlers

**Evidence:** verbatim, in multiple tool handlers:

```python
@mcp.tool()
@with_session_routing("rlm_plan_preview")
async def rlm_plan_preview(query, ..., session_id=None, query_mode=None, ...):
    ...
    try:
        query_mode = query_mode  # ←  self-assignment
        # Note: RLM's plan_preview internally determines strategy
        ...
```

This pattern appears in `rlm_plan_preview`, `rlm_orchestrate`, `validate_api_usage`, `detect_polling_requirements`, and `search_database`. The `query_mode = query_mode` line is a **no-op** that suggests the original code had `query_mode = something_else`, then was edited and the right-hand side was reduced to the parameter name. Or it was meant to be `query_mode = kwargs.get("query_mode")`.

It does no harm, but:
- It's noise that future readers will misinterpret.
- It hints at a refactor that didn't finish cleanly.
- A linter (`pylint`, `ruff`) would flag this as `useless-statement` (W0104).

**Recommendation:** Delete the line. Run `ruff check --select W0104` (or equivalent) on the whole file to catch all instances at once.

Effort: 5 min, but worth doing as part of a cleanup pass.

---

### F-CB-08 🟠 — Tool handlers catch `Exception` broadly and lose the typed error in the envelope

**Evidence:** typical pattern:
```python
try:
    ...
    return _ok(...)
except FileNotFoundError as e:
    return _err("INVALID_INPUT", str(e))
except ValueError as e:
    return _err("INVALID_INPUT", str(e))
except Exception as exc:
    return _err("INTERNAL_ERROR", str(exc))
```

For Sprint 25 maturity, `Exception` catch-all has these issues:

1. **`KeyboardInterrupt` and `SystemExit`** are NOT caught (they're `BaseException`), so this is OK in that respect. But `MemoryError`, `RuntimeError`, **everything else** gets folded into `INTERNAL_ERROR`. An auditor looking at error logs cannot distinguish:
   - Neo4j is down → connection error
   - Cypher syntax bug → schema/query error
   - LLM call failed → upstream service error
   - User input was malformed → validation error
   
   All show up as `INTERNAL_ERROR`. This is not actionable.

2. **`asyncio.CancelledError`** is caught and reported as `INTERNAL_ERROR`. In Python 3.8+, `CancelledError` is a `BaseException`, but in 3.7 it's `Exception` — the codebase targets 3.12 per Dockerfile, but it's still a pattern to be mindful of. **Cancellation is not an error**; it should re-raise.

3. **Common typed errors are not handled:**
   - `neo4j.exceptions.ServiceUnavailable` → should be `BACKEND_UNAVAILABLE`
   - `qdrant_client.http.exceptions.UnexpectedResponse` → should be `BACKEND_UNAVAILABLE`
   - `requests.exceptions.Timeout` (from RLM's LLM call) → should be `UPSTREAM_TIMEOUT`
   - `asyncio.TimeoutError` → should be `INTERNAL_TIMEOUT`

The existing `_classify_and_record_error` helper does a very similar job — but only for **Prometheus metrics**, not for the `_err()` envelope. The envelope still shows `INTERNAL_ERROR` to the caller.

**Recommendation:**
```python
ERROR_CLASSIFICATION = (
    (asyncio.CancelledError, None, lambda e: None),  # re-raise
    (neo4j.exceptions.ServiceUnavailable, "BACKEND_UNAVAILABLE", lambda e: f"Neo4j unavailable: {e}"),
    (qdrant_client.http.exceptions.UnexpectedResponse, "BACKEND_UNAVAILABLE", lambda e: f"Qdrant: {e}"),
    (asyncio.TimeoutError, "INTERNAL_TIMEOUT", lambda e: "Operation timed out"),
    (FileNotFoundError, "INVALID_INPUT", lambda e: str(e)),
    (ValueError, "INVALID_INPUT", lambda e: str(e)),
    # default fall-through
)

def _err_from_exc(exc: Exception, component: str) -> str:
    for exc_type, code, msg_fn in ERROR_CLASSIFICATION:
        if isinstance(exc, exc_type):
            if code is None:
                raise  # re-raise CancelledError, etc.
            _classify_and_record_error(exc, component)
            return _err(code, msg_fn(exc))
    _classify_and_record_error(exc, component)
    return _err("INTERNAL_ERROR", str(exc))
```
Then in handlers: `except Exception as e: return _err_from_exc(e, component="search")`.

Effort: 1 day to retrofit across all ~60 handlers.

---

### F-CB-09 🟠 — `sandbox_upload` writes to `/tmp/sandbox_{session_id}` — uncontrolled, no namespace isolation, no quota

**Evidence:**
```python
tmp_dir = Path(f"/tmp/sandbox_{session_id}")
try:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ...
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
```

Issues:

1. **No path containment:** `session_id` is user-controlled (passed to `session_start`). A session_id like `"../etc"` produces `tmp_dir = "/tmp/sandbox_../etc"`, which `Path(...)` normalizes to `/etc`. `mkdir(parents=True, exist_ok=True)` won't recreate `/etc` (it exists), but writing files into it via `tmp_path = tmp_dir / filename` could land files in arbitrary directories: `tmp_path = Path("/tmp/sandbox_../etc") / "passwd"` → `Path("/etc/passwd")` (the path normalization happens differently in `Path` — actually `pathlib` is OK here because it doesn't auto-normalize `..` until `.resolve()`, but the principle stands: **session_id is an attacker-controllable string used to construct a filesystem path**).

   Then in `session_end`: `shutil.rmtree(tmp_dir, ignore_errors=True)` — same path. If the path normalization differed even slightly, this could *delete* `/etc` (silenced by `ignore_errors=True`).

2. **No size quota:** `MAX_FILE_SIZE = 10 MB` per file (good), but no aggregate cap per session. A malicious DA could upload 1000 × 10MB files = 10 GB to `/tmp` and exhaust container disk.

3. **`/tmp` is the wrong directory:** in K8s, `/tmp` is part of the container's writable layer (unless explicitly mounted as `emptyDir`). Disk pressure on a node can OOM/evict the pod. K8s best practice is to use `/var/run/aice/sandbox` mounted as `emptyDir` with `sizeLimit`.

**Recommendation:**
1. **Validate session_id strictly:** `if not re.match(r"^[A-Za-z0-9_-]+$", session_id): raise ValueError(...)`. Apply at `session_start`.
2. **Use a configurable, controlled directory:** `SANDBOX_TMP_ROOT = Path(os.environ.get("SANDBOX_TMP_ROOT", "/var/run/aice/sandbox"))`. `tmp_dir = SANDBOX_TMP_ROOT / f"sandbox_{session_id}"`, then `if not tmp_dir.resolve().is_relative_to(SANDBOX_TMP_ROOT.resolve()): raise ValueError(...)`.
3. **Enforce aggregate quota:** track total bytes written per session, refuse uploads beyond `SANDBOX_TOTAL_BYTES` (default 100 MB).
4. **K8s deployment.yaml:** add `volumeMounts: - mountPath: /var/run/aice/sandbox; volumes: - emptyDir: { sizeLimit: 1Gi }`.

Effort: 1 day.

---

### F-CB-10 🟠 — `_detect_module_from_names` heuristic produces wrong module names that then determine traceability scope

**Evidence:**
```python
def _detect_module_from_names(node_names: List[str]) -> str:
    """Heuristic: extract module name from function names (e.g. Adc_Init → Adc)."""
    for name in node_names:
        if "_" in name:
            prefix = name.split("_")[0]
            if len(prefix) >= 2:
                return prefix
    return "unknown"
```

Used in `sandbox_upload` when caller didn't explicitly provide `module=`:
```python
detected_module = module or _detect_module_from_names(all_node_names)
```

Issues:
1. The heuristic returns the **first** prefix it finds. If the file has `Std_ReturnType`, then `IfxCan_Node_init`, it returns `"Std"` — completely wrong (and `Std` is not even a module, it's an AUTOSAR convention prefix).
2. For iLLD, function names start with `IfxXxx_...` — the prefix is `IfxCan`, `IfxAdc`, etc. Not `Can` or `Adc`. So the prod-overlay query against `n.module = "IfxCan"` will match nothing, `prod_nodes_loaded = 0` again.
3. Returns `"unknown"` for any function without underscore — and then queries Neo4j for `n.module = "unknown"` (definitely matches nothing).

This works **for MCAL only**, where function names are `Adc_StartGroupConversion`. For iLLD it's broken. Given that iLLD is the reference CIA workspace, this means CIA's sandbox feature is doubly broken (combined with F-CB-01 the feature is functionally dead in iLLD).

**Recommendation:**
1. Take the prefix mode/most-frequent across all names, not the first.
2. Strip `Ifx` prefix when present: `prefix = re.sub(r"^Ifx", "", prefix)` for iLLD compatibility.
3. **Refuse to proceed with `"unknown"` module** when `trace_depth > 0` — it's a configuration error that should fail fast: `if detected_module == "unknown": return _err("INVALID_INPUT", "Could not detect module from uploaded files; provide module= explicitly.")`.
4. Document in the `sandbox_upload` docstring: *"If module is not provided, AICE attempts to detect it from function name prefixes. Pass `module=` explicitly for iLLD content (e.g., `module='Can'` not `module='IfxCan'`)."*

Effort: 30 min.

---

### F-CB-11 🟠 — `_warmup` is sequential where the docstring claims it's parallel; the documented design from `Perf_improvements.md` was never implemented

**Evidence:**
```python
async def _warmup():
    """Eagerly initialize heavy resources so the first user request is fast."""
    t0 = _time.monotonic()
    logger.info("[Warmup] Starting eager resource initialization...")

    # 1. Qdrant client (TLS handshake)
    try:
        _get_qdrant()
        logger.info("[Warmup] Qdrant client ready (%.1fs)", _time.monotonic() - t0)
    except Exception as e:
        logger.warning("[Warmup] Qdrant client failed: %s", e)

    # 2. SearchService per profile (includes Neo4j driver)
    for profile in ("illd", "mcal"):
        try:
            svc = _get_search_service(profile)
            if svc:
                # 3. Force-load the SentenceTransformer embedding model
                svc._embed_query("warmup")
                logger.info("[Warmup] SearchService('%s') + embedding model ready (%.1fs)",
                            profile, _time.monotonic() - t0)
        except Exception as e:
            logger.warning("[Warmup] SearchService('%s') failed: %s", profile, e)
    # 4. ...
```

`Perf_improvements.md` says: *"`_warm_backends()` parallel-inits Neo4j (illd + mcal), Qdrant, Redis, PostgreSQL, CacheService with per-service error handling."* — claiming `asyncio.gather()` parallelism.

The actual code uses **sequential** for-loop. Each step waits for the prior. So:
- Qdrant TLS handshake (~500ms) — sequential.
- illd SearchService (Neo4j connect + Qdrant probe + embedding model load ~2s) — sequential.
- mcal SearchService (~2s) — sequential.

Total: ~4-5s warmup. With `asyncio.gather`, this could be ~2s. For K8s pod readiness probes that wait on `/health`, this matters — slower warmup = longer rollouts.

Master Gaps Family E PERF-02 claims "FIXED" with parallel init. Code says otherwise. (Same drift pattern as F-CA-S02 where Master Gaps says fixed but source disagrees.)

**Recommendation:**
```python
async def _warmup():
    t0 = time.monotonic()
    
    async def _warm_qdrant():
        await asyncio.to_thread(_get_qdrant)
    
    async def _warm_search(profile):
        svc = await asyncio.to_thread(_get_search_service, profile)
        if svc:
            await asyncio.to_thread(svc._embed_query, "warmup")
    
    results = await asyncio.gather(
        _warm_qdrant(),
        _warm_search("illd"),
        _warm_search("mcal"),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[Warmup] step failed: %s", r)
    logger.info("[Warmup] Complete in %.1fs", time.monotonic() - t0)
```

Effort: 30 min.

---

## 3. Medium Findings

### F-CB-12 🟡 — Path bootstrapping mutates `sys.path` at module import time; combined with the FastMCP shim, this is a fragile import sequence

**Evidence:**
```python
_MCP_DIR = Path(__file__).resolve().parent          # mcp/core/
_REPO_ROOT = _MCP_DIR.parents[1]                    # repo root
_SRC_DIR = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

Then earlier:
```python
# ── SDK import shim ────────────────────────────────────────────────────────
_repo_root = str(Path(__file__).resolve().parents[2])
_saved_paths: list[tuple[int, str]] = []
for _i in range(len(sys.path) - 1, -1, -1):
    if _abs == os.path.normcase(_repo_root):
        _saved_paths.append((_i, sys.path.pop(_i)))
... # remove local mcp from sys.modules
FastMCP = _il.import_module("mcp.server.fastmcp").FastMCP       # installed SDK
... # restore sys.path and sys.modules
```

This is **two separate sys.path manipulations** in the same file: one to evict + restore for the FastMCP shim, one to add `_REPO_ROOT` and `_SRC_DIR` for `src.*` imports.

Pass 2 F-A10 already addresses the FastMCP shim (rename `mcp/` → `aice_mcp/` to remove the shim entirely). When that lands, this whole top section becomes ~5 lines instead of ~50.

In the meantime: the bootstrapping is **order-sensitive**. If the `mcp/__init__.py` were ever to import something from `src.*`, the import would fail because `_SRC_DIR` is added *after* the FastMCP shim runs. It's not currently broken, but a single touch in the wrong place breaks it.

**Recommendation:** wait on Pass 2 F-A10 (the rename eliminates this cluster of fragility entirely). Until then, add a comment block explaining the import order is load-bearing.

Effort: 0 (deferred to F-A10).

---

### F-CB-13 🟡 — `os.environ.get("MCP_API_KEY", "")` fallback for stdio transport: silent acceptance of empty key

**Evidence:**
```python
def _authorize(tool_name: str, **kw) -> Optional[str]:
    api_key = _current_api_key.get("") or os.environ.get("MCP_API_KEY", "")
    if not api_key:
        return _err_permission_denied("No API key provided ...")
```

This is well-handled — empty key produces a deny. But the comment above (in earlier snippets):
```python
# For stdio transport MCP_API_KEY env var is the fallback.
```

If a developer runs `python -m mcp.core.mcp_server` locally without `MCP_API_KEY` set, every tool call returns PERMISSION_DENIED. The error message says "set MCP_API_KEY env var or send Authorization header" — but a developer trying to debug doesn't know which keys exist (they're in `api_keys.yaml`, K8s-mounted in production).

This isn't a bug — it's correct security posture. But the **dev experience** is rough: there's no way to "I just want to see if the server starts and tools work." A local-dev mode that accepts `MCP_API_KEY=dev` and maps it to a hardcoded `developer` tier principal would help.

**Recommendation:** add a `key-dev-local` entry to `api_keys.yaml` with `developer` role (commented as "FOR LOCAL DEV ONLY — never deploy"). Document it in `MCP_QUICKSTART.md`.

Effort: 30 min.

---

### F-CB-14 🟡 — `_load_neo4j_profile_config` swallows all exceptions silently

**Evidence:**
```python
def _load_neo4j_profile_config(profile: str) -> Optional[Dict[str, Any]]:
    """Load Neo4j connection config for *profile* from storage_config.yaml."""
    try:
        ...
        from src.HybridRAG.code.neo4j_manager import get_instance_config
        cfg = get_instance_config(profile)
        return {"uri": cfg.uri, "username": cfg.username, ...}
    except Exception as e:
        logger.debug("Could not load neo4j config for profile '%s' from storage_config: %s", profile, e)
        return None
```

`logger.debug` for a config-loading failure is the wrong level. Operators set log level to INFO in production; DEBUG won't fire. So if `storage_config.yaml` is malformed, this returns `None` silently. The fallback path then tries env-var-based config (which I haven't seen in the snippets but presumably exists), and if **that** fails, `_get_neo4j` returns `None`, and every search returns "SearchService unavailable" with no clue why.

**Recommendation:** WARNING level with the exception, plus a Prometheus event.

Effort: 5 min.

---

### F-CB-15 🟡 — `_NoOpMetric.labels(self, **kw)` returns `self` — but doesn't return the same object as the labelled metric would

**Evidence:**
```python
class _NoOpMetric:
    def labels(self, **kw): return self
    def inc(self, amount=1): pass
    def dec(self, amount=1): pass
    def set(self, value): pass
    def observe(self, value): pass
```

This is a fine no-op. But the `prometheus_client` real implementation returns a *labeled child metric* from `.labels()` — a different object than the parent. Consumer code that holds onto a labeled child:
```python
labeled = TOOL_REQUESTS_TOTAL.labels(tool="search_database", status="ok")
labeled.inc()
labeled.inc()  # incrementing same child
```
With the no-op, `labeled` is the same `_NoOpMetric` instance, no-op `.inc()`. Functionally fine. But if anyone uses `.time()` (a context manager that the real Histogram has):
```python
with TOOL_REQUEST_DURATION.labels(tool="x").time():
    ...
```
That blows up because `_NoOpMetric` doesn't expose `.time()`. A few places in the codebase might use this — check `src/Observability/metrics.py`.

**Recommendation:** add `.time()` to `_NoOpMetric` returning a no-op context manager:
```python
@contextmanager
def time(self):
    yield
```

Effort: 5 min.

---

### F-CB-16 🟡 — `_finish_tool` returns metrics keyed only by `tool` and `status`, but `query_mode` (sandbox vs hybrid) is invisible

For `search_database`, the request flows through `with_session_routing` and `query_mode` becomes `"sandbox"` or `None` (for hybrid). The Prometheus metric records `tool=search_database` regardless. If 30% of search_database calls are sandbox-routed and have completely different latency profiles, the Grafana dashboard shows a bimodal latency distribution that's confusing to interpret.

**Recommendation:** add a `mode` label:
```python
TOOL_REQUESTS_TOTAL.labels(tool=name, status=status, mode=_query_mode_ctx.get("hybrid")).inc()
```

Effort: 1 hour (but only worth it if the Grafana dashboard is actually being looked at).

---

### F-CB-17 🟡 — `_get_feedback_sink()` PatternStore wiring is deeply nested try/except — covers 4 distinct failure modes with one log line

**Evidence:**
```python
def _get_feedback_sink():
    global _feedback_sink
    if _feedback_sink is None:
        try:
            from src.ReviewGate.confidence import FeedbackSink
            pg = _get_postgres_client()

            pattern_store = None
            pattern_index = None
            try:
                from src.MemoryLayer.memory.semantic_memory import PatternStore, PatternIndex, Embedder
                embedder = Embedder()
                neo4j_driver = _get_neo4j()
                if neo4j_driver:
                    pattern_store = PatternStore(neo4j_driver=neo4j_driver, embedder=embedder)
                    ...
                qdrant = _get_qdrant()
                qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
                if qdrant:
                    pattern_index = PatternIndex(qdrant_url=qdrant_url, embedder=embedder)
                    ...
            except ImportError as ie:
                logger.info("[MCP] Semantic memory not available — learning loop disabled: %s", ie)
            except Exception as e:
                logger.warning("[MCP] Learning loop init failed: %s", e)
            ...
```

Per Pass 1 F-D05 / Pass 2 §8: `PatternStore(neo4j_driver=...)` is wrong (PatternStore is Qdrant-only). When this call site runs:
- `PatternStore.__init__` doesn't accept `neo4j_driver` → raises `TypeError` → caught by `except Exception` → "Learning loop init failed" WARNING logged.
- The whole nested block fails, `pattern_store = None` and `pattern_index = None`.
- `FeedbackSink` is constructed without learning-loop wiring. **The learning loop has been silently disabled in production since this code shipped.**

This is the same finding as F-D05 but with operational context: the learning loop is one of the value propositions of AICE, and it's been off because of a kwarg mismatch hidden by an over-broad except.

**Recommendation:** apply Pass 2 §8 fix (drop `neo4j_driver=` kwarg), AND tighten the except: catch `ImportError` separately from `TypeError`/`AttributeError` so that *misconfigured wiring* is logged as ERROR (not WARNING) and a Prometheus gauge `aice_learning_loop_active` is set.

Effort: 1 hour (combine with F-D05).

---

### F-CB-18 🟡 — Multiple tool docstrings claim "Default 60 seconds" or "Default 3600s" but the parameter has no default visible

In several tool handlers (e.g., `session_start`):
```python
async def session_start(
    session_id: str, assistant_name: Optional[str] = None,
    module_context: Optional[str] = None,
) -> str:
    """...
    All sessions have a fixed TTL of 3600 seconds (1 hour).
    ...
    """
```

The docstring says "fixed TTL of 3600 seconds." The parameter list doesn't take a `ttl_seconds` argument, so it really *is* fixed. But `MCP_QUICKSTART.md` example shows `"ttl_seconds": 3600` being passed — implying the tool accepts it. **One of these is wrong.** Either:
1. The tool accepts `ttl_seconds` and the signature in source is incomplete (likely — there are kwargs not shown).
2. The tool ignores `ttl_seconds` and the QUICKSTART example is misleading.

Without seeing the full `session_start` signature, I can't tell. But the doc/code mismatch is the same class as Pass 1 F-D03.

**Recommendation:** verify the signature; align docstring + param list + QUICKSTART.

Effort: 15 min.

---

### F-CB-19 🟡 — `health_check` does the work synchronously inside an async tool → blocks the event loop on a slow Neo4j

**Evidence:**
```python
@mcp.tool()
async def health_check(verbose: bool = False, include_test_query: bool = False, ...) -> str:
    ...
    def _run_health_check():
        ...
        with drv.session() as s:
            s.run("RETURN 1").single()
            ...
```

The function `_run_health_check` is sync. I can't see the call site that runs it, but if it's called directly (not via `await asyncio.to_thread(_run_health_check)`), then a slow Neo4j (or Qdrant TLS handshake retry) blocks the event loop **for all concurrent requests**. Health checks are typically called by k8s liveness probes — when the backend is sick is exactly when you want them to NOT block other requests.

**Recommendation:** verify the call site wraps in `asyncio.to_thread`. If not, fix.

Effort: 15 min.

---

### F-CB-20 🟡 — `INGESTION_FILES_TOTAL` Prometheus counter exists but ingestion tools were removed from MCP — counter is dead

Per Pass 1, ingestion tools (`ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, `ingest_repository`) were **removed from MCP registration** in Plan 2 Phase 2 (the `# @mcp.tool()` comments). The underlying `IngestionService` is still callable, but only via `sandbox_upload`. The `INGESTION_FILES_TOTAL` metric is imported and would-be-incremented somewhere in IngestionService — but the operational view of "files ingested per minute" is now noise (it shows zero or sandbox-only data).

This isn't broken — but it's another piece of drift. The metric is defined in `metrics.py`, listed in AICE-PROM-003, but reflects sandbox uploads only.

**Recommendation:** rename to `aice_sandbox_files_ingested_total{module=...}` and document it as such. Or keep but add a `mode="sandbox"|"prod"` label.

Effort: 30 min.

---

## 4. Low Findings

### F-CB-21 🟢 — Module docstring claims "56 Tools across 13 categories"; Sprint 25 baseline is 62 across 14

(Repeats Pass 1 F-D06, F-D14 at this specific location.) Update the file header.

Effort: 5 min.

---

### F-CB-22 🟢 — Multiple `from src.X import Y` mid-function imports

E.g., in `_get_feedback_sink`:
```python
def _get_feedback_sink():
    if _feedback_sink is None:
        try:
            from src.ReviewGate.confidence import FeedbackSink
            ...
```

Mid-function imports are fine for **lazy imports** (avoid loading expensive modules at startup), and that's clearly the intent here. But there's no comment saying so. A future contributor moving these to module-top because "imports belong at the top per PEP 8" could break startup time.

**Recommendation:** add a single-line comment per cluster: `# Lazy import — avoids loading Neo4j/torch/etc. at module load time.`

Effort: 10 min.

---

### F-CB-23 🟢 — `_finish_tool("ok")` is called *inside* `_ok()` and `_err()` — this means the timer measures from `_set_tool_name` (wherever that is) to JSON serialization. JSON serialization is included.

Time spent serializing `json.dumps(...)` is included in `tool_request_duration`. For typical tools this is microseconds, but for tools that return large payloads (build_traceability_matrix in HTML format, or visualize_subgraph), the JSON serialization could be 10-50ms — which gets attributed to the tool's "latency" rather than the network/serialization layer.

This is a minor reporting issue.

**Recommendation:** measure separately or document the metric definition: *"includes JSON serialization."*

Effort: 0 (acceptable trade-off).

---

### F-CB-24 🟢 — `asyncio.run(_serve_with_warmup())` in `main()` — no graceful shutdown of the warmup task on SIGTERM

`mcp/app.py` registers SIGTERM handlers, but `_serve_with_warmup` runs the warmup, then `uvicorn.Server.serve()`. If SIGTERM arrives **during warmup** (e.g., an aggressive K8s rolling deploy), the signal is delivered to `uvicorn` once it's running, but **during warmup it has no handler installed**. The default Python behavior is to terminate immediately, leaving Cerbos PDP subprocess running.

Edge case (only matters during deploys). Effort: 30 min for a clean fix; not urgent.

---

## 5. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CB-01 (Cypher param syntax), F-CB-02 (silent service init), F-CB-03 (Neo4j driver health) | 4 days |
| **P1 (next sprint)** | F-CB-04 (correlation_id), F-CB-05 (verify _tool_name_ctx setter), F-CB-08 (typed errors), F-CB-09 (sandbox path containment), F-CB-10 (module detection), F-CB-11 (warmup parallelism), F-CB-17 (learning loop wiring with F-D05) | 5–6 days |
| **P2** | F-CB-06 (kwargs filtering), F-CB-07 (no-op assignments), F-CB-13–F-CB-20 | 2 days |
| **P3** | F-CB-21–F-CB-24 (cosmetic) | 1 hour |

**Total cluster effort:** ~11 person-days. P0 alone is 4 days and addresses one feature-breaking bug (F-CB-01) and the silent-failure foundation (F-CB-02, F-CB-03).

---

## 6. Cross-cluster pattern: silent-failure dashboard

Cluster A ended with a "silent failure dashboard" suggestion. Cluster B confirms the same theme: **`mcp_server.py` has more silent-failure surfaces than any other file in the codebase** (F-CB-01, -02, -03, -05, -10, -11, -14, -17 are all variations of "silently does the wrong thing without alerting"). The recommendation from Cluster A — a single Grafana panel that surfaces all of these via Prometheus gauges — is even more strongly indicated after this pass.

The full set of gauges that would need to exist:

| Gauge | Source | Triggers when |
|---|---|---|
| `aice_auth_registry_loaded` | F-CA-A07 | api_keys.yaml missing or empty |
| `aice_cerbos_up` | F-CA-A04 | Cerbos PDP unreachable for 30s |
| `aice_vector_search_available` | F-CA-S04 | sentence-transformers fails to load |
| `aice_service_up{service=...}` | F-CB-02 | Any `_get_*` returns None |
| `aice_neo4j_pool_health{profile=...}` | F-CB-03 | verify_connectivity fails |
| `aice_learning_loop_active` | F-CB-17 | PatternStore wiring fails |
| `aice_sandbox_overlay_active` | F-CB-01 | TraceabilityPuller returns 0 nodes when expected |

Effort to instrument all 7: ~1 day. This is the single highest-ROI operational fix in the entire review series so far.

---

## 7. What I deliberately did not flag

- **`mcp_server.py` size** — Pass 2 F-A04 covers the architectural split.
- **Tool count drift in module docstring** — Pass 1 F-D06.
- **FastMCP shim** — Pass 2 F-A10.
- **MemoryLayer→HybridRAG inversion** — Pass 2 F-A03.
- **PatternStore Qdrant-only** — Pass 1 F-D05 + Pass 2 §8.
- **Cerbos policy duplicates** — Pass 1 F-D02.
- The 30+ `_get_*` lazy-init stubs are flagged as a *class* (F-CB-02) rather than individually.

---

**End of Cluster B.** Ready to proceed to Cluster C (Retrieval Brain — RLM + ContextBuilder + KnowledgeIntelligence) on your signal.

Cluster C will go deep on the RLM orchestration logic (planner, executor, synthesizer), the ContextBuilder 10-slot algorithm with budget redistribution, and the KnowledgeIntelligence service that powers the API/dependency/traceability tools. Expect 18–22 findings.

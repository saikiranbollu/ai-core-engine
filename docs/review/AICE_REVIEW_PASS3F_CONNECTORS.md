# AICE Review — Pass 3 / Cluster F: Connectors

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:** the 4 external-system connectors:
- `src/IngestionPipeline/Connectors/JamaConnector.py` — Jama REST API (requirements management)
- `src/IngestionPipeline/Connectors/PolarionConnector.py` — Polarion Web Tools REST API (ALM)
- `src/IngestionPipeline/Connectors/JenkinsConnector.py` — Jenkins via `jenkinsapi` library
- `src/IngestionPipeline/Connectors/BitbucketConnector.py` — Bitbucket Server REST API (source code)

Plus closely-coupled callers:
- `src/HybridRAG/code/KG/dependency_fetcher.py` — uses BitbucketConnector to download cross-module headers
- `src/HybridRAG/code/KG/fetch_jama_relationships.py` — uses JamaConnector for relationship fetching
- `src/IngestionPipeline/Parsers/header_fetcher.py` — uses BitbucketConnector with credential discovery

**Excludes:** tests/. Findings already reported in Pass 1, Pass 2, or Clusters A–E referenced but not re-stated.

---

## 0. Summary

The connectors are the **edge** of AICE — every interaction with Jama, Polarion, Jenkins, or Bitbucket goes through them. They're also the most **environmentally-sensitive** code in the repo: they depend on credentials, network reachability, TLS configuration, and API stability of external systems Anthropic doesn't control.

This cluster has the cleanest code in Pass 3 by some margin. The connectors share an obvious common architecture (data classes + retry wrapper + sync-state persistence) and the patterns are consistent across all four. That said, there are real issues:

1. **`verify_ssl=False` is the default fallback in two places** (`dependency_fetcher.py` and `header_fetcher.py`). For an automotive system handling Jama IDs and source code, that's a meaningful security gap — anyone on the corporate network can MITM the connector traffic. (See F-CF-X01.)

2. **Credential handling is inconsistent across connectors.** Jama uses Basic auth (api_key/api_secret), Polarion uses Bearer JWT, Jenkins uses Basic auth (username/api_token), Bitbucket supports either. None of the connectors store credentials with redaction-aware logging or zero them out on close. Some `__repr__`s might leak via debug output.

3. **Retry logic is duplicated four times** with subtle variations. JamaConnector and PolarionConnector use almost-identical `_request` methods with retry; JenkinsConnector wraps a third-party library's retry; BitbucketConnector has its own. No shared helper. Same anti-pattern as Clusters D (3 retry impls in KG construction) and E (LLM retry duplicated across PDF parser + SWA parser + RLM).

4. **`token` parameter for Polarion is a long-lived JWT with no refresh path.** When the token expires (typically 1-24 hours), every subsequent call fails with 401, the retry loop doesn't refresh, and the connector goes silent. No mechanism analogous to the GPT4IFX `token_manager.get_token(force_refresh=True)`. (See F-CF-P01.)

**Findings count:** 17 (2 Critical, 5 High, 7 Medium, 3 Low)

| File / area | C | H | M | L | Total |
|---|---:|---:|---:|---:|---:|
| Cross-connector (X##) | 1 | 2 | 2 | 0 | 5 |
| `JamaConnector.py` | 0 | 1 | 1 | 1 | 3 |
| `PolarionConnector.py` | 1 | 1 | 1 | 1 | 4 |
| `JenkinsConnector.py` | 0 | 1 | 1 | 0 | 2 |
| `BitbucketConnector.py` | 0 | 0 | 2 | 1 | 3 |

Severity criteria as in earlier clusters.

---

## 1. Cross-Connector Findings

### F-CF-X01 🔴 — `verify_ssl=False` is the default fallback in callers; production traffic to Bitbucket may run unencrypted (cert-not-validated)

**Evidence:**

`src/HybridRAG/code/KG/dependency_fetcher.py::_make_connector`:
```python
return BitbucketConnector(
    base_url=BITBUCKET_BASE_URL,
    project=BITBUCKET_PROJECT,
    repo=repo,
    username=os.environ.get("IFX_USERNAME"),
    password=os.environ.get("IFX_PASSWORD"),
    verify_ssl=False,    # ← hardcoded
    ref="master",
)
```

`src/IngestionPipeline/Parsers/header_fetcher.py::_get_connector`:
```python
conn = BitbucketConnector(
    base_url=_BASE_URL,
    project=_PROJECT,
    repo="aurix3g_sw_mcal_tc4xx_platform",
    token=token,
    username=username,
    password=password,
    ref="master",
    verify_ssl=False,    # ← hardcoded
)
```

In both call sites, `verify_ssl=False` is **hardcoded, not configurable, not environment-dependent**. The connectors themselves default to `verify_ssl=True` (correctly), but every caller overrides this to False.

**Why this is Critical, not just High:**

1. **Bitbucket credentials in flight.** Both call sites pass `IFX_USERNAME` and `IFX_PASSWORD` (the user's IFX SSO credentials, not just a service account). With `verify_ssl=False`, the underlying httpx client doesn't validate the cert chain — an attacker on the corporate LAN with a fake cert can MITM the connection and capture the credentials.

2. **Source code in flight.** The connector pulls iLLD source headers (`*.h` files) — IP-sensitive content. Even if the credentials weren't on the wire, the source code is.

3. **Hardcoded silently bypassing security.** This is in two files, in different parts of the codebase, with no log warning, no env-var override, no documentation explaining why. A reviewer reading `dependency_fetcher.py` cannot tell if `verify_ssl=False` is deliberate (e.g., self-signed Infineon-internal cert) or oversight.

4. **The Bitbucket internal cert is presumably available** — Infineon has an internal CA bundle (the same one used by GPT4IFX, per `pdf_pipeline.py::_CA_BUNDLE_PATH`). Switching to that CA bundle would be the right fix, not bypassing verification entirely.

**Recommendation:**

1. **Default `verify_ssl=True` in both call sites.** Make the override explicit:
   ```python
   verify_ssl_env = os.environ.get("BITBUCKET_VERIFY_SSL", "true").lower()
   verify_ssl = verify_ssl_env in ("true", "1", "yes")
   if not verify_ssl:
       logger.warning("[BitbucketConnector] SSL verification DISABLED via BITBUCKET_VERIFY_SSL — credentials and source code in flight are not protected")
   ```

2. **Use the Infineon CA bundle** by default:
   ```python
   ca_bundle = Path(__file__).resolve().parents[N] / "HybridRAG" / "code" / "ca-bundle.crt"
   if ca_bundle.is_file():
       verify_ssl = str(ca_bundle)  # httpx accepts a path
   else:
       verify_ssl = True  # use system trust store
   ```
   This is the same pattern used in `pdf_pipeline.py` for GPT4IFX. Apply consistently.

3. **Audit all connector instantiations** for hardcoded `verify_ssl=False` overrides. Same for Jama (`fetch_jama_relationships.py` may have the same pattern — verify) and Polarion.

4. **Make the connector refuse `verify_ssl=False` in production posture:**
   ```python
   def __init__(self, ..., verify_ssl=True):
       if not verify_ssl and os.environ.get("AICE_ALLOW_INSECURE_TLS") != "1":
           raise ValueError("verify_ssl=False requires AICE_ALLOW_INSECURE_TLS=1 (development only)")
   ```

This is the **highest single-finding security severity** in the entire review series so far. Effort: 4 hours for the fix; 1 day including audit + CA bundle wiring.

---

### F-CF-X02 🟠 — Credentials are stored in plaintext on instance attributes (`self._token`, `self._api_key`, `self._password`) with no zeroing on close

**Evidence:**

`PolarionConnector.__init__`:
```python
self._base_url = base_url.rstrip("/")
self._token = token            # ← plaintext JWT, lives forever
```

`JamaConnector.__init__`:
```python
self._auth = httpx.BasicAuth(username=api_key, password=api_secret)
# api_key and api_secret are captured by the BasicAuth object
```

`JenkinsConnector.__init__`:
```python
self._username = username
self._api_token = api_token    # ← plaintext
```

`BitbucketConnector.__init__` (similar):
```python
self._token = token             # ← if Bearer
self._username = username
self._password = password       # ← if Basic
```

Issues:

1. **Long-lived in memory.** Once set, these values stay in the connector instance for the life of the MCP server process. Memory dumps (e.g., from a Python crash, gdb attach, or coredump) expose them.

2. **`__repr__` could leak.** `PolarionConnector.__repr__` (visible in the snippet) is safe — only prints `base_url` and `connected`. But `__dict__` is not redacted; any logging of `vars(connector)` or pickled state would expose credentials.

3. **`close()` doesn't zero credentials.** When the connector is closed, the values stay on the instance object until garbage collection.

4. **Logger format strings could leak.** I haven't seen all log calls but a future contributor doing `logger.debug("Initialized %s", repr(self))` could expose state if `__repr__` is changed.

**Operational impact:** for a system that's about to face EU AI Act / ASPICE audits, **storing high-privilege credentials (Polarion JWT has full ALM read access; Jenkins API token can trigger builds; Bitbucket token can read all source) as plaintext attributes** on long-lived objects is a finding an auditor will flag.

**Recommendation:**

1. Wrap credentials in a redacting type:
   ```python
   class _SecretStr:
       __slots__ = ("_value",)
       def __init__(self, value: str): self._value = value
       def get(self) -> str: return self._value
       def clear(self) -> None: self._value = ""
       def __repr__(self) -> str: return "_SecretStr(***)"
       def __str__(self) -> str: return "***"
   ```
2. Use it consistently:
   ```python
   self._token = _SecretStr(token)
   # When making a request:
   headers = {"Authorization": f"Bearer {self._token.get()}"}
   ```
3. **Zero on close:**
   ```python
   def close(self):
       self._client.close()
       self._token.clear()
   ```
4. Same treatment for `JamaConnector._auth` (which holds api_secret), `JenkinsConnector._api_token`, `BitbucketConnector._token` / `_password`.

Effort: 1 day for the helper + retrofit across all 4 connectors.

---

### F-CF-X03 🟠 — Four duplicate retry/backoff implementations across the connectors; one of them swallows non-retryable errors

**Evidence:**

`JamaConnector._request` retry:
```python
for attempt in range(1, self._max_retries + 1):
    try:
        response = self._client.request(method, path, ...)
        if response.status_code == 401: raise JamaAuthError(...)
        if 400 <= response.status_code < 500: raise JamaClientError(...)
        if response.status_code >= 500: raise JamaServerError(...)
        response.raise_for_status()
        return response.json()
    except (JamaAuthError, JamaClientError):
        raise  # non-retryable
    except (httpx.RequestError, JamaServerError) as exc:
        last_exc = exc
        wait = self._backoff_factor * (2 ** (attempt - 1))
        time.sleep(wait)
```
**Good** — raises auth/4xx errors immediately, retries 5xx and connection errors.

`JenkinsConnector._with_retry` (visible in snippets):
```python
def _with_retry(self, operation, fn, *args, **kwargs):
    """Non-retryable errors (authentication, not-found) are propagated immediately."""
```
The implementation isn't fully visible but the docstring promises the same shape.

`BitbucketConnector` has its own `_request` with retry (referenced via `self._request_raw`).

`PolarionConnector` (per the snippets) follows the same shape as Jama.

So **four near-identical retry helpers** in four files, each with subtle variations:
- Backoff base/multiplier may differ (Jama uses `1.0 * 2^(n-1)`; Jenkins delegates to its library).
- Which exceptions are non-retryable differs (Jama lists 2 classes; Jenkins lists 2 different ones).
- Logging format differs (operations have different label fields).

**Worse**: `JenkinsConnector` shows several `except (NoBuildData, Exception): pass` patterns:
```python
try:
    last_build = job.get_last_buildnumber()
except (NoBuildData, Exception):  # noqa: BLE001
    pass
```
**`Exception` catches everything** — `JenkinsConnectionError` from a flaky network would be swallowed and `last_build` silently stays None. The `# noqa: BLE001` (broad-except suppression) is the lint-warning being silenced. This is anti-pattern: silently dropping connection failures during job-info enumeration produces partially-populated `JobInfo` records that downstream consumers (KG ingestion of test runs) treat as "no builds yet" instead of "failed to query."

**Recommendation:**

1. **Extract a shared `RetryClient` helper** in `src/IngestionPipeline/Connectors/_retry.py`:
   ```python
   class RetryConfig:
       max_attempts: int = 3
       backoff_base: float = 1.0
       backoff_multiplier: float = 2.0
       max_backoff: float = 60.0
       jitter: float = 0.5
       non_retryable: Tuple[type, ...] = ()
       retryable: Tuple[type, ...] = (ConnectionError, TimeoutError)
   
   def with_retry(operation: str, fn, *args, config: RetryConfig, **kwargs):
       ...
   ```
2. Each connector instantiates with its own non-retryable types.
3. **Replace `except Exception` with explicit types** in JenkinsConnector. If `get_last_buildnumber` legitimately raises something other than `NoBuildData`, catch the specific class. Use `logger.warning("Could not fetch last build for %s: %s", job_name, exc)` instead of silent `pass`.

4. This pairs with **Cluster D F-CD-X02** (3 retry impls in KG construction) and **Cluster E F-CE-S01 / F-CC-R01** (LLM retry across PDF, SWA, RLM). The same shared helper covers all of them — call it `src/_common/retry.py` or similar.

Effort: 2 days (helper + retrofit across 4 connectors). Combined effort with Clusters C/D/E retry consolidations: ~3 days total.

---

### F-CF-X04 🟡 — Sync-state JSON files are written to user-controlled `sync_state_dir` without path containment

**Evidence:**

`JamaConnector` and `PolarionConnector` both have:
```python
self._sync_state_dir: Optional[Path] = (
    Path(sync_state_dir) if sync_state_dir else None
)
if self._sync_state_dir is not None:
    self._sync_state_dir.mkdir(parents=True, exist_ok=True)
```

`sync_state_dir` is a parameter, ultimately controllable by whoever instantiates the connector. In the current MCP code path, the value is set by configuration (storage_config.yaml), so not directly user-controlled. **But** if a future MCP tool exposes "set sync state directory" as a parameter (admin-tier), this becomes exploitable for path traversal.

`mkdir(parents=True, exist_ok=True)` will create arbitrary directories. The follow-up `state_path.write_text(...)` writes JSON files into them. An attacker passing `sync_state_dir = "../../etc"` could write JSON files anywhere on disk.

This is the **same class of issue** as Cluster B F-CB-09 (sandbox path containment) and Cluster A F-CA-I01 (ingest_file path traversal).

**Recommendation:** validate the path is under an allowlisted root:
```python
ALLOWED_SYNC_DIRS = [Path("/data/aice/sync_state").resolve()]

if sync_state_dir is not None:
    p = Path(sync_state_dir).resolve()
    if not any(p.is_relative_to(root) for root in ALLOWED_SYNC_DIRS):
        raise ValueError(f"sync_state_dir must be under {ALLOWED_SYNC_DIRS}")
```

Effort: 1 hour.

---

### F-CF-X05 🟡 — Connectors all do `httpx.Client(...)` once at construction; no automatic reconnection on persistent failure

**Evidence:**

`JamaConnector.__init__`:
```python
self._client = httpx.Client(
    base_url=f"{self._base_url}{self._api_root}",
    auth=self._auth,
    verify=ssl_context,
    timeout=timeout,
    headers={"Accept": "application/json"},
)
```

The httpx client is created once and lives until `close()`. httpx maintains its own connection pool and recovers from transient connection issues — but if the **TLS context** becomes invalid (e.g., the CA bundle is rotated mid-process), the client cannot recover; only a fresh `httpx.Client(...)` would pick up the new context.

For long-lived MCP server processes (potentially weeks of uptime in K8s), this is a real concern — corporate CA rotations happen on quarterly schedules.

**Same issue as Cluster B F-CB-03** (Neo4j driver doesn't health-check; a restart of the upstream service requires recreating the client).

**Recommendation:** add a `_recreate_client()` path triggered on:
- Repeated 401 with no other failure mode.
- TLS verification failure (`httpx.ConnectError` with SSL message).
- Periodic health refresh from apscheduler.

Effort: 4 hours.

---

## 2. `JamaConnector.py`

### F-CF-J01 🟠 — `get_projects()` walks the entire project list — no caching; every call is O(N) HTTP

**Evidence:**
```python
def get_project_id(self, project_name: str) -> Optional[int]:
    """Resolve a project name to its numeric ID."""
    for project in self.get_projects():
        fields = project.get("fields", {})
        if fields.get("name") == project_name:
            return int(project["id"])
    return None
```

`self.get_projects()` calls `_get_all_pages("/projects")`. For a Jama instance with hundreds of projects, this is multiple paginated HTTP calls **every time** `get_project_id` is invoked. If the connector is used to resolve 20 project IDs (one per module), that's 20 × full-pagination calls.

**Operational impact:** an ingestion run that touches 20 modules makes hundreds of redundant HTTP calls just to resolve names → IDs. The Jama server sees this as a small DDoS pattern; rate limiting can fire (Jama has request quotas).

**Recommendation:** cache project list with TTL:
```python
def get_projects(self) -> List[dict]:
    now = time.time()
    if self._projects_cache and now - self._projects_cache_ts < 300:  # 5 min
        return self._projects_cache
    self._projects_cache = self._get_all_pages("/projects")
    self._projects_cache_ts = now
    return self._projects_cache
```

Same approach for `get_item_types()`.

Effort: 1 hour.

---

### F-CF-J02 🟡 — `JamaItem.from_api_dict` silently coerces missing/wrong-typed fields to defaults

**Evidence:**
```python
@classmethod
def from_api_dict(cls, data: dict) -> "JamaItem":
    fields = data.get("fields", {})
    item_type = int(data.get("itemType", -1))
    ...
    version_raw = data.get("version", {})
    if isinstance(version_raw, dict):
        version = int(version_raw.get("versionNumber", -1))
    elif isinstance(version_raw, int):
        version = version_raw
    else:
        version = -1
    ...
    return cls(
        id=int(data.get("id", -1)),
        ...
    )
```

`item_type = -1` if missing or unparseable. `id = -1` if missing. `version = -1` similarly.

These **sentinel values silently propagate** into the KG. A `:Requirement` node with `jama_id: -1` has no real Jama counterpart — it's an artifact of malformed Jama API output. Downstream queries:
```cypher
MATCH (r:Requirement {jama_id: -1}) RETURN count(r)
```
might return many matches, all of them garbage data.

**Recommendation:**
1. **Fail loud** for required fields:
   ```python
   if "id" not in data:
       raise ValueError(f"Jama item missing 'id': {data}")
   ```
2. For optional fields like `version`, keep the sentinel but **log a warning** when coercion happens.
3. Add a per-batch summary at end of fetch: `logger.info("[Jama] Fetched %d items, %d had missing fields (item_type/version/id)", total, malformed_count)`.

Effort: 2 hours.

---

### F-CF-J03 🟢 — Module docstring uses unicode em-dashes that look fine in editors but break some terminal renders

The file's docstrings use `—` (em dash, U+2014) liberally. Most terminals render this fine; some legacy ones (pre-UTF-8 Windows cmd, certain ssh fonts) display as `?`. Cosmetic.

Worth noting because the **same pattern showed up in Cluster D F-CD-B12** as actual byte corruption. Different issue here (real em dash vs. corrupted bytes), same surface area (display-quality of CLI/log output).

Effort: 0 (acceptable).

---

## 3. `PolarionConnector.py`

### F-CF-P01 🔴 — Polarion JWT token is captured at construction and never refreshed; expired tokens silently fail every subsequent call

**Evidence:**

`PolarionConnector.__init__`:
```python
self._client = httpx.Client(
    base_url=self._base_url + "/",
    verify=ssl_context,
    timeout=timeout,
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",   # ← baked in at construction
    },
)
```

The token is **embedded in the httpx client's default headers** at instantiation time. It cannot be changed without recreating the client.

Polarion's Web Tools JWTs typically expire in 1-24 hours (depending on Infineon's policy). Once expired:
- Every request returns 401.
- `_request` raises `PolarionAuthError`.
- `PolarionAuthError` is a non-retryable exception (`raise` immediately, not retry).
- The connector returns failure to the caller.

There's **no token-refresh path**. Compare to `pdf_pipeline.py::_process_batch_with_retry` which detects 401 and calls `token_manager.get_token(force_refresh=True)`:
```python
if "401" in str(exc) or "unauthorized" in str(exc).lower():
    new_token = token_gen() if token_gen else None
    if new_token:
        ...
```

PolarionConnector has **none of this**. After ~12 hours of MCP uptime, every Polarion call fails. The MCP server doesn't crash, just silently returns errors to DAs trying to query Polarion data.

**Operational impact:** AICE in production has multi-day uptime. Polarion sync jobs run periodically. After token expiration:
- Incremental sync calls return `PolarionAuthError`.
- Sync state stays at "last successful sync = T-12hours".
- New work items in Polarion are **never reflected in the KG** until pod restart.
- DA queries against Polarion-derived nodes return stale data, with no indication.

**Recommendation:**

1. **Accept a `token_provider` callable instead of a static token:**
   ```python
   def __init__(self, base_url: str, token_provider: Callable[[], str], ...):
       self._token_provider = token_provider
       self._refresh_token()
   
   def _refresh_token(self):
       new_token = self._token_provider()
       self._client.headers["Authorization"] = f"Bearer {new_token}"
   ```

2. **Refresh on 401:**
   ```python
   if response.status_code == 401:
       logger.info("[Polarion] Token expired, refreshing")
       self._refresh_token()
       continue  # retry the request
   ```

3. For backward compat, support a static-token mode:
   ```python
   def __init__(self, ..., token: str = None, token_provider: Callable = None):
       if token_provider:
           self._token_provider = token_provider
       elif token:
           self._token_provider = lambda: token
       else:
           raise ValueError("Need token or token_provider")
   ```

4. **Same fix should apply to `BitbucketConnector`** when used with Bearer tokens (Personal Access Tokens have configurable but finite lifetimes).

Effort: 1 day. Pairs with the LLM-retry helper (F-CF-X03 / Cluster C F-CC-R01).

---

### F-CF-P02 🟠 — `validate_connection` uses a hardcoded project ID `"AURIX_RC1_MCAL"` as the auth probe

**Evidence:**
```python
def validate_connection(self, project_id: str = "AURIX_RC1_MCAL") -> bool:
    """Validate that the Bearer token is accepted by Polarion.

    Performs a lightweight ``GET /projects/{project_id}/collections``
    call.  Using a real project ID ensures the server checks the
    Bearer token (non-existent projects return 400 *without* auth
    validation on this API).
    """
```

The default probe is `AURIX_RC1_MCAL`. Issues:

1. **Hardcoded project name in the validation helper.** If the project is renamed in Polarion (or if a non-AURIX deployment uses this connector for a different ALM project), `validate_connection` will fail with 404 — looking like an auth error to operators.

2. **The docstring's claim** that "non-existent projects return 400 *without* auth validation" is itself an external API quirk. Polarion versions could change this behavior. If a future Polarion release returns 404 *with* auth validation, the probe still works but the docstring is misleading.

3. **No alternative endpoint** is suggested. Polarion likely has `/users/me` or `/whoami` style endpoints that probe auth without requiring a specific project.

**Recommendation:**
1. Make the project ID configurable via env var: `POLARION_VALIDATION_PROJECT_ID`.
2. Document that **operators must set** this for non-AURIX deployments.
3. Investigate `/users/current` or similar endpoint as a true auth-only probe.

Effort: 30 min.

---

### F-CF-P03 🟡 — `incremental_sync` saves sync state on success but writes "FAILED" status to disk on every failure — masks transient vs. persistent failures

**Evidence:**
```python
try:
    work_items = self.get_document_work_items(...)
except PolarionConnectorError as exc:
    state.last_sync_status = SyncStatus.FAILED
    self._save_sync_state(state)
    ...
```

A single transient network failure marks the sync state as FAILED and persists this to disk. A subsequent invocation reads this state, sees FAILED, and... what? The code only uses `last_sync_timestamp` for incremental cursor logic, so the FAILED status is informational. But:

- An operator querying sync state via a future admin tool would see "FAILED" without knowing if it's a real persistent failure or a one-off network blip.
- No retry-on-startup behavior triggered by the FAILED state.
- No backoff before next attempt — if the failure was rate-limit-related, the next attempt fires immediately.

**Recommendation:**
1. Distinguish transient failures (`SyncStatus.TRANSIENT_FAILURE`) from persistent ones (`SyncStatus.FAILED`).
2. After 3 consecutive TRANSIENT_FAILURE, escalate to FAILED.
3. On startup, if state is FAILED, log loudly and don't re-attempt for at least 1 hour.

Effort: 2 hours.

---

### F-CF-P04 🟢 — `PolarionTestEnvironment.from_api_dict` has 13 fields with `data.get(..., "") or ""` pattern

**Evidence:**
```python
@classmethod
def from_api_dict(cls, data: dict) -> "PolarionTestEnvironment":
    return cls(
        polarion_id=data.get("polarionId", "") or "",
        title=data.get("title", "") or "",
        ...
    )
```

The `... or ""` pattern handles the case where the API returns `null` for a field — `data.get("title", "")` would return `None`, then `None or ""` becomes `""`. Correct, but:
1. Repeated 13 times — boilerplate.
2. Reads as superstition unless you know about the JSON-null-vs-Python-None nuance.

**Recommendation:** small helper:
```python
def _str(d: dict, key: str) -> str:
    return d.get(key) or ""
```
Replace all `data.get("X", "") or ""` with `_str(data, "X")`. Effort: 30 min, very satisfying refactor.

---

## 4. `JenkinsConnector.py`

### F-CF-N01 🟠 — `_safe_get_*` and `_job_to_info` use `except (NoBuildData, Exception): pass` — catches all errors silently

**Evidence:**
```python
@staticmethod
def _safe_get_timestamp(build: JenkinsBuild) -> Optional[datetime]:
    try:
        return build.get_timestamp()
    except Exception:  # noqa: BLE001
        return None

@staticmethod
def _job_to_info(name: str, job: JenkinsJob) -> JobInfo:
    last_build = None
    last_good = None
    last_failed = None
    is_running = False
    is_enabled = True

    try:
        is_running = job.is_running()
    except Exception:  # noqa: BLE001
        pass

    try:
        is_enabled = job.is_enabled()
    except Exception:  # noqa: BLE001
        pass
    ...
```

Five `except (NoBuildData, Exception): pass` blocks in `_job_to_info`. The `Exception` catch is overly broad — it would silently absorb:
- `JenkinsConnectionError` (Jenkins server became unreachable mid-call).
- `KeyError` from buggy field access.
- `AttributeError` from API response shape changes.
- `TypeError` from any of the above interacting badly.

For a `JobInfo` populated entirely from these silent-fail calls, partial population looks identical to a healthy job with no builds. The KG ingests `JobInfo(last_build_number=None, last_good_build_number=None)` and the user thinks "no builds yet" when actually "Jenkins was unreachable for 3 seconds during the query."

The `# noqa: BLE001` comments confirm this is deliberate — the developer suppressed the broad-except linter warning. But the suppression is wrong; the right fix is to narrow the exception types.

**Recommendation:**

1. Catch only what's expected:
   ```python
   try:
       last_build = job.get_last_buildnumber()
   except NoBuildData:
       pass  # legitimately no builds
   except (JenkinsAPIException, ConnectionError) as exc:
       logger.warning("Jenkins unreachable getting last_build for %s: %s", job.url, exc)
       raise  # let the outer retry handle it
   ```

2. **Don't degrade silently.** If the connector can't query a job's metadata reliably, return an explicit "unavailable" marker rather than `None`:
   ```python
   @dataclass
   class JobInfo:
       ...
       query_errors: List[str] = field(default_factory=list)
   ```

Effort: 4 hours.

---

### F-CF-N02 🟡 — `connect()` `except Exception` clause produces misleading error messages

**Evidence:**
```python
def connect(self) -> "JenkinsConnector":
    try:
        self._client = Jenkins(...)
        self._connected = True
        ...
        return self
    except NotAuthorized as exc:
        ...
        raise JenkinsAuthError(...) from exc
    except JenkinsAPIException as exc:
        ...
        raise JenkinsConnectionError(...) from exc
    except Exception as exc:
        self._connected = False
        raise JenkinsConnectionError(
            f"Failed to connect to Jenkins at {self._base_url}: {exc}"
        ) from exc
```

The `except Exception` fall-through wraps **everything** as `JenkinsConnectionError`. Specifically:
- `requests.exceptions.SSLError` (cert failure) → "Failed to connect" (misleading; it connected fine, the TLS cert was wrong).
- `ValueError` from a misconfigured URL → "Failed to connect" (it never tried to connect; URL parse failed).
- Any unhandled internal exception in `jenkinsapi` → "Failed to connect."

The user/operator looking at the error sees "Failed to connect to Jenkins" and starts diagnosing network issues, when the real issue is a cert or config problem.

**Recommendation:** more specific error messages or different error types:
```python
except ssl.SSLError as exc:
    raise JenkinsTLSError(f"TLS/cert error connecting to {self._base_url}: {exc}") from exc
except (ValueError, TypeError) as exc:
    raise JenkinsConfigError(f"Invalid Jenkins config: {exc}") from exc
except Exception as exc:
    raise JenkinsConnectionError(f"Unexpected error connecting to Jenkins at {self._base_url}: {exc}") from exc
```

Effort: 1 hour.

---

## 5. `BitbucketConnector.py`

### F-CF-B01 🟡 — `get_files_bulk` `ThreadPoolExecutor` with no per-thread error tracking

**Evidence:**
```python
def get_files_bulk(self, paths: List[str], *, ref=None) -> Dict[str, FileContent]:
    results: Dict[str, FileContent] = {}
    with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
        futures = {
            pool.submit(self.get_file_content, p, ref=ref): p
            for p in paths
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                results[path] = future.result()
            except BitbucketConnectorError as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
    return results
```

The bulk-fetch returns a dict with successes only — failures are logged and dropped. No way for the caller to know **which files failed** vs. **which simply don't exist** vs. **which had transient errors**.

For `dependency_fetcher.py::fetch_all` (which downloads 50+ headers from Bitbucket for clang inclusion), this means:
- 5% transient failures → silently dropped → 5% of headers missing.
- Clang parsing then fails for files that include the missing headers.
- No connection between "fetch failed" and "clang error" in logs.

**Recommendation:** return both successes and failures:
```python
@dataclass
class BulkFetchResult:
    files: Dict[str, FileContent]
    failures: Dict[str, str]  # path → error message

def get_files_bulk(self, paths: List[str], *, ref=None) -> BulkFetchResult:
    files: Dict[str, FileContent] = {}
    failures: Dict[str, str] = {}
    with ThreadPoolExecutor(...) as pool:
        ...
        for future in as_completed(futures):
            path = futures[future]
            try:
                files[path] = future.result()
            except BitbucketConnectorError as exc:
                failures[path] = str(exc)
                logger.warning("Failed to fetch %s: %s", path, exc)
    return BulkFetchResult(files=files, failures=failures)
```

Caller can then warn loudly if `failures` is non-empty.

Effort: 2 hours.

---

### F-CF-B02 🟡 — Path is normalized via `path = path.lstrip("/")` only; no traversal protection

**Evidence:**
```python
def get_file_content(self, path: str, *, ref=None, project=None, repo=None) -> FileContent:
    ...
    path = path.lstrip("/")

    raw = self._request_raw(
        "GET",
        f"/projects/{project}/repos/{repo}/raw/{path}",
        params={"at": ref},
    )
```

The path is interpolated into the URL after `lstrip("/")`. A path of `../../etc/passwd` would still produce `/projects/X/repos/Y/raw/../../etc/passwd` in the URL — Bitbucket's URL routing should reject this, but **relying on the server to validate is poor defense-in-depth**.

If a future Bitbucket version had a path-handling bug (URL-decode-twice, normalization bypass), the connector would forward any path the caller specified, including paths designed to break out of the repo.

The same call site in `dependency_fetcher.py` passes paths derived from a hardcoded list, so currently safe. But again, defense-in-depth: any future code that takes user input here would be at risk.

**Recommendation:**
```python
def get_file_content(self, path: str, ...):
    # Reject path traversal attempts
    if ".." in path.split("/") or path.startswith("/"):
        raise ValueError(f"Invalid path: {path}")
    # Validate characters
    if not re.match(r"^[\w\-./]+$", path):
        raise ValueError(f"Path contains invalid characters: {path}")
    ...
```

Effort: 30 min.

---

### F-CF-B03 🟢 — Documentation says `from_clone_url` is supported but I cannot see its body

The class docstring claims:
> Initialisation from an SSH or HTTPS clone URL via `from_clone_url`.

I haven't seen the implementation in any snippet. If it doesn't exist, this is a doc/code drift (Pass 1 family). If it exists but is misimplemented (e.g., for `git@bitbucket.example.com:proj/repo.git` SSH URLs, the project/repo extraction can fail silently), worth a closer look.

**Recommendation:** verify implementation; if missing, add or remove from doc.

Effort: 15 min to verify.

---

## 6. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CF-X01 (verify_ssl=False everywhere), F-CF-P01 (Polarion JWT no-refresh) | 1.5 days — both are P0 security/reliability |
| **P1 (next sprint)** | F-CF-X02 (credential storage), F-CF-X03 (retry consolidation), F-CF-J01 (project cache), F-CF-N01 (broad except in Jenkins), F-CF-P02 (hardcoded probe project) | 4 days |
| **P2** | F-CF-X04 (sync-state path containment), F-CF-X05 (httpx client recreation), F-CF-J02 (silent default sentinels), F-CF-P03 (transient vs persistent failure), F-CF-N02 (error message specificity), F-CF-B01 (bulk fetch failures), F-CF-B02 (path traversal defense-in-depth) | 1.5 days |
| **P3** | F-CF-J03 (em-dashes), F-CF-P04 (boilerplate refactor), F-CF-B03 (docstring verify) | 1 hour |

**Total cluster effort:** ~6.5 person-days. Smallest cluster of Pass 3 by effort.

---

## 7. Cross-cutting reinforcement: retry consolidation across the entire codebase

This is the **fifth time** the duplicate-retry pattern has surfaced:

| Area | Cluster | Implementations |
|---|---|---|
| Search service vs RLM token estimation | C | 2 (different `// 3` vs `// 4`) |
| KG construction Neo4j writes | D | 3 (MCAL vs ILLD vs batch_ingestion) |
| LLM calls | C, E | 3 (PDF pipeline, SWA enrichment, RLM `_default_llm`) |
| Connector HTTP retries | F | 4 (Jama, Polarion, Jenkins, Bitbucket) |
| **Total** | | **~12 distinct retry implementations across the repo** |

Most have:
- Different backoff algorithms (constant, linear, exponential with/without jitter).
- Different retryable exception lists.
- Different log formats.
- Different success/failure metrics.

**A single `src/_common/retry.py` helper** with `RetryConfig` and `with_retry()` would replace all 12. Estimated **3 person-days** to consolidate, and it removes the pattern of "retry impl A had a fix, retry impl B didn't" forever (cf. Cluster C F-CC-R01 where one retry got a fix but `_default_llm` didn't).

I'll include this as a top-line recommendation in the **Pass 3 final summary** at the start of Pass 4.

---

## 8. What I deliberately did not flag

- **`fetch_jama_relationships.py`** specifics — uses JamaConnector, doesn't add new patterns beyond what's already in F-CF-J01 / F-CF-X02.
- **`header_fetcher.py`** specifics — uses BitbucketConnector with the same `verify_ssl=False` issue from F-CF-X01.
- **The `get_max_workers("connectors.X")` config-driven worker counts** — well-designed, follows the central config pattern; no findings.
- **Connector data classes (`JamaItem`, `PolarionTestCase`, `JenkinsBuild`, etc.)** — they're well-structured dataclasses; the `from_api_dict` pattern is correct (though F-CF-J02 / F-CF-P04 note small improvements).
- **JUnit XML parsing in JenkinsConnector** — out of scope (parser-level concern, would belong with Cluster E if covered).

---

**End of Cluster F. Pass 3 is now complete.**

Pass 3 final totals:

| Cluster | Files | Findings | C | H | M | L | Effort (days) |
|---|---|---:|---:|---:|---:|---:|---:|
| A: Service Spine | 4 | 27 | 4 | 9 | 10 | 4 | 12 |
| B: MCP Server | 2 | 24 | 3 | 8 | 9 | 4 | 11 |
| C: Retrieval Brain | 3 | 22 | 3 | 7 | 9 | 3 | 7 |
| D: KG Construction | 4 | 26 | 4 | 8 | 10 | 4 | 11 |
| E: Parsers | 14 | 24 | 2 | 7 | 11 | 4 | 11 |
| F: Connectors | 4 | 17 | 2 | 5 | 7 | 3 | 6.5 |
| **Total** | **31** | **140** | **18** | **44** | **56** | **22** | **58.5** |

A consolidated **Pass 3 summary deliverable** with cross-cutting patterns, top-priority items, and the retry-consolidation recommendation will be the last artifact before Pass 4. Then:

- **Pass 4: Security / Safety / Compliance** — including the CI gate proposal (Pass 1 §5 + cross-file injection checks from Cluster D §6 + retry-consolidation gate from this cluster + verify_ssl audit from F-CF-X01 + parallel-implementation detection from Cluster E §9).
- **Pass 5: LLD Productivity Alignment** — reframed per your direction to assess whether AICE provides the right tools/infra/harness for DAs (CIA, GEST, REVA, ACRA, SAGA, etc.) to do their business logic. Will require clarifying questions before starting.

Ready to proceed to the **Pass 3 consolidated summary** + **Pass 4** on your signal.

# AICE Review — Implementation Plan

**Scope:** Step-by-step plan covering all 187 findings from Passes 1–5 of the AICE review.
**Date:** 2026-04-26
**Owner:** B. Sai Kiran, ATV MC D SW VDF
**Sprint baseline:** Sprint 25 → 30. Plan covers Sprints 26–30 (5 sprints, ~50 person-days).

This document is the bridge between the review findings and the JIRA backlog. It groups findings into **workstreams** that share design decisions and code paths — so a single PR can close multiple findings, and one engineer's morning of focused work makes coherent progress.

**Companion file:** `AICE_JIRA_BACKLOG.csv` — flat list of all tickets, importable into JIRA.

---

## How to read this document

- **Workstreams** (W-01 through W-26) bundle related findings. Each workstream is one PR or one short PR series.
- **Findings inside a workstream** are listed with their original IDs (F-D##, F-A##, F-CA-S##, F-CB-##, F-CC-##, F-CD-##, F-CE-##, F-CF-##, F-P5-##) so you can cross-reference back to the cluster files.
- **Steps** within each workstream are ordered — do them in sequence.
- **Verification** says how to know the workstream actually worked.
- **Sprint assignment** maps each workstream into Sprints 26–30. Sprint loading targets ~10 person-days each.

---

# SPRINT 26 — "Bleeding-stops" (~10 days)

Goal: stop ongoing data loss and close the worst security gaps. Every workstream here has clear evidence and small effort.

---

## W-01 — Fix sandbox prod-overlay Cypher (1 day)

**Findings:** F-CB-01 (Cluster B), addresses Sprint 4–5 silently-broken feature.

**Why first:** the entire Ephemeral Sandbox shadow-detection feature has never worked end-to-end in iLLD because the Cypher query has invalid syntax (Neo4j doesn't support parameterized variable-length path bounds). Combined with W-02 below.

### Steps

1. **Open** `src/MemoryLayer/memory/ephemeral_sandbox.py`. Find the `TraceabilityPuller.pull_neighbors` method.

2. **Locate the broken query:** `MATCH path = (n)-[*1..$depth]-(neighbor) ...`

3. **Replace** with literal-depth interpolation. Since `depth` is already validated to be 0/1/2 at the MCP layer (`if trace_depth < 0 or trace_depth > 2: return _err(...)`), inline interpolation is safe:
   ```python
   depth_int = int(depth)  # Defense-in-depth: cast to int
   if depth_int < 0 or depth_int > 5:
       raise ValueError(f"Invalid depth: {depth}")
   cypher = (
       f"MATCH (n {{name: $name}}) "
       f"OPTIONAL MATCH path = (n)-[*1..{depth_int}]-(neighbor) "
       f"RETURN n, neighbor"
   )
   session.run(cypher, {"name": name})
   ```

4. **Add a unit test** at `tests/unit/memory_layer/test_ephemeral_sandbox.py`:
   ```python
   def test_pull_neighbors_uses_literal_depth():
       puller = TraceabilityPuller(driver=mock_driver)
       puller.pull_neighbors(["IfxCan_init"], depth=2)
       captured_cypher = mock_driver.session().run.call_args[0][0]
       assert "*1..2" in captured_cypher
       assert "$depth" not in captured_cypher
   ```

5. **Verify in dev environment:** call `sandbox_upload` with `trace_depth=1` against a real iLLD module. Check the response contains `prod_nodes_loaded > 0`. (Today it returns 0.)

6. **Commit message:** `fix(sandbox): use literal depth in TraceabilityPuller (F-CB-01)` — references the finding ID.

### Verification

- Unit test passes.
- Manual `sandbox_upload` with `trace_depth=1` returns `prod_nodes_loaded > 0`.
- Prometheus gauge `aice_sandbox_overlay_active` (added in W-15) reads 1.

### Cross-references

- W-02 (module name detection) is the second half of getting iLLD sandbox functional. Land both in the same sprint.

---

## W-02 — Fix module-name detection in `_detect_module_from_names` (30 min)

**Findings:** F-CB-10 (Cluster B).

**Why now:** complements W-01. Without this fix, even after W-01, iLLD sandbox queries match against `module="IfxCan"` instead of `"Can"` and still return zero prod nodes.

### Steps

1. Open `mcp/core/mcp_server.py`. Find `_detect_module_from_names`.

2. Replace the body with:
   ```python
   def _detect_module_from_names(node_names: List[str]) -> str:
       """Heuristic: extract module name from function names.
       Handles both MCAL (`Adc_Init`) and iLLD (`IfxCan_init`) conventions.
       """
       from collections import Counter
       prefixes = []
       for name in node_names:
           if "_" not in name:
               continue
           prefix = name.split("_")[0]
           # Strip iLLD `Ifx` prefix
           prefix = re.sub(r"^Ifx", "", prefix)
           if len(prefix) >= 2 and prefix not in ("Std", "Ifx"):
               prefixes.append(prefix)
       if not prefixes:
           return "unknown"
       # Return the mode (most common prefix), not the first
       return Counter(prefixes).most_common(1)[0][0]
   ```

3. Update `sandbox_upload` to refuse `module="unknown"` when `trace_depth > 0`:
   ```python
   if detected_module == "unknown" and trace_depth > 0:
       return _err("INVALID_INPUT",
           "Could not detect module from uploaded files. "
           "Provide module= explicitly (e.g., module='Can').")
   ```

4. **Add a unit test:**
   ```python
   def test_detect_module_illd_strips_ifx():
       assert _detect_module_from_names(["IfxCan_Node_init", "IfxCan_Can_initModule"]) == "Can"
   def test_detect_module_mcal():
       assert _detect_module_from_names(["Adc_StartGroupConversion"]) == "Adc"
   def test_detect_module_skips_std():
       assert _detect_module_from_names(["Std_ReturnType", "IfxCan_init"]) == "Can"
   ```

### Verification

- Unit tests pass.
- Manual `sandbox_upload` with iLLD content returns `module: "Can"` (not `"IfxCan"` or `"Std"`).

---

## W-03 — Fix `--clear` scope (4 hours)

**Findings:** F-CD-B01 (Cluster D).

**Why critical:** single CLI command obliterates 20 modules of data with no warning.

### Steps

1. Open `src/HybridRAG/code/KG/build_knowledge_graph.py`. Find `main()` and the `_clear_database` methods (one in `KnowledgeGraphBuilder`, one in `ILLDKnowledgeGraphBuilder`).

2. **Add CLI flag:** `--clear-all` for global wipe; `--clear` becomes module-scoped:
   ```python
   parser.add_argument("--clear", action="store_true",
       help="Clear data for THIS MODULE only before ingestion.")
   parser.add_argument("--clear-all", action="store_true",
       help="Clear ENTIRE DATABASE (requires confirmation). Use with extreme caution.")
   ```

3. **Implement module-scoped clear** in both builder classes:
   ```python
   def _clear_module_data(self):
       module = self.module
       db = self.neo4j_cfg["database"]
       # Pre-count
       result = self._run("MATCH (n {module: $mod}) RETURN count(n) AS c", {"mod": module})
       count_before = result[0]["c"] if result else 0
       logger.warning("Clearing %d nodes for module '%s' in db '%s'…",
                      count_before, module, db)
       self._write_tx("MATCH (n {module: $mod}) DETACH DELETE n", {"mod": module})
       logger.info("Module '%s' cleared (%d nodes deleted).", module, count_before)
       # Audit log entry
       self._write_audit_event("module_clear", {"module": module, "nodes_deleted": count_before})
   ```

4. **Implement global clear with confirmation:**
   ```python
   def _clear_all(self):
       db = self.neo4j_cfg["database"]
       result = self._run("MATCH (n) RETURN count(n) AS c")
       count = result[0]["c"] if result else 0
       print(f"\n⚠️  WARNING: This will DELETE ALL {count:,} nodes in database '{db}'.")
       print(f"     This action cannot be undone.")
       confirm = input(f"     Type the database name '{db}' to confirm: ")
       if confirm != db:
           print("Aborted.")
           sys.exit(1)
       self._write_tx("MATCH (n) DETACH DELETE n")
       logger.warning("FULL DATABASE CLEAR executed by user. %d nodes deleted.", count)
       self._write_audit_event("full_clear", {"database": db, "nodes_deleted": count})
   ```

5. **Wire into `main()`:**
   ```python
   if args.clear_all and args.clear:
       parser.error("--clear and --clear-all are mutually exclusive")
   if args.clear_all:
       builder._clear_all()
   elif args.clear:
       builder._clear_module_data()
   ```

6. **Update CLI docs** in `docs/DOCUMENTATION.md` and module README.

7. **Add a smoke test** that confirms `--clear --module ADC` only deletes ADC nodes (mock Neo4j or use a test DB).

### Verification

- `--clear --module ADC` on a multi-module test DB leaves other modules intact.
- `--clear-all` prompts for confirmation.
- An `audit_logs` row is written for every clear operation (requires W-13 for the event-write helper).

---

## W-04 — Cypher injection: shared `_kg_safety.py` helper (1 day)

**Findings:** F-CC-K01 (Cluster C), F-CD-B02, F-CD-I01, F-CD-Q01, F-CD-Q02 (Cluster D). Pattern #1 from Pass 3 §2.

**Single PR closes all 5 findings.**

### Steps

1. **Create** `src/HybridRAG/code/_kg_safety.py`:
   ```python
   """KG-write safety: validates labels, relationship types, and property names
   against an allowlist before Cypher interpolation.

   Cypher does not support parameterized labels (`MATCH (n:$label)` is invalid);
   this module provides the only sanctioned mechanism for interpolating them.
   """
   from __future__ import annotations
   import re
   from functools import lru_cache
   from pathlib import Path
   from typing import FrozenSet

   import yaml

   _ONTOLOGY_PATH = Path(__file__).resolve().parents[3] / "config" / "ontology.yaml"
   _PROP_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

   @lru_cache(maxsize=1)
   def _load_valid_labels() -> FrozenSet[str]:
       data = yaml.safe_load(_ONTOLOGY_PATH.read_text())
       labels = set()
       for profile in data.get("profiles", {}).values():
           for nt in profile.get("node_types", []):
               labels.add(nt["name"])
       return frozenset(labels)

   @lru_cache(maxsize=1)
   def _load_valid_rel_types() -> FrozenSet[str]:
       data = yaml.safe_load(_ONTOLOGY_PATH.read_text())
       rels = set()
       for profile in data.get("profiles", {}).values():
           for rt in profile.get("relationship_types", []):
               rels.add(rt["name"])
       return frozenset(rels)

   def sanitize_label(label: str) -> str:
       """Validate label against ontology. Returns label or raises ValueError."""
       if label not in _load_valid_labels():
           raise ValueError(f"Unsafe Cypher label: {label!r}")
       return label

   def sanitize_rel_type(rel: str) -> str:
       if rel not in _load_valid_rel_types():
           raise ValueError(f"Unsafe Cypher relationship type: {rel!r}")
       return rel

   def sanitize_property_name(name: str) -> str:
       if not _PROP_NAME_RE.match(name):
           raise ValueError(f"Unsafe Cypher property name: {name!r}")
       return name
   ```

2. **Update** `knowledge_intelligence.py::_fuzzy_find` (F-CC-K01):
   ```python
   from src.HybridRAG.code._kg_safety import sanitize_label
   def _fuzzy_find(self, name, labels, ws, limit=5):
       safe_labels = [sanitize_label(l) for l in labels]
       for label in safe_labels:
           rows = self._run_cypher(
               f"MATCH (n:{label}) WHERE …",  # noqa: aice-cypher-safe: sanitized via _kg_safety
               …
           )
   ```

3. **Update** `build_knowledge_graph.py::_create_constraints` (F-CD-B02):
   ```python
   from src.HybridRAG.code._kg_safety import sanitize_label, sanitize_property_name
   for nt in self.node_types:
       label = sanitize_label(nt["name"])
       uid_prop = sanitize_property_name(get_unique_id_property(nt))
       cypher = (f"CREATE CONSTRAINT … FOR (n:{label}) "
                 f"REQUIRE n.{uid_prop} IS UNIQUE")  # noqa: aice-cypher-safe
   ```

4. **Update** `build_knowledge_graph.py::_create_nodes` and `_create_edges` (F-CD-B02 continuation): same pattern.

5. **Update** `illd_kg_builder.py::_merge_nodes` and `_merge_edges` (F-CD-I01): same pattern.

6. **Update** `query_knowledge_graph.py::_fetch`, `find_path`, `find_orphan_requirements` (F-CD-Q01). For `_fetch`, sanitize the `rel` parameter; for `find_path`, cast `max_depth` to int with a bound (F-CD-Q02):
   ```python
   def find_path(self, …, max_depth: int = 4):
       max_depth = int(max_depth)
       if not 1 <= max_depth <= 10:
           raise ValueError(f"max_depth must be 1..10, got {max_depth}")
       cypher = (f"MATCH (a {from_match}), (b {to_match}), "
                 f"path = shortestPath((a)-[*..{max_depth}]-(b)) "
                 …)  # noqa: aice-cypher-safe
   ```

7. **Add unit tests** at `tests/unit/test_kg_safety.py`:
   ```python
   def test_sanitize_label_valid(): assert sanitize_label("Function") == "Function"
   def test_sanitize_label_rejects_unknown():
       with pytest.raises(ValueError, match="Unsafe Cypher label"):
           sanitize_label("Function); DROP DATABASE neo4j; --")
   def test_sanitize_property_rejects_special_chars():
       with pytest.raises(ValueError):
           sanitize_property_name("name; CREATE")
   ```

### Verification

- Unit tests pass.
- The CI grep gate `gates:grep` (W-22, Sprint 27) catches any future un-sanitized f-string Cypher pattern.
- All 5 findings move from Open to Closed.

---

## W-05 — Sandbox path containment + path-traversal defense (1 day)

**Findings:** F-CB-09 (Cluster B), F-CA-I01 (Cluster A), F-CE-O01 (Cluster E partial), F-CF-X04 (Cluster F).

**Single PR closes 4 findings.**

### Steps

1. **Create** `src/_common/path_safety.py`:
   ```python
   """Path-traversal defense — used by sandbox_upload, ingest_file, OCR, etc."""
   import os, re
   from pathlib import Path
   from typing import Iterable

   _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
   _IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
   _INGEST_EXT = {".c", ".h", ".pdf", ".xlsx", ".arxml", ".puml", ".rst", ".md", ".txt", ".csv", ".json"}

   def validate_session_id(session_id: str) -> str:
       if not _SESSION_ID_RE.match(session_id):
           raise ValueError(f"Invalid session_id: {session_id!r}")
       return session_id

   def safe_path_under(path: str, allowed_roots: Iterable[Path]) -> Path:
       """Resolve `path`, ensure it lives under one of `allowed_roots`."""
       p = Path(path).resolve()
       if p.is_symlink():
           raise ValueError(f"Path is symlink: {path}")
       for root in allowed_roots:
           root_resolved = Path(root).resolve()
           try:
               if p.is_relative_to(root_resolved):
                   return p
           except (ValueError, OSError):
               continue
       raise ValueError(f"Path {path!r} not under allowed roots: {list(allowed_roots)}")

   def validate_extension(path: Path, allowed: set) -> Path:
       if path.suffix.lower() not in allowed:
           raise ValueError(f"Disallowed file extension: {path.suffix}")
       return path
   ```

2. **Apply to `sandbox_upload`** (F-CB-09):
   ```python
   from src._common.path_safety import validate_session_id, safe_path_under
   SANDBOX_TMP_ROOT = Path(os.environ.get("SANDBOX_TMP_ROOT", "/var/run/aice/sandbox"))
   SANDBOX_TMP_ROOT.mkdir(parents=True, exist_ok=True)

   async def sandbox_upload(session_id, file_path, …):
       validate_session_id(session_id)
       tmp_dir = (SANDBOX_TMP_ROOT / f"sandbox_{session_id}").resolve()
       if not tmp_dir.is_relative_to(SANDBOX_TMP_ROOT):
           return _err("INVALID_INPUT", "session_id produces invalid path")
       tmp_dir.mkdir(exist_ok=True)
       …
   ```

3. **Apply to `ingest_file`** (F-CA-I01):
   ```python
   INGEST_ALLOWED_ROOTS_ENV = os.environ.get("INGEST_ALLOWED_ROOTS", "/data,/repos")
   INGEST_ALLOWED_ROOTS = [Path(r.strip()) for r in INGEST_ALLOWED_ROOTS_ENV.split(",")]

   def ingest_file(file_path, workspace, module, …):
       p = safe_path_under(file_path, INGEST_ALLOWED_ROOTS)
       validate_extension(p, _INGEST_EXT)
       …
   ```

4. **Apply to `OCRProcessor.process_page_image`** (F-CE-O01):
   ```python
   def process_page_image(self, image_path):
       OCR_ALLOWED_ROOTS = [Path("/tmp"), Path("/var/run/aice")]
       p = safe_path_under(image_path, OCR_ALLOWED_ROOTS)
       validate_extension(p, _IMAGE_EXT)
       # Validate OCR_LANGUAGE
       if not re.match(r'^[a-z]{3}(\+[a-z]{3})*$', self._language):
           raise ValueError(f"Invalid OCR language: {self._language}")
       …
   ```

5. **Apply to connector sync_state_dir** (F-CF-X04):
   ```python
   ALLOWED_SYNC_DIRS = [Path("/data/aice/sync_state").resolve()]
   if sync_state_dir is not None:
       safe_path_under(sync_state_dir, ALLOWED_SYNC_DIRS)
   ```

6. **Add unit tests** for traversal attempts:
   ```python
   def test_session_id_rejects_traversal():
       with pytest.raises(ValueError):
           validate_session_id("../etc/passwd")
   def test_path_outside_roots_rejected():
       with pytest.raises(ValueError, match="not under allowed"):
           safe_path_under("/etc/passwd", [Path("/data")])
   def test_path_symlink_rejected(tmp_path):
       link = tmp_path / "link"
       link.symlink_to("/etc/passwd")
       with pytest.raises(ValueError, match="symlink"):
           safe_path_under(str(link), [tmp_path])
   ```

### Verification

- Unit tests pass.
- Manual test: `ingest_file("/proc/self/environ", …)` rejected with `INVALID_INPUT`.
- Manual test: `session_id="../etc"` rejected at `session_start`.

---

## W-06 — `verify_ssl=False` audit + Infineon CA bundle wiring (4 hours)

**Findings:** F-CF-X01 (Cluster F).

**Why critical:** highest single-finding security severity. Bitbucket connector callers transmit IFX SSO credentials with TLS validation disabled.

### Steps

1. **Audit current state:** `grep -rn 'verify_ssl=False\|verify=False\|ssl_verify=False' src/` — should reveal at least `dependency_fetcher.py::_make_connector` and `header_fetcher.py::_get_connector`.

2. **Locate the IFX CA bundle.** Already used by `pdf_pipeline.py::_CA_BUNDLE_PATH` — confirm it's readable from container/dev environment.

3. **Create** `src/_common/tls_config.py`:
   ```python
   """TLS verification config — uses Infineon CA bundle by default."""
   import logging, os
   from pathlib import Path
   from typing import Union

   logger = logging.getLogger(__name__)
   _CA_BUNDLE_PATH = Path(__file__).resolve().parents[2] / "src" / "HybridRAG" / "code" / "ca-bundle.crt"

   def get_verify_setting() -> Union[bool, str]:
       """Return value suitable for httpx/requests `verify=` parameter."""
       if os.environ.get("AICE_ALLOW_INSECURE_TLS") == "1":
           logger.warning(
               "[TLS] AICE_ALLOW_INSECURE_TLS=1 — TLS verification DISABLED. "
               "Credentials and source code in flight are not protected."
           )
           return False
       if _CA_BUNDLE_PATH.is_file():
           return str(_CA_BUNDLE_PATH)
       logger.info("[TLS] Using system trust store (Infineon CA bundle not found at %s)", _CA_BUNDLE_PATH)
       return True
   ```

4. **Update** `dependency_fetcher.py::_make_connector`:
   ```python
   from src._common.tls_config import get_verify_setting
   return BitbucketConnector(
       base_url=BITBUCKET_BASE_URL,
       …,
       verify_ssl=get_verify_setting(),
       …
   )
   ```

5. **Update** `header_fetcher.py::_get_connector`: same change.

6. **Update each connector's `__init__`** to refuse `verify_ssl=False` without explicit env override:
   ```python
   def __init__(self, …, verify_ssl=True):
       if verify_ssl is False and os.environ.get("AICE_ALLOW_INSECURE_TLS") != "1":
           raise ValueError(
               "verify_ssl=False requires AICE_ALLOW_INSECURE_TLS=1 (development only). "
               "In production, use the Infineon CA bundle."
           )
   ```

7. **Document** the env override in `docs/DEPLOYMENT.md` (or wherever deployment config lives).

8. **Add unit tests** in `tests/unit/connectors/test_tls_config.py`:
   ```python
   def test_returns_ca_bundle_path_when_present(monkeypatch):
       monkeypatch.delenv("AICE_ALLOW_INSECURE_TLS", raising=False)
       result = get_verify_setting()
       assert isinstance(result, str) and result.endswith("ca-bundle.crt") or result is True
   def test_explicit_insecure_returns_false(monkeypatch):
       monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
       assert get_verify_setting() is False
   ```

### Verification

- `grep -rn 'verify_ssl=False'` returns no hits in `src/HybridRAG/`, `src/IngestionPipeline/`.
- Pipeline grep gate `G-08` (added in W-22) catches future regressions.
- Manual test: dependency_fetcher run captures network trace showing TLS handshake completes with Infineon CA validation.

---

## W-07 — Lazy-init `_ServiceState` helper (2 days)

**Findings:** F-CB-02 (Cluster B), F-CB-03, F-CA-A04, F-CA-A07, F-CA-S04. Foundation for the silent-failure dashboard in W-15.

**Single PR replaces 12+ ad-hoc lazy-init patterns.**

### Steps

1. **Create** `src/_common/service_state.py`:
   ```python
   """Service-init state with negative caching and Prometheus visibility."""
   from __future__ import annotations
   import logging, time
   from dataclasses import dataclass, field
   from typing import Any, Callable, Optional, Type

   from prometheus_client import Gauge, Counter

   logger = logging.getLogger(__name__)

   AICE_SERVICE_UP = Gauge(
       "aice_service_up", "1 if service initialized successfully", ["service"]
   )
   AICE_SERVICE_INIT_ERRORS = Counter(
       "aice_service_init_errors_total", "Service init failure count",
       ["service", "error_type"],
   )

   @dataclass
   class ServiceState:
       name: str
       instance: Optional[Any] = None
       last_error: Optional[Exception] = None
       last_attempt: float = 0.0
       retry_after_seconds: float = 60.0
       _factory: Optional[Callable[[], Any]] = None

       def get_or_init(self, factory: Callable[[], Any]) -> Optional[Any]:
           if self.instance is not None:
               return self.instance
           # Negative cache: don't retry if recent failure
           if self.last_error and (time.time() - self.last_attempt) < self.retry_after_seconds:
               return None
           self.last_attempt = time.time()
           try:
               self.instance = factory()
               self.last_error = None
               AICE_SERVICE_UP.labels(service=self.name).set(1)
               logger.info("[ServiceState] %s initialized successfully", self.name)
               return self.instance
           except Exception as exc:
               self.last_error = exc
               AICE_SERVICE_UP.labels(service=self.name).set(0)
               AICE_SERVICE_INIT_ERRORS.labels(
                   service=self.name, error_type=type(exc).__name__
               ).inc()
               logger.error("[ServiceState] %s init failed: %s", self.name, exc, exc_info=True)
               return None

       def health(self) -> dict:
           return {
               "service": self.name,
               "up": self.instance is not None,
               "last_error": str(self.last_error) if self.last_error else None,
               "last_attempt": self.last_attempt,
           }
   ```

2. **Refactor `mcp_server.py` lazy-init helpers** to use `ServiceState`. Example for SearchService:
   ```python
   _search_service_states: Dict[str, ServiceState] = {}

   def _get_search_service(profile: str = "illd"):
       if profile not in _search_service_states:
           _search_service_states[profile] = ServiceState(name=f"search_service_{profile}")
       def factory():
           from src.HybridRAG.code.querier.search_service import SearchService
           …
           return SearchService(…)
       return _search_service_states[profile].get_or_init(factory)
   ```

3. **Apply to all 12+ `_get_*` functions** in `mcp_server.py`:
   - `_get_search_service` (per profile)
   - `_get_neo4j` (per profile) — also adds `verify_connectivity()` (F-CB-03)
   - `_get_qdrant`
   - `_get_redis`
   - `_get_postgres_client`
   - `_get_cerbos_client`
   - `_get_ingestion_service`
   - `_get_feedback_sink`
   - `_get_session_manager`
   - `_get_sandbox_manager`
   - `_get_pattern_store` and `_get_pattern_index`
   - `_get_cache_service`

4. **For `_get_neo4j` specifically** (F-CB-03), add `verify_connectivity()` in the factory:
   ```python
   def factory():
       cfg = _load_neo4j_profile_config(profile)
       …
       driver = GraphDatabase.driver(uri, auth=…, max_connection_pool_size=25)
       driver.verify_connectivity()  # F-CB-03 fix
       return driver
   ```

5. **For `_get_cerbos_client`** (F-CA-A04), add timeout:
   ```python
   def factory():
       client = cerbos_pdp_client(
           host=os.environ["CERBOS_HOST"],
           timeout=float(os.environ.get("CERBOS_TIMEOUT_S", "1.0")),
       )
       return client
   ```

6. **For `_get_pattern_store`** (F-CB-17), drop the `neo4j_driver=` kwarg per Pass 1 F-D05:
   ```python
   def factory():
       from src.MemoryLayer.memory.semantic_memory import PatternStore
       qdrant = _get_qdrant()
       embedder = Embedder()
       return PatternStore(qdrant_client=qdrant, embedder=embedder)
   ```

7. **Update `health_check` MCP tool** to surface per-service state:
   ```python
   @mcp.tool()
   async def health_check(verbose=False):
       services = [s.health() for s in _service_states_registry()]
       overall = "healthy" if all(s["up"] for s in services) else "degraded"
       return _ok({"status": overall, "services": services if verbose else None})
   ```

8. **Manual smoke test** in dev: stop Neo4j, call any tool, observe (a) clean error message, (b) Prometheus `aice_service_up{service="neo4j_illd"}` reads 0.

### Verification

- All `_get_*` helpers route through `ServiceState`.
- Stopping Neo4j produces a single ERROR log (not WARNING-spam per request).
- `aice_service_up{service=...}` Prometheus gauge reads 0 for failed services.
- F-CA-A07 fix: when `api_keys.yaml` is missing, the `_get_auth_registry` ServiceState shows up=0; the `MCP_REQUIRE_AUTH=1` env (added separately, see W-08) refuses to start.

---

## W-08 — API key registry hot-reload + auth hardening (1 day)

**Findings:** F-CA-A01 (apscheduler reload), F-CA-A07 (refuse to start without keys), F-CA-A02 (replace `_none` placeholder), F-CA-A05 (sha256 hash for audit), F-CA-A03 (write DENIES to audit_logs).

**Single PR closes 5 auth findings.**

### Steps

1. Open `mcp/auth/auth_middleware.py`. Find `load_api_keys()` and the global `_api_key_registry`.

2. **Add hot-reload via apscheduler:**
   ```python
   import hashlib
   from apscheduler.schedulers.background import BackgroundScheduler

   _api_keys_path = Path(os.environ.get("MCP_API_KEYS_PATH", "mcp/auth/api_keys.yaml"))
   _api_keys_mtime: float = 0.0
   _api_key_registry: Dict[str, Dict] = {}

   def _load_api_keys_if_changed():
       global _api_keys_mtime, _api_key_registry
       try:
           current_mtime = _api_keys_path.stat().st_mtime
           if current_mtime > _api_keys_mtime:
               _api_keys_mtime = current_mtime
               _api_key_registry = _parse_api_keys_yaml(_api_keys_path)
               logger.info("[Auth] Reloaded %d API keys", len(_api_key_registry))
       except FileNotFoundError:
           if os.environ.get("MCP_REQUIRE_AUTH") == "1":
               logger.error("[Auth] api_keys.yaml not found and MCP_REQUIRE_AUTH=1 — refusing to operate")
               _api_key_registry = {}
           else:
               logger.warning("[Auth] api_keys.yaml not found — registry empty")
       except Exception as exc:
           logger.error("[Auth] Failed to reload api_keys.yaml: %s", exc)

   def start_auth_reloader():
       scheduler = BackgroundScheduler()
       scheduler.add_job(_load_api_keys_if_changed, "interval", seconds=60, id="auth_reload")
       scheduler.start()

   # Initial load at module import:
   _load_api_keys_if_changed()
   ```

3. **At app startup** (in `mcp/app.py` or wherever lifespan is configured), call `start_auth_reloader()`.

4. **F-CA-A07: refuse to start in production with empty registry:**
   ```python
   def assert_auth_ready():
       if os.environ.get("MCP_REQUIRE_AUTH") == "1" and not _api_key_registry:
           raise RuntimeError(
               "MCP_REQUIRE_AUTH=1 but no API keys loaded. Refusing to start."
           )
   # Call from app startup.
   ```

5. **F-CA-A05: replace `api_key[:8] + "…"` with sha256:**
   ```python
   def hash_api_key_for_audit(api_key: str) -> str:
       """Return a non-reversible identifier for audit logs."""
       return "sha256:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
   ```
   In `_authorize`:
   ```python
   pg.log_audit(
       …,
       caller_api_key_hash=hash_api_key_for_audit(api_key),  # renamed field
       …
   )
   ```
   **Important:** add a PostgreSQL migration to rename `audit_logs.caller_api_key` → `caller_api_key_hash` (or keep both during a transition window).

6. **F-CA-A02: replace `_none` placeholder.** Find `resolve_principal` and replace placeholder-role logic with proper `Optional[Principal]` returns:
   ```python
   def resolve_principal(api_key: str, workspace: str) -> Optional[Principal]:
       entry = _api_key_registry.get(api_key)
       if not entry:
           return None
       roles = entry.get("roles", {}).get(workspace) or entry.get("roles", {}).get("*", [])
       if not roles:
           return None  # No role for this workspace → deny
       return Principal(
           id=entry["principal_id"],
           roles=roles,
           api_key=api_key,
           workspace=workspace,
       )
   ```
   Update `derived_roles.yaml` to remove the `_none` placeholder entirely.

7. **F-CA-A03: write DENIES to audit_logs.** In `check_authorization`:
   ```python
   def check_authorization(principal, tool_name, resource_attrs) -> bool:
       allowed = _cerbos_check(principal, tool_name, resource_attrs)
       pg = _get_postgres_client()
       if pg and pg.available:
           pg.log_audit(
               principal_id=principal.id,
               tool_name=tool_name,
               workspace_id=resource_attrs.get("workspace"),
               caller_api_key_hash=hash_api_key_for_audit(principal.api_key),
               authorization=("ok" if allowed else "denied"),  # both ok and denied are recorded
               correlation_id=_correlation_id_ctx.get(""),  # added in W-13
           )
       return allowed
   ```

8. **Add unit tests** for: registry reload, missing-file behavior, sha256 hashing, `_none` rejection, denied-call audit row.

### Verification

- Modifying `api_keys.yaml` and waiting 60s causes the registry to reload (verify in logs).
- Setting `MCP_REQUIRE_AUTH=1` with an empty file causes startup to fail.
- Calls to `audit_logs` table show DENIED rows (after Cerbos refuses).
- The `caller_api_key_hash` column has sha256 prefixes, not raw key fragments.

---

## W-09 — `_default_llm` retry + token-aware synthesis (1 hour for retry, 1 day for full)

**Findings:** F-CC-R01 (retry, 1 hour), F-CC-R02 (synthesis fallback shape, 2 hours), F-CC-R03 (planner JSON parse, 2 hours), F-CC-R04 (synthesis truncation, 1 day).

### Steps

1. Open `src/HybridRAG/code/querier/rlm_orchestrator.py`. Find `_default_llm`.

2. **F-CC-R01: add 3-attempt exponential backoff:**
   ```python
   import random, time
   _MAX_RETRIES = 3
   _BASE_DELAY = 1.0

   def _default_llm(self, system, user, max_tokens=1500, purpose="synthesize"):
       last_exc = None
       for attempt in range(_MAX_RETRIES):
           try:
               client = _get_shared_openai_client()
               model = os.environ.get("AICE_DEFAULT_LLM_MODEL", "gpt-5.2")
               resp = client.chat.completions.create(
                   model=model, temperature=0.1, max_tokens=max_tokens,
                   messages=[{"role": "system", "content": system},
                             {"role": "user", "content": user}],
               )
               return resp.choices[0].message.content or ""
           except Exception as exc:
               last_exc = exc
               # Token refresh on 401
               if "401" in str(exc) and attempt < _MAX_RETRIES - 1:
                   from src.IngestionPipeline.token_manager import get_token
                   get_token(force_refresh=True)
                   continue
               if attempt < _MAX_RETRIES - 1:
                   delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                   logger.warning("[RLM] LLM attempt %d/%d failed (%s); retrying in %.1fs",
                                  attempt + 1, _MAX_RETRIES, exc, delay)
                   time.sleep(delay)
       logger.error("[RLM] LLM call failed after %d attempts: %s", _MAX_RETRIES, last_exc)
       # F-CC-R02: distinguish purpose
       if purpose == "plan":
           return json.dumps({"reasoning": "LLM unavailable", "steps": [
               {"step_id": 1, "intent": "fallback single query", "query": "", "alpha": 0.5}
           ]})
       else:  # synthesize or unknown
           raise LLMUnavailableError(f"Synthesis LLM call failed: {last_exc}") from last_exc
   ```

3. **Define `LLMUnavailableError`** at the top of the module.

4. **F-CC-R02: update `_synthesize` to handle the typed error and surface to caller:**
   ```python
   def _synthesize(self, query, task_type, accumulated, session_context):
       try:
           final = self._llm_fn(system, user, max_tokens=4000, purpose="synthesize")
           tokens = len(final) // 4
           return final, tokens, False  # third value: degraded?
       except LLMUnavailableError:
           return "[LLM synthesis unavailable]", 0, True
   ```
   Update `RLMContext` to include `degraded: bool` and surface in the `rlm_orchestrate` envelope.

5. **F-CC-R03: replace greedy JSON regex with brace-counting parser:**
   ```python
   def _extract_json_object(self, raw: str) -> Optional[Dict]:
       start = raw.find("{")
       if start == -1:
           return None
       depth, in_str, esc = 0, False, False
       for i in range(start, len(raw)):
           c = raw[i]
           if in_str:
               if esc: esc = False
               elif c == "\\": esc = True
               elif c == '"': in_str = False
           else:
               if c == '"': in_str = True
               elif c == "{": depth += 1
               elif c == "}":
                   depth -= 1
                   if depth == 0:
                       try:
                           return json.loads(raw[start:i+1])
                       except json.JSONDecodeError:
                           return None
       return None

   def _parse_plan(self, raw, original_query):
       plan = self._extract_json_object(raw)
       if plan is None or "steps" not in plan:
           logger.warning("[RLM] Planner returned no parseable plan; falling back. Raw: %r", raw[:300])
           AICE_RLM_PLANNER_FALLBACKS.labels(reason="no_json").inc()
           return {"reasoning": "fallback", "steps": [
               {"step_id": 1, "intent": "direct query", "query": original_query, "alpha": 0.5}
           ]}
       return plan
   ```
   Note `query[:200]` is replaced by the full query.

6. **F-CC-R04: replace hard-coded synthesis truncation with model-aware budgeting:**
   ```python
   _MODEL_CONTEXT_TOKENS = {
       "gpt-5.2": 128000,
       "gpt-4o": 128000,
       "gpt-4o-mini": 128000,
   }
   def _synthesize(self, query, task_type, accumulated, session_context):
       model = os.environ.get("AICE_DEFAULT_LLM_MODEL", "gpt-5.2")
       max_ctx = _MODEL_CONTEXT_TOKENS.get(model, 32000)
       reserved = 4000  # for response
       budget = max_ctx - reserved - estimate_tokens(query) - 1000  # for system prompt etc
       parts = [f"Original task: {query}\n"]
       used = estimate_tokens(parts[0])
       for step_id, answer in sorted(accumulated.items()):
           tokens = estimate_tokens(answer)
           if used + tokens > budget:
               logger.info("[RLM] Synthesis budget reached at step %d", step_id)
               break
           parts.append(f"--- Sub-query {step_id} ---\n{answer}\n")
           used += tokens
       …
   ```

7. **Add unit tests** covering: retry on transient failure, planner fallback path, JSON brace-counting parser, synthesis budget limits.

### Verification

- Unit test simulating a 502 followed by 200 produces successful response after retry.
- Planner fallback emits Prometheus counter `aice_rlm_planner_fallbacks_total`.
- Synthesis no longer truncates at hard 2000 chars; uses model-aware budget.
- Master Gaps drift gate (W-22) `TestREV3H03_GPT4IFXRetry` passes.

---

## W-10 — `READ` access mode on KI Cypher (5 minutes)

**Findings:** F-CC-K02 (Cluster C).

### Steps

1. Open `src/HybridRAG/code/querier/knowledge_intelligence.py`. Find `_run_cypher`.

2. Add `default_access_mode=neo4j.READ_ACCESS`:
   ```python
   import neo4j

   def _run_cypher(self, cypher, params, ws="illd"):
       if not self._neo4j:
           return []
       try:
           with self._neo4j.session(
               database=self._db(ws),
               default_access_mode=neo4j.READ_ACCESS,  # F-CC-K02
           ) as s:
               return [dict(r) for r in s.run(cypher, params)]
       except Exception as e:
           logger.error("Cypher failed: %s", e)
           return []
   ```

### Verification

- The Master Gaps drift gate `TestREV1H14_ReadAccessMode` (W-22) passes.

---

**Sprint 26 total: ~9 person-days. 6 days for top-6 fixes from Pass 3 §1, plus W-04, W-05, W-06 which deliver high-leverage cross-cutting fixes.**

Findings closed in Sprint 26: 22+ (F-CB-01, F-CB-02, F-CB-03, F-CB-09, F-CB-10, F-CB-17, F-CC-K01, F-CC-K02, F-CC-R01, F-CC-R02, F-CC-R03, F-CC-R04, F-CD-B01, F-CD-B02, F-CD-I01, F-CD-Q01, F-CD-Q02, F-CA-A01, F-CA-A02, F-CA-A03, F-CA-A05, F-CA-A07, F-CA-S04, F-CA-A04, F-CF-X01, F-CE-O01, F-CF-X04, F-CA-I01).

---

# SPRINT 27 — "Observability" (~10 days)

Goal: make the system visible. Build the silent-failure dashboard + per-DA productivity dashboard. Wire correlation IDs and typed errors throughout.

---

## W-11 — Per-DA Prometheus instrumentation (3 days)

**Findings:** F-P5-M01 (Pass 5), F-CB-05 (Cluster B).

### Steps

1. **First verify F-CB-05** — does `_tool_name_ctx.set(...)` happen anywhere? `git grep "_tool_name_ctx.set"`. If no setter exists, the central tool-metric is silently no-op.

2. **Add `_set_tool_name` decorator** to wrap every `@mcp.tool()`:
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
   Apply across all tool registrations.

3. **F-P5-M01: add `da_name` and `tier` context vars:**
   ```python
   _da_name_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_da_name", default="unknown")
   _da_tier_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_da_tier", default="unknown")
   ```
   Set in `_authorize` after principal resolution:
   ```python
   da_name = principal.id.replace("_assistant", "")
   _da_name_ctx.set(da_name)
   _da_tier_ctx.set(principal.roles[0] if principal.roles else "unknown")
   ```

4. **Update `TOOL_REQUESTS_TOTAL` and `TOOL_REQUEST_DURATION`** to include `da` and `tier` labels:
   ```python
   TOOL_REQUESTS_TOTAL = Counter(
       "aice_tool_requests_total", "Tool calls",
       ["tool", "status", "da", "tier"],
   )
   TOOL_REQUEST_DURATION = Histogram(
       "aice_tool_request_duration_seconds", "Tool call latency",
       ["tool", "da"],
   )
   ```

5. **Implement productivity metrics** (Pass 5 §4.2 catalog):
   - `aice_da_session_duration_seconds{da, task_type}` — Histogram in `session_end`.
   - `aice_da_session_outcomes_total{da, task_type, outcome}` — Counter in `submit_human_feedback` / `complete_review`.
   - `aice_da_context_assembly_tokens{da, task_type}` — Histogram in `build_context` / `rlm_orchestrate`.
   - `aice_da_first_result_latency_seconds{da, task_type}` — Histogram from `session_start` to first `evaluate_confidence`.
   - `aice_da_review_iterations` — derived in PostgreSQL.
   - `aice_da_pattern_hits_total{da, task_type}` — Counter in PatternStore (after W-07's W-CB-17 fix).
   - `aice_da_session_llm_tokens_total{da, task_type, llm_call_type}` — Counter at every `_default_llm` call.

6. **F-P5-M02: make `task_type` mandatory at `session_start`** (default to `"adhoc"` for sessionless calls).

### Verification

- Calling any tool produces `aice_tool_requests_total{tool="...", da="cia", tier="public", status="ok"}` increment.
- Grafana panel "Top tools by DA" populates.

---

## W-12 — Silent-failure dashboard (Grafana panel + alert rules) (1 day)

**Findings:** Pattern #4 from Pass 3 §2. Surfaces gauges added in W-07.

### Steps

1. **Define Prometheus alert rules** in `monitoring/prometheus/aice_silent_failures.yml`:
   ```yaml
   groups:
     - name: aice_silent_failures
       interval: 30s
       rules:
         - alert: AICEServiceDown
           expr: aice_service_up == 0
           for: 2m
           labels: {severity: critical}
           annotations:
             summary: "AICE service {{ $labels.service }} is down"
         - alert: AICELearningLoopDisabled
           expr: aice_learning_loop_active == 0
           for: 5m
           labels: {severity: warning}
         - alert: AICESandboxOverlayInactive
           expr: rate(aice_sandbox_overlay_prod_nodes[5m]) == 0 and rate(aice_sandbox_uploads_total[5m]) > 0
           for: 5m
           labels: {severity: warning}
         - alert: AICERLMHighFallbackRate
           expr: rate(aice_rlm_planner_fallbacks_total[5m]) / rate(aice_rlm_calls_total[5m]) > 0.1
           for: 5m
           labels: {severity: warning}
   ```

2. **Create Grafana panel** at `monitoring/grafana/dashboards/aice_silent_failures.json`. Single dashboard with one row per gauge:
   - Service health row: 12 single-stat panels for `aice_service_up`.
   - Functional health row: `aice_learning_loop_active`, `aice_sandbox_overlay_active`, `aice_vector_search_available`, `aice_c_parser_clang_available`.
   - Reliability row: `aice_rlm_planner_fallbacks_total` rate, `aice_post_ingest_callback_failures_total` rate.

3. **Document** in `docs/OPERATIONS.md`: how to read the dashboard, what each alert means, response runbook.

### Verification

- Stop Neo4j in dev → `aice_service_up{service="neo4j_illd"}` reads 0 → alert fires within 2 min.

---

## W-13 — Correlation-ID propagation + typed errors (2 days)

**Findings:** F-CB-04 (Cluster B), F-CB-08 (typed errors).

### Steps

1. **Generate correlation_id at request start** (in ASGI middleware or `_authorize`):
   ```python
   import uuid
   _correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("_correlation_id", default="")

   class _APIKeyMiddleware:
       async def __call__(self, scope, receive, send):
           cid = uuid.uuid4().hex
           _correlation_id_ctx.set(cid)
           …
   ```

2. **Propagate to logs**: configure Python logging to include `correlation_id` from contextvars in every log record.

3. **Propagate to audit log rows**: every `pg.log_audit` call includes `correlation_id`.

4. **Propagate to `_ok` / `_err` envelopes**:
   ```python
   def _ok(data, **kw):
       return json.dumps({"error": False, "data": data,
                          "correlation_id": _correlation_id_ctx.get("")})
   def _err(code, message):
       return json.dumps({"error": True, "code": code, "message": message,
                          "correlation_id": _correlation_id_ctx.get("")})
   ```

5. **F-CB-08: typed error classification.** Create `_err_from_exc(exc, component)`:
   ```python
   def _err_from_exc(exc: Exception, component: str) -> str:
       if isinstance(exc, asyncio.CancelledError):
           raise  # never absorb cancellation
       elif isinstance(exc, neo4j.exceptions.ServiceUnavailable):
           return _err("BACKEND_UNAVAILABLE", f"Neo4j unavailable: {exc}")
       elif isinstance(exc, qdrant_client.http.exceptions.UnexpectedResponse):
           return _err("BACKEND_UNAVAILABLE", f"Qdrant: {exc}")
       elif isinstance(exc, asyncio.TimeoutError):
           return _err("INTERNAL_TIMEOUT", "Operation timed out")
       elif isinstance(exc, FileNotFoundError):
           return _err("INVALID_INPUT", str(exc))
       elif isinstance(exc, ValueError):
           return _err("INVALID_INPUT", str(exc))
       else:
           logger.exception("[%s] Unhandled error", component)
           return _err("INTERNAL_ERROR", str(exc))
   ```

6. **Retrofit ~60 tool handlers** to use `_err_from_exc` in their except blocks.

### Verification

- Audit log rows for a single request share the same correlation_id.
- Tool errors return typed codes (`BACKEND_UNAVAILABLE`, `INVALID_INPUT`, etc.) instead of generic `INTERNAL_ERROR`.

---

## W-14 — DA productivity Grafana dashboard (1 day)

**Findings:** Pass 5 §4.4.

### Steps

1. **Create `monitoring/grafana/dashboards/aice_da_productivity.json`** with the 5-row layout from Pass 5 §4.4:
   - Row 1: DA usage heatmap, active DAs count.
   - Row 2: AUTO rate by DA, session duration p50/p95.
   - Row 3: TTFR p50, review iterations.
   - Row 4: Tokens delivered per session, pattern reuse rate.
   - Row 5: Monthly LLM cost by DA, cost per AUTO-approved output.

2. **Create PostgreSQL view `da_productivity`** per Pass 5 §4.3 SQL.

3. **Document** the dashboard in `docs/OPERATIONS.md`.

### Verification

- Dashboard loads with non-zero data for the 6 provisioned DAs.

---

## W-15 — Provision API keys for the 15 unprovisioned DAs (1 day)

**Findings:** F-P5-D01.

### Steps

1. **For each unprovisioned DA, decide:** PROVISION / PLANNED / SHARED.
   - 21 DAs total, 6 provisioned today (gest, cia, reva, acra, saga, triplea).
   - Unprovisioned: prq, rma, atra, cta, geca, page, gevt, atqa, sava, sasa, hzop, dfa, mira, voltai, kw, stoptyping.

2. **For DAs marked PROVISION:** add to `mcp/auth/api_keys.yaml`:
   ```yaml
   "key-mira-001":
     principal_id: "mira_assistant"
     roles:
       illd: ["public"]
       mcal: ["public"]
   ```

3. **For DAs marked PLANNED:** update `requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-DA-001 status from "IMPLEMENTED" to "PARTIALLY IMPLEMENTED — 6 of 21 DAs provisioned, 15 PLANNED".

4. **For DAs marked SHARED** (e.g., all Safety DAs share `key-safety-pool-001`): document the sharing arrangement in `docs/DA_INTEGRATION.md`.

5. **F-P5-D02: align RLM `DA_TASK_MAPPING` codes** with `AI_USAGE_POLICY.md`:
   - `SASA` → `SAAN` (Safety Analyst code in docs)
   - `DaFaA` → `DFA`
   - `HazopA` → `HZOP`
   - `PRQ_Drafter` → `PRQ`

6. **F-P5-D03: decide on StopTyping.** Either document the use case or remove from `DA_TASK_MAPPING`.

### Verification

- All 21 DA codes in `DA_TASK_MAPPING` match `AI_USAGE_POLICY.md`.
- Grafana DA productivity dashboard shows entries for all provisioned DAs.

---

**Sprint 27 total: ~8 person-days.**

Findings closed in Sprint 27: F-CB-04, F-CB-05, F-CB-08, F-P5-D01, F-P5-D02, F-P5-D03, F-P5-M01, F-P5-M02 + the dashboard infrastructure that operationalizes all silent-failure findings.

---

# SPRINT 28 — "Consolidation" (~10 days)

Goal: land cross-cutting refactors. Single retry helper. ILLD builder unification. Layer-dependency contracts.

---

## W-16 — Single retry helper `src/_common/retry.py` (3 days)

**Findings:** Pattern #2 from Pass 3 §2. Replaces ~12 distinct retry implementations across F-CD-X02 (Cluster D), F-CC-R01 (already partially fixed in W-09), F-CE-S01 (Cluster E SWA), F-CF-X03 (Cluster F).

### Steps

1. **Create** `src/_common/retry.py`:
   ```python
   """Unified retry/backoff for HTTP, Neo4j, LLM, and connector operations."""
   from __future__ import annotations
   from dataclasses import dataclass, field
   import logging, random, time
   from typing import Callable, Tuple, TypeVar

   logger = logging.getLogger(__name__)
   T = TypeVar("T")

   @dataclass
   class RetryConfig:
       max_attempts: int = 3
       backoff_base: float = 1.0
       backoff_multiplier: float = 2.0
       max_backoff: float = 60.0
       jitter: float = 0.5
       non_retryable: Tuple[type, ...] = ()
       retryable: Tuple[type, ...] = (ConnectionError, TimeoutError)
       on_attempt_failed: Callable = lambda exc, attempt, total: None

   def with_retry(operation: str, fn: Callable[..., T], *args, config: RetryConfig, **kwargs) -> T:
       last_exc = None
       for attempt in range(1, config.max_attempts + 1):
           try:
               return fn(*args, **kwargs)
           except config.non_retryable:
               raise
           except Exception as exc:
               last_exc = exc
               config.on_attempt_failed(exc, attempt, config.max_attempts)
               if attempt < config.max_attempts:
                   delay = min(
                       config.backoff_base * (config.backoff_multiplier ** (attempt - 1))
                       + random.uniform(0, config.jitter),
                       config.max_backoff,
                   )
                   logger.warning("[Retry] %s attempt %d/%d failed (%s); retrying in %.1fs",
                                  operation, attempt, config.max_attempts, exc, delay)
                   time.sleep(delay)
       logger.error("[Retry] %s exhausted %d attempts: %s", operation, config.max_attempts, last_exc)
       raise last_exc

   class LLMRetryClient:
       """Wraps an OpenAI-compatible client with token-refresh + retry."""
       def __init__(self, client_factory, token_provider, retry_config):
           self._client_factory = client_factory
           self._token_provider = token_provider
           self._client = client_factory()
           self._config = retry_config

       def chat_completion(self, **opts) -> str:
           def _call():
               try:
                   return self._client.chat.completions.create(**opts).choices[0].message.content
               except Exception as exc:
                   if "401" in str(exc):
                       self._token_provider(force_refresh=True)
                       self._client = self._client_factory()
                       raise  # retry will pick this up
                   raise
           return with_retry("LLM", _call, config=self._config)
   ```

2. **Replace `_default_llm` retry from W-09** with `LLMRetryClient`.

3. **Replace `pdf_pipeline._process_batch_with_retry`** with `with_retry`.

4. **Replace `illd_swa_parser` LLM enrichment retry** with `with_retry`.

5. **Replace `KnowledgeGraphBuilder._write_tx`, `ILLDKnowledgeGraphBuilder._write_tx`, `ILLDKGBuilder._write`** — three Neo4j-write retries collapse into one (F-CD-X02):
   ```python
   _NEO4J_RETRY = RetryConfig(
       max_attempts=3, backoff_base=2.0, max_backoff=8.0,
       retryable=(ServiceUnavailable, TransientError, OSError),
   )

   def _write_tx(self, cypher, params=None):
       def _do_write():
           with self._driver.session(database=self._db) as session:
               session.execute_write(lambda tx: tx.run(cypher, params or {}).consume())
       with_retry("neo4j_write", _do_write, config=_NEO4J_RETRY)
   ```

6. **Update `batch_ingestion.Neo4jBatchWriter` to use retry** (F-CD-X02 silent data loss):
   ```python
   def merge_nodes_batch(self, nodes, label, merge_key):
       def _merge_chunk(chunk):
           …
       for i in range(0, len(nodes), self._batch_size):
           batch = nodes[i:i + self._batch_size]
           with_retry("batch_merge_nodes", _merge_chunk, batch, config=_NEO4J_RETRY)
   ```

7. **Replace JamaConnector, PolarionConnector, BitbucketConnector retry helpers** (F-CF-X03):
   ```python
   _CONNECTOR_RETRY = RetryConfig(
       max_attempts=3, backoff_base=1.0,
       non_retryable=(JamaAuthError, JamaClientError),  # parameterized per connector
       retryable=(httpx.RequestError, JamaServerError),
   )
   def _request(self, method, path, params=None, json_body=None):
       def _do():
           response = self._client.request(method, path, params=params, json=json_body)
           if response.status_code == 401:
               raise JamaAuthError(...)
           …
           return response.json()
       return with_retry(f"jama_{method}_{path}", _do, config=_CONNECTOR_RETRY)
   ```

8. **JenkinsConnector** (F-CF-N01) — replace `except (NoBuildData, Exception): pass` blocks with explicit error types and `with_retry`:
   ```python
   def _safe_get_timestamp(build):
       def _do(): return build.get_timestamp()
       try:
           return with_retry("jenkins_timestamp", _do, config=_CONNECTOR_RETRY)
       except (NotFound, NoBuildData):
           return None  # legitimately absent
       except Exception as exc:
           logger.warning("Jenkins timestamp fetch failed: %s", exc)
           return None
   ```

### Verification

- `grep -rn "for attempt in range" src/ | grep -v _common/retry.py | wc -l` → small number (only legitimate non-retry loops).
- All connectors share consistent retry-shaped logging.
- `aice_retry_attempts_total{operation, attempt}` metric (added in retry helper) shows aggregate retry stats.

---

## W-17 — Unify ILLD KG builders (3 days)

**Findings:** F-CD-X01 (Cluster D), F-A02 (Pass 2), F-A05 (Pass 2 deferred to Sprint 30).

### Steps

1. **Choose canonical version.** `ILLDKGBuilder` (in `illd_kg_builder.py`) is cleaner — per-source `ingest_swa`/`ingest_sfr`/etc. methods, explicit `create_cross_source_relationships`. Promote it.

2. **Refactor** `ILLDKnowledgeGraphBuilder` (in `build_knowledge_graph.py`) into a thin wrapper:
   ```python
   class ILLDKnowledgeGraphBuilder:
       """CLI wrapper over ILLDKGBuilder."""
       def __init__(self, neo4j_cfg, module, data_path, dry_run=False, clear_db=False):
           self._builder = ILLDKGBuilder(
               neo4j_cfg=neo4j_cfg, module=module, dry_run=dry_run,
           )
           self.data_path = data_path
           self.clear_db = clear_db

       def build(self):
           if self.clear_db:
               self._builder._clear_module_data()  # uses W-03's module-scoped clear
           # Discover files in self.data_path → dispatch to builder.ingest_swa(), etc.
           …
   ```

3. **Standardize ID format.** Pick the `FUNC_{name}` convention (the more cleanly factored version) and migrate any existing data.

4. **Add a one-time migration script** at `scripts/migrate_illd_ids.py` that detects `(:Function)` nodes without `FUNC_` prefix and re-keys them.

5. **Add a smoke test** that ingests the same iLLD module via both code paths and verifies no duplicate `(:Function)` nodes exist:
   ```python
   def test_no_duplicate_functions_after_dual_ingestion(neo4j_test_db):
       # Ingest via path A
       builder_a = ILLDKnowledgeGraphBuilder(...)
       builder_a.build()
       # Ingest via path B (legacy entry point still works)
       builder_b = ILLDKGBuilder(...)
       builder_b.ingest_swa(swa_data)
       # Assert no duplicates
       result = neo4j_test_db.run(
           "MATCH (a:Function), (b:Function) "
           "WHERE a.name = b.name AND a.id <> b.id "
           "RETURN count(*) AS dups"
       ).single()
       assert result["dups"] == 0
   ```

6. **F-CD-B04: also fix `_create_edges` label-aware MATCH** in the same PR (small dependency on F-CD-X01):
   ```python
   def _create_edges(self, edges):
       by_type = defaultdict(list)
       for e in edges:
           by_type[e.relationship_type].append({
               "source_id": e.source_id, "target_id": e.target_id,
               "from_label": e.source_label, "to_label": e.target_label,
               "props": e.properties,
           })
       for rtype, items in by_type.items():
           by_label_pair = defaultdict(list)
           for item in items:
               by_label_pair[(item["from_label"], item["to_label"])].append(item)
           for (from_label, to_label), pair_items in by_label_pair.items():
               sanitize_label(from_label); sanitize_label(to_label); sanitize_rel_type(rtype)
               cypher = (
                   f"UNWIND $edges AS e "
                   f"MATCH (a:{from_label} {{id: e.source_id}}) "
                   f"MATCH (b:{to_label} {{id: e.target_id}}) "
                   f"MERGE (a)-[r:{rtype}]->(b) SET r += e.props"
               )
               self._write_tx(cypher, {"edges": pair_items})
   ```

### Verification

- The dual-implementation gate (W-22) passes.
- Smoke test confirms no duplicate function nodes.

---

## W-18 — Layer-dependency contracts via import-linter (2 days)

**Findings:** F-A03 ContextBuilder migration (Pass 2), F-A09 (Pass 2 layer rules), Pattern #3 fast-detection.

### Steps

1. **Land** the `.import-linter.toml` from Pass 4b §8.2.

2. **Resolve the F-A03 ContextBuilder duality.** Move the canonical Sprint 8 `ContextBuilder` from `src/HybridRAG/code/querier/context_builder.py` to `src/MemoryLayer/memory/context_builder.py`. Re-export from HybridRAG via:
   ```python
   # src/HybridRAG/code/querier/context_builder.py
   from src.MemoryLayer.memory.context_builder import ContextBuilder, ContextSlot, ContextBudget
   __all__ = ["ContextBuilder", "ContextSlot", "ContextBudget"]
   ```

3. **Rename** the legacy Sprint 2 `ContextBuilder` in `src/MemoryLayer` to `LegacyContextBuilder` (preserved for E2E test compatibility).

4. **Update imports across consumers** so they import from `src.MemoryLayer.memory.context_builder`.

5. **Run import-linter:** `lint-imports --config .import-linter.toml`. The contract `MemoryLayer must not depend on HybridRAG` should now pass.

6. **Promote `lint:import-linter` job to `allow_failure: false`** in `.gitlab-ci.yml`.

### Verification

- `lint-imports` passes.
- All consumers find `ContextBuilder` at the new location.
- E2E tests still pass with the renamed `LegacyContextBuilder`.

---

## W-19 — `aice-da-sdk` Python package (5 days, parallel-trackable)

**Findings:** F-P5-I03 (Pass 5).

### Steps

1. **Create new package `aice_da_sdk/`** at repo root or as a separate repo:
   ```
   aice_da_sdk/
       __init__.py
       session.py        # AICESession context manager
       tools.py          # Per-tool wrappers
       confidence.py     # evaluate_confidence + feedback helpers
       errors.py         # SDK exceptions
   tests/
   pyproject.toml        # publishable as aice-da-sdk
   ```

2. **Implement `AICESession`:**
   ```python
   from contextlib import contextmanager
   import httpx, uuid

   class AICESession:
       def __init__(self, api_key, workspace, module=None, base_url=None):
           self._client = httpx.Client(
               base_url=base_url or os.environ["AICE_URL"],
               headers={"Authorization": f"Bearer {api_key}"},
               timeout=60.0,
           )
           self._workspace = workspace
           self._module = module
           self._session_id = None

       @classmethod
       @contextmanager
       def open(cls, api_key, workspace, da_name, task_type, module=None):
           sess = cls(api_key, workspace, module)
           sess._session_id = f"{da_name}_{int(time.time())}"
           sess._call("session_start", {
               "session_id": sess._session_id,
               "assistant_name": da_name,
               "module_context": module,
               "task_type": task_type,
           })
           try:
               yield sess
           finally:
               sess._call("session_end", {"session_id": sess._session_id})

       def search(self, query, top_k=10, alpha=0.5):
           result = self._call("search_database", {
               "query": query, "workspace": self._workspace,
               "module_filter": self._module, "alpha": alpha, "top_k": top_k,
           })
           return result["data"]["results"]

       def build_context(self, query, search_results, max_tokens=8192):
           result = self._call("build_context", {
               "session_id": self._session_id, "query": query,
               "search_results": search_results, "max_tokens": max_tokens,
           })
           return result["data"]

       def evaluate_confidence(self, response, context):
           return self._call("evaluate_confidence", {
               "response": response, "context": context, "session_id": self._session_id,
           })

       def submit_feedback(self, response_id, decision, **kwargs):
           return self._call("submit_human_feedback", {
               "response_id": response_id, "decision": decision, **kwargs,
           })

       def _call(self, tool, args):
           resp = self._client.post("/", json={
               "jsonrpc": "2.0", "method": "tools/call",
               "params": {"name": tool, "arguments": args}, "id": uuid.uuid4().hex,
           })
           result = resp.json()
           if result.get("error", False):
               raise AICEToolError(result.get("code"), result.get("message"))
           return result
   ```

3. **Document with example** at `aice_da_sdk/README.md`:
   ```python
   from aice_da_sdk import AICESession

   with AICESession.open(api_key="key-cia-001", workspace="illd",
                         da_name="CIA", task_type="code_generation",
                         module="Adc") as sess:
       results = sess.search("Adc_StartGroupConversion implementation")
       ctx = sess.build_context("Generate the implementation", results)
       # CIA does its work using ctx['assembled_context']
       generated_code = my_llm.generate(ctx['assembled_context'])
       eval_result = sess.evaluate_confidence({"code": generated_code}, ctx)
       sess.submit_feedback(eval_result['data']['response_id'], decision="APPROVE")
   ```

4. **Publish to internal PyPI** (or pin via git URL during the trial period).

### Verification

- One DA's existing integration code shrinks from ~150 lines to ~30 using the SDK.
- SDK unit tests cover happy path + error cases.

---

**Sprint 28 total: ~13 person-days.** Slightly over budget — defer W-19 to Sprint 29 if needed. W-19 can also be parallel-tracked by a different engineer.

Findings closed in Sprint 28: F-CC-R01 (full), F-CD-X02, F-CE-S01, F-CF-X03, F-CF-N01, F-CD-X01, F-CD-B04, F-A03, F-P5-I03.

---

# SPRINT 29 — "Hardening" (~10 days)

Goal: land remaining medium-severity items. Parser robustness, connector reliability, miscellaneous cleanups.

---

## W-20 — Parser hardening bundle (3 days)

**Findings:** F-CE-C01 (libclang detection), F-CE-T01 (xlsx memory), F-CE-A01 (ARXML strip), F-CE-T02 (regex IGNORECASE), F-CE-A02 (param type fallback), F-CE-A03 (ARXML namespace), F-CE-S03 (SWA module strip), F-CD-B03 (UNWIND validation).

### Steps

1. **F-CE-C01 — unify libclang detection:**
   - Create `src/IngestionPipeline/Parsers/_libclang_helpers.py` consolidating the two detection functions from `c_parser.py` and `config_struct_resolver.py`.
   - Use `platform.system()` for OS-aware extension.
   - Add Prometheus gauge `aice_c_parser_clang_available`.
   - Promote regex fallback log from DEBUG to WARNING.
   - Add `parser_method: "clang" | "regex"` to output dicts.

2. **F-CE-T01 — xlsx workbook size cap:**
   ```python
   def parse_testspec_workbook(xlsx_path, module):
       MAX_BYTES = 100 * 1024 * 1024  # 100 MB
       size = Path(xlsx_path).stat().st_size
       if size > MAX_BYTES:
           raise ValueError(f"Workbook too large ({size} bytes > {MAX_BYTES})")
       …
   ```
   Add Prometheus gauge for peak memory during test run.

3. **F-CE-A01 — ARXML structural validation post-template-strip:** count opening/closing tags, refuse to ingest if mismatched.

4. **F-CE-T02 — strip `re.IGNORECASE` from `_PRQ_REF_RE`** (Jama IDs are uppercase) and `_HAZOP_REF_RE`. Or normalize matches to uppercase.

5. **F-CE-A02 — `_extract_param_type` returns `None` instead of raw tag fallback:** caller skips the entry rather than ingesting a polluted node.

6. **F-CE-A03 — dynamic ARXML namespace detection:**
   ```python
   m = re.match(r"\{(.+?)\}", root.tag)
   ns = "{" + m.group(1) + "}" if m else _NS
   ```

7. **F-CE-S03 — anchor `re.sub` for SWA module name strip** (only strip prefix/suffix, not mid-word).

8. **F-CD-B03 — UNWIND batch validation pre-batching:**
   ```python
   def _validate_node(props, ntype):
       if not props.get("id"):
           raise ValueError(f"Missing id for {ntype} node")
       for k in props:
           if not _PROP_NAME_RE.match(k):
               raise ValueError(f"Invalid property name: {k}")
       return props
   items = [_validate_node(p, ntype) for p in items]
   ```
   Plus per-item failure recovery via batch-size-1 retry on failure.

### Verification

- Unit tests for each parser fix.
- Manual tests: ingest a known-broken ARXML, broken xlsx, etc.

---

## W-21 — Connector hardening bundle (3 days)

**Findings:** F-CF-X02 (`_SecretStr` wrapper), F-CF-P01 (Polarion JWT refresh), F-CF-J01 (Jama project cache), F-CF-J02 (Jama silent sentinels), F-CF-N02 (Jenkins error specificity), F-CF-B01 (Bitbucket bulk fetch failures), F-CF-B02 (Bitbucket path validation), F-CF-X05 (httpx client recreation), F-CF-P02 (Polarion validation project).

### Steps

1. **F-CF-X02 — `_SecretStr` wrapper:**
   ```python
   class _SecretStr:
       __slots__ = ("_value",)
       def __init__(self, value): self._value = value
       def get(self): return self._value
       def clear(self): self._value = ""
       def __repr__(self): return "_SecretStr(***)"
       def __str__(self): return "***"
   ```
   Apply across all 4 connectors. Zero on `close()`.

2. **F-CF-P01 — Polarion token-provider callable:**
   ```python
   def __init__(self, base_url, token_provider, ...):
       self._token_provider = token_provider
       self._refresh_token()
   def _refresh_token(self):
       new_token = self._token_provider()
       self._client.headers["Authorization"] = f"Bearer {new_token}"
   ```
   Detect 401 in `_request` and refresh.

3. **F-CF-J01 — Jama project cache:** TTL-cached `get_projects()`, 5-minute default.

4. **F-CF-J02 — Jama fail-loud on missing id:** raise `ValueError` instead of `id=-1` sentinel.

5. **F-CF-N02 — Jenkins error specificity:** distinguish `JenkinsTLSError` (cert), `JenkinsConfigError` (URL/config), `JenkinsConnectionError` (real network) at the connect path.

6. **F-CF-B01 — Bitbucket bulk-fetch result with failures:** return `BulkFetchResult(files, failures)` dataclass.

7. **F-CF-B02 — Bitbucket path validation:** reject `..`, restrict character set.

8. **F-CF-X05 — httpx client recreation on TLS errors:** add `_recreate_client()` helper triggered on `httpx.ConnectError` with SSL message.

9. **F-CF-P02 — Polarion validation project env-configurable:** `POLARION_VALIDATION_PROJECT_ID` env var.

### Verification

- Memory dump of connector instance does NOT show plaintext credentials.
- Polarion connector survives a token rotation event.
- Bitbucket bulk fetch reports failures explicitly.

---

## W-22 — CI gate rollout (1 day wiring + 4 hours triage = 1.5 days)

**Findings:** Pass 4b deliverable.

### Steps

1. **Drop the 8 CI artifact files** from Pass 4b §0 into the repo:
   - `.gitlab-ci.yml` modifications.
   - `tests/ci/test_consistency_gates.py`.
   - `tests/ci/test_security_gates.py`.
   - `tests/ci/test_master_gaps_drift.py`.
   - `scripts/ci/grep_gates.sh`.
   - `scripts/ci/check_dual_implementations.sh`.
   - `pyproject.toml` additions.
   - `.import-linter.toml`.
   - `scripts/ci/sca_scan.sh`.

2. **Set every gate to `allow_failure: true`** initially.

3. **Push to a feature branch** and run the pipeline. Triage failures:
   - Each failure → real issue (open ticket) or false positive (refine the pattern) or already-deferred (add `# noqa: aice-<gate-id>: <reason>` with explanation).

4. **Promote gates to blocking after Sprint 26 fixes land:**
   - `gates:filesystem-case` (after F-A01).
   - `gates:grep` (after the Sprint 26 P0 fixes).
   - `gates:consistency` `TestToolTiers` and `TestCerbosPolicy` (after Pass 1 D01/D02 fixes).
   - `gates:security` `TestCypherSafety` and `TestTLSDiscipline` (after W-04, W-06).

### Verification

- CI pipeline runs on every MR.
- A test PR introducing a known antipattern (e.g., `verify_ssl=False`) is rejected by the pipeline.

---

## W-23 — Pass 1 doc/code drift cleanup (1 day)

**Findings:** F-D01 through F-D23 (Pass 1, 23 findings). Many are already closed by W-04 through W-08; this workstream closes the rest.

### Steps

1. **F-D01: tool-tier registration audit.** Run the consistency gate, fix any tools missing from `tool_tiers.py`.

2. **F-D02: Cerbos policy duplicates.** Run the consistency gate, dedupe entries.

3. **F-D03: phantom tool names in requirements docs.** Run the consistency gate, fix or allowlist entries.

4. **F-D04: confidence scoring 7-signal documentation.** Update `docs/architecture/review-gate.md` with the canonical signal list (per Pass 1 §5).

5. **F-D05: PatternStore Qdrant-only.** Already done in W-07.

6. **F-D06, F-D14: tool count alignment.** Update `docs/DOCUMENTATION.md`, `OVERVIEW.md`, README to match actual count.

7. **F-D07 through F-D23: smaller doc-code drift items.** Bulk-fix in this workstream — each is 5-15 minutes.

8. **F-CD-B12: byte-corruption from cp1252 → UTF-8.** Single `sed` pass per Pass 4b grep gate G-04:
   ```bash
   find src/ -name '*.py' | xargs sed -i 's/ΓåÆ/→/g; s/ΓÇª/…/g; s/Γèó/⊠/g'
   ```

### Verification

- All Pass 1 finding gates pass.
- Documentation tool-count claims match `_registered_tool_names()`.

---

## W-24 — Pass 2 architectural items deferred to Sprint 30 (1 day in Sprint 29 for prep)

**Findings:** F-A04 (mcp_server.py split), F-A07 (legacy KG), F-A10 (mcp/ → aice_mcp/ rename), Pass 2 §5 target architecture.

These are large items. Sprint 29 prep:

1. **Document the target architecture** (Pass 2 §5) as ADRs.
2. **Plan F-A10 rename PR** as a single sweep — touches sys.path manipulations, all imports.
3. **Decide on F-A07 legacy `query_knowledge_graph.py` fate** — delete or feature-flag.
4. **Schedule Sprint 30 for execution.**

---

## W-25 — Remaining Cluster A/B/C/D/E/F medium items (2 days)

**Findings:** the ~40 remaining medium-severity items not covered in W-01 through W-24.

### Steps

1. Triage the medium-severity items. Many are 5-15 minutes each.

2. **Bulk-fix the trivial ones** (cosmetic docstring updates, log-level changes, small refactors):
   - F-CB-12 through F-CB-24
   - F-CE-S04 (LLM model defaults consolidation via `AICE_DEFAULT_LLM_MODEL` env)
   - F-CC-R05 through F-CC-R11 (RLM minor fixes)
   - F-CC-C02 through F-CC-C06 (ContextBuilder minor)
   - F-CD-B05 through F-CD-B13 (build_knowledge_graph minor)
   - F-CD-I02 through F-CD-I07 (illd_kg_builder minor)
   - F-CE-X01 through F-CE-X04 (parser cross-cutting minor)
   - F-CE-* miscellaneous parsers
   - F-CF-J03, F-CF-P03, F-CF-P04, F-CF-B03

3. **Track in JIRA** as small individual tickets with effort 0.5-2 hours each.

### Verification

- All Pass 3 medium-severity tickets closed.

---

**Sprint 29 total: ~10 person-days.**

Findings closed in Sprint 29: ~50+ medium-severity items.

---

# SPRINT 30 — "Architectural" (~10 days, optional)

Goal: land the Pass 2 architectural items if business need justifies. These are large refactors with diminishing day-to-day value but high long-term payoff.

---

## W-26 — Pass 2 architectural execution (10 days)

**Findings:** F-A04, F-A07, F-A10, Pass 2 §5 target architecture, F-A05 (move ILLD KG builder to IngestionPipeline).

### Steps

1. **F-A10: rename `mcp/` → `aice_mcp/`** to remove the FastMCP shim. Single-PR repo-wide sed + import update.

2. **F-A04: split `mcp_server.py`** into `aice_mcp/tools/` modules per category. Each category becomes its own file (search.py, kg_query.py, traceability.py, etc.). The main file just imports + registers.

3. **F-A07: legacy `query_knowledge_graph.py`** — delete or feature-flag. Decision: delete. Migrate any remaining callers.

4. **F-A05: move ILLD/MCAL KG builders** from `src/HybridRAG/code/KG/` to `src/IngestionPipeline/KG/`. Update imports.

5. **Run the full Pass 2 architectural sprint** per Pass 2 §5 target.

### Verification

- All Pass 2 architecture findings closed.
- `lint:import-linter` passes with the strictest layer rules enabled.

---

# Cross-cutting summary

## Sprint loading

| Sprint | Theme | Workstreams | Person-days |
|---|---|---|:---:|
| 26 | Bleeding-stops | W-01 through W-10 | ~10 |
| 27 | Observability | W-11 through W-15 | ~10 |
| 28 | Consolidation | W-16 through W-19 | ~10–13 |
| 29 | Hardening | W-20 through W-25 | ~10 |
| 30 | Architectural (optional) | W-26 | ~10 |
| **Total (Sprints 26-29)** | | | **~40 days** |
| **Total (incl. Sprint 30)** | | | **~50 days** |

## Findings closure progression

| Sprint | Cumulative findings closed |
|---|:---:|
| End of Sprint 26 | ~30 of 187 (the criticals) |
| End of Sprint 27 | ~50 (criticals + observability infra) |
| End of Sprint 28 | ~90 (criticals + cross-cutting refactors) |
| End of Sprint 29 | ~170 (criticals + medium severity) |
| End of Sprint 30 | ~187 (all incl. architectural) |

## What goes where in JIRA

The companion `AICE_JIRA_BACKLOG.csv` flattens this into individual tickets, one per finding. Workstream IDs (W-01..W-26) appear in each ticket's description so you can group them in your JIRA filter view.

For each ticket:
- **Name** — concise summary, includes finding ID prefix.
- **Description** — what the issue is, where it lives, how to fix.
- **Effort** — estimate in hours.
- **Severity** — Critical / High / Medium / Low.
- **Workstream** — W-NN tag.
- **Sprint** — Sprint 26-30 assignment.

---

**End of implementation plan.**

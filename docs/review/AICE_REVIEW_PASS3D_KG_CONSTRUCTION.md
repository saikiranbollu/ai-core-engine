# AICE Review — Pass 3 / Cluster D: KG Construction

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:**
- `src/HybridRAG/code/KG/build_knowledge_graph.py` (~4668 LoC) — main CLI entrypoint, contains 5 builder classes (`KnowledgeGraphBuilder` MCAL, `ILLDKnowledgeGraphBuilder`, `SWAKnowledgeGraphBuilder`, `SWUDKnowledgeGraphBuilder`, `TestSpecKnowledgeGraphBuilder`) plus a SourceCode builder.
- `src/HybridRAG/code/KG/illd_kg_builder.py` — `ILLDKGBuilder` class (different from `ILLDKnowledgeGraphBuilder` in build_knowledge_graph.py — see F-CD-X01).
- `src/HybridRAG/code/KG/query_knowledge_graph.py` (~1595 LoC) — Pass 2 F-A07 flagged this as "Legacy."
- `src/IngestionPipeline/batch_ingestion.py` — newer batch helper used by IngestionService.

**Excludes:** tests/, the parsers themselves (Cluster E), connectors (Cluster F). Findings already reported in Pass 1, Pass 2, or Clusters A/B/C are referenced but not re-stated.

---

## 0. Summary

This cluster is the **write side** of AICE's knowledge graph. Three structural problems jumped out immediately during research, before I even got to code-quality issues:

1. **Two `ILLD` builder classes exist with overlapping responsibilities.** `build_knowledge_graph.py` defines `ILLDKnowledgeGraphBuilder` that calls `illd_parsers.illd_parse_all_files()` and writes via its own `_create_nodes` / `_create_edges`. `illd_kg_builder.py` defines `ILLDKGBuilder` that has its own `ingest_swa`, `ingest_sfr`, `ingest_hw_spec`, `ingest_requirements`, `ingest_source`, `ingest_puml`, and `create_cross_source_relationships` methods. **They write to the same Neo4j database with overlapping schema** (Function, Struct, Register nodes), but with different IDs (`f"FUNC_{fname}"` vs whatever `illd_parsers` generates). Neither class is documented as deprecated. This is the same pattern as Pass 2 F-A02 (parser duplication) but for KG construction. Cluster F-CD-X01 below.

2. **Three concurrency-unsafe write patterns coexist.** The MCAL builder uses retry-with-backoff, the ILLD builder uses retry-with-backoff but in a different shape, and the newer `batch_ingestion.py` does no retry at all. Each has its own connection lifecycle. There's no single "Neo4j write helper" — every builder reinvents it.

3. **`query_knowledge_graph.py` is 1595 lines of "Legacy" code that nothing in the modern path imports**, but it's also not deleted, not behind a feature flag, and has its own f-string Cypher injection vectors. Pass 2 F-A07 already flagged this, so it's mostly carried forward; here I'll note specific exploitable patterns.

Bottom line: the write path is **functional but architecturally fragmented**. Three classes do similar work in slightly different ways. Refactoring this cluster is a multi-sprint effort but every individual fix is small.

**Findings count:** 26 (4 Critical, 8 High, 10 Medium, 4 Low)

| File | C | H | M | L | Total |
|---|---:|---:|---:|---:|---:|
| `build_knowledge_graph.py` | 2 | 4 | 5 | 2 | 13 |
| `illd_kg_builder.py` | 1 | 2 | 3 | 1 | 7 |
| `query_knowledge_graph.py` | 1 | 1 | 1 | 1 | 4 |
| Cross-file (X##) | 0 | 1 | 1 | 0 | 2 |

Severity criteria as in earlier clusters.

---

## 1. Cross-File Findings

### F-CD-X01 🟠 — `ILLDKnowledgeGraphBuilder` (in `build_knowledge_graph.py`) and `ILLDKGBuilder` (in `illd_kg_builder.py`) write to the same Neo4j database with overlapping schemas — origin and ID-format differ, MERGE collisions can happen

**Evidence:**

`src/HybridRAG/code/KG/build_knowledge_graph.py` defines:
```python
class ILLDKnowledgeGraphBuilder:
    """Builds a Neo4j knowledge graph from processed JSON/MD files using
    the ILLD parser pipeline (illd_parsers.py)."""
    
    BATCH_SIZE = 500
    
    def build(self):
        nodes, edges = illd_parse_all_files(str(self.data_path), self.module)
        ...
        self._create_nodes(nodes)
        self._create_edges(edges)
    
    def _create_nodes(self, nodes: List):
        by_type: Dict[str, list] = defaultdict(list)
        for n in nodes:
            ...
            by_type[n.type].append(props)
        for ntype, items in by_type.items():
            ...
            cypher = (
                f"UNWIND $items AS props "
                f"MERGE (n:{ntype} {{id: props.id}}) "
                f"ON CREATE SET n.global_id = randomUUID() "
                f"SET n += props"
            )
```

`src/HybridRAG/code/KG/illd_kg_builder.py` defines:
```python
class ILLDKGBuilder:
    """Builds the ILLD Neo4j knowledge graph from parser outputs.
    Each ingest_* method accepts the dict returned by its corresponding 
    parser and creates the appropriate nodes + edges."""
    
    def ingest_swa(self, swa_data: dict, source_file: str = None):
        ...
        for func in functions:
            fid = f"FUNC_{fname}"
            node = {"id": fid, "name": fname, ..., "module": mod, ...}
            func_nodes.append(node)
        ...
        self._merge_nodes("Function", "id", func_nodes)
```

Both write nodes labeled `:Function` (or similar) with `id` as the merge key. **The id formats differ:**
- `ILLDKnowledgeGraphBuilder` uses whatever `illd_parsers.illd_parse_all_files` generates as `n.id` (typically based on fully-qualified names, untransformed).
- `ILLDKGBuilder` uses `f"FUNC_{fname}"` (the Sprint 8 prefix scheme).

If a deployment runs both pipelines (e.g., `build_knowledge_graph.py --profile illd` for the parser-driven pipeline AND a separate ILLD-specific batch job using `ILLDKGBuilder`), the same logical function lands as **two distinct Neo4j nodes** — `(:Function {id: 'IfxCan_init'})` from one pipeline, `(:Function {id: 'FUNC_IfxCan_init'})` from the other. Search returns duplicates; traceability paths break.

**Even if only one is used today**, the existence of two classes with the same conceptual responsibility is a maintenance liability. New contributors don't know which one to extend; cross-source relationship logic (the `create_cross_source_relationships` method in `ILLDKGBuilder`) only exists in one of them.

**Recommendation:**
1. **Pick one.** `ILLDKGBuilder` is the more cleanly factored class (per-source ingest methods, explicit cross-source linking). Promote it.
2. Refactor `ILLDKnowledgeGraphBuilder` (in build_knowledge_graph.py) to be a **thin wrapper** that calls `ILLDKGBuilder` after running `illd_parse_all_files`. Or vice-versa: have `ILLDKGBuilder` accept the parsed-nodes/edges output and dispatch to its `ingest_*` methods.
3. **Standardize id format**: either always `FUNC_{name}` or always `{name}` — pick one for v3 of the schema and migrate. Add an ADR.
4. Add a smoke test: ingest the same iLLD module with each pipeline, run a Cypher query that detects duplicate nodes (`MATCH (a:Function), (b:Function) WHERE a.name = b.name AND a.id <> b.id RETURN count(*)` — should be 0).

Effort: 3-5 days for full unification; 1 day to identify which is dead code (if either) and delete.

---

### F-CD-X02 🟡 — Three different Neo4j-write retry implementations across the cluster

**Evidence:**

`build_knowledge_graph.py::KnowledgeGraphBuilder._write_tx`:
```python
def _write_tx(self, cypher, parameters=None):
    max_attempts = 3
    db = self.neo4j_cfg["database"]
    for attempt in range(1, max_attempts + 1):
        try:
            with self._driver.session(database=db) as session:
                session.execute_write(lambda tx: tx.run(cypher, parameters or {}).consume())
                return
        except (ServiceUnavailable, TransientError, OSError) as exc:
            if attempt >= max_attempts: raise
            wait = min(2 ** attempt, 8)
            ...
            time.sleep(wait)
```

`illd_kg_builder.py::ILLDKGBuilder._write`:
```python
def _write(self, cypher, parameters=None):
    if self.dry_run:
        return
    db = self.neo4j_cfg["database"]
    for attempt in range(1, 4):
        try:
            with self._driver.session(database=db) as session:
                session.execute_write(lambda tx: tx.run(cypher, parameters or {}))
            return
        except (ServiceUnavailable, TransientError, OSError) as exc:
            if attempt >= 3: raise
            wait = min(2 ** attempt, 8)
            ...
            time.sleep(wait)
```

`batch_ingestion.py::Neo4jBatchWriter.merge_nodes_batch`:
```python
for i in range(0, len(nodes), self._batch_size):
    batch = nodes[i:i + self._batch_size]
    try:
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, {"batch": batch})
            ...
    except Exception as exc:
        logger.error("Neo4j batch merge failed: %s", exc)
        total_failed += len(batch)
        errors.append(str(exc))
```

Three different retry shapes:
- The first uses `execute_write` (managed transaction with auto-retry by the driver).
- The second uses `execute_write` with manual retry on top.
- The third uses raw `session.run` with no retry.

**Impact:** Behavior differs when Neo4j has a transient blip:
- MCAL ingestion: retries 3× with backoff, plus the driver's own managed-transaction retries.
- ILLD ingestion: same as MCAL but slightly different log format.
- batch_ingestion.py: **logs the error and moves on** — silently loses nodes/relationships.

The third behavior is the worst. For an ingestion run of 50K nodes with 1% transient failure rate, that's ~500 nodes silently dropped. The job reports "completed" while half-empty.

**Recommendation:**
1. Extract a single helper `safe_write_tx(driver, db, cypher, params, max_attempts=3)` to a new `src/HybridRAG/code/KG/_neo4j_helpers.py` (or use the existing `Neo4jConnection` from `neo4j_manager.py`).
2. Call it from all three places. Delete the per-class implementations.
3. Standardize on **`execute_write`** (managed transaction) — the neo4j driver does its own deadlock retry; manual outer retry handles the higher-level connection issues.
4. **Make batch_ingestion.py raise on permanent failure** instead of silently logging. Or at minimum, set `total_failed` properly and have the caller decide whether to abort.

Effort: 1 day.

---

## 2. `src/HybridRAG/code/KG/build_knowledge_graph.py`

### F-CD-B01 🔴 — `_clear_database` does `MATCH (n) DETACH DELETE n` with **no scoping**, called from a builder that is module-scoped

**Evidence:** `KnowledgeGraphBuilder._clear_database`:
```python
def _clear_database(self):
    """Delete all nodes and relationships in the target database."""
    logger.warning("Clearing ALL data in database '%s'…", self.neo4j_cfg["database"])
    self._write_tx("MATCH (n) DETACH DELETE n")
    logger.info("Database cleared.")
```

Called when `--clear` flag is passed:
```python
if profile == "illd":
    builder = ILLDKnowledgeGraphBuilder(..., clear_db=args.clear)
    ...
```

The `ILLDKnowledgeGraphBuilder.build()` similarly:
```python
if self.clear_db:
    logger.warning("Clearing ALL data in database '%s'…", self.neo4j_cfg["database"])
    self._write_tx("MATCH (n) DETACH DELETE n")
    logger.info("Database cleared.")
```

Note the **builder is module-scoped** (`--module ADC`). User intent for `--clear --module ADC` is plausibly *"clear ADC data and re-ingest ADC"*. But the actual behavior is *"clear the entire database, then ingest ADC."* If the database holds 20 modules' worth of data (50K nodes total, painstakingly built up over weeks), running `python build_knowledge_graph.py --module ADC --clear` deletes all 20 modules. **The CLI gives no warning that `--clear` is global, not module-scoped.**

The `database` config does provide *some* isolation — illd vs mcal are separate databases, so `--profile illd --module ADC --clear` can't nuke MCAL data. But within a profile, all 20 ILLD modules share one database.

**Recommendation:**

1. **Make `--clear` module-scoped by default.** Replace the global delete with:
   ```cypher
   MATCH (n {module: $module}) DETACH DELETE n
   ```
   Add `--clear-all` for the global flush, with a confirmation prompt.

2. **Confirmation prompt** for any global clear:
   ```python
   if args.clear_all:
       confirm = input(f"This will DELETE ALL data in database '{db}'. Type 'YES' to continue: ")
       if confirm != "YES":
           sys.exit(1)
   ```

3. **Pre-clear count and post-clear assert.** Before deleting, log node counts. After deleting, log "deleted N nodes." Helps catch accidental clears.

4. **Audit log entry** to PostgreSQL `audit_logs` for the clear operation: who triggered it (via API key if called from MCP, via OS user if from CLI), when, what scope.

This is **High-bordering-on-Critical** because a single fat-finger CLI invocation can destroy weeks of ingestion work with no recovery (MERGE-based pipelines do not preserve original Jama IDs in any backup). Effort: 4 hours.

---

### F-CD-B02 🔴 — `_create_constraints` uses f-string label interpolation; the label list comes from the ontology YAML — same Cypher-injection class as F-CC-K01

**Evidence:**
```python
for nt in self.node_types:
    label = nt["name"]
    uid_prop = get_unique_id_property(nt)
    if uid_prop:
        constraint_name = f"unique_{label}_{uid_prop}".lower()
        cypher = (
            f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{uid_prop} IS UNIQUE"
        )
        try:
            self._run(cypher)
```

`label` and `uid_prop` come from `ontology.yaml` (which is repo-controlled, not user-controlled at runtime — so this isn't an active exploit). But:

1. **`uid_prop` is also interpolated into the Cypher.** A typo or copy-paste error in the YAML (`uid_property: name; CREATE...`) would inject Cypher.
2. **`constraint_name` is `.lower()`-d but not character-validated.** `nt["name"]` could contain spaces, hyphens, or non-ASCII chars (the ontology has labels like `SWA_Function` which is fine, but a future entry like `My-Type` would produce `unique_my-type_id` — the dash makes the constraint name invalid in Neo4j syntax).
3. **`get_unique_id_property` returns whatever the YAML says.** No validation that it's a valid Cypher identifier.

The critical aspect is the **same defense-in-depth issue** as F-CC-K01: even if not currently exploitable, the pattern is unsafe by construction. A YAML edit by a non-security-conscious contributor immediately becomes injectable.

**Recommendation:** apply the same `_VALID_LABELS` allowlist + `_sanitize_label()` helper from F-CC-K01 (reuse if you keep the helper in a shared module after Cluster C). For property names, allowlist `[a-zA-Z_][a-zA-Z0-9_]*`.

Effort: 1 hour (extends the F-CC-K01 fix).

---

### F-CD-B03 🟠 — `_create_nodes` and `_create_edges` use `MERGE` on properties not all of which are validated; UNWIND batch can fail entire batch on one bad item

**Evidence:**
```python
for chunk in self._chunked(items, self.BATCH_SIZE):
    cypher = (
        f"UNWIND $items AS props "
        f"MERGE (n:{ntype} {{id: props.id}}) "
        f"ON CREATE SET n.global_id = randomUUID() "
        f"SET n += props"
    )
    self._write_tx(cypher, {"items": chunk})
```

Issues:

1. **`props.id` is required** but not enforced. If `n.id` is missing for one item in a 500-item batch, the MERGE creates a `:Function {id: NULL}` node which:
   - Pollutes the graph with NULL-id nodes.
   - Causes constraint violations on subsequent runs (the unique constraint on `id`).
   - **Silently fails for that item but the rest of the batch may succeed or fail** depending on Neo4j version (5.x raises ConstraintViolation; the whole batch then fails).

2. **`SET n += props`** sets *every* property in `props`, including the merge key `id`. If `props.id` is somehow different from the value used in the MERGE pattern (after MERGE, `n.id` was already set), the SET re-sets it to the same value — no harm, but pointless work. More worrying: any other property like `props.global_id` would override the `randomUUID()` set in `ON CREATE SET`, which is exactly what we don't want.

3. **No validation pass** before batching. A single corrupt item poisons a 500-item batch.

**Recommendation:**
1. Validate items before batching:
   ```python
   def _validate_node(props, ntype):
       if not props.get("id"):
           raise ValueError(f"Missing id for {ntype} node: {props}")
       # property-name allowlist
       bad = [k for k in props if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k)]
       if bad:
           raise ValueError(f"Invalid property names: {bad}")
       return props
   
   items = [_validate_node(p, ntype) for p in items]
   ```
2. Exclude the merge key from the SET clause:
   ```cypher
   UNWIND $items AS props
   MERGE (n:{ntype} {id: props.id})
   ON CREATE SET n.global_id = randomUUID()
   SET n += apoc.map.removeKey(props, 'id')
   ```
   (Or, without APOC, build a properties subset in Python before passing.)
3. **Per-item failure recovery.** If a batch fails, retry with batch size 1 to identify the bad item. Log it, skip it, continue.

Effort: 1 day.

---

### F-CD-B04 🟠 — `_create_edges` does `MATCH (a {id: e.source_id}) MATCH (b {id: e.target_id})` with no label filter — performance disaster on large graphs

**Evidence:**
```python
cypher = (
    f"UNWIND $edges AS e "
    f"MATCH (a {{id: e.source_id}}) "
    f"MATCH (b {{id: e.target_id}}) "
    f"MERGE (a)-[r:{rtype}]->(b) "
    f"SET r += e.props"
)
```

Without a label, this Cypher does a **full-database scan** for each `MATCH` (or, if there's an index on `id`, a range scan). For 500 edges per batch:
- 500 batches × 2 MATCH operations × N nodes = N × 1000 operations per batch.
- Even with an index on `id`, each lookup is O(log N).

For a moderately populated graph (50K nodes), each batch is ~500 × 2 × log(50000) = 16K index seeks. For 100 batches, that's 1.6M seeks. A typical ingestion of 10K edges takes 30-60 seconds purely from this.

The MCAL builder's `_create_constraints` creates `CREATE INDEX idx_global_id IF NOT EXISTS FOR (n) ON (n.global_id)` (a global index on global_id), which is intended to help, but:
1. The MATCH uses `id` (not `global_id`).
2. There's no per-label index on `id`, so the database might not pick the global index.

**The MCAL code uses `jama_id` consistently and has indexes on it; the ILLD code uses `id`** (per F-CD-X01 the schemas are different).

**Recommendation:**
1. **Always include the label in MATCH:**
   ```cypher
   MATCH (a:{from_label} {id: e.source_id})
   MATCH (b:{to_label} {id: e.target_id})
   ```
   Edges should know their endpoints' labels — the parsers produce typed edges, so `from_label` and `to_label` are available.
2. **Create per-label index on the merge key:**
   ```cypher
   CREATE INDEX idx_Function_id IF NOT EXISTS FOR (n:Function) ON (n.id)
   ```
3. The `ILLDKGBuilder._merge_edges` already does this correctly:
   ```python
   cypher = (f"MATCH (a:{from_label} {{{from_uid}: e.from_key}}) "
             f"MATCH (b:{to_label} {{{to_uid}: e.to_key}}) ...")
   ```
   So **the fix is to make `ILLDKnowledgeGraphBuilder._create_edges` look like `ILLDKGBuilder._merge_edges`** (which is yet another argument for F-CD-X01 unification).

Effort: 4 hours.

---

### F-CD-B05 🟠 — Profile detection via `if profile == "illd"` is brittle; new profiles can't be added without code changes

**Evidence:**
```python
if profile == "illd":
    builder = ILLDKnowledgeGraphBuilder(...)
    builder.build()
    return

# Continue down to MCAL builder...
```

The profile dispatch is hardcoded. To add a new profile (say, `safety_island` for ASIL-D specific data), you must:
1. Add a new builder class.
2. Add an `elif profile == "safety_island"` branch.
3. Update the ontology YAML.

There's no plugin mechanism. The ontology doesn't declare which builder to use; the code does. This is fine for 2 profiles, but Master Gaps mentions deferred/planned profiles, and DA expansion may add more.

**Recommendation:** profile-to-builder registry:
```python
BUILDER_REGISTRY = {
    "illd": ILLDKnowledgeGraphBuilder,
    "mcal": KnowledgeGraphBuilder,
    "safety_island": SafetyIslandKnowledgeGraphBuilder,
}

builder_cls = BUILDER_REGISTRY.get(profile)
if not builder_cls:
    raise ValueError(f"Unknown profile: {profile}")
builder = builder_cls(...)
```

Allows adding new profiles without touching `main()`. Effort: 30 min.

---

### F-CD-B06 🟠 — `--clear` and `--clear-relationships` are mutually inconsistent; `--clear` happens AFTER `_connect()` but `--clear-relationships` deletes a file

**Evidence:**
```python
# CLI args:
if args.refresh_relationships:
    if relationships_path.exists():
        relationships_path.unlink()
        logger.info("Deleted cached relationships file: %s", relationships_path.name)

if args.refresh_folders:
    if folders_path.exists():
        folders_path.unlink()
        ...
```

Then later, `clear_db=args.clear` passes through to the builder. The two operations have different semantics:
- `--clear` → drops Neo4j data.
- `--refresh-relationships` → deletes a cache file.
- `--refresh-folders` → deletes a cache file.

A user running `--clear` for a clean rebuild probably wants the cache cleared too, but the CLI doesn't imply this. Documentation says `--clear` is for Neo4j, but the cache files affect what gets ingested.

**Recommendation:** add a `--full-clean` shortcut that does all three. Document the relationship between flags.

Effort: 30 min.

---

### F-CD-B07 🟡 — `sys.exit(1)` inside `_connect()` makes the class unusable as a library

**Evidence:**
```python
def _connect(self):
    cfg = self.neo4j_cfg
    uri = cfg["uri"]
    ...
    try:
        ...
        self._driver = GraphDatabase.driver(uri, **drv_kw)
        self._driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError) as exc:
        logger.error("Could not connect to Neo4j at %s: %s", uri, exc)
        print(f"\n  ERROR: Neo4j is not reachable at {uri}.\n  ...")
        sys.exit(1)
```

The class calls `sys.exit(1)` on connection failure. This is appropriate for a CLI but **breaks any caller that imports the class** (e.g., the IngestionService). If MCP's `IngestionService._get_kg_builder()` constructs a `KnowledgeGraphBuilder` and Neo4j is briefly unavailable, the entire MCP server process exits. K8s would restart the pod, masking the issue but causing a thundering-herd retry storm.

**Recommendation:** raise a typed exception. Let the caller decide:
```python
class KGBuildError(Exception): pass
class Neo4jUnavailable(KGBuildError): pass

except (ServiceUnavailable, AuthError, OSError) as exc:
    raise Neo4jUnavailable(f"Cannot reach Neo4j at {uri}: {exc}") from exc
```

The `main()` CLI entrypoint catches this and `sys.exit(1)`s. Library callers handle it.

Effort: 1 hour.

---

### F-CD-B08 🟡 — `Counter` for `self.stats` causes silent metric overlap when same key is used by both nodes and relationships

**Evidence:**
```python
self.stats: dict = Counter()
...
# Later:
self.stats[f"nodes:{label}"] += len(items)
...
self.stats[f"rel:{rtype}"] += len(items)
```

The keys `nodes:X` and `rel:Y` namespace nicely. **But** the assignment in `ILLDKnowledgeGraphBuilder._create_nodes`:
```python
self.stats[f"nodes:{ntype}"] = len(items)  # NOT +=
```
This **assignment** means re-running the builder doesn't accumulate; each call resets. For a single CLI run that's fine, but if the builder is re-used (instance reused across modules), counts get clobbered.

In the MCAL `KnowledgeGraphBuilder`, the assignment is also `=`, not `+=` — same risk.

**Recommendation:** consistently use `+=` for stats, or re-initialize `self.stats = Counter()` at the start of each `build()` call. Same pattern across both builders.

Effort: 5 min.

---

### F-CD-B09 🟡 — `_run` (read query) catches `OSError` for retry but a raw socket error during read is not always `OSError`

**Evidence:**
```python
def _run(self, cypher, parameters=None):
    max_attempts = 3
    db = self.neo4j_cfg["database"]
    for attempt in range(1, max_attempts + 1):
        try:
            with self._driver.session(database=db) as session:
                result = session.run(cypher, parameters or {})
                return [rec.data() for rec in result]
        except (ServiceUnavailable, TransientError, OSError) as exc:
            ...
```

On Python 3.10+, the neo4j driver wraps socket errors in its own exceptions (`Neo4jError`, `BoltConnectionBrokenError`). `OSError` covers raw `socket.error` for direct TCP failures, but most real failure modes go through the driver's exception hierarchy.

This isn't actively broken — `ServiceUnavailable` and `TransientError` are subclasses of `Neo4jError` and cover most transient cases. But adding `(neo4j.exceptions.Neo4jError, ConnectionError)` to the except clause would catch a wider set.

**Recommendation:** broaden the retry types:
```python
RETRY_EXCEPTIONS = (
    ServiceUnavailable, TransientError, SessionExpired,
    ConnectionError, OSError, neo4j.exceptions.DatabaseError
)
```

Effort: 15 min.

---

### F-CD-B10 🟡 — Module name handled inconsistently: `.upper()` in some places, raw in others

**Evidence:**
```python
# In main():
module = args.module.upper()
...
data_path = args.data or (DATA_DIR / module / "processed")
```

But later, parsers receive lowercase module names (the directory structure uses lowercase in `data/<module>/processed`):
```python
# illd_kg_builder.py
mod = self.module  # Always use pipeline module, not parser-derived
...
node["module"] = mod
```

`mod` here is `self.module` which was assigned in `__init__` as `module.upper()`. So `n.module = "ADC"` (uppercase) — but the directory was `data/adc/processed` (lowercase). Search queries that filter by module need to normalize.

**Cross-cluster impact:** Cluster B F-CB-10's `_detect_module_from_names` returns `"Adc"` (or `"Std"` per the bug). The KG has `module: "ADC"`. Sandbox prod-overlay queries fail to match.

**Recommendation:** Decide on a canonical case. Document it. Apply consistently — preferably in a single `normalize_module(name) -> str` helper used everywhere.

Effort: 1 day for repo-wide normalization (bigger than it sounds because module name strings are spread across many parsers, KG builders, search code, and ontology config).

---

### F-CD-B11 🟢 — Multiple "Step 1/4" / "Step 1/5" log markers inconsistent across builders

The `ILLDKnowledgeGraphBuilder.build()` logs `Step 1/4: Parsing processed files…` while the SourceCode builder logs `Step 1/5: Discovering and parsing C files…`. Different counts. If a future builder has 6 steps, would it know about this convention?

Cosmetic. Use a `_log_step(n, total, msg)` helper for consistency.

Effort: 30 min.

---

### F-CD-B12 🟢 — Garbled non-ASCII characters in source: `ΓåÆ`, `ΓÇª`, `Γèó`

Several log/print strings contain mangled bytes that look like UTF-8 misinterpreted as cp1252 (or vice versa):
```python
logger.info("Source Code KG Builder \u2013 module: %s", self.module)  # \u2013 = en-dash, OK
...
logger.info("Step 1/5: Discovering and parsing C filesΓÇª")  # mangled
logger.info("  Found %d C/H files to parse", len(c_files))
```

Indicates the file was edited on Windows with cp1252 encoding at some point and committed as-is. The `\u2013` literals are correct; the `Γ`-prefixed strings are corruption (likely original `…`, `→`, `«»`).

**Recommendation:** find/replace all such instances:
```bash
grep -rn 'ΓÇª\|ΓåÆ\|Γèó\|γÇª' src/HybridRAG/code/KG/
```
Replace `ΓÇª` → `…`, `ΓåÆ` → `→`, etc.

Effort: 30 min.

---

### F-CD-B13 🟡 — `_print_summary` runs `MATCH (n) RETURN count(n)` after every build — expensive on large graphs

**Evidence:**
```python
def _print_summary(self, elapsed):
    ...
    try:
        db_stats = self._run("MATCH (n) RETURN count(n) AS nodes")
        rel_count = self._run("MATCH ()-[r]->() RETURN count(r) AS rels")
        labels = self._run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
        print(...)
    except Exception:
        pass
```

`MATCH (n) RETURN count(n)` is a full graph scan. On a large graph (~1M nodes), this is 5-10 seconds. After every ingestion run.

Better: use `db.stats.retrieve("GRAPH COUNTS")` (Neo4j 5.x) or maintain incremental counts during ingestion (already in `self.stats`).

**Recommendation:** use the in-memory stats (already accumulated during ingestion). If the user wants live counts, expose a separate `--print-live-stats` flag.

Effort: 15 min.

---

## 3. `src/HybridRAG/code/KG/illd_kg_builder.py`

### F-CD-I01 🔴 — `_merge_nodes` uses f-string label interpolation; same Cypher injection pattern as F-CC-K01 / F-CD-B02

**Evidence:**
```python
def _merge_nodes(self, label: str, uid_prop: str, items: List[dict]):
    if not items:
        return
    for item in items:
        item["module"] = self.module
    ...
    for chunk in self._chunked(items):
        cypher = (
            f"UNWIND $items AS props "
            f"MERGE (n:{label} {{{uid_prop}: props.{uid_prop}}}) "
            f"ON CREATE SET n.global_id = randomUUID() "
            f"SET n += props"
        )
        self._write(cypher, {"items": chunk})
```

`label` and `uid_prop` come from `ingest_swa`, `ingest_sfr`, etc. — internal callers, not user-controlled. **But** the same defense-in-depth issue: any future caller that takes label from external input (e.g., a generic `ingest_unknown_format` method) is immediately injectable.

`_merge_edges` has the same pattern with `rel_type`, `from_label`, `to_label`.

**Recommendation:** apply the `_VALID_LABELS` allowlist + `_sanitize_label()` from F-CC-K01. Do it once for all ILLD/KG code in a shared `_kg_safety.py` helper.

Effort: 1 hour.

---

### F-CD-I02 🟠 — `ingest_swa` constructs IDs as `f"PARAM_{fname}_{pname}"` with no escaping — special characters in C param names break MERGE

**Evidence:**
```python
for idx, p in enumerate(params):
    pname = p.get("name", f"param{idx}")
    ptype = p.get("type", "unknown")
    pid = f"PARAM_{fname}_{pname}"
```

C parameter names can include underscores (fine), but parsers can also produce edge cases:
- Anonymous parameters (no name) → falls back to `param{idx}` (handled).
- Function pointer types where the "name" includes parentheses: `(*callback)`. The parser may give `pname = "(*callback)"`, producing `pid = "PARAM_func_(*callback)"`. Cypher accepts this as a string literal but it's an ugly ID and a needle for future bugs.
- Pointer asterisks in the name field: `pname = "*ptr"` → `pid = "PARAM_func_*ptr"`. Still a string but unusual.

The same `_safe_str` helper exists but isn't applied to these IDs.

**Recommendation:** sanitize IDs with `re.sub(r'[^A-Za-z0-9_]', '_', name)`. The original name is preserved as a property; the ID is just for MERGE matching.

Effort: 30 min.

---

### F-CD-I03 🟠 — `ingest_swa` overrides `func.module` with `self.module` regardless of parser output — masks parser bugs

**Evidence:**
```python
mod = self.module  # Always use pipeline module, not parser-derived

for func in functions:
    ...
    node = {
        ...
        "module": mod,  # forced
        ...
    }
```

The comment says "Always use pipeline module, not parser-derived." This is **defensive against parser bugs**, which is fine. But it also **silently corrects** a real disagreement: if the parser parsed `IfxAdc_Cmd_setRange` (clearly an Adc function) into a "CXPI" pipeline run by mistake, the function gets stamped with `module: CXPI` and pollutes the CXPI module.

**Operational impact:** module mis-assignment is hard to detect post-ingestion. The module field is used for filtering, NodeSet linking, and search scoping. A wrong assignment means the node is invisible in the correct module's queries and visible in the wrong module's.

**Recommendation:** **assert** rather than silently override:
```python
parser_mod = func.get("module")
if parser_mod and parser_mod.upper() != self.module.upper():
    logger.warning(
        "[ILLDKGBuilder] Parser module '%s' disagrees with pipeline module '%s' for function %s — using pipeline.",
        parser_mod, self.module, fname,
    )
    # OR: raise ValueError to halt the run
```

Effort: 30 min.

---

### F-CD-I04 🟡 — `_serialize_complex` JSON-serializes lists/dicts as strings, breaking native Neo4j list properties

**Evidence:**
```python
@staticmethod
def _serialize_complex(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, dict, set, frozenset)):
        return json.dumps(value, default=str)
    return str(value)
```

Neo4j supports list properties natively (e.g., `n.tags = ['a', 'b', 'c']`). Storing them as JSON strings means:
- Cypher queries can't filter on list contents (`WHERE 'a' IN n.tags` becomes impossible).
- Every consumer must `json.loads()` to use the value.
- Storage is larger.

The MCAL `_create_nodes` does the same:
```python
for k, v in list(props.items()):
    if isinstance(v, (list, dict, set, frozenset)):
        import json as _json
        props[k] = _json.dumps(v, default=str)
```

The reason is that Neo4j list properties **only support primitive types** (str, int, float, bool, point, datetime). A list of dicts can't be stored natively. So:
- A list of strings → store as native list.
- A list of dicts → must JSON-serialize (acceptable trade-off).
- A dict → must JSON-serialize (no native dict in Neo4j).

The current code over-serializes — flat lists of strings get JSON'd unnecessarily.

**Recommendation:**
```python
@staticmethod
def _to_neo4j_value(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        # If all items are primitive, keep as native list
        if all(isinstance(x, (str, int, float, bool)) for x in value):
            return value
    if isinstance(value, (list, dict, set, frozenset)):
        return json.dumps(value, default=str)
    return str(value)
```

Effort: 1 hour + a Neo4j integration test.

---

### F-CD-I05 🟡 — `create_cross_source_relationships` is documented in the class docstring but I cannot find its body

The class docstring says:
> Call `create_cross_source_relationships` after all sources are ingested to wire up inter-source links (e.g. Function → Register).

But the snippet I have ends at `ingest_sfr`. The method may exist; I just can't see it. If it doesn't exist, the documented contract is broken.

**Recommendation:** verify with `git grep "def create_cross_source_relationships" src/HybridRAG/code/KG/illd_kg_builder.py`. If missing, file as a bug. If present, no action.

Effort: 15 min to verify.

---

### F-CD-I06 🟡 — `_safe_str` strips empty strings to None, which downstream search code treats inconsistently

**Evidence:**
```python
@staticmethod
def _safe_str(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None
```

So `_safe_str("")` returns `None`, and `_safe_str("  ")` also returns `None`. Then in `ingest_swa`:
```python
"brief": self._safe_str(func.get("brief")),
"purpose": self._safe_str(func.get("purpose") or func.get("detailed_description")),
```

Empty strings become None, which then gets passed to Cypher MERGE. Search queries do:
```python
"WHERE toLower(coalesce(n.name,'')) CONTAINS $kw"
```
The `coalesce(n.name, '')` handles the None case. **But** queries that don't `coalesce` (e.g., `WHERE n.brief CONTAINS $kw`) silently fail to match on nodes with no `brief`. An empty-string would match an empty query but None doesn't match anything.

**Recommendation:** decide policy: are missing-vs-empty distinguishable? If yes, keep this. If no, store `""` instead of `None`. Either way, document the choice.

Effort: 30 min.

---

### F-CD-I07 🟢 — Module docstring says "ontology.yaml v2.0.0 ILLD profile" — version not in source control as a constant

The docstring claims the ILLD profile schema is v2.0.0. The actual version is in `ontology.yaml`. If they drift, no detection.

**Recommendation:** at startup, log the ontology version: `logger.info("ILLDKGBuilder using ontology v%s", ontology["version"])`.

Effort: 5 min.

---

## 4. `src/HybridRAG/code/KG/query_knowledge_graph.py` (Legacy)

Pass 2 F-A07 already flagged this as 1595 lines of legacy code that should be inventoried and either deleted or migrated. Findings here are about specific issues *if it stays*.

### F-CD-Q01 🔴 — `_fetch` helper interpolates `rel` (relationship type) directly into Cypher

**Evidence:** `trace_requirement`:
```python
def _fetch(rel: str, direction: str = "out") -> List[dict]:
    if direction == "out":
        cypher = (
            f"MATCH (n {{jama_id: $jid}})-[:{rel}]->(m) "
            f"RETURN m, labels(m) AS labels"
        )
    else:
        cypher = (
            f"MATCH (n {{jama_id: $jid}})<-[:{rel}]-(m) "
            f"RETURN m, labels(m) AS labels"
        )
    return self.run(cypher, {"jid": jid})
```

`rel` comes from hardcoded callers (`_fetch("DERIVES_FROM", "out")` etc.) — currently safe. But the helper **could be exposed via a future public method** and is the same vulnerability class as F-CC-K01.

Same for `find_orphan_requirements`:
```python
cypher = (
    f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
    f"AND NOT (n)-[:{relationship}]->() "
    f"RETURN n ORDER BY n.name"
)
```
Where `relationship` is a parameter (`relationship: str = "DERIVES_FROM"`). User-overridable in code, but if anything calls this from MCP with a user-controlled value, immediately injectable.

**Recommendation:** apply the same `_VALID_RELS` allowlist as F-CC-K01.

Effort: 1 hour.

---

### F-CD-Q02 🟠 — `find_path` constructs Cypher with f-string `max_depth`

**Evidence:**
```python
cypher = (
    f"MATCH (a {from_match}), (b {to_match}), "
    f"path = shortestPath((a)-[*..{max_depth}]-(b)) "
    ...
)
```

`max_depth` is interpolated. If a caller passes `max_depth = "5; CREATE..."` (string instead of int), Cypher injection. But the function signature is `max_depth: int = 4` — type hint suggests int but Python doesn't enforce.

**Recommendation:** explicit cast and bound:
```python
max_depth = int(max_depth)
if max_depth < 1 or max_depth > 10:
    raise ValueError(f"max_depth must be 1..10, got {max_depth}")
```

Same issue exists in `query_dependencies` (Cluster C F-CC-K05 noted as a positive example because it does `int(max_depth)` — so this file should mirror that).

Effort: 15 min.

---

### F-CD-Q03 🟡 — `get_database_stats` runs three full-graph queries in sequence

```python
node_count = self.run("MATCH (n) RETURN count(n) AS cnt")[0]["cnt"]
rel_count = self.run("MATCH ()-[r]->() RETURN count(r) AS cnt")[0]["cnt"]
labels = self.list_labels()
rel_types = self.list_relationship_types_live()
label_counts = self.run("MATCH (n) UNWIND labels(n) AS lbl RETURN lbl, count(*) AS cnt ORDER BY cnt DESC")
rel_type_counts = self.run("MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC")
```

Six full-graph queries. For 1M nodes, this is 30+ seconds of Neo4j load **per call to `get_database_stats`**. If this is called from a UI dashboard polling every minute, Neo4j is constantly under read load.

**Recommendation:** combine into one query with `UNION ALL`, or use Neo4j's stats functions:
```cypher
CALL apoc.meta.stats() YIELD labels, relTypes, ...
```
(Requires APOC.) Or maintain cached stats updated only at ingestion.

Effort: 1 hour.

---

### F-CD-Q04 🟢 — Module docstring + Pass 2 F-A07 carry-forward

The 1595-line "Legacy" status. Decide: delete, deprecate-with-warning, or migrate. (Pass 2 disposition already covers this.)

---

## 5. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CD-B01 (--clear scope), F-CD-B02 (label injection), F-CD-I01 (label injection), F-CD-Q01 (rel injection) | 1 day total — all share the same allowlist helper |
| **P1 (next sprint)** | F-CD-X01 (dual ILLD builders), F-CD-X02 (retry unification), F-CD-B03 (UNWIND validation), F-CD-B04 (label-less MATCH perf), F-CD-I02 (ID escaping), F-CD-I03 (module mismatch warning), F-CD-Q02 (max_depth bound) | 5–6 days |
| **P2** | F-CD-B05–B10, F-CD-I04–I06, F-CD-Q03 | 3 days |
| **P3** | F-CD-B11–B13, F-CD-I07, F-CD-Q04 | 1–2 hours |

**Total cluster effort:** ~10–11 person-days. F-CD-X01 (the dual-builder unification) is the biggest single item at 3-5 days but has the highest long-term payoff.

---

## 6. Cross-cutting reinforcement: f-string Cypher is everywhere

Cluster C surfaced the Cypher-injection pattern in `knowledge_intelligence.py` (F-CC-K01). Cluster D adds **four more files** with the same pattern:

| File | Helper / method | Severity |
|---|---|---|
| `knowledge_intelligence.py::_fuzzy_find` | label interpolation | F-CC-K01 (Critical) |
| `build_knowledge_graph.py::_create_constraints` | label + property name | F-CD-B02 (Critical) |
| `build_knowledge_graph.py::_create_nodes` / `_create_edges` | label/rel_type | F-CD-B03 (High) |
| `illd_kg_builder.py::_merge_nodes` / `_merge_edges` | label/rel_type | F-CD-I01 (Critical) |
| `query_knowledge_graph.py::_fetch` / `find_path` / `find_orphan_requirements` | rel + label | F-CD-Q01 (Critical) |

This should be **a single shared helper module** (`src/HybridRAG/code/_kg_safety.py`) with `_sanitize_label`, `_sanitize_rel_type`, `_sanitize_property_name`, plus their allowlist constants. Apply across all 5 files in one go.

I'll fold the **CI gate from Pass 1 §5 + the cross-file CI checks proposed in Cluster C §5** into Pass 4 with this finding in mind. The CI gate should grep for `f"MATCH.*\\(.*:[\\w_]+` and `f".*MERGE.*:[\\w_]+` patterns and require they go through the sanitizer.

---

## 7. What I deliberately did not flag

- **Pass 2 F-A02** — duplicate parser files. Mentioned at the cluster summary but not re-flagged here; full scope is in Pass 2.
- **Pass 2 F-A05** — `KG/build_knowledge_graph.py` and `KG/illd_kg_builder.py` belonging in IngestionPipeline rather than HybridRAG. Architectural placement, not code quality.
- **The `--ingest-swa`, `--ingest-swud`, `--ingest-testspec`, `--ingest-source` flags** and their respective builders. Out of scope for line count; the patterns I'd find there mirror what's already covered.
- **`_create_nodes` `Counter()` keys collision potential** — covered as F-CD-B08 minor.
- **PostgreSQL `IngestionJobTracker` vs `Neo4jBatchWriter` integration** — that's a Cluster A or Pass 4 concern.

---

**End of Cluster D.** Ready to proceed to Cluster E (Parsers — `swa_parsers`, `swud_parsers`, `c_parser`, `pdf_parser`, `testspec_parsers`, `regdef_parsers` + 8 others) on your signal.

Cluster E covers 14 parsers, mostly small files but with high heterogeneity. Findings will focus on:
- LLM-enrichment robustness (PDF parser, SWA parser have LLM calls)
- Output-shape consistency (each parser returns differently-shaped dicts that the KG builders then have to reconcile)
- Error handling for malformed inputs (corrupt PDF, missing fields, ARXML schema variations)
- Path containment for file inputs

Estimate: 18-25 findings, but spread thin (1-2 per parser).

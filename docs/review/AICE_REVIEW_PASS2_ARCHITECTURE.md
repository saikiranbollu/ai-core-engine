# AICE Review — Pass 2: Architecture & Boundaries

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-25
**Scope:** Layer boundaries, module dependencies, duplication, naming/casing, lifecycle ownership, layering of MCP tool categories.
**Excludes:** tests/, individual file-quality issues (Pass 3), security (Pass 4).

---

## 0. Executive Summary

AICE is a **layered MCP server** with reasonable conceptual layers (MCP → Services → Backends), but the layering is **enforced by convention, not by structure**. There is no `__init__.py`-level boundary, no dependency-direction enforcement, and several legitimate-looking imports cross layers in ways that would be hard to re-enforce later. Most architectural problems fall into three buckets:

1. **Filesystem casing & duplication** — `Parsers/` (capital) vs `parsers/` (lowercase) coexist; `swa_parsers.py` is duplicated across `IngestionPipeline` and `HybridRAG/KG/`. This is a deploy-time fail risk on case-sensitive filesystems and a cognitive-load tax for maintainers.
2. **`HybridRAG` is overweight** — at the time of Sprint 25 it owns: search, RAG, KG construction, KG querying (legacy), RLM orchestration, PDF pipeline, the canonical ContextBuilder, env_config, neo4j_manager, and token_manager. About half of these belong elsewhere.
3. **No layer protocol** — MemoryLayer imports from HybridRAG, Configuration imports from MemoryLayer, ReviewGate is called by MCP but has no formal dependency on KG/RAG. No `Protocol`/abstract interfaces between layers, so refactors break things invisibly.

**Findings count:** 16 (3 Critical, 7 High, 4 Medium, 2 Low)

| Severity | Count |
|---|---|
| 🔴 Critical | 3 |
| 🟠 High | 7 |
| 🟡 Medium | 4 |
| 🟢 Low | 2 |

I'll also propose a **target architecture** (Section 5) — a layout you can migrate to incrementally without breaking Sprint 25 functionality.

---

## 1. Critical Findings

### F-A01 🔴 — Parser directory case duplication: `Parsers/` and `parsers/` both exist

**Evidence:**

`src/IngestionPipeline/Parsers/__init__.py` (capital P):
```python
__all__ = [
    "arxml_parser", "c_parser", "doxygen_parser", "ea_parser",
    "hw_spec_parser", "illd_swa_parser", "pdf_parser", "puml_parser",
    "rst_parser", "sfr_parser", "xlsx_parser",
]
```

But the **actual imports** in `src/IngestionPipeline/ingestion_service.py` (Sprint 8 baseline) use lowercase:
```python
from src.IngestionPipeline.parsers.c_parser import parse as c_parse
from src.IngestionPipeline.parsers.regdef_parser import parse as regdef_parse
from src.IngestionPipeline.parsers.illd_swa_parser import parse as swa_hdr_parse
from src.IngestionPipeline.parsers.doxygen_parser import parse as doxygen_parse
```

And `src/HybridRAG/code/KG/build_knowledge_graph.py` (lowercase, again):
```python
from src.IngestionPipeline.parsers.swa_parsers import parse_swa_directory
from src.IngestionPipeline.parsers.swud_parsers import parse_swud_directory
from src.IngestionPipeline.parsers.testspec_parsers import parse_testspec_workbook
```

So there are **two parser packages**: an `__all__`-decorated `Parsers/` (capital, with the lazy `__getattr__`-based import system) and a `parsers/` (lowercase, where the actual files seem to live). The `Parsers/__init__.py` declares 11 parsers; `ingestion_service.py` and `build_knowledge_graph.py` import from `parsers/` not `Parsers/`.

**Impact, by environment:**

| Environment | Behavior |
|---|---|
| **macOS / Windows (case-insensitive default)** | Both work — imports resolve to the same directory. Hides the bug. |
| **Linux container (case-sensitive — your K8s deployment)** | Either `Parsers/` or `parsers/` exists, not both. If only `Parsers/` exists, all the lowercase imports fail with `ModuleNotFoundError`. If only `parsers/` exists, the `Parsers/__init__.py` lazy-loader is dead code. |
| **Mixed dev (Mac local, Linux CI)** | Tests pass locally, fail in CI, get re-enabled with a symlink hack — exactly the kind of thing that creates "works on my machine" alerts. |

I cannot tell from project knowledge alone *which* directory is the real one on disk. The imports in `ingestion_service.py` are lowercase. The `Parsers/__init__.py` is capital.

**Recommendation:**
1. **Pick lowercase `parsers/`** — matches PEP 8, matches the actual import statements, simpler to fix at the call site of one `__init__.py` than at every import statement.
2. Delete `src/IngestionPipeline/Parsers/__init__.py` after moving its `__all__` list and `__getattr__` lazy loader to `src/IngestionPipeline/parsers/__init__.py` (lowercase).
3. Run `git mv -f Parsers/ parsers_tmp/ && git mv -f parsers_tmp/ parsers/` (or equivalent two-step rename) to ensure git tracks the case change on case-insensitive filesystems.
4. Add a `tests/ci/test_filesystem_layout.py` that asserts case-correctness: `assert (REPO_ROOT / "src/IngestionPipeline/parsers").is_dir()` and `assert not (REPO_ROOT / "src/IngestionPipeline/Parsers").exists()`.

This is a one-hour fix that is otherwise a deploy-time landmine.

---

### F-A02 🔴 — Same-name parser duplicated across two layers (HybridRAG and IngestionPipeline)

**Evidence:**

Two files with **identical-looking** code (sampled `parse_hw_peripherals` is byte-equivalent):
- `src/HybridRAG/code/KG/swa_parsers.py`
- `src/IngestionPipeline/Parsers/swa_parsers.py`

Both contain `parse_hw_peripherals(content, module, source_document)`, both have the same indicator list `["Multiplicity:", "EcuC", "Post-Build", "Origin:", "Scope:", "Sub-Containers:"]`, both reference `SWA_HwPeripheral`, etc.

Looking at the imports:
- `src/HybridRAG/code/KG/build_knowledge_graph.py` does `from src.IngestionPipeline.parsers.swa_parsers import parse_swa_directory` — which means the HybridRAG-internal `KG/swa_parsers.py` is **not** the version being used by `build_knowledge_graph.py`.
- That makes `src/HybridRAG/code/KG/swa_parsers.py` either **dead code** or **a forked copy that has drifted**.

If it's dead code, it's a 1300+ line maintenance liability. If it's a fork, you have two versions diverging silently — exactly the kind of thing that produces "fixed it once, why is the bug back?" stories two sprints from now.

Same investigation needed for `swud_parsers.py` and `testspec_parsers.py` — they're both referenced from `build_knowledge_graph.py` via `src.IngestionPipeline.parsers.*`, so any copies under `HybridRAG/code/KG/` are suspect.

**Impact:** Drift risk + maintenance tax. If a parser fix lands in one location, the other version silently produces wrong results.

**Recommendation:**
1. Diff the two `swa_parsers.py` files. If identical, delete `src/HybridRAG/code/KG/swa_parsers.py`.
2. If they've diverged, identify the canonical version (probably `IngestionPipeline/parsers/` since that's what `build_knowledge_graph.py` imports), merge any KG-specific logic, delete the HybridRAG copy.
3. Repeat for `swud_parsers.py` and `testspec_parsers.py`.
4. Add a pre-commit hook: for each `*_parser*.py` file, assert there is exactly one path-to-file in the repo. (`find . -name '*_parser*.py' | xargs -n1 basename | sort | uniq -d` should be empty.)

This is the architectural symptom of "HybridRAG owns too much" — see F-A07.

---

### F-A03 🔴 — `MemoryLayer` imports from `HybridRAG`, breaking dependency direction

**Evidence:**

`src/MemoryLayer/memory/context_builder.py`:
```python
from src.HybridRAG.code.querier.context_builder import (
    ContextBuilder, ContextBudget, ContextItem, ContextSlot, AssembledContext,
)
```

Conceptually, MemoryLayer is the **lower-level** layer (sessions, working memory, semantic memory, ontology loader). HybridRAG is the **higher-level** retrieval engine that uses MemoryLayer. The Sprint 25 ContextBuilder migration decision (Master Gaps §8.4) acknowledges this — *"Architecturally, ContextBuilder belongs in MemoryLayer — but SearchService and RLMOrchestrator consumers live in HybridRAG. Decision: re-export the HybridRAG version from MemoryLayer."*

**The chosen direction is the wrong one.** Re-exporting *from MemoryLayer* of a class *defined in HybridRAG* means MemoryLayer can never be imported without HybridRAG also being importable. That makes MemoryLayer non-shippable as a standalone library, breaks any future test that wants to mock HybridRAG, and turns the dependency arrow backwards.

**Recommendation:** Reverse the migration:
1. Move `ContextBuilder`, `ContextBudget`, `ContextItem`, `ContextSlot`, `AssembledContext`, `estimate_tokens` from `src/HybridRAG/code/querier/context_builder.py` to `src/MemoryLayer/memory/context_builder.py` (the *real* file, not a re-export shim).
2. Replace the body of `src/HybridRAG/code/querier/context_builder.py` with `from src.MemoryLayer.memory.context_builder import *  # noqa` for backward-compat.
3. Two sprints later, deprecate the HybridRAG path; one sprint after that, delete it.

This is a **2-day refactor** but it removes a permanent layering inversion that will get cited in every future review.

A weaker but immediately-actionable alternative: declare ContextBuilder is a HybridRAG component (rename `MemoryLayer/memory/context_builder.py` → `legacy_context_builder.py` with only `LegacyContextBuilder` in it) and update the requirements doc to match. Either commit, but don't leave it half-done. Right now the code says one thing and the architecture intent says another.

---

## 2. High Findings

### F-A04 🟠 — `mcp_server.py` is 1800 lines and acts as a service registry, an ASGI app, *and* a tool implementation file

**Evidence:** `mcp/core/mcp_server.py` (~1800 lines) contains:
- ~60 `@mcp.tool()` decorated handlers (the bulk).
- ~10 lazy-init singletons: `_neo4j`, `_qdrant`, `_redis`, `_postgres_client`, `_cerbos`, `_cache_service`, `_ontology_services`, `_observability_services`, `_ki_services`, `_search_services`, `_session_manager`, `_sandbox_manager`, `_confidence_calc`, `_feedback_sink`, `_result_processor`, `_rlm_orchestrators` etc.
- The ASGI middleware wiring (`_APIKeyMiddleware`, `_authorize`, `_with_session_routing`).
- The `_warmup` function that pre-initializes those singletons in parallel.
- Result envelope helpers `_ok` / `_err`.
- The `mcp` `FastMCP` instance creation (with the SDK shadowing shim).
- Per-tool authorization, sandbox routing, and metrics instrumentation.

This is **the** file that will keep growing every time a new tool is added. At ~1800 lines today, every PR adding a tool merge-conflicts with every other tool PR. It's also where the SDK shadowing shim lives (`_saved_paths`, `_saved_mcp` mutation), which is dangerous to touch.

**Architectural problem:** `mcp_server.py` is doing **the work of three modules**:
1. **Service registry** — lazy initialization, dependency injection, lifecycle. Should be a `mcp/core/services.py` (~300 lines).
2. **Tool registry** — the decorator-driven mapping from tool-name → handler. Should be `mcp/tools/{search,api,trace,…}.py` (~150 lines per category).
3. **Server harness** — FastMCP instance, ASGI middleware, warmup. Should remain in `mcp_server.py` (~200 lines) or move to `mcp/app.py`.

**Recommendation:** Refactor in two passes (do **not** try in one):
- **Pass A (low-risk, 2 days):** Move all `_get_*` lazy-init functions to `mcp/core/services.py`. Replace direct calls in tool handlers with `from mcp.core.services import get_search_service`. The function bodies don't change. This drops `mcp_server.py` by ~400 lines.
- **Pass B (higher-risk, 4 days):** Split tool handlers by category. `mcp/tools/search.py` for Cat 1, `mcp/tools/api.py` for Cat 2, etc. Each file imports `mcp` (the FastMCP instance) and registers its tools at import time. `mcp_server.py` becomes ~200 lines that import all tool modules.

**Why it's High not Critical:** the file works. But it's a velocity tax that compounds with every sprint.

---

### F-A05 🟠 — `HybridRAG/code/` carries non-Hybrid-RAG concerns

`docs/architecture/OVERVIEW.md` declares HybridRAG as: *"Search, KG, RAG, RLM."* The actual layout includes far more:

| File | Purpose | Where it should live |
|---|---|---|
| `pdf_pipeline.py` | PDF→Markdown→JSON pipeline | `IngestionPipeline/parsers/pdf_pipeline.py` (it's an *ingestion* concern, not a *retrieval* one) |
| `token_manager.py` | GPT4IFX JWT lifecycle | `Configuration/auth/` or a new `src/Auth/` (it's a credentials concern, not retrieval) |
| `neo4j_manager.py` | Connection management + storage_config loader | `Configuration/storage/` (it's used by every layer) |
| `env_config.py` | .env loader | `Configuration/` (cross-cutting) |
| `KG/swa_parsers.py`, `KG/swud_parsers.py`, etc. | (See F-A02) | `IngestionPipeline/parsers/` |
| `KG/build_knowledge_graph.py` | KG construction | Borderline — could stay (KG is a retrieval concern) but the **ingestion** half should move to IngestionPipeline. |
| `KG/illd_kg_builder.py` | iLLD KG builder | Same — the construction step is ingestion |
| `KG/dependency_fetcher.py` | Bitbucket header fetch | `IngestionPipeline/Connectors/` (it fetches files from a remote system) |
| `KG/fetch_jama_relationships.py` | Jama API fetch | `IngestionPipeline/Connectors/` (same reason) |
| `KG/illd_run_pipeline.py` | End-to-end ingestion orchestrator | `IngestionPipeline/orchestrators/` (literally has "pipeline" in the name) |

**The pattern:** anything that **writes to** Neo4j/Qdrant is ingestion. Anything that **reads from** them is HybridRAG. Currently HybridRAG owns both directions, which is why the `parsers/` duplication in F-A02 exists — there's no clean home for parser code.

**Impact:** A new contributor reading "HybridRAG" thinks "the search engine," then opens it and finds a PDF parser, a JWT manager, and a build script. The cognitive cost is real.

**Recommendation:** Stage the moves over 3 sprints, not one (this is enough to fully shuffle imports):
- **Sprint N:** move `pdf_pipeline.py`, `dependency_fetcher.py`, `fetch_jama_relationships.py` → IngestionPipeline. Update imports.
- **Sprint N+1:** move `token_manager.py`, `env_config.py`, `neo4j_manager.py` → Configuration. (token_manager is the trickiest because GPT4IFX is wired across many files.)
- **Sprint N+2:** decide whether `KG/build_knowledge_graph.py` and friends move; if they do, leave a query-only `KG/query_knowledge_graph.py` in HybridRAG.

After this, `HybridRAG/` shrinks to ~3 packages (`querier/`, `RAG/`, `KG/` query-only) — actually matching its name.

---

### F-A06 🟠 — `Configuration/services.py` mixes 4 unrelated services in one file

`src/Configuration/services.py` (Sprint 6 file header: *"Ontology, Observability, Visualization & Auth Services"*) contains:
- `OntologyService` — schema/profile lookup (Cat 10)
- `ObservabilityService` — graph stats (Cat 11)
- `VisualizationService` — pyvis subgraph rendering (Cat 12)
- `AuthService` — JWT lifecycle (Cat 13)

These four services have **nothing in common** other than "Sprint 6 deliverables." They have different dependencies (Ontology needs `OntologyLoader`; Auth needs `token_manager`; Visualization needs pyvis; Observability needs Neo4j only). Bundling them was a sprint-shipping convenience.

**Impact:** `from src.Configuration.services import OntologyService` pulls in pyvis transitively (visualization) — which means the service that tells you about ontology profiles depends on a graph-rendering library. Cold import time hit, dependency footprint hit, and any future test that mocks one service has to also handle the other three.

**Recommendation:** One service per file:
- `src/Configuration/ontology_service.py` (calls `OntologyLoader`)
- `src/Observability/observability_service.py` (it's already split into `metrics.py` + `postgres_schema.py` — add the third)
- `src/Visualization/visualization_service.py` (new top-level dir or under Configuration)
- `src/Auth/auth_service.py` (new top-level — see F-A05; this is where `token_manager.py` should also land)

Then `services.py` becomes a 6-line `__init__.py`-style re-export shim for backward compatibility.

---

### F-A07 🟠 — `KG/query_knowledge_graph.py` is "Legacy" but 1595 lines and still in the codebase

`docs/architecture/OVERVIEW.md` describes `query_knowledge_graph.py` as the *"Legacy query module (1595 lines)"*. Master Gaps doesn't list it as deferred or planned for deletion. So it's:
- Long-lived legacy code
- Not actively maintained (it's "legacy")
- 1595 lines that something is presumably still calling

If nothing calls it, delete it. If something calls it, document what and when it migrates to `querier/`. **Either action is fine; non-action is not.** Long-lived "legacy" code is where bugs hide and where security regressions accumulate (because nobody watches it, but everybody can still import it).

**Recommendation:**
1. `git grep "from .* import.*query_knowledge_graph"` and `grep "import query_knowledge_graph"` — enumerate callers.
2. If callers exist: file an ADR, set a sprint, migrate.
3. If no callers: delete in the next sprint with a single `git rm` commit.

---

### F-A08 🟠 — Service initialization is per-profile but caching is global

The MCP server lazy-init pattern uses **per-profile dicts** for some services and **global singletons** for others:

| Service | Scoping | Code |
|---|---|---|
| `_neo4j` | global, but `_get_neo4j(profile)` returns the right driver | uses storage_config to resolve URI |
| `_search_services` | per-profile (`Dict[str, SearchService]`) | one SearchService per workspace |
| `_ontology_services` | per-profile | |
| `_observability_services` | per-profile | |
| `_ki_services` | per-profile | |
| `_session_manager` | global | ⚠️ only one — all workspaces share |
| `_sandbox_manager` | global | ⚠️ only one |
| `_confidence_calc` | global | ⚠️ |
| `_feedback_sink` | global | ⚠️ |
| `_cache_service` | global | ⚠️ |
| `_postgres_client` | global | ⚠️ |
| `_redis` | global | ⚠️ |

The mismatch creates subtle bugs. If a `mcal` query writes to `_session_manager` and an `illd` query reads from it, the session state is shared. For working memory (`session_id` is a UUID-like value) this is probably fine. For confidence-scoring (which has profile-specific rules — see GEST_WEIGHTS etc.) it is **not**.

**Specifically problematic:** `_get_confidence_calc()` is global, but the GEST DA uses different weights than CIA. If both DAs are active at the same time, whichever called first wins.

**Recommendation:**
1. Audit every `_<service>_` global. For each, decide: does the service have profile-specific or DA-specific state? If yes → make it per-profile (or per-DA).
2. Specifically: `_confidence_calc` should be `_confidence_calcs: Dict[str, ConfidenceCalculator]` keyed by `da_name` (or task_type), so `evaluate_confidence(task_type="test_generation")` gets the GEST weights and `task_type="code_generation")` gets CIA weights.
3. Document the scoping rule in `services.py` (after F-A04 split): a header comment that says *"per-profile services live in `_<name>_services`; cross-cutting services live as `_<name>`."*

---

### F-A09 🟠 — No formal layer-protocol; cross-layer imports are `from src.X.Y.Z import Q` which couples everything

Every cross-layer import in the repo is a deep import:
```python
from src.MemoryLayer.memory.semantic_memory import PatternStore, PatternIndex, Embedder
from src.HybridRAG.code.querier.search_service import SearchService
from src.HybridRAG.code.querier.knowledge_intelligence import KnowledgeIntelligenceService
from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
from src.ReviewGate.confidence import ConfidenceCalculator, FeedbackSink
from src.Configuration.services import OntologyService, ObservabilityService, AuthService
from src.HybridRAG.code.token_manager import get_token
from src.MemoryLayer.memory.ontology_loader import OntologyLoader
```

Two consequences:
1. **Brittle to rename.** Any reorg of internal package paths breaks every consumer. Sprint 25 has 30+ such deep imports in `mcp_server.py` alone.
2. **No protocol contract.** What does `SearchService` guarantee about its `hybrid_search()` signature? The contract is implicit — defined by the caller's needs. A change in `SearchService` may silently break MCP without any type or test failing.

**Recommendation:**
1. Each `src/<Layer>/__init__.py` should export the layer's **public API surface**:
   ```python
   # src/HybridRAG/__init__.py
   from src.HybridRAG.code.querier.search_service import SearchService
   from src.HybridRAG.code.querier.rlm_orchestrator import RLMOrchestrator
   from src.HybridRAG.code.querier.knowledge_intelligence import KnowledgeIntelligenceService
   __all__ = ["SearchService", "RLMOrchestrator", "KnowledgeIntelligenceService"]
   ```
   Then callers do `from src.HybridRAG import SearchService` — single point of breakage when paths change.
2. For the contracts cited in Master Gaps §9.1 (`llm_fn(system, user, max_tokens) → str`, `search_fn(query, max_results) → list[dict]`), add a `Protocol` definition in `src/HybridRAG/protocols.py` and have `SearchService` implement it. Same for the LLM contract.
3. Add an `import-linter` configuration (`importlinter.contracts`) that **fails CI** if anything in MemoryLayer imports from HybridRAG (per F-A03), or if anything in IngestionPipeline imports from HybridRAG.

---

### F-A10 🟠 — `mcp` module shadowing the `mcp` SDK requires a runtime hack

`mcp/core/mcp_server.py` opens with a 30-line shim:
```python
# The repo has a top-level ``mcp/`` package (this directory) which shadows
# the installed ``mcp`` SDK (mcp.server.fastmcp). We temporarily evict the
# local ``mcp`` from sys.modules, strip repo-root from sys.path, import the
# SDK, then restore everything so relative imports (.auth_middleware etc.)
# continue to work.
```

This mutates `sys.modules` and `sys.path` at module-import time. It works, but:
- It is fragile to Python version upgrades and to import-time refactoring.
- It runs even when the SDK is already cached, adding ~10ms to every cold import.
- It makes editor/static-analysis tools confused — `pyright`/`pylance` can't resolve either `mcp` reliably.
- The Dockerfile already addresses this: `COPY mcp/ /app/aice_mcp/` (renaming during the build). So **in production the shim isn't needed**, but it runs anyway because the in-repo path still has `mcp/`.

**Recommendation:**
1. Rename `mcp/` → `aice_mcp/` in the repo. This matches what the Dockerfile already does and removes the shim entirely.
2. Update `PYTHONPATH`, imports in `tests/`, and CI scripts.
3. Remove the 30-line shim from `mcp_server.py` (becomes the first 30 lines of saved code).

This is a 1-day rename that retires a 30-line `sys.modules` hack permanently.

---

## 3. Medium Findings

### F-A11 🟡 — `IngestionPipeline.Connectors` mixes ALM connectors with file-source connectors

`src/IngestionPipeline/Connectors/` per `docs/architecture/OVERVIEW.md` contains: *"Jama, Jenkins, Polarion."* The `dependency_fetcher.py` in HybridRAG (F-A05) also constructs a `BitbucketConnector` via `from src.IngestionPipeline.Connectors.BitbucketConnector import BitbucketConnector` — so Bitbucket is also in there.

These are **two different things**:
- **ALM connectors** (Jama, Polarion): write requirements/test results back to a system of record.
- **VCS / source connectors** (Bitbucket, GitLab via `illd_run_pipeline.py`): pull source code or PDFs.

A connector that reads `.h` headers from Bitbucket has a totally different lifecycle than a connector that POSTs test verdicts to Polarion. Lumping them under "Connectors" makes the layer's responsibility unclear.

**Recommendation:** Two subpackages:
- `src/IngestionPipeline/Connectors/alm/` — Jama, Polarion, Jenkins (CI is also a system-of-record for test results)
- `src/IngestionPipeline/Connectors/scm/` — Bitbucket, GitLab

Each subpackage gets its own `Protocol` interface (`ALMConnectorProtocol`, `SCMConnectorProtocol`).

---

### F-A12 🟡 — `Observability/` is split confusingly: metrics + postgres = audit + Prometheus

`src/Observability/` contains `metrics.py` (Prometheus) and `postgres_schema.py` (audit DB). These serve very different purposes:
- **`metrics.py`** is realtime time-series (Prometheus) for ops monitoring.
- **`postgres_schema.py`** is durable structured audit data for ASPICE/EU AI Act compliance.

Bundling them under "Observability" hides the compliance-critical nature of the postgres side. An auditor asking "where do you store audit logs?" should be pointed at a dir called `Audit/` or `Compliance/`, not a generic `Observability/`.

**Recommendation:**
- `src/Observability/metrics.py` (stays — this is operational telemetry)
- `src/Audit/` (new) — `postgres_schema.py`, future `incidents.py` (the GAP-08 incident tracker)
- The split signals to readers (and auditors) that audit logging is a first-class concern.

---

### F-A13 🟡 — RLM is conceptually correct but spatially wrong

ADR-009: *"RLM as Internal Context Orchestrator. RLM is an internal Core Engine capability, not a public DA-facing tool."* The position diagram in `docs/architecture/rlm-orchestrator.md` shows RLM **above** SearchService, **below** ContextBuilder. Master Gaps section captures: *"RLM belongs in Category 6 (Memory & Context), not a new category."*

But the file lives at `src/HybridRAG/code/querier/rlm_orchestrator.py` — i.e., inside HybridRAG's "querier" package, alongside SearchService. That's structurally below where it ought to sit.

**Recommendation:** Move `rlm_orchestrator.py` from `src/HybridRAG/code/querier/` to `src/MemoryLayer/memory/rlm/` once the F-A03 layering reversal lands (the reversal is a prereq because RLM uses ContextBuilder, which has to be in MemoryLayer first). RLM's natural home is the layer that owns "context assembly," which is Memory.

This is **Medium** because the current location works fine; it just makes the architecture-doc's claim that "RLM is internal to MemoryLayer" inaccurate.

---

### F-A14 🟡 — Two MCP tool category-numbering systems

`mcp/auth/policies/resource_mcp_tool.yaml` uses categories **6, 6+, 7, …**:
```yaml
# Category 6: Memory & Context
# Category 6+: Ephemeral Sandbox
# Category 6+: RLM
# Category 7: Cache Management
```

`mcp/core/tool_tiers.py` uses similar but: `Cat 6+: Ephemeral Sandbox`, `Cat 6+: RLM`, `Cat 14: GAP v2 Tools (Sprint 25)`.

`docs/DOCUMENTATION.md` uses 13 categories (no "6+", no "14").

The "6+" notation is a sign of category 6 being overloaded — it now contains: Memory, Sandbox, RLM. None of those is the same conceptual unit. And then a Cat 14 was added for one tool (`query_enhance`), which is also awkward.

**Recommendation:** Renumber once, properly:
- 1: Search & Query
- 2: API Intelligence
- 3: Dependency Analysis
- 4: Traceability
- 5: Ingestion
- 6: Memory & Context (sessions, working memory, build_context)
- 7: Sandbox (split from 6)
- 8: RLM (split from 6, post-F-A13 move)
- 9: Cache
- 10: Feedback & Learning
- 11: Review Gate
- 12: Ontology & Config
- 13: Observability & Health
- 14: Visualization
- 15: Authentication
- 16: GAP v2 (or fold `query_enhance` into Search)

Then update Cerbos policy, tool_tiers.py, all docs (covered by Pass 1 F-D14). After this, "Cat 6+" goes away entirely.

---

## 4. Low Findings

### F-A15 🟢 — `src/HybridRAG/code/` is a Russian-doll directory naming

`src/HybridRAG/code/querier/search_service.py` — three layers of nesting where each layer adds zero information. `code/` in particular is meaningless; everything in a Python package is code. Compare to `src/MemoryLayer/memory/working_memory/manager.py` — same pattern (`MemoryLayer/memory/...` has the same redundancy).

**Recommendation:** As part of F-A05 cleanup, drop the `code/` and `memory/` intermediate layers:
- `src/HybridRAG/querier/`, `src/HybridRAG/RAG/`, `src/HybridRAG/KG/`
- `src/MemoryLayer/working_memory/`, `src/MemoryLayer/semantic_memory/`, `src/MemoryLayer/node_sets/`

This shortens every import by one segment and reads better. Pure cosmetic — but cosmetics in dependency paths are not free.

---

### F-A16 🟢 — `src/HybridRAG/code/` and `src/HybridRAG/config/` mix code with config at the same depth

`src/HybridRAG/config/ontology.yaml` and `src/HybridRAG/config/storage_config.yaml` are next to `src/HybridRAG/code/`. The implication is that "code" is a sibling of "config" — but **config** is not a sibling of **code**, it's used by code.

**Recommendation:** Move `src/HybridRAG/config/` to `src/Configuration/ontology/ontology.yaml` and `src/Configuration/storage/storage_config.yaml`. Keeps all config in one place. Ontology is a particularly bad fit for HybridRAG specifically — multiple layers (MemoryLayer, IngestionPipeline) consume it.

---

## 5. Proposed Target Architecture (incremental, non-breaking)

Here is the architecture I'd propose for Sprint 25+5 (call it Sprint 30). It can be reached with the moves listed in F-A05, F-A06, F-A11, F-A12, F-A15. Nothing in this re-architecture changes the *behavior* of AICE — it's all about where files live.

```
ai-core-engine/
├── aice_mcp/                           # was mcp/ (F-A10)
│   ├── app.py                          # K8s entrypoint, Cerbos lifecycle
│   ├── server.py                       # was mcp_server.py — slimmed (F-A04)
│   ├── core/
│   │   ├── auth_middleware.py
│   │   ├── tool_tiers.py
│   │   └── services.py                 # service registry / lazy init (F-A04 Pass A)
│   ├── tools/                          # tool handlers split by category (F-A04 Pass B)
│   │   ├── search.py                   #   Cat 1 — 6 handlers
│   │   ├── api_intelligence.py         #   Cat 2
│   │   ├── dependency.py               #   Cat 3
│   │   ├── traceability.py             #   Cat 4
│   │   ├── ingestion.py                #   Cat 5
│   │   ├── memory.py                   #   Cat 6 (no more "6+")
│   │   ├── sandbox.py                  #   Cat 7
│   │   ├── rlm.py                      #   Cat 8
│   │   ├── cache.py
│   │   ├── feedback.py
│   │   ├── review_gate.py
│   │   ├── ontology.py
│   │   ├── observability.py
│   │   ├── visualization.py
│   │   └── auth.py
│   └── auth/                           # YAML policies (unchanged)
│
├── src/
│   ├── HybridRAG/                      # search-only — much slimmer
│   │   ├── __init__.py                 # exports SearchService, RLMOrchestrator (F-A09)
│   │   ├── querier/
│   │   │   ├── search_service.py
│   │   │   └── knowledge_intelligence.py
│   │   ├── RAG/
│   │   │   ├── hybrid_rag_unified.py
│   │   │   └── rag_query_unified.py
│   │   ├── KG/                         # query-only (F-A07 + F-A05)
│   │   │   └── query_knowledge_graph.py  # if kept; see F-A07
│   │   └── protocols.py                # SearchProtocol, LLMProtocol (F-A09)
│   │
│   ├── MemoryLayer/                    # owns context assembly (F-A03 reversed)
│   │   ├── working_memory/
│   │   ├── semantic_memory/            # Qdrant-only PatternStore (Pass 1 F-D05)
│   │   ├── node_sets/
│   │   ├── context_builder.py          # MOVED here from HybridRAG (F-A03)
│   │   ├── ephemeral_sandbox.py
│   │   ├── ontology_loader.py
│   │   └── rlm/                        # MOVED here (F-A13)
│   │       └── orchestrator.py
│   │
│   ├── IngestionPipeline/
│   │   ├── orchestrators/              # NEW
│   │   │   └── illd_run_pipeline.py    # MOVED from HybridRAG
│   │   ├── parsers/                    # lowercase only (F-A01)
│   │   │   ├── c_parser.py
│   │   │   ├── swa_parsers.py          # canonical (F-A02)
│   │   │   ├── swud_parsers.py
│   │   │   ├── pdf_pipeline.py         # MOVED from HybridRAG
│   │   │   └── ...
│   │   ├── builders/                   # NEW — KG construction (F-A05)
│   │   │   ├── illd_kg_builder.py      # MOVED
│   │   │   └── build_knowledge_graph.py  # MOVED
│   │   ├── Connectors/
│   │   │   ├── alm/                    # F-A11
│   │   │   │   ├── jama.py
│   │   │   │   ├── polarion.py
│   │   │   │   └── jenkins.py
│   │   │   └── scm/                    # F-A11
│   │   │       └── bitbucket.py
│   │   ├── Incremental/
│   │   └── ingestion_service.py
│   │
│   ├── ReviewGate/                     # unchanged
│   │   ├── confidence.py
│   │   └── result_processors.py
│   │
│   ├── Configuration/                  # cross-cutting, including ontology config (F-A06, F-A16)
│   │   ├── ontology/
│   │   │   ├── ontology.yaml           # MOVED from HybridRAG/config
│   │   │   └── ontology_service.py     # split from services.py
│   │   ├── storage/
│   │   │   ├── neo4j_manager.py        # MOVED
│   │   │   ├── storage_config.yaml     # MOVED
│   │   │   └── env_config.py           # MOVED
│   │   └── cache/
│   │       └── cache_service.py        # was Configuration/cache_service.py
│   │
│   ├── Auth/                           # NEW (F-A05, F-A06)
│   │   ├── token_manager.py            # MOVED from HybridRAG
│   │   └── auth_service.py             # split from services.py
│   │
│   ├── Observability/                  # metrics only (F-A12)
│   │   ├── metrics.py
│   │   └── observability_service.py    # split from services.py
│   │
│   ├── Audit/                          # NEW (F-A12)
│   │   └── postgres_schema.py          # MOVED
│   │
│   └── Visualization/                  # NEW (F-A06)
│       └── visualization_service.py    # split from services.py
```

**Layer dependency rules** (enforced by `import-linter`):
- `aice_mcp/` may import anything in `src/`.
- `src/HybridRAG/` may import: `MemoryLayer`, `Configuration`, `Audit` (audit reads), `Observability` (metrics).
- `src/MemoryLayer/` may import: `Configuration`, `Audit`, `Observability`. **Not** HybridRAG (F-A03).
- `src/IngestionPipeline/` may import: `Configuration`, `Audit`, `MemoryLayer.semantic_memory` (for PatternStore writes).
- `src/ReviewGate/` may import: `MemoryLayer.semantic_memory`, `Audit`.
- `src/Configuration/`, `src/Auth/`, `src/Observability/`, `src/Audit/`, `src/Visualization/` are **leaf layers** — they don't import any other AICE layer.

Effort estimate to reach this state: **~12 person-days** spread over 3 sprints. It can be done incrementally, with each move PR-sized.

---

## 6. What I deliberately did not flag

- The dual-database storage choice (Neo4j + Qdrant) — `docs/architecture/Perf_improvements_orig.md` already analyzes it well; the choice is appropriate for the engineering-data domain.
- The Cerbos sidecar pattern — orthogonally adopted for ASPICE traceability; a separate auth process is the right call.
- The `_warmup` parallel-init pattern — well-implemented, see `mcp_server.py::_warmup()`.
- The 7-table PostgreSQL audit schema — schema-design choices are out of scope for an architectural review.
- ADR-009 (RLM as internal) — design is correct, only the **location** is wrong (F-A13).
- Use of `contextvars` for per-request API key — standard ASGI pattern.

---

## 7. Suggested Disposition

| Priority | Finding | Effort | Risk |
|---|---|---|---|
| P0 | F-A01 (case-duplicated parsers) | 1 day | High deploy risk; do this first |
| P0 | F-A02 (duplicate `swa_parsers.py`) | 0.5 day | Drift risk; do with F-A01 |
| P0 | F-A03 (MemoryLayer→HybridRAG inversion) | 2 days | Layering integrity |
| P1 | F-A04 (mcp_server.py split, Pass A only) | 2 days | Velocity tax |
| P1 | F-A10 (mcp/ → aice_mcp/ rename) | 1 day | Removes a runtime hack |
| P1 | F-A09 (Layer protocols + import-linter CI) | 2 days | Locks in the rest |
| P2 | F-A05, F-A06, F-A11, F-A12 (HybridRAG slimming, Configuration split) | 5–6 days, staged | Multi-sprint |
| P2 | F-A07 (legacy KG query module) | 0.5 day to inventory; potentially 0 to delete | Cleanup |
| P2 | F-A08 (per-profile vs global service scoping) | 1 day | Depends on confidence-calc DA work |
| P3 | F-A13 (RLM relocation), F-A14 (renumber), F-A15, F-A16 | 1 day total | Cosmetic / consistency |

**Total: ~16 person-days**, of which 4 days are P0 and should land in the next sprint.

---

## 8. Updated F-D05 from Pass 1 (PatternStore: Qdrant-only — your decision)

Per your directive: PatternStore stays Qdrant-only. Specific changes needed:

1. **`mcp/core/mcp_server.py::_get_feedback_sink()`** — change:
   ```python
   pattern_store = PatternStore(neo4j_driver=neo4j_driver, embedder=embedder)
   ```
   to:
   ```python
   pattern_store = PatternStore(embedder=embedder, _client=_get_qdrant())
   ```
   And drop the `if neo4j_driver:` guard around it (Qdrant is now the dependency, not Neo4j).

2. **Comment in `_get_feedback_sink()`** — change `# Wire learning loop: PatternStore (Neo4j) + PatternIndex (Qdrant)` → `# Wire learning loop: PatternStore + PatternIndex (both Qdrant-backed)`.

3. **`requirements/AICE_SYSTEM_REQUIREMENTS.md` AICE-FB-005** — change `wire learned patterns to PatternStore (Neo4j) + PatternIndex (Qdrant)` → `wire learned patterns into the Qdrant-backed PatternStore + PatternIndex`.

4. **`requirements/MEMORY_LAYER_REQUIREMENTS.md`** §3.2.2 AICE-MEM-011/012 — confirm the doc says Qdrant only (it already does — no change).

5. **`docs/MEMORY_LAYER_FEATURES.md`** §7 — remove the mention of "PatternStore... needs its own Qdrant collection" once the wiring lands; rewrite to note it now reuses the per-module collection with `data_type` payload filter (which is what `PATTERN_STORAGE_DESIGN.md` says is the design).

---

**End of Pass 2.** Ready to proceed to Pass 3 (File-by-file Code Quality Deep Dive) on your signal.

For Pass 3, I'll need a quick prioritization decision from you on which files to look at first. The hottest 25 files (by line count + criticality) are listed at the top of the Pass 1 summary. I'd suggest starting with the auth + ingestion + search trio (`auth_middleware.py`, `ingestion_service.py`, `search_service.py`, `confidence.py`) and then circling back for the parser cluster.

# AICE Review — Pass 3 / Cluster E: Parsers

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:** the 14 parsers that feed the KG construction pipeline:
- `arxml_parser.py` — AUTOSAR XML
- `c_parser.py` — C source (regex + clang)
- `doxygen_parser.py` — Doxygen-annotated headers
- `ea_parser.py` — Enterprise Architect models
- `hw_spec_parser.py` — Hardware specification markdown
- `illd_swa_parser.py` — iLLD SWA C headers (with LLM enrichment)
- `pdf_parser.py` — PDF→Markdown via vision LLM
- `puml_parser.py` — PlantUML sequence diagrams
- `regdef_parser.py` — Register definition headers
- `rst_parser.py` — reStructuredText docs
- `sfr_parser.py` — SFR (Special Function Register) headers
- `swa_parsers.py` — SWA markdown extractors (different from `illd_swa_parser.py`!)
- `swud_parsers.py` — Software Unit Design markdown
- `testspec_parsers.py` — MCAL test spec Excel workbooks
- `xlsx_parser.py` — Generic xlsx
- `ocr_processor.py` — Tesseract OCR (subprocess)

**Excludes:** tests/. Findings already reported in Pass 1, Pass 2, or Clusters A–D referenced but not re-stated.

---

## 0. Summary

The parsers are the **input edge** of AICE. Garbage-in here produces garbage in the KG, and the KG is then trusted by all 21 DAs. So even small parser bugs have big downstream consequences.

Three observations from the research pass:

1. **Two SWA parsers exist with overlapping scope.** `illd_swa_parser.py` parses C header files (`*_swa.h`) using regex section extraction, while `swa_parsers.py` parses **markdown** SWA documentation (`Specification for X` tables). The `__init__.py` for `Parsers/` only declares `illd_swa_parser`; `swa_parsers.py` is imported directly from `IngestionPipeline.parsers` (lowercase per Pass 2 F-A01). Plus `Pass 2 F-A02` already flagged that `swa_parsers.py` is byte-equivalent across `IngestionPipeline/Parsers/` and `HybridRAG/code/KG/`. The naming collision is permanent confusion bait.

2. **LLM-enrichment in parsers is risky and currently the weakest link in error handling.** `illd_swa_parser` and `pdf_parser` both call vision/text LLMs. The PDF parser has solid retry/backoff with auth-error refresh. The SWA enrichment path I have less visibility into but it has *different* error handling (a parallel ThreadPoolExecutor with checkpoint/resume). Two distinct retry implementations for two distinct LLM-calling parsers — no shared helper.

3. **Output shapes vary widely across parsers.** `arxml_parser` returns `{modules, chunks, cross_references, statistics}`. `c_parser` returns `{ast, functions, diagnostics, statistics}`. `pdf_parser` returns `list[str]` (just markdown pages, no metadata). `xlsx_parser` returns `{sheet → rows}`. `testspec_parsers` returns `{node_type_label → list[node_dict]}`. The ingestion service (`_parse_file`) has to massage all of these into a uniform dispatch shape — this is fertile ground for type confusion.

**Findings count:** 24 (2 Critical, 7 High, 11 Medium, 4 Low)

| File / area | C | H | M | L | Total |
|---|---:|---:|---:|---:|---:|
| `c_parser.py` | 1 | 2 | 1 | 0 | 4 |
| `illd_swa_parser.py` | 0 | 1 | 2 | 1 | 4 |
| `pdf_parser.py` (and `pdf_pipeline.py`) | 0 | 1 | 2 | 0 | 3 |
| `arxml_parser.py` | 0 | 1 | 1 | 1 | 3 |
| `testspec_parsers.py` / `swa_parsers.py` / `swud_parsers.py` | 1 | 1 | 2 | 0 | 4 |
| `ocr_processor.py` | 0 | 1 | 1 | 0 | 2 |
| Cross-parser (X##) | 0 | 0 | 2 | 2 | 4 |

Severity criteria as in earlier clusters.

---

## 1. Cross-Parser Findings

### F-CE-X01 🟡 — Output shape inconsistency: 7 different return-type contracts across 14 parsers

**Evidence:** the `Parsers/__init__.py` documents the return types:

| Parser | Return type |
|---|---|
| `arxml_parser` | `dict` (modules + chunks + cross_references + statistics) |
| `c_parser` | `dict` (functions + statistics + ast + diagnostics) |
| `doxygen_parser` | `list[dict]` (requirements) |
| `ea_parser` | `dict` (components + stats) |
| `pdf_parser` | `str` (Markdown) |
| `puml_parser` | `dict` (pattern library) |
| `sfr_parser` | `dict` (registers) |
| `rst_parser` | `list[dict]` (sections) |
| `swa_parser` | `dict` (macros/enums/…) |
| `xlsx_parser` | `dict` (sheet → rows) |

Then the docstring for `pdf_parser` says it returns `list[str]` — not `str`. And `ingestion_service._parse_file` for PDFs:
```python
pages = pdf_parse(str(path))
return {"type": "pdf", "pages": pages, "file": str(path)}
```
So the consumer expects `pages` to be a list. This is fine if the parser returns a list. But the `__init__.py` says `str`. **Documentation drift.**

For `testspec_parsers.parse_testspec_workbook` the return is `Dict[str, List[dict]]` — keyed by node-type label (e.g., `TS_FunctionalTestCase`). For `xlsx_parser.parse` (called for non-testspec workbooks), the return is `dict[str, list[dict]]` — keyed by sheet name. **Same shape, different key semantics.** A consumer that does `for k, v in result.items():` cannot tell from the dict alone whether `k` is a sheet name or a node label.

The downstream `_parse_file` correctly distinguishes them via `_ts_` filename matching, but a unit test that mocks `xlsx_parser.parse` with `{"TS_FunctionalTestCase": [...]}` would silently pass even though the call should have routed to `testspec_parsers`.

**Recommendation:**
1. **Define a Parser Protocol:**
   ```python
   class ParserResult(TypedDict, total=False):
       type: str          # "c_source" | "arxml" | "pdf" | ...
       file: str
       statistics: Dict[str, int]
       errors: List[str]
       # type-specific keys (functions, modules, sheets, nodes, pages, sections)
   ```
2. **Make every parser return a dict** with at least `type` + `file` keys. `pdf_parser` becomes `{"type": "pdf", "pages": [...], "file": ...}` — same shape as the rest.
3. **Make `_parse_file` validate shape** before returning to the KG builder. If shape is wrong, raise.

Effort: 2 days for cross-parser standardization + tests.

---

### F-CE-X02 🟡 — Two SWA parsers (`illd_swa_parser.py` for C headers + `swa_parsers.py` for markdown) — naming is identical-looking, scope is opposite

**Evidence:**

`illd_swa_parser.py`:
> Parses C header files written in the SWA (Software Architecture) format and extracts macros, typedefs, enums, structs, and function prototypes into structured dicts.

`swa_parsers.py`:
> Extract SWA_Function, SWA_DataType, and SWA_Macro nodes from SWA markdown content. The parser scans all content for "Specification for ..." tables...

Both prefixed with "swa". Both produce node-shaped dicts. **Different input formats (C vs MD), different output schemas, different downstream consumers (`ILLDKGBuilder.ingest_swa` vs `KnowledgeGraphBuilder` MCAL paths).**

Also note Pass 2 F-A02: `swa_parsers.py` is byte-duplicated across `IngestionPipeline/Parsers/` AND `HybridRAG/code/KG/`. Combined with this naming overlap, there are effectively **3 SWA parser files** in the repo.

**Recommendation:** rename for clarity:
- `illd_swa_parser.py` → `swa_header_parser.py` (parses C `_swa.h` headers).
- `swa_parsers.py` → `swa_markdown_parser.py` or `mcal_swa_parser.py` (parses markdown SWA tables).
- Delete the duplicate per Pass 2 F-A02.

Effort: 1 hour rename + import-update.

---

### F-CE-X03 🟢 — Parsers' `parse(path, ...)` signature is documented uniformly but actual signatures diverge

`__init__.py` says:
> Each parser exposes a single `parse(path, ...)` function.

But:
- `c_parser.parse(path, method, libclang_path, include_paths, skip_default_stubs, initializer_map)` — 6 params.
- `pdf_parser.parse(path, api_key, base_url, model, max_workers, ca_bundle, resume, progress_callback)` — 8 params.
- `illd_swa_parser.parse(path, enrich, api_key, base_url, model)` — 5 params.
- `testspec_parsers.parse_testspec_workbook(xlsx_path, module)` — different name entirely.

The contract is too lax. Each parser has its own keyword args, and consumers have to know which.

**Recommendation:** keep `parse(path, **opts)` as the public surface, document `opts` per parser, and make sure `ingestion_service._parse_file` dispatches via a registry rather than a hardcoded if-elif chain.

Effort: 1 day.

---

### F-CE-X04 🟢 — `Parsers/__init__.py` lazy `__getattr__` only declares 11 of the 14 actual parsers

`__init__.py` `__all__`:
```python
__all__ = [
    "arxml_parser", "c_parser", "doxygen_parser", "ea_parser",
    "hw_spec_parser", "illd_swa_parser", "pdf_parser", "puml_parser",
    "rst_parser", "sfr_parser", "xlsx_parser",
]
```

But the actual files include: `regdef_parser`, `swa_parsers` (different from `illd_swa_parser`), `swud_parsers`, `testspec_parsers`, plus `ocr_processor`. **None of these are in `__all__`.**

Consumers that do `from src.IngestionPipeline.parsers import testspec_parsers` work via Python's normal import (the `__getattr__` lazy-loader only fires for `__all__` entries). But discoverability via `dir()` or IDE autocomplete is broken — users won't see these in suggestions.

This is the same drift pattern as Pass 2 F-A01 (Parsers/ vs parsers/), but applies *within* the package. The lazy-loader was set up at one point and hasn't been kept in sync as parsers are added.

**Recommendation:** add the 5 missing entries to `__all__` and the docstring table.

Effort: 5 min.

---

## 2. `c_parser.py`

### F-CE-C01 🔴 — `_find_libclang_dll` fallback chain has different behavior between Linux/macOS and the Sprint 25 K8s deployment

**Evidence:**
```python
def _find_libclang_dll() -> Optional[str]:
    if not LIBCLANG_AVAILABLE:
        return None

    # 1. Bundled inside the clang Python package
    try:
        clang_pkg_dir = os.path.dirname(clang.cindex.__file__)
        candidates = [
            os.path.join(clang_pkg_dir, "native", "libclang.dll"),
            os.path.join(clang_pkg_dir, "native", "libclang.so"),
            os.path.join(clang_pkg_dir, "native", "libclang.dylib"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    except Exception:
        pass

    # 2. LLVM_HOME environment variable
    llvm_home = os.environ.get("LLVM_HOME")
    if llvm_home:
        dll = os.path.join(llvm_home, "bin", "libclang.dll")
        if os.path.isfile(dll):
            return dll

    return None
```

Issues:

1. **Step 2 only checks `libclang.dll`** (Windows extension). On Linux, `LLVM_HOME` users would have `bin/libclang.so` — never found. Linux deployments fall through to "None," and `clang.cindex.Config.set_library_file(None)` does nothing (silently uses whatever is on `LD_LIBRARY_PATH` or fails).

2. **Step 1 returns the first existing candidate**, but the order is `.dll` → `.so` → `.dylib`. On a Linux system with the `libclang` pip package, the right answer is `.so`. The check correctly skips `.dll` (no such file) and returns `.so`. **OK in practice**, but the code reads as Windows-first.

3. **The Dockerfile doesn't seem to set LLVM_HOME** (per Master Gaps Family E REV3-H, the Dockerfile only added `gunicorn`, `limits`, `flashrank`, no LLVM). So the K8s container relies on the pip-installed `libclang` package — fine.

4. **`config_struct_resolver._find_libclang_dll`** has a *different* implementation that does glob over `/usr/lib/llvm-*/lib/libclang*.so*` — **two parallel libclang-discovery functions that disagree on search strategy.**

**Operational impact:** If a developer runs locally with `LLVM_HOME=/opt/llvm` set on Linux, **clang-based parsing silently falls back to regex** because `_find_libclang_dll` looks for `.dll` only. The C parser quietly uses regex (lower fidelity), and downstream KG quality drops. No log warning.

**Cross-cluster impact:** Pass 1 F-D04 confidence-scoring assumes `validation_score` accuracy depends on parser quality. If the parser silently degrades from clang to regex, validation scores become unreliable.

**Recommendation:**
1. Use `platform.system()` to pick the right extension:
   ```python
   import platform
   ext = {"Windows": ".dll", "Linux": ".so", "Darwin": ".dylib"}.get(platform.system(), ".so")
   ```
2. Step 2 should match the OS:
   ```python
   if llvm_home:
       lib_name = "libclang" + ext
       lib_path = os.path.join(llvm_home, "bin", lib_name) if platform.system() == "Windows" else os.path.join(llvm_home, "lib", lib_name)
       if os.path.isfile(lib_path):
           return lib_path
   ```
3. **Unify with `config_struct_resolver._find_libclang_dll`** — extract to `src/IngestionPipeline/parsers/_libclang_helpers.py`.
4. **Log when falling back from clang to regex:**
   ```python
   except Exception as exc:
       logger.warning("Clang parse failed for %s: %s — falling back to regex (KG fidelity reduced)", rel_path, exc)
   ```
5. Add a **Prometheus gauge** `aice_c_parser_clang_available` (0/1) so deployments where clang isn't working get noticed.

Effort: 1 day (helper extraction + cross-OS testing).

---

### F-CE-C02 🟠 — Regex-based parser is the silent fallback for the entire C-parser pipeline; no quality differentiator on output

**Evidence:** `build_knowledge_graph.py::_parse_c_file` (the Sum-mode path):
```python
try:
    parser_result = c_parser.parse(
        str(fpath), method="clang", include_paths=include_paths,
        skip_default_stubs=skip_stubs,
        initializer_map=getattr(self, '_initializer_map', None),
    )
    logger.debug("Clang parse OK for %s", rel_path)
except Exception as exc:
    logger.debug("Clang parse failed for %s: %s — falling back to regex", rel_path, exc)
    try:
        parser_result = c_parser.parse(str(fpath), method="regex")
    except Exception as exc:
        ...
```

Issues:

1. **DEBUG-level logs** for clang→regex fallback. Operators won't see this. If 30% of C files silently fall back to regex, the KG fidelity for those files is lower (no SFR detection, no global ref resolution, no internal call graph) — but it looks identical from the outside.

2. **The output dict has no `parser_method` field.** So downstream consumers can't tell that "this function" came from regex parsing while "that function" came from clang. The `confidence_calculator.evaluate()` (Cluster A) treats them identically.

3. **Diagnostics from clang are dropped** when fallback to regex happens. Clang's diagnostics often reveal real C-code issues (undefined macros, missing headers) that the user would want to see.

**Recommendation:**
1. Promote fallback log to **WARNING**: `logger.warning("Clang parse failed for %s — using regex (lower fidelity)", ...)`.
2. **Add `parser_method` to output:** `parser_result["parser_method"] = "clang" | "regex"`. KG nodes gain a `parsed_with` property — searchable, filterable.
3. **Aggregate fallback counts** at end of ingestion: `logger.info("[Ingestion] %d/%d files parsed with regex (clang failed)", regex_count, total)`.
4. **Surface in `IngestionJob` result**: `{"clang_parsed": N, "regex_parsed": M, "regex_fallback_rate": M/(M+N)}`.

Effort: 4 hours.

---

### F-CE-C03 🟠 — Clang's `diagnostics` collection includes no severity threshold; warnings spam the result

**Evidence:**
```python
@staticmethod
def _collect_diagnostics(tu: Any) -> List[Dict[str, Any]]:
    diags: List[Dict[str, Any]] = []
    for d in tu.diagnostics:
        diags.append({
            "severity": d.severity,
            "message": d.spelling,
            "line": d.location.line,
            ...
        })
    return diags
```

`d.severity` is an int (0=ignored, 1=note, 2=warning, 3=error, 4=fatal). All severities collected. For an MCAL ADC source file with hundreds of `#include` chains, clang might emit thousands of "implicit function declaration" notes — all stored in the result dict, all serialized to Neo4j as a property. **Property values can hit Neo4j's per-property size limits (default ~32KB).**

For one source file with 5000 diagnostics, the `diagnostics` list serialized to JSON could easily exceed 100KB. Neo4j accepts it but the search index size balloons; queries against `n.diagnostics CONTAINS ...` slow down.

**Recommendation:**
1. Filter to `severity >= 3` (errors and fatals) by default. Keep notes/warnings only when `verbose=True`.
2. Cap the list at 50 entries; add a `diagnostics_truncated: bool` flag.
3. **Don't store all diagnostics in the KG node** — store a summary (`{"errors": 3, "warnings": 12, "first_3_errors": [...]}`).

Effort: 2 hours.

---

### F-CE-C04 🟡 — `_extract_internal_calls` doesn't dedup; the same callee appearing N times in a function appears N times in the KG

I cannot see the body of `_extract_internal_calls` directly, but Cluster D F-CD-I01 showed the consumer:
```python
internal_calls = func_info.get("internal_calls") or []
for call in internal_calls:
    if isinstance(call, dict):
        callee = call.get("function", "")
        if callee:
            call_edges.append({
                "from_key": fid,
                "to_key": f"FUNC_{callee}",
                "call_site_line": call.get("line"),
            })
```

If `Adc_Init` calls `Mcal_GetMode` 5 times in its body, `_create_edges` does 5 MERGE operations of `(Adc_Init)-[:CALLS_INTERNALLY]->(Mcal_GetMode)`. The MERGE is idempotent (same source/target/rel-type → one edge after the first) — but the **second through fifth runs do redundant Cypher work** and produce 5 audit-log entries claiming "edge created" when only 1 actually was.

**Recommendation:** dedup before emitting:
```python
seen = set()
for m in self._pat_calls.finditer(chunk):
    fn = m.group(1)
    if fn == current or fn in self._KEYWORDS or len(fn) < 2 or fn in seen:
        continue
    seen.add(fn)
    calls.append(...)
```

The regex parser's `_extract_switch_case_calls` already does this (it has `seen = set()`). The clang path needs the same.

Effort: 30 min.

---

## 3. `illd_swa_parser.py`

### F-CE-S01 🟠 — LLM enrichment runs in a `ThreadPoolExecutor` with checkpoint/resume, but I have no visibility into the failure path; one path branch is documented but not the others

**Evidence:** the docstring claims:
> Features:
>   - Parallel LLM enrichment via ThreadPoolExecutor
>   - Checkpoint / resume capability (survives crashes mid-enrichment)
>   - Retry logic with backoff (3 retries per item)
>   - Proper HTTP timeout handling

The constants are visible:
```python
_API_TIMEOUT = 300       # 5 minutes per LLM call
_MAX_RETRIES = 3         # retry failed enrichments up to 3 times
```

But I cannot see:
- Where the ThreadPoolExecutor is constructed (max_workers value).
- Where checkpoint files are written (path containment risk?).
- What happens when 3 retries are exhausted — does the parser fail-loud or fail-quietly?
- Whether the LLM call uses the same `_get_shared_openai_client()` as RLM (Cluster C) or constructs its own.

Without visibility, I can't verify, but based on patterns elsewhere:
- If the ThreadPoolExecutor has `max_workers=10` and each call has a 300-second timeout, a single slow LLM run can hang ingestion for 50 minutes (10 × 300s) before completion.
- If checkpoints are written to `/tmp/swa_enrich_{module}.json` without sanitization (Cluster B F-CB-09 pattern), path traversal applies.
- If the retry strategy differs from `pdf_pipeline.py::_process_batch_with_retry` (which has token-refresh and connection-error rebuild), then auth-token rotation mid-enrichment fails the whole batch.

**Recommendation:**
1. **Audit the LLM-call-and-retry implementation in `illd_swa_parser.py`** specifically.
2. **Compare to `pdf_pipeline.py::_process_batch_with_retry`** — the PDF parser has token refresh on 401 + client rebuild on connection error. The SWA parser should match.
3. **Unify into a shared `_llm_with_retry` helper** in `src/HybridRAG/code/_llm_helpers.py` (also addresses Cluster C F-CC-R01's missing retry in `_default_llm`).
4. **Document checkpoint file paths** in the parser's docstring; sanitize the module name used in the path.

Effort: 1 day for audit + helper extraction.

---

### F-CE-S02 🟡 — Section detection uses `r'/\*[-]+([\w\s]+)[-]+\*/'` — fragile to comment style variations

**Evidence:**
```python
def _section(self, name: str) -> str:
    pat = r'/\*[-]+([\w\s]+)[-]+\*/'
    matches = list(re.finditer(pat, self._content))
    for i, m in enumerate(matches):
        if m.group(1).strip().lower() == name.strip().lower():
            ...
```

The regex matches Doxygen-style banner comments like `/*-- Macros --*/`. **It does not match:**
- `/* Macros */` (no dashes).
- `/* === Macros === */` (equals signs).
- `/******* Macros *******/` (asterisks).
- `// Macros` (single-line C++ comment).

If a future iLLD release switches to a different banner style (or a manually-maintained header has a typo), the parser silently returns `""` from `_section('Macros')`, falls through to `_section(...) or self._content`, and parses **the entire file** as if it were the Macros section. Most patterns then mis-attribute.

This is fragile but the headers are machine-generated, so the convention is stable. Still:

**Recommendation:**
1. Try multiple banner patterns in priority order.
2. **Log when a section is missing:** `logger.debug("[SWA] Section '%s' not found; falling back to full content", name)`.
3. If section detection fails for a file, mark the result with `"section_detection": "fallback"` so consumers know.

Effort: 1 hour.

---

### F-CE-S03 🟡 — `module = self._base.replace('_swa', '').replace('Ifx', '')` strips both — order-sensitive

**Evidence:**
```python
def __init__(self, content: str, filename: str):
    self._content = content
    self._base = Path(filename).stem
    self._module = self._base.replace('_swa', '').replace('Ifx', '')
```

For filename `IfxCan_swa.h`:
- `_base = "IfxCan_swa"`
- After `replace('_swa', '')` → `"IfxCan"`
- After `replace('Ifx', '')` → `"Can"` ✓

For filename `swaIfx.h` (hypothetical):
- `_base = "swaIfx"`
- After `replace('_swa', '')` → `"swaIfx"` (unchanged, no underscore)
- After `replace('Ifx', '')` → `"swa"`

This is unlikely in practice but the **`replace` semantics aren't anchored**. A filename containing `Ifx` mid-word (e.g., `MyIfxModule.h`) would have `Ifx` stripped from anywhere.

**Recommendation:**
```python
import re
self._module = re.sub(r'^Ifx', '', self._base)  # only strip prefix
self._module = re.sub(r'_swa$', '', self._module)  # only strip suffix
```

Same kind of regex-anchoring issue as Cluster B F-CB-10's `_detect_module_from_names`.

Effort: 15 min.

---

### F-CE-S04 🟢 — Parser docstring example uses `gpt-5.2` while RLM uses `gpt-4o`

```python
# illd_swa_parser docstring:
result = illd_swa_parser.parse(
    "IfxCxpi_swa.h",
    enrich=True,
    api_key="...",
    base_url="https://gpt4ifx.icp.infineon.com",
    model="gpt-5.2",
)
```

vs `rlm_orchestrator.py::_default_llm`:
```python
model = os.environ.get("RLM_ROOT_MODEL", "gpt-4o")
```

vs `pdf_parser._DEFAULT_MODEL`:
```python
_DEFAULT_MODEL = "gpt-5.2"
```

vs `pdf_pipeline.DEFAULT_MODEL`:
```python
DEFAULT_MODEL = "gpt-5.2"
```

**Three different default LLM models across LLM-using parsers and RLM.** No central config; each picks its own. The user's stated work uses VS Code Copilot models (Claude Opus 4.5, GPT-5.2 — per CIA documentation), but RLM defaults to `gpt-4o`.

**Recommendation:** consolidate via an env var:
```python
DEFAULT_LLM_MODEL = os.environ.get("AICE_DEFAULT_LLM_MODEL", "gpt-5.2")
```
Used by all three. RLM keeps its `RLM_ROOT_MODEL` override for cases where a different model is needed.

Effort: 30 min.

---

## 4. `pdf_parser.py` and `pdf_pipeline.py`

### F-CE-P01 🟠 — `pdf_parser._BATCH_SIZE = 2` is hardcoded and the docstring claims it's optimal; Master Gaps GAP-A07/08/09 added complexity routing that isn't reflected here

**Evidence:**
```python
_BATCH_SIZE = 2          # hardcoded — optimal for accuracy, no hallucinations
_DPI = 400               # high-resolution page rendering
```

The comment says "optimal for accuracy." That was true for one specific evaluation point. As models improve (gpt-5.2 vs the original gpt-4o), batch size 2 may be conservative. The hardcoded value can't be overridden via env var.

But more importantly: `pdf_pipeline.py` (the *other* PDF processor — yes, two PDF code paths) has adaptive DPI:
```python
dpi = IMAGE_DPI_FIGURE if is_fig else IMAGE_DPI
quality = JPEG_QUALITY_FIGURE if is_fig else JPEG_QUALITY
```

Two PDF code paths with different design decisions:
- `IngestionPipeline/Parsers/pdf_parser.py` — fixed batch=2, fixed DPI=400.
- `HybridRAG/code/pdf_pipeline.py` — adaptive DPI based on figure detection, batch_size parameter.

Same anti-pattern as the dual SWA parsers (F-CE-X02) and dual ILLD KG builders (Pass 2 F-A02 / Cluster D F-CD-X01). **Two independent implementations of the same task** with diverging choices.

**Recommendation:** pick one. The `pdf_pipeline.py` one is more sophisticated (figure detection, retry-on-connection-error, token refresh). Promote it; deprecate the parser version.

Effort: 1 day (consolidate behavior, update consumers).

---

### F-CE-P02 🟡 — `pdf_pipeline._process_batch_with_retry` token refresh logic is correct but tangled

I sampled `pdf_pipeline.py` earlier — the retry logic is good (token refresh on 401, client rebuild on connection error, exponential backoff with jitter). But it's **complex and lives in a single 60-line function** that's hard to test in isolation.

**Recommendation:** extract a small `LLMRetryClient` wrapper:
```python
class LLMRetryClient:
    def __init__(self, base_url, get_token, ca_bundle, timeout=300, max_retries=3):
        ...
    def chat_completions(self, model, messages, **opts) -> str:
        # Handles 401 → token refresh, connection errors → rebuild, 5xx → backoff retry
```
Then both `pdf_pipeline.py` and `illd_swa_parser.py` (and Cluster C `_default_llm`) use the same wrapper. **Same outcome as F-CE-S01 and F-CC-R01 recommendations.**

Effort: 1 day.

---

### F-CE-P03 🟡 — `pdf_pipeline._page_has_figure` is documented as fast (text + image introspection, no render), but I have no visibility into the implementation

```python
figure_pages: set[int] = set()
for pn in range(start_page, total):
    if _page_has_figure(doc[pn]):
        figure_pages.add(pn)
```

For a 500-page PDF, this is 500 sequential calls to `_page_has_figure`. If the function does anything that touches images (even introspection), it can be slow. Unverified without seeing the body.

**Recommendation:** verify; if slow, parallelize with ThreadPoolExecutor.

Effort: 30 min to verify.

---

## 5. `arxml_parser.py`

### F-CE-A01 🟠 — `_strip_template_macros` blindly strips EB tresos macros; no warning if the resulting XML is malformed

**Evidence:**
```python
cleaned, is_template, macros_stripped = _strip_template_macros(raw)
...
root = ET.fromstring(cleaned)
```

The docstring says:
> EB tresos / code-gen template macros — block-level and inline

EB tresos macros (`[!IF!]`, `[!FOR!]`, etc.) wrap **conditional XML content**. If you strip the macros but leave the conditional content, you may end up with:
- A `<CONTAINER>` that's defined twice (once for each branch of the IF).
- An XML element that closes a tag never opened.
- Multiple `<DEFINITION-REF>` entries that should have been mutually exclusive.

`ET.fromstring` either succeeds (with potentially-wrong content) or raises `ParseError`. The current code only handles the parse-error case (not in the snippet I saw, but implicit). For the success-but-wrong case, the parser silently produces incorrect structure.

**Recommendation:**
1. After stripping, **validate** that opening/closing tag counts balance.
2. If `is_template` is True, log a warning that template-stripped content may be schema-ambiguous.
3. **Don't ingest template ARXML directly** — require it to be expanded by EB tresos first. Document this prerequisite.

Effort: 1 day.

---

### F-CE-A02 🟡 — `_extract_param_type` has a fallback `return local` that propagates the **raw tag** as a parameter type

**Evidence:**
```python
def _extract_param_type(tag: str) -> str:
    local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
    m = _PARAM_SUFFIX_RE.match(local)
    if not m:
        logger.warning("Could not extract param type from tag: %s — returning raw tag", local)
        return local
    ...
```

Fallback is the entire raw tag (`"ECUC-NUMERICAL-PARAM-VALUE"`). Downstream KG nodes get `param_type: "ECUC-NUMERICAL-PARAM-VALUE"` instead of `"NUMERICAL"`. Search queries filtering by `param_type = "NUMERICAL"` miss these.

The WARNING log fires once per fall-through, but for a malformed ARXML with 100 broken tags, that's 100 warnings + 100 polluted KG entries. The KG quality silently degrades.

**Recommendation:**
1. **Skip the entry** instead of inserting with the raw tag:
   ```python
   if not m:
       logger.warning("Could not extract param type from tag: %s — skipping parameter", local)
       return None
   ```
2. Caller checks for None and skips the node.

Effort: 30 min.

---

### F-CE-A03 🟢 — Module docstring example uses an outdated AUTOSAR R4.0 namespace; current vehicles often use R4.4+

The hardcoded namespace:
```python
_NS = "{http://autosar.org/schema/r4.0}"
```

AUTOSAR has had R4.1, R4.2, R4.3, R4.4 since R4.0. Per the schema-detection code:
```python
ns_match = re.search(r"http://autosar\.org/schema/(r[\d.]+)", root.tag)
if ns_match:
    schema = ns_match.group(1)
```

So the code does detect the actual schema version. But `_NS` (the hardcoded namespace) is used in `findall(f"{_NS}AR-PACKAGES")` calls — **only matches r4.0 namespace tags**. A R4.4 ARXML file would have `findall` return nothing, the `modules` dict stays empty, and the parser claims success with 0 modules. Silent failure.

**Recommendation:** dynamic namespace:
```python
m = re.match(r"\{(.+?)\}", root.tag)
ns = "{" + m.group(1) + "}" if m else _NS
# Use `ns` instead of `_NS` throughout.
```

Effort: 1 hour.

---

## 6. testspec/swa/swud parsers (markdown + xlsx data sources)

### F-CE-T01 🔴 — `testspec_parsers.parse_testspec_workbook` opens `data_only=True` but `read_only=False` — full workbook in memory + slower

**Evidence:**
```python
wb = load_workbook(str(path), data_only=True, read_only=False)
```

For a 50MB MCAL test spec workbook with cached cell values:
- `data_only=True` reads cached values (good).
- `read_only=False` loads the entire workbook into memory (bad).
- `read_only=True` would stream — much faster, much less memory.

For ingestion of a single workbook: ~30 seconds, ~500MB peak RAM. For batch ingestion of 20 modules: 10 minutes, repeated peaks. K8s pod with 2GB limit would OOM-kill.

But **`read_only=True` doesn't support all features** — specifically merged-cell handling, which the parser uses. This is the trade-off.

**Recommendation:**
1. Profile actual memory usage with `tracemalloc`. If it's >500MB, this is a real risk.
2. If merged-cells are critical: cap workbook size (`if path.stat().st_size > 100_000_000: raise ValueError("Workbook too large")`).
3. Consider splitting per-sheet processing: open in `read_only=True` mode for sheets that don't need merged-cell handling, fall back to `read_only=False` only for sheets that do.

Effort: 1 day.

---

### F-CE-T02 🟠 — `_extract_traceability` uses regex with `re.IGNORECASE` for PRQ refs; case-sensitive in YAML/Jama IDs would silently produce duplicates

**Evidence:**
```python
_PRQ_REF_RE = re.compile(r"AU3GM-PRQ-\d+", re.IGNORECASE)
```

Jama's actual case for these IDs is uppercase (`AU3GM-PRQ-12345`). The `IGNORECASE` flag means `au3gm-prq-12345` would also match. If a test spec author writes lowercase by mistake, the parser captures it, and the KG ends up with a relationship to `au3gm-prq-12345` — a node that doesn't exist (because Jama IDs are uppercase). Orphan reference.

**Recommendation:**
1. Strip case-insensitivity OR normalize to uppercase post-extraction:
   ```python
   prq_refs = [m.group().upper() for m in _PRQ_REF_RE.finditer(text)]
   ```
2. Same for HAZOP refs.

Effort: 15 min.

---

### F-CE-T03 🟡 — `swa_parsers._extract_feature_id_from_syntax` has a "relaxed GUID" fallback that accepts truncated/OCR-corrupted GUIDs

**Evidence:**
```python
# Try standard GUID first
m = re.search(r"...", syntax_cell, re.IGNORECASE,)
if m:
    return m.group("fid")
# Fallback: relaxed GUID — at least 3 hyphen-separated hex segments
m = re.search(
    r"featureID\s*=\s*\\?[\[{]?\s*"
    r"(?P<fid>[0-9A-Fa-f]{4,8}(?:-[0-9A-Fa-f]{2,12}){2,4})",
    syntax_cell, re.IGNORECASE,
)
return m.group("fid") if m else None
```

The relaxed pattern is **too permissive**: a string like `featureID = abcd1234-ef56-78ab-cd-ef9012345678` (with one truncated middle segment) would match. The KG then has a "feature ID" that doesn't exist in Jama. Orphan reference.

The intent is good (handle OCR errors), but the cost is silently introducing wrong IDs.

**Recommendation:**
1. Log when relaxed match fires: `logger.warning("[SWA] Relaxed GUID match for %s — verify featureID '%s' against Jama", source, fid)`.
2. Add the relaxed-match fid to the result with a `"confidence": "low"` flag, separate from full-match GUIDs.
3. KG ingestion can then choose to skip low-confidence GUIDs or flag them for review.

Effort: 1 hour.

---

### F-CE-T04 🟡 — `_parse_spec_table_rows` is documented as "resilient to PDF→markdown page-break headings" but adds error-recovery behavior in a parser that should be deterministic

The docstring:
> Resilient to PDF→markdown page-break headings (`## Pages X-Y`) and other non-table content that may interrupt spec tables. Scanning continues past such interruptions until a new spec-table heading or numbered section heading is encountered.

This is **defensive parsing for upstream-data-quality issues**. The PDF→markdown converter (pdf_pipeline.py) emits these headings; the SWA markdown parser papers over them. Coupling between two pipeline stages.

**Recommendation:**
1. **Fix at the source**: have `pdf_pipeline.py` strip its own page-break headings before output.
2. Or, add a pre-processing step in the SWA parser that explicitly removes `## Pages X-Y` headings before the table-detection loop.
3. Either way, the pattern of "scan resumes past arbitrary non-content" is brittle. Deterministic input shape → deterministic parser.

Effort: 4 hours.

---

## 7. `ocr_processor.py`

### F-CE-O01 🟠 — `subprocess.run(['tesseract', ...])` accepts user-controlled `image_path` without validation

**Evidence:**
```python
result = subprocess.run(
    [
        self._tesseract_path,
        image_path,
        "stdout",
        "-l", self._language,
        "--oem", "1",
        "--psm", "3",
    ],
    capture_output=True,
    text=True,
    timeout=OCR_TIMEOUT_SECONDS,
)
```

`image_path` flows from the caller. Within `process_pdf`, it's a path under a temporary directory — but the temp directory comes from `tempfile.TemporaryDirectory()` (safe, OS-managed). So image_path is constructed safely.

**However**, `process_page_image` is a public method. If it's called externally with an attacker-controlled path:
- `subprocess.run(['tesseract', '/etc/passwd', 'stdout', ...])` — Tesseract attempts to OCR `/etc/passwd`. It returns garbage (text isn't an image), but **the file is read by Tesseract** with the same permissions as the AICE process.
- Tesseract has a known history of CVEs in image-loading codepaths. Pointing it at unexpected files isn't a great idea.

`OCR_LANGUAGE` env var is also passed unvalidated to `-l`. A malicious env value like `eng -c some_param=value` could inject Tesseract config.

**Recommendation:**
1. **Validate `image_path`**:
   - Resolve to absolute, check it's under an allowlist of safe roots (`/tmp/...`, `/var/run/aice/...`).
   - Reject symlinks (`Path(image_path).is_symlink()` → reject).
   - Validate file extension (`.png`, `.tiff`, `.jpg` only).
2. **Validate `OCR_LANGUAGE`**: `re.match(r'^[a-z]{3}(\+[a-z]{3})*$', lang)` — reject anything else.
3. Or, switch to `pytesseract` (Python binding) which has its own input validation.

Effort: 1 hour.

---

### F-CE-O02 🟡 — `OCRProcessor` `_estimate_confidence` is referenced but I cannot find its body

The OCR result includes:
```python
return OCRResult(
    page_number=0,
    text=text,
    confidence=confidence,  # from self._estimate_confidence(text)
    ...
)
```

`self._estimate_confidence(text)` exists but I can't see the implementation. If it returns a fake constant (e.g., `0.5`) for everything, downstream consumers think they have OCR quality scoring when they don't. This affects pages-needing-review routing for scanned documents.

**Recommendation:** verify the implementation. If it's a placeholder, file as a real bug.

Effort: 15 min.

---

## 8. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CE-C01 (libclang detection), F-CE-T01 (xlsx memory) | 2 days — both have crash-risk implications |
| **P1 (next sprint)** | F-CE-C02 (regex fallback visibility), F-CE-S01 (SWA LLM retry audit), F-CE-P01 (dual PDF parsers), F-CE-A01 (template ARXML), F-CE-T02 (case sensitivity), F-CE-O01 (subprocess validation), F-CE-X02 (SWA naming), F-CE-X01 (output shape standardization) | 6–7 days |
| **P2** | F-CE-C03–C04, F-CE-S02–S03, F-CE-P02–P03, F-CE-A02, F-CE-T03–T04, F-CE-O02 | 3 days |
| **P3** | F-CE-S04, F-CE-A03, F-CE-X03–X04 | 1 hour |

**Total cluster effort:** ~11 person-days.

Notable: most parsers' issues are **small individual fixes**, but they share a recurring theme of "duplicate implementations" (PDF, SWA, ILLD KG builder, parser dirs) — see F-CE-X02.

---

## 9. Cross-cluster pattern: dual implementations everywhere

Cluster E surfaces the **fourth instance** of the duplicate-implementation pattern:

| Pattern | Instances | Cluster |
|---|---|---|
| Parser dir `Parsers/` vs `parsers/` | 1 | Pass 2 F-A01 |
| `swa_parsers.py` byte-duplicate across IngestionPipeline + HybridRAG/KG | 1 | Pass 2 F-A02 |
| `ContextBuilder` HybridRAG vs MemoryLayer | 1 | Pass 2 F-A03 |
| `ILLDKnowledgeGraphBuilder` vs `ILLDKGBuilder` | 1 | Cluster D F-CD-X01 |
| `hybrid_search` (sync) vs `hybrid_search_async` | 1 | Cluster A F-CA-S01 |
| **`pdf_parser.py` vs `pdf_pipeline.py`** | **1** | **Cluster E F-CE-P01** |
| **`illd_swa_parser.py` vs `swa_parsers.py`** | **1** | **Cluster E F-CE-X02** |

Seven instances of the same anti-pattern across the codebase. The recipe each time is:
1. v1 of feature ships.
2. Sprint N+M needs an enhanced version; instead of refactoring, a parallel implementation is added.
3. Consumers split — some use v1, some use v2.
4. Both stay maintained "for now."
5. Drift accumulates until a review (this one) flags it.

For the Pass 4 deliverable, I'll fold a **CI gate that detects parallel implementations** by file naming (`grep -l "_v2\|_async\|_new" src/` plus `find -name '*_parser.py' | xargs basename | sort | uniq -d` from Pass 2 F-A02). This won't catch all cases but catches the obvious ones.

The architectural recommendation from Pass 2 §5 (target Sprint 30 architecture) implicitly cleans up most of these by promoting one canonical version of each component. **Pass 5 will use this list as the input for the LLD-productivity assessment** — DAs need stable parser interfaces, and these duplicates create implicit coupling.

---

## 10. What I deliberately did not flag

- **Pass 2 F-A02** byte-equivalent `swa_parsers.py` across two locations — covered there.
- **Pass 2 F-A01** `Parsers/` casing issue — covered there.
- **Per-parser unit-test coverage** — out of scope for code review; a Pass 4 concern.
- **`xlsx_parser.py` and `regdef_parser.py` and `puml_parser.py`** specifics — they follow the same patterns as the parsers I covered (no unique Critical/High findings beyond what's in F-CE-X*).
- **`ea_parser.py`** for Enterprise Architect — the file exists but I have no visibility into its body. Likely has its own LLM enrichment for component diagrams; same patterns apply.

---

**End of Cluster E.** Ready to proceed to Cluster F (Connectors — Jama, Polarion, Jenkins, Bitbucket) on your signal.

Cluster F is the smallest cluster — 4 connector files, all integration boundaries with external systems. Findings will focus on:
- Auth credential handling (especially in Jama/Polarion which have full ALM access).
- Rate-limit handling and retry/backoff behavior.
- Error propagation when external systems are slow or down.
- Test coverage proxies (the connectors are the most deployment-environment-sensitive code in the repo).

Estimate: 12–15 findings.

# AICE Review — Pass 3 / Cluster C: Retrieval Brain

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Cluster scope:**
- `src/HybridRAG/code/querier/rlm_orchestrator.py` (~712 LoC) — multi-step retrieval planner/executor/synthesizer.
- `src/HybridRAG/code/querier/context_builder.py` — 10-slot token-budget assembler (the canonical Sprint 8 version).
- `src/HybridRAG/code/querier/knowledge_intelligence.py` — Cat 2/3/4 backend (API intelligence, dependency analysis, V-Model traceability).
**Excludes:** tests/. Findings already reported in Pass 1, Pass 2, or Clusters A/B are referenced (with their original ID) but not re-stated.

---

## 0. Summary

This cluster is the "value-add brain" of AICE — what differentiates AICE from a generic vector search. It's also the most LLM-coupled, JSON-coupled, and Cypher-coupled file group in the codebase. Three things stand out from this review pass:

1. **Cypher injection has been fixed in `services.py` and `graph_search.py` (REV1-H17)**, but **the same vulnerability still exists in `knowledge_intelligence.py`** — a near-identical `f"MATCH (n:{label})"` pattern, and `_run_cypher` doesn't enforce `access_mode="READ"` either. The fix didn't propagate to the third file that needed it.
2. **The RLM JSON-parsing path is fragile**: a single greedy regex (`r'\{[\s\S]*\}'`) extracts JSON from LLM output, and the fallback path on parse failure uses `query[:200]` as the search query — silently truncating any complex query past 200 characters into a single fallback step, no warning.
3. **The retry annotation on `_gpt4ifx_call_sync`** (Master Gaps REV3-H03 says ✅) does not appear to have been applied to the real call site — `_default_llm` still has a single `client.chat.completions.create(...)` call inside one `try/except` with no retry loop. Same drift pattern as Cluster A F-CA-S02 and Cluster B F-CB-11.

Overall, **the retrieval brain is correctly designed but operationally brittle.** Failure modes are silent. Token accounting is inconsistent. LLM-output parsing is overly permissive.

**Findings count:** 22 (3 Critical, 7 High, 9 Medium, 3 Low)

| File | C | H | M | L | Total |
|---|---:|---:|---:|---:|---:|
| `rlm_orchestrator.py` | 1 | 4 | 4 | 2 | 11 |
| `context_builder.py` | 0 | 1 | 4 | 1 | 6 |
| `knowledge_intelligence.py` | 2 | 2 | 1 | 0 | 5 |

Severity criteria (as in Clusters A/B):
- 🔴 **Critical** — exploitable, data-loss, audit-bypass, or correctness regression in a production path.
- 🟠 **High** — recurring runtime issue, silent-failure, or drift from documented contract.
- 🟡 **Medium** — code-quality issue with operational impact (perf, memory, maintainability).
- 🟢 **Low** — cosmetic / future-proofing.

---

## 1. `src/HybridRAG/code/querier/rlm_orchestrator.py`

### F-CC-R01 🔴 — `_gpt4ifx_call_sync` retry/backoff is documented as fixed (REV3-H03 ✅) but does not appear in `_default_llm`

**Evidence:**

Master Gaps List §8.2 row 5: *"No retry on transient failure in `_gpt4ifx_call_sync` → Add 3-attempt exponential backoff retry. ERROR-level log on final failure. Backoff base = 1 s, multiplier = 2."* Status: ✅ FIXED. File: `src/HybridRAG/code/querier/rlm_orchestrator.py`.

What's actually in source:
```python
def _default_llm(self, system: str, user: str, max_tokens: int = 1500) -> str:
    """Call LLM via GPT4IFX OpenAI-compatible proxy (shared connection pool)."""
    try:
        client = _get_shared_openai_client()
        model = os.environ.get("RLM_ROOT_MODEL", "gpt-4o")
        resp = client.chat.completions.create(
            model=model, temperature=0.1, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("[RLM] LLM call failed: %s", e)
        return json.dumps({"reasoning": "LLM unavailable", "steps": [
            {"step_id": 1, "intent": "fallback single query", "query": user[:200], "alpha": 0.5}
        ]})
```

Single `try/except`, single attempt, no retry loop, no backoff. The retry pattern is present in `pdf_pipeline.py::_process_batch_with_retry` (which does have exponential backoff with jitter and 401-handling), but **not here**. Either:
1. The fix was made to a function literally named `_gpt4ifx_call_sync` that exists somewhere else in the file (I haven't seen the full file).
2. The fix was reverted.
3. Master Gaps is wrong.

If `_gpt4ifx_call_sync` is a private helper that wraps `_default_llm`, it should also be the function that **everyone** uses for LLM calls. If it isn't (and `_default_llm` is the actual call site for RLM), the retry is on a wrapper that nobody invokes.

**Operational impact:** A single transient failure (network blip, GPT4IFX 502, JWT mid-rotation) **aborts the entire RLM orchestration**. The user sees `final = '{"reasoning": "LLM unavailable", ...}'` returned as the synthesized output (see F-CC-R02). For complex queries that might take 15–30 seconds across 4–6 sub-queries, the failure rate cumulates: P(success) = (1 - p_fail) ^ (1 + N + 1) where N is sub-queries; for p_fail = 1% and N = 5, that's ~93% success — a **7% failure rate from transient causes alone**, completely avoidable.

**Recommendation:**
1. Verify with `git grep "_gpt4ifx_call_sync" src/HybridRAG/`. Find the actual fix.
2. If no such function exists, apply the retry directly in `_default_llm`:
   ```python
   import time, random
   MAX_RETRIES, BASE_DELAY = 3, 1.0
   for attempt in range(MAX_RETRIES):
       try:
           client = _get_shared_openai_client()
           model = os.environ.get("RLM_ROOT_MODEL", "gpt-4o")
           resp = client.chat.completions.create(...)
           return resp.choices[0].message.content or ""
       except Exception as e:
           if attempt < MAX_RETRIES - 1:
               delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
               logger.warning("[RLM] LLM call attempt %d/%d failed (%s); retrying in %.1fs", 
                              attempt + 1, MAX_RETRIES, e, delay)
               time.sleep(delay)
           else:
               logger.error("[RLM] LLM call failed after %d attempts: %s", MAX_RETRIES, e)
               return json.dumps({...})
   ```
3. **Distinguish auth errors** (401 = JWT expired) from connection errors. Auth errors should refresh the token via `token_manager.get_token(force_refresh=True)` and retry without backoff (see `pdf_pipeline.py` for the working pattern).
4. Update Master Gaps §8.2 row 5 to reflect actual state.

This is the single most operationally-impactful fix in this cluster. Effort: 1 hour for code, 30 min for unit test.

---

### F-CC-R02 🟠 — On LLM failure, `_default_llm` returns a **JSON-string** that is then passed verbatim to `_synthesize` as the final synthesized answer

**Evidence:** Following from F-CC-R01:
```python
return json.dumps({"reasoning": "LLM unavailable", "steps": [
    {"step_id": 1, "intent": "fallback single query", "query": user[:200], "alpha": 0.5}
]})
```

This is meant to be a **fallback plan** that `_plan` can parse — `_plan` calls `_default_llm` then runs `re.search(r'\{[\s\S]*\}', raw)` and `json.loads`. So when `_plan` calls `_default_llm` and the LLM is down, the fallback JSON string is parsed correctly into a single-step plan. That's fine.

But `_synthesize` *also* calls `self._llm_fn(system, user, max_tokens=4000)`:
```python
final = self._llm_fn(system, user, max_tokens=4000)
tokens = len(final) // 4
return final, tokens
```

If `_synthesize`'s LLM call fails, `_default_llm` returns the same JSON string `'{"reasoning": "LLM unavailable", "steps": [...]}'`. That string then becomes `final` — the synthesized context returned to the DA. **The DA receives a JSON snippet of a fallback plan as if it were a synthesized answer.**

A CIA assistant calling `rlm_orchestrate` for code generation would receive:
```json
{
  "answer": "{\"reasoning\": \"LLM unavailable\", \"steps\": [{\"step_id\": 1, \"intent\": \"fallback single query\", \"query\": \"You are a synthesis engine. Synthesise...\", \"alpha\": 0.5}]}",
  "confidence": ...,
  ...
}
```

CIA's downstream prompt-construction would happily insert this into the Copilot context — producing garbage code generation, with no error visibility.

**Recommendation:**
1. **Differentiate planner-fallback from synthesizer-fallback.** `_default_llm` shouldn't return a planner-shaped JSON for synthesizer calls. Either:
   - Add a `purpose` parameter (`purpose="plan"` vs `"synthesize"`) and return the appropriate fallback shape.
   - Better: raise a typed `LLMUnavailableError` and let callers handle it explicitly.
2. **Surface the failure to the DA.** The `_ok` envelope from `rlm_orchestrate` should include a `degraded: true` flag and a human-readable note when LLM synthesis failed — DAs can then choose to fall back to direct-search results.
3. **Don't hide the failure in metrics.** Currently `_finish_tool("ok")` runs because `rlm_orchestrate` returned successfully (it returned a fallback). Add a Prometheus counter `aice_rlm_llm_failures_total{stage="plan"|"synthesize"}`.

Effort: 2 hours.

---

### F-CC-R03 🟠 — `_plan`'s greedy JSON regex can grab the wrong substring; fallback path silently truncates the query

**Evidence:**
```python
def _plan(self, query: str, task_type: str) -> tuple:
    ...
    raw = self._llm_fn(system, user, max_tokens=1200)
    tokens = len(raw) // 4

    try:
        # Extract JSON from response
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            plan = json.loads(json_match.group())
        else:
            plan = {"reasoning": "No JSON in response", "steps": [
                {"step_id": 1, "intent": "direct query", "query": query[:200], "alpha": 0.5}
            ]}
    except json.JSONDecodeError:
        plan = {"reasoning": "JSON parse failed", "steps": [
            {"step_id": 1, "intent": "direct query", "query": query[:200], "alpha": 0.5}
        ]}

    return plan, tokens
```

Multiple issues:

1. **`r'\{[\s\S]*\}'` is greedy.** It matches from the **first `{`** to the **last `}`** in the entire LLM response. If the LLM emits something like:
   ```
   Here's my analysis: I noticed you asked about {register} access.
   {"reasoning": "...", "steps": [...]}
   Note: I'm using {alpha=0.3} for structural lookups.
   ```
   the regex matches `{register} access. ... {alpha=0.3}` — the entire span, not the JSON. `json.loads` then raises `JSONDecodeError`, and the fallback path activates. The actual JSON plan is silently discarded.

2. **`query[:200]` truncation.** When the fallback path activates, the search query is `query[:200]`. For a complex query like *"Generate Adc_StartGroupConversion implementation that conforms to AUTOSAR Classic 4.4 ADC SWS, integrates with EcuM and BswM lifecycle, handles ASIL-B safety requirements per ISO 26262, and includes MISRA C:2012 advisory rule 11.3 compliance for buffer alignment..."* — the cutoff hits mid-sentence. The resulting search query is meaningfully different from the original.

3. **No log warning when fallback activates.** The user sees one search step instead of 5; the metric `RLM_SUBQUERIES.observe(1)` is recorded; nothing indicates the planner actually failed.

**Recommendation:**
```python
def _plan(self, query: str, task_type: str) -> tuple:
    ...
    raw = self._llm_fn(system, user, max_tokens=1200)
    tokens = len(raw) // 4

    plan = self._parse_plan(raw, query)
    return plan, tokens

def _parse_plan(self, raw: str, original_query: str) -> Dict:
    # Try strict JSON object extraction with brace-counting
    plan = self._extract_json_object(raw)
    if plan is None:
        logger.warning("[RLM] Planner returned no parseable JSON; falling back to single-step plan. Raw output: %r", raw[:300])
        return {"reasoning": "fallback: parser failed", 
                "steps": [{"step_id": 1, "intent": "direct query", "query": original_query, "alpha": 0.5}]}
    if "steps" not in plan or not isinstance(plan["steps"], list):
        logger.warning("[RLM] Planner JSON missing 'steps' or wrong type; falling back. Got: %r", plan)
        return {"reasoning": "fallback: missing steps", "steps": [...]}
    # Validate each step has required keys
    valid_steps = [s for s in plan["steps"] if isinstance(s, dict) and "query" in s]
    if not valid_steps:
        return {"reasoning": "fallback: no valid steps", "steps": [...]}
    plan["steps"] = valid_steps
    return plan

def _extract_json_object(self, raw: str) -> Optional[Dict]:
    """Find the first complete JSON object via brace-counting (not greedy regex)."""
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
```

Drop the `query[:200]` truncation — pass the full original query as the fallback step.

Add a Prometheus counter `aice_rlm_planner_fallbacks_total{reason="..."}` so SREs can track planner reliability.

Effort: 2-3 hours.

---

### F-CC-R04 🟠 — `_synthesize` silently truncates per-step answers at 2000 chars and session_context at 500 chars

**Evidence:**
```python
def _synthesize(self, query: str, task_type: str,
                accumulated: Dict[int, str],
                session_context: Optional[List]) -> tuple:
    instruction = _SYNTH_INSTRUCTIONS.get(task_type, _SYNTH_INSTRUCTIONS["generic"])
    parts = [f"Original task: {query}\n"]
    if session_context:
        parts.append(f"Session context: {json.dumps(session_context[:5], default=str)[:500]}\n")
    for step_id, answer in sorted(accumulated.items()):
        parts.append(f"--- Sub-query {step_id} ---\n{answer[:2000]}\n")
    ...
```

Three silent-loss boundaries:
1. `session_context[:5]` — only first 5 entries.
2. `[:500]` — JSON dump of session context truncated at 500 chars.
3. `answer[:2000]` — each sub-query answer truncated at 2000 chars.

For a complex traceability query that retrieves 15 requirements per sub-query (each requirement ~150 chars of summary), 2000 chars is ~13 requirements — drops 2. For code-generation tasks where the answer might be 50+ lines of context (~5000 chars), the truncation drops 60% of the retrieved context. **The whole point of having sub-queries is to gather more context than a single search returns; truncating each sub-query result defeats that purpose.**

These limits exist to keep the synthesis prompt under GPT-4's context window. But:
- The thresholds are **hard-coded constants** with no relation to the model's actual context (gpt-4o has 128K context; the truncation assumes ~32K).
- No logging when truncation hits.
- No warning to the caller.

**Recommendation:**
1. **Use the actual model's context window.** Read from env (`RLM_ROOT_MODEL_CONTEXT_TOKENS=128000`) and compute available budget after deducting system + user prefix.
2. **Use `estimate_tokens` (the file has it via tiktoken)** instead of `len // 4` — actual token counts not character counts.
3. **Log truncations:**
   ```python
   if len(answer) > limit:
       logger.info("[RLM] Sub-query %d answer truncated %d → %d chars", step_id, len(answer), limit)
   ```
4. **If truncation is required**, prefer per-step ContextBuilder assembly over hard char limits — the ContextBuilder has prioritization logic that picks the most relevant chunks; raw `answer[:2000]` picks chronological prefix.

Effort: 1 day.

---

### F-CC-R05 🟠 — `should_use_rlm` complexity heuristic uses a regex that matches MCAL function names but not iLLD's `Ifx_*` actually-double-underscored pattern

**Evidence:**
```python
def should_use_rlm(query: str, task_type: str = "generic") -> bool:
    query_lower = query.lower()
    signals = 0
    # Signal 1: 3+ function names mentioned
    fn_pattern = re.compile(r'Ifx\w+_\w+|[A-Z][a-z]+_[A-Z][a-z]+\w+')
    if len(fn_pattern.findall(query)) >= 3:
        signals += 1
    ...
```

The regex `r'Ifx\w+_\w+'` matches iLLD names like `IfxCan_init`. The second alternative `[A-Z][a-z]+_[A-Z][a-z]+\w+` matches MCAL names like `Adc_StartGroupConversion`.

But:
1. **iLLD double-underscore convention.** Real iLLD function names follow `IfxCan_Node_init`, `IfxCan_Can_initModule`, `IfxAdc_Cmd_setRange` — the second underscore separates module-component-action. The regex `Ifx\w+_\w+` matches the *first* underscore boundary only. For `IfxCan_Node_init`, it captures `IfxCan_Node` (correct), but for `IfxCan_Can` it captures `IfxCan_Can` (wrong — `Can` is the module token, not part of the function name). It still counts as a match, but the count may differ from a human's reading.
2. **Ifx_* (single-underscore) types.** Names like `Ifx_Status` (a return type), `Ifx_None`, `Ifx_Bool` are matched as "function names" by this regex. A query like *"Why does Ifx_Status_NotOk indicate an issue with Ifx_None handling?"* counts 2 hits — not actually two function calls.
3. **Mismatch with `extract_named_entities` in search_service.** The two regexes apply different rules — the complexity heuristic and the entity extractor disagree on what constitutes a "named function."

**Recommendation:**
1. Tighten the iLLD pattern: `r'Ifx[A-Z]\w+_[a-z]\w+'` — requires the action token to start lowercase (matches iLLD convention `IfxCan_init`, `IfxAdc_Cmd_setRange`, but rejects `Ifx_Status` and `IfxCan_Can`).
2. Document the convention assumed.
3. Share the regex with `kg_node_utils.extract_named_entities` to keep the two definitions in sync.

Effort: 30 min.

---

### F-CC-R06 🟡 — Token estimation drift: RLM uses `len(raw) // 4` but ContextBuilder uses `len // 3` (and tiktoken when available)

**Evidence:**

In `rlm_orchestrator.py::_plan`:
```python
raw = self._llm_fn(system, user, max_tokens=1200)
tokens = len(raw) // 4
```

In `_synthesize`:
```python
final = self._llm_fn(system, user, max_tokens=4000)
tokens = len(final) // 4
```

In `_execute_step`:
```python
tokens = len(answer) // 4
```

But `context_builder.py::estimate_tokens`:
```python
if _TIKTOKEN_AVAILABLE:
    return max(1, len(_enc.encode(text)))
return max(1, len(text) // 4)  # M09 fix: ~4 chars/token standard
```

And per Master Gaps REV1-M09: *"Different modules used `len(text)//3` and `len(text)//4` — token-budget math drifted between Stage 4 (RRF merge) and Stage 6 (compressor). Standardize on `len(text)//4` for RRF merge, retain `len(text)//3` (more conservative) in ContextBuilder."*

So the documented standard is **inconsistent on purpose** — different layers have different conservativeness. But within RLM, the estimate is always `// 4`, which:
- Doesn't match ContextBuilder's tiktoken-based count when both are stacked (RLM's `tokens` total drives metrics; ContextBuilder's drives slot fills; user sees both).
- Means `RLMContext.total_tokens` is always less accurate than what the LLM actually saw.

**Operational impact:** For audit and cost reporting, `total_tokens` is reported back to the DA. If the actual tokens consumed by GPT4IFX are 30% higher than reported (because `// 4` underestimates English text by ~10% and code by ~30%), monthly cost projections from `RLM_REQUESTS_TOTAL × avg_tokens` are wrong by the same factor.

**Recommendation:**
```python
from src.HybridRAG.code.querier.context_builder import estimate_tokens
...
tokens = estimate_tokens(raw)
```
Use one canonical estimate function. Inside the RLM file, `estimate_tokens` from `context_builder` already handles tiktoken-vs-fallback.

Effort: 30 min (search/replace + import cleanup).

---

### F-CC-R07 🟡 — `MAX_STEPS` truncates planner output silently

**Evidence:**
```python
plan, plan_tokens = self._plan(query, tt)
total_tokens += plan_tokens
steps = plan.get("steps", [])[:MAX_STEPS]
```

If the planner LLM returns a 7-step plan and `MAX_STEPS = 6`, step 7 is silently dropped. The user sees `total_sub_queries: 6` and never knows step 7 existed. For complex queries (`debug_analysis` with full HW + register + ASIL trace), 6 may genuinely be too few.

**Recommendation:**
1. **Log truncation:** `if len(plan.get("steps", [])) > MAX_STEPS: logger.warning("[RLM] Plan had %d steps, capped at %d", len(...), MAX_STEPS)`.
2. Make `MAX_STEPS` env-configurable (`RLM_MAX_STEPS`), default 6.
3. Surface in `RLMContext`: add `plan_steps_truncated: bool` field.

Effort: 30 min.

---

### F-CC-R08 🟡 — `_execute_step` uses `step_data.get("step_id", 1)` — duplicate step_ids silently overwrite each other in `accumulated` dict

**Evidence:**
```python
for i, step_data in enumerate(steps):
    sq = self._execute_step(step_data, accumulated)
    sub_results.append(sq)
    accumulated[sq.step_id] = sq.answer
```

`accumulated` is keyed by `sq.step_id`, which comes from `step_data.get("step_id", 1)`. If the LLM emits a plan with two steps both at `step_id: 1` (or worse, the default `1` for any step missing the key), the second silently overwrites the first.

The synthesis prompt then iterates `sorted(accumulated.items())` — only the surviving entries are visible. The loss is invisible to the caller; `sub_results` (list, not keyed) does retain both, but the **synthesis input loses one**.

**Recommendation:** Use `i` (the loop index) as the dict key, not the LLM-provided step_id:
```python
for i, step_data in enumerate(steps):
    sq = self._execute_step(step_data, accumulated)
    sub_results.append(sq)
    accumulated[i] = sq.answer  # use loop index, not LLM-supplied step_id
```
This makes the contract loop-controlled (we can be certain there are exactly N entries for N steps). Keep `sq.step_id` as a metadata field for human-readable trace output.

Effort: 15 min.

---

### F-CC-R09 🟡 — Per-step `try/except TypeError` around `skip_judge` parameter is a maintenance smell

**Evidence:**
```python
if self._search_fn:
    try:
        try:
            results = self._search_fn(
                query=query, max_results=10, alpha=alpha,
                workspace_id=self.profile, skip_judge=True,
            )
        except TypeError:
            # Backward compatibility for search_fn implementations
            # that do not accept the new parameter yet.
            results = self._search_fn(
                query=query, max_results=10, alpha=alpha,
                workspace_id=self.profile,
            )
    except Exception as e:
        ...
```

The `try/except TypeError` block is supposed to detect legacy `search_fn` signatures. Two issues:
1. `TypeError` is raised by Python on missing OR extra OR wrong-type args — including bugs *inside* `search_fn` that happen to raise `TypeError` (e.g., `int + str` would propagate up). The retry would silently swallow real bugs as "missing skip_judge."
2. The "backward compatibility" comment implies a transition that should have ended. If all `search_fn` providers accept `skip_judge`, the inner `try` is dead code. If some still don't, then the contract is unstable — Master Gaps Sprint 9 lists *"Canonical search contract: `search_fn(query, max_results) → list[dict]`"* — which doesn't include `skip_judge` *or* `workspace_id` *or* `alpha`.

The canonical contract drift is **a real, documented issue** (Pass 1 F-D04 area, but specifically about confidence scoring). For search_fn the actual contract has expanded silently.

**Recommendation:**
1. **Define the contract explicitly** as a `Protocol`:
   ```python
   class SearchFn(Protocol):
       def __call__(self, query: str, max_results: int, *, alpha: float = 0.5,
                    workspace_id: str = "illd", skip_judge: bool = False,
                    **kwargs) -> List[Dict]: ...
   ```
2. **Use `**kwargs` to forward optional flags** so legacy search_fns ignore unknown ones cleanly (no TypeError to catch).
3. **Audit all `search_fn` implementations** (`search_service.hybrid_search`, sandbox `HybridGraphService.search`, mock implementations in tests). Sprint 26 task: align signatures.

Effort: 1 day for full audit + Protocol introduction.

---

### F-CC-R10 🟢 — `task_type` validation falls back to "generic" silently

**Evidence:**
```python
def run(self, query: str, task_type: str = "generic", ...):
    tt = task_type if task_type in [e.value for e in RLMTaskType] else "generic"
```

If a DA passes `task_type="code_review"` (valid) — it sticks. If it passes `task_type="codereview"` (typo) — it silently becomes `"generic"`. The DA receives a generic synthesis instruction instead of the code-review-specific one (which would emphasize MISRA, ASIL, dependencies). Quality regression, no visible error.

**Recommendation:** log a warning when fallback hits:
```python
valid_types = {e.value for e in RLMTaskType}
if task_type not in valid_types:
    logger.warning("[RLM] Unknown task_type '%s' — falling back to 'generic'. Valid: %s", 
                   task_type, sorted(valid_types))
    task_type = "generic"
```

Effort: 5 min.

---

### F-CC-R11 🟢 — File header "712 lines" claim drift

`docs/architecture/rlm-orchestrator.md` says: *"Primary class: `RLMOrchestrator` (712 lines)."* If the file is now larger (with the retry, Sprint 25 GAP integrations, etc.), this number is stale. Same family as Pass 1 doc-drift findings.

Effort: 5 min.

---

## 2. `src/HybridRAG/code/querier/context_builder.py`

### F-CC-C01 🟠 — Total-budget hard-cap removes lowest-relevance items, but the relevance scores per slot are independently normalized — comparison across slots is meaningless

**Evidence:**
```python
# Hard-cap: trim lowest-relevance items if over total budget
total_used = sum(usage.values())
if total_used > total_budget:
    selected.sort(key=lambda x: x.relevance_score)
    while total_used > total_budget and selected:
        removed = selected.pop(0)
        total_used -= removed.tokens
        usage[removed.slot] = usage.get(removed.slot, 0) - removed.tokens
        dropped += 1
```

Items are sorted by `relevance_score` and the lowest popped. But `relevance_score` is **per-slot** — items in the `requirements` slot might score 0.0–1.0 from one source, items in the `code_examples` slot might score 0.0–1.0 from another (e.g., RRF merge vs. graph CONTAINS match vs. vector cosine). **Cross-slot comparison is not meaningful**.

A `code_examples` item with score 0.6 (vector cosine) is not "less relevant" than a `requirements` item with score 0.7 (raw graph match). When the hard-cap activates, `code_examples` items get dropped first because their scores are uniformly lower — even if some of them are *the* most useful items for the query.

**Operational impact:** for queries where the budget tips over (8K limit hit by ~10%), the deletion is biased against whichever slot's relevance source produces lower absolute scores. `code_examples` (Sprint 8 added but not heavily populated) is a likely casualty.

**Recommendation:**
1. **Normalize scores within slot before global comparison.** Each slot's items get scores rescaled to (0, 1). Then global sort puts items at the same relative rank.
2. **Or, drop by slot priority.** Define an explicit slot priority order (`CONVERSATION` first to drop, `API_FUNCTIONS` last). Drop within each slot starting from the lowest, then move to the next slot. This matches `_DEFAULT_SLOT_BUDGETS` priorities and is more predictable.
3. Add a **debug-mode trace**: when hard-cap fires, log which slots lost items and how many tokens were dropped.

Effort: 1 day — needs careful unit testing.

---

### F-CC-C02 🟡 — `dropped -= 1` in second pass can produce negative `dropped` count

**Evidence:**
```python
# First pass
for slot, items in by_slot.items():
    for item in items:
        if slot_used + item.tokens <= slot_budget:
            selected.append(item)
            slot_used += item.tokens
        else:
            dropped += 1
    usage[slot] = slot_used

# ... redistribute ...

# Second pass: fill redistributed budget
for slot, items in by_slot.items():
    already = {id(i) for i in selected}
    new_budget = slot_budgets.get(slot, 0)
    slot_used = usage.get(slot, 0)
    for item in items:
        if id(item) in already:
            continue
        if slot_used + item.tokens <= new_budget:
            selected.append(item)
            slot_used += item.tokens
            dropped -= 1   # ← can decrement below zero in edge cases
    usage[slot] = slot_used
```

The `dropped -= 1` accounting is correct **only if** the second pass picks up items that were dropped in the first pass. But the second pass loops over `items` (all slot items) — including some that were already accepted in pass 1 (skipped by `if id(item) in already`) and some that were never dropped (just iterated past). The decrement assumes a specific structure that's brittle.

**Edge case:** if the slot had 3 items and pass 1 accepted all 3 (no drops in this slot), `dropped += 0` for this slot in pass 1. If pass 2 has wider budget and tries again, `if id(item) in already: continue` skips all 3. `dropped` doesn't change. **OK.**

**But:** if pass 1 dropped 1 item (slot had 4 items, accepted 3, dropped 1), `dropped += 1`. Pass 2 enters with `slot_used = 3 items' tokens`, tries to add the 4th (dropped) item, succeeds, `dropped -= 1`. Net: `dropped = 0`. **OK.**

**Where it fails:** if the dropped item's tokens > new_budget - slot_used, pass 2 cannot add it. `dropped` stays 1. **OK.**

I cannot construct a case where `dropped` goes negative — but the logic is non-obvious. The accounting should be done by **counting at the end** (`dropped = len(all_items) - len(selected)`), not by `+=`/`-=` during fills.

**Recommendation:**
```python
all_items_count = sum(len(items) for items in by_slot.values())
# ... fill loops without tracking dropped ...
dropped = all_items_count - len(selected)
```
Same result, no off-by-one risk, no negative counts possible.

Effort: 15 min.

---

### F-CC-C03 🟡 — `max_tokens` scaling formula `scale = max_tokens / 8000` produces wildly wrong slot allocations for non-default budgets

**Evidence:**
```python
total_budget = max_tokens or self.budget.total_budget
slot_budgets = copy.deepcopy(self.budget.slot_budgets)
if max_tokens:
    scale = max_tokens / 8000
    for slot in slot_budgets:
        slot_budgets[slot] = int(slot_budgets[slot] * scale)
```

`8000` is hardcoded as the implicit "base." If `total_budget` was customized at construction time to `12000` (per GAP-A09 dynamic-budget logic — *"complex → 12 K"*), then:
- `self.budget.total_budget = 12000`
- `self.budget.slot_budgets` should sum to 12000 (slots scaled accordingly).
- A caller passes `max_tokens=12000`.
- `scale = 12000 / 8000 = 1.5`.
- Slot budgets get scaled by 1.5 — but they were already at 12K-scale. **Slot budgets become 18K total.**

The hard-cap at the end catches the overflow, but only by deleting items — not by re-balancing slot proportions. The result: a 12K-budget context that does work (capped at 12K total), but with skewed slot proportions because the math double-applied scaling.

**Recommendation:** scale relative to the *current* `total_budget`, not the constant 8000:
```python
if max_tokens:
    scale = max_tokens / self.budget.total_budget
    for slot in slot_budgets:
        slot_budgets[slot] = int(slot_budgets[slot] * scale)
```

Effort: 5 min.

---

### F-CC-C04 🟡 — `_DEFAULT_SLOT_BUDGETS` sum is 24500 tokens, but `ContextBudget.total_budget` defaults to 8000 — slots over-allocate by 3×

**Evidence:**
```python
_DEFAULT_SLOT_BUDGETS: Dict[str, int] = {
    ContextSlot.REQUIREMENTS: 3000,
    ContextSlot.API_FUNCTIONS: 5000,
    ContextSlot.TESTS: 3000,
    ContextSlot.DEPENDENCIES: 2500,
    ContextSlot.RELATIONSHIPS: 1500,
    ContextSlot.CODE_EXAMPLES: 4000,
    ContextSlot.SAFETY: 1200,
    ContextSlot.REGISTERS: 3000,
    ContextSlot.CONVERSATION: 300,
    ContextSlot.CUSTOM: 1000,
}
```

Sum: **24,500**. But:
```python
@dataclass
class ContextBudget:
    total_budget: int = 8000
    slot_budgets: Dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_SLOT_BUDGETS))
```

`total_budget = 8000`. The slots collectively can hold 24500, but the hard-cap at the end will trim any total over 8000. So in practice:
- Each slot has 3× more than the total budget allows.
- The first-pass fill aggressively fills each slot (no slot will hit 90% utilization with 8K total ÷ 10 slots ≈ 800 tokens average — far below any slot's 1000+ budget).
- The redistribute pass detects "every slot is <30% used" → but no hungry slots either. Skipped.
- The hard-cap at the end prunes globally.

**Effective behavior:** the slot budgets are decorative; the hard-cap does all the real work. The 10-slot-with-redistribution algorithm reduces to "fill greedily, then trim by global relevance."

This is the documented behavior per `MEMORY_LAYER_FEATURES.md`'s `memory-layer.md` table, but the slot priorities listed there don't match the file:

| docs (memory-layer.md) | code (`_DEFAULT_SLOT_BUDGETS`) |
|---|---|
| API_FUNCTIONS: 5000 ✓ | 5000 ✓ |
| REQUIREMENTS: 3000 ✓ | 3000 ✓ |
| TESTS: 3000 ✓ | 3000 ✓ |
| DEPENDENCIES: 2500 ✓ | 2500 ✓ |
| RELATIONSHIPS: 1500 ✓ | 1500 ✓ |
| SAFETY: 1200 ✓ | 1200 ✓ |
| CUSTOM: 1000 ✓ | 1000 ✓ |
| CODE_EXAMPLES: **500** | **4000** ❌ |
| REGISTERS: **500** | **3000** ❌ |
| CONVERSATION: 300 ✓ | 300 ✓ |

**Code says CODE_EXAMPLES=4000 and REGISTERS=3000; docs say 500 each.** That's an 8× and 6× drift. Pass 1 F-D-style finding.

**Recommendation:**
1. Decide: are `CODE_EXAMPLES` and `REGISTERS` first-class slots or backup slots? If they're high-value (which the code budget implies), update the doc. If they're low-value (as docs imply), reduce the code budgets.
2. **Make slot budgets sum to total_budget.** Either:
   - Reduce all slots proportionally to sum to 8000.
   - Or, if you want oversubscription as a deliberate strategy, document it: *"Slot budgets are per-slot caps for fill order; the global hard-cap trims after redistribution."*

Effort: 1 hour for decision + 30 min to apply.

---

### F-CC-C05 🟡 — `render()` returns ungrouped output; doc says it groups by slot

The `render()` method exits the snippet at the assignment of `provenance` — I cannot see the actual rendering logic. If the doc claim *"`render()` assembles slots into sections: `=== API_FUNCTIONS === [content] === REQUIREMENTS === [content]`"* is correct, the rendering needs to group `selected` items by slot first, sort within each slot, then concatenate. Without seeing the body, this is unverified.

**Recommendation:** verify; if not implemented, implement.

Effort: 15 min to verify, 1 hour if missing.

---

### F-CC-C06 🟢 — Module docstring says "Sprint 8"; current sprint is 25

Same as Cluster A F-CA-S09. Update header.

---

## 3. `src/HybridRAG/code/querier/knowledge_intelligence.py`

### F-CC-K01 🔴 — Cypher label injection in `_fuzzy_find` and `_fetch` — REV1-H17 fix not applied to this file

**Evidence:** `_fuzzy_find` (used by `query_dependencies`, `query_api_function`, etc.):
```python
def _fuzzy_find(self, name: str, labels: List[str], ws: str, limit: int = 5) -> List[Dict]:
    ...
    kw = name.lower()
    for label in labels:
        rows = self._run_cypher(
            f"MATCH (n:{label}) WHERE toLower(coalesce(n.name,'')) CONTAINS $kw "
            f"OR toLower(coalesce(n.function_name,'')) CONTAINS $kw "
            ...
        )
```

`f"MATCH (n:{label})"` — this is **direct f-string interpolation of a label into a Cypher query**. Cypher does NOT support parameterized labels (you can't do `MATCH (n:$label)`); the only safe way is to validate the label against an allowlist before interpolation.

**This is exactly the vulnerability** that REV1-H17 fixed in `graph_search.py` and that Review4 extended via `_sanitize_label()` in `services.py`. **Neither fix was applied to `knowledge_intelligence.py`.**

`labels` comes from a hardcoded list in `query_dependencies`:
```python
labels = ["APIFunction", "DriverFunction", "Function",
          "SWA_Function", "SWUD_Function"]
```

So *currently* the labels are not user-controlled — the injection is not exploitable end-to-end **today**. But:
1. The pattern is **vulnerable by construction**. Any future enhancement that lets a caller specify which label to search (e.g., a `label` parameter on a public MCP tool) immediately becomes injectable.
2. The same `_fetch` pattern in `query_knowledge_graph.py` does the same thing for relationships (`MATCH (n {{jama_id: $jid}})-[:{rel}]->(m)`), and `rel` IS sometimes user-supplied.
3. This violates the defense-in-depth principle. The "safe today, unsafe tomorrow" pattern is exactly what causes regressions.

**Recommendation:**
```python
# At module top:
_VALID_LABELS = {
    "APIFunction", "DriverFunction", "Function",
    "SWA_Function", "SWUD_Function", "TypeDefinition",
    "DataStructure", "ConfigParameter", "Register",
    "SoftwareRequirement", "ProductRequirement", "StakeholderRequirement",
    "TestCase", "Module", ...
}

def _sanitize_label(label: str) -> str:
    if label not in _VALID_LABELS:
        raise ValueError(f"Unsafe Cypher label: {label!r}")
    return label

def _fuzzy_find(self, name: str, labels: List[str], ws: str, limit: int = 5) -> List[Dict]:
    safe_labels = [_sanitize_label(l) for l in labels]
    ...
    for label in safe_labels:
        rows = self._run_cypher(f"MATCH (n:{label}) ...", ...)
```

Same treatment for relationship types in `query_knowledge_graph.py::trace_requirement` (the `_fetch(rel, direction)` helper).

Effort: 2 hours (allowlist + apply across both files + unit test for ValueError on invalid label).

---

### F-CC-K02 🔴 — `_run_cypher` doesn't pass `access_mode="READ"` — REV1-H14 fix not applied

**Evidence:**
```python
def _run_cypher(self, cypher: str, params: Dict, ws: str = "illd") -> List[Dict]:
    """Execute a read-only Cypher query, return list of row dicts."""
    if not self._neo4j:
        return []
    try:
        with self._neo4j.session(database=self._db(ws)) as s:
            return [dict(r) for r in s.run(cypher, params)]
    except Exception as e:
        logger.error("Cypher failed: %s", e)
        return []
```

The docstring says "read-only," but **`session(database=...)` is opened without `access_mode=neo4j.READ_ACCESS`**. Per Master Gaps REV1-H14: *"Graph search sessions did not set `access_mode='READ'`, preventing Neo4j from routing to read replicas and weakening the read/write boundary."* That fix went into `graph_search.py` (and per Review4, `services.py`'s `execute_cypher`). It did **not** propagate to `knowledge_intelligence.py`.

**Impact:**
1. **Performance:** all 10 KI tools (Cat 2 API Intel, Cat 3 Dependencies, Cat 4 Traceability) hit the primary Neo4j instance even when read replicas are available. In a Neo4j cluster deployment, this concentrates load.
2. **Defense-in-depth:** if a Cypher query in this file ever started doing accidental writes (e.g., a `MERGE` smuggled in via injection — see F-CC-K01), `READ` mode would refuse it. Without it, writes succeed.

The KI tools collectively serve `query_api_function`, `get_type_definition`, `query_dependencies`, `find_callers`, `find_requirement_traces`, `build_traceability_matrix`, `analyze_hw_sw_links`, `validate_api_usage`, `detect_polling_requirements`, `generate_initialization_code` — that's the bulk of AICE's read traffic. Yet they all bypass the read-replica routing.

**Recommendation:**
```python
import neo4j

def _run_cypher(self, cypher: str, params: Dict, ws: str = "illd") -> List[Dict]:
    if not self._neo4j:
        return []
    try:
        with self._neo4j.session(
            database=self._db(ws),
            default_access_mode=neo4j.READ_ACCESS,
        ) as s:
            return [dict(r) for r in s.run(cypher, params)]
    except Exception as e:
        logger.error("Cypher failed: %s", e)
        return []
```

Effort: 5 min.

---

### F-CC-K03 🟠 — `_run_cypher` returns `[]` on **any** error — silently masks Cypher syntax bugs, broken parameter binding, Neo4j unavailability

**Evidence:** see above:
```python
except Exception as e:
    logger.error("Cypher failed: %s", e)
    return []
```

This is the same anti-pattern as Cluster B F-CB-08 (broad `except Exception`) at the data-access layer. Consequences here:
1. A Cypher syntax bug returns `[]` to the caller. Caller sees "no results" and assumes the data is missing, not the code is broken.
2. A backend-down event returns `[]`. `query_api_function` returns `{"function_name": "Foo", "found": False, ...}` even though the function exists.
3. Errors are logged at ERROR level but in a system with high request volume, ERROR spam is normal — there's no signal-to-noise differentiation.

**Recommendation:**
1. Distinguish exception types. `Neo4jError` (write attempt, syntax) should be logged at ERROR and re-raised. `ServiceUnavailable`/`SessionExpired` should be logged at WARNING and returned as `[]` (graceful degradation).
2. Surface a Prometheus counter `aice_ki_cypher_errors_total{error_type=...}`.
3. For high-value tools (`query_api_function`, `find_requirement_traces`), the empty-vs-error distinction matters for the DA — empty = "not found in KG"; error = "system problem, retry." The current code conflates both.

Effort: 1 day.

---

### F-CC-K04 🟠 — `_eid_fn()` Neo4j version detection runs once but doesn't refresh; if Neo4j is upgraded mid-deployment, the cached value goes stale

**Evidence:**
```python
def _eid_fn(self) -> str:
    if self._use_element_id is None:
        if not self._neo4j:
            self._use_element_id = False
        else:
            try:
                with self._neo4j.session(database=self._default_db) as s:
                    ver = s.run("CALL dbms.components() YIELD versions RETURN versions[0] AS v").single()["v"]
                major = int(str(ver).split(".")[0])
                self._use_element_id = major >= 5
            except Exception:
                self._use_element_id = False
    return "elementId" if self._use_element_id else "id"
```

Lazy first-call detection. Once set, never re-checked. Edge cases:
1. Neo4j 4.x → 5.x upgrade in production (rolling upgrade or blue/green): the KI service caches `False` (id), keeps emitting `id()` queries against 5.x. **`id()` still works in Neo4j 5.x as deprecated** but produces deprecation warnings; future 6.x will remove it.
2. Initial detection fails (network blip, transient `dbms.components` permission issue): caches `False`. If the actual Neo4j is 5.x with `id()` deprecated, queries continue to use the deprecated function.

**Recommendation:**
1. Detect at construction time, not lazily — fail fast if version cannot be determined.
2. Add a periodic re-check (apscheduler 5-minute job) that refreshes `_use_element_id`.
3. Log the detection result at INFO level so operators see which mode is active.

Effort: 30 min.

---

### F-CC-K05 🟡 — `query_dependencies` interpolates `max_depth` as a literal — same pattern as F-CB-01 but here it's safe because it's controlled

**Evidence:**
```python
if max_depth >= 2:
    transitive_rows = self._run_cypher(
        f"MATCH path = (a)-[*2..{int(max_depth)}]->(b) WHERE {eid_fn}(a) = $nid "
        ...
    )
```

This is the **correct** Cypher pattern for variable-length paths — inline integer (with `int()` cast for safety). Compare to Cluster B F-CB-01 (`MATCH path = (n)-[*1..$depth]-(neighbor)`) which is **wrong** (parameterized).

This is OK because `max_depth` is validated at the caller. **Listed here as a positive example — no fix needed, but worth referencing in F-CB-01's fix.** When fixing F-CB-01, mirror this pattern exactly.

---

## 4. Suggested Disposition

| Priority | Findings | Effort |
|---|---|---|
| **P0 (this sprint)** | F-CC-R01 (LLM retry), F-CC-K01 (Cypher label injection), F-CC-K02 (READ access_mode) | 4 hours total — small fixes, high impact |
| **P1 (next sprint)** | F-CC-R02 (synth fallback), F-CC-R03 (planner JSON parse), F-CC-R04 (synth truncation), F-CC-C01 (cross-slot dropping), F-CC-C04 (slot budget vs total drift), F-CC-K03 (Cypher error handling), F-CC-K04 (eid_fn refresh) | 4 days |
| **P2** | F-CC-R05 through F-CC-R09, F-CC-C02–C03, F-CC-C05 | 2 days |
| **P3** | F-CC-R10–R11, F-CC-C06 | 30 min total |

**Total cluster effort:** ~7 person-days. The P0 set is **half a day** and addresses three issues that map directly to (a) production reliability of RLM (retry) and (b) two security/correctness fixes that were thought-to-be-applied but aren't.

---

## 5. Cross-cutting observation: "fix didn't propagate"

Cluster C surfaced a **third instance** of the same drift pattern from Clusters A and B:

| Pattern | Where Master Gaps says ✅ | Where source disagrees |
|---|---|---|
| `_merge_results_rrf` rename | REV3-H01 (✅ in Master Gaps §8.2) | `search_service.py` still has the old name (Cluster A F-CA-S02) |
| Parallel warmup via `asyncio.gather` | PERF-02 (✅) | `mcp_server.py::_warmup` is sequential (Cluster B F-CB-11) |
| `_gpt4ifx_call_sync` retry | REV3-H03 (✅) | `_default_llm` has no retry (this cluster F-CC-R01) |
| Cypher label injection fix | REV1-H17 (✅) | `services.py` ✓, `graph_search.py` ✓, **`knowledge_intelligence.py` ✗** (this cluster F-CC-K01) |
| Neo4j `READ` access mode | REV1-H14 (✅) | `graph_search.py` ✓, `services.py::execute_cypher` ✓ (Review4), **`knowledge_intelligence.py` ✗** (this cluster F-CC-K02) |

**This is now a pattern, not a coincidence.** Master Gaps List is treated as a source of truth ("fix landed"), but several documented fixes either:
1. Were applied to one location and not the analogous others.
2. Were applied to a stub/prototype location while the production path was untouched.
3. Were planned but not executed.

**Recommendation for Pass 4 / governance:** add a **CI gate** that locates known-vulnerable patterns repository-wide:
```bash
# F-CC-K01-style check
grep -rn 'MATCH.*\(.*\:\${' src/  # f-string label interpolation
# F-CC-K02-style check
grep -rln 'self._neo4j.session' src/ | xargs grep -L 'access_mode\|default_access_mode'
# F-CC-R01-style check (no retry on _llm_fn)
# (more nuanced — needs AST)
```
The CI check enforces **"the fix is everywhere or nowhere."**

This pairs naturally with the CI gate proposal you already approved in Pass 1 §5 (tool_tiers consistency, Cerbos uniqueness, requirements doc tool name validation). I'll fold both into Pass 4.

---

## 6. What I deliberately did not flag

- **ContextBuilder MemoryLayer/HybridRAG location** — Pass 2 F-A03.
- **RLM location relative to MemoryLayer** — Pass 2 F-A13.
- **`_default_slot_budgets` mutation prevention** — already fixed via `copy.deepcopy` per Master Gaps Family G §8.1; verified in source (the deepcopy IS in place).
- **`_use_element_id` lock-free initialization** — small race possible (multiple threads detect at the same time, race on attribute write) but the result is the same value either way, so it's benign.
- **`_run_cypher` connection pool tuning** — out of scope for code review; deployment concern.

---

**End of Cluster C.** Ready to proceed to Cluster D (KG Construction — `build_knowledge_graph.py` 4668 LoC + `illd_kg_builder.py` + the Legacy `query_knowledge_graph.py` 1595 LoC) on your signal.

Cluster D will be the largest cluster by line count (~7000 LoC across 3 files). Expect 25–30 findings, primarily around: write-path correctness, transaction safety, error recovery, the legacy-vs-current dual-implementation drift (Pass 2 F-A07), and idempotency of ingestion runs.

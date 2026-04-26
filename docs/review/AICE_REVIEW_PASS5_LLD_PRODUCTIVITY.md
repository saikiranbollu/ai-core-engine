# AICE Review — Pass 5: LLD Productivity Alignment

**Reviewer:** Claude (independent, on behalf of B. Sai Kiran)
**Repository:** `ai-core-engine` — Sprint 25 baseline
**Date:** 2026-04-26
**Scope:** Interface fitness assessment of AICE for the 21 Domain Assistants (DAs) it serves. Pass 5 is **not a defect review** — it's a "does AICE serve its customers well" review. Each DA is a customer; AICE is the supplier of tools/infra/harness.

**Per your direction:**
- All 21 DAs at equal depth (broader, shallower).
- Method: interface contract review — what AICE tools each DA uses, where the contract is weak.
- Frame: productivity metrics — what to measure, how to instrument.

**Visibility caveat (stated up front):** DA implementations live outside this repo (CIA = VS Code extension + FastAPI backend per AICE-DA-004; others presumably similar). Pass 5 is **interface-side only** — what AICE exposes for DAs to consume vs. what the DAs need based on RLM task types and the documented `AI_USAGE_POLICY.md`. Where I extrapolate from indirect evidence, it's flagged.

---

## 0. Pass 5 framing

Passes 1–4 examined what AICE *is* (defects, architecture, security). Pass 5 asks what AICE *does for its consumers*.

**The model:** AICE is a marketplace. The 21 DAs are buyers. The 62 MCP tools are products. A productive marketplace satisfies three conditions:

1. **Coverage** — every DA finds the tools it needs.
2. **Fitness** — the tools the DAs use actually fit their workflows.
3. **Measurability** — we know which DAs are getting value and which aren't.

Pass 5 walks all three.

**Output structure:**
- §1 — The 21 DAs at-a-glance (canonical inventory + tier + task type).
- §2 — Interface contract assessment: tool category × DA matrix, gap analysis.
- §3 — DA-by-DA fitness review (one paragraph each, productivity-frame).
- §4 — Productivity metrics framework: what to measure, instrumentation plan.
- §5 — Top recommendations + 3-sprint roadmap.

---

## 1. The 21 DAs at-a-glance

Synthesizing from `docs/DOCUMENTATION.md §1.3`, `docs/architecture/rlm-orchestrator.md` `DA_TASK_MAPPING`, `mcp/auth/api_keys.yaml`, and `docs/ai_governance_files/AI_USAGE_POLICY.md §4.1`:

| DA Code | Full Name | V-Model Phase | RLM Task Type(s) | api_keys.yaml | Approved Use (Policy §4.1) |
|---|---|---|---|---|---|
| **REVA** | Requirements Reviewer | Requirements | `requirement_review` | ✅ public | Identify ambiguities, gaps, testability issues |
| **PRQ** | Requirements Drafter | Requirements | `requirement_drafting` | ❌ | Draft product requirements from stakeholder inputs |
| **RMA** | Requirements Manager | Requirements | `requirement_management` | ❌ | (not in policy table) |
| **SAGA** | Architecture Analyst | Architecture | `architecture_analysis` | ✅ developer | Analyze SW architecture for design issues |
| **ATRA** | Architecture Tracer | Architecture | `architecture_traceability` | ❌ | (not in policy table) |
| **CIA** | Code Generator | Implementation | `code_generation`, `bugfix_analysis` | ✅ public | Code gen from requirements, SFR migration (F2), Bugfix analysis (F3) |
| **CTA** | Code Transformer | Implementation | `code_transformation` | ❌ | (not in policy table) |
| **ACRA** | Code Reviewer | Implementation | `code_review`, `misra_review` | ✅ public | Flag MISRA violations, AUTOSAR issues, complexity |
| **GECA** | Config Generator | Implementation | `config_generation` | ❌ | (not in policy table) |
| **PAGE** | Page Generator | Implementation | `page_generation` | ❌ | (not in policy table) |
| **GEST** | Test Generator | Testing | `test_generation` | ✅ public | Generate test specifications and code |
| **GEVT** | Test Verifier | Testing | `test_verification` | ❌ | (not in policy table) |
| **ATQA** | Test Quality Analyst | Testing | `test_quality_analysis` | ❌ | (not in policy table) |
| **SAVA** | Safety Validator | Safety | `safety_validation` | ❌ | Gather safety requirements, AoU constraints |
| **SASA** | Safety Analyst | Safety | `safety_analysis` | ❌ | (not in policy table) |
| **HZOP** | HAZOP Analyst | Safety | `hazop_analysis` | ❌ | Gather interface definitions, guide word analysis |
| **DFA** | Data Flow Analyst | Safety | `data_flow_analysis` | ❌ | (not in policy table) |
| **MIRA** | MISRA Reviewer | Quality | `misra_review` | ❌ | (not in policy table) |
| **TripleA** | Traceability Analyst | Cross-cutting | `traceability` | ✅ developer | V-Model coverage gap identification |
| **VoltAI** | Debug Analyst | Maintenance | `debug_analysis` | ❌ | Register configs, errata, pattern analysis for debug |
| **KW** | Knowledge Weaver | Infrastructure | `knowledge_ingestion` | ❌ | (not in policy table) |
| **StopTyping** | (UI helper) | — | `stop_typing` | ❌ | — |

### F-P5-D01 🔴 — 15 of 21 DAs lack API key provisioning in `api_keys.yaml`

Only 6 DAs have explicit api_keys (gest, cia, reva, acra, saga, triplea) plus 2 admin keys. The remaining 15 cannot make MCP calls today. Either:
- They reuse one of the 6 existing keys (which violates the per-DA principal_id design — audit logs would all show `cia_assistant`).
- They are not yet deployed (status not reflected in any `STATUS` table).
- The system uses a wildcard fallback I haven't seen.

**Evidence:** `api_keys.yaml` snippet shows only 8 entries total (6 DA + 2 admin). `AICE-DA-001` claims "21+ Domain Assistants" with status `IMPLEMENTED`.

**Recommendation:** for each unprovisioned DA, decide one of:
1. Provision a key (10 minutes per DA).
2. Mark as PLANNED in `AICE_SYSTEM_REQUIREMENTS.md` (correct the IMPLEMENTED status).
3. Document the shared-key arrangement explicitly (acceptable for one DA per role bundle, e.g., "all Safety DAs share `key-safety-pool-001`").

This is the single biggest deployment-readiness gap. Without it, the "21 DAs served" claim is **6 DAs served + 15 unprovisioned**.

### F-P5-D02 🟠 — Mismatch between RLM `DA_TASK_MAPPING` codes and `AI_USAGE_POLICY.md` codes

`DA_TASK_MAPPING` (rlm_orchestrator.py) uses code `SASA` for Safety Analyst; `AI_USAGE_POLICY.md` doesn't list this DA. Same for `DaFaA` (Data Flow Analyst, code DFA in docs), `HazopA` (HAZOP Analyst, code HZOP in docs), `PRQ_Drafter` (PRQ in docs).

**Evidence:** rlm code says `"SASA": ["safety_analysis"]`; docs say `Safety Analyst — code (none)`. The codes are not coherent across the codebase.

**Recommendation:** unify naming in one PR — pick the docs-side codes (cleaner) and align the RLM mapping. Effort: 30 min.

### F-P5-D03 🟢 — `StopTyping` task type has no clear DA owner or use case

`DA_TASK_MAPPING` has `"StopTyping": ["stop_typing"]`. No DA in any documentation matches. Likely a UI helper (autocomplete control? typing-indicator?) that doesn't fit the V-Model.

**Recommendation:** decide whether StopTyping is a DA at all. If yes, document it. If no, remove from `DA_TASK_MAPPING`.

---

## 2. Interface contract — tool category × DA matrix

The 14 tool categories from `docs/DOCUMENTATION.md` and what each DA *probably* uses based on its task type. Inferred where direct evidence isn't available; flagged with ⚠️.

### Legend
- ✅ — direct evidence (named in docs, AI_USAGE_POLICY, or RLM task mapping)
- 🟡 — strong inference (the DA's task type clearly needs this tool)
- ⚠️ — speculative (DA's needs are documented vaguely)
- ❌ — almost certainly not used

### The matrix

| DA | Cat 1 Search | Cat 2 API Intel | Cat 3 Deps | Cat 4 Trace | Cat 5 Ingest | Cat 6 Memory/RLM | Cat 7 Cache | Cat 8 Feedback | Cat 9 ReviewGate | Cat 10 Ontology | Cat 11 Observability | Cat 12 Viz | Cat 13 Auth | Cat 14 GAP |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **REVA** | ✅ | 🟡 | 🟡 | ✅ | ❌ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **PRQ** | ✅ | ⚠️ | ⚠️ | 🟡 | ❌ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **RMA** | 🟡 | ⚠️ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | 🟡 | 🟡 | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **SAGA** | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | 🟡 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ⚠️ |
| **ATRA** | 🟡 | 🟡 | 🟡 | ✅ | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | 🟡 | ✅ | ⚠️ |
| **CIA** | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ✅ |
| **CTA** | 🟡 | ✅ | ✅ | 🟡 | ❌ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **ACRA** | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ✅ |
| **GECA** | 🟡 | ✅ | 🟡 | 🟡 | ❌ | ✅ | 🟡 | 🟡 | 🟡 | ✅ | ❌ | ⚠️ | ✅ | ⚠️ |
| **PAGE** | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **GEST** | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ✅ |
| **GEVT** | 🟡 | 🟡 | 🟡 | ✅ | ❌ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **ATQA** | 🟡 | ⚠️ | ⚠️ | ✅ | ❌ | ✅ | 🟡 | ✅ | 🟡 | 🟡 | 🟡 | ⚠️ | ✅ | ⚠️ |
| **SAVA** | ✅ | 🟡 | 🟡 | ✅ | ❌ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **SASA** | 🟡 | 🟡 | ✅ | ✅ | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | ⚠️ | ✅ | ⚠️ |
| **HZOP** | ✅ | ✅ | ✅ | 🟡 | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | ✅ | ✅ | ⚠️ |
| **DFA** | 🟡 | ✅ | ✅ | 🟡 | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | ❌ | ✅ | ✅ | ⚠️ |
| **MIRA** | 🟡 | ✅ | ✅ | 🟡 | ❌ | ✅ | 🟡 | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ | ⚠️ |
| **TripleA** | ✅ | 🟡 | ✅ | ✅ | ❌ | ✅ | 🟡 | 🟡 | 🟡 | 🟡 | 🟡 | ✅ | ✅ | ⚠️ |
| **VoltAI** | ✅ | ✅ | ✅ | 🟡 | ❌ | ✅ | 🟡 | ✅ | 🟡 | 🟡 | ✅ | 🟡 | ✅ | ⚠️ |
| **KW** | ❌ | ❌ | ❌ | ❌ | ✅ | 🟡 | ❌ | ❌ | ❌ | ✅ | 🟡 | ❌ | ✅ | ⚠️ |

### Reading the matrix

**Universal columns** (used by ~all DAs): Cat 6 (Memory/RLM), Cat 8 (Feedback), Cat 9 (ReviewGate), Cat 13 (Auth). These are the **6-step lifecycle** — every DA needs them.

**Implementation-DA-heavy columns** (Cat 1 Search, Cat 2 API Intel, Cat 3 Deps): heavily used by CIA, ACRA, CTA, GECA, plus ad-hoc by SAGA, GEST, VoltAI. Less by Requirements/Quality DAs.

**Architecture/Trace columns** (Cat 4 Traceability, Cat 12 Visualization): heavy users SAGA, ATRA, TripleA, HZOP, DFA. The "structured" DAs.

**Outlier rows:**
- **KW** (Knowledge Weaver) — uses Cat 5 (Ingestion) which is **admin-only** today (per Pass 4a). KW *should* be the exclusive non-admin Cat 5 user but isn't provisioned.
- **VoltAI** (Debug) — uses Cat 11 (Observability). It's the only DA that needs to query system-runtime state (register snapshots, error counts, build artifacts).
- **ATQA** — also touches Cat 11 lightly for "test quality metrics" trends.

### F-P5-I01 🟠 — Cat 5 (Ingestion) is admin-only, leaving KW unable to perform its documented role

KW's task type is `knowledge_ingestion`. Ingestion tools are admin-tier. KW would need an admin-tier API key to function — but it's documented as a *DA*, not an infrastructure component. Either:
- KW is actually an infrastructure pipeline (admin-key OK, but then it shouldn't be in the DA list).
- KW is a true DA and Cat 5 needs a public/developer-accessible variant.

**Evidence:** `AICE-DA-001` says "21+ Domain Assistants"; KW is in that list. `Cat 5 — Ingestion Pipeline` tools (`ingest_file`, `ingest_module_from_repo`, etc.) are all admin-tier per `tool_tiers.py`.

**Recommendation:** decide DA-vs-pipeline for KW. If DA, expose a developer-tier `ingest_session_attachment` (for user-uploaded docs that are workspace-scoped, not global). If pipeline, remove from DA list.

### F-P5-I02 🟠 — Tool tier mismatch: SAGA and TripleA are developer-tier; the others sharing their categories are public-tier

`api_keys.yaml` puts SAGA and TripleA at `developer` for the iLLD workspace. Other Implementation/Architecture DAs (CIA, ACRA) are `public`. This means:
- SAGA can use developer-only tools like `execute_cypher`, `override_review_routing`, `get_review_analytics`, `get_failure_patterns`.
- CIA cannot.

But SAGA's task (architecture analysis) doesn't obviously need write access or analytics dashboards. Conversely, **CIA might benefit** from being able to run `execute_cypher` for ad-hoc graph queries during code generation ("show me all functions that read this register").

**Recommendation:** revisit tier assignments per DA based on task evidence. The current public-vs-developer split appears under-justified.

### F-P5-I03 🟡 — No "tool bundle per DA" abstraction exists

Each DA must call ~8-12 tools across 4-6 categories per session. The 6-step lifecycle (`session_start` → `search_*` → `build_context` → DA-LLM → `evaluate_confidence` → `submit_human_feedback` → `session_end`) is documented but not packaged.

A DA developer integrating, say, GEVT must:
1. Read `MCP_QUICKSTART.md`.
2. Hand-stitch the 6+ tool calls.
3. Implement error handling for each.
4. Implement the JSON-RPC plumbing.

`AICE-DA-004` says CIA is "the reference implementation with VS Code extension, FastAPI backend, 3 task handlers, and 5 composable skills." **No other DA has reference implementation.** Each new DA reinvents the integration.

**Recommendation:** publish a `aice-da-sdk` Python package that wraps the 6-step lifecycle:
```python
from aice_da_sdk import AICESession

with AICESession.open(api_key="key-mira-001", workspace="mcal") as sess:
    results = sess.search("MISRA Rule 11.3 violations")
    ctx = sess.build_context(query, results)
    # DA does its work...
    eval_result = sess.evaluate_confidence(response, ctx)
    sess.submit_feedback(decision="APPROVE")
```
Effort: ~5 days for the SDK. Saves each new DA ~1 sprint of integration work.

---

## 3. DA-by-DA fitness review

One paragraph each. **Productivity-frame** — what AICE does well for this DA, what's weak. Findings flagged inline.

### REVA — Requirements Reviewer
**Task:** flag ambiguity, gaps, testability issues in requirements text.
**Tools used:** Cat 1 Search (requirement entity lookup), Cat 4 Trace (existing trace links), Cat 6 RLM (multi-step decomposition for "find untestable requirements in module X"), Cat 9 ReviewGate.
**Fitness:** ✅ Good. The RLM `requirement_review` task type has a dedicated planning prompt (per `_PLAN_CONTEXT` in rlm_orchestrator.py). Cat 4's `find_requirement_traces` directly serves traceability gap detection.
**Productivity gap:** REVA outputs are textual ("requirement X is ambiguous because Y") — there's no structured output schema. Evaluators of REVA quality have to NLP-parse the response. **Recommend:** add a `requirement_review_finding` JSON schema (severity/category/location) for `evaluate_confidence` to score against.

### PRQ — Requirements Drafter
**Task:** draft product requirements from stakeholder inputs.
**Tools used:** Cat 1 Search (template/precedent lookup), Cat 6 Memory (session_store of stakeholder inputs across drafting iterations), Cat 9 ReviewGate.
**Fitness:** 🟡 Partial. PRQ is generative (vs. REVA which is analytical). The RLM `requirement_drafting` planning prompt exists; the harness around it is the same generic one.
**Productivity gap:** PRQ probably benefits more from **pattern reuse** than from search — "show me how Adc requirements are typically structured" is more useful than "search for 'ADC conversion'". The PatternStore (Cat 8) is the right primitive but **the learning loop is currently disabled** (Cluster B F-CB-17). Until that's fixed, PRQ cannot benefit from accumulated drafting patterns.

### RMA — Requirements Manager
**Task:** manage requirement lifecycles and relationships (status transitions, link maintenance).
**Tools used:** Cat 4 Traceability heavily, Cat 6 Memory for batch operations.
**Fitness:** ⚠️ Speculative. No direct evidence of RMA in `AI_USAGE_POLICY.md §4.1`.
**Productivity gap:** RMA's task is **write-heavy** (status updates, link adds/removes). AICE's tools are read-only. RMA must use the JamaConnector / PolarionConnector directly via its own backend — AICE adds no value for the actual ALM mutations. **Recommend:** decide whether RMA is in AICE's scope at all. If yes, expose ALM-write tools. If no, remove from the canonical DA list to set expectations correctly.

### SAGA — Architecture Analyst
**Task:** analyze SW architecture for design issues.
**Tools used:** Cat 1, Cat 2 API Intel, Cat 3 Deps, Cat 4 Trace, Cat 12 Visualization.
**Fitness:** ✅ Strong. SAGA has the richest tool palette of any DA. `developer` tier gives access to `execute_cypher` for arbitrary graph queries — vital for architecture exploration.
**Productivity gap:** SAGA's outputs are typically **diagrams** (component diagrams, dependency graphs). Cat 12 (`visualize_subgraph`) is one tool — limited. **Recommend:** richer visualization output formats (PlantUML export, GraphML, SVG with hyperlinks back to KG nodes).

### ATRA — Architecture Tracer
**Task:** trace architecture decisions to requirements.
**Tools used:** Cat 4 Traceability primarily.
**Fitness:** 🟡 OK but underspecified. The `architecture_traceability` task type exists in RLM but I don't see a dedicated planning prompt in `_PLAN_CONTEXT`.
**Productivity gap:** ATRA is essentially a specialized TripleA. The duplication is suspicious — likely one of these DAs absorbs the other in practice. **Recommend:** clarify ATRA vs. TripleA scope, or consolidate.

### CIA — Code Generator (the reference DA)
**Task:** generate compliant C code from requirements + HW specs. Three sub-features: F1 (greenfield), F2 (SFR migration), F3 (bugfix analysis).
**Tools used:** the broadest set — Cat 1, 2, 3, 4 for context; Cat 6 for session/RLM; Cat 7 cache; Cat 8 feedback; Cat 9 review gate; Cat 14 GAP.
**Fitness:** ✅ Strongest of any DA. CIA has the documented reference impl (AICE-DA-004), an explicit api key, RLM task mapping, AI_USAGE_POLICY entries, and presumably the most exercised pipeline.
**Productivity gap:** several critical findings from earlier passes specifically affect CIA:
- **F-CB-01** — sandbox prod-overlay broken in iLLD; CIA's "shadow detection" feature dead.
- **F-CC-R02** — RLM synthesis garbage on LLM failure; CIA receives planner-shaped JSON as code-gen context.
- **F-CC-R04** — `_synthesize` truncates per-step at 2000 chars; for code-gen contexts that easily exceed 5000 chars, ~60% of retrieved context dropped.
- **F-CB-10** — module name regex broken for iLLD; sandbox prod queries fail to match.
**These together mean CIA is the DA most penalized by Pass 3 findings.** Sprint 26's 6 P0 fixes disproportionately help CIA.

### CTA — Code Transformer
**Task:** refactor / transform existing code (e.g., AUTOSAR migrations, bulk renames).
**Tools used:** similar to CIA but with more emphasis on Cat 2/3 (the existing-code structure) and less on Cat 4 (less requirement-tracing-driven).
**Fitness:** 🟡 Moderate. The `code_transformation` task type exists. No dedicated planning prompt visible.
**Productivity gap:** code transformations are typically *batch* (transform 50 files, not 1). AICE's session model is **per-task, not per-batch**. CTA likely runs N parallel sessions, each calling its 6-step lifecycle. Per-session overhead × N is a real cost. **Recommend:** a `batch_session` mode where context is built once and applied to N similar transformations.

### ACRA — Code Reviewer
**Task:** flag MISRA violations, AUTOSAR issues, complexity hotspots.
**Tools used:** Cat 1, 2, 3 for code lookup; Cat 6 RLM; Cat 8 PatternStore for "previously approved patterns"; Cat 9 ReviewGate for routing.
**Fitness:** ✅ Strong. The `code_review` and `misra_review` task types are both mapped. The PatternStore is the right primitive for "we've approved this style before."
**Productivity gap:** **identical findings to CIA** — ACRA shares the broken learning loop (F-CB-17), the synthesis truncation (F-CC-R04). Plus: ACRA outputs a *list of findings*, but `evaluate_confidence` was designed for a single response. Multi-finding confidence scoring isn't supported. **Recommend:** add a `evaluate_confidence_multi` variant for findings-list responses.

### GECA — Config Generator
**Task:** generate AUTOSAR configuration code (`.arxml`, EB tresos macros).
**Tools used:** Cat 1, Cat 2 (config-template lookup), Cat 6, Cat 10 (Ontology — vital for AUTOSAR schema).
**Fitness:** 🟡 OK on paper, but Pass 3 Cluster E F-CE-A01 found the ARXML parser strips template macros without structural validation. **GECA's outputs likely re-introduce the macros**, but if it ingests existing ARXML for context, the input quality is degraded. **Recommend:** GECA-specific ARXML round-trip test (parse → ingest → query → re-emit; check structural fidelity).

### PAGE — Page Generator
**Task:** generate documentation pages (e.g., function reference docs, module overviews).
**Tools used:** Cat 1, 2 for content lookup; Cat 6 for session.
**Fitness:** ⚠️ Sparse evidence. PAGE is in the DA list but has minimal task-mapping or governance entries.
**Productivity gap:** documentation generation benefits most from **cross-source synthesis** (combine requirements + code + tests into a coherent page). RLM's multi-source synthesis is the right primitive but the synthesis prompts (`_SYNTH_INSTRUCTIONS`) don't include a `page_generation` variant. PAGE likely uses the `generic` synthesis. **Recommend:** add `page_generation` to `_SYNTH_INSTRUCTIONS` with a doc-style instruction ("output Markdown with H2/H3 sections").

### GEST — Test Generator
**Task:** generate test specifications and test code.
**Tools used:** Cat 1, 2, 3, 4 (requirements → test traceability), Cat 6, Cat 8, Cat 9.
**Fitness:** ✅ Strong. GEST has explicit api key, RLM task mapping, AI_USAGE_POLICY entry. The `test_generation` planning prompt exists.
**Productivity gap:** GEST's success metric is **coverage** (does the test cover the requirement?), but `evaluate_confidence` doesn't have a coverage signal. AICE has `get_coverage_report` (Cat 11) but it's a *post-hoc* analysis. **Recommend:** integrate coverage estimation into the confidence scoring (a signal: "this test exercises N% of the named requirement's clauses").

### GEVT — Test Verifier
**Task:** verify test case quality and coverage.
**Tools used:** Cat 4 (trace tests → requirements), Cat 8 (failure patterns from past test runs), Cat 11 lightly (coverage stats).
**Fitness:** 🟡 OK. The `test_verification` task is mapped but has no documented use case in `AI_USAGE_POLICY.md`.
**Productivity gap:** GEVT operates on *test artifacts*; the KG primarily indexes *production code*. Test entities exist (`TestCase`, `TS_FunctionalTestCase` from Cluster E F-CE-T01) but their richness compared to production code is lower. **Recommend:** ingest more test-side metadata (test data tables, expected results, coverage annotations) so GEVT has primary-source material.

### ATQA — Test Quality Analyst
**Task:** analyze overall test quality metrics.
**Tools used:** Cat 11 (Observability — coverage reports, distributions), Cat 4 (trace gaps).
**Fitness:** 🟡 OK. ATQA is one of the few DAs that legitimately needs Cat 11.
**Productivity gap:** Cat 11 is mostly *system observability* (Prometheus-style), not *test quality observability*. ATQA needs metrics like "% of safety requirements with tests at boundary cases" — which AICE doesn't compute. **Recommend:** add a `test_quality_metrics` tool that aggregates from existing test/requirement nodes (no new ingestion needed).

### SAVA — Safety Validator
**Task:** validate ISO 26262 safety requirements; gather AoU (Assumption of Use) constraints.
**Tools used:** Cat 1, Cat 4 (trace safety reqs to evidence), Cat 6.
**Fitness:** 🟡 OK on tools, weak on data. Safety analysis needs ISO 26262 clause references, ASIL classifications, hazard linkages — these are partly modeled in the ontology but not richly populated.
**Productivity gap:** **the KG has function/struct/register richness; safety-domain richness is shallower.** ASIL fields exist on requirement nodes but FMEA / FTA / hazard relationships are minimally represented. SAVA likely does a lot of work *outside* the KG. **Recommend:** ingest safety analysis artifacts (FMEA tables, hazard registers) as first-class KG entities.

### SASA — Safety Analyst
**Task:** perform safety analyses (FMEA, FTA, dependent failure).
**Tools used:** similar to SAVA + Cat 3 Deps (essential for failure propagation).
**Fitness:** Same as SAVA — the data layer is the bottleneck, not the tool layer.
**Productivity gap:** Same recommendation as SAVA — ingest safety artifacts.

### HZOP — HAZOP Analyst
**Task:** hazard and operability studies; gather interface definitions, guide-word analysis.
**Tools used:** Cat 1, Cat 2 (interface definitions are API functions/structs), Cat 12 (visualize interface networks).
**Fitness:** 🟡 OK on the interface-discovery side. HAZOP-specific (guide words) isn't represented in AICE.
**Productivity gap:** HAZOP guide words ("no flow," "more flow," "less flow") are deviation patterns that don't exist as KG entities. HZOP probably uses AICE for interface enumeration only, then does the hazard analysis externally. **Recommend:** if HAZOP is in scope, ingest guide-word templates per interface type.

### DFA — Data Flow Analyst
**Task:** data flow analysis for safety (information flow, data corruption paths).
**Tools used:** Cat 2, Cat 3 (deps are essentially data flow), Cat 12 (visualize).
**Fitness:** 🟡 OK. The KG's CALLS_INTERNALLY edges are the data-flow primary structure, plus parameter / return-type / register access edges.
**Productivity gap:** *taint propagation* (mark a source, trace through which functions/buffers/registers it reaches) isn't a built-in query. DFA likely runs custom Cypher via `execute_cypher` (developer tier). **Recommend:** package taint-trace as a Cat 4 tool — `trace_data_flow(start_node, sink_predicate)`.

### MIRA — MISRA Reviewer
**Task:** MISRA C:2012 compliance checking.
**Tools used:** Cat 1, Cat 2 (rule definitions), Cat 8 (PatternStore for known violations / approved patterns), Cat 9.
**Fitness:** 🟡 OK on infra; the substance lives in MISRA rule logic which is presumably embedded in MIRA's own LLM prompts.
**Productivity gap:** MISRA rule references (e.g., "Rule 11.3 violation") aren't first-class KG entities — they're text in code review notes. **Recommend:** add a `MISRARule` node type with `rule_id`, `severity`, `description`, plus relationships `:HAS_VIOLATION` from code nodes.

### TripleA — Traceability Analyst
**Task:** V-Model traceability gap identification.
**Tools used:** Cat 4 heavily, Cat 12, Cat 6 RLM with `traceability` task type.
**Fitness:** ✅ Strong. The dedicated task type, developer-tier api key, and rich Cat 4 toolset all support TripleA.
**Productivity gap:** TripleA's outputs are *gap reports* — structured findings. Same multi-finding-confidence issue as ACRA. Plus: TripleA likely needs to *write* recommendations (not just identify gaps), but AICE is read-only.

### VoltAI — Debug Analyst
**Task:** analyze register configs, errata, patterns for debug.
**Tools used:** Cat 2 (API Intel for register access patterns), Cat 11 (Observability — runtime stats), Cat 8 (failure patterns).
**Fitness:** 🟡 OK. VoltAI is the only DA that needs Cat 11 substantively. The combo of register-access KG + runtime observability + failure patterns is a unique product.
**Productivity gap:** **erratum data isn't in the KG today.** Errata are vendor-published lists (Infineon errata sheets per silicon revision) — these would need their own ingestion path. Without erratum data, VoltAI is a generic debug helper, not a TC3xx-specific one. **Recommend:** ingest errata sheets (`SiliconErratum` node type with `tc_revision`, `affected_module`, `workaround`).

### KW — Knowledge Weaver
**Task:** knowledge ingestion and graph enrichment.
**Tools used:** Cat 5 Ingestion exclusively (admin-tier).
**Fitness:** 🔴 Blocked by F-P5-I01 — KW needs admin tier but is documented as a DA.

### StopTyping
**Task:** unclear (autocomplete control?).
**Fitness:** Out of scope per F-P5-D03.

---

## 4. Productivity metrics framework

Per your direction: define *what to measure* and *how to instrument*. This section is structured as: (a) the metric, (b) what it answers, (c) the instrumentation point, (d) the storage/query path, (e) the dashboard consumer.

### 4.1 Foundation — every metric needs a `da_name` dimension

Today, the central Prometheus metric `aice_tool_requests_total{tool="...", status="..."}` does not carry a `da_name` label. **Without that label, no per-DA productivity analysis is possible.**

The principal_id is in `_current_api_key` (resolved by the auth middleware). It needs to flow into the metric label.

### F-P5-M01 🔴 — Add `da_name` (and `tier`) labels to existing tool metrics

**Instrumentation:** in `mcp_server.py::_authorize`, after resolving the principal:
```python
da_name = principal_id.replace("_assistant", "")  # e.g., "cia"
_da_name_ctx.set(da_name)
_da_tier_ctx.set(role)
```
Then in `_finish_tool`:
```python
TOOL_REQUESTS_TOTAL.labels(
    tool=name,
    status=status,
    da=_da_name_ctx.get("unknown"),
    tier=_da_tier_ctx.get("unknown"),
).inc()
```

**Cardinality consideration:** `da_name` adds 21 distinct values × `tool` (62) × `status` (2-3) × `tier` (3) = ~12K distinct label combinations. Prometheus handles this comfortably (recommended ceiling is ~100K per metric).

**Cross-cluster dependency:** Cluster B F-CB-05 flagged that `_tool_name_ctx` may not be set. Verify that finding first; otherwise `da_name` instrumentation will silently no-op alongside the existing tool metrics.

Effort: 4 hours including verification.

### 4.2 Productivity metrics catalog

Eight metrics, each with one paragraph: *what / why / how / where to view*.

#### M-01 — Tool utilization by DA: `aice_da_tool_requests_total{da, tool, tier, status}`
**What:** counter of tool calls, per DA per tool.
**Why:** which DAs use which tools? Is REVA actually using `find_requirement_traces`, or did it never get integrated? Are tools we built actually *consumed*?
**How:** F-P5-M01 above.
**Storage:** Prometheus.
**Dashboard:** Grafana panel "Top tools by DA" + "DAs using fewer than 3 tool categories" (deployment-readiness signal).

#### M-02 — Session duration distribution: `aice_da_session_duration_seconds{da, task_type}`
**What:** histogram of `session_end - session_start` per DA per task type.
**Why:** if CIA sessions average 8 minutes and GEST average 90 seconds, that's an interesting productivity signal — either GEST is faster or it's not exercising the full lifecycle.
**How:** in `mcp_server.py::session_end`, compute the duration and observe in a Histogram.
**Storage:** Prometheus.
**Dashboard:** "Session duration p50/p95 by DA" + alert on p95 > 30 minutes (suggests stuck sessions).

#### M-03 — Confidence routing distribution: `aice_da_session_outcomes_total{da, task_type, outcome}`
**What:** counter where outcome ∈ {AUTO, QUICK, FULL, REJECTED}. Increments at `evaluate_confidence`/`submit_human_feedback`.
**Why:** AUTO rate is the headline productivity metric — "what fraction of DA outputs need no human review." If CIA's AUTO rate is 5%, the value-prop is weak; if 50%, strong.
**How:** existing FeedbackSink (Cluster A) already records this; just add `da` label.
**Storage:** Prometheus + PostgreSQL `feedback_records` for long-term trend.
**Dashboard:** "AUTO rate by DA, week-over-week."

#### M-04 — Context assembly tokens: `aice_da_context_assembly_tokens{da, task_type}`
**What:** histogram of total tokens in the context returned by `build_context` / `rlm_orchestrate`.
**Why:** **proxy for "tokens saved by AICE."** If CIA pulls 6000 tokens of pre-assembled context per session, that's 6000 tokens it didn't have to discover/load itself. Multiply by sessions/day to estimate AICE's productivity contribution.
**How:** existing `RLMContext.total_tokens` and ContextBuilder's output have this; observe at session end.
**Storage:** Prometheus + PostgreSQL response_archive for joins.
**Dashboard:** "Tokens delivered per session by DA" + "Estimated tokens saved per week."

#### M-05 — Time-to-first-result: `aice_da_first_result_latency_seconds{da, task_type}`
**What:** histogram from `session_start` to first `evaluate_confidence` call.
**Why:** developer-experience metric. Fast TTFR means the DA can deliver useful output quickly.
**How:** set timestamp in `session_start`, diff at `evaluate_confidence`.
**Storage:** Prometheus.
**Dashboard:** "TTFR p50 by DA."

#### M-06 — Review-cycle reduction: `aice_da_review_iterations{da, response_id}`
**What:** count of iterations between `evaluate_confidence` and final `complete_review` (counting routing overrides as iterations).
**Why:** quality proxy. If CIA averages 1.2 review iterations and ACRA averages 3.5, ACRA's outputs need more rework.
**How:** track in PostgreSQL `review_records`. Compute as a derived metric.
**Storage:** PostgreSQL view, exported to Prometheus via a custom exporter.
**Dashboard:** "Average review iterations per DA."

#### M-07 — Pattern reuse rate: `aice_da_pattern_hits_total{da, task_type}`
**What:** counter of times a DA's session retrieved an `ApprovedPattern` from PatternStore.
**Why:** measures whether the learning loop is actually paying back. Low pattern-hit rate after weeks of usage = learning loop isn't generalizing.
**How:** instrument PatternStore's `find_similar_pattern` calls (after Cluster B F-CB-17 fix re-enables the loop).
**Storage:** Prometheus.
**Dashboard:** "Pattern reuse % by DA, by week."

#### M-08 — Cost per session: `aice_da_session_llm_tokens_total{da, task_type, llm_call_type}`
**What:** counter of LLM tokens consumed by AICE (planning + synthesis + enrichment) per DA session. `llm_call_type` ∈ {planner, synthesizer, enrichment}.
**Why:** finance / budget tracking. Per-DA token cost lets you allocate GPT4IFX spend.
**How:** instrument every `_default_llm` and `_synthesize` call. Use `estimate_tokens` (canonical helper).
**Storage:** Prometheus + PostgreSQL.
**Dashboard:** "Monthly LLM cost by DA."

### 4.3 Recommended PostgreSQL view: `da_productivity`

For long-term trend analysis (Prometheus retention is typically 14-30 days), a denormalized view in PostgreSQL:

```sql
CREATE VIEW da_productivity AS
SELECT
    a.principal_id AS da_name,
    DATE_TRUNC('day', a.timestamp) AS day,
    COUNT(*) AS total_calls,
    SUM(CASE WHEN a.response_code = 'ok' THEN 1 ELSE 0 END) AS successful_calls,
    SUM(CASE WHEN a.response_code != 'ok' THEN 1 ELSE 0 END) AS failed_calls,
    AVG(EXTRACT(EPOCH FROM (s.ended_at - s.started_at))) AS avg_session_duration_s,
    SUM(CASE WHEN f.decision = 'APPROVE' THEN 1 ELSE 0 END) AS approvals,
    SUM(CASE WHEN f.decision = 'REJECT' THEN 1 ELSE 0 END) AS rejections,
    AVG(r.confidence_score) AS avg_confidence,
    SUM(r.tokens_total) AS total_context_tokens
FROM audit_logs a
LEFT JOIN sessions s USING (session_id)
LEFT JOIN feedback_records f ON f.session_id = s.session_id
LEFT JOIN response_archive r ON r.response_id = f.response_id
GROUP BY a.principal_id, DATE_TRUNC('day', a.timestamp);
```

Queryable for: "show me the worst-performing week for each DA," "which DAs have declining AUTO rates," etc.

### 4.4 Top-level dashboard: AICE DA Productivity Overview

A single Grafana dashboard surfacing:

| Row | Panel | Metric |
|---|---|---|
| 1 | DA usage heatmap | M-01 — tools used per DA |
| 1 | Active DAs (last 24h) | M-01 distinct `da` count |
| 2 | AUTO rate by DA | M-03 |
| 2 | Session duration p50/p95 | M-02 |
| 3 | TTFR p50 by DA | M-05 |
| 3 | Review iterations | M-06 |
| 4 | Tokens delivered per session | M-04 |
| 4 | Pattern reuse rate | M-07 |
| 5 | Monthly LLM cost by DA | M-08 |
| 5 | Cost per AUTO-approved output | derived: M-08 / M-03(AUTO) |

Single dashboard. Owners check it weekly. Anomalies (e.g., AUTO rate dropping below threshold) trigger investigation.

### F-P5-M02 🟠 — `unknown` task_type emissions will accumulate without resolution

If a DA calls `search_database` outside of an active session, `task_type` is unknown. The metrics above include `task_type` in many labels — if half of CIA's calls have `task_type=unknown`, dashboard quality drops.

**Recommendation:** make `task_type` mandatory at `session_start` (it's optional today). For one-off calls outside sessions, label as `task_type=adhoc`.

---

## 5. Top recommendations + 3-sprint roadmap

### Top 5 recommendations from Pass 5

1. **Provision API keys for the 15 unprovisioned DAs (or formally reclassify them as PLANNED)** — F-P5-D01. The "21 DAs served" claim is currently 6 served + 15 unaccounted. 1 day of decisions, 30 min of yaml.

2. **Ship the silent-failure dashboard from Pass 3 §2 Pattern #4 + the per-DA productivity dashboard from §4.4 in the same sprint.** The two share the same Prometheus instrumentation foundation. Together they answer "is the system healthy?" + "are users getting value?" Two questions, one infrastructure investment.

3. **Build `aice-da-sdk` Python package** — F-P5-I03. Eliminates 1 sprint of integration work per new DA. Pays back after 3 new DA integrations.

4. **Add `da_name` label to all metrics** — F-P5-M01. Foundation for all per-DA analytics. 4 hours.

5. **Re-enable the learning loop** (Cluster B F-CB-17) — without this, M-07 (pattern reuse) is always zero. PRQ, ACRA, MIRA, GEST all benefit.

### Pass 5 sprint additions

These slot into the Pass 3 4-sprint plan (Sprint 26-29) without conflict:

**Sprint 26 (Bleeding-stops)** — already covers F-CB-17 (learning loop) and Sprint 26's top-6 also help CIA disproportionately. Pass 5 adds:
- F-P5-D01 (API key audit + provisioning) — 1 day.
- F-P5-M01 (da_name labels) — 4 hours.

**Sprint 27 (Observability)** — already adds the silent-failure dashboard. Pass 5 adds:
- M-01 through M-08 instrumentation — 3 days.
- DA productivity dashboard — 1 day.

**Sprint 28 (Consolidation)** — Pass 5 adds:
- `aice-da-sdk` package — 5 days.

**Sprint 29 (Hardening)** — Pass 5 adds:
- DA-specific findings (per-DA paragraphs in §3 above) prioritized by deployment readiness.

**Total Pass 5 effort:** ~10 person-days, distributed across the existing 4 sprints. Doesn't push the overall plan out.

### What Pass 5 explicitly does NOT recommend

- **Don't try to "complete" all 21 DAs in a sprint.** The matrix in §2 shows realistic adoption: 5-7 DAs are mature (CIA, GEST, ACRA, SAGA, REVA, TripleA, VoltAI). The rest are documented intent. Trying to harden all 21 simultaneously dilutes effort.
- **Don't add new tool categories unless a DA needs them.** AICE has 14 categories; the matrix shows most DAs use 5-7. Adding more categories without per-DA evidence compounds maintenance.
- **Don't conflate AICE productivity with DA productivity.** AICE's productivity is "tokens saved, latency reduced, AUTO rate." DA productivity is "developer hours saved, defects prevented." Pass 5 measures the AICE side; the DA side requires field studies that AICE alone can't run.

---

## 6. What Pass 5 deliberately did not assess

Honest disclosure of what's outside this assessment:

- **DA implementation quality.** AICE is the supplier; DAs are the consumers. Pass 5 doesn't assess whether CIA's prompts are well-engineered or whether GEST's test generation has good coverage — those live in their respective repos.
- **End-user developer experience.** AICE's productivity metrics are *system-side*. Whether developers actually accept AICE-generated code or just hit Tab in Copilot is a separate study (UX research / longitudinal field study).
- **Comparison to alternatives.** Cursor, Claude Code, Devin, etc. are not assessed. The question "is AICE better than X for IFX's needs?" is out of scope.
- **DA cost-effectiveness.** Whether the engineering cost of building 21 DAs is recovered via productivity gains is a business question. Pass 5 measures the inputs (tool calls, tokens, sessions); the outputs (developer time saved) require separate measurement.
- **Integration with non-AICE AI tools.** GitHub Copilot, GPT4IFX direct usage, Claude Code — Pass 5 only assesses AICE-served DAs. The broader IFX AI tooling landscape isn't evaluated.

---

## 7. Closing

Pass 5 found that AICE's interface contract for its 21 DAs is **strong on the 6-7 mature DAs and underspecified on the rest**. The two largest gaps:

1. **Operational readiness** — 15 of 21 DAs lack API keys.
2. **Productivity measurement** — no `da_name` label on metrics means no per-DA analytics exist today.

Both are cheap to fix (~5 days combined). Once fixed, AICE moves from "framework that supports 21 DAs in principle" to "framework where 21 DAs' usage is measurable, comparable, and optimizable."

The deeper-dive findings in §3 (DA-by-DA fitness) and the metric catalog in §4 are inputs to a longer roadmap. The 5 top recommendations in §5 are the actionable shortlist.

**My honest read:** AICE is a competent platform with a well-designed 6-step lifecycle, the right primitives (PatternStore, RLM, ReviewGate), and a clear governance model. Its primary weakness is **uneven DA maturity** combined with **uninstrumented productivity** — both are bookkeeping/wiring problems, not architectural ones. The Pass 3 + Pass 5 sprint plan addresses both inside ~4 weeks of focused work.

---

**End of Pass 5.**

The complete review series is now closed:

| Pass | Topic | Lines | Findings |
|---|---|---:|---:|
| 1 | Doc/Code drift | 759 | 23 |
| 2 | Architecture | 606 | 16 |
| 3A | Service Spine | 885 | 27 |
| 3B | MCP Server | 895 | 24 |
| 3C | Retrieval Brain | 919 | 22 |
| 3D | KG Construction | 957 | 26 |
| 3E | Parsers | 854 | 24 |
| 3F | Connectors | 874 | 17 |
| 3 Summary | Synthesis + cross-cutting patterns | 479 | (synthesis) |
| 4A | Security audit | 628 | (synthesis) |
| 4B | CI gates spec (GitLab) | 1520 | (drop-in artifacts) |
| 5 | LLD productivity for 21 DAs | (this doc) | ~7 |
| **Total** | | **~10,000 lines** | **~186 findings** |

Beyond this, the natural next step would be a **field study** (instrument the dashboards from §4.4, run them for 4 weeks, see what the data reveals). That's not a desk review — that's empirical work. When that data exists, Pass 6 would assess "what we learned, what to change."

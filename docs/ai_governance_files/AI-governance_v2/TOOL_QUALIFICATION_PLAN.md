# Tool Qualification Plan — AICE and Domain Assistants
## ISO 26262-8 Clause 11 "Confidence in the use of software tools"

**Document ID**: AICE-GOV-007
**Version**: 1.0.0
**Classification**: Internal — Infineon Technologies
**Owner**: Safety Manager + AI Governance Lead + Platform Team
**Last Updated**: 2026-04-18
**Applies to**: ASIL-A through ASIL-D software development in the `mcal` and `illd` workspaces

---

## 1. Purpose

This document defines the **Software Tool Qualification Plan (STQP)** for the AI Core Engine (AICE) and the Domain Assistants (DAs) it serves, as required by ISO 26262-8:2018 Clause 11 "Confidence in the use of software tools." It covers:

1. Tool classification (TI / TD / TCL) per use-case
2. Qualification method selection
3. Evidence structure (Tool Criteria Evaluation Report and qualification reports)
4. Tool validity check
5. The **TD1 vs TD2 decision** — why it matters and how to choose

---

## 2. Regulatory Context

- **ISO 26262-8:2018 Clause 11** — determines required confidence in software tools
- **ASIL target for mcal workspace: ASIL-D** (confirmed Apr 2026)
- **Relevant ASPICE processes:** SWE.1–SWE.6 + SUP.11 (SUP.8, SUP.10 integration)
- **Related documents:** AICE_SYSTEM_CARD v2.0.0; AI_USAGE_POLICY v2.0.0; VDA AI-in-QM Yellow Volume Ch. 7

**Note on ISO/PAS 8800:2024:** This standard explicitly excludes "software tools that use AI methods" from its scope. Therefore ISO/PAS 8800 is NOT a qualification target for AICE itself. The canonical instrument is ISO 26262-8 Clause 11.

---

## 3. The TD1 vs TD2 Decision — Pros, Cons, and Our Position

### 3.1 Why this decision is the single most important one in this plan

Per ISO 26262-8 §11.4.5:

| Tool Impact | Tool Error Detection | → Tool Confidence Level | Qualification required? |
|---|---|---|---|
| TI1 (no safety impact) | — | TCL1 | **No** |
| TI2 (safety impact) | TD1 (high detection) | **TCL1** | **No** |
| TI2 | TD2 (medium detection) | TCL2 | Yes — methods 1b and 1c (highly recommended for ASIL-D) |
| TI2 | TD3 (low detection) | TCL3 | Yes — method 1c or 1d |

Every code-generating DA has **TI2** (a malfunction can introduce safety errors). The decision is on TD:

- **If TD1 is defensible → TCL1 → no formal qualification required.** Huge cost savings; evidence is the pipeline itself.
- **If TD2 → TCL2 → qualification evidence bundle per DA** (methods 1b + 1c recommended, 2–5 days per DA × 6 High-risk DAs = non-trivial).
- **TD3 → TCL3** — to be avoided.

### 3.2 The TD1 argument (aggressive, cheaper)

**Claim:** Given Infineon's existing downstream pipeline for ASIL-D MCAL — **MISRA C:2012 + AUTOSAR C++ guideline check + Polyspace Bugfinder + Polyspace CodeProver (formal) + structural coverage (MC/DC for ASIL-D) + integration test on HIL + mandatory human review + independent reviewer + safety manager sign-off** — there is "high confidence that any malfunction of the AI tool will be prevented or detected" (ISO 26262-8 §11.4.5.4 TD1 criterion).

**Supporting evidence / precedent:**
- ISO 26262-8 §11.4.5.5 Example 3 (compiler): "TD1 is selected for a code generator when the generated source code is verified in accordance with ISO 26262." Our pipeline performs exactly such verification for every AI-generated line.
- MathWorks qualifies Embedded Coder using a similar argument: the generated C code is subject to downstream verification per the user's ISO 26262 process.
- GitHub's CodeQL Coding Standards classifies itself as TCL2 because its checks are not unconditionally backed up by other V&V — **that's not our situation**. Our AI outputs go through exhaustive V&V.

**Pros of TD1 position:**
- **No formal tool qualification activity required** (no method 1c validation of the LLM itself, which is infeasible for a stochastic system). Massive cost and schedule saving.
- Faithful to ISO 26262 philosophy: the qualification burden scales with what downstream verification actually catches.
- Evidence is in the CI/CD pipeline — already exists, already runs per-commit. No additional engineering.
- Aligns with the architectural principle from AI_Governance_in_Automotive_Software.md: pairing probabilistic generation with deterministic verification.

**Cons of TD1 position:**
- **Depends critically on the downstream pipeline being truly mandatory and unbypassable.** Every gate bypassed invalidates the TD1 claim for the affected artifact. Requires rigorous CI/CD discipline.
- **Does not cover all DA output types** — the pipeline above is specifically for C code. It does not cover:
  - AI-generated *requirements* (REVA, PRQ Drafter — no static analysis catches a weakly-written requirement)
  - AI-generated *safety analysis content* (SASA, HAZOPA, DaFaA — Polyspace cannot verify whether a HAZOP table is correct)
  - AI-generated *architecture* (SAGA — correctness against intent is a human judgment)
  - AI-generated *test specs* (GEST — coverage + mutation testing partially helps, but coverage doesn't prove "the right test was written")
  - AI-generated *config* (GECA — parameter constraint validation partially helps, but semantic correctness is beyond static check)
- **Auditor challenge risk:** An ISO 26262 assessor may push back on TD1 for a stochastic tool even with strong downstream V&V. The argument is defensible but controversial.
- **Reviewer workload:** TD1 claim effectively shifts the detection burden to the downstream pipeline + human review. Humans must be disciplined.

### 3.3 The TD2 argument (conservative, more expensive)

**Claim:** Because LLMs are stochastic and generate plausible-looking-but-wrong output that can occasionally pass downstream gates (especially for semantics beyond syntax), the appropriate default is TD2 — "medium confidence that malfunctions will be prevented or detected." This triggers TCL2, which requires method 1b (evaluation of tool development process) + 1c (tool validation) for ASIL-D.

**Pros of TD2 position:**
- Conservative; matches typical industry default for generative AI tools in safety contexts.
- Auditable — the qualification evidence package (TCER + validation tests + STQR) is concrete and inspectable.
- **Forces explicit validation of the AI stack per DA** — can catch integration issues the pipeline alone might miss.
- Defensible before any ISO 26262 assessor without debate.
- Gives structure to recurring re-qualification on model updates.

**Cons of TD2 position:**
- **Cost:** 2–5 engineering-days per High-risk DA × 6+ DAs = 20–30 engineering-days just for initial qualification, plus re-qualification on each major change.
- **LLM non-determinism makes method 1c validation tricky** — what does "the tool passed the test" mean when the same prompt can yield different outputs? Solutions: validate the *envelope* (retrieval + confidence + review gate) rather than the LLM itself, use seeded / low-temp generation for validation runs, accept statistical rather than deterministic validation criteria.
- **Requires re-qualification on model updates** (GPT4IFX version changes). Tight coupling to GPT4IFX release cadence.
- **Method 1b (process evaluation) for the LLM provider** is also non-trivial — need evidence from OpenAI / upstream / Microsoft on their development process (possible for Copilot Enterprise via ISO/IEC 42001 MS cert; harder for upstream GPT-4).

### 3.4 Hybrid position — DIFFERENTIATED BY OUTPUT TYPE (recommended)

**Recommendation:** Apply TD1 **only** where downstream static analysis + formal verification + mandatory review provide high-confidence detection. Apply TD2 **elsewhere**. Specifically:

| Output Type | DAs | TD | TCL | Rationale |
|---|---|---|---|---|
| **C code** (ASIL-D production MCAL) | CIA, CTA | **TD1** | **TCL1** | Full pipeline (MISRA+AUTOSAR+Bugfinder+CodeProver+MC/DC+HIL) has high detection capability per ISO 26262-8 §11.4.5.5 Example 3 precedent. **Precondition:** no pipeline gate may be bypassed; TD1 claim invalidated on bypass. |
| **C code** (iLLD reference SW, relaxed MISRA) | CIA (illd), CTA (illd) | **TD1** | **TCL1** | Same pipeline applies; lower ASIL scope |
| **Test code** (unit/integration tests) | GEST (test code) | **TD1** | **TCL1** | Test code itself goes through MISRA + compile + peer review. Test code failures usually manifest as test failures, detectable |
| **Test specifications** (not code) | GEST (test spec), ATQA | **TD2** | **TCL2** | No static analyzer can verify that "the right test scenarios were identified." Requires method 1b + 1c: (1b) document the prompt-template + ontology process; (1c) validation on a golden set of historical requirements vs expected test scenarios |
| **Requirement drafts / reviews** | REVA, PRQ Drafter, RMA | **TD2** | **TCL2** | Semantic correctness of requirements is not statically verifiable; relies on human review. 1b + 1c: golden set of known-good requirements for regression |
| **Architecture (arch analysis, arch design)** | SAGA, SAVA | **TD2** | **TCL2** | Architecture correctness against intent requires human judgment. 1b + 1c: architecture-review calibration set |
| **Safety analysis (SASA, HAZOPA, DaFaA)** | SASA, HAZOPA, DaFaA | **TD2** | **TCL2** | **Critical** — safety analysis cannot be statically verified; expert review is the only detection. 1b + 1c: validation against historical HAZOP / DFA reference artifacts; **Safety Manager owns the qualification evidence directly** |
| **Config** | GECA, GEVT | **TD2** | **TCL2** | Parameter-constraint validation catches some errors (TD2-level), but semantic config-intent errors require human review. 1b + 1c: validation against historical config sets with known-good vs known-bad |
| **Traceability, coverage** | ATRA, TripleA | **TD1** | **TCL1** | Structural / deterministic work with graph-query semantics; errors detectable via `find_coverage_gaps` and test coverage evidence |
| **Documentation (PAGE)** | PAGE | **TD1** | **TCL1** | Generally TI1 actually — docs rarely cause safety errors if reviewed. For safety manuals: TI2/TD2 |
| **Code review** | ACRA | **TD2** | **TCL2** | Review findings are not statically verifiable; "did ACRA miss something?" requires independent human review. 1b + 1c: seeded-bug detection test set |
| **Knowledge worker / ingestion** | KW | **TD2** | **TCL2** | Could introduce errors into the KG corpus (prompt-injection, misclassification). 1b + 1c: content-type guard validation + ingestion regression suite |
| **BSP** | ZA, NXA | **TD2** | **TCL2** | BSP code flows through pipeline (could be TD1) but BSP-OS-integration correctness (device tree semantics) not fully statically verified; recommend TD2 |

**Summary distribution:**
- **TCL1 (no qualification):** CIA, CTA (both workspaces), GEST test code, ATRA, TripleA, PAGE (most) — ~5 DAs + sub-outputs
- **TCL2 (qualification required):** REVA, PRQ Drafter, RMA, SAGA, SAVA, SASA, HAZOPA, DaFaA, GECA, GEVT, ACRA, ATQA, GEST test spec, KW, ZA, NXA — ~16 DAs or sub-outputs

**Work estimate at TCL2 (average 3 engineering-days per DA × 16) = ~48 engineering-days for full initial qualification.**

### 3.5 Position to be ratified

**Safety Manager + AI Governance Lead + Platform Team must formally ratify the hybrid position in §3.4 before evidence bundles begin.** Sprint 12 gate.

---

## 4. Tool Qualification Methods (ISO 26262-8 §11.4.6)

For TCL2 DAs at ASIL-D, methods 1b + 1c are highly recommended. Our concrete approach:

### 4.1 Method 1b — Evaluation of the Tool Development Process

**For GPT4IFX (via internal Infineon process):**
- Document the GPT4IFX hosting, update, and maintenance process
- Document the model-version registry and pre-rollout regression test procedure
- Link to GPT4IFX Platform Team quality evidence (ISO 9001 / IATF 16949 org processes)
- For upstream foundation model: reference upstream vendor's documentation (partially satisfied if upstream has ISO/IEC 42001 — e.g., Microsoft for Copilot; TBD for GPT4IFX upstream)

**For AICE Core Engine:**
- Reference Infineon's internal SW development process
- ASPICE assessment result (target: CL2 / CL3)
- Corpus ingestion process documented (DATA_GOVERNANCE_POLICY v1.1.0)

**For each DA:**
- DA development process documented (prompt template engineering, test harness, sign-off)
- DA owner signs off

### 4.2 Method 1c — Validation of the Software Tool

**Approach:** Validate the **envelope** (retrieval + prompt + confidence + review gate), not the LLM itself.

**Per DA:**
- **Validation test suite** — a set of test cases covering the DA's declared tasks. Each test case has:
  - Input (prompt + RAG context)
  - Expected output characteristics (not exact output, but structural / semantic properties: "must link to requirement X", "must not use register Y", "must include DET error handling")
  - Pass criteria (property-based; statistical for multiple runs)
- **Coverage criteria** — test cases cover all declared tasks × top-5 modules × [typical, edge, error] scenarios
- **Independence** — validation tests are NOT used as training examples or approved patterns
- **Re-run cadence** — monthly + on any model update + on any DA version update
- **Regression threshold** — e.g., >95% pass rate; investigate any regression
- **Artifacts:** Validation Test Plan, Validation Test Report, results archive

**Per AICE (shared validation):**
- Hybrid-RAG retrieval validation (golden query set — GAP-11)
- Confidence formula validation (distribution sanity, calibration check against historical reviews)
- Review Gate ASIL-override enforcement validation

### 4.3 Method 1d — Development per a safety standard (optional)

- Not pursued for DAs (expensive and impractical for LLM-based components)
- Possible for deterministic AICE components (ingestion parsers, confidence formula, Cerbos policy) but method 1c suffices

---

## 5. Evidence Structure per DA

For each DA in the TCL2 set, produce a **Tool Criteria Evaluation Report (TCER)** package:

| Artifact | Content |
|---|---|
| TCER header | DA name, version, owner, date, AICE version, GPT4IFX model version in scope |
| §1 Use case description | Tasks automated, input artifacts, output artifacts, downstream verification |
| §2 TI classification | TI2 with rationale |
| §3 TD classification | TD per §3.4 of this plan with rationale per output type |
| §4 TCL | Resulting TCL per §2 and §3 |
| §5 Qualification method(s) selected | 1b + 1c with scope |
| §6 Method 1b evidence | Process documentation references |
| §7 Method 1c evidence | Validation test plan + latest report + regression status |
| §8 Re-qualification triggers | Model version update; DA major-version update; prompt template change; corpus schema change |
| §9 Sign-off | DA owner, Safety Manager, AI Governance Lead |

Retain per DATA_GOVERNANCE_POLICY v1.1.0 §6 (product lifetime + 10 years).

---

## 6. Tool Validity Check (ISO 26262-8 §11.4.10)

A validity check must run per project to confirm the tool is fit for use with the current project configuration. Our implementation:

**Per-release check (automated):**
- MLflow model registry: active GPT4IFX model matches approved version in AIBOM
- Prompt template version hashes match signed production set
- Confidence formula weights match approved version
- Cerbos policy bundle SHA matches approved version
- Corpus snapshot tag is from approved set

**Per-project check (once per project kickoff):**
- Review TCER for each DA in use — no expired qualification, no pending re-qualification trigger
- Confirm ASIL target matches qualification scope
- Confirm customer-specific data-class policies loaded if customer NDA workspace

**Automated Tool Validity Check tool (planned):** MCP tool `verify_tool_validity` returns pass/fail with detail for a given session_id.

---

## 7. Re-qualification Triggers

Re-qualify (method 1c re-run) when:

| Trigger | Scope |
|---|---|
| GPT4IFX base model version change | Full re-qualification for DAs using that model |
| DA major-version update (any change touching prompts, context assembly, output format) | The specific DA |
| Prompt template change (minor versions only require regression test, not full re-qual) | The specific DA |
| Confidence formula weight change | All DAs (shared validation) |
| Corpus schema change (major only) | Retrieval validation; DAs depending on affected ontology profile |
| Pipeline gate changes (e.g., new Polyspace rules) | Revisit TD claim — if gates weakened, TD may drop from 1→2 |
| Every 12 months minimum | All DAs (cadence) |

---

## 8. Status and Immediate Actions

**As of 2026-04-18:**
- Hybrid position (§3.4) — proposed, awaits ratification by Safety Manager + AI Governance Lead + Platform Team
- No TCER produced yet for any DA
- Golden query set — not created
- Validity check tool — not implemented

**Immediate actions (Sprint 12):**
1. Ratify §3.4 hybrid position (2026-Q2)
2. Produce TCER for top-priority High-risk DAs: **CIA, CTA, SASA, HAZOPA, GEST, ACRA** (Sprint 12)
3. Validation test suite kickoff for each of the 6
4. Golden query set v0.1 (retrieval validation)

**Sprint 13–14:**
- TCER for remaining 10 DAs
- Validity check tool implementation
- ISO 26262-8 Clause 11 audit-readiness review

---

## 9. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-04-18 | Safety Manager + AI Governance Lead | Initial release — hybrid TD1/TD2 position proposed |

**Ratification (pending):**

| Role | Name | Date |
|---|---|---|
| Safety Manager | __________ | __________ |
| AI Governance Lead | __________ | __________ |
| Platform Team Lead | __________ | __________ |
| Quality Manager | __________ | __________ |

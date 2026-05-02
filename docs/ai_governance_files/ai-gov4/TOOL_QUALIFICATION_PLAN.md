# Tool Qualification Plan — AICE and Domain Assistants
## ISO 26262-8 Clause 11 "Confidence in the use of software tools"

| Field | Value |
|---|---|
| **Document ID** | AICE-GOV-007 |
| **Version** | 3.0.0 |
| **Date** | 2026-05-02 |
| **Classification** | Internal — Infineon Technologies |
| **Owner** | Safety Manager + AI Governance Lead + Platform Team |
| **Applies to** | **Zone A (`mcal`) only** — ASIL-A through ASIL-D software development. Zones B and C use the lightweight "AI Usage Statement" (§2.2) |

---

## 1. Purpose

Defines the **Software Tool Qualification Plan (STQP)** for AICE and the Domain Assistants per ISO 26262-8:2018 Clause 11 "Confidence in the use of software tools." Covers:

1. Tool classification (TI / TD / TCL) per use-case
2. Qualification method selection
3. Evidence structure (Tool Criteria Evaluation Report and qualification reports)
4. Tool validity check
5. The **TD1 vs TD2 decision** — why it matters and how to choose

---

## 2. Regulatory Context

- **ISO 26262-8:2018 Clause 11** — required confidence in software tools
- **ASIL target for mcal: ASIL-D** (delivered to German OEMs)
- **Relevant ASPICE processes:** SWE.1–SWE.6 + SUP.11
- **Related documents:** AICE_SYSTEM_CARD v3.0.0; AI_USAGE_POLICY v3.0.0; AICE-GOV-006 (GPAI); AICE-GOV-009 (FOSS); AICE-GOV-010 (Sandbox); VDA AI-in-QM Yellow Volume Ch. 7
- **Note:** ISO/PAS 8800:2024 explicitly excludes "software tools that use AI methods" from its scope. Therefore not a qualification target for AICE itself.

### 2.1 Scope Zones — What This Plan Covers

ISO 26262-8 Clause 11 applies only where an ASIL claim exists on the output:

| Zone | Output | ASIL claim | Clause 11 applies? | Instrument |
|---|---|---|---|---|
| **A** Productive MCAL | Customer-delivered MCAL C, ARXML, safety analysis | Up to ASIL-D | **Yes** | **Full STQP / TCER per this document** |
| **B** iLLD reference SW | Reference driver code, headers, examples | None (QM) | No | "AI Usage Statement" (§2.2) |
| **C** FOSS BSP | Zephyr / NuttX BSP glue, device tree, kconfig | None (integrator qualifies as SOUP) | No | "AI Usage Statement" (§2.2) |

**The rest of this plan (§3 onwards) applies only to Zone A.** For Zones B and C, the clause 11 ceremony is dropped.

### 2.2 AI Usage Statement (Zone B / C) — lightweight substitute

Each Zone B or Zone C DA has a one-page **AI Usage Statement** documenting:

1. DA name, version, LLM backend (Copilot Enterprise primary; GPT4IFX for specialized paths), AICE version
2. Task scope
3. Explicit declaration: "No ASIL claim on outputs. ISO 26262-8 Clause 11 not applicable."
4. Downstream verification: MISRA advisory + Bugfinder + compile + peer review (Zone B); FOSS-project-native (Zone C)
5. License compliance controls reference (AICE-GOV-009)
6. Sandbox controls reference (AICE-GOV-010)
7. AI transparency markers applied — confirmed
8. Re-evaluation trigger: "If any output is later claimed against an ASIL target by Infineon or a downstream integrator, this DA must be formally qualified per §3 before that claim is valid."

The statement is a living document, ~1 page, in the DA's source repo. Not assessor-facing unless zone changes.

### 2.3 Boundary Condition — Zone B/C Output Promoted to Zone A

If an iLLD driver is lifted into a productive MCAL delivery, the current TCER from Zone A for that DA type applies. The iLLD version itself doesn't inherit qualification; the productive-path use triggers qualification burden at the point of promotion. Document in the customer delivery's safety case.

---

## 3. The TD1 vs TD2 Decision — Pros, Cons, and Recommended Position

### 3.1 Why this decision matters

Per ISO 26262-8 §11.4.5:

| Tool Impact | Tool Error Detection | → TCL | Qualification required? |
|---|---|---|---|
| TI1 (no safety impact) | — | TCL1 | No |
| TI2 (safety impact) | TD1 (high detection) | **TCL1** | **No** |
| TI2 | TD2 (medium detection) | TCL2 | Yes — methods 1b and 1c (recommended for ASIL-D) |
| TI2 | TD3 (low detection) | TCL3 | Yes — method 1c or 1d |

Every code-generating DA has TI2 (a malfunction can introduce safety errors). The decision is on TD:

- TD1 → TCL1 → no formal qualification. Evidence is the pipeline.
- TD2 → TCL2 → qualification evidence bundle per DA (methods 1b + 1c).

### 3.2 The TD1 argument (aggressive, cheaper)

Given Infineon's existing downstream pipeline for ASIL-D MCAL — **MISRA C:2012 + AUTOSAR C++ guideline check + Polyspace Bugfinder + Polyspace CodeProver (formal) + structural coverage (MC/DC for ASIL-D) + integration test on HIL + mandatory human review + independent reviewer + safety manager sign-off** — there is "high confidence that any malfunction of the AI tool will be prevented or detected" (§11.4.5.4 TD1 criterion).

**Precedent:** §11.4.5.5 Example 3 (compiler): "TD1 is selected for a code generator when the generated source code is verified in accordance with ISO 26262."

| Pros | Cons |
|---|---|
| **No method 1c validation required** — saves ~3-5 engineering-days per DA × 16+ DAs ≈ 50 days | **Depends on unbypassable CI/CD discipline** — any gate bypass invalidates TD1 for that artifact |
| Evidence is already generated on every commit | **Doesn't fit all DA output types** — only defensible for C code where static analysis is effective |
| Aligns with: pair probabilistic generation with deterministic verification | **Assessor challenge risk** — auditor may push back on TD1 for stochastic tool, even with strong V&V |
| Validates pipeline reuse | Shifts detection burden to pipeline + human review |
| No re-qualification ceremony on every Copilot model update | Difficult to explain to less-savvy auditors |

### 3.3 The TD2 argument (conservative, more expensive)

LLMs are stochastic. Plausible-looking-but-wrong output can pass downstream gates. Default to TD2 → TCL2 → methods 1b + 1c required for ASIL-D.

| Pros | Cons |
|---|---|
| Conservative; matches industry default for generative AI in safety | **Cost** — ~3 engineering-days per DA × 22 DAs ≈ 66 days initial + re-qualification on major changes |
| Concrete evidence bundle (TCER + validation tests + STQR) | **LLM non-determinism complicates 1c** — needs statistical / property-based criteria |
| Forces explicit per-DA validation | **Tight coupling to LLM release cadence** (Copilot Enterprise + GPT4IFX) |
| Defensible before any ISO 26262 assessor | Method 1b for upstream LLM provider can be tricky — see §3.5 |
| Structures recurring re-qualification | Added cost may crowd out higher-value work |

### 3.4 Hybrid position — DIFFERENTIATED BY OUTPUT TYPE (recommended)

Apply TD1 only where downstream static analysis + formal verification + mandatory review provide high-confidence detection. Apply TD2 elsewhere.

| Output Type | DAs | TD | TCL | Qualification |
|---|---|---|---|---|
| Production C code (ASIL-D) | CIA, CTA | TD1 | TCL1 | None formal; rely on pipeline + validity check |
| Test code | GEST (code portion) | TD1 | TCL1 | None formal |
| Structural / graph output | ATRA, TripleA, RMA-structural | TD1 | TCL1 | None formal |
| Requirements, safety analysis, architecture, test specs, config, code review, ingestion, BSP | REVA, PRQ, SAGA, SAVA, **SASA**, **HAZOPA**, DaFaA, ACRA, GEST (spec), ATQA, GECA, GEVT, KW, ZA, NXA | TD2 | TCL2 | Methods 1b + 1c per DA |
| Documentation | PAGE (non-safety) | TI1 | TCL1 | None |
| Documentation (safety manual) | PAGE (safety manual) | TI2/TD2 | TCL2 | 1b + 1c |

**Work estimate (hybrid):** ~3 engineering-days × 16 TCL2 DAs ≈ **48 engineering-days** for initial qualification.

### 3.5 LLM Provider Method 1b Evidence (REVISED for v3.0.0)

With Copilot Enterprise as primary LLM, Method 1b (process evaluation) leans on **Microsoft's published evidence**, which is significantly stronger than for GPT4IFX:

| LLM | Method 1b evidence available |
|---|---|
| **GitHub Copilot Enterprise** (primary) | **ISO/IEC 42001 certification** for Microsoft Copilot family (AIMS); Copilot Trust Center documentation; SOC 2 Type II audit reports; Microsoft's published responsible-AI commitments; ICS-9001 / IATF aligned where applicable |
| **GPT4IFX** (specialized paths) | Internal Infineon SW development process; ASPICE assessment; model registry / pre-rollout regression test procedure; reference to upstream foundation model vendor documentation if available |

**Net governance posture:** the 1b burden for the dominant ASIL-D code path (CIA, CTA) is largely satisfied by Microsoft's certifications. Infineon's 1b evidence focuses on the **AICE-side envelope** (RAG, prompts, review gate, ingestion processes), not the LLM internals.

### 3.6 Position to be ratified

Safety Manager + AI Governance Lead + Platform Team must formally ratify the hybrid position in §3.4 before evidence bundles begin. **Sprint 12 gate.**

---

## 4. Tool Qualification Methods (ISO 26262-8 §11.4.6)

For TCL2 DAs at ASIL-D, methods 1b + 1c are highly recommended.

### 4.1 Method 1b — Evaluation of Tool Development Process

**For Copilot Enterprise:**
- Reference Microsoft's ISO/IEC 42001 certification (covers Copilot family AIMS)
- Reference Copilot Trust Center documentation
- Reference Microsoft's commercial agreement provisions for EU Data Boundary, no-training, audit log access (§4.3 of System Card)
- Maintain an "evidence vault" with copies of relevant Microsoft documentation, dated and version-tagged

**For GPT4IFX (specialized paths):**
- Document GPT4IFX hosting, update, and maintenance process
- Document model-version registry and pre-rollout regression test procedure
- Link to GPT4IFX Platform Team quality evidence
- For upstream foundation model: reference upstream vendor documentation (subject to availability)

**For AICE Core Engine:**
- Reference Infineon's internal SW development process
- ASPICE assessment result (target: CL2 / CL3)
- Corpus ingestion process (DATA_GOVERNANCE_POLICY)

**For each DA:**
- DA development process documented (prompt template engineering, test harness, sign-off)
- DA owner signs off

### 4.2 Method 1c — Validation of the Software Tool

Validate the **envelope** (retrieval + prompt + confidence + review gate), not the LLM itself.

**Per DA:**
- **Validation test suite** — covers declared tasks. Each test case has input (prompt + RAG context), expected output characteristics (property-based: "must link to requirement X", "must not use register Y", "must include DET error handling"), pass criteria (statistical for multiple runs)
- **Coverage criteria** — declared tasks × top-5 modules × [typical, edge, error] scenarios
- **Independence** — validation tests not used as training examples or approved patterns
- **Re-run cadence** — monthly + on Copilot Enterprise major version + GPT4IFX update + DA version update
- **Regression threshold** — e.g., > 95% pass rate
- **Artifacts** — Validation Test Plan, Validation Test Report, results archive

**Per AICE (shared validation):**
- Hybrid-RAG retrieval validation (golden query set — GAP-11)
- Confidence formula validation (calibration vs historical reviews)
- Review Gate ASIL-override enforcement validation
- **Sandbox patch/inject behavior validation** (NEW v3.0.0) — test that sandbox-grounded results carry the correct `_patched`/`_injected` flags and that ASIL-D sessions enforce `ephemeral_boost=0` per AICE-GOV-010 §C10

### 4.3 Method 1d — Development per a Safety Standard (optional)

- Not pursued for DAs (impractical for LLM-based components)
- Possible for deterministic AICE components (parsers, confidence formula, Cerbos policy) but method 1c suffices

---

## 5. Evidence Structure per DA

For each DA in the TCL2 set, produce a **Tool Criteria Evaluation Report (TCER)** package:

| Artifact | Content |
|---|---|
| TCER header | DA name, version, owner, date, AICE version, **primary LLM (Copilot Enterprise) version + GPT4IFX version (if used)** |
| §1 Use case | Tasks, input artifacts, output artifacts, downstream verification |
| §2 TI classification | TI2 with rationale |
| §3 TD classification | Per §3.4 of this plan, with rationale per output type |
| §4 TCL | Resulting TCL |
| §5 Qualification methods | 1b + 1c with scope |
| §6 Method 1b evidence | Process documentation references — **for Copilot Enterprise: Microsoft 42001 cert + Trust Center + commercial agreement; for GPT4IFX: internal Infineon process** |
| §7 Method 1c evidence | Validation test plan + latest report + regression status |
| §8 Re-qualification triggers | **Copilot Enterprise major version**, GPT4IFX version, DA major-version, prompt template, corpus schema, sandbox parser change |
| §9 Sign-off | DA owner, Safety Manager, AI Governance Lead |

Retain per DATA_GOVERNANCE_POLICY §6 (product lifetime + 10 years).

---

## 6. Tool Validity Check (ISO 26262-8 §11.4.10)

Per-project check confirms tool fit for current project configuration.

**Per-release (automated):**
- MLflow registry: active **Copilot Enterprise** version matches approved version in AIBOM
- MLflow registry: active **GPT4IFX** version matches approved version
- Prompt template version hashes match signed production set
- Confidence formula weights match approved version
- Cerbos policy bundle SHA matches approved version
- Corpus snapshot tag from approved set
- **Sandbox controls C9, C10 active for ASIL-D (NEW)**

**Per-project (once at kickoff):**
- Review TCER for each DA in use — no expired qualification, no pending re-qualification trigger
- Confirm ASIL target matches qualification scope
- **Confirm Copilot Enterprise §4.3 contractual preconditions valid for project's customer (NEW)**
- Confirm customer-specific data-class policies if customer NDA workspace

**Automated tool:** MCP tool `verify_tool_validity` returns pass/fail with detail for a given session_id.

---

## 7. Re-qualification Triggers

Re-qualify (method 1c re-run) when:

| Trigger | Scope |
|---|---|
| **Copilot Enterprise major version** (GitHub Copilot Enterprise model upgrade) | All DAs using Copilot |
| **GPT4IFX base model version** | All DAs using GPT4IFX (specialized paths) |
| **Microsoft Copilot Enterprise contractual change** affecting §4.3 preconditions | Affected DAs (data-class scope) |
| DA major-version update | Specific DA |
| Prompt template change (minor only requires regression, not full re-qual) | Specific DA |
| Confidence formula weight change | All DAs |
| Corpus schema change (major) | Retrieval validation; DAs using affected ontology profile |
| **Sandbox parser change (NEW)** | DAs using sandbox path |
| **Sandbox control implementation (C1/C9/C10) changes** | Validation includes sandbox path |
| Pipeline gate changes (new Polyspace rules) | Revisit TD claim — if gates weakened, TD may drop |
| Every 12 months minimum | All DAs (cadence) |

---

## 8. Status and Immediate Actions

**As of 2026-05-02:**
- Hybrid position (§3.4) — proposed; awaits ratification by Safety Manager + AI Governance Lead + Platform Team
- No TCER produced yet for any DA
- Golden query set — not created
- Validity check tool — not implemented
- **Microsoft 42001 evidence vault for Copilot Enterprise** — not yet collected

**Immediate actions (Sprint 12):**
1. Ratify §3.4 hybrid position (2026-Q2)
2. Produce TCER for top-priority High-risk DAs: CIA, CTA, SASA, HAZOPA, GEST, ACRA (Sprint 12)
3. Validation test suite kickoff for each of the 6
4. Golden query set v0.1 (retrieval validation)
5. **Microsoft 42001 + Trust Center + commercial agreement evidence vault** — collect, store in WORM (NEW)

**Sprint 13–14:**
- TCER for remaining 10 DAs
- Validity check tool implementation
- ISO 26262-8 Clause 11 audit-readiness review

---

## 9. Document Control

| Field | Value |
|---|---|
| Current version | 3.0.0 |
| Effective date | 2026-05-02 |
| Supersedes | All prior versions |

### Version 3.0.0 — Material Changes

- **Copilot Enterprise as primary LLM** for Zone A acknowledged
- **§3.5 Method 1b evidence** — Microsoft's 42001 cert (Copilot family) is the dominant 1b evidence for the primary LLM path; Infineon's burden focuses on the AICE-side envelope and on GPT4IFX (specialized paths)
- **§4.2 Method 1c** — adds validation of sandbox patch/inject behavior and ASIL-D `ephemeral_boost=0` enforcement
- **§5 TCER §6 evidence** — explicit reference to Microsoft documentation for Copilot path
- **§6, §7 re-qualification triggers** — Copilot Enterprise version + Microsoft contractual change added
- **§8 immediate action** — establish Microsoft 42001 evidence vault
- Legacy version-history consolidated

### Approval (pending ratification)

| Role | Name | Date |
|---|---|---|
| Safety Manager | __________ | __________ |
| AI Governance Lead | __________ | __________ |
| Platform Team Lead | __________ | __________ |
| Quality Manager | __________ | __________ |

# AI Governance Implementation Plan

**Document ID**: AICE-GOV-003
**Version**: 2.2.0 (supersedes v2.1.0 of 2026-04-20)
**Classification**: Internal — Infineon Technologies
**Owner**: ATV MC D SW VDF
**Last Updated**: 2026-04-20

> This plan identifies governance gaps in the AI Core Engine and Domain Assistants,
> specifies technical implementations, and maps them to sprint delivery.
> v2.0.0 reflects clarifications from Apr 2026: GPT4IFX on-prem, ASIL-D mcal target,
> German OEM customer base, no fine-tuning, no China operations, full static analysis
> pipeline in place.

---

## 0. Changes

### v2.2.0 (2026-04-20) delta
Closes **Ephemeral Sandbox governance gap**. New **GAP-22 Sandbox Snapshot & Audit Pipeline** implementing the 8 controls from AICE-GOV-010. New requirement AICE-GOV-021. Risk register adds "Sandbox content loss → Art. 12 compliance failure" as Critical-impact risk.

### v2.1.0 (2026-04-20) delta
Adds **scope-zone aware governance**: recognizes Zone A (mcal productive), Zone B (iLLD reference), Zone C (foss-bsp). New **GAP-21 FOSS License Compliance Pipeline** for Zone B/C (see AICE-GOV-009). Clarifies that several existing GAPs (GAP-15 Tool Qualification, GAP-19 ASIL-gated override) apply to Zone A only. §1 maturity scoring unchanged (Zone A is the critical path).

### v2.0.0 (2026-04-18) delta from v1.0.0

| Area | v1.0.0 | v2.0.0 |
|---|---|---|
| Gap list | GAP-01 to GAP-11 | **Adds GAP-12 AIBOM, GAP-13 GPAI obligations, GAP-14 VDA Ch.7 adoption, GAP-15 TD1 evidence bundle, GAP-16 PII scrubber, GAP-17 prompt template signing, GAP-18 WORM audit, GAP-19 ASIL-gated override, GAP-20 field-failure traceback** |
| ISO/PAS 8800 | Listed as "partial" alignment | **Removed from alignment matrix — standard excludes dev-tool AI per its own scope** |
| MLE domain | Implicit | **Explicitly de-scoped** (no fine-tuning) |
| Sprint plan | Completes Sprint 14 | **Sprint 11–Sprint 16 timeline; Sprint 14 is minimum viable; full maturity Sprint 16** |
| Target maturity | 4.0 / 5 | 4.0 / 5 (unchanged, but scoring updated for new gaps) |
| ISO 26262-8 tool qualification | Not explicit | **Made explicit — TD1 vs TD2 decision required (see main report §2.1 and AICE-GOV-007)** |

---

## 1. Governance Maturity Assessment

### 1.1 Current State (Sprint 10 baseline + Apr 2026 reassessment)

| Governance Area | NIST RMF Function | Status | Score (1-5) |
|---|---|---|---|
| Access control (RBAC) | GOVERN | Implemented (Cerbos, 3-tier, per-DA keys) | 5 |
| Audit trail | GOVERN | Implemented (PostgreSQL 7-table); **WORM layer missing (GAP-18)** | 3 |
| Human oversight | MANAGE | Implemented (Review Gate, confidence scoring); **ASIL-gated override missing (GAP-19)** | 3 |
| Traceability | MAP | Implemented (V-Model chain, coverage gaps) | 4 |
| Observability | MEASURE | Implemented (Prometheus, Grafana, 11 metrics) | 4 |
| Feedback loop | MANAGE | Implemented (FeedbackSink, PatternStore, learning) | 4 |
| System documentation | MAP | **Improved in v2.0.0** (System Card v2.0.0 published) | 4 |
| Output provenance | GOVERN | Partial (audit logs exist; full chain incomplete — GAP-01) | 2 |
| AI transparency marking | MAP | **Not implemented (GAP-02)** | 1 |
| Data governance policy | GOVERN | **Improved in v2.0.0** (DATA_GOVERNANCE_POLICY v1.1.0 published) | 4 |
| Bias assessment | MEASURE | Not implemented (GAP-07) | 1 |
| Model versioning (GPT4IFX) | GOVERN | **Not implemented (GAP-03)** | 1 |
| Governance reporting | MEASURE | Not implemented (GAP-04) | 1 |
| Incident response process | MANAGE | **Improved in v2.0.0** (INCIDENT_RESPONSE v2.0.0 drafted, field-failure in scope, not yet exercised) | 3 |
| DA-level governance hooks | GOVERN | Not implemented (GAP-05) | 1 |
| **AIBOM (NEW)** | GOVERN | **Not implemented, no owner (GAP-12)** | 0 |
| **GPAI Art. 53 obligations (NEW)** | GOVERN | Not implemented (GAP-13); document in AICE-GOV-006 | 1 |
| **VDA AI-in-QM Ch.7 adoption (NEW)** | GOVERN | In progress — policies reference it (GAP-14) | 2 |
| **TD1 evidence bundle per DA (NEW)** | GOVERN | Not produced (GAP-15); AICE-GOV-007 defines | 1 |
| **PII scrubber on prompt path (NEW)** | MAP | Not implemented (GAP-16) | 1 |
| **Prompt template signing (NEW)** | GOVERN | Not implemented (GAP-17) | 1 |
| **WORM audit trail (NEW)** | GOVERN | Not implemented (GAP-18) | 1 |
| **ASIL-gated override in review gate (NEW)** | MANAGE | Not implemented (GAP-19) | 1 |
| **Field-failure → AI-lineage traceback (NEW)** | MANAGE | Not implemented (GAP-20) | 1 |

**Overall Maturity Score: 2.3 / 5** (weighted avg). Strong foundation, broad gap list — but governance foundation docs published in v2.0.0 material improve the score vs v1.0.0 assessment.

### 1.2 Target States

- **Sprint 14 minimum-viable governance:** 3.5 / 5 — critical regulatory items (GAP-02, GAP-03, GAP-16, GAP-18, GAP-19) closed
- **Sprint 16 mature governance:** 4.0 / 5 — all GAPs closed; ISO/IEC 42001 Stage 1 audit-ready

---

## 2. Gap Analysis (REVISED — v2.0.0)

### 2.1 Critical Gaps (MUST FIX)

#### GAP-01: Output Provenance Chain
**Problem:** AICE logs tool invocations and archives responses, but no linkage from archive to: (a) specific KG nodes / Qdrant chunks, (b) LLM model version and parameters, (c) final committed artifact in VCS, (d) complete review evidence.
**Impact:** Cannot fully reconstruct AI-generated artifacts. Required by EU AI Act Art. 12, ISO 26262-8, Art. 73 investigation.
**Sprint:** 11 (schema + handlers); 13 (VCS integration)

#### GAP-02: AI Transparency Marking
**Problem:** AI outputs not systematically marked. Code header comment and git trailer defined in policy but not enforced.
**Impact:** EU AI Act Art. 50, AI_USAGE_POLICY P3 violation.
**Sprint:** 11

#### GAP-03: Model Version Tracking
**Problem:** GPT4IFX calls (RLM, PDF extraction, DA generation) don't record model version, temperature, token counts.
**Impact:** Reproducibility compromised; cannot investigate GPAI drift (AICE-GOV-005 root-cause category).
**Sprint:** 11

#### GAP-16: PII Scrubber on Prompt Path (NEW — v2.0.0)
**Problem:** No PII redaction before LLM invocation. Even though AICE "doesn't process PII," developer names, emails, comments could leak.
**Impact:** GDPR / DPDP defensive posture; DATA_GOVERNANCE_POLICY §8.2.
**Sprint:** 11 (critical) or 12

#### GAP-18: WORM Audit Trail (NEW — v2.0.0)
**Problem:** PostgreSQL is mutable. EU AI Act Art. 12 expects tamper-resistance.
**Impact:** Compliance evidence could be disputed in regulatory investigation.
**Sprint:** 12

#### GAP-19: ASIL-Gated Review Override in Review Gate (NEW — v2.0.0)
**Problem:** Current `evaluate_confidence` bases routing on score only. ASIL-D artifacts could theoretically auto-route if signals align.
**Impact:** Policy violation risk for ASIL-D (mcal target). AI_USAGE_POLICY §5.1 enforcement.
**Sprint:** 11

#### GAP-20: Field-Failure → AI-Lineage Traceback (NEW — v2.0.0)
**Problem:** No tooling to correlate a customer field-failure to AI-generated content in that release.
**Impact:** INCIDENT_RESPONSE Phase 0 cannot execute; Art. 73 investigation support fails.
**Sprint:** 12

### 2.2 Important Gaps (SHOULD FIX)

#### GAP-04: Governance Reporting Tool
**Sprint:** 12

#### GAP-05: DA-Level Governance Enforcement
**Problem:** Policy rules (ASIL-specific review levels) enforced at AICE layer, not each DA. Overlaps with GAP-19 but covers non-ASIL cases (data-class routing, prompt template version, etc.).
**Sprint:** 12 (central enforcement in evaluate_confidence + Cerbos); 13 (per-DA client library update)

#### GAP-06: Data Governance Documentation — **✅ Closed by DATA_GOVERNANCE_POLICY v1.1.0**

#### GAP-07: Coverage Bias Assessment Framework
**Sprint:** 13

#### GAP-08: Incident Response Procedure — **✅ Closed by INCIDENT_RESPONSE v2.0.0; implementation tooling is GAP-20**

#### GAP-12: AIBOM (NEW — v2.0.0)
**Problem:** No AIBOM owner, no generation process. Action flagged in DATA_GOVERNANCE_POLICY §10.3.
**Impact:** Supply chain traceability; customer audits.
**Sprint:** 12 (organizational assignment); 13 (generation tooling)

#### GAP-13: GPAI Provider Obligations (NEW — v2.0.0)
**Problem:** Infineon likely GPAI provider for GPT4IFX; Art. 53 obligations unassigned. Detailed in AICE-GOV-006.
**Impact:** EU AI Act compliance.
**Sprint:** 12 (Legal confirmation); 13 (documentation drafting); 14 (publication)

#### GAP-14: VDA AI-in-QM Ch.7 Adoption (NEW — v2.0.0)
**Problem:** Chapter 7 referenced in policies; actual Ch.7 risk-assessment not performed for each DA yet.
**Impact:** German OEM supply-chain audits expect VDA alignment.
**Sprint:** 12 (per-DA Ch.7 assessment for High-risk DAs); 13 (remaining DAs)

#### GAP-15: TD1 Evidence Bundle per DA (NEW — v2.0.0)
**Problem:** ISO 26262-8 Clause 11 tool qualification evidence (TCER + validation + STQP) not produced. AICE-GOV-007 defines approach.
**Impact:** Tool qualification claim undefended; blocks ASIL-D delivery attestation.
**Scope:** Zone A (mcal productive) only. Zone B/C use AI Usage Statement (AICE-GOV-007 §2.2).
**Sprint:** 12 (High-risk DAs: CIA, CTA, SASA, HAZOPA, GEST, ACRA); 14 (remainder)

#### GAP-17: Prompt Template Signing (NEW — v2.0.0)
**Problem:** Prompts not signed. Tampering risk (insider threat, supply-chain).
**Impact:** Integrity of generation pipeline.
**Sprint:** 13

#### GAP-21: FOSS License Compliance Pipeline (NEW — v2.1.0)
**Problem:** No license/copyright scanning in CI for AI-touched PRs in Zone B (`illd`) or Zone C (`foss-bsp`). Copilot "Block matching public code" setting not enforced organizationally. No FOSS Compliance Officer assigned for AI-generated content. Risk: GPL-derived snippet landing in Infineon Free License iLLD, or upstream contribution rejection due to license incompatibility.
**Impact:** Legal exposure (license contamination of Infineon IP, copyright claims from upstream authors); reputational damage if iLLD public releases contain tainted code; upstream relationship damage with Zephyr/NuttX maintainers.
**Scope:** Zone B and Zone C only (Zone A is already low-risk due to GPT4IFX routing + dense controls).
**Sprint:** 12 (Copilot setting enforcement + FOSS Compliance Officer assignment); 13 (CI scanner integration: FOSSology or ScanCode); 14 (SBOM generation per iLLD release).
**Reference:** AICE-GOV-009 FOSS_LICENSE_COMPLIANCE.md

#### GAP-22: Sandbox Snapshot & Audit Pipeline (NEW — v2.2.0) ⭐ CRITICAL
**Problem:** Ephemeral Sandbox accepts arbitrary user uploads (HW PDFs, customer specs, meeting notes, compiler logs) for session-bounded retrieval augmentation, but there is NO snapshot mechanism. When a sandbox-grounded generation ships to a customer and a field failure occurs 18 months later, the grounding content cannot be reconstructed — **breaking EU AI Act Art. 12 record-keeping, ISO 26262-8 Clause 11 validity, and INCIDENT_RESPONSE Phase 0 traceback**. Additionally: no data-class inheritance (customer-NDA content could leak to Copilot via a session misclassification), no PII scrubber on sandbox-derived chunks, no content-type guard on the sandbox path (prompt-injection via ingested PDFs unchecked), no pattern-store firewall (sandbox-grounded APPROVE verdicts could create permanent patterns from un-vetted content), no cache-share flag (semantic cache can bleed sandbox-derived context across sessions).
**Impact:**
- **Critical:** Art. 12 compliance failure for every sandbox-grounded generation shipped to customers
- **Critical:** Cannot support Art. 73 investigation if field failure traces to sandbox content
- High: Customer NDA leak risk via Copilot route
- High: Prompt-injection attack surface open
- Medium: PatternStore contamination risk
**Scope:** All zones (A, B, C), with zone-differentiated retention and ASIL-D override rule per AICE-GOV-010 §5.
**Controls (C1-C8):** snapshot on generation; provenance extension; data-class inheritance; content-type guard; PII scrubber on sandbox chunks; isolation boundaries; pattern store firewall; full audit trail.
**Sprint:** 11 (C3 data-class, C4 content-type guard, C5 PII scrubber sandbox scope); 12 (C1 snapshot + WORM, C2 provenance extension, C6 cache flag, C7 pattern firewall, C8 audit); 13 (Zone A ASIL-D override + Zone C upstream gate).
**Effort:** ~8-10 engineering-days.
**Reference:** AICE-GOV-010 SANDBOX_AND_EPHEMERAL_DATA_GOVERNANCE.md.

### 2.3 Nice-to-Have Gaps (COULD FIX)

#### GAP-09: Pattern Store Governance — **Sprint 13–14**
Pattern expiration (24-month hard retirement, 12-month review), `review_patterns` admin tool.

#### GAP-10: Cross-DA Governance Dashboard — **Sprint 13**

#### GAP-11: RAG Regression Testing (Golden Query Set) — **Sprint 13**

---

## 3. Consolidated Gap → Sprint Map

| GAP | Priority | Sprint | Owner | Target completion |
|---|---|---|---|---|
| GAP-01 Provenance chain | Critical | 11 (sch), 13 (VCS) | Platform | 2026-Q3 |
| GAP-02 AI markers | Critical | 11 | Platform | 2026-Q2 |
| GAP-03 Model version tracking | Critical | 11 | Platform | 2026-Q2 |
| GAP-16 PII scrubber | Critical | 11–12 | Platform | 2026-Q2 |
| GAP-18 WORM audit | Critical | 12 | Platform + IT | 2026-Q3 |
| GAP-19 ASIL-gated override | Critical | 11 | Platform | 2026-Q2 |
| GAP-20 Field-failure traceback | Critical | 12 | Platform + Customer Interface | 2026-Q3 |
| GAP-04 governance_report tool | Important | 12 | Platform | 2026-Q3 |
| GAP-05 DA-level enforcement | Important | 12–13 | Platform + DA owners | 2026-Q3 |
| GAP-07 Coverage bias assessment | Important | 13 | Platform | 2026-Q3 |
| GAP-12 AIBOM | Important | 12 (owner), 13 (tool) | AI Gov Lead + Platform | 2026-Q3 |
| GAP-13 GPAI obligations | Important | 12–14 | Legal + AI Gov Lead + GPT4IFX Platform | 2026-Q4 |
| GAP-14 VDA Ch.7 adoption | Important | 12–13 | AI Gov Lead | 2026-Q3 |
| GAP-15 TD1 evidence | Important | 12–14 | Safety Mgr + Platform | 2026-Q4 |
| GAP-17 Prompt template signing | Important | 13 | Platform | 2026-Q3 |
| **GAP-21 FOSS License Compliance Pipeline** (NEW v2.1.0) | **Important** | **12-14** | **FOSS Compliance Officer + Platform + Module Leads (illd, foss-bsp)** | **2026-Q4** |
| **GAP-22 Sandbox Snapshot & Audit Pipeline** (NEW v2.2.0) ⭐ | **Critical** | **11-13** | **Platform Team + AI Gov Lead + Safety Mgr (Zone A rule)** | **2026-Q3** |
| GAP-09 Pattern Store governance | Nice | 13–14 | Platform | 2026-Q4 |
| GAP-10 Dashboard | Nice | 13 | Platform | 2026-Q3 |
| GAP-11 Golden query set | Nice | 13 | Platform | 2026-Q3 |

---

## 4. Sprint Delivery Plan (REVISED)

### Sprint 11 (Governance Foundation — Critical Regulatory Items)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| Provenance chain schema + handlers | GAP-01 | 3-4 days | Critical |
| AI transparency marking in CIA + GEST | GAP-02 | 1-2 days | Critical |
| Model version tracking in GPT4IFXClient | GAP-03 | 1 day | Critical |
| **PII scrubber on prompt path** (NEW) | GAP-16 | 2-3 days | Critical |
| **PII scrubber extended to sandbox-derived chunks** (NEW v2.2.0) | GAP-22 (C5) | 1 day | Critical |
| **ASIL-gated override in evaluate_confidence** (NEW) | GAP-19 | 2 days | Critical |
| **Sandbox data-class inheritance** (NEW v2.2.0) | GAP-22 (C3) | 1-2 days | Critical |
| **Content-type guard on sandbox path** (NEW v2.2.0) | GAP-22 (C4) | 1 day | Critical |
| System Card v2.2.0 | — | Delivered | Done |
| AI Usage Policy v2.1.0 | — | Delivered | Done |
| Data Governance Policy v1.3.0 | — | Delivered | Done |
| Incident Response v2.1.0 | — | Delivered | Done |

**Sprint 11 Governance Deliverables:**
- `response_archive.provenance` column populated
- AI markers injected in CIA-generated code
- LLM model version + temperature + token counts in audit
- PII scrubber active on prompt path AND sandbox path with test canaries
- ASIL-D artifacts hard-gated to FULL+independent+safety-mgr
- Sandbox data-class classifier wired (session upgrade on upload)
- Sandbox content-type guard active (prompt-injection defense)
- All v2.x policy docs in repo

### Sprint 12 (Governance Tooling + Regulatory)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| `governance_report` MCP tool | GAP-04 | 3-4 days | Important |
| `governance_policy.yaml` + enforcement | GAP-05 | 2-3 days | Important |
| `governance_incidents` table live | — | 0.5 days | Critical |
| **WORM audit trail** (NEW) | GAP-18 | 3-4 days | Critical |
| **Field-failure traceback tool** (NEW) | GAP-20 | 2-3 days | Critical |
| **AIBOM owner assignment + skeleton generator** (NEW) | GAP-12 | 2 days | Important |
| **GPAI classification Legal review initiated** (NEW) | GAP-13 | 1 day (kickoff) | Important |
| **VDA Ch.7 per-DA assessment — High-risk DAs** (NEW) | GAP-14 | 4-5 days | Important |
| **TD1 evidence bundle — CIA, CTA, GEST, ACRA, SASA, HAZOPA** (NEW) | GAP-15 | 5-7 days | Important |
| Incident Response tabletop exercise | — | 1 day | Important |
| Grafana governance dashboard | GAP-10 | 1 day | Nice |

**Sprint 12 Governance Deliverables:**
- `governance_report` tool operational
- WORM archive live for review_evidence + response_archive
- Field-failure traceback tested via tabletop
- AIBOM v0.1 generated for Sprint 12 release
- GPAI Legal opinion initiated
- VDA Ch.7 assessments for top 6 DAs

### Sprint 13 (Governance Hardening)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| Coverage bias assessment tool | GAP-07 | 2-3 days | Important |
| Pattern Store governance (expiration, review cadence) | GAP-09 | 2 days | Nice |
| RAG regression testing (golden query set) | GAP-11 | 3-4 days | Important |
| DA governance hooks for remaining DAs | GAP-05 | 2-3 days | Important |
| `get_provenance` admin tool | GAP-01 | 1 day | Important |
| **Prompt template signing + HSM/KMS integration** (NEW) | GAP-17 | 3 days | Important |
| **AIBOM generator productized** (NEW) | GAP-12 | 3 days | Important |
| **VDA Ch.7 — remainder of DAs** (NEW) | GAP-14 | 3-4 days | Important |
| **TD1 evidence — remainder of DAs** (NEW) | GAP-15 | 3-4 days | Important |

### Sprint 14 (Governance Maturity + Audit Readiness)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| First quarterly governance report | All | 1 day | Critical |
| Governance maturity re-assessment | All | 0.5 days | Important |
| Training materials + rollout | — | 2 days | Important |
| **ISO/IEC 42001 gap analysis** (NEW) | — | 3 days | Important |
| **GPAI documentation published** (NEW) | GAP-13 | 3 days | Important |
| VCS integration for provenance artifact linking | GAP-01 | 2-3 days | Important |
| External audit readiness check (EU AI Act) | — | 1 day | Important |

### Sprint 15–16 (ISO/IEC 42001 Certification Prep)

- AIMS documentation complete
- Internal audit
- Remediation
- Stage 1 audit with accredited CB (BSI / SGS / DNV / TÜV)

---

## 5. Updated Tool Count Projection

| Sprint | Current Tools | New Governance Tools | Total |
|---|---|---|---|
| 10 | 56 | 0 | 56 |
| 11 | 56 | 0 (schema only; tools unchanged) | 56 |
| 12 | 56 | +3 (`governance_report`, `field_failure_traceback`, `worm_archive_query`) | 59 |
| 13 | 59 | +3 (`assess_coverage_bias`, `get_provenance`, `pattern_review`) | 62 |

---

## 6. Requirements Traceability

| Req ID | Requirement | Gap | Sprint | Priority |
|---|---|---|---|---|
| AICE-GOV-001 | Every AI-generated response shall include a provenance chain | GAP-01 | 11 | Must |
| AICE-GOV-002 | All AI-generated code shall include transparency markers | GAP-02 | 11 | Must |
| AICE-GOV-003 | Every LLM invocation shall record model version, temperature, tokens | GAP-03 | 11 | Must |
| AICE-GOV-004 | Consolidated governance report tool | GAP-04 | 12 | Must |
| AICE-GOV-005 | Review routing enforces ASIL-based minimums per governance_policy.yaml | GAP-05 | 12 | Must |
| AICE-GOV-006 | Data governance policy covers classification, sources, quality, lineage, retention, access | GAP-06 | ✅ v1.1.0 | Done |
| AICE-GOV-007 | Coverage bias assessment tool | GAP-07 | 13 | Should |
| AICE-GOV-008 | governance_incidents table with severity, root cause, resolution tracking | GAP-08 | 12 | Must |
| AICE-GOV-009 | Pattern Store expiration + quarterly review | GAP-09 | 13 | Should |
| AICE-GOV-010 | Golden query set + automated regression testing | GAP-11 | 13 | Should |
| AICE-GOV-011 (NEW) | AIBOM generation per release | GAP-12 | 12-13 | Should |
| AICE-GOV-012 (NEW) | GPAI Art. 53 documentation published | GAP-13 | 14 | Should |
| AICE-GOV-013 (NEW) | VDA AI-in-QM Ch.7 assessment per DA | GAP-14 | 12-13 | Should |
| AICE-GOV-014 (NEW) | ISO 26262-8 Clause 11 TCER per DA + TD1 evidence bundle | GAP-15 | 12-14 | Must |
| AICE-GOV-015 (NEW) | PII scrubber on every prompt path | GAP-16 | 11-12 | Must |
| AICE-GOV-016 (NEW) | Prompt templates signed; unsigned templates blocked | GAP-17 | 13 | Must |
| AICE-GOV-017 (NEW) | WORM archive for review_evidence + response_archive + audit_logs | GAP-18 | 12 | Must |
| AICE-GOV-018 (NEW) | ASIL-D artifacts hard-gated to FULL+independent+safety-mgr | GAP-19 | 11 | Must |
| AICE-GOV-019 (NEW) | Field-failure traceback tool; Phase 0 runnable in ≤24h | GAP-20 | 12 | Must |
| AICE-GOV-020 (NEW v2.1.0) | FOSS license compliance pipeline for Zone B/C: CI scanner, Copilot "Block matching public code", SBOM per iLLD release, FOSS Compliance Officer assigned | GAP-21 | 12-14 | Should |
| AICE-GOV-021 (NEW v2.2.0) | Sandbox Snapshot & Audit Pipeline: content-addressable WORM snapshots of sandbox files referenced at generation; provenance extension with sandbox_docs[]; data-class inheritance; content-type guard + PII scrubber on sandbox path; per-session Qdrant collection + cache-share flag; pattern store firewall blocking sandbox-grounded patterns; full sandbox lifecycle audit. Retention: Zone A product lifetime+10y, Zone B/C 3y | GAP-22 | 11-13 | **Must** |

---

## 7. Risk Register (REVISED)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| EU AI Act timeline shifts (Digital Omnibus) | Medium | Low | Monitor; adjust GAP-13 timeline |
| **Infineon GPAI classification confirmed** (NEW) | **High** | Medium | Art. 53 documentation preparation (GAP-13); budget for documentation effort |
| GPT4IFX model update causes silent quality regression | Medium | High | Model version tracking (GAP-03) + golden query regression tests (GAP-11) |
| Governance overhead slows velocity | Medium | Medium | Automate; minimize manual steps; integrate into CI/CD |
| Pattern Store accumulates outdated patterns | Medium | Medium | Pattern expiration (GAP-09) |
| Engineers bypass governance controls | Low | High | Technical enforcement (GAP-05, GAP-19); training; monitoring |
| KG coverage gaps create systematic blind spots | Medium | Medium | Coverage bias assessment (GAP-07) |
| **Field incident in customer-delivered code traced to AI** (NEW) | Low | **Critical** | Incident response v2.0.0; field-failure traceback (GAP-20); Art. 73 readiness |
| **Customer audit (German OEM) finds gaps in VDA Ch.7** (NEW) | Medium | High | GAP-14 accelerated |
| **AIBOM unowned → release without inventory** (NEW) | Medium | Medium | GAP-12 organizational assignment within 30 days |
| **ISO 26262-8 tool qualification audit failure for ASIL-D** (NEW) | Low | Critical | GAP-15 TD1 evidence bundle; AICE-GOV-007 |

---

## 8. Success Criteria

| Criterion | Target | Measurement |
|---|---|---|
| Governance maturity score | ≥ 3.5 / 5 by Sprint 14; 4.0 / 5 by Sprint 16 | Self-assessment per §1.1 |
| Review Gate bypass rate | 0% | governance_report |
| ASIL-D FULL + independent + safety-mgr coverage | 100% | review_evidence |
| Provenance chain completeness | ≥ 95% of responses | governance_report |
| AI marking compliance | ≥ 95% of generated code | automated commit scan |
| Quarterly governance report delivery | On time | calendar |
| Zero critical governance incidents unresolved > 48h | 100% | governance_incidents |
| **Field-failure → AI-lineage traceback < 24h** (NEW) | 100% (when triggered) | incident resolution logs |
| **Art. 73 reporting deadlines met** (NEW) | 100% (when triggered) | legal records |
| **AIBOM published per release** (NEW) | 100% | release process |
| **ISO/IEC 42001 Stage 1 audit-ready** (NEW) | Sprint 16 | CB engagement |

---

## Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |
| 2.0.0 | 2026-04-18 | ATV MC D SW VDF | Added GAP-12 through GAP-20; revised sprint plan; removed ISO/PAS 8800 alignment; explicit TD1/TD2 decision; ASIL-D hard-gating; WORM, AIBOM, GPAI, VDA Ch.7, PII scrubber, tool qualification tracks |
| **2.1.0** | **2026-04-20** | **ATV MC D SW VDF** | **Scope-zone awareness added. GAP-21 FOSS License Compliance Pipeline for Zone B/C. Clarified GAP-15 and GAP-19 apply to Zone A only. Cross-refers to AICE-GOV-009** |

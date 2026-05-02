# AI Governance Implementation Plan

| Field | Value |
|---|---|
| **Document ID** | AICE-GOV-003 |
| **Version** | 3.0.0 |
| **Date** | 2026-05-02 |
| **Classification** | Internal — Infineon Technologies |
| **Owner** | ATV MC D SW VDF |

> Sprint plan, gap analysis, requirements traceability, and risk register for the AICE governance program. Sized for ATV MC D SW VDF and proportional to delivery scope (German OEM customers, ASIL-D mcal target, no fine-tuning, on-prem GPT4IFX for specialized paths, GitHub Copilot Enterprise for primary LLM use).

---

## 1. Governance Maturity Assessment

| Dimension | NIST AI RMF | Status | Score (1-5) |
|---|---|---|---|
| Access control / RBAC | GOVERN | Cerbos + 3-tier; well-implemented | 5 |
| Audit trail (pre-WORM) | MEASURE | PostgreSQL audit; 7 tables | 3 |
| Human oversight | GOVERN/MANAGE | Review Gate + ASIL-D hard-gate; sandbox transparency pending | 3 |
| Traceability | MEASURE | session_id chain; provenance schema partial | 4 |
| Observability | MEASURE | Prometheus + Grafana mature | 4 |
| System documentation | MAP | System Card v3.0.0 published | 4 |
| Output provenance | MEASURE | Schema designed; not implemented | 2 |
| AI transparency marking | GOVERN | Not yet enforced in CIA/GEST output | 1 |
| Data governance policy | GOVERN | Published v2.0.0 | 4 |
| Bias assessment | MEASURE | Not implemented | 1 |
| Model versioning (Copilot Enterprise + GPT4IFX) | GOVERN | Tracking not yet wired | 1 |
| Governance reporting | MEASURE | No dashboard yet | 1 |
| Incident response | MANAGE | Runbook drafted, not exercised | 3 |
| DA-level governance hooks | GOVERN | Not implemented | 1 |
| AIBOM | GOVERN | Not produced | 0 |
| GPAI Art. 53 obligations | GOVERN | Reduced scope (only GPT4IFX paths); Legal pending | 1 |
| VDA Ch.7 per-DA | MEASURE | Not started | 2 |
| Tool qualification per DA (TCER) | MAP/MEASURE | Hybrid TD1/TD2 position drafted; per-DA TCERs not produced | 1 |
| PII scrubber | GOVERN | Not implemented | 1 |
| Prompt template signing | GOVERN | Not implemented | 1 |
| WORM audit | MEASURE | Not implemented | 1 |
| ASIL-gated override | GOVERN | Not implemented | 1 |
| Field-failure traceback | MANAGE | Procedure drafted; not implemented | 1 |
| **Sandbox governance** | GOVERN | Feature implemented (Sprint 4-5); governance controls C1-C10 partial | 2 |
| FOSS license compliance | GOVERN | Not implemented | 0 |
| **Microsoft 42001 evidence vault (NEW)** | GOVERN | Not collected | 0 |

**Overall Maturity Score: 2.4 / 5** (weighted avg).

**Targets:**
- Sprint 14: 3.5 / 5
- Sprint 16: 4.0 / 5
- Sprint 16+: ISO/IEC 42001 Stage 1 audit ready (leveraging Microsoft's existing 42001 cert for Copilot path)

---

## 2. Gap Analysis

### 2.1 Critical Gaps (MUST FIX)

#### GAP-01: Provenance Chain End-to-End

**Problem:** No persistent record links `session_id` → tool calls → context sources → LLM model version → review evidence → commit SHA. Field-failure traceback impossible. Now needs to capture sandbox refs (per AICE-GOV-010).

**Owner:** Platform Team. **Sprint:** 11. **Effort:** 3-4 days.

#### GAP-02: AI-Generated Output Transparency Marking

**Problem:** No automatic injection of AI markers into generated artifacts (CIA code, GEST tests). EU AI Act Art. 50 + Art. 13 noncompliance.

**Owner:** Platform Team. **Sprint:** 11. **Effort:** 1-2 days.

#### GAP-03: LLM Model Version Tracking

**Problem:** Calls to **Copilot Enterprise** and **GPT4IFX** don't record model version, temperature, token counts. Needed for tool qualification re-qualification triggers.

**Owner:** Platform Team. **Sprint:** 11. **Effort:** 1 day per LLM client.

#### GAP-16: PII Scrubber on Prompt Path

**Problem:** No PII scrubber. Risk of inadvertent personal-data leak via developer names, email signatures, etc., into LLM prompts. Now extended to sandbox-derived chunks (AICE-GOV-010 §C5).

**Owner:** Platform Team. **Sprint:** 11. **Effort:** 2-3 days.

#### GAP-18: WORM Audit Trail

**Problem:** Audit logs stored only in mutable PostgreSQL. EU AI Act Art. 12 record-keeping not robust to tampering.

**Owner:** Platform Team. **Sprint:** 12. **Effort:** 3-5 days (depends on storage backend choice — see action items).

#### GAP-19: ASIL-Gated Review Override in Review Gate

**Problem:** `evaluate_confidence` does not hard-gate ASIL-D to FULL+independent+SafetyMgr. Confidence score can theoretically downgrade ASIL-D review.

**Owner:** Platform Team + Safety Mgr. **Sprint:** 11. **Effort:** 2 days.

#### GAP-20: Field-Failure → AI-Lineage Traceback

**Problem:** Customer reports field defect 18+ months later → no implementation of AI-causation analysis. Needs sandbox snapshot retrieval (AICE-GOV-010 §C1).

**Owner:** Platform Team + Customer Interface Lead. **Sprint:** 12. **Effort:** 5 days (depends on GAP-01, GAP-22 C1).

### 2.2 Important Gaps (SHOULD FIX)

#### GAP-12: AIBOM (AI Bill of Materials)

**Problem:** No published AIBOM per release (DAs, models, prompts, corpus snapshots, data sources, retention).

**Owner:** AI Governance Lead + Platform Team. **Sprint:** 13. **Effort:** 2-3 days for first AIBOM.

#### GAP-13: GPAI Provider Obligations (REDUCED SCOPE)

**Problem:** Art. 53 obligations apply if Infineon hosts a GPAI under its own name. With Copilot Enterprise as primary LLM (Microsoft is GPAI provider), the burden on Infineon is reduced — applies only to **GPT4IFX-served paths** (PDF extraction, RLM planning, fallback). See AICE-GOV-006.

**Owner:** Legal + AI Governance Lead + GPT4IFX Platform Team. **Sprint:** 14. **Effort:** Coordination + Legal opinion (not engineering days).

#### GAP-14: VDA AI-in-QM Ch.7 Adoption

**Problem:** No per-DA assessment using VDA framework.

**Owner:** AI Governance Lead. **Sprint:** 12-13. **Effort:** 1 day per DA × 22 DAs ≈ 22 days.

#### GAP-15: Tool Qualification Evidence Bundle per DA

**Problem:** No per-DA TCER per ISO 26262-8 Clause 11. Hybrid TD1/TD2 position (AICE-GOV-007) provides the framework but evidence not collected per DA. **Method 1b for Copilot path now relies primarily on Microsoft's published evidence (ISO/IEC 42001 cert, Trust Center, commercial agreement) — easier than for GPT4IFX**.

**Owner:** Safety Manager + Platform Team. **Sprint:** 12-14. **Effort:** ~3 days × 16 TCL2 DAs ≈ 48 days for initial.

#### GAP-17: Prompt Template Signing

**Problem:** Prompt templates not version-controlled with cryptographic integrity. Drift can occur unnoticed.

**Owner:** Platform Team. **Sprint:** 13. **Effort:** 2-3 days.

#### GAP-21: FOSS License Compliance Pipeline

**Problem:** No license/copyright scanning in CI for AI-touched PRs in Zone B (`illd`) or Zone C (`foss-bsp`). Copilot "Block matching public code" not enforced organizationally. No FOSS Compliance Officer assigned for AI-generated content.

**Scope:** Zone B and Zone C only.

**Owner:** FOSS Compliance Officer (TBA) + Platform Team + Module Leads (illd, foss-bsp).

**Sprint:** 12 (Copilot setting + officer assignment); 13 (CI scanner integration); 14 (SBOM generation per iLLD release). **Reference:** AICE-GOV-009.

#### GAP-22: Sandbox Snapshot & Audit Pipeline ⭐ CRITICAL

**Problem:** Ephemeral Sandbox is implemented (Sprint 4-5: NetworkX + ChromaDB + HybridGraphService) but **none of the 10 governance controls C1-C10** in AICE-GOV-010 are implemented. Without C1 (snapshot on generation), sandbox-grounded customer deliveries cannot be reproduced — breaking EU AI Act Art. 12 record-keeping, ISO 26262-8 Clause 11 validity check, and INCIDENT_RESPONSE Phase 0. C9 and C10 are new in v3.0.0 (patch/inject transparency; ASIL-D `ephemeral_boost=0`).

**Scope:** All zones, with zone-differentiated retention.

**Owner:** Platform Team + AI Governance Lead + Safety Manager.

**Sprint:** 11 (C3 data-class, C4 content-type guard, C5 PII scrubber, C10 boost override); 12 (C1 snapshot + WORM, C2 provenance, C6 cache flag, C7 pattern firewall, C8 audit, C9 transparency); 13 (Zone A ASIL-D + Zone C upstream gate). **Effort:** ~10-12 engineering-days. **Reference:** AICE-GOV-010.

#### GAP-23: Microsoft Copilot Enterprise Evidence Vault (NEW v3.0.0)

**Problem:** Tool qualification Method 1b for the primary LLM (Copilot Enterprise) leans on Microsoft's published evidence (ISO/IEC 42001 cert, Copilot Trust Center, SOC 2 reports, commercial agreement). This evidence must be collected, dated, version-tagged, and stored in Infineon's evidence vault (WORM where critical) so that an ISO 26262 assessor can review it without depending on external website availability.

**Owner:** AI Governance Lead + Legal + IT Procurement. **Sprint:** 12. **Effort:** 2-3 days.

### 2.3 Nice-to-Have Gaps (COULD FIX)

GAP-09 Pattern Store governance (life cycle, audit) — Sprint 13-14. Platform.

GAP-10 Cross-DA governance dashboard — Sprint 14. Platform.

GAP-11 Golden query test set — Sprint 14. Platform + AI Gov Lead.

---

## 3. Consolidated Gap → Sprint Map

| Gap | Priority | Sprint | Owner | Target |
|---|---|---|---|---|
| GAP-01 Provenance chain | Critical | 11 | Platform | 2026-Q3 |
| GAP-02 AI markers | Critical | 11 | Platform | 2026-Q3 |
| GAP-03 LLM version tracking | Critical | 11 | Platform | 2026-Q3 |
| GAP-16 PII scrubber | Critical | 11 | Platform + DPO | 2026-Q3 |
| GAP-18 WORM audit | Critical | 12 | Platform + IT | 2026-Q3 |
| GAP-19 ASIL-gated override | Critical | 11 | Platform + Safety Mgr | 2026-Q3 |
| GAP-20 Field-failure traceback | Critical | 12 | Platform + Customer Interface | 2026-Q3 |
| GAP-22 Sandbox Snapshot & Audit ⭐ | Critical | 11-13 | Platform + AI Gov + Safety Mgr | 2026-Q3 |
| GAP-12 AIBOM | Important | 13 | AI Gov + Platform | 2026-Q4 |
| GAP-13 GPAI obligations (reduced) | Important | 14 | Legal + AI Gov + GPT4IFX Platform | 2026-Q4 |
| GAP-14 VDA Ch.7 per-DA | Important | 12-13 | AI Gov + DA owners | 2026-Q4 |
| GAP-15 Tool Qual TCER per DA | Important | 12-14 | Safety Mgr + Platform | 2026-Q4 |
| GAP-17 Prompt template signing | Important | 13 | Platform | 2026-Q4 |
| GAP-21 FOSS License Compliance | Important | 12-14 | FOSS Compliance + Platform | 2026-Q4 |
| **GAP-23 MS Evidence Vault (NEW)** | Important | 12 | AI Gov + Legal | 2026-Q3 |
| GAP-09 Pattern Store governance | Nice | 13-14 | Platform | 2026-Q4 |
| GAP-10 Cross-DA dashboard | Nice | 14 | Platform | 2026-Q4 |
| GAP-11 Golden query test set | Nice | 14 | Platform + AI Gov | 2026-Q4 |

---

## 4. Sprint Delivery Plan

### Sprint 11 (Governance Foundation — Critical Regulatory Items)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| Provenance chain schema + handlers (incl. sandbox_docs) | GAP-01 | 3-4 days | Critical |
| AI transparency marking in CIA + GEST | GAP-02 | 1-2 days | Critical |
| Model version tracking (Copilot Enterprise + GPT4IFX clients) | GAP-03 | 2 days | Critical |
| PII scrubber on prompt path | GAP-16 | 2-3 days | Critical |
| PII scrubber extended to sandbox-derived chunks (C5) | GAP-22 | 1 day | Critical |
| ASIL-gated override in evaluate_confidence | GAP-19 | 2 days | Critical |
| Sandbox data-class inheritance (C3) | GAP-22 | 1-2 days | Critical |
| Content-type guard on sandbox path (C4) | GAP-22 | 1 day | Critical |
| ASIL-D `ephemeral_boost = 0` enforcement (C10) | GAP-22 | 0.5 day | Critical |
| System Card v3.0.0 | — | Delivered | Done |
| AI Usage Policy v3.0.0 | — | Delivered | Done |
| Sandbox Governance v3.0.0 | — | Delivered | Done |

**Sprint 11 deliverables:**
- `response_archive.provenance` populated with sandbox_docs[]
- AI markers injected in CIA-generated code
- LLM model version + temperature + token counts in audit (both Copilot and GPT4IFX clients)
- PII scrubber active on prompt path AND sandbox path
- ASIL-D artifacts hard-gated to FULL+independent+SafetyMgr
- Sandbox data-class classifier wired (session upgrade on upload)
- Sandbox content-type guard active (QEAX-light + non-QEAX-strict)
- Sandbox boost forced to 0 for ASIL-D sessions
- All v3.x policy docs in repo

### Sprint 12 (Governance Tooling + Regulatory)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| WORM audit trail integration | GAP-18 | 3-5 days | Critical |
| Field-failure traceback CLI/UI | GAP-20 | 5 days | Critical |
| Sandbox snapshot on generation (C1) + WORM | GAP-22 | 3 days | Critical |
| Sandbox provenance schema completion (C2) | GAP-22 | bundled with GAP-01 | Critical |
| Cache-share flag for sandbox-grounded gens (C6) | GAP-22 | 1 day | Critical |
| Pattern store firewall (C7) | GAP-22 | 2 days | Critical |
| Sandbox audit ops (C8) | GAP-22 | 1 day | Critical |
| Sandbox patch/inject transparency to Review UI (C9) | GAP-22 | 2 days | Critical |
| TCER drafting — top 6 DAs (CIA, CTA, SASA, HAZOPA, GEST, ACRA) | GAP-15 | 2 days × 6 = 12 days | Important |
| **Microsoft 42001 + Trust Center + commercial agreement evidence vault (NEW)** | **GAP-23** | **2-3 days** | **Important** |
| VDA Ch.7 per-DA assessment kickoff (High-risk DAs first) | GAP-14 | 1 day × 6 ≈ 6 days | Important |
| Copilot "Block matching public code" enforcement (Zone B/C) | GAP-21 | 0.5 day (IT) | Important |
| FOSS Compliance Officer assignment | GAP-21 | Coordination | Important |
| Quarterly governance review #1 | — | 1 day | — |

**Sprint 12 deliverables:**
- WORM audit trail backed by tamper-evident store
- `field_failure_traceback` CLI returning chain by commit SHA (incl. sandbox snapshot retrieval)
- Sandbox-grounded generations produce WORM snapshots automatically
- Pattern store firewall blocks sandbox-grounded patterns
- Review UI displays `_patched`/`_injected` flags; ASIL-D requires `sandbox_diff` invocation
- 6 TCERs in draft; validation test scaffold for those DAs
- Microsoft 42001 evidence vault populated and indexed
- FOSS Compliance Officer named; Copilot setting enforced

### Sprint 13 (Hardening)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| AIBOM v0.1 generated for Sprint 13 release | GAP-12 | 2-3 days | Important |
| Prompt template signing | GAP-17 | 2-3 days | Important |
| GPAI obligations documentation (reduced scope) | GAP-13 | Coordination | Important |
| TCER for next 6 DAs | GAP-15 | 12 days | Important |
| VDA Ch.7 for remaining DAs | GAP-14 | 16 days | Important |
| Sandbox Zone A ASIL-D review override | GAP-22 | 1-2 days | Important |
| Sandbox Zone C upstream gate (in AICE-GOV-009 checklist) | GAP-22 + AICE-GOV-009 | 1 day | Important |
| Pattern store governance | GAP-09 | 2-3 days | Nice |
| FOSS CI scanner integration (FOSSology / ScanCode / Black Duck) | GAP-21 | 3-5 days | Important |

### Sprint 14 (Audit Readiness)

| Item | Gap | Effort | Priority |
|---|---|---|---|
| Cross-DA dashboard | GAP-10 | 3-4 days | Nice |
| Golden query test set | GAP-11 | 5 days | Nice |
| TCER for remaining 4 TCL2 DAs + validation refresh | GAP-15 | 10-12 days | Important |
| Per-DA assessment closeout | GAP-14 | 4 days | Important |
| ISO/IEC 42001 Stage 1 audit prep | — | 5 days | — |
| SBOM generation for iLLD releases | GAP-21 | 2 days | Important |
| End-to-end Art. 73 simulation tabletop (incl. sandbox-grounded scenario) | GAP-08/20 | 1-2 days | — |

### Sprints 15-16 (External assessment + corrective action)

- ISO/IEC 42001 Stage 1 audit (target: pass with minor findings)
- ASPICE re-baseline if SUP.11 evidence affects level
- Customer audit responses (German OEMs)
- Corrective actions from audits + tabletop exercise

---

## 5. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Field-failure attributed to AI without traceback support | Medium | Critical | GAP-20 Phase 0 traceback (Sprint 12) |
| EU AI Act Art. 73 reporting deadline missed | Low | Critical | INCIDENT_RESPONSE §10; tabletop Sprint 14 |
| **Sandbox content loss → Art. 12 reproducibility failure** | **Medium until C1** | **Critical** | **GAP-22 C1 (Sprint 12)** |
| Sandbox prompt-injection via PDFs | Low-Medium | High | C4 content-type guard (Sprint 11) |
| Customer NDA leak via Copilot routing | Low | High | C3 data-class inheritance + Cerbos enforcement; per-customer §4.3 verification (ongoing) |
| Pattern store contamination from sandbox-grounded gens | Low | Medium | C7 firewall (Sprint 12) |
| **Sandbox patches silently affect ASIL-D reviewer (NEW v3.0.0)** | **Medium until C9** | **High** | **C9 patch/inject transparency to reviewer (Sprint 12)** |
| **Un-vetted sandbox content outranks reviewed KG for ASIL-D (NEW v3.0.0)** | **Medium until C10** | **High** | **C10 ephemeral_boost=0 for ASIL-D (Sprint 11)** |
| ISO/IEC 42001 audit failure (Sprint 16) | Medium | Major | Maturity push Sprints 11-15; **leverage Microsoft's existing 42001 cert for Copilot path** |
| FOSS license contamination in iLLD public release | Low (with controls) | High | GAP-21 (Sprints 12-14) |
| Upstream FOSS contribution rejected on license/Copyright concern | Low (with controls) | Medium | GAP-21 + Zone C gate |
| VDA assessment finding: insufficient per-DA assessment | Medium | Major | GAP-14 (Sprint 12-13) |
| Customer audit demands deeper LLM transparency | Medium | Major | GPAI documentation (reduced scope; Microsoft 42001 cert helps) |
| GPT4IFX vulnerability discovery → tool qualification re-do | Low | Major | Re-qualification trigger; specialized paths only |
| **Microsoft Copilot Enterprise contractual change** breaks §4.3 preconditions for a customer | **Medium** | **Major** | Re-route affected data classes to GPT4IFX; Legal monitoring (ongoing) |
| AI training program rollout delayed | Medium | Major | Mandatory Sprint 11 milestone |
| AIBOM not produced before customer audit | Medium | Medium | GAP-12 |
| Tool qualification effort underestimated | Medium | Major | Hybrid TD1/TD2 position reduces Zone A burden by ~40% |

---

## 6. Requirements Traceability

| Requirement ID | Requirement Statement | Gap | Sprint | MoSCoW |
|---|---|---|---|---|
| AICE-GOV-001 | Provenance chain end-to-end (incl. sandbox refs) | GAP-01 | 11 | Must |
| AICE-GOV-002 | AI markers in all code outputs | GAP-02 | 11 | Must |
| AICE-GOV-003 | LLM version + temp + tokens in audit | GAP-03 | 11 | Must |
| AICE-GOV-004 | (consolidated into 005) | — | — | — |
| AICE-GOV-005 | Confidence formula version-pinned | (existing) | n/a | Must |
| AICE-GOV-006 | DA registry per workspace | (existing) | n/a | Must |
| AICE-GOV-007 | DA-tier auth + rate-limit | (existing) | n/a | Must |
| AICE-GOV-008 | Cross-DA governance dashboard | GAP-10 | 14 | Could |
| AICE-GOV-009 | Pattern Store governance | GAP-09 | 13-14 | Should |
| AICE-GOV-010 | Pre-LLM PII scrubber (incl. sandbox scope) | GAP-16 + GAP-22 (C5) | 11 | Must |
| AICE-GOV-011 | Tamper-evident audit trail (WORM) | GAP-18 | 12 | Must |
| AICE-GOV-012 | ASIL-gated review override | GAP-19 | 11 | Must |
| AICE-GOV-013 | Field-failure → AI-lineage traceback (incl. sandbox recovery) | GAP-20 | 12 | Must |
| AICE-GOV-014 | Prompt template signing | GAP-17 | 13 | Should |
| AICE-GOV-015 | Per-DA tool qualification evidence (TCER) | GAP-15 | 12-14 | Must |
| AICE-GOV-016 | AIBOM | GAP-12 | 13 | Should |
| AICE-GOV-017 | GPAI Art. 53 docs (reduced scope: GPT4IFX paths only) | GAP-13 | 14 | Should |
| AICE-GOV-018 | VDA Ch.7 per-DA | GAP-14 | 12-13 | Should |
| AICE-GOV-019 | Incident response with field-failure scope | (existing AICE-GOV-005) | n/a | Must |
| AICE-GOV-020 | FOSS license compliance pipeline (Zone B/C) | GAP-21 | 12-14 | Should |
| AICE-GOV-021 | Sandbox Snapshot & Audit Pipeline (C1-C10) | GAP-22 | 11-13 | **Must** |
| **AICE-GOV-022 (NEW v3.0.0)** | **Microsoft Copilot Enterprise evidence vault for Method 1b** | **GAP-23** | **12** | **Should** |

---

## 7. Open Items / Org-level Actions

| Action | Owner | Due |
|---|---|---|
| Confirm 22-DA roster | DA Team owners | Immediate |
| Assign AIBOM owner | AI Governance Lead | 30 days |
| Assign FOSS Compliance Officer (may be existing Infineon FOSS lead) | AI Governance Lead | 30 days |
| Decide WORM storage backend (S3 Object Lock / Azure / on-prem) | Platform + IT | 30 days |
| Enable Copilot Enterprise "Block matching public code" org-wide | IT (GitHub admin) | Immediate |
| **Verify §4.3 Copilot Enterprise contractual preconditions per Customer-NDA contract (NEW)** | **Legal + AI Governance Lead** | **Per customer; ongoing** |
| Ratify hybrid TD1/TD2 position | Safety Mgr + AI Gov Lead + Platform | Sprint 12 |
| Initiate Legal opinion on GPT4IFX GPAI classification (reduced scope) | Legal | Sprint 14 |
| Tabletop exercise — CRITICAL field incident incl. sandbox-grounded case | AI Gov Lead + Customer Interface | 90 days (depends on C1) |
| Per-DA VDA Ch.7 assessment kickoff | AI Gov Lead | Sprint 12 |
| Per-DA TCER drafting (top 6 DAs) | Safety Mgr + Platform | Sprint 12 |
| **Collect Microsoft 42001 + Trust Center + commercial agreement into evidence vault (NEW)** | **AI Gov Lead + Legal + IT Procurement** | **Sprint 12** |

---

## 8. Implementation Status Summary

| Domain | Status |
|---|---|
| Documents (System Card, Usage Policy, Data Gov, Incident Response, Tool Qual, GPAI, FOSS, Sandbox) | ✅ v3.0.0 published |
| Provenance & marking (GAP-01, -02, -03) | ⏳ Sprint 11 |
| Privacy & safety (GAP-16, -19) | ⏳ Sprint 11 |
| Audit & compliance (GAP-18, -20, -22 C1-C10) | ⏳ Sprint 11-12 |
| Tool qualification per DA (GAP-15, -23 evidence vault) | ⏳ Sprint 12-14 |
| AIBOM, prompt signing, VDA per-DA (GAP-12, -17, -14) | ⏳ Sprint 13 |
| FOSS license compliance (GAP-21) | ⏳ Sprint 12-14 |
| GPAI obligations (GAP-13, reduced scope) | ⏳ Sprint 14 (Legal) |

---

## 9. Document Control

| Field | Value |
|---|---|
| Current version | 3.0.0 |
| Effective date | 2026-05-02 |
| Supersedes | All prior versions |

### Version 3.0.0 — Material Changes

- **Copilot Enterprise primary across all zones** acknowledged → reduced GPAI Art. 53 burden (GAP-13 scope reduced)
- New **GAP-23 Microsoft Copilot Enterprise Evidence Vault** for tool qualification Method 1b
- New **AICE-GOV-022** requirement traceability entry
- Sandbox controls C9, C10 added to Sprint 11/12 plan
- GPT4IFX scope clarified (specialized paths: PDF, RLM, fallback, contractually-precluded routing)
- New risk: "Microsoft Copilot Enterprise contractual change breaks §4.3 preconditions"
- Legacy version-history consolidated; multi-version changelogs removed

### Approval

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| Platform Team Lead | __________ | __________ |
| Safety Manager | __________ | __________ |
| Quality Manager | __________ | __________ |
| Sponsor | __________ | __________ |

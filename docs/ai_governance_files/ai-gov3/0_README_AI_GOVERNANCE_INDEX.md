# AI Governance Document Set — Infineon ATV MC D SW VDF
## AI Core Engine (AICE) + Domain Assistants for AUTOSAR MCAL / iLLD / FOSS-BSP Development

**Index version:** 1.0.0 — **Index date:** 2026-04-20
**Maintained by:** AI Governance Lead
**Scope:** All AI-assisted development activity in ATV MC D SW VDF producing MCAL, iLLD, or FOSS-BSP (Zephyr / NuttX) artifacts

---

## 0. What This Is

This is the consolidated, final v2.x document set governing the use of AI-assisted tools (AICE + 22 Domain Assistants + GPT4IFX on-prem + GitHub Copilot Enterprise) in the development of automotive embedded software delivered to customers, including German OEMs, up to ASIL-D.

The set was built through iterative refinement over April 2026 based on:
- Confirmed technical context: GPT4IFX hosted on Infineon on-prem, ASIL-D target for `mcal` workspace, German OEM customer base, no fine-tuning, no China operations, full static analysis pipeline (MISRA + AUTOSAR + Polyspace Bugfinder + Polyspace CodeProver + MC/DC)
- Regulatory landscape (April 2026): EU AI Act timeline, ISO/IEC 42001, ISO 26262-8 Clause 11, ISO/SAE 21434, ISO/PAS 8800 (de-scoped per standard's own exclusion), ASPICE 4.0 + SUP.11 (MLE de-scoped since no fine-tuning), VDA AI-in-QM Yellow Volume (March 2026)
- Three successive governance concerns raised during review: (1) GPAI provider posture for GPT4IFX hosting, (2) iLLD / FOSS-BSP being a different governance zone than mcal, (3) Ephemeral Sandbox governance gap

The result is 10 documents spanning strategy, policy, operational controls, and implementation plan.

---

## 1. Document Set Overview

### 1.1 Foundation documents (read in this order for first-time onboarding)

| # | Document | Version | Purpose | When to consult |
|---|---|---|---|---|
| 1 | **Main Report** — `AI_Governance_for_Automotive_SW_Dev_Tools.md` | 2.0 | Strategic analysis: standards landscape, applicability matrix, tool qualification decision (TD1 vs TD2), DA risk refinements, phased recommendations | **Start here** for context and rationale |
| 2 | **AICE System Card** — `AICE_SYSTEM_CARD.md` (AICE-GOV-001) | **2.2.0** | EU AI Act Annex IV technical documentation; NIST AI RMF MAP function; primary governance reference. Defines 3 scope zones (A/B/C), data classification, data routing, review gate, failure modes, sandbox lifecycle, regulatory alignment matrix | When anyone asks "what is AICE, what governance applies, how is it controlled" |
| 3 | **AI Usage Policy** — `AI_USAGE_POLICY.md` (AICE-GOV-002) | **2.1.0** | Rules for engineers and managers. Approved/restricted/prohibited activities, zone-aware review matrix, data-class routing, training requirements, policy violations | When an engineer needs to know "am I allowed to do X with AI?" |

### 1.2 Operational policies

| # | Document | Version | Purpose | When to consult |
|---|---|---|---|---|
| 4 | **Governance Implementation Plan** — `GOVERNANCE_IMPLEMENTATION_PLAN.md` (AICE-GOV-003) | **2.2.0** | Gap analysis, sprint plan, requirements traceability, risk register. 22 GAPs mapped to sprints 11-16 | Sprint planning; tracking governance debt |
| 5 | **Data Governance Policy** — `DATA_GOVERNANCE_POLICY.md` (AICE-GOV-004) | **1.3.0** | Data classification (including public-iLLD and public-FOSS-upstream), authorized sources, data quality, lineage, retention (product lifetime + 10y for Zone A safety evidence), sandbox WORM snapshots, access control, PII scrubber, AIBOM, license compliance | When designing data flows, setting retention, classifying a new data source |
| 6 | **Incident Response** — `INCIDENT_RESPONSE.md` (AICE-GOV-005) | **2.1.0** | Incident classification (CRITICAL/HIGH/MEDIUM/LOW); 5-phase response flow; Phase 0 field-failure traceback (including sandbox recovery); 11 root cause categories (LLM hallucination, prompt issue, GPAI drift, RAG poisoning, pattern poisoning, sandbox content error/loss/injection/leak, cache bleed); Art. 73 serious incident reporting | When something goes wrong; tabletop exercise scripting |

### 1.3 Specialized governance instruments

| # | Document | Version | Purpose | Scope |
|---|---|---|---|---|
| 7 | **GPAI Provider Obligations** — `GPAI_PROVIDER_OBLIGATIONS.md` (AICE-GOV-006) | 1.0.0 | EU AI Act Art. 53 analysis for Infineon's hosting of GPT4IFX. Three legal scenarios, Art. 53(1)(a)-(d) mapping, Art. 55 systemic-risk check, customer contract clause guidance | Whenever GPT4IFX posture is questioned; Legal opinion kickoff |
| 8 | **Tool Qualification Plan** — `TOOL_QUALIFICATION_PLAN.md` (AICE-GOV-007) | **1.1.0** | ISO 26262-8 Clause 11 Software Tool Qualification Plan. TI/TD/TCL classification, hybrid TD1/TD2 position (differentiated by output type), method 1b + 1c evidence structure, re-qualification triggers. Zone A only (Zone B/C use lightweight "AI Usage Statement") | When producing TCER for any DA targeting ASIL software |
| 9 | **FOSS License Compliance** — `FOSS_LICENSE_COMPLIANCE.md` (AICE-GOV-009) | 1.0.0 | License contamination risk in AI-generated content; Copilot "Block matching public code" mandatory; CI scanner selection (FOSSology/ScanCode vs Black Duck/Snyk); SBOM generation; DCO workflow for Zephyr/NuttX upstream; FOSS Compliance Officer role | Zone B (iLLD) and Zone C (FOSS-BSP) work only |
| 10 | **Sandbox & Ephemeral Data Governance** — `SANDBOX_AND_EPHEMERAL_DATA_GOVERNANCE.md` (AICE-GOV-010) | 1.0.0 | 8 controls (C1-C8) for Ephemeral Sandbox: WORM snapshots, provenance extension, data-class inheritance, content-type guard, PII scrubber, isolation boundaries, pattern store firewall, audit trail. Zone A ASIL-D override + Zone C upstream gate | Any session that uses the Ephemeral Sandbox (arbitrary user uploads) |

### 1.4 Supporting artifacts

| Artifact | Purpose |
|---|---|
| `DA_Risk_Matrix_Refined.csv` | Per-DA risk classification: phase, primary LLM, IP class, TI, TD, TCL, priority, ASIL scope, qualification methods, review defaults. Reflects hybrid TD1/TD2 position |
| `_DEPRECATED_ANALYSIS_DOCS.md` | Marks historical analysis files (`AI_governance_g.md`, `AI_Governence.md`) as superseded |

---

## 2. Key Governance Concepts — Quick Reference

### 2.1 The three scope zones

| Zone | Workspace | Highest ASIL | License scope | Primary LLM | Governance stack |
|---|---|---|---|---|---|
| **A** Productive MCAL | `mcal`, `mcal_customer_X` | **ASIL-D** | IFX-confidential / Customer-NDA | GPT4IFX only | **Full stack** — all controls apply |
| **B** iLLD reference SW | `illd` | QM (no ASIL) | Infineon Free License (public) | Copilot Enterprise permitted | Reduced stack + **FOSS license compliance** |
| **C** FOSS BSP (Zephyr/NuttX) | `foss-bsp` | QM (integrator qualifies as SOUP) | Apache-2.0 / BSD-3 | Copilot Enterprise permitted | FOSS-specific + **upstream contribution governance** |

### 2.2 The review gate (Zone A)

| ASIL | Minimum review |
|---|---|
| QM | AUTO permitted |
| ASIL-A | QUICK |
| ASIL-B | FULL mandatory |
| ASIL-C | FULL + independent |
| **ASIL-D** | **FULL + independent + Safety Manager sign-off + full static analysis pipeline** — hard-gated, confidence score cannot bypass |

### 2.3 The data-class routing matrix

| Data Class | Copilot Enterprise | GPT4IFX |
|---|---|---|
| Public (published iLLD, AUTOSAR SWS, Zephyr/NuttX upstream) | ✅ (with "Block matching public code" enabled for Zone B/C) | Optional |
| Internal non-sensitive | ✅ | Optional |
| Infineon confidential (including iLLD pre-release) | ❌ | **Required** |
| Customer NDA-restricted | ❌ | **Required**, per-customer workspace isolation |

### 2.4 The TD1 vs TD2 position (ISO 26262-8 Clause 11)

**Hybrid by output type:**
- **TD1 / TCL1 (no formal qualification):** CIA, CTA (production C code), GEST test code, ATRA, TripleA, PAGE (non-safety) — relies on full pipeline as TD1 evidence per §11.4.5.5 Example 3
- **TD2 / TCL2 (methods 1b + 1c):** ~16 DAs producing requirements, safety analysis, architecture, test specs, config, code review — where semantic correctness not statically verifiable

### 2.5 The 8 sandbox controls (AICE-GOV-010)

1. **C1** — Content snapshot (WORM) on generation
2. **C2** — Provenance chain extended with `sandbox_docs[]`
3. **C3** — Data-class inheritance on upload
4. **C4** — Content-type guard (prompt-injection defense)
5. **C5** — PII scrubber on sandbox-derived chunks
6. **C6** — Isolation boundaries (per-session Qdrant, no cross-session cache sharing)
7. **C7** — Pattern store firewall (no patterns from sandbox-grounded generations)
8. **C8** — Full audit trail (upload / reference / expire / promote events)

### 2.6 Standards applicability (one-line summary)

| Standard | Applies to AICE? |
|---|---|
| EU AI Act (2024/1689) | **Yes** — Art. 4, 11, 12, 13, 14, 50, 53 (GPAI), 73 (incidents) |
| NIST AI RMF + GenAI Profile + SP 800-218A | **Yes** (reference) |
| ISO/IEC 42001:2023 (AIMS) | **Yes** (certification target) |
| ISO 26262-8 Clause 11 (tool qualification) | **Yes** for Zone A; no for Zone B/C |
| ISO 26262 Part 6 (SW independence) | **Yes** for Zone A output |
| ISO/SAE 21434 (cybersecurity) | **Yes** for Zone A output; recommended for B/C |
| ISO/PAS 8800:2024 (road vehicles + AI) | **No** — standard's own scope exclusion for dev-tool AI |
| ISO/IEC TS 22440 (AI functional safety) | Watch-list (CD stage) |
| ASPICE 4.0 SWE + SUP.11 | **Yes**; MLE **not triggered** (no fine-tuning) |
| VDA AI-in-QM Yellow Volume Ch. 7 | **Yes** (internal method) |
| MISRA C:2012 + AUTOSAR | **Yes** for Zone A; advisory for B |
| GDPR / DPDP | **Yes** (defensive PII scrubber) |
| China AI stack | **No** (no China operations) |

---

## 3. Top 10 Open Action Items (consolidated)

These cut across multiple docs. Addressing them closes the most governance-debt fastest.

| # | Action | Owner | Target | Ref |
|---|---|---|---|---|
| 1 | **Assign AIBOM owner** | AI Gov Lead | 30 days | DATA_GOVERNANCE_POLICY §10.3 |
| 2 | **Assign FOSS Compliance Officer** (may be existing Infineon FOSS lead) | AI Gov Lead | 30 days | FOSS_LICENSE_COMPLIANCE §5.1 |
| 3 | **Decide Sandbox WORM storage backend** (S3 Object Lock? Azure? on-prem?) | Platform Team + IT | 30 days | SANDBOX §C1 |
| 4 | **Enable Copilot Enterprise "Block matching public code" org-wide** | IT / GitHub admin | Immediate (10 min) | FOSS_LICENSE_COMPLIANCE §3.1 |
| 5 | **Ratify hybrid TD1/TD2 position** for Zone A tool qualification | Safety Manager + AI Gov Lead + Platform Team | Sprint 12 | TOOL_QUALIFICATION_PLAN §3.5 |
| 6 | **Initiate Legal opinion on GPAI classification** for GPT4IFX | Legal | Sprint 14 | GPAI_PROVIDER_OBLIGATIONS §3.4 |
| 7 | **Tabletop exercise** — CRITICAL field incident (including sandbox-grounded case) | AI Gov Lead + Customer Interface Lead | 90 days | INCIDENT_RESPONSE §8 |
| 8 | **Per-DA VDA AI-in-QM Ch.7 assessment** for High-risk DAs (CIA, CTA, SASA, HAZOPA, GEST, ACRA) | AI Gov Lead | Sprint 12 | GIP GAP-14 |
| 9 | **Per-DA TCER production** for Zone A TCL2 DAs (~16 DAs) | Safety Mgr + Platform Team | Sprint 12-14 | TOOL_QUALIFICATION_PLAN §5 |
| 10 | **Verify/confirm the 22-DA roster** (check if I've named any DA incorrectly) | DA Team owners | Immediate | AICE-GOV-002 §2.1 |

---

## 4. Governance Maturity — Snapshot

As of April 2026 (per GOVERNANCE_IMPLEMENTATION_PLAN v2.2.0 §1):

| Maturity dimension | Score (1-5) |
|---|---|
| Access control / RBAC | 5 |
| Audit trail (pre-WORM) | 3 |
| Human oversight | 3 |
| Traceability | 4 |
| Observability | 4 |
| System documentation | 4 |
| Output provenance | 2 |
| AI transparency marking | 1 |
| Data governance policy | 4 |
| Bias assessment | 1 |
| Model versioning (GPT4IFX) | 1 |
| Governance reporting | 1 |
| Incident response | 3 |
| DA-level governance hooks | 1 |
| AIBOM | 0 |
| GPAI Art. 53 obligations | 1 |
| VDA Ch.7 per-DA | 2 |
| TD1 evidence per DA | 1 |
| PII scrubber | 1 |
| Prompt template signing | 1 |
| WORM audit | 1 |
| ASIL-gated override | 1 |
| Field-failure traceback | 1 |
| **Sandbox governance (NEW)** | **0** |
| **FOSS license compliance (NEW)** | **0** |

**Weighted average: 2.3 / 5.** Strong foundation in access control, observability, documentation. Weak in enforcement, provenance, and the newly-identified gaps (sandbox, FOSS licensing).

**Targets:**
- Sprint 14 minimum-viable: **3.5 / 5**
- Sprint 16 mature: **4.0 / 5**
- Sprint 16+: ISO/IEC 42001 Stage 1 audit ready

---

## 5. Document Relationships (Dependency Graph)

```
                         Main Report (strategic)
                                 |
                   +-------------+-------------+
                   |                           |
           AICE_SYSTEM_CARD              AI_USAGE_POLICY
         (governance backbone)          (operational rules)
                   |                           |
   +---------------+-------------+-------------+
   |          |             |              |               |
DATA_GOV  INCIDENT_RESP  GPAI_OBLIG   TOOL_QUAL      GOVERNANCE_IMPL_PLAN
   |          |                        PLAN          (binds everything to sprints)
   |          |                         |
   +--+    +--+-+                +------+
      |    |    |                |
   FOSS   SANDBOX               (per-DA TCER —
   LIC    EPHEMERAL              to be produced
   (Zone  (all zones)             from TOOL_QUAL)
   B/C)                             
```

Reading recommendations:
- **Executive / Safety Manager / Quality Manager:** Main Report → System Card §§0-3 → Tool Qualification Plan §3 (TD1/TD2 position) → Implementation Plan §1, §8
- **Engineer onboarding:** AI Usage Policy (full) → System Card §§2, 5, 6 → relevant zone document (FOSS if Zone B/C, Sandbox if using sandbox)
- **Legal:** GPAI Obligations (full) → System Card §3.4 → FOSS §3, §5.3 → Incident Response §10
- **Platform Team:** Implementation Plan (full) → Sandbox Governance (full) → Data Governance Policy §§5-8

---

## 6. Version History of This Index

| Version | Date | Notes |
|---|---|---|
| 1.0.0 | 2026-04-20 | First consolidated index covering final v2.x document set |

---

## 7. Pending Refinement Work (not blocking deployment)

If governance effort continues past Sprint 16, candidate future documents:

| Candidate | Scope | Priority |
|---|---|---|
| AICE-GOV-008 TARA_AI_EXTENSION | ISO/SAE 21434 TARA extended for AI-specific threats (prompt injection, RAG poisoning, model extraction) | Medium — planned in GIP |
| AICE-GOV-011 VDA_AI_IN_QM_CH7_METHOD | Internal adaptation of VDA Ch. 7 method per DA | Medium |
| AICE-GOV-012 AIMS_MANUAL | ISO/IEC 42001 AI Management System manual for certification | Medium (certification target) |
| Per-DA TCER templates | Concrete TCER instance per TCL2 DA | High — blocks ASIL-D evidence |
| GitHub Actions workflow templates | FOSS license scanner CI job + sandbox data-class check | Low |
| Tabletop exercise scripts | Three scenarios: ASIL-D field incident, sandbox-grounded incident, FOSS license finding | Medium |

---

## 8. Contact

| Role | Responsibility (for this document set) |
|---|---|
| AI Governance Lead | Document maintenance; quarterly governance review; policy updates |
| Platform Team Lead | Technical implementation of controls |
| Safety Manager | Zone A ASIL-D controls; TCER sign-off |
| Quality Manager | ASPICE alignment; audit readiness |
| Legal | GPAI classification; Art. 73 reporting; customer contract alignment |
| FOSS Compliance Officer | Zone B/C license compliance (AICE-GOV-009) |
| Customer Interface Lead | Field-incident coordination |
| GPT4IFX Platform Lead | GPAI provider obligations (AICE-GOV-006) |

---

*End of index. All referenced documents are in the same directory.*

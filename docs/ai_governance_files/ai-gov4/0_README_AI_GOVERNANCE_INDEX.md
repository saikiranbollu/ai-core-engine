# AI Governance Document Set — Infineon ATV MC D SW VDF
## AI Core Engine (AICE) + Domain Assistants for AUTOSAR MCAL / iLLD / FOSS-BSP Development

**Index version:** 3.0.0 — **Index date:** 2026-05-02
**Maintained by:** AI Governance Lead
**Scope:** All AI-assisted development activity in ATV MC D SW VDF producing MCAL, iLLD, or FOSS-BSP (Zephyr / NuttX) artifacts

---

## 0. What This Is

Consolidated, final v3.0.0 document set governing the use of AI-assisted tools (AICE + 22 Domain Assistants + GitHub Copilot Enterprise as primary LLM + GPT4IFX on-prem for specialized paths + ifxpyarch MCP for EA model access) in the development of automotive embedded software delivered to customers, including German OEMs, up to ASIL-D.

### What changed from prior versions (v3.0.0 material)

1. **GitHub Copilot Enterprise is now the primary LLM** for all zones, including Customer-NDA, subject to contractual preconditions (System Card §4.3): EU Data Boundary, no-training assurance, audit log access, "Block matching public code", customer-managed keys, tenant isolation, per-customer contract verification
2. **GPT4IFX scope reduced** to specialized paths: PDF extraction, RLM planning, fallback when Copilot unavailable, and any data class for which §4.3 preconditions cannot be confirmed
3. **GPAI Art. 53 burden reduced** for Infineon — Microsoft is the GPAI provider for Copilot Enterprise (ISO/IEC 42001 certified for the Copilot family). Infineon's residual provider obligations apply only to GPT4IFX-served paths
4. **Tool qualification Method 1b evidence** for the primary LLM path now leans on Microsoft's published evidence (42001 cert, Copilot Trust Center, commercial agreement), collected in an Infineon evidence vault (new GAP-23)
5. **Ephemeral Sandbox is implemented** (Sprint 4-5, NetworkX + ChromaDB in-memory + HybridGraphService with `_patched`/`_injected` layering on top of persistent Neo4j)
6. **ifxpyarch MCP** integration for QEAX-based EA architecture ingestion replaces PDF-based architecture ingestion for MCAL — same `IngestionPipeline/Parsers/ea_parser.py` used for both persistent KG and sandbox paths
7. **Two new sandbox controls**: C9 (patch/inject transparency to reviewer; mandatory `sandbox_diff` for ASIL-D) and C10 (`ephemeral_boost = 0` hard-enforced for ASIL-D sessions)
8. Legacy version-history changelogs purged across all documents — single current state per doc

---

## 1. Document Set Overview

### 1.1 Foundation documents

| # | Document | Version | Purpose |
|---|---|---|---|
| 1 | Main Report — `AI_Governance_for_Automotive_SW_Dev_Tools.md` | 2.0 | Strategic analysis: standards landscape, applicability matrix, tool qualification decision, DA risk refinements, phased recommendations |
| 2 | **AICE System Card** — `AICE_SYSTEM_CARD.md` (AICE-GOV-001) | **3.0.0** | EU AI Act Annex IV technical documentation; 3 scope zones (A/B/C); data classification; LLM routing per §4.3 preconditions; ifxpyarch integration; sandbox lifecycle; failure modes; regulatory alignment |
| 3 | **AI Usage Policy** — `AI_USAGE_POLICY.md` (AICE-GOV-002) | **3.0.0** | Rules for engineers and managers. Approved/restricted/prohibited activities, zone-aware review matrix, data-class routing, training requirements |

### 1.2 Operational policies

| # | Document | Version | Purpose |
|---|---|---|---|
| 4 | **Governance Implementation Plan** — `GOVERNANCE_IMPLEMENTATION_PLAN.md` (AICE-GOV-003) | **3.0.0** | Gap analysis, sprint plan, requirements traceability, risk register. 23 GAPs (incl. new GAP-23 MS evidence vault) mapped to Sprints 11-16 |
| 5 | **Data Governance Policy** — `DATA_GOVERNANCE_POLICY.md` (AICE-GOV-004) | **3.0.0** | Data classification (incl. routing under §4.3); authorized sources; sandbox WORM retention; PII scrubber; AIBOM; FOSS license compliance |
| 6 | **Incident Response** — `INCIDENT_RESPONSE.md` (AICE-GOV-005) | **3.0.0** | Severity classification; 5-phase response; Phase 0 field-failure traceback (incl. sandbox recovery); root causes (incl. Copilot drift, GPT4IFX drift, sandbox-related); Art. 73 reporting |

### 1.3 Specialized governance instruments

| # | Document | Version | Purpose |
|---|---|---|---|
| 7 | **GPAI Provider Obligations** — `GPAI_PROVIDER_OBLIGATIONS.md` (AICE-GOV-006) | **1.1.0** | Reduced-scope Art. 53 analysis for GPT4IFX-served paths only |
| 8 | **Tool Qualification Plan** — `TOOL_QUALIFICATION_PLAN.md` (AICE-GOV-007) | **3.0.0** | ISO 26262-8 Clause 11 STQP. Hybrid TD1/TD2 position. Method 1b for Copilot leans on Microsoft 42001; for GPT4IFX uses internal Infineon process |
| 9 | **FOSS License Compliance** — `FOSS_LICENSE_COMPLIANCE.md` (AICE-GOV-009) | 1.0.0 | License contamination prevention for Zone B/C; Copilot "Block matching public code"; CI scanners; SBOM; DCO workflow |
| 10 | **Sandbox & Ephemeral Data Governance** — `SANDBOX_AND_EPHEMERAL_DATA_GOVERNANCE.md` (AICE-GOV-010) | **3.0.0** | 10 controls C1-C10. Sandbox feature acknowledged as implemented (NetworkX + ChromaDB). New C9 patch/inject transparency. New C10 ASIL-D `ephemeral_boost=0`. QEAX-light vs non-QEAX-strict content guard |

### 1.4 Supporting artifacts

| Artifact | Purpose |
|---|---|
| `DA_Risk_Matrix_Refined.csv` | Per-DA risk classification: phase, primary LLM (Copilot Enterprise), specialized LLM (GPT4IFX), IP class, TI/TD/TCL, priority, ASIL scope, qualification methods, review defaults |
| `_DEPRECATED_ANALYSIS_DOCS.md` | Legacy analysis files marked superseded |

---

## 2. Key Governance Concepts — Quick Reference

### 2.1 The three scope zones

| Zone | Workspace | Highest ASIL | License scope | Primary LLM | Specialized LLM | Governance stack |
|---|---|---|---|---|---|---|
| **A** Productive MCAL | `mcal`, `mcal_customer_X` | **ASIL-D** | IFX-confidential / Customer-NDA | **Copilot Enterprise** (subject to §4.3) | GPT4IFX (PDF, RLM, fallback, contractually-precluded) | Full stack |
| **B** iLLD reference SW | `illd` | QM | Infineon Free License (public) | Copilot Enterprise | GPT4IFX (fallback) | Reduced stack + FOSS license compliance |
| **C** FOSS BSP | `foss-bsp` | QM | Apache-2.0 / BSD-3 | Copilot Enterprise | GPT4IFX (fallback) | FOSS-specific + upstream contribution governance |

### 2.2 Copilot Enterprise §4.3 preconditions (System Card §4.3)

For Confidential or Customer-NDA routing via Copilot, ALL of the following must hold:

- EU Data Boundary in effect for prompts and completions
- Microsoft contractual no-training assurance
- Audit logs accessible to Infineon
- "Block matching public code" enabled
- Customer-managed encryption keys (where applicable)
- Tenant isolation
- For Customer-NDA: customer contract does not preclude Copilot use

If any precondition fails for a data class, that class is routed via GPT4IFX. Cerbos enforces.

### 2.3 The review gate (Zone A)

| ASIL | Minimum review |
|---|---|
| QM | AUTO permitted |
| ASIL-A | QUICK |
| ASIL-B | FULL mandatory |
| ASIL-C | FULL + independent |
| **ASIL-D** | **FULL + independent + Safety Manager + full static analysis pipeline** — hard-gated |

**ASIL-D + sandbox-grounded special rule:** force FULL + independent + Safety Manager regardless of confidence; reviewer must invoke `sandbox_diff` AND verify against authoritative source.

### 2.4 The TD1 vs TD2 position (ISO 26262-8 Clause 11)

**Hybrid by output type:**
- **TD1 / TCL1 (no formal qualification):** CIA, CTA, GEST_TestCode, ATRA, TripleA, PAGE non-safety — relies on full pipeline as TD1 evidence per §11.4.5.5 Example 3
- **TD2 / TCL2 (methods 1b + 1c):** ~16 DAs producing requirements, safety analysis, architecture, test specs, config, code review — semantic correctness not statically verifiable
- **Method 1b for Copilot path:** Microsoft 42001 evidence (collected in Infineon evidence vault — GAP-23)
- **Method 1b for GPT4IFX paths:** internal Infineon process documentation

### 2.5 The 10 sandbox controls (AICE-GOV-010 v3.0.0)

1. **C1** — Content snapshot (WORM) on generation
2. **C2** — Provenance chain extended with `sandbox_docs[]` + `hybrid_query_metadata`
3. **C3** — Data-class inheritance on upload
4. **C4** — Content-type guard (QEAX-light + non-QEAX-strict)
5. **C5** — PII scrubber on sandbox-derived chunks
6. **C6** — Isolation boundaries (NetworkX + ChromaDB ephemeral; cache-share flag)
7. **C7** — Pattern store firewall
8. **C8** — Full audit trail (lifecycle ops)
9. **C9** *(NEW v3.0.0)* — Patch/inject transparency to reviewer; mandatory `sandbox_diff` for ASIL-D
10. **C10** *(NEW v3.0.0)* — ASIL-D `ephemeral_boost = 0` override

### 2.6 Standards applicability (one-line summary)

| Standard | Applies to AICE? |
|---|---|
| EU AI Act (2024/1689) | **Yes** — Art. 4, 11, 12, 13, 14, 26, 50, 53 (reduced — GPT4IFX paths only), 73 |
| NIST AI RMF + GenAI Profile + SP 800-218A | **Yes** (reference) |
| ISO/IEC 42001:2023 (AIMS) | **Yes** (certification target — leverage Microsoft cert for Copilot) |
| ISO 26262-8 Clause 11 (tool qualification) | Yes for Zone A; no for Zone B/C |
| ISO 26262 Part 6 (SW independence) | **Yes** for Zone A |
| ISO/SAE 21434 (cybersecurity) | **Yes** for Zone A; recommended for B/C |
| ISO/PAS 8800:2024 | **No** — standard's own scope exclusion for dev-tool AI |
| ASPICE 4.0 SWE + SUP.11 | **Yes**; MLE not triggered (no fine-tuning) |
| VDA AI-in-QM Yellow Volume Ch. 7 | **Yes** (internal method) |
| MISRA C:2012 + AUTOSAR | **Yes** for Zone A; advisory for B |
| GDPR / DPDP | **Yes** (defensive PII scrubber) |
| China AI stack | **No** (no China operations) |

---

## 3. Top 10 Open Action Items

| # | Action | Owner | Target | Reference |
|---|---|---|---|---|
| 1 | **Decide Sandbox WORM storage backend** (S3 Object Lock / Azure / on-prem) | Platform Team + IT | 30 days | SANDBOX §C1 |
| 2 | **Implement sandbox C1 (WORM snapshot)** — without this, sandbox-grounded ASIL-D Art. 12 fails | Platform Team | Sprint 12 | SANDBOX §7 |
| 3 | **Verify Copilot Enterprise §4.3 contractual preconditions per Customer-NDA contract** | Legal + AI Gov Lead | Per customer; ongoing | System Card §4.3 |
| 4 | **Collect Microsoft 42001 + Trust Center + commercial agreement into evidence vault** | AI Gov Lead + Legal + IT Procurement | Sprint 12 | TQ Plan §3.5; GAP-23 |
| 5 | **Enable Copilot Enterprise "Block matching public code" org-wide** | IT (GitHub admin) | Immediate (10 min) | FOSS_LICENSE_COMPLIANCE §3.1 |
| 6 | **Assign FOSS Compliance Officer** | AI Gov Lead | 30 days | FOSS §5.1 |
| 7 | **Assign AIBOM owner** | AI Gov Lead | 30 days | DATA §10.3 |
| 8 | **Ratify hybrid TD1/TD2 position** | Safety Mgr + AI Gov + Platform | Sprint 12 | TQ §3.6 |
| 9 | **Initiate Legal opinion on GPT4IFX GPAI classification** (reduced scope) | Legal | Sprint 14 | GPAI_PROVIDER_OBLIGATIONS |
| 10 | **Tabletop exercise — CRITICAL field incident incl. sandbox-grounded case** | AI Gov + Customer Interface | 90 days (after C1) | INCIDENT §8 |

---

## 4. Document Relationships (Dependency Graph)

```
                    Main Report (strategic)
                            |
                +-----------+-----------+
                |                       |
        AICE_SYSTEM_CARD          AI_USAGE_POLICY
        (governance backbone,     (operational rules,
         §4.3 preconditions)       zone-aware review)
                |                       |
   +-----+------+------+----+-----+-----+-----+
   |     |             |        |           |
DATA_  INCIDENT_   GPAI_    TOOL_     GOVERNANCE_IMPL_PLAN
GOV    RESP        OBLIG    QUAL      (binds to sprints,
   |     |          (GPT4IFX (Hybrid    GAP-22, GAP-23)
   |     |           paths   TD1/TD2,
   |     |           only)   MS 42001
   +-+   +-+                 evidence)
     |     |
   FOSS  SANDBOX
   LIC   EPHEMERAL
   (B/C)  (all zones —
          C9, C10 NEW)
```

Reading recommendations:
- **Executive / Safety Manager / Quality Manager:** Main Report → System Card §§0-3, §4.3 → TQ Plan §3 → Implementation Plan §1, §8
- **Engineer onboarding:** AI Usage Policy (full) → System Card §§2, 5, 6, §4.5 → Sandbox doc (if using sandbox)
- **Legal:** GPAI Obligations + System Card §4.3 → FOSS §3, §5.3 → Incident Response §10
- **Platform Team:** Implementation Plan (full) → Sandbox Governance (full) → Data Governance §§5-9

---

## 5. Pending Refinement Work

| Candidate | Scope |
|---|---|
| AICE-GOV-008 TARA AI Extension | ISO/SAE 21434 TARA extended for AI threats (prompt injection, RAG poisoning, model extraction) |
| AICE-GOV-011 VDA_AI_IN_QM_CH7_METHOD | Internal adaptation of VDA Ch. 7 method per DA |
| AICE-GOV-012 AIMS_MANUAL | ISO/IEC 42001 AI Management System manual for certification (leverage Microsoft 42001) |
| Per-DA TCER templates | TCER instances per TCL2 DA |
| GitHub Actions workflow templates | FOSS license scanner CI + sandbox data-class check |
| Tabletop exercise scripts | Three scenarios: ASIL-D field incident, sandbox-grounded incident, FOSS license finding |

---

## 6. Contacts

| Role | Responsibility |
|---|---|
| AI Governance Lead | Document maintenance; quarterly review |
| Platform Team Lead | Technical implementation of controls |
| Safety Manager | Zone A ASIL-D controls; TCER sign-off; sandbox C9/C10 |
| Quality Manager | ASPICE alignment; audit readiness |
| Legal | GPAI classification; Art. 73 reporting; **§4.3 contract verification per customer**; customer contract review |
| FOSS Compliance Officer | Zone B/C license compliance |
| Customer Interface Lead | Field-incident coordination |
| GPT4IFX Platform Lead | GPAI provider obligations for GPT4IFX paths |
| **IT Procurement** | **Microsoft commercial agreement; 42001 evidence collection** |
| Data Protection Officer | PII scope |

---

*End of index. All referenced documents are in the same directory.*

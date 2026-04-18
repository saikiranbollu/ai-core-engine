# ⚠️ DEPRECATED Analysis Documents — Reading Guide

**Last updated:** 2026-04-18
**Status:** Historical reference only — DO NOT implement from these

---

## Documents to mark DEPRECATED

The following files from the initial governance analysis package should be treated as **historical background reference only** and are superseded by the v2.0.0 document suite:

| File | Status | Superseded by |
|---|---|---|
| `AI_Governence.md` | **DEPRECATED — background analysis only** | Main report (`AI_Governance_for_Automotive_SW_Dev_Tools.md` v2.0) + AICE-GOV-001 System Card v2.0 |
| `AI_governance_g.md` | **DEPRECATED — background analysis only** | Main report + System Card v2.0 |
| `AI_Governance_in_Automotive_Software.md` | **REFERENCE — retained as comprehensive background** | Not superseded; complements main report as deep-dive reference |

### Why deprecated

These two files (`AI_Governence.md`, `AI_governance_g.md`) were exploratory analyses produced before the Apr 2026 clarifications:

1. They treat AICE as potentially "limited-risk" under EU AI Act without acknowledging **Infineon's GPAI provider posture** for on-prem GPT4IFX (now addressed in AICE-GOV-006)
2. They reference ISO/PAS 8800 as partially applicable — the published standard (2024) explicitly excludes software tools using AI methods (now addressed in System Card v2.0 §10)
3. They treat ASIL-B as illustrative; **actual target is ASIL-D for mcal** (now addressed in AI_USAGE_POLICY v2.0 §5.1 and System Card v2.0)
4. They do not address the **MISRA+AUTOSAR+Polyspace Bugfinder+CodeProver+MC/DC** pipeline's role in the TD1 vs TD2 decision (now addressed in Main Report §2.1a and AICE-GOV-007)
5. They do not cover **EU AI Act Art. 73 serious incident reporting** or the **field-failure → AI-lineage traceback** workflow (now addressed in INCIDENT_RESPONSE v2.0)
6. They do not reference **VDA AI-in-QM Yellow Volume Chapter 7** (published March 2026) which is commercially binding for German OEM supply chain
7. They do not address **AIBOM** as an unowned governance gap
8. They do not address **PII scrubber** or **WORM audit** controls

### Action

- Keep the three files in the historical archive for traceability of the analysis process
- Do **not** use them as implementation references
- The `AI_Governance_in_Automotive_Software.md` (the 55KB deep-dive) remains valuable as a reference document for regulatory context; the other two are superseded

---

## The current v2.0.0 document suite (implement from these)

| Document | ID | Version |
|---|---|---|
| AICE System Card | AICE-GOV-001 | 2.0.0 |
| AI Usage Policy | AICE-GOV-002 | 2.0.0 |
| Governance Implementation Plan | AICE-GOV-003 | 2.0.0 |
| Data Governance Policy | AICE-GOV-004 | 1.1.0 |
| Incident Response | AICE-GOV-005 | 2.0.0 |
| GPAI Provider Obligations | AICE-GOV-006 | 1.0.0 (NEW) |
| Tool Qualification Plan | AICE-GOV-007 | 1.0.0 (NEW) |
| Main report | — | 2.0 |
| DA Risk Matrix (refined) | — | v2 (hybrid TD1/TD2) |

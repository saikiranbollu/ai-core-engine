# AI Governance Documentation

**Location**: `docs/governance/`  
**Version**: 1.0.0  
**Last Updated**: 2026-03-29  

---

## Document Index

| Document ID | File | Title | Purpose |
|------------|------|-------|---------|
| AICE-GOV-001 | `AICE_SYSTEM_CARD.md` | AI Core Engine — System Card | EU AI Act Annex IV technical documentation; NIST AI RMF MAP function; system risk classification; capabilities and limitations |
| AICE-GOV-002 | `AI_USAGE_POLICY.md` | AI Usage Policy | Team-level policy for approved/restricted/prohibited AI use cases; review requirements by ASIL level; accountability framework |
| AICE-GOV-003 | `GOVERNANCE_IMPLEMENTATION_PLAN.md` | Governance Implementation Plan | Gap analysis (11 gaps); technical specifications for missing governance features; sprint delivery plan (Sprint 11-14) |
| AICE-GOV-004 | `DATA_GOVERNANCE_POLICY.md` | Data Governance Policy | Data classification; authorized sources; quality criteria; lineage; retention; access controls |
| AICE-GOV-005 | `INCIDENT_RESPONSE.md` | AI Incident Response Procedure | Incident classification; response flow (5 phases); containment procedures; PostgreSQL schema |

---

## Regulatory Alignment

| Regulation / Framework | Primary Document(s) |
|----------------------|---------------------|
| EU AI Act (2024/1689) | System Card (risk classification, transparency, human oversight, record-keeping) |
| NIST AI RMF 1.0 | System Card (MAP); Usage Policy (GOVERN); Implementation Plan (MEASURE, MANAGE) |
| ISO 26262 | Usage Policy (ASIL-specific review requirements); Incident Response (safety-critical containment) |
| ISO/PAS 8800 | System Card (AI safety lifecycle principles adopted for development-tool context) |
| ASPICE 4.0 | System Card (audit trail); Data Governance (data lineage and traceability) |
| GDPR | Usage Policy (prohibited data processing); Data Governance (personal data exclusion) |
| MISRA C:2012 | Usage Policy (mandatory static analysis for all AI-generated code) |

---

## Implementation Status

| Phase | Sprint | Status | Key Deliverables |
|-------|--------|--------|-----------------|
| **Foundation** | 10 | Done | PostgreSQL audit schema, Review Gate, Cerbos RBAC, Prometheus metrics |
| **Documentation** | 11 | Done | System Card, Usage Policy, Governance Plan, Data Governance, Incident Response |
| **Provenance** | 11 | Planned | Provenance chain, AI marking, model version tracking |
| **Tooling** | 12 | Planned | `governance_report` tool, policy enforcement, incident tracking |
| **Hardening** | 13 | Planned | Coverage bias assessment, pattern governance, RAG regression tests |
| **Maturity** | 14 | Planned | Quarterly report, training, audit readiness |

---

## Quick Reference — Governance Rules

**For Engineers**:
1. Every AI-generated artifact needs human review (minimum QUICK for QM, FULL for ASIL-B+)
2. All AI-generated code must pass MISRA + Polyspace analysis before commit
3. Mark AI involvement in commit messages: `AI-assisted: <DA> session <session_id>`
4. You are the author of record — AI is your tool, not your substitute
5. Report quality issues via FeedbackSink (REJECT); report safety concerns immediately

**For Module Leads**:
1. Maintain per-module ASIL classification for governance policy enforcement
2. Review rejection patterns monthly; escalate systematic issues
3. Ensure team completion of AI Usage Policy training

**For Platform Team (AICE)**:
1. Never allow governance bypass mechanisms in production
2. Maintain audit trail completeness ≥ 98%
3. Run golden query regression tests after every KG or pipeline update
4. Produce quarterly governance report on time

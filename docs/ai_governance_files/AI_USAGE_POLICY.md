# AI Usage Policy — Automotive Embedded Software Development

**Document ID**: AICE-GOV-002  
**Version**: 1.0.0  
**Classification**: Internal — Infineon Technologies  
**Scope**: ATV MC D SW VDF and all teams using AICE-backed Domain Assistants  
**Effective Date**: 2026-04-01  
**Owner**: ATV MC D SW VDF  
**Review Cycle**: Semi-annually  

---

## 1. Purpose

This policy defines the approved uses, restrictions, review requirements, and accountability framework for AI-assisted development activities within the Infineon automotive embedded software development organization. It applies to all engineers, leads, and managers using AICE-backed Domain Assistants or any AI-assisted tooling (GitHub Copilot, GPT4IFX, Claude Code) in the development of AURIX TC3xx MCAL and iLLD software.

---

## 2. Scope and Applicability

### 2.1 In Scope

- All 21 AICE Domain Assistants (CIA, GEST, ACRA, SAGA, REVA, PRQ, SAVA, etc.)
- GitHub Copilot Enterprise (code completion, chat)
- GPT4IFX API usage for engineering tasks
- Claude Code or any other AI coding assistant used in the development workflow
- Any future AI tooling integrated into the V-Model lifecycle

### 2.2 Out of Scope

- General-purpose AI usage for non-engineering tasks (email, presentations, scheduling)
- AI usage in non-safety-related internal tools
- Research and prototyping activities explicitly marked as "non-production"

---

## 3. Principles

**P1 — Human Accountability**: The human engineer is always the responsible author of record for any artifact (code, test, requirement, analysis) that enters the product baseline. AI is a tool; the engineer is accountable.

**P2 — Mandatory Review**: Every AI-generated or AI-assisted artifact must undergo review appropriate to its safety classification before integration into the product baseline.

**P3 — Transparency**: AI involvement in any development artifact must be documented and traceable. No AI-generated content may be represented as purely human-authored.

**P4 — Proportional Controls**: Governance controls are proportional to the safety classification (ASIL level) and the type of artifact produced. Higher ASIL levels demand stricter controls.

**P5 — Continuous Improvement**: AI usage patterns, failure modes, and effectiveness metrics are tracked and used to improve the AI tooling and governance controls.

---

## 4. Approved Use Cases

### 4.1 Permitted AI-Assisted Activities

| Activity | Domain Assistants | AI Role | Human Role |
|----------|------------------|---------|------------|
| Code generation from requirements | CIA | Generate draft C code from SWR + HW specs | Review, verify MISRA compliance, test, approve |
| SFR migration | CIA (F2) | Identify and apply register name changes | Verify completeness, test on target HW |
| Bugfix analysis | CIA (F3) | Analyze warnings/findings, suggest fixes | Verify correctness of fix, understand root cause |
| Test case generation | GEST | Generate test specifications and code | Verify test coverage, independence, correctness |
| Requirement review | REVA | Identify ambiguities, gaps, testability issues | Accept/reject findings, update requirements |
| Requirement drafting | PRQ | Draft product requirements from stakeholder inputs | Review, refine, formalize in Jama |
| Code review assistance | ACRA | Flag MISRA violations, AUTOSAR issues, complexity | Make final accept/reject decision |
| Architecture analysis | SAGA | Analyze SW architecture for design issues | Validate findings against design intent |
| Traceability analysis | TripleA | Identify coverage gaps in V-Model chains | Investigate and resolve gaps |
| Debug analysis | VoltAI | Analyze register configs, known errata, patterns | Verify root cause, implement fix |
| Safety validation support | SAVA | Gather safety requirements, AoU constraints | Perform independent safety assessment |
| HAZOP support | HZOP | Gather interface definitions, guide word analysis | Conduct formal HAZOP session with expert judgment |

### 4.2 Restricted Activities (Additional Controls Required)

| Activity | Restriction | Required Controls |
|----------|------------|-------------------|
| Safety analysis (FMEA, FTA, DFA) | AI may assist with data gathering only | Expert-led analysis; AI output is input data, not analysis conclusion |
| ASIL-D code generation | AI draft permitted | FULL review mandatory + independent reviewer + MISRA + Polyspace |
| Security-sensitive code | AI draft permitted with caution | Security expert review; no AI for crypto implementations |
| Configuration generation (GECA) | AI draft permitted | Verify against AUTOSAR configuration constraints; test on target |
| Inter-module interface code | AI draft permitted | Cross-team review required; dependency validation via AICE |

### 4.3 Prohibited Activities

| Activity | Rationale |
|----------|-----------|
| Autonomous code commit without human review | Violates P1 (Human Accountability) and P2 (Mandatory Review) |
| Using AI to generate safety case evidence directly | Safety cases require engineering judgment; AI-generated evidence is not independently verifiable |
| Processing customer data or personal data through AI tools | GDPR compliance; AI tools are not approved for personal data |
| Using AI outputs to replace independent verification | ISO 26262 requires independence between implementation and verification |
| Submitting AI-generated artifacts as formal deliverables without disclosure | Violates P3 (Transparency) |
| Using unapproved AI tools on confidential source code | Information security; only approved tools (AICE, GitHub Copilot Enterprise, GPT4IFX) permitted |

---

## 5. Review Requirements by ASIL Level

### 5.1 Review Matrix

| ASIL | Code Generation | Test Generation | Requirement Review | Safety Analysis Support |
|------|----------------|-----------------|-------------------|------------------------|
| **QM** | QUICK review | QUICK review | AUTO permitted | Not applicable |
| **ASIL-A** | QUICK review | QUICK review | QUICK review | FULL review (expert) |
| **ASIL-B** | FULL review | FULL review | QUICK review | FULL review (expert) |
| **ASIL-C** | FULL + independent | FULL + independent | FULL review | FULL + independent |
| **ASIL-D** | FULL + independent + safety review | FULL + independent | FULL review | FULL + independent + safety review |

### 5.2 Review Type Definitions

| Type | Duration | Reviewer | Activities |
|------|----------|----------|-----------|
| **AUTO** | ~5 min | Any team member | Spot-check output format, verify no obvious errors |
| **QUICK** | 15-20 min | Developer with module knowledge | Verify correctness, check completeness, validate compliance |
| **FULL** | 1+ hours | Domain expert | Full correctness verification, safety analysis, requirement coverage, compliance audit |
| **Independent** | Additional | Different engineer from generator | ISO 26262 independence requirement; reviewer did not participate in generation |

### 5.3 Minimum Verification for All AI-Generated Code

Regardless of ASIL level, ALL AI-generated C code must pass:

1. **MISRA C:2012 analysis** — all mandatory and required rules (Polyspace or equivalent)
2. **Compilation** — clean build with target compiler (GCC/Tasking) with `-Wall -Werror`
3. **Static analysis** — Polyspace Bugfinder at minimum; CodeProver for ASIL-B+
4. **Functional testing** — unit tests covering the generated code
5. **Human review** — at least QUICK level with documented review evidence

---

## 6. AI Output Marking and Traceability

### 6.1 Code Marking

All AI-generated or AI-assisted code files must include a header comment:

```c
/**
 * @ai_assisted  true
 * @ai_tool      CIA v1.0.0 / AICE v2.1.0
 * @ai_session   CIA_20260329_143022
 * @ai_review    FULL | Reviewer: <engineer_name> | Date: <date>
 * @ai_confidence 67 (QUICK → overridden to FULL)
 *
 * This file was generated with AI assistance and has been reviewed
 * and approved by the engineer listed above.
 */
```

### 6.2 Requirement and Test Marking

AI-assisted requirements and test specifications must include:

- `[AI-ASSISTED]` tag in the artifact description field (Jama, Polarion, or equivalent)
- Reference to the AICE session ID that produced the draft
- Reviewer identity and review date

### 6.3 Version Control

- AI-generated code is committed under the reviewing engineer's identity (not a bot account)
- The commit message must include: `AI-assisted: CIA session <session_id>`
- The reviewing engineer is the author of record for all downstream accountability

---

## 7. Data Handling

### 7.1 What May Be Sent to AI Tools

| Data Type | Permitted | Tool |
|-----------|----------|------|
| Source code (C/H files) | Yes | AICE, GitHub Copilot Enterprise, GPT4IFX |
| Requirements (SWR, PRQ, SHRQ) | Yes | AICE, GPT4IFX |
| Hardware specifications (register maps) | Yes | AICE (via Ephemeral Sandbox or KG) |
| Test results (JUnit, Polyspace) | Yes | AICE |
| Architecture models (EA exports) | Yes | AICE |
| Build logs, compiler output | Yes | AICE, GitHub Copilot |

### 7.2 What Must NOT Be Sent to AI Tools

| Data Type | Rationale |
|-----------|-----------|
| Customer-specific configurations | Customer confidentiality |
| Employee performance data | GDPR / employment law |
| Pricing, commercial agreements | Business confidentiality |
| Unreleased product roadmaps | Competitive sensitivity |
| Security keys, credentials, certificates | Information security |
| Personal data of any kind | GDPR compliance |

---

## 8. Accountability Framework

### 8.1 Roles and Responsibilities

| Role | Responsibility |
|------|---------------|
| **Development Engineer** | Use AI tools according to this policy; perform required reviews; document AI involvement; report anomalies |
| **Module Lead** | Ensure team compliance; approve review routing overrides; maintain per-module ASIL classification |
| **Safety Manager** | Define ASIL-specific review requirements; review safety-related AI outputs; approve safety analysis workflows |
| **Quality Manager** | Audit AI usage metrics; verify review completeness; ensure ASPICE compliance |
| **AI Governance Lead** | Maintain this policy; track governance metrics; coordinate with regulatory and legal teams |
| **Platform Team (AICE)** | Maintain AICE availability and integrity; implement governance tools; provide metrics and reports |

### 8.2 Escalation Path

1. Engineer identifies AI output quality issue → Reports via FeedbackSink (REJECT)
2. Module Lead reviews rejection patterns → Escalates systematic issues to AI Governance Lead
3. AI Governance Lead investigates → Updates policy, tools, or training as needed
4. Safety Manager involved for any ASIL-B+ quality escapes
5. Quarterly governance review with all stakeholders

---

## 9. Training Requirements

### 9.1 Mandatory Training

All engineers using AICE-backed tools must complete:

| Training | Content | Frequency |
|----------|---------|-----------|
| **AI Usage Policy** | This document; do's and don'ts; review requirements | Upon onboarding; refresher annually |
| **AICE Tool Training** | MCP session lifecycle; search strategies; confidence scoring | Upon onboarding |
| **Review Gate Training** | How to interpret confidence scores; when to escalate; how to provide effective feedback | Upon onboarding |
| **AI Limitations Awareness** | Known failure modes; hallucination patterns; when NOT to trust AI output | Upon onboarding; refresher annually |

### 9.2 Role-Specific Training

| Role | Additional Training |
|------|-------------------|
| Module Lead | Review routing override policies; governance metrics interpretation |
| Safety Engineer | ASIL-specific review requirements; AI in ISO 26262 context |
| AI Governance Lead | EU AI Act requirements; NIST AI RMF; ISO/PAS 8800 principles |

---

## 10. Metrics and Monitoring

### 10.1 Key Governance Metrics

| Metric | Target | Source |
|--------|--------|--------|
| Review Gate bypass rate | 0% | PostgreSQL audit_logs |
| FULL review completion rate for ASIL-B+ | 100% | Review evidence records |
| AI output rejection rate | < 15% (trending down) | FeedbackSink records |
| Time from generation to review completion | < 2 business days | Audit trail timestamps |
| MISRA violations in AI-generated code | < 5% of total findings | Polyspace result processor |
| Coverage gap density (AI-assisted modules) | Improving quarter-over-quarter | Traceability matrix reports |

### 10.2 Quarterly Governance Review

Every quarter, the AI Governance Lead produces a governance report covering:

- Total AI-assisted artifacts produced (by DA, by module, by ASIL level)
- Review routing distribution (AUTO/QUICK/FULL)
- Rejection and escalation patterns
- Confidence score calibration (do scores predict review outcomes?)
- Incident log and corrective actions
- Policy effectiveness assessment and proposed updates

---

## 11. Compliance References

| Standard / Regulation | Relevance | Key Requirements |
|----------------------|-----------|-----------------|
| **EU AI Act** (2024/1689) | Transparency, human oversight, documentation | Art. 13 (transparency), Art. 14 (human oversight), Art. 50 (AI interaction disclosure) |
| **NIST AI RMF 1.0** | Risk management framework | GOVERN, MAP, MEASURE, MANAGE functions |
| **ISO 26262** | Functional safety | Tool qualification (Part 8), traceability (Part 6), verification independence |
| **ISO/PAS 8800** | AI safety in vehicles | AI safety lifecycle principles (adopted for development tool context) |
| **ASPICE 4.0** | Process maturity | SWE.1-SWE.6; audit trail requirements; work product documentation |
| **MISRA C:2012** | Coding standard | Mandatory/required rules; deviation documentation |
| **GDPR** | Data protection | No personal data in AI tools; data minimization |
| **UNECE R155/R156** | Cybersecurity / SW updates | Secure development lifecycle; update management |

---

## 12. Policy Violations

### 12.1 Violation Categories

| Category | Examples | Consequence |
|----------|---------|-------------|
| **Critical** | Committing AI-generated ASIL-B+ code without FULL review; sending personal data to AI tools | Immediate code revert; incident report; management escalation |
| **Major** | Skipping MISRA analysis on AI-generated code; not marking AI-assisted artifacts | Code quarantine pending review; training refresher required |
| **Minor** | Incomplete AI marking in commit messages; delayed review completion | Coaching; process reminder |

### 12.2 Reporting

Policy violations should be reported through:
1. Direct discussion with Module Lead (preferred for minor issues)
2. AI Governance Lead (for systemic issues or policy questions)
3. Quality Management (for ASPICE-related concerns)
4. Safety Management (for safety-related concerns)

---

## 13. Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |

**Approval**:

| Role | Name | Date |
|------|------|------|
| AI Governance Lead | __________ | __________ |
| Safety Manager | __________ | __________ |
| Quality Manager | __________ | __________ |
| Division Head | __________ | __________ |

# AI Usage Policy — Automotive Embedded Software Development

| Field | Value |
|---|---|
| **Document ID** | AICE-GOV-002 |
| **Version** | 3.0.0 |
| **Date** | 2026-05-02 |
| **Classification** | Internal — Infineon Technologies |
| **Scope** | ATV MC D SW VDF and all teams using AICE-backed Domain Assistants |
| **Effective Date** | 2026-05-15 |
| **Owner** | ATV MC D SW VDF |
| **Review Cycle** | Semi-annually |

---

## 1. Purpose

Defines approved uses, restrictions, review requirements, and accountability for AI-assisted development activities in the development of AURIX TC3xx / TC4xx MCAL and iLLD software **delivered to Infineon customers (including German OEMs) up to ASIL-D**, plus FOSS BSP work for Zephyr and NuttX.

---

## 2. Scope

### 2.1 In Scope

- All AICE Domain Assistants (22 DAs): REVA, PRQ Drafter, RMA, SAGA, SAVA, SASA, HAZOPA, DaFaA, CIA, CTA, GECA, GEVT, ACRA, GEST, ATRA, ATQA, TripleA, PAGE, KW, M2MA, ZA, NXA
- **GitHub Copilot Enterprise** — primary LLM for all zones (subject to contractual preconditions in AICE_SYSTEM_CARD §4.3)
- **GPT4IFX** — specialized paths: PDF extraction, RLM planning, fallback, contractually-precluded routing
- **`ifxpyarch` MCP server** — EA model (QEAX) access for MCAL architecture
- Any Infineon-sanctioned AI coding assistant per IT policy
- Any future AI tooling integrated into the V-Model lifecycle

### 2.2 Out of Scope

- General-purpose AI for non-engineering tasks (covered by Infineon IT policy)
- AI usage in non-safety internal tools outside MCAL/iLLD/FOSS-BSP delivery scope
- Research and prototyping marked "non-production" in quarantined environments

---

## 3. Principles

**P1 — Human Accountability.** The human engineer is the responsible author of record for any artifact entering the product baseline.

**P2 — Mandatory Review.** Every AI-generated or AI-assisted artifact undergoes review appropriate to its safety classification. No AUTO for ASIL-B or above.

**P3 — Transparency.** AI involvement in any artifact is documented and traceable. AI-origin markers mandatory.

**P4 — Proportional Controls.** Governance proportional to safety classification and zone (A/B/C).

**P5 — Continuous Improvement.** Usage patterns, failure modes, effectiveness tracked.

**P6 — Data Sovereignty.** Routing follows data classification (AICE_SYSTEM_CARD §4.4).

**P7 — Reproducibility.** Every AI-generated artifact reproducible via provenance chain.

---

## 4. Approved Use Cases

### 4.1 Permitted AI-Assisted Activities

| Activity | Domain Assistant | Primary LLM | AI Role | Human Role |
|---|---|---|---|---|
| Code generation from requirements | **CIA** | Copilot Enterprise | Generate draft C code from SWR + HW specs | Review, verify MISRA/AUTOSAR, run Polyspace, test on HIL, approve |
| SFR / register migration | **CTA** | Copilot Enterprise | Identify and apply register name changes TC3xx→TC4xx | Verify completeness, test on target HW |
| Bugfix analysis | **CIA** (bugfix task) | Copilot Enterprise | Analyze warnings/findings, suggest fixes | Verify correctness, root cause, validate |
| Test specification + test code | **GEST** | Copilot Enterprise | Generate test specs and test code | Verify coverage (MC/DC for ASIL-D), independence, correctness |
| Test quality assessment | **ATQA** | Copilot Enterprise | Evaluate test suite adequacy | Confirm judgment; approve release |
| Requirement review | **REVA** | Copilot Enterprise | Identify ambiguities, gaps, testability | Accept/reject findings, update Jama |
| Requirement drafting | **PRQ Drafter** | Copilot Enterprise | Draft PRQs from SHRQ inputs | Review, refine, formalize in Jama |
| Requirement management | **RMA** | Copilot Enterprise | Trace requirements, identify orphans | Resolve gaps |
| Code review assistance | **ACRA** | Copilot Enterprise | Flag MISRA/AUTOSAR violations, complexity | Final accept/reject |
| Architecture analysis | **SAGA** | Copilot Enterprise + ifxpyarch | Analyze SW architecture for issues | Validate against design intent |
| Architecture verification | **SAVA** | Copilot Enterprise + ifxpyarch | Check arch against requirements | Confirm findings |
| SW arch safety analysis | **SASA** | Copilot Enterprise + ifxpyarch | Draft safety architecture analysis | **Safety expert review mandatory** — FULL + Safety Manager sign-off |
| HAZOP support | **HAZOPA** | Copilot Enterprise + ifxpyarch | Gather interface definitions, guide-word analysis | Conduct formal HAZOP with expert judgment |
| Dataflow / fault analysis | **DaFaA** | Copilot Enterprise + ifxpyarch | Dataflow / fault propagation analysis | Safety expert validation |
| Traceability analysis | **TripleA / ATRA** | Copilot Enterprise | Req → Arch → Code → Test gap detection | Investigate and resolve gaps |
| Config generation | **GECA** | Copilot Enterprise | Generate ARXML / EB Tresos config | Validate parameters, test on target |
| Config verification | **GEVT** | Copilot Enterprise | Verify config against spec | Confirm findings |
| Documentation generation | **PAGE** | Copilot Enterprise | Draft user guides, API ref, safety manuals | Editorial review; legal review for customer-facing |
| Knowledge worker / ingestion | **KW** | Copilot Enterprise + GPT4IFX (PDF) | Guide ingestion operations | Admin approval |
| Zephyr / NuttX BSP | **ZA / NXA** | Copilot Enterprise | BSP code, device tree, kconfig | Dual review: driver + OS integration |
| Model-to-model transformation | **M2MA** | Copilot Enterprise | Model transformation assistance | Validate transformation |
| **EA architecture queries** (cross-cutting) | **All architecture-related DAs** | ifxpyarch MCP | Direct QEAX access for component APIs, configs, safety, deps | Standard review per zone |

### 4.2 Restricted Activities

| Activity | Restriction | Required Controls |
|---|---|---|
| Safety analysis (FMEA, FTA, DFA) | AI may assist with data gathering only | Expert-led analysis; AI output is input data |
| **ASIL-D code generation (mcal)** | AI draft permitted | **FULL + independent + Safety Manager sign-off + complete static analysis pipeline mandatory; no AUTO; no QUICK** |
| Security-sensitive code (crypto, access control, secure boot) | AI draft permitted with caution | Security expert review; no AI for crypto implementations |
| Configuration for ASIL-D | AI draft permitted | Verify constraints; test on target; independent reviewer |
| Inter-module interface code | AI draft permitted | Cross-team review; dependency validation via AICE |
| **Customer-facing documentation / manuals / datasheets** | AI draft permitted with human authorship | **Legal + Marketing review before customer delivery** |
| **Sandbox-grounded ASIL-D generation** | Permitted with extra controls | FULL + independent + Safety Manager + reviewer-verified sandbox content vs authoritative source (`review_evidence.sandbox_source_verified`) |
| **Customer-NDA content via Copilot Enterprise** | Permitted only when AICE_SYSTEM_CARD §4.3 contractual preconditions hold for that customer | Per-customer contract verification; Cerbos enforcement; if precondition fails → route via GPT4IFX |

### 4.3 Prohibited Activities

| Activity | Rationale |
|---|---|
| Autonomous code commit without human review | Violates P1, P2 |
| AI-generated final safety case evidence | Safety case requires engineering judgment |
| Personal data through AI tools | GDPR / DPDP |
| AI outputs replacing independent verification | ISO 26262 Part 6 independence |
| Submitting AI-generated artifacts as formal deliverables without AI-origin disclosure | Violates P3 |
| Unapproved external AI tools on Infineon-confidential or customer-NDA source | Information security |
| Data routing violation (ignoring §4.3 contractual preconditions) | P6 violation; potential customer NDA breach |
| Bypassing ASIL-gated review override for ASIL-D | P2, P4 violation; blocked at tool level |
| **Promoting sandbox content into the persistent KG without admin approval + parser version + new ingestion job** | Bypasses workspace governance; AICE-GOV-010 §C7 |

---

## 5. Review Requirements — Zone-Aware

### 5.0 Zone Reminder

| Zone | Workspace | Highest ASIL | License scope |
|---|---|---|---|
| **A** Productive MCAL | `mcal`, `mcal_customer_X` | **ASIL-D** | IFX-confidential / Customer-NDA |
| **B** iLLD reference SW | `illd` | QM (no ASIL claim) | Infineon Free License (public) |
| **C** FOSS BSP | `foss-bsp` | QM (integrator qualifies as SOUP) | Apache-2.0 / BSD-3 |

### 5.1 Zone A Review Matrix

| ASIL | Code Gen | Test Gen | Requirement Review | Safety Analysis (SASA/HAZOPA/DaFaA) | Config Gen (GECA) |
|---|---|---|---|---|---|
| QM | QUICK | QUICK | AUTO | N/A | QUICK |
| ASIL-A | QUICK | QUICK | QUICK | FULL (expert) | QUICK |
| ASIL-B | FULL | FULL | QUICK | FULL (expert) | FULL |
| ASIL-C | FULL + indep | FULL + indep | FULL | FULL + indep | FULL + indep |
| **ASIL-D** | **FULL + indep + Safety Mgr + full static analysis pipeline** | **FULL + indep + MC/DC** | FULL | **FULL + indep + Safety Mgr** | **FULL + indep + target HW validation** |

**Technical enforcement:** `evaluate_confidence` hard-gates Zone A ASIL-D to FULL + indep + Safety Mgr; confidence score cannot bypass.

**ASIL-D + sandbox-grounded:** generations grounded on sandbox content force FULL + indep + Safety Mgr regardless of confidence; reviewer must verify sandbox content vs authoritative source; `review_evidence.sandbox_source_verified` recorded.

### 5.2 Zone B Review (iLLD)

| Activity | Default review |
|---|---|
| Driver code (CAN, SPI, I2C, etc.) | QUICK |
| Examples, demos | AUTO permitted |
| Header changes, API modifications | QUICK |
| Reference examples for crypto, secure boot, MPU, interrupt vectors | **FULL** (high downstream reuse) |
| Documentation, user manuals | QUICK; PAGE may use AUTO for non-safety sections |

**Zone B mandatory:** FOSS license compliance scan (AICE-GOV-009); Copilot Enterprise "Block matching public code" enabled; AI-origin markers; export-control clearance for public release.

### 5.3 Zone C Review (FOSS BSP)

| Activity | Default review |
|---|---|
| Device tree, kconfig | QUICK |
| BSP driver glue (UART, SPI, I2C, GPIO) | QUICK |
| **FFI-critical** (MPU, interrupt vectors, startup, hypervisor, secure boot) | **FULL + dual reviewer** (BSP specialist + OS integrator) |
| Upstream contribution PR | **FULL + FOSS maintainer review** before submission |

**Zone C mandatory:** FOSS license compliance scan; Copilot "Block matching public code" enabled; upstream license compatibility check (Apache-2.0 for Zephyr; BSD-3 for NuttX); AI-origin markers; DCO sign-off; CLA review (Legal) before first contribution.

**Zone C + sandbox:** generations grounded on non-public sandbox content cannot be contributed upstream (AICE-GOV-009 §5.3 + AICE-GOV-010 §5.2).

### 5.4 Review Type Definitions

| Type | Duration | Reviewer | Activities |
|---|---|---|---|
| AUTO | ~5 min | Any team member | Spot-check; not permitted for ASIL-A+ |
| QUICK | 15–20 min | Developer with module knowledge | Verify correctness, completeness, compliance |
| FULL | 1+ hours | Domain expert | Full correctness, safety, requirement coverage |
| Independent | Additional | Different engineer from generator | ISO 26262 Part 6 independence |
| Safety Manager sign-off (ASIL-D) | Additional | Designated Safety Manager | Confirms safety argument integrity |

### 5.5 Minimum Verification for All AI-Generated Code (Zone A)

1. Clean compile (`-Wall -Werror`)
2. MISRA C:2012 — mandatory + required (Polyspace or equivalent); deviations per process
3. AUTOSAR C++ guidelines for AUTOSAR modules
4. Polyspace Bugfinder — no Red findings
5. Polyspace CodeProver (formal) — required for ASIL-B+; no Red proof results
6. Structural coverage — MC/DC for ASIL-D; branch for ASIL-B/C; statement for ASIL-A
7. Integration test on HIL / ISS for safety-relevant units
8. Human review at required level
9. AI-origin markers applied (§6)

The full pipeline (1–7) is the foundation for the ISO 26262-8 Clause 11 TD claim per AICE-GOV-007. Any gate bypassed invalidates the TD claim for the affected artifact.

---

## 6. AI Output Marking and Traceability

### 6.1 Code Marking

```c
/**
 * @ai_assisted  true
 * @ai_tool      <DA_NAME> vX.Y.Z / AICE v3.0.0
 * @ai_session   <SESSION_ID>
 * @ai_llm       Copilot Enterprise <model_version> [or GPT4IFX <model_version>]
 * @ai_prompt_template_version  <HASH>
 * @ai_kg_snapshot  <CORPUS_VERSION>
 * @ai_sandbox_refs  <comma-separated SHA256s, or "none">
 * @ai_confidence  <SCORE> (<ROUTING>) → final <FINAL_ROUTING> (reason: <...>)
 * @ai_review     FULL | Reviewer: <id> | Independent: <id> | SafetyMgr: <id> | Date: <date>
 * @ai_provenance <response_archive_id>
 *
 * AI-assisted; reviewed/approved per AICE-GOV-002 v3.0.0.
 */
```

### 6.2 Requirement and Test Marking

AI-assisted requirements/test specs in Jama/Polarion include:
- `[AI-ASSISTED]` tag in description
- AICE `session_id` reference
- Reviewer identity and date
- Independent reviewer (ASIL-C/D)

### 6.3 Version Control

- Commits under reviewing engineer's identity (not bot)
- Trailers:
  ```
  AI-Generated-By: <DA_NAME>@<version>
  AI-LLM: copilot-enterprise@<version> | gpt4ifx@<version>
  AI-Session: <SESSION_ID>
  AI-Sandbox-Refs: <SHA256s, or none>
  AI-Confidence: <SCORE>
  AI-Reviewer: <reviewer_id>
  AI-Independent-Reviewer: <id_or_na>
  AI-Provenance: <response_archive_id>
  ```
- Pre-receive hook validates trailer presence/content

---

## 7. Data Handling

### 7.1 Data Classification and Routing

| Data Class | Examples | Copilot Enterprise (with §4.3 preconditions) | GPT4IFX |
|---|---|---|---|
| Public — Infineon-published | github.com/Infineon iLLD, public TC3xx datasheet | ✅ allowed | Optional |
| Public — FOSS upstream | Zephyr, NuttX upstream | ✅ allowed | Optional |
| Internal non-sensitive | Generic MISRA reasoning, embedded C patterns | ✅ allowed | Optional |
| Infineon confidential | Unpublished errata, TC4xx pre-release, iLLD pre-release branches, internal design notes | ✅ via Copilot Enterprise | Available |
| **Customer NDA-restricted** | Customer PRQ marked CONFIDENTIAL, customer variants, customer ARXML | ✅ via Copilot Enterprise (per-customer contract verification required) | Required if customer contract precludes Copilot |
| Personal data (GDPR/DPDP) | Reviewer/developer names | Scrub before prompt | Scrub before prompt |

### 7.2 Enforcement

Data-class routing enforced at MCP layer:
- Deterministic pre-LLM classifier (regex + keyword + ontology tag lookup)
- Cerbos policy (principal + data class → allowed LLM endpoint)
- Violations logged to `governance_incidents`

### 7.3 What Must NOT Be Sent to Any AI Tool

| Data Type | Rationale |
|---|---|
| Customer-specific configurations without proper NDA workspace | Customer confidentiality |
| Employee performance data | GDPR / employment law |
| Pricing, commercial agreements | Business confidentiality |
| Unreleased product roadmaps | Competitive sensitivity |
| Security keys, credentials, certificates | Information security |
| Personal data of any kind | GDPR / DPDP |

---

## 8. Accountability Framework

### 8.1 Roles and Responsibilities

| Role | Responsibility |
|---|---|
| Development Engineer | Use AI per this policy; perform required reviews; document AI involvement; report anomalies |
| Independent Reviewer (ASIL-C/D) | ISO 26262 Part 6 independent review; cannot have participated in generation, prompt, or context |
| Module Lead | Team compliance; review escalations; per-module ASIL classification |
| Safety Manager | ASIL-specific review requirements; sign off ASIL-D AI-generated artifacts; safety-critical incident response |
| Quality Manager | AI usage metric audits; review completeness; ASPICE compliance |
| AI Governance Lead | Policy maintenance; governance metrics; coordination with Legal and regulatory; quarterly review |
| Platform Team (AICE) | AICE availability + integrity; governance tools; metrics and reports |
| GPT4IFX Platform Lead | GPT4IFX availability; Art. 53 GPAI documentation (AICE-GOV-006) |
| AIBOM Owner | Generate and publish AI Bill of Materials per release |
| FOSS Compliance Officer | Zone B/C license compliance (AICE-GOV-009) |
| Customer Interface Lead | Field-incident coordination |
| Legal | GPAI classification; EU AI Act interpretation; Copilot contract preconditions; customer contract review for AI-in-delivery |
| Data Protection Officer | PII scrubber audit; DPIA if PII enters scope |

### 8.2 Escalation Path

1. Engineer identifies AI output quality issue → FeedbackSink REJECT + `submit_human_feedback`
2. Module Lead reviews patterns → escalates systematic issues to AI Governance Lead
3. AI Governance Lead investigates → policy / tool / training update
4. Safety Manager involved for ASIL-B+ quality escape
5. CRITICAL incidents affecting customer code: escalate within 2h to AI Governance Lead + Safety Manager + Legal
6. Quarterly governance review with all stakeholders

---

## 9. Training Requirements

### 9.1 Mandatory (EU AI Act Art. 4)

| Training | Content | Frequency |
|---|---|---|
| EU AI Act AI Literacy | Risk categories, obligations, GPAI, transparency | Onboarding; annual |
| AI Usage Policy | This document | Onboarding; annual |
| AICE Tool Training | MCP session lifecycle; search strategies; confidence scoring | Onboarding |
| Review Gate Training | Confidence interpretation; ASIL-gated override; effective feedback | Onboarding |
| AI Limitations Awareness | Failure modes, hallucination patterns, when NOT to trust AI | Onboarding; annual |
| **Sandbox & ifxpyarch Training** | Sandbox lifecycle, data-class inheritance, sandbox-grounded review, QEAX vs PDF source-of-truth | Onboarding |

### 9.2 Role-Specific

| Role | Additional Training |
|---|---|
| Module Lead | Review escalation; governance metrics |
| Safety Engineer | ASIL-specific requirements; AI in ISO 26262; ISO 26262-8 Clause 11 |
| Safety Manager | ASIL-D sign-off; incident response; Art. 73 reporting |
| Independent Reviewer | Independence criteria; prompt-context assessment |
| AI Governance Lead | EU AI Act full text; NIST AI RMF; VDA AI-in-QM; ISO/IEC 42001 |
| GPT4IFX Platform Lead | GPAI Art. 53; training data documentation |
| Zone C contributor | DCO + AI training (1h) before first Zephyr/NuttX upstream contribution |

---

## 10. Metrics and Monitoring

### 10.1 Key Metrics

| Metric | Target | Source |
|---|---|---|
| Review Gate bypass rate | 0% | audit_logs; ASIL-gated override |
| FULL review completion rate for ASIL-D | 100% | review_evidence |
| ASIL-D Safety Manager sign-off | 100% | review_evidence |
| Static analysis pipeline pass rate (AI-generated code) | ≥ 99% | CI/CD result processor |
| AI output rejection rate | < 15% (trending down) | FeedbackSink |
| Time generation → review completion | < 2 business days | Audit timestamps |
| AI-origin marker coverage on AI-authored commits | 100% | Pre-receive hook |
| Data-class routing violations | 0 | governance_incidents |
| Cross-workspace query attempts | 0 | Cerbos audit |
| Sandbox-grounded ASIL-D generations | All require FULL + indep + Safety Mgr | review_evidence |
| Sandbox `sandbox_source_verified` rate | 100% (when sandbox-grounded) | review_evidence |

### 10.2 Quarterly Governance Review

AI Governance Lead produces quarterly report covering:
- AI-assisted artifacts (by DA, module, ASIL, customer)
- Review routing distribution
- Rejection and escalation patterns
- Confidence score calibration
- Incident log + corrective actions
- Tool qualification evidence completeness (per DA)
- Policy effectiveness
- LLM contract compliance status (Copilot Enterprise §4.3 preconditions)

---

## 11. Compliance References — Zone Applicability

| Standard | Zone A | Zone B | Zone C |
|---|---|---|---|
| EU AI Act (2024/1689) Art. 4, 11, 12, 13, 14, 26, 50, 53, 73 | ✅ Full | ✅ Art. 4, 50, 53 (residual), 73 indirect | ✅ Same as B + upstream transparency |
| NIST AI RMF + GenAI Profile + SP 800-218A | ✅ | ✅ (adapted) | ✅ (adapted) |
| ISO/IEC 42001:2023 | ✅ (target) | ✅ | ✅ |
| ISO 26262 Part 8 Clause 11 | ✅ AICE-GOV-007 | ❌ ("AI Usage Statement") | ❌ (integrator) |
| ISO 26262 Part 6 | ✅ | ❌ | ❌ |
| ISO/SAE 21434 | ✅ | ⚠️ recommended | ⚠️ recommended |
| ISO/PAS 8800:2024 | N/A | N/A | N/A |
| ASPICE 4.0 | ✅ SWE.1–6 + SUP.11 | ⚠️ internal | ⚠️ |
| VDA AI-in-QM Yellow Volume Ch. 7 | ✅ internal method | ⚠️ lighter | ⚠️ lighter |
| MISRA C:2012 | ✅ mandatory | ✅ advisory | ⚠️ FOSS project style |
| AUTOSAR | ✅ | ❌ (iLLD non-AUTOSAR) | ❌ |
| GDPR / DPDP | ✅ | ✅ | ✅ |
| UNECE R155/R156 | ✅ via 21434 | downstream | downstream |
| **FOSS license compliance (AICE-GOV-009)** | ⚠️ low risk | ✅ mandatory | ✅ mandatory |
| **DCO** | N/A | N/A | ✅ (Zephyr, NuttX) |

**Key:** ✅ applies; ⚠️ recommended; ❌ not applicable; N/A not in scope.

---

## 12. Policy Violations and Incidents

### 12.1 Violation Categories

| Category | Examples | Consequence |
|---|---|---|
| Critical | Committing AI-generated ASIL-D without FULL+indep+SafetyMgr; sending customer NDA via non-compliant routing; bypassing PII scrubber; promoting sandbox to KG without admin approval | Code revert; incident report; management escalation; potential disciplinary action |
| Major | Skipping static analysis pipeline; not marking AI-assisted artifacts; using non-sanctioned AI tool | Code quarantine; training refresher |
| Minor | Incomplete AI marking in commit messages; delayed review | Coaching; process reminder |

### 12.2 Incident Reporting

1. Module Lead discussion (minor)
2. FeedbackSink REJECT + `submit_human_feedback` (quality)
3. AI Governance Lead (systemic / policy)
4. Quality Management (ASPICE)
5. Safety Management (ASIL-B+ quality escape)

### 12.3 Serious Incident Reporting (EU AI Act Art. 73)

If AI-assisted artifact (or policy violation) contributes to serious customer field incident:

1. Within 2 hours: notify AI Governance Lead, Safety Manager, Quality Manager, Legal
2. Within 24 hours: field-failure → AI-lineage traceback completed (INCIDENT_RESPONSE §9.3)
3. Within 48 hours (or Art. 73 deadlines — 2 days for death/serious harm; 15 days for widespread/serious): Legal assesses Art. 73 reporting
4. Coordinate with OEM customer via standard incident channels
5. Root cause and corrective action per INCIDENT_RESPONSE

---

## 13. Document Control

| Field | Value |
|---|---|
| Current version | 3.0.0 |
| Effective date | 2026-05-15 |
| Supersedes | All prior versions |

### Version 3.0.0 — Material Changes

- **Copilot Enterprise primary LLM** for all zones including Customer-NDA (per AICE_SYSTEM_CARD §4.3 preconditions)
- **GPT4IFX retained** for specialized paths (PDF extraction, RLM, fallback, contractually-precluded routing)
- **Sandbox + ifxpyarch implementation** acknowledged; new mandatory training; new commit trailer fields (`AI-Sandbox-Refs`, `AI-LLM`)
- **Customer-NDA via Copilot Enterprise** explicitly permitted under §4.3 preconditions
- **Sandbox-grounded ASIL-D special rule** (§4.2, §5.1)
- Legacy version-history consolidated

### Approval

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| Safety Manager | __________ | __________ |
| Quality Manager | __________ | __________ |
| Legal (Copilot preconditions + customer contracts + GPAI + Art. 73) | __________ | __________ |
| Division Head | __________ | __________ |

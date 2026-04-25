# AI Usage Policy — Automotive Embedded Software Development

**Document ID**: AICE-GOV-002
**Version**: 2.1.0 (supersedes v2.0.0 of 2026-04-18)
**Classification**: Internal — Infineon Technologies
**Scope**: ATV MC D SW VDF and all teams using AICE-backed Domain Assistants
**Effective Date**: 2026-05-01 (v2.0.0 baseline); v2.1.0 refinement effective 2026-04-20
**Owner**: ATV MC D SW VDF
**Review Cycle**: Semi-annually

---

## 0. Changes

### v2.1.0 (2026-04-20) delta
Formalizes **governance scope zones** (Zone A mcal productive, Zone B illd reference, Zone C foss-bsp). §5 review matrix now zone-aware; §7 data routing clarified for public iLLD content; §11 compliance references given zone applicability. New obligation: **FOSS license compliance scan** for Zone B/C (see AICE-GOV-009). Copilot Enterprise "Block matching public code" setting mandated for Zone B/C.

### v2.0.0 (2026-04-18) delta from v1.0.0

| Area | v1.0.0 | v2.0.0 | Why |
|---|---|---|---|
| ASIL target | ASIL-B treated as highest-addressed | **ASIL-D explicit target** with FULL + independent + safety-manager sign-off | mcal workspace target confirmed as ASIL-D |
| DA list (§2.1, §4.1) | 21 DAs, some missing from the roster | Expanded to 22+ (adds SASA, DaFaA, GECA, GEVT, PRQ Drafter, RMA, TripleA, M2MA, ZA, NXA properly; drops some stale names like "HZOP" → "HAZOPA", "VoltAI" retained) | Aligned with AICE architecture deck v1.2 |
| Data-routing (§7) | General "permitted/not permitted" | **Explicit data-class matrix (Public/Internal/Confidential/NDA) × Copilot/GPT4IFX routing** | Customer NDA content must not leave on-prem |
| GPAI provider obligations | Not mentioned | **New §14 — Infineon as GPAI provider for GPT4IFX** | GPT4IFX on-prem confirmed |
| VDA reference | Generic | **VDA AI-in-QM Yellow Volume Ch. 7 adopted as internal method** | Commercially binding for German OEMs |
| ISO/PAS 8800 | Listed as applicable | **Not applicable** to dev-tool AI per standard's own scope exclusion | 8800:2024 §0 exclusion |
| Claude Code | Listed as approved | **Removed** unless officially sanctioned — replace with "any Infineon-sanctioned AI coding tool per IT policy" | Ambiguity |
| Static analysis (§5.3) | "MISRA + Polyspace minimum" | **Full pipeline: MISRA+AUTOSAR+Polyspace Bugfinder+Polyspace CodeProver+MC/DC for ASIL-D, all mandatory** | Confirms existing practice; basis for TD1 claim |
| Incident reporting (§12) | Internal only | **EU AI Act Art. 73 serious-incident reporting flow** added | Regulatory obligation |
| China (§11) | Listed | **De-scoped** | No China developers/data |

---

## 1. Purpose

This policy defines the approved uses, restrictions, review requirements, and accountability framework for AI-assisted development activities within the Infineon automotive embedded software development organization. It applies to all engineers, leads, and managers using AICE-backed Domain Assistants or any AI-assisted tooling (GitHub Copilot Enterprise, GPT4IFX, Infineon-sanctioned AI coding tools per IT policy) in the development of AURIX TC3xx / TC4xx MCAL and iLLD software **delivered to Infineon customers (including German OEMs) up to ASIL-D**.

---

## 2. Scope and Applicability

### 2.1 In Scope

- All AICE Domain Assistants: **REVA, PRQ Drafter, RMA, SAGA, SAVA, SASA, HAZOPA, DaFaA, CIA, CTA, GECA, GEVT, ACRA, GEST, ATRA, ATQA, TripleA, PAGE, KW, M2MA, ZA, NXA** (22 DAs as of April 2026; list to be kept in sync with architecture deck)
- GitHub Copilot Enterprise (code completion, chat) — subject to data-class routing rules (§7)
- GPT4IFX API usage for engineering tasks (Infineon-hosted on-prem)
- Any Infineon-sanctioned AI coding assistant used in the development workflow
- Any future AI tooling integrated into the V-Model lifecycle

### 2.2 Out of Scope

- General-purpose AI usage for non-engineering tasks (email, presentations, scheduling) — covered by separate Infineon IT policy
- AI usage in non-safety-related internal tools outside the MCAL/iLLD delivery scope
- Research and prototyping activities explicitly marked as "non-production" and using quarantined environments

---

## 3. Principles

**P1 — Human Accountability.** The human engineer is always the responsible author of record for any artifact (code, test, requirement, analysis) that enters the product baseline. AI is a tool; the engineer is accountable.

**P2 — Mandatory Review.** Every AI-generated or AI-assisted artifact must undergo review appropriate to its safety classification before integration into the product baseline. No AUTO for ASIL-B or above.

**P3 — Transparency.** AI involvement in any development artifact must be documented and traceable. No AI-generated content may be represented as purely human-authored. AI-origin markers are mandatory.

**P4 — Proportional Controls.** Governance controls are proportional to the safety classification (ASIL level) and the artifact type. ASIL-D demands the strictest controls.

**P5 — Continuous Improvement.** AI usage patterns, failure modes, and effectiveness metrics are tracked and used to improve the tooling and governance.

**P6 — Data Sovereignty.** Infineon-confidential and customer-NDA content stays on-prem (GPT4IFX). External cloud LLMs (Copilot Enterprise) are permitted only for non-sensitive content per the data-class matrix.

**P7 — Reproducibility.** Every AI-generated artifact must be reproducible via its provenance chain (model version, prompt template version, KG snapshot, reviewer identity).

---

## 4. Approved Use Cases

### 4.1 Permitted AI-Assisted Activities

| Activity | Domain Assistant(s) | Primary LLM | AI Role | Human Role |
|---|---|---|---|---|
| Code generation from requirements | **CIA** | GPT4IFX (mcal) / Copilot (illd) | Generate draft C code from SWR + HW specs | Review, verify MISRA/AUTOSAR compliance, run Polyspace, test on HIL, approve |
| SFR / register migration | **CTA** | GPT4IFX | Identify and apply register name changes TC3xx→TC4xx | Verify completeness, test on target HW, check silicon errata |
| Bugfix analysis | **CIA** (bugfix task) | GPT4IFX | Analyze warnings/findings, suggest fixes | Verify correctness, understand root cause, validate on HIL |
| Test specification + test code generation | **GEST** | GPT4IFX | Generate test specs and unit/integration test code | Verify coverage (MC/DC for ASIL-D), independence from implementation, correctness |
| Test quality assessment | **ATQA** | GPT4IFX | Evaluate test suite adequacy | Confirm judgment; approve release |
| Requirement review | **REVA** | GPT4IFX | Identify ambiguities, gaps, testability issues | Accept/reject findings, update requirements in Jama |
| Requirement drafting | **PRQ Drafter** | GPT4IFX | Draft PRQs from SHRQ inputs | Review, refine, formalize in Jama |
| Requirement management | **RMA** | GPT4IFX | Trace requirements, identify orphans, coverage gaps | Resolve gaps |
| Code review assistance | **ACRA** | GPT4IFX | Flag MISRA/AUTOSAR violations, complexity | Make final accept/reject |
| Architecture analysis | **SAGA** | GPT4IFX | Analyze SW architecture for design issues | Validate against design intent |
| Architecture verification | **SAVA** | GPT4IFX | Check arch against requirements | Confirm findings |
| SW arch safety analysis | **SASA** | GPT4IFX | Draft safety architecture analysis | **Safety expert review mandatory** — FULL + Safety Manager sign-off |
| HAZOP support | **HAZOPA** | GPT4IFX | Gather interface definitions, guide word analysis | Conduct formal HAZOP session with expert judgment |
| Dataflow / fault analysis | **DaFaA** | GPT4IFX | Dataflow analysis, fault propagation analysis | Safety expert validation |
| Traceability analysis | **TripleA / ATRA** | GPT4IFX | Req → Arch → Code → Test coverage gap detection | Investigate and resolve gaps |
| Config generation | **GECA** | GPT4IFX | Generate ARXML / EB Tresos config | Validate parameter constraints, test on target |
| Config verification | **GEVT** | GPT4IFX | Verify config against spec | Confirm findings |
| Documentation generation | **PAGE** | Copilot (non-sensitive) / GPT4IFX (sensitive) | Draft user guides, API reference, safety manuals | Editorial review; legal review for customer-facing |
| Knowledge worker / ingestion support | **KW** | GPT4IFX | Guide ingestion operations | Admin approval |
| Zephyr / NuttX BSP support | **ZA / NXA** | GPT4IFX | BSP code gen, device tree, kconfig | Dual review: driver correctness + OS integration |
| Model-to-model transformation | **M2MA** | GPT4IFX | Model transformation assistance | Validate transformation correctness |

### 4.2 Restricted Activities (Additional Controls Required)

| Activity | Restriction | Required Controls |
|---|---|---|
| Safety analysis (FMEA, FTA, DFA) | AI may assist with data gathering only | Expert-led analysis; AI output is input data, not conclusion |
| **ASIL-D code generation (mcal workspace)** | AI draft permitted | **FULL review + independent reviewer + Safety Manager sign-off + complete static analysis pipeline (MISRA+AUTOSAR+Polyspace Bugfinder+CodeProver+MC/DC) mandatory; no AUTO; no QUICK** |
| Security-sensitive code (crypto, access control, secure boot) | AI draft permitted with caution | Security expert review; no AI for crypto implementations |
| Configuration generation for ASIL-D | AI draft permitted | Verify parameter constraints; test on target; independent reviewer for safety-relevant config |
| Inter-module interface code | AI draft permitted | Cross-team review required; dependency validation via AICE |
| **Customer-facing documentation / manuals / datasheets** | AI draft permitted with human authorship | **Legal + Marketing review before customer delivery**; author of record is the human |

### 4.3 Prohibited Activities

| Activity | Rationale |
|---|---|
| Autonomous code commit without human review | Violates P1, P2 |
| Using AI to generate final safety case evidence | Safety cases require engineering judgment; AI output may be input only |
| Processing customer data or personal data through AI tools | GDPR / DPDP compliance; AI tools not approved for personal data |
| Using AI outputs to replace independent verification | ISO 26262 Part 6 requires independence |
| Submitting AI-generated artifacts as formal deliverables without AI-origin disclosure | Violates P3 |
| Using unapproved / external AI tools on Infineon-confidential or customer-NDA source code | Information security; only GPT4IFX allowed for confidential content |
| **Sending customer NDA / IFX-confidential content to GitHub Copilot Enterprise** | **Data sovereignty (P6); must route via GPT4IFX on-prem** |
| **Bypassing ASIL-gated review override for ASIL-D artifacts** | Violates P2, P4; blocked at tool level |

---

## 5. Review Requirements — Zone-Aware (REVISED — v2.1.0)

### 5.0 Governance Scope Zones

This policy applies three zones with proportional controls (see AICE_SYSTEM_CARD §3.2a):

| Zone | Workspace | Highest ASIL | License scope |
|---|---|---|---|
| **A** Productive MCAL | `mcal`, `mcal_customer_X` | ASIL-D | IFX-confidential / Customer-NDA |
| **B** iLLD reference SW | `illd` | QM (no ASIL) | Infineon Free License (public on GitHub) |
| **C** FOSS BSP (Zephyr / NuttX) | `foss-bsp` | QM (integrator qualifies as SOUP) | Apache-2.0 / BSD-3 |

### 5.1 Zone A — Productive MCAL (ASIL-aware matrix)

| ASIL | Code Generation | Test Generation | Requirement Review | Safety Analysis (SASA/HAZOPA/DaFaA) | Config Gen (GECA) |
|---|---|---|---|---|---|
| **QM** | QUICK review | QUICK review | AUTO permitted | N/A | QUICK |
| **ASIL-A** | QUICK review | QUICK review | QUICK review | FULL review (expert) | QUICK |
| **ASIL-B** | FULL review | FULL review | QUICK review | FULL review (expert) | FULL |
| **ASIL-C** | FULL + independent | FULL + independent | FULL review | FULL + independent | FULL + independent |
| **ASIL-D** ⬅ mcal target | **FULL + independent + safety manager sign-off + mandatory full static analysis pipeline** | **FULL + independent + MC/DC evidence** | FULL review | **FULL + independent + safety manager sign-off** | **FULL + independent + target HW validation** |

**Technical enforcement (v2.0.0):** The review gate `evaluate_confidence` tool **hard-gates** Zone A ASIL-D artifacts to FULL + independent + safety manager — the confidence score cannot bypass this. See AICE_SYSTEM_CARD §6.2.

### 5.1a Zone B — iLLD Reference SW (NEW)

| Activity | Default review |
|---|---|
| Driver code additions (standard peripherals: CAN, SPI, I2C, etc.) | QUICK |
| Example applications, demos | AUTO permitted |
| Header changes, API modifications | QUICK |
| Reference examples for crypto, secure boot, MPU setup, interrupt vectors | **FULL** (high downstream reuse; errors amplify across customers) |
| Documentation, user manuals for iLLD | QUICK; PAGE DA may use AUTO for non-safety sections |

**Zone B additional requirements (all mandatory):**
1. **FOSS license compliance scan** before merge (see AICE-GOV-009)
2. **Copilot Enterprise "Block matching public code" setting: ENABLED** for all contributors
3. **AI-origin markers** (code header + git trailer) — iLLD is published; transparency is more important, not less
4. **Export-control clearance** for public release (standard Infineon process)

### 5.1b Zone C — FOSS BSP (Zephyr / NuttX)

| Activity | Default review |
|---|---|
| Device tree additions, kconfig fragments | QUICK |
| BSP driver glue (UART, SPI, I2C, GPIO) | QUICK |
| **FFI-critical BSP areas** — MPU config, interrupt vectors, startup code, hypervisor config, secure boot | **FULL + dual reviewer** (one BSP specialist + one OS integrator) |
| Upstream contribution PR (back to Zephyr / NuttX project) | **FULL + FOSS maintainer review** before submission |

**Zone C additional requirements (all mandatory):**
1. **FOSS license compliance scan** (see AICE-GOV-009)
2. **Copilot Enterprise "Block matching public code" setting: ENABLED**
3. **Upstream license compatibility check** — Zephyr is Apache-2.0; NuttX is BSD-3. Generated code must be compatible. Cannot contribute GPL-derived content upstream.
4. **AI-origin markers** — even more critical for upstream contribution (Zephyr and NuttX maintainers may request disclosure)
5. **DCO (Developer Certificate of Origin) sign-off** required by both projects; engineers must verify AI-assisted code meets DCO claims (they can certify the code because AI is not an "author" under DCO)
6. **CLA review** if the upstream project requires one — Infineon Legal must confirm before first contribution

### 5.2 Review Type Definitions (unchanged)

| Type | Duration | Reviewer | Activities |
|---|---|---|---|
| **AUTO** | ~5 min | Any team member | Spot-check format, verify no obvious errors. **Not permitted for ASIL-A and above in this policy.** |
| **QUICK** | 15–20 min | Developer with module knowledge | Verify correctness, completeness, compliance |
| **FULL** | 1+ hours | Domain expert | Full correctness, safety analysis, requirement coverage, compliance audit |
| **Independent** | Additional reviewer | Different engineer from generator | ISO 26262 Part 6 independence; reviewer did not participate in generation, prompt selection, or context assembly for this artifact |
| **Safety Manager sign-off** (ASIL-D) | Additional | Designated Safety Manager | Confirms safety argument integrity; documented in review_evidence with identity |

### 5.3 Minimum Verification for All AI-Generated Code (REVISED)

Regardless of ASIL level, ALL AI-generated C code must pass:

1. **Clean compile** with target compiler (GCC/Tasking) with `-Wall -Werror`
2. **MISRA C:2012 analysis** — all mandatory and required rules (Polyspace or equivalent). Deviations documented per MISRA deviation process.
3. **AUTOSAR C++ guidelines** for AUTOSAR modules
4. **Polyspace Bugfinder** — no unresolved Red findings
5. **Polyspace CodeProver** (formal) — required for ASIL-B+; no unresolved Red proof results
6. **Structural coverage** — MC/DC for ASIL-D; branch for ASIL-B/C; statement minimum for ASIL-A
7. **Integration test** on HIL / ISS for safety-relevant units
8. **Human review** at required level per §5.1
9. **AI-origin markers** applied (§6)

**The full pipeline (items 1–7) provides the foundation for the ISO 26262-8 Clause 11 Tool Detection (TD) claim in AICE-GOV-007. Any gate bypassed invalidates the TD claim for the affected artifact.**

---

## 6. AI Output Marking and Traceability (REVISED)

### 6.1 Code Marking

All AI-generated or AI-assisted C/C++/H files must include a header comment injected automatically by the DA postprocessor:

```c
/**
 * @ai_assisted  true
 * @ai_tool      <DA_NAME> vX.Y.Z / AICE v2.1.0
 * @ai_session   <SESSION_ID>
 * @ai_llm       GPT4IFX <model_version> / Copilot Enterprise (if applicable)
 * @ai_prompt_template_version  <HASH>
 * @ai_kg_snapshot  <CORPUS_VERSION>
 * @ai_confidence  <SCORE> (<ROUTING>) → overridden to <FINAL_ROUTING> (reason: <...>)
 * @ai_review     FULL | Reviewer: <engineer_id> | Independent: <engineer_id> | SafetyMgr: <id> | Date: <date>
 * @ai_provenance <response_archive_id>
 *
 * This file was generated with AI assistance and reviewed/approved per AICE-GOV-002 policy.
 */
```

### 6.2 Requirement and Test Marking

AI-assisted requirements and test specs in Jama/Polarion include:
- `[AI-ASSISTED]` tag in artifact description field
- Reference to AICE `session_id` that produced the draft
- Reviewer identity and review date
- Independent reviewer (for ASIL-C/D) if applicable

### 6.3 Version Control

- AI-generated code is committed under the **reviewing engineer's identity** (not a bot account)
- Commit message includes:
  ```
  <your normal subject line>

  AI-Generated-By: <DA_NAME>@<version>
  AI-Session: <SESSION_ID>
  AI-Confidence: <SCORE>
  AI-Reviewer: <reviewer_id>
  AI-Independent-Reviewer: <id_or_na>
  AI-Provenance: <response_archive_id>
  ```
- A pre-receive hook validates trailer presence and content for touched files

---

## 7. Data Handling (REVISED — v2.0.0)

### 7.1 Data Classification and Routing — Zone-Aware

| Data Class | Examples | GitHub Copilot Enterprise | GPT4IFX (on-prem) |
|---|---|---|---|
| **Public** | Published AUTOSAR standard text, published TC3xx datasheet, **published iLLD source** (github.com/Infineon), **Zephyr upstream**, **NuttX upstream** | ✅ Allowed (Copilot "Block matching public code" setting must be enabled for Zone B/C contributors) | Optional |
| **Internal non-sensitive** | Generic MISRA reasoning, embedded C patterns | ✅ Allowed | Optional |
| **Infineon confidential** | Unpublished errata, TC4xx pre-release, **iLLD pre-release branches**, internal design notes, proprietary ontology | ❌ Not permitted | **Required** |
| **Customer NDA-restricted** | Jama PRQ marked CUSTOMER-CONFIDENTIAL, customer-specific variants, customer ARXML | ❌ Not permitted | **Required**, per-customer workspace isolation |
| **Personal data (GDPR/DPDP)** | Developer name, email, reviewer identity in prompts | Scrub before prompt (both paths) | Scrub before prompt |

### 7.2 Enforcement

Data-class routing is enforced at the MCP layer via:
- Deterministic pre-LLM classifier (regex + keyword + ontology tag lookup)
- Cerbos policy (principal + data class → allowed LLM endpoint)
- Violations logged to governance_incidents

### 7.3 What Must NOT Be Sent to Any AI Tool

| Data Type | Rationale |
|---|---|
| Customer-specific configurations without NDA workspace | Customer confidentiality; even internal LLM needs proper workspace |
| Employee performance data | GDPR / employment law |
| Pricing, commercial agreements | Business confidentiality |
| Unreleased product roadmaps | Competitive sensitivity |
| Security keys, credentials, certificates | Information security |
| Personal data (customer or employee) of any kind | GDPR / DPDP compliance |

---

## 8. Accountability Framework

### 8.1 Roles and Responsibilities

| Role | Responsibility |
|---|---|
| **Development Engineer** | Use AI tools per this policy; perform required reviews; document AI involvement; report anomalies |
| **Independent Reviewer** (ASIL-C/D) | ISO 26262 Part 6 independent review; cannot have participated in generation, prompt, or context for this artifact |
| **Module Lead** | Ensure team compliance; approve review routing escalations; maintain per-module ASIL classification |
| **Safety Manager** | Define ASIL-specific review requirements; sign off ASIL-D AI-generated artifacts; coordinate safety-critical incident response |
| **Quality Manager** | Audit AI usage metrics; verify review completeness; ensure ASPICE compliance |
| **AI Governance Lead** | Maintain this policy; track governance metrics; coordinate with Legal and regulatory; own quarterly governance review |
| **Platform Team (AICE)** | Maintain AICE availability and integrity; implement governance tools; provide metrics and reports |
| **GPT4IFX Platform Lead (new)** | Maintain GPT4IFX; produce Art. 53 GPAI provider documentation (see AICE-GOV-006) |
| **AIBOM Owner (new — to be assigned)** | Generate and publish AI Bill of Materials per release; currently unassigned, action required |
| **Legal** | GPAI classification confirmation; EU AI Act interpretation; customer contract review for AI-in-delivery disclosure |
| **Data Protection Officer** | PII scrubber audit; DPIA if PII ever enters scope |

### 8.2 Escalation Path

1. Engineer identifies AI output quality issue → FeedbackSink (REJECT) + `submit_human_feedback`
2. Module Lead reviews rejection patterns → Escalates systematic issues to AI Governance Lead
3. AI Governance Lead investigates → Policy / tool / training update
4. Safety Manager involved for any ASIL-B+ quality escape
5. For CRITICAL incidents affecting customer-delivered code: **Escalate within 2h** to AI Governance Lead + Safety Manager + Legal (for potential Art. 73 reporting)
6. Quarterly governance review with all stakeholders

---

## 9. Training Requirements

### 9.1 Mandatory Training (EU AI Act Art. 4 — AI Literacy)

All engineers using AICE-backed tools must complete:

| Training | Content | Frequency |
|---|---|---|
| **EU AI Act AI Literacy (Art. 4)** | Risk categories, obligations, GPAI, transparency | Upon onboarding; annual refresher |
| **AI Usage Policy** | This document; do's and don'ts; review requirements | Upon onboarding; annual refresher |
| **AICE Tool Training** | MCP session lifecycle; search strategies; confidence scoring | Upon onboarding |
| **Review Gate Training** | How to interpret confidence scores; ASIL-gated override; effective feedback | Upon onboarding |
| **AI Limitations Awareness** | Failure modes, hallucination patterns, when NOT to trust AI | Upon onboarding; annual refresher |

### 9.2 Role-Specific Training

| Role | Additional Training |
|---|---|
| Module Lead | Review escalation policies; governance metrics |
| Safety Engineer | ASIL-specific requirements; AI in ISO 26262 context; ISO 26262-8 Clause 11 tool qualification |
| Safety Manager | ASIL-D sign-off; incident response; Art. 73 reporting |
| Independent Reviewer | Independence criteria; prompt-context assessment |
| AI Governance Lead | EU AI Act full text; NIST AI RMF; VDA AI-in-QM Yellow Volume; ISO/IEC 42001 AIMS |
| GPT4IFX Platform Lead | GPAI Art. 53 obligations; training data documentation |

---

## 10. Metrics and Monitoring

### 10.1 Key Governance Metrics

| Metric | Target | Source |
|---|---|---|
| Review Gate bypass rate | 0% | `audit_logs`; ASIL-gated override enforcement |
| FULL review completion rate for ASIL-D | 100% | review_evidence |
| ASIL-D artifacts with Safety Manager sign-off | 100% | review_evidence |
| Static analysis pipeline pass rate on AI-generated code | ≥ 99% (expected — if lower, static analysis is effectively a review gate) | CI/CD result processor |
| AI output rejection rate | < 15% (trending down) | FeedbackSink |
| Time from generation to review completion | < 2 business days | Audit timestamps |
| AI-origin marker coverage on AI-authored commits | 100% | Pre-receive hook metrics |
| Data-class routing violations | 0 | governance_incidents |
| Cross-workspace query attempts | 0 | Cerbos audit |

### 10.2 Quarterly Governance Review

Every quarter, AI Governance Lead produces a report covering:
- AI-assisted artifacts produced (by DA, by module, by ASIL, by customer)
- Review routing distribution (AUTO/QUICK/FULL); ASIL-D deviations if any
- Rejection and escalation patterns
- Confidence score calibration (do scores predict outcomes?)
- Incident log + corrective actions
- Tool qualification evidence completeness (per DA)
- Policy effectiveness and proposed updates

---

## 11. Compliance References — Zone Applicability

| Standard / Regulation | Relevance | Zone A (mcal) | Zone B (illd) | Zone C (foss-bsp) |
|---|---|---|---|---|
| **EU AI Act (2024/1689)** | AI governance (Art. 4, 11, 12, 13, 14, 50, 53, 73) | ✅ Full | ✅ Art. 4, 50, 53 apply (literacy, transparency, GPAI); Art. 73 indirect | ✅ Same as B + upstream contribution transparency |
| **NIST AI RMF 1.0 + GenAI Profile + SP 800-218A** | Trustworthy AI; GenAI-specific controls | ✅ | ✅ (adapted) | ✅ (adapted) |
| **ISO/IEC 42001:2023** | AIMS | ✅ (certification target) | ✅ (in scope — covered by same AIMS) | ✅ |
| **ISO 26262 Part 8 Clause 11** | Tool qualification | ✅ (see AICE-GOV-007) | ❌ (no ASIL target; "AI Usage Statement" only) | ❌ (integrator burden) |
| **ISO 26262 Part 6** | Software dev + independence | ✅ | ❌ | ❌ |
| **ISO/SAE 21434** | Cybersecurity | ✅ | ⚠️ (recommended practice) | ⚠️ (recommended practice) |
| **ISO/PAS 8800:2024** | Road vehicles — Safety and AI | N/A (AICE out of scope of std) | N/A | N/A |
| **ISO/IEC TS 22440** | AI functional safety | Watch-list | N/A | N/A |
| **ASPICE 4.0** | Process maturity | ✅ (SWE.1–6 + SUP.11) | ⚠️ (work products internal; not capability-assessed) | ⚠️ |
| **VDA AI-in-QM Yellow Volume Ch. 7** | Risk-based assessment of AI dev tools | ✅ (internal method) | ⚠️ (lighter application) | ⚠️ (lighter) |
| **MISRA C:2012** | Coding standard | ✅ (mandatory all AI-gen C) | ✅ (advisory; iLLD is permissive MISRA) | ⚠️ (per FOSS project style: Zephyr has its own style; NuttX follows K&R-ish) |
| **AUTOSAR CP/Adaptive** | Architecture | ✅ | ❌ (iLLD is non-AUTOSAR by design) | ❌ |
| **GDPR / DPDP** | Personal data | ✅ | ✅ | ✅ |
| **UNECE R155/R156** | Cybersecurity / SW updates | ✅ (via ISO/SAE 21434) | Downstream integrator concern | Downstream integrator concern |
| **FOSS license compliance (Apache-2.0, BSD-3, MIT, GPL identification)** (NEW) | License contamination risk | ⚠️ (low risk; controls usually sufficient) | ✅ (**mandatory** — AICE-GOV-009) | ✅ (**mandatory** — AICE-GOV-009; upstream compatibility critical) |
| **DCO (Developer Certificate of Origin)** (NEW) | Upstream contribution | N/A | N/A | ✅ (Zephyr, NuttX both require) |
| **CLA (Contributor License Agreement)** (NEW) | Upstream contribution | N/A | N/A | Zephyr: no; NuttX: no (both use DCO) — check if any sub-project differs |

**Key:** ✅ applies; ⚠️ recommended practice, not strict; ❌ not applicable; N/A not in scope

---

## 12. Policy Violations and Incidents

### 12.1 Violation Categories

| Category | Examples | Consequence |
|---|---|---|
| **Critical** | Committing AI-generated ASIL-D code without FULL + independent + safety-manager sign-off; sending customer NDA data to Copilot; bypassing PII scrubber | Immediate code revert; incident report; management escalation; potential disciplinary action |
| **Major** | Skipping static analysis pipeline gate; not marking AI-assisted artifacts; using non-sanctioned AI tool | Code quarantine pending review; training refresher |
| **Minor** | Incomplete AI marking in commit messages; delayed review | Coaching; process reminder |

### 12.2 Incident Reporting (REVISED)

Policy violations and AI output incidents are reported through:
1. Direct discussion with Module Lead (for minor issues)
2. FeedbackSink REJECT + `submit_human_feedback` (for quality issues)
3. AI Governance Lead (for systemic issues or policy questions)
4. Quality Management (for ASPICE concerns)
5. Safety Management (for ASIL-B+ quality escapes)

### 12.3 Serious Incident Reporting (EU AI Act Art. 73) — NEW in v2.0.0

If an AI-assisted artifact (or failure to follow this policy) contributes to a serious field incident in customer-delivered software:

1. **Within 2 hours**: notify AI Governance Lead, Safety Manager, Quality Manager, Legal
2. **Within 24 hours**: field-failure → AI-lineage traceback completed (INCIDENT_RESPONSE §9.3)
3. **Within 48 hours (or Art. 73 deadlines — 2 days for death/serious harm, 15 days for widespread/serious)**: Legal assesses Art. 73 reporting obligation
4. **Coordinate with OEM customer** via standard incident-response channels
5. **Root cause and corrective action** per INCIDENT_RESPONSE v2.0.0

---

## 13. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |
| 2.0.0 | 2026-04-18 | ATV MC D SW VDF | ASIL-D explicit; DA list updated to 22+; data-class routing matrix; GPAI provider obligations reference; VDA Ch.7 adopted; ISO/PAS 8800 de-scoped; Art. 73 incident reporting; Claude Code removed (ambiguity); Independent Reviewer role formalized |
| **2.1.0** | **2026-04-20** | **ATV MC D SW VDF** | **Zone-aware governance (Zone A/B/C). §5 review matrix split by zone. §7 data routing clarified (published iLLD is public). §11 compliance references get zone-applicability column. New obligations for Zone B/C: FOSS license scan (AICE-GOV-009), Copilot "Block matching public code" enforced, DCO sign-off for upstream contributions** |

**Approval**:

| Role | Name | Date |
|------|------|------|
| AI Governance Lead | __________ | __________ |
| Safety Manager | __________ | __________ |
| Quality Manager | __________ | __________ |
| Legal (GPAI + Art. 73) | __________ | __________ |
| Division Head | __________ | __________ |

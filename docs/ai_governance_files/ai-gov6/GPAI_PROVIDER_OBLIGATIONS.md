# GPAI Provider Obligations — GPT4IFX Hosting at Infineon

**Document ID**: AICE-GOV-006
**Version**: 1.0.0
**Classification**: Internal — Infineon Technologies
**Owner**: AI Governance Lead + GPT4IFX Platform Lead + Legal
**Last Updated**: 2026-04-18
**Status**: DRAFT — requires Legal confirmation of GPAI classification

---

## 1. Purpose

This document analyzes Infineon's potential status as a **General-Purpose AI (GPAI) provider** under EU AI Act Art. 3(63) and Art. 53 as a consequence of hosting GPT4IFX on Infineon-owned on-premises infrastructure, and specifies the obligations and operational controls that follow.

---

## 2. Scope

- GPT4IFX as hosted at Infineon (on-prem, internal use)
- Any downstream distribution of GPT4IFX-derived outputs (AICE, DAs, customer deliveries)
- NOT in scope: third-party GPAI models used via external APIs (GitHub Copilot Enterprise is Microsoft's responsibility as provider)

---

## 3. The Legal Question

### 3.1 EU AI Act definitions (Art. 3)

- **(63) "general-purpose AI model":** an AI model that displays significant generality and is capable of competently performing a wide range of distinct tasks, regardless of the way the model is placed on the market, and that can be integrated into a variety of downstream systems or applications
- **(3) "provider":** a natural or legal person that develops an AI model or an AI system, or that has an AI model or an AI system developed and **places them on the market or puts them into service under its own name or trademark**, whether for payment or free of charge
- **"Putting into service":** making available for first use directly to the user or for own use

### 3.2 Application to GPT4IFX

| Criterion | Assessment |
|---|---|
| Is GPT4IFX a GPAI? | **Likely yes.** LLMs of the class used for code generation, PDF extraction, and RLM planning meet the "significant generality" threshold |
| Does Infineon develop or have GPT4IFX developed? | **Likely no** — GPT4IFX is presumably a third-party foundation model (OpenAI GPT-4-class or similar) deployed on Infineon infrastructure. This is the critical open question for Legal |
| Does Infineon "put GPT4IFX into service under its own name"? | **Likely yes, for internal use.** "Putting into service for own use" captures the scenario |
| Is the model "made available on the market"? | **Depends.** If GPT4IFX is only used internally within Infineon and never exposed to external users or customers, the obligation landscape is different from external distribution |

### 3.3 Three possible legal scenarios

| Scenario | Description | Obligations |
|---|---|---|
| **A. Pure internal deployer of 3rd-party GPAI** | Infineon licenses a foundation model and deploys it internally; the upstream vendor remains the GPAI provider | Infineon = downstream deployer; obligations are on the upstream provider, but Infineon must document upstream and maintain evidence (Art. 25 "significant modifications" could flip this) |
| **B. GPAI provider via "putting into service under own name"** | Infineon rebrands / integrates and exposes internally as "GPT4IFX" | Infineon has Art. 53 obligations — **operational default in this document** |
| **C. Substantial modification of a 3rd-party GPAI** | Infineon fine-tunes or materially modifies the model | Infineon becomes a provider of the modified model (Art. 25). **De-scoped (no fine-tuning confirmed)** |

### 3.4 Legal action required

**Legal must confirm the applicable scenario.** Until confirmation, this document assumes **Scenario B** and implements Art. 53 obligations as the conservative default. A Legal opinion is requested by **Sprint 14** (~2026-Q3).

Inputs Legal needs:
- Upstream model origin (is the base a licensed third-party foundation model? which vendor?)
- License terms (does the license permit "putting into service under own name"?)
- Any post-license modifications beyond inference-serving (fine-tuning, RLHF, adapters)
- Distribution scope (internal only, or also to customers / partners)

---

## 4. Art. 53 Obligations — applicability mapping

Assuming Scenario B, the following Art. 53 obligations apply. Each is mapped to an owner and an implementation status.

### 4.1 Art. 53(1)(a) — Technical documentation

**Requirement:** Draw up and keep up-to-date technical documentation of the model, including training and testing process and evaluation results, containing at least the information set out in Annex XI, for the purpose of making it available upon request to the AI Office and national competent authorities.

**Scope:** General description of the AI model; design specifications (architecture, # parameters, modalities, license); training data (type, provenance, curation methodology, number of tokens); computational resources used for training; energy consumption; known or foreseeable risks.

| Owner | GPT4IFX Platform Lead |
|---|---|
| Status | Not started |
| Action | Coordinate with upstream vendor (if Scenario A or B) to obtain upstream documentation + assemble Infineon-specific portion (deployment config, any integration layers) |
| Target | Sprint 14 |

### 4.2 Art. 53(1)(b) — Documentation for downstream providers

**Requirement:** Make information and documentation available to downstream AI system providers (i.e., those who integrate GPT4IFX into their own AI systems) containing the information set out in Annex XII, such that downstream providers can understand the capabilities and limitations and comply with their own obligations.

**Scope:** Detailed description of tasks for which the model is intended; acceptable use policies; date of release / methods; architectural overview; modality and format of inputs/outputs; license.

**In our context, "downstream AI system providers" includes:**
- AICE (consumes GPT4IFX via API)
- Each Domain Assistant (CIA, GEST, etc. — each is arguably an AI system)
- Any team within Infineon that uses GPT4IFX via its API for other purposes

| Owner | GPT4IFX Platform Lead |
|---|---|
| Status | Not started |
| Action | Produce "GPT4IFX Downstream Developer Handbook" covering capabilities, limitations, AUP, known failure modes |
| Target | Sprint 14 |

### 4.3 Art. 53(1)(c) — Copyright policy

**Requirement:** Put in place a policy to comply with Union copyright law, in particular to identify and comply with reservations of rights (including via state-of-the-art technologies) expressed under Art. 4(3) of Directive (EU) 2019/790 (opt-out).

**In our context:** If GPT4IFX is a third-party licensed model, the upstream vendor is responsible. If Infineon made any training modifications, the policy applies directly.

| Owner | Legal |
|---|---|
| Status | Not started |
| Action | If Scenario A: obtain upstream copyright policy and reference in Infineon documentation. If Scenario B: publish Infineon copyright policy referencing upstream. |
| Target | Sprint 14 |

### 4.4 Art. 53(1)(d) — Summary of training data content

**Requirement:** Draw up and make publicly available a sufficiently detailed summary of the content used for training of the general-purpose AI model, according to a template provided by the AI Office.

**Template:** AI Office published template in mid-2025.

| Owner | Legal + GPT4IFX Platform Lead |
|---|---|
| Status | Not started |
| Action | If Scenario A: reference upstream provider's published summary. If Scenario B: publish an Infineon summary (likely sourced from upstream). |
| Target | Sprint 14 |

### 4.5 Art. 55 (systemic-risk GPAI) — conditional applicability

**Applies if** the GPAI model is classified as "systemic risk" (Art. 51: cumulative compute used for training ≥ 10^25 FLOPs, or designated by Commission).

| Assessment | Unlikely to apply to GPT4IFX unless the underlying foundation model exceeds the 10^25 FLOP threshold (likely applicable to GPT-4-class from OpenAI) |
|---|---|
| If applies | Additional obligations: model evaluations, adversarial testing, serious-incident tracking + reporting, cybersecurity protection |
| Action | Legal to confirm upstream model's training FLOPs during Legal opinion |

### 4.6 Exemption for free/open-source GPAI (Art. 53(2))

Art. 53(1)(a) and (b) do not apply to providers of GPAI models that are released under a free and open-source license, **unless** they are systemic-risk models. 53(1)(c) and (d) always apply.

| Assessment | Unlikely exemption applies — GPT4IFX is deployed internally, not released open-source, and its upstream base may or may not be FOSS |
|---|---|

---

## 5. Operational Controls

Regardless of final Scenario confirmation, the following operational controls are recommended:

### 5.1 Upstream model documentation vault

- GPT4IFX Platform Team maintains a vault of upstream model documentation: license, technical spec sheet, training data summary, evaluation results (if published by upstream), known limitations, usage restrictions
- Vault retained per DATA_GOVERNANCE_POLICY §6 (product lifetime + 10 years)
- Accessible by AICE Platform, Legal, AI Governance Lead, Safety Manager

### 5.2 Downstream Developer Handbook (for AICE + DAs)

Every AI system that consumes GPT4IFX receives:
- Capabilities matrix (what tasks are supported at what quality level)
- Limitations (known failure modes, non-determinism characteristics)
- AUP (Acceptable Use Policy) — aligns with AI_USAGE_POLICY
- Versioning policy (how model updates are communicated)
- Fallback behavior specification

### 5.3 GPAI Incident Reporting pipeline

If Art. 55 applies (systemic risk), serious-incident reporting to AI Office required. Tie into INCIDENT_RESPONSE v2.0.0 §10 (Art. 73) with a parallel Art. 55 pipeline.

### 5.4 Model Version Registry

Integrated with MLflow + Model Registry:
- Every GPT4IFX model version activated in production has a registry entry
- Registry entry links to: upstream model snapshot, deployment date, evaluation pass (regression benchmark), sign-off by GPT4IFX Platform Lead
- AICE provenance (GAP-01) references this registry entry

### 5.5 Downstream impact notification

When GPT4IFX rolls out a model update:
- Pre-rollout: regression benchmark pass required (Golden Query Set — GAP-11)
- Pre-rollout: notify AICE Platform Team (min 1 sprint in advance)
- Post-rollout: registry updated; DAs notified in their session_start response
- Rollback plan documented

---

## 6. Relationship to Customer Contracts

Infineon ships MCAL/iLLD software (not GPT4IFX itself) to customers. Customers are not "downstream AI providers" of GPT4IFX because they receive output artifacts, not the model.

However, customer contracts (particularly German OEM supplier agreements) may include AI-related disclosure requirements:

| Contract clause type | Infineon response |
|---|---|
| "Disclose use of AI in development process" | Yes — AI-origin markers on generated code; AI Usage Policy summary available on request |
| "Ensure AI-generated content complies with safety standards" | Yes — Review Gate + static analysis pipeline + TD1/TD2 qualification (AICE-GOV-007) |
| "No customer data sent to external AI services" | Yes — customer NDA content stays on-prem per DATA_GOVERNANCE_POLICY §9 |
| "Right to audit AI tool qualification evidence" | Yes — TCER bundles per DA available under NDA (AICE-GOV-007) |
| "Notification on AI model version changes" | Yes — through standard supplier-change process |

Contract Legal to review standard supplier agreements for AI-related clauses.

---

## 7. Timeline and Dependencies

| Milestone | Target | Dependency |
|---|---|---|
| Legal Scenario confirmation | Sprint 14 (2026-Q3) | GPT4IFX upstream documentation; license review |
| Upstream doc vault established | Sprint 14 | Legal confirmation |
| Downstream Developer Handbook published | Sprint 14 | Upstream docs available |
| Copyright policy published | Sprint 14 | Legal draft |
| Training data summary published | Sprint 14 | Upstream provider cooperation (or internal for Scenario B with FT) |
| Art. 55 systemic-risk classification check | Sprint 14 | Training FLOPs data from upstream |
| Customer contract review | Sprint 15 | Legal Contract team |

---

## 8. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-04-18 | ATV MC D SW VDF + draft for Legal review | Initial draft — Scenario B assumed as operational default |

**Approval (pending Legal confirmation):**

| Role | Name | Date |
|---|---|---|
| Legal (classification confirmation) | __________ | __________ |
| GPT4IFX Platform Lead | __________ | __________ |
| AI Governance Lead | __________ | __________ |
| Division Head | __________ | __________ |

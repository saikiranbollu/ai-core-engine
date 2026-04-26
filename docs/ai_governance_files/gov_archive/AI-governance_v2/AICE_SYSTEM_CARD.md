# AI Core Engine — System Card

**Document ID**: AICE-GOV-001
**Version**: 2.0.0 (supersedes v1.0.0 of 2026-03-29)
**Classification**: Internal — Infineon Technologies
**Owner**: ATV MC D SW VDF
**Last Updated**: 2026-04-18
**Review Cycle**: Annually, or upon major system change

> This document satisfies EU AI Act Annex IV technical documentation requirements,
> NIST AI RMF MAP function (system contextualization), VDA AI-in-QM §4 (AI competences
> / system documentation), and serves as the primary governance reference for the AI Core Engine.

---

## 0. Changes from v1.0.0 (2026-03-29)

This v2.0.0 reflects material clarifications received in Apr 2026 and standard-tracking updates.

| Area | v1.0.0 said | v2.0.0 says | Why |
|---|---|---|---|
| EU AI Act classification (AICE itself) | Limited risk | Limited risk (unchanged for AICE-as-tool), **but Infineon is GPAI provider for GPT4IFX** (new §3.4) | GPT4IFX confirmed Infineon-hosted, on-prem — Art. 53 obligations attach |
| Downstream risk cascade | Reviewed and human-approved outputs ship | Same, plus explicit statement that outputs enter **ASIL-D** MCAL delivered to German OEMs | ASIL scope confirmed as ASIL-D; OEM customer base confirmed |
| ISO/PAS 8800 alignment | "Partial" | **Not applicable to AICE** (standard's own scope exclusion) — remains relevant only if AICE outputs code for in-vehicle AI (NPU CDDs, not current scope) | ISO/PAS 8800:2024 §0 excludes "software tools that use AI methods" |
| MLE domain (ASPICE 4.0 MLE.1–4) | Implicit | **Not triggered** (no fine-tuning); SUP.11 ML Data Management applies to corpus | No fine-tuning confirmed |
| Tool Qualification (ISO 26262-8) | Undefined TCL | TI2 per-DA; TD assessment depends on downstream pipeline — TD1 claim defensible given existing MISRA+AUTOSAR+Polyspace Bugfinder+CodeProver+MC/DC pipeline. See AICE-GOV-007 | Pipeline confirmed; Clause 11.4.5 Example 3 supports TD1 for statically-verified output |
| VDA AI-in-QM Yellow Volume | Not referenced | **Chapter 7 adopted as internal risk-assessment method** | Published March 2026; specifically covers AI dev tools; commercially binding for German OEM supply chain |
| DA count | 21 | 22+ (incl. M2MA, ZA, NXA confirmed) | Clarified in discussion |
| Personal data | "Does not process" | Same, **plus enforcing PII redaction control on prompt path** (new §5.3.1) | GDPR/DPDP defensive posture even for dev-tool AI |
| Review Gate scoring vs ASIL | AUTO allowed for QM, QUICK for ASIL-A, FULL for ASIL-B+ | **Unchanged policy — but new technical enforcement rule: ASIL-D artifacts force FULL + Independent + Safety Manager sign-off regardless of confidence score** | ASIL-D confirmed as target ASIL for mcal workspace |
| China regs | Listed as applicable for reference | **De-scoped** (no Chinese developers, no CN-originating data) | Confirmed no China presence |

---

## 1. System Identity

| Field | Value |
|-------|-------|
| **System Name** | AI Core Engine (AICE) |
| **Version** | 2.1.0 (Sprint 10 baseline) |
| **Type** | Knowledge retrieval and context assembly server (MCP) |
| **Deployment** | Infineon Local Cloud (Docker Compose, 7+ services) — on-premises |
| **Protocol** | JSON-RPC 2.0 over streamable-HTTP |
| **Primary Users** | 22+ Domain Assistants + development engineers |
| **Target Domain** | Automotive embedded software (AURIX TC3xx / TC4xx, AUTOSAR MCAL, iLLD) |
| **Highest ASIL supported** | ASIL-D (mcal workspace; delivered to German OEMs) |
| **LLM backends** | GPT4IFX (Infineon-hosted, on-prem, primary for IP-sensitive work); GitHub Copilot Enterprise (cloud, for non-sensitive tasks) |

---

## 2. System Purpose and Scope

### 2.1 What AICE Is

AICE is a **knowledge-graph-backed retrieval and context assembly server** that serves structured engineering knowledge to LLM-based Domain Assistants. It exposes 50+ MCP tools across 13 categories, backed by a Hybrid RAG engine combining Neo4j graph traversal with Qdrant vector similarity search.

AICE provides: API function signatures, register maps, requirements traceability chains, compliance rules, dependency graphs, initialization ordering, test results, and MISRA/AUTOSAR compliance data.

### 2.2 What AICE Is NOT

- **AICE is NOT an LLM.** It does not generate text, code, or decisions on its own. It retrieves and assembles context that LLMs in Domain Assistants consume.
- **AICE is NOT an in-vehicle AI system.** It operates exclusively in the development environment. No AICE component runs on vehicle hardware or influences vehicle behavior at runtime.
- **AICE is NOT itself a safety-critical system in the ISO 26262 Part 4 sense.** It is a development productivity tool governed by ISO 26262 Part 8 Clause 11 (tool qualification). The safety-critical outputs are the responsibility of the Domain Assistants, the downstream verification pipeline, and the human engineers who review and approve them.

### 2.3 Intended Use

AICE is intended to accelerate embedded software development workflows by providing accurate, contextual engineering knowledge to AI-assisted tools. The outputs of AICE-backed Domain Assistants — generated code, test specifications, requirement reviews, architecture analyses, safety assessments — are **delivered to Infineon customers (German OEMs and Tier-1s) as part of productive MCAL/iLLD software deliveries, after mandatory human review**. The AI itself does not ship.

### 2.4 Unintended and Prohibited Uses

| Category | Description |
|----------|-------------|
| **Prohibited** | Direct integration of AI-generated code into the productive baseline without Human Review Gate completion |
| **Prohibited** | Use of AICE outputs as formal safety evidence without independent expert verification |
| **Prohibited** | Autonomous code commit; all commits are under the reviewing engineer's identity |
| **Prohibited** | Processing of personal data, biometric data, or employee performance data |
| **Prohibited** | Routing customer-NDA-restricted content through GitHub Copilot Enterprise (route via GPT4IFX only) |
| **Unintended** | Use as a replacement for domain expertise in safety analysis (FMEA, FTA, DFA, HAZOP) |
| **Unintended** | Use for security-sensitive code paths (cryptographic implementations, access control logic) without security expert review |
| **Unintended** | Generation of AI content for customer-facing artifacts (manuals, datasheets) without marketing/legal review |

---

## 3. EU AI Act Risk Classification

### 3.1 Classification of AICE-as-tool

Under the EU AI Act (Regulation EU 2024/1689):

| Classification Aspect | Assessment |
|----------------------|------------|
| **Annex I (product-embedded)** | **Not applicable.** AICE does not operate within a vehicle or regulated product. The *outputs* (generated code) enter a regulated product (vehicle), but via the customer's own product-integration process and subject to Infineon's human review. |
| **Annex III (standalone high-risk)** | **Not applicable.** AICE does not fall into any of the eight high-risk areas listed in Annex III. |
| **Risk level** | **Limited risk** (transparency obligations under Art. 50 apply) |
| **Rationale** | AICE is a developer tool. Its outputs undergo mandatory human review (Art. 14 human oversight). It does not autonomously make decisions affecting natural persons or safety-critical system execution. |

### 3.2 Downstream Risk Cascade (unchanged governance implication)

AICE itself is limited-risk, but its outputs are **safety-critical up to ASIL-D** and ship to customers:

- **Code generated by CIA / CTA** enters AUTOSAR MCAL drivers up to **ASIL-D** delivered to German OEMs
- **Test specifications from GEST** form SWE.4/SWE.6 verification evidence
- **Safety analyses from SASA / HAZOPA / DaFaA** feed ISO 26262 HARA and safety cases
- **Configuration artifacts from GECA** become customer-deliverable ARXML

**Consequence:** AICE must maintain governance controls proportional to the highest ASIL level of the software it supports (**ASIL-D**) even though AICE itself is not classified as high-risk. The Human Review Gate, tool qualification (ISO 26262-8 Clause 11), and provenance chain are the primary control mechanisms for this cascade.

### 3.3 Transparency Obligations (Art. 50)

| Obligation | Implementation |
|-----------|---------------|
| Users informed they interact with AI | Domain Assistants identify themselves as AI-assisted tools in all UIs (VS Code extension, CLI, MCP) |
| AI-generated content marked | AI-generated code includes `/* AI-GENERATED — Review Required */` header (see AI_USAGE_POLICY §6.1); AI-authored commits carry `AI-Generated-By:` git trailer |
| Provenance recorded | Every AI-assisted output is logged with session ID, tool chain, context sources, model version, review evidence in PostgreSQL audit trail (see AICE-GOV-003 provenance chain spec) |

### 3.4 GPAI Provider Obligations (NEW — v2.0.0)

**Because GPT4IFX is hosted on Infineon-owned on-prem infrastructure, Infineon may be classified as a GPAI (General-Purpose AI) provider under EU AI Act Art. 3(63) and subject to Art. 53 obligations.**

Legal classification is to be confirmed by Legal (see AICE-GOV-006 GPAI_PROVIDER_OBLIGATIONS.md for detailed analysis), but the operational default is to implement Art. 53 obligations as a precaution:

| Art. 53 Obligation | Implementation in AICE context |
|---|---|
| (a) Technical documentation of the model | GPT4IFX documentation (training data summary, capabilities, limitations, compute used, energy consumption) maintained by the GPT4IFX platform team |
| (b) Documentation for downstream AI providers (information sufficient for them to meet their own obligations) | AICE as a *downstream consumer* of GPT4IFX must receive and retain this documentation. DAs that consume GPT4IFX are downstream AI systems |
| (c) Copyright policy | Published Infineon policy on training/fine-tuning data copyright respect (Infineon has no fine-tuning plans, but this policy is still required) |
| (d) Summary of training data content | Published summary for transparency |
| (e-f) Systemic-risk model obligations (evaluations, adversarial testing, incident reporting) | Trigger only if GPT4IFX qualifies as a "systemic-risk GPAI model" (Art. 51: ≥10^25 FLOPs for training). Likely not — confirm with GPT4IFX team |

**Responsibility:** AI Governance Lead coordinates with GPT4IFX platform team. Reference: AICE-GOV-006.

### 3.5 AI Literacy (Art. 4) — in force since 2 Feb 2025

All engineers using AICE-backed DAs must have documented AI literacy training appropriate to their role. See AI_USAGE_POLICY §9.

---

## 4. Technical Architecture Summary

### 4.1 Component Stack

| Component | Technology | Role |
|-----------|-----------|------|
| Knowledge Graph | Neo4j 5.26 | Structured engineering data and relationships |
| Vector Store | Qdrant 1.12 | Semantic embeddings (384-dim all-MiniLM-L6-v2 + upgrade path to domain-adapted embedder) |
| Session/Cache | Redis 7 | Working memory, LRU cache, session TTL |
| Audit Store | PostgreSQL 16 | 7-table audit schema (ASPICE-compliant); WORM layer planned (see GOVERNANCE_IMPLEMENTATION_PLAN GAP-18) |
| Authorization | Cerbos PDP | 3-tier RBAC (public/developer/admin); per-DA principals |
| Metrics | Prometheus + Grafana | 11 metric types, 15s scrape, governance dashboard |
| LLM Proxy | GPT4IFX (Infineon on-prem) | RLM planning, PDF extraction, DA generation for IP-sensitive content |
| Alt LLM | GitHub Copilot Enterprise (MS cloud) | DA generation for non-IP-sensitive content only |
| Framework | FastMCP + Uvicorn | MCP server with ASGI middleware |

### 4.2 Data Flow

```
User Request → DA (LLM) → MCP Call → AICE Server
    → Auth Check (Cerbos) → Tool Handler
    → Hybrid Search (Neo4j + Qdrant) → Context Assembly
    → Response to DA → DA generates output via LLM
    → AI-transparency markers injected (code header, git trailer)
    → Confidence Score (deterministic formula)
    → ASIL check → Review Gate routing (AUTO/QUICK/FULL, ASIL-D override)
    → Human Review → Static analysis pipeline (MISRA+AUTOSAR+Polyspace Bugfinder+CodeProver+MC/DC for ASIL-D)
    → Commit under reviewer identity with provenance + AI-origin trailer
    → Feedback → PatternStore (learning loop)
    → Audit Log (PostgreSQL) + WORM archive
```

### 4.3 LLM Dependencies and Data-Sovereign Routing

| LLM Usage Point | Model | Purpose | Risk | Routing Policy |
|----------------|-------|---------|------|---|
| RLM Orchestrator | GPT4IFX | Sub-query planning for complex retrievals | Medium — plans are validated before execution; max 6 sub-queries | GPT4IFX only (internal) |
| PDF Extraction | GPT4IFX | Table/diagram extraction from HW docs | Low — extracted data verified against source | GPT4IFX only |
| Domain Assistants (IP-sensitive) | GPT4IFX | Code/test/review generation for mcal, customer NDA, IFX-confidential | High — outputs enter ASIL-D path | GPT4IFX only (enforced via Cerbos policy + prompt classifier) |
| Domain Assistants (non-sensitive) | GitHub Copilot Enterprise | iLLD reference SW, public AUTOSAR boilerplate, generic embedded C | Medium — outputs still reviewed | Copilot allowed, subject to data-class gate |

**Data-sovereign routing policy** (enforced pre-LLM):

| Data Class | Copilot Enterprise | GPT4IFX |
|---|---|---|
| Public (AUTOSAR standard text, published TC3xx datasheet) | ✅ | Optional |
| Internal non-IP-sensitive (generic MISRA reasoning) | ✅ | Optional |
| Infineon confidential (unpublished errata, TC4xx pre-release, internal design notes) | ❌ | **Required** |
| Customer NDA-restricted (Jama PRQ marked CUSTOMER-CONFIDENTIAL, customer variants) | ❌ | **Required**, per-customer workspace isolation |
| PII of developers | Scrub before prompt (both paths) | Scrub before prompt |

Classification is deterministic (regex + keyword + ontology tag lookup) before prompt construction — no LLM classifier in the critical path.

---

## 5. Data Governance

### 5.1 Data Sources

| Source | Type | Sensitivity | Ingestion Method |
|--------|------|-------------|-----------------|
| iLLD source code | C/H files | Infineon confidential | Tree-sitter AST parsing |
| MCAL requirements | Jama Connect exports | Infineon confidential | Jama XML/JSON parsers |
| Hardware specifications | PDF documents | Infineon confidential | LLM-assisted PDF extraction |
| AUTOSAR SWS documents | PDF/XML | Licensed (AUTOSAR standard) | RST/XML parsers |
| Test results | JUnit XML, Polyspace CSV | Internal | Result processors |
| Enterprise Architect models | XMI export | Infineon confidential | EA parser |

Customer-originated artifacts (customer variants, customer-specific PRQs) are isolated per-customer workspace and processed exclusively via GPT4IFX.

### 5.2 Data Lifecycle

| Phase | Controls |
|-------|----------|
| **Ingestion** | Admin-tier authorization required; `ingestion_jobs` table tracks source, parser, timestamp, node/relationship counts; signed ingestion bundles (manifest SHA256) for production |
| **Storage** | Dual workspace isolation (illd/mcal) with separate Neo4j databases per workspace; per-module NodeSet anchors prevent cross-module data bleed; per-customer sub-workspace for customer-NDA content |
| **Access** | Cerbos RBAC; API keys mapped to principals; tool-tier enforcement; no cross-workspace queries |
| **Retention** | KG data persists until explicit re-ingestion; **audit logs retained ≥ product lifetime + 10 years** (automotive warranty convention, exceeds EU AI Act Art. 12 6-month floor); Ephemeral Sandbox expires with session TTL |
| **Deletion** | Module-level data can be cleared via admin tools; no personal data processed; deletion creates audit record |

### 5.3 Personal Data Assessment

AICE does **not** intentionally process personal data as defined by GDPR Art. 4(1) or DPDP Act 2023. All data in the knowledge graph is technical engineering data. No employee names, performance data, or behavioral data enters the system. API key principals are system identifiers (e.g., `cia_assistant`), not personal identifiers.

### 5.3.1 PII Redaction Control (NEW — v2.0.0)

Inadvertent PII leakage is possible via:
- Developer names in code comments, commit messages, review notes
- Email signatures in ingested documents
- Reviewer names in `review_evidence` (this is intentional and scoped — not considered PII leakage to LLM since review names stay in PostgreSQL and are not fed to LLM context)

**Control:** A deterministic NER-based PII scrubber runs on all prompt content before LLM invocation. Scrubber rules:
- Strip personal names (first-last pairs), email addresses, phone numbers, postal addresses
- Preserve role tokens (e.g., `<REVIEWER>`) that are substituted back in UI
- Log scrub events to audit trail (no PII in logs)

**Implementation reference:** AICE-GOV implementation plan GAP-16.

### 5.4 GPAI Training Data (NEW — v2.0.0)

Under Art. 53, if Infineon is confirmed as GPAI provider for GPT4IFX:
- Training data origin summary must be published (owner: GPT4IFX platform team)
- Copyright-respect policy documented (owner: Legal)
- AICE does not fine-tune GPT4IFX; no additional training data obligations attach to AICE itself

---

## 6. Quality and Safety Controls

### 6.1 Confidence Scoring (Deterministic)

The Review Gate uses a deterministic formula (not LLM-based):

```
Score = clamp(50 + Σ(quality_signals) - Σ(risk_signals), 0, 100)

Quality: has_kg_context(+30), high_relevance(+20), has_dependency_order(+20),
         has_proven_patterns(+15), format_correct(+10), misra_compliant(+10),
         similar_approved(+5)

Risk:    missing_requirements(-30), low_relevance(-20), compliance_warnings(-20),
         novel_pattern(-15), is_safety_critical(-15), complex_logic(-10)

Routing: AUTO (≥80) | QUICK (50-79) | FULL (<50)
```

### 6.2 Review Gate Enforcement (REVISED — v2.0.0)

| ASIL Level | Minimum Review | ASIL Override Rule |
|-----------|---------------|--------|
| QM (non-safety) | AUTO permitted | None |
| ASIL-A | QUICK minimum | None |
| ASIL-B | FULL mandatory | Cannot downgrade via score alone |
| ASIL-C | FULL mandatory + independent reviewer | Cannot downgrade via score alone |
| **ASIL-D (mcal target)** | **FULL + independent reviewer + Safety Manager sign-off** | **Hard-gated — confidence score cannot bypass.** The `asil=D` tag on the artifact forces routing regardless of score |

**Governance rule (v2.0.0 technical enforcement):** The `is_safety_critical` signal must be set to `true` for any module with ASIL-B or above, and the `asil=D` metadata tag forces the review gate to FULL + independent + safety-manager. This is **implemented in `evaluate_confidence` tool logic**, not relying on reviewer discipline. Ref: GOVERNANCE_IMPLEMENTATION_PLAN GAP-05 (enforcement) and GAP-18 (ASIL-gated override).

### 6.3 Static Analysis Pipeline (critical for TD1 claim under ISO 26262-8)

All AI-generated code enters the same CI pipeline as human-authored code:

| Gate | Tool | Required for | Failure action |
|---|---|---|---|
| 1 | Clean compile (`-Wall -Werror`) | All ASIL | Block merge |
| 2 | MISRA C:2012 analysis (mandatory + required rules) | All ASIL | Block merge for mandatory; deviation review for required |
| 3 | AUTOSAR C++ guideline check | AUTOSAR modules | Block merge |
| 4 | Polyspace Bugfinder | All ASIL | Block merge on Red findings |
| 5 | Polyspace CodeProver (formal proof) | ASIL-B+ | Block merge on Red proof results |
| 6 | Structural coverage (MC/DC) | ASIL-D | Block merge if coverage below threshold |
| 7 | Integration test on HIL / ISS | Safety-relevant units | Block merge on failure |

**All seven gates are mandatory and evidenced** — this is the basis for the ISO 26262-8 Clause 11 TD1 argument. See AICE-GOV-007 TOOL_QUALIFICATION_PLAN for full rationale.

### 6.4 Feedback and Learning Loop

Approved patterns stored in Neo4j PatternStore + Qdrant PatternIndex. Positive feedback loop boosts future confidence scores. Controls:
- Only `APPROVE` and `APPROVE_WITH_EDITS` verdicts generate patterns
- `APPROVE_WITH_EDITS` patterns stored with reduced confidence (0.75)
- `REJECT` verdicts stored as failure patterns in PostgreSQL for analysis
- Pattern extraction is auditable and reversible
- **Pattern expiration (NEW):** patterns older than 12 months without re-validation enter review; patterns older than 24 months without re-validation retire automatically (ref: GAP-09)

### 6.5 Observability

| Metric Category | Implementation |
|----------------|---------------|
| Tool usage | `tool_requests_total`, `tool_request_duration` per tool per DA |
| Search quality | `search_requests_total`, `search_duration` |
| Cache efficiency | `cache_requests_total` (hit/miss/eviction) |
| Review activity | `review_routing_total` (AUTO/QUICK/FULL distribution) |
| System health | `backend_up` gauge for all 7 services |
| **AI-governance (NEW)** | `ai_origin_tagged_commits_total`, `asil_gated_overrides_total`, `static_analysis_pass_rate`, `review_escapes_detected_total` |

---

## 7. Known Limitations and Failure Modes

### 7.1 Technical Limitations

| Limitation | Impact | Mitigation |
|-----------|--------|-----------|
| KG coverage not complete for all modules | Missing context → lower-quality retrievals | `find_coverage_gaps` reports missing links; `missing_requirements` signal reduces confidence score |
| Embedding model (MiniLM-L6-v2) is general-purpose | May miss domain-specific semantic similarities | Graph-RAG (structural queries) compensates; ~75% of AICE queries are graph-structural; domain-adapted embedder on roadmap |
| RLM planning depends on LLM | Sub-query plans may be suboptimal | Plans validated and bounded (max 6 sub-queries); fallback to single hybrid search |
| Semantic cache similarity threshold (0.85) | Near-miss queries may return stale results | LRU cache uses exact match; cache invalidation on KG updates planned |
| **LLM non-determinism** | Same prompt may yield different outputs | (1) Low temperature (0.2); (2) prompt template versioning; (3) downstream verification pipeline catches deviations; (4) pattern-similarity scoring penalizes unusual outputs |

### 7.2 Failure Modes

| Failure | Detection | Response |
|---------|-----------|----------|
| Neo4j unavailable | Health check, `backend_up` gauge | Graceful degradation — vector-only search continues |
| Qdrant unavailable | Health check | Graceful degradation — graph-only search continues |
| Redis unavailable | Health check | No cache — all queries go to full RAG pipeline |
| PostgreSQL unavailable | Non-blocking writes | Audit logs buffered to stderr; **replay on recovery to avoid data loss** |
| GPT4IFX unavailable | Token refresh failure | RLM disabled — single hybrid search fallback; IP-sensitive DAs block (no Copilot failover for NDA content) |
| Cerbos unavailable | Sidecar health check | **Fail-closed** — tier checks fail; no graceful fallback allowed for production workloads |
| PII scrubber malfunction | Test canaries | Prompt path blocked; alerts to Platform Team |
| Review gate ASIL override bypass attempt | Cerbos + evaluate_confidence logic | Logged as governance incident; blocked |

### 7.3 Bias and Fairness Considerations

In automotive embedded SW dev-tool context, "bias" means *technical bias* rather than demographic:

| Bias Type | Risk | Assessment |
|-----------|------|-----------|
| **Module coverage bias** | Some modules have richer KG data than others | Measurable via `get_graph_statistics`; documented per module (GAP-07 assessment) |
| **Pattern approval bias** | Frequently-used patterns get more approvals, reinforcing themselves | Mitigated by `novel_pattern` signal and pattern expiration (GAP-09) |
| **Workspace bias** | MCAL workspace may have different coverage than iLLD | Separate statistics per workspace |
| **Source bias** | Some parsers extract more completely than others | Parser-level quality metrics tracked |
| **LLM training-data bias** | Copilot trained on public GitHub; may favor non-AUTOSAR idioms | Mitigated by RAG grounding + static analysis gates + approved pattern store |

---

## 8. Human Oversight

### 8.1 Oversight Mechanisms

| Mechanism | Description |
|-----------|-------------|
| **Review Gate** | Every AI-assisted output is scored and routed to appropriate review level, with ASIL-gated override for ASIL-D |
| **Confidence Breakdown** | Engineers see which signals contributed to the score — fully explainable |
| **Override Capability** | Any review routing can be escalated (override_review_routing); downgrades below policy minimum are blocked |
| **Feedback Loop** | Engineer decisions recorded and improve future scoring |
| **Audit Trail** | PostgreSQL log of every tool invocation, review decision, feedback event, plus WORM layer for compliance-relevant events (new — GAP-18) |
| **Incident Response** | Formal runbook (AICE-GOV-005, v2.0.0); field-failure → AI-lineage traceback procedure |

### 8.2 Human Authority

The human engineer retains full authority to:
- Override any AI-generated output
- Escalate any review to a higher level
- Reject any AI-generated artifact for any reason
- Modify AI-generated code before approval
- Disable AI assistance for specific tasks
- Request regeneration with different parameters

No AICE tool or Domain Assistant may autonomously commit code, merge branches, release artifacts, or modify production systems.

---

## 9. Incident Response

See AICE-GOV-005 INCIDENT_RESPONSE.md v2.0.0 for full procedure. Summary:

### 9.1 AI-Related Incident Classification

| Severity | Definition | Response time |
|----------|-----------|--------|
| **Critical** | AI output caused or could cause a safety hazard in the field (customer ECU), or breaks EU AI Act Art. 73 serious-incident threshold | Immediate (< 2h triage, < 48h resolution) |
| **High** | AI output contains significant technical errors that passed review but caught pre-ship | < 4h triage, < 5 business days resolution |
| **Medium** | AI output has quality issues caught in review | < 1 business day triage, < 10 days resolution |
| **Low** | Minor formatting/style | < 3 days triage |

### 9.2 EU AI Act Serious Incident Reporting (NEW — v2.0.0)

Under Art. 73, if an AI-generated artifact contributes to a serious incident in a high-risk AI system (vehicle), reporting to the national authority is required:
- 15 days: serious incidents (life-threatening or widespread)
- 2 days: death or serious harm

Although AICE itself is not high-risk, its outputs flow into high-risk vehicle systems. **Downstream reporting obligation of the OEM may trigger upstream investigation of AICE's contribution.** AICE's provenance chain (session → code commit → file → customer delivery) is the primary investigative instrument.

### 9.3 Field-Failure → AI-Lineage Traceback (NEW — v2.0.0)

When an Infineon customer reports a field failure in an Infineon-delivered MCAL driver:
1. Platform Team queries `response_archive` by commit SHA of the failing file
2. Retrieves session_id → provenance chain
3. Identifies: DA, LLM model version, KG snapshot, prompt template version, reviewer
4. Determines if AI involvement was material
5. If material: trigger incident per §9.1 and §9.2

---

## 10. Regulatory Alignment Matrix (REVISED)

| Requirement Source | Requirement | AICE Implementation | Status |
|---|---|---|---|
| **EU AI Act Art. 4** | AI literacy | AI Usage Policy §9 training program | Partial — policy drafted, training pending |
| **EU AI Act Art. 9** | Risk management system | Confidence scoring + Review Gate + Feedback Loop + VDA Ch.7 method | Partial — VDA method adoption in progress |
| **EU AI Act Art. 10** | Data governance | DATA_GOVERNANCE_POLICY.md v1.1.0 | Implemented |
| **EU AI Act Art. 11 + Annex IV** | Technical documentation | This System Card + AICE-GOV-006 GPAI + AICE-GOV-007 TQ Plan + architecture docs | Partial — complete by Sprint 14 |
| **EU AI Act Art. 12** | Record-keeping | PostgreSQL 7-table audit + WORM (planned GAP-18) + Prometheus | Partial — WORM pending |
| **EU AI Act Art. 13** | Transparency | AI-transparency markers (code header, git trailer, metadata) | Partial — enforcement in CIA/GEST (GAP-02) |
| **EU AI Act Art. 14** | Human oversight | Review Gate with ASIL-gated override | Implemented |
| **EU AI Act Art. 15** | Accuracy, robustness, cybersecurity | Hybrid RAG with fallback; deterministic scoring; TARA extension (AICE-GOV-008 planned) | Partial |
| **EU AI Act Art. 50** | AI interaction transparency | DA self-identification in UIs | Implemented |
| **EU AI Act Art. 53** (GPAI) | Provider obligations for GPT4IFX | See AICE-GOV-006 | Partial — documentation gathering in progress |
| **EU AI Act Art. 73** | Serious incident reporting | INCIDENT_RESPONSE.md v2.0.0 §9.2 | Partial — runbook drafted, not yet exercised |
| **NIST AI RMF GOVERN / MAP / MEASURE / MANAGE** | Trustworthy AI functions | Cerbos RBAC; DA risk classification; Prometheus; Review Gate | Implemented |
| **ISO/IEC 42001:2023** | AI Management System | AIMS documentation in progress | In progress — target: Stage 1 audit in 2026 |
| **ISO 26262-8 Clause 11** | Tool qualification | See AICE-GOV-007 | Partial — per-DA TCER in progress |
| **ISO/SAE 21434** | Cybersecurity | AI-extended TARA (AICE-GOV-008 planned) | Planned |
| **ASPICE 4.0 SWE.1–SWE.6** | Process maturity | CI pipeline + Review Gate + Provenance | Implemented |
| **ASPICE 4.0 SUP.11** | ML Data Management | Corpus snapshots, ingestion jobs, workspace isolation | Implemented |
| **ASPICE 4.0 MLE.1–MLE.4** | ML Engineering | **Not applicable** (no fine-tuning) | N/A |
| **ISO/PAS 8800:2024** | Road vehicles — Safety and AI | **Not applicable to AICE per standard's own scope exclusion.** May apply if future scope includes in-vehicle AI (NPU CDDs) | N/A (current scope) |
| **ISO/IEC TS 22440** | Functional safety and AI | Still at CD stage (Feb 2026); watch-list | Watch |
| **VDA AI-in-QM Yellow Volume (Mar 2026)** | AI in automotive quality mgmt | Chapter 7 method adopted as internal risk-assessment framework | In progress |
| **VDA Automotive SPICE 4.0 Blue-Gold Book** | Process assessment | Integrated into existing ASPICE CL target | Implemented |
| **China AI stack** | Generative AI Measures + 2025 GB standards | **Not applicable** (no China developers, no CN-origin data) | N/A |
| **India DPDP** | Personal data protection | PII scrubber (GAP-16) | Planned |

---

## 11. Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |
| **2.0.0** | **2026-04-18** | **ATV MC D SW VDF** | **Major revision reflecting: GPT4IFX on-prem hosting → GPAI provider posture; ASIL-D confirmed as target; VDA AI-in-QM adoption; ISO/PAS 8800 explicit non-applicability; MLE de-scoped (no fine-tuning); China regs de-scoped; tool qualification path (TD1 vs TD2) detailed in AICE-GOV-007; new PII scrubber; field-failure traceback; Art. 53 and Art. 73 obligations added** |

**Review and Approval**:

| Role | Name | Date | Signature |
|------|------|------|-----------|
| System Architect | __________ | __________ | __________ |
| Safety Manager | __________ | __________ | __________ |
| Quality Manager | __________ | __________ | __________ |
| AI Governance Lead | __________ | __________ | __________ |
| GPT4IFX Platform Lead (new) | __________ | __________ | __________ |
| Legal (GPAI classification review) (new) | __________ | __________ | __________ |

---

*This document must be reviewed whenever AICE undergoes a major version change,
when the LLM provider changes, when new Domain Assistants are onboarded,
when a new regulatory milestone activates (e.g., EU AI Act Aug 2026 / Aug 2027),
or at minimum annually.*

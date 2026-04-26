# AI Incident Response Procedure

**Document ID**: AICE-GOV-005
**Version**: 2.0.0 (supersedes v1.0.0 of 2026-03-29)
**Classification**: Internal — Infineon Technologies
**Owner**: ATV MC D SW VDF
**Last Updated**: 2026-04-18

---

## 0. Changes from v1.0.0

| Area | v1.0.0 | v2.0.0 | Why |
|---|---|---|---|
| Scope (§2) | Internal quality issues | Expanded to **field incidents in customer-delivered software** and **EU AI Act Art. 73 reporting** | Outputs ship to ASIL-D customer baseline |
| Severity (§3) | Technical impact only | Adds **field/regulatory impact** — any AI-contribution to a field incident = CRITICAL candidate | Art. 73 triggers |
| New Phase 0 | — | **Pre-triage: field-failure → AI-lineage traceback (NEW)** | Discover AI involvement in customer incidents |
| Regulatory reporting | Not addressed | **NEW §10 — Art. 73 reporting workflow** | Regulatory obligation |
| Response roles | Module Lead + AI Governance Lead | Adds **Safety Manager, Legal, Customer Interface Lead** for field incidents | Field incident complexity |
| Runbook status | Implicit | **Explicit: "not yet exercised" — action to run tabletop exercise in 2026** | Surface unimplemented state |

---

## 1. Purpose

Defines identification, classification, investigation, and resolution of incidents where AI-assisted development tools (AICE Domain Assistants) produce outputs that contain errors, create safety risks, or violate governance policies — **including incidents discovered in customer-delivered software (field incidents).**

---

## 2. Scope (REVISED)

Covers incidents involving:

- **Internal quality incidents** — AI-generated artifacts discovered defective before customer delivery
- **Field incidents (NEW)** — customer-reported failures in Infineon-delivered MCAL/iLLD where AI-generated content may be a contributor
- AI-generated code with functional errors, MISRA/AUTOSAR violations, or safety hazards
- AI-generated test specifications that miss critical scenarios
- AI-generated requirement reviews with incorrect or misleading findings
- Governance policy violations (review bypass, unmarked AI content, unauthorized data processing, data-class routing violations)
- Systematic quality degradation after AICE updates (KG changes, LLM model version changes, pipeline changes)
- **GPAI-related incidents (NEW)** — GPT4IFX model drift, systemic behavior change

---

## 3. Incident Classification (REVISED)

| Severity | Definition | Response Time | Examples |
|---|---|---|---|
| **CRITICAL** | AI output caused or could cause a safety hazard in the field OR breaks Art. 73 serious-incident threshold OR customer ECU field failure with AI-content contribution | Triage within **2 hours**; resolution within **48 hours** | Incorrect register access that could damage silicon; missing safety check in ASIL-D code; complete loss of audit trail; any field failure in delivered MCAL traceable to AI-generated code |
| **HIGH** | AI output contains significant technical errors that passed human review (caught pre-ship) | Triage within **4 hours**; resolution within **5 business days** | Missing DET error handling; incorrect initialization sequence; wrong interrupt priority; pattern-store poisoning suspected |
| **MEDIUM** | AI output has quality issues caught in review | Triage within **1 business day**; resolution within **10 business days** | Suboptimal code structure; incomplete requirement coverage; missing edge-case tests |
| **LOW** | Minor formatting, style, or documentation issues | Triage within **3 business days**; resolution within **20 business days** | Non-standard naming; missing comments; formatting inconsistencies |

---

## 4. Incident Response Flow

### Phase 0 — Field-Failure Traceback (NEW)

Triggered when a customer reports a field failure in delivered MCAL/iLLD.

1. **Receive customer report** via Customer Interface Lead
2. **Identify affected file(s) and commit SHA** from delivered release
3. **Query `response_archive`** by commit SHA: was this file touched by AI? If yes → proceed; if no → standard field-incident process (out of AI scope)
4. **Retrieve provenance chain:**
   - `session_id` → DA, LLM model version, corpus snapshot, prompt template version
   - `review_evidence` → reviewer(s), date, review type, static analysis results
   - AI-origin git trailer
5. **Preliminary assessment (within 4h):** Is the failure plausibly caused by AI-generated content? 
6. **If yes:** escalate to CRITICAL per §3 → Phase 1
7. **If no:** continue standard field-incident process but document AI-non-causation

### Phase 1 — Detection and Reporting

**Who can report:**
- Any engineer, reviewer, automated system (CI/CD, static analysis)
- Customer Interface Lead (for field incidents, via Phase 0)
- External — via OEM customer incident channel

**How to report:**
1. **Via FeedbackSink** (preferred for technical issues): Submit `REJECT` with detailed description in feedback `details`. Automatic record in `feedback_records`.
2. **Via governance incident tool** (policy violations, systematic issues): Use `governance_report` tool or insert into `governance_incidents` table
3. **Direct escalation** for CRITICAL: contact Module Lead + AI Governance Lead + Safety Manager immediately via standard escalation channels. Do not wait for tooling. **For field CRITICAL: also contact Legal + Customer Interface Lead.**

**Required information:**
- AICE session ID (or "Phase-0 derived" if from field)
- Domain Assistant name and version
- Module and workspace
- Description of the issue
- Expected vs. actual output
- Impact assessment (pre-ship: "what could go wrong in production"; field: "observed failure description + customer identity if permitted")
- **Customer identity and delivery release tag (for field incidents, NEW)**

### Phase 2 — Triage

**Triage owner:**
- Module Lead (technical issues)
- AI Governance Lead (policy violations, systemic issues)
- Safety Manager (ASIL-B+ quality escape)
- **Legal + AI Governance Lead jointly (field-CRITICAL, per Art. 73 assessment)** (NEW)

**Triage actions:**

1. **Classify severity** per §3
2. **Assess blast radius:** Are other outputs from the same session/module/period affected?
3. **Immediate containment** (CRITICAL only):
   - Disable AUTO approval for affected module
   - Notify all engineers who received outputs from the same AICE context in past 7 days
   - Quarantine any uncommitted AI-generated code for the affected module
   - **For field-CRITICAL:** coordinate customer notification via Customer Interface Lead (NEW)
   - **For suspected Art. 73 trigger:** Legal begins clock on 2-day / 15-day reporting deadlines (NEW)
4. **Assign investigator:** engineer with module domain expertise
5. **Record in `governance_incidents`** with severity, session details, assigned investigator

### Phase 3 — Investigation

1. **Retrieve provenance chain:**
   - `response_archive` for archived response and provenance
   - `audit_logs` for all tool invocations in the session
   - Confidence score breakdown from `review_evidence`
   - **Static analysis result history from CI/CD (NEW)**
   - **Corpus snapshot version in effect at generation time (NEW)**
   - If provenance chain incomplete (legacy sessions): reconstruct from audit logs

2. **Identify root cause** — classify into one of:

| Root Cause Category | Description | Typical Evidence |
|---|---|---|
| **KG Data Gap** | Knowledge Graph missing critical data (errata, register constraints, API contracts) | Provenance shows no KG nodes for relevant concept; `find_coverage_gaps` confirms |
| **KG Data Error** | Incorrect data in KG (wrong register address, incorrect API signature) | Provenance points to specific KG node with incorrect data |
| **Retrieval Failure** | Correct data exists but was not retrieved | KG contains data; search missed it |
| **LLM Hallucination** | LLM generated content not grounded in retrieved context | Context was correct; DA LLM output diverged |
| **Prompt Issue** | Prompt template led LLM to incorrect interpretation | Prompt wording caused systematic misinterpretation |
| **Confidence Miscalibration** | Review routing too permissive for actual risk | Score was high but output quality low; signals didn't capture the risk |
| **Static Analysis Bypass/Gap (NEW)** | CI/CD pipeline did not catch a defect it should have | Specific rule, tool, or gate failed to detect |
| **Human Review Failure** | Reviewer approved incorrect output | Review evidence shows approval; correct review would have caught |
| **Policy Violation** | Governance rules not followed | Audit shows bypass, missing markers, unauthorized actions |
| **GPAI Model Drift (NEW)** | GPT4IFX behavior changed between sessions without notice | Model version changed; regression benchmark failed |
| **RAG Poisoning (NEW)** | Ingested content contained adversarial instructions | Provenance points to specific ingested doc with suspicious content |
| **Pattern Store Poisoning (NEW)** | Approved pattern used in generation was itself defective | Pattern introduced by earlier incident cascaded into new outputs |

3. **Document findings** in `governance_incidents`

### Phase 4 — Corrective Action

| Root Cause | Corrective Action | Responsible |
|---|---|---|
| KG Data Gap | Ingest missing data; verify with `get_graph_statistics`; add module to coverage bias watch list | Platform Team + Module Lead |
| KG Data Error | Correct KG node; re-validate related nodes; add to golden query regression set | Platform Team |
| Retrieval Failure | Adjust search parameters; add failing query to golden query set; consider RLM task-type prompt update | Platform Team |
| LLM Hallucination | Add to DA negative examples; update prompt with explicit constraints; lower `similar_approved` weight; consider DA re-qualification | DA Developer |
| Prompt Issue | Update DA prompt template; sign with new hash; test with historical sessions | DA Developer |
| Confidence Miscalibration | Add new signal or adjust weight; validate against historical feedback | Platform Team |
| **Static Analysis Bypass/Gap** (NEW) | Tighten gate; add rule; make advisory → blocking | Platform Team + Module Lead |
| Human Review Failure | Training refresher; process review; checklist items; (ASIL-D) Safety Manager review of reviewer's qualification | Module Lead + Safety Manager |
| Policy Violation | Process reminder; training; policy update if unclear; potential disciplinary action | AI Governance Lead |
| **GPAI Model Drift** (NEW) | Coordinate with GPT4IFX platform team; pin model version; re-run regression benchmarks; document in AIBOM | AI Governance Lead + GPT4IFX Platform Lead |
| **RAG Poisoning** (NEW) | Quarantine affected ingestion batch; re-ingest from trusted source; content-type guard tightening | Platform Team |
| **Pattern Store Poisoning** (NEW) | Retire affected patterns; regenerate from known-good historical reviews; tighten pattern acceptance criteria | Platform Team |

### Phase 5 — Verification and Closure

1. **Verify corrective action:** Re-run original session (or equivalent); confirm issue resolved
2. **Regression test:** Add failing scenario to golden query set
3. **Update governance report:** Include incident in next quarterly report
4. **Close incident:** update `governance_incidents` with `status='closed'`, `resolved_at`, `resolved_by`
5. **Lessons learned:** If systemic, propose policy or process update
6. **Customer communication closure** (field incidents): Customer Interface Lead confirms with customer (NEW)

---

## 5. Containment Procedures by Severity

### CRITICAL Containment

```
1. IMMEDIATELY: Disable AUTO approval for affected module
   → Override via governance_policy.yaml or `override_review_routing` tool

2. WITHIN 2 HOURS: Notify blast radius + Safety Manager + (field) Legal + Customer Interface Lead
   → Identify all sessions for affected module in past 7 days
   → Notify engineers who received outputs from those sessions
   → Quarantine uncommitted AI-generated code

3. WITHIN 4 HOURS: Root cause hypothesis + preliminary Art. 73 assessment (field incidents)
   → Preliminary investigation
   → Legal + AI Governance Lead: does this trigger Art. 73 reporting?

4. WITHIN 24 HOURS: Implement immediate fix
   → Correct KG data, disable affected DA capability, adjust routing, halt shipment if needed

5. WITHIN 48 HOURS: Full resolution (internal) OR
   → (field) Coordinated with customer; Art. 73 report prepared if triggered

6. WITHIN 15 DAYS (field-serious): File Art. 73 report via Legal if triggered
```

### HIGH Containment

```
1. WITHIN 4 HOURS: Triage and assign
2. WITHIN 1 DAY: Adjust confidence scoring if needed (add risk signal, increase is_safety_critical weight)
3. WITHIN 5 DAYS: Root cause, corrective action, verification
```

---

## 6. Metrics

| Metric | Target |
|---|---|
| Critical incidents unresolved > 48 hours | 0 |
| High incidents unresolved > 5 business days | 0 |
| Incident recurrence (same root cause) | < 5% |
| Average time to root cause identification | < 2 business days |
| Golden query set coverage of past incidents | 100% |
| **Field-incident AI-lineage traceback completion time** (NEW) | < 24h |
| **Art. 73 reporting deadline compliance** (NEW) | 100% (when triggered) |

---

## 7. PostgreSQL Schema (REVISED — adds fields for v2.0.0)

```sql
CREATE TABLE IF NOT EXISTS governance_incidents (
    id                SERIAL PRIMARY KEY,
    severity          VARCHAR(10) NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    incident_source   VARCHAR(20) NOT NULL CHECK (incident_source IN ('internal','field','policy','other')),  -- NEW v2.0.0
    customer_ref      VARCHAR(100),  -- NEW v2.0.0 - for field incidents
    delivery_release  VARCHAR(100),  -- NEW v2.0.0 - customer-delivered release tag
    art73_assessed    BOOLEAN DEFAULT FALSE,  -- NEW v2.0.0
    art73_triggered   BOOLEAN DEFAULT FALSE,  -- NEW v2.0.0
    art73_reported_at TIMESTAMPTZ,            -- NEW v2.0.0
    reported_at       TIMESTAMPTZ DEFAULT NOW(),
    reported_by       VARCHAR(100),
    session_id        VARCHAR(100),
    da_name           VARCHAR(50),
    module            VARCHAR(50),
    workspace         VARCHAR(20),
    asil_level        VARCHAR(10),
    llm_model_version VARCHAR(100),  -- NEW v2.0.0
    corpus_version    VARCHAR(100),  -- NEW v2.0.0
    prompt_template_version VARCHAR(100),  -- NEW v2.0.0
    description       TEXT NOT NULL,
    root_cause_category VARCHAR(40),
    root_cause_detail TEXT,
    corrective_action TEXT,
    blast_radius      TEXT,
    status            VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open','triaging','investigating','implementing','verifying','resolved','closed')),
    assigned_to       VARCHAR(100),
    resolved_at       TIMESTAMPTZ,
    resolved_by       VARCHAR(100),
    regression_test_added BOOLEAN DEFAULT FALSE,
    lessons_learned   TEXT
);

CREATE INDEX idx_gov_incidents_status ON governance_incidents(status);
CREATE INDEX idx_gov_incidents_severity ON governance_incidents(severity);
CREATE INDEX idx_gov_incidents_session ON governance_incidents(session_id);
CREATE INDEX idx_gov_incidents_source ON governance_incidents(incident_source);  -- NEW
CREATE INDEX idx_gov_incidents_art73 ON governance_incidents(art73_triggered) WHERE art73_triggered = TRUE;  -- NEW
```

---

## 8. Implementation Status (NEW §)

As of v2.0.0 publication (April 2026):

| Capability | Status | Action |
|---|---|---|
| `governance_incidents` PostgreSQL table | ✅ Schema defined (v2.0.0) | Create in production |
| Field-failure → AI-lineage traceback tooling | ❌ Not implemented | Platform Team Sprint 12 |
| Art. 73 assessment runbook (Legal-owned) | ❌ Not drafted | Legal + AI Governance Lead Q2 2026 |
| Customer Interface Lead role for AI incidents | ❌ Not formally assigned | Management action |
| Tabletop exercise of CRITICAL field incident | ❌ Never conducted | **Schedule within 90 days** of v2.0.0 publication |
| Golden query regression set | ❌ Not created | Platform Team Sprint 13 |
| Pattern expiration mechanism | ❌ Not implemented | Platform Team Sprint 13 |

**Top implementation priority:** field-failure traceback tooling (enables Phase 0).

---

## 9. Roles and Contacts (NEW §)

| Role | Name | Contact | When to contact |
|---|---|---|---|
| Module Lead | __________ | __________ | Any technical issue |
| Safety Manager | __________ | __________ | ASIL-B+ quality escape |
| AI Governance Lead | __________ | __________ | Policy violations, systemic issues |
| Quality Manager | __________ | __________ | ASPICE concerns |
| Customer Interface Lead | __________ | __________ | Field incidents from customers |
| Legal | __________ | __________ | Art. 73 assessment, customer contract impact |
| GPT4IFX Platform Lead | __________ | __________ | GPAI model issues |
| Platform Team on-call | __________ | __________ | CRITICAL infrastructure issues |

---

## 10. EU AI Act Art. 73 Serious Incident Reporting — NEW §

### 10.1 Applicability

Art. 73 applies to **providers of high-risk AI systems**. AICE itself is limited-risk, but:

- OEM customers (high-risk AI in-vehicle) have Art. 73 obligations
- Infineon as potential GPAI provider for GPT4IFX has obligations under Art. 55 (systemic risk, if triggered) and may be pulled into OEM's Art. 73 investigation
- **If AI-generated code contributed to a field incident, Infineon as supplier may be contractually obligated to support the OEM's Art. 73 filing**

### 10.2 Reporting Timelines (reference)

| Incident type | Art. 73 deadline |
|---|---|
| Death or serious harm to health | 2 days |
| Serious and irreversible disruption of critical infrastructure | 2 days |
| Breach of Union law intended to protect fundamental rights | Without undue delay, max 15 days |
| Serious harm to property or environment | Without undue delay, max 15 days |

### 10.3 Infineon Internal Process

1. CRITICAL field incident triggers Phase 0 + Phase 2 CRITICAL containment
2. Legal assesses within 4h whether Art. 73 applies (direct obligation) or whether OEM is likely to file and Infineon needs to support
3. If yes: Legal drives the filing or support-filing process; AI Governance Lead provides provenance + technical facts
4. Customer Interface Lead coordinates with OEM on joint communications
5. All filings and customer communications retained in `governance_incidents.lessons_learned` and WORM archive

---

## 11. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |
| **2.0.0** | **2026-04-18** | **ATV MC D SW VDF** | **Major revision: field-incident scope; Phase 0 traceback; EU AI Act Art. 73 reporting workflow; GPAI model drift and RAG/pattern poisoning as new root causes; schema extended; §8 implementation status and §9 role contacts added; tabletop exercise action** |

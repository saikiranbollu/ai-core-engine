# AI Incident Response Procedure

**Document ID**: AICE-GOV-005  
**Version**: 1.0.0  
**Classification**: Internal — Infineon Technologies  
**Owner**: ATV MC D SW VDF  
**Last Updated**: 2026-03-29  

---

## 1. Purpose

This procedure defines the process for identifying, classifying, investigating, and resolving incidents where AI-assisted development tools (AICE Domain Assistants) produce outputs that contain errors, create safety risks, or violate governance policies.

---

## 2. Scope

This procedure covers incidents involving:

- AI-generated code with functional errors, MISRA violations, or safety hazards
- AI-generated test specifications that miss critical scenarios
- AI-generated requirement reviews with incorrect or misleading findings
- Governance policy violations (review bypass, unmarked AI content, unauthorized data processing)
- Systematic quality degradation after AICE updates (KG changes, model changes, pipeline changes)

---

## 3. Incident Classification

| Severity | Definition | Response Time | Examples |
|----------|-----------|---------------|---------|
| **CRITICAL** | AI output caused or could cause a safety hazard, hardware damage, or data loss | Triage within 2 hours; resolution within 48 hours | Incorrect register access that could damage silicon; missing safety check in ASIL-D code; complete loss of audit trail |
| **HIGH** | AI output contains significant technical errors that passed human review | Triage within 4 hours; resolution within 5 business days | Missing DET error handling; incorrect initialization sequence; wrong interrupt priority |
| **MEDIUM** | AI output has quality issues that were caught in review | Triage within 1 business day; resolution within 10 business days | Suboptimal code structure; incomplete requirement coverage; missing edge case tests |
| **LOW** | Minor formatting, style, or documentation issues | Triage within 3 business days; resolution within 20 business days | Non-standard naming; missing comments; formatting inconsistencies |

---

## 4. Incident Response Flow

### Phase 1: Detection and Reporting

**Who can report**: Any engineer, reviewer, or automated system (CI/CD, static analysis)

**How to report**:

1. **Via FeedbackSink** (preferred for technical issues): Submit `REJECT` verdict with detailed description in the feedback `details` field. This automatically creates a record in `feedback_records`.

2. **Via governance incident tool** (for policy violations or systematic issues): Use the `governance_report` tool's incident creation capability, or insert directly into the `governance_incidents` table.

3. **Direct escalation** (for CRITICAL severity): Contact Module Lead and AI Governance Lead immediately via standard escalation channels. Do not wait for tooling.

**Required information**:
- AICE session ID (`session_id` from the DA)
- Domain Assistant name and version
- Module and workspace
- Description of the issue
- Expected vs. actual output
- Impact assessment (what could go wrong if this had entered production?)

---

### Phase 2: Triage

**Triage owner**: Module Lead (for technical issues); AI Governance Lead (for policy violations)

**Triage actions**:

1. **Classify severity** using the table in Section 3
2. **Assess blast radius**: Are other outputs from the same session/module/period potentially affected?
3. **Immediate containment** (CRITICAL only):
   - Disable AUTO approval for the affected module
   - Notify all engineers who received outputs from the same AICE context in the past 7 days
   - Quarantine any uncommitted AI-generated code for the affected module
4. **Assign investigator**: An engineer with module domain expertise
5. **Record in `governance_incidents` table** with severity, session details, and assigned investigator

---

### Phase 3: Investigation

**Investigation steps**:

1. **Retrieve provenance chain**:
   - Query `response_archive` for the session's archived response and provenance
   - Query `audit_logs` for all tool invocations in the session
   - Retrieve the confidence score breakdown from `review_evidence`
   - If provenance chain is not yet implemented: reconstruct manually from audit logs

2. **Identify root cause** — classify into one of:

| Root Cause Category | Description | Typical Evidence |
|--------------------|-------------|-----------------|
| **KG Data Gap** | Knowledge Graph missing critical data (errata, register constraints, API contracts) | Provenance shows no KG nodes for the relevant concept; `find_coverage_gaps` confirms missing links |
| **KG Data Error** | Incorrect data in the Knowledge Graph (wrong register address, incorrect API signature) | Provenance points to specific KG node with incorrect data |
| **Retrieval Failure** | Correct data exists in KG but was not retrieved (search miss, wrong alpha, wrong node types) | KG contains the data; search query did not surface it; RLM sub-queries missed it |
| **LLM Hallucination** | LLM generated content not grounded in the retrieved context | Context assembly was correct; DA's LLM output diverged from context |
| **Prompt Issue** | DA prompt template led the LLM to incorrect interpretation | Prompt wording caused systematic misinterpretation |
| **Confidence Miscalibration** | Review routing was too permissive for the actual risk level | Score was high but output quality was low; signals didn't capture the risk |
| **Human Review Failure** | Reviewer approved incorrect output | Review evidence shows approval; correct review would have caught the issue |
| **Policy Violation** | Governance rules were not followed | Audit trail shows bypass, missing markers, or unauthorized actions |

3. **Document findings** in the `governance_incidents` record

---

### Phase 4: Corrective Action

| Root Cause | Corrective Action | Responsible |
|-----------|-------------------|-------------|
| **KG Data Gap** | Ingest missing data; verify with `get_graph_statistics`; add module to coverage bias watch list | Platform Team + Module Lead |
| **KG Data Error** | Correct KG node; re-validate related nodes; add to golden query regression set | Platform Team |
| **Retrieval Failure** | Adjust search parameters; add failing query to golden query set; consider RLM task-type prompt update | Platform Team |
| **LLM Hallucination** | Add to DA's negative examples; update prompt with explicit constraints; consider lowering `similar_approved` weight | DA Developer |
| **Prompt Issue** | Update DA prompt template; test with historical sessions; review with domain expert | DA Developer |
| **Confidence Miscalibration** | Add new signal or adjust weight; validate against historical feedback data | Platform Team |
| **Human Review Failure** | Training refresher; process review; consider adding checklist items | Module Lead |
| **Policy Violation** | Process reminder; training; policy update if unclear; potential disciplinary action | AI Governance Lead |

---

### Phase 5: Verification and Closure

1. **Verify corrective action**: Re-run the original session (or equivalent) and confirm the issue is resolved
2. **Regression test**: Add the failing scenario to the golden query set (GAP-11)
3. **Update governance report**: Include the incident in the next quarterly governance report
4. **Close the incident**: Update `governance_incidents` with `status='closed'`, `resolved_at`, and `resolved_by`
5. **Lessons learned**: If the incident reveals a systemic issue, propose a policy or process update

---

## 5. Containment Procedures by Severity

### CRITICAL Containment

```
1. IMMEDIATELY: Disable AUTO approval for affected module
   → Override in governance_policy.yaml or via override_review_routing tool

2. WITHIN 2 HOURS: Notify blast radius
   → Identify all sessions for the affected module in the past 7 days
   → Notify engineers who received outputs from those sessions
   → Quarantine any uncommitted AI-generated code

3. WITHIN 4 HOURS: Root cause hypothesis
   → Preliminary investigation to determine if the issue is data, retrieval, or LLM

4. WITHIN 24 HOURS: Implement immediate fix
   → Correct KG data, or disable the affected DA capability, or adjust routing

5. WITHIN 48 HOURS: Full resolution
   → Root cause confirmed, corrective action verified, incident closed
```

### HIGH Containment

```
1. WITHIN 4 HOURS: Triage and assign
2. WITHIN 1 DAY: Adjust confidence scoring if needed
   → Add risk signal or increase is_safety_critical weight
3. WITHIN 5 DAYS: Root cause, corrective action, verification
```

---

## 6. Metrics

| Metric | Target |
|--------|--------|
| Critical incidents unresolved > 48 hours | 0 |
| High incidents unresolved > 5 business days | 0 |
| Incident recurrence (same root cause) | < 5% |
| Average time to root cause identification | < 2 business days |
| Golden query set coverage of past incidents | 100% |

---

## 7. PostgreSQL Schema

```sql
CREATE TABLE IF NOT EXISTS governance_incidents (
    id                SERIAL PRIMARY KEY,
    severity          VARCHAR(10) NOT NULL CHECK (severity IN ('critical','high','medium','low')),
    reported_at       TIMESTAMPTZ DEFAULT NOW(),
    reported_by       VARCHAR(100),
    session_id        VARCHAR(100),
    da_name           VARCHAR(50),
    module            VARCHAR(50),
    workspace         VARCHAR(20),
    asil_level        VARCHAR(10),
    description       TEXT NOT NULL,
    root_cause_category VARCHAR(30),
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
```

---

## 8. Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |

# AI Governance Implementation Plan

**Document ID**: AICE-GOV-003  
**Version**: 1.0.0  
**Classification**: Internal — Infineon Technologies  
**Owner**: ATV MC D SW VDF  
**Last Updated**: 2026-03-29  

> This plan identifies governance gaps in the AI Core Engine and Domain Assistants,
> specifies the technical implementations needed, and maps them to sprint delivery.

---

## 1. Governance Maturity Assessment

### 1.1 Current State (Sprint 10)

| Governance Area | NIST RMF Function | Status | Score (1-5) |
|----------------|-------------------|--------|-------------|
| Access control (RBAC) | GOVERN | Implemented (Cerbos, 3-tier, per-DA keys) | 5 |
| Audit trail | GOVERN | Implemented (PostgreSQL 7-table schema) | 4 |
| Human oversight | MANAGE | Implemented (Review Gate, confidence scoring) | 4 |
| Traceability | MAP | Implemented (V-Model chain, coverage gaps) | 4 |
| Observability | MEASURE | Implemented (Prometheus, Grafana, 11 metrics) | 4 |
| Feedback loop | MANAGE | Implemented (FeedbackSink, PatternStore, learning) | 4 |
| System documentation | MAP | Partial (arch docs exist; governance docs missing) | 3 |
| Output provenance | GOVERN | Partial (audit logs exist; full chain incomplete) | 2 |
| AI transparency marking | MAP | Not implemented | 1 |
| Data governance policy | GOVERN | Not implemented (ingestion tracked but no formal policy) | 2 |
| Bias assessment | MEASURE | Not implemented (statistics available but no framework) | 1 |
| Model versioning | GOVERN | Not implemented (GPT4IFX version not tracked per call) | 1 |
| Governance reporting | MEASURE | Not implemented (raw data available but no consolidated view) | 1 |
| Incident response process | MANAGE | Not implemented (ad-hoc; no formal process) | 1 |
| DA-level governance hooks | GOVERN | Not implemented (DAs don't enforce policy rules) | 1 |

**Overall Maturity Score: 2.5 / 5** — Strong technical foundation, weak governance formalization.

### 1.2 Target State (Sprint 14)

**Target Maturity Score: 4.0 / 5** — All critical governance gaps closed; documentation complete; governance tools operational; DA-level enforcement active.

---

## 2. Gap Analysis

### 2.1 Critical Gaps (Must Fix)

#### GAP-01: Output Provenance Chain

**Problem**: AICE logs tool invocations (`audit_logs`) and archives responses (`response_archive`), but there is no linkage from the archived response to: (a) the specific KG nodes and Qdrant chunks used as context, (b) the LLM model version and parameters, (c) the final committed artifact in version control, (d) the complete review evidence chain.

**Impact**: Cannot fully reconstruct how any AI-generated artifact was produced. This is required by EU AI Act Art. 12 (record-keeping) and ISO 26262 Part 8 (tool qualification — demonstrating the tool's contribution to the output).

**Technical Spec**: See Section 3.1.

---

#### GAP-02: AI Transparency Marking

**Problem**: AI-generated outputs are not systematically marked as AI-assisted. The `/* AI-GENERATED */` header comment pattern is defined in this policy but not enforced by any tool. DAs generate code/text but don't inject transparency markers.

**Impact**: Violates P3 (Transparency) of AI Usage Policy. EU AI Act Art. 50 requires marking of AI-generated content.

**Technical Spec**: See Section 3.2.

---

#### GAP-03: Model Version Tracking

**Problem**: When GPT4IFX is called (RLM planning, PDF extraction, DA code generation), the model version, temperature, and token count are not recorded in the audit trail. If GPT4IFX updates the underlying model, historical outputs cannot be attributed to a specific model version.

**Impact**: Reproducibility is compromised. Cannot investigate whether a model version change caused quality regressions.

**Technical Spec**: See Section 3.3.

---

#### GAP-04: Governance Reporting Tool

**Problem**: All governance data exists in PostgreSQL, Prometheus, and Neo4j, but there is no consolidated governance report. The quarterly governance review requires manual data assembly across multiple sources.

**Impact**: Governance monitoring is labor-intensive and inconsistent.

**Technical Spec**: See Section 3.4.

---

### 2.2 Important Gaps (Should Fix)

#### GAP-05: DA-Level Governance Enforcement

**Problem**: The AI Usage Policy (AICE-GOV-002) defines ASIL-specific review requirements (e.g., FULL review mandatory for ASIL-B+), but this is not enforced at the DA or AICE level. A DA could theoretically accept an AUTO routing for ASIL-D code if the confidence score happens to be high.

**Impact**: Policy relies entirely on human discipline rather than technical enforcement.

**Technical Spec**: See Section 3.5.

---

#### GAP-06: Data Governance Documentation

**Problem**: The ingestion pipeline tracks jobs (`ingestion_jobs` table) but there is no formal Data Governance Policy documenting: data classification, authorized data sources, data quality criteria, data retention rules, and data lineage requirements.

**Impact**: NIST AI RMF GOVERN function requires documented data governance; EU AI Act Art. 10 requires data governance for training and operational data.

**Technical Spec**: See Section 3.6.

---

#### GAP-07: Coverage Bias Assessment Framework

**Problem**: `get_graph_statistics` provides raw node/relationship counts per module, but there is no systematic framework for assessing whether coverage gaps create systematic bias in AI outputs. No threshold for "adequate coverage" is defined.

**Impact**: Cannot demonstrate fairness of AI outputs across modules. Some modules may consistently receive lower-quality context, leading to lower-quality AI outputs.

**Technical Spec**: See Section 3.7.

---

#### GAP-08: Incident Response Procedure

**Problem**: No formal procedure exists for handling AI-related quality escapes (AI-generated code with serious errors that passed review, or AI-generated test cases that missed critical defects).

**Impact**: Ad-hoc response to AI quality issues; no systematic learning from incidents.

**Technical Spec**: See Section 3.8.

---

### 2.3 Nice-to-Have Gaps (Could Fix)

#### GAP-09: Pattern Store Governance

**Problem**: Approved patterns accumulate in Neo4j PatternStore and Qdrant PatternIndex, but there is no periodic review process, no expiration, and no mechanism to retire patterns that may have become outdated due to specification changes.

**Technical Spec**: Periodic pattern review cadence (quarterly); pattern expiration after 12 months without re-validation; `review_patterns` admin tool.

---

#### GAP-10: Cross-DA Governance Dashboard

**Problem**: Each DA operates independently. There is no unified view showing which DAs are generating the most outputs, which have the highest rejection rates, and where governance risks concentrate.

**Technical Spec**: Grafana dashboard extension with per-DA panels; requires `assistant_name` label on Prometheus metrics.

---

#### GAP-11: Regression Testing for RAG Quality

**Problem**: After KG updates, embedding model changes, or cache configuration changes, the retrieval quality may silently degrade. No automated regression testing framework exists for RAG pipeline quality.

**Technical Spec**: "Golden query set" with expected results; automated comparison after updates; quality degradation alerts.

---

## 3. Technical Specifications

### 3.1 GAP-01: Output Provenance Chain

**Sprint**: 11  
**Effort**: 3-4 days  
**Components Modified**: `response_archive` table, `build_context` tool handler, `evaluate_confidence`, `complete_review`

#### 3.1.1 Schema Extension

Add `provenance` JSONB column to `response_archive`:

```sql
ALTER TABLE response_archive ADD COLUMN provenance JSONB DEFAULT '{}';
```

Provenance structure:

```json
{
  "session_id": "CIA_20260329_143022",
  "assistant": "CIA",
  "task_type": "code_generation",
  "workspace": "mcal",
  "module": "CAN",
  "timestamp": "2026-03-29T14:30:22Z",
  "context_sources": {
    "kg_nodes": [
      {"id": "neo4j://mcal/APIFunction/IfxCan_Node_init", "type": "APIFunction", "relevance": 0.92},
      {"id": "neo4j://mcal/DataStructure/IfxCan_Config", "type": "DataStructure", "relevance": 0.88}
    ],
    "qdrant_chunks": [
      {"collection": "mcal_CAN_embeddings", "id": "chunk_4821", "score": 0.91},
      {"collection": "mcal_CAN_embeddings", "id": "chunk_3294", "score": 0.87}
    ],
    "sandbox_docs": ["user_uploaded_hw_spec.pdf"],
    "rlm_used": true,
    "rlm_sub_queries": 4,
    "cache_hit": false
  },
  "llm_info": {
    "provider": "GPT4IFX",
    "model_version": "gpt-4o-2025-11-20",
    "temperature": 0.2,
    "max_tokens": 8192,
    "prompt_tokens": 6841,
    "completion_tokens": 3294
  },
  "review": {
    "confidence_score": 67,
    "review_type": "QUICK",
    "override_to": "FULL",
    "override_reason": "ASIL-B module",
    "verdict": "APPROVE_WITH_EDITS",
    "reviewer": "engineer_x",
    "review_duration_minutes": 42,
    "edits_summary": "Fixed DET check ordering in Init function"
  },
  "artifact": {
    "commit_sha": "a1b2c3d4",
    "files_affected": ["Can_17_McalCan.c", "Can_17_McalCan.h"],
    "branch": "feature/CAN-init-gen"
  }
}
```

#### 3.1.2 Implementation Steps

1. Extend `PostgresClient.archive_response()` to accept `provenance` dict
2. Modify `build_context` tool handler to collect and return source node IDs and chunk IDs
3. Modify `evaluate_confidence` to pass through LLM info received from DA
4. Modify `complete_review` to finalize the provenance chain with review evidence
5. Add `get_provenance` admin tool to retrieve complete provenance for a given session/response
6. Update CIA and GEST Python backends to pass `artifact` info after commit

---

### 3.2 GAP-02: AI Transparency Marking

**Sprint**: 11  
**Effort**: 1-2 days  
**Components Modified**: CIA backend (`prompt_builder.py`), GEST backend, DA response postprocessor

#### 3.2.1 Implementation

Add a `postprocess_output` step in each DA's response pipeline:

```python
def inject_ai_markers(output: str, session_id: str, da_name: str, 
                       aice_version: str, confidence: int) -> str:
    """Inject AI transparency markers into generated code."""
    header = f"""/**
 * @ai_assisted  true
 * @ai_tool      {da_name} / AICE v{aice_version}
 * @ai_session   {session_id}
 * @ai_confidence {confidence}
 * @ai_review    PENDING
 *
 * This file was generated with AI assistance.
 * Human review is required before integration.
 */
"""
    return header + output
```

For non-code outputs (requirements, test specs), inject a metadata block:

```
[AI-ASSISTED] Generated by {da_name} via AICE v{aice_version}
Session: {session_id} | Confidence: {confidence} | Review: PENDING
```

#### 3.2.2 DA Client Library Update

Add `inject_markers=True` parameter to the `AICEClient.build_context()` method. When enabled, the returned context includes metadata that the DA can pass through to its output postprocessor.

---

### 3.3 GAP-03: Model Version Tracking

**Sprint**: 11  
**Effort**: 1 day  
**Components Modified**: `GPT4IFXClient`, `RLMOrchestrator`, audit logging

#### 3.3.1 Implementation

Modify the `GPT4IFXClient` wrapper to capture and return model metadata from the LLM response headers:

```python
class GPT4IFXClient:
    async def complete(self, messages, **kwargs):
        response = await self._http_client.post(...)
        
        # Extract model info from response
        model_info = {
            "provider": "GPT4IFX",
            "model_version": response.json().get("model", "unknown"),
            "temperature": kwargs.get("temperature", 0.2),
            "prompt_tokens": response.json().get("usage", {}).get("prompt_tokens"),
            "completion_tokens": response.json().get("usage", {}).get("completion_tokens"),
        }
        
        return completion_text, model_info
```

Store `model_info` in the `audit_logs` table's `params` JSONB column, and pass it through to the provenance chain.

---

### 3.4 GAP-04: Governance Reporting Tool

**Sprint**: 12  
**Effort**: 3-4 days  
**Components Modified**: New `governance_report` MCP tool (Category 11: Observability)

#### 3.4.1 Tool Specification

```python
@mcp.tool()
async def governance_report(
    period: str = "last_30_days",      # "last_7_days", "last_30_days", "last_quarter", "custom"
    start_date: str = None,            # ISO date for custom period
    end_date: str = None,              # ISO date for custom period
    workspace: str = "mcal",
    format: str = "json"               # "json" or "markdown"
) -> dict:
    """
    Generate a consolidated AI governance report.
    
    Tier: developer
    
    Aggregates data from PostgreSQL (audit_logs, feedback_records, 
    review_evidence), Prometheus (metrics), and Neo4j (graph statistics, 
    pattern store) into a single governance summary.
    """
```

#### 3.4.2 Report Structure

```json
{
  "period": {"start": "2026-03-01", "end": "2026-03-29"},
  "workspace": "mcal",
  
  "activity_summary": {
    "total_sessions": 342,
    "total_tool_invocations": 4821,
    "unique_das_active": 12,
    "da_breakdown": {
      "CIA": {"sessions": 89, "invocations": 1243},
      "GEST": {"sessions": 67, "invocations": 892},
      "...": "..."
    }
  },
  
  "review_gate_summary": {
    "total_reviews": 156,
    "routing_distribution": {"AUTO": 42, "QUICK": 78, "FULL": 36},
    "override_count": 8,
    "override_reasons": ["ASIL-B module (5)", "Novel pattern concern (2)", "Complex logic (1)"],
    "verdict_distribution": {"APPROVE": 98, "APPROVE_WITH_EDITS": 34, "REJECT": 18, "ESCALATE": 6},
    "rejection_rate": 0.115,
    "avg_confidence_by_verdict": {"APPROVE": 76.2, "REJECT": 38.4},
    "confidence_calibration": {
      "auto_correct_rate": 0.95,
      "full_override_rate": 0.22
    }
  },
  
  "quality_metrics": {
    "misra_violations_in_ai_code": 12,
    "polyspace_findings_in_ai_code": 4,
    "coverage_gap_modules": ["UART", "CXPI"],
    "pattern_store_size": 234,
    "patterns_added_this_period": 18,
    "patterns_from_rejections": 7
  },
  
  "data_governance": {
    "ingestion_jobs_this_period": 12,
    "new_kg_nodes": 3421,
    "new_qdrant_chunks": 1892,
    "modules_updated": ["CAN", "SPI", "ADC"],
    "kg_coverage": {
      "CAN": {"nodes": 8924, "relationships": 12341, "coverage_score": 0.87},
      "SPI": {"nodes": 5621, "relationships": 7823, "coverage_score": 0.79},
      "...": "..."
    }
  },
  
  "incidents": {
    "critical": 0,
    "high": 1,
    "medium": 3,
    "low": 8,
    "details": [
      {
        "severity": "high",
        "date": "2026-03-15",
        "description": "CIA generated CAN driver with incorrect interrupt priority register access",
        "root_cause": "Missing errata entry in KG for TC39xA step",
        "corrective_action": "Ingested errata document; added regression test",
        "session_id": "CIA_20260315_091204"
      }
    ]
  },
  
  "compliance_status": {
    "review_gate_bypass_count": 0,
    "asil_b_plus_full_review_rate": 1.0,
    "ai_marking_compliance": 0.82,
    "audit_trail_completeness": 0.98,
    "provenance_chain_completeness": 0.0
  }
}
```

#### 3.4.3 Data Sources

| Report Section | Primary Source | Query |
|---------------|---------------|-------|
| Activity summary | PostgreSQL: `audit_logs` | `SELECT tool, COUNT(*) GROUP BY tool` filtered by period |
| Review gate | PostgreSQL: `feedback_records`, `review_evidence` | JOIN on `response_id` |
| Quality metrics | PostgreSQL: `failure_patterns`; Neo4j: PatternStore | Count by type/period |
| Data governance | PostgreSQL: `ingestion_jobs`; Neo4j: `get_graph_statistics` | Period-filtered |
| Incidents | PostgreSQL: custom `governance_incidents` table (new) | Period-filtered |
| Compliance | Cross-query: audit completeness, review coverage | Computed |

---

### 3.5 GAP-05: DA-Level Governance Enforcement

**Sprint**: 12  
**Effort**: 2-3 days  
**Components Modified**: `evaluate_confidence`, DA session lifecycle, new `governance_policy` config

#### 3.5.1 Policy-Enforced Review Routing

Add a `governance_policy.yaml` configuration file:

```yaml
# Governance policy configuration
# Overrides confidence-based routing when safety constraints apply

review_overrides:
  # Force minimum review type based on ASIL classification
  asil_minimum_review:
    QM: "AUTO"       # No override — confidence-based routing
    ASIL-A: "QUICK"  # Minimum QUICK for ASIL-A
    ASIL-B: "FULL"   # Minimum FULL for ASIL-B
    ASIL-C: "FULL"   # Minimum FULL for ASIL-C
    ASIL-D: "FULL"   # Minimum FULL for ASIL-D

  # Force minimum review type based on task type
  task_minimum_review:
    code_generation: "QUICK"
    safety_validation: "FULL"
    safety_analysis: "FULL"
    hazop_analysis: "FULL"
    bugfix_analysis: "QUICK"
    test_generation: "QUICK"
    requirement_review: "AUTO"
    
  # Require independent reviewer for these combinations
  independent_review_required:
    - asil: ["ASIL-C", "ASIL-D"]
      tasks: ["code_generation", "test_generation", "safety_validation"]

# Transparency marking
marking:
  inject_ai_markers: true
  marker_format: "doxygen"  # "doxygen" | "comment_block" | "metadata_tag"

# Provenance
provenance:
  track_kg_sources: true
  track_llm_version: true
  track_qdrant_chunks: true
  require_artifact_link: false  # Will become true when VCS integration is ready
```

#### 3.5.2 Enforcement in evaluate_confidence

```python
def evaluate(self, signals, response_id, asil_level=None, task_type=None):
    # Normal confidence calculation
    result = self._calculate_score(signals, response_id)
    
    # Apply governance policy overrides
    if asil_level and asil_level in self._policy["asil_minimum_review"]:
        min_review = self._policy["asil_minimum_review"][asil_level]
        if self._review_rank(result["review_type"]) < self._review_rank(min_review):
            result["review_type"] = min_review
            result["governance_override"] = {
                "reason": f"ASIL-{asil_level} requires minimum {min_review} review",
                "original_routing": result["review_type"],
                "policy_ref": "AICE-GOV-002 Section 5.1"
            }
    
    # Check independent reviewer requirement
    if self._requires_independent(asil_level, task_type):
        result["independent_reviewer_required"] = True
    
    return result
```

---

### 3.6 GAP-06: Data Governance Documentation

**Sprint**: 12  
**Effort**: 1-2 days (documentation)  
**Deliverable**: `docs/governance/DATA_GOVERNANCE_POLICY.md`

Key sections:

1. **Data Classification**: Engineering data (Confidential), Public standards (Licensed), Test results (Internal)
2. **Authorized Data Sources**: Jama Connect, Enterprise Architect, source repos, HW spec repositories, AUTOSAR standards
3. **Data Quality Criteria**: Parser validation rules, minimum node completeness, relationship integrity checks
4. **Data Lineage**: Every KG node must trace to a source document, parser version, and ingestion job ID
5. **Data Retention**: KG data retained until superseded by re-ingestion; audit logs per organizational retention policy; Ephemeral Sandbox data: session TTL (default 1 hour)
6. **Data Access Control**: Workspace-level isolation; module-level NodeSet anchors; Cerbos RBAC for tools
7. **Data Quality Monitoring**: `get_graph_statistics` periodic snapshots; coverage gap tracking per module

---

### 3.7 GAP-07: Coverage Bias Assessment Framework

**Sprint**: 13  
**Effort**: 2-3 days  
**Components Modified**: New `assess_coverage_bias` developer-tier tool

#### 3.7.1 Framework

Define "adequate coverage" thresholds per node type:

```yaml
coverage_thresholds:
  APIFunction:
    minimum_per_module: 20
    minimum_with_relationships: 0.8  # 80% must have at least one relationship
  DataStructure:
    minimum_per_module: 10
    minimum_with_relationships: 0.7
  SoftwareRequirement:
    minimum_per_module: 30
    minimum_with_traceability: 0.9  # 90% must trace to code or test
  Register:
    minimum_per_module: 50
    minimum_with_sw_link: 0.6  # 60% must link to a function
```

The tool compares actual counts against thresholds and produces a per-module bias risk assessment:

```json
{
  "module": "UART",
  "bias_risk": "HIGH",
  "issues": [
    "APIFunction count (8) below minimum (20)",
    "Only 45% of registers linked to SW functions (threshold: 60%)",
    "0 approved patterns in PatternStore"
  ],
  "recommendation": "Ingest UART iLLD source code and SWS document before using AI for this module"
}
```

---

### 3.8 GAP-08: Incident Response Procedure

**Sprint**: 12  
**Effort**: 1 day (documentation + table creation)  
**Deliverable**: `docs/governance/INCIDENT_RESPONSE.md` + `governance_incidents` PostgreSQL table

#### 3.8.1 Table Schema

```sql
CREATE TABLE IF NOT EXISTS governance_incidents (
    id            SERIAL PRIMARY KEY,
    severity      VARCHAR(10) NOT NULL,     -- critical, high, medium, low
    reported_at   TIMESTAMPTZ DEFAULT NOW(),
    reported_by   VARCHAR(100),
    session_id    VARCHAR(100),
    da_name       VARCHAR(50),
    module        VARCHAR(50),
    workspace     VARCHAR(20),
    description   TEXT NOT NULL,
    root_cause    TEXT,
    corrective_action TEXT,
    status        VARCHAR(20) DEFAULT 'open',  -- open, investigating, resolved, closed
    resolved_at   TIMESTAMPTZ,
    resolved_by   VARCHAR(100)
);
```

#### 3.8.2 Process Flow

```
1. Engineer detects AI quality issue
   → Submit REJECT via FeedbackSink (automatic)
   → If severity ≥ HIGH: create incident via governance_report tool or direct

2. Triage (Module Lead, within 4 hours for HIGH/CRITICAL)
   → Classify severity
   → Assign investigator
   → For CRITICAL: immediate halt of AUTO approvals for affected module

3. Investigation (within 2 business days)
   → Retrieve provenance chain for the session
   → Identify root cause (KG gap, retrieval failure, LLM hallucination, prompt issue)
   → Document in incident record

4. Corrective Action
   → KG gap: ingest missing data
   → Retrieval failure: adjust search parameters, add test to golden query set
   → LLM hallucination: add to known failure modes; adjust confidence signals
   → Prompt issue: update DA prompt templates

5. Resolution
   → Verify corrective action effectiveness
   → Update governance report
   → Close incident
```

---

## 4. Sprint Delivery Plan

### Sprint 11 (Current Sprint — Validation & Test Coverage + Governance Foundation)

| Item | Gap | Effort | Priority |
|------|-----|--------|----------|
| Provenance chain schema + implementation | GAP-01 | 3-4 days | Critical |
| AI transparency marking in CIA + GEST | GAP-02 | 1-2 days | Critical |
| Model version tracking in GPT4IFXClient | GAP-03 | 1 day | Critical |
| System Card document (AICE-GOV-001) | — | Delivered | Done |
| AI Usage Policy document (AICE-GOV-002) | — | Delivered | Done |

**Sprint 11 Governance Deliverables**:
- `response_archive.provenance` column populated for all new responses
- AI markers injected in CIA-generated code
- LLM model version recorded in audit logs
- `AICE_SYSTEM_CARD.md` and `AI_USAGE_POLICY.md` in repo

---

### Sprint 12 (Governance Tooling)

| Item | Gap | Effort | Priority |
|------|-----|--------|----------|
| `governance_report` MCP tool | GAP-04 | 3-4 days | Important |
| `governance_policy.yaml` + enforcement in `evaluate_confidence` | GAP-05 | 2-3 days | Important |
| Data Governance Policy document | GAP-06 | 1-2 days | Important |
| Incident Response procedure + table | GAP-08 | 1 day | Important |
| `governance_incidents` PostgreSQL table | GAP-08 | 0.5 days | Important |
| Grafana governance dashboard panel | GAP-10 | 1 day | Nice-to-have |

**Sprint 12 Governance Deliverables**:
- `governance_report` tool operational (tool #57)
- Policy-enforced review routing for ASIL-B+ modules
- `DATA_GOVERNANCE_POLICY.md` in repo
- `INCIDENT_RESPONSE.md` in repo
- Incident tracking table in PostgreSQL

---

### Sprint 13 (Governance Hardening)

| Item | Gap | Effort | Priority |
|------|-----|--------|----------|
| Coverage bias assessment tool | GAP-07 | 2-3 days | Important |
| Pattern Store governance (expiration, review cadence) | GAP-09 | 2 days | Nice-to-have |
| RAG regression testing framework ("golden query set") | GAP-11 | 3-4 days | Important |
| DA governance hooks for remaining DAs (ACRA, REVA, SAGA, etc.) | GAP-05 | 2-3 days | Important |
| `get_provenance` admin tool | GAP-01 | 1 day | Important |

**Sprint 13 Governance Deliverables**:
- `assess_coverage_bias` tool operational (tool #58)
- Pattern expiration mechanism
- Golden query set with automated regression testing
- All active DAs enforce governance policy

---

### Sprint 14 (Governance Maturity)

| Item | Gap | Effort | Priority |
|------|-----|--------|----------|
| First quarterly governance report | All | 1 day | Critical |
| Governance maturity re-assessment | All | 0.5 days | Important |
| Training materials (AI Usage Policy walkthrough) | — | 2 days | Important |
| External audit readiness check (EU AI Act) | — | 1 day | Important |
| VCS integration for artifact linking in provenance | GAP-01 | 2-3 days | Nice-to-have |

---

## 5. Updated Tool Count Projection

| Sprint | Current Tools | New Governance Tools | Total |
|--------|--------------|---------------------|-------|
| 10 | 56 | 0 | 56 |
| 12 | 56 | +1 (`governance_report`) | 57 |
| 13 | 57 | +2 (`assess_coverage_bias`, `get_provenance`) | 59 |

---

## 6. Requirements Traceability

New governance requirements to add to `AICE_SYSTEM_REQUIREMENTS.md`:

| Req ID | Requirement | Gap | Sprint | Priority |
|--------|-------------|-----|--------|----------|
| AICE-GOV-001 | Every AI-generated response shall include a provenance chain linking KG sources, LLM version, review evidence, and artifact reference | GAP-01 | 11 | Must |
| AICE-GOV-002 | All AI-generated code shall include transparency markers identifying the DA, AICE version, session ID, and confidence score | GAP-02 | 11 | Must |
| AICE-GOV-003 | Every LLM invocation shall record the model version, temperature, and token counts in the audit trail | GAP-03 | 11 | Must |
| AICE-GOV-004 | The system shall provide a consolidated governance report aggregating audit, review, quality, and compliance metrics | GAP-04 | 12 | Must |
| AICE-GOV-005 | Review routing shall enforce minimum review levels based on ASIL classification as defined in `governance_policy.yaml` | GAP-05 | 12 | Must |
| AICE-GOV-006 | Data governance policy shall document data classification, authorized sources, quality criteria, lineage, retention, and access controls | GAP-06 | 12 | Should |
| AICE-GOV-007 | The system shall provide a coverage bias assessment comparing per-module KG statistics against defined adequacy thresholds | GAP-07 | 13 | Should |
| AICE-GOV-008 | AI-related quality incidents shall be tracked in a dedicated table with severity, root cause, corrective action, and resolution status | GAP-08 | 12 | Must |
| AICE-GOV-009 | Approved patterns in PatternStore shall expire after 12 months without re-validation and be subject to quarterly review | GAP-09 | 13 | Should |
| AICE-GOV-010 | A golden query set shall be maintained and executed after KG or pipeline updates to detect retrieval quality regressions | GAP-11 | 13 | Should |

---

## 7. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| EU AI Act classification changes (AICE reclassified as high-risk) | Low | High | System Card designed to Annex IV standards regardless; controls already proportional |
| GPT4IFX model update causes silent quality regression | Medium | High | Model version tracking (GAP-03) + golden query regression tests (GAP-11) |
| Governance overhead slows development velocity | Medium | Medium | Automate governance checks; minimize manual steps; integrate into existing CI/CD |
| Pattern Store accumulates outdated patterns | Medium | Medium | Pattern expiration (GAP-09); quarterly review cadence |
| Engineers bypass governance controls | Low | High | Technical enforcement (GAP-05); training; monitoring; violation reporting |
| KG coverage gaps create systematic blind spots | Medium | Medium | Coverage bias assessment (GAP-07); per-module coverage tracking |

---

## 8. Success Criteria

| Criterion | Target | Measurement |
|-----------|--------|-------------|
| Governance maturity score | ≥ 4.0 / 5 by Sprint 14 | Re-assessment using Section 1.1 framework |
| Review Gate bypass rate | 0% | `governance_report` compliance section |
| ASIL-B+ FULL review coverage | 100% | `governance_report` review gate section |
| Provenance chain completeness | ≥ 95% of responses | `governance_report` compliance section |
| AI marking compliance | ≥ 95% of generated code | Automated scan of committed code |
| Quarterly governance report delivery | On time | Calendar tracking |
| Zero critical governance incidents unresolved > 48 hours | 100% | Incident tracking table |

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |

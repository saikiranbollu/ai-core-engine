# Sandbox and Ephemeral Data Governance

**Document ID**: AICE-GOV-010
**Version**: 1.0.0
**Classification**: Internal — Infineon Technologies
**Owner**: Platform Team + AI Governance Lead + Safety Manager (for Zone A)
**Last Updated**: 2026-04-20
**Applies to**: All AICE workspaces — Zone A (`mcal`), Zone B (`illd`), Zone C (`foss-bsp`)
**Status**: NEW — closes a governance blind spot identified in v2.x docs

---

## 1. Purpose

The AICE Ephemeral Sandbox is a per-session scratch workspace that allows Domain Assistants to ingest arbitrary user-uploaded content (HW PDFs, customer specs, meeting notes, compiler logs, ad-hoc files) and use it for retrieval augmentation during the session. Content expires at session TTL.

This is a useful feature, but it creates a governance blind spot: **ephemeral content that influences AI-generated outputs that ship to customers** has, to date, not been systematically snapshotted, classified, or audit-tracked. This document closes that gap.

---

## 2. Scope

**In scope:**
- Ephemeral Sandbox (per-session user-uploaded content)
- Semantic cache entries derived from sandbox-grounded generations
- Pattern store entries that could potentially originate from sandbox-grounded generations

**Out of scope:**
- Permanent KG content (covered by DATA_GOVERNANCE_POLICY)
- Transient LLM context assembly (covered by provenance chain spec — GOVERNANCE_IMPLEMENTATION_PLAN §3.1)

---

## 3. The Problem (brief)

Sandbox content can be:
- **Confidential (customer NDA, Infineon pre-release)** — potentially routed to the wrong LLM if data class isn't enforced
- **Adversarial (prompt-injection payloads in ingested PDFs)** — a known LLM attack surface
- **PII-laden (meeting notes with attendee names, email threads)** — GDPR / DPDP risk
- **Safety-relevant (HW errata that materially change generated driver behavior)** — if the sandbox evaporates, you cannot reproduce the generation, breaking ISO 26262-8 Clause 11 validity and EU AI Act Art. 12 record-keeping

Current state: sandbox content lives in Redis session memory + per-session Qdrant collection, deleted at TTL. No snapshot. No enforced data-class inheritance. No content-type guard. No PII scrub. No audit of which generation referenced which sandbox file.

Eight concrete failure modes this creates:

| # | Failure mode | Consequence |
|---|---|---|
| 1 | Provenance chain broken (cannot reconstruct context) | EU AI Act Art. 12 record-keeping fails; auditor cannot verify |
| 2 | Re-qualification cannot reproduce generation | ISO 26262-8 Clause 11 validity check fails |
| 3 | Field-failure traceback has a hole | INCIDENT_RESPONSE Phase 0 cannot recover sandbox content |
| 4 | Data-class drift (wrong LLM route) | Customer NDA content may reach Copilot |
| 5 | PII in sandbox reaches LLM | GDPR / DPDP exposure |
| 6 | Prompt injection via ingested PDFs | Arbitrary hijack of LLM behavior |
| 7 | Cross-session cache bleed via semantic similarity | Session isolation violated |
| 8 | Pattern store contamination by sandbox content | Permanent KG contaminated by ephemeral/un-vetted data |

---

## 4. Controls (C1–C8)

The following 8 controls, applied together, close the governance gap. Each is described with rationale, specification, and zone applicability.

### C1. Sandbox Content Snapshot on Generation

**Requirement.** Whenever a DA generates output grounded (partially or wholly) on sandbox content, a **snapshot of the referenced sandbox files** is persisted alongside the `response_archive` record for that generation.

**Specification.**
- Snapshot scope: only the files actually referenced in retrieval for that generation (not the whole sandbox). Tracked via Qdrant chunk IDs → file mapping.
- Storage: WORM-backed blob storage (S3 Object Lock or equivalent — see DATA_GOVERNANCE_POLICY §6.3).
- Content-addressable: SHA-256 hash of file contents is the key. Identical file referenced in 100 generations = 1 blob, 100 references.
- Metadata: filename at upload time, sha256, file size, content-type (MIME), session_id, upload timestamp, first-reference timestamp, data class (§C3).

**Retention.**

| Zone | Retention |
|---|---|
| A (mcal) | Product lifetime + 10 years (same as `review_evidence`) |
| B (illd) | 3 years minimum (aligns with audit_logs) |
| C (foss-bsp) | 3 years minimum |

### C2. Provenance Chain Includes Sandbox References

**Requirement.** The `response_archive.provenance.context_sources.sandbox_docs` field becomes structured (not a name list):

```json
"sandbox_docs": [
  {
    "name": "customer_spec_v3.pdf",
    "sha256": "a1b2c3d4e5f6...",
    "mime_type": "application/pdf",
    "snapshot_ref": "s3://aice-worm/sandbox-snapshots/2026/04/20/a1b2c3d4.pdf",
    "data_class": "customer_nda",
    "parsed_chunks_used": ["sb_chunk_5", "sb_chunk_12"],
    "parser_version": "pdf_extractor_llm_v2.3.1",
    "content_type_guard_verdict": "clean",
    "pii_scrub_verdict": "clean",
    "upload_timestamp": "2026-04-20T10:15:00Z",
    "session_id": "CIA_20260420_101500"
  }
]
```

**Implementation.** Extension of the provenance schema in GOVERNANCE_IMPLEMENTATION_PLAN §3.1 / GAP-01.

### C3. Data-Class Inheritance on Sandbox Ingestion

**Requirement.** Sandbox ingestion triggers the same deterministic data-class classifier used on the prompt path (AI_USAGE_POLICY §7.1). The session's effective data class becomes:

```
session_effective_class = max(workspace_baseline_class, max(class(file) for file in sandbox))
```

where `max` uses the lattice: Public < Internal-non-sensitive < Infineon-confidential < Customer-NDA.

**Enforcement.**
- At file upload: classifier runs; class recorded in sandbox metadata
- If uploaded class > current session class: session is upgraded, user is notified, routing adjusted (Cerbos policy re-evaluated)
- **Downgrade is not permitted** — once a session has touched Customer-NDA content, it stays on GPT4IFX for the rest of the session
- If an upload exceeds what the user's principal is allowed to handle in the current workspace: upload rejected, session unchanged

**Zone-specific:**

| Zone | Baseline | On Customer-NDA upload | On Infineon-confidential upload |
|---|---|---|---|
| A (mcal) | Customer-NDA (if customer workspace) or Infineon-confidential | Already at max; continue | Continue |
| B (illd) | Public or Internal | Upgrade session to GPT4IFX-only + warn user OR reject if principal not authorized | Upgrade session; warn |
| C (foss-bsp) | Public | Upgrade or reject | Upgrade; warn — IP leak risk to upstream |

### C4. Content-Type Guard on Sandbox Path

**Requirement.** Sandbox-ingested files pass through the same content-type guard applied to KG ingestion (DATA_GOVERNANCE_POLICY §4.1).

**Specification.**
- Detect and strip imperative-text patterns typical of prompt-injection attacks ("ignore previous instructions", "you are now", role-hijack patterns, invisible Unicode tag smuggling, base64-encoded instruction blocks)
- Detect and flag suspicious structural patterns: zero-width characters, homoglyphs in instruction-like contexts, unusual Unicode direction overrides
- Verdict recorded: `clean`, `sanitized` (found and stripped), `blocked` (high-risk content; upload rejected with reason)
- Verdict written to `sandbox_file.content_type_guard_verdict` and to `provenance.sandbox_docs[*].content_type_guard_verdict`

**Zone-specific:** Same control across all zones. Higher-vigilance patterns for Zone A (customer deliveries).

### C5. PII Scrubber on Sandbox-Derived Chunks

**Requirement.** Before any sandbox-derived content is added to an LLM prompt, PII scrubber (GAP-16) runs on the extracted text chunks, not just on the raw prompt.

**Specification.**
- Same NER-based scrubber used on prompt path (DATA_GOVERNANCE_POLICY §8.2)
- Patterns scrubbed: personal names, email addresses, phone numbers, postal addresses, IP addresses identifying individuals
- Verdict recorded: `clean`, `scrubbed` (PII found and replaced with role tokens), `blocked` (excessive PII density; upload rejected)
- Scrub events logged (without the scrubbed content)

### C6. Isolation Boundaries

**Requirement.** Sandbox content is strictly session-scoped and never leaks into permanent or cross-session storage.

**Specification.**

| Storage layer | Sandbox data treatment |
|---|---|
| Redis session state | Keys namespaced `sandbox:{session_id}:*`; TTL enforced; explicit delete on session end |
| Qdrant | **Per-session collection** `sandbox_{session_id}`; deleted at session TTL; **never** written to permanent workspace collections |
| Semantic cache | Entries from sandbox-grounded generations: **not cached for cross-session reuse**. Flag `cache_shareable=false` written at generation time; cache lookup respects flag |
| Pattern store | See C7 (firewall) |
| WORM snapshot | Content-addressable; not session-scoped, but accessible only to provenance queries (not retrieval) |

### C7. Pattern Store Firewall

**Requirement.** The FeedbackSink → PatternStore pipeline **refuses** to create patterns from generations whose provenance includes sandbox references, unless the sandbox content has been formally promoted to the permanent KG through admin-tier ingestion.

**Specification.**
- Pre-pattern check: `if response_archive.provenance.context_sources.sandbox_docs is not empty: block pattern creation`
- Bypass path: sandbox file can be formally ingested to permanent KG (admin approval + standard parser + new `ingestion_jobs` record) — only then can generations based on that (now-permanent) content contribute patterns
- Blocked attempts logged to `governance_incidents` at LOW severity for monitoring (not for individual-user alerting; it's expected)

**Rationale.** Prevents customer-NDA content, un-vetted HW docs, or content that passed only the content-type guard (not the full KG quality gates) from contaminating the permanent pattern store.

### C8. Full Audit Trail for Sandbox Lifecycle

**Requirement.** Every sandbox operation logged to `audit_logs` with session context.

**Operations logged:**

| Operation | Fields |
|---|---|
| `sandbox_upload` | session_id, user_principal, filename, sha256, size, mime_type, data_class, content_type_guard_verdict, pii_scrub_verdict, accepted/rejected, reject_reason |
| `sandbox_reference` | session_id, generation_id (→ response_archive), file_sha256, chunks_referenced |
| `sandbox_expire` | session_id, expired_at, expired_files_count (just counts; file details remain queryable via WORM snapshots) |
| `sandbox_promote_to_kg` | session_id, file_sha256, target_workspace, ingestion_job_id, promoted_by, approver |
| `sandbox_data_class_upgrade` | session_id, old_class, new_class, trigger_file_sha256 |

---

## 5. Zone-Specific Policy

| Control | Zone A (mcal) | Zone B (illd) | Zone C (foss-bsp) |
|---|---|---|---|
| C1 Snapshot on generation | ✅ Mandatory; WORM; product lifetime + 10y | ✅ Mandatory; standard storage; 3y | ✅ Mandatory; 3y |
| C2 Provenance sandbox_refs | ✅ | ✅ | ✅ |
| C3 Data-class inheritance | ✅ | ✅ | ✅ |
| C4 Content-type guard | ✅ | ✅ | ✅ |
| C5 PII scrubber | ✅ | ✅ | ✅ |
| C6 Isolation boundaries | ✅ | ✅ | ✅ |
| C7 Pattern store firewall | ✅ | ✅ | ✅ |
| C8 Audit trail | ✅ | ✅ | ✅ |
| **ASIL-D override** (NEW) | **Sandbox-grounded generation for ASIL-D cannot use AUTO/QUICK regardless of confidence score. Forces FULL + independent + safety manager per AI_USAGE_POLICY §5.1.** | N/A | N/A |
| **Upstream contribution restriction** (NEW) | N/A | N/A | **Zone C upstream PRs must NOT include code generated from sandbox content unless the sandbox content is itself public upstream material. Protects Infineon IP from leakage via Zephyr/NuttX contributions.** |

### 5.1 Zone A ASIL-D special rule

Sandbox-grounded generations targeting ASIL-D elements are inherently higher risk because:
- The grounding content is un-vetted (not gone through KG ingestion quality gates)
- Reproducibility depends on WORM snapshot integrity
- A sandbox PDF with a subtle error (wrong register bitfield, wrong timing constraint) can silently produce wrong ASIL-D code

**Rule:** ASIL-D + sandbox-grounded → FULL + independent + safety manager review mandatory, confidence score cannot bypass. The reviewer must explicitly acknowledge: "I verified the sandbox content against the authoritative source." This acknowledgment is recorded in `review_evidence.sandbox_source_verified: true`.

### 5.2 Zone C upstream contribution rule

If an Infineon engineer generates Zephyr/NuttX BSP code with CIA/CTA grounded on a sandbox HW errata PDF, that generated code **cannot be contributed upstream** unless:
- The sandbox content is itself already public (e.g., a publicly-posted erratum) — then it's fine
- OR the Infineon content has been formally cleared for upstream disclosure through standard Infineon IP processes

**Enforcement:** Upstream contribution checklist in AICE-GOV-009 §5.3 extended with a new line: "Confirm AI-generated code was NOT grounded on any non-public sandbox content" — signed off by Module Lead before PR submission.

---

## 6. Workflow Examples

### 6.1 Normal Zone A flow (customer-NDA workspace)

```
Engineer starts session in workspace mcal_customer_X
  session_effective_class = Customer-NDA (baseline)
    ↓
Engineer uploads customer_spec_v3.pdf to sandbox
  → content_type_guard: clean
  → pii_scrubber: 2 names scrubbed → replaced with role tokens
  → data_class: Customer-NDA (matches session)
  → accepted; sandbox_upload logged
    ↓
CIA generates code grounded on sandbox + KG
  → response_archive.provenance.sandbox_docs populated with C2 schema
  → sandbox_reference logged
  → ASIL-D target → FULL + independent + safety manager routing forced
    ↓
Reviewer does FULL review
  → Verifies sandbox content against authoritative source (Jama PRQ)
  → Marks review_evidence.sandbox_source_verified = true
  → Independent reviewer + safety manager sign off
  → FeedbackSink: APPROVE
    ↓
Pattern store firewall (C7) refuses pattern creation
  → Reason: sandbox-grounded generation; patterns not promoted
    ↓
Session ends; sandbox content deleted from Redis + Qdrant per-session collection
  → WORM snapshot persists for product lifetime + 10y
  → sandbox_expire logged
```

### 6.2 Zone B flow with data-class upgrade

```
Engineer starts session in workspace illd
  session_effective_class = Public (baseline)
  Routing: GPT4IFX or Copilot allowed
    ↓
Engineer uploads internal_errata_tc4xx_preA0.pdf to sandbox
  → content_type_guard: clean
  → data_class classifier: detects "Infineon Confidential" stamp → Infineon-confidential
  → Session upgrade triggered: Public → Infineon-confidential
  → User notified: "Session upgraded to GPT4IFX-only due to sensitive upload"
  → sandbox_data_class_upgrade logged
    ↓
Copilot routing now blocked for remainder of session
  → Cerbos policy re-evaluated: GPT4IFX enforced
    ↓
Rest of flow proceeds with GPT4IFX only
```

### 6.3 Zone C flow with upstream contribution gate

```
Engineer generates Zephyr CAN driver grounded on sandbox TC4xx errata
  → response_archive.provenance.sandbox_docs: [errata_doc]
    ↓
Engineer prepares upstream PR
    ↓
Upstream contribution checklist (AICE-GOV-009 §5.3):
  - "AI-generated code NOT grounded on non-public sandbox content?" → FAIL
    ↓
PR submission BLOCKED at Infineon internal staging
  → Module Lead + IP team review
  → Options:
    (a) Infineon publishes the errata externally → unblocks contribution
    (b) Regenerate code without sandbox grounding → proceed
    (c) Keep code internal (don't contribute upstream)
```

---

## 7. Implementation Status and Sprint Plan

Tracks to GOVERNANCE_IMPLEMENTATION_PLAN v2.2.0 GAP-22.

| Control | Current status | Target sprint |
|---|---|---|
| C1 Snapshot on generation | Not implemented | Sprint 12 (schema + WORM backend) |
| C2 Provenance sandbox_refs | Not implemented | Sprint 12 (bundled with GAP-01 provenance chain) |
| C3 Data-class inheritance | Partially (classifier exists, sandbox path not wired) | Sprint 11 |
| C4 Content-type guard | Partially (on KG path; sandbox path not wired) | Sprint 11 |
| C5 PII scrubber on sandbox | Not implemented (bundled with GAP-16) | Sprint 11-12 |
| C6 Isolation boundaries | Partial (Redis yes; Qdrant per-session TBC; cache flag not implemented) | Sprint 12 |
| C7 Pattern store firewall | Not implemented | Sprint 12 |
| C8 Audit trail | Partial (sandbox_upload yes; other ops missing) | Sprint 12 |
| Zone A ASIL-D override | Not implemented | Sprint 12 (bundled with GAP-19) |
| Zone C upstream gate | Not implemented | Sprint 13 (bundled with AICE-GOV-009 §5.3 checklist) |

---

## 8. Metrics

| Metric | Target | Source |
|---|---|---|
| Sandbox content-type guard block rate | < 1% of uploads (higher = active attack or corpus hygiene issue) | audit_logs |
| PII scrub rate on sandbox content | Track trend; high rate → engineer training | audit_logs |
| Data-class upgrade frequency | Track — high rate suggests workspace policy review needed | audit_logs |
| Pattern store firewall refusals | Track | governance_incidents |
| Sandbox-grounded ASIL-D generations | Track; each requires FULL + indep + safety mgr | review_evidence |
| WORM snapshot integrity (daily check) | 100% | WORM provider |
| Sandbox reference provenance completeness | ≥ 99% of sandbox-grounded generations | response_archive |

---

## 9. Relationship to Other Documents

| Document | Relationship |
|---|---|
| AICE_SYSTEM_CARD v2.2.0 §5.1c, §7.2 | Adds sandbox lifecycle stage, failure modes |
| DATA_GOVERNANCE_POLICY v1.3.0 §6, §8.2, §12 | Sandbox retention, PII scrubber scope, AIBOM references sandbox |
| INCIDENT_RESPONSE v2.1.0 §4 Phase 0, new root cause | Field-failure traceback includes sandbox content recovery |
| GOVERNANCE_IMPLEMENTATION_PLAN v2.2.0 GAP-22 | Implementation sprint plan |
| AICE-GOV-007 TOOL_QUALIFICATION_PLAN §7 | Re-qualification trigger: sandbox parser change |
| AI_USAGE_POLICY v2.1.0 §5.1, §7 | ASIL-D + sandbox rule reference |

---

## 10. Open Items

| Item | Owner | Due |
|---|---|---|
| Implement sandbox snapshot (C1) + WORM backend | Platform Team | Sprint 12 |
| Wire PII scrubber + content-type guard to sandbox path (C4, C5) | Platform Team | Sprint 11 |
| Implement data-class inheritance (C3) | Platform Team + Cerbos policy | Sprint 11 |
| Add pattern store firewall (C7) | Platform Team | Sprint 12 |
| Extend upstream checklist (Zone C) | FOSS Compliance Officer | Sprint 13 |
| Sandbox-grounded ASIL-D review workflow | Safety Manager + Platform | Sprint 12 |
| Tabletop exercise: field-failure with sandbox content recovery | AI Governance Lead + Customer Interface Lead | After C1 implemented |

---

## 11. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-04-20 | ATV MC D SW VDF | Initial release — closes sandbox governance gap identified in v2.x suite. Defines 8 controls (C1-C8) with zone-specific application |

**Approval:**

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| Platform Team Lead | __________ | __________ |
| Safety Manager (Zone A rule) | __________ | __________ |
| Data Protection Officer (PII scope) | __________ | __________ |

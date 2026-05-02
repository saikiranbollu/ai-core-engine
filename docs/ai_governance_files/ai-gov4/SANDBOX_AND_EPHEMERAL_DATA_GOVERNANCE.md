# Sandbox and Ephemeral Data Governance

| Field | Value |
|---|---|
| **Document ID** | AICE-GOV-010 |
| **Version** | 3.0.0 |
| **Date** | 2026-05-02 |
| **Classification** | Internal — Infineon Technologies |
| **Owner** | Platform Team + AI Governance Lead + Safety Manager (Zone A) |
| **Applies to** | All AICE workspaces — Zone A (`mcal`), Zone B (`illd`), Zone C (`foss-bsp`) |
| **Implementation status** | **Sandbox feature: implemented (Sprint 4-5).** Governance controls C1-C10 in this document: partial (see §7) |

---

## 1. Purpose

The AICE Ephemeral Sandbox is a per-session scratch workspace allowing Domain Assistants to ingest user-uploaded content — primarily **structured EA models (`.qeax` files via the `ifxpyarch` MCP server)** for MCAL architecture work, plus PDFs, customer specs, meeting notes, compiler logs — for session-bounded retrieval augmentation. It layers on top of the persistent KG (Neo4j + Qdrant) via `HybridGraphService`, with `_patched` and `_injected` flags on results that combine sandbox and production data.

This document specifies governance for the implemented sandbox feature. The dominant concern is **reproducibility of sandbox-grounded generations** that flow into customer-delivered software — particularly Zone A ASIL-D outputs.

---

## 2. Scope

**In scope:**
- Ephemeral Sandbox (NetworkX graph + ChromaDB in-memory vectors, per-session)
- The four sandbox MCP tools: `sandbox_upload`, `sandbox_query`, `sandbox_status`, `sandbox_clear`, plus `sandbox_diff`
- The `HybridGraphService` patch/inject behavior layering sandbox on top of production Neo4j
- Semantic cache entries derived from sandbox-grounded generations
- Pattern store entries that could originate from sandbox-grounded generations
- Sandbox ingestion via the `ifxpyarch` MCP server (QEAX path) — same EA parser as the persistent ingestion path
- Sandbox ingestion via PDF/markdown/code parsers (non-QEAX path)

**Out of scope:**
- Permanent KG content (covered by DATA_GOVERNANCE_POLICY)
- Transient LLM context assembly (covered by provenance chain spec — GOVERNANCE_IMPLEMENTATION_PLAN GAP-01)
- The `ifxpyarch-mcp` server itself (covered by AICE_SYSTEM_CARD §4.5)

---

## 3. Implementation Reality

### 3.1 Storage Architecture

| Layer | Technology | Lifetime | Isolation |
|---|---|---|---|
| Sandbox graph | **NetworkX** in-memory | Per-session, destroyed at session end | Python object graph dereferenced; physically released from RAM |
| Sandbox vectors | **ChromaDB ephemeral mode** (true in-memory) | Per-session, destroyed at session end | ChromaDB ephemeral backend dereferenced; physically released |
| Working memory | Redis (prod) / in-memory (dev) | Session TTL (1h default) | Keys namespaced `sandbox:{session_id}:*` |

**Note:** This is **stronger isolation** than per-session Qdrant collections would be. ChromaDB ephemeral mode literally disappears from RAM when the Python process dereferences it; there is no on-disk artifact to clean up.

### 3.2 Query Layering — `HybridGraphService`

The implemented `HybridGraphService` classifies tools into **shallow** vs **deep**:

| Tool category | Examples | Behavior when sandbox active |
|---|---|---|
| **Shallow** | `search_database`, `search_nodes`, `get_node_by_id`, `get_neighbors`, `sandbox_query`, `sandbox_status`, `query_api_function`, `get_type_definition`, `query_dependencies`, `get_distribution` | Query sandbox NetworkX/ChromaDB **only** |
| **Deep** | `find_coverage_gaps`, `build_traceability_matrix`, `find_requirement_traces`, `shortest_path`, `analyze_hw_sw_links`, `detect_communities`, `get_ontology_compliance`, `get_coverage_report`, `get_graph_statistics`, `get_failure_patterns`, `get_review_analytics`, `get_learning_metrics`, `execute_cypher` | Query production Neo4j; **patch records with sandbox overrides** (`_patched=True`, `_origin=sandbox`); **inject sandbox-only nodes** (`_injected=True`); fall back to sandbox-only if Neo4j unreachable |

**Critical implication:** When a deep tool runs during an active sandbox session, the result set is a **mixture of production and sandbox data**. The `_patched`, `_injected`, `_origin` flags exist in code but **must be surfaced to the human reviewer** for governance to function — see C9.

### 3.3 Score Fusion

Sandbox results receive a configurable boost (`ephemeral_boost`, default `+0.05`) during ranking. This is documented in `SandboxQuerier.search()`. Governance implication: **for ASIL-D sessions this boost must be 0**, or sandbox content can outrank reviewed production KG (see C10).

### 3.4 RLM Integration

Sandbox state is one of four signals in the RLM (Recursive Language Model) trigger heuristic. When `sandbox_active=true`, RLM planning is more likely to fire and the planning prompt includes the sandbox file manifest. This means RLM plans can intentionally distribute sub-queries between sandbox and production.

### 3.5 Sandbox Tools (Public Tier)

| Tool | Purpose | Limits |
|---|---|---|
| `sandbox_upload` | Parse user-provided documents into sandbox graph + vectors | 20 files / 50MB total per session |
| `sandbox_query` | Search ephemeral stores | None |
| `sandbox_status` | Inspect loaded files, node counts, storage stats | None |
| `sandbox_clear` | Explicit release before TTL | None |
| `sandbox_diff` | Show what changed in sandbox vs production (added / modified / unchanged) | None |

`sandbox_diff` is the existing tool that surfaces patch/inject deltas — it's the right instrument for the governance C9 control below.

### 3.6 Common Ingestion via ifxpyarch

For QEAX inputs, sandbox ingestion uses the **same `IngestionPipeline/Parsers/ea_parser.py`** that the persistent KG ingestion uses. This means:

- Sandbox QEAX ingestion is structured (deterministic schema, EA metamodel)
- No LLM-assisted parsing for QEAX path (lower prompt-injection risk)
- The same node/relationship types (ontology-aligned) — sandbox content is schema-compatible with production
- Patches/injects are well-formed: `_canonical_id_from_record()` resolves prod records to canonical IDs that match sandbox node IDs

**Governance benefit:** the QEAX path is the safer ingestion path. Where users have a choice, prefer QEAX over PDF for architecture content.

---

## 4. The Governance Problem

Sandbox content can be:
- **Confidential (Customer NDA, Infineon pre-release)** — must inherit session data class
- **Adversarial (prompt-injection in PDFs)** — non-QEAX path is the primary risk
- **PII-laden (meeting notes, email threads)** — defensive scrubbing required
- **Safety-relevant (HW errata, customer-supplied design)** — if sandbox evaporates, generation cannot be reproduced

**Reproducibility is the dominant concern.** A Zone A ASIL-D customer delivery may include code generated 18 months ago, partly grounded on a sandbox-uploaded customer spec. If a field failure occurs, the field-failure→AI-lineage traceback (INCIDENT_RESPONSE §9.3 Phase 0) requires reconstructing that exact sandbox content. Without a snapshot at generation time, this fails — breaking EU AI Act Art. 12 record-keeping and ISO 26262-8 Clause 11 validity.

---

## 5. Controls (C1–C10)

The following 10 controls together close the governance gap. C1–C8 are the original sandbox controls; **C9 and C10 are new in v3.0.0**, specific to the implemented `HybridGraphService` behavior.

### C1. Sandbox Content Snapshot on Generation

**Requirement.** Whenever a DA generates output grounded (partially or wholly) on sandbox content, a snapshot of the referenced sandbox files is persisted alongside the `response_archive` record.

**Specification.**
- Scope: only files actually referenced in retrieval for that generation (tracked via NetworkX node IDs / ChromaDB chunk IDs → file mapping)
- Storage: WORM-backed blob storage (S3 Object Lock or equivalent)
- Content-addressable: SHA-256 of file contents is the key
- Metadata: filename at upload, sha256, size, content-type, session_id, upload timestamp, first-reference timestamp, data class (§C3), parser used (ifxpyarch QEAX vs PDF/markdown/code)

**Retention:**

| Zone | Retention |
|---|---|
| A (mcal) | Product lifetime + 10 years |
| B (illd) | 3 years minimum |
| C (foss-bsp) | 3 years minimum |

### C2. Provenance Chain Includes Sandbox References

**Requirement.** The `response_archive.provenance.context_sources.sandbox_docs` field is structured:

```json
"sandbox_docs": [
  {
    "name": "TC4xx_Adc_arch_v3.qeax",
    "sha256": "a1b2c3...",
    "mime_type": "application/x-sqlite3",
    "snapshot_ref": "s3://aice-worm/sandbox-snapshots/2026/05/02/a1b2c3.qeax",
    "data_class": "infineon_confidential",
    "parser": "ifxpyarch_v1.0.0",
    "parsed_chunks_used": ["sb_node_42", "sb_node_57"],
    "content_type_guard_verdict": "clean",
    "pii_scrub_verdict": "clean",
    "upload_timestamp": "2026-05-02T10:15:00Z",
    "session_id": "SAGA_20260502_101500"
  }
]
```

For deep-tool generations, also include:

```json
"hybrid_query_metadata": {
  "deep_tools_used": ["find_requirement_traces", "build_traceability_matrix"],
  "patched_records_count": 7,
  "injected_records_count": 12,
  "ephemeral_boost_used": 0.05
}
```

### C3. Data-Class Inheritance on Upload

**Requirement.** Sandbox upload triggers the same data-class classifier as the prompt path. Session class becomes:

```
session_effective_class = max(workspace_baseline_class, max(class(file) for file in sandbox))
```

Lattice: Public < Internal < Infineon-confidential < Customer-NDA.

**Enforcement:**
- At upload: classifier runs; class recorded
- If uploaded class > current session class: session upgraded; user notified; routing adjusted (Cerbos re-evaluation)
- **Downgrade not permitted** — once a session has touched Customer-NDA content, it stays Customer-NDA-routed for the remainder of the session
- If upload exceeds principal authorization for current workspace: rejected, session unchanged
- **Cerbos enforces** the routing change — affected DAs re-route subsequent LLM calls to the correct backend (Copilot Enterprise with §4.3 preconditions, or GPT4IFX if precondition fails)

### C4. Content-Type Guard on Sandbox Path

Two parser paths, two risk profiles:

**QEAX path (ifxpyarch parser) — low risk:**
- Structured SQLite content; deterministic schema
- No natural-language imperative content typically reaches LLM context
- Content-type guard validates: file is valid QEAX (SQLite header check, table structure check); no embedded blobs containing executables; tagged-value strings sanitized for prompt-injection patterns before being included in any LLM context
- Verdict recorded: `clean`, `sanitized`, `blocked`

**Non-QEAX path (PDF, markdown, plain-text, code) — higher risk:**
- Apply full prompt-injection defense: detect and strip imperative-text patterns ("ignore previous instructions", "you are now", role-hijack patterns); zero-width characters; homoglyphs; Unicode direction overrides; base64-encoded instruction blocks
- Verdict recorded: `clean`, `sanitized`, `blocked`

Verdict written to `sandbox_file.content_type_guard_verdict` and to `provenance.sandbox_docs[*].content_type_guard_verdict`.

### C5. PII Scrubber on Sandbox-Derived Chunks

**Requirement.** Before any sandbox-derived content enters an LLM prompt, PII scrubber runs on the extracted text chunks.

- Same NER-based scrubber as prompt path
- Patterns: personal names, email addresses, phone numbers, postal addresses, IP addresses identifying individuals
- Verdict: `clean`, `scrubbed`, `blocked` (excessive PII density rejects the upload)
- Logged without the PII content

### C6. Isolation Boundaries (Updated for actual implementation)

| Storage layer | Treatment |
|---|---|
| Redis session state | Keys `sandbox:{session_id}:*`; TTL enforced; explicit delete at session end |
| **NetworkX graph** | Python object graph; per-session; dereferenced at session end (RAM physically released by GC) |
| **ChromaDB ephemeral vectors** | True in-memory mode; per-session; dereferenced at session end |
| Persistent Neo4j / Qdrant | Sandbox content **never written** to persistent stores (the `HybridGraphService` only reads from prod and patches results in-memory) |
| Semantic cache | Generations grounded on sandbox content set `cache_shareable=false`; cache lookups respect flag — no cross-session bleed |
| Pattern store | See C7 |
| WORM snapshot | Content-addressable; not session-scoped; accessible only via provenance queries (not retrieval) |

**Verification:** session-end inspection should confirm zero leakage. Test canary: spin a session, upload a synthetic document with a unique marker, end the session, then in a new session run searches for the marker — must return zero hits.

### C7. Pattern Store Firewall

**Requirement.** The FeedbackSink → PatternStore pipeline refuses to create patterns from generations whose provenance includes sandbox references, unless the sandbox content has been formally promoted to persistent KG via admin-tier ingestion.

**Specification:**
- Pre-pattern check: `if response_archive.provenance.context_sources.sandbox_docs is not empty: block pattern creation`
- Bypass path: sandbox file can be formally ingested to persistent KG (admin approval + standard parser + new `ingestion_jobs` record). Only then can generations based on that (now-persistent) content contribute patterns.
- Blocked attempts logged to `governance_incidents` at LOW severity (expected; for monitoring trends)

### C8. Full Audit Trail for Sandbox Lifecycle

**Operations logged to `audit_logs`:**

| Operation | Fields |
|---|---|
| `sandbox_upload` | session_id, principal, filename, sha256, size, mime_type, parser (ifxpyarch / pdf / markdown / code), data_class, content_type_guard_verdict, pii_scrub_verdict, accepted/rejected, reject_reason |
| `sandbox_reference` | session_id, generation_id (→ response_archive), file_sha256, chunks_referenced, query_layer (shallow / deep), patched_count, injected_count |
| `sandbox_expire` | session_id, expired_at, expired_files_count |
| `sandbox_promote_to_kg` | session_id, file_sha256, target_workspace, ingestion_job_id, promoted_by, approver |
| `sandbox_data_class_upgrade` | session_id, old_class, new_class, trigger_file_sha256 |
| `sandbox_diff_invoked` | session_id, principal, generation_id (target of review), diff_summary |

### C9. Patch / Inject Transparency to Reviewer (NEW v3.0.0)

**Problem.** When a deep tool (e.g., `find_requirement_traces`) returns a result mixing production records (`_origin=production`), sandbox-overridden production records (`_patched=True`, `_origin=sandbox`), and sandbox-only injected records (`_injected=True`, `_origin=sandbox`), the human reviewer must be able to distinguish them. Otherwise the reviewer may approve a generation believing the entire context came from the production KG when in fact some came from un-vetted sandbox content.

**Requirement.**

- Review UI must display the `_origin`, `_patched`, `_injected` flags on every record returned by a deep tool during an active sandbox session
- When a sandbox-grounded generation reaches the Review Gate, the reviewer interface explicitly shows:
  - Which retrieval calls used deep tools
  - How many records were `_patched` or `_injected`
  - Direct link to invoke `sandbox_diff` for a side-by-side view
- For Zone A ASIL-D + sandbox-grounded generations: reviewer **must invoke `sandbox_diff`** before approving; tool invocation recorded in `review_evidence.sandbox_diff_reviewed=true` (see also `review_evidence.sandbox_source_verified`)

### C10. ASIL-D `ephemeral_boost` Override (NEW v3.0.0)

**Problem.** The default `ephemeral_boost = +0.05` gives sandbox content a slight ranking advantage over production KG during active experimentation. For Zone A ASIL-D, this is unacceptable — un-vetted sandbox content must not outrank reviewed production data.

**Requirement.**

- For Zone A sessions targeting ASIL-D artifacts: `ephemeral_boost` is forced to `0.0` regardless of configuration
- Enforcement: `evaluate_confidence` and the retrieval pipeline check the session's ASIL target; if ASIL-D, override the boost to 0
- Logged to `audit_logs` for every ASIL-D session that touches sandbox content
- This is implementation-level: no user override permitted

---

## 6. Zone-Specific Policy

| Control | Zone A (mcal) | Zone B (illd) | Zone C (foss-bsp) |
|---|---|---|---|
| C1 Snapshot on generation | ✅ Mandatory; WORM; product lifetime + 10y | ✅ Mandatory; standard storage; 3y | ✅ Mandatory; 3y |
| C2 Provenance sandbox_refs | ✅ | ✅ | ✅ |
| C3 Data-class inheritance | ✅ | ✅ | ✅ |
| C4 Content-type guard | ✅ (QEAX-light + non-QEAX-strict) | ✅ | ✅ |
| C5 PII scrubber | ✅ | ✅ | ✅ |
| C6 Isolation boundaries | ✅ | ✅ | ✅ |
| C7 Pattern store firewall | ✅ | ✅ | ✅ |
| C8 Audit trail | ✅ | ✅ | ✅ |
| C9 Patch/inject transparency | ✅ + mandatory `sandbox_diff` for ASIL-D | ✅ | ✅ |
| C10 `ephemeral_boost = 0` override | ✅ Hard-enforced for ASIL-D | N/A | N/A |
| **ASIL-D + sandbox special rule** | **FULL + indep + Safety Mgr + `sandbox_source_verified=true` + `sandbox_diff_reviewed=true`; confidence cannot bypass** | N/A | N/A |
| **Upstream contribution restriction** | N/A | N/A | **No upstream PRs based on non-public sandbox content. AICE-GOV-009 §5.3 checklist + AICE-GOV-010 §6.3 cross-check.** |

### 6.1 Zone A ASIL-D Special Rule

Sandbox-grounded ASIL-D generations require:

| Control | Requirement |
|---|---|
| Review level | FULL + independent reviewer + Safety Manager sign-off |
| Confidence override | Score cannot bypass |
| `sandbox_diff` invocation | Mandatory; recorded in `review_evidence.sandbox_diff_reviewed` |
| Authoritative-source verification | Reviewer compares sandbox content to authoritative source (Jama PRQ, EA model official version, customer-confirmed spec); recorded in `review_evidence.sandbox_source_verified` |
| `ephemeral_boost` | Forced to 0 |
| WORM snapshot | Generated automatically (C1) |

### 6.2 Zone C Upstream Contribution Rule

If a Zephyr/NuttX BSP code is generated by CIA/CTA grounded on sandbox content, it cannot be contributed upstream unless:
- The sandbox content is itself already public (e.g., publicly-posted erratum) — then proceed
- OR the Infineon content has been formally cleared for upstream disclosure through standard Infineon IP processes

Enforcement: AICE-GOV-009 §5.3 upstream contribution checklist line: "Confirm AI-generated code was NOT grounded on any non-public sandbox content."

---

## 7. Implementation Status (v3.0.0)

| Control | Status | Action |
|---|---|---|
| Sandbox feature itself (NetworkX, ChromaDB, HybridGraphService) | ✅ Implemented (Sprint 4-5) | — |
| ifxpyarch QEAX ingestion path | ✅ Implemented | — |
| C1 Snapshot on generation | ❌ Not implemented | Sprint 12 (schema + WORM backend) |
| C2 Provenance sandbox_refs (full schema + hybrid_query_metadata) | ⚠️ Partial (basic refs exist; structured schema not) | Sprint 12 (bundled with GAP-01) |
| C3 Data-class inheritance | ❌ Not implemented | Sprint 11 |
| C4 Content-type guard (sandbox path) | ⚠️ Partial (KG path implemented; sandbox path not wired) | Sprint 11 |
| C5 PII scrubber on sandbox chunks | ❌ Not implemented (bundled with GAP-16) | Sprint 11-12 |
| C6 Isolation boundaries | ✅ Mostly (NetworkX + ChromaDB ephemeral; cache-share flag NOT implemented) | Sprint 12 (cache flag) |
| C7 Pattern store firewall | ❌ Not implemented | Sprint 12 |
| C8 Audit trail (full set of ops) | ⚠️ Partial (sandbox_upload exists; other ops missing) | Sprint 12 |
| **C9 Patch/inject transparency (NEW)** | ❌ Not implemented (flags exist in code; not surfaced to reviewer) | Sprint 12 |
| **C10 `ephemeral_boost=0` for ASIL-D (NEW)** | ❌ Not implemented (boost is currently config-only) | Sprint 11 |
| Zone A ASIL-D + sandbox review override | ❌ Not implemented (bundled with GAP-19) | Sprint 12 |
| Zone C upstream gate | ❌ Not implemented (bundled with AICE-GOV-009 checklist) | Sprint 13 |

**Top implementation priorities:**
1. **C1 (snapshot)** — single most important; without it Art. 12 reproducibility fails
2. **C10 (ASIL-D boost override)** — small change, high impact for ASIL-D safety
3. **C9 (transparency)** — flags exist; just need UI surfacing + sandbox_diff mandate
4. **C3 (data-class inheritance)** — required when uploads change session sensitivity

---

## 8. Workflow Examples

### 8.1 Zone A flow (productive MCAL, customer NDA workspace, QEAX upload)

```
Engineer starts session in workspace mcal_customer_X
  session_effective_class = Customer-NDA (baseline)
  Routing: Copilot Enterprise (subject to §4.3 customer contract verification);
           if customer contract precludes → GPT4IFX
    ↓
Engineer uploads customer_arch_v3.qeax via sandbox_upload
  → ifxpyarch parser extracts components, APIs, configs
  → content_type_guard (QEAX-light): clean
  → pii_scrubber: 2 names scrubbed → role tokens
  → data_class: Customer-NDA (matches session)
  → accepted; sandbox_upload logged
    ↓
SAGA generates architecture analysis via deep tool find_requirement_traces
  → HybridGraphService runs query against persistent Neo4j
  → 7 records patched with sandbox overrides; 12 records injected from sandbox
  → response_archive.provenance.sandbox_docs populated
  → response_archive.provenance.hybrid_query_metadata records counts
  → Confidence score computed; ASIL-D target → FULL + indep + Safety Mgr forced
  → ephemeral_boost = 0 (C10 override)
    ↓
Reviewer sees Review Gate UI with patch/inject indicators (C9)
  → Reviewer invokes sandbox_diff to see what sandbox added
  → Reviewer verifies sandbox content against authoritative source (Jama PRQ + customer-confirmed spec)
  → Marks review_evidence.sandbox_diff_reviewed = true
  → Marks review_evidence.sandbox_source_verified = true
  → Independent reviewer + Safety Manager sign off
  → FeedbackSink: APPROVE
    ↓
Pattern store firewall (C7) refuses pattern creation
  → Reason: sandbox-grounded generation; pattern not created
    ↓
Session ends
  → NetworkX graph dereferenced; ChromaDB physically released; Redis keys deleted
  → WORM snapshot persists for product lifetime + 10y (C1)
  → sandbox_expire logged
```

### 8.2 Zone B flow with data-class upgrade (PDF upload)

```
Engineer starts session in workspace illd
  session_effective_class = Public (baseline)
  Routing: Copilot Enterprise allowed
    ↓
Engineer uploads internal_errata_tc4xx_preA0.pdf via sandbox_upload
  → PDF parser (non-QEAX path): extracts text
  → content_type_guard (non-QEAX-strict): clean
  → data_class classifier: detects "Infineon Confidential" stamp → Infineon-confidential
  → Session upgrade: Public → Infineon-confidential
  → User notified; Cerbos re-evaluation
  → sandbox_data_class_upgrade logged
    ↓
Routing remains Copilot Enterprise (Internal/Infineon-confidential are Copilot-allowed
under §4.3 preconditions)
    ↓
Rest of flow proceeds normally
```

### 8.3 Zone C upstream contribution gate

```
Engineer generates Zephyr CAN driver grounded on sandbox TC4xx errata (Infineon-confidential)
  → response_archive.provenance.sandbox_docs: [errata_doc, data_class=Infineon-confidential]
    ↓
Engineer prepares upstream PR
    ↓
Upstream contribution checklist (AICE-GOV-009 §5.3):
  - "AI-generated code NOT grounded on any non-public sandbox content?" → FAIL
    ↓
PR submission BLOCKED at Infineon internal staging (foss-bsp repo)
  → Module Lead + IP team review
  → Options:
    (a) Infineon publishes the errata externally → unblocks contribution
    (b) Regenerate code without sandbox grounding → proceed
    (c) Keep code internal (don't contribute upstream)
```

---

## 9. Metrics

| Metric | Target | Source |
|---|---|---|
| Sandbox content-type guard block rate | < 1% of uploads (higher = active attack or hygiene issue) | audit_logs |
| PII scrub rate on sandbox content | Track trend; high rate → engineer training | audit_logs |
| Data-class upgrade frequency | Track | audit_logs |
| Pattern store firewall refusals | Track | governance_incidents |
| Sandbox-grounded ASIL-D generations | Each requires FULL + indep + Safety Mgr | review_evidence |
| `sandbox_diff_reviewed` rate (ASIL-D + sandbox) | 100% | review_evidence |
| `sandbox_source_verified` rate (ASIL-D + sandbox) | 100% | review_evidence |
| WORM snapshot integrity (daily check) | 100% | WORM provider |
| Sandbox reference provenance completeness | ≥ 99% | response_archive |
| ASIL-D `ephemeral_boost = 0` enforcement | 100% | audit_logs |

---

## 10. Relationship to Other Documents

| Document | Relationship |
|---|---|
| AICE_SYSTEM_CARD §3.3, §4.5, §5.5 | Defines zones; describes ifxpyarch integration; references this document |
| AI_USAGE_POLICY §4.2, §5.1 | ASIL-D + sandbox special rule; sandbox training in §9 |
| DATA_GOVERNANCE_POLICY §6 retention; §8 PII scope | Sandbox WORM retention; PII scrubber sandbox scope |
| INCIDENT_RESPONSE §4 Phase 0 + root causes | Field-failure traceback includes sandbox recovery; sandbox-specific root causes |
| GOVERNANCE_IMPLEMENTATION_PLAN GAP-22 | Implementation sprint plan |
| AICE-GOV-007 TOOL_QUALIFICATION_PLAN §7 | Re-qualification trigger: sandbox parser change |
| AICE-GOV-009 FOSS_LICENSE_COMPLIANCE §5.3 | Upstream contribution checklist line |
| `docs/architecture/memory-layer.md` (internal) | Implementation reference |
| `src/MemoryLayer/memory/ephemeral_sandbox.py` (code) | Implementation source |
| `ifxpyarch-mcp` (external project) | QEAX parsing source |

---

## 11. Open Items

| Item | Owner | Due |
|---|---|---|
| Implement sandbox snapshot (C1) + WORM backend selection | Platform Team + IT | Sprint 12 |
| Wire PII scrubber + content-type guard to sandbox path (C4, C5) | Platform Team | Sprint 11 |
| Implement data-class inheritance (C3) | Platform Team + Cerbos policy | Sprint 11 |
| Add pattern store firewall (C7) | Platform Team | Sprint 12 |
| Surface `_patched` / `_injected` flags in Review UI; mandate `sandbox_diff` for ASIL-D (C9) | Platform Team + UX | Sprint 12 |
| Hard-enforce `ephemeral_boost = 0` for ASIL-D sessions (C10) | Platform Team | Sprint 11 |
| Extend upstream checklist (Zone C) | FOSS Compliance Officer | Sprint 13 |
| Sandbox-grounded ASIL-D review workflow | Safety Manager + Platform | Sprint 12 |
| Tabletop exercise: field-failure with sandbox recovery | AI Governance Lead + Customer Interface Lead | After C1 implemented |

---

## 12. Document Control

| Field | Value |
|---|---|
| Current version | 3.0.0 |
| Effective date | 2026-05-02 |
| Supersedes | All prior versions |

### Version 3.0.0 — Material Changes

- **Sandbox feature acknowledged as implemented** (Sprint 4-5: NetworkX + ChromaDB in-memory; HybridGraphService with patch/inject layering)
- **ifxpyarch QEAX ingestion path** documented as primary architecture-source path (replacing PDF-extraction)
- **C9 Patch / Inject Transparency to Reviewer** (NEW) — required because deep-tool results mix production and sandbox data
- **C10 ASIL-D `ephemeral_boost` Override** (NEW) — required because the default boost gives un-vetted sandbox priority over reviewed production
- C4 split into QEAX-light vs non-QEAX-strict guard variants
- C6 corrected to reflect actual storage (NetworkX + ChromaDB ephemeral, not per-session Qdrant)
- Implementation status table reflects actual state per-control
- Legacy version-history consolidated

### Approval

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| Platform Team Lead | __________ | __________ |
| Safety Manager (Zone A rules C9, C10) | __________ | __________ |
| Data Protection Officer (PII scope) | __________ | __________ |

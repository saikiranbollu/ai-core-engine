# Data Governance Policy — AI Core Engine

**Document ID**: AICE-GOV-004
**Version**: 3.0.0
**Date**: 2026-05-02
**Classification**: Internal — Infineon Technologies
**Owner**: ATV MC D SW VDF

---

## 0. Version Status

Current: **v3.0.0** (effective 2026-05-02). Supersedes all prior versions. See §12 Document Control for material changes in v3.0.0.

---

## 1. Purpose

This policy defines the rules, responsibilities, and controls governing all data ingested into, stored within, and retrieved from the AI Core Engine (AICE) knowledge infrastructure — Neo4j Knowledge Graph, Qdrant vector store, Redis cache, PostgreSQL audit store — and governs the AIBOM (AI Bill of Materials) artifacts.

---

## 2. Data Classification

| Classification | Definition | Examples in AICE | Handling |
|---|---|---|---|
| **Confidential (Infineon)** | Infineon proprietary engineering data | Productive MCAL source, customer PRQ, HW register maps, internal test results, architecture models, unpublished errata, TC4xx pre-release content, iLLD pre-release branches | **Copilot Enterprise** (subject to AICE_SYSTEM_CARD §4.3 contractual preconditions: EU Data Boundary, no-training, audit access); **GPT4IFX** as fallback or for specialized paths (PDF extraction, RLM); workspace-isolated |
| **Customer NDA-restricted** | Customer-provided or customer-owned artifacts under NDA | Customer variants, customer-specific PRQs, customer ARXML | **Copilot Enterprise** (subject to §4.3 + per-customer contract verification); **GPT4IFX** if customer contract precludes Copilot use; per-customer sub-workspace |
| **Licensed** | Third-party data under license agreement | AUTOSAR SWS specs, MISRA C:2012 rule text | License terms respected; used for retrieval context only; no redistribution |
| **Internal** | Internal operational data | Audit logs, feedback records, confidence scores, session metadata, model registry entries | Retained per §6; access via developer/admin tiers |
| **Public — Infineon-published** | Infineon-controlled content released publicly | Published iLLD source on github.com/Infineon (Infineon Free License); published datasheets; public TC3xx errata | Treated as public content; Copilot Enterprise routing permitted; **AI-output marking still required** |
| **Public — FOSS upstream** | External open-source content | Zephyr upstream (Apache-2.0); NuttX upstream (BSD-3); Linux headers | License preserved; AI outputs consuming FOSS context must respect upstream license (see §11) |
| **Public — general** | Publicly available data | General C references, compiler docs | No special handling |

---

## 3. Authorized Data Sources

### 3.1 Approved Sources

| Source | Data Type | Parser | Classification | Authorization |
|---|---|---|---|---|
| Infineon iLLD repositories | C/H source code | Tree-sitter AST parser | Confidential | Module Lead |
| Jama Connect (AU3GM) | Requirements (SHRQ, PRQ, VS, VR) | Jama XML/JSON parser | Confidential | Requirements Manager |
| Enterprise Architect models (`.qeax`) | Architecture models (SQLite) | **EA parser via `ifxpyarch` MCP** (`IngestionPipeline/Parsers/ea_parser.py`) | Confidential | Architecture Lead |
| HW specification repository | Register maps, timing (PDF) | PDF extractor (LLM-assisted, GPT4IFX) | Confidential | HW team Lead |
| AUTOSAR standard documents | SWS specifications (PDF/XML) | RST/XML parser | Licensed | Platform Team |
| CI/CD pipeline | Test results (JUnit, Polyspace, gcov, MC/DC) | Result processors | Internal | Automated |
| Compiler output | Warnings, errors (GCC/Tasking logs) | Compiler log parser | Internal | Automated |
| **Customer-provided artifacts (NEW)** | Customer PRQs, customer-specific variants, customer ARXML | Jama XML + custom parsers | **Customer NDA-restricted** | Project Lead + Customer approval per contract |

### 3.2 Prohibited Sources

| Source | Rationale |
|---|---|
| Customer data outside scope of the relevant NDA / customer workspace | Customer confidentiality |
| Employee personal data (HR, performance, behavioral) | GDPR / DPDP; employment law |
| Internet-scraped content | IP concerns; quality unverifiable |
| Unlicensed third-party code | License compliance |
| Data from other Infineon divisions without authorization | Data ownership boundaries |
| Data originating in China (per Apr 2026 scope exclusion) | Not in current operating footprint |
| Any content containing credentials, tokens, or keys | Information security |

### 3.3 New Source Approval Process

Any new data source requires approval from:
1. Module Lead (data relevance and ownership)
2. Platform Team (parser availability, data quality)
3. Data Protection Officer (PII screening)
4. **For customer NDA sources:** Contract / Legal review of NDA scope, + customer approval if required per contract (NEW)

---

## 4. Data Quality Criteria

### 4.1 Ingestion Quality Gates

| Quality Check | Enforcement Point | Action on Failure |
|---|---|---|
| Parser validation | Ingestion pipeline | Reject file; log error in `ingestion_jobs` |
| Schema compliance | Node/relationship creation | Reject non-conforming nodes; log validation error |
| Duplicate detection | Pre-ingestion check | Skip or merge (configurable) |
| Relationship integrity | Post-ingestion validation | Log orphan nodes; queue for manual review |
| Embedding generation | Qdrant indexing | Retry with fallback; log embedding failures |
| **Content-type guard (NEW)** | Ingestion pipeline | Strip imperative text from HW docs to mitigate prompt-injection via ingested content; logged |
| **Signed ingestion bundle (NEW)** | Admin ingestion batches | Manifest SHA256 signed with ingestion key; unsigned bundles rejected |

### 4.2 Minimum Completeness per Node Type

| Node Type | Required Fields | Optional Fields |
|---|---|---|
| APIFunction | name, module, return_type, parameters | description, source_file, line_number |
| DataStructure | name, module, fields | description, size_bytes |
| Register | name, address, module | bitfields, reset_value, access_type |
| SoftwareRequirement | id, title, module, status | description, asil_level, verification_method |
| TestCase | id, name, module | requirement_traces, expected_result |

### 4.3 Data Quality Metrics

| Metric | Target | Measurement Tool |
|---|---|---|
| Node completeness (required fields present) | ≥ 95% | `get_graph_statistics` + custom query |
| Relationship coverage (nodes with ≥ 1 relationship) | ≥ 80% | `get_graph_statistics` |
| Traceability chain completeness (Req → Code → Test) | ≥ 70% | `find_coverage_gaps` |
| Embedding coverage (KG nodes with Qdrant vectors) | ≥ 90% | Qdrant collection stats |

---

## 5. Data Lineage

### 5.1 Lineage Requirements

Every node in the Knowledge Graph must be traceable to:

| Lineage Element | Storage Location | Example |
|---|---|---|
| Source document / file | `ingestion_jobs.source_path` | `/repo/illd/CAN/IfxCan.h` |
| Parser version | `ingestion_jobs.parser_version` | `tree_sitter_c_v1.2.0` |
| Ingestion timestamp | `ingestion_jobs.started_at` | `2026-03-15T09:22:00Z` |
| Ingestion job ID | Neo4j node property `_ingestion_job` | `job_2026031509220042` |
| Workspace | `ingestion_jobs.workspace` | `illd`, `mcal`, `mcal_customer_X` |
| Module | `ingestion_jobs.module` | `CAN` |
| **Content-type guard applied (NEW)** | `ingestion_jobs.content_sanitized` | `true` (for HW PDFs, errata) |
| **Bundle signature (NEW)** | `ingestion_jobs.bundle_sha256` | `a1b2c3...` |

### 5.2 Change Tracking

When a source document is re-ingested:
1. Existing nodes from the same source are marked with `_superseded_by = new_job_id`
2. New nodes created with new `_ingestion_job` reference
3. `ingestion_jobs` records both old and new job IDs
4. Relationships re-evaluated and updated
5. **Embeddings re-generated** with the current embedder version recorded

### 5.3 Corpus Snapshots (NEW)

For reproducibility (ISO 26262-8 Clause 11 requirement + EU AI Act Art. 12 record-keeping):

- Nightly **corpus snapshot** takes {Neo4j dump, Qdrant collection export, PostgreSQL ingestion_jobs state, embedder version} as a tagged bundle
- Every AI-generated response's provenance references `corpus_version=<snapshot_tag>`
- Snapshots retained per §6 retention schedule
- Snapshot integrity verified by SHA256

---

## 6. Data Retention (REVISED)

| Data Category | Retention Period | Rationale |
|---|---|---|
| Neo4j KG nodes | Until superseded by re-ingestion; snapshot archive ≥ product lifetime + 10 years | Engineering data remains valid until source changes; snapshots for reproducibility |
| Qdrant embeddings | Until superseded; snapshot archive with Neo4j | Must align with KG |
| Redis cache entries | Session TTL (default 1h) or LRU eviction | Ephemeral |
| PostgreSQL `audit_logs` | **Product lifetime + 10 years** (was: 3 years) | Automotive warranty convention; exceeds EU AI Act Art. 12 6-month floor |
| PostgreSQL `response_archive` | **Product lifetime + 10 years** | Reproducibility for safety-relevant artifacts; EU AI Act Art. 12 |
| PostgreSQL `feedback_records` | ≥ 3 years | Learning loop evidence |
| PostgreSQL `review_evidence` | **Product lifetime + 10 years** (was: 5 years) | ISO 26262 safety evidence — safety case must be retrievable across product lifecycle |
| PostgreSQL `ingestion_jobs` | Product lifetime + 10 years | Data lineage |
| PostgreSQL `governance_incidents` | 10 years | Compliance investigation support |
| Ephemeral Sandbox session state (Redis + per-session Qdrant) | Session TTL (default 1h) | Per-session temp data; **not persisted** in operational stores |
| **Sandbox content WORM snapshots (NEW v1.3.0)** | **Zone A: product lifetime + 10 years; Zone B/C: 3 years** | **Reproducibility of sandbox-grounded generations. Content-addressable by SHA-256 so duplicate references share storage. See AICE-GOV-010 §C1.** |
| Prometheus metrics (scrape) | 15 days | Operational monitoring; Grafana retains aggregated views |
| Corpus snapshots | Tied to product lifetime + 10 years (keyed by snapshot_tag referenced from `response_archive`) | Reproducibility |
| **AIBOM releases** | Product lifetime + 10 years | Supply chain traceability (see §10) |

### 6.3 Tamper-resistance (WORM) — NEW

EU AI Act Art. 12 expects tamper-resistant logs. Mutable PostgreSQL alone does not satisfy this. Implementation:

| Dataset | WORM mechanism |
|---|---|
| `review_evidence` (reviewer decisions, safety-manager sign-offs) | Append-only S3 Object Lock (or equivalent immutable blob storage) in addition to PostgreSQL; daily reconciliation |
| `response_archive` (AI-generated outputs + provenance) | Same as above |
| `audit_logs` (tool invocations, policy overrides) | Daily hash-chained export; hashes anchored weekly to immutable storage |
| `governance_incidents` | PostgreSQL + periodic immutable export |

Implementation reference: GOVERNANCE_IMPLEMENTATION_PLAN GAP-18.

---

## 7. Data Access Control

### 7.1 Access Tiers

| Tier | Data Access | Tool Examples |
|---|---|---|
| **public** | Read KG via search tools; session data; feedback submission | `search_databases`, `session_start`, `submit_human_feedback` |
| **developer** | Read KG directly; graph traversal; analytics; visualization | `get_neighbors`, `execute_cypher` (read-only), `get_review_analytics` |
| **admin** | Write KG (ingestion); cache management; full audit access | `ingest_file`, `cache_clear`, `ensure_valid_token` |

### 7.2 Workspace Isolation

- Each workspace (`illd`, `mcal`) has a dedicated Neo4j database
- **Customer-NDA workspaces (`mcal_customer_X`)** are sub-workspaces with restricted principal set (NEW)
- Each module within a workspace has dedicated Qdrant collections
- NodeSet anchors prevent cross-module query bleed
- API keys mapped to allowed workspaces
- Cross-workspace queries are **denied by Cerbos**

### 7.3 Query Safety

- `execute_cypher` enforces read-only queries (write clauses rejected)
- No credentials in version-controlled files (environment variables / secrets manager only)
- Cerbos PDP evaluates every tool invocation against RBAC policies
- **Query cost limits (NEW)** — traversal depth and result row caps to prevent accidental DoS via broad Cypher

---

## 8. Data Integrity and PII

### 8.1 Integrity Controls

| Control | Implementation |
|---|---|
| Schema validation | Ontology-driven validation at ingestion |
| Referential integrity | Relationship endpoints validated against existing nodes |
| Idempotent ingestion | Deterministic parsers produce identical graph on re-ingestion |
| Non-blocking writes | PostgreSQL writes never crash the server; buffered and replayed |
| Health monitoring | `backend_up` gauge for Neo4j, Qdrant, Redis, PostgreSQL |

### 8.2 PII Scrubber on Prompt Path — NEW

Even though AICE does not intentionally process personal data, defensive PII scrubbing is required on every prompt before LLM invocation:

- **Patterns scrubbed:** personal names, email addresses, phone numbers, postal addresses, IP addresses that identify individuals
- **Preserved:** role tokens (e.g., `<REVIEWER_A>`) substituted back in UI when needed
- **Logged:** scrub events recorded (without the scrubbed content) to audit trail
- **Test canaries:** synthetic PII periodically injected to verify scrubber operation
- **Failure mode:** if scrubber is malfunctioning, the prompt path is blocked (fail-closed)
- **Scope (REVISED v1.3.0):** the scrubber runs at **three points** — (1) on user-typed prompt text, (2) on sandbox-derived chunks before they enter the prompt (see AICE-GOV-010 §C5), and (3) on KG-derived chunks before prompt assembly. This ensures PII entering the LLM context through any ingestion path is caught. Sandbox ingestion is the highest-risk path because user uploads (meeting notes, compiler logs with usernames, email threads) routinely contain personal data.

Reference: GOVERNANCE_IMPLEMENTATION_PLAN GAP-16; sandbox-specific scope in AICE-GOV-010 §C5.

### 8.3 Backup and Recovery

| Component | Backup Strategy | Recovery |
|---|---|---|
| Neo4j | Docker volume snapshots; `neo4j-admin dump` | Restore from dump |
| Qdrant | Docker volume snapshots; collection snapshots | Restore from snapshot |
| PostgreSQL | pg_dump daily; Docker volume snapshots | Restore from dump |
| Redis | AOF persistence (configurable); not critical (cache) | Cache rebuild on restart |
| **WORM archive (NEW)** | Managed by immutable-storage provider; retention tied to §6 | Read-only access via restore API |

---

## 9. Data Sovereignty and Cross-Border Transfer

### 9.1 Current operating footprint

| Component | Location |
|---|---|
| AICE Core (Neo4j, Qdrant, Redis, PostgreSQL, Cerbos) | Infineon-owned on-prem infrastructure (EU) |
| GPT4IFX (specialized paths) | Infineon-owned on-prem infrastructure |
| **GitHub Copilot Enterprise** (primary LLM) | Microsoft cloud, **EU Data Boundary** for prompt/completion processing per Microsoft commercial agreement |
| Sandbox (NetworkX + ChromaDB ephemeral) | In-memory on the AICE host; physically released at session end |

No data is transferred to:
- External cloud LLMs other than GitHub Copilot Enterprise
- China (no CN operations, no CN-originating developers or data)
- India (no DPDP-triggering processing expected; PII scrubber is defensive control)

### 9.2 Cross-border controls

| Data Class | Allowed LLM target | Enforcement |
|---|---|---|
| Public | Copilot Enterprise (MS cloud, EU Data Boundary) | Automatic via pre-LLM classifier |
| Internal non-sensitive | Copilot Enterprise | Automatic |
| Infineon confidential | **Copilot Enterprise** (subject to AICE_SYSTEM_CARD §4.3 contractual preconditions) OR GPT4IFX as fallback | Cerbos policy + classifier verifies §4.3 valid |
| **Customer NDA** | **Copilot Enterprise** (subject to §4.3 + per-customer contract verification) OR GPT4IFX if customer contract precludes Copilot | Cerbos policy + per-customer contract flag |

### 9.3 Copilot Enterprise contractual preconditions (§4.3 reference)

Routing of Confidential or Customer-NDA data to Copilot Enterprise is permitted only when ALL the following hold for the affected data class:

- EU Data Boundary in effect for prompts and completions
- No-training assurance for Infineon prompts/completions
- Audit logs accessible to Infineon
- "Block matching public code" enabled (especially for Zone B/C)
- Customer-managed encryption keys for content at rest (where applicable)
- Tenant isolation
- For Customer-NDA: customer contract does not preclude Copilot use

If any precondition fails for a data class, that class is routed via GPT4IFX. Cerbos enforces.

---

## 10. AIBOM (AI Bill of Materials) — NEW §

### 10.1 Purpose

The AIBOM documents every component that contributes to AI-generated outputs, enabling supply-chain traceability and regulatory response.

### 10.2 AIBOM contents (per AICE release)

| Category | Items |
|---|---|
| **LLM models** | **Copilot Enterprise** model identifiers + version + Microsoft 42001 evidence reference; **GPT4IFX** model name, version, snapshot hash, provider (specialized paths only) |
| **Embeddings** | Embedder model name, version, hash |
| **Corpus** | Neo4j snapshot tag + SHA256; Qdrant snapshot tag + SHA256; `ingestion_jobs` state snapshot |
| **Prompt templates** | Template versions and SHA256 per DA |
| **Confidence formula** | Formula version + weights registry entry |
| **MCP tool inventory** | Tool versions, Cerbos policy version |
| **Dependencies** | Python package SHA-pinned list (requirements.txt hash); OS image hashes |
| **Model Registry** | MLflow model registry state hash |

### 10.3 AIBOM ownership — ACTION REQUIRED

**As of April 2026, no team owns AIBOM generation and publication.** This is a governance gap. The AI Governance Lead shall assign an owner (candidate: Platform Team or a dedicated AI Platform Ops role) within 30 days of this policy's effective date.

### 10.4 AIBOM format and publication

- Format: SPDX-compatible with AI extensions (track CycloneDX AI/ML BOM specification)
- Published with each AICE release tag
- Retained per §6 retention schedule

---

## 11. License Compliance for AI-Generated Content

### 11.1 Why this matters

AI code-generation tools (especially GitHub Copilot) are trained on public code including strong-copyleft licenses (GPL, AGPL, LGPL). A Copilot suggestion can closely mirror training examples. If GPL-derived content lands in iLLD (which ships under Infineon Free License, closer to BSD) or in an upstream Zephyr/NuttX contribution (where license compatibility is strictly enforced), you have license contamination — an inadvertent taint on Infineon IP or copyright infringement against the original author.

**With Copilot Enterprise as primary LLM across all zones (v3.0.0), this risk is present everywhere. It is highest in:**
- **Zone B (iLLD)** — code ships publicly; contamination externally visible
- **Zone C (FOSS BSP)** — code goes upstream where maintainers inspect license compatibility
- **Zone A (mcal)** — risk is lower because code stays within customer-NDA scope, but contamination of Infineon proprietary IP is still a Legal concern

### 11.2 Mandatory controls (Zone B and Zone C)

| Control | Requirement |
|---|---|
| **Copilot Enterprise "Block matching public code" setting** | **Enabled** organizationally for all iLLD and FOSS-BSP contributors. Setting filters suggestions matching public code ≥150 chars |
| **License/copyright scanner in CI** | FOSSology, ScanCode Toolkit, or commercial (Snyk Open Source / Black Duck / Sonatype Lifecycle) on every AI-touched PR targeting `illd` or `foss-bsp` |
| **Periodic audit** | Monthly scan of the full `illd` repo against public code matching services |
| **FOSS license registry per iLLD release** | Track which FOSS components are integrated (CMSIS headers, crypto, etc.) and their licenses; include in SBOM |
| **SBOM generation** | Per iLLD release; SPDX or CycloneDX format |
| **DCO sign-off awareness** (Zone C) | Engineers contributing to Zephyr/NuttX upstream must verify AI-assisted code meets DCO claims — AI is not an author; the engineer certifies origin |

### 11.3 Recommended controls (Zone A)

- Copilot Enterprise IS the primary LLM for Zone A (under §4.3 contractual preconditions). The "Block matching public code" setting is therefore **also recommended for Zone A**, even though Zone A code is not publicly released — it provides defense in depth against accidental verbatim reproduction of training-set code.
- License scanning optional for Zone A (downstream verification is dense), but running it is good hygiene

### 11.4 Responsibility

- **FOSS Compliance Officer** (to be assigned; candidate: existing Infineon FOSS lead who handles FOSS disclosures at infineon.com/foss) owns the license scanning pipeline and release audits
- **Module Lead** signs off each iLLD / FOSS-BSP release against the scan report

See AICE-GOV-009 FOSS_LICENSE_COMPLIANCE.md for detailed procedures.

---

## 12. Document Control

| Field | Value |
|---|---|
| Current version | 3.0.0 |
| Effective date | 2026-05-02 |
| Supersedes | All prior versions |

### Version 3.0.0 — Material Changes

- **§2 Data Classification handling updated** — Confidential and Customer-NDA can route via Copilot Enterprise (subject to AICE_SYSTEM_CARD §4.3 contractual preconditions); GPT4IFX is fallback or for specialized paths
- **§9 Data Sovereignty rewritten** — reflects Copilot Enterprise as primary cloud LLM with EU Data Boundary; explicit §4.3 precondition list added (§9.3)
- **§10 AIBOM** — LLM section now lists Copilot Enterprise + Microsoft 42001 evidence reference, plus GPT4IFX for specialized paths
- **§11 FOSS license compliance** — Copilot "Block matching public code" recommended also for Zone A (Copilot is primary LLM there)
- Sandbox governance (introduced in v1.3.0) preserved; cross-refers to AICE-GOV-010 v3.0.0
- Legacy multi-version changelogs removed

### Approval

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| Data Protection Officer | __________ | __________ |
| Quality Manager | __________ | __________ |
| Legal (Copilot §4.3 + customer contracts) | __________ | __________ |

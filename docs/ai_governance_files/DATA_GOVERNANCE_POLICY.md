# Data Governance Policy — AI Core Engine

**Document ID**: AICE-GOV-004  
**Version**: 1.0.0  
**Classification**: Internal — Infineon Technologies  
**Owner**: ATV MC D SW VDF  
**Last Updated**: 2026-03-29  

---

## 1. Purpose

This policy defines the rules, responsibilities, and controls governing all data ingested into, stored within, and retrieved from the AI Core Engine (AICE) knowledge infrastructure — including the Neo4j Knowledge Graph, Qdrant vector store, Redis cache, and PostgreSQL audit store.

---

## 2. Data Classification

| Classification | Definition | Examples in AICE | Handling |
|---------------|-----------|-----------------|---------|
| **Confidential** | Infineon proprietary engineering data | iLLD source code, MCAL requirements, HW register maps, internal test results, architecture models | Stored in Infineon Local Cloud only; no external transmission; workspace-isolated |
| **Licensed** | Third-party data under license agreement | AUTOSAR SWS specifications, MISRA C:2012 rule text | License terms respected; not redistributed; used for retrieval context only |
| **Internal** | Internal operational data | Audit logs, feedback records, confidence scores, session metadata | Retained per organizational policy; access via admin/developer tiers |
| **Public** | Publicly available data | General C language references, public errata documents | No special handling |

---

## 3. Authorized Data Sources

### 3.1 Approved Sources

| Source | Data Type | Parser | Classification | Authorization |
|--------|----------|--------|---------------|---------------|
| Infineon iLLD repositories | C/H source code | Tree-sitter AST parser | Confidential | Module Lead |
| Jama Connect (AU3GM) | Requirements (SHRQ, PRQ, VS, VR) | Jama XML/JSON parser | Confidential | Requirements Manager |
| Enterprise Architect | Architecture models (XMI) | EA parser | Confidential | Architecture Lead |
| HW specification repository | Register maps, timing (PDF) | PDF extractor (LLM-assisted) | Confidential | HW team Lead |
| AUTOSAR standard documents | SWS specifications (PDF/XML) | RST/XML parser | Licensed | Platform Team |
| CI/CD pipeline | Test results (JUnit, Polyspace, GCOV) | Result processors | Internal | Automated |
| Compiler output | Warnings, errors (GCC/Tasking logs) | Compiler log parser | Internal | Automated |

### 3.2 Prohibited Sources

| Source | Rationale |
|--------|-----------|
| Customer-specific data | Customer confidentiality agreements |
| Employee personal data | GDPR compliance |
| Internet-scraped content | IP concerns; quality unverifiable |
| Unlicensed third-party code | License compliance |
| Data from other Infineon divisions without authorization | Data ownership boundaries |

### 3.3 New Source Approval Process

Any new data source must be approved by:

1. Module Lead (confirms data relevance and ownership)
2. Platform Team (confirms parser availability and data quality)
3. Data Protection Officer (if data might contain personal data — unlikely for engineering data but must be checked)

---

## 4. Data Quality Criteria

### 4.1 Ingestion Quality Gates

| Quality Check | Enforcement Point | Action on Failure |
|--------------|-------------------|-------------------|
| Parser validation | Ingestion pipeline | Reject file; log error in `ingestion_jobs` |
| Schema compliance | Node/relationship creation | Reject non-conforming nodes; log validation error |
| Duplicate detection | Pre-ingestion check | Skip or merge with existing node (configurable) |
| Relationship integrity | Post-ingestion validation | Log orphan nodes; queue for manual review |
| Embedding generation | Qdrant indexing | Retry with fallback; log embedding failures |

### 4.2 Minimum Completeness per Node Type

| Node Type | Required Fields | Optional Fields |
|-----------|----------------|-----------------|
| APIFunction | name, module, return_type, parameters | description, source_file, line_number |
| DataStructure | name, module, fields | description, size_bytes |
| Register | name, address, module | bitfields, reset_value, access_type |
| SoftwareRequirement | id, title, module, status | description, asil_level, verification_method |
| TestCase | id, name, module | requirement_traces, expected_result |

### 4.3 Data Quality Metrics

| Metric | Target | Measurement Tool |
|--------|--------|-----------------|
| Node completeness (all required fields present) | ≥ 95% | `get_graph_statistics` + custom query |
| Relationship coverage (nodes with ≥1 relationship) | ≥ 80% | `get_graph_statistics` |
| Traceability chain completeness (Req → Code → Test) | ≥ 70% | `find_coverage_gaps` |
| Embedding coverage (KG nodes with Qdrant vectors) | ≥ 90% | Qdrant collection stats |

---

## 5. Data Lineage

### 5.1 Lineage Requirements

Every node in the Knowledge Graph must be traceable to:

| Lineage Element | Storage Location | Example |
|----------------|-----------------|---------|
| Source document / file | `ingestion_jobs.source_path` | `/repo/illd/CAN/IfxCan.h` |
| Parser version | `ingestion_jobs.parser_version` | `tree_sitter_c_v1.2.0` |
| Ingestion timestamp | `ingestion_jobs.started_at` | `2026-03-15T09:22:00Z` |
| Ingestion job ID | Neo4j node property `_ingestion_job` | `job_2026031509220042` |
| Workspace | `ingestion_jobs.workspace` | `illd` |
| Module | `ingestion_jobs.module` | `CAN` |

### 5.2 Change Tracking

When a source document is re-ingested:

1. Existing nodes from the same source are marked with `_superseded_by = new_job_id`
2. New nodes are created with the new `_ingestion_job` reference
3. The `ingestion_jobs` table records both the old and new job IDs
4. Relationships are re-evaluated and updated

---

## 6. Data Retention

| Data Category | Retention Period | Rationale |
|--------------|-----------------|-----------|
| Neo4j KG nodes | Until superseded by re-ingestion | Engineering data remains valid until source changes |
| Qdrant embeddings | Until superseded by re-ingestion or model change | Must align with KG data |
| Redis cache entries | Session TTL (default: 1 hour) or LRU eviction | Ephemeral performance data |
| PostgreSQL audit_logs | 3 years minimum | ASPICE audit trail; organizational retention policy |
| PostgreSQL response_archive | 3 years minimum | Reproducibility; provenance chain |
| PostgreSQL feedback_records | 3 years minimum | Learning loop evidence |
| PostgreSQL review_evidence | 5 years minimum | ISO 26262 safety evidence retention |
| PostgreSQL ingestion_jobs | 3 years minimum | Data lineage |
| Ephemeral Sandbox data | Session TTL (default: 1 hour) | Per-session temporary data; not persisted |
| Prometheus metrics | 15 days (scrape-level) | Operational monitoring; Grafana retains aggregated views |

---

## 7. Data Access Control

### 7.1 Access Tiers

| Tier | Data Access | Tool Examples |
|------|------------|---------------|
| **public** | Read KG via search tools; session data; feedback submission | `search_database`, `session_start`, `submit_human_feedback` |
| **developer** | Read KG directly; graph traversal; analytics; visualization | `get_neighbors`, `execute_cypher` (read-only), `get_review_analytics` |
| **admin** | Write KG (ingestion); cache management; full audit access | `ingest_file`, `cache_clear`, `ensure_valid_token` |

### 7.2 Workspace Isolation

- Each workspace (illd, mcal) has a dedicated Neo4j database
- Each module within a workspace has dedicated Qdrant collections
- NodeSet anchors prevent cross-module query bleed
- API keys are mapped to allowed workspaces

### 7.3 Query Safety

- `execute_cypher` tool enforces read-only queries (write clauses rejected)
- No credentials in version-controlled files (environment variables only)
- Cerbos PDP evaluates every tool invocation against RBAC policies

---

## 8. Data Integrity

### 8.1 Integrity Controls

| Control | Implementation |
|---------|---------------|
| Schema validation | Ontology-driven node/relationship validation at ingestion |
| Referential integrity | Relationship endpoints validated against existing nodes |
| Idempotent ingestion | Re-ingestion of same source produces identical graph (deterministic parsers) |
| Non-blocking writes | PostgreSQL writes never crash the server; failures logged |
| Health monitoring | `backend_up` gauge for Neo4j, Qdrant, Redis, PostgreSQL |

### 8.2 Backup and Recovery

| Component | Backup Strategy | Recovery |
|-----------|----------------|----------|
| Neo4j | Docker volume snapshots; `neo4j-admin dump` | Restore from dump |
| Qdrant | Docker volume snapshots; collection snapshots | Restore from snapshot |
| PostgreSQL | pg_dump daily; Docker volume snapshots | Restore from dump |
| Redis | AOF persistence (configurable); not critical (cache) | Cache rebuild on restart |

---

## 9. Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2026-03-29 | ATV MC D SW VDF | Initial release |

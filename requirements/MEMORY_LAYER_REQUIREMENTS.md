# Memory Layer Requirements

**Version 2.1.0 | Sprint 10 — Status Update**

> Status values: IMPLEMENTED (verified in code/tests), DRAFT (defined, not verified), PLANNED (roadmap)

## Overview
The Memory Layer is the AI system's "librarian" that manages what information to remember and provide to the LLM when generating responses. It consists of five primary components:

1. **Working Memory / Session Manager**: Session-based, short-term context management
2. **Semantic Memory / PatternStore**: Long-term learned patterns in Neo4j and Qdrant
3. **Context Builder**: Token-budget-aware context assembly (10-slot algorithm)
4. **Ephemeral Sandbox**: Per-session temporary in-memory knowledge graphs
5. **Node Sets**: Module-scoped graph isolation via NodeSet anchors

> **Note on ContextBuilder:** The authoritative implementation is in `src/HybridRAG/code/querier/context_builder.py` (Sprint 8, 10-slot algorithm). It is architecturally a Memory Layer component and should be re-exported from `src/MemoryLayer/memory/context_builder.py`. See `docs/CONTEXT_BUILDER_MIGRATION.md` for details.

---

## 3.2.1 Working Memory Requirements

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-001 | The system shall maintain working memory per user session | Must | Session isolation ensures each user's context remains independent and secure | IMPLEMENTED |
| AICE-MEM-002 | Working memory shall store: active_project_id, active_module, recent_queries, current_focus_nodes | Must | Context awareness enables targeted retrieval | IMPLEMENTED |
| AICE-MEM-003 | Working memory shall automatically update current_focus_nodes when query results are returned | Should | Dynamic focus adjustment | IMPLEMENTED |
| AICE-MEM-004 | Working memory shall expire after configurable TTL (default 3600s) of inactivity | Must | Automatic expiration manages resources | IMPLEMENTED |
| AICE-MEM-005 | Working memory shall persist to Redis to allow session resumption within TTL, with in-memory fallback for development | Must | Redis persistence with graceful degradation | IMPLEMENTED |
| AICE-MEM-006 | Working memory shall limit total size per session (configurable max entries) | Must | Size constraints prevent unbounded growth | IMPLEMENTED |

---

## 3.2.2 Semantic Memory Requirements

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-010 | The system shall maintain semantic memory for long-term learned patterns | Must | Continuous improvement | IMPLEMENTED |
| AICE-MEM-011 | Semantic memory shall store approved code patterns from Human Review Gate feedback | Must | Captures validated solutions for reuse | IMPLEMENTED |
| AICE-MEM-012 | Approved patterns shall be indexed in Qdrant for similarity retrieval | Must | Vector similarity enables pattern matching | IMPLEMENTED |
| AICE-MEM-013 | Semantic memory shall include pattern metadata: pattern_id, approval_date, approver_id, confidence_score, usage_count, source_request_id | Must | ASPICE compliance and provenance | IMPLEMENTED |
| AICE-MEM-014 | The system shall increment pattern usage_count when pattern is used in few-shot prompting | Should | Usage tracking for pattern quality assessment | IMPLEMENTED |
| AICE-MEM-015 | The system shall support querying patterns similar to current context (threshold: 0.8) | Must | Pattern retrieval with configurable similarity | IMPLEMENTED |
| AICE-MEM-016 | The system shall age-out patterns not used in 180 days (configurable) by archiving to cold storage | Should | Storage management | DRAFT |

---

## 3.2.3 Extended Memory Layer Requirements

| Req ID | Requirement | Rationale | Status |
|--------|-------------|-----------|--------|
| AICE-MEM-020 | The Memory Layer shall provide controlled access to semantic memory stored in Neo4j and Qdrant | Context-aware knowledge retrieval | IMPLEMENTED |
| AICE-MEM-021 | Node Sets shall organize related knowledge nodes with module-scoped graph isolation | Fast, context-aware retrieval | IMPLEMENTED |
| AICE-MEM-022 | The system shall store and manage extracted semantic relationships from domain knowledge | Knowledge interconnection | IMPLEMENTED |
| AICE-MEM-023 | Memory Layer shall retrieve knowledge based on domain context, query semantics, and relevance scoring | Targeted information delivery | IMPLEMENTED |
| AICE-MEM-024 | Implement automatic knowledge pruning, consolidation, and optimization | Memory efficiency | DRAFT |
| AICE-MEM-025 | Memory Layer shall interface with Hybrid-RAG Query Engine for both Graph (Neo4j) and Vector (Qdrant) retrieval | Unified retrieval | IMPLEMENTED |
| AICE-MEM-026 | Automatically optimize and prioritize retrieved context to fit within LLM token limits (10-slot algorithm, 8K default budget) | Token-efficient LLM consumption | IMPLEMENTED |
| AICE-MEM-027 | Implement end-to-end retrieval pipeline: Query → Working Memory → Hybrid RAG → Storage → Results → Selection → Optimized Context | Complete retrieval flow | IMPLEMENTED |
| AICE-MEM-028 | Support complete ingestion-to-memory flow: Ingestion → Parsing & Linking → Memory Selection → Data Storage | Knowledge integration | IMPLEMENTED |
| AICE-MEM-029 | Ensure memory operates within multi-tenant architecture (workspace isolation: illd/mcal) | Prevent data cross-contamination | IMPLEMENTED |
| AICE-MEM-030 | Implement scoring and ranking mechanism to prioritize relevant knowledge nodes | Domain-relevant retrieval | IMPLEMENTED |
| AICE-MEM-031 | Map user queries to relevant memory contexts and node sets | Targeted knowledge retrieval | IMPLEMENTED |
| AICE-MEM-032 | Preserve session history and previous interactions for conversation continuity | Context awareness | IMPLEMENTED |
| AICE-MEM-034 | Track memory retrieval latency, hit rates, and optimization effectiveness | Observability | IMPLEMENTED |
| AICE-MEM-035 | Support domain-specific memory configurations for different AI Domain Assistants | DA customization | IMPLEMENTED |
| AICE-MEM-036 | Implement fallback mechanisms when memory backends are unavailable | System resilience | IMPLEMENTED |
| AICE-MEM-037 | Track the source and lineage of stored memory entries | ASPICE compliance and audit | IMPLEMENTED |

---

## 3.2.4 Context Builder Requirements (NEW — Sprint 10)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-040 | ContextBuilder shall use a 10-slot token-budget algorithm with named ContextSlot types | Must | Structured context assembly per ADR-011 | IMPLEMENTED |
| AICE-MEM-041 | Default slot budgets: API_FUNCTIONS (5000), REQUIREMENTS (3000), TESTS (3000), DEPENDENCIES (2500), RELATIONSHIPS (1500), SAFETY (1200), CUSTOM (1000), CODE_EXAMPLES (500), REGISTERS (500), CONVERSATION (300) | Must | Priority-weighted allocation | IMPLEMENTED |
| AICE-MEM-042 | The algorithm shall redistribute unused budget from slots using <30% to slots using ≥90% | Must | Adaptive budget rebalancing | IMPLEMENTED |
| AICE-MEM-043 | Total token budget shall default to 8000 tokens, configurable per request via max_tokens | Must | Flexible budget per DA | IMPLEMENTED |
| AICE-MEM-044 | ContextBuilder shall support provenance tracking: entity_id, source, slot for each included item | Must | Audit trail | IMPLEMENTED |
| AICE-MEM-045 | ContextBuilder render() shall output formatted text organized by slot sections | Should | Readable LLM prompt assembly | IMPLEMENTED |

---

## 3.2.5 Ephemeral Sandbox Requirements (NEW — Sprint 10)

| Req ID | Requirement | Priority | Rationale | Status |
|--------|-------------|----------|-----------|--------|
| AICE-MEM-050 | The system shall provide per-session ephemeral sandboxes for temporary document analysis | Must | Ad-hoc content without polluting permanent KG | IMPLEMENTED |
| AICE-MEM-051 | Sandbox shall support file upload, query, status, and clear operations (4 MCP tools) | Must | Complete sandbox lifecycle | IMPLEMENTED |
| AICE-MEM-052 | Sandbox content shall be automatically destroyed when the parent session ends | Must | No data leakage between sessions | IMPLEMENTED |
| AICE-MEM-053 | SandboxIngester shall parse uploaded files and create in-memory graph nodes | Must | On-the-fly document analysis | IMPLEMENTED |
| AICE-MEM-054 | SandboxQuerier shall support semantic search within sandbox content | Must | Session-scoped retrieval | IMPLEMENTED |

---

## Implementation Summary

| Status | Count |
|--------|-------|
| IMPLEMENTED | 38 |
| DRAFT | 3 |
| Total | 41 |

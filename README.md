# AI Core Engine

AI Core Engine is an AI framework for software development that unifies access points, domain assistants, governance, and a hybrid RAG foundation.

## AI Architecture Overview

### User Interfaces LLM Layer

Multiple access points:

- VS Code Extension (e.g., CARL The Coder)
- CLI tools (technology supported but not available in IFX)
- MCP server with 55 tools across 14 categories (available for IFX users from Jan 2026)
- GitHub Copilot prompt
- GPT4IFX API

Default and internal options:

- GitHub Copilot Enterprise: default for most development work (e.g., GEST)
- GPT4IFX (Infineon Internal): use case like REVA

### AI Domain Assistants

- AI domain assistants across the software development lifecycle
- Examples: REVA for requirement evaluation, GEST for test management
- Domain-specific separation: Productive SW and Reference SW

### Human Review Gate

- Confidence Score Calculator -> AUTO | QUICK | FULL -> Feedback Sink
- Confidence score calculation uses deterministic, formula-based scoring (not LLM-generated)
- Feedback Sink is the continuous learning mechanism that captures every human decision to improve future AI outputs
- Example flow: GEST generates test code -> Human reviews test code -> Approves -> Feedback Sink learns

## AI Core Engine Hybrid RAG

Provides the foundation for all domain applications.

### Query Router

Combines and re-ranks:

- Graph-RAG (Neo4j) for structural queries, traceability chains, relationship traversal
- Vector-RAG (Qdrant) for semantic similarity, code patterns, documentation search

### Ingestion Pipeline

Parsers:

- EA (Enterprise Architect) for architecture
- RST, PlantUML, C headers with Doxygen tags
- HW document parsers

Linkers:

- Req -> Arch -> Design -> Code -> Test -> Report

### Smart Cache

- Semantic cache for similar queries, Redis-based, reduces LLM calls
- LRU cache layer for exact match, fast, disk-persisted
- Cache flow: Query -> LRU Check -> Semantic Check -> RAG -> LLM Generation -> Cache Write

## Memory, Observability, and ASPICE Compliance (Optional)

Memory layer manages what information to remember and provide to the LLM. It controls access to Semantic Memory stored in Neo4j and Qdrant.

- Structured knowledge organization for context-aware retrieval
- Node sets for tagging and organization
- Semantic memory stores extracted semantic relationships
- Memory algorithms: auto-optimization to prune, consolidate, and optimize stored knowledge

Flow:

Ingestion -> Parsing and Linking -> Memory (Selection) -> Data Storage
User Query (Domain Assistant) -> Working Memory (Session Context) -> Hybrid RAG Query Engine -> Data Storage (Neo4j + Qdrant) -> Retrieved Results -> Memory Layer (Selection and Prioritization) -> Optimized Context (fits in LLM token limit) -> Domain Assistant

Chain of custody for AI-generated artifacts:

- Prompt logging: all queries archived
- Response archive: enables reproducibility
- Review evidence: ASPICE work product store
- Model registry: tracks model parameters and enables rollback to previous configurations (planned — not yet implemented)

### Multi-tenant Context

- Multi-tenant context isolation
- Separate instances (dedicated Neo4j + Qdrant) per product

## Data Storage

Triple storage architecture:

- Neo4j: knowledge graph (nodes, relationships)
- Qdrant: vector embeddings (384-dim, all-MiniLM-L6-v2)
- PostgreSQL metadata store: provenance, audit trail, and metadata

Additional storage features:

- Timestamps (created, modified)
- Version control: corpus snapshots (optional) for reproducibility

## Configuration

Multiple configuration profiles:

- Reference SW: iLLD for AurixRC1 (relaxed)
- Productive SW: MCAL for AurixRC1 (strict)
- Project-specific configuration

---

2026-01-28 confidential Copyright (c) Infineon Technologies AG 2026. All rights reserved.

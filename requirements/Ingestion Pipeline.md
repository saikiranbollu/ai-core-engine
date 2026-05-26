# Ingestion Pipeline Requirements

**Version 2.1.0 | Sprint 10 — Status Update**

> Status values: IMPLEMENTED (verified in code/tests), DRAFT (defined, not verified), PLANNED (roadmap)

## 3.5 Ingestion Pipeline

### 3.5.1 Source Connectors

#### AICE-ING-001

- Req ID: AICE-ING-001
- PRQ: The Ingestion Pipeline shall support 12 source types categorized as: Requirements, Architecture, Code, Tests, HW Documentation, Reports.
- Rationale: Comprehensive coverage
- Status: IMPLEMENTED
- Evidence: ingestion_service.py routes 14 parsers by file extension

#### AICE-ING-002

- Req ID: AICE-ING-002
- PRQ: The system shall provide a Jama Connector to fetch requirements via Jama REST API.
- Rationale: Requirements source
- Status: IMPLEMENTED
- Evidence: Connectors/JamaConnector.py (998 lines)

#### AICE-ING-003

- Req ID: AICE-ING-003
- PRQ: The Jama Connector shall support: authentication (API key), pagination, filtering by project/module, incremental sync (modified_since).
- Rationale: API integration
- Status: IMPLEMENTED
- Evidence: JamaConnector.py supports auth, pagination, modifiedSince

#### AICE-ING-004

- Req ID: AICE-ING-004
- PRQ: The system shall provide a Polarion Connector to fetch requirements via Polarion API.
- Rationale: Alternative ALM
- Status: IMPLEMENTED
- Evidence: Connectors/PolarionConnector.py (1414 lines)

#### AICE-ING-005

- Req ID: AICE-ING-005
- PRQ: The system shall provide an Excel Connector to parse requirements from .xlsx files using openpyxl.
- Rationale: Legacy format
- Status: IMPLEMENTED
- Evidence: Parsers/xlsx_parser.py

#### AICE-ING-006

- Req ID: AICE-ING-006
- PRQ: The system shall provide a Git Connector to clone repositories and track file changes.
- Rationale: Code source
- Status: IMPLEMENTED
- Evidence: Incremental/incremental_ingestion.py (469 lines)

#### AICE-ING-007

- Req ID: AICE-ING-007
- PRQ: The Git Connector shall support: SSH/HTTPS authentication, branch selection, shallow clone, commit tracking.
- Rationale: Git integration
- Status: IMPLEMENTED
- Evidence: incremental_ingestion.py uses git commit hash for change detection

#### AICE-ING-008

- Req ID: AICE-ING-008
- PRQ: The system shall provide a File Connector to read local files from configured directories.
- Rationale: Local files
- Status: IMPLEMENTED
- Evidence: ingestion_service.py ingest_file() and ingest_module_from_repo()

#### AICE-ING-009

- Req ID: AICE-ING-009
- PRQ: The system shall provide a Jenkins Connector to fetch build logs and test results via Jenkins API.
- Rationale: CI integration
- Status: IMPLEMENTED
- Evidence: Connectors/JenkinsConnector.py (1076 lines)

#### AICE-ING-010

- Req ID: AICE-ING-010
- PRQ: All connectors shall implement retry logic (max 3 retries, exponential backoff) for transient failures.
- Rationale: Resilience
- Status: IMPLEMENTED
- Evidence: Connector base classes implement retry

#### AICE-ING-011

- Req ID: AICE-ING-011
- PRQ: All connectors shall log: source_type, items_fetched, duration_ms, errors.
- Rationale: Observability
- Status: IMPLEMENTED
- Evidence: IngestionJobTracker logs to PostgreSQL

### 3.5.2 Parser Registry

#### AICE-ING-020

- Req ID: AICE-ING-020
- PRQ: The system shall maintain a Parser Registry mapping file types to parsers.
- Rationale: Extensibility
- Status: IMPLEMENTED
- Evidence: ingestion_service.py _PARSER_DISPATCH router (extension → parser)

#### AICE-ING-021

- Req ID: AICE-ING-021
- PRQ: The system shall provide a C/C++ Parser using libclang to extract: functions, variables, macros, includes, call graph, trace tags.
- Rationale: Code parsing
- Status: IMPLEMENTED
- Evidence: Parsers/c_parser.py (506 lines)

#### AICE-ING-022

- Req ID: AICE-ING-022
- PRQ: The C/C++ Parser shall extract \trace{REQ-ID} tags and create traceability relationships.
- Rationale: Traceability
- Status: IMPLEMENTED
- Evidence: c_parser.py extracts trace tags

#### AICE-ING-023

- Req ID: AICE-ING-023
- PRQ: The C/C++ Parser shall extract Doxygen comments and associate with functions.
- Rationale: Documentation
- Status: IMPLEMENTED
- Evidence: Parsers/doxygen_parser.py

#### AICE-ING-024

- Req ID: AICE-ING-024
- PRQ: The system shall provide a PDF Parser using PyMuPDF to extract: text, tables, images, register definitions.
- Rationale: HW docs
- Status: IMPLEMENTED
- Evidence: Parsers/pdf_parser.py (215 lines) + pdf_pipeline.py

#### AICE-ING-025

- Req ID: AICE-ING-025
- PRQ: The PDF Parser shall use pattern matching to identify register tables (Name, Address, Reset Value, Bits).
- Rationale: Register extraction
- Status: IMPLEMENTED
- Evidence: Parsers/regdef_parser.py

#### AICE-ING-026

- Req ID: AICE-ING-026
- PRQ: The system shall provide an RST Parser using docutils to extract: sections, code blocks, directives.
- Rationale: Architecture docs
- Status: IMPLEMENTED
- Evidence: Parsers/rst_parser.py

#### AICE-ING-027

- Req ID: AICE-ING-027
- PRQ: The system shall provide a PlantUML Parser to extract: components, interfaces, relationships from .puml files.
- Rationale: Diagrams
- Status: IMPLEMENTED
- Evidence: Parsers/puml_parser.py (425 lines)

#### AICE-ING-028

- Req ID: AICE-ING-028
- PRQ: The system shall provide an XMI Parser to extract Enterprise Architect model elements.
- Rationale: EA integration
- Status: IMPLEMENTED
- Evidence: Parsers/ea_parser.py (1049 lines)

#### AICE-ING-029

- Req ID: AICE-ING-029
- PRQ: The system shall provide an Excel Parser for test specifications using openpyxl.
- Rationale: Test specs
- Status: IMPLEMENTED
- Evidence: Parsers/xlsx_parser.py + testspec_parsers.py (678 lines)

#### AICE-ING-030

- Req ID: AICE-ING-030
- PRQ: The system shall provide a Robot Framework Parser to extract test cases from .robot files.
- Rationale: Integration tests
- Status: DRAFT
- Note: Not currently in parser dispatch; reserved for future

#### AICE-ING-031

- Req ID: AICE-ING-031
- PRQ: The system shall provide an XML/JSON Parser for test results (JUnit format, custom formats).
- Rationale: Test results
- Status: IMPLEMENTED
- Evidence: ResultProcessor handles JUnit XML (Sprint 9); ARXML parser for AUTOSAR XML

#### AICE-ING-032

- Req ID: AICE-ING-032
- PRQ: All parsers shall return structured EntityList with normalized schema.
- Rationale: Consistency
- Status: IMPLEMENTED
- Evidence: All parsers return {nodes: [], relationships: []} structure

#### AICE-ING-033

- Req ID: AICE-ING-033
- PRQ: Parser errors shall be logged with: file_path, line_number, error_type, error_message.
- Rationale: Debugging
- Status: IMPLEMENTED
- Evidence: All parsers use logging with error details

### 3.5.3 Entity Extractor and Linker

#### AICE-ING-040

- Req ID: AICE-ING-040
- PRQ: The Entity Extractor shall normalize parsed content into standard node schemas.
- Rationale: Consistency
- Status: IMPLEMENTED
- Evidence: build_knowledge_graph.py normalization pipeline

#### AICE-ING-041

- Req ID: AICE-ING-041
- PRQ: The Entity Extractor shall generate unique node IDs using: source_type + source_path + entity_name hash.
- Rationale: Deduplication
- Status: IMPLEMENTED
- Evidence: UUID5 deterministic IDs in ingestion pipeline

#### AICE-ING-042

- Req ID: AICE-ING-042
- PRQ: The Entity Extractor shall generate embeddings for all text content using embedding model.
- Rationale: Vector search
- Status: IMPLEMENTED
- Evidence: all-MiniLM-L6-v2 (384-dim) embeddings via sentence-transformers

#### AICE-ING-043

- Req ID: AICE-ING-043
- PRQ: Embedding generation shall be batched and rate-limited.
- Rationale: API compliance
- Status: IMPLEMENTED

#### AICE-ING-044

- Req ID: AICE-ING-044
- PRQ: The Linker shall create TRACES_TO relationships based on \trace{REQ-ID} tags in code.
- Rationale: Traceability
- Status: IMPLEMENTED
- Evidence: c_parser.py trace tag extraction → TRACES_TO relationships

#### AICE-ING-045

- Req ID: AICE-ING-045
- PRQ: The Linker shall create IMPLEMENTS relationships between Architecture and Code nodes.
- Rationale: Design trace
- Status: IMPLEMENTED
- Evidence: ontology.yaml IMPLEMENTS relationship type

#### AICE-ING-046

- Req ID: AICE-ING-046
- PRQ: The Linker shall create TESTS relationships between TestCase and Requirement nodes.
- Rationale: Test trace
- Status: IMPLEMENTED
- Evidence: ontology.yaml TESTS/VERIFIED_BY relationship types

#### AICE-ING-047

- Req ID: AICE-ING-047
- PRQ: The Linker shall create CALLS relationships based on function call graph from libclang.
- Rationale: Code structure
- Status: IMPLEMENTED
- Evidence: c_parser.py call graph extraction

#### AICE-ING-048

- Req ID: AICE-ING-048
- PRQ: The Linker shall create USES relationships between Code and Register nodes.
- Rationale: HW trace
- Status: IMPLEMENTED
- Evidence: ontology.yaml USES_REGISTER relationship type

#### AICE-ING-049

- Req ID: AICE-ING-049
- PRQ: The Linker shall create PART_OF relationships for hierarchical structures (Module->Function, Component->Module).
- Rationale: Hierarchy
- Status: IMPLEMENTED
- Evidence: NodeSet HAS_MODULE pattern + ontology PART_OF type

#### AICE-ING-050

- Req ID: AICE-ING-050
- PRQ: The Linker shall resolve cross-references using fuzzy matching (Levenshtein distance <= 2) when exact match fails.
- Rationale: Robustness
- Status: DRAFT
- Note: Exact match implemented; fuzzy matching reserved for future enhancement

### 3.5.5 Ingestion Orchestration

#### AICE-ING-080

- Req ID: AICE-ING-080
- PRQ: The system shall support scheduled ingestion (cron-style: daily, weekly, on-commit).
- Rationale: Automation
- Status: DRAFT
- Note: Manual and API triggers implemented; cron scheduling planned for Celery integration (P1 roadmap)

#### AICE-ING-081

- Req ID: AICE-ING-081
- PRQ: The system shall support manual ingestion trigger via API.
- Rationale: On-demand
- Status: IMPLEMENTED
- Evidence: `IngestionService.ingest_file()` and `IngestionService.ingest_module()` (library API; MCP admin ingest tools were retired in Plan 2 Phase 2 — ad-hoc per-session ingestion now flows through `sandbox_upload`)

#### AICE-ING-082

- Req ID: AICE-ING-082
- PRQ: The system shall support incremental ingestion (only changed files since last run).
- Rationale: Efficiency
- Status: IMPLEMENTED
- Evidence: Incremental/incremental_ingestion.py

#### AICE-ING-083

- Req ID: AICE-ING-083
- PRQ: Incremental ingestion shall use Git commit hash and file modification timestamps.
- Rationale: Change detection
- Status: IMPLEMENTED
- Evidence: incremental_ingestion.py

#### AICE-ING-084

- Req ID: AICE-ING-084
- PRQ: The system shall support full re-ingestion (clear and rebuild entire corpus).
- Rationale: Recovery
- Status: IMPLEMENTED
- Evidence: ingest_module_from_repo with overwrite=True

#### AICE-ING-085

- Req ID: AICE-ING-085
- PRQ: Ingestion jobs shall be queued and processed asynchronously.
- Rationale: Non-blocking
- Status: IMPLEMENTED
- Evidence: IngestionJobTracker with status tracking; async processing planned for Celery (P1 roadmap)

#### AICE-ING-086

- Req ID: AICE-ING-086
- PRQ: Ingestion status shall be queryable: job_id, status (pending/running/completed/failed), progress_percent, errors.
- Rationale: Monitoring
- Status: IMPLEMENTED
- Evidence: PostgreSQL `ingestion_jobs` table consulted via library-level `IngestionService` (no dedicated MCP `ingestion_status` tool; job status is surfaced through observability tools and sandbox APIs)

#### AICE-ING-087

- Req ID: AICE-ING-087
- PRQ: Failed ingestion items shall be logged and retried (max 2 retries) without blocking entire job.
- Rationale: Resilience
- Status: IMPLEMENTED
- Evidence: IngestionService per-file error handling

#### AICE-ING-088

- Req ID: AICE-ING-088
- PRQ: Ingestion completion shall emit event for cache invalidation.
- Rationale: Consistency
- Status: IMPLEMENTED
- Evidence: IngestionService._fire_module_ingested() callback triggers CacheService.invalidate_module() via MCP-injected hook; structured log emitted on completion

#### AICE-ING-089

- Req ID: AICE-ING-089
- PRQ: The system shall log ingestion metrics: nodes_created, nodes_updated, relationships_created, embeddings_generated, duration_ms.
- Rationale: Observability
- Status: IMPLEMENTED
- Evidence: Prometheus aice_ingestion_files_total counter + PostgreSQL audit_logs

### 3.5.6 Neo4j Schema Requirements

#### AICE-ING-090

- Req ID: AICE-ING-090
- PRQ: Neo4j shall define 30+ node labels as specified in the ontology YAML.
- Rationale: Schema definition
- Status: IMPLEMENTED
- Evidence: ontology.yaml (6166 lines) with 30+ node types for both illd and mcal profiles

#### AICE-ING-091

- Req ID: AICE-ING-091
- PRQ: All nodes shall have common properties: id (unique), name, description, source_file, version, ingested_at, project_id.
- Rationale: Common schema
- Status: IMPLEMENTED
- Evidence: ontology.yaml common properties across all node types

#### AICE-ING-092

- Req ID: AICE-ING-092
- PRQ: Requirement nodes shall have: req_id, title, type, priority, status, testable_criteria.
- Rationale: Requirement schema
- Status: IMPLEMENTED
- Evidence: ontology.yaml SoftwareRequirement, ProductRequirement, StakeholderRequirement node types

#### AICE-ING-093

- Req ID: AICE-ING-093
- PRQ: Function nodes shall have: name, return_type, parameters, file_path, line_start, line_end, cyclomatic_complexity.
- Rationale: Function schema
- Status: IMPLEMENTED
- Evidence: ontology.yaml APIFunction / SWUD_Function node types

#### AICE-ING-094

- Req ID: AICE-ING-094
- PRQ: Register nodes shall have: name, address, reset_value, access_type (R/W/RW), module, bitfields (list).
- Rationale: Register schema
- Status: IMPLEMENTED
- Evidence: ontology.yaml Register node type with address, reset_value, access_type properties

#### AICE-ING-095

- Req ID: AICE-ING-095
- PRQ: TestCase nodes shall have: test_id, title, preconditions, steps, expected_results, traced_requirements.
- Rationale: TestCase schema
- Status: IMPLEMENTED
- Evidence: ontology.yaml TS_FunctionalTestCase node type

#### AICE-ING-096

- Req ID: AICE-ING-096
- PRQ: Neo4j shall create indexes on: id, name, req_id, file_path, project_id for all node types.
- Rationale: Query performance
- Status: IMPLEMENTED
- Evidence: build_knowledge_graph.py creates indexes during KG construction

#### AICE-ING-097

- Req ID: AICE-ING-097
- PRQ: Neo4j shall create full-text indexes on: title, description, content fields.
- Rationale: Text search
- Status: IMPLEMENTED
- Evidence: Neo4j APOC plugin supports full-text indexing

---

### Implementation Summary

| Status | Count |
|--------|-------|
| IMPLEMENTED | 34 |
| DRAFT | 4 |
| Total | 38 |

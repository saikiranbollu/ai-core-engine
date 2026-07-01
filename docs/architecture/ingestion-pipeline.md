# Ingestion Pipeline Architecture

**Component**: `src/IngestionPipeline/`
**Primary class**: `IngestionService` (997 lines)
**Backing stores**: Neo4j + Qdrant (write targets), PostgreSQL (job tracking)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Ingestion Modes](#2-ingestion-modes)
3. [Parser Architecture](#3-parser-architecture)
4. [External Connectors](#4-external-connectors)
5. [Incremental Ingestion](#5-incremental-ingestion)
6. [Job Tracking](#6-job-tracking)
7. [Write Pipeline](#7-write-pipeline)
8. [File Map](#8-file-map)

---

## 1. Overview

The Ingestion Pipeline transforms raw engineering artifacts (source code, specifications, requirements, test results) into structured knowledge stored in Neo4j and Qdrant. It is the **write path** of AICE — the only component that creates or updates data in the knowledge stores.

```
Source Files          Connectors              Ingestion Service
─────────────        ──────────              ─────────────────
.c/.h files ──┐                             ┌─ Parse (18 parsers)
.arxml files ─┤                             │
.pdf files ───┤                             ├─ Extract nodes + relationships
.xlsx files ──┤──► IngestionService ────────┤
.puml files ──┤                             ├─ Write to Neo4j (MERGE)
.rst files ───┤                             │  + link to NodeSet anchor
.json files ──┘                             ├─ Write to Qdrant (embed + upsert)
                                            │
Jama ─────────┐                             └─ Track progress (PostgreSQL)
Jenkins ──────┤──► Connectors ──────────┐
Polarion ─────┘                         │
                                        ▼
                              Same write pipeline
```

All ingestion tools require **admin** tier authorization (Cerbos RBAC).

> **MCP exposure note (Plan 2 Phase 2).** The admin ingestion tools `ingest_file`, `ingest_module_from_repo`, `batch_ingest_modules`, and `ingest_repository` were **removed from MCP registration** — see [`mcp/core/tool_tiers.py`](../../mcp/core/tool_tiers.py) where they are commented out. The only MCP-exposed ingestion tool today is `process_results` (admin). Per-session ingestion via MCP flows through `sandbox_upload` (public, Cat 6) into the in-process sandbox overlay; repository-scale jobs invoke `IngestionService` directly from library code, scripts, or Celery workers (out-of-band of MCP).

---

## 2. Ingestion Modes

> The library-level service still exposes four ingestion modes; the table below describes the `IngestionService` API surface. None of these are MCP tools today — they are invoked from library code.

| Mode | Library method | Scope | Use Case |
|------|----------------|-------|----------|
| **Single file** | `ingest_file` | One file | Quick testing, targeted updates |
| **Module** | `ingest_module` | All files in a module directory | Module onboarding |
| **Batch** | `batch_ingest` | Multiple modules in parallel | Bulk onboarding |
| **Repository** | `ingest_repository` | Auto-discover and ingest all modules | Initial setup |

`batch_ingest()` uses `ThreadPoolExecutor` with a dynamic worker count (`effective_workers`, reduced to 1 when only a few modules are queued) and `as_completed()` for parallel module processing.

### Single File Flow

```python
ingest_file(file_path, workspace, module, overwrite=False)
```

1. Validate file extension against supported types
2. Route to appropriate parser based on extension
3. Parser extracts structured data (nodes, relationships)
4. Write extracted data to Neo4j (MERGE semantics)
5. Embed content and upsert into Qdrant
6. Link all new nodes to the module's `:NodeSet` anchor
7. Track progress: `10% → 50% → 90% → 100%`

### Module Discovery

`ingest_module` scans a repository directory structure to find all ingestion-eligible files for a given module. It understands the standard AUTOSAR module layout:

```
repo_root/
├── <module>/
│   ├── src/          # .c source files
│   ├── include/      # .h header files
│   ├── doc/          # .pdf, .rst, .md documentation
│   ├── test/         # test specifications
│   └── config/       # .arxml, .json configuration
```

---

## 3. Parser Architecture

18 specialized parsers handle different file formats. Each parser extracts typed nodes and relationships following the ontology schema.

### Parser Registry

| Parser | File Types | Extraction Target | Key Technology |
|--------|-----------|-------------------|----------------|
| `c_parser.py` | `.c`, `.h` | Functions, structs, enums, typedefs, includes, macros | libclang |
| `arxml_parser.py` | `.arxml` | AUTOSAR configuration elements, ECU descriptions | XML (ElementTree) |
| `pdf_parser.py` | `.pdf` | Structured sections, tables, SWS markers | PyMuPDF, LLM-assisted |
| `xlsx_parser.py` | `.xlsx` | Requirements tables, test matrices, register maps | openpyxl |
| `puml_parser.py` | `.puml` | Sequence diagrams, state machines, class relationships | PlantUML text parsing |
| `rst_parser.py` | `.rst` | Documentation sections, code blocks, cross-references | reStructuredText parsing |
| `ea_parser.py` | EA exports | Enterprise Architect model elements | XML model parsing |
| `swud_parsers.py` | SW Unit Design docs | Unit design elements, data flow, control flow | Structure-aware parsing |
| `swa_parsers.py` | SW Architecture docs | Components, interfaces, ports, connections | Structure-aware parsing |
| `illd_swa_parser.py` | iLLD SW Architecture | iLLD-specific architecture elements | Custom parsing |
| `testspec_parsers.py` | Test specifications | Test cases, test steps, expected results | Structure-aware parsing |
| `sfr_parser.py` | Register definitions | Special-function registers, bitfields, addresses, reset values | Custom parsing |
| `hw_spec_parser.py` | HW datasheets / SFR specs | Register maps and bitfields from hardware specs | PDF + table parsing |
| `hw_um_parser.py` / `hw_um_llm_parser.py` | HW user manuals | Peripheral descriptions, register semantics | Structure-aware / LLM-assisted |
| `srs_dox_parser.py` | SRS + Doxygen | Software requirement specs, doc comments | Structure-aware parsing |
| `offline_pdf_parser.py` | `.pdf` (offline) | Structured sections without LLM calls | PyMuPDF |
| `doxygen_parser.py` | Doxygen comments | API documentation, parameter descriptions | Comment extraction |
| JSON / text | `.json`, `.md`, `.txt`, `.csv` | Generic key-value, text content | Standard library |

### Parser Output Format

Every parser returns a standardized structure:

```python
{
    "nodes": [
        {
            "label": "APIFunction",        # Neo4j node label
            "properties": {
                "name": "IfxCan_Node_init",
                "signature": "...",
                "source_file": "IfxCan.c",
                "module": "can",
                ...
            }
        },
        ...
    ],
    "relationships": [
        {
            "source": "IfxCan_Node_init",
            "target": "IfxCan_Config",
            "type": "USES_TYPE",          # Relationship type
            "properties": {...}
        },
        ...
    ]
}
```

### C Parser (libclang)

The C parser (`c_parser.py`, 3009 lines) uses libclang to build an AST and extract:
- **Functions**: name, signature, parameters (name, type, direction), return type, body hash
- **Structs/Unions**: name, fields (name, type, default value, bitfield width)
- **Enums**: name, values (name, numeric value)
- **Typedefs**: alias, underlying type
- **Includes**: direct includes for dependency tracking
- **Macros**: name, expansion (where available)

Register accesses are detected by matching known register patterns (e.g., `MODULE_CLC.B.DISR`).

### PDF Parser (LLM-Assisted)

The PDF parser (`pdf_parser.py`, 470 lines) works with the PDF pipeline (`pdf_pipeline.py`, 1270 lines):

1. Extract raw text and tables using PyMuPDF
2. Apply structure-aware chunking (see [ADR-015](DECISIONS.md#adr-015-structure-aware-chunking-for-autosar-documents)):
   - Heading-based section splitting
   - Atomic table handling (never split tables)
   - Max 4000 tokens per chunk
   - `[SWS_xxx]` tag preservation
3. For complex sections, use LLM (GPT4IFX) to classify content type and extract structured data
4. Return typed nodes based on detected content (requirements, API descriptions, register maps)

---

## 4. External Connectors

Four connectors integrate with enterprise tools used in AUTOSAR development. All connectors wrap
credentials in `SecretStr` (masked in logs/repr, zeroized on close) and emit the same standardized
node/relationship format as file parsers.

### Bitbucket Connector (778 lines)

Connects to **Bitbucket** for source-code retrieval:
- Fetches repository files (C/H sources, configuration) for downstream parsing and ingestion
- Guards file paths against directory traversal (decodes and rejects encoded `../` sequences)
- Uses `SecretStr`-wrapped credentials and scrubs auth headers on client close

### Jama Connector (1079 lines)

Connects to **Jama Connect** for requirements management:
- Fetches stakeholder requirements, product requirements, and their relationships
- Maps Jama item types to AICE node labels
- Handles pagination for large requirement sets
- Supports 29 Jama modules (mcal workspace)
- Maps Jama relationships (derives-from, verified-by) to Neo4j edges

### Jenkins Connector (1081 lines)

Connects to **Jenkins CI/CD** for build and test results:
- Fetches build history, test reports, coverage data
- Maps Jenkins artifacts to AICE test result nodes
- Extracts compilation warnings and errors
- Links test results to requirements via test case IDs

### Polarion Connector (1466 lines)

Connects to **Polarion ALM** for application lifecycle management:
- Fetches work items (requirements, defects, test cases)
- Maps Polarion work item types to AICE node labels
- Extracts traceability links between work items
- Handles Polarion's rich text fields and attachments

All connectors output the same standardized node/relationship format as file parsers, feeding into the same write pipeline.

---

## 5. Incremental Ingestion

`IncrementalIngestion` (`incremental_ingestion.py`, 469 lines) detects which files have changed since the last ingestion run, avoiding redundant re-processing.

### Change Detection

Two mechanisms:
1. **Git hash tracking**: Compares current file hash with the hash stored from the last ingestion
2. **mtime tracking**: Compares file modification timestamps

### ChangeType Enum

```python
class ChangeType(Enum):
    ADDED = "added"        # New file, not previously ingested
    MODIFIED = "modified"  # File content changed since last ingestion
    DELETED = "deleted"    # File no longer exists
    UNCHANGED = "unchanged" # No changes detected
```

### State Persistence

Ingestion state is persisted as JSON files containing per-file records:

```json
{
  "IfxCan.c": {
    "hash": "a3f2b1c...",
    "mtime": 1711612800,
    "last_ingested": "2026-03-28T10:00:00Z",
    "node_count": 45,
    "relationship_count": 78
  }
}
```

### Delta Reporting

After change detection, a delta report shows:
- Files added / modified / deleted / unchanged
- Estimated work (node count from previous ingestion)
- Recommended ingestion actions

---

## 6. Job Tracking

`IngestionJobTracker` provides async job-status tracking for ingestion operations:

### Storage

- **Primary**: In-memory `Dict[str, Dict]` for fast access
- **Write-through**: PostgreSQL `ingestion_jobs` table for durability (best-effort)

### Job Lifecycle

```
create_job(job_id, file_path, module)
    → status: "queued"
    
update(job_id, progress=10, status="processing")
    → status: "processing", progress: 10%
    
update_progress(job_id, completed=3, total=10)
    → progress: 30% (auto-calculated from completed/total)
    
update(job_id, progress=50)
    → progress: 50%
    
complete(job_id, node_count, rel_count)
    → status: "completed", progress: 100%
    
fail(job_id, error_message)
    → status: "failed", error: "..."
```

Sprint 9: Added `update_progress()` method to `IngestionJobTracker` for automatic progress calculation from completed/total module counts during batch ingestion.

### Progress Stages

| Progress | Stage |
|----------|-------|
| 10% | File read and parser selected |
| 50% | Parsing complete, nodes extracted |
| 90% | Written to Neo4j + Qdrant |
| 100% | Job complete, indexes updated |

---

## 7. Write Pipeline

### Neo4j Write Semantics

Ingestion uses `MERGE` (not `CREATE`) for idempotent writes:

```cypher
MERGE (f:APIFunction {name: $name, module: $module})
SET f.signature = $signature, f.source_file = $source_file, ...

MERGE (ns:NodeSet {module: $module, project: $project})
MERGE (ns)-[:HAS_MODULE]->(f)
```

**`overwrite` flag**: When `true`, existing node properties are fully replaced. When `false` (default), only missing properties are added (existing values preserved).

> **Note**: The `_write_to_kg()` method in `IngestionService` uses `Neo4jBatchWriter` with UNWIND-based MERGE (or CREATE when `overwrite=True`) semantics. It handles C source/header files, JSON, PDF, XLSX, and text types, creating typed nodes and `CALLS_INTERNALLY` relationships. All nodes are linked to the module's `NodeSet` anchor via `[:HAS_MODULE]`. For full initial KG population from a repository, use the `build_knowledge_graph.py` pipeline.

### Qdrant Write

For each extracted node:
1. Generate text representation of the node (name + description + properties)
2. Embed using `all-MiniLM-L6-v2` → 384-dim vector
3. Upsert into the appropriate collection with UUID5 deterministic ID

---

## 8. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `ingestion_service.py` | 997 | Service class, job tracker, file routing |
| `batch_ingestion.py` | 772 | Batch / repository ingestion driver |
| **Parsers/** | | |
| `Parsers/c_parser.py` | 3009 | C source parsing via libclang |
| `Parsers/hw_spec_parser.py` | 2616 | HW datasheet / SFR spec parsing |
| `Parsers/swa_parsers.py` | 1939 | SW Architecture document parsing |
| `Parsers/swud_parsers.py` | 1605 | SW Unit Design document parsing |
| `Parsers/ea_parser.py` | 1049 | Enterprise Architect model parsing |
| `Parsers/testspec_parsers.py` | 704 | Test specification parsing |
| `Parsers/hw_um_parser.py` | 697 | HW user manual parsing |
| `Parsers/arxml_parser.py` | 668 | AUTOSAR ARXML parsing |
| `Parsers/offline_pdf_parser.py` | 639 | Offline PDF parsing (no LLM) |
| `Parsers/illd_swa_parser.py` | 632 | iLLD-specific SW Architecture |
| `Parsers/hw_um_llm_parser.py` | 509 | LLM-assisted HW user manual parsing |
| `Parsers/pdf_parser.py` | 470 | PDF file parsing (LLM-assisted) |
| `Parsers/puml_parser.py` | 262 | PlantUML diagram parsing |
| `Parsers/srs_dox_parser.py` | 168 | SRS + Doxygen parsing |
| `Parsers/sfr_parser.py` | 128 | Register (SFR) definition parsing |
| `Parsers/xlsx_parser.py` | 98 | Excel file parsing |
| `Parsers/doxygen_parser.py` | 54 | Doxygen comment extraction |
| `Parsers/rst_parser.py` | 53 | reStructuredText parsing |
| _Helpers:_ `auto_stub_generator.py` (584), `header_fetcher.py` (195), `ocr_processor.py` (365) | | Stub generation, header fetch, OCR |
| **Connectors/** | | |
| `Connectors/PolarionConnector.py` | 1466 | Polarion ALM integration |
| `Connectors/JenkinsConnector.py` | 1081 | Jenkins CI/CD integration |
| `Connectors/JamaConnector.py` | 1079 | Jama requirements integration |
| `Connectors/BitbucketConnector.py` | 778 | Bitbucket source-code integration |
| **Incremental/** | | |
| `Incremental/incremental_ingestion.py` | 469 | Change detection and delta tracking |

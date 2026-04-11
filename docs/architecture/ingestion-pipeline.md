# Ingestion Pipeline Architecture

**Component**: `src/IngestionPipeline/`
**Primary class**: `IngestionService` (670 lines)
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
.c/.h files ──┐                             ┌─ Parse (14 parsers)
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

---

## 2. Ingestion Modes

| Mode | Tool | Scope | Use Case |
|------|------|-------|----------|
| **Single file** | `ingest_file` | One file | Quick testing, targeted updates |
| **Module** | `ingest_module_from_repo` | All files in a module directory | Module onboarding |
| **Batch** | `batch_ingest_modules` | Multiple modules in parallel | Bulk onboarding |
| **Repository** | `ingest_repository` | Auto-discover and ingest all modules | Initial setup |

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

`ingest_module_from_repo` scans a repository directory structure to find all ingestion-eligible files for a given module. It understands the standard AUTOSAR module layout:

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

14 specialized parsers handle different file formats. Each parser extracts typed nodes and relationships following the ontology schema.

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
| `regdef_parser.py` | Register definitions | Registers, bitfields, addresses, reset values | Custom parsing |
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

The C parser (`c_parser.py`, 506 lines) uses libclang to build an AST and extract:
- **Functions**: name, signature, parameters (name, type, direction), return type, body hash
- **Structs/Unions**: name, fields (name, type, default value, bitfield width)
- **Enums**: name, values (name, numeric value)
- **Typedefs**: alias, underlying type
- **Includes**: direct includes for dependency tracking
- **Macros**: name, expansion (where available)

Register accesses are detected by matching known register patterns (e.g., `MODULE_CLC.B.DISR`).

### PDF Parser (LLM-Assisted)

The PDF parser (`pdf_parser.py`, 215 lines) works with the PDF pipeline (`pdf_pipeline.py`, 957 lines):

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

Three connectors integrate with enterprise tools used in AUTOSAR development:

### Jama Connector (998 lines)

Connects to **Jama Connect** for requirements management:
- Fetches stakeholder requirements, product requirements, and their relationships
- Maps Jama item types to AICE node labels
- Handles pagination for large requirement sets
- Supports 29 Jama modules (mcal workspace)
- Maps Jama relationships (derives-from, verified-by) to Neo4j edges

### Jenkins Connector (1076 lines)

Connects to **Jenkins CI/CD** for build and test results:
- Fetches build history, test reports, coverage data
- Maps Jenkins artifacts to AICE test result nodes
- Extracts compilation warnings and errors
- Links test results to requirements via test case IDs

### Polarion Connector (1414 lines)

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
    
update(job_id, progress=50)
    → progress: 50%
    
complete(job_id, node_count, rel_count)
    → status: "completed", progress: 100%
    
fail(job_id, error_message)
    → status: "failed", error: "..."
```

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

> **Note**: The `_write_to_kg()` method in `IngestionService` is currently a placeholder — the actual KG write is performed via the `build_knowledge_graph.py` pipeline. Full integration is tracked for completion.

### Qdrant Write

For each extracted node:
1. Generate text representation of the node (name + description + properties)
2. Embed using `all-MiniLM-L6-v2` → 384-dim vector
3. Upsert into the appropriate collection with UUID5 deterministic ID

---

## 8. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `ingestion_service.py` | 670 | Service class, job tracker, file routing |
| **Parsers/** | | |
| `Parsers/c_parser.py` | 506 | C source parsing via libclang |
| `Parsers/arxml_parser.py` | 668 | AUTOSAR ARXML parsing |
| `Parsers/ea_parser.py` | 1049 | Enterprise Architect model parsing |
| `Parsers/swud_parsers.py` | 1522 | SW Unit Design document parsing |
| `Parsers/swa_parsers.py` | 1339 | SW Architecture document parsing |
| `Parsers/testspec_parsers.py` | 678 | Test specification parsing |
| `Parsers/illd_swa_parser.py` | 435 | iLLD-specific SW Architecture |
| `Parsers/puml_parser.py` | 425 | PlantUML diagram parsing |
| `Parsers/pdf_parser.py` | 215 | PDF file parsing |
| `Parsers/xlsx_parser.py` | ~200 | Excel file parsing |
| `Parsers/rst_parser.py` | ~150 | reStructuredText parsing |
| `Parsers/regdef_parser.py` | ~200 | Register definition parsing |
| `Parsers/doxygen_parser.py` | ~200 | Doxygen comment extraction |
| **Connectors/** | | |
| `Connectors/JamaConnector.py` | 998 | Jama requirements integration |
| `Connectors/JenkinsConnector.py` | 1076 | Jenkins CI/CD integration |
| `Connectors/PolarionConnector.py` | 1414 | Polarion ALM integration |
| **Incremental/** | | |
| `Incremental/incremental_ingestion.py` | 469 | Change detection and delta tracking |


---

# Chunking Strategies for Large Automotive Documents (PDF/MD/DOCX)

## Your Current State

Your `AutomotiveSmartChunker` is well-designed for **structured, pre-parsed data** (enriched JSON from C headers, PlantUML, RST). However, it has **critical gaps** for your new use case:

| Gap | Impact |
|-----|--------|
| No PDF parser | Can't ingest AUTOSAR specs (SWS, EXP) directly |
| No DOCX parser | Can't ingest HW manuals (1000+ pages) |
| No large-document chunking | 2000-token limit insufficient for cross-referencing in 800-page docs |
| No table/figure extraction | AUTOSAR specs are table-heavy (SWS API tables, config params) |
| No .arxml support | AUTOSAR configuration data not parseable |

---

## Recommended Chunking Strategy: **Hybrid Hierarchical + Semantic Chunking**

For your automotive MCAL use case, no single strategy works. You need a **layered approach** tailored to document type.

### Strategy Overview

```
                    ┌──────────────────────────────┐
                    │   Document Ingestion Router   │
                    └──────────┬───────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                   │
     ┌──────▼──────┐   ┌──────▼──────┐    ┌──────▼──────┐
     │  PDF Parser  │   │  DOCX Parser │    │  MD/RST     │
     │  (Docling/   │   │  (python-    │    │  (existing) │
     │   PyMuPDF)   │   │   docx)      │    │             │
     └──────┬──────┘   └──────┬──────┘    └──────┬──────┘
            │                  │                   │
            └──────────────────┼───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  Structure-Aware Pre-Processor│
                    │  (Heading tree, tables, figs) │
                    └──────────┬───────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                   │
     ┌──────▼──────┐   ┌──────▼──────┐    ┌──────▼──────┐
     │  Hierarchical│   │  Semantic   │    │  Table/API  │
     │  Section     │   │  Boundary   │    │  Structured │
     │  Chunking    │   │  Chunking   │    │  Chunking   │
     └──────┬──────┘   └──────┬──────┘    └──────┬──────┘
            │                  │                   │
            └──────────────────┼───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  Chunk Enrichment & Metadata  │
                    │  (Parent-child, cross-refs)   │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  Embedding + KG Ingestion     │
                    └──────────────────────────────┘
```

---

## The 5 Chunking Strategies Evaluated

### 1. Fixed-Size Token Chunking (LangChain `RecursiveCharacterTextSplitter`)

| Aspect | Assessment |
|--------|------------|
| **How it works** | Split every N tokens with M overlap |
| **Typical config** | `chunk_size=1000, overlap=200` |
| **Pros** | Simple, predictable, works everywhere |
| **Cons** | Breaks mid-sentence, mid-table, mid-API-definition |
| **Verdict for MCAL** | **REJECT** - Destroys AUTOSAR table structures, register definitions split mid-field |

**Why it fails for your case:** An AUTOSAR SWS API table like `Adc_Init` has parameter descriptions, return values, DET error codes — splitting at 1000 tokens can put the function name in one chunk and its error codes in another.

---

### 2. Hierarchical / Structure-Aware Chunking (RECOMMENDED - Primary)

| Aspect | Assessment |
|--------|------------|
| **How it works** | Parse document structure (headings, ToC), chunk at section boundaries |
| **Implementation** | Extend your existing `HierarchicalSection` to 4 levels for AUTOSAR |
| **Pros** | Preserves logical units, tables stay intact, cross-refs maintained |
| **Cons** | Requires good heading detection; some sections too large (>8000 tokens) |
| **Verdict for MCAL** | **PRIMARY STRATEGY** - AUTOSAR specs are heavily structured with clear hierarchies |

**How it maps to AUTOSAR documents:**

```
Level 1: Chapter (e.g., "7 API specification")
  Level 2: Section (e.g., "7.1 Imported types")
    Level 3: Subsection (e.g., "7.1.1 Adc_ConfigType")
      Level 4: Content blocks (tables, code, descriptions) → ROLLUP into L3
```

**Key rules for automotive:**
- **Never split a table** — AUTOSAR SWS API tables must be atomic chunks
- **Never split a register definition** — Bitfield + reset value + description = 1 chunk
- **Keep [SWS_xxx_yyyyy] requirement tags with their content** — These are your traceability anchors
- **Preserve `[confs]` configuration parameter blocks** — These map to MCAL config structs

**Recommended config:**
```python
HIERARCHICAL_CONFIG = {
    "max_chunk_tokens": 4000,      # Larger than your current 2000 — AUTOSAR tables need room
    "min_chunk_tokens": 200,       # Don't create tiny fragments
    "max_levels": 4,               # AUTOSAR goes deeper than 3
    "rollup_threshold": 500,       # Sections < 500 tokens merge with parent
    "table_atomic": True,          # Never split tables
    "code_block_atomic": True,     # Never split code blocks
    "preserve_tag_context": True,  # Keep [SWS_xxx] with surrounding content
}
```

---

### 3. Semantic / Topic-Based Chunking (RECOMMENDED - Secondary)

| Aspect | Assessment |
|--------|------------|
| **How it works** | Use embedding similarity to detect topic shifts; split at low-similarity boundaries |
| **Implementation** | `LangChain SemanticChunker` or custom with sentence-transformers |
| **Pros** | Catches topic shifts within long sections; groups related content |
| **Cons** | Slower (requires embedding each sentence), less deterministic |
| **Verdict for MCAL** | **SECONDARY / FALLBACK** - Use when hierarchical chunks are too large (>4000 tokens) |

**When to use for AUTOSAR:**
- Long narrative sections without sub-headings (e.g., "General Description" chapters)
- HW manual operational descriptions (e.g., "ADC conversion sequence" spread across 5 pages without headings)
- Error handling descriptions that span multiple paragraphs

**Config:**
```python
SEMANTIC_CONFIG = {
    "embedding_model": "all-MiniLM-L6-v2",  # Fast, local
    "similarity_threshold": 0.75,             # Split when cosine sim drops below this
    "min_chunk_sentences": 5,
    "max_chunk_tokens": 4000,
    "buffer_size": 3,                         # Sentences of context overlap
}
```

---

### 4. Table-Aware / Structured Element Chunking (RECOMMENDED - For Tables/Figures)

| Aspect | Assessment |
|--------|------------|
| **How it works** | Detect tables, figures, code blocks; extract as standalone chunks with metadata |
| **Implementation** | PyMuPDF (tables), Docling, or Camelot for PDF tables |
| **Pros** | Tables preserved perfectly; structured data queryable |
| **Cons** | Table detection not 100% accurate in complex PDFs |
| **Verdict for MCAL** | **CRITICAL** - AUTOSAR specs are 40-60% tables |

**AUTOSAR table types you'll encounter:**

| Table Type | Example | Chunking Rule |
|------------|---------|---------------|
| API function table | `Adc_Init`, params, return, DET errors | 1 table = 1 chunk, tag with `[SWS_Adc_00xxx]` |
| Configuration parameter table | `AdcHwUnit`, multiplicity, default | 1 table = 1 chunk, link to config struct |
| Register map | Base address, offset, field names | 1 register block = 1 chunk |
| Error code table | DET error ID, description | Full table = 1 chunk |
| State machine table | State transitions | Full table = 1 chunk |
| Timing table | Min/max/typical values | Full table = 1 chunk |

**Implementation approach:**
```python
class TableAwareChunker:
    def extract_tables(self, document) -> List[TableChunk]:
        """Extract tables as atomic chunks with metadata."""
        for table in document.tables:
            yield TableChunk(
                content=table.to_markdown(),  # or structured dict
                metadata={
                    "type": "table",
                    "caption": table.caption,
                    "sws_tags": extract_sws_tags(table),
                    "column_headers": table.headers,
                    "row_count": len(table.rows),
                    "parent_section": table.parent_heading,
                    "module": detect_module(table),
                }
            )
```

---

### 5. Sliding Window with Propositions (Research-Grade)

| Aspect | Assessment |
|--------|------------|
| **How it works** | Convert text to atomic propositions, then group propositions by topic |
| **Reference** | "Dense X Retrieval" (2023) paper |
| **Pros** | Best retrieval recall; each proposition is self-contained |
| **Cons** | Requires LLM call per page (~$expensive for 1000-page docs), slow |
| **Verdict for MCAL** | **NOT RECOMMENDED** for initial ingestion; consider for high-value sections only |

---

## Best Strategy for Your Specific Document Types

### AUTOSAR SWS Specifications (PDF, 200-800 pages)

```
Document: AUTOSAR_SWS_ADCDriver.pdf (400 pages)

Strategy: Hierarchical + Table-Aware
──────────────────────────────────────

Step 1: PDF → Structured Markdown (via Docling or PyMuPDF4LLM)
   - Preserves heading hierarchy
   - Extracts tables as markdown tables
   - Preserves [SWS_xxx_yyyyy] tags

Step 2: Heading-Based Split
   Chapter 7 "API Specification"
     → 7.1 "Type definitions" (chunk)
       → 7.1.1 "Adc_ConfigType" (chunk with table)
       → 7.1.2 "Adc_ChannelType" (chunk with table)
     → 7.2 "Function definitions"
       → 7.2.1 "Adc_Init" (chunk: description + API table + preconditions)
       → 7.2.2 "Adc_DeInit" (chunk)
     → 7.3 "Scheduled functions"
     → 7.4 "Expected interfaces"

Step 3: Metadata Enrichment
   Each chunk gets:
   - requirement_tags: [SWS_Adc_00100, SWS_Adc_00101, ...]
   - module: "ADC"
   - api_functions: ["Adc_Init", ...]
   - config_params: ["AdcHwUnit", ...]
   - autosar_version: "4.4.0"
   - document_type: "SWS"
```

### Hardware Manuals (PDF, 500-1500 pages)

```
Document: TC3xx_User_Manual.pdf (1200 pages)

Strategy: Hierarchical + Register-Aware + Semantic Fallback
────────────────────────────────────────────────────────────

Step 1: PDF → Markdown (PyMuPDF4LLM recommended for register tables)

Step 2: Module-Boundary Detection
   - Identify peripheral chapters: "Chapter 23: ADC Module"
   - Each peripheral = separate ingestion unit
   
Step 3: Per-Module Hierarchical Chunking
   Chapter 23: ADC
     → 23.1 "Feature Overview" (chunk)
     → 23.2 "Block Diagram" (chunk with figure reference)
     → 23.3 "Register Description"
       → 23.3.1 "ADC_GLOBCFG" (register chunk - ATOMIC)
         Includes: Address, reset value, ALL bitfields
       → 23.3.2 "ADC_GLOBICLASS0" (register chunk - ATOMIC)
     → 23.4 "Functional Description"
       → (Long section → Semantic chunking fallback)
     → 23.5 "Timing Specifications" (table chunk - ATOMIC)

Step 4: Cross-Reference Linking
   - Register chunks → linked to AUTOSAR SWS API chunks
   - Timing specs → linked to driver init/config chunks
```

### AUTOSAR EXP Documents (PDF, 50-200 pages)

```
Strategy: Simple Hierarchical (no table-heavy content)
- These are explanatory/conceptual
- Standard heading-based chunking works well
- max_chunk_tokens: 2000 (smaller, more focused)
```

### Large DOCX Hardware Specifications

```
Strategy: python-docx → Markdown → Hierarchical + Table-Aware
─────────────────────────────────────────────────────────────
 
Step 1: DOCX → Markdown via python-docx / mammoth
   - Preserve heading styles → markdown headings
   - Preserve tables → markdown tables
   - Extract images → save as referenced assets

Step 2: Same pipeline as PDF-derived markdown
```

---

## Recommended Implementation Architecture

```python
# New file: packages/graphrag_core/graphrag_core/chunking/document_chunker.py

from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

class DocumentType(Enum):
    AUTOSAR_SWS = "autosar_sws"       # SWS specification PDFs
    AUTOSAR_EXP = "autosar_exp"       # Explanatory PDFs  
    HW_MANUAL = "hw_manual"           # TC3xx user manuals
    HW_DATASHEET = "hw_datasheet"     # Peripheral datasheets
    DESIGN_DOC = "design_doc"         # Architecture/design DOCX
    TEST_SPEC = "test_spec"           # Test specification docs

@dataclass
class ChunkConfig:
    max_tokens: int = 4000
    min_tokens: int = 200
    overlap_tokens: int = 100          # For context continuity
    max_heading_depth: int = 4
    table_atomic: bool = True          # Never split tables
    code_block_atomic: bool = True
    rollup_small_sections: bool = True
    rollup_threshold: int = 300
    semantic_fallback_threshold: int = 6000  # Use semantic if section > this

# Per-document-type configs
CONFIGS = {
    DocumentType.AUTOSAR_SWS: ChunkConfig(
        max_tokens=4000,     # SWS tables can be large
        max_heading_depth=4, # SWS has deep nesting
        table_atomic=True,
    ),
    DocumentType.HW_MANUAL: ChunkConfig(
        max_tokens=4000,
        max_heading_depth=4,
        semantic_fallback_threshold=5000,  # HW manuals have long narrative sections
    ),
    DocumentType.AUTOSAR_EXP: ChunkConfig(
        max_tokens=2000,     # Simpler, conceptual content
        max_heading_depth=3,
    ),
    DocumentType.DESIGN_DOC: ChunkConfig(
        max_tokens=3000,
        max_heading_depth=3,
    ),
}
```

---

## PDF Parser Comparison for Your Use Case

| Parser | Table Quality | Heading Detection | Speed (1000pg) | AUTOSAR Tags | License | Recommendation |
|--------|-------------|-------------------|-----------------|-------------|---------|----------------|
| **Docling** (IBM) | Excellent | Excellent | ~15 min | Good | MIT | **Best overall** |
| **PyMuPDF4LLM** | Good | Excellent | ~5 min | Good | AGPL/Commercial | **Best speed** |
| **Unstructured.io** | Good | Good | ~20 min | Moderate | Apache 2.0 | Good but heavy |
| **LlamaParse** | Excellent | Excellent | Cloud-based | Excellent | Commercial | **Best accuracy** (paid) |
| **pdfplumber** | Excellent tables | Manual | ~10 min | Manual | MIT | Best for table-focused work |
| **marker** | Good | Good | ~8 min | Good | GPL | Good OCR support |

**My recommendation:** **Docling** as primary (MIT license, excellent tables+structure), **PyMuPDF4LLM** as fast fallback.

---

## Critical Metadata to Capture Per Chunk

For your automotive KG, each chunk must carry:

```python
@dataclass
class AutomotiveDocumentChunkMetadata:
    # Identity
    chunk_id: str                          # Unique ID
    document_name: str                     # Source filename
    document_type: DocumentType            # SWS, HW manual, etc.
    
    # Position
    page_numbers: List[int]                # Original PDF pages
    section_path: str                      # "7 > 7.2 > 7.2.1 Adc_Init"
    heading_hierarchy: List[str]           # ["API Specification", "Function definitions", "Adc_Init"]
    
    # AUTOSAR-specific
    sws_requirement_tags: List[str]        # [SWS_Adc_00100, SWS_Adc_00101]
    autosar_version: str                   # "R22-11", "4.4.0"
    module_name: str                       # "ADC", "CAN", "SPI"
    
    # Content classification
    content_type: str                      # "api_definition", "register_map", "timing_spec", "narrative"
    contains_table: bool
    contains_code: bool
    contains_figure_ref: bool
    
    # Relationships  
    parent_chunk_id: Optional[str]         # For hierarchical navigation
    sibling_chunk_ids: List[str]           # Same-level chunks
    cross_reference_ids: List[str]         # Referenced [SWS_xxx] in other chunks
    
    # Retrieval helpers
    token_count: int
    summary: str                           # LLM-generated 1-line summary (optional)
    keywords: List[str]                    # Extracted automotive keywords
```

---

## Chunk Size Recommendations

| Document Type | Recommended Size | Rationale |
|---------------|-----------------|-----------|
| AUTOSAR SWS API tables | **2000-4000 tokens** | One API = one chunk; tables can be large |
| AUTOSAR SWS narrative | **1500-2500 tokens** | Conceptual descriptions |
| HW manual register maps | **1000-3000 tokens** | One register block = one chunk |
| HW manual functional desc | **2000-4000 tokens** | Needs enough context for operational sequences |
| Config parameter tables | **1500-3000 tokens** | Keep related params together |
| Timing/electrical specs | **500-1500 tokens** | Usually compact tables |
| Design documents | **1500-2500 tokens** | Standard document chunking |

**Why 4000 max instead of your current 2000:** AUTOSAR SWS API definition tables (function signature + parameters + return + DET errors + pre/post conditions) regularly hit 2500-3500 tokens. Splitting these destroys the API's semantic completeness.

---

## Summary: Recommended Implementation Plan

| Priority | Action | Impact |
|----------|--------|--------|
| **P0** | Add Docling PDF parser | Unblocks AUTOSAR SWS + HW manual ingestion |
| **P0** | Add python-docx/mammoth DOCX parser | Unblocks DOCX hw manuals |
| **P1** | Extend `HierarchicalSection` to 4 levels + table-atomic rule | Correct chunking for AUTOSAR structure |
| **P1** | Increase `max_chunk_tokens` to 4000 for SWS/HW docs | Prevents splitting API tables |
| **P1** | Add `[SWS_xxx]` tag extraction to chunk metadata | Requirement traceability |
| **P2** | Add semantic fallback for oversized sections | Handles long narrative HW manual sections |
| **P2** | Add per-document-type `ChunkConfig` routing | One config doesn't fit all |
| **P3** | Add chunk cross-referencing (SWS tag → chunk mapping) | Enables requirement-based retrieval across docs |
| **P3** | Add .arxml parser for AUTOSAR configuration data | Complete AUTOSAR support |

---

**Questions for you before I implement anything:**

1. **Which PDF parser** do you prefer? Docling (MIT, best quality) vs PyMuPDF4LLM (fast, AGPL)?
2. **Are the AUTOSAR specs you'll ingest primarily SWS (API specs) or also BSW module descriptions, EXP docs, and AUTOSAR schema (.arxml)?** This affects parser priority.
3. **Do you need OCR support?** Some older HW manuals are scanned PDFs.
4. **Will this run locally or on a server?** Affects whether cloud-based parsers (LlamaParse) are viable.
5. **What's your typical retrieval query pattern?** e.g., "What API initializes ADC?" vs "What register controls ADC clock?" — this affects whether you need more granular or coarser chunks.

=======================
# Gemini
====================

Processing large, highly technical documents like 1000-page microcontroller hardware reference manuals and extensive AUTOSAR specifications requires moving beyond basic chunking methods. Standard approaches often destroy the context needed for embedded C programming, where a single missed register bit-field or SWS (Software Specification) requirement can break a low-level driver.

Here is a thorough breakdown of chunking strategies, evaluated specifically for automotive MCAL development, followed by the recommended approach.

### 1. Structural / Hierarchical Chunking (Highly Recommended)

This strategy parses documents based on their inherent structure (Chapters, Headers, Sub-headers) rather than arbitrary character counts.

* **How it works:** You use parsers (like Unstructured.io or LlamaParse) to identify Document Object Models (DOM). A chunk becomes a specific subsection, such as "Section 7.2.4: ADC Interrupt Registers."
* **MCAL Use Case:** Perfect for AUTOSAR specifications and `.doc/.md` files. When querying an API requirement, you want the entire API description (Function signature, Parameters, Return values, Reentrancy) kept together in one chunk, not split across two.
* **Pros:** Preserves exact contextual boundaries.
* **Cons:** Can result in highly variable chunk sizes. If a single chapter is 30 pages long, you will still need a secondary splitting mechanism.

### 2. Domain-Specific / Logical Chunking (Crucial for HW Manuals)

This involves writing custom regex or logic to chunk data based on domain-specific boundaries, such as C-code functions, register maps, or requirements tags.

* **How it works:** * **For HW Manuals:** You isolate tables. Microcontroller manuals are essentially thousands of tables defining Base Addresses, Offsets, and Bit-fields. A table must be extracted as a single markdown or HTML chunk.
* **For AUTOSAR:** You chunk by requirement IDs (e.g., `[SWS_Can_00001]`).
* **For C Code:** You chunk by abstract syntax tree (AST) boundaries (e.g., keeping an entire `Spi_Init()` function and its associated struct definitions together).


* **Pros:** Yields the highest accuracy for deterministic engineering queries ("What is the bit offset for the CAN RX interrupt flag?").
* **Cons:** High initial setup effort. Requires custom parsing pipelines for different document vendors (e.g., Infineon vs. NXP manuals).

### 3. Graph-Based / Entity-Centric Chunking (GraphRAG)

Instead of just storing text, this strategy extracts entities (modules, registers, APIs) and maps their relationships before chunking.

* **How it works:** The ingestion pipeline identifies entities (e.g., `Gpt_Init`, `GPT_HW_TIMER_0`, `Prescaler`) and creates nodes. Chunks are enriched with a summary of their relationships.
* **MCAL Use Case:** MCAL development is highly relational. An AUTOSAR complex driver might depend on specific clock trees defined in a completely different 800-page hardware manual. Graph-based chunking allows the LLM to traverse these cross-document dependencies during architecture and design phases.
* **Pros:** Unmatched for complex, multi-hop reasoning across different documents (e.g., mapping an AUTOSAR spec requirement to a specific hardware register).
* **Cons:** Computationally expensive to build the graph during ingestion.

### 4. Semantic Chunking

This method uses embedding models to group sentences that are semantically similar, creating a break when the topic shifts.

* **How it works:** Calculates the cosine similarity between adjacent sentences. If the similarity drops below a threshold, a new chunk is created.
* **MCAL Use Case:** Good for high-level architecture documents, design rationale, and test strategy documents where narrative flow matters more than rigid structure.
* **Pros:** Reduces noise in the context window.
* **Cons:** Struggles with highly dense technical tables and code snippets where semantic meaning is less narrative and more structural.

### 5. Fixed-Size Chunking with Overlap (The Baseline)

Splitting text into fixed token sizes (e.g., 512 tokens) with a sliding window overlap (e.g., 50 tokens).

* **How it works:** Brute-force splitting.
* **MCAL Use Case:** Only useful as a fallback mechanism for plain text or when structural parsing fails.
* **Pros:** Fast, easy to implement, guarantees uniform token usage.
* **Cons:** Terrible for embedded development. It will arbitrarily slice a register definition table in half, blinding the LLM to critical bit-field descriptions.

---

### Which is the "Best" Strategy for Your Use Case?

There is no single "best" strategy for an entire MCAL lifecycle; the best approach is a **Hybrid Routing Strategy** based on the document type:

1. **For HW Reference Manuals (PDFs):** Use **Domain-Specific Chunking focused on Table Extraction**. Use an advanced parser (like LlamaParse or specialized vision models) to convert register tables into Markdown. Keep each table as a distinct chunk, enriched with its parent section header (e.g., "SPI Baud Rate Register").
2. **For AUTOSAR Specifications (.doc/.pdf):** Use **Hierarchical Chunking** to split strictly by SWS requirement IDs and API definitions.
3. **For Cross-Domain Architecture (GraphRAG):** Use **Graph-Based Chunking**. Since you need to connect software architecture to hardware constraints, building an entity graph will yield the most accurate design and code generation results.

---

### Clarifications Needed to Refine This Further:

To give you a more precise pipeline recommendation, I have a few questions about your current setup:

1. **Document Quality:** Are the hardware PDFs text-searchable (native digital), or are they scanned copies requiring OCR?
2. **Downstream LLM:** What model are you using for generation, and what is its context window limits? (This dictates how large our structural chunks can safely be).
3. **Current Tooling:** Are you building this ingestion pipeline from scratch using LangChain/LlamaIndex, or are you looking to integrate this into a specific framework or agentic setup (like crewAI)?

Would you like me to outline a sample Python code snippet for parsing hardware register tables using a hierarchical approach?
-----

Having text-searchable PDFs and access to advanced models like GPT5.4 or Claude 4.6 gives you a massive advantage. While these models have enormous context windows, dumping 1000 pages directly into the prompt is inefficient, highly expensive, and prone to "lost in the middle" hallucinations—which is unacceptable for deterministic MCAL logic where a single wrong bit-mask causes a hard fault.

Building from scratch allows you to design a highly specialized ingestion engine. Since MCAL development requires linking high-level architecture to low-level hardware constraints, the most robust approach is an automotive-focused GraphRAG architecture.

Here is the ideal chunking and ingestion strategy for your custom pipeline.

### 1. Document-Specific Chunking Rules

Standard text splitters will destroy your data. You must treat different documents with specialized parsing logic before embedding them.

* **Hardware Manuals (PDFs):** Implement **Table-Centric Chunking**. Since your PDFs are text-searchable, use modern parsing libraries like `Docling` or `PyMuPDF4LLM` to convert the documents directly into Markdown. Write custom logic to isolate every register map or bit-field table as a single, unbreakable Markdown chunk. Crucially, prepend the hierarchical chapter headers (e.g., "Chapter 12: SPI -> Section 12.4: Control Registers -> SPI_CR1") to the table chunk so the context of the peripheral is never lost.
* **AUTOSAR Specifications (.doc / .md):** Implement **Regex-Driven Hierarchical Chunking**. AUTOSAR documents are highly structured. Write a script to split the text strictly by requirement tags (e.g., `[SWS_Gpt_00341]`). Group the requirement tag, the normative text, the parameter description, and the associated API function signature into one atomic chunk.
* **Source Code (C/H files):** Implement **AST (Abstract Syntax Tree) Chunking**. Use a tool like `Tree-sitter` to parse your existing driver code. This ensures that an entire driver initialization function, alongside its associated `#define` macros and struct definitions, remains intact within the same chunk, rather than being split arbitrarily by line counts.

### 2. The Ingestion Pipeline Architecture

To leverage models like Claude 4.6 effectively for generating architecture, design, and test cases, your custom pipeline should process the chunks into a dual-layered retrieval system.

* **Parsing & Markdown Conversion:** Pass all PDFs and Word documents through a Markdown converter. Markdown is the optimal format for LLMs to understand tables and code blocks.
* **Entity Extraction:** Before embedding the chunks into a vector database, run a lightweight extraction pass. Prompt the LLM to identify key entities within each chunk, such as Peripheral Names, Memory Addresses, Register Names, SWS IDs, and API signatures.
* **Graph Construction:** Store the raw Markdown chunks in a standard vector database (like Qdrant or Milvus) for semantic search. Simultaneously, map the extracted entities in a graph database (like Neo4j). Create edges that define relationships, such as `[SWS_Spi_0001]` -> *Requires* -> `SPI_CR1 Register`.
* **Agentic Retrieval:** When you need to draft a design or generate test cases, use an agentic framework (like crewAI) to orchestrate the workflow. The agent can query the graph to find the exact hardware register, trace it back to the AUTOSAR requirement, pull those specific, highly accurate chunks from the vector database, and feed that precise context into GPT5.4.

### 3. Handling Massive Context Windows

With models capable of analyzing massive token counts, your chunking strategy shifts from "cramming data" to "precision feeding."

* **Pre-Processing for the LLM:** Instead of sending an entire hardware manual to Claude 4.6 to write a driver, your pipeline retrieves the 15 specific Markdown chunks containing the relevant register tables, the 5 relevant AUTOSAR requirement chunks, and the existing header file.
* **Context Assembly:** You dynamically assemble these high-fidelity chunks into a single, clean prompt. This dramatically reduces latency, cuts inference costs, and forces the LLM to ground its C code generation strictly in the provided register definitions.

---

Would you like me to provide a Python script using `Docling` or `PyMuPDF` that isolates and extracts a microcontroller register table from a native PDF into a clean Markdown chunk?

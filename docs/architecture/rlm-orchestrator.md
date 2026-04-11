# RLM Orchestrator Architecture

**Component**: `src/HybridRAG/code/querier/rlm_orchestrator.py`
**Primary class**: `RLMOrchestrator` (712 lines)
**Dependencies**: `SearchService`, `ContextBuilder`, GPT4IFX (LLM proxy)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Position in Architecture](#2-position-in-architecture)
3. [Task Types](#3-task-types)
4. [DA-to-Task Mapping](#4-da-to-task-mapping)
5. [Orchestration Flow](#5-orchestration-flow)
6. [Sub-Query Planning](#6-sub-query-planning)
7. [Synthesis](#7-synthesis)
8. [Preview Mode](#8-preview-mode)
9. [Configuration](#9-configuration)

---

## 1. Overview

The RLM (Retrieval-augmented Language Model) Orchestrator handles **complex multi-step retrieval** for queries that cannot be answered with a single search pass. It decomposes a complex query into up to 6 targeted sub-queries, executes each against the Hybrid RAG engine, and synthesizes the combined results.

**Key design decision**: RLM is an **internal** Core Engine capability, not a public DA-facing tool. DAs interact with `build_context` which may internally delegate to RLM when the query complexity warrants it. This keeps the DA lifecycle stable — DAs don't need to know about multi-step retrieval. See [ADR-009](DECISIONS.md#adr-009-rlm-as-internal-context-orchestrator).

However, two MCP tools are exposed at the **developer** tier for visibility and debugging:
- `rlm_orchestrate` — execute a full orchestration
- `rlm_preview` — inspect the generated plan without executing it

---

## 2. Position in Architecture

```
Domain Assistant
      │
      │  build_context(query, session_id)
      ▼
┌─────────────────────────────────┐
│         MCP Server              │
│                                 │
│  build_context tool handler     │
│         │                       │
│    ┌────┴────────────┐          │
│    │  Simple query?  │          │
│    │  (single topic) │          │
│    └────┬────────┬───┘          │
│     YES │        │ NO           │
│         ▼        ▼              │
│  ContextBuilder  RLMOrchestrator│  ← Internal delegation
│  (direct fill)   (multi-step)   │
│         │              │        │
│         │    ┌─────────┘        │
│         │    │                  │
│         │    ├── Plan (LLM)     │
│         │    ├── Sub-query 1 → SearchService
│         │    ├── Sub-query 2 → SearchService
│         │    ├── ...            │
│         │    ├── Sub-query N    │
│         │    └── Synthesize     │
│         │              │        │
│         └──────┬───────┘        │
│                ▼                │
│         Assembled context       │
│         (token-budget-aware)    │
└─────────────────────────────────┘
```

---

## 3. Task Types

`RLMTaskType` is an enum with **24 values** covering all V-Model lifecycle phases:

### Requirements Phase

| Task Type | Description |
|-----------|-------------|
| `REQUIREMENT_REVIEW` | Review requirements for completeness, ambiguity, testability |
| `REQUIREMENT_DRAFTING` | Draft requirements from stakeholder inputs |
| `REQUIREMENT_MANAGEMENT` | Manage requirement lifecycles and relationships |

### Architecture Phase

| Task Type | Description |
|-----------|-------------|
| `ARCHITECTURE_ANALYSIS` | Analyze software architecture, detect design issues |
| `ARCHITECTURE_TRACEABILITY` | Trace architecture decisions to requirements |

### Implementation Phase

| Task Type | Description |
|-----------|-------------|
| `CODE_GENERATION` | Generate compliant C code from specs |
| `CODE_TRANSFORMATION` | Transform/refactor existing code |
| `CODE_REVIEW` | Review code for correctness and compliance |
| `BUGFIX_ANALYSIS` | Analyze and fix bugs, warnings, MISRA violations |
| `CONFIG_GENERATION` | Generate AUTOSAR configuration code |
| `PAGE_GENERATION` | Generate documentation pages |

### Testing Phase

| Task Type | Description |
|-----------|-------------|
| `TEST_GENERATION` | Generate test cases from requirements and code |
| `TEST_VERIFICATION` | Verify test case quality and coverage |
| `TEST_QUALITY_ANALYSIS` | Analyze overall test quality metrics |

### Safety Phase

| Task Type | Description |
|-----------|-------------|
| `MISRA_REVIEW` | MISRA C:2012 compliance checking |
| `SAFETY_VALIDATION` | Validate ISO 26262 safety requirements |
| `SAFETY_ANALYSIS` | FMEA, FTA analysis |
| `HAZOP_ANALYSIS` | Hazard and operability studies |
| `DATA_FLOW_ANALYSIS` | Data flow analysis for safety |

### Cross-Cutting

| Task Type | Description |
|-----------|-------------|
| `TRACEABILITY` | V-Model traceability analysis |
| `DEBUG_ANALYSIS` | Debug analysis and root cause investigation |
| `KNOWLEDGE_INGESTION` | Knowledge ingestion workflows |
| `STOP_TYPING` | Placeholder (unused) |
| `GENERIC` | Fallback for unclassified queries |

---

## 4. DA-to-Task Mapping

`DA_TASK_MAPPING` maps each of the 21 Domain Assistants to one or more task types:

| Domain Assistant | Code | Task Types |
|-----------------|------|------------|
| Code Generator | CIA | `code_generation`, `bugfix_analysis` |
| Test Generator | GEST | `test_generation` |
| Code Reviewer | ACRA | `code_review`, `misra_review` |
| Architecture Analyst | SAGA | `architecture_analysis` |
| Architecture Tracer | ATRA | `architecture_traceability` |
| Requirements Reviewer | REVA | `requirement_review` |
| Requirements Drafter | PRQ | `requirement_drafting` |
| Requirements Manager | RMA | `requirement_management` |
| Config Generator | GECA | `config_generation` |
| Page Generator | PAGE | `page_generation` |
| Test Verifier | GEVT | `test_verification` |
| Test Quality Analyst | ATQA | `test_quality_analysis` |
| Safety Validator | SAVA | `safety_validation` |
| Safety Analyst | SAAN | `safety_analysis` |
| HAZOP Analyst | HZOP | `hazop_analysis` |
| Data Flow Analyst | DFA | `data_flow_analysis` |
| MISRA Reviewer | MIRA | `misra_review` |
| Traceability Analyst | TripleA | `traceability` |
| Debug Analyst | VoltAI | `debug_analysis` |
| Knowledge Weaver | KW | `knowledge_ingestion` |
| Code Transformer | CTA | `code_transformation` |

---

## 5. Orchestration Flow

```python
def orchestrate(self, query, task_type, workspace, module, session_id):
```

### Step 1: Plan

The orchestrator calls GPT4IFX with a task-type-specific planning prompt. The LLM returns a structured plan of N sub-queries (max 6):

```json
{
  "sub_queries": [
    {
      "query": "API signature for IfxCan_Node_init",
      "purpose": "Get function signature and parameters",
      "alpha": 0.3,
      "node_types": ["APIFunction"]
    },
    {
      "query": "IfxCan_Config struct definition",
      "purpose": "Get configuration struct fields",
      "alpha": 0.8,
      "node_types": ["DataStructure"]
    },
    ...
  ]
}
```

### Step 2: Execute Sub-Queries

Each sub-query is executed via `SearchService.search()` with:
- The planned query text
- The planned alpha value (per-sub-query alpha)
- Node type filters from the plan
- An 8K token budget per step

### Step 3: Build Per-Step Context

Each sub-query result is assembled into a context chunk using `ContextBuilder` with the step's 8K budget.

### Step 4: Synthesize

All per-step contexts are combined and (optionally) passed through the LLM for synthesis — producing a coherent, deduplicated final context.

### Step 5: Return

The final assembled context is returned to the `build_context` tool handler, which delivers it to the DA.

---

## 6. Sub-Query Planning

The planning prompt is **task-type-aware**. Each `RLMTaskType` has domain-specific instructions:

### Example: `CODE_GENERATION` Planning

```
Given the user query: "Generate initialization code for CAN module"

Generate sub-queries to gather:
1. API function signatures (alpha=0.3, structural)
2. Configuration struct definitions (alpha=0.8, structural)
3. Dependency/initialization ordering (alpha=0.3)
4. Register access patterns (alpha=0.3)
5. MISRA compliance rules for init patterns (alpha=0.5)
6. Similar approved code examples (alpha=0.7, semantic)
```

### Example: `TEST_GENERATION` Planning

```
Given the user query: "Generate test cases for ADC conversion"

Generate sub-queries to gather:
1. Requirements linked to ADC conversion (alpha=0.5)
2. API functions under test (alpha=0.3)
3. Expected value ranges from HW specs (alpha=0.5)
4. Existing test patterns (alpha=0.7)
5. Coverage gaps in current test suite (alpha=0.3)
```

The per-sub-query alpha values are tuned for the type of information needed: structural lookups use low alpha (graph-heavy), semantic lookups use high alpha (vector-heavy).

---

## 7. Synthesis

After all sub-queries complete, the orchestrator has N context chunks (one per step). Synthesis options:

1. **Concatenation** (default): simply concatenate all chunks within the token budget
2. **LLM synthesis**: pass combined chunks through GPT4IFX with a synthesis prompt that:
   - Deduplicates overlapping information across steps
   - Orders content by relevance to the original query
   - Ensures coherent narrative flow

The synthesis choice depends on total token count — if concatenation fits within budget, no LLM call is needed. If it exceeds budget, LLM synthesis with summarization is used.

---

## 8. Preview Mode

`rlm_preview` returns the generated plan **without executing** the sub-queries:

```python
preview = rlm.preview(query, task_type, workspace, module)
# Returns:
{
    "sub_queries": [...],
    "estimated_steps": 4,
    "estimated_tokens": 32000,
    "task_type": "code_generation",
    "da_name": "CIA"
}
```

This lets operators:
- Inspect planned sub-queries before execution
- Estimate cost and latency
- Validate that the planner is interpreting queries correctly
- Debug unexpected search patterns

---

## 9. Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_STEPS` | 6 | Maximum sub-queries per orchestration |
| `SUB_BUDGET` | 8000 | Token budget per sub-query step |
| LLM model | GPT4IFX | Infineon LLM proxy for planning + synthesis |
| `DA_TASK_MAPPING` | 21 entries | Maps DA names to task types |
| Alpha per step | Varies | Set per sub-query by the planner |

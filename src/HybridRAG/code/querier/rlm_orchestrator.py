"""
RLM Orchestrator — Sprint 5 → Sprint 8
========================================
Recursive Language Model context builder. Multi-step variant of build_context.

Architecture (from PPTX-aligned note):
  DA → rlm_orchestrate → Context Orchestrator → Strategy Selector
    → RLM Strategy: Root LLM plans N sub-queries (max 6)
    → Each sub-query: SearchService + ContextBuilder (8K budget)
    → Final synthesis: LLM merges all sub-results

Complexity routing heuristic (2-of-3 triggers RLM):
  1. 3+ functions needed
  2. Register-level keywords present
  3. ASIL-B/D requirements detected

Sprint 8: Full 24-task planning prompts + synthesis instructions ported from
docs/new_architecture/rlm_task_types.py. All Domain Assistants now get
tailored decomposition instead of falling back to generic.
ContextBuilder (slot-based, token-budget) used for per-step assembly.

Uses GPT4IFX OpenAI-compatible proxy for all LLM calls.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

from .context_builder import (
    AssembledContext, ContextBuilder, ContextBudget, ContextItem, ContextSlot,
    estimate_tokens,
)
from .kg_node_utils import Source, classify_source

logger = logging.getLogger(__name__)

MAX_STEPS = 6
SUB_BUDGET = 8000

# Sentinel returned by the default LLM when all retries fail. The planner treats
# it as an unparseable plan (and falls back to the full query); synthesis treats
# it as a degraded result and surfaces a `degraded` flag (F-CC-R02).
_LLM_UNAVAILABLE_SENTINEL = "[LLM unavailable]"

# ── Synthesis token budgeting (F-CC-R04) ─────────────────────────────
# Tokens reserved for the synthesis response, and per-model context windows.
_SYNTH_RESPONSE_TOKENS = 8000
_DEFAULT_MODEL_CONTEXT_TOKENS = 32000


def _load_model_context_tokens() -> Dict[str, int]:
    """Per-model context windows, overridable via ``RLM_MODEL_CONTEXT_TOKENS``.

    The env var, when set, is a JSON object mapping model name -> context-window
    token count; entries merge over (and extend) the built-in defaults so a new
    model release needs no code change (F5 follow-up to F-CC-R04). A malformed
    override is ignored with a warning so it can never break planning.
    """
    base: Dict[str, int] = {
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4": 128000,
        "gpt-5.2": 128000,
    }
    raw = os.environ.get("RLM_MODEL_CONTEXT_TOKENS", "").strip()
    if raw:
        try:
            override = json.loads(raw)
            if not isinstance(override, dict):
                raise ValueError("expected a JSON object")
            for name, tokens in override.items():
                base[str(name)] = int(tokens)
        except (ValueError, TypeError) as exc:
            logger.warning("Ignoring invalid RLM_MODEL_CONTEXT_TOKENS: %s", exc)
    return base


_MODEL_CONTEXT_TOKENS = _load_model_context_tokens()

# ── Config-driven default alpha (MEG_SW-308) ─────────────────────────
_DEFAULT_ALPHA: Optional[float] = None


def _get_default_alpha() -> float:
    """Return the default RRF blend factor from storage_config.yaml (cached)."""
    global _DEFAULT_ALPHA
    if _DEFAULT_ALPHA is not None:
        return _DEFAULT_ALPHA
    try:
        from env_config import get_default_search_alpha
        _DEFAULT_ALPHA = get_default_search_alpha()
    except Exception:
        _DEFAULT_ALPHA = 0.6
    return _DEFAULT_ALPHA


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Extract the first complete JSON object from *raw* LLM output.

    F-CC-R03: A greedy regex (``\\{[\\s\\S]*\\}``) matches from the first ``{`` to
    the *last* ``}`` in the string, which breaks when the model emits trailing
    prose, markdown fences, or multiple JSON-looking fragments. This brace-counting
    scanner respects string literals and escape sequences, returning the first
    *valid* JSON object and skipping malformed or non-dict brace fragments.

    Returns the parsed ``dict`` or ``None`` when no valid object is present.
    """
    search_from = 0
    n = len(raw)
    while True:
        start = raw.find("{", search_from)
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        end = None
        for i in range(start, n):
            c = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
        if end is not None:
            try:
                parsed = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        # Malformed, unbalanced, or non-dict block — resume at the next '{'.
        search_from = start + 1


# ── Shared httpx + OpenAI client (connection pooling) ─────────────────
import threading as _threading
_rlm_client_lock = _threading.Lock()  # M08 fix: guard global mutable state
_shared_http_client = None
_shared_openai_client = None
_shared_openai_token = None   # tracks token to detect refreshes


def _get_shared_openai_client():
    """Return a shared OpenAI client, reusing the httpx connection pool.

    Rebuilds only when the auth token has changed (refresh).
    """
    global _shared_http_client, _shared_openai_client, _shared_openai_token
    with _rlm_client_lock:  # M08 fix
        from pathlib import Path
        import httpx
        from openai import OpenAI
        from src.HybridRAG.code.token_manager import get_token

        token = get_token()

        if _shared_openai_client is not None and token == _shared_openai_token:
            return _shared_openai_client

        # Build/rebuild httpx client only when needed
        if _shared_http_client is None:
            ca_bundle = Path(__file__).resolve().parent.parent / "ca-bundle.crt"
            if ca_bundle.exists():
                _shared_http_client = httpx.Client(verify=str(ca_bundle), timeout=httpx.Timeout(60))
            else:
                _shared_http_client = httpx.Client(timeout=httpx.Timeout(60))

        _shared_openai_client = OpenAI(
            base_url="https://gpt4ifx.icp.infineon.com",
            api_key=token,
            http_client=_shared_http_client,
        )
        _shared_openai_token = token
        return _shared_openai_client


def _reset_shared_openai_client() -> None:
    """Drop the cached OpenAI client so the next call rebuilds with a fresh token.

    Used after a 401 so a refreshed bearer token is picked up (F-CC-R01).
    """
    global _shared_openai_client, _shared_openai_token
    with _rlm_client_lock:
        _shared_openai_client = None
        _shared_openai_token = None


# ═════════════════════════════════════════════════════════════════════════
#  Task Types — catalog covering the AI Domain Assistants
# ═════════════════════════════════════════════════════════════════════════

class RLMTaskType(str, Enum):
    # Requirements
    REQUIREMENT_REVIEW = "requirement_review"
    REQUIREMENT_DRAFTING = "requirement_drafting"
    REQUIREMENT_MANAGEMENT = "requirement_management"
    # Architecture
    ARCHITECTURE_ANALYSIS = "architecture_analysis"
    ARCHITECTURE_TRACEABILITY = "architecture_traceability"
    # Code
    CODE_GENERATION = "code_generation"
    CODE_TRANSFORMATION = "code_transformation"
    CODE_REVIEW = "code_review"
    BUGFIX_ANALYSIS = "bugfix_analysis"
    CONFIG_GENERATION = "config_generation"
    PAGE_GENERATION = "page_generation"
    # Test
    TEST_GENERATION = "test_generation"
    TEST_VERIFICATION = "test_verification"
    TEST_QUALITY_ANALYSIS = "test_quality_analysis"
    # Safety
    MISRA_REVIEW = "misra_review"
    SAFETY_VALIDATION = "safety_validation"
    SAFETY_ANALYSIS = "safety_analysis"
    HAZOP_ANALYSIS = "hazop_analysis"
    DATA_FLOW_ANALYSIS = "data_flow_analysis"
    # Traceability
    TRACEABILITY = "traceability"
    # Debug
    DEBUG_ANALYSIS = "debug_analysis"
    # Infrastructure
    KNOWLEDGE_INGESTION = "knowledge_ingestion"
    STOP_TYPING = "stop_typing"
    # HSI
    HSI_ANALYSIS = "hsi_analysis"
    GENERIC = "generic"


# DA codes are the canonical AI Domain Assistant names from the Confluence
# "AI Assistants" space (MCSWAI). Reference table only (not subscripted at runtime).
DA_TASK_MAPPING: Dict[str, List[str]] = {
    # ── Canonical AI Domain Assistants (Confluence MCSWAI) ──
    "GEST": ["test_generation"], "GECA": ["config_generation"],
    "GEVT": ["test_verification"], "ACRA": ["code_review", "misra_review"],
    "ATRA": ["architecture_traceability"], "ATQA": ["test_quality_analysis"],
    "TripleA": ["traceability"], "REVA": ["requirement_review"],
    "SAVA": ["safety_validation"], "SASA": ["safety_analysis"],
    "DaFaA": ["data_flow_analysis"], "HAZOPA": ["hazop_analysis"],
    "PRQGEN": ["requirement_drafting"], "SWQMA": ["quality_management"],
    "J-WIZ": ["java_code_generation"], "Zephyr": ["zephyr_soc_generation"],
    # ── Embedded Driver Assistant (iLLD workspace) ──
    "EDA": ["code_generation"],
}


# ═════════════════════════════════════════════════════════════════════════
#  Data Classes
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class SubQueryStep:
    step_id: int
    intent: str
    query: str
    alpha: float = field(default_factory=_get_default_alpha)
    answer: str = ""
    sources_n: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0


@dataclass
class RLMContext:
    """Return value — same format concept as build_context output."""
    assembled_context: str
    sub_query_trace: List[SubQueryStep] = field(default_factory=list)
    total_tokens: int = 0
    total_elapsed_s: float = 0.0
    plan: List[Dict] = field(default_factory=list)
    module: str = ""
    profile: str = ""
    task_type: str = "generic"
    degraded: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assembled_context": self.assembled_context,
            "sub_queries": len(self.sub_query_trace),
            "total_tokens": self.total_tokens,
            "total_elapsed_s": round(self.total_elapsed_s, 2),
            "plan": self.plan,
            "module": self.module,
            "profile": self.profile,
            "task_type": self.task_type,
            "degraded": self.degraded,
            "sub_query_trace": [
                {"step": s.step_id, "intent": s.intent, "query": s.query,
                 "alpha": s.alpha, "sources": s.sources_n, "tokens": s.tokens}
                for s in self.sub_query_trace
            ],
        }


# ═════════════════════════════════════════════════════════════════════════
#  Planning Prompts (per task type)
# ═════════════════════════════════════════════════════════════════════════

_BASE_PLAN_INSTRUCTION = """
Decompose the task into {max_steps} or fewer targeted sub-queries.
Each sub-query must be answerable within an 8000-token context window.
Alpha controls graph-vs-vector blend: 0.8=graph-heavy, 0.3=vector-heavy, 0.5=balanced.

Return ONLY valid JSON:
{{"reasoning":"...","steps":[{{"step_id":1,"intent":"...","query":"...","alpha":0.5}},...]}}
"""

_PLAN_CONTEXT = {
    "requirement_review": (
        "Requirements review for automotive SW (Domain Assistant: REVA). "
        "Gather: the requirement(s) under review (ProductRequirement, StakeholderRequirement), "
        "parent/child chains (DERIVES_FROM), linked SWA elements, linked test specs, "
        "ASIL classification, safety measures, similar requirements for consistency, "
        "Jama metadata (status, feasibility, verifiability). "
        "Use alpha=0.8 for structural tracing, alpha=0.4 for semantic similarity."
    ),
    "requirement_drafting": (
        "Requirement drafting for AUTOSAR MCAL PRQ (Domain Assistant: PRQGEN). "
        "Gather: existing requirements for style reference, SHRQ parent chain, "
        "HW register specs for technical accuracy, AUTOSAR SWS references, "
        "naming conventions and ID patterns, ASIL inheritance rules, "
        "affected configuration parameters. "
        "Use alpha=0.6 balanced. Include HW register sub-query."
    ),
    "requirement_management": (
        "Requirement management. "
        "Gather: full traceability chains (Req→Arch→Code→Test→Result), "
        "coverage gaps (missing_code, missing_test), orphan artifacts, "
        "status distribution, ASIL distribution, impact analysis, cross-module deps. "
        "Use alpha=0.8 for structural traversal."
    ),
    "architecture_analysis": (
        "SW architecture analysis. "
        "Gather: SWA architectural decisions (§3.1), call sequences (§3.2), "
        "safety views and trusted boundaries (§3.3/§3.4), HW peripheral deps, "
        "SW dependencies, config container hierarchy, source file organisation, "
        "component models, ARXML module descriptions. "
        "Use alpha=0.8 for graph, alpha=0.3 for semantic."
    ),
    "architecture_traceability": (
        "Architecture traceability (Domain Assistant: ATRA). "
        "Gather: Req→Architecture mapping, Arch→Code mapping, Arch→Test mapping, "
        "orphan elements, gap analysis, call sequence vs code consistency, "
        "safety architecture completeness. "
        "Use alpha=0.8 throughout — structural graph traversal."
    ),
    "code_generation": (
        "AUTOSAR MCAL code generation on Infineon AURIX TC3xx (Domain Assistant: EDA). "
        "Gather: API function signatures, struct/enum definitions, dependency chains, "
        "HW register specs, MISRA C:2012 rules, existing approved patterns, "
        "AUTOSAR SWS API contracts, SWUD function-level design. "
        "Use alpha=0.8 for structured API queries, alpha=0.3 for semantic patterns. "
        "Use exact TC3xx register names."
    ),
    "code_transformation": (
        "Code transformation. "
        "Gather: source code to transform, target API contracts/patterns, "
        "MISRA rules for transformation, dependency graphs, register-level diffs "
        "(TC3xx→TC4xx), AUTOSAR migration patterns, config parameter changes. "
        "Use alpha=0.5 balanced."
    ),
    "bugfix_analysis": (
        "Bugfix analysis for AUTOSAR MCAL on Infineon AURIX TC3xx. "
        "This task handles targeted fixing of compiler warnings, Polyspace findings (Bugfinder + CodeProver), "
        "and MISRA C:2012 violations in existing driver source code. "
        "Gather: warning/error details (full text, severity, file, line), "
        "affected source code functions (body, callers, callees), "
        "MISRA C:2012 rule definitions for each reported violation, "
        "similar resolved bug patterns from the knowledge graph, "
        "register interaction patterns if warnings involve HW register access, "
        "Polyspace proof context for CodeProver findings (value ranges, unreachable paths), "
        "data flow paths through affected functions, "
        "error handling completeness (DET/DEM, defensive checks, return value propagation). "
        "Priority: Bugfinder required > CodeProver Red > Compiler > Advisory MISRA. "
        "Use alpha=0.6 balanced."
    ),
    "code_review": (
        "Full code review (Domain Assistant: ACRA). "
        "Gather: MISRA C:2012 rules by category (types, pointers, control flow, expressions), "
        "AUTOSAR API contracts, SWUD design spec, dependency/call graph, "
        "HW register usage validation, approved patterns, error handling completeness, "
        "resource cleanup verification, concurrency/reentrancy analysis. "
        "Use alpha=0.6 balanced. Separate MISRA and AUTOSAR sub-queries."
    ),
    "config_generation": (
        "Configuration generation/review (Domain Assistant: GECA). "
        "Gather: config container hierarchy, parameter types/defaults/ranges/constraints, "
        "ARXML parameter definitions, config dependencies, variant handling, "
        "requirements mandating config values, EB Tresos macros, derived params. "
        "Use alpha=0.8 for hierarchy, alpha=0.4 for semantic."
    ),
    "page_generation": (
        "Documentation generation. "
        "Gather: API signatures/params/returns, module overview, "
        "init/usage sequences, config parameters, error codes, "
        "HW peripheral overview, cross-references, safety classification, "
        "version history. "
        "Use alpha=0.5 balanced."
    ),
    "test_generation": (
        "AUTOSAR MCAL test generation (Domain Assistant: GEST). "
        "Gather: requirements to test with ASIL levels, API function signatures, "
        "dependency/init sequences, existing test patterns, polling requirements, "
        "config parameters affecting tests, error detection params for negative tests, "
        "HW register states for assertions. "
        "Use alpha=0.5 balanced."
    ),
    "test_verification": (
        "Test verification (Domain Assistant: GEVT). "
        "Gather: existing test cases, execution results (pass/fail, VP output, coverage), "
        "requirement traceability per test, missing coverage, WCET analysis, "
        "boundary/edge case coverage, VP config, cross-revision comparison. "
        "Use alpha=0.8 for structured, alpha=0.4 for semantic."
    ),
    "test_quality_analysis": (
        "Test quality analysis (Domain Assistant: ATQA). "
        "Gather: all test cases with traceability links, coverage metrics "
        "(statement, branch, MC/DC), requirement coverage %, ASIL coverage adequacy, "
        "test quality indicators, redundant/duplicate tests, orphan tests, "
        "execution time statistics. "
        "Use alpha=0.8 for structural."
    ),
    "misra_review": (
        "MISRA C:2012 code review (Domain Assistant: ACRA, MISRA mode). "
        "Gather rules by category: type rules (10.x), pointer rules (11.x, 18.x), "
        "control flow (14.x, 15.x), expression (12.x, 13.x), declaration (8.x), "
        "preprocessing (20.x, 21.x), memory rules, existing Polyspace findings. "
        "Use alpha=0.4 for semantic matching."
    ),
    "safety_validation": (
        "Safety validation per ISO 26262 (Domain Assistant: SAVA). "
        "Gather: safety requirements (ASIL-B/D), safety measures, AoU constraints, "
        "safety-relevant function properties from SWUD, DET/DEM error detection, "
        "safety-critical config constraints, trusted boundaries, safety manual sections, "
        "dual-point failure analysis for ASIL-D. "
        "Use alpha=0.8 for structural, alpha=0.3 for semantic."
    ),
    "safety_analysis": (
        "Safety analysis per ISO 26262 (Domain Assistant: SASA). "
        "Gather: functional safety concept, safety-relevant dependencies, "
        "dependent failure analysis, ASIL inheritance/decomposition chains, "
        "freedom from interference mechanisms, systematic failure mitigation, "
        "random HW failure mitigation, runtime error detection capabilities. "
        "Use alpha=0.7 mostly structural."
    ),
    "hazop_analysis": (
        "HAZOP analysis (Domain Assistant: HAZOPA). "
        "Gather: interface definitions (params, types, ranges), guide word analysis "
        "(no output, wrong output, late, early, stuck, oscillating), "
        "HW register failure modes, existing safety measures, threat analysis, "
        "security measures, previous HAZOP findings, boundary conditions. "
        "Use alpha=0.6 balanced."
    ),
    "data_flow_analysis": (
        "Data flow analysis (Domain Assistant: DaFaA). "
        "Gather: function call graphs, parameter data flow, shared global access patterns, "
        "critical section usage (SchM_Enter/Exit), DMA data flow paths, "
        "interrupt data flows, data type overflow/underflow paths, memory section assignments. "
        "Use alpha=0.8 for structural call graph traversal."
    ),
    "traceability": (
        "ISO 26262 traceability verification (Domain Assistant: TripleA). "
        "Gather: full V-Model chains (Req→Arch→Code→Test→Result), coverage gaps "
        "(missing_code, missing_test, missing_result, orphan_code, orphan_test), "
        "bidirectional traceability, HW-SW link analysis, cross-module traceability, "
        "ASIL-specific coverage. "
        "Use alpha=0.8 throughout — structural graph traversal."
    ),
    "debug_analysis": (
        "Embedded SW debug analysis on AURIX TC3xx. "
        "Gather: HW register specs, known errata, similar bug patterns, "
        "driver source code + callers, DMA/interrupt interactions, "
        "timing-sensitive paths (WCET, polling), affecting config params, VP test results. "
        "Use alpha=0.6 balanced."
    ),
    "knowledge_ingestion": (
        "Knowledge ingestion planning. "
        "Gather: current graph statistics, ontology profiles, parser capabilities, "
        "existing ingestion status, schema compliance, missing knowledge areas, "
        "cross-module dependency map. "
        "Use alpha=0.8 for structural graph metadata."
    ),
    "stop_typing": (
        "Quick lookup. "
        "Keep plan SHORT (2-3 sub-queries max): primary entity lookup, "
        "immediate context (params, return type, parent), one level of relationships. "
        "Do NOT over-decompose. Speed over comprehensiveness. "
        "Use alpha=0.5 balanced."
    ),
    "hsi_analysis": (
        "HSI (Hardware-Software Interface) analysis for AUTOSAR MCAL function. "
        "This produces SWUD-format HSI documentation. "
        "Step 1 (alpha=0.8): Query the function's SRC_ACCESSES_SFR relationships "
        "to get all SFR registers accessed, with access_type (READ/WRITE), field, line number. "
        "Step 2 (alpha=0.8): Query the function's SRC_USES_GLOBAL relationships "
        "to get all global/shared variables used, with access_type, via_chain, data_type. "
        "Step 3 (alpha=0.8): Query EA_Register nodes for trust zone data "
        "(read_apu, write_apu, cpu_mode) for each register found in step 1. "
        "Also query EA_Function -> EA_ACCESSES_REGISTER for additional register access info. "
        "IMPORTANT: Include the exact function name in every sub-query. "
        "Keep to 3 steps max — this is a structured extraction, not a broad search."
    ),
    "generic": "General automotive embedded software knowledge base query.",
}

_SYNTH_INSTRUCTIONS = {
    "requirement_review": (
        "Synthesise a structured requirement review report. "
        "For each requirement: ID, text, ASIL level, completeness assessment, "
        "testability assessment, ambiguity issues, traceability status, recommendations."
    ),
    "requirement_drafting": (
        "Synthesise a draft PRQ following the module's naming conventions and style. "
        "Include: requirement ID template, text, ASIL level, domain classification, "
        "verifiability assessment, traceability links to parent SHRQ."
    ),
    "requirement_management": (
        "Synthesise a requirement management report. Include: coverage matrix summary, "
        "gap list with severity, orphan artifacts, status distribution, prioritised action items."
    ),
    "architecture_analysis": (
        "Synthesise a SW architecture analysis. Include: architectural decision summary, "
        "component dependencies, call sequence descriptions, safety measure mapping, "
        "configuration hierarchy, identified risks or inconsistencies."
    ),
    "architecture_traceability": (
        "Synthesise an architecture traceability matrix. Columns: "
        "Requirement ID | Architecture Element | Implementation Function | Test Case | Gap Status."
    ),
    "code_generation": (
        "Synthesise production-ready AUTOSAR-compliant C code. "
        "Use exact TC3xx register names. Include: proper AUTOSAR API contracts, "
        "MISRA-compliant patterns, DET/DEM error handling, requirement traceability tags."
    ),
    "code_transformation": (
        "Synthesise the transformed code with before/after markers. "
        "Include: what changed and why, MISRA compliance, register-level changes, validation steps."
    ),
    "bugfix_analysis": (
        "Synthesise a structured bugfix report with concrete code fixes. "
        "For each warning/error, provide: "
        "(1) Warning ID and severity classification, "
        "(2) Affected code location (file, function, line), "
        "(3) Root cause analysis — why the warning is triggered, "
        "(4) Proposed fix as a minimal code diff (before/after), "
        "(5) MISRA C:2012 rule reference if applicable, "
        "(6) Side-effect assessment — does the fix affect callers, timing, or register access? "
        "Group fixes by priority: Bugfinder required > CodeProver Red > Compiler > Advisory. "
        "Include a summary count: total warnings, fixed, needs-review, cannot-fix-safely."
    ),
    "code_review": (
        "Synthesise a comprehensive code review report. Sections: "
        "MISRA violations (rule, severity, location, fix), AUTOSAR compliance, "
        "design deviations, error handling gaps, resource management, concurrency concerns, "
        "quality assessment with score."
    ),
    "config_generation": (
        "Synthesise AUTOSAR configuration output. Include: container hierarchy, "
        "parameter values with types/constraints, dependency notes, variant applicability, "
        "ARXML snippets where applicable."
    ),
    "page_generation": (
        "Synthesise structured documentation. Include: module overview, "
        "API reference (all public functions), usage sequences, configuration guide, "
        "error handling reference, safety notes, and cross-references."
    ),
    "test_generation": (
        "Synthesise complete test specification + C test function. "
        "Include: preconditions, steps with expected results, pass/fail criteria, "
        "requirement traceability tags, ASIL-appropriate coverage, "
        "AUTOSAR test patterns (setup→execute→verify→cleanup)."
    ),
    "test_verification": (
        "Synthesise a test verification report. Include: execution summary (pass/fail/skip), "
        "coverage metrics, requirement coverage %, WCET compliance, "
        "failed test root cause analysis, recommended additional tests."
    ),
    "test_quality_analysis": (
        "Synthesise a test quality analysis report. Include: coverage metrics "
        "(statement, branch, MC/DC), requirement coverage %, ASIL adequacy, "
        "redundancy assessment, orphan tests, improvement recommendations."
    ),
    "misra_review": (
        "Synthesise MISRA C:2012 violation report. "
        "For each violation: rule number, severity (mandatory/required/advisory), "
        "code location, explanation, compliant fix suggestion."
    ),
    "safety_validation": (
        "Synthesise a safety validation report per ISO 26262. Include: "
        "safety requirement coverage, safety measure status, AoU compliance, "
        "error detection adequacy, trusted boundary integrity, identified gaps."
    ),
    "safety_analysis": (
        "Synthesise a safety analysis report. Include: ASIL decomposition assessment, "
        "dependent failure analysis, freedom from interference evidence, "
        "systematic failure mitigation status, diagnostic coverage, remaining risks."
    ),
    "hazop_analysis": (
        "Synthesise a HAZOP worksheet. Columns: Interface | Guide Word | Deviation | "
        "Cause | Effect | Severity | Existing Safeguard | Recommended Action | Priority."
    ),
    "data_flow_analysis": (
        "Synthesise a data flow analysis report. Include: call graph summary, "
        "shared data access patterns, critical section analysis, DMA/interrupt flows, "
        "potential data races, recommendations."
    ),
    "traceability": (
        "Synthesise ISO 26262 traceability matrix. Columns: Req ID | ASIL | "
        "Architecture | Code (IMPLEMENTS) | Test (TRACES_TO) | Result (VERIFIES) | Gap Status. "
        "Include coverage percentages per ASIL level."
    ),
    "debug_analysis": (
        "Synthesise root cause analysis: most likely cause, evidence, "
        "register-level explanation, recommended fix, verification steps."
    ),
    "knowledge_ingestion": (
        "Synthesise an ingestion plan. Include: modules to ingest (priority order), "
        "estimated counts, parser requirements, dependency order, validation steps."
    ),
    "stop_typing": (
        "Synthesise a concise, focused answer. Keep it short and direct — "
        "quick lookup, not comprehensive analysis."
    ),
    "hsi_analysis": (
        "Synthesise the HSI (Hardware-Software Interface) section in SWUD format. "
        "You MUST produce THREE tables:\n"
        "1. **SFR Registers Accessed** table with columns: "
        "Register Name | Access Type (READ/WRITE) | Field | Line | Trust Zone | Description\n"
        "2. **Global/Shared Variables** table with columns: "
        "Variable Name | Access Type (READ/WRITE/READ_WRITE) | Data Type | Via Chain | Description\n"
        "3. **Events** table (or 'None' if no events)\n\n"
        "RULES:\n"
        "- For each register, append access type abbreviation: e.g. ADC_SUPLLEV(w) for WRITE\n"
        "- Trust zone: cite read_apu/write_apu values. PTOP = Untrusted, PCPU = Trusted\n"
        "- For global variables, show the via_chain if present (e.g. ConfigPtr->PartitionConfigPtr)\n"
        "- Group partition-specific variables (e.g. list all 7 Adc_kEcucPartition_X_ConfigPtr together)\n"
        "- Include access line numbers when available\n"
        "- Do NOT add registers or variables that are not in the sub-query data\n"
        "- Do NOT paraphrase — use exact names from the data"
    ),
    "generic": (
        "Synthesise a comprehensive, citation-rich answer from the gathered knowledge. "
        "CRITICAL: You MUST cite specific entity names, register names, variable names, "
        "property values, and relationship details exactly as they appear in the sub-query data. "
        "Do NOT paraphrase concrete data into vague categories. "
        "For SFR registers: cite the exact register name, access type (READ/WRITE), line number. "
        "For global variables: cite the exact variable name, data type, access_type, via_chain if present. "
        "For functions: cite parameters, return type, register_accesses, traceability IDs. "
        "For HSI details: cite trust zone (read_apu, write_apu), cpu_mode, device. "
        "Use markdown tables or structured lists for clarity. "
        "If a piece of data appears in the sub-query results, it MUST appear in your answer."
    ),
}


# ═════════════════════════════════════════════════════════════════════════
#  Complexity Routing Heuristic
# ═════════════════════════════════════════════════════════════════════════

REGISTER_KEYWORDS = {"register", "bitfield", "sfr", "clc", "krst", "globcon",
                     "hwreg", "peripheral", "dma", "interrupt", "isr",
                     "hsi", "trust zone", "trust_zone", "apu", "hardware-software"}
ASIL_KEYWORDS = {"asil-b", "asil-c", "asil-d", "safety-critical", "iso26262", "iso 26262"}
HSI_KEYWORDS = {"hsi", "trust zone", "hardware-software interface", "hardware software interface",
                "read_apu", "write_apu", "access type", "global variable"}


def should_use_rlm(query: str, task_type: str = "generic") -> bool:
    """Return True if the query is complex enough to warrant RLM."""
    query_lower = query.lower()
    signals = 0
    # Signal 1: 3+ function names mentioned
    fn_pattern = re.compile(r'Ifx\w+_\w+|[A-Z][a-z]+_[A-Z][a-z]+\w+')
    if len(fn_pattern.findall(query)) >= 3:
        signals += 1
    # Signal 2: Register-level keywords
    if any(kw in query_lower for kw in REGISTER_KEYWORDS):
        signals += 1
    # Signal 3: ASIL requirements
    if any(kw in query_lower for kw in ASIL_KEYWORDS):
        signals += 1
    # Always use RLM for certain task types (inherently complex)
    if task_type in ("traceability", "debug_analysis", "architecture_analysis", "hsi_analysis"):
        signals += 2
    return signals >= 2


# ═════════════════════════════════════════════════════════════════════════
#  RLM Orchestrator
# ═════════════════════════════════════════════════════════════════════════

class RLMOrchestrator:
    """
    Multi-step context builder using LLM-planned sub-queries.

    Parameters
    ----------
    module : str
        MCAL/iLLD module (e.g. "CAN", "CXPI").
    profile : str
        Workspace ("mcal" or "illd").
    search_fn : callable, optional
        Function(query, max_results, alpha, workspace_id) → list of results.
        If None, returns empty results (useful for plan-only preview).
    llm_fn : callable, optional
        Function(system_prompt, user_message) → str response.
        If None, uses GPT4IFX via OpenAI client.
    """

    def __init__(self, module: str = "CAN", profile: str = "mcal",
                 search_fn=None, llm_fn=None):
        self.module = module.upper()
        self.profile = profile
        self._search_fn = search_fn
        self._llm_fn = llm_fn or self._default_llm

    def _default_llm(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Call LLM via GPT4IFX OpenAI-compatible proxy (shared connection pool).

        F-CC-R01: retries on real OpenAI SDK transport errors (connection/timeout)
        and 5xx server errors, refreshes the token once on a 401, and increments
        ``aice_rlm_planner_fallbacks_total`` when all retries are exhausted.
        """
        try:
            from openai import (
                APIConnectionError, APITimeoutError, APIStatusError,
                AuthenticationError, RateLimitError, InternalServerError,
            )
            _retryable = (
                TimeoutError, ConnectionError,
                APIConnectionError, APITimeoutError, RateLimitError,
                InternalServerError,
            )
        except Exception:  # openai not importable / older SDK
            APIStatusError = AuthenticationError = ()  # type: ignore[assignment]
            _retryable = (TimeoutError, ConnectionError)

        def _should_retry(exc: BaseException) -> bool:
            if isinstance(exc, _retryable):
                return True
            # Retry server-side (5xx) APIStatusError; 401 handled inline below.
            if APIStatusError and isinstance(exc, APIStatusError):
                return getattr(exc, "status_code", 0) >= 500
            return False

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        )
        def _call():
            try:
                client = _get_shared_openai_client()
                model = os.environ.get("RLM_ROOT_MODEL", "gpt-4o")
                resp = client.chat.completions.create(
                    model=model, temperature=0.1, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
                return resp.choices[0].message.content or ""
            except BaseException as exc:
                # 401: token likely expired — drop the client to force a refresh
                # and let tenacity re-attempt with the new token.
                is_401 = (
                    isinstance(exc, AuthenticationError)
                    or (APIStatusError and isinstance(exc, APIStatusError)
                        and getattr(exc, "status_code", 0) == 401)
                )
                if is_401:
                    logger.warning("[RLM] LLM 401 — refreshing token and retrying")
                    _reset_shared_openai_client()
                    raise ConnectionError("token refresh required") from exc
                raise
        try:
            return _call()
        except Exception as e:
            logger.error("[RLM] LLM call failed after retries: %s", e)
            try:
                from src.Observability.metrics import RLM_PLANNER_FALLBACKS
                RLM_PLANNER_FALLBACKS.labels(reason="exhausted").inc()
            except Exception:
                pass
            # F-CC-R02/R03: return a non-JSON sentinel so the planner falls back
            # using the full original query (see _plan) instead of a truncated
            # slice of the wrapped prompt, and synthesis surfaces a clear marker.
            return _LLM_UNAVAILABLE_SENTINEL

    # ── Public API ─────────────────────────────────────────────────────

    def run(self, query: str, task_type: str = "generic",
            session_context: Optional[List] = None,
            on_progress: Optional[callable] = None) -> RLMContext:
        """Execute multi-step context assembly.

        Parameters
        ----------
        on_progress : callable, optional
            Callback ``(step_index: int, total_steps: int, message: str) -> None``
            invoked after planning and after each sub-query completes.
        """
        t0 = time.time()
        tt = task_type if task_type in [e.value for e in RLMTaskType] else "generic"
        total_tokens = 0

        # Step 1: Plan
        plan, plan_tokens = self._plan(query, tt)
        total_tokens += plan_tokens
        steps = plan.get("steps", [])[:MAX_STEPS]
        total_steps = len(steps) + 2  # plan + N sub-queries + synthesize
        logger.info("[RLM] Plan: %d steps for task '%s'", len(steps), tt)
        if on_progress:
            on_progress(1, total_steps, f"Planned {len(steps)} sub-queries")

        # Step 2: Execute sub-queries
        sub_results: List[SubQueryStep] = []
        accumulated: Dict[int, str] = {}

        for i, step_data in enumerate(steps):
            sq = self._execute_step(step_data, accumulated)
            sub_results.append(sq)
            accumulated[sq.step_id] = sq.answer
            total_tokens += sq.tokens
            if on_progress:
                on_progress(i + 2, total_steps, f"Sub-query {i+1}/{len(steps)} done")

        # Step 3: Synthesize
        final, synth_tokens, degraded = self._synthesize(query, tt, accumulated, session_context)
        total_tokens += synth_tokens
        if on_progress:
            on_progress(total_steps, total_steps, "Synthesis complete")

        elapsed = time.time() - t0
        logger.info("[RLM] Complete — %d sub-queries, %d tokens, %.1fs", len(sub_results), total_tokens, elapsed)

        return RLMContext(
            assembled_context=final,
            sub_query_trace=sub_results,
            total_tokens=total_tokens,
            total_elapsed_s=elapsed,
            plan=steps,
            module=self.module,
            profile=self.profile,
            task_type=tt,
            degraded=degraded,
        )

    def plan_preview(self, query: str, task_type: str = "generic") -> Dict[str, Any]:
        """Preview plan without executing sub-queries."""
        plan, tokens = self._plan(query, task_type)
        return {"plan": plan, "step_count": len(plan.get("steps", [])), "tokens": tokens}

    # ── Planning ───────────────────────────────────────────────────────

    def _plan(self, query: str, task_type: str) -> tuple:
        context_desc = _PLAN_CONTEXT.get(task_type, _PLAN_CONTEXT["generic"])
        system = f"You are the RLM context planner for {context_desc}\n{_BASE_PLAN_INSTRUCTION.format(max_steps=MAX_STEPS)}"
        user = f"Module: {self.module} | Profile: {self.profile}\n\nTask: {query}"

        raw = self._llm_fn(system, user, max_tokens=1200)
        tokens = estimate_tokens(raw)

        # F-CC-R03: brace-counting extraction tolerates trailing prose / fences.
        plan = _extract_json_object(raw)
        if not isinstance(plan, dict) or "steps" not in plan:
            logger.warning(
                "[RLM] Planner returned no parseable plan; falling back. Raw: %r",
                raw[:300],
            )
            plan = {"reasoning": "fallback", "steps": [
                {"step_id": 1, "intent": "direct query", "query": query, "alpha": _get_default_alpha()}
            ]}

        return plan, tokens

    # ── Sub-query Execution ────────────────────────────────────────────

    def _execute_step(self, step_data: Dict, accumulated: Dict[int, str]) -> SubQueryStep:
        t0 = time.time()
        step_id = step_data.get("step_id", 1)
        intent = step_data.get("intent", "")
        query = step_data.get("query", "")
        alpha = step_data.get("alpha", _get_default_alpha())

        # Search
        sources_n = 0
        answer = ""
        if self._search_fn:
            try:
                try:
                    # Intermediate RLM steps should avoid expensive LLM-as-judge.
                    # We keep judging for non-RLM final contexts.
                    search_kwargs = dict(query=query, max_results=20,
                                         alpha=alpha, workspace_id=self.profile,
                                         skip_judge=True)
                    if self.module:
                        search_kwargs["filter_by_module"] = self.module
                    results = self._search_fn(**search_kwargs)
                except TypeError:
                    # Backward compatibility for search_fn implementations
                    # that do not accept the new parameters yet.
                    search_kwargs = dict(query=query, max_results=10,
                                         alpha=alpha, workspace_id=self.profile)
                    if self.module:
                        search_kwargs["filter_by_module"] = self.module
                    results = self._search_fn(**search_kwargs)
                sources = results if isinstance(results, list) else results.get("results", [])
                sources_n = len(sources)

                # Slot-based context assembly via ContextBuilder
                budget = ContextBudget(total_budget=SUB_BUDGET)
                builder = ContextBuilder(budget=budget)
                candidates: List[ContextItem] = []

                for r in sources[:20]:
                    content = r.get("content", r.get("text", str(r.get("properties", ""))))
                    node_type = r.get("node_type", "")
                    src = Source(
                        origin=r.get("source", "unknown"),
                        score=r.get("score", 0.0),
                        heading=r.get("node_id", ""),
                        text=content,
                        node_label=node_type,
                        metadata=r.get("properties", {}),
                    )
                    slot = classify_source(src)
                    candidates.append(ContextItem(
                        slot=slot,
                        content=content,
                        relevance_score=r.get("score", 0.0),
                        source=f"{r.get('source', 'unknown')}:{node_type}",
                        entity_id=r.get("node_id", ""),
                    ))

                assembled = builder.build(candidates, max_tokens=SUB_BUDGET)
                answer = ContextBuilder.render(assembled)
                logger.debug(
                    "[RLM] Step %d: %d items in %d tokens (dropped %d)",
                    step_id, assembled.items_included,
                    assembled.total_tokens, assembled.items_dropped,
                )
            except Exception as e:
                logger.warning("[RLM] Step %d search failed: %s", step_id, e)
                answer = f"[Search failed: {e}]"
        else:
            answer = f"[No search function — step {step_id}: {intent}]"

        tokens = estimate_tokens(answer)
        elapsed = time.time() - t0

        return SubQueryStep(
            step_id=step_id, intent=intent, query=query, alpha=alpha,
            answer=answer, sources_n=sources_n, tokens=tokens, elapsed_s=elapsed,
        )

    # ── Synthesis ──────────────────────────────────────────────────────

    def _synthesize(self, query: str, task_type: str,
                    accumulated: Dict[int, str],
                    session_context: Optional[List]) -> tuple:
        instruction = _SYNTH_INSTRUCTIONS.get(task_type, _SYNTH_INSTRUCTIONS["generic"])

        # F-CC-R04: model-aware token budget instead of a hard-coded per-answer
        # char cap. Sub-query answers are admitted until the model context window
        # (minus reserved response + scaffolding headroom) is exhausted.
        model = os.environ.get("RLM_ROOT_MODEL", "gpt-4o")
        max_ctx = _MODEL_CONTEXT_TOKENS.get(model, _DEFAULT_MODEL_CONTEXT_TOKENS)
        system = f"You are a synthesis engine. {instruction}"

        header = f"Original task: {query}\n"
        parts = [header]
        used = estimate_tokens(system) + estimate_tokens(header)
        if session_context:
            ctx_str = f"Session context: {json.dumps(session_context[:5], default=str)[:500]}\n"
            parts.append(ctx_str)
            used += estimate_tokens(ctx_str)

        # Reserve headroom for the response and prompt scaffolding.
        budget = max_ctx - _SYNTH_RESPONSE_TOKENS - 1000
        for step_id, answer in sorted(accumulated.items()):
            block = f"--- Sub-query {step_id} ---\n{answer}\n"
            block_tokens = estimate_tokens(block)
            if used + block_tokens > budget:
                remaining = budget - used
                if remaining > 200:
                    approx_chars = max(0, remaining * 4)
                    parts.append(
                        f"--- Sub-query {step_id} (truncated) ---\n{answer[:approx_chars]}\n"
                    )
                logger.info(
                    "[RLM] Synthesis token budget (%d) reached at sub-query %d",
                    budget, step_id,
                )
                break
            parts.append(block)
            used += block_tokens

        user = "\n".join(parts)

        # F-CC-R02: surface synthesis degradation instead of passing the LLM
        # failure sentinel off as real content. Handles both the default LLM
        # sentinel and arbitrary llm_fn exceptions; callers see degraded=True.
        try:
            final = self._llm_fn(system, user, max_tokens=_SYNTH_RESPONSE_TOKENS)
        except Exception as exc:
            logger.error("[RLM] Synthesis LLM call failed: %s", exc)
            return "[LLM synthesis unavailable]", 0, True
        if not final or final.strip() == _LLM_UNAVAILABLE_SENTINEL:
            logger.warning("[RLM] Synthesis degraded: LLM unavailable")
            return "[LLM synthesis unavailable]", 0, True
        tokens = estimate_tokens(final)
        return final, tokens, False

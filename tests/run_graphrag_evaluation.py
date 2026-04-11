#!/usr/bin/env python3
"""
GraphRAG Evaluation Runner
===========================
Runs the 4 evaluation queries from graphrag_evaluation_queries.md against
the live GraphRAG system, scores them via keyword/entity matching against
ground truth, and prints a final metrics report.

Usage:
    python tests/run_graphrag_evaluation.py
    python tests/run_graphrag_evaluation.py --no-llm        # skip LLM, context-only
    python tests/run_graphrag_evaluation.py --alpha 0.7      # heavier graph weight
    python tests/run_graphrag_evaluation.py --json           # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — make the querier importable
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent              # .../tests
REPO_ROOT = SCRIPT_DIR.parent                             # repo root
CODE_DIR = REPO_ROOT / "src" / "HybridRAG" / "code"
QUERIER_DIR = CODE_DIR / "querier"

for p in (str(CODE_DIR), str(QUERIER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════════════
#  Ground-truth definitions for auto-scoring
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GroundTruthCheck:
    """A single verifiable fact to look for in the LLM answer."""
    description: str
    keywords: list[str]          # ALL must appear (case-insensitive)
    weight: float = 1.0          # relative importance


@dataclass
class EvalQuery:
    """One evaluation query with its ground-truth checklist."""
    id: int
    title: str
    query: str
    checks: list[GroundTruthCheck]
    alpha: float = 0.6           # recommended alpha for this query


# ---------------------------------------------------------------------------
#  Query 1 — End-to-End Traceability Chain (Wakeup Feature)
# ---------------------------------------------------------------------------
Q1 = EvalQuery(
    id=1,
    title="End-to-End Traceability Chain (Wakeup Feature)",
    alpha=0.7,
    query=(
        "Trace the full lifecycle of the Port wakeup feature from architecture "
        "through design to test verification. Specifically:\n"
        "1. Which SWA architectural decision defines the wakeup status interface, "
        "and what was the rationale for choosing PortId + PinNum as inputs over PinId alone?\n"
        "2. How does the SWUD function Port_GetWakeUpStatus implement the wakeup re-enable "
        "mechanism, and which HW registers does it access?\n"
        "3. What safety Assumption of Use (AoU) applies to the wakeup status, and why is the "
        "wakeup signal considered untrusted?\n"
        "4. Which product requirements (AU3GM-PRQ-*) tie the SWA decision, the SWUD "
        "implementation, and the safety AoU together?"
    ),
    checks=[
        # Sub-question 1: SWA Decision
        GroundTruthCheck("SWA decision title mentions wakeup",
                         ["wakeup status", "pin"], weight=1.0),
        GroundTruthCheck("Mentions PortId + PinNum alternative",
                         ["portid", "pinnum"], weight=1.0),
        GroundTruthCheck("Mentions rationale / integrator",
                         ["integrator"], weight=0.5),

        # Sub-question 2: SWUD Port_GetWakeUpStatus
        GroundTruthCheck("Mentions Port_GetWakeUpStatus function",
                         ["port_getwakeupstatus"], weight=1.0),
        GroundTruthCheck("Mentions P_WKEN register",
                         ["p_wken"], weight=1.0),
        GroundTruthCheck("Mentions P_WKSTS register",
                         ["p_wksts"], weight=1.0),
        GroundTruthCheck("Mentions PORT_WAKEUP_REENABLE",
                         ["wakeup_reenable"], weight=0.5),
        GroundTruthCheck("Return values TRIGGERED / NOT_TRIGGERED",
                         ["wakeup_triggered"], weight=0.5),
        GroundTruthCheck("Service ID 0x06",
                         ["0x06"], weight=0.5),

        # Sub-question 3: Safety AoU
        GroundTruthCheck("AoU about untrusted wakeup",
                         ["untrusted"], weight=1.0),
        GroundTruthCheck("Authenticate wakeup status",
                         ["authenticate"], weight=0.5),

        # Sub-question 4: PRQ unification
        GroundTruthCheck("PRQ-40811 is the unifying requirement",
                         ["40811"], weight=1.5),
        GroundTruthCheck("Additional PRQs (29892 or 29893 or 37898)",
                         ["29892"], weight=0.5),

        # Traceability completeness
        GroundTruthCheck("ASIL D classification",
                         ["asil", "d"], weight=0.5),
    ],
)

# ---------------------------------------------------------------------------
#  Query 2 — Cross-Document Error Handling (Port_SetPinDirection)
# ---------------------------------------------------------------------------
Q2 = EvalQuery(
    id=2,
    title="Cross-Document Error Handling (Port_SetPinDirection)",
    alpha=0.6,
    query=(
        "For the Port_SetPinDirection API:\n"
        "1. List every development and safety error code it can raise, including the hex "
        "value, the triggering condition, and the PRQ that mandates each error.\n"
        "2. Describe the critical-section behavior when changing pin direction from Input to "
        "Output — specifically, how many critical section enter/exit pairs are needed, and why "
        "does it differ from the Output-to-Input case?\n"
        "3. Which SWA safety measure protects against invalid PortId, and how does the SWUD "
        "error-checking algorithm implement that protection?\n"
        "4. What is the maximum allowed execution time for Port_SetPinDirection, and under "
        "what operating conditions does that constraint apply?"
    ),
    checks=[
        # Error codes
        GroundTruthCheck("PORT_E_PARAM_PIN with hex 0x0A",
                         ["port_e_param_pin", "0x0a"], weight=1.0),
        GroundTruthCheck("PORT_E_DIRECTION_UNCHANGEABLE with hex 0x0B",
                         ["port_e_direction_unchangeable", "0x0b"], weight=1.0),
        GroundTruthCheck("PORT_E_UNINIT with hex 0x0F",
                         ["port_e_uninit", "0x0f"], weight=1.0),
        GroundTruthCheck("PORT_E_UNAUTH_PARTITION with hex 0x97",
                         ["port_e_unauth_partition"], weight=1.0),
        GroundTruthCheck("PORT_E_PARAM_INVALID_DIRECTION 0x64",
                         ["port_e_param_invalid_direction"], weight=0.5),
        GroundTruthCheck("PORT_E_PARAM_INVALID_CURRENT_MODE 0x65",
                         ["port_e_param_invalid_current_mode"], weight=0.5),

        # Critical section behavior
        GroundTruthCheck("Mentions critical section enter/exit",
                         ["critical section"], weight=1.0),
        GroundTruthCheck("Input-to-Output vs Output-to-Input asymmetry",
                         ["input", "output"], weight=0.5),
        GroundTruthCheck("SchM_Enter/Exit for protection",
                         ["schm"], weight=0.5),
        GroundTruthCheck("GPIO mode forced on Input→Output",
                         ["gpio"], weight=0.5),

        # Safety measure
        GroundTruthCheck("Parameter range check for Port Identifier",
                         ["parameter range check"], weight=1.0),

        # Timing constraint
        GroundTruthCheck("Max execution time 1500 ns",
                         ["1500"], weight=1.0),
        GroundTruthCheck("CPU/SRI 400 MHz operating condition",
                         ["400"], weight=0.5),

        # PRQ references
        GroundTruthCheck("PRQ-31484 for PORT_E_PARAM_PIN",
                         ["31484"], weight=0.5),
        GroundTruthCheck("PRQ-29873 or PRQ-30140 for critical sections",
                         ["29873"], weight=0.5),
    ],
)

# ---------------------------------------------------------------------------
#  Query 3 — Configuration Traceability (SWA Config → SWUD Macros)
# ---------------------------------------------------------------------------
Q3 = EvalQuery(
    id=3,
    title="Configuration Traceability (SWA Config → SWUD Macros)",
    alpha=0.6,
    query=(
        "Explain the full configuration chain for enabling/disabling Port APIs at "
        "compile time:\n"
        "1. For each of the following SWA configuration parameters, identify the "
        "corresponding SWUD derived macro, its default value, and the PRQ that mandates "
        "the parameter:\n"
        "   - PortSetPinDirectionApi\n"
        "   - PortSetPinModeApi\n"
        "   - PortSetPinCharacteristicsApi\n"
        "   - PortInitCheckApi\n"
        "   - PortGetWakeUpStatusApi\n"
        "   - PortDevErrorDetect\n"
        "   - PortSafetyErrorDetect\n"
        "2. Which SWA configuration parameters map to BOTH PORT_MCAL_SUPERVISOR and "
        "PORT_MCAL_USER1 in the SWUD, and how does the PortInitApiMode vs "
        "PortRuntimeApiMode distinction affect supervisor/user mode SFR access?\n"
        "3. How does the SWUD derive PORT_NO_OF_PARTITIONS from the SWA "
        "PortEcucPartitionRef, and what is the traceability chain between them?"
    ),
    checks=[
        # Config parameter → macro mappings
        GroundTruthCheck("PortSetPinDirectionApi → PORT_SET_PIN_DIRECTION_API",
                         ["portsetpindirectionapi", "port_set_pin_direction_api"], weight=1.0),
        GroundTruthCheck("PortSetPinModeApi → PORT_SET_PIN_MODE_API",
                         ["portsetpinmodeapi", "port_set_pin_mode_api"], weight=1.0),
        GroundTruthCheck("PortSetPinCharacteristicsApi mapping",
                         ["port_set_pin_characteristics_api"], weight=1.0),
        GroundTruthCheck("PortInitCheckApi → PORT_INIT_CHECK_API",
                         ["port_init_check_api"], weight=1.0),
        GroundTruthCheck("PortGetWakeUpStatusApi → PORT_GET_WAKEUP_STATUS_API",
                         ["port_get_wakeup_status_api"], weight=1.0),
        GroundTruthCheck("PortDevErrorDetect → PORT_DEV_ERR_CHECK or REPORTING",
                         ["port_dev_err"], weight=1.0),
        GroundTruthCheck("PortSafetyErrorDetect → PORT_SAFETY_ERR_REPORTING",
                         ["port_safety_err"], weight=1.0),

        # Default values
        GroundTruthCheck("Mentions default value TRUE",
                         ["true"], weight=0.5),

        # Supervisor / User mode
        GroundTruthCheck("PORT_MCAL_SUPERVISOR mentioned",
                         ["port_mcal_supervisor"], weight=1.0),
        GroundTruthCheck("PORT_MCAL_USER1 mentioned",
                         ["port_mcal_user1"], weight=1.0),
        GroundTruthCheck("PortInitApiMode vs PortRuntimeApiMode distinction",
                         ["portinit"], weight=0.5),

        # Partition derivation
        GroundTruthCheck("PORT_NO_OF_PARTITIONS derived from PortEcucPartitionRef",
                         ["port_no_of_partitions"], weight=1.0),

        # PRQ references
        GroundTruthCheck("PRQ-31521 for SetPinDirection API",
                         ["31521"], weight=0.5),
        GroundTruthCheck("PRQ-31539 for SetPinMode API",
                         ["31539"], weight=0.5),
    ],
)

# ---------------------------------------------------------------------------
#  Query 4 — Safety, ASIL, and Completeness Verification
# ---------------------------------------------------------------------------
Q4 = EvalQuery(
    id=4,
    title="Safety, ASIL, and Completeness Verification",
    alpha=0.7,
    query=(
        "Evaluate the safety completeness of the Port module:\n"
        "1. List all Assumptions of Use (AoU) defined in the SWA safety view and "
        "trusted view, including their featureIDs, the PRQs they trace to, and the "
        "exact rationale text.\n"
        "2. All Port APIs are classified as ASIL D. The SWA states that certain ASIL B "
        "PRQs are 'not applicable' because 'Port module is ASIL D driver.' List at least "
        "5 specific AU3GM-PRQ IDs that are marked inapplicable for this reason.\n"
        "3. What are the memory constraints for the Port module (RAM, Code ROM, Data ROM, "
        "Stack, CSA blocks), and which SWA featureID defines them?\n"
        "4. For Port_Init, what is the complete sequence of safety-relevant checks: from "
        "NULL pointer validation of ConfigPtr through partition authorization to the final "
        "PORT_E_INIT_FAILED error — citing the specific PRQs for each step?"
    ),
    checks=[
        # AoUs
        GroundTruthCheck("AoU: PROT state transition protection",
                         ["prot", "state transition"], weight=1.0),
        GroundTruthCheck("AoU: Generated symbolic names",
                         ["symbolic name"], weight=1.0),
        GroundTruthCheck("AoU: Group access protection",
                         ["group access protection"], weight=1.0),
        GroundTruthCheck("AoU: Authenticate wakeup status",
                         ["authenticate", "wakeup"], weight=1.0),
        GroundTruthCheck("PRQ-31525 for PROT state transition",
                         ["31525"], weight=0.5),
        GroundTruthCheck("PRQ-31498 for symbolic names",
                         ["31498"], weight=0.5),

        # Inapplicable ASIL B PRQs
        GroundTruthCheck("'Port module is ASIL D driver' rationale",
                         ["asil d"], weight=1.0),
        GroundTruthCheck("At least one inapplicable PRQ ID (41907 or 40367)",
                         ["41907"], weight=0.5),
        GroundTruthCheck("Another inapplicable PRQ (40363 or 40365)",
                         ["40363"], weight=0.5),

        # Memory constraints
        GroundTruthCheck("RAM < 40 Bytes",
                         ["40"], weight=1.0),
        GroundTruthCheck("Code ROM < 4 KB",
                         ["4 kb"], weight=0.5),
        GroundTruthCheck("Stack < 32 Bytes per API",
                         ["32"], weight=0.5),

        # Port_Init safety sequence
        GroundTruthCheck("ConfigPtr NULL check → PORT_E_INCORRECT_CFG_POINTER",
                         ["port_e_incorrect_cfg_pointer"], weight=1.0),
        GroundTruthCheck("Partition authorization → PORT_E_UNAUTH_PARTITION",
                         ["port_e_unauth_partition"], weight=1.0),
        GroundTruthCheck("PORT_E_INIT_FAILED error",
                         ["port_e_init_failed"], weight=1.0),

        # PRQs for Port_Init
        GroundTruthCheck("PRQ-40353 for NULL pointer check",
                         ["40353"], weight=0.5),
        GroundTruthCheck("PRQ-31486 for init failed",
                         ["31486"], weight=0.5),
    ],
)

ALL_QUERIES = [Q1, Q2, Q3, Q4]


# ═══════════════════════════════════════════════════════════════════════════
#  Auto-scorer
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CheckResult:
    """Result of evaluating one ground-truth check against an answer."""
    check: GroundTruthCheck
    found: bool
    match_details: str = ""


@dataclass
class QueryScore:
    """Scored result for a single evaluation query."""
    query_id: int
    query_title: str
    check_results: list[CheckResult]
    elapsed_seconds: float
    token_usage: dict = field(default_factory=dict)
    context_stats: dict = field(default_factory=dict)
    source_count: int = 0
    answer_length: int = 0
    answer_preview: str = ""

    @property
    def total_weight(self) -> float:
        return sum(cr.check.weight for cr in self.check_results)

    @property
    def achieved_weight(self) -> float:
        return sum(cr.check.weight for cr in self.check_results if cr.found)

    @property
    def score_pct(self) -> float:
        tw = self.total_weight
        return (self.achieved_weight / tw * 100) if tw > 0 else 0.0

    @property
    def passed(self) -> int:
        return sum(1 for cr in self.check_results if cr.found)

    @property
    def failed(self) -> int:
        return sum(1 for cr in self.check_results if not cr.found)


def score_answer(answer: str, checks: list[GroundTruthCheck]) -> list[CheckResult]:
    """Check each ground-truth item against the LLM answer."""
    answer_lower = answer.lower()
    results = []
    for check in checks:
        all_found = all(kw.lower() in answer_lower for kw in check.keywords)
        match_info = ""
        if not all_found:
            missing = [kw for kw in check.keywords if kw.lower() not in answer_lower]
            match_info = f"missing: {missing}"
        results.append(CheckResult(check=check, found=all_found, match_details=match_info))
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════════════

def run_evaluation(
    module: str = "PORT",
    profile: str = "mcal",
    base_alpha: float = 0.6,
    neo4j_enabled: bool = True,
    llm_enabled: bool = True,
    context_budget: int = 16000,
    top_k: int = 25,
) -> list[QueryScore]:
    """Run all evaluation queries and return scored results."""
    from graphrag_query import GraphRAGQuerier

    scores: list[QueryScore] = []

    for eq in ALL_QUERIES:
        alpha = eq.alpha if eq.alpha else base_alpha
        print(f"\n{'='*70}")
        print(f"  Running Query {eq.id}: {eq.title}")
        print(f"  alpha={alpha}  module={module}  neo4j={neo4j_enabled}  llm={llm_enabled}")
        print(f"{'='*70}")

        querier = GraphRAGQuerier(
            module=module,
            profile=profile,
            alpha=alpha,
            neo4j_enabled=neo4j_enabled,
            llm_enabled=llm_enabled,
            context_budget=context_budget,
        )

        t0 = time.time()
        result = querier.query(eq.query, top_k=top_k)
        elapsed = time.time() - t0

        # For context-only mode, score against the assembled context
        text_to_score = result.answer if result.answer else result.context

        check_results = score_answer(text_to_score, eq.checks)

        qs = QueryScore(
            query_id=eq.id,
            query_title=eq.title,
            check_results=check_results,
            elapsed_seconds=elapsed,
            token_usage=result.token_usage,
            context_stats=result.context_stats,
            source_count=len(result.sources),
            answer_length=len(text_to_score),
            answer_preview=text_to_score[:500] if text_to_score else "(no answer)",
        )
        scores.append(qs)

        # Print per-query progress
        print(f"\n  Score: {qs.achieved_weight:.1f}/{qs.total_weight:.1f} "
              f"({qs.score_pct:.0f}%)  |  {qs.passed} passed, {qs.failed} failed  "
              f"|  {elapsed:.1f}s  |  {qs.source_count} sources")

    return scores


# ═══════════════════════════════════════════════════════════════════════════
#  Report
# ═══════════════════════════════════════════════════════════════════════════

def print_report(scores: list[QueryScore], json_output: bool = False) -> None:
    """Print the final evaluation report."""
    if json_output:
        report = {
            "queries": [],
            "summary": {},
        }
        for qs in scores:
            report["queries"].append({
                "id": qs.query_id,
                "title": qs.query_title,
                "score_pct": round(qs.score_pct, 1),
                "passed": qs.passed,
                "failed": qs.failed,
                "total_weight": qs.total_weight,
                "achieved_weight": round(qs.achieved_weight, 1),
                "elapsed_seconds": round(qs.elapsed_seconds, 1),
                "source_count": qs.source_count,
                "token_usage": qs.token_usage,
                "context_stats": qs.context_stats,
                "answer_length": qs.answer_length,
                "checks": [
                    {
                        "description": cr.check.description,
                        "found": cr.found,
                        "weight": cr.check.weight,
                        "details": cr.match_details,
                    }
                    for cr in qs.check_results
                ],
            })

        total_weight = sum(qs.total_weight for qs in scores)
        total_achieved = sum(qs.achieved_weight for qs in scores)
        report["summary"] = {
            "overall_score_pct": round(total_achieved / total_weight * 100, 1) if total_weight else 0,
            "total_achieved": round(total_achieved, 1),
            "total_weight": round(total_weight, 1),
            "total_elapsed_seconds": round(sum(qs.elapsed_seconds for qs in scores), 1),
            "queries_run": len(scores),
        }
        print(json.dumps(report, indent=2))
        return

    # ── Human-readable report ─────────────────────────────────────────

    print("\n" + "=" * 74)
    print("  GRAPHRAG EVALUATION REPORT")
    print("=" * 74)

    total_weight = 0.0
    total_achieved = 0.0
    total_passed = 0
    total_failed = 0

    for qs in scores:
        total_weight += qs.total_weight
        total_achieved += qs.achieved_weight
        total_passed += qs.passed
        total_failed += qs.failed

        bar_len = 30
        filled = int(bar_len * qs.score_pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        print(f"\n  Q{qs.query_id}: {qs.query_title}")
        print(f"  Score: {qs.achieved_weight:.1f}/{qs.total_weight:.1f}  "
              f"({qs.score_pct:.0f}%)  [{bar}]")
        print(f"  Checks: {qs.passed} passed / {qs.failed} failed  "
              f"|  {qs.elapsed_seconds:.1f}s  |  {qs.source_count} sources  "
              f"|  {qs.answer_length} chars")

        if qs.token_usage:
            tu = qs.token_usage
            print(f"  Tokens: prompt={tu.get('prompt', '?')}, "
                  f"completion={tu.get('completion', '?')}, "
                  f"total={tu.get('total', '?')}")

        # Show failures
        failures = [cr for cr in qs.check_results if not cr.found]
        if failures:
            print(f"  ── Missing facts ({len(failures)}):")
            for cr in failures:
                print(f"     ✗ {cr.check.description}  (w={cr.check.weight})  "
                      f"{cr.match_details}")

    # ── Summary ───────────────────────────────────────────────────────
    overall_pct = (total_achieved / total_weight * 100) if total_weight > 0 else 0
    total_time = sum(qs.elapsed_seconds for qs in scores)

    filled = int(30 * overall_pct / 100)
    bar = "█" * filled + "░" * (30 - filled)

    print(f"\n{'='*74}")
    print(f"  OVERALL SCORE: {total_achieved:.1f}/{total_weight:.1f}  "
          f"({overall_pct:.0f}%)  [{bar}]")
    print(f"  Checks: {total_passed} passed / {total_failed} failed")
    print(f"  Total time: {total_time:.1f}s")

    if overall_pct >= 90:
        grade = "EXCELLENT — System rivals expert human review"
    elif overall_pct >= 70:
        grade = "GOOD — System provides usable, mostly-correct answers"
    elif overall_pct >= 50:
        grade = "FAIR — System needs improvement in retrieval/reasoning"
    else:
        grade = "POOR — Significant gaps in retrieval or knowledge fusion"

    print(f"  Grade: {grade}")
    print(f"{'='*74}\n")


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="GraphRAG Evaluation Runner")
    ap.add_argument("--module", default="PORT", help="MCAL module (default: PORT)")
    ap.add_argument("--profile", default="mcal", help="Ontology profile (default: mcal)")
    ap.add_argument("--alpha", type=float, default=0.6, help="Base alpha (default: 0.6)")
    ap.add_argument("--top-k", type=int, default=25, help="Top-k sources (default: 25)")
    ap.add_argument("--context-budget", type=int, default=16000, help="Token budget")
    ap.add_argument("--no-neo4j", action="store_true", help="Disable Neo4j")
    ap.add_argument("--no-llm", action="store_true", help="Skip LLM, score context only")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    scores = run_evaluation(
        module=args.module,
        profile=args.profile,
        base_alpha=args.alpha,
        neo4j_enabled=not args.no_neo4j,
        llm_enabled=not args.no_llm,
        context_budget=args.context_budget,
        top_k=args.top_k,
    )

    print_report(scores, json_output=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())

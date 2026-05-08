"""
Module Release Readiness Score
===============================
Composite score per module that no single ALM tool can compute — each row in
the scorecard requires data from 2+ artifact silos.

Grade scale:
  A (90-100)  — Release ready
  B (75-89)   — Minor gaps
  C (60-74)   — Significant gaps
  D (40-59)   — Major gaps
  F (<40)     — Not ready
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from typing import Dict, List, Tuple

from db import run_cypher, get_module_list


# ─── Individual readiness checks ────────────────────────────────────────────
# Each returns (score 0–100, numerator, denominator, detail_rows)

def _req_implementation_coverage(module: str, profile: str) -> Tuple[float, int, int, List[Dict]]:
    """% of requirements that have at least one implementing function.
    Sources: Polarion/Jama (requirements) + EA/Source (functions)."""
    rows = run_cypher(
        "MATCH (r) "
        "WHERE any(l IN labels(r) WHERE l IN "
        "    ['ProductRequirement', 'SoftwareRequirement', 'StakeholderRequirement']) "
        "  AND toLower(coalesce(r.module, '')) = toLower($mod) "
        "OPTIONAL MATCH (f)-[:ARCHITECTURALLY_REALIZES|SWUD_TRACES_TO|SWA_CONFIG_TRACES_TO]->(r) "
        "RETURN r.requirement_id AS req_id, r.name AS req_name, "
        "       count(f) AS impl_count",
        {"mod": module}, profile,
    )
    # Fallback: check via BELONGS_TO_MODULE
    if not rows:
        rows = run_cypher(
            "MATCH (r)-[:BELONGS_TO_MODULE]->(m:MCALModule {module_name: $mod}) "
            "WHERE any(l IN labels(r) WHERE l IN "
            "    ['ProductRequirement', 'SoftwareRequirement', 'StakeholderRequirement']) "
            "OPTIONAL MATCH (f)-[:ARCHITECTURALLY_REALIZES|SWUD_TRACES_TO|SWA_CONFIG_TRACES_TO]->(r) "
            "RETURN r.requirement_id AS req_id, r.name AS req_name, "
            "       count(f) AS impl_count",
            {"mod": module}, profile,
        )

    total = len(rows)
    if total == 0:
        return 0.0, 0, 0, []
    covered = sum(1 for r in rows if r["impl_count"] > 0)
    gaps = [r for r in rows if r["impl_count"] == 0]
    score = (covered / total) * 100 if total > 0 else 0
    return score, covered, total, gaps


def _func_design_coverage(module: str, profile: str) -> Tuple[float, int, int, List[Dict]]:
    """% of SRC functions linked to SWA or SWUD design.
    Sources: Git/Source + EA (architecture model)."""
    rows = run_cypher(
        "MATCH (f:SRC_Function) "
        "WHERE toLower(coalesce(f.module, '')) = toLower($mod) "
        "OPTIONAL MATCH (f)-[:SRC_IMPLEMENTS_SWA|SRC_IMPLEMENTS_SWUD]->(d) "
        "RETURN f.function_name AS fn, count(d) AS design_count",
        {"mod": module}, profile,
    )
    total = len(rows)
    if total == 0:
        return 0.0, 0, 0, []
    covered = sum(1 for r in rows if r["design_count"] > 0)
    gaps = [{"function_name": r["fn"], "issue": "No design link"}
            for r in rows if r["design_count"] == 0]
    score = (covered / total) * 100
    return score, covered, total, gaps


def _test_coverage(module: str, profile: str) -> Tuple[float, int, int, List[Dict]]:
    """% of requirement-implementing functions that have test cases.
    Sources: Polarion (req) + EA (func) + TestSpec (tests)."""
    rows = run_cypher(
        "MATCH (f)-[:ARCHITECTURALLY_REALIZES|SWUD_TRACES_TO|SRC_IMPLEMENTS_SWA]->(r) "
        "WHERE any(l IN labels(r) WHERE l IN "
        "    ['ProductRequirement', 'SoftwareRequirement', 'StakeholderRequirement']) "
        "  AND toLower(coalesce(f.module, '')) = toLower($mod) "
        "OPTIONAL MATCH (f)-[:TRACES_TO]->(t) "
        "RETURN DISTINCT coalesce(f.function_name, f.name) AS fn, "
        "       count(DISTINCT t) AS test_count",
        {"mod": module}, profile,
    )
    total = len(rows)
    if total == 0:
        return 0.0, 0, 0, []
    covered = sum(1 for r in rows if r["test_count"] > 0)
    gaps = [{"function_name": r["fn"], "issue": "Implements requirement but no test case"}
            for r in rows if r["test_count"] == 0]
    score = (covered / total) * 100
    return score, covered, total, gaps


def _orphan_code_ratio(module: str, profile: str) -> Tuple[float, int, int, List[Dict]]:
    """% of functions WITH traceability (inverse of orphan ratio).
    Sources: Source code + Polarion requirements."""
    total_rows = run_cypher(
        "MATCH (f) "
        "WHERE (f:SRC_Function OR f:SWA_Function OR f:SWUD_Function) "
        "  AND toLower(coalesce(f.module, '')) = toLower($mod) "
        "RETURN count(f) AS total",
        {"mod": module}, profile,
    )
    total = total_rows[0]["total"] if total_rows else 0
    if total == 0:
        return 0.0, 0, 0, []

    orphan_rows = run_cypher(
        "MATCH (f) "
        "WHERE (f:SRC_Function OR f:SWA_Function OR f:SWUD_Function) "
        "  AND toLower(coalesce(f.module, '')) = toLower($mod) "
        "  AND NOT (f)-[:ARCHITECTURALLY_REALIZES|SWUD_TRACES_TO|SRC_IMPLEMENTS_SWA|"
        "              SRC_IMPLEMENTS_SWUD]->() "
        "RETURN coalesce(f.function_name, f.name) AS function_name, labels(f)[0] AS layer",
        {"mod": module}, profile,
    )
    orphans = len(orphan_rows)
    traced = total - orphans
    score = (traced / total) * 100
    return score, traced, total, orphan_rows


def _stale_refs_check(module: str, profile: str) -> Tuple[float, int, int, List[Dict]]:
    """Score = 100 if no stale cross-references, degrades with each stale link.
    Sources: All silos cross-referenced."""
    # Check SWA functions without SWUD match
    stale = run_cypher(
        "MATCH (swa:SWA_Function) "
        "WHERE toLower(coalesce(swa.module, '')) = toLower($mod) "
        "  AND NOT EXISTS { "
        "    MATCH (swud:SWUD_Function) "
        "    WHERE swud.function_name = swa.function_name "
        "  } "
        "RETURN swa.function_name AS function_name, 'SWA→SWUD stale' AS issue",
        {"mod": module}, profile,
    )

    total_swa = run_cypher(
        "MATCH (swa:SWA_Function) "
        "WHERE toLower(coalesce(swa.module, '')) = toLower($mod) "
        "RETURN count(swa) AS total",
        {"mod": module}, profile,
    )
    total = total_swa[0]["total"] if total_swa else 0
    if total == 0:
        return 100.0, 0, 0, stale

    valid = total - len(stale)
    score = (valid / total) * 100
    return score, valid, total, stale


# ─── Grade calculation ───────────────────────────────────────────────────────

def _letter_grade(score: float) -> Tuple[str, str]:
    """Return (grade, colour)."""
    if score >= 90:
        return "A", "#27ae60"
    elif score >= 75:
        return "B", "#2ecc71"
    elif score >= 60:
        return "C", "#f39c12"
    elif score >= 40:
        return "D", "#e67e22"
    else:
        return "F", "#e74c3c"


# ─── Page rendering ──────────────────────────────────────────────────────────

def render(profile: str):
    st.title("Module Release Readiness Score")
    st.caption(
        "A composite readiness score per module. Each check requires data from 2+ "
        "artifact silos — no single ALM tool can compute this."
    )

    modules = get_module_list(profile)
    if not modules:
        module = st.text_input("Module name", value="Adc")
    else:
        module = st.selectbox("Module", modules, key="readiness_module")

    if not module:
        return

    run_btn = st.button("Calculate Readiness", type="primary", key="calc_readiness")
    if not run_btn:
        st.info("Click **Calculate Readiness** to compute the composite score.")
        return

    # ── Run all checks ──────────────────────────────────────────
    checks = [
        ("Req → Function Coverage", "All requirements have implementing functions",
         "Polarion/Jama + EA/Source", 0.30, _req_implementation_coverage),
        ("Function → Design Link", "All source functions linked to SWA/SWUD",
         "Git + EA Model", 0.15, _func_design_coverage),
        ("Test Coverage", "All implementing functions have test cases",
         "Polarion + EA + TestSpec", 0.30, _test_coverage),
        ("No Orphan Code", "Functions with traceability (non-orphan ratio)",
         "Source + Polarion", 0.15, _orphan_code_ratio),
        ("No Stale Cross-References", "SWA↔SWUD consistency",
         "EA SWA + EA SWUD", 0.10, _stale_refs_check),
    ]

    progress = st.progress(0.0, text="Computing readiness...")
    results = []

    for i, (name, desc, sources, weight, func) in enumerate(checks):
        progress.progress((i + 1) / len(checks), text=f"Checking: {name}...")
        score, num, den, gaps = func(module, profile)
        results.append({
            "name": name,
            "description": desc,
            "sources": sources,
            "weight": weight,
            "score": score,
            "numerator": num,
            "denominator": den,
            "gaps": gaps,
        })

    progress.empty()

    # ── Composite score ─────────────────────────────────────────
    composite = sum(r["score"] * r["weight"] for r in results)
    grade, grade_colour = _letter_grade(composite)

    st.divider()

    # Big grade display
    col_grade, col_score, col_module = st.columns([1, 1, 1])
    with col_grade:
        st.markdown(
            f'<div style="text-align:center">'
            f'<span style="font-size:5em;font-weight:bold;color:{grade_colour}">'
            f'{grade}</span></div>',
            unsafe_allow_html=True,
        )
    col_score.metric("Composite Score", f"{composite:.1f} / 100")
    col_module.metric("Module", module)

    st.divider()

    # ── Scorecard table ─────────────────────────────────────────
    st.subheader("Scorecard")

    scorecard_data = []
    for r in results:
        row_grade, row_colour = _letter_grade(r["score"])
        scorecard_data.append({
            "Check": r["name"],
            "Data Sources": r["sources"],
            "Score": f"{r['score']:.0f}%",
            "Coverage": f"{r['numerator']}/{r['denominator']}" if r["denominator"] > 0 else "N/A",
            "Weight": f"{r['weight']*100:.0f}%",
            "Grade": row_grade,
            "Gaps": len(r["gaps"]),
        })

    df = pd.DataFrame(scorecard_data)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Gap details (expandable) ────────────────────────────────
    st.divider()
    st.subheader("Gap Details")

    for r in results:
        if r["gaps"]:
            with st.expander(f"**{r['name']}** — {len(r['gaps'])} gaps", expanded=False):
                st.caption(r["description"])
                gap_df = pd.DataFrame(r["gaps"])
                st.dataframe(gap_df, use_container_width=True, hide_index=True)

    # ── What-if: show how fixing gaps would improve the score ───
    st.divider()
    st.subheader("What-If: Fixing Gaps")
    total_gaps = sum(len(r["gaps"]) for r in results)
    st.markdown(
        f"If all **{total_gaps}** gaps were resolved, the composite score would be "
        f"**100.0** (Grade: **A**)."
    )

    # Show per-check impact
    for r in results:
        if r["gaps"] and r["denominator"] > 0:
            current = r["score"]
            fixed = 100.0
            delta = (fixed - current) * r["weight"]
            st.markdown(
                f"- Fixing **{r['name']}** ({len(r['gaps'])} gaps): "
                f"composite score **+{delta:.1f}** points"
            )

"""
Impact Analysis Engine
======================
Given a starting node (register, function, requirement, etc.), compute the
full blast radius across all artifact silos:

  Register → Functions → Requirements → Tests → ASIL levels

Every result is pure graph traversal — zero LLM, zero probability.
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from collections import defaultdict
from typing import Dict, List

from db import run_cypher, get_module_list

# ─── Colour palette for node types ──────────────────────────────────────────
_COLOURS = {
    "SFR_Register": "#e74c3c",
    "SFR_BitField": "#c0392b",
    "SRC_Function": "#3498db",
    "SWA_Function": "#2980b9",
    "SWUD_Function": "#1abc9c",
    "ProductRequirement": "#f39c12",
    "SoftwareRequirement": "#e67e22",
    "StakeholderRequirement": "#d35400",
    "TS_FunctionalTestCase": "#9b59b6",
    "TS_WCETTestCase": "#8e44ad",
    "SWA_ArchitecturalDecision": "#2ecc71",
    "SWUD_DesignDecision": "#27ae60",
    "MCALModule": "#34495e",
}


# ─── Impact queries ─────────────────────────────────────────────────────────

def _impact_from_register(register_name: str, profile: str) -> Dict:
    """Blast radius starting from an SFR register."""

    # 1. Find the register
    regs = run_cypher(
        "MATCH (r:SFR_Register) "
        "WHERE toLower(r.name) = toLower($name) OR toLower(r.register_id) CONTAINS toLower($name) "
        "RETURN r.name AS name, r.register_id AS id, r.address AS addr, r.description AS desc",
        {"name": register_name}, profile,
    )
    if not regs:
        return {"found": False, "query": register_name}

    reg = regs[0]

    # 2. Functions that access this register
    funcs = run_cypher(
        "MATCH (f)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
        "WHERE toLower(r.name) = toLower($name) OR toLower(r.register_id) CONTAINS toLower($name) "
        "RETURN DISTINCT f.function_name AS fn, f.name AS name, "
        "       labels(f)[0] AS label, f.file_path AS file",
        {"name": register_name}, profile,
    )

    func_names = [f.get("fn") or f.get("name") or "?" for f in funcs]

    # 3. Requirements traced from those functions
    reqs = []
    if func_names:
        reqs = run_cypher(
            "MATCH (f)-[rel]->(r) "
            "WHERE (f.function_name IN $fns OR f.name IN $fns) "
            "  AND type(rel) IN ['ARCHITECTURALLY_REALIZES', 'SWUD_TRACES_TO', "
            "                     'SWA_CONFIG_TRACES_TO', 'SRC_IMPLEMENTS_SWA'] "
            "  AND any(l IN labels(r) WHERE l IN ['ProductRequirement', "
            "          'SoftwareRequirement', 'StakeholderRequirement', "
            "          'SWA_Function', 'SWUD_Function']) "
            "RETURN DISTINCT coalesce(r.requirement_id, r.function_name, r.name) AS id, "
            "       labels(r)[0] AS label, r.name AS name, "
            "       coalesce(r.asil, r.safety_level, 'N/A') AS asil",
            {"fns": func_names}, profile,
        )

    # Also try reverse: SWA_Function → ARCHITECTURALLY_REALIZES → Requirement
    req_ids_direct = {r["id"] for r in reqs}
    swa_fns = [f.get("fn") or f.get("name") for f in funcs
               if f.get("label") in ("SWA_Function", "SRC_Function")]
    if swa_fns:
        bridged = run_cypher(
            "MATCH (swa:SWA_Function)-[:ARCHITECTURALLY_REALIZES]->(req) "
            "WHERE swa.function_name IN $fns "
            "  AND any(l IN labels(req) WHERE l IN $req_labels) "
            "RETURN DISTINCT coalesce(req.requirement_id, req.name) AS id, "
            "       labels(req)[0] AS label, req.name AS name, "
            "       coalesce(req.asil, req.safety_level, 'N/A') AS asil",
            {"fns": swa_fns,
             "req_labels": ["ProductRequirement", "SoftwareRequirement",
                            "StakeholderRequirement"]},
            profile,
        )
        for b in bridged:
            if b["id"] not in req_ids_direct:
                reqs.append(b)

    req_names = [r["id"] for r in reqs if r.get("id")]

    # 4. Test cases covering those requirements
    tests = []
    if req_names:
        tests = run_cypher(
            "MATCH (f)-[:TRACES_TO]->(t) "
            "WHERE any(l IN labels(t) WHERE l IN ['TS_FunctionalTestCase', 'TS_WCETTestCase']) "
            "  AND (f.function_name IN $fns OR f.name IN $fns) "
            "RETURN DISTINCT coalesce(t.test_case_id, t.global_id, t.name) AS id, "
            "       labels(t)[0] AS label, t.name AS name",
            {"fns": func_names}, profile,
        )

    # Also check TS_VERIFIES (test → requirement)
    if req_names:
        verified = run_cypher(
            "MATCH (t)-[:TS_VERIFIES]->(r) "
            "WHERE coalesce(r.requirement_id, r.name) IN $rids "
            "RETURN DISTINCT coalesce(t.test_case_id, t.global_id, t.name) AS id, "
            "       labels(t)[0] AS label, t.name AS name",
            {"rids": req_names}, profile,
        )
        existing_ids = {t["id"] for t in tests}
        for v in verified:
            if v["id"] not in existing_ids:
                tests.append(v)

    # 5. ASIL summary
    asil_counts = defaultdict(int)
    for r in reqs:
        asil_counts[r.get("asil", "N/A")] += 1

    return {
        "found": True,
        "register": reg,
        "functions": funcs,
        "requirements": reqs,
        "tests": tests,
        "asil_summary": dict(asil_counts),
        "chain_summary": {
            "registers": 1,
            "functions": len(funcs),
            "requirements": len(reqs),
            "tests": len(tests),
        },
    }


def _impact_from_function(function_name: str, profile: str) -> Dict:
    """Blast radius starting from a function name."""

    # 1. Find the function across all function labels
    funcs = run_cypher(
        "MATCH (f) "
        "WHERE (f:SRC_Function OR f:SWA_Function OR f:SWUD_Function) "
        "  AND (toLower(f.function_name) = toLower($name) OR toLower(f.name) = toLower($name)) "
        "RETURN f.function_name AS fn, f.name AS name, labels(f)[0] AS label, "
        "       f.file_path AS file",
        {"name": function_name}, profile,
    )
    if not funcs:
        return {"found": False, "query": function_name}

    fn = funcs[0].get("fn") or funcs[0].get("name")

    # 2. Registers this function accesses
    registers = run_cypher(
        "MATCH (f)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
        "WHERE f.function_name = $fn OR f.name = $fn "
        "RETURN DISTINCT r.name AS name, r.register_id AS id, r.address AS addr",
        {"fn": fn}, profile,
    )

    # 3. Requirements (upstream)
    reqs = run_cypher(
        "MATCH (f)-[rel]->(r) "
        "WHERE (f.function_name = $fn OR f.name = $fn) "
        "  AND type(rel) IN ['ARCHITECTURALLY_REALIZES', 'SWUD_TRACES_TO', "
        "                     'SRC_IMPLEMENTS_SWA', 'SRC_IMPLEMENTS_SWUD'] "
        "RETURN DISTINCT coalesce(r.requirement_id, r.function_name, r.name) AS id, "
        "       labels(r)[0] AS label, r.name AS name, "
        "       coalesce(r.asil, r.safety_level, 'N/A') AS asil",
        {"fn": fn}, profile,
    )

    # Also bridge: SWA_Function → ARCHITECTURALLY_REALIZES → ProductRequirement
    swa_targets = [r["id"] for r in reqs if r.get("label") == "SWA_Function"]
    if swa_targets:
        bridged = run_cypher(
            "MATCH (swa:SWA_Function)-[:ARCHITECTURALLY_REALIZES]->(req) "
            "WHERE swa.function_name IN $fns "
            "RETURN DISTINCT coalesce(req.requirement_id, req.name) AS id, "
            "       labels(req)[0] AS label, req.name AS name, "
            "       coalesce(req.asil, req.safety_level, 'N/A') AS asil",
            {"fns": swa_targets}, profile,
        )
        existing = {r["id"] for r in reqs}
        for b in bridged:
            if b["id"] not in existing:
                reqs.append(b)

    # 4. Tests (downstream)
    tests = run_cypher(
        "MATCH (f)-[:TRACES_TO]->(t) "
        "WHERE (f.function_name = $fn OR f.name = $fn) "
        "RETURN DISTINCT coalesce(t.test_case_id, t.global_id, t.name) AS id, "
        "       labels(t)[0] AS label, t.name AS name",
        {"fn": fn}, profile,
    )

    # ASIL
    asil_counts = defaultdict(int)
    for r in reqs:
        asil_counts[r.get("asil", "N/A")] += 1

    return {
        "found": True,
        "function": funcs[0],
        "registers": registers,
        "requirements": reqs,
        "tests": tests,
        "asil_summary": dict(asil_counts),
        "chain_summary": {
            "registers": len(registers),
            "functions": len(funcs),
            "requirements": len(reqs),
            "tests": len(tests),
        },
    }


def _impact_from_requirement(req_id: str, profile: str) -> Dict:
    """Blast radius starting from a requirement ID."""

    reqs = run_cypher(
        "MATCH (r) "
        "WHERE any(l IN labels(r) WHERE l IN $labels) "
        "  AND (r.requirement_id = $rid OR toLower(r.name) CONTAINS toLower($rid)) "
        "RETURN r.requirement_id AS id, r.name AS name, labels(r)[0] AS label, "
        "       coalesce(r.asil, r.safety_level, 'N/A') AS asil",
        {"rid": req_id,
         "labels": ["ProductRequirement", "SoftwareRequirement", "StakeholderRequirement"]},
        profile,
    )
    if not reqs:
        return {"found": False, "query": req_id}

    rid = reqs[0]["id"] or reqs[0]["name"]

    # Functions implementing this requirement
    funcs = run_cypher(
        "MATCH (f)-[rel]->(r) "
        "WHERE (r.requirement_id = $rid OR r.name = $rid) "
        "  AND type(rel) IN ['ARCHITECTURALLY_REALIZES', 'SWUD_TRACES_TO', "
        "                     'SWA_CONFIG_TRACES_TO'] "
        "RETURN DISTINCT f.function_name AS fn, f.name AS name, labels(f)[0] AS label",
        {"rid": rid}, profile,
    )

    fn_names = [f.get("fn") or f.get("name") for f in funcs]

    # Registers accessed by those functions
    registers = []
    if fn_names:
        registers = run_cypher(
            "MATCH (f)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
            "WHERE f.function_name IN $fns OR f.name IN $fns "
            "RETURN DISTINCT r.name AS name, r.register_id AS id, r.address AS addr",
            {"fns": fn_names}, profile,
        )

    # Tests
    tests = []
    if fn_names:
        tests = run_cypher(
            "MATCH (f)-[:TRACES_TO]->(t) "
            "WHERE f.function_name IN $fns OR f.name IN $fns "
            "RETURN DISTINCT coalesce(t.test_case_id, t.global_id, t.name) AS id, "
            "       labels(t)[0] AS label, t.name AS name",
            {"fns": fn_names}, profile,
        )

    # TS_VERIFIES
    verified = run_cypher(
        "MATCH (t)-[:TS_VERIFIES]->(r) "
        "WHERE r.requirement_id = $rid OR r.name = $rid "
        "RETURN DISTINCT coalesce(t.test_case_id, t.global_id, t.name) AS id, "
        "       labels(t)[0] AS label, t.name AS name",
        {"rid": rid}, profile,
    )
    existing_ids = {t["id"] for t in tests}
    for v in verified:
        if v["id"] not in existing_ids:
            tests.append(v)

    return {
        "found": True,
        "requirement": reqs[0],
        "functions": funcs,
        "registers": registers,
        "tests": tests,
        "asil_summary": {reqs[0].get("asil", "N/A"): 1},
        "chain_summary": {
            "registers": len(registers),
            "functions": len(funcs),
            "requirements": 1,
            "tests": len(tests),
        },
    }


# ─── Rendering ───────────────────────────────────────────────────────────────

def _render_chain_metrics(result: Dict):
    """Show the chain summary as big metric cards."""
    s = result["chain_summary"]
    cols = st.columns(4)
    cols[0].metric("Registers", s["registers"])
    cols[1].metric("Functions", s["functions"])
    cols[2].metric("Requirements", s["requirements"])
    cols[3].metric("Test Cases", s["tests"])


def _render_asil_bar(result: Dict):
    """Show ASIL distribution as a horizontal bar."""
    asil = result.get("asil_summary", {})
    if not asil:
        return
    st.subheader("Safety Classification (ASIL)")
    # Order: ASIL-D > ASIL-C > ASIL-B > ASIL-A > QM > N/A
    order = ["ASIL-D", "ASIL-C", "ASIL-B", "ASIL-A", "QM", "N/A"]
    asil_colours = {
        "ASIL-D": "#c0392b", "ASIL-C": "#e74c3c", "ASIL-B": "#e67e22",
        "ASIL-A": "#f1c40f", "QM": "#2ecc71", "N/A": "#95a5a6",
    }
    for level in order:
        count = asil.get(level, 0)
        if count > 0:
            colour = asil_colours.get(level, "#95a5a6")
            st.markdown(
                f'<span style="background:{colour};color:white;padding:4px 12px;'
                f'border-radius:4px;margin-right:8px;font-weight:bold">'
                f'{level}: {count}</span>',
                unsafe_allow_html=True,
            )


def _render_table(title: str, rows: List[Dict], key_col: str = "id"):
    """Render a list of dicts as a Streamlit dataframe."""
    if not rows:
        st.info(f"No {title.lower()} found.")
        return
    st.subheader(f"{title} ({len(rows)})")
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ─── Page entry point ────────────────────────────────────────────────────────

def render(profile: str):
    st.title("Impact Analysis Engine")
    st.caption(
        "Type a register, function, or requirement ID. The engine traverses the "
        "knowledge graph to compute the full blast radius across all artifact "
        "silos — zero LLM, pure deterministic graph traversal."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        query = st.text_input(
            "Search",
            placeholder="e.g.  CLC  |  Adc_Init  |  PRQ-ADC-042",
            key="impact_query",
        )
    with col2:
        entity_type = st.selectbox(
            "Type",
            ["Auto-detect", "Register", "Function", "Requirement"],
            key="impact_type",
        )

    if not query:
        st.info("Enter a register name, function name, or requirement ID above.")
        return

    # Auto-detect entity type from naming patterns
    if entity_type == "Auto-detect":
        ql = query.lower().strip()
        if any(ql.startswith(p) for p in ("prq", "shrq", "swrq", "req")):
            entity_type = "Requirement"
        elif any(c in ql for c in ("_init", "_de", "_get", "_set", "ifx")):
            entity_type = "Function"
        else:
            entity_type = "Register"

    with st.spinner(f"Traversing graph from {entity_type}: **{query}** ..."):
        if entity_type == "Register":
            result = _impact_from_register(query, profile)
        elif entity_type == "Function":
            result = _impact_from_function(query, profile)
        else:
            result = _impact_from_requirement(query, profile)

    if not result.get("found"):
        st.warning(f"No {entity_type.lower()} matching **{query}** found in the graph.")
        return

    # ── Summary metrics ─────────────────────────────────────────
    st.divider()
    st.subheader("Blast Radius Summary")
    _render_chain_metrics(result)

    # ── ASIL distribution ───────────────────────────────────────
    _render_asil_bar(result)

    # ── Detail tables ───────────────────────────────────────────
    st.divider()

    if entity_type == "Register":
        _render_table("Affected Functions", result.get("functions", []))
        _render_table("Linked Requirements", result.get("requirements", []))
        _render_table("Test Cases to Re-run", result.get("tests", []))
    elif entity_type == "Function":
        _render_table("Registers Accessed", result.get("registers", []))
        _render_table("Linked Requirements", result.get("requirements", []))
        _render_table("Test Cases to Re-run", result.get("tests", []))
    else:
        _render_table("Implementing Functions", result.get("functions", []))
        _render_table("Registers Touched", result.get("registers", []))
        _render_table("Test Cases", result.get("tests", []))

    # ── Manual effort estimate ──────────────────────────────────
    st.divider()
    s = result["chain_summary"]
    total_artifacts = s["registers"] + s["functions"] + s["requirements"] + s["tests"]
    # Conservative: 5 min per artifact lookup across tools
    manual_minutes = total_artifacts * 5
    manual_hours = manual_minutes / 60

    st.subheader("Estimated Manual Effort")
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Artifacts in blast radius", total_artifacts)
    mc2.metric("Manual lookup estimate", f"{manual_hours:.1f} hours")
    mc3.metric("Graph traversal time", "< 3 sec")

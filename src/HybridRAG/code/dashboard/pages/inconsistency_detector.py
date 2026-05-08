"""
Cross-Silo Inconsistency Detector
===================================
Finds real broken links, coverage gaps, and design-vs-code mismatches
that span multiple artifact silos (EA architecture, source code,
test specs, Polarion requirements, SFR hardware model).

No single ALM tool can detect these — only the knowledge graph sees
across all silos simultaneously.
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
from typing import Dict, List, Tuple

from db import run_cypher


# ─── Colour scheme ──────────────────────────────────────────────────────────

_SEVERITY_COLOURS = {
    "critical": "#c0392b",
    "high":     "#e74c3c",
    "medium":   "#e67e22",
    "low":      "#f1c40f",
}

_SILO_TAGS = {
    "EA":     "#3498db",
    "SRC":    "#2ecc71",
    "TEST":   "#9b59b6",
    "PRQ":    "#e67e22",
    "SFR":    "#1abc9c",
}


def _severity_badge(severity: str) -> str:
    c = _SEVERITY_COLOURS.get(severity, "#95a5a6")
    return (f'<span style="background:{c};color:white;padding:2px 10px;'
            f'border-radius:4px;font-size:0.78em;font-weight:600">'
            f'{severity.upper()}</span>')


def _silo_tag(name: str) -> str:
    c = _SILO_TAGS.get(name, "#7f8c8d")
    return (f'<span style="background:{c};color:white;padding:1px 7px;'
            f'border-radius:3px;font-size:0.72em;margin-right:3px">{name}</span>')


# ═════════════════════════════════════════════════════════════════════════════
# Inconsistency checks — each returns (rows, silos_crossed)
# ═════════════════════════════════════════════════════════════════════════════

def _check_unverified_prqs(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """Product requirements with no test verification (TS_VERIFIES)."""
    rows = run_cypher("""
        MATCH (prq:ProductRequirement)
        WHERE prq.module = $mod AND NOT ()-[:TS_VERIFIES]->(prq)
        RETURN prq.requirement_id AS requirement_id,
               prq.name            AS requirement_name,
               coalesce(prq.asil, 'N/A') AS asil
        ORDER BY
            CASE prq.asil
                WHEN 'ASIL_D' THEN 0 WHEN 'ASIL_C' THEN 1
                WHEN 'ASIL_B' THEN 2 WHEN 'ASIL_A' THEN 3
                ELSE 4
            END,
            prq.requirement_id
    """, {"mod": module}, profile)
    return rows, ["PRQ", "TEST"]


def _check_unimplemented_ea_reqs(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """EA requirements that no function implements (EA_IMPLEMENTS)."""
    rows = run_cypher("""
        MATCH (req:EA_Requirement)
        WHERE req.module = $mod AND NOT ()-[:EA_IMPLEMENTS]->(req)
        RETURN req.name                          AS requirement_name,
               coalesce(req.tv_rid, 'N/A')       AS prq_id,
               coalesce(req.tv_rasil, 'N/A')     AS asil,
               coalesce(req.tv_rstatus, 'N/A')   AS status
        ORDER BY req.name
    """, {"mod": module}, profile)
    return rows, ["EA", "PRQ"]


def _check_ea_without_src(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """EA architecture functions with no source-code implementation."""
    rows = run_cypher("""
        MATCH (ea:EA_Function)
        WHERE ea.module = $mod AND NOT ()-[:SRC_IMPLEMENTS_EA]->(ea)
        RETURN ea.name                       AS ea_function,
               coalesce(ea.kind, 'N/A')      AS kind,
               coalesce(ea.stereotype, 'N/A') AS stereotype
        ORDER BY ea.name
    """, {"mod": module}, profile)
    return rows, ["EA", "SRC"]


def _check_undoc_register_access(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """Source code accesses hardware registers not documented in EA model."""
    rows = run_cypher("""
        MATCH (src:SRC_Function)-[:SRC_ACCESSES_SFR]->(sfr:SFR_Register)
        WHERE src.module = $mod
          AND NOT EXISTS {
            MATCH (ea:EA_Function)-[:EA_ACCESSES_REGISTER]->(eareg:EA_Register)
            WHERE ea.name = src.name AND eareg.name = sfr.name
          }
        WITH src, count(DISTINCT sfr) AS undoc_regs,
             collect(DISTINCT sfr.name)[0..5] AS sample_registers
        RETURN src.name           AS source_function,
               undoc_regs         AS undocumented_register_count,
               sample_registers   AS sample_registers
        ORDER BY undoc_regs DESC
    """, {"mod": module}, profile)
    return rows, ["SRC", "SFR", "EA"]


def _check_untraced_design_decisions(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """Design decisions not traced to any requirement."""
    rows = run_cypher("""
        MATCH (dd:EA_DesignDecision)
        WHERE dd.module = $mod AND NOT (dd)-[:EA_IMPLEMENTS]->(:EA_Requirement)
        RETURN dd.name AS design_decision
        ORDER BY dd.name
    """, {"mod": module}, profile)
    return rows, ["EA", "PRQ"]


def _check_writes_no_test(module: str, profile: str) -> Tuple[List[Dict], List[str]]:
    """Source functions writing to hardware registers with no test coverage via EA chain."""
    rows = run_cypher("""
        MATCH (src:SRC_Function)-[acc:SRC_ACCESSES_SFR]->(sfr:SFR_Register)
        WHERE src.module = $mod AND acc.access_type = 'WRITE'
        WITH src, collect(DISTINCT sfr.name) AS written_regs
        WHERE NOT EXISTS {
            MATCH (src)-[:SRC_IMPLEMENTS_EA]->(ea:EA_Function)-[:TRACES_TO]->()
        }
        RETURN src.name          AS source_function,
               size(written_regs) AS registers_written,
               written_regs[0..5] AS sample_registers
        ORDER BY size(written_regs) DESC
    """, {"mod": module}, profile)
    return rows, ["SRC", "SFR", "TEST"]


# ═════════════════════════════════════════════════════════════════════════════
# KG overview stats (top banner)
# ═════════════════════════════════════════════════════════════════════════════

def _load_stats(module: str, profile: str) -> Dict[str, int]:
    counts = {}
    queries = {
        "EA Functions":         "MATCH (n:EA_Function) WHERE n.module=$mod RETURN count(n) AS c",
        "SRC Functions":        "MATCH (n:SRC_Function) WHERE n.module=$mod RETURN count(n) AS c",
        "Product Requirements": "MATCH (n:ProductRequirement) WHERE n.module=$mod RETURN count(n) AS c",
        "EA Requirements":      "MATCH (n:EA_Requirement) WHERE n.module=$mod RETURN count(n) AS c",
        "Test Cases":           "MATCH (n) WHERE n.module=$mod AND any(l IN labels(n) WHERE l STARTS WITH 'TS_') RETURN count(n) AS c",
        "SFR Registers":        "MATCH (n:SFR_Register) WHERE n.module=$mod RETURN count(n) AS c",
        "Design Decisions":     "MATCH (n:EA_DesignDecision) WHERE n.module=$mod RETURN count(n) AS c",
    }
    for label, cypher in queries.items():
        r = run_cypher(cypher, {"mod": module}, profile)
        counts[label] = r[0]["c"] if r else 0
    return counts


# ═════════════════════════════════════════════════════════════════════════════
# Check-card rendering
# ═════════════════════════════════════════════════════════════════════════════

def _render_check(title: str, why: str, severity: str,
                  rows: List[Dict], silos: List[str],
                  effort_min: int):
    """Render one inconsistency check as an expander card."""
    count = len(rows)
    icon = "✅" if count == 0 else ("🔴" if severity in ("critical", "high") else "⚠️")
    silo_html = " ".join(_silo_tag(s) for s in silos)

    header = (f"{icon}  **{title}** — {count} finding{'s' if count != 1 else ''}  "
              f"{_severity_badge(severity) if count else ''}")

    with st.expander(header, expanded=(count > 0)):
        st.markdown(f"**Silos crossed:** {silo_html}", unsafe_allow_html=True)
        st.caption(why)

        if count == 0:
            st.success("No issues detected.")
            return

        hrs = count * effort_min / 60
        st.markdown(
            f"⏱️ Estimated manual effort to find these: **~{hrs:.1f} engineer-hours**"
        )

        df = pd.DataFrame(rows)

        # Highlight ASIL columns if present
        if "asil" in df.columns:
            def _color_asil(val):
                m = {"ASIL_D": "#c0392b", "ASIL_C": "#e74c3c",
                     "ASIL_B": "#e67e22", "ASIL_A": "#f1c40f"}
                bg = m.get(val, "")
                return f"background-color: {bg}; color: white" if bg else ""
            styled = df.style.map(_color_asil, subset=["asil"])
            st.dataframe(styled, use_container_width=True, hide_index=True)
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)


# ═════════════════════════════════════════════════════════════════════════════
# Main page render
# ═════════════════════════════════════════════════════════════════════════════

def render(profile: str):

    # ── Title block ─────────────────────────────────────────────
    st.markdown(
        '<h1 style="margin-bottom:0">🔍 Cross-Silo Inconsistency Detector</h1>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Automated audit across EA architecture, source code, hardware registers, "
        "test specs, and Polarion requirements. "
        "These findings are **invisible** to any single ALM tool."
    )

    # ── Module selector ─────────────────────────────────────────
    module = st.text_input("Module", value="ADC", key="audit_module")
    if not module:
        return

    run_btn = st.button("🚀 Run Full Audit", type="primary", key="run_audit")

    if not run_btn:
        st.info("Click **Run Full Audit** to scan for cross-silo inconsistencies.")
        return

    # ── Run all checks ──────────────────────────────────────────
    checks = [
        # (id, title, why_text, severity, check_fn, effort_min_per_item)
        ("unverified_prqs",
         "Unverified Product Requirements",
         "Product requirements in Polarion that have **no test case** verifying them "
         "(no TS_VERIFIES edge). Polarion shows the requirement exists, but cannot tell "
         "you whether any test actually validates it — that requires cross-referencing "
         "the test specification silo.",
         "critical", _check_unverified_prqs, 20),

        ("unimpl_ea_reqs",
         "Unimplemented EA Requirements",
         "EA requirements that **no architecture function implements** (no incoming "
         "EA_IMPLEMENTS edge). The requirement exists in the EA model but nothing in "
         "the design claims to implement it.",
         "high", _check_unimplemented_ea_reqs, 15),

        ("ea_without_src",
         "EA Functions Without Source Code",
         "Architecture functions defined in EA but with **no source code implementation** "
         "(no SRC_IMPLEMENTS_EA edge). EA says this function should exist, but the code "
         "doesn't implement it.",
         "high", _check_ea_without_src, 15),

        ("undoc_reg",
         "Undocumented Register Access (Code vs. EA Model)",
         "Source code accesses hardware registers (SRC_ACCESSES_SFR) that are **not "
         "documented in the EA architecture model** (no matching EA_ACCESSES_REGISTER). "
         "This is the classic cross-silo gap: only by combining source code analysis "
         "with the architecture model can you detect this.",
         "high", _check_undoc_register_access, 25),

        ("untraced_dd",
         "Untraced Design Decisions",
         "Design decisions in the EA model that are **not linked to any requirement** "
         "(no EA_IMPLEMENTS edge to EA_Requirement). These decisions have no traceability — "
         "you can't tell which requirement drove them.",
         "medium", _check_untraced_design_decisions, 10),

        ("writes_no_test",
         "Register Writes Without Test Coverage",
         "Source functions that **write to hardware registers** but have **no test "
         "coverage** through the EA traceability chain (SRC→EA→Test). Hardware writes "
         "without testing are a safety risk.",
         "critical", _check_writes_no_test, 30),
    ]

    progress = st.progress(0.0, text="Running audit...")
    results: Dict[str, Tuple[List[Dict], List[str]]] = {}

    for i, (cid, title, _, _, fn, _) in enumerate(checks):
        progress.progress((i + 1) / len(checks), text=f"Checking: {title}...")
        results[cid] = fn(module, profile)

    progress.empty()

    # ── Stats banner ────────────────────────────────────────────
    st.divider()

    stats = _load_stats(module, profile)
    stat_cols = st.columns(len(stats))
    for col, (label, val) in zip(stat_cols, stats.items()):
        col.metric(label, f"{val:,}")

    # ── Summary metrics ─────────────────────────────────────────
    total_findings = sum(len(results[c[0]][0]) for c in checks)
    critical_findings = sum(
        len(results[c[0]][0]) for c in checks if c[3] == "critical"
    )
    total_hrs = sum(
        len(results[c[0]][0]) * c[5] / 60 for c in checks
    )
    silos_hit = set()
    for cid, (rows, silos) in results.items():
        if rows:
            silos_hit.update(silos)

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Findings", total_findings,
              help="Sum of all issues across all checks")
    m2.metric("Critical / High", critical_findings,
              help="Issues marked Critical or that need immediate attention")
    m3.metric("Silos Crossed", len(silos_hit),
              help="Number of distinct artifact silos involved in findings")
    m4.metric("Manual Audit Equivalent", f"{total_hrs:.0f} hrs",
              help="Estimated engineer-hours to find these manually")

    # Comparison callout
    if total_findings > 0:
        st.markdown(
            f"""
            <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                        padding: 18px 24px; border-radius: 10px; margin: 10px 0 20px 0;
                        border-left: 4px solid #e74c3c;">
                <span style="font-size:1.1em; color: #ecf0f1;">
                    <strong>🏢 Why Polarion / EA / Jama can't find these:</strong>
                    Each tool only sees its own silo.
                    These {total_findings} findings span <strong>{len(silos_hit)} silos</strong>
                    ({', '.join(sorted(silos_hit))}) —
                    only a cross-tool knowledge graph can detect them.
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Individual check cards ──────────────────────────────────
    for cid, title, why, severity, _, effort in checks:
        rows, silos = results[cid]
        _render_check(title, why, severity, rows, silos, effort)


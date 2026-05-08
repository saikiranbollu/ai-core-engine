"""
Knowledge Graph Health Dashboard
=================================
Streamlit dashboard for monitoring Neo4j KG performance, structure,
and data quality.

Usage:
    streamlit run src/HybridRAG/code/kg_dashboard.py -- --profile mcal
    streamlit run src/HybridRAG/code/kg_dashboard.py -- --profile local
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Resolve paths so imports work regardless of cwd
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_CODE_DIR = _SCRIPT_DIR
_KG_DIR = _SCRIPT_DIR / "KG"
for p in (_CODE_DIR, _KG_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from env_config import load_yaml_with_env  # noqa: E402

STORAGE_CONFIG_PATH = _CODE_DIR.parent / "config" / "storage_config.yaml"

# ---------------------------------------------------------------------------
# Neo4j connection (cached for the session)
# ---------------------------------------------------------------------------

def _parse_profile() -> str:
    """Read --profile from sys.argv (after the Streamlit '--' separator)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="mcal")
    # Streamlit passes its own args; only parse after '--'
    try:
        idx = sys.argv.index("--")
        args = parser.parse_args(sys.argv[idx + 1:])
    except ValueError:
        args = parser.parse_args([])
    return args.profile


@st.cache_resource
def get_driver(profile: str):
    """Create a Neo4j driver (cached across reruns)."""
    from neo4j import GraphDatabase
    cfg = load_yaml_with_env(STORAGE_CONFIG_PATH)
    neo = cfg["neo4j"][profile]
    uri = neo["uri"]
    drv_kw = dict(
        auth=(neo["username"], neo["password"]),
        max_connection_lifetime=neo.get("max_connection_lifetime", 3600),
        max_connection_pool_size=neo.get("max_connection_pool_size", 10),
    )
    if "+s" not in uri.split("://")[0]:
        drv_kw["encrypted"] = neo.get("encrypted", False)
    driver = GraphDatabase.driver(uri, **drv_kw)
    driver.verify_connectivity()
    return driver, neo.get("database", "neo4j"), neo.get("description", profile)


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    """Execute a read query and return list of record dicts."""
    driver, db, _ = get_driver(st.session_state.profile)
    with driver.session(database=db) as session:
        result = session.run(cypher, params or {})
        return [dict(r) for r in result]


def timed_query(cypher: str, params: dict | None = None) -> tuple[list[dict], float]:
    """Execute a query and return (records, elapsed_ms)."""
    t0 = time.perf_counter()
    records = run_query(cypher, params)
    elapsed = (time.perf_counter() - t0) * 1000
    return records, elapsed


def profile_query(cypher: str, params: dict | None = None) -> dict:
    """Run PROFILE on a query and return plan analysis dict."""
    driver, db, _ = get_driver(st.session_state.profile)
    with driver.session(database=db) as session:
        t0 = time.perf_counter()
        result = session.run(f"PROFILE {cypher}", params or {})
        records = list(result)
        elapsed = (time.perf_counter() - t0) * 1000
        summary = result.consume()
        plan = summary.profile

        total_db_hits = 0
        total_rows = 0
        operators = []
        has_full_scan = False
        has_index_seek = False

        def walk_plan(p, depth=0):
            nonlocal total_db_hits, total_rows, has_full_scan, has_index_seek
            db_hits = getattr(p, 'db_hits', 0)
            rows = getattr(p, 'rows', 0)
            op_type = getattr(p, 'operator_type', '?')
            total_db_hits += db_hits
            if depth == 0:
                total_rows = rows
            if 'NodeByLabelScan' in op_type or 'AllNodesScan' in op_type:
                has_full_scan = True
            if 'IndexSeek' in op_type or 'UniqueIndexSeek' in op_type:
                has_index_seek = True
            operators.append({
                "depth": depth,
                "operator": op_type,
                "db_hits": db_hits,
                "rows": rows,
            })
            for child in getattr(p, 'children', []):
                walk_plan(child, depth + 1)

        walk_plan(plan)

        return {
            "elapsed_ms": elapsed,
            "total_db_hits": total_db_hits,
            "result_rows": len(records),
            "operators": operators,
            "has_full_scan": has_full_scan,
            "has_index_seek": has_index_seek,
            "plan_rows_scanned": max((op["rows"] for op in operators), default=0),
        }


# ---------------------------------------------------------------------------
# Dashboard sections
# ---------------------------------------------------------------------------

def section_overview():
    """Top-level KG size metrics."""
    st.header("Graph Overview")

    col1, col2, col3 = st.columns(3)

    rows, ms = timed_query("MATCH (n) RETURN count(n) AS cnt")
    total_nodes = rows[0]["cnt"] if rows else 0
    col1.metric("Total Nodes", f"{total_nodes:,}", help=f"Query: {ms:.0f}ms")

    rows, ms = timed_query("MATCH ()-[r]->() RETURN count(r) AS cnt")
    total_rels = rows[0]["cnt"] if rows else 0
    col2.metric("Total Relationships", f"{total_rels:,}", help=f"Query: {ms:.0f}ms")

    rows, ms = timed_query(
        "MATCH (n) RETURN count(DISTINCT labels(n)) AS label_count"
    )
    label_count = rows[0]["label_count"] if rows else 0
    col3.metric("Distinct Label Combos", f"{label_count:,}", help=f"Query: {ms:.0f}ms")

    # Density (approximate for directed graph)
    if total_nodes > 1:
        density = total_rels / (total_nodes * (total_nodes - 1))
        st.caption(f"Graph density: {density:.2e}")


def section_node_distribution():
    """Node counts per label."""
    st.header("Node Distribution")

    rows, ms = timed_query("""
        MATCH (n)
        WITH labels(n) AS lbls
        UNWIND lbls AS label
        RETURN label, count(*) AS count
        ORDER BY count DESC
    """)
    if not rows:
        st.info("No nodes found.")
        return

    st.caption(f"Query time: {ms:.0f}ms")

    import pandas as pd
    df = pd.DataFrame(rows)
    col1, col2 = st.columns([2, 3])
    with col1:
        st.dataframe(df, width='stretch', hide_index=True)
    with col2:
        st.bar_chart(df.set_index("label")["count"])


def section_edge_distribution():
    """Relationship counts per type."""
    st.header("Relationship Distribution")

    rows, ms = timed_query("""
        MATCH ()-[r]->()
        RETURN type(r) AS rel_type, count(*) AS count
        ORDER BY count DESC
    """)
    if not rows:
        st.info("No relationships found.")
        return

    st.caption(f"Query time: {ms:.0f}ms")

    import pandas as pd
    df = pd.DataFrame(rows)
    col1, col2 = st.columns([2, 3])
    with col1:
        st.dataframe(df, width='stretch', hide_index=True)
    with col2:
        st.bar_chart(df.set_index("rel_type")["count"])


def section_module_breakdown():
    """Nodes per module."""
    st.header("Module Breakdown")

    rows, ms = timed_query("""
        MATCH (n)
        WHERE n.module IS NOT NULL
        WITH labels(n) AS lbls, n.module AS module
        UNWIND lbls AS label
        RETURN module, label, count(*) AS count
        ORDER BY module, count DESC
    """)
    if not rows:
        st.info("No nodes with `module` property found.")
        return

    st.caption(f"Query time: {ms:.0f}ms")

    import pandas as pd
    df = pd.DataFrame(rows)

    # Summary by module
    module_totals = df.groupby("module")["count"].sum().reset_index()
    module_totals.columns = ["module", "total_nodes"]
    module_totals = module_totals.sort_values("total_nodes", ascending=False)

    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("Module totals")
        st.dataframe(module_totals, width='stretch', hide_index=True)
    with col2:
        st.subheader("Nodes by module & label")
        # Pivot: module as rows, label as columns
        pivot = df.pivot_table(index="module", columns="label", values="count", fill_value=0)
        st.dataframe(pivot, width='stretch')


def section_index_health():
    """List indexes and their status."""
    st.header("Index Health")

    rows, ms = timed_query("""
        SHOW INDEXES
        YIELD name, type, labelsOrTypes, properties, state, populationPercent
        RETURN name, type, labelsOrTypes, properties, state, populationPercent
        ORDER BY type, name
    """)
    if not rows:
        st.warning("No indexes found (or SHOW INDEXES not supported).")
        return

    st.caption(f"Query time: {ms:.0f}ms | {len(rows)} indexes")

    import pandas as pd
    df = pd.DataFrame(rows)
    # Convert list cols to strings for display
    df["labelsOrTypes"] = df["labelsOrTypes"].apply(lambda x: ", ".join(x) if x else "")
    df["properties"] = df["properties"].apply(lambda x: ", ".join(x) if x else "")

    # Summary metrics
    col1, col2, col3 = st.columns(3)
    online = len(df[df["state"] == "ONLINE"])
    col1.metric("Online Indexes", online)
    col2.metric("Total Indexes", len(df))
    non_full = df[df["populationPercent"] < 100.0]
    col3.metric("Still Populating", len(non_full))

    st.dataframe(df, width='stretch', hide_index=True)


def section_orphan_nodes():
    """Nodes with no relationships."""
    st.header("Orphan Nodes (no edges)")

    rows, ms = timed_query("""
        MATCH (n)
        WHERE NOT (n)--()
        WITH labels(n) AS lbls
        UNWIND lbls AS label
        RETURN label, count(*) AS orphan_count
        ORDER BY orphan_count DESC
    """)
    st.caption(f"Query time: {ms:.0f}ms")

    if not rows:
        st.success("No orphan nodes found — all nodes have at least one edge.")
        return

    import pandas as pd
    df = pd.DataFrame(rows)
    total_orphans = df["orphan_count"].sum()
    st.metric("Total Orphan Nodes", f"{total_orphans:,}")
    st.dataframe(df, width='stretch', hide_index=True)


def section_property_fill():
    """Property fill rates for key node types."""
    st.header("Property Fill Rates")

    # Dynamically fetch labels that actually exist in the graph,
    # falling back to a curated default list.
    preferred = [
        "SRC_Function", "SRC_GlobalVariable", "SRC_DataType",
        "SFR_Register", "SFR_BitField",
        "EA_Function", "EA_DataType", "EA_Register",
        "EA_DesignDecision", "EA_ConfigParameter",
        "ProductRequirement", "StakeholderRequirement",
        "TS_FunctionalTestCase",
        "MCALModule",
    ]
    try:
        live_labels_rows = run_query("CALL db.labels() YIELD label RETURN label ORDER BY label")
        live_labels = [r["label"] for r in live_labels_rows]
        # Show preferred ones first (if they exist), then the rest
        ordered = [l for l in preferred if l in live_labels]
        ordered += [l for l in live_labels if l not in ordered]
    except Exception:
        ordered = preferred

    label = st.selectbox("Select node label", ordered, index=0)

    rows, ms = timed_query(f"""
        MATCH (n:{label})
        WITH n, keys(n) AS props
        UNWIND props AS prop
        RETURN prop, count(*) AS filled
        ORDER BY filled DESC
    """)
    if not rows:
        st.info(f"No :{label} nodes found.")
        return

    total_rows, _ = timed_query(f"MATCH (n:{label}) RETURN count(n) AS cnt")
    total = total_rows[0]["cnt"] if total_rows else 0

    st.caption(f"Query time: {ms:.0f}ms | {total:,} total :{label} nodes")

    import pandas as pd
    df = pd.DataFrame(rows)
    df["total"] = total
    df["fill_pct"] = (df["filled"] / total * 100).round(1)
    df = df[["prop", "filled", "total", "fill_pct"]]
    df.columns = ["Property", "Filled", "Total", "Fill %"]

    col1, col2 = st.columns([3, 2])
    with col1:
        st.dataframe(df, width='stretch', hide_index=True)
    with col2:
        chart_df = df.set_index("Property")["Fill %"]
        st.bar_chart(chart_df)


def section_degree_distribution():
    """Degree distribution (top hubs and stats)."""
    st.header("Degree Distribution")

    rows, ms = timed_query("""
        MATCH (n)
        WITH n, size((n)--()) AS degree
        RETURN labels(n)[0] AS label, n.name AS name,
               n.function_name AS fn_name, degree
        ORDER BY degree DESC
        LIMIT 25
    """)
    st.caption(f"Query time: {ms:.0f}ms")

    if not rows:
        st.info("No nodes found.")
        return

    import pandas as pd
    df = pd.DataFrame(rows)
    df["display_name"] = df.apply(
        lambda r: r["name"] or r["fn_name"] or "(unnamed)", axis=1
    )
    df = df[["label", "display_name", "degree"]]
    df.columns = ["Label", "Name", "Degree"]

    st.subheader("Top 25 Hub Nodes")
    st.dataframe(df, width='stretch', hide_index=True)

    # Degree stats
    stats_rows, _ = timed_query("""
        MATCH (n)
        WITH size((n)--()) AS degree
        RETURN min(degree) AS min_deg, max(degree) AS max_deg,
               avg(degree) AS avg_deg, percentileCont(degree, 0.5) AS median_deg,
               percentileCont(degree, 0.95) AS p95_deg
    """)
    if stats_rows:
        s = stats_rows[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Min", s["min_deg"])
        c2.metric("Median", f"{s['median_deg']:.0f}")
        c3.metric("Mean", f"{s['avg_deg']:.1f}")
        c4.metric("P95", f"{s['p95_deg']:.0f}")
        c5.metric("Max", s["max_deg"])


def section_query_benchmark():
    """Benchmark common query patterns."""
    st.header("Query Benchmark")
    st.caption("Measures round-trip time for common query patterns against the live database.")

    benchmarks = [
        ("Single node lookup (indexed)",
         "MATCH (n:SRC_Function {name: 'Adc_Init'}) RETURN n LIMIT 1"),
        ("Label scan + filter",
         "MATCH (n:SRC_Function) WHERE n.module = 'ADC' RETURN count(n)"),
        ("1-hop traversal",
         "MATCH (f:SRC_Function)-[:CALLS]->(g:SRC_Function) RETURN count(*) AS cnt"),
        ("2-hop traversal",
         "MATCH (f:SRC_Function)-[:CALLS*1..2]->(g) RETURN count(*) AS cnt"),
        ("Cross-label join (SRC→EA)",
         "MATCH (f:SRC_Function)-[:SRC_IMPLEMENTS_EA]->(e:EA_Function) RETURN count(*) AS cnt"),
        ("Aggregation (all labels)",
         "MATCH (n) RETURN labels(n)[0] AS lbl, count(n) AS c ORDER BY c DESC"),
        ("Node count",
         "MATCH (n) RETURN count(n)"),
        ("Relationship count",
         "MATCH ()-[r]->() RETURN count(r)"),
    ]

    results = []
    progress = st.progress(0, text="Running benchmarks...")
    for i, (name, cypher) in enumerate(benchmarks):
        try:
            _, elapsed = timed_query(cypher)
            results.append({"Query": name, "Time (ms)": round(elapsed, 1), "Status": "OK"})
        except Exception as e:
            results.append({"Query": name, "Time (ms)": None, "Status": f"Error: {e}"})
        progress.progress((i + 1) / len(benchmarks), text=f"Running: {name}")
    progress.empty()

    import pandas as pd
    df = pd.DataFrame(results)

    # Color code
    st.dataframe(df, width='stretch', hide_index=True)

    ok_df = df[df["Status"] == "OK"]
    if not ok_df.empty:
        col1, col2, col3 = st.columns(3)
        times = ok_df["Time (ms)"]
        col1.metric("Fastest", f"{times.min():.1f}ms")
        col2.metric("Median", f"{times.median():.1f}ms")
        col3.metric("Slowest", f"{times.max():.1f}ms")


def section_constraints():
    """List uniqueness constraints."""
    st.header("Constraints")

    rows, ms = timed_query("""
        SHOW CONSTRAINTS
        YIELD name, type, labelsOrTypes, properties
        RETURN name, type, labelsOrTypes, properties
        ORDER BY name
    """)
    if not rows:
        st.info("No constraints found.")
        return

    st.caption(f"{len(rows)} constraints | Query: {ms:.0f}ms")

    import pandas as pd
    df = pd.DataFrame(rows)
    df["labelsOrTypes"] = df["labelsOrTypes"].apply(lambda x: ", ".join(x) if x else "")
    df["properties"] = df["properties"].apply(lambda x: ", ".join(x) if x else "")
    st.dataframe(df, width='stretch', hide_index=True)


def section_connected_components():
    """Check for disconnected subgraphs (sampling-based)."""
    st.header("Connectivity Check")

    st.caption("Samples random nodes and checks if they can reach the majority of the graph.")

    # Get total node count first
    total_rows, _ = timed_query("MATCH (n) RETURN count(n) AS cnt")
    total = total_rows[0]["cnt"] if total_rows else 0

    if total == 0:
        st.info("Empty graph.")
        return

    # Check orphan ratio
    orphan_rows, ms = timed_query("""
        MATCH (n) WHERE NOT (n)--()
        RETURN count(n) AS orphans
    """)
    orphans = orphan_rows[0]["orphans"] if orphan_rows else 0
    connected = total - orphans

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Nodes", f"{total:,}")
    col2.metric("Connected (≥1 edge)", f"{connected:,}")
    col3.metric("Orphans (0 edges)", f"{orphans:,}")

    pct = connected / total * 100 if total else 0
    st.progress(pct / 100, text=f"{pct:.1f}% of nodes have at least one edge")

    if orphans > 0:
        # Show which labels have orphans
        orphan_detail, _ = timed_query("""
            MATCH (n) WHERE NOT (n)--()
            WITH labels(n) AS lbls UNWIND lbls AS label
            RETURN label, count(*) AS count ORDER BY count DESC
            LIMIT 10
        """)
        if orphan_detail:
            import pandas as pd
            st.subheader("Orphan breakdown (top 10 labels)")
            st.dataframe(pd.DataFrame(orphan_detail), width='stretch', hide_index=True)


# ---------------------------------------------------------------------------
# PERFORMANCE ANALYTICS sections
# ---------------------------------------------------------------------------

def section_index_gap_detection():
    """PROFILE common query patterns and detect full scans vs index seeks."""
    st.header("Index Gap Detection")
    st.caption(
        "Runs PROFILE on common query patterns used by the search/retrieval layer. "
        "Detects whether each query uses an **index seek** (fast) or a "
        "**full label scan** (slow — needs an index)."
    )

    # These mirror the actual query patterns from search_service.py
    test_queries = [
        {
            "name": "SRC_Function by name",
            "cypher": "MATCH (n:SRC_Function {name: 'Adc_Init'}) RETURN n LIMIT 1",
            "label": "SRC_Function", "property": "name",
            "expected_index": "idx_SRC_Function_name",
        },
        {
            "name": "SRC_Function by module",
            "cypher": "MATCH (n:SRC_Function) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "SRC_Function", "property": "module",
            "expected_index": "idx_SRC_Function_module",
        },
        {
            "name": "EA_Function by name",
            "cypher": "MATCH (n:EA_Function {name: 'Adc_Init'}) RETURN n LIMIT 1",
            "label": "EA_Function", "property": "name",
            "expected_index": "idx_EA_Function_name",
        },
        {
            "name": "SFR_Register by module",
            "cypher": "MATCH (n:SFR_Register) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "SFR_Register", "property": "module",
            "expected_index": "idx_SFR_Register_module",
        },
        {
            "name": "SFR_BitField by module",
            "cypher": "MATCH (n:SFR_BitField) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "SFR_BitField", "property": "module",
            "expected_index": "idx_SFR_BitField_module",
        },
        {
            "name": "SFR_BaseAddress by module (largest label)",
            "cypher": "MATCH (n:SFR_BaseAddress) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "SFR_BaseAddress", "property": "module",
            "expected_index": "idx_SFR_BaseAddress_module",
        },
        {
            "name": "EA_Register by name",
            "cypher": "MATCH (n:EA_Register {name: 'DMA_CH0'}) RETURN n LIMIT 1",
            "label": "EA_Register", "property": "name",
            "expected_index": "idx_EA_Register_name",
        },
        {
            "name": "ProductRequirement by module",
            "cypher": "MATCH (n:ProductRequirement) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "ProductRequirement", "property": "module",
            "expected_index": "idx_ProductRequirement_module",
        },
        {
            "name": "Cross-join: SRC_Function→SFR_Register by module",
            "cypher": "MATCH (f:SRC_Function)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) WHERE f.module = 'ADC' RETURN count(*)",
            "label": "SRC_Function", "property": "module",
            "expected_index": "idx_SRC_Function_module",
        },
        {
            "name": "Label-less module scan (unavoidable full scan)",
            "cypher": "MATCH (n) WHERE n.module = 'ADC' RETURN count(n)",
            "label": "(none)", "property": "module",
            "expected_index": "(cannot index — no label)",
        },
    ]

    import pandas as pd

    results = []
    progress = st.progress(0, text="Profiling queries...")
    for i, tq in enumerate(test_queries):
        try:
            p = profile_query(tq["cypher"])
            results.append({
                "Query": tq["name"],
                "Label": tq["label"],
                "Property": tq["property"],
                "Time (ms)": round(p["elapsed_ms"], 1),
                "Rows Scanned": p["plan_rows_scanned"],
                "Result Rows": p["result_rows"],
                "db_hits": p["total_db_hits"],
                "Index Used": "Yes" if p["has_index_seek"] else "No",
                "Full Scan": "YES" if p["has_full_scan"] else "No",
                "Expected Index": tq["expected_index"],
            })
        except Exception as e:
            results.append({
                "Query": tq["name"], "Label": tq["label"],
                "Property": tq["property"], "Time (ms)": None,
                "Rows Scanned": None, "Result Rows": None,
                "db_hits": None, "Index Used": "?",
                "Full Scan": f"Error: {e}", "Expected Index": tq["expected_index"],
            })
        progress.progress((i + 1) / len(test_queries))
    progress.empty()

    df = pd.DataFrame(results)

    # Summary metrics
    full_scans = df[df["Full Scan"] == "YES"]
    indexed = df[df["Index Used"] == "Yes"]
    col1, col2, col3 = st.columns(3)
    col1.metric("Queries with Index Seek", len(indexed), help="Fast path — index used")
    col2.metric("Full Scans Detected", len(full_scans),
                delta=f"-{len(full_scans)}" if len(full_scans) > 0 else None,
                delta_color="inverse",
                help="Slow path — missing index or label-less query")
    col3.metric("Total Queries Tested", len(df))

    # Highlight full scans
    st.dataframe(df, width='stretch', hide_index=True)

    if len(full_scans) > 0:
        st.subheader("Recommendations")
        for _, row in full_scans.iterrows():
            if row["Label"] == "(none)":
                st.info(f"**{row['Query']}**: Label-less query — cannot be indexed. "
                        "Consider rewriting to iterate specific labels.")
            else:
                st.warning(
                    f"**{row['Query']}**: Full scan on `:{row['Label']}` "
                    f"filtering by `{row['Property']}`. "
                    f"**Fix**: `CREATE INDEX {row['Expected Index']} IF NOT EXISTS "
                    f"FOR (n:{row['Label']}) ON (n.{row['Property']})`"
                )


def section_index_coverage():
    """Analyze which properties are indexed vs which are commonly queried."""
    st.header("Index Coverage Analysis")
    st.caption(
        "Compares existing indexes against the properties commonly "
        "filtered on by the search layer."
    )

    import pandas as pd

    # Fetch existing indexes (exclude LOOKUP type)
    idx_rows, _ = timed_query("""
        SHOW INDEXES
        YIELD name, type, labelsOrTypes, properties, state
        WHERE type <> 'LOOKUP'
        RETURN labelsOrTypes[0] AS label, properties[0] AS property,
               name, type, state
        ORDER BY label, property
    """)
    existing = set()
    for r in idx_rows:
        existing.add((r["label"], r["property"]))

    # Properties that SHOULD be indexed (based on search_service.py patterns)
    needed = [
        ("SRC_Function", "name"), ("SRC_Function", "module"),
        ("SRC_GlobalVariable", "module"), ("SRC_DataType", "module"),
        ("SRC_Macro", "module"), ("SRC_SourceFile", "module"),
        ("SRC_LocalVariable", "module"),
        ("SFR_Register", "name"), ("SFR_Register", "module"),
        ("SFR_BitField", "name"), ("SFR_BitField", "module"),
        ("SFR_BaseAddress", "module"), ("SFR_File", "module"),
        ("EA_Function", "name"), ("EA_Function", "module"),
        ("EA_Register", "name"), ("EA_Register", "module"),
        ("EA_DataType", "name"), ("EA_DataType", "module"),
        ("EA_Requirement", "module"), ("EA_DesignDecision", "module"),
        ("EA_ConfigParameter", "module"),
        ("ProductRequirement", "module"),
        ("StakeholderRequirement", "module"),
        ("TS_FunctionalTestCase", "module"),
        ("TS_ConfigTestCase", "module"),
    ]

    rows = []
    for label, prop in needed:
        has_idx = (label, prop) in existing
        rows.append({
            "Label": label,
            "Property": prop,
            "Indexed": "Yes" if has_idx else "MISSING",
            "Create Statement": "" if has_idx else
                f"CREATE INDEX idx_{label}_{prop} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})",
        })

    df = pd.DataFrame(rows)
    missing = df[df["Indexed"] == "MISSING"]

    col1, col2, col3 = st.columns(3)
    col1.metric("Required Indexes", len(needed))
    col2.metric("Present", len(needed) - len(missing))
    col3.metric("Missing", len(missing),
                delta=f"-{len(missing)}" if len(missing) > 0 else None,
                delta_color="inverse")

    # Coverage percentage
    pct = (len(needed) - len(missing)) / len(needed) * 100 if needed else 100
    st.progress(pct / 100, text=f"Index coverage: {pct:.0f}%")

    st.dataframe(df, width='stretch', hide_index=True)

    if len(missing) > 0:
        st.subheader("Create Missing Indexes")
        st.caption("Run these statements in Neo4j Browser or via the Query Inspector below.")
        stmts = "\n".join(r["Create Statement"] for _, r in missing.iterrows())
        st.code(stmts, language="cypher")

        if st.button("Create all missing indexes now", type="primary"):
            created = 0
            for _, r in missing.iterrows():
                try:
                    run_query(r["Create Statement"])
                    created += 1
                except Exception as e:
                    st.error(f"Failed: {r['Create Statement']} — {e}")
            if created:
                st.success(f"Created {created} indexes. Refresh the page to see updated status.")
                st.rerun()


def section_query_plan_inspector():
    """Interactive query plan inspector — paste any Cypher and see PROFILE output."""
    st.header("Query Plan Inspector")
    st.caption(
        "Enter a Cypher query to see its execution plan, db hits, rows scanned, "
        "and whether it uses an index. Uses PROFILE for accurate measurement."
    )

    default_query = "MATCH (n:SRC_Function) WHERE n.module = 'ADC' RETURN n.name LIMIT 10"
    cypher = st.text_area("Cypher Query", value=default_query, height=100)

    if st.button("Run PROFILE", type="primary"):
        if not cypher.strip():
            st.warning("Enter a query first.")
            return

        try:
            p = profile_query(cypher.strip())
        except Exception as e:
            st.error(f"Query error: {e}")
            return

        import pandas as pd

        # Summary
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Time", f"{p['elapsed_ms']:.1f}ms")
        col2.metric("db_hits", f"{p['total_db_hits']:,}")
        col3.metric("Rows Scanned", f"{p['plan_rows_scanned']:,}")
        col4.metric("Result Rows", f"{p['result_rows']:,}")

        # Verdict
        if p["has_full_scan"] and not p["has_index_seek"]:
            st.error("FULL SCAN — no index used. This query scans every node of the label.")
        elif p["has_full_scan"] and p["has_index_seek"]:
            st.warning("MIXED — some operators use index, others do full scan.")
        elif p["has_index_seek"]:
            st.success("INDEX SEEK — fast path, index used.")
        else:
            st.info("No scan or seek detected (may be a metadata/count query).")

        # Execution plan tree
        st.subheader("Execution Plan")
        plan_rows = []
        for op in p["operators"]:
            indent = "  " * op["depth"] + "└─ " if op["depth"] > 0 else ""
            plan_rows.append({
                "Operator": indent + op["operator"],
                "db_hits": op["db_hits"],
                "Rows": op["rows"],
            })
        plan_df = pd.DataFrame(plan_rows)
        st.dataframe(plan_df, width='stretch', hide_index=True)

        # Efficiency ratio
        if p["plan_rows_scanned"] > 0 and p["result_rows"] > 0:
            ratio = p["plan_rows_scanned"] / p["result_rows"]
            st.caption(
                f"Scan efficiency: scanned {p['plan_rows_scanned']:,} rows "
                f"to produce {p['result_rows']:,} results "
                f"(ratio: {ratio:.1f}x — closer to 1.0 is better)"
            )


def section_query_telemetry():
    """Display live query telemetry from the SearchService query log."""
    st.header("Query Telemetry (Live)")
    st.caption(
        "Shows real queries executed by the SearchService layer "
        "(MCP tools, Copilot Chat, etc.). Data comes from the JSONL query log "
        "written by the instrumented search_service.py."
    )

    import pandas as pd
    from datetime import datetime

    # Resolve the query log path
    _hybridrag = Path(__file__).resolve().parents[1]
    log_file = _hybridrag / "logs" / "query_log.jsonl"

    if not log_file.exists():
        st.info(
            f"No query log found at `{log_file}`.\n\n"
            "Run some searches via MCP tools or Copilot Chat to generate data. "
            "The log is written automatically by the instrumented SearchService."
        )
        return

    # Read log
    records = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue

    if not records:
        st.info("Query log is empty.")
        return

    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["ts"], unit="s")
    df["time_str"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # ── Per-workspace query counts ───────────────────────────────────
    if "profile" in df.columns:
        ws_counts = df["profile"].value_counts().to_dict()
        cols = st.columns(len(ws_counts) + 1)
        cols[0].metric("All Workspaces", f"{len(df):,}")
        for i, (ws, cnt) in enumerate(sorted(ws_counts.items()), 1):
            cols[i].metric(f"{ws}", f"{cnt:,}")
        st.divider()

    # ── Filter by sidebar Neo4j profile ──────────────────────────────
    current_profile = st.session_state.get("profile", "")
    if "profile" in df.columns and current_profile:
        filtered = df[df["profile"] == current_profile]
        if filtered.empty:
            st.info(f"No telemetry records for profile **{current_profile}**. Showing all workspaces below.")
        else:
            df = filtered

    # ── Summary metrics ──────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Queries Logged", f"{len(df):,}")
    col2.metric("Unique Methods", df["method"].nunique())
    errors = df[df["error"].notna() & (df["error"] != "")]
    col3.metric("Errors", len(errors))
    if not df.empty:
        span_min = (df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 60
        col4.metric("Log Span", f"{span_min:.0f} min" if span_min < 120 else f"{span_min / 60:.1f} hr")

    # ── Latency distribution ─────────────────────────────────────────
    st.subheader("Latency Distribution")
    latencies = df["elapsed_ms"].dropna()
    if not latencies.empty:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Min", f"{latencies.min():.1f}ms")
        col2.metric("P50", f"{latencies.median():.1f}ms")
        col3.metric("P95", f"{latencies.quantile(0.95):.1f}ms")
        col4.metric("P99", f"{latencies.quantile(0.99):.1f}ms")
        col5.metric("Max", f"{latencies.max():.1f}ms")

        # Histogram
        import numpy as np
        bins = np.logspace(np.log10(max(latencies.min(), 0.1)), np.log10(latencies.max() + 1), 30)
        hist_df = pd.cut(latencies, bins=bins).value_counts().sort_index().reset_index()
        hist_df.columns = ["bin", "count"]
        hist_df["bin"] = hist_df["bin"].astype(str)
        st.bar_chart(hist_df.set_index("bin")["count"], height=200)
    else:
        st.info("No latency data available.")

    # ── Queries by method ────────────────────────────────────────────
    st.subheader("Queries by Method")
    method_stats = df.groupby("method").agg(
        count=("elapsed_ms", "size"),
        avg_ms=("elapsed_ms", "mean"),
        p95_ms=("elapsed_ms", lambda x: x.quantile(0.95)),
        max_ms=("elapsed_ms", "max"),
        total_rows=("row_count", "sum"),
    ).reset_index()
    method_stats = method_stats.sort_values("count", ascending=False)
    for col in ["avg_ms", "p95_ms", "max_ms"]:
        method_stats[col] = method_stats[col].round(1)
    st.dataframe(method_stats, width='stretch', hide_index=True)

    # ── Slowest queries ──────────────────────────────────────────────
    st.subheader("Top 20 Slowest Queries")
    slow = df.nlargest(20, "elapsed_ms")[
        ["time_str", "method", "cypher", "elapsed_ms", "row_count", "module", "error"]
    ].copy()
    slow["elapsed_ms"] = slow["elapsed_ms"].round(1)
    slow.columns = ["Time", "Method", "Cypher", "Latency (ms)", "Rows", "Module", "Error"]
    st.dataframe(slow, width='stretch', hide_index=True)

    # ── Queries over time ────────────────────────────────────────────
    st.subheader("Query Volume Over Time")
    if len(df) > 1:
        df["minute"] = df["timestamp"].dt.floor("1min")
        vol = df.groupby("minute").agg(
            queries=("elapsed_ms", "size"),
            avg_ms=("elapsed_ms", "mean"),
        ).reset_index()
        vol["avg_ms"] = vol["avg_ms"].round(1)

        col1, col2 = st.columns(2)
        with col1:
            st.caption("Queries per minute")
            st.line_chart(vol.set_index("minute")["queries"], height=200)
        with col2:
            st.caption("Avg latency per minute (ms)")
            st.line_chart(vol.set_index("minute")["avg_ms"], height=200)

    # ── Module breakdown ─────────────────────────────────────────────
    modules_in_log = df["module"].dropna().unique()
    if len(modules_in_log) > 0:
        st.subheader("Queries by Module")
        mod_stats = df[df["module"].notna()].groupby("module").agg(
            count=("elapsed_ms", "size"),
            avg_ms=("elapsed_ms", "mean"),
        ).reset_index().sort_values("count", ascending=False)
        mod_stats["avg_ms"] = mod_stats["avg_ms"].round(1)
        st.dataframe(mod_stats, width='stretch', hide_index=True)

    # ── Recent queries (live tail) ───────────────────────────────────
    st.subheader("Recent Queries (last 50)")
    recent = df.tail(50).iloc[::-1][
        ["time_str", "method", "cypher", "elapsed_ms", "row_count", "module"]
    ].copy()
    recent["elapsed_ms"] = recent["elapsed_ms"].round(1)
    recent.columns = ["Time", "Method", "Cypher", "Latency (ms)", "Rows", "Module"]
    st.dataframe(recent, width='stretch', hide_index=True)

    # ── Log management ───────────────────────────────────────────────
    st.caption(f"Log file: `{log_file}` ({log_file.stat().st_size / 1024:.1f} KB, {len(df)} records)")
    if st.button("Clear query log"):
        log_file.unlink()
        st.success("Query log cleared. Refresh to see updated state.")
        st.rerun()


def collect_dashboard_snapshot() -> dict:
    """Collect all dashboard data into a single dict for JSON export."""
    snapshot = {
        "exported_at": datetime.now().isoformat(),
        "profile": st.session_state.profile,
    }

    # Overview
    try:
        rows, _ = timed_query("MATCH (n) RETURN count(n) AS cnt")
        snapshot["total_nodes"] = rows[0]["cnt"] if rows else 0
        rows, _ = timed_query("MATCH ()-[r]->() RETURN count(r) AS cnt")
        snapshot["total_relationships"] = rows[0]["cnt"] if rows else 0
    except Exception:
        pass

    # Node distribution
    try:
        rows, _ = timed_query(
            "MATCH (n) WITH labels(n) AS lbls UNWIND lbls AS label "
            "RETURN label, count(*) AS count ORDER BY count DESC"
        )
        snapshot["node_distribution"] = rows
    except Exception:
        pass

    # Relationship distribution
    try:
        rows, _ = timed_query(
            "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(*) AS count ORDER BY count DESC"
        )
        snapshot["relationship_distribution"] = rows
    except Exception:
        pass

    # Module breakdown
    try:
        rows, _ = timed_query(
            "MATCH (n) WHERE n.module IS NOT NULL "
            "WITH labels(n) AS lbls, n.module AS module UNWIND lbls AS label "
            "RETURN module, label, count(*) AS count ORDER BY module, count DESC"
        )
        snapshot["module_breakdown"] = rows
    except Exception:
        pass

    # Indexes
    try:
        rows, _ = timed_query(
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state, populationPercent "
            "RETURN name, type, labelsOrTypes, properties, state, populationPercent ORDER BY type, name"
        )
        for r in rows:
            r["labelsOrTypes"] = list(r.get("labelsOrTypes") or [])
            r["properties"] = list(r.get("properties") or [])
        snapshot["indexes"] = rows
    except Exception:
        pass

    # Constraints
    try:
        rows, _ = timed_query(
            "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
            "RETURN name, type, labelsOrTypes, properties ORDER BY name"
        )
        for r in rows:
            r["labelsOrTypes"] = list(r.get("labelsOrTypes") or [])
            r["properties"] = list(r.get("properties") or [])
        snapshot["constraints"] = rows
    except Exception:
        pass

    # Orphan counts
    try:
        rows, _ = timed_query(
            "MATCH (n) WHERE NOT (n)--() WITH labels(n) AS lbls UNWIND lbls AS label "
            "RETURN label, count(*) AS orphan_count ORDER BY orphan_count DESC"
        )
        snapshot["orphan_nodes"] = rows
    except Exception:
        pass

    # Degree stats
    try:
        rows, _ = timed_query(
            "MATCH (n) WITH size([(n)--() | 1]) AS degree "
            "RETURN min(degree) AS min_deg, max(degree) AS max_deg, "
            "avg(degree) AS avg_deg, percentileCont(degree, 0.5) AS median_deg, "
            "percentileCont(degree, 0.95) AS p95_deg"
        )
        if rows:
            snapshot["degree_stats"] = rows[0]
    except Exception:
        pass

    # Query telemetry
    _hybridrag = Path(__file__).resolve().parents[1]
    log_file = _hybridrag / "logs" / "query_log.jsonl"
    if log_file.exists():
        records = []
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
        if records:
            latencies = [r["elapsed_ms"] for r in records if r.get("elapsed_ms") is not None]
            latencies.sort()
            snapshot["query_telemetry"] = {
                "total_queries": len(records),
                "latency_min_ms": round(latencies[0], 2) if latencies else None,
                "latency_p50_ms": round(latencies[len(latencies) // 2], 2) if latencies else None,
                "latency_p95_ms": round(latencies[int(len(latencies) * 0.95)], 2) if latencies else None,
                "latency_max_ms": round(latencies[-1], 2) if latencies else None,
                "queries": records,
            }

    return snapshot


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="KG Health Dashboard",
        page_icon="🔬",
        layout="wide",
    )

    # Parse profile
    if "profile" not in st.session_state:
        st.session_state.profile = _parse_profile()

    st.title("Knowledge Graph Health Dashboard")

    # Profile selector in sidebar
    with st.sidebar:
        st.header("Connection")
        cfg = load_yaml_with_env(STORAGE_CONFIG_PATH)
        profiles = list(cfg.get("neo4j", {}).keys())
        current = st.session_state.profile
        idx = profiles.index(current) if current in profiles else 0
        selected = st.selectbox("Neo4j Profile", profiles, index=idx)
        if selected != st.session_state.profile:
            st.session_state.profile = selected
            get_driver.clear()  # reset cached driver
            st.rerun()

        _, db, desc = get_driver(st.session_state.profile)
        st.success(f"Connected to **{st.session_state.profile}**")
        st.caption(desc)
        st.caption(f"Database: `{db}`")

        st.divider()
        st.header("Export")
        if st.button("Export All to JSON", type="primary"):
            snapshot = collect_dashboard_snapshot()
            json_str = json.dumps(snapshot, indent=2, default=str)
            st.download_button(
                label="Download JSON",
                data=json_str,
                file_name=f"kg_snapshot_{st.session_state.profile}_{datetime.now():%Y%m%d_%H%M%S}.json",
                mime="application/json",
            )

        st.divider()
        st.header("Sections")

        # Live / performance sections first
        st.caption("**Live & Performance**")
        live_sections = {
            "query_telemetry": "Query Telemetry (Live)",
            "plan_inspector": "Query Plan Inspector",
            "benchmark": "Query Benchmark",
            "index_gaps": "Index Gap Detection",
            "index_coverage": "Index Coverage Analysis",
        }
        # Statistical / structural sections second
        st.caption("**Graph Structure**")
        stat_sections = {
            "overview": "Graph Overview",
            "nodes": "Node Distribution",
            "edges": "Relationship Distribution",
            "modules": "Module Breakdown",
            "degrees": "Degree Distribution",
            "indexes": "Index Health",
            "constraints": "Constraints",
            "orphans": "Orphan Nodes",
            "fill": "Property Fill Rates",
            "connectivity": "Connectivity Check",
        }
        sections = {**live_sections, **stat_sections}

        selected_sections = []
        for key, label in live_sections.items():
            if st.checkbox(label, value=True, key=f"sec_{key}"):
                selected_sections.append(key)
        for key, label in stat_sections.items():
            if st.checkbox(label, value=True, key=f"sec_{key}"):
                selected_sections.append(key)

    # Render selected sections
    section_map = {
        "query_telemetry": section_query_telemetry,
        "plan_inspector": section_query_plan_inspector,
        "benchmark": section_query_benchmark,
        "index_gaps": section_index_gap_detection,
        "index_coverage": section_index_coverage,
        "overview": section_overview,
        "nodes": section_node_distribution,
        "edges": section_edge_distribution,
        "modules": section_module_breakdown,
        "degrees": section_degree_distribution,
        "indexes": section_index_health,
        "constraints": section_constraints,
        "orphans": section_orphan_nodes,
        "fill": section_property_fill,
        "connectivity": section_connected_components,
    }

    for key in selected_sections:
        if key in section_map:
            try:
                section_map[key]()
            except Exception as e:
                st.error(f"Error in {sections.get(key, key)}: {e}")
            st.divider()


if __name__ == "__main__":
    main()

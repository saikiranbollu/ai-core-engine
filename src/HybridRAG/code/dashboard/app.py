"""
AICE Knowledge Graph — Cross-Silo Inconsistency Detector
==========================================================
Finds real bugs that no single ALM tool can detect.

Launch:
    streamlit run src/HybridRAG/code/dashboard/app.py -- --profile mcal
    streamlit run src/HybridRAG/code/dashboard/app.py -- --profile local

All analysis is deterministic graph traversal — zero LLM, zero probability.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Path setup — ensure dashboard package imports work
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

_CODE_DIR = _THIS_DIR.parent
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from pages import inconsistency_detector  # noqa: E402
from db import get_neo4j_driver  # noqa: E402

# ---------------------------------------------------------------------------
# CLI args (passed after -- in streamlit command)
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(description="KG Dashboard")
    parser.add_argument("--profile", default="mcal",
                        help="Neo4j profile: mcal, illd, or local (default: mcal)")
    args, _ = parser.parse_known_args()
    return args


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Cross-Silo Inconsistency Detector",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Parse profile
# ---------------------------------------------------------------------------
args = _parse_args()
profile = args.profile

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("AICE Dashboard")
    st.caption(f"Profile: **{profile}**")
    st.divider()

    # Connection status
    try:
        driver = get_neo4j_driver(profile)
        driver.verify_connectivity()
        st.success("Neo4j connected")
    except Exception as e:
        st.error(f"Neo4j: {e}")

    st.divider()
    st.markdown(
        "**What is this?**\n\n"
        "This tool runs automated audits across "
        "EA architecture, source code, test specs, "
        "and Polarion requirements — all stitched "
        "together in a knowledge graph.\n\n"
        "These findings are **invisible** to any "
        "single ALM tool because they span multiple "
        "artifact silos."
    )

# ---------------------------------------------------------------------------
# Main content — single focused page
# ---------------------------------------------------------------------------
inconsistency_detector.render(profile)

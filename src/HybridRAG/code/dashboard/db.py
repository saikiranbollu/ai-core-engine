"""
Neo4j connection helper for the KG Dashboard.

Reuses the project's env_config.py for credential resolution and
storage_config.yaml for connection settings. Falls back to env vars
(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD) for local profiles.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup — make project imports work regardless of cwd
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_CODE_DIR = _THIS_DIR.parent                       # .../HybridRAG/code
_HYBRIDRAG_DIR = _CODE_DIR.parent                  # .../HybridRAG
_CONFIG_DIR = _HYBRIDRAG_DIR / "config"
_STORAGE_CFG_PATH = _CONFIG_DIR / "storage_config.yaml"

if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from env_config import load_yaml_with_env  # noqa: E402
from neo4j import GraphDatabase            # noqa: E402

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_drivers: Dict[str, Any] = {}


def get_neo4j_driver(profile: str = "mcal"):
    """Return a cached Neo4j driver for *profile*.

    Resolution order:
      1. storage_config.yaml  (profile key in neo4j section)
      2. NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD env vars (fallback)
    """
    if profile in _drivers:
        return _drivers[profile]

    cfg = load_yaml_with_env(_STORAGE_CFG_PATH)
    neo4j_section = cfg.get("neo4j", {})

    if profile in neo4j_section:
        pcfg = neo4j_section[profile]
        uri = pcfg["uri"]
        auth = (pcfg["username"], pcfg["password"])
        pool_kw = {
            "max_connection_pool_size": pcfg.get("max_connection_pool_size", 50),
            "max_connection_lifetime": pcfg.get("max_connection_lifetime", 3600),
        }
    else:
        # Fallback: env vars (covers --profile local)
        # Local Neo4j uses LOCAL_NEO4J_PASSWORD; fall back to NEO4J_PASSWORD
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        password = os.environ.get("LOCAL_NEO4J_PASSWORD") or os.environ.get("NEO4J_PASSWORD", "")
        auth = (
            os.environ.get("NEO4J_USERNAME", "neo4j"),
            password,
        )
        pool_kw = {"max_connection_pool_size": 50, "max_connection_lifetime": 3600}

    driver = GraphDatabase.driver(uri, auth=auth, **pool_kw)
    _drivers[profile] = driver
    return driver


def run_cypher(query: str, params: Optional[Dict] = None,
               profile: str = "mcal", database: str = "neo4j") -> List[Dict]:
    """Execute a read-only Cypher query, return list of row dicts."""
    driver = get_neo4j_driver(profile)
    with driver.session(database=database) as session:
        return [dict(record) for record in session.run(query, params or {})]


def get_module_list(profile: str = "mcal") -> List[str]:
    """Return list of MCAL module names from MCALModule nodes."""
    rows = run_cypher(
        "MATCH (m:MCALModule) RETURN m.module_name AS name ORDER BY name",
        profile=profile,
    )
    return [r["name"] for r in rows if r.get("name")]

"""
One-shot script to create V-Model bridge edges in Neo4j for the MCAL profile.

Creates:
  - IMPLEMENTS: ProductRequirement → SWA/SWUD (reverse of *_TRACES_TO)
  - TRACES_TO:  SWA/SWUD → TS test cases  (reverse of TS_VALIDATES_*)

These edges enable the MCP traceability tools:
  build_traceability_matrix, find_coverage_gaps, get_coverage_report, find_requirement_traces
"""
import os
import sys
import yaml
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from env_config import load_yaml_with_env

STORAGE_CFG = Path(__file__).resolve().parent.parent.parent / "config" / "storage_config.yaml"


def main():
    if len(sys.argv) < 2:
        print("Usage: python create_vmodel_bridges.py <MODULE>")
        print("  Example: python create_vmodel_bridges.py ADC")
        sys.exit(1)
    module = sys.argv[1].upper()

    raw = load_yaml_with_env(STORAGE_CFG)

    neo = raw["neo4j"]["mcal"]
    uri = neo["uri"]
    user = neo.get("username", "")
    pw = neo.get("password", "")
    db = neo.get("database", "neo4j")

    print(f"Connecting to {uri} db={db} ...")
    drv_kw = dict(auth=(user, pw))
    if "+s" not in uri.split("://")[0]:
        drv_kw["encrypted"] = neo.get("encrypted", False)
    drv = GraphDatabase.driver(uri, **drv_kw)
    drv.verify_connectivity()
    print("Connected!\n")

    def write(cypher, params=None):
        with drv.session(database=db) as s:
            s.execute_write(lambda tx: tx.run(cypher, params or {}))

    def read_count(cypher, params=None):
        with drv.session(database=db) as s:
            return s.run(cypher, params or {}).single()["c"]

    # ── 1. IMPLEMENTS: PRQ → SWA (reverse of ARCHITECTURALLY_REALIZES) ──
    print("Creating IMPLEMENTS (PRQ → SWA via ARCHITECTURALLY_REALIZES)...")
    write(
        "MATCH (swa)-[:ARCHITECTURALLY_REALIZES]->(prq:ProductRequirement) "
        "WHERE swa.module = $module "
        'MERGE (prq)-[:IMPLEMENTS {source: "bridge", derived_from: "ARCHITECTURALLY_REALIZES"}]->(swa)',
        {"module": module},
    )

    # ── 1b. IMPLEMENTS: PRQ → SWA_ArchDecision ───────────────────────
    print("Creating IMPLEMENTS (PRQ → SWA via SWA_ARCH_DECISION_TRACES_TO)...")
    write(
        "MATCH (swa)-[:SWA_ARCH_DECISION_TRACES_TO]->(prq:ProductRequirement) "
        'MERGE (prq)-[:IMPLEMENTS {source: "bridge", derived_from: "SWA_ARCH_DECISION_TRACES_TO"}]->(swa)',
        {},
    )

    # ── 1c. IMPLEMENTS: PRQ → SWA_Config* ────────────────────────────
    print("Creating IMPLEMENTS (PRQ → SWA via SWA_CONFIG_TRACES_TO)...")
    write(
        "MATCH (swa)-[:SWA_CONFIG_TRACES_TO]->(prq:ProductRequirement) "
        'MERGE (prq)-[:IMPLEMENTS {source: "bridge", derived_from: "SWA_CONFIG_TRACES_TO"}]->(swa)',
        {},
    )

    n1 = read_count(
        "MATCH (prq:ProductRequirement)-[r:IMPLEMENTS]->(swa) "
        "WHERE any(l IN labels(swa) WHERE l STARTS WITH 'SWA_') "
        "RETURN count(r) AS c"
    )
    print(f"  → {n1} SWA IMPLEMENTS edges")

    # ── 2. IMPLEMENTS: PRQ → SWUD (reverse of SWUD_TRACES_TO) ────────
    print("Creating IMPLEMENTS (PRQ → SWUD)...")
    write(
        "MATCH (swud)-[:SWUD_TRACES_TO]->(prq:ProductRequirement) "
        "WHERE swud.module = $module "
        'MERGE (prq)-[:IMPLEMENTS {source: "bridge", derived_from: "SWUD_TRACES_TO"}]->(swud)',
        {"module": module},
    )
    n2 = read_count(
        "MATCH ()-[r:IMPLEMENTS {derived_from: 'SWUD_TRACES_TO'}]->() "
        "RETURN count(r) AS c"
    )
    print(f"  → {n2} edges")

    # ── 3. TRACES_TO: SWA → TS (reverse of TS_VALIDATES_SWA) ────────
    print("Creating TRACES_TO (SWA → TS)...")
    write(
        "MATCH (ts)-[:TS_VALIDATES_SWA]->(swa) "
        "WHERE ts.module = $module "
        'MERGE (swa)-[:TRACES_TO {source: "bridge", derived_from: "TS_VALIDATES_SWA"}]->(ts)',
        {"module": module},
    )
    n3 = read_count(
        "MATCH ()-[r:TRACES_TO {derived_from: 'TS_VALIDATES_SWA'}]->() "
        "RETURN count(r) AS c"
    )
    print(f"  → {n3} edges")

    # ── 4. TRACES_TO: SWUD → TS (reverse of TS_VALIDATES_SWUD) ──────
    print("Creating TRACES_TO (SWUD → TS)...")
    write(
        "MATCH (ts)-[:TS_VALIDATES_SWUD]->(swud) "
        "WHERE ts.module = $module "
        'MERGE (swud)-[:TRACES_TO {source: "bridge", derived_from: "TS_VALIDATES_SWUD"}]->(ts)',
        {"module": module},
    )
    n4 = read_count(
        "MATCH ()-[r:TRACES_TO {derived_from: 'TS_VALIDATES_SWUD'}]->() "
        "RETURN count(r) AS c"
    )
    print(f"  → {n4} edges")

    # ── Summary ──────────────────────────────────────────────────────
    total_impl = read_count("MATCH ()-[r:IMPLEMENTS]->() RETURN count(r) AS c")
    total_traces = read_count("MATCH ()-[r:TRACES_TO]->() RETURN count(r) AS c")

    print(f"\n{'='*50}")
    print(f"  V-Model Bridge Summary for {module}")
    print(f"{'='*50}")
    print(f"  IMPLEMENTS (PRQ→SWA):   {n1}")
    print(f"  IMPLEMENTS (PRQ→SWUD):  {n2}")
    print(f"  TRACES_TO  (SWA→TS):    {n3}")
    print(f"  TRACES_TO  (SWUD→TS):   {n4}")
    print(f"  ─────────────────────────")
    print(f"  Total IMPLEMENTS:       {total_impl}")
    print(f"  Total TRACES_TO:        {total_traces}")
    print(f"{'='*50}\n")

    drv.close()
    print("Done!")


if __name__ == "__main__":
    main()

"""
Backfill cross-module SRC_CALLS edges.

Reads call_edges.json from each module's temp directory and creates
SRC_CALLS edges where the callee exists as a SRC_Function in another module.

This fixes the ordering problem: modules ingested early couldn't create edges
to modules that didn't exist yet. Now that all modules are present, we can
MERGE all missing cross-module edges.

Usage:
    python src/HybridRAG/code/KG/backfill_cross_calls.py --profile mcal [-v]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[4]
KG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(KG_DIR))

from build_knowledge_graph import load_storage_config

from neo4j import GraphDatabase

logger = logging.getLogger("backfill_cross_calls")

HYBRIDRAG_DIR = PROJECT_ROOT / "src" / "HybridRAG"
TEMP_DIR = HYBRIDRAG_DIR / "temp"
BATCH_SIZE = 500


def load_module_data(module_dir: Path) -> tuple[str, list[dict], set[str]]:
    """Load call_edges.json and functions.json for a module."""
    module_name = module_dir.name.replace("src_", "").upper()
    # Handle multi-part names like src_can_17_mcmcan -> CAN_17_MCMCAN
    # The module property in Neo4j uses the exact module name from ingestion
    summary_file = module_dir / "summary.json"
    if summary_file.exists():
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        module_name = summary.get("module", module_name)

    call_edges_file = module_dir / "call_edges.json"
    functions_file = module_dir / "functions.json"

    if not call_edges_file.exists() or not functions_file.exists():
        return module_name, [], set()

    call_edges = json.loads(call_edges_file.read_text(encoding="utf-8"))
    functions = json.loads(functions_file.read_text(encoding="utf-8"))
    func_names = {f["name"] for f in functions}

    return module_name, call_edges, func_names


def get_cross_module_edges(call_edges: list[dict], own_func_names: set[str]) -> list[dict]:
    """Filter to only cross-module edges (callee not in own module)."""
    cross = []
    for edge in call_edges:
        if edge["callee_name"] not in own_func_names:
            cross.append({
                "caller_id": edge["caller_id"],
                "callee_name": edge["callee_name"],
                "call_order": edge.get("call_order", 0),
                "case_label": edge.get("case_label"),
            })
    return cross


def backfill_module(session, module: str, cross_edges: list[dict], dry_run: bool = False) -> int:
    """Create missing cross-module SRC_CALLS edges for one module."""
    if not cross_edges:
        return 0

    total_created = 0

    for i in range(0, len(cross_edges), BATCH_SIZE):
        chunk = cross_edges[i:i + BATCH_SIZE]

        if dry_run:
            # In dry-run, count how many would match
            result = session.run(
                """
                UNWIND $edges AS e
                MATCH (caller:SRC_Function {function_id: e.caller_id})
                MATCH (callee:SRC_Function {name: e.callee_name})
                WHERE callee.module <> $module
                  AND NOT EXISTS((caller)-[:SRC_CALLS]->(callee))
                RETURN count(*) AS cnt
                """,
                {"edges": chunk, "module": module},
            )
            total_created += result.single()["cnt"]
        else:
            # MERGE to avoid duplicates, only create if not existing
            result = session.run(
                """
                UNWIND $edges AS e
                MATCH (caller:SRC_Function {function_id: e.caller_id})
                MATCH (callee:SRC_Function {name: e.callee_name})
                WHERE callee.module <> $module
                MERGE (caller)-[r:SRC_CALLS]->(callee)
                ON CREATE SET r.call_order = e.call_order,
                              r.case_label = e.case_label,
                              r.cross_module = true
                RETURN count(r) AS cnt
                """,
                {"edges": chunk, "module": module},
            )
            total_created += result.single()["cnt"]

    return total_created


def main():
    parser = argparse.ArgumentParser(description="Backfill cross-module SRC_CALLS edges")
    parser.add_argument("--profile", choices=["mcal", "illd", "test", "local"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="Count edges without creating")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # Load storage config
    storage_cfg = load_storage_config()
    neo4j_cfg = storage_cfg["neo4j"][args.profile]
    uri = neo4j_cfg["uri"]
    user = neo4j_cfg["username"]
    password = neo4j_cfg["password"]

    logger.info("Connecting to Neo4j: %s (profile=%s)", uri, args.profile)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    logger.info("Connected.")

    # Find all module temp dirs
    module_dirs = sorted(TEMP_DIR.glob("src_*"))
    logger.info("Found %d module directories with cached call edges", len(module_dirs))

    # Get existing cross-module edge count
    with driver.session() as session:
        result = session.run(
            "MATCH ()-[r:SRC_CALLS {cross_module: true}]->() RETURN count(r) AS cnt"
        )
        existing = result.single()["cnt"]
        logger.info("Existing cross-module SRC_CALLS edges: %d", existing)

    # Process each module
    total_edges = 0
    total_cross_candidates = 0
    results = []

    for module_dir in module_dirs:
        module_name, call_edges, func_names = load_module_data(module_dir)
        if not call_edges:
            logger.debug("  %s: no call edges, skipping", module_name)
            continue

        cross_edges = get_cross_module_edges(call_edges, func_names)
        total_cross_candidates += len(cross_edges)

        if not cross_edges:
            continue

        t0 = time.perf_counter()
        with driver.session() as session:
            created = backfill_module(session, module_name, cross_edges, dry_run=args.dry_run)

        elapsed = time.perf_counter() - t0
        total_edges += created
        results.append((module_name, len(cross_edges), created, elapsed))

        action = "would create" if args.dry_run else "created/merged"
        logger.info("  %s: %d candidates → %d %s (%.1fs)",
                    module_name, len(cross_edges), created, action, elapsed)

    # Final summary
    with driver.session() as session:
        result = session.run(
            "MATCH ()-[r:SRC_CALLS {cross_module: true}]->() RETURN count(r) AS cnt"
        )
        final_count = result.single()["cnt"]

    logger.info("")
    logger.info("=" * 60)
    logger.info("BACKFILL SUMMARY")
    logger.info("=" * 60)
    logger.info("  Modules processed:         %d", len(results))
    logger.info("  Total cross-module candidates: %d", total_cross_candidates)
    logger.info("  Edges %s:       %d", "that would be created" if args.dry_run else "created/merged", total_edges)
    logger.info("  Cross-module edges before: %d", existing)
    logger.info("  Cross-module edges after:  %d", final_count)
    logger.info("  Net new edges:             %d", final_count - existing)
    logger.info("=" * 60)

    driver.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()

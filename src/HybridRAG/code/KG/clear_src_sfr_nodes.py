"""One-shot cleanup: delete all SRC_* and SFR_Register nodes (with edges).

Usage:
    python clear_src_sfr_nodes.py --profile mcal [--dry-run]

This is a targeted wipe that preserves Jama, EA, TestSpec, BVEC, and other nodes.
Only the following labels are removed:
  - SRC_Function
  - SRC_GlobalVariable
  - SRC_LocalVariable
  - SRC_SourceFile
  - SFR_Register
"""
import sys
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_knowledge_graph import load_storage_config, get_neo4j_settings
from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LABELS_TO_CLEAR = [
    "SRC_Function",
    "SRC_GlobalVariable",
    "SRC_LocalVariable",
    "SRC_SourceFile",
    "SFR_Register",
]

BATCH_SIZE = 5000


def clear_nodes(driver, database: str, label: str, dry_run: bool) -> int:
    """Delete all nodes with the given label in batches. Returns total deleted."""
    with driver.session(database=database) as session:
        # Count first
        result = session.run(f"MATCH (n:{label}) RETURN count(n) AS cnt")
        count = result.single()["cnt"]
        logger.info("  %s: %d nodes found", label, count)

        if dry_run or count == 0:
            return count

        # Batch delete to avoid transaction timeout
        total_deleted = 0
        while True:
            result = session.run(
                f"MATCH (n:{label}) "
                f"WITH n LIMIT $batch "
                f"DETACH DELETE n "
                f"RETURN count(*) AS deleted",
                batch=BATCH_SIZE,
            )
            deleted = result.single()["deleted"]
            total_deleted += deleted
            if deleted > 0:
                logger.info("    deleted %d (total: %d / %d)", deleted, total_deleted, count)
            if deleted < BATCH_SIZE:
                break

        return total_deleted


def main():
    parser = argparse.ArgumentParser(description="Clear SRC_* and SFR_Register nodes from Neo4j")
    parser.add_argument("--profile", default="mcal", help="Neo4j profile (default: mcal)")
    parser.add_argument("--dry-run", action="store_true", help="Only count, don't delete")
    args = parser.parse_args()

    storage_cfg = load_storage_config()
    neo4j_cfg = get_neo4j_settings(args.profile, storage_cfg)

    uri = neo4j_cfg["uri"]
    database = neo4j_cfg.get("database", "neo4j")

    logger.info("Connecting to Neo4j at %s (database: %s)…", uri, database)

    drv_kw = dict(
        auth=(neo4j_cfg["username"], neo4j_cfg["password"]),
        max_connection_lifetime=neo4j_cfg.get("max_connection_lifetime", 3600),
        max_connection_pool_size=neo4j_cfg.get("max_connection_pool_size", 50),
    )
    if "+s" not in uri.split("://")[0]:
        drv_kw["encrypted"] = neo4j_cfg.get("encrypted", False)

    driver = GraphDatabase.driver(uri, **drv_kw)
    driver.verify_connectivity()
    logger.info("Connected.")

    if args.dry_run:
        logger.info("DRY RUN — no deletions will be performed.\n")
    else:
        logger.warning("LIVE RUN — nodes will be permanently deleted!\n")

    grand_total = 0
    for label in LABELS_TO_CLEAR:
        deleted = clear_nodes(driver, database, label, args.dry_run)
        grand_total += deleted

    driver.close()

    if args.dry_run:
        logger.info("\nDRY RUN complete. %d nodes would be deleted.", grand_total)
    else:
        logger.info("\nDone. %d nodes deleted (with all their relationships).", grand_total)


if __name__ == "__main__":
    main()

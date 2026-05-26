"""
Corpus Snapshot — Neo4j Dump (MEG_SW-99)

Exports the knowledge graph from Neo4j using APOC cypher export.
Supports both iLLD and MCAL profiles. Output is a .cypher file that
can be replayed to restore the graph.

Usage:
    python -m src.IngestionPipeline.backup.neo4j_dump --profile illd
    python -m src.IngestionPipeline.backup.neo4j_dump --profile mcal
    python -m src.IngestionPipeline.backup.neo4j_dump --profile all
    python -m src.IngestionPipeline.backup.neo4j_dump --profile mcal --uri bolt://localhost:7687
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

# Add both src/ and HybridRAG/code/ to path (env_config lives alongside neo4j_manager)
_src_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_src_dir))
sys.path.insert(0, str(_src_dir / "HybridRAG" / "code"))

from HybridRAG.code.neo4j_manager import (
    Neo4jConnection,
    get_instance_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("neo4j_dump")

PROFILES = ("illd", "mcal")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _dump_profile(profile: str, output_dir: Path, uri_override: str | None = None) -> Path:
    """Export a single Neo4j profile to a .cypher file."""
    logger.info("Starting Neo4j dump for profile: %s", profile)
    config = get_instance_config(instance_name=profile)

    if uri_override:
        logger.info("URI override: %s → %s", config.uri, uri_override)
        config.uri = uri_override

    with Neo4jConnection(config) as conn:
        # Pre-export stats
        logger.info("Fetching database statistics…")
        stats = conn.get_database_stats()
        logger.info(
            "Pre-export stats [%s]: %d nodes, %d relationships, %d labels",
            profile,
            stats["node_count"],
            stats["relationship_count"],
            len(stats["labels"]),
        )

        # Use APOC streaming export; falls back to manual Cypher extraction
        # if APOC streaming is not available on this Neo4j version.
        timestamp = _timestamp()
        filename = f"{profile}-neo4j-{timestamp}.cypher"
        output_path = output_dir / filename

        start = time.monotonic()

        try:
            logger.info("Attempting APOC streaming export…")
            cypher_lines = _export_via_apoc(conn)
            logger.info("APOC export returned %d lines", len(cypher_lines))
        except Exception as exc:
            logger.warning(
                "APOC export failed (%s), falling back to manual export", exc
            )
            cypher_lines = _export_manual(conn, stats["labels"])

        logger.info("Writing %d lines to %s…", len(cypher_lines), output_path.name)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"// Neo4j corpus snapshot — {profile}\n")
            f.write(f"// Exported: {timestamp}\n")
            f.write(f"// Nodes: {stats['node_count']}\n")
            f.write(f"// Relationships: {stats['relationship_count']}\n")
            f.write(f"// Labels: {', '.join(stats['labels'])}\n\n")
            for line in tqdm(cypher_lines, desc="Writing cypher", unit=" lines"):
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")

        elapsed = time.monotonic() - start
        size_mb = output_path.stat().st_size / (1024 * 1024)

        logger.info(
            "Dump complete [%s]: %s (%.2f MB, %.1fs)",
            profile, output_path, size_mb, elapsed,
        )
        print(f"\n  Profile      : {profile}")
        print(f"  Output       : {output_path}")
        print(f"  Size         : {size_mb:.2f} MB")
        print(f"  Nodes        : {stats['node_count']}")
        print(f"  Relationships: {stats['relationship_count']}")
        print(f"  Duration     : {elapsed:.1f}s")

    return output_path


def _export_via_apoc(conn: Neo4jConnection) -> list[str]:
    """Use APOC apoc.export.cypher.all with stream:true."""
    result = conn.query(
        "CALL apoc.export.cypher.all(null, {stream: true, format: 'cypher-shell'}) "
        "YIELD cypherStatements RETURN cypherStatements"
    )
    if not result:
        raise RuntimeError("APOC export returned empty result")
    statements = result[0].get("cypherStatements", "")
    if not statements:
        raise RuntimeError("APOC export returned empty cypherStatements")
    return statements.splitlines(keepends=True)


def _export_manual(conn: Neo4jConnection, labels: list[str]) -> list[str]:
    """Fallback: manually export nodes and relationships as Cypher CREATE statements.

    Uses a temporary ``_dump_id`` property set to the Neo4j internal element
    id so that relationship MATCH clauses are guaranteed to be 1-to-1.
    A cleanup REMOVE statement and index drop are emitted at the end.
    """
    lines: list[str] = []

    # Collect all unique labels that actually have nodes (for index/cleanup)
    all_labels_used: set[str] = set()

    # Export nodes per label, embedding the Neo4j internal id as _dump_id
    logger.info("Exporting nodes for %d labels…", len(labels))
    for label in tqdm(labels, desc="Exporting labels", unit=" label"):
        logger.debug("Querying nodes with label: %s", label)
        nodes = conn.query(
            f"MATCH (n:`{label}`) "
            f"RETURN id(n) AS nid, labels(n) AS lbls, properties(n) AS props"
        )
        logger.info("  Label %-40s → %d nodes", label, len(nodes))
        for record in nodes:
            props = dict(record["props"])
            props["_dump_id"] = record["nid"]
            lbls = record["lbls"]
            for l in lbls:
                all_labels_used.add(l)
            label_str = ":".join(f"`{l}`" for l in lbls)
            props_str = _props_to_cypher(props)
            lines.append(f"CREATE (:{label_str} {props_str});\n")

    logger.info("Total node CREATE statements: %d", len(lines))

    # Create indexes on _dump_id for fast relationship matching
    for label in sorted(all_labels_used):
        lines.append(
            f"CREATE INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n._dump_id);\n"
        )
    # Need a barrier statement to ensure indexes are online before relationships
    lines.append("CALL db.awaitIndexes(300);\n")

    # Export relationships using _dump_id for exact 1:1 matching
    logger.info("Exporting relationships (single bulk query)…")
    rels = conn.query(
        "MATCH (a)-[r]->(b) "
        "RETURN id(a) AS src_id, labels(a) AS src_labels, "
        "       type(r) AS rel_type, properties(r) AS rel_props, "
        "       id(b) AS tgt_id, labels(b) AS tgt_labels"
    )
    logger.info("Fetched %d relationships, generating Cypher…", len(rels))
    for record in tqdm(rels, desc="Exporting rels", unit=" rel"):
        src_label = record["src_labels"][0]  # use first label for indexed lookup
        tgt_label = record["tgt_labels"][0]
        rel_type = record["rel_type"]
        rel_props = _props_to_cypher(record["rel_props"]) if record["rel_props"] else ""

        lines.append(
            f"MATCH (a:`{src_label}` {{_dump_id: {record['src_id']}}}), "
            f"(b:`{tgt_label}` {{_dump_id: {record['tgt_id']}}}) "
            f"CREATE (a)-[:`{rel_type}` {rel_props}]->(b);\n"
        )

    logger.info("Total relationship CREATE statements: %d", len(rels))

    # Cleanup: remove _dump_id property and drop indexes
    for label in sorted(all_labels_used):
        lines.append(
            f"MATCH (n:`{label}`) REMOVE n._dump_id;\n"
        )
        lines.append(
            f"DROP INDEX IF EXISTS FOR (n:`{label}`) ON (n._dump_id);\n"
        )

    logger.info("Export complete — %d total Cypher lines generated", len(lines))
    return lines


def _props_to_cypher(props: dict) -> str:
    """Convert a properties dict to a Cypher map literal."""
    if not props:
        return "{}"
    # Skip embedding vectors (large float arrays) from export
    filtered = {k: v for k, v in props.items() if k != "embedding"}
    parts = [f"{k}: {_cypher_value(v)}" for k, v in filtered.items()]
    return "{" + ", ".join(parts) + "}"


def _cypher_value(val) -> str:
    """Format a Python value as a Cypher literal."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n")
        return f"'{escaped}'"
    if isinstance(val, list):
        return "[" + ", ".join(_cypher_value(v) for v in val) + "]"
    return f"'{val}'"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Neo4j knowledge graph as Cypher statements"
    )
    parser.add_argument(
        "--profile",
        choices=["illd", "mcal", "all"],
        required=True,
        help="Instance profile to export",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./backups"),
        help="Directory for output files (default: ./backups)",
    )
    parser.add_argument(
        "--uri",
        type=str,
        default=None,
        help="Override Neo4j URI (e.g. bolt://localhost:7687 when port-forwarding)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    profiles = PROFILES if args.profile == "all" else (args.profile,)
    results: list[Path] = []

    print(f"\n{'=' * 55}")
    print(f" Neo4j Corpus Snapshot")
    print(f" Profiles: {', '.join(profiles)}")
    print(f" Output  : {args.output_dir.resolve()}")
    if args.uri:
        print(f" URI     : {args.uri}")
    print(f"{'=' * 55}")

    for profile in profiles:
        try:
            path = _dump_profile(profile, args.output_dir, uri_override=args.uri)
            results.append(path)
        except Exception as exc:
            logger.error("Failed to dump %s: %s", profile, exc)
            print(f"\n  ERROR [{profile}]: {exc}")

    print(f"\n{'=' * 55}")
    print(f" Summary: {len(results)}/{len(profiles)} profiles exported")
    for path in results:
        print(f"   {path}")
    print(f"{'=' * 55}\n")

    sys.exit(0 if len(results) == len(profiles) else 1)


if __name__ == "__main__":
    main()

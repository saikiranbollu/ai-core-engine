"""
Corpus Restore (MEG_SW-102)

Restores Neo4j and/or Qdrant from export files (local or S3).

Qdrant restore uses point-level upsert from JSON exports (scroll-based).
The Qdrant snapshot API is unreliable (produces 0-byte files on Qdrant 1.15.x),
so we use JSON dumps with full point data (vectors + payloads + config).

Usage:
    # Restore Neo4j from local file
    python -m src.IngestionPipeline.backup.restore --profile illd --component neo4j --from-file backups/illd-neo4j-2026-04-20T14-30-00Z.cypher

    # Restore Qdrant from local JSON export
    python -m src.IngestionPipeline.backup.restore --profile illd --component qdrant --from-file backups/illd-qdrant-cxpi-2026-04-20T14-30-00Z.json

    # Restore Qdrant from S3 migration dumps
    python -m src.IngestionPipeline.backup.restore --profile illd --component qdrant --from-s3-latest

    # Restore all components, clearing existing data first
    python -m src.IngestionPipeline.backup.restore --profile illd --component all --from-s3-latest --clear-first
"""

import argparse
import json
import logging
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

# Add both src/ and HybridRAG/code/ to path (env_config lives alongside neo4j_manager)
_src_dir = Path(__file__).resolve().parents[2]
_repo_root = _src_dir.parent
sys.path.insert(0, str(_src_dir))
sys.path.insert(0, str(_src_dir / "HybridRAG" / "code"))
sys.path.insert(0, str(_repo_root))

from HybridRAG.code.neo4j_manager import (
    Neo4jConnection,
    Neo4jInstanceConfig,
    get_instance_config,
    get_qdrant_config,
)
from src.IngestionPipeline.backup.s3_upload import download_file, list_snapshots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("restore")

CYPHER_BATCH_SIZE = 500
PARALLEL_WORKERS = 8


# ---------------------------------------------------------------------------
# Neo4j Restore
# ---------------------------------------------------------------------------

def restore_neo4j(
    profile: str,
    cypher_path: Path,
    clear_first: bool = False,
    neo4j_override: dict | None = None,
    workers: int = PARALLEL_WORKERS,
) -> dict:
    """Restore a Neo4j instance from a .cypher export file.
    
    If neo4j_override is provided, use those connection details instead of
    the profile config. Keys: uri, username, password, database.
    """
    logger.info("Starting Neo4j restore for profile: %s from %s (%d workers)", profile, cypher_path, workers)

    if neo4j_override:
        config = Neo4jInstanceConfig(
            name=f"{profile}-local",
            description=f"Local override for {profile}",
            uri=neo4j_override["uri"],
            username=neo4j_override["username"],
            password=neo4j_override["password"],
            database=neo4j_override.get("database", "neo4j"),
        )
        logger.info("Using Neo4j override: %s (db=%s)", config.uri, config.database)
    else:
        config = get_instance_config(instance_name=profile)

    with Neo4jConnection(config) as conn:
        # Pre-restore stats
        pre_stats = conn.get_database_stats()
        logger.info(
            "Pre-restore [%s]: %d nodes, %d relationships",
            profile, pre_stats["node_count"], pre_stats["relationship_count"],
        )

        start = time.monotonic()

        # Clear existing data if requested
        if clear_first:
            logger.warning("Clearing all data in %s...", profile)
            conn.query("MATCH (n) DETACH DELETE n")
            logger.info("Cleared all nodes and relationships")

        # Read and execute Cypher statements
        with open(cypher_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Filter out comments and empty lines, collect statements
        statements: list[str] = []
        current = ""
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            current += line
            if stripped.endswith(";"):
                statements.append(current.strip().rstrip(";"))
                current = ""
        if current.strip():
            statements.append(current.strip().rstrip(";"))

        # Split into phases:
        # - node CREATEs: "CREATE (:" — independent node creation
        # - relationship MATCHes: "MATCH ... CREATE (a)-[" — depend on nodes
        # - cleanup: "MATCH ... REMOVE" — remove temp props after restore
        # - other: CREATE INDEX, DROP INDEX, CALL, etc. — run sequentially between phases
        node_stmts = []
        rel_stmts = []
        cleanup_stmts = []
        index_stmts = []  # CREATE INDEX, DROP INDEX, CALL db.awaitIndexes
        for s in statements:
            if s.startswith("CREATE ("):
                node_stmts.append(s)
            elif s.startswith("MATCH") and "REMOVE" in s:
                cleanup_stmts.append(s)
            elif s.startswith("MATCH") and "CREATE" in s:
                rel_stmts.append(s)
            else:
                index_stmts.append(s)

        logger.info(
            "Parsed %d statements: %d nodes, %d rels, %d index/other, %d cleanup",
            len(statements), len(node_stmts), len(rel_stmts),
            len(index_stmts), len(cleanup_stmts),
        )

        # Thread-safe counters
        _lock = threading.Lock()
        executed = 0
        errors = 0

        def _run_batch(batch: list[str]) -> tuple[int, int]:
            """Execute a batch in its own session/transaction.
            
            If any statement fails, rolls back and retries each statement
            individually to avoid cascading the failure across the batch.
            Returns (ok, err).
            """
            ok, err = 0, 0
            with conn.driver.session(database=config.database) as session:
                try:
                    with session.begin_transaction() as tx:
                        for stmt in batch:
                            tx.run(stmt)
                        tx.commit()
                    ok = len(batch)
                except Exception:
                    # Batch had at least one bad statement — retry individually
                    for stmt in batch:
                        try:
                            session.run(stmt)
                            ok += 1
                        except Exception as exc:
                            logger.warning("Statement failed: %s — %s", stmt[:80], exc)
                            err += 1
            return ok, err

        def _run_phase(stmts: list[str], label: str) -> tuple[int, int]:
            """Run a list of statements in parallel batches."""
            nonlocal executed, errors
            batches = [
                stmts[i : i + CYPHER_BATCH_SIZE]
                for i in range(0, len(stmts), CYPHER_BATCH_SIZE)
            ]
            phase_ok, phase_err = 0, 0
            done_count = 0

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_run_batch, b): i for i, b in enumerate(batches)}
                for future in as_completed(futures):
                    ok, err = future.result()
                    phase_ok += ok
                    phase_err += err
                    done_count += 1
                    if done_count % 10 == 0 or done_count == len(batches):
                        logger.info(
                            "%s: %d/%d batches done (%d ok, %d err)",
                            label, done_count, len(batches), phase_ok, phase_err,
                        )

            with _lock:
                executed += phase_ok
                errors += phase_err
            return phase_ok, phase_err

        # Phase 1: Create all nodes in parallel (independent of each other)
        if node_stmts:
            print(f"\n  Phase 1: Creating {len(node_stmts)} nodes ({workers} workers)...")
            n_ok, n_err = _run_phase(node_stmts, "Nodes")
            print(f"  Phase 1 done: {n_ok} created, {n_err} errors")

        # Phase 1.5: Run index/setup statements sequentially (CREATE INDEX, CALL, etc.)
        if index_stmts:
            print(f"\n  Creating {len(index_stmts)} indexes...")
            for stmt in index_stmts:
                try:
                    with conn.driver.session(database=config.database) as session:
                        session.run(stmt)
                    executed += 1
                except Exception as exc:
                    logger.warning("Index/setup statement failed: %s — %s", stmt[:80], exc)
                    errors += 1

        # Phase 2: Create all relationships in parallel (nodes now exist)
        if rel_stmts:
            print(f"\n  Phase 2: Creating {len(rel_stmts)} relationships ({workers} workers)...")
            r_ok, r_err = _run_phase(rel_stmts, "Rels")
            print(f"  Phase 2 done: {r_ok} created, {r_err} errors")

        # Phase 3: Cleanup (_dump_id removal, index drops)
        if cleanup_stmts:
            print(f"\n  Phase 3: Cleanup ({len(cleanup_stmts)} statements)...")
            c_ok, c_err = _run_phase(cleanup_stmts, "Cleanup")
            print(f"  Phase 3 done: {c_ok} ok, {c_err} errors")

        elapsed = time.monotonic() - start

        # Post-restore stats
        post_stats = conn.get_database_stats()

        result = {
            "profile": profile,
            "component": "neo4j",
            "source": str(cypher_path),
            "statements_total": len(statements),
            "statements_executed": executed,
            "statements_failed": errors,
            "pre_nodes": pre_stats["node_count"],
            "post_nodes": post_stats["node_count"],
            "pre_relationships": pre_stats["relationship_count"],
            "post_relationships": post_stats["relationship_count"],
            "duration_s": round(elapsed, 1),
        }

        print(f"\n  Neo4j Restore [{profile}]")
        print(f"  Source      : {cypher_path}")
        print(f"  Statements  : {executed}/{len(statements)} ({errors} errors)")
        print(f"  Nodes       : {pre_stats['node_count']} → {post_stats['node_count']}")
        print(f"  Relationships: {pre_stats['relationship_count']} → {post_stats['relationship_count']}")
        print(f"  Duration    : {elapsed:.1f}s")

    return result


# ---------------------------------------------------------------------------
# Qdrant Restore (scroll/upsert from JSON exports)
# ---------------------------------------------------------------------------

UPSERT_BATCH_SIZE = 100


def _create_collection(
    client: httpx.Client, url: str, name: str, config: dict
) -> None:
    """Create a Qdrant collection with matching vector config."""
    vectors = config.get("vectors", {"size": 384, "distance": "Cosine"})
    body = {
        "vectors": vectors,
        "shard_number": config.get("shard_number", 1),
        "replication_factor": config.get("replication_factor", 1),
        "on_disk_payload": config.get("on_disk_payload", True),
    }
    resp = client.put(f"{url}/collections/{name}", json=body)
    resp.raise_for_status()


def _upsert_points(
    client: httpx.Client, url: str, collection: str, points: list[dict]
) -> None:
    """Upsert points in batches."""
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        formatted = []
        for p in batch:
            formatted.append({
                "id": p["id"],
                "vector": p["vector"],
                "payload": p.get("payload", {}),
            })
        resp = client.put(
            f"{url}/collections/{collection}/points",
            json={"points": formatted},
            params={"wait": "true"},
        )
        resp.raise_for_status()


def restore_qdrant(
    profile: str,
    json_path: Path,
    clear_first: bool = False,
) -> dict:
    """Restore a Qdrant collection from a JSON export file.

    The JSON file contains: collection name, config, and all points
    (with vectors and payloads) as produced by qdrant_export.py.
    """
    logger.info("Starting Qdrant restore for profile: %s from %s", profile, json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    collection = data["collection"]
    col_config = data.get("config", {})
    points = data.get("points", [])

    config = get_qdrant_config(instance_name=profile)
    url = config.url.rstrip("/")
    if config.port and config.port != 443:
        url = f"{url}:{config.port}"

    headers = {}
    if config.api_key:
        headers["api-key"] = config.api_key

    start = time.monotonic()

    with httpx.Client(verify=config.verify_ssl, timeout=300, headers=headers) as client:
        # Check if collection exists
        resp = client.get(f"{url}/collections/{collection}")
        pre_points = 0
        if resp.status_code == 200:
            pre_points = resp.json()["result"].get("points_count", 0)
            if clear_first:
                logger.warning("Deleting collection '%s'...", collection)
                client.delete(f"{url}/collections/{collection}")
                time.sleep(0.5)
                _create_collection(client, url, collection, col_config)
        else:
            _create_collection(client, url, collection, col_config)

        # Upsert all points
        if points:
            _upsert_points(client, url, collection, points)

        # Verify
        resp = client.get(f"{url}/collections/{collection}")
        post_points = resp.json()["result"].get("points_count", 0) if resp.status_code == 200 else 0

    elapsed = time.monotonic() - start

    result = {
        "profile": profile,
        "component": "qdrant",
        "source": str(json_path),
        "collection": collection,
        "pre_points": pre_points,
        "post_points": post_points,
        "expected_points": len(points),
        "duration_s": round(elapsed, 1),
    }

    print(f"\n  Qdrant Restore [{profile}]")
    print(f"  Source    : {json_path}")
    print(f"  Collection: {collection}")
    print(f"  Points    : {pre_points} → {post_points} (expected {len(points)})")
    print(f"  Duration  : {elapsed:.1f}s")

    return result


def restore_qdrant_from_s3(
    profile: str,
    s3_key: str,
    bucket: str = "aicoreengine",
    clear_first: bool = False,
) -> dict:
    """Restore a Qdrant collection from an S3 JSON export.

    Downloads the JSON dump from S3 and restores using point-level upsert.
    Works with exports created by qdrant_export.py --upload-s3.
    """
    logger.info("Starting Qdrant S3 restore for profile: %s from s3://%s/%s", profile, bucket, s3_key)

    # Download to temp file
    tmp_path = Path(tempfile.mktemp(suffix=".json"))
    try:
        download_file(s3_key, tmp_path, bucket=bucket)
        result = restore_qdrant(profile, tmp_path, clear_first)
        result["source"] = f"s3://{bucket}/{s3_key}"
        result["method"] = "s3-download-upsert"
    finally:
        tmp_path.unlink(missing_ok=True)

    return result


def restore_qdrant_all_from_s3(
    profile: str,
    bucket: str = "aicoreengine",
    clear_first: bool = False,
) -> list[dict]:
    """Restore ALL Qdrant collections for a profile from S3 migration dumps.

    Looks for JSON files under migration/{profile}/ in S3.
    """
    prefix = f"migration/{profile}/"
    snapshots = list_snapshots(bucket=bucket, prefix=prefix)
    json_files = [s for s in snapshots if s["key"].endswith(".json")]

    if not json_files:
        # Fall back to snapshots/ prefix (newer export format)
        prefix = f"snapshots/{profile}/qdrant/"
        snapshots = list_snapshots(bucket=bucket, prefix=prefix)
        json_files = [s for s in snapshots if s["key"].endswith(".json")]

    if not json_files:
        raise FileNotFoundError(
            f"No JSON exports found in s3://{bucket}/migration/{profile}/ "
            f"or s3://{bucket}/snapshots/{profile}/qdrant/"
        )

    logger.info("Found %d JSON exports for profile '%s'", len(json_files), profile)
    results = []
    for snap in sorted(json_files, key=lambda s: s["key"]):
        try:
            result = restore_qdrant_from_s3(profile, snap["key"], bucket, clear_first)
            results.append(result)
        except Exception as exc:
            logger.error("Failed to restore %s: %s", snap["key"], exc)
            results.append({
                "profile": profile,
                "component": "qdrant",
                "source": f"s3://{bucket}/{snap['key']}",
                "status": "failed",
                "error": str(exc),
            })

    return results


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _find_latest_s3(profile: str, component: str, bucket: str) -> str:
    """Find the most recent snapshot/export in S3 for a profile/component."""
    if component == "qdrant":
        # Check migration/ prefix first (scroll-based exports)
        prefix = f"migration/{profile}/"
        snapshots = list_snapshots(bucket=bucket, prefix=prefix)
        json_files = [s for s in snapshots if s["key"].endswith(".json")]
        if json_files:
            json_files.sort(key=lambda s: s["key"], reverse=True)
            return json_files[0]["key"]
        # Fall back to snapshots/ prefix
        prefix = f"snapshots/{profile}/qdrant/"
        snapshots = list_snapshots(bucket=bucket, prefix=prefix)
        json_files = [s for s in snapshots if s["key"].endswith(".json")]
        if json_files:
            json_files.sort(key=lambda s: s["key"], reverse=True)
            return json_files[0]["key"]
        raise FileNotFoundError(
            f"No Qdrant exports found in s3://{bucket}/migration/{profile}/ "
            f"or s3://{bucket}/snapshots/{profile}/qdrant/"
        )
    else:
        prefix = f"snapshots/{profile}/{component}/"
        snapshots = list_snapshots(bucket=bucket, prefix=prefix)
        if not snapshots:
            raise FileNotFoundError(
                f"No snapshots found in s3://{bucket}/{prefix}"
            )
        snapshots.sort(key=lambda s: s["key"], reverse=True)
        return snapshots[0]["key"]


def _download_from_s3(
    object_key: str, bucket: str, tmp_dir: Path
) -> Path:
    """Download a snapshot from S3 to a temp directory."""
    filename = object_key.rsplit("/", 1)[-1]
    local_path = tmp_dir / filename
    download_file(object_key, local_path, bucket=bucket)
    return local_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore Neo4j and/or Qdrant from corpus snapshots"
    )
    parser.add_argument(
        "--profile",
        choices=["illd", "mcal"],
        required=True,
        help="Instance profile to restore",
    )
    parser.add_argument(
        "--component",
        choices=["neo4j", "qdrant", "all"],
        required=True,
        help="Component to restore",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-file",
        type=Path,
        help="Restore from local file(s). For --component all, provide a directory",
    )
    source.add_argument(
        "--from-s3",
        type=str,
        help="S3 object key to download and restore from",
    )
    source.add_argument(
        "--from-s3-latest",
        action="store_true",
        help="Automatically find and restore the latest snapshot from S3",
    )

    parser.add_argument(
        "--bucket",
        default="aicoreengine",
        help="S3 bucket (default: aicoreengine)",
    )
    parser.add_argument(
        "--clear-first",
        action="store_true",
        help="Delete existing data before restore (DESTRUCTIVE)",
    )
    parser.add_argument(
        "--neo4j-uri",
        type=str,
        help="Override Neo4j URI (e.g. bolt://localhost:7687)",
    )
    parser.add_argument(
        "--neo4j-user",
        type=str,
        default="neo4j",
        help="Override Neo4j username (default: neo4j)",
    )
    parser.add_argument(
        "--neo4j-password",
        type=str,
        help="Override Neo4j password",
    )
    parser.add_argument(
        "--neo4j-database",
        type=str,
        default="neo4j",
        help="Override Neo4j database name (default: neo4j)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=PARALLEL_WORKERS,
        help=f"Parallel worker threads for restore (default: {PARALLEL_WORKERS})",
    )
    args = parser.parse_args()

    # Safety confirmation for --clear-first
    if args.clear_first:
        answer = input(
            f"\n  WARNING: --clear-first will DELETE all existing data in "
            f"{args.profile}/{args.component}.\n"
            f"  Type 'yes' to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("  Aborted.")
            sys.exit(1)

    components = ("neo4j", "qdrant") if args.component == "all" else (args.component,)

    # Build Neo4j override if custom connection provided
    neo4j_override = None
    if args.neo4j_uri:
        neo4j_override = {
            "uri": args.neo4j_uri,
            "username": args.neo4j_user,
            "password": args.neo4j_password or "",
            "database": args.neo4j_database,
        }

    print(f"\n{'=' * 55}")
    print(f" Corpus Restore")
    print(f" Profile    : {args.profile}")
    print(f" Components : {', '.join(components)}")
    print(f" Clear first: {args.clear_first}")
    if neo4j_override:
        print(f" Neo4j      : {args.neo4j_uri} (db={args.neo4j_database})")
    print(f"{'=' * 55}")

    results: list[dict] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="aice_restore_"))

    try:
        for component in components:
            # Resolve the source file
            if args.from_file:
                if args.from_file.is_dir():
                    if component == "neo4j":
                        ext = ".cypher"
                        matches = sorted(args.from_file.glob(f"{args.profile}-{component}-*{ext}"))
                    else:
                        ext = ".json"
                        matches = sorted(args.from_file.glob(f"{args.profile}-qdrant-*{ext}"))
                    if not matches:
                        logger.error("No %s files found for %s in %s", ext, args.profile, args.from_file)
                        continue
                    source_path = matches[-1]  # latest by name
                else:
                    source_path = args.from_file

                # Local file restore
                if component == "neo4j":
                    result = restore_neo4j(
                        args.profile, source_path, args.clear_first,
                        neo4j_override, workers=args.workers,
                    )
                    results.append(result)
                else:
                    result = restore_qdrant(args.profile, source_path, args.clear_first)
                    results.append(result)

            elif args.from_s3 or args.from_s3_latest:
                if component == "qdrant":
                    if args.from_s3:
                        # Single collection from specific S3 key
                        result = restore_qdrant_from_s3(
                            args.profile, args.from_s3, args.bucket, args.clear_first,
                        )
                        results.append(result)
                    else:
                        # Restore ALL collections for this profile from S3
                        qdrant_results = restore_qdrant_all_from_s3(
                            args.profile, args.bucket, args.clear_first,
                        )
                        results.extend(qdrant_results)
                else:
                    # Neo4j still needs local download (cypher statements)
                    if args.from_s3:
                        s3_key = args.from_s3
                    else:
                        s3_key = _find_latest_s3(args.profile, component, args.bucket)
                        logger.info("Latest S3 snapshot: %s", s3_key)
                    source_path = _download_from_s3(s3_key, args.bucket, tmp_dir)
                    result = restore_neo4j(
                        args.profile, source_path, args.clear_first,
                        neo4j_override, workers=args.workers,
                    )
                    results.append(result)

    finally:
        # Clean up temp downloads
        import shutil
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n{'=' * 55}")
    print(f" Restore Summary")
    print(f"{'=' * 55}")
    for r in results:
        print(f"  {r['component']:6s} [{r['profile']}]: {r.get('duration_s', '?')}s")
    print(f"{'=' * 55}\n")

    sys.exit(0 if len(results) == len(components) else 1)


if __name__ == "__main__":
    main()

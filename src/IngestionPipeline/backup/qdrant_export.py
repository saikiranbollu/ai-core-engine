"""
Corpus Snapshot — Qdrant Export (MEG_SW-100)

Exports ALL Qdrant collections using point-level scroll API.
The Qdrant snapshot API produces 0-byte files on our deployment (known issue
with Qdrant 1.15.x), so this uses scroll + JSON serialization instead.

Collections are auto-discovered and classified by profile:
  - Collections with '_' in the name → MCAL (e.g., adc_swa_architecture)
  - Collections without '_' → iLLD (e.g., cxpi, fray, sent)

Export format: JSON file per collection containing points + collection config.
Can export to local dir or upload directly to S3 (Ceph).

Usage:
    python -m src.IngestionPipeline.backup.qdrant_export --profile illd
    python -m src.IngestionPipeline.backup.qdrant_export --profile mcal --upload-s3
    python -m src.IngestionPipeline.backup.qdrant_export --profile all --output-dir /tmp/backups
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Add both src/ and HybridRAG/code/ to path (env_config lives alongside neo4j_manager)
_src_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_src_dir))
sys.path.insert(0, str(_src_dir / "HybridRAG" / "code"))

from HybridRAG.code.neo4j_manager import (
    QdrantConnection,
    get_qdrant_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("qdrant_export")

PROFILES = ("illd", "mcal")
SCROLL_BATCH_SIZE = 100


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def classify_collection(name: str) -> str:
    """Classify a collection as illd or mcal by naming convention.

    - Names with '_' → mcal  (e.g. adc_swa_architecture, port_swud_design)
    - Names without '_' → illd (e.g. cxpi, fray, sent, xspi)
    """
    return "mcal" if "_" in name else "illd"


def _build_base_url(config) -> str:
    """Build the Qdrant REST base URL from config."""
    url = config.url.rstrip("/")
    if config.port and config.port != 443:
        url = f"{url}:{config.port}"
    return url


def discover_collections(config) -> list[dict]:
    """Connect to Qdrant and return all collection names with stats and config."""
    base_url = _build_base_url(config)
    headers = {}
    if config.api_key:
        headers["api-key"] = config.api_key

    with httpx.Client(verify=config.verify_ssl, timeout=60, headers=headers) as client:
        resp = client.get(f"{base_url}/collections")
        resp.raise_for_status()
        collections = []
        for col in resp.json()["result"]["collections"]:
            name = col["name"]
            try:
                info = client.get(f"{base_url}/collections/{name}")
                info.raise_for_status()
                result = info.json()["result"]
                points = result.get("points_count") or 0
                col_config = result.get("config", {}).get("params", {})
            except Exception:
                points = 0
                col_config = {}
            collections.append({
                "name": name,
                "points": points,
                "profile": classify_collection(name),
                "config": col_config,
            })
    return sorted(collections, key=lambda c: c["name"])


def scroll_all_points(
    client: httpx.Client, base_url: str, collection: str
) -> list[dict]:
    """Scroll through all points in a collection via REST API.

    Returns a list of point dicts with id, vector, and payload.
    """
    all_points = []
    offset = None
    while True:
        body = {
            "limit": SCROLL_BATCH_SIZE,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        resp = client.post(
            f"{base_url}/collections/{collection}/points/scroll", json=body
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        points = result.get("points", [])
        all_points.extend(points)
        offset = result.get("next_page_offset")
        if offset is None or not points:
            break
    return all_points


def _export_collection(
    collection_name: str,
    profile: str,
    base_url: str,
    headers: dict,
    verify_ssl: bool,
    output_dir: Path,
    col_config: dict,
) -> Path:
    """Export a single Qdrant collection to a JSON file using scroll API."""
    timestamp = _timestamp()
    filename = f"{profile}-qdrant-{collection_name}-{timestamp}.json"
    output_path = output_dir / filename

    start = time.monotonic()

    with httpx.Client(
        verify=verify_ssl, timeout=300, headers=headers
    ) as client:
        points = scroll_all_points(client, base_url, collection_name)

    data = {
        "collection": collection_name,
        "profile": profile,
        "config": col_config,
        "exported_at": timestamp,
        "points_count": len(points),
        "points": points,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    elapsed = time.monotonic() - start
    size_mb = output_path.stat().st_size / (1024 * 1024)

    logger.info(
        "Exported '%s' → %s (%d points, %.2f MB, %.1fs)",
        collection_name, output_path, len(points), size_mb, elapsed,
    )
    return output_path


def _export_collection_to_s3(
    collection_name: str,
    profile: str,
    base_url: str,
    headers: dict,
    verify_ssl: bool,
    col_config: dict,
    bucket: str,
) -> dict:
    """Export a single Qdrant collection directly to S3 as JSON."""
    from src.IngestionPipeline.backup.s3_upload import _get_minio_client

    start = time.monotonic()

    with httpx.Client(
        verify=verify_ssl, timeout=300, headers=headers
    ) as client:
        points = scroll_all_points(client, base_url, collection_name)

    data = {
        "collection": collection_name,
        "profile": profile,
        "config": col_config,
        "exported_at": _timestamp(),
        "points_count": len(points),
        "points": points,
    }

    # Write to temp file and upload
    import tempfile
    tmp_path = Path(tempfile.mktemp(suffix=".json"))
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        minio_client = _get_minio_client()
        object_key = f"migration/{profile}/{collection_name}.json"
        minio_client.fput_object(bucket, object_key, str(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)

    elapsed = time.monotonic() - start
    logger.info(
        "Exported '%s' → s3://%s/%s (%d points, %.1fs)",
        collection_name, bucket, object_key, len(points), elapsed,
    )
    return {"object_key": object_key, "points": len(points), "elapsed": elapsed}


def export_profile(
    profile: str,
    output_dir: Path,
    upload_s3: bool = False,
    bucket: str = "aicoreengine",
) -> list[dict]:
    """Export all Qdrant collections for a given profile (or all).

    Uses scroll API to export point data as JSON (snapshot API is unreliable).
    Optionally uploads directly to S3 instead of writing to local disk.

    Returns a list of result dicts with export details.
    """
    config = get_qdrant_config(instance_name="illd")
    base_url = _build_base_url(config)
    headers = {}
    if config.api_key:
        headers["api-key"] = config.api_key

    all_collections = discover_collections(config)

    if profile == "all":
        target_collections = all_collections
    else:
        target_collections = [c for c in all_collections if c["profile"] == profile]

    if not target_collections:
        logger.warning("No collections found for profile '%s'", profile)
        return []

    total_points = sum(c["points"] for c in target_collections)
    print(f"\n  Discovered {len(target_collections)} collections for '{profile}' ({total_points} total points):")
    for c in target_collections:
        print(f"    {c['name']:40s} ({c['points']} points)")

    results = []
    for col_info in target_collections:
        col_name = col_info["name"]
        col_profile = col_info["profile"]
        try:
            if upload_s3:
                s3_result = _export_collection_to_s3(
                    col_name, col_profile, base_url, headers,
                    config.verify_ssl, col_info.get("config", {}), bucket,
                )
                results.append({
                    "collection": col_name,
                    "profile": col_profile,
                    "s3_key": s3_result["object_key"],
                    "points": s3_result["points"],
                    "status": "ok",
                })
                print(f"    ✓ {col_name:40s} → s3://{bucket}/{s3_result['object_key']} ({s3_result['points']} pts)")
            else:
                path = _export_collection(
                    col_name, col_profile, base_url, headers,
                    config.verify_ssl, output_dir, col_info.get("config", {}),
                )
                size_mb = path.stat().st_size / (1024 * 1024)
                results.append({
                    "collection": col_name,
                    "profile": col_profile,
                    "path": str(path),
                    "size_mb": round(size_mb, 2),
                    "points": col_info["points"],
                    "status": "ok",
                })
                print(f"    ✓ {col_name:40s} → {path.name} ({size_mb:.2f} MB)")
        except Exception as exc:
            logger.error("Failed to export '%s': %s", col_name, exc)
            results.append({
                "collection": col_name,
                "profile": col_profile,
                "status": "failed",
                "error": str(exc),
            })
            print(f"    x {col_name:40s} FAILED: {exc}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Qdrant collections via scroll API (JSON format)"
    )
    parser.add_argument(
        "--profile",
        choices=["illd", "mcal", "all"],
        required=True,
        help="Instance profile to export (collections auto-discovered)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./backups"),
        help="Directory for output files (default: ./backups)",
    )
    parser.add_argument(
        "--upload-s3",
        action="store_true",
        help="Upload exports directly to S3 instead of saving locally",
    )
    parser.add_argument(
        "--bucket",
        default="aicoreengine",
        help="S3 bucket for --upload-s3 (default: aicoreengine)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List collections without exporting",
    )
    args = parser.parse_args()

    if not args.upload_s3:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 65}")
    print(f" Qdrant Corpus Export (scroll-based)")
    print(f" Profile: {args.profile}")
    if args.upload_s3:
        print(f" Target : s3://{args.bucket}/migration/")
    else:
        print(f" Output : {args.output_dir.resolve()}")
    print(f"{'=' * 65}")

    if args.list_only:
        config = get_qdrant_config(instance_name="illd")
        all_cols = discover_collections(config)
        target = all_cols if args.profile == "all" else [c for c in all_cols if c["profile"] == args.profile]
        print(f"\n  {'Collection':<42s} {'Points':>8s}  Profile")
        print(f"  {'-'*42} {'-'*8}  {'-'*6}")
        for c in target:
            print(f"  {c['name']:<42s} {c['points']:>8d}  {c['profile']}")
        total_pts = sum(c["points"] for c in target)
        print(f"\n  Total: {len(target)} collections, {total_pts} points")
        print(f"{'=' * 65}\n")
        return

    results = export_profile(
        args.profile, args.output_dir,
        upload_s3=args.upload_s3, bucket=args.bucket,
    )
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "failed"]
    total_points = sum(r.get("points", 0) for r in ok)

    print(f"\n{'=' * 65}")
    print(f" Summary: {len(ok)}/{len(results)} collections exported ({total_points} points)")
    if failed:
        print(f" Failed : {len(failed)}")
        for r in failed:
            print(f"   x {r['collection']}: {r.get('error','')}")
    print(f"{'=' * 65}\n")

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()

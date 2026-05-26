"""
Corpus Snapshot — Full Orchestrator (MEG_SW-101)

Runs Neo4j dump + Qdrant export + optional S3 upload in sequence.
Qdrant export uses scroll-based JSON format (snapshot API is unreliable).

Usage:
    python -m src.IngestionPipeline.backup.snapshot_all --profile illd
    python -m src.IngestionPipeline.backup.snapshot_all --profile all --upload
    python -m src.IngestionPipeline.backup.snapshot_all --profile mcal --upload --bucket aicoreengine
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from src.IngestionPipeline.backup.neo4j_dump import _dump_profile as neo4j_dump
from src.IngestionPipeline.backup.qdrant_export import export_profile as qdrant_export_profile
from src.IngestionPipeline.backup.s3_upload import upload_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("snapshot_all")

PROFILES = ("illd", "mcal")


def snapshot_profile(
    profile: str,
    output_dir: Path,
    upload: bool = False,
    bucket: str = "aicoreengine",
) -> dict:
    """
    Run full snapshot for a single profile.

    Returns a dict with status and file paths.
    """
    result = {"profile": profile, "neo4j": None, "qdrant": None, "s3_keys": []}

    # Neo4j dump
    try:
        neo4j_path = neo4j_dump(profile, output_dir)
        result["neo4j"] = str(neo4j_path)
    except Exception as exc:
        logger.error("Neo4j dump failed for %s: %s", profile, exc)
        result["neo4j_error"] = str(exc)

    # Qdrant export (scroll-based JSON; optionally direct to S3)
    qdrant_paths: list[str] = []
    try:
        if upload:
            # Export directly to S3 (skip local disk)
            qdrant_results = qdrant_export_profile(
                profile, output_dir, upload_s3=True, bucket=bucket,
            )
            for qr in qdrant_results:
                if qr["status"] == "ok":
                    result["s3_keys"].append(qr.get("s3_key", ""))
        else:
            qdrant_results = qdrant_export_profile(profile, output_dir)
            for qr in qdrant_results:
                if qr["status"] == "ok":
                    qdrant_paths.append(qr["path"])
        result["qdrant"] = qdrant_paths or [f"{len(qdrant_results)} collections"]
    except Exception as exc:
        logger.error("Qdrant export failed for %s: %s", profile, exc)
        result["qdrant_error"] = str(exc)

    # S3 upload (for Neo4j dump only — Qdrant already uploaded if --upload)
    if upload:
        if result.get("neo4j"):
            try:
                s3_key = upload_file(Path(result["neo4j"]), bucket=bucket)
                result["s3_keys"].append(s3_key)
            except Exception as exc:
                logger.error("S3 upload failed for %s: %s", result["neo4j"], exc)
                result.setdefault("s3_errors", []).append(str(exc))

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full corpus snapshot: Neo4j + Qdrant + optional S3 upload"
    )
    parser.add_argument(
        "--profile",
        choices=["illd", "mcal", "all"],
        required=True,
        help="Instance profile to snapshot",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./backups"),
        help="Directory for snapshot files (default: ./backups)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload snapshots to MinIO after export",
    )
    parser.add_argument(
        "--bucket",
        default="aicoreengine",
        help="S3 bucket for upload (default: aicoreengine)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove local files after successful S3 upload",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    profiles = PROFILES if args.profile == "all" else (args.profile,)

    print(f"\n{'=' * 60}")
    print(f" Full Corpus Snapshot")
    print(f" Profiles : {', '.join(profiles)}")
    print(f" Output   : {args.output_dir.resolve()}")
    print(f" S3 Upload: {'Yes → ' + args.bucket if args.upload else 'No'}")
    print(f"{'=' * 60}")

    overall_start = time.monotonic()
    all_results: list[dict] = []

    for profile in profiles:
        print(f"\n--- {profile.upper()} ---")
        result = snapshot_profile(
            profile, args.output_dir, upload=args.upload, bucket=args.bucket
        )
        all_results.append(result)

        # Cleanup local files if uploaded successfully
        if args.cleanup and args.upload and result["s3_keys"]:
            if result.get("neo4j") and Path(result["neo4j"]).exists():
                Path(result["neo4j"]).unlink()
                logger.info("Cleaned up local file: %s", result["neo4j"])

    overall_elapsed = time.monotonic() - overall_start

    # Summary
    print(f"\n{'=' * 60}")
    print(f" Snapshot Summary ({overall_elapsed:.1f}s total)")
    print(f"{'=' * 60}")
    for r in all_results:
        status_neo4j = "OK" if r["neo4j"] else "FAILED"
        qdrant_count = len(r.get("qdrant", []))
        status_qdrant = f"OK ({qdrant_count} cols)" if qdrant_count else "FAILED"
        print(f"  {r['profile']:6s}  Neo4j: {status_neo4j:6s}  Qdrant: {status_qdrant:16s}  S3: {len(r['s3_keys'])} files")
        if r["neo4j"]:
            print(f"           {r['neo4j']}")
        for qpath in r.get("qdrant", []):
            print(f"           {qpath}")
        for key in r["s3_keys"]:
            print(f"           → s3://{args.bucket}/{key}")
    print(f"{'=' * 60}\n")

    # Exit code: 0 if all succeeded
    has_errors = any(
        r.get("neo4j_error") or r.get("qdrant_error") or r.get("s3_errors")
        for r in all_results
    )
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()

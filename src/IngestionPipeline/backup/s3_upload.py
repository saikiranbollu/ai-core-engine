"""
Corpus Snapshot — S3 Upload (MEG_SW-101)

Uploads corpus snapshot files to MinIO (S3-compatible) with structured
naming: {profile}/{component}/{profile}-{component}-{timestamp}.{ext}

Usage:
    python -m src.IngestionPipeline.backup.s3_upload --file backups/illd-neo4j-2026-04-20T14-30-00Z.cypher
    python -m src.IngestionPipeline.backup.s3_upload --dir backups/ --bucket corpus-backups
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

from minio import Minio
from minio.error import S3Error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("s3_upload")

DEFAULT_BUCKET = "aicoreengine"

# Pattern: {profile}-{component}-{timestamp}.{ext}  or
#          {profile}-{component}-{collection}-{timestamp}.{ext}
FILENAME_PATTERN = re.compile(
    r"^(?P<profile>illd|mcal)-(?P<component>neo4j|qdrant)"
    r"(?:-(?P<collection>[a-z0-9_]+))?"
    r"-(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)"
    r"\.(?P<ext>\w+)$"
)


def _get_minio_client() -> Minio:
    """Create MinIO/S3-compatible client from environment variables.

    Supports both legacy MINIO_* vars and Ceph S3 vars (RW_ACCESS_KEY/RW_SECRET_KEY).
    Priority: MINIO_* vars > RW_* vars > RO_* vars > defaults.
    """
    endpoint = os.environ.get(
        "MINIO_ENDPOINT",
        os.environ.get("S3_ENDPOINT", "s3muccephp.infineon.com"),
    )
    # Strip protocol prefix if provided
    endpoint = endpoint.replace("http://", "").replace("https://", "")

    access_key = (
        os.environ.get("MINIO_ROOT_USER")
        or os.environ.get("RW_ACCESS_KEY")
        or os.environ.get("RO_ACCESS_KEY")
        or "minioadmin"
    )
    secret_key = (
        os.environ.get("MINIO_ROOT_PASSWORD")
        or os.environ.get("RW_SECRET_KEY")
        or os.environ.get("RO_SECRET_KEY")
        or "minioadmin"
    )

    # Default to secure if using known Ceph endpoint
    if "s3muccephp" in endpoint:
        default_secure = True
    else:
        default_secure = os.environ.get("MINIO_SECURE", "false").lower() == "true"
    secure = os.environ.get("MINIO_SECURE", str(default_secure)).lower() == "true"

    region = os.environ.get("S3_REGION", "us-east-1")

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        cert_check=False,
        region=region,
    )


def _ensure_bucket(client: Minio, bucket: str) -> None:
    """Create bucket if it does not exist."""
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        logger.info("Created bucket: %s", bucket)


def _build_object_key(filepath: Path) -> str:
    """
    Build an S3 object key from the filename.

    Maps: illd-neo4j-2026-04-20T14-30-00Z.cypher
    To:   snapshots/illd/neo4j/illd-neo4j-2026-04-20T14-30-00Z.cypher

    Maps: mcal-qdrant-adc_swa_architecture-2026-04-20T14-30-00Z.snapshot
    To:   snapshots/mcal/qdrant/mcal-qdrant-adc_swa_architecture-2026-04-20T14-30-00Z.snapshot
    """
    match = FILENAME_PATTERN.match(filepath.name)
    if match:
        profile = match.group("profile")
        component = match.group("component")
        return f"snapshots/{profile}/{component}/{filepath.name}"
    # Fallback: put in misc/ if filename doesn't match convention
    logger.warning("Filename '%s' does not match naming convention", filepath.name)
    return f"snapshots/misc/{filepath.name}"


def upload_file(filepath: Path, bucket: str = DEFAULT_BUCKET) -> str:
    """Upload a single file to MinIO. Returns the object key."""
    client = _get_minio_client()
    _ensure_bucket(client, bucket)

    object_key = _build_object_key(filepath)
    file_size = filepath.stat().st_size

    start = time.monotonic()

    # Extract metadata from filename
    match = FILENAME_PATTERN.match(filepath.name)
    metadata = {}
    if match:
        metadata = {
            "x-amz-meta-profile": match.group("profile"),
            "x-amz-meta-component": match.group("component"),
            "x-amz-meta-timestamp": match.group("timestamp"),
        }

    client.fput_object(
        bucket,
        object_key,
        str(filepath),
        metadata=metadata,
    )

    elapsed = time.monotonic() - start
    size_mb = file_size / (1024 * 1024)

    logger.info(
        "Uploaded %s → s3://%s/%s (%.2f MB, %.1fs)",
        filepath.name, bucket, object_key, size_mb, elapsed,
    )
    return object_key


def download_file(
    object_key: str, output_path: Path, bucket: str = DEFAULT_BUCKET
) -> Path:
    """Download a file from MinIO. Returns the local path."""
    client = _get_minio_client()

    start = time.monotonic()
    client.fget_object(bucket, object_key, str(output_path))
    elapsed = time.monotonic() - start

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Downloaded s3://%s/%s → %s (%.2f MB, %.1fs)",
        bucket, object_key, output_path, size_mb, elapsed,
    )
    return output_path


def get_presigned_url(
    object_key: str, bucket: str = DEFAULT_BUCKET, expires: timedelta = timedelta(hours=1)
) -> str:
    """Generate a presigned GET URL for direct access to an S3 object.

    This allows services (e.g. Qdrant) to pull snapshots directly from S3
    without downloading to local disk first.
    """
    client = _get_minio_client()
    url = client.presigned_get_object(bucket, object_key, expires=expires)
    logger.info("Generated presigned URL for s3://%s/%s (expires in %s)", bucket, object_key, expires)
    return url


def list_snapshots(
    bucket: str = DEFAULT_BUCKET, prefix: str = ""
) -> list[dict]:
    """List snapshot objects in the bucket."""
    client = _get_minio_client()
    _ensure_bucket(client, bucket)

    objects = client.list_objects(bucket, prefix=prefix, recursive=True)
    results = []
    for obj in objects:
        results.append({
            "key": obj.object_name,
            "size": obj.size,
            "last_modified": str(obj.last_modified),
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload corpus snapshots to MinIO (S3-compatible storage)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--file",
        type=Path,
        help="Single file to upload",
    )
    group.add_argument(
        "--dir",
        type=Path,
        help="Directory of files to upload (all .cypher and .snapshot files)",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List existing snapshots in the bucket",
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=f"S3 bucket name (default: {DEFAULT_BUCKET})",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Filter prefix for --list (e.g., 'illd/' or 'mcal/neo4j/')",
    )
    args = parser.parse_args()

    if args.list:
        snapshots = list_snapshots(bucket=args.bucket, prefix=args.prefix)
        print(f"\n{'=' * 65}")
        print(f" Snapshots in s3://{args.bucket}/{args.prefix}")
        print(f"{'=' * 65}")
        if not snapshots:
            print("  (none)")
        for s in snapshots:
            size_mb = s["size"] / (1024 * 1024)
            print(f"  {s['key']:50s}  {size_mb:8.2f} MB  {s['last_modified']}")
        print(f"{'=' * 65}\n")
        return

    files_to_upload: list[Path] = []
    if args.file:
        if not args.file.exists():
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        files_to_upload.append(args.file)
    elif args.dir:
        if not args.dir.is_dir():
            print(f"ERROR: Directory not found: {args.dir}", file=sys.stderr)
            sys.exit(1)
        for ext in ("*.cypher", "*.snapshot", "*.json"):
            files_to_upload.extend(args.dir.glob(ext))
        if not files_to_upload:
            print(f"ERROR: No .cypher, .snapshot, or .json files found in {args.dir}", file=sys.stderr)
            sys.exit(1)

    print(f"\n{'=' * 55}")
    print(f" S3 Upload to s3://{args.bucket}")
    print(f" Files: {len(files_to_upload)}")
    print(f"{'=' * 55}")

    uploaded: list[str] = []
    for filepath in sorted(files_to_upload):
        try:
            key = upload_file(filepath, bucket=args.bucket)
            uploaded.append(key)
            print(f"\n  Uploaded: s3://{args.bucket}/{key}")
        except S3Error as exc:
            logger.error("Failed to upload %s: %s", filepath, exc)
            print(f"\n  ERROR [{filepath.name}]: {exc}")

    print(f"\n{'=' * 55}")
    print(f" Summary: {len(uploaded)}/{len(files_to_upload)} files uploaded")
    for key in uploaded:
        print(f"   s3://{args.bucket}/{key}")
    print(f"{'=' * 55}\n")

    sys.exit(0 if len(uploaded) == len(files_to_upload) else 1)


if __name__ == "__main__":
    main()

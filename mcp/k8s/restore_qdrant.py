"""
Qdrant Restore Script — Download from S3 & Restore to target Qdrant

Standalone script for one-time migration. Downloads snapshots from S3 (Ceph)
and restores them to the target Qdrant instance via the snapshot upload API.

Usage (inside K8s job in ai-core-engine namespace):
    python /tmp/restore_qdrant.py
"""

import os
import re
import sys
import time
from pathlib import Path

import httpx
from minio import Minio

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.ai-core-engine.svc.cluster.local:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "s3muccephp.infineon.com")
S3_BUCKET = os.environ.get("S3_BUCKET", "aicoreengine")
RW_ACCESS_KEY = os.environ["RW_ACCESS_KEY"]
RW_SECRET_KEY = os.environ["RW_SECRET_KEY"]
OUTPUT_DIR = Path("/tmp/qdrant-restore")
# Restore all profiles by default, or set RESTORE_PROFILE=illd or mcal
RESTORE_PROFILE = os.environ.get("RESTORE_PROFILE", "all")


def get_minio_client() -> Minio:
    """Create MinIO client configured for Ceph."""
    secure = S3_ENDPOINT.startswith("https://") or "s3muccephp" in S3_ENDPOINT
    endpoint = S3_ENDPOINT.replace("https://", "").replace("http://", "")
    return Minio(
        endpoint,
        access_key=RW_ACCESS_KEY,
        secret_key=RW_SECRET_KEY,
        secure=secure,
        region="us-east-1",
        cert_check=False,
    )


def extract_collection_name(filename: str) -> str:
    """Extract collection name from snapshot filename.
    
    Format: {profile}-qdrant-{collection_name}-{timestamp}.snapshot
    """
    # Remove the profile-qdrant- prefix and -timestamp.snapshot suffix
    match = re.match(r'^(?:illd|mcal)-qdrant-(.+)-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.snapshot$', filename)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot extract collection name from: {filename}")


def list_s3_snapshots(minio_client: Minio) -> list[dict]:
    """List all Qdrant snapshots in S3, grouped by collection (latest only)."""
    snapshots = {}
    
    for profile in ["illd", "mcal"]:
        if RESTORE_PROFILE != "all" and profile != RESTORE_PROFILE:
            continue
        prefix = f"snapshots/{profile}/qdrant/"
        objects = minio_client.list_objects(S3_BUCKET, prefix=prefix)
        for obj in objects:
            try:
                col_name = extract_collection_name(obj.object_name.split("/")[-1])
                # Keep only the latest snapshot per collection
                if col_name not in snapshots or obj.last_modified > snapshots[col_name]["last_modified"]:
                    snapshots[col_name] = {
                        "collection": col_name,
                        "profile": profile,
                        "object_key": obj.object_name,
                        "size": obj.size,
                        "last_modified": obj.last_modified,
                    }
            except ValueError:
                continue
    
    return sorted(snapshots.values(), key=lambda s: s["collection"])


def restore_collection(client: httpx.Client, col_name: str, snapshot_path: Path) -> None:
    """Restore a single collection from a snapshot file via Qdrant upload API."""
    # Delete existing collection if it exists
    resp = client.get(f"{QDRANT_URL}/collections/{col_name}")
    if resp.status_code == 200:
        print(f"    Deleting existing collection '{col_name}'...")
        del_resp = client.delete(f"{QDRANT_URL}/collections/{col_name}")
        del_resp.raise_for_status()
        time.sleep(1)

    # Upload snapshot to recover collection (multipart form)
    print(f"    Uploading snapshot to recover '{col_name}'...")
    with open(snapshot_path, "rb") as f:
        files = {"snapshot": (snapshot_path.name, f, "application/octet-stream")}
        resp = client.post(
            f"{QDRANT_URL}/collections/{col_name}/snapshots/upload",
            files=files,
            params={"priority": "snapshot"},
        )
    resp.raise_for_status()
    
    # Verify
    time.sleep(1)
    info = client.get(f"{QDRANT_URL}/collections/{col_name}")
    info.raise_for_status()
    points = info.json()["result"]["points_count"] or 0
    print(f"    ✓ Restored '{col_name}' ({points} points)")


def main():
    print("=" * 60)
    print(" Qdrant Restore: S3 → Target Qdrant")
    print(f" Target:  {QDRANT_URL}")
    print(f" S3:      {S3_ENDPOINT}/{S3_BUCKET}")
    print(f" Profile: {RESTORE_PROFILE}")
    print("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    minio_client = get_minio_client()

    # List available snapshots
    print("\nScanning S3 for snapshots...")
    snapshots = list_s3_snapshots(minio_client)

    if not snapshots:
        print("No snapshots found in S3!")
        sys.exit(1)

    print(f"\nFound {len(snapshots)} collections to restore:")
    for s in snapshots:
        size_mb = (s["size"] or 0) / (1024 * 1024)
        print(f"  {s['collection']:40s} [{s['profile']}] ({size_mb:.2f} MB)")

    headers = {}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    # Restore each collection
    print("\n--- Restoring ---")
    results = []
    with httpx.Client(verify=False, timeout=600, headers=headers) as client:
        for snap in snapshots:
            col_name = snap["collection"]
            print(f"\n  [{snap['profile']}] {col_name}:")
            try:
                # Download from S3
                local_path = OUTPUT_DIR / snap["object_key"].split("/")[-1]
                print(f"    Downloading from S3...")
                minio_client.fget_object(S3_BUCKET, snap["object_key"], str(local_path))
                
                # Restore
                restore_collection(client, col_name, local_path)
                results.append({"collection": col_name, "status": "ok"})
                
                # Cleanup
                local_path.unlink()
            except Exception as exc:
                print(f"    ✗ FAILED: {exc}")
                results.append({"collection": col_name, "status": "error", "error": str(exc)})

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    print(f"\n{'=' * 60}")
    print(f" Restore Summary: {ok} OK, {failed} FAILED out of {len(results)}")
    print("=" * 60)

    if failed:
        print("\nFailed collections:")
        for r in results:
            if r["status"] == "error":
                print(f"  - {r['collection']}: {r['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

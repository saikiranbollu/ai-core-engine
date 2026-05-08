"""
Qdrant Migration Script — Snapshot & Upload to S3

Standalone script for one-time migration. Connects directly to in-cluster
Qdrant service, creates snapshots, and uploads them to S3 (Ceph).

Usage (inside K8s job):
    python /app/mcp/k8s/migrate_qdrant.py
"""

import os
import sys
import time
from pathlib import Path
from datetime import timedelta

import httpx
from minio import Minio

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.mcswai.svc.cluster.local:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "s3muccephp.infineon.com")
S3_BUCKET = os.environ.get("S3_BUCKET", "aicoreengine")
RW_ACCESS_KEY = os.environ["RW_ACCESS_KEY"]
RW_SECRET_KEY = os.environ["RW_SECRET_KEY"]
OUTPUT_DIR = Path("/tmp/qdrant-snapshots")


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


def classify_collection(name: str) -> str:
    """Classify collection as illd or mcal by naming convention."""
    return "mcal" if "_" in name else "illd"


def list_collections(client: httpx.Client) -> list[dict]:
    """List all Qdrant collections with point counts."""
    resp = client.get(f"{QDRANT_URL}/collections")
    resp.raise_for_status()
    collections = []
    for col in resp.json()["result"]["collections"]:
        name = col["name"]
        try:
            info = client.get(f"{QDRANT_URL}/collections/{name}")
            info.raise_for_status()
            points = info.json()["result"]["points_count"] or 0
        except Exception:
            points = 0
        collections.append({
            "name": name,
            "points": points,
            "profile": classify_collection(name),
        })
    return sorted(collections, key=lambda c: c["name"])


def export_collection(client: httpx.Client, col_name: str, profile: str) -> Path:
    """Create and download a snapshot for a single collection."""
    timestamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    filename = f"{profile}-qdrant-{col_name}-{timestamp}.snapshot"
    output_path = OUTPUT_DIR / filename

    print(f"  Creating snapshot for '{col_name}'...")
    resp = client.post(f"{QDRANT_URL}/collections/{col_name}/snapshots")
    resp.raise_for_status()
    snapshot_name = resp.json()["result"]["name"]

    print(f"  Downloading snapshot '{snapshot_name}'...")
    with client.stream(
        "GET",
        f"{QDRANT_URL}/collections/{col_name}/snapshots/{snapshot_name}",
    ) as download:
        download.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in download.iter_bytes(chunk_size=65536):
                f.write(chunk)

    # Clean up remote snapshot
    client.delete(f"{QDRANT_URL}/collections/{col_name}/snapshots/{snapshot_name}")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ {col_name} → {filename} ({size_mb:.2f} MB)")
    return output_path


def upload_to_s3(minio_client: Minio, local_path: Path, profile: str, col_name: str) -> str:
    """Upload snapshot file to S3."""
    object_key = f"snapshots/{profile}/qdrant/{local_path.name}"
    minio_client.fput_object(
        S3_BUCKET,
        object_key,
        str(local_path),
    )
    print(f"  ↑ Uploaded → s3://{S3_BUCKET}/{object_key}")
    return object_key


def main():
    print("=" * 60)
    print(" Qdrant Migration: Snapshot & Upload to S3")
    print(f" Source:  {QDRANT_URL}")
    print(f" S3:     {S3_ENDPOINT}/{S3_BUCKET}")
    print("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    headers = {}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    minio_client = get_minio_client()

    with httpx.Client(verify=False, timeout=300, headers=headers) as client:
        # Discover collections
        print("\nDiscovering collections...")
        collections = list_collections(client)

        if not collections:
            print("No collections found!")
            sys.exit(1)

        print(f"\nFound {len(collections)} collections:")
        for c in collections:
            print(f"  {c['name']:40s} [{c['profile']}] ({c['points']} points)")

        # Export and upload each
        print("\n--- Exporting & Uploading ---")
        results = []
        for col in collections:
            try:
                path = export_collection(client, col["name"], col["profile"])
                s3_key = upload_to_s3(minio_client, path, col["profile"], col["name"])
                results.append({"collection": col["name"], "status": "ok", "s3_key": s3_key})
                # Remove local file after upload
                path.unlink()
            except Exception as exc:
                print(f"  ✗ FAILED {col['name']}: {exc}")
                results.append({"collection": col["name"], "status": "error", "error": str(exc)})

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    print(f"\n{'=' * 60}")
    print(f" Migration Summary: {ok} OK, {failed} FAILED out of {len(results)}")
    print("=" * 60)

    if failed:
        print("\nFailed collections:")
        for r in results:
            if r["status"] == "error":
                print(f"  - {r['collection']}: {r['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

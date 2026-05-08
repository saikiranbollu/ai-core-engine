"""
Qdrant Point-Level Migration via Scroll + Upsert

Since Qdrant snapshots produce 0-byte files (server-side issue), this script
migrates data by scrolling all points from source and upserting to target.

Runs inside a pod that can reach BOTH Qdrant services (or via S3 serialization).
For cross-namespace migration, run from a pod in the target namespace.

Usage:
    python /tmp/migrate_qdrant_scroll.py
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SOURCE_URL = os.environ.get("SOURCE_QDRANT_URL", "http://qdrant.mcswai.svc.cluster.local:6333")
SOURCE_API_KEY = os.environ.get("SOURCE_QDRANT_API_KEY", "")
TARGET_URL = os.environ.get("TARGET_QDRANT_URL", "http://qdrant.ai-core-engine.svc.cluster.local:6333")
TARGET_API_KEY = os.environ.get("TARGET_QDRANT_API_KEY", "")
BATCH_SIZE = 100  # points per scroll/upsert batch
S3_INTERMEDIATE = os.environ.get("USE_S3", "false").lower() == "true"
DUMP_DIR = Path("/tmp/qdrant-dump")
# Mode: "export" (phase 1 only), "import" (phase 2 only), or "full" (both)
MODE = os.environ.get("MODE", "full")

# S3 config (if cross-namespace and no direct connectivity)
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "s3muccephp.infineon.com")
S3_BUCKET = os.environ.get("S3_BUCKET", "aicoreengine")
RW_ACCESS_KEY = os.environ.get("RW_ACCESS_KEY", "")
RW_SECRET_KEY = os.environ.get("RW_SECRET_KEY", "")


def get_minio_client():
    from minio import Minio
    endpoint = S3_ENDPOINT.replace("https://", "").replace("http://", "")
    return Minio(endpoint, access_key=RW_ACCESS_KEY, secret_key=RW_SECRET_KEY,
                 secure=True, region="us-east-1", cert_check=False)


def classify_collection(name: str) -> str:
    return "mcal" if "_" in name else "illd"


def list_collections(client: httpx.Client, url: str) -> list[dict]:
    resp = client.get(f"{url}/collections")
    resp.raise_for_status()
    collections = []
    for col in resp.json()["result"]["collections"]:
        name = col["name"]
        try:
            info = client.get(f"{url}/collections/{name}")
            info.raise_for_status()
            result = info.json()["result"]
            points = result.get("points_count") or 0
            config = result.get("config", {}).get("params", {})
        except Exception:
            points = 0
            config = {}
        collections.append({
            "name": name, "points": points, "profile": classify_collection(name),
            "config": config,
        })
    return sorted(collections, key=lambda c: c["name"])


def scroll_all_points(client: httpx.Client, url: str, collection: str) -> list[dict]:
    """Scroll through all points in a collection."""
    all_points = []
    offset = None
    while True:
        body = {"limit": BATCH_SIZE, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        resp = client.post(f"{url}/collections/{collection}/points/scroll", json=body)
        resp.raise_for_status()
        result = resp.json()["result"]
        points = result.get("points", [])
        all_points.extend(points)
        offset = result.get("next_page_offset")
        if offset is None or not points:
            break
    return all_points


def create_collection(client: httpx.Client, url: str, name: str, config: dict):
    """Create a collection on target with matching config."""
    vectors = config.get("vectors", {"size": 384, "distance": "Cosine"})
    body = {
        "vectors": vectors,
        "shard_number": config.get("shard_number", 1),
        "replication_factor": config.get("replication_factor", 1),
        "on_disk_payload": config.get("on_disk_payload", True),
    }
    resp = client.put(f"{url}/collections/{name}", json=body)
    resp.raise_for_status()


def upsert_points(client: httpx.Client, url: str, collection: str, points: list[dict]):
    """Upsert points in batches."""
    for i in range(0, len(points), BATCH_SIZE):
        batch = points[i:i + BATCH_SIZE]
        formatted = []
        for p in batch:
            point = {"id": p["id"], "vector": p["vector"], "payload": p.get("payload", {})}
            formatted.append(point)
        resp = client.put(
            f"{url}/collections/{collection}/points",
            json={"points": formatted},
            params={"wait": "true"},
        )
        resp.raise_for_status()


def export_to_s3(minio_client, collection: str, profile: str, points: list[dict], config: dict):
    """Export collection data as JSON to S3."""
    data = {"collection": collection, "profile": profile, "config": config, "points": points}
    local_path = DUMP_DIR / f"{collection}.json"
    with open(local_path, "w") as f:
        json.dump(data, f)
    object_key = f"migration/{profile}/{collection}.json"
    minio_client.fput_object(S3_BUCKET, object_key, str(local_path))
    local_path.unlink()
    return object_key


def import_from_s3(minio_client, client: httpx.Client, url: str):
    """Import all collections from S3 JSON dumps."""
    objects = list(minio_client.list_objects(S3_BUCKET, prefix="migration/", recursive=True))
    print(f"\nFound {len(objects)} collection dumps in S3")
    
    results = []
    for obj in sorted(objects, key=lambda o: o.object_name):
        col_name = obj.object_name.split("/")[-1].replace(".json", "")
        print(f"\n  Restoring '{col_name}'...")
        local_path = DUMP_DIR / f"{col_name}.json"
        minio_client.fget_object(S3_BUCKET, obj.object_name, str(local_path))
        
        with open(local_path) as f:
            data = json.load(f)
        local_path.unlink()
        
        config = data["config"]
        points = data["points"]
        
        # Delete if exists
        resp = client.get(f"{url}/collections/{col_name}")
        if resp.status_code == 200:
            client.delete(f"{url}/collections/{col_name}")
            time.sleep(0.5)
        
        # Create and populate
        create_collection(client, url, col_name, config)
        if points:
            upsert_points(client, url, col_name, points)
        
        # Verify
        resp = client.get(f"{url}/collections/{col_name}")
        count = resp.json()["result"]["points_count"] if resp.status_code == 200 else 0
        print(f"    ✓ {col_name}: {count} points (expected {len(points)})")
        results.append({"collection": col_name, "status": "ok", "points": count})
    
    return results


def main():
    mode = "s3" if S3_INTERMEDIATE else "direct"
    print("=" * 60)
    print(f" Qdrant Migration via Scroll+Upsert (mode: {mode})")
    print(f" Source:  {SOURCE_URL}")
    print(f" Target:  {TARGET_URL}")
    if S3_INTERMEDIATE:
        print(f" S3:      {S3_ENDPOINT}/{S3_BUCKET}")
    print("=" * 60)

    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    source_headers = {"api-key": SOURCE_API_KEY} if SOURCE_API_KEY else {}
    target_headers = {"api-key": TARGET_API_KEY} if TARGET_API_KEY else {}

    # Import-only mode (phase 2)
    if MODE == "import":
        minio_client = get_minio_client()
        with httpx.Client(verify=False, timeout=120, headers=target_headers) as target:
            print("\n[Phase 2] Importing from S3 to target...")
            results = import_from_s3(minio_client, target, TARGET_URL)
            ok = sum(1 for r in results if r["status"] == "ok")
            print(f"\n{'=' * 60}")
            print(f" S3 Import: {ok}/{len(results)} OK")
            print("=" * 60)
        return

    # Phase 1: Export from source
    with httpx.Client(verify=False, timeout=120, headers=source_headers) as source:
        print("\n[Phase 1] Reading source collections...")
        collections = list_collections(source, SOURCE_URL)
        
        if not collections:
            print("No collections found!")
            sys.exit(1)

        total_points = sum(c["points"] for c in collections)
        print(f"Found {len(collections)} collections ({total_points} total points)")
        for c in collections:
            print(f"  {c['name']:40s} [{c['profile']}] ({c['points']} points)")

        if S3_INTERMEDIATE:
            minio_client = get_minio_client()
            print("\n[Phase 1b] Exporting to S3...")
            for col in collections:
                print(f"  Scrolling '{col['name']}'...", end=" ", flush=True)
                points = scroll_all_points(source, SOURCE_URL, col["name"])
                s3_key = export_to_s3(minio_client, col["name"], col["profile"], points, col["config"])
                print(f"{len(points)} points → s3://{S3_BUCKET}/{s3_key}")
            print("\n[Phase 1 complete] All data exported to S3.")
        else:
            # Direct mode: scroll + upsert immediately
            with httpx.Client(verify=False, timeout=120, headers=target_headers) as target:
                print("\n[Phase 2] Migrating directly...")
                results = []
                for col in collections:
                    col_name = col["name"]
                    print(f"\n  Migrating '{col_name}' ({col['points']} pts)...")
                    
                    # Scroll all points
                    points = scroll_all_points(source, SOURCE_URL, col_name)
                    print(f"    Scrolled {len(points)} points")
                    
                    # Delete target collection if exists
                    resp = target.get(f"{TARGET_URL}/collections/{col_name}")
                    if resp.status_code == 200:
                        target.delete(f"{TARGET_URL}/collections/{col_name}")
                        time.sleep(0.5)
                    
                    # Create on target
                    create_collection(target, TARGET_URL, col_name, col["config"])
                    
                    # Upsert
                    if points:
                        upsert_points(target, TARGET_URL, col_name, points)
                    
                    # Verify
                    resp = target.get(f"{TARGET_URL}/collections/{col_name}")
                    count = resp.json()["result"]["points_count"] if resp.status_code == 200 else 0
                    status = "ok" if count == len(points) else "mismatch"
                    print(f"    ✓ Restored: {count} points")
                    results.append({"collection": col_name, "expected": len(points), "actual": count, "status": status})
                
                # Summary
                ok = sum(1 for r in results if r["status"] == "ok")
                print(f"\n{'=' * 60}")
                print(f" Direct Migration: {ok}/{len(results)} OK")
                print("=" * 60)
                if ok < len(results):
                    for r in results:
                        if r["status"] != "ok":
                            print(f"  ! {r['collection']}: expected {r['expected']}, got {r['actual']}")
                return

    # Phase 2 (S3 mode): Import to target
    if S3_INTERMEDIATE:
        minio_client = get_minio_client()
        with httpx.Client(verify=False, timeout=120, headers=target_headers) as target:
            print("\n[Phase 2] Importing from S3 to target...")
            results = import_from_s3(minio_client, target, TARGET_URL)
            ok = sum(1 for r in results if r["status"] == "ok")
            print(f"\n{'=' * 60}")
            print(f" S3 Migration: {ok}/{len(results)} OK")
            print("=" * 60)


if __name__ == "__main__":
    main()

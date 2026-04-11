"""
Jama Relationship Extractor
============================

Fetches upstream and downstream relationships from the Jama REST API for
every item in a previously-exported JSON file (e.g. jama_adc_combined_requirements.json).

The output is a standalone JSON file containing all inter-item relationships
that the Knowledge Graph Builder can consume to create edges like
DERIVES_FROM (PRQâSHRQ), VERIFIED_BY, ASSUMES, RAISED_BY, etc.

Usage:
    python fetch_jama_relationships.py                             # defaults
    python fetch_jama_relationships.py --input jama_adc_combined_requirements.json
    python fetch_jama_relationships.py --input ... --output adc_rels.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Set

# Ensure the repository root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

# Add code dir for sibling module imports (env_config etc.)
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Add repo root for src.* imports
REPO_ROOT = HYBRIDRAG_DIR.parent.parent               # .../ai-core-engine
sys.path.insert(0, str(REPO_ROOT))

from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
from build_knowledge_graph import ProgressBar
from env_config import load_env

# ---------------------------------------------------------------------------
# Load secrets from env/.env
# ---------------------------------------------------------------------------
load_env()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

# Legacy fallbacks (ADC) â prefer module_input_path / module_output_path
DEFAULT_INPUT = JAMA_REQ_DIR / "jama_adc_combined_requirements.json"
DEFAULT_OUTPUT = JAMA_REQ_DIR / "jama_adc_relationships.json"


def module_input_path(module: str) -> Path:
    """Return the expected combined-requirements JSON for *module*."""
    return JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"


def module_output_path(module: str) -> Path:
    """Return the expected relationships-output JSON for *module*."""
    return JAMA_REQ_DIR / f"jama_{module.lower()}_relationships.json"

# Jama connection defaults â read from environment (populated by env/.env)
JAMA_BASE_URL = os.environ.get("JAMA_BASE_URL", "https://rqmprod.intra.infineon.com")
JAMA_API_KEY = os.environ.get("JAMA_API_KEY", "")
JAMA_API_SECRET = os.environ.get("JAMA_API_SECRET", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("jama_rel_fetch")


# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------
def load_items(path: Path) -> List[dict]:
    """Load items from the exported JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_relationships(
    items: List[dict],
    *,
    conn_kwargs: dict,
    item_id_set: Set[int],
    fetch_upstream: bool = True,
    fetch_downstream: bool = True,
    delay: float = 0.1,
    max_workers: int = 10,
    jama_cfg: Dict[str, Any] | None = None,
) -> List[dict]:
    """Fetch all relationships for the given items **in parallel**.

    Each worker thread creates its own ``JamaConnector`` to avoid sharing
    HTTP connections across threads.

    Parameters
    ----------
    connector : JamaConnector
        Used only as a fallback if *jama_cfg* is not provided (single-thread
        degrades gracefully).
    items : list[dict]
        Items loaded from the JSON export (each has an ``id`` key).
    conn_kwargs : dict
        Keyword arguments for creating per-thread JamaConnector instances
        (base_url, api_key, api_secret, verify_ssl, timeout).
    item_id_set : set[int]
        Set of all item IDs present in the export.
    fetch_upstream / fetch_downstream : bool
        Which relationship directions to fetch.
    delay : float
        Seconds to sleep between API calls (per thread).
    max_workers : int
        Number of parallel threads (default: 10).
    jama_cfg : dict | None
        Jama connection settings (base_url, api_key, api_secret, â¦).
        When provided, each thread creates its own connector.

    Returns
    -------
    list[dict]
        De-duplicated relationship records.
    """
    seen_ids: Set[int] = set()
    seen_lock = threading.Lock()
    relationships: List[dict] = []
    rels_lock = threading.Lock()
    errors = 0
    errors_lock = threading.Lock()
    total = len(items)

    progress = ProgressBar(total, prefix="Fetching")
    progress_lock = threading.Lock()

    # Silence per-request logging from JamaConnector and httpx so the
    # single progress bar renders cleanly without interleaved log lines.
    _noisy_loggers = ["aice.ingestion.jama", "httpx", "httpcore"]
    _saved_levels = {name: logging.getLogger(name).level for name in _noisy_loggers}
    for name in _noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    def _fetch_one(item: dict) -> None:
        nonlocal errors
        item_id = item["id"]

        # Each thread gets its own connector to avoid shared-socket issues
        if jama_cfg:
            local_conn = JamaConnector(
                base_url=jama_cfg["base_url"],
                api_key=jama_cfg["api_key"],
                api_secret=jama_cfg["api_secret"],
                verify_ssl=jama_cfg.get("verify_ssl", True),
                timeout=jama_cfg.get("timeout", 120),
            )
            owns_conn = True
        else:
            local_conn = JamaConnector(**conn_kwargs)
            owns_conn = True

        local_rels: List[dict] = []
        try:
            # --- Downstream ---
            if fetch_downstream:
                try:
                    for rel in local_conn.get_downstream_relationships(item_id):
                        rid = rel.get("id")
                        if rid:
                            with seen_lock:
                                if rid in seen_ids:
                                    continue
                                seen_ids.add(rid)
                            local_rels.append(_enrich_relationship(rel, item_id_set))
                except Exception:
                    with errors_lock:
                        errors += 1

            # --- Upstream ---
            if fetch_upstream:
                try:
                    for rel in local_conn.get_upstream_relationships(item_id):
                        rid = rel.get("id")
                        if rid:
                            with seen_lock:
                                if rid in seen_ids:
                                    continue
                                seen_ids.add(rid)
                            local_rels.append(_enrich_relationship(rel, item_id_set))
                except Exception:
                    with errors_lock:
                        errors += 1
        finally:
            if owns_conn:
                local_conn.close()

        if local_rels:
            with rels_lock:
                relationships.extend(local_rels)

    # --- Run in parallel ---------------------------------------------------
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one, item) for item in items]
        for f in as_completed(futures):
            f.result()  # propagate exceptions

    progress.finish()

    # Restore original log levels
    for name, lvl in _saved_levels.items():
        logging.getLogger(name).setLevel(lvl)

    logger.info(
        "Relationship extraction complete: %d unique relationships from %d items (%d errors)",
        len(relationships), total, errors,
    )
    return relationships


def _enrich_relationship(rel: dict, item_id_set: Set[int]) -> dict:
    """Normalise a raw Jama relationship dict.

    The Jama REST API returns relationships with the structure:
        {
            "id": 12345,
            "fromItem": 8822204,
            "toItem": 9519253,
            "relationshipType": 4,
            ...
        }

    We add a boolean ``internal`` flag indicating whether both ends
    are within our dataset.
    """
    from_id = rel.get("fromItem")
    to_id = rel.get("toItem")
    return {
        "relationship_id": rel.get("id"),
        "from_item": from_id,
        "to_item": to_id,
        "relationship_type": rel.get("relationshipType"),
        "internal": (from_id in item_id_set and to_id in item_id_set),
        "suspect": rel.get("suspect", False),
        "raw": rel,
    }


def save_relationships(
    relationships: List[dict],
    item_type_index: Dict[int, int],
    output_path: Path,
) -> None:
    """Persist the relationships to disk.

    Includes a summary header for quick inspection.
    """
    # Build a summary
    internal_count = sum(1 for r in relationships if r["internal"])
    external_count = len(relationships) - internal_count

    # Count by relationship type
    type_counts: Dict[int, int] = {}
    for r in relationships:
        rt = r["relationship_type"]
        type_counts[rt] = type_counts.get(rt, 0) + 1

    output = {
        "metadata": {
            "description": (
                "Jama item relationships extracted via REST API. "
                "Use with build_knowledge_graph.py --relationships flag."
            ),
            "total_relationships": len(relationships),
            "internal_relationships": internal_count,
            "external_relationships": external_count,
            "relationship_type_counts": type_counts,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "item_type_index": {str(k): v for k, v in item_type_index.items()},
        "relationships": relationships,
    }

    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved %d relationships to %s", len(relationships), output_path)
    print(f"\n  Saved {len(relationships)} relationships to {output_path}")
    print(f"    Internal (both ends in dataset) : {internal_count}")
    print(f"    External (one end outside)       : {external_count}")
    print(f"    By relationship type ID          : {type_counts}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch Jama relationships for items in an exported JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--module", "-m",
        type=str,
        default="ADC",
        help=(
            "MCAL module name (e.g. ADC, GPT, SPI). "
            "Resolves --input / --output automatically. Default: ADC."
        ),
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        default=None,
        help="Override input items JSON path (default: jama-req/jama_<module>_combined_requirements.json)",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Override output relationships JSON path (default: jama-req/jama_<module>_relationships.json)",
    )
    parser.add_argument(
        "--base-url",
        default=JAMA_BASE_URL,
        help="Jama server base URL",
    )
    parser.add_argument(
        "--api-key",
        default=JAMA_API_KEY,
        help="Jama API key (client ID)",
    )
    parser.add_argument(
        "--api-secret",
        default=JAMA_API_SECRET,
        help="Jama API secret",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Number of parallel threads for API calls (default: 10)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Seconds to sleep between API calls per thread (default: 0.1)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve module-aware paths
    mod = args.module.upper()
    input_path = args.input or module_input_path(mod)
    output_path = args.output or module_output_path(mod)

    # Anchor bare filenames / relative paths inside jama-req/
    if not input_path.is_absolute():
        input_path = JAMA_REQ_DIR / input_path
    if not output_path.is_absolute():
        output_path = JAMA_REQ_DIR / output_path

    # Ensure the target directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load items
    print(f"\n  Module: {mod}")
    print(f"  Loading items from {input_path}â¦")
    items = load_items(input_path)
    print(f"  Loaded {len(items)} items.")

    # Build item-ID â item_type index (used later by the graph builder)
    item_id_set: Set[int] = set()
    item_type_index: Dict[int, int] = {}
    for item in items:
        iid = item["id"]
        item_id_set.add(iid)
        item_type_index[iid] = item["item_type"]

    # Connect to Jama
    print(f"  Connecting to Jama at {args.base_url}â¦")
    connector = JamaConnector(
        base_url=args.base_url,
        api_key=args.api_key,
        api_secret=args.api_secret,
        verify_ssl=True,
        timeout=120.0,
    )

    try:
        connector.validate_connection()
        print("  Connected!\n")

        print(f"  Fetching relationships for {len(items)} itemsâ¦")
        print(f"  (Using {args.max_workers} parallel threads â ~2 API calls per item)\n")

        jama_cfg = {
            "base_url": args.base_url,
            "api_key": args.api_key,
            "api_secret": args.api_secret,
            "verify_ssl": True,
            "timeout": 120,
        }

        conn_kwargs = {
            "base_url": args.base_url,
            "api_key": args.api_key,
            "api_secret": args.api_secret,
            "verify_ssl": True,
            "timeout": 120.0,
        }

        relationships = fetch_relationships(
            items,
            conn_kwargs=conn_kwargs,
            item_id_set=item_id_set,
            delay=args.delay,
            max_workers=args.max_workers,
            jama_cfg=jama_cfg,
        )

        save_relationships(relationships, item_type_index, output_path)

    finally:
        connector.close()

    print("\n  Done. You can now run:")
    print(f"    python build_knowledge_graph.py --profile mcal --module {mod}\n")


if __name__ == "__main__":
    main()

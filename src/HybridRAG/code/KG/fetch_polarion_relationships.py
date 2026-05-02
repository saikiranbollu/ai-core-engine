#!/usr/bin/env python3
"""
Polarion Relationship Extractor
================================

Extracts relationships from the ``polarion_<module>_combined_requirements.json``
file produced by ``fetch_polarion_requirements.py``.

Unlike Jama, where relationships must be fetched separately via REST API,
Polarion's SOAP API returns ``linkedWorkItems`` directly on each work item.
This script simply normalises those embedded links into the same relationship
JSON format that ``build_knowledge_graph.py`` expects.

Supported link roles:
  - ifxRefines  → equivalent to Jama DERIVES_FROM (PRQ → SHRQ)
  - ifxChild    → parent-child hierarchy
  - ifxImplements, ifxVerify, ifxContained → bonus traceability

Usage:
    python fetch_polarion_relationships.py --module GPT
    python fetch_polarion_relationships.py --module GPT --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Set

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent            # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                            # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                         # .../HybridRAG
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fetch_polarion_relationships")


# ---------------------------------------------------------------------------
# Role → relationship_type mapping
# ---------------------------------------------------------------------------
# Jama uses integer relationship_type (e.g. 4 = related, 2 = derives).
# We keep role strings for Polarion but provide a stable integer mapping
# so the downstream KG builder can treat them consistently.

ROLE_TYPE_MAP: Dict[str, int] = {
    "ifxRefines": 100,       # PRQ → SHRQ (≈ Jama derives_from)
    "ifxChild": 101,         # parent-child hierarchy
    "ifxImplements": 102,    # SWUD → PRQ
    "ifxVerify": 103,        # Test → requirement
    "ifxContained": 104,     # grouping
    "ifxRelatedTo": 105,     # generic related
}


def _load_polarion_items(path: Path) -> List[Dict[str, Any]]:
    """Load items from the combined requirements JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    # Wrapped format: { "metadata": {...}, "items": [...] }
    return data.get("items", [])


def extract_relationships(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Extract all relationships from the linked_workitems embedded in items.

    Returns a de-duplicated list of relationship records.
    """
    item_id_set: Set[str] = {it["id"] for it in items if "id" in it}

    seen: Set[str] = set()
    relationships: List[Dict[str, Any]] = []

    for item in items:
        from_id = item.get("id", "")
        links = item.get("linked_workitems", [])
        if not links:
            continue

        for link in links:
            role = link.get("role", "")
            target_id = link.get("target_id", "")
            if not role or not target_id:
                continue

            # De-duplicate by (from, to, role)
            dedup_key = f"{from_id}|{target_id}|{role}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            relationships.append({
                "relationship_id": dedup_key,
                "from_item": from_id,
                "to_item": target_id,
                "relationship_type": ROLE_TYPE_MAP.get(role, 999),
                "relationship_role": role,
                "internal": (from_id in item_id_set and target_id in item_id_set),
                "suspect": link.get("suspect", False),
            })

    return relationships


def save_relationships(
    relationships: List[Dict[str, Any]],
    module: str,
    output_path: Path,
    input_count: int,
) -> None:
    """Save relationships to JSON with metadata header."""
    internal_count = sum(1 for r in relationships if r["internal"])
    external_count = len(relationships) - internal_count

    # Count by role
    role_counts: Dict[str, int] = {}
    for r in relationships:
        role = r.get("relationship_role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1

    output = {
        "metadata": {
            "source": "polarion",
            "description": (
                "Polarion work item relationships extracted from linked work items. "
                "Use with build_knowledge_graph.py --relationships flag."
            ),
            "module": module,
            "input_item_count": input_count,
            "total_relationships": len(relationships),
            "internal_relationships": internal_count,
            "external_relationships": external_count,
            "relationship_role_counts": role_counts,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "relationships": relationships,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved %d relationships to %s", len(relationships), output_path)
    print(f"\n  Saved {len(relationships)} relationships to {output_path}")
    print(f"    Internal (both ends in dataset) : {internal_count}")
    print(f"    External (one end outside)       : {external_count}")
    print(f"    By role                          : {role_counts}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract relationships from a Polarion combined-requirements JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_polarion_relationships.py --module GPT\n"
            "  python fetch_polarion_relationships.py --module ADC --force\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if the output file already exists.")
    parser.add_argument("--input", "-i", type=Path, default=None,
                        help="Path to the combined requirements JSON (auto-detected from module).")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output path (default: jama-req/polarion_<module>_relationships.json).")
    args = parser.parse_args()

    module = args.module.upper()

    input_path = args.input or (JAMA_REQ_DIR / f"polarion_{module.lower()}_combined_requirements.json")
    output_path = args.output or (JAMA_REQ_DIR / f"polarion_{module.lower()}_relationships.json")

    if output_path.exists() and not args.force:
        logger.info("Output already exists: %s (use --force to re-extract)", output_path)
        return 0

    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        logger.error("Run fetch_polarion_requirements.py --module %s first.", module)
        return 1

    logger.info("Loading items from %s ...", input_path)
    items = _load_polarion_items(input_path)
    logger.info("  Loaded %d items", len(items))

    logger.info("Extracting relationships ...")
    relationships = extract_relationships(items)
    logger.info("  Extracted %d relationships", len(relationships))

    save_relationships(relationships, module, output_path, len(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Automated Jama SHRQ + PRQ Fetch
=================================
Non-interactive replacement for ``testapi.py``.  Given a module name,
automatically discovers the SHRQ and PRQ folders in Jama and recursively
fetches all requirements, saving them to
``jama-req/jama_<module>_combined_requirements.json``.

Folder discovery strategy:
  - **SHRQ**: Container ``7458908`` → sub-folder ``AUTOSAR CP R20-11``
    (``7463354``) → module folder by name (e.g. ``ETH``, ``ADC``).
  - **PRQ**: Container ``7463476`` → module folder by name directly.

Usage:
  python fetch_jama_requirements.py --module ETH
  python fetch_jama_requirements.py --module ADC --dry-run
  python fetch_jama_requirements.py --module DIO --force
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_DIR = Path(__file__).resolve().parent.parent       # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                         # .../HybridRAG
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
from env_config import load_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fetch_jama_requirements")

# ---------------------------------------------------------------------------
# Jama project constants
# ---------------------------------------------------------------------------
PROJECT_ID = 1074                       # AURIX 3G MCAL
SHRQ_CONTAINER_ID = 7458908            # Stakeholder Requirements
SHRQ_AUTOSAR_R20_11_ID = 7463354       # SHRQ → AUTOSAR CP R20-11
PRQ_CONTAINER_ID = 7463476             # Product Requirements
FOLDER_TYPE = 32                        # Jama item-type for folders


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _create_connector() -> JamaConnector:
    """Create and validate a JamaConnector from environment variables."""
    load_env()
    connector = JamaConnector(
        base_url=os.environ.get("JAMA_BASE_URL", "https://rqmprod.intra.infineon.com"),
        api_key=os.environ.get("JAMA_API_KEY", ""),
        api_secret=os.environ.get("JAMA_API_SECRET", ""),
        verify_ssl=True,
        timeout=120.0,
    )
    connector.validate_connection()
    logger.info("Connected to Jama API.")
    return connector


def _find_module_folder(
    connector: JamaConnector,
    parent_id: int,
    module: str,
    label: str,
) -> int | None:
    """List child folders under parent_id and find the one matching module name."""
    logger.info("Scanning %s (ID: %d) for module '%s' ...", label, parent_id, module)
    children = connector.get_children_items(parent_id)
    folders = [c for c in children if c.item_type == FOLDER_TYPE]
    logger.info("  Found %d folders.", len(folders))

    module_upper = module.upper()
    match = next((f for f in folders if f.name.upper() == module_upper), None)

    if match:
        logger.info("  ✓ Matched '%s' → folder ID %d", module, match.id)
        return match.id
    else:
        # Also try case-insensitive startswith for modules like "Fee" vs "FEE"
        match = next(
            (f for f in folders if f.name.upper().startswith(module_upper)),
            None,
        )
        if match:
            logger.info("  ✓ Matched '%s' (prefix) → folder ID %d ('%s')",
                         module, match.id, match.name)
            return match.id

        available = [f.name for f in folders]
        logger.warning("  ✗ No match for '%s'. Available: %s", module, available)
        return None


def _fetch_items(connector: JamaConnector, folder_id: int, label: str) -> list:
    """Recursively fetch all items under a folder."""
    logger.info("Fetching %s from folder %d (recursive) ...", label, folder_id)
    t0 = time.perf_counter()
    items = connector.get_module_items(folder_id, recurse=True)
    elapsed = time.perf_counter() - t0
    logger.info("  Fetched %d items in %.1fs", len(items), elapsed)
    return items


def fetch_module_requirements(
    module: str,
    dry_run: bool = False,
    force: bool = False,
) -> Path | None:
    """Fetch SHRQ + PRQ requirements for a module and save to JSON.

    Parameters
    ----------
    module : str
        MCAL module name (e.g. "ETH", "ADC", "DIO").
    dry_run : bool
        Only discover folders, don't fetch items.
    force : bool
        Re-fetch even if the output file already exists.

    Returns
    -------
    Path | None
        Path to the saved JSON file, or None on failure / dry-run.
    """
    module = module.upper()
    output_file = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"

    # Skip if already fetched
    if output_file.exists() and not force:
        logger.info("Output file already exists: %s (use --force to re-fetch)", output_file)
        return output_file

    connector = _create_connector()

    try:
        # Discover folders
        shrq_folder = _find_module_folder(
            connector, SHRQ_AUTOSAR_R20_11_ID, module,
            "SHRQ (AUTOSAR CP R20-11)",
        )
        prq_folder = _find_module_folder(
            connector, PRQ_CONTAINER_ID, module, "PRQ",
        )

        if not shrq_folder and not prq_folder:
            logger.error("Module '%s' not found in either SHRQ or PRQ containers.", module)
            return None

        if dry_run:
            logger.info("[DRY-RUN] Would fetch from:")
            logger.info("  SHRQ folder: %s", shrq_folder)
            logger.info("  PRQ folder:  %s", prq_folder)
            return None

        # Fetch items
        all_items = []
        if shrq_folder:
            all_items.extend(_fetch_items(connector, shrq_folder, "SHRQ"))
        if prq_folder:
            all_items.extend(_fetch_items(connector, prq_folder, "PRQ"))

        # Filter to requirement items only (exclude folders, text items)
        requirement_items = [i for i in all_items if i.item_type in (57, 58)]
        shrq_count = sum(1 for i in requirement_items if i.item_type == 58)
        prq_count = sum(1 for i in requirement_items if i.item_type == 57)

        logger.info("Total: %d requirements (SHRQ: %d, PRQ: %d)",
                     len(requirement_items), shrq_count, prq_count)

        # Save
        JAMA_REQ_DIR.mkdir(parents=True, exist_ok=True)
        items_data = [asdict(item) for item in requirement_items]
        output_file.write_text(
            json.dumps(items_data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        logger.info("Saved to %s", output_file)
        return output_file

    finally:
        connector.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch SHRQ + PRQ requirements from Jama for a given MCAL module.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_jama_requirements.py --module ETH\n"
            "  python fetch_jama_requirements.py --module ADC --force\n"
            "  python fetch_jama_requirements.py --module DIO --dry-run\n"
        ),
    )
    parser.add_argument("--module", required=True,
                        help="MCAL module name (e.g. ETH, ADC, DIO).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only discover folders, don't fetch items.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if the output file already exists.")
    args = parser.parse_args()

    result = fetch_module_requirements(
        module=args.module,
        dry_run=args.dry_run,
        force=args.force,
    )
    return 0 if result is not None or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())

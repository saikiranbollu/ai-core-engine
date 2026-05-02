#!/usr/bin/env python3
"""
Fetch RC1 Test Specification work items from Polarion (SOAP API).
=================================================================

Queries Polarion for test case work items (ifxITSTTestcase,
ifxConfigurationTestcase, ifxStaticTestcase) scoped to a specific module,
then fetches full details (custom fields, linked work items) per item.

Output: ``jama-req/polarion_{module}_testspec.json``

Usage::

    python fetch_polarion_testspec.py --module GPT
    python fetch_polarion_testspec.py --module DMA --limit 50
    python fetch_polarion_testspec.py --module ADC --types ifxITSTTestcase
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from env_config import load_env
from fetch_polarion_requirements import (
    PolarionSoapClient,
    _parse_basic_workitem,
    _parse_custom_fields,
    strip_html,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLARION_PROJECT = "AURIX_RC1_MCAL"
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"

# Test case types to fetch
TESTCASE_TYPES = [
    "ifxITSTTestcase",
    "ifxConfigurationTestcase",
    "ifxStaticTestcase",
]

# Module → title prefix mapping for scoping test cases
# Test case titles follow: {Prefix}_Tc_Fn_001, {Prefix}_Tc_Conf_001, etc.
MODULE_TITLE_PREFIX = {
    "ADC": "Adc",
    "BFX": "Bfx",
    "BMC": "Bmc",
    "CAN": "Can",
    "CCD": "Ccd",
    "CRC": "Crc",
    "DIO": "Dio",
    "DMA": "Dma",
    "ENCODER": "Encoder",
    "FEE": "Fee",
    "GPT": "Gpt",
    "I2C": "I2c",
    "ICU": "Icu",
    "LIN": "Lin",
    "MCU": "Mcu",
    "MEMACC": "MemAcc",
    "MEM_NVM": "Mem",
    "OCU": "Ocu",
    "PORT": "Port",
    "PWM": "Pwm",
    "RVLIB": "RvLib",
    "SENT": "Sent",
    "SPI": "Spi",
    "TINFRA": "TInfra",
    "UART": "Uart",
    "WDG": "Wdg",
    "XSPI": "XSpi",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("fetch_polarion_testspec")


# ---------------------------------------------------------------------------
# Title-prefix scoping
# ---------------------------------------------------------------------------
def _get_title_prefix(module: str) -> str:
    """Return the title prefix used to scope test cases for a module."""
    prefix = MODULE_TITLE_PREFIX.get(module.upper())
    if prefix:
        return prefix
    # Fallback: capitalize first letter
    return module.capitalize()


# ---------------------------------------------------------------------------
# Fetch logic
# ---------------------------------------------------------------------------
def fetch_module_testspec(
    client: PolarionSoapClient,
    module: str,
    project_id: str = POLARION_PROJECT,
    tc_types: Optional[List[str]] = None,
    limit: int = 0,
) -> Dict[str, Any]:
    """Fetch all test spec items for a module from Polarion.

    Parameters
    ----------
    client : PolarionSoapClient
        Authenticated SOAP client.
    module : str
        MCAL module name (e.g. ``"GPT"``).
    project_id : str
        Polarion project ID.
    tc_types : list of str, optional
        Which test case types to fetch.  Default: all 3 core types.
    limit : int
        Max items per type.  0 = unlimited.

    Returns
    -------
    dict
        ``{"metadata": {...}, "items": [...]}``.
    """
    tc_types = tc_types or TESTCASE_TYPES
    module_upper = module.upper()
    title_prefix = _get_title_prefix(module_upper)

    all_items: List[Dict[str, Any]] = []
    type_counts: Dict[str, int] = {}

    for tc_type in tc_types:
        logger.info("Querying %s for module %s (title prefix: %s_Tc_)…",
                     tc_type, module_upper, title_prefix)

        # Lucene query: project + type + title prefix
        # NOTE: wildcard must be OUTSIDE quotes — quoted strings are exact phrases
        query = (
            f"project.id:{project_id} AND type:{tc_type}"
            f" AND title:{title_prefix}_Tc_*"
        )

        # Retry the batch query itself (Polarion SOAP flakiness)
        elements = None
        for attempt in range(3):
            try:
                elements = client.query_workitems(query, sort="id")
                break
            except Exception as exc:
                if "500" in str(exc) and attempt < 2:
                    logger.warning("  Query retry %d/2 for %s (500 error)", attempt + 1, tc_type)
                    time.sleep(3 * (attempt + 1))
                else:
                    logger.error("  Query failed for %s: %s", tc_type, exc)
                    elements = []
                    break
        if elements is None:
            elements = []
        logger.info("  → %d items returned from query", len(elements))

        # Parse basic info from query results
        basics = []
        for el in elements:
            parsed = _parse_basic_workitem(el)
            if parsed and parsed.get("id"):
                basics.append(parsed)

        if limit and len(basics) > limit:
            logger.info("  Limiting to %d items (from %d)", limit, len(basics))
            basics = basics[:limit]

        # Fetch full details (custom fields) for each item
        logger.info("  Fetching full details for %d items…", len(basics))
        for i, basic in enumerate(basics, 1):
            if i % 50 == 0 or i == len(basics):
                logger.info("    %d / %d", i, len(basics))

            uri = client.make_uri(basic["id"])

            # Retry on transient 500 errors (Polarion SOAP flakiness)
            full_el = None
            for attempt in range(3):
                try:
                    full_el = client.get_workitem_by_uri(uri)
                    break
                except Exception as exc:
                    if "500" in str(exc) and attempt < 2:
                        logger.warning("  Retry %d/2 for %s (500 error)", attempt + 1, basic["id"])
                        time.sleep(2 * (attempt + 1))
                    else:
                        logger.error("  Failed to fetch %s: %s", basic["id"], exc)
                        break
            if full_el is None:
                logger.warning("  Could not fetch %s by URI", basic["id"])
                continue

            # Re-parse from full element (has all fields)
            item = _parse_basic_workitem(full_el)
            customs = _parse_custom_fields(full_el)

            item["raw_fields"] = customs
            item["item_type"] = tc_type
            item["project_id"] = project_id
            item["source"] = "polarion"
            item["module"] = module_upper

            all_items.append(item)

        type_counts[tc_type] = len(basics)

    # Build metadata
    metadata = {
        "source": "polarion",
        "project_id": project_id,
        "module": module_upper,
        "title_prefix": title_prefix,
        "types_queried": tc_types,
        "type_counts": type_counts,
        "total_count": len(all_items),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    return {"metadata": metadata, "items": all_items}


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
def save_testspec_json(data: Dict[str, Any], module: str) -> Path:
    """Save fetched test spec data to JSON."""
    JAMA_REQ_DIR.mkdir(parents=True, exist_ok=True)
    out_path = JAMA_REQ_DIR / f"polarion_{module.lower()}_testspec.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    logger.info("Saved %d items to %s", data["metadata"]["total_count"], out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch RC1 test specifications from Polarion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python fetch_polarion_testspec.py --module GPT\n"
            "  python fetch_polarion_testspec.py --module DMA --limit 50\n"
            "  python fetch_polarion_testspec.py --module ADC --types ifxITSTTestcase ifxStaticTestcase\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--types", nargs="+", default=None,
                        choices=TESTCASE_TYPES,
                        help="Specific test case types to fetch (default: all 3).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max items per type (0 = unlimited).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load environment (credentials)
    load_env()
    server = os.environ.get("POLARION_URL", "https://alm-plr.intra.infineon.com")
    user = (os.environ.get("POLARION_USERNAME") or os.environ.get("IFX_USERNAME", "")).strip().strip('"')
    pwd = (os.environ.get("POLARION_PASSWORD") or os.environ.get("IFX_PASSWORD", "")).strip().strip('"')

    if not user or not pwd:
        logger.error("Credentials not found. Set POLARION_USERNAME/POLARION_PASSWORD "
                      "or IFX_USERNAME/IFX_PASSWORD in env/.env")
        sys.exit(1)

    t0 = time.time()

    # Connect
    client = PolarionSoapClient(server, user, pwd, POLARION_PROJECT)

    try:
        # Fetch
        data = fetch_module_testspec(
            client=client,
            module=args.module,
            tc_types=args.types,
            limit=args.limit,
        )

        # Save
        out_path = save_testspec_json(data, args.module)

        elapsed = time.time() - t0
        meta = data["metadata"]
        print(f"\n{'='*60}")
        print(f"  Fetch complete — module: {meta['module']}")
        print(f"  Title prefix: {meta['title_prefix']}_Tc_*")
        for tc_type, count in meta["type_counts"].items():
            print(f"    {tc_type:<30s}  {count:>5d}")
        print(f"    {'TOTAL':<30s}  {meta['total_count']:>5d}")
        print(f"  Saved to: {out_path}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print(f"{'='*60}\n")
    finally:
        client.close()


if __name__ == "__main__":
    main()

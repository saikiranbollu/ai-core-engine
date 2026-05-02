"""
RC1 Source Code Knowledge Graph Builder
========================================

Thin wrapper around the A3G ``SourceCodeKnowledgeGraphBuilder`` that sets up
RC1-specific paths and delegates all parsing/ingestion to the existing builder.

The A3G builder is already fully parameterized via constructor arguments
(source_dir, sfr_include_dir, module, etc.).  This module:

1. Uses ``RC1SourceRepoFetcher`` to shallow-clone the RC1 source repo
2. Uses ``RC1SumConfigFetcher`` to fetch compile configurations (Sum mode)
3. Uses ``RC1DependencyFetcher`` to fetch cross-module headers
4. Passes RC1-specific paths to ``SourceCodeKnowledgeGraphBuilder``

Usage::

    python rc1_source_code_builder.py --module GPT --profile test --dry-run
    python rc1_source_code_builder.py --module DMA --profile test --sum-mode
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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

TEMP_DATA_DIR = HYBRIDRAG_DIR / "temp" / "temporary_data"

# RC1 project identifier — stamped on every node so RC1 data can be
# distinguished from A3G data sharing the same Neo4j database.
PROJECT = "RC1"

# RC1 SFR device folder name (single device, unlike A3G's multiple TC* variants)
RC1_SFR_DEVICE = "RC1S16"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def build_rc1_source_code(
    module: str,
    neo4j_cfg: dict,
    *,
    dry_run: bool = False,
    sum_mode: bool = False,
    sum_configs: Optional[list] = None,
    force_fetch: bool = False,
    force_incremental: bool = False,
    source_dir: Optional[Path] = None,
    sfr_include_dir: Optional[Path] = None,
):
    """Build the RC1 source code knowledge graph for a module.

    Parameters
    ----------
    module : str
        MCAL module name (e.g. ``"GPT"``, ``"DMA"``).
    neo4j_cfg : dict
        Neo4j connection settings.
    dry_run : bool
        Parse only, no database writes.
    sum_mode : bool
        Fetch and use real Sum (compile) configs from Bitbucket.
    sum_configs : list, optional
        Specific Sum config names to use.  ``None`` → auto-discover.
    force_fetch : bool
        Force re-download of headers even if cached.
    force_incremental : bool
        Skip incremental tracking (force full re-ingestion).
    source_dir : Path, optional
        Override: path to the RC1 module source repo root.
        Default: auto-detect from ``temp/temporary_data/aurix_rc1_sw_mcal_dev_{module}``.
    sfr_include_dir : Path, optional
        Override: path to the SFR include directory.
        Default: auto-detect from ``temp/temporary_data/aurix_rc1_sw_mcal_sfr/ssc/RC1S16/inc``.
    """
    from build_knowledge_graph import SourceCodeKnowledgeGraphBuilder

    module = module.upper()
    mod_lower = module.lower()

    # ── Resolve source directory ──
    if source_dir is None:
        source_dir = TEMP_DATA_DIR / f"aurix_rc1_sw_mcal_dev_{mod_lower}"
    source_dir = Path(source_dir)

    if not source_dir.exists():
        logger.error("RC1 source directory not found: %s", source_dir)
        print(
            f"\n  ERROR: RC1 source directory not found:\n"
            f"  {source_dir}\n\n"
            f"  Clone the source repository first:\n"
            f"    python -c \"from rc1_dependency_fetcher import RC1SourceRepoFetcher; "
            f"RC1SourceRepoFetcher(Path('{TEMP_DATA_DIR}'), '{module}').fetch_source()\"\n"
        )
        sys.exit(1)

    # ── Resolve SFR include directory ──
    if sfr_include_dir is None:
        sfr_base = TEMP_DATA_DIR / "aurix_rc1_sw_mcal_sfr"
        candidate = sfr_base / "ssc" / RC1_SFR_DEVICE / "inc"
        if candidate.is_dir():
            sfr_include_dir = candidate
        else:
            # Try direct device folder (flat structure)
            candidate2 = sfr_base / RC1_SFR_DEVICE
            if candidate2.is_dir():
                sfr_include_dir = candidate2

    # ── Temp directory for intermediate data ──
    temp_dir = HYBRIDRAG_DIR / "temp" / f"rc1_src_{mod_lower}"

    logger.info("=" * 60)
    logger.info("RC1 Source Code KG Builder — module: %s", module)
    logger.info("  Source dir      : %s", source_dir)
    logger.info("  SFR include dir : %s", sfr_include_dir)
    logger.info("  Temp dir        : %s", temp_dir)
    logger.info("  Sum mode        : %s", sum_mode)
    logger.info("  Dry run         : %s", dry_run)
    logger.info("=" * 60)

    # ── Build using A3G's SourceCodeKnowledgeGraphBuilder ──
    # The builder is fully parameterized — it works for RC1 when given
    # the correct paths.  The only difference is the SFR auto-detect
    # fallback (which we bypass by providing sfr_include_dir explicitly).
    builder = SourceCodeKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        module=module,
        source_dir=source_dir,
        dry_run=dry_run,
        temp_dir=temp_dir,
        sfr_include_dir=sfr_include_dir,
        sum_mode=sum_mode,
        sum_configs=sum_configs,
        force_fetch=force_fetch,
        force_incremental=force_incremental,
        project=PROJECT,
    )
    builder.build()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    from build_rc1_knowledge_graph import load_storage_config, get_neo4j_settings

    parser = argparse.ArgumentParser(
        description="Build RC1 source code knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python rc1_source_code_builder.py --module GPT --profile test --dry-run\n"
            "  python rc1_source_code_builder.py --module DMA --profile test --sum-mode\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--profile", "-p", default="test",
                        choices=["mcal", "test", "local"],
                        help="Neo4j profile (default: test).")
    parser.add_argument("--source-dir", type=Path, default=None,
                        help="Override: path to RC1 module source repo.")
    parser.add_argument("--sfr-include-dir", type=Path, default=None,
                        help="Override: path to SFR include directory.")
    parser.add_argument("--sum-mode", action="store_true",
                        help="Use real Sum configs from Bitbucket.")
    parser.add_argument("--sum-configs", nargs="+", default=None,
                        help="Specific Sum config names to use.")
    parser.add_argument("--force-fetch", action="store_true",
                        help="Force re-download of headers.")
    parser.add_argument("--force", action="store_true",
                        help="Skip incremental tracking (full re-ingestion).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse only — no database changes.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    storage_cfg = load_storage_config()
    neo4j_cfg = get_neo4j_settings(args.profile, storage_cfg)

    build_rc1_source_code(
        module=args.module,
        neo4j_cfg=neo4j_cfg,
        dry_run=args.dry_run,
        sum_mode=args.sum_mode,
        sum_configs=args.sum_configs,
        force_fetch=args.force_fetch,
        force_incremental=args.force,
        source_dir=args.source_dir,
        sfr_include_dir=args.sfr_include_dir,
    )


if __name__ == "__main__":
    main()

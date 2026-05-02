"""
RC1 SFR (Special Function Register) Knowledge Graph Builder
=============================================================

Thin wrapper around the A3G ``SFRKnowledgeGraphBuilder`` that sets up
RC1-specific paths and delegates all parsing/ingestion to the existing builder.

Key RC1 differences from A3G:
- SFR repo: ``aurix_rc1_sw_mcal_sfr`` (vs ``aurix3g_sw_mcal_tc4xx_infra_sfr``)
- Device folder: ``RC1S16`` (vs ``TC44xA``, ``TC49xN``, etc.)
- Headers at: ``ssc/RC1S16/inc/Ifx*.h`` (extra ``inc/`` subdirectory)
- Single device (vs A3G's multiple device variants)

Since ``sfr_parsers.py``'s ``discover_devices()`` only matches ``TC*`` folders,
we pass ``devices=["RC1S16"]`` explicitly.  However, sfr_parsers expects
``{device}/Ifx*.h`` directly at the repo root level — and RC1 puts them at
``ssc/RC1S16/inc/Ifx*.h``.  We handle this by creating a symlink or by
pointing sfr_dir at a restructured path where the device folder directly
contains the .h files.

Usage::

    python rc1_sfr_builder.py --module GPT --profile test --dry-run
    python rc1_sfr_builder.py --module DMA --profile test
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
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

# RC1 SFR device folder
RC1_SFR_DEVICE = "RC1S16"

# RC1 project identifier
PROJECT = "RC1"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def _prepare_sfr_dir(sfr_repo_path: Path) -> Path:
    """Prepare an SFR directory layout compatible with ``sfr_parsers.py``.

    ``sfr_parsers.py`` expects::

        <repo_root>/
            <device>/          ← e.g. TC44xA
                IfxAdc_regdef.h
                IfxAdc_bf.h
                IfxAdc_reg.h

    But RC1 SFR repo has::

        aurix_rc1_sw_mcal_sfr/
            ssc/
                RC1S16/
                    inc/       ← extra level!
                        IfxBtm_regdef.h
                        IfxTinfra_bf.h

    Solution: Create a staging directory with a symlink (or copy) so
    ``RC1S16/`` directly contains the .h files.  This avoids modifying
    the generic sfr_parsers.py.

    Returns the staging directory to pass as ``sfr_dir`` to the builder.
    """
    real_inc = sfr_repo_path / "ssc" / RC1_SFR_DEVICE / "inc"
    if not real_inc.is_dir():
        raise FileNotFoundError(
            f"RC1 SFR include directory not found: {real_inc}\n"
            f"Expected headers at: {real_inc}/Ifx*_regdef.h"
        )

    # Create staging directory: temp/rc1_sfr_staging/RC1S16/ → symlink to inc/
    staging_root = HYBRIDRAG_DIR / "temp" / "rc1_sfr_staging"
    staging_device = staging_root / RC1_SFR_DEVICE

    if staging_device.is_symlink() or staging_device.is_dir():
        # Already set up — verify it points to the right place
        if staging_device.is_symlink():
            target = staging_device.resolve()
            if target == real_inc.resolve():
                logger.info("SFR staging symlink already exists: %s → %s", staging_device, real_inc)
                return staging_root
            # Stale symlink — remove and recreate
            staging_device.unlink()
        elif staging_device.is_dir():
            # Direct copy exists — check if it has .h files
            if any(staging_device.glob("Ifx*_regdef.h")):
                logger.info("SFR staging directory already has headers: %s", staging_device)
                return staging_root

    staging_root.mkdir(parents=True, exist_ok=True)

    # Try symlink first (preferred — no disk space, always fresh)
    try:
        staging_device.symlink_to(real_inc, target_is_directory=True)
        logger.info("Created SFR staging symlink: %s → %s", staging_device, real_inc)
        return staging_root
    except OSError:
        # Symlink failed (e.g. no SeCreateSymbolicLinkPrivilege on Windows)
        logger.info("Symlink failed, falling back to junction/copy")

    # Try Windows junction (no admin needed, works on NTFS)
    if sys.platform == "win32":
        try:
            import subprocess
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(staging_device), str(real_inc)],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("Created SFR staging junction: %s → %s", staging_device, real_inc)
            return staging_root
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.info("Junction failed, falling back to directory copy")

    # Last resort: copy the .h files (only headers, not the entire tree)
    staging_device.mkdir(parents=True, exist_ok=True)
    count = 0
    for h_file in real_inc.glob("Ifx*.h"):
        shutil.copy2(h_file, staging_device / h_file.name)
        count += 1
    logger.info("Copied %d SFR headers to staging: %s", count, staging_device)
    return staging_root


def build_rc1_sfr(
    module: str,
    neo4j_cfg: dict,
    *,
    dry_run: bool = False,
    devices: Optional[list] = None,
    force_incremental: bool = False,
    sfr_dir: Optional[Path] = None,
):
    """Build the RC1 SFR knowledge graph for a module.

    Parameters
    ----------
    module : str
        MCAL module name (e.g. ``"GPT"``, ``"DMA"``).
    neo4j_cfg : dict
        Neo4j connection settings.
    dry_run : bool
        Parse only, no database writes.
    devices : list, optional
        Specific devices to process.  Default: ``["RC1S16"]``.
    force_incremental : bool
        Skip incremental tracking (force full re-ingestion).
    sfr_dir : Path, optional
        Override: path to the SFR repo root (must contain device subfolders).
        Default: auto-detect from ``temp/temporary_data/aurix_rc1_sw_mcal_sfr``.
    """
    from build_knowledge_graph import SFRKnowledgeGraphBuilder
    from sfr_parsers import discover_modules

    module = module.upper()

    # ── Resolve SFR directory ──
    if sfr_dir is None:
        sfr_repo_path = TEMP_DATA_DIR / "aurix_rc1_sw_mcal_sfr"
        if not sfr_repo_path.exists():
            logger.error("RC1 SFR repo not found: %s", sfr_repo_path)
            print(
                f"\n  ERROR: RC1 SFR repository not found:\n"
                f"  {sfr_repo_path}\n\n"
                f"  Clone the SFR repository first:\n"
                f"    python -c \"from rc1_dependency_fetcher import RC1SourceRepoFetcher; "
                f"RC1SourceRepoFetcher(Path('{TEMP_DATA_DIR}'), '{module}').fetch_sfr()\"\n"
            )
            sys.exit(1)

        # Prepare staging directory for sfr_parsers.py compatibility
        sfr_dir = _prepare_sfr_dir(sfr_repo_path)
    else:
        sfr_dir = Path(sfr_dir)

    # Default to RC1's single device
    if devices is None:
        devices = [RC1_SFR_DEVICE]

    # ── Check if module has SFR headers ──
    available = discover_modules(sfr_dir, devices[0])
    module_lower = module.lower()
    if not any(m.lower() == module_lower for m in available):
        logger.warning(
            "Module '%s' has no SFR register headers in %s/%s — skipping SFR ingestion. "
            "Available modules: %s",
            module, sfr_dir, devices[0], available,
        )
        return

    logger.info("=" * 60)
    logger.info("RC1 SFR KG Builder — module: %s", module)
    logger.info("  SFR dir    : %s", sfr_dir)
    logger.info("  Devices    : %s", devices)
    logger.info("  Dry run    : %s", dry_run)
    logger.info("=" * 60)

    # ── Build using A3G's SFRKnowledgeGraphBuilder ──
    builder = SFRKnowledgeGraphBuilder(
        neo4j_cfg=neo4j_cfg,
        module=module,
        sfr_dir=sfr_dir,
        dry_run=dry_run,
        devices=devices,
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
        description="Build RC1 SFR knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python rc1_sfr_builder.py --module GPT --profile test --dry-run\n"
            "  python rc1_sfr_builder.py --module DMA --profile test\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--profile", "-p", default="test",
                        choices=["mcal", "test", "local"],
                        help="Neo4j profile (default: test).")
    parser.add_argument("--sfr-dir", type=Path, default=None,
                        help="Override: path to SFR repo (must have device subdirs).")
    parser.add_argument("--devices", nargs="+", default=None,
                        help="Specific device folders to process (default: RC1S16).")
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

    build_rc1_sfr(
        module=args.module,
        neo4j_cfg=neo4j_cfg,
        dry_run=args.dry_run,
        devices=args.devices,
        force_incremental=args.force,
        sfr_dir=args.sfr_dir,
    )


if __name__ == "__main__":
    main()

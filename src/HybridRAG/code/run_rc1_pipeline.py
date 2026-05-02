#!/usr/bin/env python3
"""
RC1 Pipeline Orchestrator
==========================
Runs the full RC1 MCAL ingestion pipeline for a given module in sequence.

Unlike the A3G pipeline (``run_pipeline.py``), this pipeline uses:
  - **Polarion** for requirements and test specifications (not Jama)
  - **RC1-specific** Bitbucket repos for source code and SFR
  - **RC1-specific** QEAX model for EA architecture

Steps:
  0. Fetch Polarion requirements (PRQ + SHRQ)
  1. Extract Polarion relationships
  2. Build base KG (requirements → Neo4j)
  3. Ingest EA architecture (QEAX → Neo4j)
  4. Fetch Polarion test specifications
  5. Ingest test specs (→ Neo4j)
  6. Ingest source code (→ Neo4j)
  7. Ingest SFR headers (→ Neo4j)

Usage::

    python run_rc1_pipeline.py --module GPT --profile test
    python run_rc1_pipeline.py --module GPT --profile test --dry-run
    python run_rc1_pipeline.py --module GPT --profile test --only 4,5
    python run_rc1_pipeline.py --module GPT --profile test --clear
    python run_rc1_pipeline.py --module GPT --profile test --start-from 4
"""
from __future__ import annotations

import argparse
import inspect
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_DIR = Path(__file__).resolve().parent
HYBRIDRAG_DIR = CODE_DIR.parent
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"
TEMP_DIR = HYBRIDRAG_DIR / "temp"
TEMP_DATA_DIR = TEMP_DIR / "temporary_data"
KG_DIR = CODE_DIR / "KG"

RC1_DEFAULT_QEAX = Path(
    r"C:\Users\NairSurajRet\Downloads\master_rc1_sw_mcal.qeax"
)

# Reliable Python executable: prefer the venv that contains this script
_VENV_DIR = HYBRIDRAG_DIR.parents[1] / ".venv"          # ai-core-engine/.venv
if sys.platform == "win32":
    _VENV_PYTHON = _VENV_DIR / "Scripts" / "python.exe"
else:
    _VENV_PYTHON = _VENV_DIR / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

logger = logging.getLogger("rc1_pipeline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cwd: Path, label: str, dry_run: bool = False) -> None:
    """Run a subprocess, stream output, and abort on failure."""
    cmd_str = " ".join(cmd)
    logger.info("┌─ %s", label)
    logger.info("│  cwd: %s", cwd)
    logger.info("│  cmd: %s", cmd_str)

    if dry_run:
        logger.info("│  [DRY-RUN] skipped")
        logger.info("└─ %s (dry-run)\n", label)
        return

    start = time.perf_counter()
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = time.perf_counter() - start

    if result.returncode != 0:
        logger.error("└─ FAILED: %s (exit %d, %.1fs)\n", label, result.returncode, elapsed)
        sys.exit(result.returncode)

    logger.info("└─ %s  (%.1fs)\n", label, elapsed)


def _check_file(path: Path, description: str) -> None:
    """Abort if a required file doesn't exist."""
    if not path.exists():
        logger.error("Required file not found: %s", path)
        logger.error("  → %s", description)
        sys.exit(1)


def _ask_profile() -> str:
    """Interactively ask which Neo4j instance to target."""
    print("\n" + "=" * 64)
    print("  Which Neo4j instance should the pipeline write to?")
    print("  1. test   — Test instance (bolt-passthrough-neo4j-mcswai-test)")
    print("  2. mcal   — Production MCAL (bolt-passthrough-neo4j-mcswai-mcal)")
    print("  3. local  — Local Neo4j Desktop (127.0.0.1:7687)")
    print("=" * 64)
    while True:
        choice = input("  Enter choice [1/2/3] (default=1 → test): ").strip()
        if choice in ("", "1", "test"):
            return "test"
        if choice in ("2", "mcal"):
            return "mcal"
        if choice in ("3", "local"):
            return "local"
        print("  Invalid choice. Please enter 1, 2, or 3.")


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step0_fetch_requirements(module: str, dry_run: bool, force: bool = False) -> None:
    """Fetch PRQ + SHRQ requirements from Polarion."""
    output = JAMA_REQ_DIR / f"polarion_{module.lower()}_combined_requirements.json"
    if output.exists() and not force:
        logger.info("┌─ Step 0: Fetch Polarion requirements")
        logger.info("│  Already exists: %s", output)
        logger.info("│  (use --force to re-fetch)")
        logger.info("└─ Step 0 skipped (file exists)\n")
        return
    _run(
        [PYTHON, "fetch_polarion_requirements.py", "--module", module],
        cwd=KG_DIR,
        label="Step 0: Fetch Polarion requirements (PRQ + SHRQ)",
        dry_run=dry_run,
    )


def step1_extract_relationships(module: str, dry_run: bool, force: bool = False) -> None:
    """Extract relationships from Polarion requirements JSON."""
    output = JAMA_REQ_DIR / f"polarion_{module.lower()}_relationships.json"
    if output.exists() and not force:
        logger.info("┌─ Step 1: Extract Polarion relationships")
        logger.info("│  Already exists: %s", output)
        logger.info("│  (use --force to re-extract)")
        logger.info("└─ Step 1 skipped (file exists)\n")
        return
    if not dry_run:
        _check_file(
            JAMA_REQ_DIR / f"polarion_{module.lower()}_combined_requirements.json",
            "Run Step 0 first or use: python KG/fetch_polarion_requirements.py --module <MODULE>",
        )
    cmd = [PYTHON, "fetch_polarion_relationships.py", "--module", module]
    if force:
        cmd.append("--force")
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 1: Extract Polarion relationships",
        dry_run=dry_run,
    )


def step2_base_kg(module: str, dry_run: bool, clear: bool = False,
                  profile: str = "test", force: bool = False) -> None:
    """Build base KG (requirements → Neo4j)."""
    cmd = [PYTHON, "build_rc1_knowledge_graph.py",
           "--profile", profile, "--module", module]
    if clear:
        cmd.append("--clear")
    if force:
        cmd.append("--force")
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 2: Build base KG (Polarion requirements → Neo4j)",
        dry_run=dry_run,
    )


def step3_ea_ingestion(module: str, dry_run: bool,
                       qeax_path: Path = RC1_DEFAULT_QEAX,
                       profile: str = "test") -> None:
    """Ingest EA architecture from QEAX into Neo4j."""
    cmd = [
        PYTHON, "build_rc1_knowledge_graph.py",
        "--profile", profile, "--module", module,
        "--ingest-ea", "--qeax-path", str(qeax_path),
    ]
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 3: KG ingestion (EA architecture → Neo4j)",
        dry_run=dry_run,
    )


def step4_fetch_testspec(module: str, dry_run: bool, force: bool = False) -> None:
    """Fetch test specifications from Polarion."""
    output = JAMA_REQ_DIR / f"polarion_{module.lower()}_testspec.json"
    if output.exists() and not force:
        logger.info("┌─ Step 4: Fetch Polarion test specifications")
        logger.info("│  Already exists: %s", output)
        logger.info("│  (use --force to re-fetch)")
        logger.info("└─ Step 4 skipped (file exists)\n")
        return
    _run(
        [PYTHON, "fetch_polarion_testspec.py", "--module", module],
        cwd=KG_DIR,
        label="Step 4: Fetch Polarion test specifications",
        dry_run=dry_run,
    )


def step5_testspec_ingestion(module: str, dry_run: bool,
                             profile: str = "test", force: bool = False) -> None:
    """Ingest test specs into Neo4j KG."""
    cmd = [PYTHON, "build_rc1_knowledge_graph.py",
           "--profile", profile, "--module", module,
           "--ingest-testspec"]
    if force:
        cmd.append("--force")
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 5: KG ingestion (test specs → Neo4j)",
        dry_run=dry_run,
    )


def step6_source_ingestion(module: str, dry_run: bool,
                           profile: str = "test", force: bool = False) -> None:
    """Ingest C source code into Neo4j KG."""
    source_dir = TEMP_DATA_DIR / f"aurix_rc1_sw_mcal_dev_{module.lower()}"
    cmd = [PYTHON, "build_rc1_knowledge_graph.py",
           "--profile", profile, "--module", module,
           "--ingest-source", "--source-dir", str(source_dir),
           "--sum-mode"]
    if force:
        cmd.append("--force")
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 6: KG ingestion (source code → Neo4j)",
        dry_run=dry_run,
    )


def step7_sfr_ingestion(module: str, dry_run: bool,
                        profile: str = "test", force: bool = False) -> None:
    """Ingest SFR header files into Neo4j KG."""
    cmd = [PYTHON, "build_rc1_knowledge_graph.py",
           "--profile", profile, "--module", module,
           "--ingest-sfr"]
    if force:
        cmd.append("--force")
    _run(
        cmd,
        cwd=KG_DIR,
        label="Step 7: KG ingestion (SFR → Neo4j)",
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Step Registry
# ---------------------------------------------------------------------------
STEPS: dict[int, dict] = {
    0: {"fn": step0_fetch_requirements,   "label": "Fetch Polarion requirements",          "needs_module": True},
    1: {"fn": step1_extract_relationships,"label": "Extract Polarion relationships",        "needs_module": True},
    2: {"fn": step2_base_kg,              "label": "Build base KG (requirements → Neo4j)",  "needs_module": True},
    3: {"fn": step3_ea_ingestion,         "label": "KG ingestion (EA → Neo4j)",             "needs_module": True, "needs_qeax": True},
    4: {"fn": step4_fetch_testspec,       "label": "Fetch Polarion test specs",             "needs_module": True},
    5: {"fn": step5_testspec_ingestion,   "label": "KG ingestion (test specs → Neo4j)",     "needs_module": True},
    6: {"fn": step6_source_ingestion,     "label": "KG ingestion (source code → Neo4j)",    "needs_module": True},
    7: {"fn": step7_sfr_ingestion,        "label": "KG ingestion (SFR → Neo4j)",            "needs_module": True},
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full RC1 MCAL ingestion pipeline for a module.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Steps:\n"
            "  0. Fetch Polarion requirements (PRQ + SHRQ)\n"
            "  1. Extract Polarion relationships\n"
            "  2. Build base KG (requirements → Neo4j)\n"
            "  3. Ingest EA architecture (QEAX → Neo4j)\n"
            "  4. Fetch Polarion test specifications\n"
            "  5. Ingest test specs (→ Neo4j)\n"
            "  6. Ingest source code (→ Neo4j)\n"
            "  7. Ingest SFR headers (→ Neo4j)\n"
            "\n"
            "Examples:\n"
            "  python run_rc1_pipeline.py --module GPT --profile test\n"
            "  python run_rc1_pipeline.py --module GPT --profile test --clear\n"
            "  python run_rc1_pipeline.py --module GPT --profile test --dry-run\n"
            "  python run_rc1_pipeline.py --module GPT --profile test --only 4,5\n"
            "  python run_rc1_pipeline.py --module GPT --profile test --start-from 3\n"
        ),
    )
    parser.add_argument("--module", "-m", required=True,
                        help="MCAL module name (e.g. GPT, ADC, DMA).")
    parser.add_argument("--profile", type=str, default=None,
                        choices=["test", "mcal", "local"],
                        help="Neo4j target profile. Skips interactive prompt.")
    parser.add_argument("--qeax-path", type=Path, default=RC1_DEFAULT_QEAX,
                        help=f"Path to the RC1 QEAX model file. Default: {RC1_DEFAULT_QEAX}")
    parser.add_argument("--start-from", type=int, default=0, metavar="N",
                        help="Start from step N (skip earlier steps). Default: 0.")
    parser.add_argument("--only", type=str, default=None, metavar="3,5,7",
                        help="Run only these steps (comma-separated). Overrides --start-from.")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing RC1 data for this module before building.")
    parser.add_argument("--force", action="store_true",
                        help="Force re-fetch/re-ingestion (bypass caching and incremental checks).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing them.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging.")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    # Silence noisy loggers
    for _lib in ("httpx", "httpcore", "urllib3", "hpack", "charset_normalizer"):
        logging.getLogger(_lib).setLevel(logging.WARNING)

    module = args.module.upper()
    qeax_path = args.qeax_path

    # Determine Neo4j profile
    if args.profile:
        profile = args.profile
    else:
        profile = _ask_profile()
    print(f"\n  → Target: {profile}\n")

    # Validate QEAX path
    if not qeax_path.exists():
        logger.warning("QEAX file not found: %s — Step 3 (EA) will fail if selected.", qeax_path)

    # Determine which steps to run
    if args.only:
        selected = {int(s.strip()) for s in args.only.split(",")}
    else:
        selected = {k for k in STEPS if k >= args.start_from}

    steps_to_run = sorted(s for s in selected if s in STEPS)

    if not steps_to_run:
        logger.error("No valid steps selected.")
        sys.exit(1)

    # If --clear, also force re-fetch
    if args.clear:
        args.force = True

    # Dependency checks
    req_dependent = {s for s in steps_to_run if s in (1, 2)}
    if req_dependent and 0 not in steps_to_run and not args.dry_run:
        req_file = JAMA_REQ_DIR / f"polarion_{module.lower()}_combined_requirements.json"
        if not req_file.exists():
            logger.error(
                "Polarion requirements file not found: %s\n"
                "  → Include Step 0 or run:\n"
                "    python KG/fetch_polarion_requirements.py --module %s",
                req_file, module,
            )
            sys.exit(1)

    ts_dependent = {s for s in steps_to_run if s == 5}
    if ts_dependent and 4 not in steps_to_run and not args.dry_run:
        ts_file = JAMA_REQ_DIR / f"polarion_{module.lower()}_testspec.json"
        if not ts_file.exists():
            logger.error(
                "Polarion test spec file not found: %s\n"
                "  → Include Step 4 or run:\n"
                "    python KG/fetch_polarion_testspec.py --module %s",
                ts_file, module,
            )
            sys.exit(1)

    # Ensure jama-req/ dir exists
    JAMA_REQ_DIR.mkdir(parents=True, exist_ok=True)

    # Print plan
    print("=" * 64)
    print(f"  RC1 Pipeline  |  Module: {module}")
    print(f"  Target: {profile}")
    print(f"  Steps: {', '.join(str(s) for s in steps_to_run)}")
    for s in steps_to_run:
        print(f"    {s}. {STEPS[s]['label']}")
    if args.clear:
        print("  Mode: CLEAR (clean rebuild)")
    if args.dry_run:
        print("  Mode: DRY-RUN")
    if args.force:
        print("  Mode: FORCE (re-fetch & full re-ingestion)")
    print("=" * 64)
    print()

    # Execute
    pipeline_start = time.perf_counter()

    for step_num in steps_to_run:
        step = STEPS[step_num]
        fn = step["fn"]
        sig = inspect.signature(fn)
        kwargs: dict = {}
        if "clear" in sig.parameters:
            kwargs["clear"] = args.clear
        if "profile" in sig.parameters:
            kwargs["profile"] = profile
        if "force" in sig.parameters:
            kwargs["force"] = args.force
        if step.get("needs_qeax"):
            kwargs["qeax_path"] = qeax_path
        if step["needs_module"]:
            fn(module, args.dry_run, **kwargs)
        else:
            fn(args.dry_run, **kwargs)

    total = time.perf_counter() - pipeline_start

    print("=" * 64)
    print(f"  RC1 Pipeline complete  |  {len(steps_to_run)} steps  |  {total:.1f}s")
    print("=" * 64)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
HybridRAG Pipeline Orchestrator
================================
Runs the full MCAL HybridRAG pipeline for a given module in sequence.

Steps:
  0. Refresh LLM token
  1. Fetch Jama SHRQ + PRQ requirements
  2. Fetch Jama relationships
  3. Build base KG (Jama → Neo4j)
  4. KG ingestion (EA from QEAX → Neo4j)
  5. KG ingestion (Test Spec → Neo4j)
  6. KG ingestion (Source Code → Neo4j)
  7. KG ingestion (SFR → Neo4j)

Usage:
  python run_pipeline.py --module ADC --auto-fetch
  python run_pipeline.py --module ADC --auto-fetch --ref feature/adc-fix
  python run_pipeline.py --module ADC --skip-token --start-from 4
  python run_pipeline.py --module ADC --only 4,5,6,7
  python run_pipeline.py --module ADC --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
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
TESTSPEC_DIR = HYBRIDRAG_DIR / "testspec"
TEMP_DIR = HYBRIDRAG_DIR / "temp"
TEMP_DATA_DIR = TEMP_DIR / "temporary_data"
DEFAULT_QEAX = Path(r"C:\Users\NairSurajRet\Downloads\2.20.0_tc4xx_sw_mcal\2.20.0_tc4xx_sw_mcal.qeax")
VALID_PROFILES = {"mcal", "test", "illd", "local"}

# Reliable Python executable: prefer the venv that contains this script,
# falling back to sys.executable only when no venv is found.
_VENV_DIR = HYBRIDRAG_DIR.parents[1] / ".venv"          # ai-core-engine/.venv
if sys.platform == "win32":
    _VENV_PYTHON = _VENV_DIR / "Scripts" / "python.exe"
else:
    _VENV_PYTHON = _VENV_DIR / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

logger = logging.getLogger("pipeline")


def _ask_profile() -> str:
    """Interactively ask which Neo4j instance to target."""
    print("\n" + "=" * 64)
    print("  Which Neo4j instance should the pipeline write to?")
    print("  1. test   — Test instance (bolt-passthrough-neo4j-mcswai-test)")
    print("  2. mcal   — Production MCAL (bolt-passthrough-neo4j-mcswai-mcal)")
    print("  3. illd   — ILLD (bolt-passthrough-neo4j-mcswai-legato)")
    print("  4. local  — Local Neo4j Desktop (127.0.0.1:7687)")
    print("=" * 64)
    while True:
        choice = input("  Enter choice [1/2/3/4] (default=1 → test): ").strip()
        if choice in ("", "1", "test"):
            return "test"
        if choice in ("2", "mcal"):
            return "mcal"
        if choice in ("3", "illd"):
            return "illd"
        if choice in ("4", "local"):
            return "local"
        print("  Invalid choice. Please enter 1, 2, 3, or 4.")


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


def _run_parallel(tasks: list[dict], dry_run: bool = False) -> None:
    """Run multiple subprocesses in parallel and wait for all to finish."""
    if dry_run:
        for t in tasks:
            logger.info("┌─ %s", t["label"])
            logger.info("│  [DRY-RUN] skipped (parallel)")
            logger.info("└─ %s (dry-run)\n", t["label"])
        return

    logger.info("┌─ Running %d steps in PARALLEL:", len(tasks))
    for t in tasks:
        logger.info("│  • %s", t["label"])

    start = time.perf_counter()
    procs: list[tuple[subprocess.Popen, dict]] = []
    for t in tasks:
        logger.info("│  Starting: %s", t["label"])
        p = subprocess.Popen(
            t["cmd"],
            cwd=str(t["cwd"]),
            env={**os.environ},
        )
        procs.append((p, t))

    # Wait for all
    failed = []
    for p, t in procs:
        rc = p.wait()
        elapsed = time.perf_counter() - start
        if rc != 0:
            logger.error("│  FAILED: %s (exit %d)", t["label"], rc)
            failed.append(t["label"])
        else:
            logger.info("│  Done: %s", t["label"])

    elapsed = time.perf_counter() - start
    if failed:
        logger.error("└─ PARALLEL FAILED: %s  (%.1fs)\n", ", ".join(failed), elapsed)
        sys.exit(1)
    logger.info("└─ Parallel steps complete  (%.1fs)\n", elapsed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_repo_dir(module: str, kind: str) -> Path:
    """Return the Bitbucket repo directory inside temp/temporary_data/.

    *kind* is one of ``arch``, ``design``, ``val``, ``src``.
    Naming convention: ``aurix3g_sw_mcal_tc4xx_{kind}_{module}``
    (with ``dev_`` prefix for arch/design).
    """
    mod = module.lower()
    if kind in ("arch", "design"):
        return TEMP_DATA_DIR / f"aurix3g_sw_mcal_tc4xx_dev_{mod}_{kind}"
    elif kind == "val":
        return TEMP_DATA_DIR / f"aurix3g_sw_mcal_tc4xx_val_{mod}"
    elif kind == "src":
        return TEMP_DATA_DIR / f"aurix3g_sw_mcal_tc4xx_{mod}_src"
    raise ValueError(f"Unknown repo kind: {kind!r}")


def _find_file(directory: Path, glob_pattern: str, description: str) -> Path:
    """Find exactly one file matching *glob_pattern* in *directory*."""
    matches = list(directory.glob(glob_pattern))
    if not matches:
        logger.error("No %s found in %s (pattern: %s)", description, directory, glob_pattern)
        sys.exit(1)
    return matches[0]


def setup_working_dirs(module: str, dry_run: bool = False,
                      auto_fetch: bool = False, ref: str = "master") -> None:
    """Create working directories and copy input files from temp/temporary_data/.

    Creates ``jama-req/`` and ``testspec/`` under HYBRIDRAG_DIR and
    populates them with the required XLSX files from the cloned Bitbucket repos.

    When *auto_fetch* is True, missing repos are shallow-cloned from
    Bitbucket automatically via :class:`SourceRepoFetcher`.
    """
    logger.info("=" * 64)
    logger.info("  SETUP — creating working directories for %s", module)
    logger.info("=" * 64)

    # Auto-fetch repos from Bitbucket if requested or if TEMP_DATA_DIR is empty
    if auto_fetch or not TEMP_DATA_DIR.exists():
        if dry_run:
            logger.info("  [DRY-RUN] Would auto-fetch repos from Bitbucket (ref=%s)", ref)
        else:
            # dependency_fetcher lives in code/KG/ — add to path for import
            _kg_dir = str(CODE_DIR / "KG")
            if _kg_dir not in sys.path:
                sys.path.insert(0, _kg_dir)
            from dependency_fetcher import SourceRepoFetcher
            fetcher = SourceRepoFetcher(TEMP_DATA_DIR, module, ref=ref)
            logger.info("  Auto-fetching repos from Bitbucket (ref=%s) ...", ref)
            paths = fetcher.fetch_all()
            for kind, path in paths.items():
                logger.info("    %s → %s", kind, path)

    if not TEMP_DATA_DIR.exists():
        logger.error(
            "Input directory not found: %s\n"
            "  → Use --auto-fetch or clone the repos manually.",
            TEMP_DATA_DIR,
        )
        sys.exit(1)

    # --- TestSpec XLSX -------------------------------------------------------
    val_dir = _resolve_repo_dir(module, "val")
    if val_dir.exists():
        if not dry_run:
            TESTSPEC_DIR.mkdir(parents=True, exist_ok=True)
        specs_dir = val_dir / "00_Specs"
        search_dir = specs_dir if specs_dir.exists() else val_dir
        src_xlsx = _find_file(search_dir, "TC4xx_SW_MCAL_TS_*.xlsx", "TestSpec XLSX")
        dst_xlsx = TESTSPEC_DIR / src_xlsx.name
        if not dst_xlsx.exists():
            if dry_run:
                logger.info("  [DRY-RUN] Would copy %s → %s", src_xlsx.name, dst_xlsx)
            else:
                shutil.copy2(src_xlsx, dst_xlsx)
                logger.info("  Copied %s → %s", src_xlsx.name, dst_xlsx.name)
        else:
            logger.info("  SKIP (exists): %s", dst_xlsx.name)
    else:
        logger.warning("  Val repo not found: %s — skipping TestSpec setup", val_dir)

    # --- jama-req (just ensure directory exists) -----------------------------
    if not dry_run:
        JAMA_REQ_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("  Setup complete.\n")


def cleanup_working_dirs(module: str, delete_temp: bool = False,
                         dry_run: bool = False) -> None:
    """Move runtime artefacts to ``temp/{MODULE}/`` and remove working dirs."""
    logger.info("=" * 64)
    logger.info("  CLEANUP — archiving artefacts for %s", module)
    logger.info("=" * 64)

    archive = TEMP_DIR / module
    if dry_run:
        logger.info("  [DRY-RUN] Would archive to %s", archive)
        return

    archive.mkdir(parents=True, exist_ok=True)

    # --- jama-req/ → temp/{MODULE}/ -----------------------------------------
    if JAMA_REQ_DIR.exists():
        jama_archive = archive / "jama-req"
        jama_archive.mkdir(parents=True, exist_ok=True)
        for f in JAMA_REQ_DIR.iterdir():
            if f.is_file():
                shutil.move(str(f), str(jama_archive / f.name))
        shutil.rmtree(JAMA_REQ_DIR, ignore_errors=True)
        logger.info("  jama-req/ → %s", jama_archive)

    # --- testspec/ → temp/{MODULE}/ -----------------------------------------
    if TESTSPEC_DIR.exists():
        ts_archive = archive / "testspec"
        ts_archive.mkdir(parents=True, exist_ok=True)
        for f in TESTSPEC_DIR.iterdir():
            if f.is_file():
                shutil.move(str(f), str(ts_archive / f.name))
        shutil.rmtree(TESTSPEC_DIR, ignore_errors=True)
        logger.info("  testspec/ → %s", ts_archive)

    # --- data/.checkpoints/ → temp/{MODULE}/checkpoints/ --------------------
    ckpt_dir = HYBRIDRAG_DIR / "data" / ".checkpoints"
    if ckpt_dir.exists():
        ckpt_archive = archive / "checkpoints"
        ckpt_archive.mkdir(parents=True, exist_ok=True)
        for f in ckpt_dir.iterdir():
            if f.is_file():
                shutil.move(str(f), str(ckpt_archive / f.name))
        shutil.rmtree(HYBRIDRAG_DIR / "data", ignore_errors=True)
        logger.info("  data/.checkpoints/ → %s", ckpt_archive)

    logger.info("  Artefacts archived to %s", archive)

    if delete_temp:
        shutil.rmtree(archive, ignore_errors=True)
        logger.info("  Deleted %s (--delete-temp)", archive)

    logger.info("  Cleanup complete.\n")


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step0_token(dry_run: bool) -> None:
    """Refresh LLM token."""
    _run(
        [PYTHON, "token_manager.py"],
        cwd=CODE_DIR,
        label="Step 1: Refresh LLM token",
        dry_run=dry_run,
    )


def step2_jama_fetch(module: str, dry_run: bool) -> None:
    """Fetch SHRQ + PRQ + Safety + Cybersecurity requirements from Jama."""
    output = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"
    if output.exists():
        logger.info("┌─ Step 2: Jama SHRQ + PRQ + Safety + Cybersecurity fetch")
        logger.info("│  Already exists: %s", output)
        logger.info("│  (use --force-jama to re-fetch)")
        logger.info("└─ Step 2 skipped (file exists)\n")
        return
    _run(
        [PYTHON, "fetch_jama_requirements.py", "--module", module],
        cwd=CODE_DIR / "KG",
        label="Step 2: Fetch Jama SHRQ + PRQ + Safety + Cybersecurity requirements",
        dry_run=dry_run,
    )


def step3_relationships(module: str, dry_run: bool) -> None:
    """Fetch Jama relationships."""
    if not dry_run:
        _check_file(
            JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json",
            "Run Step 2 first or use: python KG/fetch_jama_requirements.py --module <MODULE>"
        )
    _run(
        [PYTHON, "fetch_jama_relationships.py", "--module", module, "-v"],
        cwd=CODE_DIR / "KG",
        label="Step 3: Fetch Jama relationships",
        dry_run=dry_run,
    )


def step4_base_kg(module: str, dry_run: bool, clear: bool = False, profile: str = "test", force: bool = False, project: str | None = None) -> None:
    """Build base KG from Jama data."""
    cmd = [PYTHON, "build_knowledge_graph.py", "--profile", profile, "--module", module, "-v"]
    if clear:
        cmd.append("--clear")
    if force:
        cmd.append("--force")
    if project:
        cmd.extend(["--project", project])
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 3: Build base Knowledge Graph (Jama → Neo4j)",
        dry_run=dry_run,
    )


def step_kg_ea(module: str, dry_run: bool, qeax_path: Path = DEFAULT_QEAX, profile: str = "test", project: str | None = None) -> None:
    """Ingest EA architecture elements from QEAX into Neo4j KG."""
    cmd = [
        PYTHON, "build_knowledge_graph.py",
        "--profile", profile, "--module", module,
        "--ingest-ea", "--qeax-path", str(qeax_path), "-v",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if project:
        cmd.extend(["--project", project])
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 4: KG ingestion (EA → Neo4j)",
        dry_run=False,
    )


def step9_kg_testspec(module: str, dry_run: bool, profile: str = "test", force: bool = False, project: str | None = None) -> None:
    """Ingest test spec into Neo4j KG."""
    cmd = [PYTHON, "build_knowledge_graph.py",
           "--profile", profile, "--module", module,
           "--ingest-testspec", "-v"]
    if force:
        cmd.append("--force")
    if project:
        cmd.extend(["--project", project])
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 5: KG ingestion (Test Spec → Neo4j)",
        dry_run=dry_run,
    )


def step10_kg_source(module: str, dry_run: bool, profile: str = "test", force: bool = False, project: str | None = None) -> None:
    """Ingest C source code into Neo4j KG."""
    source_dir = TEMP_DATA_DIR / f"aurix3g_sw_mcal_tc4xx_{module.lower()}_src"
    cmd = [PYTHON, "build_knowledge_graph.py",
        "--profile", profile, "--module", module,
        "--ingest-source", "--source-dir", str(source_dir),
        "--sum-mode",           # auto-discovers configs from Bitbucket
        "-v",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    if project:
        cmd.extend(["--project", project])
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 6: KG ingestion (Source Code → Neo4j)",
        dry_run=False,
    )


def step11_kg_sfr(module: str, dry_run: bool, profile: str = "test", force: bool = False, project: str | None = None) -> None:
    """Ingest SFR header files into Neo4j KG."""
    sfr_dir = TEMP_DATA_DIR / "aurix3g_sw_mcal_tc4xx_infra_sfr"
    cmd = [
        PYTHON, "build_knowledge_graph.py",
        "--profile", profile, "--module", module,
        "--ingest-sfr", "--sfr-dir", str(sfr_dir), "-v",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    if project:
        cmd.extend(["--project", project])
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 7: KG ingestion (SFR → Neo4j)",
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
STEPS: dict[int, dict] = {
    0:  {"fn": step0_token,          "label": "Refresh LLM token",              "needs_module": False},
    1:  {"fn": step2_jama_fetch,     "label": "Fetch Jama SHRQ + PRQ",          "needs_module": True},
    2:  {"fn": step3_relationships,  "label": "Fetch Jama relationships",       "needs_module": True},
    3:  {"fn": step4_base_kg,        "label": "Build base KG (Jama → Neo4j)",   "needs_module": True},
    4:  {"fn": step_kg_ea,           "label": "KG ingestion (EA → Neo4j)",      "needs_module": True,  "needs_qeax": True},
    5:  {"fn": step9_kg_testspec,    "label": "KG ingestion (Test Spec)",       "needs_module": True},
    6:  {"fn": step10_kg_source,     "label": "KG ingestion (Source Code)",     "needs_module": True},
    7:  {"fn": step11_kg_sfr,        "label": "KG ingestion (SFR → Neo4j)",     "needs_module": True},
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full HybridRAG MCAL pipeline for a module.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_pipeline.py --module ADC\n"
            "  python run_pipeline.py --module ADC --start-from 4\n"
            "  python run_pipeline.py --module ADC --only 4,5,6,7\n"
            "  python run_pipeline.py --module ADC --force-jama\n"
            "  python run_pipeline.py --module ADC --dry-run\n"
        ),
    )
    parser.add_argument("--module", required=True, help="MCAL module name (e.g. DIO, ADC, GPT).")
    parser.add_argument("--qeax-path", type=Path, default=DEFAULT_QEAX,
                        help=f"Path to the QEAX model file. Default: {DEFAULT_QEAX}")
    parser.add_argument("--start-from", type=int, default=0, metavar="N",
                        help="Start from step N (skip earlier steps). Default: 0.")
    parser.add_argument("--only", type=str, default=None, metavar="3,5,7",
                        help="Run only these steps (comma-separated). Overrides --start-from.")
    parser.add_argument("--skip-token", action="store_true",
                        help="Skip Step 0 (token refresh). Use if token is still valid.")
    parser.add_argument("--force-jama", action="store_true",
                        help="Re-fetch Jama requirements even if the file already exists.")
    parser.add_argument("--clear", action="store_true",
                        help="Clean slate: wipe Neo4j & Qdrant before rebuilding.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing them.")
    parser.add_argument("--delete-temp", action="store_true",
                        help="Delete temp/{MODULE}/ after archiving (default: keep for reference).")
    parser.add_argument("--skip-setup", action="store_true",
                        help="Skip automatic working-dir setup (dirs must already exist).")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Skip post-pipeline cleanup.")
    parser.add_argument("--force", action="store_true",
                        help="Force full re-ingestion (bypass incremental hash checks).")
    parser.add_argument("--auto-fetch", action="store_true",
                        help="Auto-clone repos from Bitbucket (no manual git clone needed).")
    parser.add_argument("--ref", type=str, default="master",
                        help="Git ref (branch/tag) to clone. Default: master.")
    parser.add_argument("--profile", type=str, default=None,
                        choices=["test", "mcal", "illd", "local"],
                        help="Neo4j target profile. Skips interactive prompt.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging.")
    parser.add_argument("--project", type=str, default=None,
                        help="Project tag to stamp on all nodes (e.g. A3G, RC1).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # Silence noisy third-party loggers even in verbose mode
    for _lib in ("httpx", "httpcore", "urllib3", "hpack", "charset_normalizer"):
        logging.getLogger(_lib).setLevel(logging.WARNING)

    module = args.module.upper()
    qeax_path = args.qeax_path

    # Determine Neo4j profile (CLI flag or interactive)
    if args.profile:
        profile = args.profile
    else:
        profile = _ask_profile()
    print(f"\n  → Target: {profile}\n")

    # Validate QEAX path
    if not qeax_path.exists():
        logger.warning("QEAX file not found: %s — Step 4 (EA) will fail if selected.", qeax_path)

    # Determine which steps to run
    if args.only:
        selected = {int(s.strip()) for s in args.only.split(",")}
    else:
        selected = {k for k in STEPS if k >= args.start_from}

    if args.skip_token:
        selected.discard(0)

    steps_to_run = sorted(s for s in selected if s in STEPS)

    if not steps_to_run:
        logger.error("No valid steps selected.")
        sys.exit(1)

    # Handle --clear: also implies --force-jama
    if args.clear:
        args.force_jama = True

    # Print plan
    print("=" * 64)
    print(f"  HybridRAG Pipeline  |  Module: {module}")
    print(f"  Target: {profile}")
    print(f"  Steps: {', '.join(str(s) for s in steps_to_run)}")
    if args.clear:
        print("  Mode: CLEAR (clean rebuild)")
    if args.dry_run:
        print("  Mode: DRY-RUN")
    if args.delete_temp:
        print(f"  Mode: DELETE-TEMP (will remove temp/{module}/ after)")
    print("=" * 64)
    print()

    # --- Setup: create working dirs from temp/temporary_data/ ----------------
    if not args.skip_setup:
        setup_working_dirs(module, dry_run=args.dry_run,
                          auto_fetch=args.auto_fetch, ref=args.ref)

    # Handle --force-jama: delete existing file so Step 1 re-fetches
    if args.force_jama and not args.dry_run:
        jama_file = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"
        if jama_file.exists():
            jama_file.unlink()
            logger.info("Removed existing %s (--force-jama)", jama_file.name)

    # If steps 2-4 are selected but step 1 is not, check the Jama file exists
    # Steps 5+ (testspec, source code, SFR) do not need Jama data.
    jama_dependent_steps = {s for s in steps_to_run if 2 <= s <= 4}
    if jama_dependent_steps and 1 not in steps_to_run and not args.dry_run:
        jama_file = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"
        if not jama_file.exists():
            logger.error(
                "Jama requirements file not found: %s\n"
                "  → Include Step 1 or run:\n"
                "    python KG/fetch_jama_requirements.py --module %s",
                jama_file, module,
            )
            sys.exit(1)

    # Execute
    pipeline_start = time.perf_counter()

    for step_num in steps_to_run:
        step = STEPS[step_num]
        fn = step["fn"]
        # Pass clear= to steps that support it
        import inspect
        sig = inspect.signature(fn)
        kwargs: dict = {}
        if "clear" in sig.parameters:
            kwargs["clear"] = args.clear
        if "profile" in sig.parameters:
            kwargs["profile"] = profile
        if "force" in sig.parameters:
            kwargs["force"] = args.force
        if "project" in sig.parameters:
            kwargs["project"] = args.project
        if step.get("needs_qeax"):
            kwargs["qeax_path"] = qeax_path
        if step["needs_module"]:
            fn(module, args.dry_run, **kwargs)
        else:
            fn(args.dry_run, **kwargs)

    total = time.perf_counter() - pipeline_start

    # --- Cleanup: move artefacts to temp/{MODULE}/ and remove working dirs ---
    if not args.skip_cleanup:
        cleanup_working_dirs(module, delete_temp=args.delete_temp,
                             dry_run=args.dry_run)

    print("=" * 64)
    print(f"  Pipeline complete  |  {len(steps_to_run)} steps  |  {total:.1f}s")
    print("=" * 64)


if __name__ == "__main__":
    main()

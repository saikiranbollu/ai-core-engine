#!/usr/bin/env python3
"""
HybridRAG Pipeline Orchestrator
================================
Runs the full MCAL HybridRAG pipeline for a given module in sequence.

Prerequisites (manual, one-time):
  1. Activate venv:  & .venv\\Scripts\\Activate.ps1
  2. Fetch Jama SHRQ/PRQ (interactive):  python testapi.py
     → produces jama-req/jama_<module>_combined_requirements.json

Usage:
  python run_pipeline.py --module DIO
  python run_pipeline.py --module DIO --skip-docx2pdf --skip-token
  python run_pipeline.py --module DIO --start-from 5
  python run_pipeline.py --module DIO --only 7,8,9
  python run_pipeline.py --module DIO --dry-run
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
SWA_DIR = HYBRIDRAG_DIR / "swa"
SWUD_DIR = HYBRIDRAG_DIR / "swud"
JAMA_REQ_DIR = HYBRIDRAG_DIR / "jama-req"
TESTSPEC_DIR = HYBRIDRAG_DIR / "testspec"
TEMP_DIR = HYBRIDRAG_DIR / "temp"
TEMP_DATA_DIR = TEMP_DIR / "temporary_data"

logger = logging.getLogger("pipeline")


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


def setup_working_dirs(module: str, dry_run: bool = False) -> None:
    """Create working directories and copy input files from temp/temporary_data/.

    Creates ``swa/``, ``swud/``, ``jama-req/``, ``testspec/`` under
    HYBRIDRAG_DIR and populates them with the required DOCX / XLSX files
    from the cloned Bitbucket repos.
    """
    logger.info("=" * 64)
    logger.info("  SETUP — creating working directories for %s", module)
    logger.info("=" * 64)

    if not TEMP_DATA_DIR.exists():
        logger.error(
            "Input directory not found: %s\n"
            "  → Clone the Bitbucket repos into temp/temporary_data/ first.",
            TEMP_DATA_DIR,
        )
        sys.exit(1)

    # --- SWA DOCX -----------------------------------------------------------
    arch_dir = _resolve_repo_dir(module, "arch")
    if arch_dir.exists():
        if not dry_run:
            SWA_DIR.mkdir(parents=True, exist_ok=True)
        src_docx = _find_file(arch_dir, "TC4xx_SW_MCAL_SWA_*.docx", "SWA DOCX")
        dst_docx = SWA_DIR / f"TC4xx_SW_MCAL_SWA_{module}.docx"
        if not dst_docx.exists():
            if dry_run:
                logger.info("  [DRY-RUN] Would copy %s → %s", src_docx.name, dst_docx)
            else:
                shutil.copy2(src_docx, dst_docx)
                logger.info("  Copied %s → %s", src_docx.name, dst_docx.name)
        else:
            logger.info("  SKIP (exists): %s", dst_docx.name)
    else:
        logger.warning("  Arch repo not found: %s — skipping SWA setup", arch_dir)

    # --- SWUD DOCX ----------------------------------------------------------
    design_dir = _resolve_repo_dir(module, "design")
    if design_dir.exists():
        if not dry_run:
            SWUD_DIR.mkdir(parents=True, exist_ok=True)
        src_docx = _find_file(design_dir, "TC4xx_SW_MCAL_SWUD_*.docx", "SWUD DOCX")
        dst_docx = SWUD_DIR / f"TC4xx_SW_MCAL_SWUD_{module}.docx"
        if not dst_docx.exists():
            if dry_run:
                logger.info("  [DRY-RUN] Would copy %s → %s", src_docx.name, dst_docx)
            else:
                shutil.copy2(src_docx, dst_docx)
                logger.info("  Copied %s → %s", src_docx.name, dst_docx.name)
        else:
            logger.info("  SKIP (exists): %s", dst_docx.name)
    else:
        logger.warning("  Design repo not found: %s — skipping SWUD setup", design_dir)

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

    # --- SWA sections → temp/{MODULE}/swa_sections/ -------------------------
    swa_sections = archive / "swa_sections"
    if SWA_DIR.exists():
        swa_sections.mkdir(parents=True, exist_ok=True)
        for f in sorted(SWA_DIR.glob("section_*_raw.md")):
            shutil.move(str(f), str(swa_sections / f.name))
        full_md = SWA_DIR / f"TC4xx_SW_MCAL_SWA_{module}.md"
        if full_md.exists():
            shutil.move(str(full_md), str(archive / full_md.name))
        shutil.rmtree(SWA_DIR, ignore_errors=True)
        logger.info("  swa/ → %s", swa_sections)

    # --- SWUD sections → temp/{MODULE}/swud_sections/ -----------------------
    swud_sections = archive / "swud_sections"
    if SWUD_DIR.exists():
        swud_sections.mkdir(parents=True, exist_ok=True)
        for f in sorted(SWUD_DIR.glob("section_*_raw.md")):
            shutil.move(str(f), str(swud_sections / f.name))
        full_md = SWUD_DIR / f"TC4xx_SW_MCAL_SWUD_{module}.md"
        if full_md.exists():
            shutil.move(str(full_md), str(archive / full_md.name))
        shutil.rmtree(SWUD_DIR, ignore_errors=True)
        logger.info("  swud/ → %s", swud_sections)

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


def _remove_md_artifacts(folder: Path, module: str, prefix: str) -> None:
    """Delete generated MD files for *module* from *folder*.

    Removes the main converted markdown and all ``section_*_raw.md`` files
    so the pipeline can regenerate them from the PDF.  Files belonging to
    other modules (e.g. PORT) are left untouched.
    """
    main_md = folder / f"TC4xx_SW_MCAL_{prefix}_{module}.md"
    if main_md.exists():
        main_md.unlink()
        logger.info("Removed %s (--clear)", main_md.name)

    for sect in sorted(folder.glob("section_*_raw.md")):
        sect.unlink()
        logger.info("Removed %s (--clear)", sect.name)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step0_docx2pdf(module: str, dry_run: bool, clear: bool = False) -> None:
    """Unhide reference tags then convert SWA and SWUD DOCX → PDF.

    If *clear* is True, existing PDFs are deleted first so they get
    regenerated from the DOCX source.

    Sub-steps:
      0a. Run unhide_tags.py to clear hidden formatting on [req ...] tags,
          producing *_tags.docx files.
      0b. Convert the _tags.docx files to PDF via Word COM automation.
    """
    # --- Delete existing PDFs when --clear is set ----------------------------
    if clear and not dry_run:
        for folder, prefix in [(SWA_DIR, "SWA"), (SWUD_DIR, "SWUD")]:
            pdf = folder / f"TC4xx_SW_MCAL_{prefix}_{module}.pdf"
            if pdf.exists():
                pdf.unlink()
                logger.info("Removed existing PDF: %s (--clear)", pdf.name)

    # --- 0a: Unhide reference tags -------------------------------------------
    _run(
        [sys.executable, "unhide_tags.py", "--module", module],
        cwd=CODE_DIR / "references",
        label="Step 0a: Unhide reference tags in DOCX",
        dry_run=dry_run,
    )

    # --- 0b: Convert _tags.docx → PDF via Word COM ----------------------------
    # Use the _tags.docx files produced by 0a.  The PDF keeps the original
    # naming (without _tags) so downstream steps don't need to change.
    if not dry_run:
        import pythoncom, win32com.client          # noqa: E401
        pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone — suppress all dialogs

        try:
            for folder, prefix in [(SWA_DIR, "SWA"), (SWUD_DIR, "SWUD")]:
                pdf = folder / f"TC4xx_SW_MCAL_{prefix}_{module}.pdf"
                if pdf.exists():
                    logger.info("SKIP (PDF exists): %s", pdf)
                    continue
                tags_docx = folder / f"TC4xx_SW_MCAL_{prefix}_{module}_tags.docx"
                orig_docx = folder / f"TC4xx_SW_MCAL_{prefix}_{module}.docx"
                docx = tags_docx if tags_docx.exists() else orig_docx
                if not docx.exists():
                    logger.warning("SKIP (not found): %s", docx)
                    continue
                logger.info("Converting %s → %s …", docx.name, pdf.name)
                doc = word.Documents.Open(
                    str(docx.resolve()),
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Visible=False,
                )
                doc.SaveAs2(str(pdf.resolve()), FileFormat=17)  # 17 = wdFormatPDF
                doc.Close(0)  # 0 = wdDoNotSaveChanges
                logger.info("OK: %s (%.1f MB)", pdf.name,
                            pdf.stat().st_size / 1_048_576)
        finally:
            word.Quit()
            pythoncom.CoUninitialize()
    else:
        logger.info("[DRY-RUN] Would convert DOCX → PDF for %s", module)


def step1_token(dry_run: bool) -> None:
    """Refresh LLM token."""
    _run(
        [sys.executable, "token_manager.py"],
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
        [sys.executable, "fetch_jama_requirements.py", "--module", module],
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
        [sys.executable, "fetch_jama_relationships.py", "--module", module, "-v"],
        cwd=CODE_DIR / "KG",
        label="Step 3: Fetch Jama relationships",
        dry_run=dry_run,
    )


def step4_base_kg(module: str, dry_run: bool, clear: bool = False) -> None:
    """Build base KG from Jama data."""
    cmd = [sys.executable, "build_knowledge_graph.py", "--profile", "mcal", "--module", module, "-v"]
    if clear:
        cmd.append("--clear")
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 4: Build base Knowledge Graph (Jama → Neo4j)",
        dry_run=dry_run,
    )


def step5_swa_markdown(module: str, dry_run: bool, clear: bool = False) -> None:
    """Convert SWA PDF → Markdown sections."""
    if clear and not dry_run:
        _remove_md_artifacts(SWA_DIR, module, "SWA")
    pdf = SWA_DIR / f"TC4xx_SW_MCAL_SWA_{module}.pdf"
    if not dry_run:
        _check_file(pdf, "Run Step 0 (DOCX → PDF) first, or place the PDF in swa/.")
    _run(
        [sys.executable, "prepare_markdowns.py",
         "--input", str(pdf),
         "--output-dir", str(SWA_DIR),
         "--split-depth", "3",
         "--reconvert", "-v"],
        cwd=CODE_DIR / "RAG",
        label="Step 5: SWA PDF → Markdown",
        dry_run=dry_run,
    )


def step6_swud_markdown(module: str, dry_run: bool, clear: bool = False) -> None:
    """Convert SWUD PDF → Markdown sections."""
    if clear and not dry_run:
        _remove_md_artifacts(SWUD_DIR, module, "SWUD")
    pdf = SWUD_DIR / f"TC4xx_SW_MCAL_SWUD_{module}.pdf"
    if not dry_run:
        _check_file(pdf, "Run Step 0 (DOCX → PDF) first, or place the PDF in swud/.")
    _run(
        [sys.executable, "prepare_markdowns.py",
         "--input", str(pdf),
         "--output-dir", str(SWUD_DIR),
         "--split-depth", "3",
         "--reconvert", "-v"],
        cwd=CODE_DIR / "RAG",
        label="Step 6: SWUD PDF → Markdown",
        dry_run=dry_run,
    )


def step7_rag_ingestion(module: str, dry_run: bool, clear: bool = False) -> None:
    """Ingest SWA + SWUD sections into Qdrant."""
    cmd = [sys.executable, "rag_ingestion.py", "--module", module, "-v"]
    if clear:
        cmd.append("--clear")
    _run(
        cmd,
        cwd=CODE_DIR / "RAG",
        label="Step 7: RAG ingestion (SWA + SWUD → Qdrant)",
        dry_run=dry_run,
    )


def step8_kg_swa_swud(module: str, dry_run: bool) -> None:
    """Ingest SWA + SWUD into Neo4j KG."""
    _run(
        [sys.executable, "build_knowledge_graph.py",
         "--profile", "mcal", "--module", module,
         "--ingest-swa", "--ingest-swud", "-v"],
        cwd=CODE_DIR / "KG",
        label="Step 8: KG ingestion (SWA + SWUD → Neo4j)",
        dry_run=dry_run,
    )


def step9_kg_testspec(module: str, dry_run: bool) -> None:
    """Ingest test spec into Neo4j KG."""
    _run(
        [sys.executable, "build_knowledge_graph.py",
         "--profile", "mcal", "--module", module,
         "--ingest-testspec", "-v"],
        cwd=CODE_DIR / "KG",
        label="Step 9: KG ingestion (Test Spec → Neo4j)",
        dry_run=dry_run,
    )


def step10_kg_source(module: str, dry_run: bool) -> None:
    """Ingest C source code into Neo4j KG."""
    source_dir = TEMP_DATA_DIR / f"aurix3g_sw_mcal_tc4xx_{module.lower()}_src"
    cmd = [
        sys.executable, "build_knowledge_graph.py",
        "--profile", "mcal", "--module", module,
        "--ingest-source", "--source-dir", str(source_dir), "-v",
    ]
    if dry_run:
        cmd.append("--dry-run")
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 10: KG ingestion (Source Code → Neo4j)",
        dry_run=False,
    )


def step11_kg_sfr(module: str, dry_run: bool) -> None:
    """Ingest SFR header files into Neo4j KG."""
    sfr_dir = TEMP_DATA_DIR / "aurix3g_sw_mcal_tc4xx_infra_sfr"
    cmd = [
        sys.executable, "build_knowledge_graph.py",
        "--profile", "mcal", "--module", module,
        "--ingest-sfr", "--sfr-dir", str(sfr_dir), "-v",
    ]
    if dry_run:
        cmd.append("--dry-run")
    _run(
        cmd,
        cwd=CODE_DIR / "KG",
        label="Step 11: KG ingestion (SFR → Neo4j)",
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
STEPS: dict[int, dict] = {
    0:  {"fn": step0_docx2pdf,      "label": "DOCX → PDF conversion",         "needs_module": True},
    1:  {"fn": step1_token,          "label": "Refresh LLM token",              "needs_module": False},
    2:  {"fn": step2_jama_fetch,     "label": "Fetch Jama SHRQ + PRQ",          "needs_module": True},
    3:  {"fn": step3_relationships,  "label": "Fetch Jama relationships",       "needs_module": True},
    4:  {"fn": step4_base_kg,        "label": "Build base KG (Jama → Neo4j)",   "needs_module": True},
    5:  {"fn": step5_swa_markdown,   "label": "SWA PDF → Markdown",             "needs_module": True,  "parallel_with": 6},
    6:  {"fn": step6_swud_markdown,  "label": "SWUD PDF → Markdown",            "needs_module": True,  "parallel_with": 5},
    7:  {"fn": step7_rag_ingestion,  "label": "RAG ingestion (Qdrant)",         "needs_module": True},
    8:  {"fn": step8_kg_swa_swud,    "label": "KG ingestion (SWA + SWUD)",      "needs_module": True},
    9:  {"fn": step9_kg_testspec,    "label": "KG ingestion (Test Spec)",       "needs_module": True},
    10: {"fn": step10_kg_source,     "label": "KG ingestion (Source Code)",     "needs_module": True},
    11: {"fn": step11_kg_sfr,        "label": "KG ingestion (SFR)",             "needs_module": True},
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
            "  python run_pipeline.py --module DIO\n"
            "  python run_pipeline.py --module DIO --start-from 5\n"
            "  python run_pipeline.py --module DIO --only 7,8,9\n"
            "  python run_pipeline.py --module DIO --skip-docx2pdf\n"
            "  python run_pipeline.py --module DIO --force-jama\n"
            "  python run_pipeline.py --module DIO --dry-run\n"
        ),
    )
    parser.add_argument("--module", required=True, help="MCAL module name (e.g. DIO, ADC, GPT).")
    parser.add_argument("--start-from", type=int, default=0, metavar="N",
                        help="Start from step N (skip earlier steps). Default: 0.")
    parser.add_argument("--only", type=str, default=None, metavar="3,5,7",
                        help="Run only these steps (comma-separated). Overrides --start-from.")
    parser.add_argument("--skip-docx2pdf", action="store_true",
                        help="Skip Step 0 (DOCX→PDF). Use if PDFs already exist.")
    parser.add_argument("--skip-token", action="store_true",
                        help="Skip Step 1 (token refresh). Use if token is still valid.")
    parser.add_argument("--force-jama", action="store_true",
                        help="Re-fetch Jama requirements even if the file already exists.")
    parser.add_argument("--clear", action="store_true",
                        help="Clean slate: delete PDFs, wipe Neo4j & Qdrant before rebuilding.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without executing them.")
    parser.add_argument("--delete-temp", action="store_true",
                        help="Delete temp/{MODULE}/ after archiving (default: keep for reference).")
    parser.add_argument("--skip-setup", action="store_true",
                        help="Skip automatic working-dir setup (dirs must already exist).")
    parser.add_argument("--skip-cleanup", action="store_true",
                        help="Skip post-pipeline cleanup (leave swa/, swud/ etc. in place).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # Silence noisy third-party loggers even in verbose mode
    for _lib in ("httpx", "httpcore", "urllib3", "hpack", "charset_normalizer"):
        logging.getLogger(_lib).setLevel(logging.WARNING)

    module = args.module.upper()

    # Determine which steps to run
    if args.only:
        selected = {int(s.strip()) for s in args.only.split(",")}
    else:
        selected = {k for k in STEPS if k >= args.start_from}

    if args.skip_docx2pdf:
        selected.discard(0)
    if args.skip_token:
        selected.discard(1)

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
        setup_working_dirs(module, dry_run=args.dry_run)

    # Handle --force-jama: delete existing file so Step 2 re-fetches
    if args.force_jama and not args.dry_run:
        jama_file = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"
        if jama_file.exists():
            jama_file.unlink()
            logger.info("Removed existing %s (--force-jama)", jama_file.name)

    # If steps 3-9 are selected but step 2 is not, check the Jama file exists
    # Steps 10+ (source code, SFR) do not need Jama data.
    jama_dependent_steps = {s for s in steps_to_run if 3 <= s <= 9}
    if jama_dependent_steps and 2 not in steps_to_run and not args.dry_run:
        jama_file = JAMA_REQ_DIR / f"jama_{module.lower()}_combined_requirements.json"
        if not jama_file.exists():
            logger.error(
                "Jama requirements file not found: %s\n"
                "  → Include Step 2 or run:\n"
                "    python KG/fetch_jama_requirements.py --module %s",
                jama_file, module,
            )
            sys.exit(1)

    # Execute
    pipeline_start = time.perf_counter()
    executed = set()

    for step_num in steps_to_run:
        if step_num in executed:
            continue

        step = STEPS[step_num]
        partner = step.get("parallel_with")

        # If this step has a parallel partner and both are selected, run them together
        if partner is not None and partner in steps_to_run and partner not in executed:
            swa_pdf = SWA_DIR / f"TC4xx_SW_MCAL_SWA_{module}.pdf"
            swud_pdf = SWUD_DIR / f"TC4xx_SW_MCAL_SWUD_{module}.pdf"
            if not args.dry_run:
                _check_file(swa_pdf, "Run Step 0 (DOCX → PDF) first, or place the PDF in swa/.")
                _check_file(swud_pdf, "Run Step 0 (DOCX → PDF) first, or place the PDF in swud/.")

            _run_parallel(
                [
                    {
                        "label": "Step 5: SWA PDF → Markdown",
                        "cmd": [sys.executable, "prepare_markdowns.py",
                                "--input", str(swa_pdf),
                                "--output-dir", str(SWA_DIR),
                                "--reconvert", "-v"],
                        "cwd": CODE_DIR / "RAG",
                    },
                    {
                        "label": "Step 6: SWUD PDF → Markdown",
                        "cmd": [sys.executable, "prepare_markdowns.py",
                                "--input", str(swud_pdf),
                                "--output-dir", str(SWUD_DIR),
                                "--reconvert", "-v"],
                        "cwd": CODE_DIR / "RAG",
                    },
                ],
                dry_run=args.dry_run,
            )
            executed.add(step_num)
            executed.add(partner)
        else:
            fn = step["fn"]
            # Pass clear= to steps that support it (0, 4, 5, 6, 7)
            import inspect
            sig = inspect.signature(fn)
            kwargs: dict = {}
            if "clear" in sig.parameters:
                kwargs["clear"] = args.clear
            if step["needs_module"]:
                fn(module, args.dry_run, **kwargs)
            else:
                fn(args.dry_run, **kwargs)
            executed.add(step_num)

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

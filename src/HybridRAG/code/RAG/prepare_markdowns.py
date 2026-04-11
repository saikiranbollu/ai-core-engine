#!/usr/bin/env python3
"""
PDF → Section-wise Markdowns  (Document-Agnostic)
===================================================
Stage 0 of the RAG pipeline.

Given **any** PDF, this script:

  1. Converts the entire PDF to a single Markdown file via the LLM-based
     ``pdf_pipeline.convert_pdf_to_md``.
  2. Automatically detects section headings (``## 3.1.3 Title``, etc.)
     from the generated Markdown — **no hardcoded page ranges needed**.
  3. Splits the Markdown into per-section files at a configurable heading
     depth (default: split at 2-component numbers like 3.1, 3.2, …).
  4. Stores the section files in the output directory using the naming
     convention  ``section_<number>_raw.md``.

The section files are then consumed by the RAG ingestion scripts
(``mcal_rag_ingestion.py``, ``swa_ingestion.py``) for chunking and
indexing into ChromaDB.

Usage
-----
::

  # Convert a PDF and auto-split into sections (auto-discovers PDF)
  python prepare_markdowns.py --input ../swa/TC4xx_SW_MCAL_SWA_Adc.pdf

  # Use an already-converted full markdown (skip PDF→MD step)
  python prepare_markdowns.py --input ../swa/TC4xx_SW_MCAL_SWA_Adc.md

  # Split at deeper heading level (e.g. 3.1.3, 3.1.5, …)
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --split-depth 3

  # Only keep certain sections from the split
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --sections 3.2 3.3

  # Write section files to a custom directory
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --output-dir ../swud

  # Force re-conversion of the PDF even if full .md already exists
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --reconvert

  # Dry-run: preview detected sections without writing files
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --dry-run

  # After splitting, trigger RAG ingestion automatically
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --ingest

  # Specify page range for PDF→MD conversion (0-based)
  python prepare_markdowns.py --input ../swa/SomeDoc.pdf \\
      --start-page 36 --max-pages 250

Environment
-----------
  LLAMA_TOKEN   – Bearer token for the LLM API (required for PDF→MD)
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/RAG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

# Ensure the code dir is on sys.path so sibling modules resolve
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("prepare_markdowns")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Regex patterns (reused from mcal_rag_ingestion)                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Page-marker lines inserted by pdf_pipeline: ``## Pages 36-37``
PAGE_MARKER_RE = re.compile(
    r"^##\s+Pages?\s+(\d+)(?:\s*[-–]\s*(\d+))?\s*$",
    re.MULTILINE,
)

# Markdown heading with a section number:
#   ## 3.2 Dynamic view
#   ### 3.1.3.2.1 ADC: Conversion complete notification …
#   # 3.1.3 Architectural decisions
MD_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+"
    r"(?P<number>\d+(?:\.\d+)+)\s+"
    r"(?P<title>.+?)\s*$",
    re.MULTILINE,
)

# Fallback: any markdown heading that is NOT a page-batch marker.
# Used when the LLM conversion didn't preserve section numbers.
UNNUMBERED_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<title>(?!Pages?\s+\d).+?)\s*$",
    re.MULTILINE,
)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Data class for a detected section                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@dataclass
class DetectedSection:
    """A section detected from heading analysis of the markdown."""
    number: str              # e.g. "3.1.3"
    title: str               # e.g. "Architectural Decisions"
    depth: int               # number of components (3.1.3 → 3)
    start_offset: int        # character offset in full markdown
    end_offset: int = -1     # filled in after all headings parsed
    content: str = ""        # filled in during split


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Stage 1 — PDF → full Markdown                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def ensure_full_markdown(
    input_path: Path,
    batch_size: int = 2,
    reconvert: bool = False,
    start_page: int = 0,
    max_pages: int = 0,
) -> Path:
    """
    If *input_path* is a PDF, convert it to Markdown using the LLM pipeline.
    If it's already ``.md``, return it as-is (unless ``--reconvert``).

    The output markdown is placed alongside the input file with the same
    stem and a ``.md`` extension.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".md":
        logger.info("Input is already Markdown: %s", input_path.name)
        return input_path

    if suffix not in (".pdf",):
        logger.error("Unsupported file type: %s (expected .pdf or .md)", suffix)
        sys.exit(1)

    md_path = input_path.with_suffix(".md")

    # Reuse existing conversion unless --reconvert
    if md_path.exists() and not reconvert:
        logger.info("Full Markdown already exists: %s (use --reconvert to redo)",
                     md_path.name)
        return md_path

    # Backup before reconversion
    if md_path.exists() and reconvert:
        backup = md_path.with_suffix(".md.bak")
        if backup.exists():
            backup.unlink()
        md_path.rename(backup)
        logger.info("Backed up existing MD → %s", backup.name)

    from pdf_pipeline import convert_pdf_to_md

    logger.info("Converting PDF → full Markdown: %s", input_path.name)
    t0 = time.time()
    convert_pdf_to_md(
        pdf_path=input_path,
        output_path=md_path,
        batch_size=batch_size,
        start_page=start_page,
        max_pages=max_pages,
    )
    elapsed = time.time() - t0
    logger.info("PDF → MD complete in %.1fs → %s", elapsed, md_path.name)
    return md_path


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Stage 2 — Detect sections from Markdown headings                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def detect_sections(md_text: str) -> list[DetectedSection]:
    """
    Parse all numbered headings from the markdown text and return them
    as ``DetectedSection`` objects sorted by document order.

    Every heading that matches ``## N.N.N Title`` (any depth) is collected.

    If **no** numbered headings are found (e.g. the LLM conversion lost
    section numbers), falls back to unnumbered headings (skipping
    ``## Pages X-Y`` batch markers) and assigns synthetic section numbers.
    """
    sections: list[DetectedSection] = []

    for m in MD_HEADING_RE.finditer(md_text):
        number = m.group("number")
        title = m.group("title").strip()
        depth = len(number.split("."))

        sections.append(DetectedSection(
            number=number,
            title=title,
            depth=depth,
            start_offset=m.start(),
        ))

    # Fallback: use unnumbered headings when no numbered ones are found
    if not sections:
        logger.warning("No numbered headings found — falling back to "
                       "unnumbered headings (skipping page markers).")
        counters: dict[int, int] = {}  # depth → running counter
        for m in UNNUMBERED_HEADING_RE.finditer(md_text):
            title = m.group("title").strip()
            hashes = m.group("hashes")
            depth = len(hashes)

            # Assign synthetic hierarchical section number
            counters.setdefault(depth, 0)
            counters[depth] += 1
            # Reset deeper counters when a shallower heading appears
            for d in list(counters):
                if d > depth:
                    counters[d] = 0
            number = ".".join(
                str(counters.get(d, 0)) for d in range(min(counters), depth + 1)
            )

            sections.append(DetectedSection(
                number=number,
                title=title,
                depth=depth,  # align with numbered: # =1, ## =2, ### =3
                start_offset=m.start(),
            ))
        if sections:
            logger.info("Fallback detected %d unnumbered headings", len(sections))

    # Fill end_offset: each section ends where the next one starts
    for i, sec in enumerate(sections):
        if i + 1 < len(sections):
            sec.end_offset = sections[i + 1].start_offset
        else:
            sec.end_offset = len(md_text)

    logger.info("Detected %d headings in markdown", len(sections))
    return sections


def choose_split_depth(sections: list[DetectedSection],
                       requested_depth: int | None = None) -> int:
    """
    Decide at which heading depth to split into separate files.

    If the user supplied ``--split-depth``, use that.  Otherwise pick the
    shallowest depth that produces at least 3 sections (avoiding a single
    giant file).  Fall back to depth 2 if nothing else works.
    """
    if requested_depth is not None:
        return requested_depth

    from collections import Counter
    depth_counts = Counter(s.depth for s in sections)
    # Try each depth from shallowest to deepest
    for d in sorted(depth_counts.keys()):
        if depth_counts[d] >= 3:
            logger.info("Auto-selected split depth %d (%d sections at that level)",
                        d, depth_counts[d])
            return d

    fallback = min(depth_counts.keys()) if depth_counts else 2
    logger.info("Fallback split depth: %d", fallback)
    return fallback


def split_at_depth(
    md_text: str,
    sections: list[DetectedSection],
    split_depth: int,
    target_sections: list[str] | None = None,
) -> list[DetectedSection]:
    """
    Return only the sections at the chosen *split_depth* with their
    content filled in.  Each section's content includes everything from
    its heading until the next heading at the same or shallower depth.

    Sub-headings within a section are included in that section's content.

    Parameters
    ----------
    target_sections
        If provided, only include sections whose number is in this list
        (e.g. ``["3.2", "3.3"]``).
    """
    # Filter to the split depth
    split_sections = [s for s in sections if s.depth == split_depth]

    if not split_sections:
        logger.warning("No sections found at depth %d", split_depth)
        return []

    # Recalculate end_offset so each top-level section spans until the
    # next section at the same or shallower depth (not just the next heading)
    for i, sec in enumerate(split_sections):
        # Find the next heading at same or shallower depth after this one
        found_end = len(md_text)
        for other in sections:
            if other.start_offset > sec.start_offset and other.depth <= split_depth:
                if other.number != sec.number:
                    found_end = other.start_offset
                    break
        sec.end_offset = found_end
        sec.content = md_text[sec.start_offset:sec.end_offset].strip()

    # Apply section filter
    if target_sections:
        split_sections = [s for s in split_sections if s.number in target_sections]

    logger.info("Split depth %d → %d section file(s)", split_depth, len(split_sections))
    return split_sections


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Stage 3 — Write section files                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _clean_stale_section_files(output_dir: Path) -> int:
    """
    Remove existing ``section_*_raw.md`` files from *output_dir* to
    prevent stale data from a previous module run from persisting.

    Returns the number of files removed.
    """
    removed = 0
    for f in output_dir.glob("section_*_raw.md"):
        f.unlink()
        removed += 1
    if removed:
        logger.info("Cleaned %d stale section file(s) from %s", removed, output_dir)
    return removed


def write_section_files(
    split_sections: list[DetectedSection],
    output_dir: Path,
    dry_run: bool = False,
) -> list[Path]:
    """
    Write each section to ``output_dir/section_<num>_raw.md``.

    Removes any existing ``section_*_raw.md`` files first to prevent
    stale data from a previous module run.

    Returns the list of written file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        _clean_stale_section_files(output_dir)

    written: list[Path] = []

    for sec in split_sections:
        safe_num = sec.number.replace(".", "_")
        filename = f"section_{safe_num}_raw.md"
        md_path = output_dir / filename

        logger.info("─" * 60)
        logger.info("Section %s — %s", sec.number, sec.title)
        logger.info("  Content: %d chars, output → %s", len(sec.content), filename)

        if dry_run:
            logger.info("  [DRY-RUN] Would write %s", filename)
            written.append(md_path)
            continue

        md_path.write_text(sec.content, encoding="utf-8")
        logger.info("  ✓ Written → %s", filename)
        written.append(md_path)

    return written


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Optional: Trigger RAG Ingestion                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def run_rag_ingestion(md_files: list[Path], dry_run: bool = False) -> None:
    """
    After section markdown generation, trigger ``mcal_rag_ingestion.py``
    for each produced file.
    """
    if not md_files:
        logger.warning("No markdown files to ingest.")
        return

    ingestion_script = SCRIPT_DIR / "rag_ingestion.py"
    if not ingestion_script.exists():
        logger.error("Ingestion script not found: %s", ingestion_script)
        return

    for md_path in md_files:
        if not md_path.exists():
            logger.warning("Markdown not found (skipping): %s", md_path)
            continue

        logger.info("─" * 60)
        logger.info("Ingesting: %s", md_path.name)

        cmd = [sys.executable, str(ingestion_script), "--input", str(md_path)]
        if dry_run:
            cmd.append("--dry-run")

        logger.info("  CMD: %s", " ".join(cmd))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                logger.info("  ✓ Ingestion succeeded")
            else:
                logger.error("  ✗ Ingestion failed (exit %d)", result.returncode)
                if result.stderr:
                    for line in result.stderr.strip().splitlines()[-5:]:
                        logger.error("    %s", line)
        except subprocess.TimeoutExpired:
            logger.error("  ✗ Ingestion timed out after 600s")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Document-agnostic PDF → section-wise Markdown splitter.\n\n"
            "Stage 0 of the RAG pipeline:\n"
            "  1. PDF → full Markdown  (LLM-based, via pdf_pipeline)\n"
            "  2. Detect section headings automatically\n"
            "  3. Split into per-section Markdown files\n"
            "  4. (optional) Trigger RAG ingestion"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python prepare_markdowns.py --input ../swa/TC4xx_SW_MCAL_SWA_Adc.pdf\n"
            "  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --split-depth 3\n"
            "  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --sections 3.2 3.3\n"
            "  python prepare_markdowns.py --input ../swa/SomeDoc.md --dry-run\n"
            "  python prepare_markdowns.py --input ../swa/SomeDoc.pdf --ingest\n"
        ),
    )
    ap.add_argument(
        "--input", type=Path, required=True, dest="input_path",
        help="Path to the source PDF or a pre-converted full Markdown file.",
    )
    ap.add_argument(
        "--output-dir", type=Path, default=None,
        help="Directory for section markdowns (default: same folder as input).",
    )
    ap.add_argument(
        "--split-depth", type=int, default=None,
        help="Heading depth at which to split (e.g. 2 → '3.1', '3.2'; "
             "3 → '3.1.3', '3.1.5').  Auto-detected if omitted.",
    )
    ap.add_argument(
        "--sections", nargs="+", default=None,
        help="Only keep these sections (e.g. --sections 3.2 3.3).",
    )
    ap.add_argument(
        "--batch-size", type=int, default=2,
        help="Pages per LLM batch for PDF→MD (default: 2).",
    )
    ap.add_argument(
        "--start-page", type=int, default=0,
        help="0-based page to start PDF conversion from (default: 0).",
    )
    ap.add_argument(
        "--max-pages", type=int, default=0,
        help="Max pages to convert (0 = all). Useful for quick tests.",
    )
    ap.add_argument(
        "--reconvert", action="store_true",
        help="Force re-conversion of the PDF even if full .md exists.",
    )
    ap.add_argument(
        "--ingest", action="store_true",
        help="After splitting, trigger RAG ingestion for each section file.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Preview detected sections without writing files or calling LLM.",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging.",
    )
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        # Silence noisy HTTP libraries that dump base64 PDF content
        for _lib in ("httpx", "httpcore", "urllib3", "hpack", "charset_normalizer"):
            logging.getLogger(_lib).setLevel(logging.WARNING)

    # Resolve input path
    input_path = args.input_path
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path
    input_path = input_path.resolve()

    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        return 1

    # Output directory defaults to the input file's parent
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = input_path.parent
    elif not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir = output_dir.resolve()

    logger.info("=" * 60)
    logger.info("PREPARE MARKDOWNS (document-agnostic)")
    logger.info("Input : %s", input_path.name)
    logger.info("Output: %s", output_dir)
    logger.info("=" * 60)

    # ── Step 1: PDF → full Markdown ────────────────────────────────────
    full_md_path = ensure_full_markdown(
        input_path,
        batch_size=args.batch_size,
        reconvert=args.reconvert,
        start_page=args.start_page,
        max_pages=args.max_pages,
    )

    md_text = full_md_path.read_text(encoding="utf-8")
    if not md_text.strip():
        logger.error("Markdown file is empty: %s", full_md_path)
        return 1

    logger.info("Full markdown: %d chars", len(md_text))

    # ── Step 2: Detect section headings ────────────────────────────────
    all_sections = detect_sections(md_text)
    if not all_sections:
        logger.error("No numbered section headings detected in the markdown.")
        return 1

    # Print summary of detected heading depths
    from collections import Counter
    depth_counts = Counter(s.depth for s in all_sections)
    for d in sorted(depth_counts):
        logger.info("  Depth %d (%s): %d headings",
                     d, ".".join(["N"] * d), depth_counts[d])

    # ── Step 3: Choose split depth & split ─────────────────────────────
    split_depth = choose_split_depth(all_sections, args.split_depth)
    split_sections = split_at_depth(
        md_text, all_sections, split_depth,
        target_sections=args.sections,
    )

    if not split_sections:
        logger.error("No sections to write after filtering.")
        return 1

    # Preview: show what sections were found
    logger.info("")
    logger.info("Sections to write:")
    for sec in split_sections:
        logger.info("  %s  %-40s  (%d chars)", sec.number, sec.title[:40], len(sec.content))

    # ── Step 4: Write section files ────────────────────────────────────
    md_files = write_section_files(split_sections, output_dir, dry_run=args.dry_run)

    logger.info("=" * 60)
    logger.info("SECTION SPLIT %s", "PREVIEW" if args.dry_run else "COMPLETE")
    logger.info("Files: %d section markdown(s)", len(md_files))
    logger.info("=" * 60)

    # ── Step 5: Optional RAG ingestion ─────────────────────────────────
    if args.ingest:
        logger.info("")
        logger.info("=" * 60)
        logger.info("TRIGGERING RAG INGESTION")
        logger.info("=" * 60)
        run_rag_ingestion(md_files, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())

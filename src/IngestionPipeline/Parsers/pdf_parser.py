"""
PDF-to-Markdown Parser (LLM-assisted, Parallel, with Checkpoints)
==================================================================

Converts image-based PDF documents to Markdown by rendering each page as a
high-resolution image (400 DPI) and sending **batches of 2 pages** to an
OpenAI-compatible vision model.

Batch size is hardcoded to **2** — this is the optimal setting that produces
the most accurate Markdown with zero hallucinations.  Anything higher causes
the LLM to hallucinate or miss content.

Features:
  - Parallel batch processing via ``ThreadPoolExecutor``
  - Checkpoint / resume capability (survives crashes mid-document)
  - Retry logic with exponential backoff (3 retries per batch)
  - Detailed diagram/figure explanation in output
  - Automatic header/footer/watermark removal

**Requires** ``PyMuPDF``, ``langchain-openai``, and ``httpx``.

Usage::

    from IngestionPipeline.parsers import pdf_parser

    # Minimal — uses token_manager for automatic auth
    pages = pdf_parser.parse("report.pdf")

    # With explicit configuration
    pages = pdf_parser.parse(
        "report.pdf",
        api_key="...",
        base_url="https://gpt4ifx.icp.infineon.com",
        model="gpt-4o",
        max_workers=2,
    )
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
import httpx
from langchain_openai import ChatOpenAI

from ..config import get_max_workers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (NOT user-configurable)
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gpt-5.2"
_API_TIMEOUT = 300       # 5 minutes per API call
_MAX_RETRIES = 3         # retry failed batches up to 3 times
_BATCH_SIZE = 2          # hardcoded — optimal for accuracy, no hallucinations
_DPI = 400               # high-resolution page rendering

# Auto-detect Infineon CA bundle (shared cert, not user-specific)
_CA_BUNDLE_PATH = Path(__file__).resolve().parents[2] / "HybridRAG" / "code" / "ca-bundle.crt"

_PROMPT_TEMPLATE = """\
Convert these PDF pages ({pages}) to Markdown.

CRITICAL RULES - FOLLOW STRICTLY:

1. Retain all text content that is ACTUAL DOCUMENT CONTENT (not headers/footers).
2. Retain all tables, mathematical formulas, and structure exactly as seen.
3. FOR ALL DIAGRAMS/FIGURES (Figure 1, Figure 2, etc.):
   - Identify each diagram/figure in the PDF.
   - Explain the diagram in DETAIL in SIMPLE WORDS - as if someone asked \
you "Please explain this diagram to me".
   - Start with: What is this diagram showing overall?
   - Explain what each COMPONENT/BLOCK does (main purpose).
   - Explain how COMPONENTS ARE CONNECTED to each other.
   - Explain the LOGIC and FLOW (step by step, what happens first, then \
what, etc.).
   - Explain WHY things are connected this way and how they work together.
   - Use simple language that a beginner can understand.
   - Focus on MEANING, LOGIC, and FUNCTION - NOT visual design details \
like colors or shapes.

4. STRICTLY REMOVE ALL HEADERS AND FOOTERS:
   - Remove ANY text that appears at the top or bottom of every page \
(page headers/footers)
   - Remove ANY classification/restriction marks (like "restricted", \
"confidential", "internal only")
   - Remove ANY document metadata (version numbers, dates, document IDs, \
RC numbers)
   - Remove page numbers
   - Remove ANY text that repeats identically in the same position across \
multiple pages
   - Remove company logos, branding, watermarks
   - If you see the same text appearing at the top of pages 1, 2, 3, \
etc. - DELETE IT

5. STRICTLY PRESERVE SECTION HIERARCHY WITH MARKDOWN HEADINGS:
   - The PDF contains numbered sections and subsections (e.g. 1, 1.1, \
1.1.1, 2, 2.1, etc.).
   - You MUST replicate the EXACT same hierarchy using Markdown heading \
hashes: # for top-level sections, ## for subsections, ### for \
sub-subsections, and so on.
   - Match the nesting depth precisely — if the PDF shows "3.2.1 Title" \
that is a ### heading, not # or ##.
   - Keep the original section numbers in the heading text \
(e.g. "## 1.1 Overview").
   - Do NOT flatten the hierarchy — every level must be preserved.
   - Do NOT skip heading levels (e.g. jumping from # to ###).

KEEP ONLY: Main content like sections, explanations, tables, figures with \
descriptions, TOC, and actual data.

Output ONLY valid Markdown. Group diagrams with their explanations clearly \
labeled."""


# ---------------------------------------------------------------------------
# Checkpoint manager — survives crashes mid-document
# ---------------------------------------------------------------------------

class _CheckpointManager:
    """Persists progress so a long PDF conversion can be resumed."""

    def __init__(self, pdf_name: str, checkpoint_dir: Path | None = None):
        self._dir = checkpoint_dir or (
            Path.home() / ".cache" / "aice_pdf_parser" / "checkpoints"
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / f"{pdf_name}.json"

    def save(self, batch_idx: int, results: dict) -> None:
        data = {
            "last_batch_idx": batch_idx,
            "timestamp": datetime.now().isoformat(),
            "results": results,
        }
        with open(self._file, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def load(self) -> tuple[int, dict]:
        if self._file.exists():
            with open(self._file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("last_batch_idx", -1), data.get("results", {})
        return -1, {}

    def clear(self) -> None:
        if self._file.exists():
            self._file.unlink()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_client(
    api_key: str,
    base_url: str,
    model: str,
    ca_bundle: Optional[str] = None,
) -> ChatOpenAI:
    """Create a ``ChatOpenAI`` client with timeout and optional CA bundle."""
    # Auto-detect CA bundle if not explicitly provided
    if not ca_bundle and _CA_BUNDLE_PATH.exists():
        ca_bundle = str(_CA_BUNDLE_PATH)
    verify: bool | str = ca_bundle if ca_bundle else False

    # Strip trailing /v1 — gpt4ifx serves at /chat/completions (root)
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    http_client = httpx.Client(verify=verify, timeout=httpx.Timeout(_API_TIMEOUT))

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_completion_tokens=13000,
        temperature=0,
        http_client=http_client,
        request_timeout=_API_TIMEOUT,
    )


def _render_batch_images(doc: fitz.Document, page_indices: list[int]) -> list[dict]:
    """Render pages to base64-encoded PNG images at ``_DPI`` resolution."""
    images = []
    for pg in page_indices:
        pix = doc[pg].get_pixmap(dpi=_DPI)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        images.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    return images


def _process_batch(
    client: ChatOpenAI,
    images: list,
    batch_pages: list[int],
    retry: int = 0,
) -> str:
    """Send a batch of page images to the LLM and return Markdown.

    Retries up to ``_MAX_RETRIES`` times with a 5-second backoff.
    """
    prompt = _PROMPT_TEMPLATE.format(
        pages=", ".join(str(p + 1) for p in batch_pages)
    )
    messages = [
        {"role": "user", "content": [{"type": "text", "text": prompt}] + images}
    ]

    try:
        logger.info("Processing pages %s (attempt %d)…", batch_pages, retry + 1)
        resp = client.invoke(messages)
        return getattr(resp, "content", "")
    except Exception as exc:
        if retry < _MAX_RETRIES - 1:
            logger.warning(
                "Batch %s attempt %d failed (%s). Retrying in 5s…",
                batch_pages, retry + 1, exc,
            )
            time.sleep(5)
            return _process_batch(client, images, batch_pages, retry + 1)
        logger.error(
            "Batch %s failed after %d retries: %s", batch_pages, _MAX_RETRIES, exc
        )
        return (
            f"[ERROR: Batch pages {[p+1 for p in batch_pages]} failed — "
            f"{type(exc).__name__}: {exc}]"
        )


# ---------------------------------------------------------------------------
# Post-processing — deterministic fixes applied after LLM output
# ---------------------------------------------------------------------------

_SECTION_HEADING_RE = re.compile(
    r"^(#{1,6})\s+(\d+(?:\.\d+)*)\s+(.*)$", re.MULTILINE
)

_PAGE_MARKER_RE = re.compile(
    r"^## Pages?\s+\d+(?:\s*[–\-]\s*\d+)?\s*\n?", re.MULTILINE
)


def _fix_numbered_heading_levels(text: str) -> str:
    """Fix heading levels based on section numbers (deterministic).

    The LLM sometimes assigns wrong heading depths.  This function
    recomputes the correct Markdown heading level from the dot-separated
    section number:  ``1`` → ``#``, ``1.1`` → ``##``, ``1.1.1`` → ``###``.
    """
    def _replace(m: re.Match) -> str:
        number = m.group(2)
        title = m.group(3)
        level = max(1, min(6, len(number.split("."))))
        return f"{'#' * level} {number} {title}"

    return _SECTION_HEADING_RE.sub(_replace, text)


def _remove_repeated_page_titles(text: str) -> str:
    """Remove chapter-level headings that repeat across pages (PDF headers).

    Running headers like ``# 1 Local Interconnect Network (LIN)`` appear on
    every page in the PDF.  The LLM is instructed to remove them but often
    fails.  This function detects any ``# N Title`` heading that appears
    more than twice and keeps only the *first* occurrence.
    """
    # Match top-level headings: # N Title (single number, no dots)
    chapter_re = re.compile(r"^(# \d+\s+.+)$", re.MULTILINE)
    matches = chapter_re.findall(text)
    if not matches:
        return text

    counts = Counter(matches)
    duplicates = {h for h, c in counts.items() if c > 2}
    if not duplicates:
        return text

    seen: set[str] = set()

    def _dedup(m: re.Match) -> str:
        heading = m.group(1)
        if heading in duplicates:
            if heading in seen:
                return ""  # remove duplicate
            seen.add(heading)
        return heading

    # Remove the duplicate lines and collapse resulting extra blank lines
    result = chapter_re.sub(_dedup, text)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


def postprocess_markdown(text: str) -> str:
    """Apply all deterministic post-processing to joined LLM markdown output.

    This should be called on the **full** joined markdown (after all batch
    parts have been concatenated).  It performs:

      1. Remove ``## Pages X–Y`` batch markers (artifacts of parallel processing)
      2. Fix numbered heading levels (deterministic from section numbers)
      3. Remove repeated page-title headings (PDF running headers)
    """
    text = _PAGE_MARKER_RE.sub("", text)
    text = _fix_numbered_heading_levels(text)
    text = _remove_repeated_page_titles(text)
    # Clean up excessive blank lines introduced by removals
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(
    path: str,
    *,
    api_key: Optional[str] = None,
    base_url: str = "https://gpt4ifx.icp.infineon.com",
    model: str = _DEFAULT_MODEL,
    max_workers: int | None = None,
    ca_bundle: Optional[str] = None,
    resume: bool = True,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """
    Convert a PDF file to Markdown via an LLM vision model.

    Pages are rendered at 400 DPI and sent in **fixed batches of 2** to
    the vision model.  Multiple batches are processed in parallel via a
    thread pool.

    Args:
        path:        Path to the PDF file.
        api_key:     API key.  Falls back to ``token_manager.get_token()``.
        base_url:    OpenAI-compatible API base URL.
        model:       Model name to use for conversion.
        max_workers: Concurrent batch requests (default from config).
        ca_bundle:   Optional path to a CA certificate bundle.
        resume:      Resume from checkpoint if available (default True).

    Returns:
        A list of Markdown strings, one per batch of pages.

    Raises:
        FileNotFoundError: If *path* does not exist.
        RuntimeError:      If no API key is available.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    # ── Resolve API key ───────────────────────────────────────────────
    if not api_key:
        from src.HybridRAG.code.token_manager import get_token
        try:
            token = get_token()
        except RuntimeError:
            raise RuntimeError(
                "No API key provided and automatic token refresh failed. "
                "Ensure IFX_USERNAME and IFX_PASSWORD are configured."
            )
    else:
        token = api_key

    client = _build_client(token, base_url, model, ca_bundle)
    doc = fitz.open(str(p))
    total = len(doc)
    logger.info(
        "Opened %s — %d pages (batch_size=%d, dpi=%d)",
        p.name, total, _BATCH_SIZE, _DPI,
    )

    # ── Build batch ranges ────────────────────────────────────────────
    batch_ranges = [
        list(range(i, min(i + _BATCH_SIZE, total)))
        for i in range(0, total, _BATCH_SIZE)
    ]

    # ── Checkpoint: load previous progress if available ───────────────
    ckpt = _CheckpointManager(p.stem)
    results_by_idx: dict[int, tuple[list[int], str]] = {}

    if resume:
        last_idx, saved = ckpt.load()
        if last_idx >= 0 and saved:
            logger.info(
                "Resuming from checkpoint — %d batches already done",
                last_idx + 1,
            )
            for k, v in saved.items():
                results_by_idx[int(k)] = (v["pages"], v["md"])

    # ── Determine which batches still need processing ─────────────────
    pending = [
        (idx, pages)
        for idx, pages in enumerate(batch_ranges)
        if idx not in results_by_idx
    ]

    if not pending:
        logger.info("All batches already completed (from checkpoint)")
    else:
        # ── Parallel processing ───────────────────────────────────────
        if max_workers is None:
            max_workers = get_max_workers("parsers.pdf")
        max_workers = max(1, max_workers)

        logger.info(
            "Processing %d pending batches with %d workers…",
            len(pending), max_workers,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for idx, pages in pending:
                images = _render_batch_images(doc, pages)
                futures[pool.submit(_process_batch, client, images, pages)] = idx

            for fut in as_completed(futures):
                idx = futures[fut]
                pages = batch_ranges[idx]
                try:
                    md = fut.result()
                except Exception as exc:
                    md = f"[ERROR: Batch pages {[pg+1 for pg in pages]} — {exc}]"
                results_by_idx[idx] = (pages, md)

                # Progress callback
                if progress_callback:
                    progress_callback(len(results_by_idx), len(batch_ranges))

                # Save checkpoint after each completed batch
                ckpt.save(idx, {
                    str(k): {"pages": v[0], "md": v[1]}
                    for k, v in results_by_idx.items()
                })

    doc.close()

    # ── Assemble in page order ────────────────────────────────────────
    parts: list[str] = []
    for idx in range(len(batch_ranges)):
        pages, md = results_by_idx[idx]
        md = _fix_numbered_heading_levels(md)
        parts.append(f"## Pages {pages[0]+1}\u2013{pages[-1]+1}\n\n{md}")

    # Clear checkpoint on success
    ckpt.clear()
    logger.info("PDF conversion complete — %d parts produced", len(parts))

    return parts

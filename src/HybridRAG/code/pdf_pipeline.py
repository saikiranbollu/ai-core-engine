#!/usr/bin/env python3
"""
PDF Processing Pipeline for ILLD Profile
=========================================
Two-stage pipeline for converting hardware manuals into structured data:

  Stage 1 – PDF → Markdown   (LLM-based, page-by-page image extraction)
  Stage 2 – Markdown → JSON  (regex-based hardware spec extraction)

Reads from   ``data/<MODULE>/raw/*.pdf``
Writes to    ``data/<MODULE>/processed/<name>__hwa_output.md``
             ``data/<MODULE>/processed/<name>__hwa_output_hardware_spec.json``

Usage:
  python pdf_pipeline.py --module CXPI                         # both stages
  python pdf_pipeline.py --module CXPI --stage md              # PDF→MD only
  python pdf_pipeline.py --module CXPI --stage json            # MD→JSON only
  python pdf_pipeline.py --module CXPI --pdf MyManual.pdf      # specific PDF
  python pdf_pipeline.py --module CXPI --batch-size 3          # 3 pages / batch
  python pdf_pipeline.py --module CXPI --dry-run               # preview files

Environment variables:
  LLAMA_TOKEN   – Bearer token for the LLM API (required for Stage 1)

Requirements:
  pip install PyMuPDF langchain-openai httpx tqdm
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
HYBRIDRAG_DIR = SCRIPT_DIR.parent
DATA_DIR = HYBRIDRAG_DIR / "data"

# Ensure code dir is on sys.path (needed when imported from sub-packages)
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pdf_pipeline")

# Load secrets (LLAMA_TOKEN etc.) from  env/.env  into os.environ
try:
    from env_config import load_env
    load_env()
except Exception:  # env_config or .env not available – rely on shell env
    pass


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Stage 1 — PDF → Markdown  (LLM-based)                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

DEFAULT_MODEL = "gpt-5.2"
API_BASE_URL = "https://gpt4ifx.icp.infineon.com"
API_TIMEOUT = 600          # 10 min per batch (Bedrock can be slow for large payloads)
MAX_RETRIES = 5            # More retries since Bedrock timeouts are transient
IMAGE_DPI = 100            # 100 DPI for text-only pages
IMAGE_DPI_FIGURE = 150     # Higher DPI for pages with diagrams/figures
IMAGE_FORMAT = "jpeg"      # jpeg is ~5-10x smaller than png for document scans
JPEG_QUALITY = 70          # Lower quality keeps payload small while retaining text clarity
JPEG_QUALITY_FIGURE = 85   # Higher quality for diagram detail
RETRY_BASE_DELAY = 10      # Initial retry delay in seconds (doubles each attempt)
MAX_PARALLEL_BATCHES = 4   # Concurrent LLM API calls for PDF→MD conversion

# ---------------------------------------------------------------------------
# Thread-safe shared token — all worker threads read from here so a single
# refresh after a 401 benefits every thread immediately.
# ---------------------------------------------------------------------------
import threading as _thr

_token_val: str | None = None
_token_lock = _thr.Lock()
_token_generation = 0          # bumped on each refresh so threads detect staleness


def _get_shared_token() -> tuple[str, int]:
    """Return (token, generation).  Refreshes only if no token is cached."""
    global _token_val, _token_generation
    with _token_lock:
        if _token_val is None:
            from token_manager import get_token
            _token_val = get_token()
            _token_generation += 1
        return _token_val, _token_generation


def _refresh_shared_token(stale_gen: int) -> tuple[str, int]:
    """Refresh the token **once** — skip if another thread already did it."""
    global _token_val, _token_generation
    with _token_lock:
        if _token_generation != stale_gen:
            # Another thread already refreshed — just return the new one
            return _token_val, _token_generation
        from token_manager import ensure_valid_token
        logger.info("  Token expired — refreshing (gen %d) …", stale_gen)
        _token_val = ensure_valid_token(force_refresh=True)
        _token_generation += 1
        os.environ["LLAMA_TOKEN"] = _token_val
        logger.info("  Token refreshed → gen %d", _token_generation)
        return _token_val, _token_generation


class CheckpointManager:
    """Saves / loads checkpoint files so interrupted conversions can resume."""

    def __init__(self, pdf_name: str, checkpoint_dir: Path | None = None):
        self.checkpoint_dir = checkpoint_dir or (HYBRIDRAG_DIR / "data" / ".checkpoints")
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.checkpoint_dir / f"{pdf_name}.json"

    def save(self, page_num: int, content: str) -> None:
        data = {
            "last_processed_page": page_num,
            "timestamp": datetime.now().isoformat(),
            "content": content,
        }
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self) -> tuple[int, str]:
        if self.checkpoint_file.exists():
            data = json.loads(self.checkpoint_file.read_text(encoding="utf-8"))
            return data.get("last_processed_page", -1), data.get("content", "")
        return -1, ""

    def clear(self) -> None:
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()


def _init_llm_client(model: str = DEFAULT_MODEL, token: str | None = None):
    """Return a raw ``openai.OpenAI`` client pointed at ``gpt4ifx``.

    We use the raw OpenAI client instead of ``langchain_openai.ChatOpenAI``
    because langchain normalises content blocks to OpenAI format, converting
    Anthropic-native ``image`` blocks into ``image_url`` blocks.  The
    ``gpt4ifx`` proxy forwards the payload to Anthropic/Bedrock without
    translating them, so we must send Anthropic-native format directly.

    If *token* is provided it is used directly; otherwise the shared
    thread-safe token cache is consulted.
    """
    try:
        from openai import OpenAI
        import httpx
    except ImportError:
        logger.error("openai and httpx are required for PDF→MD.  "
                      "pip install openai httpx")
        raise

    if token is None:
        token, _ = _get_shared_token()

    ca_bundle = SCRIPT_DIR / "ca-bundle.crt"
    if ca_bundle.exists():
        os.environ["SSL_CERT_FILE"] = str(ca_bundle)
        os.environ["REQUESTS_CA_BUNDLE"] = str(ca_bundle)
        http_client = httpx.Client(verify=str(ca_bundle), timeout=httpx.Timeout(API_TIMEOUT))
    else:
        http_client = httpx.Client(timeout=httpx.Timeout(API_TIMEOUT))

    return OpenAI(
        api_key=token,
        base_url=API_BASE_URL,
        http_client=http_client,
        timeout=API_TIMEOUT,
    )


_PDF_TO_MD_PROMPT = """\
Convert these PDF pages ({pages}) to Markdown.

CRITICAL RULES:
1. Retain all ACTUAL DOCUMENT CONTENT (not headers/footers).
2. Retain tables, mathematical formulas, and structure exactly.
3. PRESERVE SECTION NUMBERING AND HEADINGS EXACTLY as printed in the document.
   - Every section heading MUST be a Markdown heading line starting with '#' characters.
   - The number of '#' characters MUST equal the number of segments in the section number:
       "3 Overview"           → "# 3 Overview"           (1 segment → 1 #)
       "3.1 Architecture"     → "## 3.1 Architecture"     (2 segments → 2 #)
       "3.1.3 HW-SW Interface"→ "### 3.1.3 HW-SW Interface" (3 segments → 3 #)
       "3.1.3.2 ADC Config"   → "#### 3.1.3.2 ADC Config" (4 segments → 4 #)
   - NEVER drop or omit section numbers from headings.
   - NEVER use **bold** text instead of '#' headings for section titles.
   - NEVER output a section heading without the leading '#' marks.
4. FOR DIAGRAMS/FIGURES:
   - Explain each diagram in DETAIL in SIMPLE WORDS.
   - What is it showing overall? What does each component do?
   - How are components connected? What is the logic/flow?
5. REMOVE ALL HEADERS, FOOTERS, PAGE NUMBERS,
   classification marks, document metadata, logos, watermarks.

Output ONLY valid Markdown."""

_PDF_TO_MD_PROMPT_FIGURE = """\
Convert these PDF pages ({pages}) to Markdown.

CRITICAL RULES:
1. Retain all ACTUAL DOCUMENT CONTENT (not headers/footers).
2. Retain tables, mathematical formulas, and structure exactly.
3. PRESERVE SECTION NUMBERING AND HEADINGS EXACTLY as printed in the document.
   - Every section heading MUST be a Markdown heading line starting with '#' characters.
   - The number of '#' characters MUST equal the number of segments in the section number:
       "3 Overview"           → "# 3 Overview"           (1 segment → 1 #)
       "3.1 Architecture"     → "## 3.1 Architecture"     (2 segments → 2 #)
       "3.1.3 HW-SW Interface"→ "### 3.1.3 HW-SW Interface" (3 segments → 3 #)
       "3.1.3.2 ADC Config"   → "#### 3.1.3.2 ADC Config" (4 segments → 4 #)
   - NEVER drop or omit section numbers from headings.
   - NEVER use **bold** text instead of '#' headings for section titles.
   - NEVER output a section heading without the leading '#' marks.
4. REMOVE ALL HEADERS, FOOTERS, PAGE NUMBERS,
   classification marks, document metadata, logos, watermarks.
5. FOR EVERY DIAGRAM/FIGURE — produce a structured description with ALL of the following:

   **Figure N: <title>**

   **Diagram Type:** (one of: block diagram, sequence diagram, state machine, \
flowchart, class diagram, data flow, architecture view, comparison diagram, other)

   **Overall Purpose:**
   <1-3 sentence summary of what the diagram shows>

   **Components:**
   | # | Component Name | Type | Description |
   |---|----------------|------|-------------|
   | 1 | <exact label from diagram> | <box/actor/block/IP/module/signal/buffer> | <what it does> |

   **Connections:**
   | # | From | To | Line Style | Label/Description |
   |---|------|----|------------|-------------------|
   | 1 | <source component> | <target component> | <solid/dashed/dotted/arrow> | <relationship or signal name> |

   **Flow/Sequence (if applicable):**
   1. <Step 1: what happens first>
   2. <Step 2: what happens next>

   **Trust/Security Domains (if applicable):**
   | Domain | Boundary | Components Inside |
   |--------|----------|-------------------|
   | Trusted | <boundary description> | <list of components> |

   IMPORTANT:
   - List EVERY component and EVERY connection visible in the diagram
   - Use the EXACT labels/names shown in the diagram
   - If an arrow has a label, include it in the connection description
   - Distinguish solid lines from dashed lines
   - For sequence diagrams: capture ALL lifelines and ALL messages in order
   - For block diagrams: capture ALL blocks and ALL arrows between them

Output ONLY valid Markdown."""


def _page_has_figure(page) -> bool:
    """Detect whether a PDF page contains diagrams or significant images.

    Uses PyMuPDF page introspection (no rendering needed).
    Two signals:
      1. Page text contains a "Figure N" caption.
      2. Page has ≥200 vector drawings (typical of diagrams/flowcharts;
         normal text/tables rarely exceed 100).
    """
    # Check text for figure captions (most reliable signal)
    try:
        text = page.get_text("text")
        if re.search(r"Figure\s+\d+", text, re.IGNORECASE):
            return True
    except Exception:
        pass
    # Check for high vector-drawing count (catches diagrams without captions)
    try:
        if len(page.get_drawings()) >= 200:
            return True
    except Exception:
        pass
    return False


def _is_auth_error(exc: Exception) -> bool:
    """Return True when *exc* is a 401 authentication / token-expiry error."""
    try:
        from openai import AuthenticationError
        if isinstance(exc, AuthenticationError):
            return True
    except ImportError:
        pass
    # Also check status_code attribute (some wrappers expose it)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 401:
        return True
    return False


def _refresh_token_and_rebuild_client(model: str, stale_gen: int = 0):
    """Refresh the shared token (thread-safe) and return a new LLM client."""
    try:
        new_token, _ = _refresh_shared_token(stale_gen)
    except Exception as refresh_exc:
        logger.error("  Token refresh failed: %s", refresh_exc)
        new_token, _ = _get_shared_token()  # fall back to whatever is cached
    return _init_llm_client(model, token=new_token)


def _process_batch_with_retry(
    client, model: str, images: list, batch_pages: list[int],
    retry: int = 0, token_gen: int = 0, has_figure: bool = False,
) -> str:
    """Send a batch of page images to the LLM with retry logic.

    When *has_figure* is True, uses the structured diagram prompt with
    lower temperature and higher token budget for detailed extraction.

    On connection-level errors (``RemoteProtocolError``, ``ConnectionError``),
    the HTTP client is recreated to avoid reusing a broken connection from
    the httpx connection pool.

    On 401 authentication errors (token expiry), the token is automatically
    refreshed via ``token_manager`` before retrying.  The *token_gen*
    parameter tracks the token generation so that only the first thread
    to hit 401 actually refreshes; later threads piggyback on that refresh.
    """
    try:
        template = _PDF_TO_MD_PROMPT_FIGURE if has_figure else _PDF_TO_MD_PROMPT
        prompt = template.format(pages=", ".join(map(str, batch_pages)))
        messages = [
            {"role": "user", "content": [{"type": "text", "text": prompt}] + images}
        ]
        # Figure batches: more tokens + lower temperature for structured output
        max_tokens = 16000 if has_figure else 13000
        temperature = 0.3 if has_figure else 1
        tag = "fig" if has_figure else "txt"
        logger.info("  Batch pages %s [%s] – waiting for API …", batch_pages, tag)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_completion_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        is_auth = _is_auth_error(exc)
        is_connection_error = _is_connection_error(exc)
        logger.warning(
            "Batch %s attempt %d/%d failed (%s): %s",
            batch_pages, retry + 1, MAX_RETRIES,
            "auth/token" if is_auth else "connection" if is_connection_error else "api", exc,
        )
        if retry < MAX_RETRIES - 1:
            # On 401, refresh the token immediately (no exponential backoff)
            if is_auth:
                client = _refresh_token_and_rebuild_client(model, stale_gen=token_gen)
                _, new_gen = _get_shared_token()
                return _process_batch_with_retry(
                    client, model, images, batch_pages, retry + 1, token_gen=new_gen,
                    has_figure=has_figure,
                )

            import random
            # Exponential backoff with jitter to avoid hammering a recovering server
            delay = RETRY_BASE_DELAY * (2 ** retry) + random.uniform(0, 5)
            logger.info("  Retrying in %.1fs …", delay)
            time.sleep(delay)
            # On connection-level errors, rebuild the client so we get a
            # fresh TCP/TLS socket instead of a dead pooled connection.
            if is_connection_error:
                logger.info("  Rebuilding HTTP client (stale connection) …")
                client = _init_llm_client(model)
            return _process_batch_with_retry(
                client, model, images, batch_pages, retry + 1, token_gen=token_gen,
                has_figure=has_figure,
            )
        return f"[ERROR: Batch {batch_pages} failed after {MAX_RETRIES} retries – {exc}]"


def _is_connection_error(exc: Exception) -> bool:
    """Return True when *exc* looks like a transport / connection error
    rather than a normal API (HTTP 4xx/5xx) error.

    We inspect the exception chain for common httpx / httpcore indicators
    so that the retry logic can recreate the HTTP client.
    """
    import httpx
    if isinstance(exc, (httpx.RemoteProtocolError,
                        httpx.ConnectError,
                        httpx.ReadError,
                        httpx.WriteError,
                        httpx.CloseError,
                        ConnectionError,
                        OSError)):
        return True
    # openai wraps transport errors inside APIConnectionError
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        return _is_connection_error(cause)
    return False


def convert_pdf_to_md(
    pdf_path: Path,
    output_path: Path,
    batch_size: int = 1,
    model: str = DEFAULT_MODEL,
    resume: bool = True,
    max_pages: int = 0,
    start_page: int = 0,
) -> Path:
    """
    Convert a PDF to Markdown via the LLM image-extraction approach.

    Returns the path to the generated ``.md`` file.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF is required. pip install PyMuPDF")
        raise

    pdf_name = pdf_path.stem
    ckpt = CheckpointManager(pdf_name)

    # Preserve the caller-requested start page before checkpoint-resume logic
    requested_start = start_page

    resume_page = 0
    full_md = ""
    if resume:
        last_page, saved = ckpt.load()
        if last_page >= 0:
            resume_page = last_page + 1
            full_md = saved
            logger.info("Resuming from checkpoint – page %d", resume_page)

    doc = fitz.open(str(pdf_path))
    total = len(doc)

    # Determine effective start page: checkpoint-resume takes priority over
    # caller-requested start_page (if we already converted past it).
    if resume_page > 0:
        effective_start = resume_page
    elif requested_start > 0:
        effective_start = requested_start
    else:
        effective_start = 0

    if effective_start >= total:
        logger.warning("start_page %d >= total pages %d; nothing to convert",
                        effective_start, total)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        return output_path

    if max_pages and max_pages > 0:
        total = min(total, effective_start + max_pages)

    start_page = effective_start

    logger.info("PDF: %s  (pages %d-%d, batch=%d)", pdf_path.name, start_page, total - 1, batch_size)

    try:
        from tqdm import tqdm
        pbar = tqdm(total=total, initial=start_page, desc="PDF→MD")
    except ImportError:
        pbar = None

    # -- Build all batch ranges up-front ------------------------------------
    batch_ranges = []
    for i in range(start_page, total, batch_size):
        pages = list(range(i, min(i + batch_size, total)))
        batch_ranges.append(pages)

    # -- Detect figure pages (fast: text + image introspection, no render) --
    figure_pages: set[int] = set()
    for pn in range(start_page, total):
        if _page_has_figure(doc[pn]):
            figure_pages.add(pn)
    if figure_pages:
        logger.info("Detected %d figure pages (will use DPI=%d): %s",
                     len(figure_pages), IMAGE_DPI_FIGURE,
                     sorted(figure_pages)[:20])

    # -- Pre-render images per batch (CPU-bound, fast) ----------------------
    # Adaptive DPI: figure pages get higher resolution + quality
    batch_images: list[list[dict]] = []
    batch_has_figure: list[bool] = []      # track which batches contain figures
    total_img_bytes = 0
    for pages in batch_ranges:
        images = []
        has_fig = False
        for pn in pages:
            is_fig = pn in figure_pages
            if is_fig:
                has_fig = True
            dpi = IMAGE_DPI_FIGURE if is_fig else IMAGE_DPI
            quality = JPEG_QUALITY_FIGURE if is_fig else JPEG_QUALITY
            pix = doc[pn].get_pixmap(dpi=dpi)
            if IMAGE_FORMAT == "jpeg":
                img_bytes = pix.tobytes("jpeg", jpg_quality=quality)
                media_type = "image/jpeg"
            else:
                img_bytes = pix.tobytes("png")
                media_type = "image/png"
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            total_img_bytes += len(img_bytes)
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })
        batch_images.append(images)
        batch_has_figure.append(has_fig)

    doc.close()
    logger.info("Rendered %d pages (%d batches, %d with figures) → %.1f MB total",
                total - start_page, len(batch_ranges),
                sum(batch_has_figure), total_img_bytes / 1024 / 1024)

    # -- Submit batches to thread pool (I/O-bound API calls) ----------------
    # Prime the shared token once before spawning threads
    _get_shared_token()

    def _process_one(idx: int) -> tuple[int, str]:
        """Worker: create a per-thread LLM client and process one batch."""
        tok, gen = _get_shared_token()
        thread_client = _init_llm_client(model, token=tok)
        md = _process_batch_with_retry(
            thread_client, model, batch_images[idx], batch_ranges[idx],
            token_gen=gen,
            has_figure=batch_has_figure[idx],
        )
        return idx, md

    results_by_idx: dict[int, str] = {}
    # Lock for incremental checkpointing (assembles pages in order)
    _ckpt_lock = _thr.Lock()
    _next_ckpt_idx = [0]  # mutable so the closure can update it

    def _try_checkpoint():
        """Write checkpoint for all contiguous completed batches."""
        nonlocal full_md
        with _ckpt_lock:
            while _next_ckpt_idx[0] < len(batch_ranges) and _next_ckpt_idx[0] in results_by_idx:
                ci = _next_ckpt_idx[0]
                pages = batch_ranges[ci]
                full_md += f"\n## Pages {pages[0]}-{pages[-1]}\n\n{results_by_idx[ci]}\n"
                ckpt.save(pages[-1], full_md)
                _next_ckpt_idx[0] += 1

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_BATCHES) as pool:
        futures = {pool.submit(_process_one, idx): idx
                   for idx in range(len(batch_ranges))}

        for fut in as_completed(futures):
            idx = futures[fut]
            pages = batch_ranges[idx]
            try:
                _, batch_md = fut.result()
            except Exception as exc:
                batch_md = f"[ERROR: Batch {pages} — {exc}]"
                logger.error("Batch %s failed: %s", pages, exc)
            results_by_idx[idx] = batch_md

            if pbar:
                pbar.update(len(pages))

            # Incrementally checkpoint contiguous completed batches
            _try_checkpoint()

    # Flush any remaining batches that completed out of order
    _try_checkpoint()

    if pbar:
        pbar.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full_md, encoding="utf-8")
    ckpt.clear()
    logger.info("Saved MD → %s", output_path)
    return output_path


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Stage 2 — Markdown → JSON  (regex-based hardware extraction)           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class HardwareExtractor:
    """
    Extracts registers, fields, interrupts, errors, formulas, and
    inter-entity relationships from a hardware-manual markdown file.

    Fully dynamic / module-agnostic — works with any hardware module.
    """

    def __init__(self, md_path: Path) -> None:
        self.md_path = Path(md_path)
        self.content = ""
        self.lines: list[str] = []

    # -- I/O ----------------------------------------------------------------

    def _read(self) -> bool:
        try:
            self.content = self.md_path.read_text(encoding="utf-8")
            self.lines = self.content.split("\n")
            logger.info("Read %d lines from %s", len(self.lines), self.md_path.name)
            return True
        except Exception as exc:
            logger.error("Cannot read %s: %s", self.md_path, exc)
            return False

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _is_md_formatting(cells: list[str]) -> bool:
        if not cells:
            return True
        for c in cells:
            c = c.strip()
            if not c:
                continue
            if re.match(r"^[:\-\s]+$", c):
                return True
        first = cells[0].strip().lower()
        return first in ("field", "name", "bit", "bits")

    @staticmethod
    def _is_reserved(name: str) -> bool:
        n = name.strip()
        return n == "0" or n.lower() == "reserved"

    # -- extractors ---------------------------------------------------------

    def extract_registers(self) -> list[dict]:
        regs: list[dict] = []
        seen: set[str] = set()
        pat = re.compile(r"^([A-Z][A-Za-z0-9_]+(?:\s*\([^)]+\))?)\s*[–—]\s*(.+)$")

        i = 0
        while i < len(self.lines):
            line = self.lines[i].strip()
            m = pat.match(line)

            if m:
                short = re.sub(r"\s*\([^)]+\)", "", m.group(1)).strip()
                long_name = m.group(2).strip()
            elif (
                re.match(r"^[A-Z][A-Za-z0-9_]+(?:\s*\([^)]+\))?\s*$", line)
                and len(line) <= 50
            ):
                short = re.sub(r"\s*\([^)]+\)", "", line).strip()
                if "_" not in short and not short.isupper():
                    i += 1; continue
                long_name = None
                for di in range(i + 1, min(i + 15, len(self.lines))):
                    cl = self.lines[di].strip()
                    if not cl:
                        continue
                    raw = cl[2:].strip() if cl.startswith("- ") else cl
                    if raw.startswith("Offset") or re.match(r"^rst_\w+", raw) or raw.startswith("|"):
                        continue
                    if re.match(r"^[A-Z][A-Za-z0-9_]+(?:\s*\([^)]+\))?\s*$", cl) and len(cl) <= 50:
                        break
                    if len(cl) > 10:
                        long_name = cl
                        break
                if not long_name:
                    i += 1; continue
            else:
                i += 1; continue

            if short in seen:
                i += 1; continue

            offset = reset_val = reset_type = None
            for j in range(i + 1, min(i + 11, len(self.lines))):
                al = self.lines[j].strip()
                if al.startswith("- "):
                    al = al[2:].strip()
                if al.startswith("Offset address:"):
                    offset = al.replace("Offset address:", "").strip()
                rm = re.match(r"(rst_\w+)\s+value:\s*(.+)", al)
                if rm:
                    reset_type = rm.group(1)
                    reset_val = rm.group(2).strip()

            if offset:
                seen.add(short)
                regs.append({
                    "name": short, "long_name": long_name,
                    "offset": offset, "reset_value": reset_val, "reset_type": reset_type,
                })
            i += 1

        logger.info("Extracted %d registers", len(regs))
        return regs

    def extract_fields(self, registers: list[dict]) -> list[dict]:
        reg_names = [{"short": r["name"], "long": r["long_name"]} for r in registers]
        all_fields: list[dict] = []

        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            cur_reg = None
            for rn in reg_names:
                if rn["short"] in line or rn["long"] in line:
                    if "—" in line or "–" in line:
                        cur_reg = rn["short"]; break
                    for ci in range(i + 1, min(i + 5, len(self.lines))):
                        if "offset address" in self.lines[ci].lower():
                            cur_reg = rn["short"]; break
                    if cur_reg:
                        break

            if cur_reg:
                for j in range(i, min(i + 100, len(self.lines))):
                    tl = self.lines[j]
                    if tl.strip().startswith("|") and tl.count("|") >= 4:
                        cells = [c.strip() for c in tl.split("|")[1:-1]]
                        if len(cells) < 3 or self._is_md_formatting(cells):
                            continue
                        fn, bits = cells[0].strip(), cells[1].strip() if len(cells) > 1 else ""
                        ft = cells[2].strip() if len(cells) > 2 else ""
                        desc = cells[3].strip() if len(cells) > 3 else ""
                        if self._is_reserved(fn) or not fn or not bits:
                            continue
                        if not re.match(r"^\d+(:\d+)?$", bits):
                            continue
                        all_fields.append({
                            "name": fn, "parent_register": cur_reg,
                            "bits": bits, "type": ft, "description": desc[:200],
                        })
            i += 1

        seen: set[tuple] = set()
        unique: list[dict] = []
        for f in all_fields:
            k = (f["parent_register"], f["name"], f["bits"])
            if k not in seen:
                seen.add(k); unique.append(f)
        logger.info("Extracted %d unique fields", len(unique))
        return unique

    def extract_interrupts(self) -> list[dict]:
        kw = ["INTR", "INT", "IRQ", "INTERRUPT"]
        sections: list[dict] = []
        for i, line in enumerate(self.lines):
            if "—" in line:
                parts = line.split("—")
                if len(parts) >= 2:
                    rn = re.sub(r"\s*\([^)]+\)", "", parts[0].strip()).strip()
                    desc = parts[1].strip().lower()
                    if any(k in rn.upper() for k in kw) or "interrupt" in desc:
                        sections.append({"register": rn, "line": i})

        intrs: list[dict] = []
        for sec in sections:
            for j in range(sec["line"], min(sec["line"] + 150, len(self.lines))):
                ln = self.lines[j]
                if ln.strip().startswith("|") and ln.count("|") >= 4:
                    cells = [c.strip() for c in ln.split("|")[1:-1]]
                    if len(cells) < 3 or self._is_md_formatting(cells):
                        continue
                    fn = cells[0].strip()
                    bits = cells[1].strip() if len(cells) > 1 else ""
                    desc = cells[3].strip() if len(cells) > 3 else ""
                    if self._is_reserved(fn) or not fn or not bits:
                        continue
                    if not re.match(r"^\d+$", bits):
                        continue
                    intrs.append({
                        "name": fn, "register": sec["register"],
                        "bit": int(bits), "description": desc[:200],
                    })
        logger.info("Extracted %d interrupts", len(intrs))
        return intrs

    def extract_errors(self) -> list[dict]:
        err_kw = [
            "ERROR", "ERR", "FAULT", "TIMEOUT", "FAIL",
            "VIOLATION", "LOSS", "ARB_LOST", "OVERRUN", "UNDERRUN",
        ]
        errors: list[dict] = []
        seen: set[str] = set()
        cur_reg: str | None = None

        for line in self.lines:
            if "—" in line and not line.strip().startswith("|"):
                m = re.match(r"^([A-Z][A-Za-z0-9_]+)", line.split("—")[0].strip())
                if m:
                    cur_reg = m.group(1)
            if cur_reg and line.strip().startswith("|") and line.count("|") >= 4:
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if len(cells) < 3 or self._is_md_formatting(cells):
                    continue
                fn = cells[0].strip()
                bits = cells[1].strip() if len(cells) > 1 else ""
                desc = cells[3].strip() if len(cells) > 3 else ""
                if not any(k in fn.upper() for k in err_kw):
                    continue
                if not re.match(r"^\d+(:\d+)?$", bits):
                    continue
                if fn in seen:
                    continue
                seen.add(fn)

                fu = fn.upper()
                if any(x in fu for x in ("TX", "TRANSMIT", "SEND")):
                    et = "transmission"
                elif any(x in fu for x in ("RX", "RECEIVE", "RECV")):
                    et = "reception"
                elif any(x in fu for x in ("BUS", "ARB")):
                    et = "bus"
                elif any(x in fu for x in ("PARITY", "CRC", "CHECKSUM")):
                    et = "data_integrity"
                elif any(x in fu for x in ("TIMEOUT", "WATCHDOG")):
                    et = "timing"
                else:
                    et = "general"
                errors.append({
                    "name": fn, "type": et, "description": desc[:200],
                    "detected_in": cur_reg,
                    "bit": int(bits.split(":")[0]) if re.match(r"^\d+", bits) else None,
                })
        logger.info("Extracted %d errors", len(errors))
        return errors

    def extract_formulas(self) -> list[dict]:
        formulas: list[dict] = []
        seen: set[str] = set()

        # LaTeX blocks $$…$$
        for m in re.finditer(r"\$\$(.+?)\$\$", self.content, re.DOTALL):
            ft = m.group(1).strip()
            if re.match(r"^[A-Z_]+\s*=\s*\d+$", ft) or ft.endswith("= ("):
                continue
            if ft not in seen:
                seen.add(ft)
                formulas.append({
                    "name": f"formula_{len(formulas)+1}",
                    "formula": ft, "type": "calculation",
                    "description": "Mathematical formula from hardware manual",
                })

        # Plain-text equations
        eq_pat = re.compile(
            r"^[\s\-•]*([A-Za-z_]\w*)\s*=\s*(.+?)(?:\s*\[Equation\s+\d+\])?$"
        )
        for line in self.lines:
            m = eq_pat.match(line.strip())
            if m:
                expr = m.group(2).strip()
                if any(op in expr for op in "/×÷*+-()") and not re.match(r"^\d+(\.\d+)?$", expr):
                    ft = f"{m.group(1)} = {expr}"
                    if ft not in seen:
                        seen.add(ft)
                        formulas.append({
                            "name": f"formula_{len(formulas)+1}",
                            "formula": ft, "type": "calculation",
                            "description": "Mathematical formula from hardware manual",
                        })
        logger.info("Extracted %d formulas", len(formulas))
        return formulas

    # -- relationships ------------------------------------------------------

    def _rel_register_has_field(self, fields: list[dict]) -> list[dict]:
        return [
            {"register": f["parent_register"], "field": f["name"], "bits": f["bits"]}
            for f in fields
        ]

    def _rel_error_sets_interrupt(
        self, errors: list[dict], interrupts: list[dict],
    ) -> list[dict]:
        out: list[dict] = []
        seen: set[tuple] = set()
        for err in errors:
            for intr in interrupts:
                if intr["name"] == err["name"]:
                    k = (err["name"], intr["register"], intr["bit"])
                    if k not in seen:
                        seen.add(k)
                        out.append({
                            "error": err["name"],
                            "error_register": err["detected_in"],
                            "interrupt": f"{intr['register']}.{intr['name']}",
                            "interrupt_register": intr["register"],
                            "interrupt_bit": intr["bit"],
                        })
        return out

    def _rel_field_used_in_formula(
        self, fields: list[dict], formulas: list[dict],
    ) -> list[dict]:
        lookup = {f["name"]: f for f in fields}
        out: list[dict] = []
        seen: set[tuple] = set()
        for fm in formulas:
            clean = fm["formula"].replace("\\_", "_")
            refs = re.findall(r"[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)?", clean)
            for ref in refs:
                fn = ref.split(".")[-1] if "." in ref else ref
                if fn in lookup:
                    fi = lookup[fn]
                    role = "output" if re.search(rf"^{re.escape(fn)}\s*=", clean) else "input"
                    k = (fm["name"], fn, fi["parent_register"])
                    if k not in seen:
                        seen.add(k)
                        out.append({
                            "formula": fm["name"], "field": fn,
                            "register": fi["parent_register"],
                            "bits": fi["bits"], "role": role,
                        })
        return out

    def _rel_field_enables_feature(self, fields: list[dict]) -> list[dict]:
        kw = [
            "enable", "enabled", "disable", "disabled",
            "activation", "activate", "deactivate",
            "must be set", "control",
        ]
        out: list[dict] = []
        seen: set[tuple] = set()
        for f in fields:
            desc = f.get("description", "").lower()
            if not any(k in desc for k in kw):
                continue
            m = re.search(r"enable\s+(?:the\s+)?([a-z_]\w[\w\s]*)", desc)
            feat = m.group(1).strip().replace(" ", "_") if m else None
            if not feat:
                m = re.search(r"([\w\s]+)\s+enable", desc)
                feat = m.group(1).strip().replace(" ", "_") if m else None
            if not feat:
                feat = re.sub(
                    r"(_EN|_ENABLE|_ENABLED|EN)$", "", f["name"], flags=re.I
                ).lower() + "_feature"
            k = (f["name"], f["parent_register"], feat)
            if k not in seen:
                seen.add(k)
                out.append({
                    "field": f["name"], "register": f["parent_register"],
                    "bits": f["bits"], "feature": feat,
                    "enable_value": 1, "disable_value": 0,
                })
        return out

    def _rel_operation_triggers_interrupt(self, interrupts: list[dict]) -> list[dict]:
        op_kw = {
            "transmit": "transmit", "transmission": "transmit",
            "send": "send", "receive": "receive", "reception": "receive",
            "transfer": "transfer", "read": "read", "write": "write",
        }
        timing_kw = ["after", "when", "on", "upon", "during"]
        out: list[dict] = []
        seen: set[tuple] = set()
        for intr in interrupts:
            desc = intr.get("description", "").lower()
            operation = None
            for tk in timing_kw:
                if tk in desc:
                    rem = desc.split(tk, 1)[1]
                    for ok, ov in op_kw.items():
                        if ok in rem:
                            operation = ov; break
                if operation:
                    break
            if not operation:
                iu = intr["name"].upper()
                if "TX" in iu:
                    operation = "transmit"
                elif "RX" in iu:
                    operation = "receive"
            if not operation:
                parts = intr["name"].lower().split("_")
                skip = {"done", "error", "complete", "ready", "flag", "status"}
                op_parts = [p for p in parts if p not in skip]
                if op_parts:
                    operation = "_".join(op_parts)
            if operation:
                k = (operation, intr["name"])
                if k not in seen:
                    seen.add(k)
                    out.append({
                        "operation": operation, "interrupt": intr["name"],
                        "interrupt_register": intr["register"],
                        "interrupt_bit": intr["bit"],
                    })
        return out

    # -- orchestration ------------------------------------------------------

    def extract_all(self) -> dict[str, Any] | None:
        """Run all extractors and return the combined result dict."""
        if not self._read():
            return None

        registers = self.extract_registers()
        fields = self.extract_fields(registers)
        interrupts = self.extract_interrupts()
        errors = self.extract_errors()
        formulas = self.extract_formulas()

        relationships = {
            "register_has_field": self._rel_register_has_field(fields),
            "error_sets_interrupt": self._rel_error_sets_interrupt(errors, interrupts),
            "field_used_in_formula": self._rel_field_used_in_formula(fields, formulas),
            "field_enables_feature": self._rel_field_enables_feature(fields),
            "interrupt_masked_by_field": [],  # future implementation
            "operation_triggers_interrupt": self._rel_operation_triggers_interrupt(interrupts),
        }

        result = {
            "metadata": {
                "source_file": self.md_path.name,
                "total_lines": len(self.lines),
                "extraction_date": datetime.now().isoformat(),
                "counts": {
                    "registers": len(registers),
                    "fields": len(fields),
                    "interrupts": len(interrupts),
                    "errors": len(errors),
                    "formulas": len(formulas),
                },
            },
            "registers": registers,
            "fields": fields,
            "interrupts": interrupts,
            "errors": errors,
            "formulas": formulas,
            "relationships": relationships,
        }

        total_rels = sum(len(v) for v in relationships.values())
        logger.info(
            "Extraction complete — %d registers, %d fields, %d interrupts, "
            "%d errors, %d formulas, %d relationships",
            len(registers), len(fields), len(interrupts),
            len(errors), len(formulas), total_rels,
        )
        return result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Pipeline orchestration                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def run_pipeline(
    module: str,
    pdf_name: str | None = None,
    stage: str = "both",
    batch_size: int = 2,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> None:
    """
    Execute the PDF pipeline for *module*.

    Parameters
    ----------
    stage : ``"md"`` | ``"json"`` | ``"both"``
    """
    raw_dir = DATA_DIR / module.upper() / "raw"
    processed_dir = DATA_DIR / module.upper() / "processed"

    if not raw_dir.exists():
        raw_dir.mkdir(parents=True, exist_ok=True)
        logger.warning("Created empty raw dir: %s  — place PDFs there.", raw_dir)

    pdfs = list(raw_dir.glob("*.pdf")) if not pdf_name else [raw_dir / pdf_name]
    pdfs = [p for p in pdfs if p.exists()]

    if not pdfs:
        logger.error("No PDFs found in %s", raw_dir)
        return

    processed_dir.mkdir(parents=True, exist_ok=True)

    for pdf in pdfs:
        stem = pdf.stem
        md_out = processed_dir / f"{stem}__hwa_output.md"
        json_out = processed_dir / f"{stem}__hwa_output_hardware_spec.json"

        logger.info("─" * 60)
        logger.info("PDF: %s", pdf.name)

        # Stage 1: PDF → MD
        if stage in ("md", "both"):
            if dry_run:
                logger.info("[DRY-RUN] Would convert %s → %s", pdf.name, md_out.name)
            else:
                convert_pdf_to_md(pdf, md_out, batch_size=batch_size, model=model)

        # Stage 2: MD → JSON
        if stage in ("json", "both"):
            md_source = md_out
            if not md_source.exists():
                logger.warning("MD file not found: %s — skipping JSON stage", md_source)
                continue
            if dry_run:
                logger.info("[DRY-RUN] Would extract %s → %s", md_source.name, json_out.name)
            else:
                extractor = HardwareExtractor(md_source)
                result = extractor.extract_all()
                if result:
                    json_out.write_text(
                        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    logger.info("Saved JSON → %s", json_out)
                else:
                    logger.error("Extraction returned no data for %s", md_source.name)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def main() -> int:
    ap = argparse.ArgumentParser(
        description="PDF processing pipeline (PDF→MD→JSON) for ILLD hardware manuals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python pdf_pipeline.py --module CXPI\n"
            "  python pdf_pipeline.py --module CXPI --stage md --batch-size 3\n"
            "  python pdf_pipeline.py --module CXPI --stage json\n"
            "  python pdf_pipeline.py --module CXPI --pdf MyManual.pdf\n"
        ),
    )
    ap.add_argument("--module", default="CXPI", help="Module name (default: CXPI)")
    ap.add_argument(
        "--stage",
        choices=["md", "json", "both"],
        default="both",
        help="Which stage to run (default: both)",
    )
    ap.add_argument("--pdf", default=None, help="Specific PDF filename in raw/")
    ap.add_argument("--batch-size", type=int, default=2, help="Pages per LLM batch (default: 2)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM model (default: {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="Preview without processing")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run_pipeline(
        module=args.module,
        pdf_name=args.pdf,
        stage=args.stage,
        batch_size=args.batch_size,
        model=args.model,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

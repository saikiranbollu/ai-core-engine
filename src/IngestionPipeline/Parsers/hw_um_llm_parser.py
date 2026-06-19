"""
Hardware User Manual LLM-based Parser (Markdown → Structured JSON)
===================================================================

Extracts hardware register specifications from Markdown produced by
``pdf_parser.py`` for Infineon TC44x (and similar) User Manual PDFs.

Unlike the regex-based ``hw_um_parser.py``, this parser uses an LLM to
extract structured data from each register section.  This avoids the
fragile substring-matching and context-bleed issues of the regex approach.

Approach:
  1. Split the markdown into register sections (each starts with **REGNAME**)
  2. Send each section to the LLM with a JSON schema
  3. Parse the LLM JSON response and assemble the final output

Output format is **identical** to ``hw_spec_parser.parse()`` / ``hw_um_parser.parse()``
so the same KG builder and RAG ingestion code can consume the results.

Usage::

    from IngestionPipeline.parsers import hw_um_llm_parser

    result = hw_um_llm_parser.parse("hw_um_gpt12_tc44x.md", max_workers=3)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_BASE_URL = "https://gpt4ifx.icp.infineon.com"
_DEFAULT_MODEL = "gpt-5.2"
_API_TIMEOUT = 120
_MAX_RETRIES = 3
_CA_BUNDLE_PATH = Path(__file__).resolve().parents[2] / "HybridRAG" / "code" / "ca-bundle.crt"

# Rate limit: GPT4IFX allows 30 requests per minute
_RATE_LIMIT_RPM = 28  # slightly under 30 to leave headroom

# Register header patterns — allow mixed-case names (e.g. TMADCx_MODCFG)
# and optional parenthetical suffixes like (x=0-6)
_PAT_REG_HEADER = re.compile(
    r"^\*\*([A-Z][A-Za-z0-9_]+(?:\s*\([^)]+\))?)\*\*\s*(?:\u2014.*|\\n.*)?$"
)


class _RateLimiter:
    """Simple sliding-window rate limiter (thread-safe)."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self._max = max_calls
        self._period = period
        self._lock = threading.Lock()
        self._timestamps: List[float] = []

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                # Evict old timestamps outside the window
                self._timestamps = [t for t in self._timestamps if now - t < self._period]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                # Wait until the oldest entry expires
                wait = self._period - (now - self._timestamps[0]) + 0.1
            time.sleep(wait)

# ---------------------------------------------------------------------------
# LLM extraction prompt
# ---------------------------------------------------------------------------
_EXTRACTION_PROMPT = """\
You are a hardware register specification extractor. Extract structured data from the following register section of an Infineon hardware user manual.

Return ONLY valid JSON (no markdown fences, no explanation) matching this schema:

{{
  "register": {{
    "name": "<register short name, e.g. T3CON>",
    "long_name": "<full description, e.g. Timer T3 Control Register>",
    "offset": "<hex offset address with trailing H, e.g. 0114H>",
    "reset_value": "<hex reset value with trailing H, e.g. 00000000H>",
    "reset_type": "<kernel or application>"
  }},
  "fields": [
    {{
      "name": "<field name, e.g. T3I>",
      "bits": "<bit range, e.g. 2:0 or single bit like 6>",
      "type": "<access type: r, rw, rh, rwh, w>",
      "description": "<concise description, max 200 chars>"
    }}
  ]
}}

Rules:
- For "bits": use "high:low" format for multi-bit (e.g. "5:3"), single number for 1-bit (e.g. "6")
- Skip reserved fields (name="0" or "Reserved")
- For "offset": normalize to uppercase hex with H suffix, no spaces/underscores (e.g. "0114H")
- For "reset_value": normalize to uppercase hex with H suffix, no spaces/underscores (e.g. "00000000H")
- If offset or reset_value is not found, use null
- For "description": keep it concise, include the key functional purpose
- Include ALL non-reserved fields from the section
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """Get a valid GPT4IFX token."""
    import sys
    code_dir = Path(__file__).resolve().parents[2] / "HybridRAG" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    from token_manager import ensure_valid_token
    return ensure_valid_token()


def _build_client(token: str, model: str = _DEFAULT_MODEL):
    """Create an OpenAI client pointed at GPT4IFX."""
    import httpx
    from openai import OpenAI

    ca_bundle = _CA_BUNDLE_PATH
    verify = str(ca_bundle) if ca_bundle.exists() else True

    http_client = httpx.Client(verify=verify, timeout=httpx.Timeout(_API_TIMEOUT))

    return OpenAI(
        api_key=token,
        base_url=_API_BASE_URL,
        http_client=http_client,
    )


def _split_register_sections(content: str) -> List[Dict[str, str]]:
    """Split markdown into sections, one per register.

    Returns list of {name, text} dicts where text is the full section content.
    """
    lines = content.split("\n")
    sections: List[Dict[str, str]] = []
    current_name: Optional[str] = None
    current_lines: List[str] = []

    for line in lines:
        m = _PAT_REG_HEADER.match(line.strip())
        if m:
            # Save previous section
            if current_name and current_lines:
                sections.append({
                    "name": current_name,
                    "text": "\n".join(current_lines),
                })
            # Strip parenthetical suffix e.g. "TMADCx_MODCFG (x=0-6)" -> "TMADCx_MODCFG"
            raw_name = m.group(1)
            current_name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name)
            current_lines = [line]
        elif current_name:
            current_lines.append(line)

    # Save last section
    if current_name and current_lines:
        sections.append({
            "name": current_name,
            "text": "\n".join(current_lines),
        })

    return sections


def _has_register_metadata(section_text: str) -> bool:
    """Check if a section looks like it has register definition metadata (offset/reset)."""
    lower = section_text.lower()
    return "offset address" in lower or "reset value" in lower


def _extract_register_section(
    client,
    model: str,
    section: Dict[str, str],
    retry: int = 0,
) -> Optional[Dict[str, Any]]:
    """Send a single register section to the LLM and parse the JSON response."""
    text = section["text"]

    # Strip verbose diagram explanations — they add noise without useful field data.
    # Also strip page-break markers and repeated chapter headers that interrupt field tables.
    lines = text.split("\n")
    kept = []
    in_diagram_explanation = False
    for line in lines:
        stripped = line.strip()

        # Skip page-break markers (e.g. "## Pages 71\u201372" or "## Pages 77-78")
        if re.match(r"^#{1,2}\s+Pages?\s+\d+", stripped):
            continue

        # Skip repeated chapter/section headers (e.g. "# 41 General Purpose Timer (GPT12)")
        if re.match(r"^#{1,2}\s+\d+\s+", stripped):
            continue

        # Skip "(continued)" lines
        if stripped == "(continued)":
            continue

        # Skip "*(table continues…)*" lines
        if stripped.startswith("*(table continues"):
            continue

        # Detect start of diagram explanation block
        if (stripped.startswith("### Diagram explanation") or
                stripped.startswith("### Figure:") or
                stripped.startswith("**What is this diagram showing") or
                stripped.startswith("**What this diagram")):
            in_diagram_explanation = True
            continue

        # End diagram explanation when we hit a field table, another heading, or register-relevant content
        if in_diagram_explanation:
            if (stripped.startswith("|") or stripped.startswith("###") or
                    "offset address" in stripped.lower() or "reset value" in stripped.lower()):
                in_diagram_explanation = False
            else:
                continue

        kept.append(line)

    text = "\n".join(kept)

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            max_completion_tokens=2000,
            messages=[
                {"role": "system", "content": _EXTRACTION_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        raw = resp.choices[0].message.content or ""

        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
            raw = re.sub(r"\n?```\s*$", "", raw)

        data = json.loads(raw)
        return data

    except json.JSONDecodeError as e:
        if retry < _MAX_RETRIES - 1:
            logger.warning(
                "JSON parse error for %s (attempt %d): %s. Retrying...",
                section["name"], retry + 1, e,
            )
            time.sleep(2)
            return _extract_register_section(client, model, section, retry + 1)
        logger.error("Failed to parse LLM JSON for %s after %d retries", section["name"], _MAX_RETRIES)
        return None

    except Exception as e:
        if retry < _MAX_RETRIES - 1:
            is_rate_limit = "429" in str(e) or "Rate limit" in str(e)
            wait = 30 if is_rate_limit else 5
            logger.warning(
                "LLM call failed for %s (attempt %d): %s. Retrying in %ds...",
                section["name"], retry + 1, e, wait,
            )
            time.sleep(wait)
            return _extract_register_section(client, model, section, retry + 1)
        logger.error("LLM extraction failed for %s after %d retries: %s", section["name"], _MAX_RETRIES, e)
        return None


def _assemble_results(
    extracted: List[Dict[str, Any]],
    source_name: str,
) -> Dict[str, Any]:
    """Assemble individual register extractions into the standard output format."""
    registers: List[Dict[str, Any]] = []
    fields: List[Dict[str, Any]] = []
    seen_regs: set = set()

    for item in extracted:
        if not item:
            continue

        reg_data = item.get("register", {})
        reg_name = reg_data.get("name", "")
        if not reg_name or reg_name in seen_regs:
            continue
        seen_regs.add(reg_name)

        registers.append({
            "name": reg_name,
            "long_name": reg_data.get("long_name", ""),
            "offset": reg_data.get("offset"),
            "reset_value": reg_data.get("reset_value"),
            "reset_type": reg_data.get("reset_type", "kernel"),
        })

        for f in item.get("fields", []):
            fname = f.get("name", "")
            if not fname:
                continue
            fields.append({
                "name": fname,
                "parent_register": reg_name,
                "bits": f.get("bits", ""),
                "type": f.get("type", ""),
                "description": (f.get("description") or "")[:200],
            })

    # Derive interrupts and errors from fields (same logic as regex parser)
    interrupts = _extract_interrupts(fields)
    errors = _extract_errors(fields)

    return {
        "metadata": {
            "source_file": source_name,
            "parser": "hw_um_llm_parser",
            "extraction_date": datetime.now().isoformat(),
            "counts": {
                "registers": len(registers),
                "fields": len(fields),
                "interrupts": len(interrupts),
                "errors": len(errors),
            },
        },
        "registers": registers,
        "fields": fields,
        "interrupts": interrupts,
        "errors": errors,
        "formulas": [],
        "relationships": [],
    }


# ---------------------------------------------------------------------------
# Interrupt / Error extraction (from fields — same heuristic as regex parser)
# ---------------------------------------------------------------------------
_INTERRUPT_KWS = {"INTR", "INT", "IRQ", "INTERRUPT", "SRN", "SRC"}
_ERROR_KWS = {"ERROR", "ERR", "FAULT", "TIMEOUT", "FAIL", "VIOLATION", "LOSS", "OVERRUN", "UNDERRUN"}


def _extract_interrupts(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find interrupt-related single-bit fields."""
    interrupts: List[Dict[str, Any]] = []
    seen: set = set()

    for f in fields:
        name = f["name"].upper()
        desc = f.get("description", "").upper()
        reg = f.get("parent_register", "").upper()

        is_interrupt = (
            any(kw in name for kw in _INTERRUPT_KWS)
            or any(kw in reg for kw in _INTERRUPT_KWS)
            or "INTERRUPT" in desc
            or "SERVICE REQUEST" in desc
        )
        if not is_interrupt:
            continue
        if ":" in f["bits"]:
            continue
        if f["name"] in seen:
            continue
        seen.add(f["name"])
        interrupts.append({
            "name": f["name"],
            "register": f["parent_register"],
            "bit": int(f["bits"]) if f["bits"].isdigit() else 0,
            "description": f.get("description", "")[:200],
        })

    return interrupts


def _extract_errors(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find error-related fields."""
    errors: List[Dict[str, Any]] = []
    seen: set = set()

    for f in fields:
        name = f["name"].upper()
        if not any(kw in name for kw in _ERROR_KWS):
            continue
        if f["name"] in seen:
            continue
        seen.add(f["name"])
        errors.append({
            "name": f["name"],
            "register": f["parent_register"],
            "bits": f["bits"],
            "type": f["type"],
            "description": f.get("description", "")[:200],
        })

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(
    path: str,
    *,
    model: str = _DEFAULT_MODEL,
    max_workers: int = 3,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract hardware register specs from a TC44x User Manual Markdown file
    using LLM-based extraction.

    Args:
        path: Path to the ``.md`` file (output from pdf_parser).
        model: LLM model name (default: gpt-4o).
        max_workers: Parallel workers for LLM calls (default: 3).
        api_key: Optional API key override. If None, uses token_manager.

    Returns:
        A dict with keys ``metadata``, ``registers``, ``fields``,
        ``interrupts``, ``errors``, ``formulas``, ``relationships``.
        Format is identical to ``hw_spec_parser.parse()`` / ``hw_um_parser.parse()``.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    content = p.read_text(encoding="utf-8")
    logger.info("Parsing HW UM (LLM) from %s (%d lines)", p.name, content.count("\n") + 1)

    # 1. Split into register sections
    sections = _split_register_sections(content)
    logger.info("Found %d register-header sections", len(sections))

    # Filter to sections that actually have register metadata
    reg_sections = [s for s in sections if _has_register_metadata(s["text"])]
    logger.info("Filtered to %d sections with offset/reset metadata", len(reg_sections))

    if not reg_sections:
        logger.warning("No register sections found in %s", p.name)
        return _assemble_results([], p.name)

    # 2. Get LLM client
    token = api_key or _get_token()
    client = _build_client(token, model)

    # 3. Extract in parallel (with rate limiting)
    extracted: List[Optional[Dict[str, Any]]] = [None] * len(reg_sections)
    rate_limiter = _RateLimiter(_RATE_LIMIT_RPM)

    def _process(idx: int) -> tuple:
        rate_limiter.acquire()
        result = _extract_register_section(client, model, reg_sections[idx])
        return idx, result

    logger.info("Starting LLM extraction for %d registers (workers=%d)...", len(reg_sections), max_workers)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process, i): i for i in range(len(reg_sections))}
        done = 0
        for future in as_completed(futures):
            idx, result = future.result()
            extracted[idx] = result
            done += 1
            if done % 5 == 0 or done == len(reg_sections):
                logger.info("  Progress: %d/%d registers extracted", done, len(reg_sections))

    elapsed = time.time() - t0
    success = sum(1 for r in extracted if r is not None)
    logger.info(
        "LLM extraction complete: %d/%d successful in %.1fs",
        success, len(reg_sections), elapsed,
    )

    # 4. Assemble results
    results = _assemble_results([r for r in extracted if r], p.name)
    logger.info(
        "Final: %d registers, %d fields, %d interrupts, %d errors",
        len(results["registers"]),
        len(results["fields"]),
        len(results["interrupts"]),
        len(results["errors"]),
    )

    return results

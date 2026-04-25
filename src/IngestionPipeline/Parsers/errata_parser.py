"""
Errata Parser
=============

Parses errata / advisory documents (PDF, XLSX, CSV) and extracts
individual errata items with severity, affected modules, and workarounds.

Usage:
    from IngestionPipeline.Parsers import errata_parser

    result = errata_parser.parse("errata.xlsx", module="Adc")
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Filename patterns that identify errata documents
FILENAME_PATTERNS = [
    re.compile(r"errata", re.I),
    re.compile(r"advisory", re.I),
    re.compile(r"silicon.?issue", re.I),
]

SEVERITY_MAP = {
    "critical": "Critical", "crit": "Critical", "high": "Critical",
    "major": "Major", "med": "Major", "medium": "Major",
    "minor": "Minor", "low": "Minor",
    "info": "Info", "informational": "Info", "note": "Info",
}


def matches_filename(filename: str) -> bool:
    """Return True if *filename* looks like an errata document."""
    return any(p.search(filename) for p in FILENAME_PATTERNS)


def parse(
    path: str,
    *,
    module: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse an errata document and return structured items.

    Returns
    -------
    dict
        ``{"parse_type": "errata", "module": str, "items": list[dict]}``
    """
    p = Path(path)
    ext = p.suffix.lower()

    if ext == ".csv":
        items = _parse_csv(p)
    elif ext == ".xlsx":
        items = _parse_xlsx(p)
    elif ext == ".pdf":
        items = _parse_pdf(p)
    else:
        raise ValueError(f"Unsupported errata format: {ext}")

    # Tag with module
    for item in items:
        if module and not item.get("module"):
            item["module"] = module

    logger.info("[ErrataParser] Parsed %d items from %s", len(items), p.name)
    return {
        "parse_type": "errata",
        "module": module or "",
        "items": items,
        "source_file": str(p),
    }


def _normalize_severity(raw: str) -> str:
    return SEVERITY_MAP.get(raw.strip().lower(), "Info")


def _parse_csv(path: Path) -> List[Dict[str, Any]]:
    items = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            item = _row_to_errata(row)
            if item:
                items.append(item)
    return items


def _parse_xlsx(path: Path) -> List[Dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("pandas required for XLSX parsing: pip install pandas openpyxl")

    df = pd.read_excel(path, sheet_name=0)
    items = []
    for _, row in df.iterrows():
        item = _row_to_errata(row.to_dict())
        if item:
            items.append(item)
    return items


def _parse_pdf(path: Path) -> List[Dict[str, Any]]:
    """Best-effort PDF errata extraction using pdf_parser."""
    try:
        from IngestionPipeline.Parsers import pdf_parser
        md_text = pdf_parser.parse(str(path))
    except Exception:
        md_text = path.read_text(encoding="utf-8", errors="replace")

    items = []
    # Try to split on errata IDs like "ERRATA-001" or "ERR_TC39x_001"
    pattern = re.compile(r"(ERR(?:ATA)?[-_]\S+)", re.I)
    blocks = pattern.split(md_text)

    for i in range(1, len(blocks), 2):
        errata_id = blocks[i].strip()
        body = blocks[i + 1] if i + 1 < len(blocks) else ""
        severity = "Info"
        for sev_key in SEVERITY_MAP:
            if re.search(rf"\b{sev_key}\b", body, re.I):
                severity = SEVERITY_MAP[sev_key]
                break
        items.append({
            "errata_id": errata_id,
            "title": errata_id,
            "severity": severity,
            "description": body[:2000].strip(),
            "workaround": "",
            "status": "Open",
            "affected_modules": [],
        })

    return items


def _row_to_errata(row: dict) -> Optional[Dict[str, Any]]:
    """Convert a row dict (from CSV/XLSX) to an errata item dict."""
    # Flexible column name matching
    def _get(candidates):
        for c in candidates:
            for k, v in row.items():
                if k and c.lower() in str(k).lower() and v and str(v).strip():
                    return str(v).strip()
        return ""

    errata_id = _get(["errata_id", "id", "issue_id", "number", "err_id"])
    title = _get(["title", "summary", "name", "subject"])
    if not errata_id and not title:
        return None

    severity_raw = _get(["severity", "priority", "impact", "level"])
    return {
        "errata_id": errata_id or title[:40],
        "title": title or errata_id,
        "severity": _normalize_severity(severity_raw) if severity_raw else "Info",
        "description": _get(["description", "detail", "body"]),
        "workaround": _get(["workaround", "mitigation", "fix", "resolution"]),
        "status": _get(["status", "state"]) or "Open",
        "affected_modules": [
            m.strip() for m in _get(["module", "affected", "component"]).split(",") if m.strip()
        ],
    }

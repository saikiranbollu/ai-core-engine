"""
Datasheet Parser
================

Parses datasheet documents (PDF, XLSX) and extracts parametric
specifications (min/typ/max values, units, conditions).

Usage:
    from IngestionPipeline.Parsers import datasheet_parser

    result = datasheet_parser.parse("datasheet.xlsx", module="Adc")
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FILENAME_PATTERNS = [
    re.compile(r"datasheet", re.I),
    re.compile(r"ds_", re.I),
    re.compile(r"electrical.?spec", re.I),
]


def matches_filename(filename: str) -> bool:
    """Return True if *filename* looks like a datasheet."""
    return any(p.search(filename) for p in FILENAME_PATTERNS)


def parse(
    path: str,
    *,
    module: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a datasheet and return parametric specifications.

    Returns
    -------
    dict
        ``{"parse_type": "datasheet", "module": str, "specs": list[dict]}``
    """
    p = Path(path)
    ext = p.suffix.lower()

    if ext == ".xlsx":
        specs = _parse_xlsx(p)
    elif ext == ".pdf":
        specs = _parse_pdf(p)
    else:
        raise ValueError(f"Unsupported datasheet format: {ext}")

    for spec in specs:
        if module and not spec.get("module"):
            spec["module"] = module

    logger.info("[DatasheetParser] Parsed %d specs from %s", len(specs), p.name)
    return {
        "parse_type": "datasheet",
        "module": module or "",
        "specs": specs,
        "source_file": str(p),
    }


def _parse_xlsx(path: Path) -> List[Dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("pandas required: pip install pandas openpyxl")

    df = pd.read_excel(path, sheet_name=0)
    specs = []
    for _, row in df.iterrows():
        spec = _row_to_spec(row.to_dict())
        if spec:
            specs.append(spec)
    return specs


def _parse_pdf(path: Path) -> List[Dict[str, Any]]:
    """Best-effort PDF datasheet extraction — looks for tables with min/typ/max."""
    try:
        from IngestionPipeline.Parsers import pdf_parser
        md_text = pdf_parser.parse(str(path))
    except Exception:
        md_text = path.read_text(encoding="utf-8", errors="replace")

    specs = []
    # Look for table rows with parametric data: Parameter | Min | Typ | Max | Unit
    pattern = re.compile(
        r"([A-Za-z_][\w\s]+?)\s*\|\s*([0-9.\-]*)\s*\|\s*([0-9.\-]*)\s*\|\s*([0-9.\-]*)\s*\|\s*(\S+)"
    )
    for m in pattern.finditer(md_text):
        specs.append({
            "parameter_name": m.group(1).strip(),
            "min_value": m.group(2).strip() or None,
            "typ_value": m.group(3).strip() or None,
            "max_value": m.group(4).strip() or None,
            "unit": m.group(5).strip(),
            "conditions": "",
            "category": "",
        })

    return specs


def _row_to_spec(row: dict) -> Optional[Dict[str, Any]]:
    """Convert a row dict to a datasheet specification."""
    def _get(candidates):
        for c in candidates:
            for k, v in row.items():
                if k and c.lower() in str(k).lower() and v is not None:
                    val = str(v).strip()
                    if val and val.lower() != "nan":
                        return val
        return None

    param = _get(["parameter", "param", "name", "symbol"])
    if not param:
        return None

    return {
        "parameter_name": param,
        "min_value": _get(["min", "minimum"]),
        "typ_value": _get(["typ", "typical"]),
        "max_value": _get(["max", "maximum"]),
        "unit": _get(["unit", "units"]) or "",
        "conditions": _get(["condition", "test_condition", "note"]) or "",
        "category": _get(["category", "group", "section"]) or "",
    }

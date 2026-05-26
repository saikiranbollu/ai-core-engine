"""
SRS .dox Parser (v3.0)
======================

Extracts iLLD Software Requirements Specification entries from Doxygen
``@uid{...}`` blocks found in ``Ifx<FB>_srs.dox`` files.

Canonical block shape::

    @uid{AURC1-REQA-286 , Enable Module}
    @uid_litem{Description}
    Enable Module
    @uid_fw_trace
    - @tr{IfxCxpi_Cxpi_enableModule}: Function for enabling module clock.
    - @tr{IfxCxpi_Cxpi_disableModule}: ...
    @enduid

Produces::

    {
        "requirements": [
            {"id": "AURC1-REQA-286",
             "name": "Enable Module",
             "description": "Enable Module",
             "source_file": "IfxCxpi_srs.dox"},
            ...
        ],
        "traces": [
            {"requirement_id": "AURC1-REQA-286",
             "function": "IfxCxpi_Cxpi_enableModule",
             "description": "Function for enabling module clock."},
            ...
        ],
        "metadata": {
            "source_file": "...",
            "extraction_date": "...",
            "counts": {"requirements": N, "traces": M}
        }
    }
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("srs_dox_parser")

# Tolerant regexes — Doxygen uses both '@' and '\' for commands.
_UID_OPEN_RE = re.compile(
    r"[@\\]uid\s*\{\s*(?P<id>[A-Z0-9][A-Z0-9_-]*)\s*,\s*(?P<name>[^}]+?)\s*\}",
    re.IGNORECASE,
)
_UID_END_RE = re.compile(r"[@\\]enduid\b", re.IGNORECASE)
_LITEM_RE = re.compile(r"[@\\]uid_litem\s*\{\s*([^}]+?)\s*\}", re.IGNORECASE)
_FW_TRACE_RE = re.compile(r"[@\\]uid_fw_trace\b", re.IGNORECASE)
_TR_RE = re.compile(
    r"[@\\]tr\s*\{\s*(?P<fn>[A-Za-z_][A-Za-z0-9_]*)\s*\}\s*:?\s*(?P<desc>.*)",
    re.IGNORECASE,
)


def _strip_comment_decoration(line: str) -> str:
    """Remove Doxygen comment markers like ``/*!``, ``*``, ``*/`` so we can
    process .dox lines exactly like the in-XML highlighted version."""
    s = line.strip()
    if s.startswith("/*!"):
        s = s[3:]
    if s.startswith("/*"):
        s = s[2:]
    if s.endswith("*/"):
        s = s[:-2]
    if s.startswith("*"):
        s = s[1:]
    return s.strip()


def parse(path: str) -> Dict[str, Any]:
    """Parse an SRS .dox file and return requirements + forward-trace links."""
    p = Path(path)
    content = p.read_text(encoding="utf-8", errors="replace")
    lines = [_strip_comment_decoration(ln) for ln in content.splitlines()]
    source_file = p.name

    requirements: List[Dict[str, Any]] = []
    traces: List[Dict[str, Any]] = []

    i = 0
    n = len(lines)
    while i < n:
        m = _UID_OPEN_RE.search(lines[i])
        if not m:
            i += 1
            continue

        req_id = m.group("id").strip()
        req_name = m.group("name").strip()
        description_parts: List[str] = []
        current_litem: str | None = None
        in_fw_trace = False
        block_traces: List[Dict[str, Any]] = []

        j = i + 1
        while j < n:
            line = lines[j]
            if _UID_END_RE.search(line):
                break

            lm = _LITEM_RE.search(line)
            if lm:
                current_litem = lm.group(1).strip().lower()
                in_fw_trace = False
                j += 1
                continue

            if _FW_TRACE_RE.search(line):
                in_fw_trace = True
                current_litem = None
                j += 1
                continue

            if in_fw_trace:
                stripped = line.lstrip("- \t")
                tm = _TR_RE.search(stripped)
                if tm:
                    block_traces.append({
                        "requirement_id": req_id,
                        "function": tm.group("fn").strip(),
                        "description": tm.group("desc").strip().rstrip(".") or None,
                    })
            elif current_litem == "description":
                if line.strip():
                    description_parts.append(line.strip())

            j += 1

        requirements.append({
            "id": req_id,
            "name": req_name,
            "description": " ".join(description_parts).strip() or req_name,
            "source_file": source_file,
        })
        traces.extend(block_traces)
        i = j + 1  # skip past @enduid

    logger.info(
        "Parsed SRS %s: %d requirements, %d forward-trace links",
        source_file, len(requirements), len(traces),
    )

    return {
        "requirements": requirements,
        "traces": traces,
        "metadata": {
            "source_file": source_file,
            "extraction_date": datetime.now().isoformat(),
            "counts": {
                "requirements": len(requirements),
                "traces": len(traces),
            },
        },
    }


__all__ = ["parse"]

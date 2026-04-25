"""
Release Parser
==============

Parses release / changelog documents (Markdown, TXT, JSON) and extracts
version metadata, release dates, changelogs, and module lists.

Usage:
    from IngestionPipeline.Parsers import release_parser

    result = release_parser.parse("CHANGELOG.md", module="Can")
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FILENAME_PATTERNS = [
    re.compile(r"release", re.I),
    re.compile(r"changelog", re.I),
    re.compile(r"version", re.I),
    re.compile(r"whatsnew", re.I),
]


def matches_filename(filename: str) -> bool:
    """Return True if *filename* looks like a release document."""
    return any(p.search(filename) for p in FILENAME_PATTERNS)


def parse(
    path: str,
    *,
    module: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a release document and return version metadata.

    Returns
    -------
    dict
        ``{"parse_type": "release", "module": str, "releases": list[dict]}``
    """
    p = Path(path)
    ext = p.suffix.lower()

    if ext == ".json":
        releases = _parse_json(p)
    elif ext in (".md", ".txt", ".rst"):
        content = p.read_text(encoding="utf-8", errors="replace")
        releases = _parse_changelog(content, p.name)
    else:
        raise ValueError(f"Unsupported release format: {ext}")

    for rel in releases:
        if module and not rel.get("module"):
            rel["module"] = module

    logger.info("[ReleaseParser] Parsed %d releases from %s", len(releases), p.name)
    return {
        "parse_type": "release",
        "module": module or "",
        "releases": releases,
        "source_file": str(p),
    }


def _parse_json(path: Path) -> List[Dict[str, Any]]:
    """Parse JSON release metadata (array of objects or single object)."""
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        data = [data]

    releases = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        releases.append({
            "release_version": entry.get("version", entry.get("tag_name", "unknown")),
            "release_date": entry.get("date", entry.get("release_date", "")),
            "branch_name": entry.get("branch", entry.get("branch_name", "")),
            "changelog": entry.get("changelog", entry.get("body", entry.get("notes", ""))),
            "status": entry.get("status", "Released"),
        })
    return releases


def _parse_changelog(content: str, source_name: str) -> List[Dict[str, Any]]:
    """Parse a markdown/text changelog into release entries."""
    releases = []

    # Match version headings like: ## v2.0.0 (2024-01-15), # [1.0.0], ## Version 3.0
    version_pattern = re.compile(
        r"^#{1,3}\s+(?:v(?:ersion)?\s*)?(\[?[\d]+\.[\d]+(?:\.[\d]+)?(?:[._-]\w+)?\]?)"
        r"(?:\s*[-–—]\s*|\s*\()?(\d{4}[-/]\d{2}[-/]\d{2})?",
        re.MULTILINE | re.IGNORECASE,
    )

    matches = list(version_pattern.finditer(content))
    for i, m in enumerate(matches):
        version = m.group(1).strip("[]")
        date = m.group(2) or ""

        # Extract body between this heading and the next
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        # Detect status from body keywords
        status = "Released"
        if re.search(r"\b(unreleased|upcoming|planned)\b", body, re.I):
            status = "Planning"
        elif re.search(r"\b(deprecated|eol|end.of.life)\b", body, re.I):
            status = "Deprecated"

        releases.append({
            "release_version": version,
            "release_date": date,
            "branch_name": "",
            "changelog": body[:5000],
            "status": status,
        })

    # If no structured headings found, treat as a single release note
    if not releases and content.strip():
        # Try to extract a version from filename
        ver_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", source_name)
        releases.append({
            "release_version": ver_match.group(1) if ver_match else "unknown",
            "release_date": "",
            "branch_name": "",
            "changelog": content[:5000].strip(),
            "status": "Released",
        })

    return releases

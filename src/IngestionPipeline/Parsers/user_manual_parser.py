"""
User Manual Parser
==================

Parses hardware user manual documents (PDF, Markdown, RST) and extracts
hierarchical sections with content, building a parent-child tree.

Usage:
    from IngestionPipeline.Parsers import user_manual_parser

    result = user_manual_parser.parse("user_manual.pdf", module="Adc")
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FILENAME_PATTERNS = [
    re.compile(r"user.?manual", re.I),
    re.compile(r"um_", re.I),
    re.compile(r"usermanual", re.I),
    re.compile(r"hw_user", re.I),
]


def matches_filename(filename: str) -> bool:
    """Return True if *filename* looks like a user manual."""
    return any(p.search(filename) for p in FILENAME_PATTERNS)


def parse(
    path: str,
    *,
    module: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a user manual and return hierarchical sections.

    Returns
    -------
    dict
        ``{"parse_type": "user_manual", "module": str, "sections": list[dict]}``
    """
    p = Path(path)
    ext = p.suffix.lower()

    if ext == ".pdf":
        sections = _parse_pdf(p)
    elif ext in (".md", ".rst"):
        content = p.read_text(encoding="utf-8", errors="replace")
        sections = _parse_markdown(content, p.name)
    else:
        raise ValueError(f"Unsupported user manual format: {ext}")

    # Assign module and build parent chain
    _assign_hierarchy(sections, module)

    logger.info("[UserManualParser] Parsed %d sections from %s", len(sections), p.name)
    return {
        "parse_type": "user_manual",
        "module": module or "",
        "sections": sections,
        "source_file": str(p),
    }


def _parse_pdf(path: Path) -> List[Dict[str, Any]]:
    """Parse PDF user manual into sections."""
    try:
        from IngestionPipeline.Parsers import pdf_parser
        md_text = pdf_parser.parse(str(path))
    except Exception:
        md_text = path.read_text(encoding="utf-8", errors="replace")
    return _parse_markdown(md_text, path.name)


def _parse_markdown(content: str, source_name: str) -> List[Dict[str, Any]]:
    """Split markdown/RST content into hierarchical sections by headings."""
    sections = []
    # Match Markdown headings (# H1, ## H2, etc.) and RST underline headings
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    lines = content.split("\n")
    current_section = None
    current_content: List[str] = []
    section_counter = 0

    for line in lines:
        m = heading_pattern.match(line)
        if m:
            # Save previous section
            if current_section:
                current_section["content"] = "\n".join(current_content).strip()
                sections.append(current_section)

            level = len(m.group(1))
            title = m.group(2).strip()
            section_counter += 1

            # Generate section ID from source + counter
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", source_name.split(".")[0])
            current_section = {
                "section_id": f"UM_{safe_name}_S{section_counter}",
                "title": title,
                "level": level,
                "content": "",
                "parent_section": None,
            }
            current_content = []
        else:
            current_content.append(line)

    # Save last section
    if current_section:
        current_section["content"] = "\n".join(current_content).strip()
        sections.append(current_section)

    # If no headings found, treat entire content as one section
    if not sections and content.strip():
        sections.append({
            "section_id": f"UM_{re.sub(r'[^a-zA-Z0-9]', '_', source_name.split('.')[0])}_S1",
            "title": source_name,
            "level": 1,
            "content": content[:10000],
            "parent_section": None,
        })

    return sections


def _assign_hierarchy(sections: List[Dict], module: Optional[str]) -> None:
    """Assign parent_section fields and module to each section in-place."""
    # Stack of (level, section_id) for hierarchy tracking
    stack: List[tuple] = []

    for sec in sections:
        level = sec.get("level", 1)
        # Pop stack until we find a parent with a lower level
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            sec["parent_section"] = stack[-1][1]
        stack.append((level, sec["section_id"]))
        if module:
            sec["module"] = module

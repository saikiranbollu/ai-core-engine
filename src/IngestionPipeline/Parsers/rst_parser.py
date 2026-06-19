"""
reStructuredText Parser
========================

Parses ``.rst`` files and returns a list of sections with their title,
heading level, and body content.

Usage:
    from IngestionPipeline.parsers import rst_parser

    sections = rst_parser.parse("documentation.rst")
    # returns a list of dicts with title, level, content
"""

import re
from pathlib import Path
from typing import Dict, List


_LEVEL_MAP = {'=': 1, '~': 2, '^': 3, '-': 4}


def parse(path: str) -> List[Dict[str, object]]:
    """
    Parse an RST file into a list of sections.

    Args:
        path: Path to a ``.rst`` file.

    Returns:
        A list of dicts, each with ``title`` (str), ``level`` (int 1-5),
        and ``content`` (str).

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = p.read_text(encoding="utf-8")

    pattern = r'(^[^\n]+)\n([=~\^-]{3,})\n(.*?)(?=\n^[^\n]+\n[=~\^-]{3,}|\Z)'
    matches = re.findall(pattern, text, re.MULTILINE | re.DOTALL)

    return [
        {
            "title": title.strip(),
            "level": _LEVEL_MAP.get(underline[0], 5),
            "content": content.strip(),
        }
        for title, underline, content in matches
    ]

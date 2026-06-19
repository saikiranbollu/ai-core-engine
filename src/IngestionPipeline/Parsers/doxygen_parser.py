"""
Doxygen Requirements Parser
============================

Extracts requirement blocks (uid, title, description) from Doxygen-annotated
files.

Usage:
    from IngestionPipeline.parsers import doxygen_parser

    requirements = doxygen_parser.parse("path/to/doxygen_file.h")
    # returns a list of dicts with uid, title, description
"""

import re
from pathlib import Path
from typing import Dict, List


def parse(path: str) -> List[Dict[str, str]]:
    """
    Parse a Doxygen file and extract requirement blocks.

    Args:
        path: Path to a Doxygen-annotated file.

    Returns:
        A list of dicts, each containing ``uid``, ``title``, and
        ``description``.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    content = p.read_text(encoding="utf-8")

    pattern = (
        r"@uid\{([^,]+),\s*([^\}]+)\}.*?"
        r"@uid_litem\{Description\}\s*(.*?)\s*"
        r"@uid_fw_trace.*?@enduid"
    )

    requirements: List[Dict[str, str]] = []
    for uid, title, description in re.findall(pattern, content, re.DOTALL):
        requirements.append({
            "uid": uid.strip(),
            "title": title.strip(),
            "description": description.strip(),
        })

    return requirements

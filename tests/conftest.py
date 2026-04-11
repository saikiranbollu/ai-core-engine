"""
Shared fixtures for ai-core-engine tests.

Sets up sys.path so that ``domain_apps``, ``src/MemoryLayer``, and
``mcp`` packages are importable regardless of the working directory.
"""

import sys
from pathlib import Path

# ── path bootstrapping ──────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent          # ai-core-engine/
_ML   = _ROOT / "src" / "MemoryLayer"

for p in [str(_ROOT), str(_ML)]:
    if p not in sys.path:
        sys.path.insert(0, p)

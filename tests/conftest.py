"""
Shared fixtures for ai-core-engine tests.

Sets up sys.path so that ``domain_apps``, ``src/MemoryLayer``, and
``mcp`` packages are importable regardless of the working directory.
"""

import sys
from pathlib import Path

import pytest

# ── path bootstrapping ──────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent          # ai-core-engine/
_SRC  = _ROOT / "src"
_ML   = _ROOT / "src" / "MemoryLayer"

for p in [str(_ROOT), str(_SRC), str(_ML)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── pytest marker registration ──────────────────────────────────────────────
def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast unit tests with no external dependencies")
    config.addinivalue_line("markers", "integration: tests requiring mocked or real services")
    config.addinivalue_line("markers", "e2e: end-to-end tests against a deployed MCP server")

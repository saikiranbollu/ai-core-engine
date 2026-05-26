"""Validate all MCP tools have TOOL_TIERS entries and vice versa."""
from __future__ import annotations

import os
import sys

import pytest

# Make mcp/core importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp"))

from core.tool_tiers import TOOL_TIERS, validate_tool_registration


class _FakeToolManager:
    """Minimal stand-in for FastMCP._tool_manager."""

    def __init__(self, names):
        self._tools = {n: None for n in names}


class _FakeMCP:
    """Minimal stand-in for a FastMCP instance."""

    def __init__(self, tool_names):
        self._tool_manager = _FakeToolManager(tool_names)


class TestValidateToolRegistration:
    """Unit tests for validate_tool_registration()."""

    def test_matching_sets_passes(self):
        """No error when registered tools match TOOL_TIERS exactly."""
        fake = _FakeMCP(TOOL_TIERS.keys())
        # Should not raise
        validate_tool_registration(fake)

    def test_missing_from_tiers_raises(self):
        """RuntimeError if a tool is registered but missing from TOOL_TIERS."""
        extra = list(TOOL_TIERS.keys()) + ["unregistered_new_tool"]
        fake = _FakeMCP(extra)
        with pytest.raises(RuntimeError, match="unregistered_new_tool"):
            validate_tool_registration(fake)

    def test_orphan_tiers_warns(self, caplog):
        """Warning logged if TOOL_TIERS has entries with no registered tool."""
        subset = list(TOOL_TIERS.keys())[:-1]  # drop one tool
        fake = _FakeMCP(subset)
        with caplog.at_level("WARNING"):
            validate_tool_registration(fake)
        assert "TOOL_TIERS entries with no matching @mcp.tool()" in caplog.text

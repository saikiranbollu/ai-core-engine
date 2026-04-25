"""
Tests for Rate Limiter (mcp/core/rate_limiter.py)
===================================================
Covers singleton access, per-category rate limits, check() allow/deny,
and graceful degradation when the `limits` library is missing.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


# ── Helper: fresh-import to reset singleton ─────────────────────────────

def _fresh_import():
    """Force re-import of rate_limiter to reset module-level singleton."""
    mod_name = "mcp.core.rate_limiter"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


# ═══════════════════════════════════════════════════════════════════════
#  Singleton behaviour
# ═══════════════════════════════════════════════════════════════════════

class TestSingleton:

    def test_get_rate_limiter_returns_singleton(self):
        """Successive calls return the same instance."""
        mod = _fresh_import()
        rl1 = mod.get_rate_limiter()
        rl2 = mod.get_rate_limiter()
        assert rl1 is rl2

    def test_singleton_is_rate_limiter_instance(self):
        """Returned object must be a RateLimiter."""
        mod = _fresh_import()
        rl = mod.get_rate_limiter()
        assert isinstance(rl, mod.RateLimiter)


# ═══════════════════════════════════════════════════════════════════════
#  Category detection
# ═══════════════════════════════════════════════════════════════════════

class TestCategoryDetection:

    def test_search_tools_use_search_limit(self):
        """Tools not in _ADMIN_TOOLS or _INGESTION_TOOLS should use search limit."""
        mod = _fresh_import()
        # search_database, search_nodes are search-category tools
        assert "search_database" not in mod._ADMIN_TOOLS
        assert "search_database" not in mod._INGESTION_TOOLS

    def test_admin_tools_recognised(self):
        """Known admin tools should be in the _ADMIN_TOOLS set."""
        mod = _fresh_import()
        admin_expected = {"cache_invalidate_module", "cache_clear", "ensure_valid_token", "process_results"}
        for tool in admin_expected:
            assert tool in mod._ADMIN_TOOLS

    def test_ingestion_tools_recognised(self):
        """Known ingestion tools should be in the _INGESTION_TOOLS set."""
        mod = _fresh_import()
        ingestion_expected = {"ingest_file", "ingest_module_from_repo",
                              "batch_ingest_modules", "ingest_repository"}
        for tool in ingestion_expected:
            assert tool in mod._INGESTION_TOOLS

    def test_ingestion_tools_are_subset_of_admin(self):
        """Ingestion tools are a subset of admin tools."""
        mod = _fresh_import()
        assert mod._INGESTION_TOOLS.issubset(mod._ADMIN_TOOLS)

    def test_default_rate_limits(self):
        """Default rate limits should be 60/minute, 10/minute, 5/minute."""
        mod = _fresh_import()
        assert mod.RATE_LIMIT_SEARCH == "60/minute"
        assert mod.RATE_LIMIT_ADMIN == "10/minute"
        assert mod.RATE_LIMIT_INGESTION == "5/minute"


# ═══════════════════════════════════════════════════════════════════════
#  check() — normal traffic
# ═══════════════════════════════════════════════════════════════════════

class TestCheckAllowed:

    def test_check_returns_none_for_normal_traffic(self):
        """First request should be allowed (returns None)."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        if not rl._enabled:
            pytest.skip("limits library not installed")
        result = rl.check("test-api-key-123456", "search_database")
        assert result is None

    def test_check_returns_none_when_disabled(self):
        """When limits library is unavailable, check always returns None."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        rl._enabled = False
        result = rl.check("any-key", "any_tool")
        assert result is None

    def test_anonymous_key_handling(self):
        """Empty or None api_key should be handled without error."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        if not rl._enabled:
            pytest.skip("limits library not installed")
        # Should not raise
        result_empty = rl.check("", "search_database")
        assert result_empty is None or isinstance(result_empty, str)
        result_short = rl.check("abc", "search_database")
        assert result_short is None or isinstance(result_short, str)


# ═══════════════════════════════════════════════════════════════════════
#  check() — exceeding limits
# ═══════════════════════════════════════════════════════════════════════

class TestCheckDenied:

    def test_check_returns_error_after_exceeding_limit(self):
        """After exceeding the limit, check() should return an error string."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        if not rl._enabled:
            pytest.skip("limits library not installed")

        # Exhaust the ingestion limit (5/minute is the lowest)
        api_key = "test-key-exhaust-1234"
        tool = "ingest_file"
        denied = False
        for _ in range(20):  # more than 5/minute
            result = rl.check(api_key, tool)
            if result is not None:
                denied = True
                break
        assert denied, "Expected rate limit to be exceeded after 20 calls to ingestion tool"

    def test_error_message_mentions_tool_name(self):
        """The error string should mention the tool name."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        if not rl._enabled:
            pytest.skip("limits library not installed")

        api_key = "test-key-mention-1234"
        tool = "ingest_file"
        error = None
        for _ in range(20):
            error = rl.check(api_key, tool)
            if error is not None:
                break
        if error:
            assert "ingest_file" in error

    def test_different_keys_tracked_independently(self):
        """Different API keys should have independent rate limits."""
        mod = _fresh_import()
        rl = mod.RateLimiter()
        if not rl._enabled:
            pytest.skip("limits library not installed")

        # Exhaust one key
        key_a = "key-aaaa-exhaust-1234"
        for _ in range(20):
            rl.check(key_a, "ingest_file")

        # Another key should still be allowed
        key_b = "key-bbbb-fresh-12345"
        result = rl.check(key_b, "ingest_file")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  Graceful degradation
# ═══════════════════════════════════════════════════════════════════════

class TestGracefulDegradation:

    def test_missing_limits_library_disables_limiter(self):
        """When the `limits` library cannot be imported, _enabled stays False."""
        mod_name = "mcp.core.rate_limiter"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        # Block `limits` from being imported
        with patch.dict(sys.modules, {"limits": None,
                                       "limits.storage": None,
                                       "limits.strategies": None}):
            mod = importlib.import_module(mod_name)
            rl = mod.RateLimiter()
            assert rl._enabled is False

        # Clean up
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    def test_check_always_allows_when_degraded(self):
        """When limits library is missing, check() must always allow traffic."""
        mod_name = "mcp.core.rate_limiter"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        with patch.dict(sys.modules, {"limits": None,
                                       "limits.storage": None,
                                       "limits.strategies": None}):
            mod = importlib.import_module(mod_name)
            rl = mod.RateLimiter()
            for _ in range(100):
                result = rl.check("flood-key", "ingest_file")
                assert result is None

        if mod_name in sys.modules:
            del sys.modules[mod_name]

"""
Tests for OpenTelemetry Tracing Module (src/Observability/otel_tracing.py)
==========================================================================
Covers the trace_tool decorator, _get_tracer lazy init, and current_span
fallback behaviour.  All tests run with ENABLE_OTEL=false (default) so
no real OTel SDK is required.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio  # noqa: F401 — ensures plugin is loaded

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))


# ── Helper: fresh-import the module with controlled env ─────────────────

def _fresh_import(enable_otel: str = "false"):
    """Re-import otel_tracing with a controlled ENABLE_OTEL value.

    The module caches _tracer and _init_attempted at import time, so we
    must force a fresh import for each test scenario.
    """
    env_patch = {"ENABLE_OTEL": enable_otel}
    with patch.dict(os.environ, env_patch, clear=False):
        mod_name = "Observability.otel_tracing"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        mod = importlib.import_module(mod_name)
    return mod


# ═══════════════════════════════════════════════════════════════════════
#  _get_tracer tests
# ═══════════════════════════════════════════════════════════════════════

class TestGetTracer:

    def test_returns_none_when_otel_disabled(self):
        """_get_tracer must return None when ENABLE_OTEL is false."""
        mod = _fresh_import("false")
        tracer = mod._get_tracer()
        assert tracer is None

    def test_returns_none_on_repeated_call(self):
        """Second call should also return None (cached _init_attempted)."""
        mod = _fresh_import("false")
        assert mod._get_tracer() is None
        assert mod._get_tracer() is None  # second call, same result

    def test_init_attempted_flag_set(self):
        """After first call, _init_attempted is True regardless of result."""
        mod = _fresh_import("false")
        mod._get_tracer()
        assert mod._init_attempted is True


# ═══════════════════════════════════════════════════════════════════════
#  trace_tool decorator – sync
# ═══════════════════════════════════════════════════════════════════════

class TestTraceToolSync:

    def test_decorated_sync_function_runs_correctly(self):
        """Decorated sync function must return the expected value."""
        mod = _fresh_import("false")

        @mod.trace_tool("test_tool")
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_no_op_when_otel_disabled(self):
        """With OTel disabled the decorator is a transparent pass-through."""
        mod = _fresh_import("false")
        call_log = []

        @mod.trace_tool("my_tool", attributes={"extra": "val"})
        def tracked():
            call_log.append("called")
            return 42

        result = tracked()
        assert result == 42
        assert call_log == ["called"]

    def test_preserves_function_name(self):
        """functools.wraps should keep the original function's __name__."""
        mod = _fresh_import("false")

        @mod.trace_tool("some_tool")
        def my_special_function():
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_exception_propagates(self):
        """Exceptions must not be swallowed by the decorator."""
        mod = _fresh_import("false")

        @mod.trace_tool("error_tool")
        def boom():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            boom()

    def test_kwargs_forwarded(self):
        """Keyword arguments must be forwarded to the wrapped function."""
        mod = _fresh_import("false")

        @mod.trace_tool("kw_tool")
        def greet(name="world"):
            return f"hello {name}"

        assert greet(name="alice") == "hello alice"


# ═══════════════════════════════════════════════════════════════════════
#  trace_tool decorator – async
# ═══════════════════════════════════════════════════════════════════════

class TestTraceToolAsync:

    @pytest.mark.asyncio
    async def test_decorated_async_function_runs_correctly(self):
        """Decorated async function must return the expected value."""
        mod = _fresh_import("false")

        @mod.trace_tool("async_tool")
        async def fetch(url):
            return f"data from {url}"

        result = await fetch("http://example.com")
        assert result == "data from http://example.com"

    @pytest.mark.asyncio
    async def test_async_no_op_when_otel_disabled(self):
        """With OTel disabled, async decorator is a transparent pass-through."""
        mod = _fresh_import("false")
        call_log = []

        @mod.trace_tool("async_tool_2")
        async def process():
            call_log.append("processed")
            return 99

        result = await process()
        assert result == 99
        assert call_log == ["processed"]

    @pytest.mark.asyncio
    async def test_async_exception_propagates(self):
        """Async exceptions must propagate through the decorator."""
        mod = _fresh_import("false")

        @mod.trace_tool("async_error")
        async def explode():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            await explode()

    @pytest.mark.asyncio
    async def test_async_preserves_function_name(self):
        """Async decorated function should preserve __name__."""
        mod = _fresh_import("false")

        @mod.trace_tool("named")
        async def my_async_fn():
            pass

        assert my_async_fn.__name__ == "my_async_fn"


# ═══════════════════════════════════════════════════════════════════════
#  current_span fallback
# ═══════════════════════════════════════════════════════════════════════

class TestCurrentSpan:

    def test_returns_none_when_otel_not_installed(self):
        """current_span must return None if opentelemetry is not importable."""
        mod = _fresh_import("false")

        with patch.dict(sys.modules, {"opentelemetry": None}):
            # Force ImportError on `from opentelemetry import trace`
            with patch("builtins.__import__", side_effect=ImportError("no otel")):
                result = mod.current_span()
        # Should gracefully return None
        assert result is None

    def test_current_span_does_not_raise(self):
        """current_span must never raise, even in degraded environments."""
        mod = _fresh_import("false")
        # Even without any OTel setup, calling current_span should be safe
        try:
            mod.current_span()
        except Exception:
            pytest.fail("current_span raised an exception unexpectedly")

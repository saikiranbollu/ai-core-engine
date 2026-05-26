"""Validate da_name label is derived from API key principal_id."""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys

import pytest

# Make mcp/core importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcp"))


class TestResolveDaName:
    """Unit tests for _resolve_da_name()."""

    def test_known_key_returns_principal_id(self, tmp_path):
        """An API key in the registry resolves to its principal_id."""
        import yaml

        keys_file = tmp_path / "api_keys.yaml"
        keys_file.write_text(yaml.dump({
            "keys": {
                "test-key-001": {
                    "principal_id": "gest_assistant",
                    "roles": {"illd": ["public"]},
                },
            }
        }))

        from core.auth_middleware import reload_api_keys
        reload_api_keys(keys_file)

        from core.mcp_server import _resolve_da_name
        assert _resolve_da_name("test-key-001") == "gest_assistant"

    def test_unknown_key_returns_unknown(self, tmp_path):
        """An unknown API key resolves to 'unknown'."""
        import yaml

        keys_file = tmp_path / "api_keys.yaml"
        keys_file.write_text(yaml.dump({"keys": {}}))

        from core.auth_middleware import reload_api_keys
        reload_api_keys(keys_file)

        from core.mcp_server import _resolve_da_name
        assert _resolve_da_name("nonexistent-key") == "unknown"


class TestDaNameContextVar:
    """Verify _current_da_name is async-safe across concurrent tasks."""

    def test_concurrent_tasks_isolated(self, tmp_path):
        """Each async task gets its own _current_da_name value."""
        import yaml

        keys_file = tmp_path / "api_keys.yaml"
        keys_file.write_text(yaml.dump({
            "keys": {
                "key-a": {"principal_id": "da_alpha", "roles": {"illd": ["public"]}},
                "key-b": {"principal_id": "da_beta", "roles": {"illd": ["public"]}},
            }
        }))

        from core.auth_middleware import reload_api_keys
        reload_api_keys(keys_file)

        from core.mcp_server import _current_da_name, _resolve_da_name

        results = {}

        async def simulate_request(key: str, label: str):
            _current_da_name.set(_resolve_da_name(key))
            # Yield control to simulate interleaving
            await asyncio.sleep(0.01)
            results[label] = _current_da_name.get("unknown")

        async def run():
            ctx_a = contextvars.copy_context()
            ctx_b = contextvars.copy_context()
            task_a = asyncio.ensure_future(ctx_a.run(simulate_request, "key-a", "a"))
            task_b = asyncio.ensure_future(ctx_b.run(simulate_request, "key-b", "b"))
            await asyncio.gather(task_a, task_b)

        asyncio.run(run())

        assert results["a"] == "da_alpha"
        assert results["b"] == "da_beta"


class TestMetricsDaNameLabel:
    """Verify TOOL_REQUESTS_TOTAL and TOOL_REQUEST_DURATION include da_name."""

    def test_metrics_have_da_name_label(self):
        """Tool metrics must include 'da_name' in their label names."""
        os.environ["ENABLE_METRICS"] = "true"
        # Force re-import to pick up the flag
        if "src.Observability.metrics" in sys.modules:
            del sys.modules["src.Observability.metrics"]

        try:
            from src.Observability.metrics import PROMETHEUS_AVAILABLE
            if not PROMETHEUS_AVAILABLE:
                pytest.skip("prometheus_client not installed")

            from src.Observability.metrics import TOOL_REQUESTS_TOTAL, TOOL_REQUEST_DURATION
            assert "da_name" in TOOL_REQUESTS_TOTAL._labelnames
            assert "da_name" in TOOL_REQUEST_DURATION._labelnames
        finally:
            os.environ.pop("ENABLE_METRICS", None)

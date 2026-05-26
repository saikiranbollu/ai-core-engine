"""Validate MAX_DEPENDENCIES_DEPTH env var configures query_dependencies default."""
import importlib
import os


def test_query_dependencies_respects_env_var(monkeypatch):
    """MAX_DEPENDENCIES_DEPTH env var overrides the default max_depth."""
    monkeypatch.setenv("MAX_DEPENDENCIES_DEPTH", "5")
    import mcp.core.mcp_server as mod
    importlib.reload(mod)
    assert mod._DEFAULT_MAX_DEPTH == 5


def test_query_dependencies_default_is_3_without_env(monkeypatch):
    """Without env var, default max_depth is 3."""
    monkeypatch.delenv("MAX_DEPENDENCIES_DEPTH", raising=False)
    import mcp.core.mcp_server as mod
    importlib.reload(mod)
    assert mod._DEFAULT_MAX_DEPTH == 3

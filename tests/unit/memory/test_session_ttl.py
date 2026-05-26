"""Validate session_start enforces server-side TTL from SESSION_TTL_SECONDS."""
import importlib
import os


def test_session_ttl_respects_env_var(monkeypatch):
    """SESSION_TTL_SECONDS env var overrides the default TTL."""
    monkeypatch.setenv("SESSION_TTL_SECONDS", "7200")
    import mcp.core.mcp_server as mod
    importlib.reload(mod)
    assert mod._SESSION_TTL_SECONDS == 7200


def test_session_ttl_default_is_3600(monkeypatch):
    """Without env var, default TTL is 3600s."""
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    import mcp.core.mcp_server as mod
    importlib.reload(mod)
    assert mod._SESSION_TTL_SECONDS == 3600

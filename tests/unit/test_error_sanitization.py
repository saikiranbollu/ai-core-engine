"""Validate error messages don't leak internal details in production."""
import json
import os

# Ensure sanitization is on for these tests
os.environ["SANITIZE_ERRORS"] = "true"


def test_internal_error_is_sanitized():
    """INTERNAL_ERROR should not expose raw exception message."""
    import importlib
    import mcp.core.mcp_server as mod
    importlib.reload(mod)
    from mcp.core.mcp_server import _err

    result = json.loads(_err(
        "INTERNAL_ERROR",
        "Connection refused: bolt://neo4j-legato:7687",
        _raw_exception=ConnectionError("bolt://neo4j-legato:7687"),
    ))

    assert "neo4j" not in result["message"]
    assert "bolt://" not in result["message"]
    assert "correlation_id" in result
    assert result["message"].startswith("Internal error occurred")


def test_permission_denied_not_sanitized():
    """PERMISSION_DENIED is safe to surface as-is."""
    from mcp.core.mcp_server import _err

    result = json.loads(_err("PERMISSION_DENIED", "Tool 'cache_clear' requires admin role"))

    assert "cache_clear" in result["message"]
    assert "admin" in result["message"]


def test_correlation_id_present():
    """All errors include a correlation ID for log tracing."""
    from mcp.core.mcp_server import _err

    result = json.loads(_err("INTERNAL_ERROR", "something broke"))
    assert "correlation_id" in result
    assert len(result["correlation_id"]) >= 8


SENSITIVE_PATTERNS = [
    "bolt://", "redis://", "/app/", "/usr/local/",
    "neo4j-legato", "qdrant-legato", "Traceback",
    "File \"/", ".py\", line",
]


import pytest


@pytest.mark.parametrize("pattern", SENSITIVE_PATTERNS)
def test_no_sensitive_patterns_in_sanitized_error(pattern):
    """Sanitized errors must not contain infrastructure details."""
    from mcp.core.mcp_server import _err

    result = json.loads(_err("INTERNAL_ERROR", f"Error: {pattern} connection failed"))
    assert pattern not in result["message"]


def test_unsanitized_when_disabled(monkeypatch):
    """With SANITIZE_ERRORS=false, full message is returned."""
    import importlib
    monkeypatch.setenv("SANITIZE_ERRORS", "false")
    import mcp.core.mcp_server as mod
    importlib.reload(mod)

    result = json.loads(mod._err("INTERNAL_ERROR", "bolt://neo4j:7687 refused"))
    assert "bolt://neo4j:7687" in result["message"]

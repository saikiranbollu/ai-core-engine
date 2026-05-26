"""Validate enriched Cerbos resource attributes."""
import pytest


def test_safety_critical_classification():
    """Safety modules get 'safety-critical' classification."""
    from mcp.core.auth_middleware import _get_data_classification

    assert _get_data_classification("adc") == "safety-critical"
    assert _get_data_classification("dio") == "safety-critical"
    assert _get_data_classification("port") == "safety-critical"
    assert _get_data_classification("scu") == "safety-critical"
    assert _get_data_classification("clocksc") == "safety-critical"
    assert _get_data_classification("lin") == "general"
    assert _get_data_classification(None) == "general"


def test_api_key_classification_defaults_to_external():
    """Unknown API keys default to 'external' classification."""
    from mcp.core.auth_middleware import _classify_api_key

    assert _classify_api_key("unknown-key", {}) == "external"


def test_api_key_classification_reads_registry():
    """API key classification is read from registry entry."""
    from mcp.core.auth_middleware import _classify_api_key

    registry = {
        "internal-key": {"classification": "internal"},
        "partner-key": {"classification": "partner"},
    }
    assert _classify_api_key("internal-key", registry) == "internal"
    assert _classify_api_key("partner-key", registry) == "partner"
    assert _classify_api_key("other-key", registry) == "external"


def test_check_authorization_passes_module_name():
    """check_authorization accepts module_name parameter without error."""
    from mcp.core.auth_middleware import check_authorization

    # With unknown API key, returns denied — but the signature works
    allowed, msg = check_authorization(
        api_key="nonexistent-key",
        tool_name="search_database",
        workspace_id="illd",
        module_name="adc",
    )
    assert not allowed
    assert "Unknown" in msg or "missing" in msg

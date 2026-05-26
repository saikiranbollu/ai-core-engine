"""Tests for authorization middleware and tool-tier enforcement.

Covers:
- API key resolution and principal building
- Workspace-scoped role assignment
- Cerbos allow/deny for each tier (public / developer / admin)
- extract_workspace_id with various parameter patterns
- Missing / invalid / unknown API keys
- Tool tier mapping completeness
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make mcp/core importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp"))

from core.tool_tiers import (
    ADMIN,
    DEVELOPER,
    PUBLIC,
    TIER_HIERARCHY,
    TOOL_TIERS,
    get_tool_tier,
    role_may_invoke,
)
from core.auth_middleware import (
    extract_workspace_id,
    load_api_keys,
    reload_api_keys,
    resolve_principal,
    check_authorization,
    _err_permission_denied,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_REGISTRY = textwrap.dedent("""\
    keys:
      "key-admin-global":
        principal_id: "admin-pipeline"
        roles:
          "*": ["admin"]
      "key-dev-illd":
        principal_id: "saga-assistant"
        roles:
          illd: ["developer"]
          mcal: ["public"]
      "key-public-only":
        principal_id: "gest-assistant"
        roles:
          illd: ["public"]
      "key-no-roles":
        principal_id: "orphan-assistant"
        roles: {}
""")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the cached API key registry and Cerbos client between tests."""
    import core.auth.api_key_registry as reg_mod
    import core.auth.cerbos_client as cerb_mod
    from core.config import get_settings
    get_settings.cache_clear()
    reg_mod._api_key_registry = None
    cerb_mod._cerbos_client = None
    yield
    reg_mod._api_key_registry = None
    cerb_mod._cerbos_client = None
    get_settings.cache_clear()


@pytest.fixture()
def registry_path(tmp_path: Path) -> Path:
    """Write the sample API key registry to a temp file and return its path."""
    p = tmp_path / "api_keys.yaml"
    p.write_text(SAMPLE_REGISTRY, encoding="utf-8")
    return p


# =====================================================================
#  TOOL TIERS
# =====================================================================


class TestToolTiers:
    """Verify the TOOL_TIERS mapping and helper functions."""

    def test_all_56_tools_mapped(self):
        assert len(TOOL_TIERS) == 58

    def test_tiers_are_valid(self):
        valid = {PUBLIC, DEVELOPER, ADMIN}
        for tool, tier in TOOL_TIERS.items():
            assert tier in valid, f"Tool '{tool}' has invalid tier '{tier}'"

    def test_get_tool_tier_known(self):
        assert get_tool_tier("search_database") == PUBLIC
        assert get_tool_tier("execute_cypher") == DEVELOPER
        assert get_tool_tier("ingest_file") == ADMIN

    def test_get_tool_tier_unknown(self):
        assert get_tool_tier("nonexistent_tool") is None

    @pytest.mark.parametrize(
        "role,tool,expected",
        [
            ("public", "search_database", True),   # public tool, public role
            ("developer", "search_database", True), # public tool, higher role
            ("admin", "search_database", True),     # public tool, admin role
            ("public", "execute_cypher", False),            # dev tool, public role
            ("developer", "execute_cypher", True),          # dev tool, dev role
            ("admin", "execute_cypher", True),              # dev tool, admin role
            ("public", "ingest_file", False),               # admin tool, public role
            ("developer", "ingest_file", False),            # admin tool, dev role
            ("admin", "ingest_file", True),                 # admin tool, admin role
        ],
    )
    def test_role_may_invoke(self, role, tool, expected):
        assert role_may_invoke(role, tool) == expected

    def test_role_may_invoke_unknown_tool(self):
        assert role_may_invoke("admin", "does_not_exist") is False

    def test_hierarchy_completeness(self):
        assert TIER_HIERARCHY[ADMIN] == {PUBLIC, DEVELOPER, ADMIN}
        assert TIER_HIERARCHY[DEVELOPER] == {PUBLIC, DEVELOPER}
        assert TIER_HIERARCHY[PUBLIC] == {PUBLIC}


# =====================================================================
#  API KEY LOADING
# =====================================================================


class TestAPIKeyLoading:
    """Verify load_api_keys and reload_api_keys."""

    def test_load_from_path(self, registry_path: Path):
        keys = load_api_keys(registry_path)
        assert "key-admin-global" in keys
        assert keys["key-admin-global"]["principal_id"] == "admin-pipeline"

    def test_load_returns_cached(self, registry_path: Path):
        first = load_api_keys(registry_path)
        second = load_api_keys(registry_path)
        assert first is second  # same dict object

    def test_reload_forces_fresh(self, registry_path: Path):
        first = load_api_keys(registry_path)
        reloaded = reload_api_keys(registry_path)
        assert first is not reloaded
        assert reloaded == first  # same content, different object

    def test_missing_file_returns_empty(self, tmp_path: Path):
        keys = load_api_keys(tmp_path / "no_such_file.yaml")
        assert keys == {}

    def test_load_via_env_var(self, registry_path: Path, monkeypatch):
        monkeypatch.setenv("API_KEY_REGISTRY_PATH", str(registry_path))
        keys = load_api_keys()
        assert "key-dev-illd" in keys


# =====================================================================
#  PRINCIPAL RESOLUTION
# =====================================================================


class TestResolvePrincipal:
    """Verify resolve_principal with workspace-scoped roles."""

    def test_admin_global_wildcard(self, registry_path: Path):
        load_api_keys(registry_path)
        p = resolve_principal("key-admin-global", workspace_id="illd")
        assert p is not None
        assert p.id == "admin-pipeline"
        assert "admin" in p.roles

    def test_admin_global_any_workspace(self, registry_path: Path):
        load_api_keys(registry_path)
        p = resolve_principal("key-admin-global", workspace_id="anything")
        assert p is not None
        assert "admin" in p.roles

    def test_developer_illd_workspace(self, registry_path: Path):
        load_api_keys(registry_path)
        p = resolve_principal("key-dev-illd", workspace_id="illd")
        assert p is not None
        assert "developer" in p.roles

    def test_developer_mcal_workspace_demoted(self, registry_path: Path):
        """Developer on illd should only be public on mcal."""
        load_api_keys(registry_path)
        p = resolve_principal("key-dev-illd", workspace_id="mcal")
        assert p is not None
        assert "public" in p.roles
        assert "developer" not in p.roles

    def test_unknown_key(self, registry_path: Path):
        load_api_keys(registry_path)
        p = resolve_principal("totally-invalid-key")
        assert p is None

    def test_no_roles_for_workspace(self, registry_path: Path):
        """Key with empty roles dict should get _none placeholder."""
        load_api_keys(registry_path)
        p = resolve_principal("key-no-roles", workspace_id="illd")
        assert p is not None
        assert "_none" in p.roles


# =====================================================================
#  extract_workspace_id
# =====================================================================


class TestExtractWorkspaceId:
    """Verify extract_workspace_id handles all parameter patterns."""

    def test_workspace_id_param(self):
        assert extract_workspace_id(workspace_id="mcal") == "mcal"

    def test_profile_param(self):
        assert extract_workspace_id(profile="mcal") == "mcal"

    def test_workspace_id_takes_precedence(self):
        assert extract_workspace_id(workspace_id="illd", profile="mcal") == "illd"

    def test_default_fallback(self):
        assert extract_workspace_id() == "illd"

    def test_empty_string_falls_through(self):
        assert extract_workspace_id(workspace_id="", profile="mcal") == "mcal"


# =====================================================================
#  CERBOS AUTHORIZATION CHECK (mocked)
# =====================================================================


class TestCheckAuthorization:
    """Test check_authorization with mocked Cerbos client."""

    def test_unknown_key_denied(self, registry_path: Path):
        load_api_keys(registry_path)
        allowed, msg = check_authorization("bad-key", "search_database")
        assert not allowed
        assert "Unknown" in msg

    def test_unknown_tool_denied(self, registry_path: Path):
        load_api_keys(registry_path)
        allowed, msg = check_authorization("key-admin-global", "nonexistent_tool")
        assert not allowed
        assert "Unknown tool" in msg

    @patch("core.auth.cerbos_client.CerbosClient")
    @patch("core.auth.cerbos_client._CERBOS_SDK_AVAILABLE", True)
    def test_cerbos_allows(self, mock_cls, registry_path: Path):
        load_api_keys(registry_path)
        mock_client = MagicMock()
        mock_client.is_allowed.return_value = True
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = mock_client

        allowed, msg = check_authorization("key-admin-global", "ingest_file", "illd")
        assert allowed
        assert msg == "allowed"

    @patch("core.auth.cerbos_client.CerbosClient")
    @patch("core.auth.cerbos_client._CERBOS_SDK_AVAILABLE", True)
    def test_cerbos_denies(self, mock_cls, registry_path: Path):
        load_api_keys(registry_path)
        mock_client = MagicMock()
        mock_client.is_allowed.return_value = False
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = mock_client

        allowed, msg = check_authorization("key-public-only", "ingest_file", "illd")
        assert not allowed
        assert "Insufficient" in msg

    @patch("core.auth.cerbos_client.CerbosClient")
    @patch("core.auth.cerbos_client._CERBOS_SDK_AVAILABLE", True)
    def test_cerbos_connection_failure(self, mock_cls, registry_path: Path):
        """When Cerbos PDP is unreachable, fall back to local tier enforcement."""
        load_api_keys(registry_path)
        mock_cls.side_effect = ConnectionError("Connection refused")

        allowed, msg = check_authorization("key-admin-global", "health_check")
        assert allowed
        assert msg == "allowed"

    @patch("core.auth.cerbos_client.CerbosClient")
    @patch("core.auth.cerbos_client._CERBOS_SDK_AVAILABLE", True)
    def test_workspace_scoped_deny(self, mock_cls, registry_path: Path):
        """Developer on illd (public on mcal) denied a dev tool on mcal."""
        load_api_keys(registry_path)
        mock_client = MagicMock()
        mock_client.is_allowed.return_value = False
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = mock_client

        allowed, msg = check_authorization("key-dev-illd", "execute_cypher", "mcal")
        assert not allowed


# =====================================================================
#  ERROR ENVELOPE
# =====================================================================


class TestErrorEnvelope:

    def test_permission_denied_format(self):
        result = _err_permission_denied("no access")
        parsed = json.loads(result)
        assert parsed["error"] is True
        assert parsed["error_code"] == "PERMISSION_DENIED"
        assert "no access" in parsed["message"]

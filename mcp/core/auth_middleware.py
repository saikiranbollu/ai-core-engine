"""
Authorization Middleware for AI Core Engine MCP Server
======================================================

MEG_SW-289: Decomposed into mcp/core/auth/ package.
This module is a thin backward-compatibility facade — all logic lives in:
  - mcp.core.auth.api_key_registry  (YAML loading + key lookup)
  - mcp.core.auth.principal          (API key → Cerbos Principal)
  - mcp.core.auth.cerbos_client      (Cerbos PDP singleton + auth check)
  - mcp.core.auth.local_fallback     (local tier-based authorization)
"""
from __future__ import annotations

import json
from typing import Any

# Re-export everything existing consumers need
from .auth.api_key_registry import load_api_keys, reload_api_keys
from .auth.cerbos_client import (
    check_authorization,
    _classify_api_key,
    _get_data_classification,
    _get_cerbos_url,
    _get_cerbos_client,
    _CERBOS_SDK_AVAILABLE,
)
from .auth.local_fallback import check_via_local_tiers as _check_via_local_tiers
from .auth.principal import resolve_principal, FallbackPrincipal as _FallbackPrincipal

# Re-export Cerbos SDK types for test patching
try:
    from cerbos.sdk.client import CerbosClient
    from cerbos.sdk.model import Principal, Resource
except ImportError:
    CerbosClient = None  # type: ignore[assignment,misc]
    Principal = None      # type: ignore[assignment,misc]
    Resource = None       # type: ignore[assignment,misc]


def extract_workspace_id(**kwargs: Any) -> str:
    """Extract workspace_id from tool kwargs (handles both 'workspace_id' and 'profile')."""
    return kwargs.get("workspace_id") or kwargs.get("profile") or "illd"


def _err_permission_denied(message: str) -> str:
    """Return the standard PERMISSION_DENIED error envelope (code E015)."""
    return json.dumps({
        "error": True,
        "error_code": "PERMISSION_DENIED",
        "message": message,
    })

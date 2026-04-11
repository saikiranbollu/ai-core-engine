"""
Authorization Middleware for AI Core Engine MCP Server
======================================================

Integrates Cerbos PDP to enforce the 3-tier access model
(public / developer / admin) with per-workspace role scoping.

Flow:
    1. Extract API key from MCP request metadata.
    2. Resolve API key → Cerbos principal (with workspace-scoped roles).
    3. Call Cerbos PDP — is_allowed(principal, resource=tool, action=invoke).
    4. Allow → proceed.  Deny → return PERMISSION_DENIED (E015).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from .tool_tiers import TOOL_TIERS, get_tool_tier

logger = logging.getLogger("aice_mcp.auth")

# ---------------------------------------------------------------------------
# Cerbos SDK — lazy import so the server starts even without the package
# ---------------------------------------------------------------------------
try:
    from cerbos.sdk.client import CerbosClient
    from cerbos.sdk.model import Principal, Resource
    _CERBOS_SDK_AVAILABLE = True
except ImportError:
    CerbosClient = None  # type: ignore[assignment,misc]
    Principal = None      # type: ignore[assignment,misc]
    Resource = None       # type: ignore[assignment,misc]
    _CERBOS_SDK_AVAILABLE = False
    logger.warning("cerbos SDK not installed — Cerbos PDP checks disabled, "
                    "falling back to local tier hierarchy")


# ---------------------------------------------------------------------------
# API Key Registry
# ---------------------------------------------------------------------------

_api_key_registry: Dict[str, dict] | None = None


@dataclass
class _FallbackPrincipal:
    id: str
    roles: set[str]
    attr: Dict[str, Any]


def load_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Load the API-key → principal registry from a YAML file.

    File format::

        keys:
          "<api-key>":
            principal_id: "<id>"
            roles:
              illd: ["public"]
              mcal: ["public", "developer"]
    """
    global _api_key_registry
    if _api_key_registry is not None:
        return _api_key_registry

    if path is None:
        path = os.environ.get(
            "API_KEY_REGISTRY_PATH",
            str(Path(__file__).resolve().parent.parent / "auth" / "api_keys.yaml"),
        )
    path = Path(path)
    if not path.is_file():
        logger.warning("API key registry not found at %s — all requests will be denied", path)
        _api_key_registry = {}
        return _api_key_registry

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    _api_key_registry = data.get("keys", {})
    logger.info("Loaded %d API keys from %s", len(_api_key_registry), path)
    return _api_key_registry


def reload_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Force-reload the API key registry (e.g. after rotation)."""
    global _api_key_registry
    _api_key_registry = None
    return load_api_keys(path)


# ---------------------------------------------------------------------------
# Principal Resolution
# ---------------------------------------------------------------------------

def resolve_principal(
    api_key: str,
    workspace_id: str = "illd",
) -> "Principal | None":
    """Look up *api_key* in the registry and build a Cerbos ``Principal``.

    The principal's roles are scoped to the requested *workspace_id*.
    If the key is unknown, returns ``None``.
    """
    registry = load_api_keys()
    entry = registry.get(api_key)
    if entry is None:
        return None

    principal_id = entry.get("principal_id", "unknown")
    workspace_roles: dict = entry.get("roles", {})

    # Resolve roles for the requested workspace (fall back to wildcard)
    roles = workspace_roles.get(workspace_id, [])
    if not roles:
        roles = workspace_roles.get("*", [])

    # Cerbos requires at least one role — use a placeholder that
    # will match nothing in the policies.
    if not roles:
        roles = ["_none"]

    if _CERBOS_SDK_AVAILABLE:
        return Principal(
            id=principal_id,
            roles=set(roles),
            attr={
                "workspace_id": workspace_id,
                "api_key_hash": api_key[:8] + "…",  # partial for audit logs
            },
        )

    # SDK not available — return a lightweight dict stand-in
    return _FallbackPrincipal(
        id=principal_id,
        roles=set(roles),
        attr={"workspace_id": workspace_id, "api_key_hash": api_key[:8] + "…"},
    )


# ---------------------------------------------------------------------------
# Cerbos Client (reused across requests to avoid per-request TCP overhead)
# ---------------------------------------------------------------------------

_cerbos_client = None


def _get_cerbos_url() -> str:
    """Build the Cerbos HTTP base URL."""
    host = os.environ.get("CERBOS_HOST", "localhost")
    port = os.environ.get("CERBOS_HTTP_PORT", "3592")
    return f"http://{host}:{port}"


def _get_cerbos_client():
    """Return a reusable CerbosClient (created once per process)."""
    global _cerbos_client
    if _cerbos_client is None and _CERBOS_SDK_AVAILABLE:
        try:
            _cerbos_client = CerbosClient(host=_get_cerbos_url())
        except Exception as exc:
            logger.warning("Failed to create reusable CerbosClient: %s", exc)
    return _cerbos_client


# ---------------------------------------------------------------------------
# Authorization Check — main entry point
# ---------------------------------------------------------------------------

def check_authorization(
    api_key: str,
    tool_name: str,
    workspace_id: str = "illd",
) -> tuple[bool, str]:
    """Check whether *api_key* may invoke *tool_name* in *workspace_id*.

    Returns:
        (allowed: bool, message: str)
    """
    # 1. Resolve principal
    principal = resolve_principal(api_key, workspace_id)
    if principal is None:
        return False, "Unknown or missing API key"

    # 2. Determine tool tier — unknown tools are DENIED (strict)
    tier = get_tool_tier(tool_name)
    if tier is None:
        return False, f"Unknown tool: {tool_name}"

    # 3. Try Cerbos PDP if SDK is available
    if _CERBOS_SDK_AVAILABLE:
        resource = Resource(
            id=tool_name,
            kind="mcp_tool",
            attr={
                "tool_name": tool_name,
                "tier": tier,
                "workspace_id": workspace_id,
            },
        )
        try:
            client = _get_cerbos_client()
            if client is None:
                # Fallback: one-off client if reusable creation failed
                with CerbosClient(host=_get_cerbos_url()) as client:
                    resp = client.is_allowed("invoke", principal, resource)
            else:
                resp = client.is_allowed("invoke", principal, resource)
            if resp:
                logger.debug(
                    "ALLOW  principal=%s tool=%s workspace=%s",
                    principal.id, tool_name, workspace_id,
                )
                return True, "allowed"
            else:
                msg = (
                    f"Insufficient access tier for tool '{tool_name}'. "
                    f"Required: {tier}, your roles: {sorted(principal.roles)}"
                )
                logger.warning(
                    "DENY   principal=%s tool=%s workspace=%s — %s",
                    principal.id, tool_name, workspace_id, msg,
                )
                return False, msg
        except Exception as exc:
            logger.exception("Cerbos check failed for tool=%s — falling back to local tier check", tool_name)
            # Fall through to local tier check below

    # 4. Fallback: local tier hierarchy check
    return _check_via_local_tiers(principal, tool_name, tier)


def _check_via_local_tiers(principal: Any, tool_name: str, tier: str) -> tuple[bool, str]:
    """Local tier hierarchy check when Cerbos PDP is unreachable or unavailable."""
    from .tool_tiers import TIER_HIERARCHY
    roles = principal.roles if hasattr(principal, "roles") else principal.get("roles", set())
    for role in roles:
        allowed_tiers = TIER_HIERARCHY.get(role, set())
        if tier in allowed_tiers:
            return True, "allowed"
    p_id = principal.id if hasattr(principal, "id") else principal.get("id", "unknown")
    msg = (f"Insufficient access tier for tool '{tool_name}'. "
           f"Required: {tier}, your roles: {sorted(roles)}")
    logger.warning("DENY   principal=%s tool=%s — %s", p_id, tool_name, msg)
    return False, msg


# ---------------------------------------------------------------------------
# Convenience helpers for MCP tool handlers
# ---------------------------------------------------------------------------

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

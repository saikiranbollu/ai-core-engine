"""Cerbos PDP Client — singleton client + authorization check."""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..config import get_settings as _get_settings
from ..tool_tiers import get_tool_tier
from .api_key_registry import load_api_keys
from .local_fallback import check_via_local_tiers
from .principal import resolve_principal

logger = logging.getLogger("aice_mcp.auth")

# Cerbos SDK — lazy import
try:
    from cerbos.sdk.client import CerbosClient
    from cerbos.sdk.model import Resource
    _CERBOS_SDK_AVAILABLE = True
except ImportError:
    CerbosClient = None  # type: ignore[assignment,misc]
    Resource = None       # type: ignore[assignment,misc]
    _CERBOS_SDK_AVAILABLE = False

_cerbos_client = None

_SAFETY_MODULES = {"adc", "dio", "port", "scu", "clocksc"}


def _get_cerbos_url() -> str:
    """Build the Cerbos HTTP base URL."""
    _s = _get_settings()
    return f"http://{_s.cerbos_host}:{_s.cerbos_http_port}"


def _get_cerbos_client():
    """Return a reusable CerbosClient (created once per process)."""
    global _cerbos_client
    if _cerbos_client is None and _CERBOS_SDK_AVAILABLE:
        try:
            _cerbos_client = CerbosClient(host=_get_cerbos_url())
        except Exception as exc:
            logger.warning("Failed to create reusable CerbosClient: %s", exc)
    return _cerbos_client


def _classify_api_key(api_key: str, registry: dict) -> str:
    """Classify API key as 'internal', 'partner', or 'external'."""
    entry = registry.get(api_key, {})
    return entry.get("classification", "external")


def _get_data_classification(module_name: str | None) -> str:
    """Return data classification for a module."""
    if module_name and module_name.lower() in _SAFETY_MODULES:
        return "safety-critical"
    return "general"


def check_authorization(
    api_key: str,
    tool_name: str,
    workspace_id: str = "illd",
    module_name: str | None = None,
) -> tuple[bool, str]:
    """Check whether *api_key* may invoke *tool_name* in *workspace_id*.

    Returns:
        (allowed: bool, message: str)
    """
    principal = resolve_principal(api_key, workspace_id)
    if principal is None:
        return False, "Unknown or missing API key"

    tier = get_tool_tier(tool_name)
    if tier is None:
        return False, f"Unknown tool: {tool_name}"

    if _CERBOS_SDK_AVAILABLE:
        registry = load_api_keys()
        resource = Resource(
            id=tool_name,
            kind="mcp_tool",
            attr={
                "tool_name": tool_name,
                "tier": tier,
                "workspace_id": workspace_id,
                "api_key_class": _classify_api_key(api_key, registry),
                "data_classification": _get_data_classification(module_name),
                "module_scope": module_name or "",
            },
        )
        try:
            client = _get_cerbos_client()
            if client is None:
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
        except Exception:
            logger.exception(
                "Cerbos check failed for tool=%s — falling back to local tier check",
                tool_name,
            )

    return check_via_local_tiers(principal, tool_name, tier)

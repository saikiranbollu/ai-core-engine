"""Principal Resolution — maps API key to Cerbos Principal."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .api_key_registry import load_api_keys

logger = logging.getLogger("aice_mcp.auth")

# Cerbos SDK — lazy import
try:
    from cerbos.sdk.model import Principal
    _CERBOS_SDK_AVAILABLE = True
except ImportError:
    Principal = None  # type: ignore[assignment,misc]
    _CERBOS_SDK_AVAILABLE = False


@dataclass
class FallbackPrincipal:
    """Lightweight stand-in when Cerbos SDK is not installed."""
    id: str
    roles: set[str]
    attr: Dict[str, Any]


def resolve_principal(
    api_key: str,
    workspace_id: str = "illd",
) -> "Principal | FallbackPrincipal | None":
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

    if not roles:
        roles = ["_none"]

    attr = {
        "workspace_id": workspace_id,
        "api_key_hash": api_key[:8] + "…",
    }

    if _CERBOS_SDK_AVAILABLE:
        return Principal(id=principal_id, roles=set(roles), attr=attr)

    return FallbackPrincipal(id=principal_id, roles=set(roles), attr=attr)

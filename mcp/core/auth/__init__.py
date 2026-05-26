"""
Auth Package — MEG_SW-289
==========================
Re-exports for backward compatibility with existing imports from auth_middleware.
"""
from __future__ import annotations

from .api_key_registry import load_api_keys, reload_api_keys
from .cerbos_client import check_authorization
from .local_fallback import check_via_local_tiers
from .principal import resolve_principal

__all__ = [
    "check_authorization",
    "load_api_keys",
    "reload_api_keys",
    "resolve_principal",
    "check_via_local_tiers",
]

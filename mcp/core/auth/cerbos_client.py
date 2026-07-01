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

# F-CA-A04: bound Cerbos PDP calls so a hung PDP cannot block tool dispatch.
_CERBOS_TIMEOUT_S = 1.0

# F-CA-A03: dedicated audit PostgresClient. Built lazily and owned by auth so we
# never import the MCP server module (which would re-import the whole server as a
# separate module instance and re-run registration/init).
_audit_pg = None


def _set_cerbos_up(up: bool) -> None:
    """Update the aice_cerbos_up gauge (best-effort)."""
    try:
        from src.Observability.metrics import CERBOS_UP
        CERBOS_UP.set(1 if up else 0)
    except Exception:
        pass


def _get_audit_pg():
    """Return a process-wide PostgresClient for DENY audit rows (best-effort)."""
    global _audit_pg
    if _audit_pg is None:
        try:
            from src.Observability.postgres_schema import PostgresClient
            _audit_pg = PostgresClient()
            # Ensure audit_logs exists so DENY inserts don't silently fail on a
            # fresh DB / auth-only path (mirrors the server's init_schema()).
            if _audit_pg.available:
                _audit_pg.init_schema()
        except Exception:
            logger.debug("Audit PostgresClient unavailable", exc_info=True)
            return None
    return _audit_pg


def _hash_api_key(api_key: str) -> str:
    """Hash an API key for audit correlation (matches server audit convention)."""
    import hashlib
    return "sha256:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _audit_deny(tool_name: str, workspace_id: str, api_key: str, msg: str) -> None:
    """F-CA-A03: persist a DENY decision to PostgreSQL audit_logs (ASPICE SUP.10)."""
    try:
        pg = _get_audit_pg()
        if pg and pg.available:
            pg.log_audit(
                tool_name=tool_name,
                workspace_id=workspace_id,
                caller_api_key=_hash_api_key(api_key) if api_key else "anonymous",
                response_code="denied",
                parameters={"reason": msg},
            )
    except Exception:
        logger.debug("Could not persist DENY audit row", exc_info=True)


def _get_cerbos_url() -> str:
    """Build the Cerbos HTTP base URL."""
    _s = _get_settings()
    return f"http://{_s.cerbos_host}:{_s.cerbos_http_port}"


def _get_cerbos_client():
    """Return a reusable CerbosClient (created once per process)."""
    global _cerbos_client
    if _cerbos_client is None and _CERBOS_SDK_AVAILABLE:
        try:
            _cerbos_client = CerbosClient(host=_get_cerbos_url(), timeout_secs=_CERBOS_TIMEOUT_S)
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
                with CerbosClient(host=_get_cerbos_url(), timeout_secs=_CERBOS_TIMEOUT_S) as client:
                    resp = client.is_allowed("invoke", principal, resource)
            else:
                resp = client.is_allowed("invoke", principal, resource)
            _set_cerbos_up(True)
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
            # F-CA-A04: PDP unreachable/timed out — mark down, drop the cached
            # client so the next call reconnects, then fall back to local tiers.
            global _cerbos_client
            _cerbos_client = None
            _set_cerbos_up(False)
            logger.exception(
                "Cerbos check failed for tool=%s — falling back to local tier check",
                tool_name,
            )

    allowed, message = check_via_local_tiers(principal, tool_name, tier)
    return allowed, message

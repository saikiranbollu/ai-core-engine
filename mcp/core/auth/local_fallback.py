"""Local Tier Fallback — when Cerbos PDP is unreachable or unavailable."""
from __future__ import annotations

import logging
from typing import Any

from ..tool_tiers import TIER_HIERARCHY

logger = logging.getLogger("aice_mcp.auth")


def check_via_local_tiers(principal: Any, tool_name: str, tier: str) -> tuple[bool, str]:
    """Local tier hierarchy check when Cerbos PDP is unreachable."""
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

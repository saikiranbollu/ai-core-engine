"""Validate Cerbos policy YAML stays in sync with TOOL_TIERS."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

POLICY_PATH = Path(__file__).resolve().parents[3] / "mcp" / "auth" / "policies" / "resource_mcp_tool.yaml"

# Make mcp/core importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp"))


def _tier_from_roles(roles: list[str]) -> str | None:
    """Infer the tier name from the roles list on a Cerbos rule."""
    role_set = set(roles)
    if role_set == {"public", "developer", "admin"}:
        return "public"
    if role_set == {"developer", "admin"}:
        return "developer"
    if role_set == {"admin"}:
        return "admin"
    return None


# Regex to extract tool name from: request.resource.attr.tool_name == 'xxx'
_TOOL_RE = re.compile(r"request\.resource\.attr\.tool_name\s*==\s*'([^']+)'")


def _load_policy_tools_by_tier() -> dict[str, set[str]]:
    """Parse resource_mcp_tool.yaml and extract tool names per rule/tier."""
    with open(POLICY_PATH) as f:
        doc = yaml.safe_load(f)

    result: dict[str, set[str]] = {}
    for rule in doc["resourcePolicy"]["rules"]:
        tier = _tier_from_roles(rule["roles"])
        if tier is None:
            continue
        tools: set[str] = set()
        for condition in rule["condition"]["match"]["any"]["of"]:
            m = _TOOL_RE.search(condition["expr"])
            if m:
                tools.add(m.group(1))
        result[tier] = tools
    return result


def test_no_duplicate_tools_across_tiers():
    """A tool should appear in exactly one tier rule."""
    policy_tiers = _load_policy_tools_by_tier()
    all_tools: list[tuple[str, str]] = []
    for tier, tools in policy_tiers.items():
        all_tools.extend((t, tier) for t in tools)

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for tool, tier in all_tools:
        if tool in seen:
            duplicates.append(f"{tool} in both '{seen[tool]}' and '{tier}'")
        seen[tool] = tier

    assert not duplicates, f"Duplicate tool entries: {duplicates}"


def test_policy_matches_tool_tiers():
    """Every tool in TOOL_TIERS must appear in the matching policy rule."""
    from core.tool_tiers import TOOL_TIERS

    policy_tiers = _load_policy_tools_by_tier()

    mismatches: list[str] = []
    for tool, expected_tier in TOOL_TIERS.items():
        if tool not in policy_tiers.get(expected_tier, set()):
            actual = next(
                (t for t, tools in policy_tiers.items() if tool in tools),
                "MISSING",
            )
            mismatches.append(f"{tool}: expected in '{expected_tier}', found in '{actual}'")

    assert not mismatches, "Policy/tier mismatches:\n" + "\n".join(mismatches)


def test_no_orphan_policy_entries():
    """Policy should not reference tools that don't exist in TOOL_TIERS."""
    from core.tool_tiers import TOOL_TIERS

    policy_tiers = _load_policy_tools_by_tier()

    all_policy_tools: set[str] = set()
    for tools in policy_tiers.values():
        all_policy_tools.update(tools)

    orphans = all_policy_tools - set(TOOL_TIERS.keys())
    assert not orphans, f"Policy references non-existent tools: {sorted(orphans)}"


# ── Tier hierarchy enforcement ──────────────────────────────────────────


_EXPECTED_ROLES = {
    "public": {"public", "developer", "admin"},
    "developer": {"developer", "admin"},
    "admin": {"admin"},
}


def _load_policy_roles_by_tier() -> dict[str, set[str]]:
    """Parse the YAML and return {tier: set_of_roles} from each rule."""
    with open(POLICY_PATH) as f:
        doc = yaml.safe_load(f)

    result: dict[str, set[str]] = {}
    for rule in doc["resourcePolicy"]["rules"]:
        tier = _tier_from_roles(rule["roles"])
        if tier is not None:
            result[tier] = set(rule["roles"])
    return result


def test_policy_role_hierarchy():
    """Each tier rule must grant access to the correct roles (nested permissions)."""
    actual = _load_policy_roles_by_tier()
    for tier, expected_roles in _EXPECTED_ROLES.items():
        assert actual.get(tier) == expected_roles, (
            f"Tier '{tier}': expected roles {sorted(expected_roles)}, "
            f"got {sorted(actual.get(tier, set()))}"
        )


def test_admin_can_access_all_tools():
    """Admin role must be able to invoke every tool in TOOL_TIERS."""
    from core.tool_tiers import TOOL_TIERS, role_may_invoke

    denied = [t for t in TOOL_TIERS if not role_may_invoke("admin", t)]
    assert not denied, f"Admin denied access to: {sorted(denied)}"


def test_developer_can_access_developer_and_public():
    """Developer role can invoke developer + public tools, but not admin."""
    from core.tool_tiers import TOOL_TIERS, ADMIN, role_may_invoke

    for tool, tier in TOOL_TIERS.items():
        allowed = role_may_invoke("developer", tool)
        if tier == ADMIN:
            assert not allowed, f"Developer should NOT access admin tool '{tool}'"
        else:
            assert allowed, f"Developer should access {tier} tool '{tool}'"


def test_public_can_access_only_public():
    """Public role can invoke only public tools."""
    from core.tool_tiers import TOOL_TIERS, PUBLIC, role_may_invoke

    for tool, tier in TOOL_TIERS.items():
        allowed = role_may_invoke("public", tool)
        if tier == PUBLIC:
            assert allowed, f"Public should access public tool '{tool}'"
        else:
            assert not allowed, f"Public should NOT access {tier} tool '{tool}'"

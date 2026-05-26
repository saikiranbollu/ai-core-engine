"""Tool Access Tiers. Every tool → exactly one of: public, developer, admin.

Role hierarchy:  admin ⊃ developer ⊃ public
  • admin     → may invoke all tools
  • developer → may invoke public + developer tools
  • public    → may invoke public tools only
"""
from __future__ import annotations
import inspect
import logging
import os
from typing import Dict, List, Optional, Set

# Tier constants
PUBLIC = "public"
DEVELOPER = "developer"
ADMIN = "admin"

# BUG FIX #1: search_database (not search_databases) per PPTX v3
TOOL_TIERS: Dict[str, str] = {
    # Cat 1: Search & Query
    "search_database": PUBLIC, "search_nodes": PUBLIC, "get_node_by_id": PUBLIC,
    "get_neighbors": DEVELOPER, "shortest_path": DEVELOPER, "execute_cypher": DEVELOPER,
    # Cat 2: API Intelligence
    "query_api_function": PUBLIC, "get_type_definition": PUBLIC, "generate_initialization_code": PUBLIC,
    # Cat 3: Dependency Analysis
    "query_dependencies": PUBLIC, "validate_api_usage": PUBLIC, "detect_polling_requirements": PUBLIC,
    # Cat 4: Traceability
    "find_requirement_traces": PUBLIC, "build_traceability_matrix": PUBLIC,
    "find_coverage_gaps": PUBLIC, "analyze_hw_sw_links": PUBLIC,
    # Cat 5: Ingestion Pipeline — Removed from MCP (Plan 2 Phase 2)
    # Use sandbox_upload for all file ingestion via MCP.
    # Underlying IngestionService preserved as library code.
    # "ingest_file": ADMIN, "ingest_module_from_repo": ADMIN,
    # "batch_ingest_modules": ADMIN, "ingest_repository": ADMIN,
    # Cat 6: Memory & Context
    "session_start": PUBLIC, "session_store": PUBLIC, "session_retrieve": PUBLIC,
    "build_context": PUBLIC, "session_end": PUBLIC,
    # Cat 6+: Ephemeral Sandbox
    "sandbox_upload": PUBLIC,
    # "sandbox_query": PUBLIC,  # Deprecated: use search_database(session_id=...) instead
    "sandbox_status": PUBLIC, "sandbox_clear": PUBLIC,
    "sandbox_diff": PUBLIC,
    # Cat 6+: RLM
    "rlm_orchestrate": DEVELOPER, "rlm_plan_preview": PUBLIC,
    # Cat 6+: HSI
    "get_function_hsi": PUBLIC,
    # Cat 7: Cache
    "cache_get": DEVELOPER, "cache_stats": DEVELOPER,
    "cache_invalidate_module": ADMIN, "cache_clear": ADMIN, "cache_refresh_config": ADMIN,
    # Cat 8: Feedback & Learning
    "submit_human_feedback": PUBLIC, "get_learning_metrics": DEVELOPER,
    "get_failure_patterns": DEVELOPER, "process_results": ADMIN,
    # Cat 9: Review Gate
    "evaluate_confidence": PUBLIC, "complete_review": PUBLIC,
    "override_review_routing": DEVELOPER, "get_review_analytics": DEVELOPER,
    # Cat 10: Ontology & Config
    "list_ontology_profiles": PUBLIC, "get_ontology_schema": PUBLIC,
    "validate_entity": DEVELOPER, "get_ontology_compliance": DEVELOPER,
    # Cat 11: Observability & Health
    "health_check": PUBLIC, "get_graph_statistics": PUBLIC,
    "list_available_modules": PUBLIC, "get_distribution": PUBLIC,
    "get_coverage_report": PUBLIC, "detect_communities": DEVELOPER,
    # Cat 12: Visualization
    "visualize_subgraph": DEVELOPER,
    # Cat 13: Authentication
    "get_token_info": DEVELOPER, "ensure_valid_token": ADMIN,
    # Cat 14: GAP v2 Tools (Sprint 25 — C01 fix)
    "query_enhance": DEVELOPER,
}

# Tier hierarchy — higher tiers include all lower tiers
TIER_HIERARCHY: Dict[str, Set[str]] = {
    PUBLIC:    {PUBLIC},
    DEVELOPER: {PUBLIC, DEVELOPER},
    ADMIN:     {PUBLIC, DEVELOPER, ADMIN},
}


def get_tool_tier(tool_name: str) -> Optional[str]:
    """Return the access tier for a tool, or None if unknown."""
    return TOOL_TIERS.get(tool_name)


def role_may_invoke(role: str, tool_name: str) -> bool:
    """Check whether *role* is allowed to invoke *tool_name* (tier hierarchy)."""
    tier = TOOL_TIERS.get(tool_name)
    if tier is None:
        return False
    allowed_tiers = TIER_HIERARCHY.get(role, set())
    return tier in allowed_tiers


def validate_tool_registration(mcp_instance) -> None:
    """Assert all registered MCP tools exist in TOOL_TIERS.

    Raises RuntimeError at startup if any tool is missing from the tier map.
    Call after all @mcp.tool() decorators have executed.
    """
    import logging

    registered_tools = set(mcp_instance._tool_manager._tools.keys())
    tier_tools = set(TOOL_TIERS.keys())

    missing_from_tiers = registered_tools - tier_tools
    if missing_from_tiers:
        raise RuntimeError(
            f"Tools registered via @mcp.tool() but missing from TOOL_TIERS: "
            f"{sorted(missing_from_tiers)}. Add them to mcp/core/tool_tiers.py"
        )

    orphan_tiers = tier_tools - registered_tools
    if orphan_tiers:
        logging.getLogger(__name__).warning(
            "TOOL_TIERS entries with no matching @mcp.tool(): %s",
            sorted(orphan_tiers),
        )

    # ── MEG_SW-338: Signature validation ──
    sig_issues = validate_tool_signatures(mcp_instance)
    if sig_issues:
        _logger = logging.getLogger(__name__)
        for issue in sig_issues:
            _logger.warning("Signature issue: %s", issue)
        if os.environ.get("STRICT_SIGNATURE_VALIDATION", "").lower() == "true":
            raise RuntimeError(
                f"{len(sig_issues)} tool signature validation failure(s)"
            )


# ── Injected params that are added by decorators, not by the caller ────────
_INJECTED_PARAMS = frozenset({
    "session_id", "graph_service", "hybrid_traversal",
    "query_mode", "sandbox_ctx",
})


def validate_tool_signatures(mcp_instance) -> List[str]:
    """Inspect every registered tool's signature for missing annotations.

    Returns a list of human-readable issue strings (empty = all clean).
    """
    issues: List[str] = []
    for tool_name, fn in mcp_instance._tool_manager._tools.items():
        # Get the underlying function (unwrap the Tool object if needed)
        handler = getattr(fn, "fn", fn)
        if handler is None or not callable(handler):
            continue
        try:
            sig = inspect.signature(handler)
        except (ValueError, TypeError):
            continue
        for name, param in sig.parameters.items():
            if name in _INJECTED_PARAMS:
                continue
            if param.annotation is inspect.Parameter.empty:
                issues.append(
                    f"{tool_name}: param '{name}' missing type annotation"
                )
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                issues.append(
                    f"{tool_name}: uses *args (unsupported by MCP)"
                )
        if sig.return_annotation is inspect.Signature.empty:
            issues.append(f"{tool_name}: missing return type annotation")
    return issues

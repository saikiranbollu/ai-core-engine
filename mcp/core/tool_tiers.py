"""Tool Access Tiers. Every tool → exactly one of: public, developer, admin.

Role hierarchy:  admin ⊃ developer ⊃ public
  • admin     → may invoke all tools
  • developer → may invoke public + developer tools
  • public    → may invoke public tools only
"""
from __future__ import annotations
from typing import Dict, Optional, Set

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

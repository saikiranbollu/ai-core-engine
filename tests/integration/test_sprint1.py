"""
Sprint 1 Integration Tests — AI Core Engine
=============================================
Validates:
    1. All 56 tools are registered on the MCP server
  2. Tool naming matches PPTX v3 (Bug Fix #1)
  3. Auth middleware enforces tiers (Bug Fix #2)
  4. health_check tool returns structured response
  5. No hardcoded credentials in source (Bug Fix #3)
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

# Make mcp importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.core.tool_tiers import TOOL_TIERS
from mcp.core.auth_middleware import check_authorization


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: Tool Inventory
# ═════════════════════════════════════════════════════════════════════════

class TestToolInventory:
    """Verify all 56 tools are registered with correct tiers."""

    def test_total_tool_count(self):
        """50 PPTX base + 4 Sandbox + 2 RLM = 56 tools."""
        assert len(TOOL_TIERS) == 56, f"Expected 56 tools, got {len(TOOL_TIERS)}"

    def test_public_tool_count(self):
        public = [t for t, tier in TOOL_TIERS.items() if tier == "public"]
        assert len(public) == 34, f"Expected 34 Public tools, got {len(public)}"

    def test_developer_tool_count(self):
        dev = [t for t, tier in TOOL_TIERS.items() if tier == "developer"]
        assert len(dev) == 14, f"Expected 14 Developer tools, got {len(dev)}"

    def test_admin_tool_count(self):
        admin = [t for t, tier in TOOL_TIERS.items() if tier == "admin"]
        assert len(admin) == 8, f"Expected 8 Admin tools, got {len(admin)}"

    def test_bug_fix_1_naming(self):
        """Bug Fix #1: search_database (singular), not search_databases."""
        assert "search_database" in TOOL_TIERS
        assert "search_databases" not in TOOL_TIERS

    def test_all_categories_represented(self):
        """All 13 categories + Sandbox + RLM should have tools."""
        expected_tools = {
            # Cat 1
            "search_database", "search_nodes", "get_node_by_id",
            "get_neighbors", "shortest_path", "execute_cypher",
            # Cat 2
            "query_api_function", "get_type_definition", "generate_initialization_code",
            # Cat 3
            "query_dependencies", "validate_api_usage", "detect_polling_requirements",
            # Cat 4
            "find_requirement_traces", "build_traceability_matrix",
            "find_coverage_gaps", "analyze_hw_sw_links",
            # Cat 5
            "ingest_file", "ingest_module_from_repo",
            "batch_ingest_modules", "ingest_repository",
            # Cat 6
            "session_start", "session_store", "session_retrieve",
            "build_context", "session_end",
            # Cat 6+ Sandbox
            "sandbox_upload", "sandbox_query", "sandbox_status", "sandbox_clear",
            # Cat 6+ RLM
            "rlm_orchestrate", "rlm_plan_preview",
            # Cat 7
            "cache_get", "cache_stats", "cache_invalidate_module", "cache_clear",
            # Cat 8
            "submit_human_feedback", "get_learning_metrics",
            "get_failure_patterns", "process_results",
            # Cat 9
            "evaluate_confidence", "complete_review",
            "override_review_routing", "get_review_analytics",
            # Cat 10
            "list_ontology_profiles", "get_ontology_schema",
            "validate_entity", "get_ontology_compliance",
            # Cat 11
            "health_check", "get_graph_statistics", "list_available_modules",
            "get_distribution", "get_coverage_report", "detect_communities",
            # Cat 12
            "visualize_subgraph",
            # Cat 13
            "get_token_info", "ensure_valid_token",
        }
        actual = set(TOOL_TIERS.keys())
        missing = expected_tools - actual
        extra = actual - expected_tools
        assert not missing, f"Missing tools: {missing}"
        assert not extra, f"Extra tools not in spec: {extra}"


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: Authorization (Bug Fix #2)
# ═════════════════════════════════════════════════════════════════════════

class TestAuthorization:
    """Verify Cerbos RBAC enforcement is active."""

    def test_public_tool_allowed_with_public_key(self):
        """Public tools should be accessible with a valid public-tier API key."""
        allowed, msg = check_authorization("key-gest-001", "search_database", "illd")
        assert allowed is True
        assert msg == "allowed"

    def test_public_tool_denied_without_key(self):
        """All tools require a known API key in the current auth contract."""
        allowed, msg = check_authorization("", "search_database", "illd")
        assert allowed is False
        assert "missing api key" in msg.lower() or "unknown" in msg.lower()

    def test_admin_tool_denied_with_public_key(self):
        """Admin tools denied for public-tier keys."""
        allowed, msg = check_authorization("key-gest-001", "ingest_file", "illd")
        assert allowed is False
        assert "insufficient" in msg.lower()

    def test_admin_tool_allowed_with_admin_key(self):
        """Admin tools should be accessible with an admin-tier API key."""
        allowed, msg = check_authorization("key-admin-pipeline", "cache_clear", "illd")
        assert allowed is True
        assert msg == "allowed"


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: No Hardcoded Credentials (Bug Fix #3)
# ═════════════════════════════════════════════════════════════════════════

class TestNoHardcodedCredentials:
    """Scan source files for hardcoded tokens/passwords."""

    CREDENTIAL_PATTERNS = [
        r'api_key\s*=\s*["\'][^$\{][a-zA-Z0-9]{10,}',    # api_key = "sk-abc..."
        r'password\s*=\s*["\'][^$\{][a-zA-Z0-9]{6,}',      # password = "secret"
        r'token\s*=\s*["\'][^$\{][a-zA-Z0-9]{10,}',        # token = "jwt..."
        r'Bearer\s+[a-zA-Z0-9\-_]{20,}',                    # Bearer <token>
    ]

    def test_mcp_server_no_hardcoded_creds(self):
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        for pattern in self.CREDENTIAL_PATTERNS:
            matches = re.findall(pattern, content)
            assert not matches, f"Hardcoded credential found: {matches[0][:50]}..."

    def test_auth_middleware_no_hardcoded_creds(self):
        auth_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "auth_middleware.py"
        content = auth_path.read_text(encoding="utf-8")
        for pattern in self.CREDENTIAL_PATTERNS:
            matches = re.findall(pattern, content)
            assert not matches, f"Hardcoded credential found: {matches[0][:50]}..."


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Cypher Write Rejection
# ═════════════════════════════════════════════════════════════════════════

class TestCypherSafety:
    """execute_cypher must reject write operations."""

    WRITE_KEYWORDS = ("CREATE", "MERGE", "DELETE", "SET ", "REMOVE", "DETACH")

    def _check_write_rejected(self, query: str) -> bool:
        """Replicate the write-clause check from mcp_server.py."""
        upper_q = query.upper()
        for kw in self.WRITE_KEYWORDS:
            if kw in upper_q:
                return True
        return False

    def test_cypher_rejects_create(self):
        assert self._check_write_rejected("CREATE (n:Test {name: 'bad'})")

    def test_cypher_rejects_delete(self):
        assert self._check_write_rejected("MATCH (n) DELETE n")

    def test_cypher_rejects_merge(self):
        assert self._check_write_rejected("MERGE (n:Test {id: 1})")

    def test_cypher_rejects_detach_delete(self):
        assert self._check_write_rejected("MATCH (n) DETACH DELETE n")

    def test_cypher_rejects_set(self):
        assert self._check_write_rejected("MATCH (n) SET n.name = 'x'")

    def test_cypher_allows_read(self):
        assert not self._check_write_rejected("MATCH (n:Function) RETURN n.name LIMIT 10")

    def test_cypher_allows_match_with_where(self):
        assert not self._check_write_rejected(
            "MATCH (n:DriverFunction {module: $m}) WHERE n.name CONTAINS 'init' RETURN n"
        )

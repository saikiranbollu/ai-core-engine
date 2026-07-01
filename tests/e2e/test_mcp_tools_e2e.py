"""
E2E Test Suite — All 50 MCP Tools via Streamable HTTP.

Exercises every registered MCP tool against a live test server deployment.
Tests are organized by tool category and validate:
  - Tool is callable (HTTP 200, no JSON-RPC error)
  - Response follows the expected envelope schema
  - Tool-specific fields are present in the result

Usage:
    MCP_TEST_URL=https://test-mcp-ai-core-engine.eu-de-7.icp.infineon.com \
    MCP_TEST_API_KEY=<key> \
    pytest tests/e2e/test_mcp_tools_e2e.py -v

Markers:
    @pytest.mark.e2e — all tests in this file
"""

import json
import uuid

import pytest
import pytest_asyncio

from .mcp_test_client import MCPTestClient

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="session")]


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 0: Discovery & Health
# ═══════════════════════════════════════════════════════════════════════════════

class TestDiscoveryAndHealth:
    """Tests for server health, tool listing, and graph stats."""

    async def test_tool_listing(self, mcp_client: MCPTestClient):
        """Server exposes tools via tools/list."""
        tools = await mcp_client.list_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 40, f"Expected >=40 tools, got {len(tools)}"
        tool_names = [t["name"] for t in tools]
        assert "health_check" in tool_names

    async def test_health_check(self, mcp_client: MCPTestClient):
        """health_check returns server status."""
        resp = await mcp_client.call_tool("health_check")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_list_available_modules(self, mcp_client: MCPTestClient):
        """list_available_modules returns module inventory."""
        resp = await mcp_client.call_tool("list_available_modules", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_graph_statistics(self, mcp_client: MCPTestClient):
        """get_graph_statistics returns node/relationship counts."""
        resp = await mcp_client.call_tool("get_graph_statistics", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 1: Search & Query
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchAndQuery:
    """Tests for database search, node retrieval, and graph traversal."""

    async def test_search_database(self, mcp_client: MCPTestClient):
        """search_database performs hybrid RAG search."""
        resp = await mcp_client.call_tool("search_database", {
            "query": "ADC initialization sequence",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_search_nodes(self, mcp_client: MCPTestClient):
        """search_nodes finds nodes by label/property."""
        resp = await mcp_client.call_tool("search_nodes", {
            "query": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_node_by_id(self, mcp_client: MCPTestClient):
        """get_node_by_id retrieves a node (may 404 on empty DB)."""
        resp = await mcp_client.call_tool("get_node_by_id", {
            "node_id": "test-nonexistent-id",
            "workspace_id": "illd",
        })
        # Not asserting success — node may not exist; just no crash
        assert "error" not in resp or resp["error"].get("code") != -32603

    async def test_get_neighbors(self, mcp_client: MCPTestClient):
        """get_neighbors traverses graph relationships."""
        resp = await mcp_client.call_tool("get_neighbors", {
            "node_id": "test-nonexistent-id",
            "workspace_id": "illd",
        })
        assert "error" not in resp or resp["error"].get("code") != -32603

    async def test_shortest_path(self, mcp_client: MCPTestClient):
        """shortest_path finds path between two nodes."""
        resp = await mcp_client.call_tool("shortest_path", {
            "source_id": "node-a",
            "target_id": "node-b",
            "workspace_id": "illd",
        })
        assert "error" not in resp or resp["error"].get("code") != -32603

    async def test_execute_cypher(self, mcp_client: MCPTestClient):
        """execute_cypher runs read-only Cypher query."""
        resp = await mcp_client.call_tool("execute_cypher", {
            "query": "MATCH (n) RETURN count(n) AS total LIMIT 1",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 2: API Intelligence
# ═══════════════════════════════════════════════════════════════════════════════

class TestAPIIntelligence:
    """Tests for API function queries, type defs, and code generation."""

    async def test_query_api_function(self, mcp_client: MCPTestClient):
        """query_api_function looks up function documentation."""
        resp = await mcp_client.call_tool("query_api_function", {
            "function_name": "Adc_Init",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_type_definition(self, mcp_client: MCPTestClient):
        """get_type_definition retrieves struct/type info."""
        resp = await mcp_client.call_tool("get_type_definition", {
            "type_name": "Adc_ConfigType",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_generate_initialization_code(self, mcp_client: MCPTestClient):
        """generate_initialization_code produces boilerplate."""
        resp = await mcp_client.call_tool("generate_initialization_code", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 3: Dependency Analysis
# ═══════════════════════════════════════════════════════════════════════════════

class TestDependencyAnalysis:
    """Tests for dependency queries and validation."""

    async def test_query_dependencies(self, mcp_client: MCPTestClient):
        """query_dependencies finds module dependencies."""
        resp = await mcp_client.call_tool("query_dependencies", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_validate_api_usage(self, mcp_client: MCPTestClient):
        """validate_api_usage checks API correctness."""
        resp = await mcp_client.call_tool("validate_api_usage", {
            "function_name": "Adc_Init",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_detect_polling_requirements(self, mcp_client: MCPTestClient):
        """detect_polling_requirements identifies polling patterns."""
        resp = await mcp_client.call_tool("detect_polling_requirements", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 4: Traceability
# ═══════════════════════════════════════════════════════════════════════════════

class TestTraceability:
    """Tests for requirement tracing and coverage."""

    async def test_find_requirement_traces(self, mcp_client: MCPTestClient):
        """find_requirement_traces locates requirement links."""
        resp = await mcp_client.call_tool("find_requirement_traces", {
            "requirement_id": "REQ-ADC-001",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_build_traceability_matrix(self, mcp_client: MCPTestClient):
        """build_traceability_matrix generates a trace matrix."""
        resp = await mcp_client.call_tool("build_traceability_matrix", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_find_coverage_gaps(self, mcp_client: MCPTestClient):
        """find_coverage_gaps identifies untested requirements."""
        resp = await mcp_client.call_tool("find_coverage_gaps", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_analyze_hw_sw_links(self, mcp_client: MCPTestClient):
        """analyze_hw_sw_links traces HW-SW boundaries."""
        resp = await mcp_client.call_tool("analyze_hw_sw_links", {
            "module_name": "ADC",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 6: Session & Memory
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionLifecycle:
    """Tests for working-memory session lifecycle (ordered)."""

    async def test_session_start(self, mcp_client: MCPTestClient):
        """session_start creates a new working-memory session."""
        resp = await mcp_client.call_tool("session_start", {
            "session_id": f"e2e_lifecycle_{uuid.uuid4().hex[:8]}",
            "assistant_name": "aice-e2e",
            "module_context": "Adc",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)
        result = mcp_client.get_tool_result(resp)
        assert isinstance(result, dict)
        payload = result.get("data", result)
        assert "session_id" in payload

    async def test_session_store(self, mcp_client: MCPTestClient, mcp_session: str):
        """session_store persists data in working memory."""
        resp = await mcp_client.call_tool("session_store", {
            "session_id": mcp_session,
            "key": "test_key",
            "value": "test_value_123",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_session_retrieve(self, mcp_client: MCPTestClient, mcp_session: str):
        """session_retrieve reads back stored data."""
        resp = await mcp_client.call_tool("session_retrieve", {
            "session_id": mcp_session,
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_build_context(self, mcp_client: MCPTestClient, mcp_session: str):
        """build_context assembles token-budget context."""
        resp = await mcp_client.call_tool("build_context", {
            "session_id": mcp_session,
            "query": "ADC initialization",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_session_end(self, mcp_client: MCPTestClient):
        """session_end terminates a session cleanly."""
        # Create a short-lived session to end
        sid = f"e2e_end_{uuid.uuid4().hex[:8]}"
        start_resp = await mcp_client.call_tool("session_start", {
            "session_id": sid,
            "assistant_name": "aice-e2e",
        })
        result = mcp_client.get_tool_result(start_resp)
        payload = result.get("data", result) if isinstance(result, dict) else {}
        sid = payload.get("session_id", sid) if isinstance(payload, dict) else sid

        resp = await mcp_client.call_tool("session_end", {"session_id": sid})
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 6+: Ephemeral Sandbox
# ═══════════════════════════════════════════════════════════════════════════════

class TestSandbox:
    """Tests for ephemeral sandbox operations."""

    async def test_sandbox_upload(self, mcp_client: MCPTestClient, mcp_session: str):
        """sandbox_upload ingests a document into the sandbox."""
        resp = await mcp_client.call_tool("sandbox_upload", {
            "session_id": mcp_session,
            "content": "# Test Doc\nThis is a test document for E2E sandbox.",
            "filename": "e2e_test.md",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_sandbox_status(self, mcp_client: MCPTestClient, mcp_session: str):
        """sandbox_status shows sandbox state."""
        resp = await mcp_client.call_tool("sandbox_status", {
            "session_id": mcp_session,
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_sandbox_diff(self, mcp_client: MCPTestClient, mcp_session: str):
        """sandbox_diff shows what sandbox adds over production."""
        resp = await mcp_client.call_tool("sandbox_diff", {
            "session_id": mcp_session,
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_sandbox_clear(self, mcp_client: MCPTestClient, mcp_session: str):
        """sandbox_clear removes all sandbox data."""
        resp = await mcp_client.call_tool("sandbox_clear", {
            "session_id": mcp_session,
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 6+: RLM (Reinforcement Learning Manager)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRLM:
    """Tests for RLM orchestration and plan preview."""

    async def test_rlm_orchestrate(self, mcp_client: MCPTestClient):
        """rlm_orchestrate routes a complex query."""
        resp = await mcp_client.call_tool("rlm_orchestrate", {
            "query": "Generate unit test for ADC module",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_rlm_plan_preview(self, mcp_client: MCPTestClient):
        """rlm_plan_preview shows execution plan without running."""
        resp = await mcp_client.call_tool("rlm_plan_preview", {
            "query": "Explain ADC driver architecture",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 6+: HSI (Hardware-Software Interface)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHSI:
    """Tests for Hardware-Software Interface queries."""

    async def test_get_function_hsi(self, mcp_client: MCPTestClient):
        """get_function_hsi returns HW-SW interface details."""
        resp = await mcp_client.call_tool("get_function_hsi", {
            "function_name": "Adc_Init",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 7: Cache
# ═══════════════════════════════════════════════════════════════════════════════

class TestCache:
    """Tests for cache management tools."""

    async def test_cache_stats(self, mcp_client: MCPTestClient):
        """cache_stats returns cache hit/miss metrics."""
        resp = await mcp_client.call_tool("cache_stats")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_cache_get(self, mcp_client: MCPTestClient):
        """cache_get retrieves a cached item (miss is OK)."""
        resp = await mcp_client.call_tool("cache_get", {
            "key": "nonexistent_test_key",
        })
        # Cache miss is valid — just ensure no server crash
        assert "error" not in resp or resp["error"].get("code") != -32603

    async def test_cache_invalidate_module(self, mcp_client: MCPTestClient):
        """cache_invalidate_module clears module-specific cache."""
        resp = await mcp_client.call_tool("cache_invalidate_module", {
            "module_name": "TEST_MODULE",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_cache_refresh_config(self, mcp_client: MCPTestClient):
        """cache_refresh_config reloads cache configuration."""
        resp = await mcp_client.call_tool("cache_refresh_config")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_cache_clear(self, mcp_client: MCPTestClient):
        """cache_clear wipes entire cache (admin operation)."""
        resp = await mcp_client.call_tool("cache_clear")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 8: Feedback & Learning
# ═══════════════════════════════════════════════════════════════════════════════

class TestFeedback:
    """Tests for feedback submission and learning metrics."""

    async def test_submit_human_feedback(self, mcp_client: MCPTestClient):
        """submit_human_feedback records user feedback."""
        resp = await mcp_client.call_tool("submit_human_feedback", {
            "query": "test query",
            "response_id": str(uuid.uuid4()),
            "rating": 5,
            "comment": "E2E test feedback",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_learning_metrics(self, mcp_client: MCPTestClient):
        """get_learning_metrics returns feedback aggregates."""
        resp = await mcp_client.call_tool("get_learning_metrics")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_failure_patterns(self, mcp_client: MCPTestClient):
        """get_failure_patterns identifies recurring issues."""
        resp = await mcp_client.call_tool("get_failure_patterns")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_process_results(self, mcp_client: MCPTestClient):
        """process_results feeds back search results for learning."""
        resp = await mcp_client.call_tool("process_results", {
            "query": "ADC configuration",
            "results": [{"text": "sample result", "score": 0.9}],
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 9: Review Gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestReviewGate:
    """Tests for confidence scoring and review routing."""

    async def test_evaluate_confidence(self, mcp_client: MCPTestClient):
        """evaluate_confidence scores a response."""
        resp = await mcp_client.call_tool("evaluate_confidence", {
            "query": "How to initialize ADC?",
            "response": "Call Adc_Init() with a valid config pointer.",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_complete_review(self, mcp_client: MCPTestClient):
        """complete_review finalizes a review cycle."""
        resp = await mcp_client.call_tool("complete_review", {
            "review_id": "test-review-" + str(uuid.uuid4())[:8],
            "decision": "approve",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_override_review_routing(self, mcp_client: MCPTestClient):
        """override_review_routing changes routing rules."""
        resp = await mcp_client.call_tool("override_review_routing", {
            "tool_name": "search_database",
            "route_to": "auto",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_review_analytics(self, mcp_client: MCPTestClient):
        """get_review_analytics returns review statistics."""
        resp = await mcp_client.call_tool("get_review_analytics")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 10: Ontology & Config
# ═══════════════════════════════════════════════════════════════════════════════

class TestOntology:
    """Tests for ontology management and validation."""

    async def test_list_ontology_profiles(self, mcp_client: MCPTestClient):
        """list_ontology_profiles returns available profiles."""
        resp = await mcp_client.call_tool("list_ontology_profiles")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_ontology_schema(self, mcp_client: MCPTestClient):
        """get_ontology_schema returns node/edge definitions."""
        resp = await mcp_client.call_tool("get_ontology_schema", {
            "profile": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_validate_entity(self, mcp_client: MCPTestClient):
        """validate_entity checks entity compliance."""
        resp = await mcp_client.call_tool("validate_entity", {
            "entity_type": "ILLD_Function",
            "properties": {"name": "Adc_Init", "module": "ADC"},
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_ontology_compliance(self, mcp_client: MCPTestClient):
        """get_ontology_compliance reports compliance metrics."""
        resp = await mcp_client.call_tool("get_ontology_compliance", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 11: Observability & Visualization
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservability:
    """Tests for graph analysis and visualization tools."""

    async def test_get_distribution(self, mcp_client: MCPTestClient):
        """get_distribution shows node type distribution."""
        resp = await mcp_client.call_tool("get_distribution", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_get_coverage_report(self, mcp_client: MCPTestClient):
        """get_coverage_report shows knowledge graph coverage."""
        resp = await mcp_client.call_tool("get_coverage_report", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_detect_communities(self, mcp_client: MCPTestClient):
        """detect_communities identifies graph clusters."""
        resp = await mcp_client.call_tool("detect_communities", {
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)

    async def test_visualize_subgraph(self, mcp_client: MCPTestClient):
        """visualize_subgraph generates subgraph visualization."""
        resp = await mcp_client.call_tool("visualize_subgraph", {
            "center_node": "ADC",
            "depth": 1,
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 13: Authentication
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthentication:
    """Tests for auth token inspection and validation."""

    async def test_get_token_info(self, mcp_client: MCPTestClient):
        """get_token_info returns current principal details."""
        resp = await mcp_client.call_tool("get_token_info")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)
        result = mcp_client.get_tool_result(resp)
        if isinstance(result, dict):
            assert "principal_id" in result or "roles" in result or "key" in result

    async def test_ensure_valid_token(self, mcp_client: MCPTestClient):
        """ensure_valid_token confirms auth is working."""
        resp = await mcp_client.call_tool("ensure_valid_token")
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Cat 14: GAP v2 (Query Enhancement)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGAPv2:
    """Tests for GAP query enhancement pipeline."""

    async def test_query_enhance(self, mcp_client: MCPTestClient):
        """query_enhance improves a user query."""
        resp = await mcp_client.call_tool("query_enhance", {
            "query": "How does ADC work?",
            "workspace_id": "illd",
        })
        assert mcp_client.is_success(resp), mcp_client.get_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# Meta: Tool Completeness Check
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolCompleteness:
    """Validates that all expected tools are registered on the server."""

    EXPECTED_TOOLS = [
        # Cat 1: Search & Query
        "search_database", "search_nodes", "get_node_by_id",
        "get_neighbors", "shortest_path", "execute_cypher",
        # Cat 2: API Intelligence
        "query_api_function", "get_type_definition", "generate_initialization_code",
        # Cat 3: Dependency Analysis
        "query_dependencies", "validate_api_usage", "detect_polling_requirements",
        # Cat 4: Traceability
        "find_requirement_traces", "build_traceability_matrix",
        "find_coverage_gaps", "analyze_hw_sw_links",
        # Cat 6: Session & Memory
        "session_start", "session_store", "session_retrieve",
        "build_context", "session_end",
        # Cat 6+: Sandbox
        "sandbox_upload", "sandbox_status", "sandbox_clear", "sandbox_diff",
        # Cat 6+: RLM
        "rlm_orchestrate", "rlm_plan_preview",
        # Cat 6+: HSI
        "get_function_hsi",
        # Cat 7: Cache
        "cache_get", "cache_stats", "cache_invalidate_module",
        "cache_clear", "cache_refresh_config",
        # Cat 8: Feedback
        "submit_human_feedback", "get_learning_metrics",
        "get_failure_patterns", "process_results",
        # Cat 9: Review Gate
        "evaluate_confidence", "complete_review",
        "override_review_routing", "get_review_analytics",
        # Cat 10: Ontology
        "list_ontology_profiles", "get_ontology_schema",
        "validate_entity", "get_ontology_compliance",
        # Cat 11: Observability
        "health_check", "get_graph_statistics", "list_available_modules",
        "get_distribution", "get_coverage_report",
        "detect_communities", "visualize_subgraph",
        # Cat 13: Auth
        "get_token_info", "ensure_valid_token",
        # Cat 14: GAP v2
        "query_enhance",
    ]

    async def test_all_expected_tools_registered(self, mcp_client: MCPTestClient):
        """Verify all 50 expected tools are available on the server."""
        tools = await mcp_client.list_tools()
        registered_names = {t["name"] for t in tools}

        missing = set(self.EXPECTED_TOOLS) - registered_names
        assert not missing, f"Missing tools on server: {sorted(missing)}"

    async def test_no_unexpected_tool_count_drop(self, mcp_client: MCPTestClient):
        """Guard against accidental tool removal (minimum 48 tools)."""
        tools = await mcp_client.list_tools()
        assert len(tools) >= 48, (
            f"Tool count dropped below threshold: {len(tools)} < 48"
        )

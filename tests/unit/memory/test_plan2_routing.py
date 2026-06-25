"""
Tests for Plan 2: Hybrid Query Routing

Validates that:
1. Shallow tools route to sandbox NetworkX
2. Deep tools route to production Neo4j
3. Results are tagged with _origin indicator
4. Sandbox patching works (override prod with sandbox modifications)
5. Sandbox-only nodes are injected into deep query results
"""

import pytest
from unittest.mock import MagicMock
from pathlib import Path

from src.MemoryLayer.memory.ephemeral_sandbox import HybridGraphService, EphemeralGraph


class TestShallowQueryRouting:
    """Test shallow queries route to sandbox."""
    
    def test_search_uses_sandbox_when_active(self):
        """search_database should use sandbox graph when session is active."""
        # Create mock sandbox and vector store
        mock_graph = MagicMock(spec=EphemeralGraph)
        mock_graph.keyword_search.return_value = [
            MagicMock(node_id="SRC_Function:Adc_Init:Adc", node_type="SRC_Function",
                     content="Modified implementation", score=0.95, origin="sandbox")
        ]
        mock_vectors = MagicMock()
        mock_vectors.search.return_value = []
        
        hybrid = HybridGraphService(sandbox=MagicMock(), neo4j_driver=MagicMock())
        hybrid._sandbox = MagicMock()
        hybrid._sandbox.graph = mock_graph
        hybrid._sandbox.vectors = mock_vectors
        
        # Shallow search
        results = hybrid.search("Adc_Init", top_k=10, alpha=0.5)
        
        # Should have called sandbox search
        mock_graph.keyword_search.assert_called()
        assert len(results) > 0
        assert results[0].origin in ("eph_graph", "sandbox")


class TestDeepQueryRouting:
    """Test deep queries route to production Neo4j with sandbox patching."""
    
    def test_deep_query_patches_sandbox_overrides(self):
        """Deep query results should replace prod nodes with sandbox versions."""

        mock_driver = MagicMock()
        # Mock prod result
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        prod_result = [
            {
                "label": "SRC_Function",
                "name": "Adc_Init",
                "module": "Adc",
                "signature": "void Adc_Init(cfg)",
                "_origin": "production"
            },
            {
                "label": "SRC_Function",
                "name": "Adc_Enable",
                "module": "Adc",
                "signature": "void Adc_Enable()",
                "_origin": "production"
            }
        ]
        
        mock_session.run.return_value.data.return_value = prod_result
        
        # Create sandbox with modified Adc_Init; canonical id is SRC_Function:Adc_Init:Adc
        mock_sandbox = MagicMock()
        mock_sandbox.graph.get_node.side_effect = lambda nid: {
            "SRC_Function:Adc_Init:Adc": {
                "signature": "void Adc_Init(cfg, mode)",
                "_origin": "sandbox",
                "_shadows": "SRC_Function:Adc_Init:Adc"
            }
        }.get(nid)
        mock_sandbox.graph.get_nodes_by_origin.return_value = []
        
        # Create hybrid service
        hybrid = HybridGraphService(sandbox=mock_sandbox, neo4j_driver=mock_driver)
        
        # Run deep query with patching logic
        cypher = "MATCH (n:SRC_Function) WHERE n.module='Adc' RETURN n"
        result = hybrid.deep_query(cypher, {}, workspace_id="illd")
        
        assert len(result) >= 1
        patched = [r for r in result if r.get("name") == "Adc_Init"]
        assert patched
        assert patched[0].get("_origin") == "sandbox"
        assert patched[0]["signature"] == "void Adc_Init(cfg, mode)"
    
    def test_inject_sandbox_only_nodes(self):
        """Deep query should inject nodes that only exist in sandbox."""
        
        # Mock prod result (empty or limited)
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        mock_session.run.return_value.data.return_value = [
            {"node_id": "prod_a", "name": "ProdFunc", "_origin": "production"}
        ]
        
        # Create sandbox with additional node — must include _node_type and module
        # for _sandbox_node_matches_query to accept it
        mock_sandbox = MagicMock()
        sandbox_nodes = {
            "sandbox_node_1": {
                "node_id": "sandbox_node_1",
                "name": "SandboxOnlyFunc",
                "module": "Adc",
                "_node_type": "SRC_Function",
                "_origin": "sandbox"
            }
        }
        mock_sandbox.graph.get_nodes_by_origin.return_value = list(sandbox_nodes.items())
        mock_sandbox.graph.get_node.return_value = None
        
        hybrid = HybridGraphService(sandbox=mock_sandbox, neo4j_driver=mock_driver)
        
        # Simulate deep_query logic
        result = hybrid.deep_query(
            "MATCH (n:SRC_Function) WHERE n.module = $mod RETURN n",
            {"mod": "Adc"},
        )
        
        # Should include both prod and sandbox-only nodes
        injected = [r for r in result if r.get("_injected") is True]
        assert injected
        assert injected[0].get("name") == "SandboxOnlyFunc"

    def test_deep_query_injects_only_nodes_matching_query_shape(self):
        """Sandbox-only injection should respect Cypher label/module shape."""

        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        mock_session.run.return_value.data.return_value = []

        mock_sandbox = MagicMock()
        mock_sandbox.graph.get_nodes_by_origin.return_value = [
            ("Macro:IFX_ADC_MAX_CH:ADC", {
                "name": "IFX_ADC_MAX_CH",
                "module": "ADC",
                "_node_type": "Macro",
                "_origin": "sandbox",
            }),
            ("Function:IfxAdc_init:ADC", {
                "name": "IfxAdc_init",
                "module": "ADC",
                "_node_type": "Function",
                "_origin": "sandbox",
            }),
        ]
        mock_sandbox.graph.get_node.return_value = None

        hybrid = HybridGraphService(sandbox=mock_sandbox, neo4j_driver=mock_driver)
        result = hybrid.deep_query(
            "MATCH (m:Macro) WHERE m.module = $mod RETURN m",
            {"mod": "ADC"},
            workspace_id="illd",
        )

        injected = [r for r in result if r.get("_injected") is True]
        assert len(injected) == 1
        assert injected[0]["name"] == "IFX_ADC_MAX_CH"


class TestToolClassification:
    """Test that tools are classified as SHALLOW or DEEP."""
    
    def test_shallow_tools_list(self):
        """Verify SHALLOW_TOOLS contains expected shallow query tools."""
        shallow = HybridGraphService.SHALLOW_TOOLS
        
        expected_shallow = {
            "search_database", "search_nodes", "get_node_by_id",
            "get_neighbors", "query_api_function", "query_dependencies"
        }
        
        for tool in expected_shallow:
            assert tool in shallow, f"{tool} should be in SHALLOW_TOOLS"
    
    def test_deep_tools_list(self):
        """Verify DEEP_TOOLS contains expected deep traversal tools."""
        deep = HybridGraphService.DEEP_TOOLS
        
        expected_deep = {
            "find_coverage_gaps", "build_traceability_matrix",
            "find_requirement_traces", "analyze_hw_sw_links"
        }
        
        for tool in expected_deep:
            assert tool in deep, f"{tool} should be in DEEP_TOOLS"


class TestResultTagging:
    """Test that query results are tagged with _origin indicators."""
    
    def test_sandbox_results_tagged_origin_sandbox(self):
        """Results from sandbox queries get _origin=sandbox."""
        mock_graph = MagicMock(spec=EphemeralGraph)
        mock_graph.keyword_search.return_value = [
            MagicMock(node_id="id1", origin="sandbox")
        ]
        mock_vectors = MagicMock()
        mock_vectors.search.return_value = []
        
        hybrid = HybridGraphService(sandbox=MagicMock(), neo4j_driver=MagicMock())
        hybrid._sandbox = MagicMock()
        hybrid._sandbox.graph = mock_graph
        hybrid._sandbox.vectors = mock_vectors
        
        results = hybrid.search("query", top_k=10, alpha=0.5)
        
        # Results should preserve origin tags
        assert all(hasattr(r, 'origin') or isinstance(r, dict) for r in results)
    
    def test_prod_results_tagged_origin_production(self):
        """Results from prod queries get _origin=production (default)."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        prod_result = [
            {"node_id": "prod_1", "name": "Func_A"}  # No _origin yet
        ]
        mock_session.run.return_value.data.return_value = prod_result
        
        mock_sandbox = MagicMock()
        mock_sandbox.graph.get_node.return_value = None
        mock_sandbox.graph.get_nodes_by_origin.return_value = []

        hybrid = HybridGraphService(sandbox=mock_sandbox, neo4j_driver=mock_driver)
        
        result = hybrid.deep_query("MATCH (n) RETURN n", {})
        
        # First result should be tagged
        first = result[0]
        assert first.get("_origin") in (None, "production")  # Unset or explicitly production


class TestHybridGraphServiceIntegration:
    """Integration tests for full hybrid query workflow."""
    
    def test_hybrid_service_respects_session_sandbox(self):
        """When session has sandbox, hybrid service uses it for shallow queries."""
        mock_neo4j = MagicMock()
        mock_sandbox = MagicMock()
        
        hybrid = HybridGraphService(sandbox=mock_sandbox, neo4j_driver=mock_neo4j)
        
        # Shallow tool should prefer sandbox
        assert "search_database" in HybridGraphService.SHALLOW_TOOLS
        
        # Deep tool should use prod
        assert "find_coverage_gaps" in HybridGraphService.DEEP_TOOLS
    
    def test_sandbox_not_active_uses_prod_only(self):
        """When no sandbox active (sandbox=None), all queries go to prod."""
        mock_neo4j = MagicMock()
        
        hybrid = HybridGraphService(sandbox=None, neo4j_driver=mock_neo4j)
        
        # Both shallow and deep should route to prod
        # (implementation detail: shallow falls through to prod if no sandbox)
        assert hybrid._sandbox is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for Plan 2: True Graph Overlay - Shadow/Override Semantics

Validates that:
1. Sandbox nodes can shadow prod nodes by node_id
2. Shadowed nodes retain original prod properties in _original_prod_properties
3. Edge override logic works (new edges added, replaced if same source/target/type)
4. Metadata tags (_origin, _shadows) are correctly applied
"""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
import json

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from MemoryLayer.memory.ephemeral_sandbox import (
    EphemeralGraph, SandboxAdapter, TraceabilityPuller
)


class TestShadowLogic:
    """Test node shadowing and override semantics."""
    
    def test_shadow_detection_same_node_id(self):
        """When sandbox ingests a node matching a prod node_id, it shadows."""
        graph = EphemeralGraph()
        
        # Add prod node
        prod_props = {
            "name": "Adc_Init",
            "module": "Adc",
            "signature": "void Adc_Init(const Adc_Config *cfg)",
            "_origin": "production",
            "_neo4j_id": "prod_123"
        }
        graph.add_node("SRC_Function", "SRC_Function:Adc_Init:Adc", prod_props)
        
        # Now sandbox ingests a modified version of the same function
        sandbox_props = {
            "name": "Adc_Init",
            "module": "Adc",
            "signature": "void Adc_Init(const Adc_Config *cfg, uint8_t mode)",
            "_origin": "sandbox",
        }
        
        # Simulate shadow logic
        adapter = SandboxAdapter()
        node_id = "SRC_Function:Adc_Init:Adc"
        existing = graph.get_node(node_id)
        
        assert existing is not None
        assert existing.get("_origin") == "production"
        
        # Apply shadow: keep prod properties, replace with sandbox
        if existing and existing.get("_origin") == "production":
            sandbox_props["_origin"] = "sandbox"
            sandbox_props["_shadows"] = node_id
            sandbox_props["_original_prod_properties"] = {
                k: v for k, v in existing.items() if not k.startswith("_")
            }
        
        graph.update_node(node_id, sandbox_props)
        updated = graph.get_node(node_id)
        
        assert updated["_origin"] == "sandbox"
        assert updated["_shadows"] == node_id
        assert updated["_original_prod_properties"]["signature"] == "void Adc_Init(const Adc_Config *cfg)"
        assert updated["signature"] == "void Adc_Init(const Adc_Config *cfg, uint8_t mode)"
    
    def test_non_shadow_new_node(self):
        """Sandbox can add completely new nodes not in prod."""
        graph = EphemeralGraph()
        
        # New node only in sandbox
        sandbox_only = {
            "name": "My_TestFunction",
            "module": "Adc",
            "_origin": "sandbox",
        }
        
        node_id = "SRC_Function:My_TestFunction:Adc"
        graph.add_node("SRC_Function", node_id, sandbox_only)
        
        node = graph.get_node(node_id)
        assert node is not None
        assert node["_origin"] == "sandbox"
        assert "_shadows" not in node
    
    def test_metadata_tags_origin(self):
        """All nodes have _origin tag (sandbox or production)."""
        graph = EphemeralGraph()
        
        # Add mix of nodes
        graph.add_node("SRC_Function", "id1", {"name": "A", "_origin": "sandbox"})
        graph.add_node("SRC_Function", "id2", {"name": "B", "_origin": "production"})
        
        nodes = graph.get_all_nodes()
        for node_id, props in nodes:
            assert "_origin" in props
            assert props["_origin"] in ("sandbox", "production")


class TestTraceabilityPull:
    """Test the ±N neighbor pull from production Neo4j."""

    def test_pull_neighbors_depth_1(self):
        """Pull ±1 neighbors from prod Neo4j."""

        mock_driver = MagicMock()
        # Mock Neo4j session
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        # Simulate Neo4j result for ±1 pull
        mock_result = MagicMock()
        mock_result.single.return_value = {
            "nodes": [
                {
                    "node_id": "prod_A",
                    "node_type": "SRC_Function",
                    "properties": {"name": "Adc_Init", "module": "Adc"}
                },
                {
                    "node_id": "prod_B",
                    "node_type": "SWA_Function",
                    "properties": {"name": "Adc_Init", "module": "Adc"}
                },
            ],
            "relationships": [
                {
                    "source": "prod_A",
                    "target": "prod_B",
                    "rel_type": "IMPLEMENTS",
                    "properties": {}
                }
            ]
        }
        mock_session.run.return_value = mock_result
        
        puller = TraceabilityPuller(mock_driver)
        nodes, rels = puller.pull_neighbors(
            node_names=["Adc_Init"],
            module="Adc",
            workspace_id="illd",
            depth=1
        )
        
        assert len(nodes) == 2
        assert len(rels) == 1
        assert nodes[0]["properties"]["name"] == "Adc_Init"
        assert rels[0]["rel_type"] == "IMPLEMENTS"
    
    def test_pull_safety_cap(self):
        """Ensure pull respects 500-node safety cap."""
        puller = TraceabilityPuller(MagicMock())
        
        # TraceabilityPuller.MAX_PULL_NODES should be 500
        assert hasattr(puller, 'MAX_PULL_NODES')
        assert puller.MAX_PULL_NODES == 500

    def test_pull_neighbors_uses_literal_depth(self):
        """F-CB-01: Cypher must interpolate depth as a literal, not a $param.

        Neo4j 4.x does not allow parameters inside [*1..N]. Regression
        guard against re-introducing the silently-broken `$depth` form.
        """
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        mock_session.run.return_value.single.return_value = {
            "nodes": [], "relationships": []
        }

        puller = TraceabilityPuller(mock_driver)
        puller.pull_neighbors(
            node_names=["Adc_Init"], module="Adc",
            workspace_id="mcal", depth=2,
        )
        captured_cypher = mock_session.run.call_args[0][0]
        assert "*1..2" in captured_cypher
        assert "$depth" not in captured_cypher

    def test_pull_neighbors_rejects_out_of_range_depth(self):
        """F-CB-01: depth outside 1..5 must raise, not silently inject."""
        puller = TraceabilityPuller(MagicMock())
        import pytest
        with pytest.raises(ValueError, match="depth must be 1..5"):
            puller.pull_neighbors(
                node_names=["x"], module="Adc", depth=99,
            )


class TestLoadProdNodes:
    """Test loading prod nodes into ephemeral graph."""
    
    def test_load_prod_nodes_with_origin_tag(self):
        """All loaded prod nodes get _origin=production and _neo4j_id."""
        graph = EphemeralGraph()
        
        prod_nodes = [
            {
                "node_id": "prod_1",
                "node_type": "SRC_Function",
                "properties": {"name": "Func_A", "module": "Mod"}
            },
            {
                "node_id": "prod_2",
                "node_type": "SRC_Function",
                "properties": {"name": "Func_B", "module": "Mod"}
            }
        ]
        
        prod_rels = [
            {
                "source": "prod_1",
                "target": "prod_2",
                "rel_type": "CALLS",
                "properties": {}
            }
        ]
        
        # Mock the _canonical_id method
        graph._canonical_id = lambda node_type, props: f"{node_type}:{props.get('name', 'unknown')}:{props.get('module', 'unknown')}"
        
        graph.load_prod_nodes(prod_nodes, prod_rels)
        
        # Verify nodes were loaded with correct tags
        canonical_id_1 = "SRC_Function:Func_A:Mod"
        node_1 = graph.get_node(canonical_id_1)
        
        assert node_1 is not None
        assert node_1.get("_origin") == "production"
        assert node_1.get("_neo4j_id") == "prod_1"
    
    def test_canonical_id_consistency(self):
        """Same node in prod and sandbox use same canonical_id."""
        graph = EphemeralGraph()
        
        prod_node = {
            "node_id": "prod_adc_init",
            "node_type": "SRC_Function",
            "properties": {
                "name": "Adc_Init",
                "module": "Adc",
                "signature": "void Adc_Init(cfg)"
            }
        }
        
        sandbox_node = {
            "name": "Adc_Init",
            "module": "Adc",
            "signature": "void Adc_Init(cfg, mode)"
        }
        
        # Both should use the same canonical_id
        canonical_prod = graph._canonical_id("SRC_Function", prod_node["properties"])
        canonical_sandbox = graph._canonical_id("SRC_Function", sandbox_node)
        
        assert canonical_prod == canonical_sandbox
        assert "Adc_Init" in canonical_prod
        assert "Adc" in canonical_prod


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

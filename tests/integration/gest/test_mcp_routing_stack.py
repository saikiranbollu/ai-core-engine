"""
Integration tests — MCP routing stack
=======================================
Verifies that GEST components correctly wire through MCP bridge.
All external dependencies (MCP subprocess, Neo4j, Qdrant) are mocked.
"""

import importlib.util

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("domain_apps") is None,
    reason="domain_apps package is not part of this repository workspace",
)


class TestKgAdapterViaBridge:
    """MCPKgAdapter → MCPBridge wiring (mocked async client)."""

    @pytest.fixture
    def stack(self):
        from domain_apps.gest.mcp.bridge import MCPBridge
        from domain_apps.gest.database.mcp_kg_adapter import MCPKgAdapter

        bridge = MCPBridge(profile="illd")
        bridge._client = MagicMock()
        bridge._client.start = AsyncMock(return_value=None)
        bridge._client.stop = AsyncMock(return_value=None)
        bridge.start()

        kg = MCPKgAdapter(bridge=bridge, module="cxpi", profile="illd")
        yield {"bridge": bridge, "kg": kg, "client": bridge._client}
        bridge.stop()

    def test_get_function_dependencies_routes_through_bridge(self, stack):
        stack["client"].execute_cypher = AsyncMock(
            return_value={"rows": [{"function": "IfxCxpi_init", "depends_on": "IfxCxpi_config"}], "columns": ["function", "depends_on"]}
        )
        result = stack["kg"].get_function_dependencies("IfxCxpi_init")
        assert isinstance(result, list)
        assert len(result) == 1
        stack["client"].execute_cypher.assert_called_once()

    def test_get_struct_members_routes_through_bridge(self, stack):
        stack["client"].execute_cypher = AsyncMock(
            return_value={"rows": [{"name": "baudRate", "type": "uint32"}], "columns": ["name", "type"]}
        )
        result = stack["kg"].get_struct_members("IfxCxpi_Config")
        assert isinstance(result, list)
        assert len(result) == 1


class TestSessionAdapterViaBridge:
    """DomainSessionAdapter → MCPBridge wiring."""

    @pytest.fixture
    def stack(self):
        from domain_apps.gest.mcp.bridge import MCPBridge
        from memory.domain_session_adapter import DomainSessionAdapter

        bridge = MCPBridge(profile="illd")
        bridge._client = MagicMock()
        bridge._client.start = AsyncMock(return_value=None)
        bridge._client.stop = AsyncMock(return_value=None)
        bridge._client.session_start = AsyncMock(return_value={"status": "ok"})
        bridge._client.session_store = AsyncMock(return_value={"status": "ok"})
        bridge._client.session_retrieve = AsyncMock(return_value={"found": True, "value": "test"})
        bridge._client.session_end = AsyncMock(return_value={"session_id": "sid", "context_count": 0})
        bridge.start()

        adapter = DomainSessionAdapter(bridge=bridge, assistant_name="GEST", default_ttl=3600)
        yield {"bridge": bridge, "adapter": adapter, "client": bridge._client}
        bridge.stop()

    def test_session_lifecycle_routes_through_bridge(self, stack):
        adapter = stack["adapter"]
        client = stack["client"]

        sid = adapter.create_session(module="cxpi")
        assert sid.startswith("gest_")
        client.session_start.assert_called_once()

        adapter.store_key_value(sid, "test_key", "test_val")
        client.session_store.assert_called()

        summary = adapter.close_session(sid)
        client.session_end.assert_called_once()

    def test_store_rag_results_routes_through_bridge(self, stack):
        adapter = stack["adapter"]
        sid = adapter.create_session(module="cxpi")
        results = [
            {"metadata": {"function": "f1"}, "similarity": 0.9},
            {"metadata": {"function": "f2"}, "similarity": 0.8},
        ]
        count = adapter.store_rag_results(sid, results, "function")
        assert count == 2


class TestNoDirectImports:
    """Verify that MCP-routed components have no direct DB imports."""

    def test_kg_adapter_no_direct_neo4j_import(self):
        import importlib
        import domain_apps.gest.database.mcp_kg_adapter as mod
        source = importlib.util.find_spec(mod.__name__).origin
        with open(source, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Check only actual import lines, not docstring mentions
        import_lines = [l for l in lines if l.strip().startswith(("import ", "from "))]
        import_text = "\n".join(import_lines)
        assert "from neo4j" not in import_text
        assert "import neo4j" not in import_text
        assert "KnowledgeGraphQuerier" not in import_text

    def test_session_adapter_no_direct_wm_import(self):
        import importlib
        import memory.domain_session_adapter as mod
        source = importlib.util.find_spec(mod.__name__).origin
        with open(source, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Check only non-comment/non-docstring lines for direct imports
        code_lines = [l for l in lines if not l.strip().startswith('#') and not l.strip().startswith('"""') and not l.strip().startswith("'''")]
        code_text = ''.join(code_lines)
        assert "from memory.working_memory import" not in code_text
        assert "import WorkingMemoryManager" not in code_text

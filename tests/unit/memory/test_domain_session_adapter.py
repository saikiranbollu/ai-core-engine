"""
Unit tests — DomainSessionAdapter
===================================
Verifies the generic MCP-routed session adapter used by domain assistants.
All MCP bridge calls are mocked (no real subprocess).
"""

import pytest
from unittest.mock import MagicMock, patch, call

from src.MemoryLayer.memory.domain_session_adapter import DomainSessionAdapter, RAG_TYPE_MAP, KG_TYPE_MAP


@pytest.fixture
def mock_bridge():
    bridge = MagicMock()
    bridge.session_start.return_value = {"status": "ok", "session_id": "test-sid"}
    bridge.session_store.return_value = {"status": "ok"}
    bridge.session_retrieve.return_value = {"found": True, "value": "test-value"}
    bridge.session_end.return_value = {"session_id": "test-sid", "context_count": 5}
    return bridge


@pytest.fixture
def adapter(mock_bridge) -> DomainSessionAdapter:
    return DomainSessionAdapter(bridge=mock_bridge, assistant_name="GEST", default_ttl=3600)


# ── Session Lifecycle ───────────────────────────────────────────────────────

class TestSessionLifecycle:
    def test_create_session_returns_sid(self, adapter: DomainSessionAdapter):
        sid = adapter.create_session(module="cxpi")
        assert isinstance(sid, str)
        assert sid.startswith("gest_")

    def test_create_session_calls_bridge(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi", project="proj_a")
        mock_bridge.session_start.assert_called_once()
        args = mock_bridge.session_start.call_args
        assert args.kwargs["session_id"] == sid
        assert args.kwargs["assistant_name"] == "GEST"
        assert args.kwargs["module_context"] == "cxpi"

    def test_close_session_calls_bridge(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        summary = adapter.close_session(sid)
        mock_bridge.session_end.assert_called_once_with(sid)
        assert "session_id" in summary

    def test_list_active_sessions(self, adapter: DomainSessionAdapter):
        sid1 = adapter.create_session(module="cxpi")
        sid2 = adapter.create_session(module="cxpi")
        sessions = adapter.list_active_sessions()
        session_ids = [s["session_id"] for s in sessions]
        assert sid1 in session_ids
        assert sid2 in session_ids

    def test_bridge_failure_still_creates_local_session(self, mock_bridge):
        mock_bridge.session_start.side_effect = Exception("MCP down")
        adapter = DomainSessionAdapter(bridge=mock_bridge, assistant_name="GEST")
        sid = adapter.create_session(module="cxpi")
        assert sid.startswith("gest_")
        session_ids = [s["session_id"] for s in adapter.list_active_sessions()]
        assert sid in session_ids


# ── Store RAG Results ───────────────────────────────────────────────────────

class TestStoreRAGResults:
    def test_store_rag_results(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        results = [
            {"metadata": {"function": "IfxCxpi_init"}, "similarity": 0.95},
            {"metadata": {"function": "IfxCxpi_send"}, "similarity": 0.90},
        ]
        count = adapter.store_rag_results(sid, results, "function", query_text="init cxpi")
        assert count == 2
        mock_bridge.session_store.assert_called()

    def test_store_empty_results_returns_zero(self, adapter: DomainSessionAdapter):
        sid = adapter.create_session(module="cxpi")
        assert adapter.store_rag_results(sid, [], "function") == 0

    def test_rag_type_map_used(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        results = [{"metadata": {"name": "TestStruct"}, "similarity": 0.8}]
        adapter.store_rag_results(sid, results, "struct")
        store_call = mock_bridge.session_store.call_args_list[-1]
        key = store_call.args[1] if len(store_call.args) > 1 else store_call.kwargs.get("key")
        assert key == "rag_struct"

    def test_store_rag_results_preserves_explicit_node_type(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        results = [{"node_type": "APIFunction", "name": "IfxCxpi_init", "similarity": 0.95}]

        adapter.store_rag_results(sid, results, "function")

        entries = mock_bridge.session_store.call_args_list[-1].args[2]
        assert entries[0]["node_type"] == "APIFunction"


# ── Store KG Results ────────────────────────────────────────────────────────

class TestStoreKGResults:
    def test_store_kg_results(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        results = [
            {"function_name": "IfxCxpi_init", "depends_on": "IfxCxpi_config"},
        ]
        count = adapter.store_kg_results(sid, results, "dependency")
        assert count == 1

    def test_store_single_dict_result(self, adapter: DomainSessionAdapter):
        sid = adapter.create_session(module="cxpi")
        result = {"function_name": "IfxCxpi_init"}
        count = adapter.store_kg_results(sid, result, "function")
        assert count == 1

    def test_store_kg_results_preserves_explicit_node_type(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        results = [{"node_type": "SoftwareRequirement", "node_id": "REQ-1", "name": "Init requirement"}]

        adapter.store_kg_results(sid, results, "requirement")

        entries = mock_bridge.session_store.call_args_list[-1].args[2]
        assert entries[0]["node_type"] == "SoftwareRequirement"


# ── Store Key-Value ─────────────────────────────────────────────────────────

class TestStoreKeyValue:
    def test_store_key_value(self, adapter: DomainSessionAdapter, mock_bridge):
        sid = adapter.create_session(module="cxpi")
        adapter.store_key_value(sid, "my_key", {"data": 123})
        mock_bridge.session_store.assert_called()


# ── Type Map Constants ──────────────────────────────────────────────────────

class TestTypeMaps:
    def test_rag_type_map_has_expected_keys(self):
        for key in ["function", "struct", "enum", "requirement", "register"]:
            assert key in RAG_TYPE_MAP

    def test_kg_type_map_has_expected_keys(self):
        for key in ["dependency", "function", "struct_member", "enum_value", "parameter"]:
            assert key in KG_TYPE_MAP

    def test_type_maps_use_canonical_labels(self):
        assert RAG_TYPE_MAP["function"] == "APIFunction"
        assert RAG_TYPE_MAP["struct"] == "DataStructure"
        assert RAG_TYPE_MAP["requirement"] == "SoftwareRequirement"
        assert KG_TYPE_MAP["function"] == "APIFunction"
        assert KG_TYPE_MAP["dependency"] == "APIFunction"
        assert KG_TYPE_MAP["requirement"] == "SoftwareRequirement"

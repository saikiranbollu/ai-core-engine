import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from src.MemoryLayer.memory.ephemeral_sandbox import EphemeralGraph, EphemeralSandbox, EphemeralVectors, _FallbackEmbedder
import mcp.core.mcp_server as mod


@pytest.fixture
def sandbox_ctx():
    sandbox = EphemeralSandbox(
        session_id="sandbox-query-test",
        graph=EphemeralGraph(),
        vectors=EphemeralVectors("sandbox-query-test", _FallbackEmbedder()),
    )
    sandbox.graph.add_node("HardwareRegister", "HardwareRegister:CLC:ADC", {
        "name": "CLC",
        "module": "ADC",
        "access": "rw",
        "_origin": "sandbox",
    })
    sandbox.graph.add_node("HardwareRegister", "HardwareRegister:CLC:CAN", {
        "name": "CLC",
        "module": "CAN",
        "access": "ro",
        "_origin": "sandbox",
    })
    sandbox.graph.add_node("RegisterField", "RegisterField:REGFIELD_ADC_CLC_DISR:ADC", {
        "name": "DISR",
        "module": "ADC",
        "register": "CLC",
        "_origin": "sandbox",
    })
    return sandbox


def test_search_database_sandbox_passes_filter_by_module(monkeypatch):
    monkeypatch.setattr(mod, "_authorize", lambda *args, **kwargs: None)
    graph_service = MagicMock()
    graph_service.search.return_value = []

    asyncio.run(
        mod.search_database(
            query="find registers",
            max_results=5,
            filter_by_module="ADC",
            query_mode="sandbox",
            graph_service=graph_service,
        )
    )

    graph_service.search.assert_called_once_with(
        "find registers",
        top_k=5,
        alpha=mod._DEFAULT_SEARCH_ALPHA,
        filter_by_module="ADC",
    )


def test_search_database_sandbox_applies_node_type_filter_and_offset(monkeypatch):
    monkeypatch.setattr(mod, "_authorize", lambda *args, **kwargs: None)
    graph_service = MagicMock()
    graph_service.search.return_value = [
        MagicMock(node_id="1", content="macro", score=0.9, origin="sandbox", node_type="Macro", metadata={}),
        MagicMock(node_id="2", content="func", score=0.8, origin="sandbox", node_type="Function", metadata={}),
        MagicMock(node_id="3", content="macro-2", score=0.7, origin="sandbox", node_type="Macro", metadata={}),
    ]

    raw = asyncio.run(
        mod.search_database(
            query="find macros",
            max_results=1,
            offset=1,
            filter_by_node_type=["Macro"],
            query_mode="sandbox",
            graph_service=graph_service,
        )
    )
    result = json.loads(raw)

    assert result["error"] is False
    assert result["data"]["total_count"] == 2
    assert len(result["data"]["results"]) == 1
    assert result["data"]["results"][0]["node_id"] == "3"


def test_search_nodes_sandbox_honors_label_filters_and_offset(monkeypatch, sandbox_ctx):
    monkeypatch.setattr(mod, "_authorize", lambda *args, **kwargs: None)

    raw = asyncio.run(
        mod.search_nodes(
            label="HardwareRegister",
            keyword=None,
            filters={"module": "ADC"},
            return_properties=["name", "module"],
            limit=10,
            offset=0,
            query_mode="sandbox",
            sandbox_ctx=sandbox_ctx,
        )
    )
    result = json.loads(raw)

    assert result["error"] is False
    nodes = result["data"]["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "HardwareRegister:CLC:ADC"
    assert nodes[0]["properties"] == {"name": "CLC", "module": "ADC"}


def test_search_nodes_sandbox_keyword_filters_without_prod_service(monkeypatch, sandbox_ctx):
    monkeypatch.setattr(mod, "_authorize", lambda *args, **kwargs: None)

    raw = asyncio.run(
        mod.search_nodes(
            label="RegisterField",
            keyword="DISR",
            filters={"module": "ADC"},
            limit=10,
            offset=0,
            query_mode="sandbox",
            sandbox_ctx=sandbox_ctx,
        )
    )
    result = json.loads(raw)

    assert result["error"] is False
    nodes = result["data"]["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["label"] == "RegisterField"
    assert nodes[0]["properties"]["name"] == "DISR"


def test_execute_cypher_sandbox_uses_hybrid_deep_query(monkeypatch):
    monkeypatch.setattr(mod, "_authorize", lambda *args, **kwargs: None)
    graph_service = MagicMock()
    graph_service.deep_query.return_value = [{"name": "IFX_ADC_MAX_CH", "module": "ADC", "_origin": "sandbox"}]

    raw = asyncio.run(
        mod.execute_cypher(
            query="MATCH (m:Macro) WHERE m.module = $mod RETURN m",
            parameters={"mod": "ADC"},
            query_mode="hybrid",
            graph_service=graph_service,
            sandbox_ctx=object(),
        )
    )
    result = json.loads(raw)

    assert result["error"] is False
    graph_service.deep_query.assert_called_once_with(
        cypher="MATCH (m:Macro) WHERE m.module = $mod RETURN m",
        params={"mod": "ADC"},
        workspace_id="illd",
    )
    assert result["data"]["_origin"] == "sandbox"
    assert result["data"]["count"] == 1


def test_canonicalize_illd_module_maps_regdef_submodules_to_parent():
    assert mod._canonicalize_illd_module("PMSCORE") == "PMS"
    assert mod._canonicalize_illd_module(filename="IfxPmsmon_regdef.h") == "PMS"
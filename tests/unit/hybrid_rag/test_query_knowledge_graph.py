import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
# _kg_safety lives alongside query_knowledge_graph.py in the KG directory
sys.path.insert(0, str(_ROOT / "src" / "HybridRAG" / "code" / "KG"))

try:
    from src.HybridRAG.code.KG.query_knowledge_graph import KnowledgeGraphQuerier
except (ImportError, ModuleNotFoundError) as _err:
    pytest.skip(f"Cannot import KnowledgeGraphQuerier: {_err}", allow_module_level=True)


def _make_querier() -> KnowledgeGraphQuerier:
    ontology = {
        "profiles": {
            "illd": {
                "node_types": [
                    {"name": "Function", "properties": [{"name": "function_name", "unique": True}]},
                    {"name": "Requirement", "properties": [{"name": "id", "unique": True}]},
                    {"name": "StakeholderRequirement", "properties": [{"name": "requirement_id", "unique": True}]},
                ],
                "relationship_types": [],
            }
        }
    }
    storage_cfg = {
        "neo4j": {
            "illd": {
                "uri": "bolt://example",
                "username": "neo4j",
                "password": "test",
                "database": "neo4j",
            }
        }
    }
    return KnowledgeGraphQuerier(profile="illd", ontology=ontology, storage_cfg=storage_cfg)


def test_search_nodes_uses_alias_labels_for_canonical_function():
    querier = _make_querier()
    captured = {}

    def fake_run(cypher, parameters=None):
        captured["cypher"] = cypher
        captured["parameters"] = parameters
        return []

    querier.run = fake_run

    querier.search_nodes("APIFunction", keyword="init")

    assert "labels(n)" in captured["cypher"]
    assert captured["parameters"]["labels"] == ["APIFunction", "Function"]


def test_get_module_details_uses_module_property_fallback_when_no_module_node():
    querier = _make_querier()
    calls = []

    def fake_run(cypher, parameters=None):
        calls.append((cypher, parameters or {}))
        if "RETURN m" in cypher:
            return []
        if "RETURN lbl, count(*) AS cnt" in cypher:
            return [{"lbl": "SoftwareRequirement", "cnt": 2}]
        if "RETURN n.status AS status" in cypher:
            return [{"status": "Approved", "cnt": 2}]
        if "RETURN n.requirement_category AS category" in cypher:
            return [{"category": "Safety", "cnt": 2}]
        return []

    querier.run = fake_run

    result = querier.get_module_details("cxpi")

    assert result["module"]["module_name"] == "CXPI"
    assert any("coalesce(n.module, n.module_name, n.module_prefix" in cypher for cypher, _ in calls)


def test_get_coverage_report_uses_requirement_aliases():
    querier = _make_querier()
    calls = []

    def fake_run(cypher, parameters=None):
        calls.append((cypher, parameters or {}))
        return [{"cnt": 1}]

    querier.run = fake_run

    report = querier.get_coverage_report()

    assert report["total_prq"] == 1
    assert all(params.get("requirement_labels") == ["ProductRequirement", "SoftwareRequirement", "Requirement"] for _, params in calls[:4])
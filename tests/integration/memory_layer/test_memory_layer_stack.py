"""
Integration tests — Memory Layer stack
========================================
Tests OntologyLoader → WorkingMemoryManager → Session working together
end-to-end (no external services, all in-memory).
"""

import time
import pytest
from pathlib import Path

from memory.ontology_loader import OntologyLoader
from memory.working_memory.manager import WorkingMemoryManager, InMemoryBackend
from memory.working_memory.session import SessionExpiredError

_ONTOLOGY_PATH = Path(__file__).resolve().parents[3] / "src" / "HybridRAG" / "config" / "ontology.yaml"


@pytest.fixture(scope="module")
def ontology() -> OntologyLoader:
    return OntologyLoader(str(_ONTOLOGY_PATH))


@pytest.fixture
def wm(ontology: OntologyLoader) -> WorkingMemoryManager:
    return WorkingMemoryManager(
        ontology=ontology,
        profile="illd",
        backend=InMemoryBackend(),
        default_ttl_seconds=3600,
    )


class TestFullSessionWorkflow:
    """Simulates a complete session lifecycle matching what the GEST pipeline does."""

    def test_create_populate_query_close(self, wm: WorkingMemoryManager):
        # 1. Create session
        sid = wm.create_session(project="demo", module="cxpi", metadata={"test": True})
        assert sid

        # 2. Store RAG-style context (Function nodes from Qdrant)
        for i, func_name in enumerate(["IfxCxpi_initChannel", "IfxCxpi_sendMessage", "IfxCxpi_getStatus"]):
            wm.add_context(
                session_id=sid,
                node_type="Function",
                node_id=func_name,
                data={"function_name": func_name, "module": "cxpi"},
                source="qdrant",
                relevance_score=0.95 - (i * 0.05),
                query_text="initialise cxpi channel",
            )

        # 3. Store KG-style context (Struct nodes from Neo4j)
        wm.add_context(
            session_id=sid,
            node_type="Struct",
            node_id="IfxCxpi_Cxpi_Config",
            data={"name": "IfxCxpi_Cxpi_Config", "members": ["baudRate", "channel"]},
            source="neo4j",
        )

        # 4. Store key-value data (generation metadata)
        wm.store_data(sid, "generation_step", "rag_query_complete")
        wm.store_data(sid, "description", "Initialize CXPI channel")

        # 5. Query back context by type
        functions = wm.get_context(sid, node_type="Function")
        assert len(functions) == 3

        structs = wm.get_context(sid, node_type="Struct")
        assert len(structs) == 1
        assert structs[0].node_id == "IfxCxpi_Cxpi_Config"

        # 6. Query by source
        qdrant_entries = wm.get_context(sid, source="qdrant")
        assert len(qdrant_entries) == 3

        neo4j_entries = wm.get_context(sid, source="neo4j")
        assert len(neo4j_entries) == 1

        # 7. Retrieve key-value data
        assert wm.retrieve_data(sid, "generation_step") == "rag_query_complete"
        assert wm.retrieve_data(sid, "description") == "Initialize CXPI channel"

        # 8. Close session
        assert wm.close_session(sid) is True
        assert wm.get_session(sid) is None


class TestMultipleConcurrentSessions:
    """Tests that multiple sessions can coexist without interference."""

    def test_two_sessions_isolated(self, wm: WorkingMemoryManager):
        sid1 = wm.create_session(project="p1", module="cxpi")
        sid2 = wm.create_session(project="p2", module="cxpi")

        wm.add_context(sid1, "Function", "func_A", {"name": "A"}, "neo4j")
        wm.add_context(sid2, "Function", "func_B", {"name": "B"}, "neo4j")

        entries1 = wm.get_context(sid1)
        entries2 = wm.get_context(sid2)

        assert len(entries1) == 1
        assert entries1[0].node_id == "func_A"
        assert len(entries2) == 1
        assert entries2[0].node_id == "func_B"

        wm.close_session(sid1)
        wm.close_session(sid2)


class TestOntologyDrivenValidation:
    """Tests that ontology constraints are enforced by the full stack."""

    def test_valid_node_types_from_ontology(self, wm: WorkingMemoryManager, ontology: OntologyLoader):
        expected = set(ontology.get_node_type_names("illd"))
        actual = set(wm.valid_node_types)
        assert expected == actual

    def test_add_context_with_valid_type(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        # Should not raise
        wm.add_context(sid, "Function", "f1", {}, "neo4j")
        entries = wm.get_context(sid)
        assert len(entries) == 1
        wm.close_session(sid)


class TestSessionAdapter:
    """Tests DomainSessionAdapter with a real (mocked-bridge) flow."""

    def test_adapter_tracks_context_count(self):
        from unittest.mock import MagicMock
        from memory.domain_session_adapter import DomainSessionAdapter

        bridge = MagicMock()
        bridge.session_start.return_value = {"status": "ok"}
        bridge.session_store.return_value = {"status": "ok"}
        bridge.session_end.return_value = {}

        adapter = DomainSessionAdapter(bridge=bridge, assistant_name="GEST")
        sid = adapter.create_session(module="cxpi")

        # Store RAG results
        rag = [
            {"metadata": {"function": "IfxCxpi_init"}, "similarity": 0.95},
            {"metadata": {"function": "IfxCxpi_send"}, "similarity": 0.90},
        ]
        count = adapter.store_rag_results(sid, rag, "function", "init cxpi")
        assert count == 2

        # Check local tracking
        summary = adapter.get_session_summary(sid)
        assert summary is not None
        assert summary["context_count"] == 2

        adapter.close_session(sid)

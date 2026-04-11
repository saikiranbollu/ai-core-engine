"""
Unit tests — WorkingMemoryManager
==================================
Verifies session lifecycle, context management, TTL enforcement,
store_data / retrieve_data, and session listing/purging.
"""

import time
import pytest
from pathlib import Path

from src.MemoryLayer.memory.ontology_loader import OntologyLoader
from src.MemoryLayer.memory.working_memory.manager import WorkingMemoryManager, InMemoryBackend
from src.MemoryLayer.memory.working_memory.session import Session, ContextEntry, SessionExpiredError

_ONTOLOGY_PATH = Path(__file__).resolve().parents[3] / "src" / "HybridRAG" / "config" / "ontology.yaml"


@pytest.fixture
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


# ── Session Lifecycle ───────────────────────────────────────────────────────

class TestSessionLifecycle:
    def test_create_session_returns_id(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="proj_a", module="cxpi")
        assert isinstance(sid, str)
        assert len(sid) > 0

    def test_get_session_returns_session(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="proj_a", module="cxpi")
        session = wm.get_session(sid)
        assert session is not None
        assert session.project == "proj_a"
        assert session.module == "cxpi"

    def test_close_session(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="proj_a", module="cxpi")
        assert wm.close_session(sid) is True
        assert wm.get_session(sid) is None

    def test_close_nonexistent_session(self, wm: WorkingMemoryManager):
        assert wm.close_session("nonexistent-id") is False

    def test_session_metadata(self, wm: WorkingMemoryManager):
        sid = wm.create_session(
            project="proj_a", module="cxpi",
            metadata={"description": "test session"}
        )
        session = wm.get_session(sid)
        assert session.metadata.get("description") == "test session"


# ── Context Management ──────────────────────────────────────────────────────

class TestContextManagement:
    def test_add_and_get_context(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.add_context(
            session_id=sid,
            node_type="Function",
            node_id="IfxCxpi_initChannel",
            data={"function_name": "IfxCxpi_initChannel", "return_type": "void"},
            source="neo4j",
        )
        entries = wm.get_context(sid)
        assert len(entries) == 1
        assert entries[0].node_id == "IfxCxpi_initChannel"
        assert entries[0].node_type == "Function"

    def test_filter_by_node_type(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.add_context(sid, "Function", "f1", {"name": "f1"}, "neo4j")
        wm.add_context(sid, "Struct", "s1", {"name": "s1"}, "neo4j")
        funcs = wm.get_context(sid, node_type="Function")
        assert len(funcs) == 1
        assert funcs[0].node_id == "f1"

    def test_filter_by_source(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.add_context(sid, "Function", "f1", {}, "neo4j")
        wm.add_context(sid, "Function", "f2", {}, "qdrant")
        qdrant_entries = wm.get_context(sid, source="qdrant")
        assert len(qdrant_entries) == 1
        assert qdrant_entries[0].node_id == "f2"

    def test_clear_context(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.add_context(sid, "Function", "f1", {}, "neo4j")
        wm.add_context(sid, "Function", "f2", {}, "neo4j")
        count = wm.clear_context(sid)
        assert count == 2
        assert len(wm.get_context(sid)) == 0

    def test_context_on_nonexistent_session_raises(self, wm: WorkingMemoryManager):
        with pytest.raises(ValueError):
            wm.add_context("no-such-id", "Function", "f1", {}, "neo4j")


# ── Store / Retrieve Data (Key-Value) ──────────────────────────────────────

class TestKeyValueStorage:
    def test_store_and_retrieve(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.store_data(sid, "my_key", {"hello": "world"})
        val = wm.retrieve_data(sid, "my_key")
        assert val == {"hello": "world"}

    def test_retrieve_missing_key_returns_none(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        assert wm.retrieve_data(sid, "nonexistent") is None

    def test_store_overwrites_previous(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi")
        wm.store_data(sid, "k", "v1")
        wm.store_data(sid, "k", "v2")
        assert wm.retrieve_data(sid, "k") == "v2"

    def test_store_on_nonexistent_session_raises(self, wm: WorkingMemoryManager):
        with pytest.raises(ValueError):
            wm.store_data("no-such-id", "k", "v")


# ── TTL and Expiration ──────────────────────────────────────────────────────

class TestTTL:
    def test_session_with_short_ttl_expires(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi", ttl_seconds=1)
        time.sleep(1.5)
        session = wm.get_session(sid)
        assert session is None

    def test_expired_session_raises_on_context(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi", ttl_seconds=1)
        time.sleep(1.5)
        with pytest.raises(SessionExpiredError):
            wm.get_context(sid)

    def test_expired_session_raises_on_store_data(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi", ttl_seconds=1)
        time.sleep(1.5)
        with pytest.raises(SessionExpiredError):
            wm.store_data(sid, "k", "v")

    def test_extend_session(self, wm: WorkingMemoryManager):
        sid = wm.create_session(project="p", module="cxpi", ttl_seconds=2)
        assert wm.extend_session(sid, 3600) is True
        session = wm.get_session(sid)
        assert session.ttl_seconds == 3602

    def test_purge_expired(self, wm: WorkingMemoryManager):
        sid1 = wm.create_session(project="p", module="cxpi", ttl_seconds=1)
        sid2 = wm.create_session(project="p", module="cxpi", ttl_seconds=3600)
        time.sleep(1.5)
        purged = wm.purge_expired_sessions()
        assert purged == 1
        assert wm.get_session(sid1) is None
        assert wm.get_session(sid2) is not None


# ── Listing ─────────────────────────────────────────────────────────────────

class TestSessionListing:
    def test_list_active_sessions(self, wm: WorkingMemoryManager):
        wm.create_session(project="p1", module="cxpi")
        wm.create_session(project="p2", module="cxpi")
        active = wm.list_active_sessions()
        assert len(active) >= 2

    def test_list_filtered_by_project(self, wm: WorkingMemoryManager):
        wm.create_session(project="alpha", module="cxpi")
        wm.create_session(project="beta", module="cxpi")
        alpha = wm.list_active_sessions(project="alpha")
        assert all(s["project"] == "alpha" for s in alpha)

    def test_list_filtered_by_module(self, wm: WorkingMemoryManager):
        wm.create_session(project="p", module="cxpi")
        cxpi = wm.list_active_sessions(module="cxpi")
        assert all(s["module"] == "cxpi" for s in cxpi)


# ── Ontology Validation ────────────────────────────────────────────────────

class TestOntologyValidation:
    def test_valid_node_types_loaded(self, wm: WorkingMemoryManager):
        assert len(wm.valid_node_types) > 0
        assert "Function" in wm.valid_node_types

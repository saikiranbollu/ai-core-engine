"""
Tests for Working Memory (Tickets 1 + 2)
==========================================
All tests use InMemoryBackend so no Neo4j, Redis, or Qdrant is needed.
Everything dynamic is driven through a real ontology.yaml read.

Test groups
-----------
TestSessionDataclass        — ContextEntry + Session unit tests
TestWorkingMemoryManager    — manager CRUD, validation, purge
TestTTLBehaviour            — expiry, extend_session (Ticket 2 TTL management)
TestOntologyValidation      — node_type and module validated against ontology
"""

import sys
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ── make the MEMORY_LAYER root importable ────────────────────────────────────
TEST_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_LAYER_ROOT = Path(__file__).resolve().parents[4] / "src" / "MemoryLayer"
sys.path.insert(0, str(MEMORY_LAYER_ROOT))

from memory.ontology_loader import OntologyLoader
from memory.working_memory  import (
    WorkingMemoryManager,
    Session,
    ContextEntry,
    SessionExpiredError,
    InMemoryBackend,
)

# ── shared ontology fixture ──────────────────────────────────────────────────
# Point to canonical location: ai-core-engine/src/HybridRAG/config/ontology.yaml
ONTOLOGY_PATH = Path(__file__).resolve().parents[3] / "src" / "HybridRAG" / "config" / "ontology.yaml"


def _make_ontology() -> OntologyLoader:
    return OntologyLoader(str(ONTOLOGY_PATH))


def _make_manager(profile="illd") -> WorkingMemoryManager:
    return WorkingMemoryManager(
        ontology=_make_ontology(),
        profile=profile,
        backend=InMemoryBackend(),
        default_ttl_seconds=3600,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. SESSION DATACLASS TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionDataclass(unittest.TestCase):

    def setUp(self):
        self.session = Session(
            project="proj_a",
            module="cxpi",
            profile="illd",
            ttl_seconds=10,
        )

    def test_session_id_is_uuid_string(self):
        import uuid
        uuid.UUID(self.session.session_id)  # must not raise

    def test_not_expired_immediately(self):
        self.assertFalse(self.session.is_expired)

    def test_remaining_seconds_positive(self):
        self.assertGreater(self.session.remaining_seconds, 0)

    def test_expires_at_in_future(self):
        self.assertGreater(self.session.expires_at, datetime.now(timezone.utc))

    def test_add_entry_increases_context(self):
        entry = ContextEntry(node_type="Function", node_id="fn1", data={"x": 1})
        self.session.add_entry(entry)
        self.assertEqual(len(self.session.context), 1)

    def test_get_entries_filter_by_node_type(self):
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn1", data={}))
        self.session.add_entry(ContextEntry(node_type="TestCode", node_id="tc1", data={}))
        result = self.session.get_entries(node_type="Function")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].node_type, "Function")

    def test_get_entries_filter_by_source(self):
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn1",
                                            data={}, source="neo4j"))
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn2",
                                            data={}, source="qdrant"))
        result = self.session.get_entries(source="qdrant")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].node_id, "fn2")

    def test_get_entries_filter_by_min_score(self):
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn1",
                                            data={}, relevance_score=0.9))
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn2",
                                            data={}, relevance_score=0.3))
        result = self.session.get_entries(min_score=0.5)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].relevance_score, 0.9)

    def test_clear_context_returns_count(self):
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn1", data={}))
        self.session.add_entry(ContextEntry(node_type="TestCode", node_id="tc1", data={}))
        count = self.session.clear_context()
        self.assertEqual(count, 2)
        self.assertEqual(len(self.session.context), 0)

    def test_get_node_type_counts(self):
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn1", data={}))
        self.session.add_entry(ContextEntry(node_type="Function", node_id="fn2", data={}))
        self.session.add_entry(ContextEntry(node_type="TestCode", node_id="tc1", data={}))
        counts = self.session.get_node_type_counts()
        self.assertEqual(counts["Function"], 2)
        self.assertEqual(counts["TestCode"], 1)

    def test_to_dict_keys(self):
        d = self.session.to_dict()
        for key in ("session_id", "project", "module", "profile",
                    "ttl_seconds", "created_at", "last_accessed",
                    "is_expired", "expires_at", "context_count"):
            self.assertIn(key, d, f"Missing key: {key}")

    def test_expired_session_raises(self):
        fast = Session(project="p", module="m", profile="illd", ttl_seconds=0)
        time.sleep(0.05)
        with self.assertRaises(SessionExpiredError):
            fast.add_entry(ContextEntry(node_type="Function", node_id="fn1", data={}))

    def test_touch_updates_last_accessed(self):
        original = self.session.last_accessed
        time.sleep(0.02)
        self.session.touch()
        self.assertGreater(self.session.last_accessed, original)


# ─────────────────────────────────────────────────────────────────────────────
# 2. WORKING MEMORY MANAGER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkingMemoryManager(unittest.TestCase):

    def setUp(self):
        self.mgr = _make_manager()

    def test_create_session_returns_string(self):
        sid = self.mgr.create_session(project="proj_a", module="cxpi")
        self.assertIsInstance(sid, str)
        self.assertTrue(len(sid) > 10)

    def test_get_session_returns_session_object(self):
        sid     = self.mgr.create_session(project="proj_a", module="cxpi")
        session = self.mgr.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session.project, "proj_a")
        self.assertEqual(session.module, "cxpi")

    def test_project_and_module_stored_lowercase(self):
        sid     = self.mgr.create_session(project="PrOJ_A", module="CXPI")
        session = self.mgr.get_session(sid)
        self.assertEqual(session.project, "proj_a")
        self.assertEqual(session.module,  "cxpi")

    def test_profile_matches_manager_profile(self):
        sid     = self.mgr.create_session(project="proj_a", module="cxpi")
        session = self.mgr.get_session(sid)
        self.assertEqual(session.profile, "illd")

    def test_close_session_returns_true(self):
        sid = self.mgr.create_session(project="proj_a", module="cxpi")
        ok  = self.mgr.close_session(sid)
        self.assertTrue(ok)

    def test_close_unknown_session_returns_false(self):
        ok = self.mgr.close_session("no-such-id")
        self.assertFalse(ok)

    def test_add_context_stores_entry(self):
        sid = self.mgr.create_session(project="proj_a", module="cxpi")
        self.mgr.add_context(
            session_id=sid,
            node_type="Function",
            node_id="IfxCxpi_initChannel",
            data={"function_name": "IfxCxpi_initChannel"},
            source="neo4j",
        )
        entries = self.mgr.get_context(sid, node_type="Function")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].node_id, "IfxCxpi_initChannel")

    def test_add_context_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_context(
                session_id="ghost",
                node_type="Function",
                node_id="fn",
                data={},
            )

    def test_get_context_returns_empty_list_for_no_match(self):
        sid = self.mgr.create_session(project="proj_a", module="cxpi")
        result = self.mgr.get_context(sid, node_type="Register")
        self.assertEqual(result, [])

    def test_clear_context(self):
        sid = self.mgr.create_session(project="proj_a", module="cxpi")
        self.mgr.add_context(sid, "Function", "fn1", {})
        self.mgr.add_context(sid, "Function", "fn2", {})
        count = self.mgr.clear_context(sid)
        self.assertEqual(count, 2)
        self.assertEqual(self.mgr.get_context(sid), [])

    def test_list_active_sessions_excludes_expired(self):
        sid_live    = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=3600)
        sid_fast    = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=0)
        time.sleep(0.05)
        live_sessions = self.mgr.list_active_sessions(project="p")
        ids = [s["session_id"] for s in live_sessions]
        self.assertIn(sid_live, ids)
        self.assertNotIn(sid_fast, ids)

    def test_list_active_sessions_filter_by_module(self):
        sid_cxpi = self.mgr.create_session(project="p", module="cxpi")
        sid_can  = self.mgr.create_session(project="p", module="can")
        cxpi_sessions = self.mgr.list_active_sessions(module="cxpi")
        ids = [s["session_id"] for s in cxpi_sessions]
        self.assertIn(sid_cxpi, ids)
        self.assertNotIn(sid_can, ids)

    def test_purge_expired_removes_dead_sessions(self):
        self.mgr.create_session(project="p", module="cxpi", ttl_seconds=0)
        self.mgr.create_session(project="p", module="cxpi", ttl_seconds=0)
        time.sleep(0.05)
        purged = self.mgr.purge_expired_sessions()
        self.assertGreaterEqual(purged, 2)

    def test_get_session_summary_returns_dict(self):
        sid     = self.mgr.create_session(project="proj_a", module="cxpi")
        summary = self.mgr.get_session_summary(sid)
        self.assertIsNotNone(summary)
        self.assertIn("session_id", summary)
        self.assertIn("context_count", summary)

    def test_valid_node_types_non_empty(self):
        self.assertGreater(len(self.mgr.valid_node_types), 0)

    def test_valid_modules_non_empty(self):
        self.assertGreater(len(self.mgr.valid_modules), 0)

    def test_profile_property(self):
        self.assertEqual(self.mgr.profile, "illd")


# ─────────────────────────────────────────────────────────────────────────────
# 3. TTL MANAGEMENT TESTS (Ticket 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestTTLBehaviour(unittest.TestCase):

    def setUp(self):
        self.mgr = _make_manager()

    def test_session_expires_after_ttl(self):
        sid     = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=0)
        time.sleep(0.05)
        # get_session auto-purges expired sessions and returns None
        session = self.mgr.get_session(sid)
        self.assertIsNone(session, "Expired session should be auto-purged and return None")

    def test_add_context_to_expired_raises(self):
        sid = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=0)
        time.sleep(0.05)
        with self.assertRaises(SessionExpiredError):
            self.mgr.add_context(sid, "Function", "fn", {})

    def test_extend_session_increases_ttl(self):
        sid     = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=60)
        ok      = self.mgr.extend_session(sid, extra_seconds=120)
        self.assertTrue(ok)
        session = self.mgr.get_session(sid)
        self.assertEqual(session.ttl_seconds, 180)

    def test_extend_unknown_session_returns_false(self):
        ok = self.mgr.extend_session("ghost", extra_seconds=60)
        self.assertFalse(ok)

    def test_custom_ttl_respected(self):
        sid     = self.mgr.create_session(project="p", module="cxpi", ttl_seconds=9999)
        session = self.mgr.get_session(sid)
        self.assertEqual(session.ttl_seconds, 9999)


# ─────────────────────────────────────────────────────────────────────────────
# 4. ONTOLOGY VALIDATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestOntologyValidation(unittest.TestCase):
    """
    Confirm that the manager reads its valid types and modules from the
    ontology — not from any hardcoded list.
    """

    def setUp(self):
        self.ontology = _make_ontology()
        self.mgr      = _make_manager(profile="illd")

    def test_valid_node_types_come_from_ontology(self):
        expected = set(self.ontology.get_node_type_names("illd"))
        actual   = set(self.mgr.valid_node_types)
        self.assertEqual(expected, actual)

    def test_valid_modules_come_from_ontology(self):
        expected = {m.lower() for m in self.ontology.get_supported_modules("illd")}
        actual   = set(self.mgr.valid_modules)
        self.assertEqual(expected, actual)

    def test_mcal_profile_has_different_node_types(self):
        mgr_mcal  = _make_manager(profile="mcal")
        mgr_illd  = _make_manager(profile="illd")
        self.assertNotEqual(
            set(mgr_mcal.valid_node_types),
            set(mgr_illd.valid_node_types),
        )

    def test_unknown_node_type_still_stored(self):
        """Unknown types log a warning but are not rejected — partial ontology support."""
        sid = self.mgr.create_session(project="p", module="cxpi")
        # Should not raise, even though 'UnknownType' is not in ontology
        self.mgr.add_context(sid, "UnknownType", "node1", {"data": 1})
        entries = self.mgr.get_context(sid)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].node_type, "UnknownType")

    def test_metadata_stored_on_session(self):
        sid     = self.mgr.create_session(
            project="p", module="cxpi",
            metadata={"source_ticket": "AICE-001"}
        )
        session = self.mgr.get_session(sid)
        self.assertEqual(session.metadata["source_ticket"], "AICE-001")


if __name__ == "__main__":
    unittest.main(verbosity=2)

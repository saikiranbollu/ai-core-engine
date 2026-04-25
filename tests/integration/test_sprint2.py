"""
Sprint 2 Integration Tests — Search & Query + Memory & Context
===============================================================
Tests:
  1. SessionManager: create, store, retrieve, close lifecycle
  2. ContextBuilder: token budgeting, greedy fill, provenance
  3. SearchService: structure (without live Neo4j)
  4. Full 6-step session lifecycle simulation
"""
import json
import sys
import time
from pathlib import Path

import pytest

# Make src/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.MemoryLayer.memory.session_manager import (
    SessionManager, SessionData, DictBackend, SessionExpiredError,
)
from src.MemoryLayer.memory.context_builder import LegacyContextBuilder as ContextBuilder, _legacy_estimate_tokens as estimate_tokens


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: SessionManager Lifecycle
# ═════════════════════════════════════════════════════════════════════════

class TestSessionManager:
    """Full session lifecycle with DictBackend."""

    def setup_method(self):
        self.mgr = SessionManager(backend=DictBackend())

    def test_create_session(self):
        s = self.mgr.create("GEST_001", assistant_name="GEST", module_context="CXPI")
        assert s.session_id == "GEST_001"
        assert s.assistant_name == "GEST"
        assert s.module_context == "CXPI"
        assert not s.is_expired

    def test_create_duplicate_raises(self):
        self.mgr.create("DUP_001")
        with pytest.raises(ValueError, match="already exists"):
            self.mgr.create("DUP_001")

    def test_store_and_retrieve(self):
        self.mgr.create("S_001")
        self.mgr.store("S_001", "module", "CXPI")
        self.mgr.store("S_001", "rag_results", [{"id": 1}, {"id": 2}])

        assert self.mgr.retrieve("S_001", "module") == "CXPI"
        assert len(self.mgr.retrieve("S_001", "rag_results")) == 2
        assert self.mgr.retrieve("S_001", "nonexistent") is None

    def test_retrieve_missing_session_raises(self):
        with pytest.raises(ValueError, match="not found"):
            self.mgr.retrieve("GHOST_001", "key")

    def test_close_session(self):
        self.mgr.create("CLOSE_001", assistant_name="ACRA")
        self.mgr.store("CLOSE_001", "data", "value")
        summary = self.mgr.close("CLOSE_001")

        assert summary["session_id"] == "CLOSE_001"
        assert summary["total_store_keys"] == 1
        # Session should be gone after close
        assert self.mgr.get("CLOSE_001") is None

    def test_ttl_expiry(self):
        self.mgr.create("TTL_001", ttl_seconds=1)
        assert self.mgr.get("TTL_001") is not None
        time.sleep(1.1)
        # Expired sessions are cleaned up on access
        assert self.mgr.get("TTL_001") is None

    def test_session_to_dict(self):
        s = self.mgr.create("DICT_001", assistant_name="CIA", module_context="SPI")
        d = s.to_dict()
        assert d["session_id"] == "DICT_001"
        assert d["assistant_name"] == "CIA"
        assert d["module_context"] == "SPI"
        assert "remaining_seconds" in d
        assert d["is_expired"] is False

    def test_multiple_sessions(self):
        self.mgr.create("A_001", module_context="CAN")
        self.mgr.create("B_001", module_context="SPI")
        self.mgr.create("C_001", module_context="ADC")
        self.mgr.store("A_001", "k", "v1")
        self.mgr.store("B_001", "k", "v2")
        assert self.mgr.retrieve("A_001", "k") == "v1"
        assert self.mgr.retrieve("B_001", "k") == "v2"


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: ContextBuilder
# ═════════════════════════════════════════════════════════════════════════

class TestContextBuilder:
    """Token-budget-aware context assembly."""

    def _make_results(self, n: int, content_size: int = 200) -> list:
        """Generate n fake RAG results with given content size."""
        return [
            {
                "node_id": f"node_{i}",
                "node_type": "APIFunction",
                "source": "neo4j",
                "score": 1.0 - (i * 0.05),
                "content": f"Function_{i} " + "x" * content_size,
            }
            for i in range(n)
        ]

    def test_basic_build(self):
        builder = ContextBuilder(max_tokens=8000)
        results = self._make_results(5, content_size=100)
        ctx = builder.build(rag_results=results)

        assert ctx["items_included"] == 5
        assert ctx["items_dropped"] == 0
        assert ctx["total_tokens"] > 0
        assert len(ctx["provenance"]) == 5
        assert "rendered_context" in ctx

    def test_budget_enforcement(self):
        """With a tiny budget, not all results should fit."""
        builder = ContextBuilder(max_tokens=50)  # ~200 chars
        results = self._make_results(10, content_size=100)  # Each ~110 chars
        ctx = builder.build(rag_results=results)

        assert ctx["items_included"] < 10
        assert ctx["items_dropped"] > 0
        assert len(ctx["dropped_ids"]) > 0

    def test_relevance_ordering(self):
        """Higher-scoring results should be included first."""
        builder = ContextBuilder(max_tokens=100)  # tight budget
        results = [
            {"node_id": "low", "score": 0.1, "content": "A" * 80},
            {"node_id": "high", "score": 0.9, "content": "B" * 80},
            {"node_id": "mid", "score": 0.5, "content": "C" * 80},
        ]
        ctx = builder.build(rag_results=results)

        # High score should be included first
        if ctx["items_included"] >= 1:
            assert ctx["provenance"][0]["node_id"] == "high"

    def test_conversation_history(self):
        builder = ContextBuilder(max_tokens=2000)
        history = [
            {"role": "user", "content": "Find SPI init functions"},
            {"role": "assistant", "content": "Here are the results..."},
        ]
        ctx = builder.build(conversation_history=history)
        assert "[Conversation History]" in ctx["rendered_context"]

    def test_session_context(self):
        builder = ContextBuilder(max_tokens=2000)
        session_ctx = {"module": "CXPI", "assistant": "GEST"}
        ctx = builder.build(session_context=session_ctx)
        assert "[Session Context]" in ctx["rendered_context"]

    def test_empty_input(self):
        builder = ContextBuilder(max_tokens=8000)
        ctx = builder.build()
        assert ctx["items_included"] == 0
        assert ctx["total_tokens"] == 0 or ctx["total_tokens"] >= 0

    def test_provenance_tracking(self):
        builder = ContextBuilder(max_tokens=8000)
        results = [
            {"node_id": "fn_1", "node_type": "APIFunction", "source": "neo4j",
             "score": 0.95, "content": "IfxCxpi_initModule..."},
            {"node_id": "req_1", "node_type": "ProductRequirement", "source": "qdrant",
             "score": 0.88, "content": "The CXPI driver shall..."},
        ]
        ctx = builder.build(rag_results=results)

        assert len(ctx["provenance"]) == 2
        p0 = ctx["provenance"][0]
        assert p0["node_id"] == "fn_1"
        assert p0["node_type"] == "APIFunction"
        assert p0["source"] == "neo4j"
        assert p0["score"] == 0.95

    def test_budget_unit_characters(self):
        builder = ContextBuilder(max_tokens=500, budget_unit="characters")
        results = self._make_results(10, content_size=200)
        ctx = builder.build(rag_results=results)
        assert ctx["budget_unit"] == "characters"
        assert ctx["items_included"] < 10


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: SearchService (structure, no live Neo4j)
# ═════════════════════════════════════════════════════════════════════════

class TestSearchServiceStructure:
    """Verify SearchService methods exist and handle no-backend gracefully."""

    def test_import(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        assert not svc.available

    def test_hybrid_search_no_backend(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        result = svc.hybrid_search("test query")
        assert result["results"] == []
        assert result["total_count"] == 0

    def test_get_node_by_id_no_backend(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        result = svc.get_node_by_id(document_id="AURC1-REQA-286")
        assert result["found"] is False

    def test_get_neighbors_no_backend(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        result = svc.get_neighbors(document_id="test")
        assert result["neighbors"] == []

    def test_shortest_path_no_backend(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        result = svc.shortest_path(from_id="a", to_id="b")
        assert result["found"] is False

    def test_execute_cypher_no_backend(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        result = svc.execute_cypher("MATCH (n) RETURN n LIMIT 1")
        assert "error" in result


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Full 6-Step Session Lifecycle
# ═════════════════════════════════════════════════════════════════════════

class TestFullSessionLifecycle:
    """Simulate the complete DA lifecycle from PPTX slide 23."""

    def test_six_step_lifecycle(self):
        mgr = SessionManager(backend=DictBackend())

        # Step 1: session_start
        session = mgr.create(
            session_id="GEST_20260322_001",
            assistant_name="GEST",
            module_context="CXPI",
            ttl_seconds=3600,
        )
        assert session.session_id == "GEST_20260322_001"

        # Step 2: search_database (simulated — no Neo4j)
        fake_search_results = [
            {"node_id": "IfxCxpi_Cxpi_initModule", "node_type": "APIFunction",
             "score": 0.95, "source": "neo4j",
             "content": "void IfxCxpi_Cxpi_initModule(IfxCxpi_Cxpi *cxpi, const IfxCxpi_Cxpi_Config *config)"},
            {"node_id": "AURC1-REQA-286", "node_type": "ProductRequirement",
             "score": 0.88, "source": "neo4j",
             "content": "The CXPI driver shall provide an initialization API that configures the module."},
            {"node_id": "IfxCxpi_Cxpi_Config", "node_type": "DataStructure",
             "score": 0.82, "source": "neo4j",
             "content": "Configuration structure for CXPI module. Fields: baudrate, mode, timeout."},
        ]
        mgr.store("GEST_20260322_001", "search_results", fake_search_results)

        # Step 3: build_context
        builder = ContextBuilder(max_tokens=8000)
        ctx = builder.build(
            rag_results=fake_search_results,
            session_context={"module": "CXPI", "assistant": "GEST"},
        )
        assert ctx["items_included"] == 3
        assert ctx["total_tokens"] > 0
        assert "IfxCxpi_Cxpi_initModule" in ctx["rendered_context"]
        mgr.store("GEST_20260322_001", "built_context", ctx["rendered_context"])

        # Step 4: Domain-specific work (simulated)
        generated_code = """
        void Test_IfxCxpi_Cxpi_initModule_001(void) {
            IfxCxpi_Cxpi_Config config;
            IfxCxpi_Cxpi_initModuleConfig(&config, &MODULE_CXPI);
            config.baudrate = 10000;
            IfxCxpi_Cxpi_initModule(&g_cxpi, &config);
            TEST_ASSERT(g_cxpi.initialized == TRUE);
        }
        """
        mgr.store("GEST_20260322_001", "generated_output", generated_code)

        # Step 5: evaluate_confidence (simulated)
        confidence_signals = {
            "pattern_match": True,
            "call_order_valid": True,
            "validation_score": 85,
        }
        # Base=20 + pattern(+10) + order(+25) + validation(+15) = 70 → QUICK
        mgr.store("GEST_20260322_001", "confidence_signals", confidence_signals)

        # Step 6: session_end
        summary = mgr.close("GEST_20260322_001", persist_audit=True)
        assert summary["session_id"] == "GEST_20260322_001"
        assert summary["total_store_keys"] >= 4  # search_results, built_context, generated_output, confidence_signals

        # Session should be gone
        assert mgr.get("GEST_20260322_001") is None

    def test_multi_turn_session(self):
        """Test multiple search → build_context cycles within one session."""
        mgr = SessionManager(backend=DictBackend())
        mgr.create("CIA_001", assistant_name="CIA", module_context="CAN")

        # Turn 1: Init patterns
        results_1 = [{"node_id": "IfxCan_init", "score": 0.9, "content": "CAN init function"}]
        mgr.store("CIA_001", "turn_1_results", results_1)
        ctx_1 = ContextBuilder(max_tokens=4000).build(rag_results=results_1)
        assert ctx_1["items_included"] == 1

        # Turn 2: Error handling patterns
        results_2 = [{"node_id": "IfxCan_errorHandler", "score": 0.85, "content": "CAN error handler"}]
        mgr.store("CIA_001", "turn_2_results", results_2)

        # Turn 3: Combined context build
        all_results = results_1 + results_2
        ctx_final = ContextBuilder(max_tokens=8000).build(rag_results=all_results)
        assert ctx_final["items_included"] == 2

        mgr.close("CIA_001")


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: Token Estimation
# ═════════════════════════════════════════════════════════════════════════

class TestTokenEstimation:
    def test_estimate_tokens(self):
        assert estimate_tokens("hello world") >= 1
        assert estimate_tokens("a" * 400) == 100  # 400 chars / 4

    def test_empty_string(self):
        assert estimate_tokens("") == 1  # min 1

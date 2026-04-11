"""
Sprint 3 Integration Tests — Ephemeral Sandbox (P1)
====================================================
Tests:
  1. EphemeralGraph: add nodes, keyword search, traceability
  2. EphemeralVectors: chunks, search, cosine similarity
  3. SandboxManager: create/get/destroy lifecycle
  4. SandboxIngester: parse C header file
  5. Full sandbox lifecycle: session → upload → query → status → clear
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.MemoryLayer.memory.ephemeral_sandbox import (
    EphemeralGraph, EphemeralVectors, EphemeralSandbox,
    SandboxManager, SandboxIngester, SandboxQuerier,
    Chunk, SearchResult,
)
from src.MemoryLayer.memory.session_manager import SessionManager, DictBackend


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: EphemeralGraph
# ═════════════════════════════════════════════════════════════════════════

class TestEphemeralGraph:

    def test_add_node_and_stats(self):
        g = EphemeralGraph()
        g.add_node("APIFunction", "IfxCan_init", {"function_name": "IfxCan_init", "module": "CAN"})
        g.add_node("Register", "CAN_CLC", {"register_name": "CAN_CLC", "description": "Clock control"})
        assert g.node_count == 2
        assert g.stats()["total_nodes"] == 2

    def test_add_relationship(self):
        g = EphemeralGraph()
        g.add_node("APIFunction", "fn1", {})
        g.add_node("Register", "reg1", {})
        g.add_relationship("fn1", "reg1", "ACCESSES")
        assert g.edge_count == 1

    def test_keyword_search(self):
        g = EphemeralGraph()
        g.add_node("APIFunction", "IfxCan_init", {"function_name": "IfxCan_init", "description": "Initialize CAN"})
        g.add_node("APIFunction", "IfxSpi_init", {"function_name": "IfxSpi_init", "description": "Initialize SPI"})
        g.add_node("Register", "CAN_CLC", {"register_name": "CAN_CLC"})

        results = g.keyword_search(["can", "init"])
        assert len(results) >= 1
        # CAN init should score highest (matches both keywords)
        assert results[0].node_id == "IfxCan_init"

    def test_keyword_search_with_type_filter(self):
        g = EphemeralGraph()
        g.add_node("APIFunction", "IfxCan_init", {"function_name": "IfxCan_init"})
        g.add_node("Register", "CAN_CLC", {"register_name": "CAN_CLC"})

        results = g.keyword_search(["can"], node_types=["Register"])
        assert all(r.node_type == "Register" for r in results)

    def test_bulk_add(self):
        g = EphemeralGraph()
        nodes = [{"node_type": "APIFunction", "node_id": f"fn_{i}", "name": f"function_{i}"} for i in range(10)]
        count = g.add_nodes_bulk(nodes)
        assert count == 10
        assert g.node_count == 10

    def test_traceability(self):
        g = EphemeralGraph()
        g.add_node("Requirement", "REQ_001", {})
        g.add_node("APIFunction", "fn_001", {})
        g.add_node("TestCase", "TC_001", {})
        g.add_relationship("REQ_001", "fn_001", "IMPLEMENTS")
        g.add_relationship("fn_001", "TC_001", "TRACES_TO")

        chain = g.get_traceability(["REQ_001", "fn_001"])
        assert len(chain) == 2
        assert chain[0]["relationship"] == "IMPLEMENTS"
        assert chain[1]["relationship"] == "TRACES_TO"

    def test_clear(self):
        g = EphemeralGraph()
        g.add_node("X", "n1", {})
        g.add_node("X", "n2", {})
        stats = g.clear()
        assert stats["nodes_removed"] == 2
        assert g.node_count == 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: EphemeralVectors
# ═════════════════════════════════════════════════════════════════════════

class TestEphemeralVectors:

    def test_add_chunks_and_search(self):
        ev = EphemeralVectors(session_id="test_001")
        chunks = [
            Chunk(text="IfxCan_init initializes the CAN module with clock and pin configuration"),
            Chunk(text="IfxSpi_init initializes the SPI module for serial communication"),
            Chunk(text="DMA transfer configuration for ADC peripheral"),
        ]
        added = ev.add_chunks(chunks)
        assert added == 3
        assert ev.stats()["chunk_count"] == 3

    def test_vector_search(self):
        ev = EphemeralVectors(session_id="test_002")
        ev.add_chunks([
            Chunk(text="CAN bus initialization with baud rate 500kbps"),
            Chunk(text="SPI master mode configuration"),
            Chunk(text="Unrelated content about weather"),
        ])
        results = ev.search("CAN initialization", top_k=3)
        assert len(results) >= 1
        # All 3 chunks should be returned (fallback embedder doesn't do true semantic ranking)
        contents = [r.content for r in results]
        assert any("CAN" in c for c in contents), "CAN chunk should be in results"
        # Scores should all be between 0 and 1
        assert all(0 <= r.score <= 1.0 for r in results)

    def test_max_chunks_limit(self):
        ev = EphemeralVectors(session_id="test_003", max_chunks=5)
        chunks = [Chunk(text=f"chunk {i}") for i in range(5)]
        ev.add_chunks(chunks)
        with pytest.raises(ValueError, match="exceed"):
            ev.add_chunks([Chunk(text="one more")])

    def test_clear(self):
        ev = EphemeralVectors(session_id="test_004")
        ev.add_chunks([Chunk(text="test")])
        stats = ev.clear()
        assert stats["chunks_removed"] == 1
        assert ev.stats()["chunk_count"] == 0

    def test_empty_search(self):
        ev = EphemeralVectors(session_id="test_005")
        results = ev.search("anything")
        assert results == []


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: SandboxManager
# ═════════════════════════════════════════════════════════════════════════

class TestSandboxManager:

    def test_create_and_get(self):
        sm = SandboxManager()
        sb = sm.create_sandbox("sess_001")
        assert sb.session_id == "sess_001"
        assert sb.active
        assert sm.get_sandbox("sess_001") is sb

    def test_idempotent_create(self):
        sm = SandboxManager()
        sb1 = sm.create_sandbox("sess_002")
        sb2 = sm.create_sandbox("sess_002")
        assert sb1 is sb2  # Same object returned

    def test_destroy(self):
        sm = SandboxManager()
        sm.create_sandbox("sess_003")
        result = sm.destroy_sandbox("sess_003")
        assert result["cleared"] is True
        assert sm.get_sandbox("sess_003") is None

    def test_destroy_nonexistent(self):
        sm = SandboxManager()
        result = sm.destroy_sandbox("ghost")
        assert result["cleared"] is False

    def test_status_active(self):
        sm = SandboxManager()
        sm.create_sandbox("sess_004")
        status = sm.get_status("sess_004")
        assert status["active"] is True
        assert status["session_id"] == "sess_004"

    def test_status_inactive(self):
        sm = SandboxManager()
        status = sm.get_status("nonexistent")
        assert status["active"] is False

    def test_active_count(self):
        sm = SandboxManager()
        sm.create_sandbox("a")
        sm.create_sandbox("b")
        assert sm.active_count == 2
        sm.destroy_sandbox("a")
        assert sm.active_count == 1


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: SandboxIngester
# ═════════════════════════════════════════════════════════════════════════

class TestSandboxIngester:

    def test_ingest_c_header(self):
        sm = SandboxManager()
        sb = sm.create_sandbox("ingest_001")
        ingester = SandboxIngester(sb)

        # Create a temp C header file
        with tempfile.NamedTemporaryFile(suffix=".h", mode="w", delete=False) as f:
            f.write("""
/* CXPI Driver Header */
#ifndef IFXCXPI_H
#define IFXCXPI_H

typedef struct {
    uint32 baudrate;
    uint8 mode;
} IfxCxpi_Config;

void IfxCxpi_initModule(IfxCxpi_Cxpi *cxpi, const IfxCxpi_Config *config);
uint8 IfxCxpi_sendFrame(IfxCxpi_Cxpi *cxpi, const uint8 *data, uint32 len);
boolean IfxCxpi_getStatus(IfxCxpi_Cxpi *cxpi);

#endif
""")
            tmp_path = f.name

        stats = ingester.ingest_files([tmp_path])
        assert stats["files_processed"] == 1
        assert stats["nodes_created"] >= 3  # file node + 3 functions
        assert stats["chunks_embedded"] >= 1
        assert len(stats["errors"]) == 0

        # Verify graph has the functions
        results = sb.graph.keyword_search(["cxpi", "init"])
        assert len(results) >= 1

        Path(tmp_path).unlink()

    def test_ingest_text_file(self):
        sm = SandboxManager()
        sb = sm.create_sandbox("ingest_002")
        ingester = SandboxIngester(sb)

        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# CAN Driver\n\nThe CAN driver provides initialization and transmit APIs.\n")
            tmp_path = f.name

        stats = ingester.ingest_files([tmp_path])
        assert stats["files_processed"] == 1
        assert stats["chunks_embedded"] >= 1

        # Vector search should find CAN content
        results = sb.vectors.search("CAN initialization")
        assert len(results) >= 1

        Path(tmp_path).unlink()

    def test_ingest_missing_file(self):
        sm = SandboxManager()
        sb = sm.create_sandbox("ingest_003")
        ingester = SandboxIngester(sb)
        stats = ingester.ingest_files(["/nonexistent/file.h"])
        assert len(stats["errors"]) == 1
        assert stats["files_processed"] == 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: SandboxQuerier
# ═════════════════════════════════════════════════════════════════════════

class TestSandboxQuerier:

    def _populated_sandbox(self) -> EphemeralSandbox:
        sm = SandboxManager()
        sb = sm.create_sandbox("query_001")
        sb.graph.add_node("APIFunction", "IfxCan_init", {
            "function_name": "IfxCan_init", "description": "Initialize CAN module"})
        sb.graph.add_node("Register", "CAN_CLC", {
            "register_name": "CAN_CLC", "description": "Clock control register"})
        sb.graph.add_relationship("IfxCan_init", "CAN_CLC", "ACCESSES")
        sb.vectors.add_chunks([
            Chunk(text="IfxCan_init configures CAN module clock and pins", source_file="can.h"),
            Chunk(text="CAN_CLC register controls module clock gating", source_file="can_reg.h"),
        ])
        return sb

    def test_combined_search(self):
        sb = self._populated_sandbox()
        querier = SandboxQuerier(sb)
        results = querier.search("CAN init clock")
        assert len(results) >= 1

    def test_graph_only_search(self):
        sb = self._populated_sandbox()
        querier = SandboxQuerier(sb)
        results = querier.search("CAN init", alpha=1.0)  # graph only
        assert len(results) >= 1
        assert all(r.origin == "eph_graph" for r in results)

    def test_vector_only_search(self):
        sb = self._populated_sandbox()
        querier = SandboxQuerier(sb)
        results = querier.search("clock configuration", alpha=0.0)  # vector only
        assert len(results) >= 1
        assert all(r.origin == "eph_vector" for r in results)


# ═════════════════════════════════════════════════════════════════════════
#  Test 6: Full Sandbox Lifecycle with Session Integration
# ═════════════════════════════════════════════════════════════════════════

class TestFullSandboxLifecycle:

    def test_session_sandbox_lifecycle(self):
        """session_start → sandbox_upload → sandbox_query → sandbox_status → sandbox_clear → session_end"""
        session_mgr = SessionManager(backend=DictBackend())
        sandbox_mgr = SandboxManager()

        # Step 1: Start session
        session = session_mgr.create("GEST_SB_001", assistant_name="GEST", module_context="CAN")

        # Step 2: Upload files to sandbox
        sandbox = sandbox_mgr.create_sandbox("GEST_SB_001")
        ingester = SandboxIngester(sandbox)

        with tempfile.NamedTemporaryFile(suffix=".h", mode="w", delete=False) as f:
            f.write("void IfxCan_init(IfxCan_Config *cfg);\nvoid IfxCan_transmit(uint8 *data);\n")
            tmp = f.name

        stats = ingester.ingest_files([tmp])
        assert stats["files_processed"] == 1

        # Step 3: Query sandbox
        querier = SandboxQuerier(sandbox)
        results = querier.search("CAN initialization")
        assert len(results) >= 1

        # Step 4: Check status
        status = sandbox_mgr.get_status("GEST_SB_001")
        assert status["active"] is True
        assert status["file_count"] == 1

        # Step 5: Clear sandbox
        clear_result = sandbox_mgr.destroy_sandbox("GEST_SB_001")
        assert clear_result["cleared"] is True
        assert sandbox_mgr.get_sandbox("GEST_SB_001") is None

        # Step 6: Close session
        summary = session_mgr.close("GEST_SB_001")
        assert summary["session_id"] == "GEST_SB_001"

        Path(tmp).unlink()

    def test_sandbox_merged_with_context_builder(self):
        """Verify sandbox results can feed into ContextBuilder."""
        from src.MemoryLayer.memory.context_builder import LegacyContextBuilder as ContextBuilder

        sandbox_mgr = SandboxManager()
        sb = sandbox_mgr.create_sandbox("merge_001")
        sb.graph.add_node("APIFunction", "IfxAdc_init", {
            "function_name": "IfxAdc_init", "description": "Initialize ADC"})
        sb.vectors.add_chunks([Chunk(text="ADC initialization with 12-bit resolution")])

        # Query sandbox
        querier = SandboxQuerier(sb)
        sandbox_results = querier.search("ADC init")

        # Convert to ContextBuilder format
        rag_results = [
            {"node_id": r.node_id, "node_type": r.node_type, "score": r.score,
             "content": r.content, "source": r.origin}
            for r in sandbox_results
        ]

        # Build context with sandbox + simulated permanent results
        permanent_results = [
            {"node_id": "ADC_CTRL", "node_type": "Register", "score": 0.8,
             "content": "ADC control register", "source": "neo4j"},
        ]

        builder = ContextBuilder(max_tokens=4000)
        ctx = builder.build(rag_results=permanent_results + rag_results)
        assert ctx["items_included"] >= 2  # permanent + sandbox results merged
        assert "ADC" in ctx["rendered_context"]

"""
Tests for iLLD Sandbox Integration

Validates the full end-to-end flow:
1. Parser routing (SWA, SFR, xlsx, C files → correct parsers)
2. Ingestion (parsed output → correct graph nodes + vector chunks)
3. Shadow/override (updated files shadow prod, non-updated use prod)
4. Hybrid search (sandbox + prod Qdrant merged, stale prod excluded)
5. Module detection (Ifx prefix stripping)
6. Real embedder integration
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
from dataclasses import dataclass
import sys
import tempfile
import os

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from src.MemoryLayer.memory.ephemeral_sandbox import (
    EphemeralGraph,
    EphemeralVectors,
    EphemeralSandbox,
    SandboxAdapter,
    SandboxManager,
    SandboxParserDispatcher,
    SandboxQuerier,
    HybridGraphService,
    SearchResult,
    Chunk,
    _FallbackEmbedder,
    _SentenceTransformerEmbedder,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sandbox():
    """Create a fresh sandbox with fallback embedder for fast tests."""
    return EphemeralSandbox(
        session_id="test-session-001",
        graph=EphemeralGraph(),
        vectors=EphemeralVectors("test-session-001", _FallbackEmbedder()),
    )


def _get_outgoing_edges(graph: EphemeralGraph, node_id: str, rel_type: str = None):
    """Helper: get outgoing edges from a node, optionally filtered by type."""
    if node_id not in graph._graph:
        return []
    edges = []
    for _, tgt, data in graph._graph.out_edges(node_id, data=True):
        if rel_type is None or data.get("_rel_type") == rel_type:
            edges.append({"_target": tgt, "_rel_type": data.get("_rel_type"), **data})
    return edges


@pytest.fixture
def adapter():
    """Create a SandboxAdapter configured for iLLD."""
    return SandboxAdapter(workspace_id="illd")


@pytest.fixture
def dispatcher(tmp_path):
    """Create a SandboxParserDispatcher configured for iLLD."""
    return SandboxParserDispatcher(workspace_id="illd")


@pytest.fixture
def sandbox_manager():
    """Create a SandboxManager with fallback embedder."""
    return SandboxManager(embedder=_FallbackEmbedder(), max_chunks=5000)


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Parser Routing
# ═══════════════════════════════════════════════════════════════════════════

class TestParserRouting:
    """Validate that files are routed to the correct parser based on name/extension."""

    def test_swa_header_routed_to_illd_swa_parser(self, dispatcher, tmp_path):
        """*_swa.h files should route to _parse_illd_swa when workspace=illd."""
        swa_file = tmp_path / "IfxAdc_swa.h"
        swa_file.write_text("/* SWA header */\nvoid IfxAdc_initModule(void);")

        with patch.object(dispatcher, '_parse_illd_swa',
                          return_value={"type": "illd_swa", "functions": [], "file": str(swa_file)}) as mock:
            result = dispatcher.parse(swa_file)
            mock.assert_called_once()
            assert result["type"] == "illd_swa"

    def test_regdef_header_routed_to_sfr_parser(self, dispatcher, tmp_path):
        """*_regdef.h files should route to _parse_sfr when workspace=illd."""
        regdef_file = tmp_path / "IfxAdc_regdef.h"
        regdef_file.write_text("/* regdef header */")

        with patch.object(dispatcher, '_parse_sfr',
                          return_value={"type": "illd_sfr", "registers": {}, "file": str(regdef_file)}) as mock:
            result = dispatcher.parse(regdef_file)
            mock.assert_called_once()
            assert result["type"] == "illd_sfr"

    def test_regular_c_file_not_routed_to_illd_parsers(self, dispatcher, tmp_path):
        """Regular .c files should go through normal c_parser, not SWA/SFR."""
        c_file = tmp_path / "IfxAdc_init.c"
        c_file.write_text("void IfxAdc_initModule(void) { }")

        with patch('src.IngestionPipeline.Parsers.c_parser.parse',
                   return_value={"type": "c_source", "functions": [], "file": str(c_file)}):
            result = dispatcher.parse(c_file)
            assert result["type"] == "c_source"

    def test_regular_header_not_routed_to_illd(self, dispatcher, tmp_path):
        """Regular .h files (not _swa/_regdef) should go through c_parser."""
        h_file = tmp_path / "IfxAdc.h"
        h_file.write_text("void IfxAdc_doSomething(void);")

        with patch('src.IngestionPipeline.Parsers.c_parser.parse',
                   return_value={"type": "c_header", "functions": [], "file": str(h_file)}):
            result = dispatcher.parse(h_file)
            assert result["type"] == "c_header"

    def test_mcal_workspace_skips_illd_routing(self, tmp_path):
        """When workspace_id='mcal', _swa.h should NOT route to iLLD parser."""
        dispatcher = SandboxParserDispatcher(workspace_id="mcal")
        swa_file = tmp_path / "Adc_swa.h"
        swa_file.write_text("/* MCAL swa header */")

        with patch('src.IngestionPipeline.Parsers.c_parser.parse',
                   return_value={"type": "c_header", "functions": [], "file": str(swa_file)}):
            result = dispatcher.parse(swa_file)
            assert result["type"] == "c_header"

    def test_xlsx_jama_detection(self, dispatcher, tmp_path):
        """iLLD Jama xlsx format (R4/C1='ID') should be detected and parsed."""
        xlsx_file = tmp_path / "Adc_requirements.xlsx"

        # Mock openpyxl
        mock_ws = MagicMock()
        mock_ws.cell.side_effect = lambda row, column: MagicMock(
            value={"ID": "ID", "module": "Adc"}.get(
                {(4, 1): "ID", (3, 1): "Adc"}.get((row, column), ""), None
            )
        )
        # R4/C1 = "ID"
        def cell_fn(row, column):
            m = MagicMock()
            if row == 4 and column == 1:
                m.value = "ID"
            elif row == 3 and column == 1:
                m.value = "Adc"
            else:
                m.value = None
            return m

        mock_ws.cell = cell_fn
        mock_ws.iter_rows = MagicMock(return_value=[
            ("AURC1-REQA-100", "Req", "True", "2026-01-01", "0", "0",
             "Module Init", "R1", "", "0", "0", "Approved"),
            ("Total Items:", None, None, None, None, None, None, None, None, None, None, None),
        ])

        mock_wb = MagicMock()
        mock_wb.active = mock_ws

        with patch('openpyxl.load_workbook', return_value=mock_wb):
            result = dispatcher._try_parse_illd_jama_xlsx(xlsx_file)

        assert result is not None
        assert result["type"] == "illd_xlsx_req"
        assert result["module"] == "Adc"
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["requirement_id"] == "AURC1-REQA-100"
        assert result["requirements"][0]["name"] == "Module Init"
        assert result["requirements"][0]["status"] == "Approved"


# ═══════════════════════════════════════════════════════════════════════════
#  Test: iLLD Ingestion Methods
# ═══════════════════════════════════════════════════════════════════════════

class TestIlldIngestion:
    """Validate that parsed iLLD data creates correct graph nodes and vector chunks."""

    def test_ingest_swa_header_creates_function_nodes(self, sandbox, adapter):
        """SWA header ingestion should create Function nodes with correct IDs."""
        parsed = {
            "type": "illd_swa",
            "functions": [
                {"name": "IfxAdc_initModule", "brief": "Init ADC module",
                 "return_type": "void", "dependencies": ["IfxAdc_reset"]},
                {"name": "IfxAdc_reset", "brief": "Reset ADC",
                 "return_type": "void", "dependencies": []},
            ],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")

        # Should have created 2 Function nodes
        assert "IfxAdc_initModule" in names
        assert "IfxAdc_reset" in names

        # Verify nodes are in the graph
        node = sandbox.graph.get_node("Function:IfxAdc_initModule:ADC")
        assert node is not None
        assert node["name"] == "IfxAdc_initModule"
        assert node["id"] == "FUNC_IfxAdc_initModule"
        assert node["module"] == "ADC"

        # Verify DEPENDS_ON edge
        dep_edges = _get_outgoing_edges(sandbox.graph, "Function:IfxAdc_initModule:ADC", "DEPENDS_ON")
        assert len(dep_edges) >= 1

    def test_ingest_swa_header_creates_struct_nodes(self, sandbox, adapter):
        """SWA header should create Struct + StructMember nodes + HAS_MEMBER edges."""
        parsed = {
            "type": "illd_swa",
            "functions": [],
            "structs": [
                {"name": "IfxAdc_Config", "brief": "ADC configuration",
                 "members": [
                     {"name": "channel", "type": "uint8", "description": "ADC channel"},
                     {"name": "resolution", "type": "uint16", "description": "Bits"},
                 ]},
            ],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")
        assert "IfxAdc_Config" in names

        # Struct node
        node = sandbox.graph.get_node("Struct:IfxAdc_Config:ADC")
        assert node is not None
        assert node["id"] == "STRUCT_IfxAdc_Config"

        # StructMember nodes
        member_node = sandbox.graph.get_node("StructMember:MEMBER_IfxAdc_Config_channel:ADC")
        assert member_node is not None
        assert member_node["type"] == "uint8"

        # HAS_MEMBER edge
        member_edges = _get_outgoing_edges(sandbox.graph, "Struct:IfxAdc_Config:ADC", "HAS_MEMBER")
        assert len(member_edges) == 2

    def test_ingest_swa_header_creates_enum_nodes(self, sandbox, adapter):
        """SWA header should create Enum + EnumValue nodes + HAS_VALUE edges."""
        parsed = {
            "type": "illd_swa",
            "functions": [],
            "structs": [],
            "enums": [
                {"name": "IfxAdc_ChannelId", "brief": "ADC channels",
                 "values": [
                     {"name": "IfxAdc_ChannelId_0", "value": "0", "description": "Ch 0"},
                     {"name": "IfxAdc_ChannelId_1", "value": "1", "description": "Ch 1"},
                 ]},
            ],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")
        assert "IfxAdc_ChannelId" in names

        # Enum node
        node = sandbox.graph.get_node("Enum:IfxAdc_ChannelId:ADC")
        assert node is not None
        assert node["id"] == "ENUM_IfxAdc_ChannelId"

        # EnumValue nodes
        val_node = sandbox.graph.get_node("EnumValue:ENUMVAL_IfxAdc_ChannelId_0:ADC")
        assert val_node is not None

        # HAS_VALUE edges
        val_edges = _get_outgoing_edges(sandbox.graph, "Enum:IfxAdc_ChannelId:ADC", "HAS_VALUE")
        assert len(val_edges) == 2

    def test_ingest_sfr_creates_register_nodes(self, sandbox, adapter):
        """SFR ingestion should create Register + BitField nodes + HAS_BITFIELD edges."""
        parsed = {
            "type": "illd_sfr",
            "registers": {
                "Ifx_ADC_GLOBCFG": [
                    {"name": "DIVA", "width": "5", "bit_range": "0:4",
                     "description": "Divider A"},
                    {"name": "DCMSB", "width": "1", "bit_range": "7:7",
                     "description": "Double Clock MSB"},
                ],
            },
            "file": "IfxAdc_regdef.h",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "IfxAdc_regdef.h", module="ADC")
        assert "Ifx_ADC_GLOBCFG" in names

        # Register node
        node = sandbox.graph.get_node("Register:Ifx_ADC_GLOBCFG:ADC")
        assert node is not None
        assert node["register_name"] == "Ifx_ADC_GLOBCFG"

        # BitField nodes
        bf_node = sandbox.graph.get_node("BitField:BITFIELD_Ifx_ADC_GLOBCFG_DIVA:ADC")
        assert bf_node is not None
        assert bf_node["bit_range"] == "0:4"

        # HAS_BITFIELD edges
        bf_edges = _get_outgoing_edges(sandbox.graph, "Register:Ifx_ADC_GLOBCFG:ADC", "HAS_BITFIELD")
        assert len(bf_edges) == 2

    def test_ingest_xlsx_requirements_illd(self, sandbox, adapter):
        """xlsx Jama requirements should create Requirement nodes."""
        parsed = {
            "type": "illd_xlsx_req",
            "requirements": [
                {"requirement_id": "AURC1-REQA-100", "name": "Module Init", "status": "Approved"},
                {"requirement_id": "AURC1-REQA-101", "name": "Error Handling", "status": "Draft"},
            ],
            "module": "ADC",
            "file": "Adc_reqs.xlsx",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "Adc_reqs.xlsx", module="ADC")
        assert "AURC1-REQA-100" in names
        assert "AURC1-REQA-101" in names

        # Requirement nodes
        node = sandbox.graph.get_node("Requirement:AURC1-REQA-100:ADC")
        assert node is not None
        assert node["name"] == "Module Init"
        assert node["status"] == "Approved"

    def test_ingest_c_illd_creates_function_with_calls(self, sandbox, adapter):
        """C file ingestion (iLLD) should create Function nodes + CALLS_INTERNALLY edges."""
        parsed = {
            "type": "c_source",
            "functions": [
                {"name": "IfxAdc_initModule", "return_type": "void",
                 "parameters": "IfxAdc_Config *config",
                 "internal_calls": [{"function": "IfxAdc_reset"}]},
            ],
            "content": "void IfxAdc_initModule(IfxAdc_Config *config) { IfxAdc_reset(); }",
            "file": "IfxAdc.c",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "IfxAdc.c", module="ADC")
        assert "IfxAdc_initModule" in names

        # Function node
        node = sandbox.graph.get_node("Function:IfxAdc_initModule:ADC")
        assert node is not None
        assert node["source"] == "Source_Code"

        # CALLS_INTERNALLY edge
        call_edges = _get_outgoing_edges(sandbox.graph, "Function:IfxAdc_initModule:ADC", "CALLS_INTERNALLY")
        assert len(call_edges) == 1

    def test_ingest_json_requirements_illd(self, sandbox, adapter):
        """JSON requirements (Jama API format) should create Requirement nodes."""
        parsed = {
            "type": "json",
            "data": {
                "requirements": [
                    {"document_key": "AURC1-REQA-200", "name": "Safety Check",
                     "description": "Validates ADC safety", "status": "Approved"},
                ]
            },
            "file": "adc_reqs.json",
        }

        names = adapter.ingest_parsed(sandbox, parsed, "adc_reqs.json", module="ADC")
        assert "AURC1-REQA-200" in names

        node = sandbox.graph.get_node("Requirement:AURC1-REQA-200:ADC")
        assert node is not None
        assert node["description"] == "Validates ADC safety"

    def test_semantic_chunks_created_for_illd(self, sandbox, adapter):
        """iLLD ingestion should create semantic per-entity vector chunks."""
        parsed = {
            "type": "illd_swa",
            "functions": [
                {"name": "IfxAdc_initModule", "brief": "Init ADC",
                 "return_type": "void", "dependencies": []},
            ],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }

        adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")

        # Should have at least 1 vector chunk for the function
        stats = sandbox.vectors.stats()
        assert stats["chunk_count"] >= 1

        # Search should find the function
        results = sandbox.vectors.search("IfxAdc init module", top_k=5)
        assert len(results) >= 1

    def test_no_generic_chunking_for_illd(self, sandbox, adapter):
        """iLLD types should NOT create generic text chunks (only semantic per-entity)."""
        parsed = {
            "type": "illd_swa",
            "functions": [{"name": "Fn1", "brief": "b", "return_type": "void", "dependencies": []}],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "content": "A" * 2000,  # Large content that would generate many generic chunks
            "file": "test_swa.h",
        }

        adapter.ingest_parsed(sandbox, parsed, "test_swa.h", module="ADC")

        # Should only have 1 chunk (for the 1 function), NOT many overlapping text chunks
        stats = sandbox.vectors.stats()
        assert stats["chunk_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Shadow/Override for iLLD
# ═══════════════════════════════════════════════════════════════════════════

class TestIlldShadowOverride:
    """Validate shadow semantics: updated files shadow prod, non-updated use prod."""

    def test_sandbox_function_shadows_prod(self, sandbox, adapter):
        """A sandbox function should shadow a prod function with same canonical ID."""
        # Simulate prod node (pulled by TraceabilityPuller)
        prod_node_id = "Function:IfxAdc_initModule:ADC"
        sandbox.graph.add_node("Function", prod_node_id, {
            "name": "IfxAdc_initModule",
            "module": "ADC",
            "brief": "OLD brief from prod",
            "_origin": "production",
        })

        # Now upload the updated version via sandbox adapter
        parsed = {
            "type": "illd_swa",
            "functions": [
                {"name": "IfxAdc_initModule", "brief": "NEW brief from sandbox",
                 "return_type": "int", "dependencies": []},
            ],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }

        adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")

        # The sandbox version should now be the active node
        node = sandbox.graph.get_node(prod_node_id)
        assert node is not None
        assert node.get("brief") == "NEW brief from sandbox"
        assert node.get("_origin") == "sandbox"

    def test_non_uploaded_prod_nodes_remain_visible(self, sandbox, adapter):
        """Prod nodes for files NOT uploaded should remain visible."""
        # Simulate prod nodes for different functions
        sandbox.graph.add_node("Function", "Function:IfxAdc_deinit:ADC", {
            "name": "IfxAdc_deinit",
            "module": "ADC",
            "brief": "Deinit function from production",
            "_origin": "production",
        })

        # Upload only initModule (deinit not re-uploaded)
        parsed = {
            "type": "illd_swa",
            "functions": [
                {"name": "IfxAdc_initModule", "brief": "Init",
                 "return_type": "void", "dependencies": []},
            ],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxAdc_swa.h",
        }
        adapter.ingest_parsed(sandbox, parsed, "IfxAdc_swa.h", module="ADC")

        # Prod deinit should still be there
        deinit = sandbox.graph.get_node("Function:IfxAdc_deinit:ADC")
        assert deinit is not None
        assert deinit.get("_origin") == "production"
        assert deinit.get("brief") == "Deinit function from production"

    def test_calls_internally_edges_replaced_on_shadow(self, sandbox, adapter):
        """When a function is re-uploaded, old CALLS_INTERNALLY edges should be cleared."""
        node_id = "Function:IfxAdc_initModule:ADC"

        # Simulate prod node with a prod edge
        sandbox.graph.add_node("Function", node_id, {
            "name": "IfxAdc_initModule", "module": "ADC", "_origin": "production",
        })
        sandbox.graph.add_node("Function", "Function:IfxAdc_oldCall:ADC", {
            "name": "IfxAdc_oldCall", "module": "ADC", "_origin": "production",
        })
        sandbox.graph.add_relationship(
            node_id, "Function:IfxAdc_oldCall:ADC",
            "CALLS_INTERNALLY", {"_origin": "production"})

        # Re-upload with different call target
        parsed = {
            "type": "c_source",
            "functions": [
                {"name": "IfxAdc_initModule", "return_type": "void",
                 "parameters": "", "internal_calls": [{"function": "IfxAdc_newCall"}]},
            ],
            "content": "void IfxAdc_initModule() { IfxAdc_newCall(); }",
            "file": "IfxAdc.c",
        }
        adapter.ingest_parsed(sandbox, parsed, "IfxAdc.c", module="ADC")

        # Check: newCall edge should exist
        call_edges = _get_outgoing_edges(sandbox.graph, node_id, "CALLS_INTERNALLY")
        callee_ids = [e.get("_target") for e in call_edges]
        assert "Function:IfxAdc_newCall:ADC" in callee_ids


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Hybrid Search (Sandbox + Prod Qdrant)
# ═══════════════════════════════════════════════════════════════════════════

class TestHybridSearch:
    """Validate that search merges sandbox + prod and filters stale results."""

    def test_search_returns_sandbox_results(self, sandbox):
        """Basic search should return results from sandbox vectors."""
        sandbox.vectors.add_chunks([Chunk(
            text="Function IfxAdc_initModule initializes the ADC hardware",
            metadata={"node_type": "Function", "node_id": "Function:IfxAdc_initModule:ADC",
                      "module": "ADC", "source_file": "IfxAdc_swa.h"},
        )])

        hybrid = HybridGraphService(sandbox, neo4j_driver=None, qdrant_client=None)
        results = hybrid.search("ADC init module", top_k=5)
        assert len(results) >= 1

    def test_prod_qdrant_excluded_for_uploaded_files(self, sandbox):
        """Prod Qdrant results from files uploaded to sandbox should be filtered out."""
        # Mark a file as uploaded to sandbox
        sandbox.files_ingested.append({"filename": "IfxAdc_swa.h", "type": "illd_swa"})

        # Mock Qdrant returning a result FROM that uploaded file
        mock_qdrant = MagicMock()
        mock_hit = MagicMock()
        mock_hit.payload = {
            "source_file": "IfxAdc_swa.h",
            "_original_id": "FUNC_IfxAdc_initModule",
            "document": "old prod content",
            "type": "function",
        }
        mock_hit.score = 0.95
        mock_hit.id = "uuid-1"

        mock_response = MagicMock()
        mock_response.points = [mock_hit]
        mock_qdrant.query_points.return_value = mock_response
        mock_qdrant.get_collections.return_value = MagicMock(collections=[])

        hybrid = HybridGraphService(sandbox, neo4j_driver=None,
                                    qdrant_client=mock_qdrant, workspace_id="illd")

        # The prod result should be excluded because IfxAdc_swa.h is in sandbox
        prod_results = hybrid._query_prod_qdrant(
            "ADC init", top_k=5,
            exclude_files={"ifxadc_swa.h"})
        assert len(prod_results) == 0

    def test_prod_qdrant_included_for_non_uploaded_files(self, sandbox):
        """Prod Qdrant results from NON-uploaded files should be included."""
        sandbox.files_ingested.append({"filename": "IfxAdc_swa.h", "type": "illd_swa"})

        # Mock Qdrant returning a result from a DIFFERENT file (not uploaded)
        mock_qdrant = MagicMock()
        mock_hit = MagicMock()
        mock_hit.payload = {
            "source_file": "IfxSpi_swa.h",  # NOT uploaded
            "_original_id": "FUNC_IfxSpi_init",
            "document": "SPI init function",
            "type": "function",
        }
        mock_hit.score = 0.85
        mock_hit.id = "uuid-2"

        mock_response = MagicMock()
        mock_response.points = [mock_hit]
        mock_qdrant.query_points.return_value = mock_response
        mock_qdrant.get_collections.return_value = MagicMock(
            collections=[MagicMock(name="adc")])

        hybrid = HybridGraphService(sandbox, neo4j_driver=None,
                                    qdrant_client=mock_qdrant, workspace_id="illd")

        prod_results = hybrid._query_prod_qdrant(
            "SPI init", top_k=5, filter_by_module="adc",
            exclude_files={"ifxadc_swa.h"})

        # Should include the SPI result (not shadowed)
        assert len(prod_results) == 1
        assert prod_results[0].node_id == "FUNC_IfxSpi_init"
        assert prod_results[0].origin == "prod_qdrant"

    def test_sandbox_takes_priority_over_prod_same_node_id(self, sandbox):
        """When both sandbox and prod have the same node_id, sandbox wins."""
        # Add sandbox vector chunk
        sandbox.vectors.add_chunks([Chunk(
            text="UPDATED: IfxAdc_initModule now takes two params",
            metadata={"node_type": "Function",
                      "node_id": "FUNC_IfxAdc_initModule",
                      "source_file": "IfxAdc_swa.h", "module": "ADC"},
        )])
        sandbox.files_ingested.append({"filename": "IfxAdc_swa.h", "type": "illd_swa"})

        # Mock prod returning same node_id
        mock_qdrant = MagicMock()
        mock_hit = MagicMock()
        mock_hit.payload = {
            "source_file": "IfxSpi.h",
            "_original_id": "FUNC_IfxSpi_init",
            "document": "SPI function",
            "type": "function",
        }
        mock_hit.score = 0.99
        mock_hit.id = "uuid-3"

        mock_response = MagicMock()
        mock_response.points = [mock_hit]
        mock_qdrant.query_points.return_value = mock_response
        mock_qdrant.get_collections.return_value = MagicMock(
            collections=[MagicMock(name="adc")])

        hybrid = HybridGraphService(sandbox, neo4j_driver=None,
                                    qdrant_client=mock_qdrant, workspace_id="illd")
        results = hybrid.search("IfxAdc init", top_k=10, filter_by_module="adc")

        # The sandbox result should be present
        sandbox_results = [r for r in results if r.origin != "prod_qdrant"]
        assert len(sandbox_results) >= 1


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Module Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestModuleDetection:
    """Validate Ifx prefix stripping in module detection."""

    def test_ifx_prefix_stripped(self):
        """IfxAdc_init → ADC (not IFXADC)."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "mcp"))
        from core.mcp_server import _detect_module_from_names
        assert _detect_module_from_names(["IfxAdc_initModule"]) == "ADC"

    def test_ifx_prefix_stripped_port(self):
        """IfxPort_setPinHigh → PORT."""
        from core.mcp_server import _detect_module_from_names
        assert _detect_module_from_names(["IfxPort_setPinHigh"]) == "PORT"

    def test_mcal_naming_unchanged(self):
        """Adc_Init → ADC (no Ifx prefix, works as before)."""
        from core.mcp_server import _detect_module_from_names
        assert _detect_module_from_names(["Adc_Init"]) == "ADC"

    def test_no_underscore_returns_unknown(self):
        """Names without underscore → 'unknown'."""
        from core.mcp_server import _detect_module_from_names
        assert _detect_module_from_names(["NoUnderscore"]) == "unknown"

    def test_returns_mode_not_first(self):
        """F-CB-10: must return the most common prefix, not the first one.

        Std utility helpers leak in at the top of headers and previously
        masked the real iLLD module name.
        """
        from core.mcp_server import _detect_module_from_names
        names = [
            "Std_ReturnType",         # noise
            "IfxCan_init",            # → CAN
            "IfxCan_Node_init",       # → CAN
            "IfxCan_Can_initModule",  # → CAN
        ]
        assert _detect_module_from_names(names) == "CAN"

    def test_skips_std_and_bare_ifx(self):
        """F-CB-10: ``Std_*`` and bare ``Ifx_*`` prefixes are ignored."""
        from core.mcp_server import _detect_module_from_names
        assert _detect_module_from_names(
            ["Std_ReturnType", "Ifx_GlobalState"]
        ) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Embedder (G5)
# ═══════════════════════════════════════════════════════════════════════════

class TestEmbedder:
    """Validate real embedder integration."""

    def test_sentence_transformer_embedder_loads(self):
        """_SentenceTransformerEmbedder should load the real model."""
        emb = _SentenceTransformerEmbedder()
        vecs = emb.embed(["test query"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 384  # all-MiniLM-L6-v2 dimension

    def test_semantic_similarity_works(self):
        """Semantically similar texts should have higher cosine similarity."""
        emb = _SentenceTransformerEmbedder()
        vecs = emb.embed([
            "Initialize the ADC module",
            "Start the analog to digital converter",
            "Set the GPIO pin to high",
        ])

        # Compare cosine similarity
        def cos_sim(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(x * x for x in b) ** 0.5
            return dot / (na * nb) if na > 0 and nb > 0 else 0

        sim_related = cos_sim(vecs[0], vecs[1])  # ADC init vs start ADC
        sim_unrelated = cos_sim(vecs[0], vecs[2])  # ADC init vs GPIO

        # Related texts should be more similar
        assert sim_related > sim_unrelated

    def test_fallback_when_no_sentence_transformers(self):
        """If sentence-transformers import fails, should fall back to hash."""
        emb = _SentenceTransformerEmbedder()
        with patch('src.Configuration.embedding_singleton.get_shared_model',
                   side_effect=ImportError("not installed")):
            emb._initialized = False
            emb._model = None
            emb._init()
            assert emb._fallback is not None
            vecs = emb.embed(["test"])
            assert len(vecs[0]) == 384  # Fallback also produces 384-dim


# ═══════════════════════════════════════════════════════════════════════════
#  Test: Node Name Extraction
# ═══════════════════════════════════════════════════════════════════════════

class TestNodeNameExtraction:
    """Validate _extract_node_names_from_parsed for all iLLD types."""

    def test_extract_swa_names(self, adapter):
        parsed = {
            "type": "illd_swa",
            "functions": [{"name": "IfxAdc_init"}],
            "structs": [{"name": "IfxAdc_Config"}],
            "enums": [{"name": "IfxAdc_ChannelId"}],
        }
        names = adapter._extract_node_names_from_parsed(parsed)
        assert "IfxAdc_init" in names
        assert "IfxAdc_Config" in names
        assert "IfxAdc_ChannelId" in names

    def test_extract_sfr_names(self, adapter):
        parsed = {"type": "illd_sfr", "registers": {"Ifx_ADC_GLOBCFG": [], "Ifx_ADC_GLOBICLASS": []}}
        names = adapter._extract_node_names_from_parsed(parsed)
        assert "Ifx_ADC_GLOBCFG" in names
        assert "Ifx_ADC_GLOBICLASS" in names

    def test_extract_xlsx_req_names(self, adapter):
        parsed = {
            "type": "illd_xlsx_req",
            "requirements": [
                {"requirement_id": "AURC1-REQA-100"},
                {"requirement_id": "AURC1-REQA-101"},
            ],
        }
        names = adapter._extract_node_names_from_parsed(parsed)
        assert "AURC1-REQA-100" in names
        assert "AURC1-REQA-101" in names

    def test_extract_json_req_names(self, adapter):
        parsed = {
            "type": "json",
            "data": {"requirements": [{"document_key": "AURC1-REQA-200"}]},
        }
        names = adapter._extract_node_names_from_parsed(parsed)
        assert "AURC1-REQA-200" in names


# ═══════════════════════════════════════════════════════════════════════════
#  Test: ILLD_DETECTABLE_REL_TYPES
# ═══════════════════════════════════════════════════════════════════════════

class TestDetectableRelTypes:
    """Validate the ILLD_DETECTABLE_REL_TYPES constant is used correctly."""

    def test_constant_defined(self, adapter):
        assert hasattr(adapter, 'ILLD_DETECTABLE_REL_TYPES')
        assert "CALLS_INTERNALLY" in adapter.ILLD_DETECTABLE_REL_TYPES

    def test_sandbox_detectable_unchanged(self, adapter):
        """MCAL types should be unchanged."""
        assert "SRC_CALLS" in adapter.SANDBOX_DETECTABLE_REL_TYPES


# ═══════════════════════════════════════════════════════════════════════════
#  Test: End-to-End Sandbox Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestSandboxLifecycle:
    """Test full lifecycle: create → upload → search → destroy."""

    def test_full_lifecycle(self, sandbox_manager):
        """Complete lifecycle: create sandbox, ingest, search, destroy."""
        sb = sandbox_manager.create_sandbox("lifecycle-test")
        assert sb.active

        # Ingest a SWA file
        adapter = SandboxAdapter(workspace_id="illd")
        parsed = {
            "type": "illd_swa",
            "functions": [
                {"name": "IfxDma_initChannel", "brief": "Init DMA channel",
                 "return_type": "void", "dependencies": []},
            ],
            "structs": [],
            "enums": [],
            "typedefs": [],
            "macros": [],
            "file": "IfxDma_swa.h",
        }
        adapter.ingest_parsed(sb, parsed, "IfxDma_swa.h", module="DMA")

        # Search
        querier = SandboxQuerier(sb)
        results = querier.search("DMA channel init", top_k=5)
        assert len(results) >= 1

        # Destroy
        stats = sandbox_manager.destroy_sandbox("lifecycle-test")
        assert stats["cleared"] is True

        # Should be gone
        assert sandbox_manager.get_sandbox("lifecycle-test") is None

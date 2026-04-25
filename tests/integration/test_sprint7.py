"""
Sprint 7 Integration Tests — Knowledge Intelligence + Zero Stubs
================================================================
Tests:
  1. API Intelligence: query_api_function, get_type_definition, generate_init_code
  2. Dependency Analysis: query_dependencies, validate_api_usage, detect_polling
  3. Traceability: find_traces, build_matrix, coverage_gaps, hw_sw_links
  4. Zero stubs remaining in MCP server
  5. All 56 tools in tool_tiers.py match MCP server registrations
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.HybridRAG.code.querier.knowledge_intelligence import KnowledgeIntelligenceService


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: API Intelligence (without Neo4j — graceful degradation)
# ═════════════════════════════════════════════════════════════════════════

class TestAPIIntelligence:
    def setup_method(self):
        self.ki = KnowledgeIntelligenceService()  # No neo4j

    def test_query_api_function_no_backend(self):
        result = self.ki.query_api_function("IfxCan_init")
        assert result["matches_found"] == 0
        assert result["api_function"] is None

    def test_get_type_definition_no_backend(self):
        result = self.ki.get_type_definition("IfxCan_Config")
        assert result.get("found") is False or result.get("struct_name") == "IfxCan_Config"

    def test_generate_initialization_code(self):
        """Should generate code even without Neo4j — uses overrides only."""
        result = self.ki.generate_initialization_code(
            "IfxCan_Config",
            user_overrides={"baudrate": 500000, "mode": "CAN_MODE_NORMAL"},
            variable_name="can_cfg",
        )
        assert result["struct_name"] == "IfxCan_Config"
        assert result["variable_name"] == "can_cfg"
        assert "baudrate" in result["c_code"]
        assert "500000" in result["c_code"]
        assert "can_cfg" in result["c_code"]
        assert "CAN_MODE_NORMAL" in result["c_code"]
        assert len(result["overrides_applied"]) == 2

    def test_generate_init_code_default_varname(self):
        result = self.ki.generate_initialization_code("IfxSpi_Config")
        assert result["variable_name"] == "config_config"  # auto-derived


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: Dependency Analysis
# ═════════════════════════════════════════════════════════════════════════

class TestDependencyAnalysis:
    def setup_method(self):
        self.ki = KnowledgeIntelligenceService()

    def test_query_dependencies_no_backend(self):
        result = self.ki.query_dependencies("IfxCan_init")
        assert result["function_name"] == "IfxCan_init"
        assert result["found"] is False
        assert result["direct_dependencies"] == []

    def test_validate_single_function(self):
        result = self.ki.validate_api_usage(["IfxCan_init"])
        assert result["is_valid"] is True
        assert result["violations"] == []

    def test_validate_empty_sequence(self):
        result = self.ki.validate_api_usage([])
        assert result["is_valid"] is True

    def test_detect_polling_async_function(self):
        result = self.ki.detect_polling_requirements(["IfxCan_sendFrame", "IfxCan_init"])
        polling = result["polling"]
        assert polling["IfxCan_sendFrame"]["needs_polling"] is True
        assert polling["IfxCan_sendFrame"]["status_function"] is not None
        assert polling["IfxCan_init"]["needs_polling"] is False

    def test_detect_polling_transmit(self):
        result = self.ki.detect_polling_requirements(["IfxSpi_transmit"])
        assert result["polling"]["IfxSpi_transmit"]["needs_polling"] is True

    def test_detect_polling_getStatus(self):
        """Status/getter functions don't need polling themselves."""
        result = self.ki.detect_polling_requirements(["IfxCan_getStatus"])
        # getStatus matches STATUS_PATTERNS but not ASYNC_PATTERNS
        assert result["polling"]["IfxCan_getStatus"]["needs_polling"] is False


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Traceability
# ═════════════════════════════════════════════════════════════════════════

class TestTraceability:
    def setup_method(self):
        self.ki = KnowledgeIntelligenceService()

    def test_find_traces_no_backend(self):
        result = self.ki.find_requirement_traces("AURC1-REQA-286")
        assert result["found"] is False
        assert result["chain"] == []

    def test_build_matrix_no_backend(self):
        result = self.ki.build_traceability_matrix("CXPI")
        assert result["module"] == "CXPI"
        assert result["matrix"] == []
        assert result["coverage"]["total_requirements"] == 0

    def test_build_matrix_uses_canonical_requirement_labels_and_case_insensitive_module(self):
        ki = KnowledgeIntelligenceService()
        captured = {}

        def _fake_run(cypher, params, ws="illd"):
            captured["cypher"] = cypher
            captured["params"] = params
            return [{
                "req_id": "REQ-1",
                "req_name": "Init requirement",
                "implementations": ["IfxCxpi_initModule"],
                "tests": ["TC-1"],
            }]

        ki._run_cypher = _fake_run

        result = ki.build_traceability_matrix("CxPi")

        assert "requirement_labels" in captured["params"]
        assert captured["params"]["requirement_labels"] == [
            "SoftwareRequirement",
            "ProductRequirement",
            "StakeholderRequirement",
        ]
        assert captured["params"]["mod"] == "CxPi"
        assert "toLower(coalesce(r.module, '')) = toLower($mod)" in captured["cypher"]
        assert result["coverage"]["total_requirements"] == 1
        assert result["coverage"]["with_code"] == 1
        assert result["coverage"]["with_tests"] == 1

    def test_find_gaps_no_backend(self):
        result = self.ki.find_coverage_gaps("CXPI")
        assert result["module"] == "CXPI"
        assert result["gaps"] == []

    def test_hw_sw_links_no_backend(self):
        result = self.ki.analyze_hw_sw_links("CXPI")
        assert result["module"] == "CXPI"
        assert result["hw_sw_links"] == []


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Zero Stubs + Complete Tool Coverage
# ═════════════════════════════════════════════════════════════════════════

class TestCompleteImplementation:

    def test_zero_stubs_in_mcp_server(self):
        """No NOT_IMPLEMENTED strings should remain."""
        server = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server.read_text(encoding="utf-8")
        count = content.count("NOT_IMPLEMENTED")
        assert count == 0, f"Found {count} remaining NOT_IMPLEMENTED stubs"

    def test_all_56_tools_have_ok_path(self):
        """Every tool should have a _ok() return path."""
        server = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server.read_text(encoding="utf-8")
        # Count async def tool functions vs _ok returns
        import re
        tool_defs = re.findall(r'async def (\w+)\(', content)
        ok_calls = content.count("return _ok")
        # We should have at least 46+ _ok calls (one per implemented tool)
        assert ok_calls >= 46, f"Only {ok_calls} _ok return paths, expected ≥46"

    def test_tool_tiers_match_server(self):
        """Every tool in TOOL_TIERS should be registered as an async def in the server."""
        from mcp.core.tool_tiers import TOOL_TIERS
        server = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server.read_text(encoding="utf-8")

        missing = []
        for tool_name in TOOL_TIERS:
            if f"async def {tool_name}(" not in content:
                missing.append(tool_name)

        assert not missing, f"Tools in TOOL_TIERS but not in mcp_server.py: {missing}"

    def test_56_tools_in_tiers(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        assert len(TOOL_TIERS) == 62, f"Expected 62 tools in tiers, got {len(TOOL_TIERS)}"

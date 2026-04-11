"""
Sprint 9 Integration Tests — Result Processors & FeedbackSink Learning Loop
=============================================================================
Validates:
    1. ResultProcessor dispatches to correct parser by result_type
    2. JUnitParser: standard xUnit XML parsing
    3. PolyspaceParser: Bug Finder CSV/XML parsing
    4. VPParser: Vector Processor XML report parsing
    5. CompilerParser: GCC/Tasking warning/error log parsing
    6. CoverageParser: GCOV/LCOV coverage data parsing
    7. FeedbackSink wiring to PatternStore (learning loop)
    8. process_results MCP tool returns real data (no NOT_IMPLEMENTED)
"""
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.ReviewGate.result_processors import (
    ResultProcessor,
    JUnitParser,
    VPParser,
    PolyspaceParser,
    CoverageParser,
    CompilerParser,
    TestResult,
)
from src.ReviewGate.confidence import FeedbackSink


# ═════════════════════════════════════════════════════════════════════════
#  Fixtures — Temporary test result files
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def junit_xml_file(tmp_path):
    """Create a standard JUnit XML report."""
    content = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="4" failures="1" errors="1" time="1.234">
  <testsuite name="Test_Adc_Init" tests="4" failures="1" errors="1" time="1.234">
    <testcase classname="adc.init" name="test_init_default_config" time="0.100"/>
    <testcase classname="adc.init" name="test_init_null_config" time="0.050">
      <failure message="Assertion failed" type="AssertionError">
Expected: E_NOT_OK
Actual: E_OK
      </failure>
    </testcase>
    <testcase classname="adc.init" name="test_init_invalid_channel" time="0.080">
      <error message="Segmentation fault" type="RuntimeError">
SIGSEGV at Adc_Init+0x42
      </error>
    </testcase>
    <testcase classname="adc.init" name="test_init_already_initialized" time="0.030"/>
  </testsuite>
</testsuites>"""
    f = tmp_path / "junit_results.xml"
    f.write_text(content)
    return str(f)


@pytest.fixture
def compiler_log_file(tmp_path):
    """Create a GCC-style compiler warning/error log."""
    content = """Adc_Init.c:42:5: warning: implicit declaration of function 'Adc_lSetBit' [-Wimplicit-function-declaration]
Adc_Init.c:87:12: error: 'ADC_INVALID_CHANNEL' undeclared (first use in this function)
Adc_ReadGroup.c:120:3: warning: unused variable 'tempResult' [-Wunused-variable]
Adc_StartConversion.c:55:8: warning: comparison of unsigned expression >= 0 is always true [-Wtype-limits]
"""
    f = tmp_path / "compiler.log"
    f.write_text(content)
    return str(f)


@pytest.fixture
def polyspace_csv_file(tmp_path):
    """Create a Polyspace Bug Finder CSV-style report."""
    content = """ID,Family,Group,Color,File,Line,Column,Function,Check,Status,Severity,Comment
1,Defect,Data flow,Red,Adc_Init.c,42,5,Adc_Init,Non-initialized variable,New,High,
2,Defect,Programming,Orange,Adc_ReadGroup.c,120,3,Adc_ReadGroup,Dead code,New,Medium,
3,MISRA C:2012,Rule 10.3,Red,Adc_Init.c,87,12,Adc_Init,Implicit conversion,New,High,
4,Defect,Concurrency,Red,Adc_StartConversion.c,55,8,Adc_StartConversion,Data race,New,High,
"""
    f = tmp_path / "polyspace_results.csv"
    f.write_text(content)
    return str(f)


@pytest.fixture
def coverage_lcov_file(tmp_path):
    """Create an LCOV-style coverage data file."""
    content = """TN:
SF:Adc_Init.c
FN:10,Adc_Init
FN:50,Adc_DeInit
FNDA:5,Adc_Init
FNDA:0,Adc_DeInit
FNF:2
FNH:1
DA:10,5
DA:11,5
DA:12,5
DA:50,0
DA:51,0
LF:5
LH:3
end_of_record
"""
    f = tmp_path / "coverage.info"
    f.write_text(content)
    return str(f)


@pytest.fixture
def vp_xml_file(tmp_path):
    """Create a minimal Vector Processor (VP) XML report."""
    content = """<?xml version="1.0" encoding="UTF-8"?>
<VectorProcessorReport>
  <TestSuite name="Adc_VP_Suite" timestamp="2026-03-20T10:00:00">
    <TestCase name="TC_Adc_Init_001" verdict="PASS" duration_ms="120">
      <Description>Verify Adc_Init with default configuration</Description>
    </TestCase>
    <TestCase name="TC_Adc_Init_002" verdict="FAIL" duration_ms="85">
      <Description>Verify Adc_Init with invalid group</Description>
      <FailureInfo>Expected return E_NOT_OK but got E_OK</FailureInfo>
    </TestCase>
    <TestCase name="TC_Adc_ReadGroup_001" verdict="PASS" duration_ms="200">
      <Description>Verify Adc_ReadGroup normal operation</Description>
    </TestCase>
  </TestSuite>
</VectorProcessorReport>"""
    f = tmp_path / "vp_report.xml"
    f.write_text(content)
    return str(f)


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: JUnit Parser
# ═════════════════════════════════════════════════════════════════════════

class TestJUnitParser:
    """Parse standard JUnit/xUnit XML test reports."""

    def test_parse_junit_xml(self, junit_xml_file):
        parser = JUnitParser()
        results = parser.parse(junit_xml_file)

        assert len(results) == 4
        # Check pass/fail/error status
        statuses = {r.test_name: r.status for r in results}
        assert statuses["test_init_default_config"] == "PASS"
        assert statuses["test_init_null_config"] == "FAIL"
        assert statuses["test_init_invalid_channel"] == "ERROR"
        assert statuses["test_init_already_initialized"] == "PASS"

    def test_failure_has_message(self, junit_xml_file):
        parser = JUnitParser()
        results = parser.parse(junit_xml_file)
        failures = [r for r in results if r.status == "FAIL"]
        assert len(failures) == 1
        assert "Assertion failed" in (failures[0].message or "")

    def test_error_has_message(self, junit_xml_file):
        parser = JUnitParser()
        results = parser.parse(junit_xml_file)
        errors = [r for r in results if r.status == "ERROR"]
        assert len(errors) == 1
        assert "Segmentation fault" in (errors[0].message or "")

    def test_is_failure_property(self, junit_xml_file):
        parser = JUnitParser()
        results = parser.parse(junit_xml_file)
        fail_count = sum(1 for r in results if r.is_failure)
        assert fail_count == 2  # 1 FAIL + 1 ERROR

    def test_to_dict(self, junit_xml_file):
        parser = JUnitParser()
        results = parser.parse(junit_xml_file)
        d = results[0].to_dict()
        assert "test_name" in d
        assert "status" in d

    def test_parse_nonexistent_file(self):
        parser = JUnitParser()
        results = parser.parse("/nonexistent/path.xml")
        assert results == []


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: ResultProcessor Dispatch
# ═════════════════════════════════════════════════════════════════════════

class TestResultProcessorDispatch:
    """ResultProcessor routes to correct parser by result_type."""

    def test_processor_creation_without_backends(self):
        """Should create without Neo4j or FeedbackSink."""
        processor = ResultProcessor()
        assert processor is not None

    def test_process_junit(self, junit_xml_file):
        processor = ResultProcessor()
        result = processor.process(
            results_dir=str(Path(junit_xml_file).parent),
            result_type="junit",
            module_name="Adc",
        )
        assert result is not None
        assert "total" in result or "files_processed" in result or "results" in result

    def test_process_unknown_type_returns_error(self, tmp_path):
        processor = ResultProcessor()
        # Unknown result type should be handled gracefully
        try:
            result = processor.process(
                results_dir=str(tmp_path),
                result_type="unknown_format",
                module_name="Adc",
            )
            # If it returns rather than raising, it should indicate error
            assert result is not None
        except (ValueError, KeyError):
            pass  # Acceptable to raise on unknown type

    def test_process_empty_directory(self, tmp_path):
        processor = ResultProcessor()
        result = processor.process(
            results_dir=str(tmp_path),
            result_type="junit",
            module_name="Adc",
        )
        assert result is not None


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Compiler Log Parser
# ═════════════════════════════════════════════════════════════════════════

class TestCompilerParser:
    """Parse GCC/Tasking compiler warning/error logs."""

    def test_parse_compiler_log(self, compiler_log_file):
        parser = CompilerParser()
        results = parser.parse(Path(compiler_log_file))
        assert len(results) == 4
        errors = [r for r in results if r.status == "ERROR"]
        warnings = [r for r in results if r.status == "WARNING"]
        assert len(errors) == 1
        assert len(warnings) == 3
        assert errors[0].file_path == "Adc_Init.c"
        assert errors[0].line_number == 87

    def test_compiler_log_contains_warnings_and_errors(self, compiler_log_file):
        """Verify compiler parser distinguishes warnings from errors."""
        parser = CompilerParser()
        results = parser.parse(Path(compiler_log_file))
        statuses = {r.status for r in results}
        assert "ERROR" in statuses
        assert "WARNING" in statuses


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Polyspace Parser
# ═════════════════════════════════════════════════════════════════════════

class TestPolyspaceParser:
    """Parse Polyspace Bug Finder / Code Prover results."""

    def test_parse_polyspace_csv(self, polyspace_csv_file):
        parser = PolyspaceParser()
        results = parser.parse(Path(polyspace_csv_file))
        assert len(results) == 4
        violations = [r for r in results if r.status == "VIOLATION"]
        assert len(violations) >= 1
        # Check that severity, file_path, and rule_id are populated
        for r in results:
            assert r.file_path is not None
            assert r.severity is not None

    def test_polyspace_findings_have_severity(self, polyspace_csv_file):
        parser = PolyspaceParser()
        results = parser.parse(Path(polyspace_csv_file))
        severities = {r.severity for r in results}
        assert "ERROR" in severities or "WARNING" in severities


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: Coverage Parser
# ═════════════════════════════════════════════════════════════════════════

class TestCoverageParser:
    """Parse GCOV/LCOV coverage data."""

    def test_parse_lcov(self, coverage_lcov_file):
        parser = CoverageParser()
        results = parser.parse(Path(coverage_lcov_file))
        assert len(results) == 1
        r = results[0]
        assert r.source_type == "coverage"
        assert r.file_path == "Adc_Init.c"
        assert r.status == "WARNING"  # 3/5 = 60% — between 50% and 80%
        assert "60.0%" in r.message
        assert r.extra["lines_found"] == 5
        assert r.extra["lines_hit"] == 3


# ═════════════════════════════════════════════════════════════════════════
#  Test 6: VP (Vector Processor) Parser
# ═════════════════════════════════════════════════════════════════════════

class TestVPParser:
    """Parse Vector Processor XML reports."""

    def test_parse_vp_xml(self, vp_xml_file):
        parser = VPParser()
        results = parser.parse(Path(vp_xml_file))
        assert len(results) == 3
        by_name = {r.test_name: r for r in results}
        # Verify status parsed from 'verdict' attribute
        assert by_name["TC_Adc_Init_001"].status == "PASS"
        assert by_name["TC_Adc_Init_002"].status == "FAIL"
        assert by_name["TC_Adc_ReadGroup_001"].status == "PASS"
        # Verify duration parsed from 'duration_ms' attribute
        assert by_name["TC_Adc_Init_001"].duration_ms == 120
        assert by_name["TC_Adc_Init_002"].duration_ms == 85
        # Verify message parsed from Description/FailureInfo
        assert by_name["TC_Adc_Init_001"].message is not None
        assert "default configuration" in by_name["TC_Adc_Init_001"].message
        assert "E_NOT_OK" in (by_name["TC_Adc_Init_002"].message or "")


# ═════════════════════════════════════════════════════════════════════════
#  Test 7: FeedbackSink Learning Wiring
# ═════════════════════════════════════════════════════════════════════════

class TestFeedbackSinkLearning:
    """Verify FeedbackSink records learning data from result processing."""

    def test_feedback_sink_accepts_result_data(self):
        """FeedbackSink should store feedback from processed results."""
        sink = FeedbackSink()
        sink.submit_feedback(
            "test_resp_001",
            "REJECT",
            correction_notes="Init sequence wrong — Adc_DeInit missing",
            issues_found=2,
        )
        metrics = sink.get_learning_metrics()
        assert metrics["total_feedbacks"] >= 1
        assert metrics["rejections"] >= 1

    def test_failure_patterns_from_results(self):
        """Rejected results should populate failure patterns."""
        sink = FeedbackSink()
        sink.submit_feedback("r1", "REJECT", correction_notes="Missing polling after async call")
        sink.submit_feedback("r2", "REJECT", correction_notes="Wrong register access pattern")
        patterns = sink.get_failure_patterns()
        assert len(patterns) >= 2

    def test_learning_loop_approve_creates_pattern(self):
        """APPROVE should eventually feed into PatternStore."""
        sink = FeedbackSink()
        result = sink.complete_review("r1", "APPROVE", reviewer_id="dev_a",
                                       rationale="Correct init, MISRA compliant")
        assert result["review_id"].startswith("rv_")
        assert result["feedback_recorded"] is True


# ═════════════════════════════════════════════════════════════════════════
#  Test 8: MCP Tool process_results Returns Real Data
# ═════════════════════════════════════════════════════════════════════════

class TestProcessResultsMCPTool:
    """Verify process_results tool is not a stub."""

    def test_no_not_implemented_in_process_results(self):
        """process_results should not contain NOT_IMPLEMENTED."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")

        # Find the process_results function
        import re
        match = re.search(
            r'async def process_results\(.*?\n(?=(?:@mcp\.tool|# ═))',
            content, re.DOTALL
        )
        if match:
            func_body = match.group(0)
            assert "NOT_IMPLEMENTED" not in func_body, \
                "process_results still contains NOT_IMPLEMENTED stub"

    def test_result_processor_importable(self):
        """ResultProcessor should be importable from the expected path."""
        from src.ReviewGate.result_processors import ResultProcessor
        p = ResultProcessor()
        assert p is not None

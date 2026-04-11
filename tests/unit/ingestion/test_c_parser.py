"""Tests for the C Source Code Parser (c_parser).

Covers both parsing backends:
- ``regex``: lightweight regex-based extraction (no external deps).
- ``clang``: libclang-based AST parser (requires ``pip install libclang``).

A small sample C file is written to a temp directory for each test session.
"""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

# Allow running from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Parsers import c_parser

# ---------------------------------------------------------------------------
# Sample C source used by all tests
# ---------------------------------------------------------------------------

SAMPLE_C_CODE = textwrap.dedent("""\
    #include <stdint.h>

    /* Module-level comment */

    static int helper_add(int a, int b) {
        return a + b;
    }

    void simple_function(int x) {
        int y = helper_add(x, 1);
        if (y > 10) {
            helper_add(y, 2);
        }
    }

    typedef enum {
        OP_READ  = 0,
        OP_WRITE = 1,
        OP_RESET = 2
    } Operation_t;

    void dispatcher(Operation_t op) {
        switch (op) {
            case OP_READ:
                helper_add(1, 2);
                break;
            case OP_WRITE:
                simple_function(42);
                break;
            default:
                break;
        }
    }
""")


@pytest.fixture(scope="session")
def sample_c_file(tmp_path_factory):
    """Write SAMPLE_C_CODE to a temp .c file once per test session."""
    tmp = tmp_path_factory.mktemp("c_parser_tests")
    c_file = tmp / "sample.c"
    c_file.write_text(SAMPLE_C_CODE, encoding="utf-8")
    return str(c_file)


# ===================================================================
# Regex-based parser tests
# ===================================================================

class TestRegexParser:
    """Tests for method='regex'."""

    def test_returns_functions_and_statistics(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="regex")
        assert "functions" in result
        assert "statistics" in result

    def test_statistics_total_functions(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="regex")
        stats = result["statistics"]
        # helper_add, simple_function, dispatcher
        assert stats["total_functions"] >= 3

    def test_detects_internal_calls(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="regex")
        funcs = result["functions"]
        # simple_function calls helper_add
        assert "simple_function" in funcs
        calls = funcs["simple_function"].get("internal_calls", [])
        called_names = [c["function"] for c in calls]
        assert "helper_add" in called_names

    def test_detects_switch_case_calls(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="regex")
        funcs = result["functions"]
        assert "dispatcher" in funcs
        calls = funcs["dispatcher"].get("internal_calls", [])
        # Should detect case-based calls
        assert len(calls) > 0
        # At least one entry should be a switch_case_calls type
        assert any(c.get("type") == "switch_case_calls" for c in calls)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            c_parser.parse("nonexistent_file_12345.c", method="regex")


# ===================================================================
# Clang-based parser tests
# ===================================================================

# Skip the entire clang class if libclang is not installed
clang_available = pytest.mark.skipif(
    not c_parser.LIBCLANG_AVAILABLE,
    reason="libclang not installed (pip install libclang)",
)


@clang_available
class TestClangParser:
    """Tests for method='clang' (default)."""

    def test_returns_ast_and_functions(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        assert "ast" in result
        assert "functions" in result
        assert "diagnostics" in result
        assert "statistics" in result

    def test_statistics_parse_method(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        assert result["statistics"]["parse_method"] == "clang"

    def test_finds_all_functions(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        funcs = result["functions"]
        assert "helper_add" in funcs
        assert "simple_function" in funcs
        assert "dispatcher" in funcs

    def test_function_has_parameters(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        helper = result["functions"]["helper_add"]
        params = helper.get("parameters", [])
        assert len(params) == 2
        param_names = {p["name"] for p in params}
        assert param_names == {"a", "b"}

    def test_function_has_return_type(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        helper = result["functions"]["helper_add"]
        assert helper.get("return_type") == "int"

    def test_ast_root_has_children(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        ast = result["ast"]
        assert "children" in ast
        assert len(ast["children"]) > 0

    def test_ast_nodes_have_kind_and_spelling(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        ast = result["ast"]

        def _check(node):
            assert "kind" in node
            assert "spelling" in node
            for child in node.get("children", []):
                _check(child)

        _check(ast)

    def test_ast_contains_function_definitions(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        ast = result["ast"]

        func_kinds = []

        def _collect(node):
            if node.get("kind") == "function_definition":
                func_kinds.append(node.get("spelling") or node.get("name"))
            for child in node.get("children", []):
                _collect(child)

        _collect(ast)
        assert "helper_add" in func_kinds
        assert "simple_function" in func_kinds
        assert "dispatcher" in func_kinds

    def test_ast_contains_switch_statement(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        ast = result["ast"]

        found_switch = False

        def _find(node):
            nonlocal found_switch
            if node.get("kind") == "switch_statement":
                found_switch = True
            for child in node.get("children", []):
                _find(child)

        _find(ast)
        assert found_switch, "Expected a switch_statement node in the AST"

    def test_ast_contains_call_expressions(self, sample_c_file):
        result = c_parser.parse(sample_c_file, method="clang")
        ast = result["ast"]

        call_targets = []

        def _find(node):
            if node.get("kind") == "call_expression":
                call_targets.append(node.get("spelling", ""))
            for child in node.get("children", []):
                _find(child)

        _find(ast)
        assert "helper_add" in call_targets

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            c_parser.parse("nonexistent_file_12345.c", method="clang")

    def test_include_paths_accepted(self, sample_c_file):
        """Passing include_paths should not raise."""
        result = c_parser.parse(
            sample_c_file, method="clang", include_paths=[os.path.dirname(sample_c_file)]
        )
        assert "ast" in result


# ===================================================================
# Default method / edge-case tests
# ===================================================================

class TestParseDefaults:
    """Tests for default behaviour and invalid inputs."""

    def test_default_method_is_clang_when_available(self, sample_c_file):
        """parse() should use clang when available, otherwise fall back to regex."""
        result = c_parser.parse(sample_c_file)
        if c_parser.LIBCLANG_AVAILABLE:
            assert "ast" in result
            assert result["statistics"]["parse_method"] == "clang"
        else:
            assert "functions" in result
            assert result["statistics"]["parse_method"] == "regex"

    def test_invalid_method_raises(self, sample_c_file):
        with pytest.raises(ValueError, match="Unknown parsing method"):
            c_parser.parse(sample_c_file, method="magic")

    def test_regex_and_clang_find_same_function_names(self, sample_c_file):
        """Both backends should discover the same set of defined functions."""
        if not c_parser.LIBCLANG_AVAILABLE:
            pytest.skip("libclang not installed")

        regex_result = c_parser.parse(sample_c_file, method="regex")
        clang_result = c_parser.parse(sample_c_file, method="clang")

        regex_funcs = set(regex_result["functions"].keys())
        clang_funcs = set(clang_result["functions"].keys())

        # The regex parser may skip functions with no calls/register accesses,
        # but every regex-detected function should also appear in clang output.
        assert regex_funcs.issubset(clang_funcs), (
            f"Regex found functions not in clang: {regex_funcs - clang_funcs}"
        )

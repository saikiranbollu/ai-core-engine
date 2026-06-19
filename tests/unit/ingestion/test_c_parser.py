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

from IngestionPipeline.parsers import c_parser

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


# ===================================================================
# Enhanced global_refs tests (direct, passed-to-callee, alias)
# ===================================================================

GLOBAL_REFS_C_CODE = textwrap.dedent("""\
    /* Minimal types */
    typedef unsigned int uint32;
    typedef unsigned char uint8;

    typedef struct {
        uint32 status;
        uint32 count;
    } PartitionDataType;

    typedef struct {
        const PartitionDataType *PartitionPtr;
    } ConfigType;

    /* ── Global variables ── */
    static PartitionDataType GlobalData;
    static uint32 GlobalCounter;
    static const ConfigType *ConfigPtr;
    static uint32 *GlobalPtr;

    /* ── Helper functions ── */
    static void helper_read_only(const PartitionDataType *const ptr) {
        volatile uint32 x = ptr->status;
    }

    static void helper_read_write(PartitionDataType *ptr) {
        ptr->count = 0;
    }

    /* ── Direct access ── */
    static void func_direct_read(void) {
        volatile uint32 x = GlobalCounter;
    }

    static void func_direct_write(void) {
        GlobalCounter = 42;
    }

    /* ── Passed-to-callee ── */
    static void func_pass_const(void) {
        helper_read_only(&GlobalData);
    }

    static void func_pass_mutable(void) {
        helper_read_write(&GlobalData);
    }

    /* ── Local alias ── */
    static void func_alias_read(void) {
        const PartitionDataType *local_ptr = &GlobalData;
        volatile uint32 x = local_ptr->status;
    }

    static void func_alias_write(void) {
        PartitionDataType *local_ptr = &GlobalData;
        local_ptr->count = 99;
    }

    /* ── Mixed: direct + alias + pass ── */
    static void func_mixed(void) {
        uint32 val = GlobalCounter;
        PartitionDataType *ptr = &GlobalData;
        ptr->count = val;
        helper_read_only(&GlobalData);
    }

    /* ── Scalar copy (not alias) ── */
    static void func_scalar_copy(void) {
        uint32 localVal = GlobalCounter;
        localVal = localVal + 1;
    }

    /* ── Increment/decrement (read+write) ── */
    static void func_increment(void) {
        GlobalCounter++;
    }

    static void func_prefix_decrement(void) {
        --GlobalCounter;
    }

    /* ── Dereference write (Issue 11): *GlobalPtr = val ── */
    static void func_deref_write(void) {
        *GlobalPtr = 42;
    }

    /* ── Pointer chain patterns (Issue 11/7) ── */
    static PartitionDataType *GlobalPtrArray[4];

    static void func_ptr_array_chain_read(void) {
        /* GlobalPtrArray[i]->field = val: array is READ (navigates chain) */
        GlobalPtrArray[0]->count = 99;
    }

    static void func_ptr_array_element_write(void) {
        /* GlobalPtrArray[i] = val: array element IS written */
        GlobalPtrArray[0] = &GlobalData;
    }

    static void func_ptr_deref_chain_read(void) {
        /* *GlobalPtrArray[i]->field: array is READ */
        volatile uint32 x = GlobalPtrArray[0]->status;
    }

    static void func_ptr_arrow_field_write(void) {
        /* GlobalPtr->field: pointer is READ, write goes to pointed-to struct */
        ConfigPtr->PartitionPtr;
    }

    static void func_ptr_array_no_assign_read(void) {
        /* GlobalPtrArray[i] used as RHS: array is READ */
        PartitionDataType *local = GlobalPtrArray[1];
        volatile uint32 x = local->status;
    }
""")


@pytest.fixture(scope="session")
def global_refs_c_file(tmp_path_factory):
    """Write GLOBAL_REFS_C_CODE to a temp .c file."""
    tmp = tmp_path_factory.mktemp("global_refs_tests")
    c_file = tmp / "global_refs.c"
    c_file.write_text(GLOBAL_REFS_C_CODE, encoding="utf-8")
    return str(c_file)


@pytest.mark.skipif(not c_parser.LIBCLANG_AVAILABLE,
                    reason="libclang not installed")
class TestEnhancedGlobalRefs:
    """Tests for the three-layer global variable detection."""

    def _get_refs(self, global_refs_c_file, func_name):
        """Parse the test file and return global_refs for *func_name*."""
        result = c_parser.parse(global_refs_c_file, method="clang")
        func = result["functions"].get(func_name, {})
        return func.get("global_refs", [])

    # ── Direct access ──────────────────────────────────────────────

    def test_direct_read(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_direct_read")
        names = [r["name"] for r in refs]
        assert "GlobalCounter" in names
        gr = next(r for r in refs if r["name"] == "GlobalCounter")
        assert gr["access_type"] == "READ"
        assert gr["access_context"] == "DIRECT"

    def test_direct_write(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_direct_write")
        names = [r["name"] for r in refs]
        assert "GlobalCounter" in names
        wr = next(r for r in refs if r["name"] == "GlobalCounter")
        assert wr["access_type"] == "WRITE"
        assert wr["access_context"] == "DIRECT"

    # ── Passed to callee ──────────────────────────────────────────

    def test_pass_to_const_callee(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_pass_const")
        passed = [r for r in refs
                  if r.get("access_context") == "PASSED_TO_CALLEE"]
        assert len(passed) >= 1
        gr = next(r for r in passed if r["name"] == "GlobalData")
        assert gr["callee"] == "helper_read_only"
        assert gr["access_type"] == "READ"  # const param

    def test_pass_to_mutable_callee(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_pass_mutable")
        passed = [r for r in refs
                  if r.get("access_context") == "PASSED_TO_CALLEE"]
        assert len(passed) >= 1
        gr = next(r for r in passed if r["name"] == "GlobalData")
        assert gr["callee"] == "helper_read_write"
        assert gr["access_type"] == "READ_WRITE"  # non-const param

    # ── Local alias ───────────────────────────────────────────────

    def test_alias_read(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_alias_read")
        aliases = [r for r in refs if r.get("access_context") == "ALIAS"]
        assert len(aliases) >= 1
        assert any(r["name"] == "GlobalData" for r in aliases)

    def test_alias_write(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_alias_write")
        aliases = [r for r in refs if r.get("access_context") == "ALIAS"]
        assert len(aliases) >= 1
        assert any(r["name"] == "GlobalData" for r in aliases)

    # ── Mixed scenario ────────────────────────────────────────────

    def test_mixed_all_three_contexts(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_mixed")
        contexts = {r.get("access_context") for r in refs}
        # Should contain at least DIRECT (GlobalCounter) and one of
        # ALIAS or PASSED_TO_CALLEE (GlobalData)
        assert "DIRECT" in contexts
        names = {r["name"] for r in refs}
        assert "GlobalCounter" in names
        assert "GlobalData" in names

    def test_mixed_has_direct_and_passed(self, global_refs_c_file):
        refs = self._get_refs(global_refs_c_file, "func_mixed")
        passed = [r for r in refs
                  if r.get("access_context") == "PASSED_TO_CALLEE"]
        assert len(passed) >= 1

    # ── All refs have required keys ───────────────────────────────

    def test_all_refs_have_access_context(self, global_refs_c_file):
        result = c_parser.parse(global_refs_c_file, method="clang")
        for fname, fdata in result["functions"].items():
            for gr in fdata.get("global_refs", []):
                assert "access_context" in gr, (
                    f"Missing access_context in {fname}: {gr}"
                )
                assert gr["access_context"] in (
                    "DIRECT", "PASSED_TO_CALLEE", "ALIAS"
                ), f"Unexpected access_context in {fname}: {gr['access_context']}"

    def test_passed_refs_have_callee(self, global_refs_c_file):
        result = c_parser.parse(global_refs_c_file, method="clang")
        for fname, fdata in result["functions"].items():
            for gr in fdata.get("global_refs", []):
                if gr["access_context"] == "PASSED_TO_CALLEE":
                    assert "callee" in gr and gr["callee"], (
                        f"Missing callee in PASSED_TO_CALLEE ref in {fname}"
                    )

    def test_alias_refs_have_alias_local(self, global_refs_c_file):
        result = c_parser.parse(global_refs_c_file, method="clang")
        for fname, fdata in result["functions"].items():
            for gr in fdata.get("global_refs", []):
                if gr["access_context"] == "ALIAS":
                    assert "alias_local" in gr and gr["alias_local"], (
                        f"Missing alias_local in ALIAS ref in {fname}"
                    )

    # ── Scalar copy should NOT produce ALIAS context ──────────────

    def test_scalar_copy_not_alias(self, global_refs_c_file):
        """A scalar copy (uint32 localVal = GlobalCounter) must not
        generate ALIAS refs — only DIRECT."""
        refs = self._get_refs(global_refs_c_file, "func_scalar_copy")
        aliases = [r for r in refs if r.get("access_context") == "ALIAS"]
        assert len(aliases) == 0, (
            f"Scalar copy should not produce ALIAS refs: {aliases}"
        )
        # GlobalCounter should still appear as DIRECT
        direct = [r for r in refs if r.get("access_context") == "DIRECT"]
        assert any(r["name"] == "GlobalCounter" for r in direct)

    # ── Increment/decrement detection ─────────────────────────────

    def test_postfix_increment_is_write(self, global_refs_c_file):
        """GlobalCounter++ should be detected as WRITE (read+write)."""
        refs = self._get_refs(global_refs_c_file, "func_increment")
        names = [r["name"] for r in refs]
        assert "GlobalCounter" in names
        gr = next(r for r in refs if r["name"] == "GlobalCounter")
        assert gr["access_type"] == "WRITE"

    def test_prefix_decrement_is_write(self, global_refs_c_file):
        """--GlobalCounter should be detected as WRITE (read+write)."""
        refs = self._get_refs(global_refs_c_file, "func_prefix_decrement")
        names = [r["name"] for r in refs]
        assert "GlobalCounter" in names
        gr = next(r for r in refs if r["name"] == "GlobalCounter")
        assert gr["access_type"] == "WRITE"

    # ── Dereference write (Issue 11) ──────────────────────────────

    def test_deref_write_is_read(self, global_refs_c_file):
        """*GlobalPtr = 42 is a READ of GlobalPtr (provides address),
        the WRITE goes to pointed-to memory."""
        refs = self._get_refs(global_refs_c_file, "func_deref_write")
        names = [r["name"] for r in refs]
        assert "GlobalPtr" in names
        gr = next(r for r in refs if r["name"] == "GlobalPtr")
        assert gr["access_type"] == "READ", (
            "*ptr = val should classify ptr as READ, not WRITE"
        )

    # ── Pointer chain patterns (Issue 11/7) ───────────────────────

    def test_ptr_array_chain_is_read(self, global_refs_c_file):
        """GlobalPtrArray[i]->field = val: array is READ (chain navigation)."""
        refs = self._get_refs(global_refs_c_file, "func_ptr_array_chain_read")
        gpa = [r for r in refs if r["name"] == "GlobalPtrArray"]
        assert len(gpa) >= 1, "Should detect GlobalPtrArray reference"
        for r in gpa:
            assert r["access_type"] == "READ", (
                f"ptr_array[i]->field = val should be READ, got {r['access_type']}"
            )

    def test_ptr_array_element_write(self, global_refs_c_file):
        """GlobalPtrArray[i] = val: array element IS written."""
        refs = self._get_refs(global_refs_c_file, "func_ptr_array_element_write")
        gpa = [r for r in refs if r["name"] == "GlobalPtrArray"]
        assert len(gpa) >= 1, "Should detect GlobalPtrArray reference"
        writes = [r for r in gpa if r["access_type"] == "WRITE"]
        assert len(writes) >= 1, (
            "ptr_array[i] = val should have at least one WRITE"
        )

    def test_ptr_array_no_assign_is_read(self, global_refs_c_file):
        """GlobalPtrArray[i] used on RHS: array is READ."""
        refs = self._get_refs(global_refs_c_file, "func_ptr_array_no_assign_read")
        gpa = [r for r in refs if r["name"] == "GlobalPtrArray"]
        assert len(gpa) >= 1, "Should detect GlobalPtrArray reference"
        for r in gpa:
            assert r["access_type"] == "READ", (
                f"ptr_array[i] on RHS should be READ, got {r['access_type']}"
            )


# ===================================================================
# Register access extractor tests (Issue 9: SWPMSK + SFRWRITE)
# ===================================================================

class TestRegisterAccessExtractor:
    """Tests for _RegisterAccessExtractor SWPMSK detection."""

    def test_swpmsk_standalone_emits_read_and_write(self):
        """MCALUTIL_SWPMSK with register.U argument → both READ and WRITE."""
        code = '    MCALUTIL_SWPMSK(&EvaAdcSFR->ADC_CLKEN.U, mask, val);\n'
        extractor = c_parser._RegisterAccessExtractor(code)
        accesses = extractor.extract_function_accesses("test", code)
        regs = [a for a in accesses if a["register"] == "ADC_CLKEN"]
        types = {a["access_type"] for a in regs}
        assert "READ" in types, f"Expected READ in {types}"
        assert "WRITE" in types, f"Expected WRITE in {types}"

    def test_sfrwrite_with_swpmsk_on_same_line(self):
        """MCALUTIL_SFRWRITE + MCALUTIL_SWPMSK on same line → register in both READ and WRITE."""
        code = '    MCALUTIL_SFRWRITE(EvaAdcSFR->ADC_CLKEN_TMADC.U, MCALUTIL_SWPMSK(regval, mask, val));\n'
        extractor = c_parser._RegisterAccessExtractor(code)
        accesses = extractor.extract_function_accesses("test", code)
        regs = [a for a in accesses if a["register"] == "ADC_CLKEN_TMADC"]
        types = {a["access_type"] for a in regs}
        assert "READ" in types, f"Expected READ for SWPMSK-assisted SFRWRITE, got: {types}"
        assert "WRITE" in types, f"Expected WRITE for SFRWRITE, got: {types}"

    def test_sfrwrite_without_swpmsk(self):
        """Plain MCALUTIL_SFRWRITE without SWPMSK → WRITE present (READ may also come from Pattern 1)."""
        code = '    MCALUTIL_SFRWRITE(EvaAdcSFR->ADC_CFG.U, configval);\n'
        extractor = c_parser._RegisterAccessExtractor(code)
        accesses = extractor.extract_function_accesses("test", code)
        regs = [a for a in accesses if a["register"] == "ADC_CFG"]
        types = {a["access_type"] for a in regs}
        assert "WRITE" in types

    def test_swpmsk_with_volatile_cast_emits_read_and_write(self):
        """MCALUTIL_SWPMSK with (volatile uint32 *) cast → both READ and WRITE."""
        code = '    (void)MCALUTIL_SWPMSK((volatile uint32 *)&ADC_CLKEN_TMADC.U, ~HwClockEn, HwClockEn);\n'
        extractor = c_parser._RegisterAccessExtractor(code)
        accesses = extractor.extract_function_accesses("test", code)
        regs = [a for a in accesses if a["register"] == "ADC_CLKEN_TMADC"]
        types = {a["access_type"] for a in regs}
        assert "READ" in types, f"Expected READ for SWPMSK with cast, got: {types}"
        assert "WRITE" in types, f"Expected WRITE for SWPMSK with cast, got: {types}"


# ===================================================================
# Struct member access direction tests (Issue 8: ++ detection)
# ===================================================================

class TestStructMemberWriteDetection:
    """Tests for _is_write_on_line and _is_read_on_line heuristics."""

    def test_postfix_increment_is_write(self):
        """field++ should be detected as write."""
        src_lines = ["    GrpData->CurrSampCount++;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "CurrSampCount")
        assert result is True

    def test_postfix_increment_is_also_read(self):
        """field++ should also be read (increment reads before writing)."""
        src_lines = ["    GrpData->CurrSampCount++;\n"]
        result = c_parser._ClangAnalyzer._is_read_on_line(src_lines, 1, "CurrSampCount")
        assert result is True

    def test_prefix_increment_is_write(self):
        """++field should be detected as write."""
        src_lines = ["    ++GrpData->CurrSampCount;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "CurrSampCount")
        assert result is True

    def test_prefix_decrement_is_write(self):
        """--field should be detected as write."""
        src_lines = ["    --GrpData->CurrSampCount;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "CurrSampCount")
        assert result is True

    def test_compound_assign_is_write(self):
        """field += val should be detected as write."""
        src_lines = ["    GrpData->CurrSampCount += 1;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "CurrSampCount")
        assert result is True

    def test_simple_read_is_not_write(self):
        """Reading a field should NOT be write."""
        src_lines = ["    x = GrpData->CurrSampCount;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "CurrSampCount")
        assert result is False

    def test_pointer_subscript_assign_is_not_write(self):
        """ptr[x] = val means ptr is READ (dereferenced), not WRITE."""
        src_lines = ["    GrpData->ResultBufferPtr[idx] = adcResult;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ResultBufferPtr")
        assert result is False, "ptr[x] = val should be READ of the pointer, not WRITE"

    def test_array_subscript_assign_is_write(self):
        """array[x] = val means the array member IS written (Issue 20)."""
        src_lines = ["    HwUnitDataPtr->ActiveSRMap[SRId] = Group;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ActiveSRMap")
        assert result is True, "array[idx] = val should be WRITE for non-pointer array member"

    def test_array_subscript_compound_assign_is_write(self):
        """array[x] |= val means the array member IS written."""
        src_lines = ["    DataPtr->ActiveChMask[HwUnitIndex] |= ChMask;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ActiveChMask")
        assert result is True

    def test_array_subscript_deref_is_not_write(self):
        """array[x]->member = val means array is READ (provides pointer)."""
        src_lines = ["    HwUnitGroupMapPtr[HwUnitIndex]->HwUnitDataPtr = &Data;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "HwUnitGroupMapPtr")
        assert result is False

    def test_pointer_subscript_assign_is_read(self):
        """ptr[x] = val means ptr is READ (dereferenced to get address)."""
        src_lines = ["    GrpData->ResultBufferPtr[idx] = adcResult;\n"]
        result = c_parser._ClangAnalyzer._is_read_on_line(src_lines, 1, "ResultBufferPtr")
        assert result is True

    def test_pointer_arrow_deref_is_not_write(self):
        """ptr->member = val means ptr is READ, not WRITE."""
        src_lines = ["    GrpData->ChannelPtr->Status = ADC_IDLE;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ChannelPtr")
        assert result is False

    def test_direct_pointer_assign_is_write(self):
        """ptr = newAddr IS a write to the pointer field itself."""
        src_lines = ["    GrpData->ResultBufferPtr = newBuffer;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ResultBufferPtr")
        assert result is True

    # -- Issue 11: Leading * dereference patterns --

    def test_leading_deref_chain_field_is_not_write(self):
        """*chain->field = val means field is READ (provides address for deref)."""
        src_lines = ["    *Adc_kData[PartitionId]->ConfigPtr = ConfigPtr;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ConfigPtr")
        assert result is False, "*chain->field = val: field is READ, write goes to *field"

    def test_leading_deref_chain_field_is_read(self):
        """*chain->field = val means field is READ."""
        src_lines = ["    *Adc_kData[PartitionId]->ConfigPtr = ConfigPtr;\n"]
        result = c_parser._ClangAnalyzer._is_read_on_line(src_lines, 1, "ConfigPtr")
        assert result is True

    def test_leading_deref_with_cast_is_not_write(self):
        """*(type*)chain->field = val means field is READ."""
        src_lines = ["    *(uint32*)RuntimeInfoPtr->StatusPtr = 0U;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "StatusPtr")
        assert result is False

    def test_no_leading_deref_field_is_write(self):
        """chain->field = val (no leading *) IS a write to the field."""
        src_lines = ["    Adc_kData[PartitionId]->ConfigPtr = newConfig;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ConfigPtr")
        assert result is True

    # -- Issue 24/27/29: Local variable shadowing struct member name --

    def test_local_shadow_ptr_assign_is_not_write(self):
        """LocalVar = Struct->LocalVar — field on RHS is READ, not WRITE."""
        src_lines = ["    HwTrigDataPtr = RuntimeInfoPtr->HwTrigDataPtr;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "HwTrigDataPtr")
        assert result is False, "RHS struct member is read, not written"

    def test_local_shadow_ptr_assign_is_read(self):
        """LocalVar = Struct->LocalVar — field on RHS IS a read."""
        src_lines = ["    HwTrigDataPtr = RuntimeInfoPtr->HwTrigDataPtr;\n"]
        result = c_parser._ClangAnalyzer._is_read_on_line(src_lines, 1, "HwTrigDataPtr")
        assert result is True

    def test_local_shadow_config_ptr_is_not_write(self):
        """ConfigPtr = Adc_kData->ConfigPtr — field on RHS is READ."""
        src_lines = ["    ConfigPtr = Adc_kData[PartitionId]->ConfigPtr;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "ConfigPtr")
        assert result is False

    def test_local_shadow_group_map_ptr_is_not_write(self):
        """HwUnitGroupMapPtr = Data->HwUnitGroupMapPtr — READ."""
        src_lines = ["    HwUnitGroupMapPtr = PartitionData->HwUnitGroupMapPtr;\n"]
        result = c_parser._ClangAnalyzer._is_write_on_line(src_lines, 1, "HwUnitGroupMapPtr")
        assert result is False


class TestDerefTargetAccess:
    """Tests for _get_deref_target_access — detecting writes THROUGH pointers."""

    def test_simple_assign_through_deref(self):
        """*chain->PtrField = val → target is WRITE."""
        src_lines = ["    *PartitionDataPtr->ActiveCdspCoreMapPtr = Group;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveCdspCoreMapPtr"
        )
        assert result == "WRITE"

    def test_compound_or_assign_through_deref(self):
        """*chain->PtrField |= val → target is READ_WRITE."""
        src_lines = ["    *HwTrigDataPtr->ActiveEruErsChMaskPtr |= EruErsChMask;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveEruErsChMaskPtr"
        )
        assert result == "READ_WRITE"

    def test_compound_and_assign_through_deref(self):
        """*chain->PtrField &= ~val → target is READ_WRITE."""
        src_lines = ["    *HwTrigDataPtr->ActiveEruErsChMaskPtr &= ~EruErsChMask;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveEruErsChMaskPtr"
        )
        assert result == "READ_WRITE"

    def test_simple_assign_zero_through_deref(self):
        """*chain->PtrField = (Type)0 → target is WRITE."""
        src_lines = ["    *PartitionDataPtr->ActiveCdspCoreMapPtr = (Adc_GroupType)0;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveCdspCoreMapPtr"
        )
        assert result == "WRITE"

    def test_read_through_deref_no_assign(self):
        """Group = *chain->PtrField → no deref write (just a read)."""
        src_lines = ["    Group = *PartitionDataPtr->ActiveCdspCoreMapPtr;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveCdspCoreMapPtr"
        )
        assert result == ""

    def test_compare_through_deref(self):
        """*chain->PtrField == 0U → no deref write."""
        src_lines = ["    if (*PartitionDataPtr->ActiveCdspCoreMapPtr == 0U)\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveCdspCoreMapPtr"
        )
        assert result == ""

    def test_ptr_subscript_assign(self):
        """chain->PtrField[idx] = val → target is WRITE (Pattern B)."""
        src_lines = ["    PartitionDataPtr->ActiveDmaChMapPtr[CoreId] = Group;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveDmaChMapPtr"
        )
        assert result == "WRITE"

    def test_ptr_subscript_compound_assign(self):
        """chain->PtrField[idx] |= val → target is READ_WRITE (Pattern B)."""
        src_lines = ["    HwTrigDataPtr->ActiveTimerGCChMaskPtr[idx] |= mask;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveTimerGCChMaskPtr"
        )
        assert result == "READ_WRITE"

    def test_non_ptr_subscript_not_detected(self):
        """chain->ArrayField[idx] = val — non-Ptr suffix → empty (handled by _is_write)."""
        src_lines = ["    GrpDataPtr->ActiveSRMap[CoreId] = Group;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveSRMap"
        )
        assert result == ""

    def test_no_deref_plain_assign(self):
        """chain->Field = val (no leading *, not Ptr suffix) → empty."""
        src_lines = ["    GrpDataPtr->CurrSampCount = 0;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "CurrSampCount"
        )
        assert result == ""

    def test_deref_with_dot_access(self):
        """*chain.PtrField = val → target is WRITE."""
        src_lines = ["    *ConfigData.ActiveBufferPtr = value;\n"]
        result = c_parser._ClangAnalyzer._get_deref_target_access(
            src_lines, 1, "ActiveBufferPtr"
        )
        assert result == "WRITE"

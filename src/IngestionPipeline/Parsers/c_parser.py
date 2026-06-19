"""
C Source Code Parser
====================

Parses C source files to extract function bodies, internal call graphs,
register access patterns (READ/WRITE), and switch-case structures.

Supports two parsing backends:
- **regex** (default): Lightweight regex-based extraction.
- **clang**: libclang-based parsing that produces an abstract syntax tree (AST).

Usage:
    from IngestionPipeline.parsers import c_parser

    # Regex-based (default)
    result = c_parser.parse("path/to/source.c")

    # Clang-based AST
    result = c_parser.parse("path/to/source.c", method="clang")
"""

import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    import clang.cindex
    from clang.cindex import CursorKind, TypeKind, TranslationUnit
    LIBCLANG_AVAILABLE = True
except ImportError:
    LIBCLANG_AVAILABLE = False
    clang = None

logger = logging.getLogger(__name__)


def _find_libclang_dll() -> Optional[str]:
    """Try to locate the bundled libclang shared library.

    Searches common locations:
    1. ``<site-packages>/clang/native/libclang.dll`` (pip install libclang)
    2. LLVM installation via ``LLVM_HOME`` or ``PATH``

    Returns the path as a string, or *None* if not found.
    """
    if not LIBCLANG_AVAILABLE:
        return None

    # 1. Bundled inside the clang Python package
    try:
        clang_pkg_dir = os.path.dirname(clang.cindex.__file__)
        candidates = [
            os.path.join(clang_pkg_dir, "native", "libclang.dll"),
            os.path.join(clang_pkg_dir, "native", "libclang.so"),
            os.path.join(clang_pkg_dir, "native", "libclang.dylib"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    except Exception:
        pass

    # 2. LLVM_HOME environment variable
    llvm_home = os.environ.get("LLVM_HOME")
    if llvm_home:
        dll = os.path.join(llvm_home, "bin", "libclang.dll")
        if os.path.isfile(dll):
            return dll

    return None


class _RegisterAccessExtractor:
    """Extract register READ/WRITE access patterns from C source code."""

    def __init__(self, c_content: str):
        self.c_content = c_content

    def extract_function_accesses(self, func_name: str, func_body: str) -> List[Dict[str, Any]]:
        accesses: List[Dict[str, Any]] = []

        # Pattern 1: Direct SFR pointer access  (e.g. EvaAdcSFR->REG.B.FIELD)
        access_pattern = re.compile(
            r'(\w+(?:SFR|ChSFR|Ch))->(\w+(?:\[\w+(?:\[\w+\])?\])?)\.(B|U)(?:\.(\w+))?'
        )
        # Pattern 1b: Generic register pointer access with array subscript
        # Matches: Ptr[idx]->REG.U, Ptr[idx].MEMBER.U, Ptr[idx].ARR[n].REG.U
        generic_ptr_pattern = re.compile(
            r'\w+Ptr(?:\[\w+\])+[.-]>?'           # pointer with subscript(s)
            r'(\w+(?:\[\w+\])?(?:\.\w+(?:\[\w+\])?)*)'  # register path (REG or A[n].REG)
            r'\.(B|U)(?:\.(\w+))?'                 # .U or .B.FIELD
        )
        # Pattern 2: MCALUTIL macro access  (e.g. MCALUTIL_SFRWRITE(REG.U, val))
        mcal_write_pattern = re.compile(
            r'MCALUTIL_SFRWRITE\s*\(\s*([\w>\-\[\]\.]+?)\.(B|U)\s*,'
        )
        mcal_read_pattern = re.compile(
            r'MCALUTIL_SFRREAD\s*\(\s*\w+\s*,\s*([\w>\-\[\]\.]+?)\.(B|U)\s*\)'
        )
        # Pattern 2b: MCALUTIL_SWPMSK (swap-with-mask, read-modify-write)
        mcal_swpmsk_pattern = re.compile(
            r'MCALUTIL_SWPMSK\s*\(\s*(?:\([^)]*\)\s*)?&?\s*([\w>\-\[\]\.]+?)\.(B|U)\s*,'
        )
        # Pattern 3: Module SFR macros  (e.g. ADC_SFR_RUNTIME_WRITE32(REG.U, val))
        sfr_macro_write = re.compile(
            r'\w+_SFR_(?:RUNTIME|INIT_DEINIT)_WRITE(?:32)?\s*\(\s*([\w>\-\[\]\.]+?)\.(B|U)\s*,'
        )
        sfr_macro_read = re.compile(
            r'\w+_SFR_(?:RUNTIME|INIT_DEINIT)_READ(?:32)?\s*\(\s*([\w>\-\[\]\.]+?)\.(B|U)\s*\)'
        )
        compound_ops = re.compile(r'\s*(\||&|\^|\+|-|\*|/|<<|>>)=')

        for line_idx, line in enumerate(func_body.split('\n'), 1):
            for match in access_pattern.finditer(line):
                register = match.group(2)
                field = match.group(4) if match.group(4) else 'U'
                rest = line[match.end():]

                if compound_ops.match(rest):
                    accesses.append({"register": register, "field": field, "access_type": "READ", "line": line_idx})
                    accesses.append({"register": register, "field": field, "access_type": "WRITE", "line": line_idx})
                elif re.match(r'^\s*=(?!=)', rest):
                    accesses.append({"register": register, "field": field, "access_type": "WRITE", "line": line_idx})
                else:
                    accesses.append({"register": register, "field": field, "access_type": "READ", "line": line_idx})

            # Pattern 1b: Generic register pointer (e.g. Dma_RegBasePtr[n]->PROTSE.U)
            for match in generic_ptr_pattern.finditer(line):
                reg_path = match.group(1)
                # Strip array subscripts then split on dots: ACCGRP[n].PROTE → ACCGRP_PROTE
                reg_no_idx = re.sub(r'\[\w+\]', '', reg_path)
                parts = [p for p in reg_no_idx.split('.') if p]
                register = '_'.join(parts)
                field = match.group(3) if match.group(3) else 'U'
                rest = line[match.end():]

                if compound_ops.match(rest):
                    accesses.append({"register": register, "field": field, "access_type": "READ", "line": line_idx})
                    accesses.append({"register": register, "field": field, "access_type": "WRITE", "line": line_idx})
                elif re.match(r'^\s*=(?!=)', rest):
                    accesses.append({"register": register, "field": field, "access_type": "WRITE", "line": line_idx})
                else:
                    accesses.append({"register": register, "field": field, "access_type": "READ", "line": line_idx})

            # Pattern 2: MCALUTIL_SFRWRITE / MCALUTIL_SFRREAD macros
            # Check if MCALUTIL_SWPMSK appears on this line (read-modify-write)
            line_has_swpmsk = 'MCALUTIL_SWPMSK' in line
            for match in mcal_write_pattern.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "WRITE", "line": line_idx})
                # If SWPMSK is on the same line, the write target is also read
                # (SWPMSK = read-modify-write: read current value, mask, write back)
                if line_has_swpmsk:
                    accesses.append({"register": register, "field": "U", "access_type": "READ", "line": line_idx})
            for match in mcal_read_pattern.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "READ", "line": line_idx})
            # Pattern 2b: MCALUTIL_SWPMSK (read-modify-write)
            for match in mcal_swpmsk_pattern.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "READ", "line": line_idx})
                accesses.append({"register": register, "field": "U", "access_type": "WRITE", "line": line_idx})

            # Pattern 3: Module-specific SFR macros
            for match in sfr_macro_write.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "WRITE", "line": line_idx})
            for match in sfr_macro_read.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "READ", "line": line_idx})

        return accesses


class _CSourceAnalyzer:
    """Analyse a C source file and return structured data."""

    _KEYWORDS = {
        'if', 'else', 'while', 'for', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return', 'goto', 'sizeof', 'typeof',
        'void', 'int', 'float', 'double', 'char', 'struct', 'union',
        'typedef', 'const', 'static', 'extern', 'volatile', 'inline',
        'auto', 'register', 'restrict', 'aligned', 'defined',
    }
    _CONTROL_FLOW = {'if', 'else', 'while', 'for', 'do', 'switch', 'case', 'default'}

    def __init__(self):
        self._pat_func = re.compile(r'(\w+)\s*\([^)]*\)\s*\{')
        self._pat_calls = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')
        self._pat_switch = re.compile(r'switch\s*\([^)]*\)\s*\{')
        self._pat_case = re.compile(r'case\s+([^:]+):|default\s*:')

    # ------------------------------------------------------------------
    def analyze(self, content: str) -> Dict[str, Any]:
        clean = self._strip_comments(content)
        all_funcs = self._extract_functions(clean)

        functions_data: Dict[str, Any] = {}
        funcs_with_reg = 0
        total_reg = total_r = total_w = 0

        for name, body in all_funcs.items():
            patterns = self._extract_patterns(name, body, content)
            if patterns:
                functions_data[name] = patterns
                regs = patterns.get("register_accesses", [])
                if regs:
                    funcs_with_reg += 1
                    total_reg += len(regs)
                    total_r += sum(1 for a in regs if a.get("access_type") == "READ")
                    total_w += sum(1 for a in regs if a.get("access_type") == "WRITE")

        return {
            "functions": functions_data,
            "statistics": {
                "total_functions": len(all_funcs),
                "functions_with_register_accesses": funcs_with_reg,
                "total_register_accesses": total_reg,
                "register_read_accesses": total_r,
                "register_write_accesses": total_w,
                "parse_method": "regex",
            },
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _strip_comments(code: str) -> str:
        code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
        code = re.sub(r'//.*?$', '', code, flags=re.MULTILINE)
        return code

    def _extract_functions(self, clean: str) -> Dict[str, str]:
        funcs: Dict[str, str] = {}
        for m in self._pat_func.finditer(clean):
            name = m.group(1)
            if name in self._CONTROL_FLOW:
                continue
            start = m.end() - 1
            depth, pos = 1, start + 1
            while pos < len(clean) and depth:
                if clean[pos] == '{':
                    depth += 1
                elif clean[pos] == '}':
                    depth -= 1
                pos += 1
            if depth == 0:
                funcs[name] = clean[start:pos]
        return funcs

    def _extract_patterns(self, name: str, body: str, raw: str) -> Optional[Dict[str, Any]]:
        start_line = raw.count('\n', 0, raw.find(f'{name}(')) + 1
        calls = self._extract_calls(body, name)
        regs = _RegisterAccessExtractor(raw).extract_function_accesses(name, body)

        if not calls and not regs:
            return None

        result: Dict[str, Any] = {"start_line": start_line}
        if regs:
            result["register_accesses"] = regs
        if calls:
            result["internal_calls"] = calls
        return result

    def _extract_calls(self, body: str, current: str) -> List[Dict[str, Any]]:
        if self._pat_switch.search(body):
            return self._switch_calls(body, current)
        return self._regular_calls(body, current)

    def _regular_calls(self, body: str, current: str) -> List[Dict[str, Any]]:
        calls, order, seen = [], 0, set()
        for m in self._pat_calls.finditer(body):
            fn = m.group(1)
            if fn == current or fn in self._KEYWORDS or len(fn) < 2 or fn in seen:
                continue
            calls.append({"function": fn, "order": order, "line": m.start()})
            order += 1
            seen.add(fn)
        return calls[:100]

    def _switch_calls(self, body: str, current: str) -> List[Dict[str, Any]]:
        sm = self._pat_switch.search(body)
        if not sm:
            return []

        start = sm.end() - 1
        depth, end = 1, start
        for i in range(start + 1, len(body)):
            if body[i] == '{':
                depth += 1
            elif body[i] == '}':
                depth -= 1
            if depth == 0:
                end = i + 1
                break

        block = body[sm.start():end]
        cases = list(self._pat_case.finditer(block))
        result = []

        for idx, cm in enumerate(cases):
            label = cm.group(1).strip() if cm.group(1) else "default"
            c_start = cm.end()
            c_end = cases[idx + 1].start() if idx + 1 < len(cases) else len(block)
            chunk = block[c_start:c_end]

            calls, order, seen = [], 0, set()
            for m in self._pat_calls.finditer(chunk):
                fn = m.group(1)
                if fn == current or fn in self._KEYWORDS or len(fn) < 2 or fn in seen:
                    continue
                calls.append({"function": fn, "order": order, "case": label})
                order += 1
                seen.add(fn)
            if calls:
                result.append({"case": label, "case_value": label, "calls": calls, "type": "switch_case_calls"})

        return result


_analyzer = _CSourceAnalyzer()


# ---------------------------------------------------------------------------
# Clang-based AST analyser
# ---------------------------------------------------------------------------

class _ClangAnalyzer:
    """Parse a C source file with libclang and return an abstract syntax tree."""

    # CursorKind values mapped to human-readable node types
    _KIND_MAP = {
        CursorKind.FUNCTION_DECL: "function_definition",
        CursorKind.PARM_DECL: "parameter",
        CursorKind.VAR_DECL: "variable_declaration",
        CursorKind.TYPEDEF_DECL: "typedef",
        CursorKind.STRUCT_DECL: "struct",
        CursorKind.UNION_DECL: "union",
        CursorKind.ENUM_DECL: "enum",
        CursorKind.ENUM_CONSTANT_DECL: "enum_constant",
        CursorKind.FIELD_DECL: "field",
        CursorKind.COMPOUND_STMT: "compound_statement",
        CursorKind.IF_STMT: "if_statement",
        CursorKind.FOR_STMT: "for_statement",
        CursorKind.WHILE_STMT: "while_statement",
        CursorKind.DO_STMT: "do_statement",
        CursorKind.SWITCH_STMT: "switch_statement",
        CursorKind.CASE_STMT: "case_statement",
        CursorKind.DEFAULT_STMT: "default_statement",
        CursorKind.RETURN_STMT: "return_statement",
        CursorKind.BREAK_STMT: "break_statement",
        CursorKind.CONTINUE_STMT: "continue_statement",
        CursorKind.CALL_EXPR: "call_expression",
        CursorKind.BINARY_OPERATOR: "binary_operator",
        CursorKind.UNARY_OPERATOR: "unary_operator",
        CursorKind.INTEGER_LITERAL: "integer_literal",
        CursorKind.FLOATING_LITERAL: "floating_literal",
        CursorKind.STRING_LITERAL: "string_literal",
        CursorKind.DECL_REF_EXPR: "decl_ref",
        CursorKind.MEMBER_REF_EXPR: "member_ref",
        CursorKind.ARRAY_SUBSCRIPT_EXPR: "array_subscript",
        CursorKind.CONDITIONAL_OPERATOR: "ternary_operator",
        CursorKind.COMPOUND_ASSIGNMENT_OPERATOR: "compound_assignment",
        CursorKind.GOTO_STMT: "goto_statement",
        CursorKind.LABEL_STMT: "label_statement",
        CursorKind.NULL_STMT: "null_statement",
        CursorKind.DECL_STMT: "decl_statement",
        CursorKind.UNEXPOSED_EXPR: "expression",
        CursorKind.PAREN_EXPR: "paren_expression",
        CursorKind.CXX_UNARY_EXPR: "unary_expression",
        CursorKind.INCLUSION_DIRECTIVE: "include_directive",
        CursorKind.MACRO_DEFINITION: "macro_definition",
        CursorKind.MACRO_INSTANTIATION: "macro_expansion",
    } if LIBCLANG_AVAILABLE else {}

    def __init__(self, libclang_path: Optional[str] = None,
                 include_paths: Optional[List[str]] = None,
                 skip_default_stubs: bool = False,
                 initializer_map: Any = None):
        if not LIBCLANG_AVAILABLE:
            raise ImportError(
                "clang.cindex is not available.  Install it with: pip install libclang"
            )

        # Resolve the native library: explicit path > auto-discovery
        resolved = libclang_path if (libclang_path and os.path.exists(libclang_path)) else _find_libclang_dll()
        if resolved:
            try:
                clang.cindex.Config.set_library_file(resolved)
            except Exception:
                pass  # already set in this process — ignore

        self._index = clang.cindex.Index.create()
        self._include_paths: List[str] = include_paths or []
        self._skip_default_stubs: bool = skip_default_stubs
        self._initializer_map = initializer_map  # ConfigStructResolver or None

    # ------------------------------------------------------------------
    def analyze(self, file_path: str) -> Dict[str, Any]:
        """Parse *file_path* and return structured analysis.

        Returns a dict with keys:
        - ``ast``: Recursive dict representation of the clang AST.
        - ``functions``: Dict mapping function name → analysis data.
          Each function entry contains ``parameters``, ``return_type``,
          ``sfr_accesses``, ``global_refs``, ``register_accesses``
          (legacy format), and ``internal_calls``.
        - ``diagnostics``: Clang warnings / errors encountered.
        - ``statistics``: Aggregate counts.
        """
        tu = self._parse_file(file_path)
        src_path = os.path.abspath(file_path)

        diagnostics = self._collect_diagnostics(tu)

        # Pre-scan headers for write-wrapper macros (used by _is_write_context)
        self._build_write_macro_cache(src_path)

        # Build the recursive AST dict (backward-compatible)
        ast = self._cursor_to_dict(tu.cursor, src_path)

        # Walk the raw AST cursors (not dict) for semantic extraction
        func_cursors = self._collect_function_cursors(tu.cursor, src_path)

        functions: Dict[str, Any] = {}
        total_sfr = 0
        total_globals = 0
        total_calls = 0

        for fc in func_cursors:
            name = fc.spelling
            loc = fc.location
            start_line = loc.line if loc.file else 0

            sfr_accesses = self._extract_sfr_accesses(fc, src_path)
            global_refs = self._extract_global_refs(fc, src_path)
            internal_calls = self._extract_internal_calls(fc, src_path, name)

            # ── Critical section annotation for SFR accesses ──────────
            # Phase 5 inside _extract_global_refs already annotates global_refs.
            # Apply the same annotation to sfr_accesses so SRC_ACCESSES_SFR
            # edges also carry in_critical_section / critical_section_name.
            if sfr_accesses:
                try:
                    extent = fc.extent
                    cs_ranges = self._detect_critical_sections(
                        src_path, extent.start.line, extent.end.line
                    )
                    if cs_ranges:
                        self._annotate_critical_sections(sfr_accesses, cs_ranges)
                    else:
                        for sa in sfr_accesses:
                            sa.setdefault("in_critical_section", False)
                            sa.setdefault("critical_section_name", "")
                except Exception:
                    for sa in sfr_accesses:
                        sa.setdefault("in_critical_section", False)
                        sa.setdefault("critical_section_name", "")

            # Build legacy register_accesses format from sfr_accesses
            register_accesses = [
                {
                    "register": sa["register"],
                    "field": sa.get("field", "U"),
                    "access_type": sa["access_type"],
                    "line": sa.get("line", 0),
                }
                for sa in sfr_accesses
            ]

            # Extract parameters and return type from cursor
            params = []
            for arg in fc.get_arguments():
                params.append({"name": arg.spelling, "type": arg.type.spelling})
            return_type = fc.result_type.spelling if fc.result_type else ""

            func_entry: Dict[str, Any] = {
                "start_line": start_line,
                "parameters": params,
                "return_type": return_type,
            }
            if sfr_accesses:
                func_entry["sfr_accesses"] = sfr_accesses
            if global_refs:
                func_entry["global_refs"] = global_refs
            if register_accesses:
                func_entry["register_accesses"] = register_accesses
            if internal_calls:
                func_entry["internal_calls"] = internal_calls

            # Branch-conditional propagation: extract branch map, constant
            # call-site args, and annotate global_refs with branch_ids.
            branch_map = self._extract_branch_map(fc, src_path)
            call_constant_args = self._extract_call_constant_args(
                fc, src_path, name
            )
            # Only include branch data when there are param-conditional branches
            has_param_branches = any(
                b.get("condition_param") for b in branch_map if b["branch_id"] != "B0"
            )
            if has_param_branches and global_refs:
                self._annotate_refs_with_branch(global_refs, branch_map)
                func_entry["branch_map"] = branch_map
            if call_constant_args:
                func_entry["call_constant_args"] = call_constant_args

            functions[name] = func_entry
            total_sfr += len(sfr_accesses)
            total_globals += len(global_refs)
            total_calls += len(internal_calls)

        return {
            "ast": ast,
            "functions": functions,
            "diagnostics": diagnostics,
            "statistics": {
                "total_functions": len(functions),
                "total_sfr_accesses": total_sfr,
                "total_global_refs": total_globals,
                "total_internal_calls": total_calls,
                "total_diagnostics": len(diagnostics),
                "parse_method": "clang",
            },
        }

    # ------------------------------------------------------------------
    # Semantic extraction from raw AST cursors
    # ------------------------------------------------------------------

    def _build_write_macro_cache(self, src_path: str) -> None:
        """Scan header files in include paths for macros wrapping SFRWRITE/SWPMSK.

        Populates ``self._write_macros`` (set of macro names whose body contains
        MCALUTIL_SFRWRITE or SFR_*_WRITE patterns) and ``self._swpmsk_macros``
        (set of macro names whose body contains MCALUTIL_SWPMSK).
        These are used by ``_is_write_context`` to correctly classify register
        accesses that go through wrapper macros.
        """
        write_macros: set = set()
        swpmsk_macros: set = set()

        # Scan all .h files in include paths
        dirs_to_scan = list(self._include_paths)
        # Also scan the directory of the source file itself
        src_dir = os.path.dirname(src_path)
        if src_dir and src_dir not in dirs_to_scan:
            dirs_to_scan.append(src_dir)

        for inc_dir in dirs_to_scan:
            if not os.path.isdir(inc_dir):
                continue
            for fname in os.listdir(inc_dir):
                if not fname.endswith(".h"):
                    continue
                hpath = os.path.join(inc_dir, fname)
                try:
                    with open(hpath, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except Exception:
                    continue
                i = 0
                while i < len(lines):
                    line = lines[i]
                    m = re.match(r"#define\s+(\w+)", line)
                    if m:
                        macro_name = m.group(1)
                        # Collect full macro body (continuation lines)
                        full_def = line
                        while line.rstrip().endswith("\\") and i + 1 < len(lines):
                            i += 1
                            line = lines[i]
                            full_def += " " + line
                        if re.search(
                            r"MCALUTIL_SFRWRITE|SFR_(?:RUNTIME|INIT_DEINIT)_WRITE",
                            full_def,
                        ):
                            write_macros.add(macro_name)
                        if re.search(r"MCALUTIL_SWPMSK", full_def):
                            swpmsk_macros.add(macro_name)
                    i += 1

        self._write_macros = write_macros
        self._swpmsk_macros = swpmsk_macros

    @staticmethod
    def _collect_function_cursors(root_cursor: Any, src_path: str) -> List[Any]:
        """Return all function-definition cursors in the source file."""
        results = []
        for child in root_cursor.get_children():
            if (child.kind == CursorKind.FUNCTION_DECL
                    and child.is_definition()
                    and child.location.file
                    and os.path.abspath(child.location.file.name) == src_path):
                results.append(child)
        return results

    def _extract_sfr_accesses(self, func_cursor: Any, src_path: str) -> List[Dict[str, Any]]:
        """Walk function AST to find SFR register accesses via MEMBER_REF_EXPR.

        Detects two patterns:
        1. Bitfield access: ``reg.B.FIELD`` — parent type ``Ifx_*_Bits``
        2. Raw register access: ``reg.U`` — parent type ``Ifx_*`` register union

        Both resolve to KG ``SFR_Register.name`` convention (e.g. ``ADC_CLC``).
        """
        accesses: List[Dict[str, Any]] = []
        seen: set = set()  # dedup (line, register, field, access_type)

        def _is_sfr_bits_type(type_spelling: str) -> bool:
            """Check if a type name is an SFR bitfield struct."""
            return (type_spelling.startswith("Ifx_")
                    and type_spelling.endswith("_Bits"))

        def _is_sfr_register_union(type_spelling: str) -> bool:
            """Check if a type name is an SFR register union (e.g. Ifx_ADC_CLC).

            Register unions start with ``Ifx_`` and contain at least one
            underscore in the body (i.e. ``Ifx_MODULE_REG``), but do NOT
            end with ``_Bits``.
            """
            if not type_spelling.startswith("Ifx_"):
                return False
            if type_spelling.endswith("_Bits"):
                return False
            # Must have module_register structure: Ifx_MODULE_REG
            body = type_spelling[4:]  # strip "Ifx_"
            return "_" in body  # at least one underscore → MODULE_REG pattern

        def _resolve_register_name(type_spelling: str, member_name: str = "") -> str:
            """Convert SFR type to KG register name.

            ``Ifx_ADC_CLC_Bits`` → ``ADC_CLC``
            ``Ifx_ADC_CLC``      → ``ADC_CLC``

            When *member_name* is given (the struct field name from the parent
            module register struct, e.g. ``PROTSE``), use it to build the
            register name if it differs from the type-derived name.  This
            handles the case where multiple register instances share the same
            underlying type (e.g. ``PROT`` and ``PROTSE`` both use
            ``Ifx_DMA_PROT``).
            """
            type_name = type_spelling.replace("Ifx_", "").replace("_Bits", "")
            if not member_name:
                return type_name
            # Extract module prefix from type: "DMA_PROT" → "DMA"
            parts = type_name.split("_", 1)
            if len(parts) == 2:
                module_prefix = parts[0]
                type_reg_suffix = parts[1]
                # If member name differs from what the type gives us, prefer it
                if member_name.upper() != type_reg_suffix.upper():
                    # For EGTM types with deep hierarchy (CLS_ATOM_*, CLS_TOM_*),
                    # the member_name IS the register name — don't prefix with EGTM.
                    # e.g. type "EGTM_CLS_ATOM_AGC_ENDIS_CTRL" + member "ENDIS_CTRL"
                    #      → return "ENDIS_CTRL" (not "EGTM_ENDIS_CTRL")
                    # This allows ENDS WITH matching against SFR nodes like
                    # "EGTM_CLS0_ATOM_AGC_ENDIS_CTRL".
                    if (type_reg_suffix.upper().startswith("CLS_") and
                            type_reg_suffix.upper().endswith("_" + member_name.upper())):
                        return member_name
                    return f"{module_prefix}_{member_name}"
            return type_name

        # Pre-read source lines for macro-based write detection
        try:
            with open(src_path, 'r', encoding='utf-8', errors='replace') as _f:
                _src_lines = _f.readlines()
        except Exception:
            _src_lines = []

        # Local refs to instance macro caches for closure access
        _write_macros = getattr(self, '_write_macros', set())
        _swpmsk_macros = getattr(self, '_swpmsk_macros', set())

        def _get_access_type(cursor: Any) -> str:
            """Determine register access type: 'READ', 'WRITE', or 'READ_WRITE'.

            Strategies:
            1. Check source line for direct SFRWRITE/SFRREAD/SWPMSK patterns
            2. Check source line for known write-wrapper macros (from header scan)
            3. Check source line for known swap-mask macros (read-modify-write)
            4. Token-based heuristic for direct assignment operators
            """
            line_no = cursor.location.line
            if 0 < line_no <= len(_src_lines):
                src_line = _src_lines[line_no - 1]
                # Strategy 1a: Direct SFRWRITE on source line
                if re.search(r'SFRWRITE|SFR_(?:RUNTIME|INIT_DEINIT)_WRITE', src_line):
                    return "WRITE"
                # Strategy 1b: Direct SFRREAD on source line
                if re.search(r'SFRREAD|SFR_(?:RUNTIME|INIT_DEINIT)_READ', src_line):
                    return "READ"
                # Strategy 1c: Direct SWPMSK on source line (read-modify-write)
                if re.search(r'MCALUTIL_SWPMSK', src_line):
                    return "READ_WRITE"
                # Strategy 2: Source line uses a wrapper macro known to call SFRWRITE
                if _write_macros:
                    for macro in _write_macros:
                        if macro in src_line:
                            return "WRITE"
                # Strategy 3: Source line uses a wrapper macro known to call SWPMSK
                if _swpmsk_macros:
                    for macro in _swpmsk_macros:
                        if macro in src_line:
                            return "READ_WRITE"

            # Strategy 4: Token-based heuristic for non-macro assignments
            try:
                extent = cursor.extent
                tu = cursor.translation_unit
                all_tokens = list(tu.get_tokens(extent=extent))
                for tok in all_tokens:
                    if tok.spelling in ('=', '|=', '&=', '^=', '<<=', '>>=', '+=', '-='):
                        return "WRITE"
            except Exception:
                pass
            return "READ"

        def _walk(cursor: Any):
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            if cursor.kind == CursorKind.MEMBER_REF_EXPR:
                # Check the type of the object being accessed
                # For expr like ptr->REG.B.FIELD, the parent struct type tells us
                # if it's an SFR
                try:
                    obj_type = ""
                    for child in cursor.get_children():
                        if child.type and child.type.spelling:
                            obj_type = child.type.spelling
                            break
                    if not obj_type:
                        obj_type = cursor.type.spelling if cursor.type else ""

                    # Check if this member ref or its parent chain involves SFR types
                    type_canonical = ""
                    if cursor.type:
                        type_canonical = cursor.type.get_canonical().spelling

                    # Direct check: is this member of an Ifx_*_Bits struct?
                    ref_type = cursor.referenced
                    parent_type_name = ""
                    if ref_type and ref_type.semantic_parent:
                        parent_type_name = ref_type.semantic_parent.spelling

                    if _is_sfr_bits_type(parent_type_name):
                        # For ptr->REG.B.FIELD, try to resolve the instance
                        # member name (REG) from the grandchild cursor.
                        instance_member = ""
                        try:
                            for ch in cursor.get_children():
                                # ch is the ".B" member ref
                                if ch.kind == CursorKind.MEMBER_REF_EXPR:
                                    for gch in ch.get_children():
                                        if gch.kind == CursorKind.MEMBER_REF_EXPR:
                                            instance_member = gch.spelling
                                            break
                                    break
                        except Exception:
                            pass
                        register_name = _resolve_register_name(
                            parent_type_name, instance_member)
                        field = cursor.spelling
                        line = cursor.location.line
                        raw_access = _get_access_type(cursor)
                        # READ_WRITE produces both entries
                        access_types = (["READ", "WRITE"] if raw_access == "READ_WRITE"
                                        else [raw_access])
                        for access_type in access_types:
                            key = (line, register_name, field, access_type)
                            if key not in seen:
                                seen.add(key)
                                accesses.append({
                                    "register": register_name,
                                    "field": field,
                                    "access_type": access_type,
                                    "line": line,
                                    "struct_type": parent_type_name,
                                })
                    elif (cursor.spelling in ("U", "B")
                          and _is_sfr_register_union(parent_type_name)):
                        # Raw register access: reg.U or reg.B
                        # Get the instance member path from child cursors.
                        # For ptr->PROTSE.U, child is PROTSE → "PROTSE"
                        # For ptr->ACCGRP[n].PROTE.U, child is PROTE,
                        #   grandchild is ACCGRP[n] → "ACCGRP_PROTE"
                        instance_member = ""
                        try:
                            member_parts: list = []
                            node = cursor
                            while True:
                                children = [c for c in node.get_children()
                                            if c.kind == CursorKind.MEMBER_REF_EXPR]
                                if not children:
                                    # Check for array subscript wrapping a member ref
                                    arr_children = [
                                        c for c in node.get_children()
                                        if c.kind == CursorKind.ARRAY_SUBSCRIPT_EXPR
                                    ]
                                    if arr_children:
                                        # Inside array subscript, find the member
                                        arr_members = [
                                            c for c in arr_children[0].get_children()
                                            if c.kind == CursorKind.MEMBER_REF_EXPR
                                        ]
                                        if arr_members:
                                            member_parts.append(arr_members[0].spelling)
                                    break
                                child = children[0]
                                member_parts.append(child.spelling)
                                node = child
                            # member_parts is [PROTE, ACCGRP] for nested case
                            # or [PROTSE] for simple case. Reverse and join.
                            if member_parts:
                                instance_member = "_".join(reversed(member_parts))
                        except Exception:
                            pass
                        register_name = _resolve_register_name(
                            parent_type_name, instance_member)
                        field = cursor.spelling
                        line = cursor.location.line
                        raw_access = _get_access_type(cursor)
                        access_types = (["READ", "WRITE"] if raw_access == "READ_WRITE"
                                        else [raw_access])
                        for access_type in access_types:
                            key = (line, register_name, field, access_type)
                            if key not in seen:
                                seen.add(key)
                                accesses.append({
                                    "register": register_name,
                                    "field": field,
                                    "access_type": access_type,
                                    "line": line,
                                    "struct_type": parent_type_name,
                                })
                except Exception:
                    pass  # Skip nodes we can't resolve

            for child in cursor.get_children():
                _walk(child)

        _walk(func_cursor)

        # Also run the regex extractor as fallback for macro-based SFR access
        # patterns that clang may not resolve (MCALUTIL_SFRWRITE, etc.)
        try:
            tu = func_cursor.translation_unit
            extent = func_cursor.extent
            start = extent.start.line
            end = extent.end.line
            source = tu.get_file(func_cursor.location.file.name)
            # Read function body from file for regex patterns
            with open(src_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            body = "".join(all_lines[start - 1:end])
            regex_accesses = _RegisterAccessExtractor(body).extract_function_accesses(
                func_cursor.spelling, body
            )
            for ra in regex_accesses:
                key = (ra.get("line", 0) + start - 1, ra["register"], ra.get("field", ""), ra["access_type"])
                if key not in seen:
                    seen.add(key)
                    accesses.append({
                        "register": ra["register"],
                        "field": ra.get("field", "U"),
                        "access_type": ra["access_type"],
                        "line": ra.get("line", 0) + start - 1,
                    })
        except Exception:
            pass

        return accesses

    def _extract_global_refs(self, func_cursor: Any, src_path: str) -> List[Dict[str, Any]]:
        """Walk function AST to find references to global/extern variables.

        Detects four patterns:
        1. **Direct access**: ``DECL_REF_EXPR`` whose referenced declaration
           is at file scope (global or extern).
        2. **Passed to callee**: A global appears as an argument in a
           ``CALL_EXPR`` (e.g. ``Adc_lInit(&Adc_kData[i])``).
           Records the callee name, parameter index, and whether the
           parameter type is ``const`` (read-only) or non-const pointer
           (potential write).
        3. **Local alias**: A local variable is initialised (or assigned)
           from a global (e.g. ``ptr = Adc_kData[i]``).  Subsequent uses
           of that local are recorded as indirect accesses to the original
           global.
        4. **Struct chain** (Phase 4): Indirect accesses through config
           struct pointer chains (e.g.
           ``PartitionDataPtr->HwTrigDataPtr->ActiveEruErsChMaskPtr``),
           resolved via an initializer map built from config files.
        """
        refs: List[Dict[str, Any]] = []
        seen: set = set()  # dedup (name, line, context)

        # --- Collect local and parameter names to exclude from direct globals ---
        local_names: set = set()
        for child in func_cursor.get_children():
            if child.kind == CursorKind.PARM_DECL:
                local_names.add(child.spelling)
        for child in func_cursor.walk_preorder():
            if child.kind == CursorKind.VAR_DECL:
                if (child.semantic_parent
                        and child.semantic_parent.kind == CursorKind.FUNCTION_DECL):
                    local_names.add(child.spelling)

        # --- Helpers -----------------------------------------------------------

        def _is_file_scope_var(ref_cursor: Any) -> bool:
            """Return True when *ref_cursor* points to a file-scope VAR_DECL."""
            if not ref_cursor or ref_cursor.kind != CursorKind.VAR_DECL:
                return False
            parent = ref_cursor.semantic_parent
            if parent and parent.kind == CursorKind.TRANSLATION_UNIT:
                return True
            try:
                if ref_cursor.storage_class and ref_cursor.storage_class.name == 'EXTERN':
                    return True
            except Exception:
                pass
            return False

        # Pre-read source lines for write-context detection
        try:
            with open(src_path, 'r', encoding='utf-8', errors='replace') as _gf:
                _global_src_lines = _gf.readlines()
        except Exception:
            _global_src_lines = []

        def _is_write_context(cursor: Any) -> bool:
            # Strategy 1: Check source line for LHS assignment pattern
            # e.g.  "GlobalCounter = 42;"  or  "*ptr->field |= val;"
            line_no = cursor.location.line
            col = cursor.location.column  # 1-based
            if 0 < line_no <= len(_global_src_lines):
                src_line = _global_src_lines[line_no - 1]
                name = cursor.spelling or ""
                if name and col > 0:
                    # Text after the identifier on this line
                    after_name = src_line[col - 1 + len(name):].lstrip()
                    # If variable is preceded by * (dereference), any
                    # assignment goes to pointed-to memory. The variable
                    # itself is READ (provides the address).
                    before_name = src_line[:col - 1].rstrip()
                    if before_name.endswith('*'):
                        return False
                    # Strip array subscripts: GlobalArray[i] = val IS a write
                    # to the global (its contents change).
                    import re as _re_wc
                    after_name = _re_wc.sub(r'^(\s*\[[^\]]*\])+', '', after_name).lstrip()
                    if after_name and after_name[0] == '=' and (len(after_name) < 2 or after_name[1] != '='):
                        return True
                    if len(after_name) >= 2 and after_name[:2] in ('|=', '&=', '^=', '+=', '-=', '++', '--'):
                        return True
                    if len(after_name) >= 3 and after_name[:3] in ('<<=', '>>='):
                        return True
                    # Check for prefix ++/-- (e.g. ++GlobalCounter)
                    if before_name.endswith('++') or before_name.endswith('--'):
                        return True
                    # Pattern: *GlobalVar[...]->field = val
                    # The * dereferences the result of the expression, so
                    # GlobalVar itself is READ (provides the address).
                    # Do NOT treat this as a write to GlobalVar.

            # Strategy 2: Token-based heuristic (original)
            try:
                extent = cursor.extent
                tu = cursor.translation_unit
                all_tokens = list(tu.get_tokens(extent=extent))
                for tok in all_tokens:
                    if tok.spelling in ('=', '|=', '&=', '^=', '<<=', '>>=', '+=', '-=', '++', '--'):
                        return True
            except Exception:
                pass
            return False

        def _find_globals_in_subtree(cursor: Any) -> List[str]:
            """Return names of all file-scope variables referenced under *cursor*."""
            names: List[str] = []
            for node in cursor.walk_preorder():
                if (node.kind == CursorKind.DECL_REF_EXPR
                        and node.location.file
                        and os.path.abspath(node.location.file.name) == src_path):
                    ref = node.referenced
                    if ref and ref.spelling and _is_file_scope_var(ref):
                        names.append(ref.spelling)
            return names

        def _is_const_pointer(type_obj: Any) -> bool:
            """Check if a type is a const-qualified pointer (read-only param)."""
            try:
                spelling = type_obj.spelling if type_obj else ""
                # e.g.  "const Adc_ConfigType *const" → const
                if "*" in spelling and "const" in spelling.split("*")[0]:
                    return True
                pointee = type_obj.get_pointee()
                if pointee and pointee.is_const_qualified():
                    return True
            except Exception:
                pass
            return False

        # ── Phase 1: Direct global references ─────────────────────────────

        def _walk_direct(cursor: Any):
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            if cursor.kind == CursorKind.DECL_REF_EXPR:
                ref = cursor.referenced
                if ref and ref.spelling and ref.spelling not in local_names:
                    if _is_file_scope_var(ref):
                        name = ref.spelling
                        line = cursor.location.line
                        key = (name, line, "DIRECT")
                        if key not in seen:
                            seen.add(key)
                            access_type = "WRITE" if _is_write_context(cursor) else "READ"
                            refs.append({
                                "name": name,
                                "access_type": access_type,
                                "line": line,
                                "data_type": ref.type.spelling if ref.type else "",
                                "access_context": "DIRECT",
                            })

            for child in cursor.get_children():
                _walk_direct(child)

        _walk_direct(func_cursor)

        # ── Phase 2: Globals passed as call arguments ─────────────────────

        def _walk_calls(cursor: Any):
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            if cursor.kind == CursorKind.CALL_EXPR:
                callee_name = cursor.spelling
                if callee_name and len(callee_name) >= 2:
                    children = list(cursor.get_children())
                    # First child is typically the UNEXPOSED_EXPR for the
                    # callee; actual arguments start after that.
                    args = children[1:] if len(children) > 1 else []

                    # Try to get parameter types from the callee declaration
                    callee_ref = cursor.referenced
                    param_types: List[Any] = []
                    if callee_ref:
                        try:
                            param_types = list(callee_ref.get_arguments())
                        except Exception:
                            pass

                    for idx, arg in enumerate(args):
                        globals_in_arg = _find_globals_in_subtree(arg)
                        for gname in globals_in_arg:
                            line = cursor.location.line
                            key = (gname, line, "PASSED_TO:" + callee_name)
                            if key not in seen:
                                seen.add(key)
                                # Determine read/write intent from param type
                                if idx < len(param_types):
                                    ptype = param_types[idx].type
                                    if _is_const_pointer(ptype):
                                        access = "READ"
                                    else:
                                        access = "READ_WRITE"
                                else:
                                    access = "READ_WRITE"
                                refs.append({
                                    "name": gname,
                                    "access_type": access,
                                    "line": line,
                                    "data_type": "",
                                    "access_context": "PASSED_TO_CALLEE",
                                    "callee": callee_name,
                                    "param_index": idx,
                                })

            for child in cursor.get_children():
                _walk_calls(child)

        _walk_calls(func_cursor)

        # ── Phase 3: Local-from-global aliases ────────────────────────────
        # Detect two patterns:
        # A) VAR_DECL with initialiser: ``Type *ptr = &GlobalData;``
        # B) Assignment to a local: ``ptr = Adc_kData[i];``  (BINARY_OPERATOR)
        # Then scan subsequent DECL_REF_EXPR of that local to record
        # indirect accesses.

        alias_map: Dict[str, str] = {}  # local_name → global_name

        # Pattern A: VAR_DECL with initialiser containing a global
        # Only pointer locals can truly alias a global (scalar copies are not aliases).
        for node in func_cursor.walk_preorder():
            if node.kind != CursorKind.VAR_DECL:
                continue
            if not (node.semantic_parent
                    and node.semantic_parent.kind == CursorKind.FUNCTION_DECL):
                continue
            if not (node.location.file
                    and os.path.abspath(node.location.file.name) == src_path):
                continue
            # Skip non-pointer locals — scalar copies are not aliases
            if node.type.kind != TypeKind.POINTER:
                continue
            local_var_name = node.spelling
            if not local_var_name:
                continue
            # Check initialiser subtree for globals
            globals_in_init = _find_globals_in_subtree(node)
            if globals_in_init:
                alias_map[local_var_name] = globals_in_init[0]

        # Pattern B: Assignment statement — local = expr(global)
        # Clang represents ``ptr = Adc_kData[i]`` as BINARY_OPERATOR
        # or COMPOUND_ASSIGNMENT_OPERATOR with LHS → DECL_REF_EXPR(local)
        # and RHS containing DECL_REF_EXPR(global).
        def _scan_assignments(cursor: Any):
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return
            if cursor.kind in (CursorKind.BINARY_OPERATOR,
                               CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
                children = list(cursor.get_children())
                if len(children) >= 2:
                    lhs = children[0]
                    rhs = children[1]
                    # Check if LHS is a pointer-type local variable reference
                    # (scalar copies are not aliases)
                    lhs_name = None
                    if lhs.kind == CursorKind.DECL_REF_EXPR:
                        ref = lhs.referenced
                        if (ref and ref.kind == CursorKind.VAR_DECL
                                and ref.type.kind == TypeKind.POINTER
                                and ref.spelling in local_names
                                and ref.semantic_parent
                                and ref.semantic_parent.kind == CursorKind.FUNCTION_DECL):
                            lhs_name = ref.spelling
                    if lhs_name and lhs_name not in alias_map:
                        # Check if RHS contains a global
                        globals_in_rhs = _find_globals_in_subtree(rhs)
                        if globals_in_rhs:
                            alias_map[lhs_name] = globals_in_rhs[0]
            for child in cursor.get_children():
                _scan_assignments(child)

        _scan_assignments(func_cursor)

        if alias_map:
            def _walk_aliases(cursor: Any):
                if (cursor.location.file
                        and os.path.abspath(cursor.location.file.name) != src_path):
                    return

                if cursor.kind == CursorKind.DECL_REF_EXPR:
                    ref = cursor.referenced
                    if (ref and ref.spelling in alias_map
                            and ref.kind == CursorKind.VAR_DECL
                            and ref.semantic_parent
                            and ref.semantic_parent.kind == CursorKind.FUNCTION_DECL):
                        local_name = ref.spelling
                        global_name = alias_map[local_name]
                        line = cursor.location.line
                        key = (global_name, line, "ALIAS:" + local_name)
                        if key not in seen:
                            seen.add(key)
                            if _is_write_context(cursor):
                                # For pointer aliases, distinguish real writes
                                # to the global from operations that only use
                                # the global's pointer value (READ):
                                # 1. alias = ... → local reassignment, global READ
                                # 2. *alias... = val → deref write, global READ
                                # 3. alias->field = val → member write via ptr, global READ
                                access_type = "WRITE"
                                col = cursor.location.column
                                if 0 < line <= len(_global_src_lines):
                                    src_line = _global_src_lines[line - 1]
                                    after = src_line[col - 1 + len(local_name):].lstrip()
                                    before = src_line[:col - 1].rstrip()
                                    # Case 1: alias = ... (assignment TO alias)
                                    if (after and after[0] == '='
                                            and (len(after) < 2 or after[1] != '=')):
                                        access_type = "READ"
                                    # Case 2: *alias... = val (deref write)
                                    elif before.endswith('*'):
                                        access_type = "READ"
                                    # Case 3: alias->field (member access through ptr)
                                    elif after.startswith('->') or after.startswith('.'):
                                        access_type = "READ"
                            else:
                                access_type = "READ"
                            refs.append({
                                "name": global_name,
                                "access_type": access_type,
                                "line": line,
                                "data_type": "",
                                "access_context": "ALIAS",
                                "alias_local": local_name,
                            })

                for child in cursor.get_children():
                    _walk_aliases(child)

            _walk_aliases(func_cursor)

        # ── Phase 4: Struct-chain resolution ───────────────────────────
        # Resolve indirect global accesses through config struct pointer
        # chains (e.g. param->HwTrigDataPtr->ActiveEruErsChMaskPtr).
        # Requires an initializer_map built from config files.
        if self._initializer_map and not self._initializer_map.is_empty:
            self._phase4_struct_chains(func_cursor, src_path, refs, seen)

        # ── Phase 5: Critical section annotation ──────────────────────
        # Tag each global access with whether it falls inside a critical
        # section (SchM_Enter/Exit or SchMEnterFnPtr/SchMExitFnPtr).
        # DaFA team needs this for race condition / mutual access analysis.
        try:
            extent = func_cursor.extent
            func_start = extent.start.line
            func_end = extent.end.line
            cs_ranges = self._detect_critical_sections(
                src_path, func_start, func_end
            )
            if cs_ranges:
                self._annotate_critical_sections(refs, cs_ranges)
        except Exception:
            pass  # Non-fatal — don't block parsing if CS detection fails

        return refs

    # ------------------------------------------------------------------
    # Phase 4 helpers — struct-chain global resolution
    # ------------------------------------------------------------------

    def _phase4_struct_chains(
        self,
        func_cursor: Any,
        src_path: str,
        refs: List[Dict[str, Any]],
        seen: set,
    ) -> None:
        """Detect indirect global accesses via struct pointer chains."""
        resolver = self._initializer_map
        # Read source lines for write-detection heuristic
        try:
            with open(src_path, encoding="utf-8", errors="replace") as fh:
                src_lines = fh.readlines()
        except OSError:
            src_lines = []

        # Collect all MEMBER_REF_EXPR chains from the function body
        chains = self._collect_member_chains(func_cursor, src_path)

        # Deduplicate: keep only the longest chain for each (root_var, prefix)
        # so that a->b->c supersedes a->b.
        # Collect ALL lines where a chain appears so that write detection
        # checks every occurrence (Issue 8: CurrSampCount++ on one line,
        # comparison on another — need to detect write from any line).
        chain_map: dict = {}  # (root_var, *fields) → chain_info
        chain_lines: dict = {}  # same key → list of all line numbers
        for info in chains:
            key = (info["root_var"], tuple(info["fields"]))
            chain_map[key] = info
            chain_lines.setdefault(key, []).append(info["line"])

        # Remove prefix chains (a->b if a->b->c exists)
        keys_to_remove: list = []
        all_keys = list(chain_map.keys())
        for k in all_keys:
            root, fields = k[0], k[1:]
            for other in all_keys:
                if other == k:
                    continue
                o_root, o_fields = other[0], other[1:]
                if o_root == root and len(o_fields) > len(fields):
                    # Check if fields is a prefix of o_fields
                    if o_fields[:len(fields)] == fields:
                        keys_to_remove.append(k)
                        break
        for k in keys_to_remove:
            chain_map.pop(k, None)

        # Resolve each chain
        for (root_var, fields_tuple), info in chain_map.items():
            root_type = info["root_type"]
            fields = list(fields_tuple)
            is_runtime = False
            resolved = resolver.resolve_chain(root_type, fields)
            if not resolved:
                # Fallback: runtime struct — root type has no globals
                # but intermediate field types may have globals.
                field_types = info.get("field_types", [])
                if field_types:
                    resolved = self._resolve_runtime_chain(
                        resolver, fields, field_types
                    )
                    is_runtime = True
            if not resolved:
                continue

            line = info["line"]

            # Determine write context from ALL source lines where this chain
            # appears (not just the last AST occurrence).  This ensures that
            # e.g. CurrSampCount++ on one line is detected even if the last
            # occurrence is a comparison (Issue 8).
            leaf_field = fields[-1]
            all_lines = chain_lines.get((root_var, fields_tuple), [line])

            # Pre-compute leaf access_type so it can be used for the
            # container edge when the chain has exactly 1 field (DaFA fix:
            # container edge should reflect how the member is accessed,
            # not always hardcoded READ).
            is_write_early = any(
                self._is_write_on_line(src_lines, ln, leaf_field)
                for ln in all_lines
            )
            is_read_early = any(
                self._is_read_on_line(src_lines, ln, leaf_field)
                for ln in all_lines
            )
            split_rw_early = False
            has_compound_line_early = False
            if is_write_early and is_read_early:
                has_pure_write_line_early = any(
                    self._is_write_on_line(src_lines, ln, leaf_field)
                    and not self._is_read_on_line(src_lines, ln, leaf_field)
                    for ln in all_lines
                )
                split_rw_early = has_pure_write_line_early
                if split_rw_early:
                    # Check for compound access (++, |=, etc.) on any line
                    has_compound_line_early = any(
                        self._is_write_on_line(src_lines, ln, leaf_field)
                        and self._is_read_on_line(src_lines, ln, leaf_field)
                        for ln in all_lines
                    )

            # Pre-compute pointer dereference write for Ptr-suffixed fields.
            # Used for DAFA data-flow semantics: writing through a pointer
            # (ptr[i] = val) counts as WRITE to the pointer member.
            has_deref_write_early = False
            has_deref_compound_early = False
            if leaf_field.endswith("Ptr"):
                deref_accesses_early = [
                    self._get_deref_target_access(src_lines, ln, leaf_field)
                    for ln in all_lines
                ]
                has_deref_write_early = any(
                    a in ("WRITE", "READ_WRITE")
                    for a in deref_accesses_early
                )
                has_deref_compound_early = any(
                    a == "READ_WRITE" for a in deref_accesses_early
                )

            # Determine container edge access_type:
            # - Multi-field chains (len > 1): always READ (traversing
            #   intermediate pointer to reach deeper members)
            # - Single-field chains: use actual leaf member access_type
            #   so DaFA sees the real access pattern on the container struct
            if len(fields) == 1:
                if is_write_early and is_read_early and not split_rw_early:
                    container_access = "READ_WRITE"
                elif split_rw_early and has_compound_line_early:
                    # Compound access (++ etc) alongside pure writes → READ_WRITE
                    container_access = "READ_WRITE"
                elif is_write_early:
                    # Write dominates when split_rw (guard-then-write pattern)
                    container_access = "WRITE"
                elif has_deref_write_early:
                    # DAFA data-flow: ptr[i] = val counts as WRITE to ptr member
                    if has_deref_compound_early:
                        container_access = "READ_WRITE"
                    else:
                        container_access = "WRITE"
                else:
                    container_access = "READ"
            else:
                # Multi-field chains: normally READ (traversal). But when the
                # first field IS the leaf Ptr field (duplicate-field artifact
                # from Clang AST), apply deref-write semantics.
                if (has_deref_write_early
                        and leaf_field.endswith("Ptr")
                        and fields[0] == leaf_field):
                    if has_deref_compound_early:
                        container_access = "READ_WRITE"
                    else:
                        container_access = "WRITE"
                else:
                    container_access = "READ"

            # Emit the root global (e.g. Adc_kEcucPartition_0Data) with the
            # computed access_type when the chain root is a parameter pointing
            # to a known global.
            # (Skip for runtime structs — they have no globals.)
            # Include the first field in accessed_member so the DaFA analysis
            # can see which pointer member of the const struct was traversed.
            if not is_runtime:
                root_globals = resolver.get_globals_for_type(root_type)
                first_field = fields[0] if fields else ""
                for rg in root_globals:
                    key = (rg, line, "STRUCT_CHAIN", root_var, first_field)
                    if key not in seen:
                        seen.add(key)
                        refs.append({
                            "name": rg,
                            "access_type": container_access,
                            "line": line,
                            "data_type": "",
                            "access_context": "STRUCT_CHAIN",
                            "via_chain": root_var,
                            "accessed_member": first_field,
                        })

            # --- Pointer-target separation (DaFA fix) ---
            # For Ptr-suffixed leaf fields, distinguish between:
            # (a) reading the pointer value (address-obtaining) → container READ only
            # (b) dereferencing the pointer to access target data → target READ/WRITE
            # Non-Ptr fields are scalar members — always access the target directly.
            is_ptr_leaf = leaf_field.endswith("Ptr")

            # Check which lines actually dereference the pointer to access target
            if is_ptr_leaf:
                deref_lines = [
                    ln for ln in all_lines
                    if self._is_ptr_field_dereferenced(src_lines, ln, leaf_field)
                ]
                # Filter out address-obtaining subscript patterns where the
                # subscript result is cast to a struct pointer type (e.g.,
                # (Dma_ChType *)...PtrField[i]).  These read the pointer
                # value (address) from the container array, NOT target data.
                deref_lines = [
                    ln for ln in deref_lines
                    if not self._is_pointer_address_obtain(
                        src_lines, ln, leaf_field)
                ]
            else:
                deref_lines = all_lines  # scalar fields always access target

            is_write = is_write_early
            is_read = is_read_early

            # Path-sensitivity: when is_write AND is_read, check whether
            # they come from the SAME line (compound assignment like |=)
            # or DIFFERENT lines (different code paths).  If different,
            # the write is the primary operation (DaFA convention).
            split_rw = split_rw_early

            # Check for dereference writes THROUGH a pointer field.
            # When code does *chain->ptrField = val, the ptrField itself
            # is READ (provides address), but the TARGET global that the
            # pointer resolves to should be WRITE.  Compound assignments
            # (*ptr |= val) make the target READ_WRITE.
            deref_accesses = [
                self._get_deref_target_access(src_lines, ln, leaf_field)
                for ln in all_lines
            ]
            has_deref_write = any(a in ("WRITE", "READ_WRITE")
                                  for a in deref_accesses)
            has_deref_compound = any(a == "READ_WRITE"
                                     for a in deref_accesses)

            # For Ptr leaf fields: check if target data is READ through deref
            has_deref_read = False
            if is_ptr_leaf and deref_lines:
                has_deref_read = any(
                    self._get_deref_target_read(src_lines, ln, leaf_field)
                    for ln in deref_lines
                )

            for res in resolved:
                gname = res["global_name"]
                # Build per-step via_chain showing how we reach *this* global
                step_idx = res.get("step_index", 0)
                via_fields = fields[: step_idx + 1]
                # For leaf entries, append unresolved trailing fields so
                # the via_chain shows the actual member being accessed
                # (e.g. "RuntimeInfoPtr->GrpDataPtr->ResultBufferPtr").
                unresolved = res.get("unresolved_fields", [])
                if not res["is_intermediate"] and unresolved:
                    via_fields = via_fields + unresolved
                via_chain = "->".join([root_var] + via_fields)

                if res["is_intermediate"]:
                    accesses_to_emit = ["READ"]  # intermediates are always read-through
                else:
                    # --- Pointer-target separation (DaFA fix) ---
                    # For Ptr-suffixed leaf fields: only emit target access
                    # when the pointer is actually DEREFERENCED on some line.
                    # If the pointer value is merely read (address-obtaining),
                    # the container struct's READ edge (emitted above) already
                    # captures the pointer member access. No target access.
                    if is_ptr_leaf and not unresolved and not deref_lines:
                        # Pointer is never dereferenced in this function —
                        # no access to the target data.
                        accesses_to_emit = []
                    elif is_ptr_leaf and not unresolved and deref_lines:
                        # Pointer IS dereferenced — determine target access
                        # type from the dereference context.
                        if has_deref_write and has_deref_read:
                            accesses_to_emit = ["READ_WRITE"]
                        elif has_deref_write and has_deref_compound:
                            accesses_to_emit = ["READ_WRITE"]
                        elif has_deref_write:
                            accesses_to_emit = ["WRITE"]
                        elif has_deref_read:
                            accesses_to_emit = ["READ"]
                        else:
                            # Dereferenced but can't determine direction
                            # (conservative: assume READ)
                            accesses_to_emit = ["READ"]
                    elif is_write and is_read:
                        # Non-Ptr field or field with unresolved trailing
                        if split_rw:
                            # Check if any line has BOTH write AND read
                            # (compound access like ++, --). If so, the
                            # function genuinely reads-and-writes (e.g.
                            # reset + increment + compare). → READ_WRITE.
                            # If only pure writes + pure reads on DIFFERENT
                            # lines, it's guard-then-modify → WRITE.
                            has_compound_line = any(
                                self._is_write_on_line(src_lines, ln, leaf_field)
                                and self._is_read_on_line(src_lines, ln, leaf_field)
                                for ln in all_lines
                            )
                            if has_compound_line:
                                accesses_to_emit = ["READ_WRITE"]
                            else:
                                accesses_to_emit = ["WRITE"]
                        else:
                            accesses_to_emit = ["READ_WRITE"]
                    elif is_write:
                        accesses_to_emit = ["WRITE"]
                    elif has_deref_write:
                        # DAFA data-flow semantics: writing through a pointer
                        # (ptr[i] = val) counts as WRITE to the pointer member
                        # on the container struct, even when the pointer target
                        # is unresolved (e.g. user-provided buffer).
                        if unresolved:
                            if has_deref_read or has_deref_compound:
                                accesses_to_emit = ["READ_WRITE"]
                            else:
                                accesses_to_emit = ["WRITE"]
                        elif has_deref_compound:
                            accesses_to_emit = ["READ_WRITE"]
                        else:
                            accesses_to_emit = ["WRITE"]
                    else:
                        # Pointer-target isolation (Issue 21 fix):
                        # When a Ptr field resolves to a target global and
                        # the source line casts the subscript result to a
                        # struct pointer type, the access is address-obtaining
                        # only.  The container struct already has an intermediate
                        # READ edge; the target data is NOT being read.
                        if (not unresolved
                                and leaf_field.endswith("Ptr")
                                and all(
                                    self._is_pointer_address_obtain(
                                        src_lines, ln, leaf_field)
                                    for ln in all_lines)):
                            accesses_to_emit = []
                        else:
                            accesses_to_emit = ["READ"]

                # Determine the accessed struct member name.
                # For leaf entries with unresolved trailing fields,
                # use the last unresolved field (deepest member).
                # Otherwise use the last field in the chain.
                # For intermediates: set accessed_member to the NEXT field
                # in the chain (the member being read to continue traversal).
                if not res["is_intermediate"]:
                    accessed_member = (unresolved[-1] if unresolved
                                       else fields[-1])
                else:
                    # Intermediate: the field at step_idx resolves to this
                    # global; the NEXT field (step_idx+1) is what's being
                    # accessed on this struct to continue the chain.
                    next_idx = step_idx + 1
                    if next_idx < len(fields):
                        accessed_member = fields[next_idx]
                    else:
                        accessed_member = fields[step_idx] if step_idx < len(fields) else ""

                for access in accesses_to_emit:
                    key = (gname, line, "STRUCT_CHAIN", via_chain,
                           accessed_member, access)
                    if key not in seen:
                        seen.add(key)
                        refs.append({
                            "name": gname,
                            "access_type": access,
                            "line": line,
                            "data_type": "",
                            "access_context": "STRUCT_CHAIN",
                            "via_chain": via_chain,
                            "accessed_member": accessed_member,
                        })

    def _resolve_runtime_chain(
        self,
        resolver: Any,
        fields: List[str],
        field_types: List[str],
    ) -> List[Dict[str, Any]]:
        """Resolve a chain whose root type has no globals (runtime struct).

        Walk the fields until finding one whose type has known globals,
        then continue normal chain resolution from that point.
        """
        # Primitive/scalar types should never be resolved as struct chains.
        # A field of type uint8 matching a global of type uint8 is
        # coincidental, not a struct pointer relationship.
        _PRIMITIVE_TYPES = frozenset({
            "uint8", "uint16", "uint32", "uint64",
            "sint8", "sint16", "sint32", "sint64",
            "int8_t", "int16_t", "int32_t", "int64_t",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t",
            "int", "unsigned", "char", "short", "long",
            "float", "double", "boolean", "void",
            "size_t", "ptrdiff_t", "uintptr_t", "intptr_t",
            "Std_ReturnType", "StatusType",
        })

        results: List[Dict[str, Any]] = []
        for i, (field, ftype) in enumerate(zip(fields, field_types)):
            if ftype in _PRIMITIVE_TYPES:
                continue
            type_globals = resolver.get_globals_for_type(ftype)
            if not type_globals:
                continue

            # Filter globals by field-name affinity: when a field has a
            # recognizable suffix (Ptr, Map, Mask, SV), only keep globals
            # whose name contains the field's core identifier.  This avoids
            # emitting phantom edges to unrelated globals of the same type.
            type_globals = self._filter_globals_by_field(
                type_globals, field
            )

            remaining = fields[i + 1:]
            if remaining:
                inner = resolver.resolve_chain(ftype, remaining)
                # Filter: only keep inner results that resolved to a
                # DIFFERENT global than the root globals we started from.
                # When the inner resolver can't make progress, it returns
                # the root globals (same as type_globals) with
                # unresolved_fields set — this represents "no resolution"
                # and should fall through to the leaf-with-unresolved path.
                type_globals_set = set(type_globals)
                inner_progressed = [
                    r for r in inner
                    if r["global_name"] not in type_globals_set
                ]
                if inner_progressed:
                    # Emit field-type globals as intermediates
                    for g in type_globals:
                        results.append({
                            "global_name": g,
                            "is_intermediate": True,
                            "step_index": i,
                        })
                    # Append inner results with adjusted step_index
                    for r in inner_progressed:
                        results.append({
                            "global_name": r["global_name"],
                            "is_intermediate": r["is_intermediate"],
                            "step_index": r.get("step_index", 0) + i + 1,
                            "unresolved_fields": r.get("unresolved_fields", []),
                        })
                else:
                    # Remaining chain couldn't resolve — emit as leaf
                    # with unresolved trailing fields so the caller knows
                    # the write goes through an unresolved pointer (READ).
                    for g in type_globals:
                        results.append({
                            "global_name": g,
                            "is_intermediate": False,
                            "step_index": i,
                            "unresolved_fields": remaining,
                        })
            else:
                # No remaining fields — these are leaf globals
                for g in type_globals:
                    results.append({
                        "global_name": g,
                        "is_intermediate": False,
                        "step_index": i,
                    })
            break
        return results

    @staticmethod
    def _filter_globals_by_field(
        type_globals: List[str], field_name: str
    ) -> List[str]:
        """Filter globals to those whose name matches the field's core name.

        For fields with recognizable suffixes (Ptr, Map, Mask, SV), extract
        the core identifier and only keep globals containing it.  This
        prevents runtime chain resolution from emitting ALL globals of a
        shared type when only a subset actually corresponds to the field.
        """
        # Try to extract a meaningful core from the field name
        for suffix in ("Ptr", "Map", "Mask", "SV"):
            if field_name.endswith(suffix):
                base = field_name[: -len(suffix)]
                break
        else:
            return type_globals  # No recognized suffix — can't filter

        # Strip common prefixes like "Active"
        core = base.replace("Active", "")
        if len(core) < 4:
            return type_globals  # Core too short for reliable filtering

        core_lower = core.lower()
        filtered = [g for g in type_globals if core_lower in g.lower()]
        # Fall back to unfiltered if nothing matched (avoid empty results)
        return filtered if filtered else type_globals

    def _collect_member_chains(
        self, func_cursor: Any, src_path: str
    ) -> List[Dict[str, Any]]:
        """Extract all MEMBER_REF_EXPR chains from a function body."""
        chains: List[Dict[str, Any]] = []
        for node in func_cursor.walk_preorder():
            if node.kind != CursorKind.MEMBER_REF_EXPR:
                continue
            if not node.location.file:
                continue
            if os.path.abspath(node.location.file.name) != src_path:
                continue
            info = self._build_member_chain(node)
            if info and len(info["fields"]) >= 1:
                chains.append(info)
        return chains

    def _build_member_chain(self, member_node: Any) -> Optional[Dict[str, Any]]:
        """Build a chain from a MEMBER_REF_EXPR back to its root variable.

        Returns ``{'root_var', 'root_type', 'fields', 'field_types', 'line'}``
        or None.  ``field_types`` contains the base type name resolved by
        clang for each member expression in the chain.
        """
        fields = [member_node.spelling]
        field_types = [self._extract_base_type_name(member_node.type)]
        current = member_node

        while True:
            children = list(current.get_children())
            if not children:
                return None
            base = children[0]

            if base.kind == CursorKind.MEMBER_REF_EXPR:
                fields.insert(0, base.spelling)
                field_types.insert(0, self._extract_base_type_name(base.type))
                current = base
            elif base.kind == CursorKind.DECL_REF_EXPR:
                # Root variable found
                type_name = self._extract_base_type_name(base.type)
                if type_name:
                    return {
                        "root_var": base.spelling,
                        "root_type": type_name,
                        "fields": fields,
                        "field_types": field_types,
                        "line": member_node.location.line
                              if member_node.location else 0,
                    }
                return None
            elif base.kind in (
                CursorKind.UNEXPOSED_EXPR,
                CursorKind.CSTYLE_CAST_EXPR,
                CursorKind.PAREN_EXPR,
                CursorKind.UNARY_OPERATOR,
                CursorKind.ARRAY_SUBSCRIPT_EXPR,
            ):
                # Skip through casts, parens, unary ops, array subscripts
                current = base
            else:
                return None

    @staticmethod
    def _extract_base_type_name(clang_type: Any) -> str:
        """Strip pointers, arrays, and qualifiers from a clang type to get base name."""
        t = clang_type
        # Unwrap pointers AND arrays (ConstantArray, IncompleteArray, etc.)
        while True:
            if t.kind == TypeKind.POINTER:
                t = t.get_pointee()
            elif t.kind in (TypeKind.CONSTANTARRAY, TypeKind.INCOMPLETEARRAY,
                            TypeKind.VARIABLEARRAY, TypeKind.DEPENDENTSIZEDARRAY):
                t = t.element_type
            else:
                break
        decl = t.get_declaration()
        if decl and decl.spelling:
            return decl.spelling
        # Fallback: string cleanup
        name = t.spelling
        for qual in ("const ", "volatile ", "restrict "):
            name = name.replace(qual, "")
        return name.strip()

    @staticmethod
    def _find_field_as_member_idx(line: str, field_name: str) -> int:
        """Find the position of field_name used as a struct member access.

        Searches for ``->field_name`` or ``.field_name`` patterns first
        (the struct member access context).  Falls back to rfind when
        no member-access pattern is found.

        This avoids incorrect matches when the same identifier appears
        multiple times on a line in different roles (e.g. as both a
        struct member ``->ConfigPtr`` and a function parameter ``ConfigPtr``).
        """
        import re as _re_fam
        # Search for ->field_name or .field_name followed by non-identifier char
        for prefix in ('->', '.'):
            pattern = _re_fam.escape(prefix) + _re_fam.escape(field_name) + r'(?!\w)'
            m = _re_fam.search(pattern, line)
            if m:
                return m.start() + len(prefix)
        # Fallback: rfind (last occurrence)
        return line.rfind(field_name)

    @staticmethod
    def _is_write_on_line(
        src_lines: List[str], line_no: int, field_name: str
    ) -> bool:
        """Heuristic: check if the source line writes to the field."""
        if line_no < 1 or line_no > len(src_lines):
            return False
        line = src_lines[line_no - 1]
        # Find the field as a struct member access (->field or .field).
        # This handles cases where the same identifier appears multiple
        # times on the line in different roles (e.g. struct member AND
        # function parameter: *chain->ConfigPtr = ConfigPtr;).
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return False
        rest = line[idx + len(field_name):]
        import re as _re_local
        # Check if field is followed by -> (pointer dereference).
        # field->member = val means field is READ (provides address);
        # the write targets the member, not the field itself.
        has_ptr_deref = bool(_re_local.match(r'\s*->', rest))
        # Check if field is a pointer used with subscript: PtrField[i] = val
        # means the pointer provides an address (READ), write goes to
        # pointed-to memory.  Array members (no "Ptr" suffix) are genuinely
        # modified by subscript assignment (Issue 20: ActiveSRMap[i] = val).
        has_ptr_subscript = (
            bool(_re_local.match(r'\s*\[', rest))
            and field_name.endswith("Ptr")
        )
        # Strip trailing brackets, array subscripts, parens, deref, spaces
        rest = _re_local.sub(r'^(\s*\[[^\]]*\]|\s*\)|\s*\]|\s*->|\s)+', '', rest)
        if has_ptr_deref or has_ptr_subscript:
            # Field is pointer-dereferenced — assignment after deref writes to
            # pointed-to memory, not to the field. Field is READ only.
            return False
        # Check for leading * dereference: *chain->field = val
        # or (*chain->field) = val.  The entire expression is dereferenced,
        # so the field provides an address (READ); the write goes to the
        # pointed-to memory.
        before = line[:idx]
        before_tail = before.rstrip()
        if before_tail.endswith('->') or before_tail.endswith('.'):
            lhs_stripped = line.lstrip()
            if lhs_stripped.startswith('*') or lhs_stripped.startswith('(*'):
                return False
        for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
            if rest.startswith(op):
                return True
        if rest.startswith('=') and not rest.startswith('=='):
            return True
        # Detect ++ and -- (increment/decrement = read + write)
        if rest.startswith('++') or rest.startswith('--'):
            return True
        # Also check for prefix ++/-- before the expression containing field_name
        # e.g. "++GrpData->CurrSampCount" — the ++ is before the chain root
        before = line[:idx]
        import re as _re_local2
        # Check if the non-whitespace content before the field starts with ++/--
        # Strip the struct chain part (identifiers, ->, ., []) to find the operator
        # e.g. "    ++GrpData->" → strip "GrpData->" → "    ++"
        before_stripped = _re_local2.sub(r'[\w.\[\]]+\s*->\s*$', '', before)
        before_stripped = _re_local2.sub(r'[\w.\[\]]+\s*\.\s*$', '', before_stripped)
        before_stripped = before_stripped.rstrip()
        if before_stripped.endswith('++') or before_stripped.endswith('--'):
            return True
        return False

    @staticmethod
    def _is_read_on_line(
        src_lines: List[str], line_no: int, field_name: str
    ) -> bool:
        """Heuristic: check if the source line reads the field.

        A field is read when:
        - It appears on the RHS of an assignment (after =).
        - It appears as a function argument.
        - It's used with a compound-assign op like |= (read+write).
        - It appears without any assignment to it.
        - It is subscripted or dereferenced (ptr[x] or ptr->member) — the
          pointer field itself is read to obtain the address.
        """
        if line_no < 1 or line_no > len(src_lines):
            return True  # default assume read
        line = src_lines[line_no - 1]
        # Find the field as a struct member access (->field or .field).
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return True
        rest = line[idx + len(field_name):]
        import re as _re_local
        # If field is dereferenced via ->, it is always READ
        # (the pointer/struct is read to compute the target address)
        if _re_local.match(r'\s*->', rest):
            return True
        # If field is subscripted [x] and has a "Ptr" suffix, it is READ
        # (pointer is read to compute the target address for ptr[i] = val).
        # Non-Ptr arrays (e.g. ActiveSRMap[i] = 0) are genuinely the write
        # target — fall through to normal assignment analysis.
        if _re_local.match(r'\s*\[', rest) and field_name.endswith("Ptr"):
            return True
        # If the line has a leading * (or (* ) and the field is accessed via ->/.,
        # the field is always READ (provides address for dereference).
        before = line[:idx]
        before_tail = before.rstrip()
        if before_tail.endswith('->') or before_tail.endswith('.'):
            lhs_stripped = line.lstrip()
            if lhs_stripped.startswith('*') or lhs_stripped.startswith('(*'):
                return True
        # Strip trailing brackets, array subscripts, parens, deref, spaces
        rest = _re_local.sub(r'^(\s*\[[^\]]*\]|\s*\)|\s*\]|\s)+', '', rest)
        # Compound operators are both read and write
        for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
            if rest.startswith(op):
                return True  # read + write
        # Simple assignment: LHS is written, not read
        if rest.startswith('=') and not rest.startswith('=='):
            return False  # pure write
        # Everything else is a read (RHS, argument, condition, etc.)
        return True

    @staticmethod
    def _get_deref_target_access(
        src_lines: List[str], line_no: int, field_name: str
    ) -> str:
        """Determine access type for memory pointed to by a dereferenced field.

        When code does ``*chain->ptrField = val``, the ptrField itself is
        READ (provides address), but the TARGET memory is WRITTEN.  This
        method detects such patterns and returns the access type for the
        target variable that the pointer field resolves to:

        - ``"WRITE"`` for simple assignment: ``*chain->ptr = val``
        - ``"READ_WRITE"`` for compound assignment: ``*chain->ptr |= val``
        - ``""`` if not a dereference-write pattern
        """
        if line_no < 1 or line_no > len(src_lines):
            return ""
        line = src_lines[line_no - 1]
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return ""
        rest = line[idx + len(field_name):]
        before = line[:idx]
        import re as _re_local

        # --- Pattern A: Leading * with field accessed via -> or . ---
        # e.g., *chain->ptrField = val  or  *chain->ptrField |= val
        # or (*chain->ptrField) = val
        before_tail = before.rstrip()
        if before_tail.endswith('->') or before_tail.endswith('.'):
            lhs_stripped = line.lstrip()
            if lhs_stripped.startswith('*') or lhs_stripped.startswith('(*'):
                # Strip whitespace, brackets, subscripts after field name
                rest_stripped = _re_local.sub(
                    r'^(\s*\[[^\]]*\]|\s*\)|\s*\]|\s*->|\s)+', '', rest
                )
                for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
                    if rest_stripped.startswith(op):
                        return "READ_WRITE"
                if (rest_stripped.startswith('=')
                        and not rest_stripped.startswith('==')):
                    return "WRITE"

        # --- Pattern B: Pointer field with subscript then assignment ---
        # e.g., chain->ptrField[idx] = val  or  chain->ptrField[idx] |= val
        # Only for fields ending in "Ptr" (to avoid false positives on
        # regular array members like ActiveSRMap[]).
        if field_name.endswith("Ptr") and _re_local.match(r'\s*\[', rest):
            rest_after_sub = _re_local.sub(
                r'^(\s*\[[^\]]*\])+', '', rest
            ).lstrip()
            for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
                if rest_after_sub.startswith(op):
                    return "READ_WRITE"
            if (rest_after_sub.startswith('=')
                    and not rest_after_sub.startswith('==')):
                return "WRITE"

        return ""

    # Primitive types for pointer-cast detection.  When a subscripted Ptr
    # field is cast to one of these pointer types, it's likely a data
    # reinterpretation (not address-obtaining).  Casts to OTHER types
    # (struct pointers like Dma_ChType*, Ifx_DMA_CH*) indicate the
    # subscript yields an address, not data.
    _PRIM_CAST_TYPES = frozenset({
        "uint8", "uint16", "uint32", "uint64",
        "sint8", "sint16", "sint32", "sint64",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int", "unsigned", "char", "short", "long",
        "float", "double", "boolean", "void",
        "size_t", "ptrdiff_t", "uintptr_t", "intptr_t",
    })

    # Regex: matches a C-style cast to a pointer type, e.g. (Dma_ChType *)
    _RE_PTR_CAST = re.compile(
        r'\(\s*(?:const\s+|volatile\s+)*(\w+)\s*\*\s*\)'
    )

    @staticmethod
    def _is_ptr_field_dereferenced(
        src_lines: List[str], line_no: int, field_name: str
    ) -> bool:
        """Check if a Ptr-suffixed field is dereferenced to access target data.

        Returns True when the source line shows the pointer being used to
        access the memory it points to (read or write of target data).
        Returns False when the pointer value is merely obtained/copied
        (address-obtaining only — no target data access).

        Dereference patterns (True):
          - ``*chain->PtrField``         — leading * dereference
          - ``chain->PtrField[i]``       — subscript access (array element)
          - ``*(chain->PtrField)``       — parenthesized dereference
          - ``chain->PtrField->member``  — member access through pointer

        Non-dereference patterns (False):
          - ``local = chain->PtrField;``   — pointer value assignment
          - ``fn(chain->PtrField)``        — passing pointer as argument
          - ``if (chain->PtrField != NULL)``  — pointer comparison
        """
        if line_no < 1 or line_no > len(src_lines):
            return False
        line = src_lines[line_no - 1]
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return False

        import re as _re_local

        rest = line[idx + len(field_name):]
        before = line[:idx]

        # Pattern 1: field followed by subscript [i] — subscript dereferences
        # the pointer to access target data (element of pointed-to array).
        if _re_local.match(r'\s*\[', rest):
            return True

        # Pattern 2: field followed by -> — member access through pointer
        # (the pointer is dereferenced to reach the pointed-to struct's member)
        if _re_local.match(r'\s*->', rest):
            return True

        # Pattern 3: leading * dereference
        # Check if the chain expression (up to and including this field)
        # is preceded by a * operator.
        # e.g. "*(PartitionDataPtr->ActiveGrpSVPtr)" or
        #      "*RuntimeInfoPtr->PartitionDataPtr->ActiveGrpSVPtr" or
        #      "(*Adc_kData[i]->ConfigPtr) = val"
        before_tail = before.rstrip()
        if before_tail.endswith('->') or before_tail.endswith('.'):
            # The field is accessed via chain->field or struct.field
            # Check for leading * on the entire expression
            lhs_stripped = line.lstrip()
            if lhs_stripped.startswith('*') or lhs_stripped.startswith('(*'):
                return True
            # Also check for *(expr) pattern — the * may be further left
            # e.g. "*( RuntimeInfoPtr->PtrField )"
            # Find the start of the chain expression
            chain_start = line.find('*')
            if chain_start >= 0 and chain_start < idx:
                # Verify there's nothing but whitespace/parens between * and chain
                between = line[chain_start + 1:idx].lstrip()
                if between == '' or between.startswith('('):
                    return True

        return False

    @staticmethod
    def _get_deref_target_read(
        src_lines: List[str], line_no: int, field_name: str
    ) -> bool:
        """Check if target data is READ through a dereferenced pointer field.

        Returns True when the dereference result is used in a read context
        (comparison, RHS of assignment, function argument, etc.) rather than
        exclusively in a write context.

        For ``*chain->PtrField = val`` → target is only WRITTEN (False).
        For ``x = *chain->PtrField`` → target is READ (True).
        For ``*chain->PtrField |= val`` → target is READ+WRITE (True).
        For ``chain->PtrField[i] != 0`` → target is READ (True).
        For ``chain->PtrField[i] = val`` → target is only WRITTEN (False).
        """
        if line_no < 1 or line_no > len(src_lines):
            return False
        line = src_lines[line_no - 1]
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return False

        import re as _re_local

        rest = line[idx + len(field_name):]
        before = line[:idx]

        # Case A: subscript access — PtrField[i]...
        m_sub = _re_local.match(r'(\s*\[[^\]]*\])+', rest)
        if m_sub:
            after_sub = rest[m_sub.end():].lstrip()
            # If followed by -> or another [, it's further dereference (read)
            if after_sub.startswith('->') or after_sub.startswith('['):
                return True
            # Strip trailing parens/brackets
            after_sub = _re_local.sub(
                r'^(\s*\[[^\]]*\]|\s*\)|\s*\]|\s)+', '', after_sub
            ).lstrip()
            # Compound assignment (|=, &=, etc.) = read + write
            for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
                if after_sub.startswith(op):
                    return True
            # Simple assignment = write only
            if after_sub.startswith('=') and not after_sub.startswith('=='):
                return False
            # Increment/decrement = read + write
            if after_sub.startswith('++') or after_sub.startswith('--'):
                return True
            # Everything else (comparison, function arg, etc.) = read
            return True

        # Case B: leading * dereference — *chain->PtrField...
        before_tail = before.rstrip()
        is_deref = False
        if before_tail.endswith('->') or before_tail.endswith('.'):
            lhs_stripped = line.lstrip()
            if lhs_stripped.startswith('*'):
                is_deref = True

        if is_deref:
            # After the field name, strip whitespace/parens
            rest_stripped = _re_local.sub(
                r'^(\s*\[[^\]]*\]|\s*\)|\s*\]|\s)+', '', rest
            ).lstrip()
            # Compound assignment = read + write
            for op in ('|=', '&=', '^=', '+=', '-=', '<<=', '>>='):
                if rest_stripped.startswith(op):
                    return True
            # Simple assignment = write only (target is NOT read)
            if rest_stripped.startswith('=') and not rest_stripped.startswith('=='):
                return False
            # Everything else = read (comparison, passed as value, etc.)
            return True

        # Case C: field followed by -> (member access through pointer)
        if _re_local.match(r'\s*->', rest):
            # Accessing a member through the pointer always reads target struct
            return True

        return False

    @classmethod
    def _is_pointer_address_obtain(
        cls, src_lines: List[str], line_no: int, field_name: str
    ) -> bool:
        """Detect if a Ptr-suffixed subscripted field is used for address-obtaining.

        Returns True when the source line casts the subscript result to a
        non-primitive pointer type (e.g. ``(Dma_ChType *)...GrpTcsDataPtr[i]``).
        This indicates the field access yields an ADDRESS of a struct instance,
        not actual data from the target.  The READ belongs to the container
        struct (which holds the pointer array), not to the pointed-to data.
        """
        if not field_name.endswith("Ptr"):
            return False
        if line_no < 1 or line_no > len(src_lines):
            return False
        line = src_lines[line_no - 1]
        idx = _ClangAnalyzer._find_field_as_member_idx(line, field_name)
        if idx < 0:
            return False
        rest = line[idx + len(field_name):]
        # Field must be followed by subscript [...]
        if not rest.lstrip().startswith('['):
            return False
        # Look for a pointer type cast on the line (before the chain expression)
        # that wraps/precedes the subscript expression.
        for m in cls._RE_PTR_CAST.finditer(line):
            cast_type = m.group(1).lower()
            if cast_type not in cls._PRIM_CAST_TYPES:
                # Non-primitive pointer cast found → address-obtaining
                return True
        return False

    # ------------------------------------------------------------------
    # Phase 5 helpers — Critical section detection & annotation
    # ------------------------------------------------------------------

    # Regex patterns for detecting critical section entry/exit:
    # Pattern 1: Direct call — SchM_Enter_<Module>_<Name>() / SchM_Exit_<Module>_<Name>()
    _RE_SCHM_ENTER = re.compile(
        r'\bSchM_Enter_(\w+)\s*\(\s*\)', re.MULTILINE
    )
    _RE_SCHM_EXIT = re.compile(
        r'\bSchM_Exit_(\w+)\s*\(\s*\)', re.MULTILINE
    )
    # Pattern 2: Indirect via function pointer map — .SchMEnterFnPtr() / .SchMExitFnPtr()
    #   Also matches .SchMEnterPtr() / .SchMExitPtr() (used by SPI, ICU, PWM, OCU, PORT, ENCODER)
    _RE_FNPTR_ENTER = re.compile(
        r'(\w+(?:\[.*?\])?(?:\.\w+)*)\s*\.\s*SchMEnter(?:Fn)?Ptr\s*\(\s*\)',
        re.MULTILINE
    )
    _RE_FNPTR_EXIT = re.compile(
        r'(\w+(?:\[.*?\])?(?:\.\w+)*)\s*\.\s*SchMExit(?:Fn)?Ptr\s*\(\s*\)',
        re.MULTILINE
    )

    def _detect_critical_sections(
        self, src_path: str, func_start: int, func_end: int
    ) -> List[tuple]:
        """Detect critical section line ranges within a function.

        Scans the source text of the function for Enter/Exit patterns and
        returns a list of (enter_line, exit_line, section_name) tuples.

        Handles two patterns:
        1. Direct: SchM_Enter_<Module>_<SectionName>() / SchM_Exit_...()
        2. Indirect: <map>.SchMEnterFnPtr() / <map>.SchMExitFnPtr()

        For nested or multiple critical sections, each Enter is matched
        with the nearest subsequent Exit of the same type (stack-based).
        """
        try:
            with open(src_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
        except OSError:
            return []

        # Extract the function body lines (keep absolute line numbering)
        if func_start < 1 or func_end > len(all_lines):
            return []

        # Build list of (line_no, event_type, section_name)
        # event_type: "ENTER" or "EXIT"
        events: List[tuple] = []

        for line_idx in range(func_start - 1, func_end):
            line_no = line_idx + 1  # 1-based
            line_text = all_lines[line_idx]

            # Pattern 1: SchM_Enter_<Name>()
            for m in self._RE_SCHM_ENTER.finditer(line_text):
                section_name = m.group(1)  # e.g. "Adc_RuntimeProtWriteSeq"
                events.append((line_no, "ENTER", section_name))

            for m in self._RE_SCHM_EXIT.finditer(line_text):
                section_name = m.group(1)
                events.append((line_no, "EXIT", section_name))

            # Pattern 2: <map>.SchMEnterFnPtr()
            for m in self._RE_FNPTR_ENTER.finditer(line_text):
                # Use a generic name derived from the map expression
                # e.g. "Adc_kSchMFnMap[...]" → "SchMFnMap"
                map_expr = m.group(1)
                section_name = "SchMFnMap"
                # Try to extract a more specific name from comment above
                if line_idx > 0:
                    prev_line = all_lines[line_idx - 1]
                    comment_match = re.search(
                        r'/\*\s*(?:[Ee]nter\s+)?(.+?)\s*[Cc]ritical\s*[Ss]ection\s*\*/',
                        prev_line
                    )
                    if comment_match:
                        extracted = comment_match.group(1).strip().rstrip('-_ ')
                        if extracted and extracted.lower() not in ('', 'the', 'a', 'enter', 'exit'):
                            section_name = extracted
                    else:
                        # Check the line before the prev_line (2 lines up)
                        if line_idx > 1:
                            prev2_line = all_lines[line_idx - 2]
                            comment_match = re.search(
                                r'/\*\s*(?:[Ee]nter\s+)?(.+?)\s*[Cc]ritical\s*[Ss]ection\s*\*/',
                                prev2_line
                            )
                            if comment_match:
                                extracted = comment_match.group(1).strip().rstrip('-_ ')
                                if extracted and extracted.lower() not in ('', 'the', 'a', 'enter', 'exit'):
                                    section_name = extracted
                events.append((line_no, "ENTER", section_name))

            for m in self._RE_FNPTR_EXIT.finditer(line_text):
                # Match exit to the most recent enter's name
                events.append((line_no, "EXIT", "SchMFnMap"))

        # Match Enter/Exit pairs using a stack approach
        # For indirect (SchMFnMap) entries, match by order (LIFO)
        # For direct (named) entries, match by name
        cs_ranges: List[tuple] = []
        enter_stack: List[tuple] = []  # (line_no, section_name)

        for line_no, event_type, section_name in sorted(events, key=lambda x: x[0]):
            if event_type == "ENTER":
                enter_stack.append((line_no, section_name))
            elif event_type == "EXIT":
                if not enter_stack:
                    continue
                # Try to match by name first
                matched = False
                for i in range(len(enter_stack) - 1, -1, -1):
                    if enter_stack[i][1] == section_name or section_name == "SchMFnMap":
                        enter_line, enter_name = enter_stack.pop(i)
                        cs_ranges.append((enter_line, line_no, enter_name))
                        matched = True
                        break
                if not matched:
                    # Fallback: pop the most recent enter
                    enter_line, enter_name = enter_stack.pop()
                    cs_ranges.append((enter_line, line_no, enter_name))

        return cs_ranges

    @staticmethod
    def _annotate_critical_sections(
        refs: List[Dict[str, Any]],
        cs_ranges: List[tuple],
    ) -> None:
        """Tag each global reference with critical section membership.

        Adds two fields to each ref dict:
        - ``in_critical_section``: bool
        - ``critical_section_name``: str (empty if not in CS)

        A ref is considered inside a CS if its ``line`` falls strictly
        between the Enter line and Exit line (exclusive of both markers).
        """
        for ref in refs:
            line = ref.get("line", 0)
            if line == 0:
                ref["in_critical_section"] = False
                ref["critical_section_name"] = ""
                continue

            cs_name = ""
            in_cs = False
            for enter_line, exit_line, section_name in cs_ranges:
                if enter_line < line < exit_line:
                    in_cs = True
                    cs_name = section_name
                    break  # Take the first (innermost) match

            ref["in_critical_section"] = in_cs
            ref["critical_section_name"] = cs_name

    def _extract_internal_calls(self, func_cursor: Any, src_path: str,
                                current_name: str) -> List[Dict[str, Any]]:
        """Walk function AST to find function calls.

        Tracks switch-case context: calls inside a CASE_STMT or
        DEFAULT_STMT are tagged with ``"case": "<value>"`` so the KG
        builder can identify dispatcher functions and avoid propagating
        through conditional call paths.
        """
        calls: List[Dict[str, Any]] = []
        seen: set = set()
        order = 0

        def _get_case_value(cursor: Any) -> str:
            """Extract the case label value from a CASE_STMT's first child."""
            children = list(cursor.get_children())
            if not children:
                return "?"
            val_cursor = children[0]
            # Try to get the literal/enum value from tokens
            tokens = list(val_cursor.get_tokens())
            if tokens:
                return tokens[0].spelling
            # Fallback: use the spelling of the cursor
            return val_cursor.spelling or "?"

        def _walk(cursor: Any, case_label: str = ""):
            nonlocal order
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            # Track switch-case context
            current_case = case_label
            if cursor.kind == CursorKind.CASE_STMT:
                current_case = _get_case_value(cursor)
            elif cursor.kind == CursorKind.DEFAULT_STMT:
                current_case = "default"

            if cursor.kind == CursorKind.CALL_EXPR:
                callee = cursor.spelling
                if (callee and callee != current_name
                        and len(callee) >= 2 and callee not in seen):
                    seen.add(callee)
                    entry: Dict[str, Any] = {
                        "function": callee,
                        "order": order,
                        "line": cursor.location.line,
                    }
                    if current_case:
                        entry["case"] = current_case
                    calls.append(entry)
                    order += 1

            for child in cursor.get_children():
                _walk(child, current_case)

        _walk(func_cursor)
        return calls

    # ------------------------------------------------------------------
    # Branch-conditional propagation support
    # ------------------------------------------------------------------

    def _extract_branch_map(self, func_cursor: Any, src_path: str) -> List[Dict[str, Any]]:
        """Build a branch tree for the function.

        Identifies if/else-if/else branches whose condition compares a
        function parameter to a constant (enum, macro, literal).  Returns
        a list of branch descriptors:

            {
                "branch_id": "B<n>",
                "start_line": int,
                "end_line": int,
                "condition_param": str | None,  # parameter name if condition is param == const
                "condition_value": str | None,  # the constant value
                "condition_op": "==" | "!=" | None,
                "is_else": bool,  # True if this is a default/else branch
                "parent_branch_id": str | None,
                "sibling_ids": [str],  # other branches in the same if/else chain
            }

        The "root" branch (unconditional function body) has branch_id "B0".
        """
        branches: List[Dict[str, Any]] = []
        branch_counter = [0]  # mutable counter for closure

        # Collect parameter names for condition matching
        param_names: set = set()
        for child in func_cursor.get_children():
            if child.kind == CursorKind.PARM_DECL and child.spelling:
                param_names.add(child.spelling)

        # Root branch: entire function body
        extent = func_cursor.extent
        branches.append({
            "branch_id": "B0",
            "start_line": extent.start.line,
            "end_line": extent.end.line,
            "condition_param": None,
            "condition_value": None,
            "condition_op": None,
            "is_else": False,
            "parent_branch_id": None,
            "sibling_ids": [],
        })

        def _get_branch_id() -> str:
            branch_counter[0] += 1
            return f"B{branch_counter[0]}"

        def _extract_condition_info(cond_cursor: Any) -> Dict[str, Any]:
            """Check if a condition cursor is `param == CONSTANT` or `param != CONSTANT`."""
            result = {"param": None, "value": None, "op": None}
            if not cond_cursor:
                return result

            # Walk to find BINARY_OPERATOR with == or !=
            def _find_comparison(cursor: Any) -> bool:
                if cursor.kind == CursorKind.BINARY_OPERATOR:
                    children = list(cursor.get_children())
                    if len(children) == 2:
                        # Get the operator from tokens
                        tokens = list(cursor.get_tokens())
                        token_strs = [t.spelling for t in tokens]
                        op = None
                        op_idx = -1
                        for i, tok in enumerate(token_strs):
                            if tok in ("==", "!="):
                                op = tok
                                op_idx = i
                                break
                        if op:
                            # Check if one side is a param and other is a constant
                            lhs, rhs = children[0], children[1]
                            lhs_name = self._get_decl_ref_name(lhs)
                            rhs_name = self._get_decl_ref_name(rhs)
                            lhs_const = self._get_constant_value(lhs)
                            rhs_const = self._get_constant_value(rhs)

                            # Fallback: extract constant from parent tokens
                            # when child nodes have empty tokens (macro expansion)
                            if lhs_name in param_names and rhs_const is None:
                                # RHS constant from tokens after op
                                rhs_tokens = token_strs[op_idx + 1:]
                                if len(rhs_tokens) == 1 and rhs_tokens[0].isidentifier():
                                    rhs_const = rhs_tokens[0]
                            elif rhs_name in param_names and lhs_const is None:
                                # LHS constant from tokens before op
                                lhs_tokens = token_strs[:op_idx]
                                if len(lhs_tokens) == 1 and lhs_tokens[0].isidentifier():
                                    lhs_const = lhs_tokens[0]

                            if lhs_name in param_names and rhs_const is not None:
                                result["param"] = lhs_name
                                result["value"] = rhs_const
                                result["op"] = op
                                return True
                            elif rhs_name in param_names and lhs_const is not None:
                                result["param"] = rhs_name
                                result["value"] = lhs_const
                                result["op"] = op
                                return True
                # Recurse into unexposed/paren expressions
                for child in cursor.get_children():
                    if _find_comparison(child):
                        return True
                return False

            _find_comparison(cond_cursor)
            return result

        def _walk_branches(cursor: Any, parent_id: str):
            """Recursively walk AST to find if/else-if/else chains."""
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            if cursor.kind == CursorKind.IF_STMT:
                children = list(cursor.get_children())
                if not children:
                    return

                # IF_STMT children: [condition, then_body, else_body?]
                # Extract the if/else-if chain
                sibling_ids: List[str] = []
                chain_branches: List[Dict[str, Any]] = []

                self._extract_if_chain(
                    cursor, src_path, parent_id, param_names,
                    chain_branches, sibling_ids, _get_branch_id,
                    _extract_condition_info,
                )

                # Set sibling_ids on all branches in the chain
                for b in chain_branches:
                    b["sibling_ids"] = [s for s in sibling_ids if s != b["branch_id"]]
                branches.extend(chain_branches)

                # Recurse into branch bodies (don't re-process IF_STMT)
                for b_info in chain_branches:
                    # Find the body cursor for this branch to recurse
                    pass  # Nested ifs will be caught by recursive _walk_branches

            # Recurse into all children
            for child in cursor.get_children():
                _walk_branches(child, parent_id)

        _walk_branches(func_cursor, "B0")
        return branches

    def _extract_if_chain(
        self,
        if_cursor: Any,
        src_path: str,
        parent_id: str,
        param_names: set,
        out_branches: List[Dict[str, Any]],
        out_sibling_ids: List[str],
        get_branch_id,
        extract_condition_info,
    ):
        """Extract an if / else-if / else chain into branch descriptors."""
        children = list(if_cursor.get_children())
        if len(children) < 2:
            return

        # First child is condition, second is then-body
        cond_cursor = children[0]
        then_body = children[1]
        else_body = children[2] if len(children) > 2 else None

        # Extract condition info
        cond_info = extract_condition_info(cond_cursor)

        # "then" branch
        then_id = get_branch_id()
        out_sibling_ids.append(then_id)
        then_extent = then_body.extent
        out_branches.append({
            "branch_id": then_id,
            "start_line": then_extent.start.line,
            "end_line": then_extent.end.line,
            "condition_param": cond_info["param"],
            "condition_value": cond_info["value"],
            "condition_op": cond_info["op"],
            "is_else": False,
            "parent_branch_id": parent_id,
            "sibling_ids": [],  # filled later
        })

        # Handle else / else-if
        if else_body:
            if else_body.kind == CursorKind.IF_STMT:
                # else-if: recurse
                self._extract_if_chain(
                    else_body, src_path, parent_id, param_names,
                    out_branches, out_sibling_ids, get_branch_id,
                    extract_condition_info,
                )
            else:
                # plain else
                else_id = get_branch_id()
                out_sibling_ids.append(else_id)
                else_extent = else_body.extent
                out_branches.append({
                    "branch_id": else_id,
                    "start_line": else_extent.start.line,
                    "end_line": else_extent.end.line,
                    "condition_param": cond_info["param"] if cond_info["param"] else None,
                    "condition_value": None,  # else = complement of all siblings
                    "condition_op": None,
                    "is_else": True,
                    "parent_branch_id": parent_id,
                    "sibling_ids": [],
                })

    @staticmethod
    def _get_decl_ref_name(cursor: Any) -> Optional[str]:
        """Get the name from a DECL_REF_EXPR, traversing unexposed/paren wrappers."""
        if cursor.kind == CursorKind.DECL_REF_EXPR:
            return cursor.spelling
        # Walk through implicit casts, paren expressions
        for child in cursor.get_children():
            if child.kind == CursorKind.DECL_REF_EXPR:
                return child.spelling
            # Recurse one more level for nested casts
            for grandchild in child.get_children():
                if grandchild.kind == CursorKind.DECL_REF_EXPR:
                    return grandchild.spelling
        return None

    @staticmethod
    def _get_constant_value(cursor: Any) -> Optional[str]:
        """Extract a constant value (enum, macro, literal) from an expression cursor."""
        # Direct integer literal
        if cursor.kind == CursorKind.INTEGER_LITERAL:
            tokens = list(cursor.get_tokens())
            return tokens[0].spelling if tokens else None

        # Enum constant or macro reference
        if cursor.kind == CursorKind.DECL_REF_EXPR:
            ref = cursor.referenced
            if ref and ref.kind == CursorKind.ENUM_CONSTANT_DECL:
                return cursor.spelling
            # Reject parameters and local variables — they are NOT constants
            if ref and ref.kind in (CursorKind.PARM_DECL, CursorKind.VAR_DECL):
                return None
            # Macro constants appear as DECL_REF_EXPR — accept if the name
            # looks like an uppercase macro/constant (heuristic)
            if ref and ref.spelling:
                name = cursor.spelling
                if name and (name.isupper() or name.upper() == name
                             or '_SID' in name or '_ID' in name):
                    return name
                return None

        # Walk through implicit casts / unexposed expressions
        for child in cursor.get_children():
            result = _ClangAnalyzer._get_constant_value(child)
            if result is not None:
                return result

        # Try tokens as last resort (handles macros expanded to literals)
        # Only use this for leaf nodes with no children — avoids false positives
        # on UNEXPOSED_EXPR wrappers around parameters/variables.
        children = list(cursor.get_children())
        if not children:
            tokens = list(cursor.get_tokens())
            if tokens and len(tokens) == 1:
                tok = tokens[0].spelling
                # Accept only ALL_CAPS identifiers (macro/enum convention) or integers
                if tok.isdigit() or (tok.isidentifier() and tok.isupper()):
                    return tok

        return None

    def _extract_call_constant_args(self, func_cursor: Any, src_path: str,
                                     current_name: str) -> Dict[str, Dict[str, str]]:
        """Extract constant arguments at each call site in a function.

        Returns a dict mapping callee_name → {param_index_str: constant_value}.
        Only records arguments that are compile-time constants (enum values,
        macro constants, integer literals, or _SID-suffixed identifiers).
        """
        call_args: Dict[str, Dict[str, str]] = {}

        def _walk(cursor: Any):
            if (cursor.location.file
                    and os.path.abspath(cursor.location.file.name) != src_path):
                return

            if cursor.kind == CursorKind.CALL_EXPR:
                callee = cursor.spelling
                if callee and callee != current_name and len(callee) >= 2:
                    children = list(cursor.get_children())
                    # Arguments start after the callee reference (first child)
                    args = children[1:] if len(children) > 1 else []
                    const_args: Dict[str, str] = {}
                    for idx, arg in enumerate(args):
                        val = self._get_constant_value(arg)
                        if val is not None:
                            const_args[str(idx)] = val

                    # Fallback: use CALL_EXPR tokens to extract macro constants
                    # that have empty tokens on child nodes (macro expansion)
                    if len(const_args) < len(args):
                        call_tokens = [t.spelling for t in cursor.get_tokens()]
                        # Parse token list: callee ( arg0 , arg1 , ... )
                        # Find opening paren, then split by commas
                        if call_tokens and '(' in call_tokens:
                            paren_idx = call_tokens.index('(')
                            # Extract arg tokens between parens, respecting nesting
                            arg_token_groups: List[List[str]] = []
                            current_group: List[str] = []
                            depth = 0
                            for tok in call_tokens[paren_idx + 1:]:
                                if tok == '(':
                                    depth += 1
                                    current_group.append(tok)
                                elif tok == ')':
                                    if depth == 0:
                                        if current_group:
                                            arg_token_groups.append(current_group)
                                        break
                                    depth -= 1
                                    current_group.append(tok)
                                elif tok == ',' and depth == 0:
                                    arg_token_groups.append(current_group)
                                    current_group = []
                                else:
                                    current_group.append(tok)

                            # For each arg that wasn't captured, check if it's
                            # a single ALL_CAPS identifier (macro constant)
                            for idx in range(len(args)):
                                if str(idx) not in const_args and idx < len(arg_token_groups):
                                    toks = arg_token_groups[idx]
                                    if (len(toks) == 1 and toks[0].isidentifier()
                                            and toks[0].isupper()):
                                        const_args[str(idx)] = toks[0]

                    if const_args and callee not in call_args:
                        call_args[callee] = const_args

            for child in cursor.get_children():
                _walk(child)

        _walk(func_cursor)
        return call_args

    def _annotate_refs_with_branch(
        self,
        global_refs: List[Dict[str, Any]],
        branch_map: List[Dict[str, Any]],
    ) -> None:
        """Annotate each global_ref entry with the branch_id it belongs to.

        Uses line-range matching: a ref at line L belongs to the most
        specific (deepest nested) branch whose [start_line, end_line]
        contains L.  Falls back to "B0" (root) if no branch matches.
        """
        # Sort branches by specificity (smallest range = most specific)
        sorted_branches = sorted(
            branch_map,
            key=lambda b: (b["end_line"] - b["start_line"]),
        )

        for ref in global_refs:
            line = ref.get("line", 0)
            matched_branch = "B0"
            for b in sorted_branches:
                if b["branch_id"] == "B0":
                    continue
                if b["start_line"] <= line <= b["end_line"]:
                    matched_branch = b["branch_id"]
                    break  # Most specific (smallest range) wins
            ref["branch_id"] = matched_branch

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _parse_file(self, file_path: str) -> Any:
        """Parse a file on disk with libclang."""
        args = [
            "-std=c11",
            "-DIFX_INLINE=inline",
            "-D__attribute__(x)=",
            "-D__HIGHTEC__",
            "-Wno-everything",
        ]
        # Always force-include McalLib.h (defines uint32, MCAL macros etc.)
        # even in Sum mode. Config-generated headers (e.g. Adc_Cfg.h) may
        # contain broken multi-line macros that poison uint32 if it isn't
        # already typedef'd before they are parsed.
        #
        # Search include paths for McalLib.h (expected in infra_integration/00_Common/).
        mcallib = None
        for inc in self._include_paths:
            candidate = os.path.join(inc, "McalLib.h")
            if os.path.isfile(candidate):
                mcallib = candidate
                break
        if mcallib:
            args.extend(["-include", mcallib])
        # Include paths from caller (real production headers)
        for inc in self._include_paths:
            args.extend(["-I", inc])

        options = (
            TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            | TranslationUnit.PARSE_INCOMPLETE
            | TranslationUnit.PARSE_INCLUDE_BRIEF_COMMENTS_IN_CODE_COMPLETION
        )
        return self._index.parse(file_path, args=args, options=options)

    def _cursor_to_dict(self, cursor: Any, source_file: str) -> Dict[str, Any]:
        """Recursively convert a clang cursor into a JSON-friendly dict."""
        kind = cursor.kind
        node: Dict[str, Any] = {
            "kind": self._KIND_MAP.get(kind, kind.name if hasattr(kind, 'name') else str(kind)),
            "spelling": cursor.spelling or "",
        }

        # Location (only for nodes in the target file)
        loc = cursor.location
        if loc.file and os.path.abspath(loc.file.name) == os.path.abspath(source_file):
            node["location"] = {
                "line": loc.line,
                "column": loc.column,
            }

        # Type information when available
        if cursor.type and cursor.type.spelling:
            node["type"] = cursor.type.spelling

        # Return type for functions
        if kind == CursorKind.FUNCTION_DECL:
            node["return_type"] = cursor.result_type.spelling
            node["is_definition"] = cursor.is_definition()
            params = []
            for arg in cursor.get_arguments():
                params.append({
                    "name": arg.spelling,
                    "type": arg.type.spelling,
                })
            node["parameters"] = params
            node["name"] = cursor.spelling

        # Documentation comment
        if cursor.raw_comment:
            node["documentation"] = cursor.raw_comment

        # Recurse into children that belong to the target source file
        children: List[Dict[str, Any]] = []
        for child in cursor.get_children():
            child_loc = child.location
            if child_loc.file and os.path.abspath(child_loc.file.name) == os.path.abspath(source_file):
                children.append(self._cursor_to_dict(child, source_file))
        if children:
            node["children"] = children

        return node

    @staticmethod
    def _collect_functions(ast_node: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Walk the AST dict and return all function-definition nodes."""
        results: List[Dict[str, Any]] = []

        def _walk(n: Dict[str, Any]):
            if n.get("kind") == "function_definition" and n.get("is_definition"):
                results.append(n)
            for child in n.get("children", []):
                _walk(child)

        _walk(ast_node)
        return results

    @staticmethod
    def _collect_diagnostics(tu: Any) -> List[Dict[str, Any]]:
        """Return clang diagnostics as a list of dicts."""
        diags: List[Dict[str, Any]] = []
        for d in tu.diagnostics:
            diags.append({
                "severity": d.severity,
                "message": d.spelling,
                "line": d.location.line,
                "column": d.location.column,
                "file": d.location.file.name if d.location.file else "",
            })
        return diags


def parse(
    path: str,
    method: str = "clang",
    libclang_path: Optional[str] = None,
    include_paths: Optional[List[str]] = None,
    skip_default_stubs: bool = False,
    initializer_map: Any = None,
) -> Dict[str, Any]:
    """
    Parse a C source file and return structured analysis.

    Args:
        path: Path to a ``.c`` file.
        method: Parsing backend to use.
            - ``"clang"`` (default): libclang-based parsing returning an AST.
            - ``"regex"``: fast regex-based extraction.
        libclang_path: Path to the libclang shared library
            (only used when *method* is ``"clang"``).
        include_paths: Additional include directories for clang
            (only used when *method* is ``"clang"``).
        skip_default_stubs: When True, do not add the built-in stubs/
            directory or force-include McalLib.h.  Used in Sum mode
            where real production headers are provided via
            *include_paths* instead.
        initializer_map: Optional ``ConfigStructResolver`` instance for
            Phase 4 struct-chain global detection.  When provided,
            indirect accesses through config struct pointer chains
            are resolved to the actual global variables.

    Returns:
        When *method* is ``"regex"``:
            A dict with keys ``functions`` (per-function analysis) and
            ``statistics`` (aggregate counts).
        When *method* is ``"clang"``:
            A dict with keys ``ast`` (recursive AST), ``functions``,
            ``diagnostics``, and ``statistics``.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If *method* is not ``"regex"`` or ``"clang"``.
        ImportError: If *method* is ``"clang"`` but libclang is not installed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    method = method.lower()
    if method == "regex":
        content = p.read_text(encoding="utf-8")
        return _analyzer.analyze(content)
    elif method == "clang":
        if not LIBCLANG_AVAILABLE:
            logger.warning(
                "libclang unavailable for %s; falling back to regex parsing",
                path,
            )
            content = p.read_text(encoding="utf-8")
            return _analyzer.analyze(content)
        clang_analyzer = _ClangAnalyzer(
            libclang_path=libclang_path,
            include_paths=include_paths,
            skip_default_stubs=skip_default_stubs,
            initializer_map=initializer_map,
        )
        return clang_analyzer.analyze(str(p.resolve()))
    else:
        raise ValueError(
            f"Unknown parsing method: {method!r}. Use 'regex' or 'clang'."
        )

"""
C Source Code Parser
====================

Parses C source files to extract function bodies, internal call graphs,
register access patterns (READ/WRITE), and switch-case structures.

Supports two parsing backends:
- **regex** (default): Lightweight regex-based extraction.
- **clang**: libclang-based parsing that produces an abstract syntax tree (AST).

Usage:
    from IngestionPipeline.Parsers import c_parser

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
        # Pattern 2: MCALUTIL macro access  (e.g. MCALUTIL_SFRWRITE(REG.U, val))
        mcal_write_pattern = re.compile(
            r'MCALUTIL_SFRWRITE\s*\(\s*([\w>\-\[\]\.]+?)\.(B|U)\s*,'
        )
        mcal_read_pattern = re.compile(
            r'MCALUTIL_SFRREAD\s*\(\s*\w+\s*,\s*([\w>\-\[\]\.]+?)\.(B|U)\s*\)'
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

            # Pattern 2: MCALUTIL_SFRWRITE / MCALUTIL_SFRREAD macros
            for match in mcal_write_pattern.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "WRITE", "line": line_idx})
            for match in mcal_read_pattern.finditer(line):
                reg_path = match.group(1)
                register = reg_path.rsplit('->', 1)[-1] if '->' in reg_path else reg_path
                accesses.append({"register": register, "field": "U", "access_type": "READ", "line": line_idx})

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
                 include_paths: Optional[List[str]] = None):
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

    # ------------------------------------------------------------------
    def analyze(self, file_path: str) -> Dict[str, Any]:
        """Parse *file_path* and return the full AST as a nested dict.

        Returns:
            A dict with keys:
            - ``ast``: The root AST node (recursive children).
            - ``functions``: Flat list of every function definition found.
            - ``diagnostics``: Clang warnings / errors encountered.
            - ``statistics``: Aggregate counts.
        """
        tu = self._parse_file(file_path)

        diagnostics = self._collect_diagnostics(tu)
        ast_root = self._cursor_to_dict(tu.cursor, file_path)
        functions = self._collect_functions(ast_root)

        return {
            "ast": ast_root,
            "functions": {f["name"]: f for f in functions},
            "diagnostics": diagnostics,
            "statistics": {
                "total_functions": len(functions),
                "total_diagnostics": len(diagnostics),
                "parse_method": "clang",
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _parse_file(self, file_path: str) -> Any:
        """Parse a file on disk with libclang."""
        args = [
            "-std=c11",
            "-DIFX_INLINE=inline",
            "-D__attribute__(x)=",
            "-Wno-everything",
        ]
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
        )
        return clang_analyzer.analyze(str(p.resolve()))
    else:
        raise ValueError(
            f"Unknown parsing method: {method!r}. Use 'regex' or 'clang'."
        )

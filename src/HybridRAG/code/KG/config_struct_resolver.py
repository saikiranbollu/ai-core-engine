"""Config Struct Resolver — Phase 4 global variable detection.

Resolves indirect global variable accesses through struct pointer
dereference chains found in AUTOSAR MCAL configuration files.

AUTOSAR MCAL code commonly passes configuration data through nested
struct pointers, e.g.::

    PartitionDataPtr->HwTrigDataPtr->ActiveEruErsChMaskPtr

The actual global variable name (``Adc_EcucPartition_0ActiveEruErsChMask``)
never appears in the function body.  This module:

1. Uses **clang** to parse the module's own headers (``ssc/inc/*.h``) and
   extract struct typedef field definitions.
2. Uses **regex** to extract struct initializers from the generated config
   files (``*_Data.c``, ``*_PBcfg.c``) — these files are Tresos-generated
   and have a very predictable format, avoiding clang's dependency issues.
3. Provides a ``resolve_chain(root_type, fields)`` method that traces a
   member-access chain through the map to the actual target globals.

The map is module-agnostic — it works for any MCAL module (ADC, SPI, etc.)
as long as the config files follow the standard AUTOSAR pattern.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("struct_resolver")

try:
    import clang.cindex
    from clang.cindex import (
        CursorKind,
        Index,
        TranslationUnit,
        TypeKind,
    )
    HAS_CLANG = True
except ImportError:
    HAS_CLANG = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_libclang_dll() -> Optional[str]:
    """Locate the libclang shared library (same logic as c_parser)."""
    import glob
    import platform
    candidates: list[str] = []
    if platform.system() == "Windows":
        for pattern in [
            r"C:\Program Files\LLVM\bin\libclang.dll",
            r"C:\Program Files (x86)\LLVM\bin\libclang.dll",
        ]:
            candidates.extend(glob.glob(pattern))
    else:
        for pattern in [
            "/usr/lib/llvm-*/lib/libclang*.so*",
            "/usr/lib/x86_64-linux-gnu/libclang*.so*",
            "/usr/local/lib/libclang*.dylib",
        ]:
            candidates.extend(glob.glob(pattern))
    return candidates[0] if candidates else None


# Regex patterns for generated config files
#
# Variable declaration:  static [const] <Type> [*] <Name> [= ...];
_RE_VAR_DECL = re.compile(
    r"^(?:static\s+)?"                      # optional static
    r"(?:const\s+)?"                         # optional const
    r"(\w+)"                                 # type name  (group 1)
    r"(?:\s*\*+\s*(?:const\s*)?)?"           # optional pointer
    r"\s+(\w+)"                              # variable name (group 2)
    r"\s*(?:\[[^\]]*\])?"                    # optional array
    r"\s*;",                                 # semicolon
    re.MULTILINE,
)

# Struct initializer:  static [const] <Type> <Name> [=\n{ ... };]
# We capture the type, name, and the braced initialiser list.
_RE_STRUCT_INIT = re.compile(
    r"^(?:static\s+)?"
    r"(?:const\s+)?"
    r"(\w+)"                                 # type name  (group 1)
    r"(?:\s*\*+\s*(?:const\s*)?)?\s+"
    r"(\w+)"                                 # var name   (group 2)
    r"(?:\s*\[[^\]]*\])?"
    r"\s*=\s*\n\{"                           # = \n {
    r"(.*?)"                                 # init body  (group 3)
    r"\};",
    re.DOTALL | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ConfigStructResolver:
    """Builds and queries an initializer map from MCAL config files.

    Uses clang for struct field definitions (from module headers that
    parse cleanly) and regex for initialiser extraction (from generated
    config files that may have unresolvable header dependencies).

    Usage::

        resolver = ConfigStructResolver()
        resolver.build_map(config_c_files, include_paths,
                           header_dirs=[ssc_inc_path])
        hits = resolver.resolve_chain("Adc_PartitionDataType",
                                      ["HwTrigDataPtr", "ActiveEruErsChMaskPtr"])
    """

    def __init__(self) -> None:
        # (global_var_name, field_name) → [target_global_names]
        self._field_map: Dict[Tuple[str, str], List[str]] = {}
        # base_type_name → [global_var_names of that type]
        self._type_to_globals: Dict[str, List[str]] = {}
        # global_var_name → base_type_name
        self._global_types: Dict[str, str] = {}
        # type_name → ordered list of field names (from clang)
        self._struct_fields: Dict[str, List[str]] = {}
        # Pointer-array elements: array_var_name → [element globals]
        self._array_elements: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Map building
    # ------------------------------------------------------------------

    def build_map(
        self,
        config_files: List[Path],
        include_paths: List[str],
        libclang_path: Optional[str] = None,
        header_dirs: Optional[List[Path]] = None,
    ) -> None:
        """Build the initializer map.

        Args:
            config_files: Generated config C files (``CfgMcal/src/*.c``).
            include_paths: Include paths for header resolution.
            libclang_path: Optional explicit path to libclang.
            header_dirs: Directories containing module headers (``ssc/inc``)
                whose ``*.h`` files will be parsed with clang to extract
                struct field definitions.
        """
        if not config_files:
            return

        # Fix double-CR line endings from Bitbucket downloads.
        self._normalize_line_endings(config_files, include_paths)

        # Step 1: Extract struct field definitions from module headers
        if header_dirs and HAS_CLANG:
            self._extract_struct_fields(
                header_dirs, include_paths, libclang_path,
            )
        logger.info(
            "  Struct resolver: %d struct types with field definitions",
            len(self._struct_fields),
        )

        # Step 2: Regex-extract var declarations & initializers from config
        for fpath in config_files:
            try:
                self._regex_parse_config(fpath)
            except Exception as exc:
                logger.warning(
                    "  Struct resolver: failed on %s: %s", fpath.name, exc,
                )

        logger.info(
            "  Struct resolver: %d field mappings, %d typed globals across %d types",
            len(self._field_map),
            sum(len(v) for v in self._type_to_globals.values()),
            len(self._type_to_globals),
        )

    # ------------------------------------------------------------------
    # Step 1 — clang: struct field definitions from module headers
    # ------------------------------------------------------------------

    def _extract_struct_fields(
        self,
        header_dirs: List[Path],
        include_paths: List[str],
        libclang_path: Optional[str],
    ) -> None:
        """Parse module headers with clang to learn struct field order."""
        resolved_lib = libclang_path or _find_libclang_dll()
        if resolved_lib:
            try:
                clang.cindex.Config.set_library_file(resolved_lib)
            except Exception:
                pass

        index = Index.create()
        clang_args = [
            "-std=c11",
            "-fsyntax-only",
            "-DIFX_INLINE=inline",
            '-D__attribute__(x)=',
            "-D__HIGHTEC__",
            "-Wno-everything",
        ]
        for ip in include_paths:
            clang_args.append(f"-I{ip}")

        for hdir in header_dirs:
            if not hdir.is_dir():
                continue
            for hfile in sorted(hdir.glob("*.h")):
                try:
                    tu = index.parse(
                        str(hfile), args=clang_args, options=0,
                    )
                    self._collect_struct_typedefs(tu)
                except Exception as exc:
                    logger.debug(
                        "  Struct resolver: header %s skipped: %s",
                        hfile.name, exc,
                    )

    def _collect_struct_typedefs(self, tu: Any) -> None:
        """Walk a TU for typedef'd structs and record their field names."""
        for cursor in tu.cursor.get_children():
            if cursor.kind != CursorKind.TYPEDEF_DECL:
                continue
            type_name = cursor.spelling
            if not type_name:
                continue
            underlying = cursor.underlying_typedef_type
            decl = underlying.get_declaration()
            if not decl or decl.kind != CursorKind.STRUCT_DECL:
                continue
            fields = [
                c.spelling
                for c in decl.get_children()
                if c.kind == CursorKind.FIELD_DECL and c.spelling
            ]
            if fields and type_name not in self._struct_fields:
                self._struct_fields[type_name] = fields
                logger.debug(
                    "    struct %s: %s", type_name, fields,
                )

    # ------------------------------------------------------------------
    # Step 2 — regex: var declarations & initializers from config files
    # ------------------------------------------------------------------

    def _regex_parse_config(self, fpath: Path) -> None:
        """Regex-extract variable declarations and struct initializers."""
        text = fpath.read_text(encoding="utf-8", errors="replace")

        # Pass 1: plain variable declarations (no initializer)
        #   e.g.  static uint8 Adc_EcucPartition_0ActiveEruErsChMask;
        for m in _RE_VAR_DECL.finditer(text):
            type_name, var_name = m.group(1), m.group(2)
            self._record_global(var_name, type_name)

        # Pass 2: struct initialisers
        #   e.g.  static const Adc_HwTrigDataType Adc_k... = \n{ ... };
        for m in _RE_STRUCT_INIT.finditer(text):
            type_name, var_name = m.group(1), m.group(2)
            init_body = m.group(3)

            self._record_global(var_name, type_name)

            # Match init values to struct fields
            fields = self._struct_fields.get(type_name, [])
            if not fields:
                # No struct fields — check for pointer-array initialiser
                # e.g. static uint32 * const GrpTcsData[] = { (uint32 *)Elem0, ... };
                self._extract_array_elements(var_name, init_body)
                continue

            # Extract top-level initialiser elements (comma-separated,
            # but nested braces form sub-initialisers — skip them).
            init_vals = self._split_init_list(init_body)

            # Detect array-of-struct: top-level elements are brace-enclosed
            # Strip leading C comments before checking (Tresos inserts them)
            first_stripped = re.sub(
                r"/\*.*?\*/", "", init_vals[0], flags=re.DOTALL
            ).lstrip() if init_vals else ""
            if init_vals and first_stripped.startswith("{"):
                # Each top-level element is one array element; parse its
                # inner fields individually.
                for elem in init_vals:
                    inner = re.sub(
                        r"/\*.*?\*/", "", elem, flags=re.DOTALL
                    ).strip()
                    if inner.startswith("{") and inner.endswith("}"):
                        inner = inner[1:-1]
                    inner_vals = self._split_init_list(inner)
                    for field_name, val in zip(fields, inner_vals):
                        ref = self._extract_identifier(val)
                        if ref and ref not in ("NULL_PTR", "0"):
                            bucket = self._field_map.setdefault(
                                (var_name, field_name), [],
                            )
                            if ref not in bucket:
                                bucket.append(ref)
                            logger.debug(
                                "    %s.%s → %s (array elem)",
                                var_name, field_name, ref,
                            )
            else:
                for field_name, val in zip(fields, init_vals):
                    ref = self._extract_identifier(val)
                    if ref and ref not in ("NULL_PTR", "0"):
                        bucket = self._field_map.setdefault(
                            (var_name, field_name), [],
                        )
                        if ref not in bucket:
                            bucket.append(ref)
                        logger.debug(
                            "    %s.%s → %s", var_name, field_name, ref,
                        )

    def _extract_array_elements(self, var_name: str, init_body: str) -> None:
        """Extract element targets from a pointer-array initialiser.

        For initialisers like ``{ (uint32 *)Elem0, (uint32 *)Elem1 }``
        record the element globals so array-subscript accesses can be
        resolved.
        """
        vals = self._split_init_list(init_body)
        elements: List[str] = []
        for val in vals:
            ref = self._extract_identifier(val)
            if ref and ref not in ("NULL_PTR", "0"):
                elements.append(ref)
        if elements:
            self._array_elements[var_name] = elements
            for ref in elements:
                logger.debug(
                    "    %s[] element → %s", var_name, ref,
                )

    # Primitive/scalar types that should not participate in struct chain resolution.
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

    def _record_global(self, var_name: str, type_name: str) -> None:
        """Register a global variable and its type."""
        self._global_types[var_name] = type_name
        # Skip primitive types — they would cause false matches in chain resolution
        if type_name in self._PRIMITIVE_TYPES:
            return
        bucket = self._type_to_globals.setdefault(type_name, [])
        if var_name not in bucket:
            bucket.append(var_name)

    @staticmethod
    def _split_init_list(body: str) -> List[str]:
        """Split a brace-initialiser body into top-level elements.

        Handles nested braces (sub-initialisers for arrays/structs)
        by tracking brace depth.
        """
        elements: List[str] = []
        depth = 0
        current: list[str] = []
        for ch in body:
            if ch == "{":
                depth += 1
                current.append(ch)
            elif ch == "}":
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                elements.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        tail = "".join(current).strip()
        if tail:
            elements.append(tail)
        return elements

    @staticmethod
    def _extract_identifier(val: str) -> Optional[str]:
        """Pull the C identifier from an initialiser value.

        Strips ``&``, casts, whitespace, and trailing comments.
        """
        # Remove comments
        val = re.sub(r"/\*.*?\*/", "", val, flags=re.DOTALL).strip()
        # Remove address-of
        val = val.lstrip("&").strip()
        # Remove C-style casts: (type *), (const type *), etc.
        val = re.sub(r"\([^)]*\)\s*", "", val).strip()
        # Match first C identifier
        m = re.search(r"\b([A-Za-z_]\w+)\b", val)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # Line-ending normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_line_endings(
        config_files: List[Path], include_paths: List[str]
    ) -> None:
        """Fix ``\\r\\r\\n`` line endings in downloaded files.

        Bitbucket raw-content downloads sometimes produce double-CR line
        endings which break backslash-newline continuation in C macros.
        We fix the files in-place (they live in a disposable temp dir).
        """
        fixed = 0
        dirs_to_scan: List[Path] = [f.parent for f in config_files]
        for ip in include_paths:
            dirs_to_scan.append(Path(ip))

        seen_dirs: set = set()
        for d in dirs_to_scan:
            d_resolved = d.resolve()
            if d_resolved in seen_dirs or not d_resolved.is_dir():
                continue
            seen_dirs.add(d_resolved)
            for ext in ("*.c", "*.h"):
                for fp in d_resolved.glob(ext):
                    try:
                        raw = fp.read_bytes()
                        if b"\r\r\n" in raw:
                            fp.write_bytes(raw.replace(b"\r\r\n", b"\n"))
                            fixed += 1
                    except OSError:
                        pass
        if fixed:
            logger.info(
                "  Struct resolver: normalised line endings in %d files",
                fixed,
            )

    # ------------------------------------------------------------------
    # Chain resolution
    # ------------------------------------------------------------------

    def resolve_chain(
        self, root_type: str, fields: List[str]
    ) -> List[Dict[str, Any]]:
        """Resolve a member-access chain to actual global variables.

        Args:
            root_type: Base type name (e.g. ``'Adc_PartitionDataType'``).
            fields: Ordered field names (e.g.
                ``['HwTrigDataPtr', 'ActiveEruErsChMaskPtr']``).

        Returns:
            List of dicts, each with:
              - ``global_name``: the resolved global variable name
              - ``is_intermediate``: True if this is a pass-through struct,
                False if it is the leaf (final target)
        """
        root_globals = self._type_to_globals.get(root_type, [])
        if not root_globals:
            return []

        all_results: List[Dict[str, Any]] = []

        for root_global in root_globals:
            self._resolve_chain_recursive(
                root_global, fields, 0, [], all_results,
            )

        return all_results

    def _resolve_chain_recursive(
        self,
        current: str,
        fields: List[str],
        step: int,
        path: List[Dict[str, Any]],
        out: List[Dict[str, Any]],
    ) -> None:
        """Recursively resolve a chain, fanning out on multi-target fields."""
        if step >= len(fields):
            # At the end of the chain — also emit pointer-array elements
            # of the final resolved global if any exist.
            if path:
                last = path[-1]
                arr_elems = self._array_elements.get(last["global_name"], [])
                if arr_elems:
                    # The leaf global is itself a pointer array;
                    # emit its elements as additional leaf globals.
                    for elem in arr_elems:
                        path.append({
                            "global_name": elem,
                            "is_intermediate": False,
                            "step_index": last["step_index"],
                        })
                    # Reclassify the array variable as intermediate
                    last["is_intermediate"] = True
                out.extend(path)
            return
        targets = self._field_map.get((current, fields[step]))
        if not targets:
            # Can't resolve this step — emit the accumulated path so far.
            # The last resolved global becomes the leaf, and we record
            # the unresolved trailing fields so the caller can include
            # them in the via_chain (e.g. "->ResultBufferPtr").
            if path:
                path[-1]["is_intermediate"] = False
                path[-1]["unresolved_fields"] = list(fields[step:])
                out.extend(path)
            elif step == 0:
                # Path is empty but current IS a root global. The field
                # is a scalar member (not a pointer to another global).
                # Emit current as the leaf with unresolved_fields so the
                # caller knows which member was accessed.
                out.append({
                    "global_name": current,
                    "is_intermediate": False,
                    "step_index": 0,
                    "unresolved_fields": list(fields[step:]),
                })
            return
        is_final = step == len(fields) - 1
        for target in targets:
            entry = {
                "global_name": target,
                "is_intermediate": not is_final,
                "step_index": step,
            }
            self._resolve_chain_recursive(
                target, fields, step + 1, path + [entry], out,
            )

    def get_globals_for_type(self, type_name: str) -> List[str]:
        """Return global variable names declared with *type_name*."""
        return list(self._type_to_globals.get(type_name, []))

    def get_array_elements(self, var_name: str) -> List[str]:
        """Return element globals for a pointer-array variable."""
        return list(self._array_elements.get(var_name, []))

    @property
    def is_empty(self) -> bool:
        """True when no field mappings were found."""
        return len(self._field_map) == 0

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "field_mappings": len(self._field_map),
            "typed_globals": sum(len(v) for v in self._type_to_globals.values()),
            "types": len(self._type_to_globals),
        }

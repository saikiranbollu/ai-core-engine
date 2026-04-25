"""
Auto Stub Generator
===================

Scans MCAL module C source files and generates minimal header stubs
required for clang parsing.  Works for **any** MCAL module without
manual configuration.

Generated stubs
---------------
- ``{Module}_Cfg.h``        — feature switches, version info, constants
- ``{Module}_Data.h``       — data header (includes real module header)
- ``{Module}_PBcfg.h``      — post-build config (minimal)
- ``{Module}_CpuPrivMode.h``— CPU privilege mode (macro stub)
- ``{Module}_MemMap.h``     — memory mapping (no-op)
- ``{Module}_Cbk.h``        — callbacks (minimal)
- ``SchM_{Module}.h``       — BSW Scheduler exclusive areas
- cross-module dependency stubs (``Dma.h``, ``Gtm.h``, …)

Usage::

    from auto_stub_generator import AutoStubGenerator

    gen = AutoStubGenerator("ADC", source_dir, output_dir)
    stubs_path = gen.generate()   # returns output_dir
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Set, Optional

logger = logging.getLogger(__name__)

# Headers provided by the common stubs directory — never generate these
COMMON_HEADERS = frozenset({
    "Std_Types.h",
    "Platform_Types.h",
    "Compiler.h",
    "McalLib.h",
    "McalLib_OsCfg.h",
    "Mcu_TimeDelay.h",
    "Mcal_ErrorTypes.h",
    "Mcal_ExecutionContext.h",
    "Mcal_SafetyError.h",
    "Det.h",
    "Dem.h",
})


class AutoStubGenerator:
    """Generate module-specific header stubs for clang parsing."""

    # ------------------------------------------------------------------
    # Regex patterns for source scanning
    # ------------------------------------------------------------------
    _RE_INCLUDE = re.compile(r'#include\s+[<"]([^>"]+)[>"]')

    # #if (MOD_AR_RELEASE_MAJOR_VERSION != 4U)
    _RE_VERSION_CHECK = re.compile(
        r'#if\s*\(\s*(\w+_(?:AR_RELEASE|SW)_(?:MAJOR|MINOR|REVISION|PATCH)_VERSION)\s*!=\s*(\d+)[Uu]?\s*\)'
    )

    # (FEATURE == STD_ON)  anywhere in code
    _RE_FEATURE_ON = re.compile(r'\(\s*(\w+)\s*==\s*STD_ON\s*\)')

    # (FEATURE != STD_OFF) anywhere in code
    _RE_FEATURE_NEQ_OFF = re.compile(r'\(\s*(\w+)\s*!=\s*STD_OFF\s*\)')

    # #ifdef FEATURE  /  #if defined(FEATURE)
    _RE_IFDEF = re.compile(r'#ifdef\s+(\w+)')
    _RE_DEFINED = re.compile(r'defined\s*\(\s*(\w+)\s*\)')

    # DEM reporting: *_DEM_REPORTING == STD_ON
    _RE_DEM_REPORTING = re.compile(r'(\w+_DEM_REPORTING)\b')

    # SchM_Enter_{Module}_{Area}()  /  SchM_Exit_{Module}_{Area}()
    _RE_SCHM_CALL = re.compile(r'SchM_(?:Enter|Exit)_(\w+?)_(\w+)\s*\(')

    # Constants: {MODULE}_MAX_FOO, {MODULE}_NUM_BAR, …
    _RE_CONSTANT_USE = re.compile(
        r'\b([A-Z][A-Z0-9_]*_(?:MAX|COUNT|NUM|SIZE|TOTAL|LIMIT|KERNEL_COUNT|SCHM_COUNT)\w*)\b'
    )

    # MODULE_ID / VENDOR_ID version checks
    _RE_MODULE_ID = re.compile(r'(\w+_MODULE_ID)\s*!=?\s*(\d+)u?')
    _RE_VENDOR_ID = re.compile(r'(\w+_VENDOR_ID)\s*!=?\s*(\d+)u?')

    # SFR headers (from the infra_sfr repo) — skip in cross-module stubs
    _RE_SFR_HEADER = re.compile(r'^Ifx\w+\.h$')

    # ------------------------------------------------------------------
    def __init__(
        self,
        module: str,
        source_dir: Path,
        output_dir: Path,
    ):
        self.module = module.upper()
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        # Mixed-case name (e.g. "Adc", "Can_17_McmCan") detected from source
        self.module_mixed: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate(self) -> Path:
        """Scan source files and generate all required stubs.

        Returns *output_dir* containing the generated headers.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Detect the mixed-case module name from actual header files
        self.module_mixed = self._detect_mixed_case()
        logger.info(
            "AutoStubGenerator: module=%s  mixed=%s  source=%s  output=%s",
            self.module, self.module_mixed, self.source_dir, self.output_dir,
        )

        source_content = self._read_all_sources()
        if not source_content:
            logger.warning("No C/H source found in %s — generating minimal stubs",
                           self.source_dir)

        # Scan source for patterns
        includes     = self._scan_includes(source_content)
        versions     = self._scan_version_checks(source_content)
        features     = self._scan_feature_switches(source_content)
        dem_switches = self._scan_dem_reporting(source_content)
        schm_areas   = self._scan_schm_exclusive_areas(source_content)
        constants    = self._scan_constants(source_content)
        module_ids   = self._scan_module_ids(source_content)

        # Headers that already exist in the module's own include dirs
        own_headers  = self._get_own_headers()

        # Generate module-specific stubs (skip if the file exists in ssc/)
        self._generate_cfg_h(versions, features, dem_switches, constants, module_ids)
        self._generate_data_h(source_content, own_headers)
        self._generate_pbcfg_h(own_headers)
        self._generate_cpuprivmode_h(own_headers)
        self._generate_memmap_h()
        self._generate_cbk_h(own_headers)
        self._generate_schm_h(schm_areas)

        # Cross-module dependency stubs
        self._generate_cross_module_stubs(includes, own_headers)

        generated = list(self.output_dir.glob("*.h"))
        logger.info(
            "Generated %d stub headers for module %s: %s",
            len(generated), self.module,
            ", ".join(f.name for f in sorted(generated)),
        )
        return self.output_dir

    # ------------------------------------------------------------------
    # Source scanning
    # ------------------------------------------------------------------
    def _read_all_sources(self) -> str:
        """Read and concatenate all C/H files from the module source tree."""
        parts: list[str] = []
        for ext in ("*.c", "*.h"):
            for fpath in sorted(self.source_dir.rglob(ext)):
                if ".git" in fpath.parts or "META-INF" in fpath.parts:
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                # Skip Tresos EB template files (contain [! … !] syntax)
                if "[!" in text[:500]:
                    continue
                parts.append(text)
        return "\n".join(parts)

    def _detect_mixed_case(self) -> str:
        """Detect the mixed-case module name from ssc/inc headers.

        Searches for a ``.h`` file whose uppercased stem matches
        ``self.module``.  Falls back to ``capitalize()``.
        """
        if not self.module.isupper():
            return self.module

        upper = self.module
        for h_file in sorted(self.source_dir.rglob("*.h")):
            if ".git" in h_file.parts:
                continue
            if h_file.stem.upper() == upper:
                return h_file.stem
        # Fallback: capitalise first letter
        return self.module.capitalize()

    def _scan_includes(self, content: str) -> Set[str]:
        return set(self._RE_INCLUDE.findall(content))

    def _scan_version_checks(self, content: str) -> Dict[str, str]:
        versions: Dict[str, str] = {}
        prefix = self.module + "_"
        for m in self._RE_VERSION_CHECK.finditer(content):
            name, val = m.group(1), m.group(2)
            if name.startswith(prefix):
                versions[name] = val + "u"
        return versions

    def _scan_feature_switches(self, content: str) -> Set[str]:
        """Collect feature switch names from preprocessor conditionals."""
        features: Set[str] = set()
        prefix = self.module + "_"
        for pattern in (self._RE_FEATURE_ON, self._RE_FEATURE_NEQ_OFF):
            for m in pattern.finditer(content):
                name = m.group(1)
                if name.startswith(prefix):
                    features.add(name)
        # #ifdef / defined()
        for pattern in (self._RE_IFDEF, self._RE_DEFINED):
            for m in pattern.finditer(content):
                name = m.group(1)
                if name.startswith(prefix) and not name.endswith(("_H", "_H_")):
                    features.add(name)
        # Remove version defines (handled separately) and DEM switches
        features -= set(self._scan_version_checks(content).keys())
        return features

    def _scan_dem_reporting(self, content: str) -> Set[str]:
        switches: Set[str] = set()
        prefix = self.module + "_"
        for m in self._RE_DEM_REPORTING.finditer(content):
            name = m.group(1)
            if name.startswith(prefix):
                switches.add(name)
        return switches

    def _scan_schm_exclusive_areas(self, content: str) -> Set[str]:
        areas: Set[str] = set()
        for m in self._RE_SCHM_CALL.finditer(content):
            mod_name, area_name = m.group(1), m.group(2)
            if mod_name.upper() == self.module or mod_name == self.module_mixed:
                areas.add(area_name)
        return areas

    def _scan_constants(self, content: str) -> Dict[str, str]:
        constants: Dict[str, str] = {}
        prefix = self.module + "_"
        for m in self._RE_CONSTANT_USE.finditer(content):
            name = m.group(1)
            if name.startswith(prefix) and name not in constants:
                if any(kw in name for kw in ("MAX", "TOTAL", "LIMIT")):
                    constants[name] = "16u"
                else:
                    constants[name] = "8u"
        return constants

    def _scan_module_ids(self, content: str) -> Dict[str, str]:
        ids: Dict[str, str] = {}
        for m in self._RE_MODULE_ID.finditer(content):
            ids[m.group(1)] = m.group(2) + "u"
        for m in self._RE_VENDOR_ID.finditer(content):
            ids[m.group(1)] = m.group(2) + "u"
        return ids

    def _scan_extern_declarations(self, source_content: str) -> List[str]:
        """Scan module source for references to globals declared in template headers.

        Looks for identifiers like ``Adc_kData[...`` or ``Adc_kSchMFnMap[...``
        that follow the AUTOSAR naming convention ``{Module}_k*``.
        Returns a list of ``extern ...;`` declarations as strings.
        """
        # Pattern: Module_k<Name> used with array subscript or pointer deref
        prefix = self.module_mixed + "_k"
        pat = re.compile(
            rf'\b({re.escape(prefix)}\w+)\s*\['
        )
        found: Dict[str, str] = {}  # name → best-guess type
        for m in pat.finditer(source_content):
            name = m.group(1)
            if name not in found:
                found[name] = name  # placeholder

        if not found:
            return []

        # Try to find the actual declarations in the template headers
        extern_lines: List[str] = []
        template_dirs = [
            self.source_dir / "Plugins",
        ]
        extern_re = re.compile(r'^extern\s+.+?\b(' + '|'.join(re.escape(n) for n in found) + r')\b[^;]*;', re.MULTILINE)

        for td in template_dirs:
            if not td.is_dir():
                continue
            for hfile in td.rglob("*.h"):
                try:
                    hcontent = hfile.read_text(encoding="utf-8", errors="replace")
                    # Strip Tresos template syntax
                    clean = re.sub(r'\[!.*?!\]', '', hcontent)
                    for em in extern_re.finditer(clean):
                        extern_lines.append(em.group(0))
                except OSError:
                    continue

        # For any globals we found in source but couldn't find an extern decl,
        # generate a generic one
        declared_names = set()
        for line in extern_lines:
            for name in found:
                if name in line:
                    declared_names.add(name)

        for name in found:
            if name not in declared_names:
                # Conservative: declare as extern const void *const name[];
                extern_lines.append(f"extern const void *const {name}[];")

        return extern_lines

    # Also scan for type names referenced in extern declarations
    def _scan_type_forwards(self, extern_lines: List[str]) -> List[str]:
        """Extract type names from extern declarations and generate forward typedefs."""
        type_pat = re.compile(r'\b([A-Z][A-Za-z0-9_]*Type)\b')
        types_seen: Set[str] = set()
        forwards: List[str] = []
        for line in extern_lines:
            for tm in type_pat.finditer(line):
                tname = tm.group(1)
                if tname not in types_seen:
                    types_seen.add(tname)
                    forwards.append(f"typedef struct {{ int _dummy; }} {tname};")
        return forwards

    def _get_own_headers(self) -> Set[str]:
        """Header names that exist in the module's own ssc/ directories."""
        own: Set[str] = set()
        for sub in ("ssc/inc", "ssc/src", "ssc"):
            inc_dir = self.source_dir / sub
            if inc_dir.is_dir():
                for h in inc_dir.rglob("*.h"):
                    own.add(h.name)
        return own

    # ------------------------------------------------------------------
    # Feature classification
    # ------------------------------------------------------------------
    @staticmethod
    def _should_disable(name: str) -> bool:
        """Return True for features that should be STD_OFF.

        Error/safety *reporting* features gate cross-module version checks
        (Det, Dem, …).  Setting them to STD_OFF avoids ``#error`` from
        undefined version macros in stub headers.
        """
        upper = name.upper()
        if upper.endswith("_REPORTING"):
            return True
        if "PARTITION_ERR_CHECK" in upper:
            return True
        return False

    # ------------------------------------------------------------------
    # Stub generators
    # ------------------------------------------------------------------
    def _generate_cfg_h(
        self,
        versions: Dict[str, str],
        features: Set[str],
        dem_switches: Set[str],
        constants: Dict[str, str],
        module_ids: Dict[str, str],
    ):
        """Generate ``{Module}_Cfg.h``."""
        M = self.module          # ADC
        Mm = self.module_mixed   # Adc
        lines = [
            f"/* Auto-generated stub: {Mm}_Cfg.h */",
            f"#ifndef {M}_CFG_H",
            f"#define {M}_CFG_H",
            "",
            '#include "Std_Types.h"',
            "",
        ]

        if versions:
            lines.append("/* Version info */")
            for name, val in sorted(versions.items()):
                lines.append(f"#define {name}   {val}")
            lines.append("")

        if module_ids:
            lines.append("/* Module / Vendor IDs */")
            for name, val in sorted(module_ids.items()):
                lines.append(f"#define {name}   {val}")
            lines.append("")

        # Feature switches
        on_feats  = sorted(f for f in features if not self._should_disable(f))
        off_feats = sorted(f for f in features if self._should_disable(f))

        if on_feats:
            lines.append("/* Feature switches — ON for maximum code coverage */")
            for name in on_feats:
                lines.append(f"#define {name}   STD_ON")
            lines.append("")

        if off_feats:
            lines.append("/* Reporting / partition — OFF to skip cross-module version checks */")
            for name in off_feats:
                lines.append(f"#define {name}   STD_OFF")
            lines.append("")

        # DEM reporting switches (always OFF)
        if dem_switches:
            lines.append("/* DEM reporting — OFF */")
            for name in sorted(dem_switches):
                lines.append(f"#define {name}   STD_OFF")
            lines.append("")

        # Configuration constants
        if constants:
            lines.append("/* Configuration constants */")
            for name, val in sorted(constants.items()):
                lines.append(f"#ifndef {name}")
                lines.append(f"#define {name}   {val}")
                lines.append(f"#endif")
            lines.append("")

        lines.append(f"#endif /* {M}_CFG_H */")
        lines.append("")
        self._write(f"{Mm}_Cfg.h", "\n".join(lines))

    def _generate_data_h(self, source_content: str, own_headers: Set[str]):
        """Generate ``{Module}_Data.h``."""
        name = f"{self.module_mixed}_Data.h"
        if name in own_headers:
            logger.debug("Skipping %s — exists in module source", name)
            return

        # Find module-specific _COUNT constants
        count_re = re.compile(
            rf'\b({self.module}_\w*(?:COUNT|KERNEL_COUNT|SCHM_COUNT)\w*)\b'
        )
        counts = sorted(set(count_re.findall(source_content)))

        # Find extern declarations for module globals (Adc_kData, etc.)
        extern_decls = self._scan_extern_declarations(source_content)
        type_forwards = self._scan_type_forwards(extern_decls)

        lines = [
            f"/* Auto-generated stub: {name} */",
            f"#ifndef {self.module}_DATA_H",
            f"#define {self.module}_DATA_H",
            "",
            f'#include "{self.module_mixed}.h"',
            f'#include "SchM_{self.module_mixed}.h"',
            '#include "Mcal_ExecutionContext.h"',
            "",
        ]
        for c in counts:
            lines += [f"#ifndef {c}", f"#define {c}   8u", f"#endif"]
        if counts:
            lines.append("")

        # Forward-declare types used in extern declarations
        if type_forwards:
            for fwd in type_forwards:
                lines.append(fwd)
            lines.append("")

        # Add extern declarations for module globals
        if extern_decls:
            lines.append("/* Global variable declarations from template headers */")
            for decl in extern_decls:
                lines.append(decl)
            lines.append("")

        lines.append(f"#endif /* {self.module}_DATA_H */")
        lines.append("")
        self._write(name, "\n".join(lines))

    def _generate_pbcfg_h(self, own_headers: Set[str]):
        name = f"{self.module_mixed}_PBcfg.h"
        if name in own_headers:
            return
        lines = [
            f"/* Auto-generated stub: {name} */",
            f"#ifndef {self.module}_PBCFG_H",
            f"#define {self.module}_PBCFG_H",
            '#include "Std_Types.h"',
            f"#endif /* {self.module}_PBCFG_H */",
            "",
        ]
        self._write(name, "\n".join(lines))

    def _generate_cpuprivmode_h(self, own_headers: Set[str]):
        name = f"{self.module_mixed}_CpuPrivMode.h"
        if name in own_headers:
            return
        lines = [
            f"/* Auto-generated stub: {name} */",
            f"#ifndef {self.module}_CPUPRIVMODE_H",
            f"#define {self.module}_CPUPRIVMODE_H",
            '#include "Std_Types.h"',
            "#define MCAL_GETEXECUTIONINDEX()  ((uint32)0)",
            f"#endif /* {self.module}_CPUPRIVMODE_H */",
            "",
        ]
        self._write(name, "\n".join(lines))

    def _generate_memmap_h(self):
        self._write(
            f"{self.module_mixed}_MemMap.h",
            f"/* Auto-generated stub: {self.module_mixed}_MemMap.h — no-op */\n",
        )

    def _generate_cbk_h(self, own_headers: Set[str]):
        name = f"{self.module_mixed}_Cbk.h"
        if name in own_headers:
            return
        lines = [
            f"/* Auto-generated stub: {name} */",
            f"#ifndef {self.module}_CBK_H",
            f"#define {self.module}_CBK_H",
            '#include "Std_Types.h"',
            f"#endif /* {self.module}_CBK_H */",
            "",
        ]
        self._write(name, "\n".join(lines))

    def _generate_schm_h(self, areas: Set[str]):
        """Generate ``SchM_{Module}.h`` with exclusive area macros."""
        Mm = self.module_mixed
        lines = [
            f"/* Auto-generated stub: SchM_{Mm}.h */",
            f"#ifndef SCHM_{self.module}_H",
            f"#define SCHM_{self.module}_H",
            "",
        ]
        if areas:
            for area in sorted(areas):
                lines.append(f"#define SchM_Enter_{Mm}_{area}()")
                lines.append(f"#define SchM_Exit_{Mm}_{area}()")
        else:
            lines.append("/* No exclusive areas detected */")

        lines += ["", f"#endif /* SCHM_{self.module}_H */", ""]
        self._write(f"SchM_{Mm}.h", "\n".join(lines))

    def _generate_cross_module_stubs(
        self, includes: Set[str], own_headers: Set[str],
    ):
        """Generate minimal stubs for headers from other MCAL modules."""
        # Gather names we already provide
        provided: Set[str] = set(COMMON_HEADERS)
        for f in self.output_dir.glob("*.h"):
            provided.add(f.name)

        for inc_name in sorted(includes):
            basename = Path(inc_name).name

            # Skip already-provided, module-own, and SFR headers
            if basename in provided or basename in own_headers:
                continue
            if self._RE_SFR_HEADER.match(basename):
                continue

            guard = re.sub(r'[^A-Za-z0-9]', '_', basename).upper()
            lines = [
                f"/* Auto-generated stub: {basename} */",
                f"#ifndef {guard}",
                f"#define {guard}",
                '#include "Std_Types.h"',
                f"#endif /* {guard} */",
                "",
            ]
            self._write(basename, "\n".join(lines))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _write(self, filename: str, content: str):
        (self.output_dir / filename).write_text(content, encoding="utf-8")

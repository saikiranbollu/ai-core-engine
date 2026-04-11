"""
SFR (Special Function Register) Parser
=======================================

Parses Infineon AURIX SFR header files from a cloned ``aurix3g_sw_mcal_tc4xx_infra_sfr``
repository.  Each device folder (e.g. ``TC49xN/``) contains three files per
peripheral module:

* ``Ifx<Module>_regdef.h`` – ``typedef struct`` bitfield definitions
* ``Ifx<Module>_bf.h``     – ``#define`` masks / offsets / lengths
* ``Ifx<Module>_reg.h``    – base addresses and memory-mapped module instances

This parser extracts structured data from all three file types and returns
lists of dicts suitable for Neo4j ingestion.

Usage::

    from sfr_parsers import parse_sfr_repo

    files, registers, bitfields, base_addrs = parse_sfr_repo(
        repo_dir=Path("temp/temporary_data/aurix3g_sw_mcal_tc4xx_infra_sfr"),
        module="Adc",          # case-insensitive partial match on "IfxAdc_*"
        devices=None,          # None → all devices
    )
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for _regdef.h
# ---------------------------------------------------------------------------

# Matches:  typedef struct _Ifx_ADC_ACCEN_PRS_Bits { ... } Ifx_ADC_ACCEN_PRS_Bits;
_RE_REGDEF_STRUCT = re.compile(
    r'(?:/\*\*\s*\\brief\s+(.*?)\s*\*/\s*)?'            # optional Doxygen brief
    r'typedef\s+struct\s+_?(\w+)\s*\{([^}]*)\}\s*(\w+)\s*;',
    re.DOTALL,
)

# Matches a bitfield member inside a struct, e.g.:
#   __IO Ifx_UReg_32Bit RD00:1;  /**< \brief [0:0] Read access enable ... (rw) */
_RE_BITFIELD = re.compile(
    r'(__IO|__I|__O)\s+\w+\s+'     # access qualifier + type
    r'(\w+)'                        # field name
    r':(\d+);'                      # width
    r'\s*/\*\*<\s*\\brief\s+'       # Doxygen inline brief open
    r'\[(\d+):(\d+)\]\s*'           # [msb:lsb]
    r'(.*?)\s*'                     # description
    r'\((\w+)\)\s*\*/',             # (rw), (rh), (w), etc.
)

# Reserved / padding lines (unnamed fields) — skip these
_RE_RESERVED = re.compile(
    r'(__I|__IO)\s+\w+\s+:(\d+);\s*/\*\*<.*\\internal Reserved',
)

# ---------------------------------------------------------------------------
# Regex patterns for _bf.h
# ---------------------------------------------------------------------------

# #define IFX_ADC_CLC_DISR_LEN (1u)
# #define IFX_ADC_CLC_DISR_MSK (0x1u)
# #define IFX_ADC_CLC_DISR_OFF (0u)
_RE_BF_DEFINE = re.compile(
    r'^\s*#define\s+'
    r'(IFX_\w+?)_(LEN|MSK|OFF)\s+'
    r'\(([^)]+)\)',
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Regex patterns for _reg.h
# ---------------------------------------------------------------------------

# #define MODULE_ADC ((*(Ifx_ADC*)0xF5000000u))
_RE_MODULE_BASE = re.compile(
    r'#define\s+(MODULE_\w+)\s+.*?0x([0-9A-Fa-f]+)u?\)',
)

# #define ADC_CDSP_DSP0_ICCM ((void*)0xF50C0000u)
_RE_REG_ADDRESS = re.compile(
    r'/\*\*\s*\\brief\s+(.*?)\s*\*/\s*'
    r'#define\s+(\w+)\s+.*?0x([0-9A-Fa-f]+)u?\)',
    re.DOTALL,
)

# Header version line
_RE_VERSION = re.compile(r'Version:\s*(\S+)')


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  PUBLIC API                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════╝

def discover_devices(repo_dir: Path) -> list[str]:
    """Return sorted list of device folder names (e.g. ['TC44xA', 'TC49xN', ...])."""
    return sorted(
        d.name for d in repo_dir.iterdir()
        if d.is_dir() and d.name.startswith("TC") and d.name != "EA"
    )


def discover_modules(repo_dir: Path, device: str) -> list[str]:
    """Return sorted list of module names available for a device."""
    device_dir = repo_dir / device
    modules = set()
    for f in device_dir.glob("Ifx*_regdef.h"):
        m = re.match(r'Ifx(\w+?)_regdef\.h$', f.name)
        if m:
            modules.add(m.group(1))
    return sorted(modules)


def parse_sfr_repo(
    repo_dir: Path,
    module: str,
    devices: Optional[list[str]] = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Parse SFR files for the given module across one or more devices.

    Parameters
    ----------
    repo_dir : Path
        Root of the cloned ``aurix3g_sw_mcal_tc4xx_infra_sfr`` repository.
    module : str
        Module name as it appears in filenames (e.g. ``"Adc"``).
        Matched case-insensitively.
    devices : list[str] or None
        Specific device folders to parse.  ``None`` → all devices.

    Returns
    -------
    (sfr_files, registers, bitfields, base_addresses)
        Each is a list of dicts with properties ready for Neo4j ingestion.
    """
    repo_dir = Path(repo_dir)
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"SFR repo not found: {repo_dir}")

    if devices is None:
        devices = discover_devices(repo_dir)
    else:
        devices = [d for d in devices if (repo_dir / d).is_dir()]

    if not devices:
        raise ValueError(f"No valid device folders found in {repo_dir}")

    # Normalise module name to match filename casing (e.g. "ADC" → "Adc")
    module_norm = _normalise_module_name(repo_dir, devices[0], module)
    if module_norm is None:
        raise ValueError(
            f"Module '{module}' not found in {repo_dir / devices[0]}. "
            f"Available: {discover_modules(repo_dir, devices[0])}"
        )

    all_files: list[dict] = []
    all_registers: list[dict] = []
    all_bitfields: list[dict] = []
    all_base_addrs: list[dict] = []

    for device in devices:
        device_dir = repo_dir / device
        if not device_dir.is_dir():
            logger.warning("Skipping missing device dir: %s", device)
            continue

        regdef_path = device_dir / f"Ifx{module_norm}_regdef.h"
        bf_path = device_dir / f"Ifx{module_norm}_bf.h"
        reg_path = device_dir / f"Ifx{module_norm}_reg.h"

        version = None

        # -- regdef.h --
        if regdef_path.is_file():
            text = regdef_path.read_text(encoding="utf-8", errors="replace")
            vm = _RE_VERSION.search(text)
            if vm:
                version = vm.group(1)

            regs, bfs = _parse_regdef(text, device, module_norm.upper(), version)
            all_registers.extend(regs)
            all_bitfields.extend(bfs)

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_regdef.h",
                "file_name": f"Ifx{module_norm}_regdef.h",
                "file_type": "regdef",
                "device": device,
                "module": module_norm.upper(),
                "version": version,
                "register_count": len(regs),
                "bitfield_count": len(bfs),
                "path": str(regdef_path),
            })
            logger.info("  %s/%s_regdef.h → %d registers, %d bitfields",
                        device, module_norm, len(regs), len(bfs))

        # -- bf.h --
        if bf_path.is_file():
            text = bf_path.read_text(encoding="utf-8", errors="replace")
            bf_defines = _parse_bf(text, device, module_norm.upper())

            # Merge bf defines (LEN/MSK/OFF) onto existing bitfields
            _merge_bf_defines(all_bitfields, bf_defines, device)

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_bf.h",
                "file_name": f"Ifx{module_norm}_bf.h",
                "file_type": "bf",
                "device": device,
                "module": module_norm.upper(),
                "version": version,
                "define_count": len(bf_defines),
                "path": str(bf_path),
            })
            logger.info("  %s/%s_bf.h → %d defines", device, module_norm, len(bf_defines))

        # -- reg.h --
        if reg_path.is_file():
            text = reg_path.read_text(encoding="utf-8", errors="replace")
            bases = _parse_reg(text, device, module_norm.upper())
            all_base_addrs.extend(bases)

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_reg.h",
                "file_name": f"Ifx{module_norm}_reg.h",
                "file_type": "reg",
                "device": device,
                "module": module_norm.upper(),
                "version": version,
                "base_address_count": len(bases),
                "path": str(reg_path),
            })
            logger.info("  %s/%s_reg.h → %d base addresses", device, module_norm, len(bases))

    logger.info(
        "SFR parse complete: %d files, %d registers, %d bitfields, %d base addresses",
        len(all_files), len(all_registers), len(all_bitfields), len(all_base_addrs),
    )
    return all_files, all_registers, all_bitfields, all_base_addrs


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  INTERNAL PARSING FUNCTIONS                                          ║
# ╚═══════════════════════════════════════════════════════════════════════╝

def _normalise_module_name(repo_dir: Path, device: str, module: str) -> Optional[str]:
    """Find the exact casing of the module name from filenames.

    SFR files use ``IfxAdc_regdef.h`` (title-cased) while users might pass
    ``"ADC"`` or ``"adc"``.
    """
    device_dir = repo_dir / device
    for f in device_dir.glob("Ifx*_regdef.h"):
        m = re.match(r'^Ifx(\w+?)_regdef\.h$', f.name)
        if m and m.group(1).lower() == module.lower():
            return m.group(1)
    return None


def _parse_regdef(
    text: str,
    device: str,
    module: str,
    version: Optional[str],
) -> tuple[list[dict], list[dict]]:
    """Parse a ``_regdef.h`` file → (registers, bitfields)."""
    registers: list[dict] = []
    bitfields: list[dict] = []

    for m in _RE_REGDEF_STRUCT.finditer(text):
        brief = (m.group(1) or "").strip()
        struct_tag = m.group(2)           # e.g. Ifx_ADC_ACCEN_PRS_Bits
        body = m.group(3)
        typedef_name = m.group(4)         # e.g. Ifx_ADC_ACCEN_PRS_Bits

        # Skip union and memory-map structs (those don't end in _Bits)
        if not typedef_name.endswith("_Bits"):
            continue

        reg_name = typedef_name.replace("_Bits", "").replace("Ifx_", "")  # ADC_ACCEN_PRS
        reg_id = f"SFR:{device}:{reg_name}"

        registers.append({
            "register_id": reg_id,
            "name": reg_name,
            "description": brief,
            "struct_name": typedef_name,
            "device": device,
            "module": module,
            "version": version,
        })

        # Parse bitfield members
        for bf_m in _RE_BITFIELD.finditer(body):
            access_qual = bf_m.group(1)
            field_name = bf_m.group(2)
            width = int(bf_m.group(3))
            msb = int(bf_m.group(4))
            lsb = int(bf_m.group(5))
            desc = bf_m.group(6).strip()
            access = bf_m.group(7)        # rw, rh, w, etc.

            bf_id = f"SFR:{device}:{reg_name}:{field_name}"

            bitfields.append({
                "bitfield_id": bf_id,
                "name": field_name,
                "register_name": reg_name,
                "description": desc,
                "bits": f"[{msb}:{lsb}]",
                "width": width,
                "msb": msb,
                "lsb": lsb,
                "access": access,
                "access_qualifier": access_qual,
                "device": device,
                "module": module,
                "_register_id": reg_id,
            })

    return registers, bitfields


def _parse_bf(text: str, device: str, module: str) -> dict[str, dict]:
    """Parse a ``_bf.h`` file → dict keyed by define prefix.

    Returns ``{ "IFX_ADC_CLC_DISR": {"LEN": "1u", "MSK": "0x1u", "OFF": "0u"}, ... }``
    """
    result: dict[str, dict] = {}
    for m in _RE_BF_DEFINE.finditer(text):
        prefix = m.group(1)   # IFX_ADC_CLC_DISR
        suffix = m.group(2)   # LEN | MSK | OFF
        value = m.group(3)    # 1u
        result.setdefault(prefix, {})[suffix] = value
    return result


def _merge_bf_defines(
    bitfields: list[dict],
    bf_defines: dict[str, dict],
    device: str,
) -> None:
    """Enrich bitfield dicts with mask/offset/length from _bf.h defines."""
    for bf in bitfields:
        if bf["device"] != device:
            continue
        # Build the expected define prefix: IFX_<MODULE>_<REG>_<FIELD>
        # reg_name example: ADC_ACCEN_PRS, field: RD00
        prefix = f"IFX_{bf['register_name']}_{bf['name']}"
        info = bf_defines.get(prefix)
        if info:
            bf["mask"] = info.get("MSK")
            bf["offset_define"] = info.get("OFF")
            bf["length_define"] = info.get("LEN")


def _parse_reg(text: str, device: str, module: str) -> list[dict]:
    """Parse a ``_reg.h`` file → list of base address dicts."""
    bases: list[dict] = []

    for m in _RE_MODULE_BASE.finditer(text):
        name = m.group(1)     # MODULE_ADC
        addr = m.group(2)     # F5000000
        bases.append({
            "base_address_id": f"SFR:{device}:{name}",
            "name": name,
            "address": f"0x{addr}",
            "device": device,
            "module": module,
            "address_type": "module_base",
        })

    for m in _RE_REG_ADDRESS.finditer(text):
        desc = m.group(1).strip()
        name = m.group(2)
        addr = m.group(3)
        # Skip module bases (already captured above)
        if name.startswith("MODULE_"):
            continue
        bases.append({
            "base_address_id": f"SFR:{device}:{name}",
            "name": name,
            "description": desc,
            "address": f"0x{addr}",
            "device": device,
            "module": module,
            "address_type": "memory_region",
        })

    return bases

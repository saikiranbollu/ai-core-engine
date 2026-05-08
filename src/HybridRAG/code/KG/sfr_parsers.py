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

    files, registers, bitfields = parse_sfr_repo(
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


# ── Module → SFR peripheral name mapping ──────────────────────────────────
# MCAL module names don't always match the SFR peripheral prefix in
# ``Ifx<Peripheral>_regdef.h``.  This map provides explicit overrides.
# Keys are upper-cased MCAL module names; values are the *exact* SFR
# peripheral prefix as it appears in filenames (title-cased).
_MODULE_SFR_MAP: dict[str, str] = {
    "ETH_17_LETH": "Leth",
    "ETH_17_GETH": "Geth",
    "SPI": "Qspi",
    "GPT": "Gpt12",
    "GTM": "Egtm",
    "CAN_17_MCMCAN": "Can",
    "LIN_17_ASCLIN": "Asclin",
    "WDG_17_WTU": "Wtu",
}

# Reverse map: upper-cased SFR peripheral → canonical MCAL module name.
_SFR_TO_MCAL_MAP: dict[str, str] = {
    v.upper(): k for k, v in _MODULE_SFR_MAP.items()
}


def resolve_mcal_module_name(module: str) -> str:
    """Resolve a peripheral or MCAL module name to the canonical MCAL name.

    E.g. "Leth" → "ETH_17_LETH", "Gpt12" → "GPT", "DMA" → "DMA".
    """
    upper = module.upper()
    # Already a full MCAL name (key in _MODULE_SFR_MAP)?
    if upper in _MODULE_SFR_MAP:
        return upper
    # Peripheral name (value in _MODULE_SFR_MAP)?  Reverse-lookup.
    return _SFR_TO_MCAL_MAP.get(upper, upper)


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
# Register instance defines in _reg.h:
# #define DMA0_PROTSE /*lint --e(923, 9078)*/ (*(volatile Ifx_DMA_PROT*)0xF0010024u)
_RE_REG_INSTANCE = re.compile(
    r'/\*\*\s*\\brief\s+(.*?)\s*\*/\s*'
    r'#define\s+(\w+)\s+/\*.*?\*/\s*'
    r'\(\*\(volatile\s+(Ifx_\w+)\*\)0x([0-9A-Fa-f]+)u?\)',
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
) -> tuple[list[dict], list[dict], list[dict]]:
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
    (sfr_files, registers, bitfields)
        Each is a list of dicts with properties ready for Neo4j ingestion.
        Base addresses are collapsed into properties: ``module_base_address``
        on the reg file dict, and ``address`` on individual register dicts.
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
    # Try first device, then fall back to others (some peripherals like Geth
    # only exist on certain devices).
    module_norm = None
    for dev in devices:
        module_norm = _normalise_module_name(repo_dir, dev, module)
        if module_norm is not None:
            break
    if module_norm is None:
        raise ValueError(
            f"Module '{module}' not found in any device folder. "
            f"Available in {devices[0]}: {discover_modules(repo_dir, devices[0])}"
        )

    # Canonical MCAL module name for KG node properties (e.g. "ETH_17_LETH")
    mcal_module = resolve_mcal_module_name(module)

    all_files: list[dict] = []
    all_registers: list[dict] = []
    all_bitfields: list[dict] = []

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

            regs, bfs = _parse_regdef(text, device, mcal_module, version)
            all_registers.extend(regs)
            all_bitfields.extend(bfs)

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_regdef.h",
                "file_name": f"Ifx{module_norm}_regdef.h",
                "file_type": "regdef",
                "device": device,
                "module": mcal_module,
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
            bf_defines = _parse_bf(text, device, mcal_module)

            # Merge bf defines (LEN/MSK/OFF) onto existing bitfields
            _merge_bf_defines(all_bitfields, bf_defines, device)

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_bf.h",
                "file_name": f"Ifx{module_norm}_bf.h",
                "file_type": "bf",
                "device": device,
                "module": mcal_module,
                "version": version,
                "define_count": len(bf_defines),
                "path": str(bf_path),
            })
            logger.info("  %s/%s_bf.h → %d defines", device, module_norm, len(bf_defines))

        # -- reg.h --
        if reg_path.is_file():
            text = reg_path.read_text(encoding="utf-8", errors="replace")
            bases = _parse_reg(text, device, mcal_module)

            # Parse register instances (e.g. PROTSE sharing PROT type)
            existing_reg_names = {r["name"] for r in all_registers if r["device"] == device}
            instances = _parse_reg_instances(
                text, device, mcal_module, version, existing_reg_names
            )
            all_registers.extend(instances)

            # Enrich registers with addresses from base address list.
            # Build name→address lookup from parsed bases.
            addr_lookup: dict[str, str] = {
                b["name"]: b["address"] for b in bases
            }
            for reg in all_registers:
                if reg["device"] != device:
                    continue
                if "address" not in reg or reg.get("address") is None:
                    addr = addr_lookup.get(reg["name"])
                    if addr:
                        reg["address"] = addr

            # Extract module base address
            module_base = next(
                (b["address"] for b in bases if b["address_type"] == "module_base"),
                None,
            )

            all_files.append({
                "file_id": f"SFR:{device}:Ifx{module_norm}_reg.h",
                "file_name": f"Ifx{module_norm}_reg.h",
                "file_type": "reg",
                "device": device,
                "module": mcal_module,
                "version": version,
                "base_address_count": len(bases),
                "module_base_address": module_base,
                "instance_count": len(instances),
                "path": str(reg_path),
            })
            logger.info("  %s/%s_reg.h → %d base addresses (collapsed), %d register instances",
                        device, module_norm, len(bases), len(instances))

    logger.info(
        "SFR parse complete: %d files, %d registers, %d bitfields "
        "(base addresses collapsed into register/file properties)",
        len(all_files), len(all_registers), len(all_bitfields),
    )
    return all_files, all_registers, all_bitfields


# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  INTERNAL PARSING FUNCTIONS                                          ║
# ╚═══════════════════════════════════════════════════════════════════════╝

def _normalise_module_name(repo_dir: Path, device: str, module: str) -> Optional[str]:
    """Find the exact casing of the module name from filenames.

    SFR files use ``IfxAdc_regdef.h`` (title-cased) while users might pass
    ``"ADC"`` or ``"adc"``.  For modules whose MCAL name differs from the
    SFR peripheral name (e.g. ``ETH_17_LETH`` → ``Leth``), an explicit
    mapping in ``_MODULE_SFR_MAP`` is checked first.
    """
    # Check explicit mapping first (e.g. ETH_17_LETH → Leth)
    mapped = _MODULE_SFR_MAP.get(module.upper())
    if mapped is not None:
        # Verify the mapped name actually exists in this device
        device_dir = repo_dir / device
        regdef = device_dir / f"Ifx{mapped}_regdef.h"
        if regdef.is_file():
            return mapped
        # File doesn't exist for this device — fall through to glob search

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


def _instance_name_to_reg_name(instance_name: str) -> str:
    """Strip trailing digits from module instance prefix.

    DMA0_PROTSE → DMA_PROTSE, ADC0_CLC → ADC_CLC
    """
    parts = instance_name.split('_', 1)
    if len(parts) == 2:
        module = re.sub(r'\d+$', '', parts[0])
        return f"{module}_{parts[1]}"
    return instance_name


def _parse_reg_instances(
    text: str,
    device: str,
    module: str,
    version: Optional[str],
    existing_names: set[str],
) -> list[dict]:
    """Parse register INSTANCE defines from ``_reg.h``.

    Creates SFR_Register nodes for instances whose normalized name differs
    from existing nodes (i.e. instances that share a type with another register
    but have a distinct name, like PROTSE using Ifx_DMA_PROT type).
    """
    instances: list[dict] = []

    for m in _RE_REG_INSTANCE.finditer(text):
        desc = (m.group(1) or "").strip()
        define_name = m.group(2)      # DMA0_PROTSE
        type_name = m.group(3)        # Ifx_DMA_PROT
        addr = m.group(4)             # F0010024

        # Skip aliases (defines whose name starts with MODULE_)
        if define_name.startswith("MODULE_"):
            continue

        # Normalize: DMA0_PROTSE → DMA_PROTSE
        reg_name = _instance_name_to_reg_name(define_name)

        # Skip if already covered by _parse_regdef
        if reg_name in existing_names:
            continue

        # Derive the "parent" type name: Ifx_DMA_PROT → DMA_PROT
        parent_reg_name = type_name.replace("Ifx_", "")

        reg_id = f"SFR:{device}:{reg_name}"
        instances.append({
            "register_id": reg_id,
            "name": reg_name,
            "description": desc,
            "struct_name": type_name,
            "parent_register": parent_reg_name,
            "address": f"0x{addr}",
            "device": device,
            "module": module,
            "version": version,
            "source": "reg_instance",
        })
        existing_names.add(reg_name)

    return instances

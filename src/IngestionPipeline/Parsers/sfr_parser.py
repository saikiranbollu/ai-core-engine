"""
SFR (Register Definition) Header Parser
========================================

Parses C header files that contain register/bitfield definitions
(e.g. ``IfxCxpi_regdef.h``) and returns a structured representation with
module name, registers, bitfields, and statistics.

Usage::

    from IngestionPipeline.Parsers import sfr_parser

    result = sfr_parser.parse("IfxCxpi_regdef.h")
    # result is a dict with module, file, registers, statistics
"""

import re
from pathlib import Path
from typing import Any, Dict, List


def parse(path: str) -> Dict[str, Any]:
    """
    Parse register definitions from a C header file.

    Args:
        path: Path to a header file containing register bitfield structs.

    Returns:
        A dict with keys ``module``, ``file``, ``registers`` (mapping
        struct names to lists of bitfield dicts), and ``statistics``.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    # Derive module name from filename convention (e.g. IfxCxpi_regdef.h → Cxpi)
    mod_match = re.search(r'Ifx(\w+)_regdef', p.stem)
    module_name = mod_match.group(1) if mod_match else p.stem.replace('_regdef', '')

    lines = p.read_text(encoding="utf-8").splitlines()

    struct_decl_re = re.compile(r'typedef struct\s+_?([A-Za-z0-9_]+)')
    bitfield_re = re.compile(
        r'^\s*\w+\s+Ifx_UReg_32Bit(?:\s+(\w+))?\s*:(\d+);\s*/\*\*<(.*?)\*/\s*$'
    )
    comment_re = re.compile(r'\\brief\s*\[([^\]]+)\]\s*(.*)')

    registers: Dict[str, List[Dict[str, str]]] = {}
    waiting = False
    pending_name = None
    current_struct = None

    for line in lines:
        if waiting:
            if '{' in line:
                current_struct = pending_name
                registers[current_struct] = []
                waiting = False
                pending_name = None
            continue

        sm = struct_decl_re.search(line)
        if sm:
            pending_name = sm.group(1)
            waiting = True
            continue

        bm = bitfield_re.search(line)
        if bm and current_struct is not None:
            field_name = bm.group(1) or ''
            bit_width = bm.group(2)
            comment = bm.group(3).strip()

            cm = comment_re.search(comment)
            bit_range = cm.group(1) if cm else ''
            description = cm.group(2).strip() if cm else comment

            if not field_name:
                safe = bit_range.replace(':', '_')
                field_name = f'Reserved_{safe}' if bit_range else 'Reserved'
                label = f"Reserved [{bit_range}]" if bit_range else 'Reserved'
                description = description or 'Reserved'
            else:
                label = f"{field_name} [{bit_range}]" if bit_range else field_name

            registers[current_struct].append({
                'name': field_name,
                'width': bit_width,
                'bit_range': bit_range,
                'description': description,
                'label': label,
            })

        if line.strip().startswith('}'):
            current_struct = None

    return {
        "module": module_name,
        "file": p.name,
        "registers": registers,
        "statistics": {
            "total_registers": len(registers),
            "total_bitfields": sum(len(v) for v in registers.values()),
        },
    }

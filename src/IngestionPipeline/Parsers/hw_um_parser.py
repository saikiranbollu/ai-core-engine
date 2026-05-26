"""
Hardware User Manual Parser – TC44x / TC4xx format (Markdown → Structured JSON)
================================================================================

Extracts hardware register specifications from Markdown produced by
``pdf_parser.py`` for Infineon TC44x (and similar) User Manual PDFs.

This parser handles the TC44x UM markdown format which differs from the
ILLD hardware-spec format handled by ``hw_spec_parser.py``:

- Register names in ``**BOLD**`` markers (not plain text)
- Offset addresses with backticks, ``<sub>H</sub>``, ``\\_H``, or ``_H``
- ``Kernel Reset value:`` (not ``rst_\\w+ value:``)
- LLM-generated ``\\n`` artifacts in lines
- Field tables with ``| Field | Bits | Type | Description |`` columns

Output format is **identical** to ``hw_spec_parser.parse()`` so that the
same KG builder and RAG ingestion code can consume the results.

Usage::

    from IngestionPipeline.Parsers import hw_um_parser

    result = hw_um_parser.parse("hw_um_gpt12_tc44x.md")
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword sets (same as hw_spec_parser for consistency)
# ---------------------------------------------------------------------------
_INTERRUPT_KWS = {"INTR", "INT", "IRQ", "INTERRUPT", "SRN", "SRC"}

_ERROR_KWS = {
    "ERROR", "ERR", "FAULT", "TIMEOUT", "FAIL",
    "VIOLATION", "LOSS", "ARB_LOST", "OVERRUN", "UNDERRUN",
}

_ENABLE_KWS = [
    "enable", "enabled", "disable", "disabled",
    "activation", "activate", "deactivate",
    "must be set", "should be set", "control",
    "turn on", "turn off", "switch on", "switch off",
]

_OP_KWS: Dict[str, str] = {
    "transmit": "transmit", "transmission": "transmit",
    "send": "send", "sending": "send",
    "receive": "receive", "reception": "receive", "receiving": "receive",
    "transfer": "transfer", "read": "read", "write": "write",
    "enable": "enable", "disable": "disable",
    "reset": "reset", "initialize": "init", "configuration": "config",
}

_TIMING_KWS = ["after", "when", "on", "upon", "during", "at", "before", "following"]
_STATUS_WORDS = {"done", "error", "complete", "ready", "flag", "status"}


# ---------------------------------------------------------------------------
# Hex-value normaliser
# ---------------------------------------------------------------------------
def _normalise_hex(raw: str) -> str:
    """Strip formatting artefacts and normalise a hex value string."""
    v = raw.strip()
    v = re.sub(r"</?sub>", "", v)          # <sub>H</sub>
    v = v.replace("`", "")                 # backticks
    v = v.replace("\\", "")               # escaped underscore \_
    v = v.replace("_", "")                 # plain underscore
    v = v.replace(" ", "")                 # internal spaces (0000 0000)
    # Ensure trailing H
    if v and not v.upper().endswith("H"):
        v = v + "H"
    return v.upper()


# ---------------------------------------------------------------------------
# Internal extractor
# ---------------------------------------------------------------------------

class _TC44xExtractor:
    """Regex-based HW UM extractor for TC44x-style markdown."""

    def __init__(self, content: str, source_name: str):
        self._content = content
        self._lines = content.split("\n")
        self._source = source_name

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _is_formatting_row(cells: List[str]) -> bool:
        for cell in cells:
            c = cell.strip()
            if not c:
                continue
            if re.match(r"^[:\-\s]+$", c):
                return True
        first = cells[0].strip().lower() if cells else ""
        return first in {"field", "name", "bit", "bits"}

    @staticmethod
    def _is_reserved(name: str) -> bool:
        c = name.strip()
        return c == "0" or c.lower() == "reserved"

    # ── 1. Registers ────────────────────────────────────────────────

    # Pattern 1: **NAME** — Description  (bold name + em dash on same line)
    _PAT_REG1 = re.compile(
        r"^\*\*([A-Z][A-Z0-9_]+)\*\*\s*[—–-]\s*(.+)$"
    )
    # Pattern 2: **NAME** alone on line (description on next line)
    _PAT_REG2 = re.compile(r"^\*\*([A-Z][A-Z0-9_]+)\*\*\s*$")
    # Pattern 3: **NAME**  \nDescription  (LLM literal \n artefact)
    _PAT_REG3 = re.compile(
        r"^\*\*([A-Z][A-Z0-9_]+)\*\*\s*\\n\s*(.+)$"
    )

    # Offset address (all format variants)
    _PAT_OFFSET = re.compile(
        r"Offset\s+address:\s*[`]*([0-9A-Fa-f_ \\]+)"
        r"(?:H|<sub>H</sub>)[`]*",
        re.IGNORECASE,
    )
    # Reset value (all format variants)
    _PAT_RESET = re.compile(
        r"(?:Kernel\s+)?Reset\s+value:\s*[`]*([0-9A-Fa-f_ \\]+)"
        r"(?:H|<sub>H</sub>)[`]*",
        re.IGNORECASE,
    )

    def extract_registers(self) -> List[Dict[str, Any]]:
        registers: List[Dict[str, Any]] = []
        seen: set[str] = set()

        i = 0
        while i < len(self._lines):
            line = self._lines[i].strip()
            name = desc = None

            # Pattern 1: **NAME** — Description
            m = self._PAT_REG1.match(line)
            if m:
                name, desc = m.group(1), m.group(2).strip()
            else:
                # Pattern 3: **NAME**  \nDescription (literal \n)
                m = self._PAT_REG3.match(line)
                if m:
                    name, desc = m.group(1), m.group(2).strip()
                else:
                    # Pattern 2: **NAME** alone
                    m = self._PAT_REG2.match(line)
                    if m:
                        name = m.group(1)
                        desc = self._find_description(i)

            if name and name not in seen:
                reg = self._scan_offset_reset(i, name, desc or "")
                if reg:
                    seen.add(name)
                    registers.append(reg)

            i += 1

        logger.info("Extracted %d registers", len(registers))
        return registers

    def _scan_offset_reset(
        self, start: int, short: str, long_name: str, lookahead: int = 10,
    ) -> Dict[str, Any] | None:
        offset = reset_value = None

        for j in range(start + 1, min(start + lookahead, len(self._lines))):
            ahead = self._lines[j].strip()
            # Strip leading bullet
            if ahead.startswith("- "):
                ahead = ahead[2:].strip()

            om = self._PAT_OFFSET.search(ahead)
            if om:
                offset = _normalise_hex(om.group(1))

            rm = self._PAT_RESET.search(ahead)
            if rm:
                reset_value = _normalise_hex(rm.group(1))

        if not offset:
            return None
        return {
            "name": short,
            "long_name": long_name,
            "offset": offset,
            "reset_value": reset_value,
            "reset_type": "kernel",
        }

    def _find_description(self, start: int) -> str | None:
        """Find the description text following a register name on its own line."""
        for idx in range(start + 1, min(start + 6, len(self._lines))):
            cl = self._lines[idx].strip()
            if not cl:
                continue
            # Skip if we hit structural lines
            if cl.startswith("|") or cl.startswith("#"):
                continue
            if "offset address" in cl.lower() or "reset value" in cl.lower():
                continue
            if self._PAT_REG1.match(cl) or self._PAT_REG2.match(cl):
                break
            if len(cl) > 5:
                return cl
        return None

    # ── 2. Fields ───────────────────────────────────────────────────

    def extract_fields(self, registers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        reg_names = {r["name"] for r in registers}
        all_fields: List[Dict[str, Any]] = []
        current_reg: str | None = None

        for i, line in enumerate(self._lines):
            stripped = line.strip()

            # Track current register context
            detected = self._detect_register_context(stripped, reg_names)
            if detected:
                current_reg = detected

            # Parse field table rows
            if current_reg and stripped.startswith("|") and stripped.count("|") >= 4:
                field = self._parse_field_row(stripped, current_reg)
                if field:
                    all_fields.append(field)

        # Deduplicate
        seen: set[tuple] = set()
        unique: List[Dict[str, Any]] = []
        for f in all_fields:
            key = (f["parent_register"], f["name"], f["bits"])
            if key not in seen:
                seen.add(key)
                unique.append(f)

        logger.info("Extracted %d unique fields", len(unique))
        return unique

    def _detect_register_context(
        self, line: str, reg_names: set[str],
    ) -> str | None:
        """Detect when we enter a new register's section."""
        # Pattern 1/3: **NAME** — desc  or  **NAME**  \n desc
        m = self._PAT_REG1.match(line) or self._PAT_REG3.match(line)
        if m and m.group(1) in reg_names:
            return m.group(1)
        # Pattern 2: **NAME** alone
        m = self._PAT_REG2.match(line)
        if m and m.group(1) in reg_names:
            return m.group(1)
        # Heading containing register name
        if line.startswith("#"):
            for rn in reg_names:
                if rn in line:
                    return rn
        return None

    def _parse_field_row(self, line: str, register: str) -> Dict[str, Any] | None:
        """Parse a field from a pipe-delimited table row.

        Handles both column orderings:
          | Field | Bits | Type | Description |   (4-col, field-first)
          | Bit(s) | Field | Access |             (3-col, bits-first)
        """
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 3 or self._is_formatting_row(cells):
            return None

        # Detect column order: if first cell looks like bit-range, swap
        if re.match(r"^\d+(:\d+)?$", cells[0]):
            # | Bits | Field | Access | format (3-col bit-layout tables)
            bits, name, ftype = cells[0], cells[1], cells[2]
            desc = cells[3] if len(cells) > 3 else ""
        else:
            # | Field | Bits | Type | Description | format (4-col)
            name, bits, ftype = cells[0], cells[1], cells[2]
            desc = cells[3] if len(cells) > 3 else ""

        name = name.strip()
        bits = bits.strip()
        ftype = ftype.strip()
        desc = desc.strip()

        if self._is_reserved(name) or not name or not bits:
            return None
        # Name must look like a valid identifier (not a number or bit-range)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            return None
        if not re.match(r"^\d+(:\d+)?$", bits):
            return None
        # Accept common access types
        if ftype and not re.match(r"^r[wh]*$|^w$|^rw[h]?$", ftype, re.IGNORECASE):
            return None

        return {
            "name": name,
            "parent_register": register,
            "bits": bits,
            "type": ftype,
            "description": desc[:200],
        }

    # ── 3. Interrupts ──────────────────────────────────────────────

    def extract_interrupts(self, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Find interrupt-related single-bit fields."""
        interrupts: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for f in fields:
            name = f["name"].upper()
            desc = f.get("description", "").upper()
            reg = f.get("parent_register", "").upper()

            is_interrupt = (
                any(kw in name for kw in _INTERRUPT_KWS)
                or any(kw in reg for kw in _INTERRUPT_KWS)
                or "INTERRUPT" in desc
                or "SERVICE REQUEST" in desc
            )
            if not is_interrupt:
                continue
            # Prefer single-bit fields
            if ":" in f["bits"]:
                continue
            if f["name"] in seen:
                continue
            seen.add(f["name"])
            interrupts.append({
                "name": f["name"],
                "register": f["parent_register"],
                "bit": int(f["bits"]),
                "description": f.get("description", "")[:200],
            })

        logger.info("Extracted %d interrupts", len(interrupts))
        return interrupts

    # ── 4. Errors ───────────────────────────────────────────────────

    def extract_errors(self, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Find error-related fields."""
        errors: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for f in fields:
            name = f["name"].upper()
            if not any(kw in name for kw in _ERROR_KWS):
                continue
            if f["name"] in seen:
                continue
            seen.add(f["name"])

            upper = name
            if "TX" in upper or "TRANSMIT" in upper or "SEND" in upper:
                etype = "transmission"
            elif "RX" in upper or "RECEIVE" in upper:
                etype = "reception"
            elif "BUS" in upper or "ARB" in upper:
                etype = "bus"
            elif "PARITY" in upper or "CRC" in upper or "CHECKSUM" in upper:
                etype = "data_integrity"
            elif "TIMEOUT" in upper or "WATCHDOG" in upper:
                etype = "timing"
            else:
                etype = "general"

            bits = f["bits"]
            errors.append({
                "name": f["name"],
                "type": etype,
                "description": f.get("description", "")[:200],
                "detected_in": f["parent_register"],
                "bit": int(bits.split(":")[0]) if re.match(r"^\d+", bits) else None,
            })

        logger.info("Extracted %d errors", len(errors))
        return errors

    # ── 5. Formulas ─────────────────────────────────────────────────

    def extract_formulas(self) -> List[Dict[str, Any]]:
        formulas: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            text = text.strip()
            if text in seen:
                return
            if re.match(r"^[A-Z_]+\s*=\s*\d+$", text):
                return
            seen.add(text)
            formulas.append({
                "name": f"formula_{len(formulas) + 1}",
                "formula": text,
                "type": "calculation",
                "description": "Mathematical formula from hardware user manual",
            })

        # Method 1: LaTeX $$...$$ blocks
        for m in re.finditer(r"\$\$(.+?)\$\$", self._content, re.DOTALL):
            _add(m.group(1))

        # Method 2: \(...\) inline LaTeX
        for m in re.finditer(r"\\\((.+?)\\\)", self._content):
            inner = m.group(1).strip()
            if "=" in inner and len(inner) > 5:
                _add(inner)

        # Method 3: plain-text equations
        eq_pat = re.compile(
            r"^[\s\-•]*([A-Za-z_][A-Za-z0-9_,\.]*)\s*=\s*(.+?)$"
        )
        for line in self._lines:
            m = eq_pat.match(line.strip())
            if m:
                expr = m.group(2).strip()
                if any(op in expr for op in ["/", "*", "+", "-", "×", "÷", "(", ")"]):
                    if not re.match(r"^\d+(\.\d+)?$", expr):
                        _add(f"{m.group(1)} = {expr}")

        logger.info("Extracted %d formulas", len(formulas))
        return formulas

    # ── 6. Relationships ────────────────────────────────────────────

    def build_relationships(
        self,
        registers: List[Dict],
        fields: List[Dict],
        interrupts: List[Dict],
        errors: List[Dict],
        formulas: List[Dict],
    ) -> Dict[str, List[Dict]]:
        rels: Dict[str, List[Dict]] = {
            "register_has_field": [],
            "error_sets_interrupt": [],
            "field_used_in_formula": [],
            "field_enables_feature": [],
            "interrupt_masked_by_field": [],
            "operation_triggers_interrupt": [],
        }

        # 1. register_has_field
        for f in fields:
            rels["register_has_field"].append({
                "register": f["parent_register"],
                "field": f["name"],
                "bits": f["bits"],
            })

        # 2. error_sets_interrupt
        seen_ei: set[tuple] = set()
        for err in errors:
            for intr in interrupts:
                if intr["name"] == err["name"]:
                    key = (err["name"], intr["name"], intr["register"], intr["bit"])
                    if key not in seen_ei:
                        seen_ei.add(key)
                        rels["error_sets_interrupt"].append({
                            "error": err["name"],
                            "error_register": err["detected_in"],
                            "interrupt": f"{intr['register']}.{intr['name']}",
                            "interrupt_register": intr["register"],
                            "interrupt_bit": intr["bit"],
                        })

        # 3. field_used_in_formula
        rels["field_used_in_formula"] = self._field_formula_rels(fields, formulas)

        # 4. field_enables_feature
        rels["field_enables_feature"] = self._field_enables_rels(fields)

        # 5. interrupt_masked_by_field (stub)

        # 6. operation_triggers_interrupt
        rels["operation_triggers_interrupt"] = self._op_trigger_rels(interrupts)

        for k, v in rels.items():
            logger.info("  %s: %d", k, len(v))
        return rels

    # ── relationship helpers ─────────────────────────────────────────

    def _field_formula_rels(
        self, fields: List[Dict], formulas: List[Dict],
    ) -> List[Dict]:
        lookup: Dict[str, Dict] = {}
        for f in fields:
            lookup[f["name"]] = f

        rels: List[Dict] = []
        seen: set[tuple] = set()
        for formula in formulas:
            clean = (
                formula["formula"]
                .replace("\\_", "_")
                .replace("\\times", "*")
                .replace("\\text{", "")
                .replace("\\mathrm{", "")
                .replace("\\,", " ")
                .replace("\\quad", " ")
                .replace("}", "")
            )
            for potential in re.findall(
                r"[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)?", clean,
            ):
                fname = potential.split(".")[-1] if "." in potential else potential
                if fname not in lookup:
                    continue
                fi = lookup[fname]
                role = (
                    "output"
                    if re.search(rf"^{re.escape(fname)}\s*=", clean)
                    else "input"
                )
                key = (formula["name"], fname, fi["parent_register"])
                if key not in seen:
                    seen.add(key)
                    rels.append({
                        "formula": formula["name"],
                        "field": fname,
                        "register": fi["parent_register"],
                        "bits": fi["bits"],
                        "role": role,
                        "formula_content": formula["formula"][:100],
                    })
        return rels

    def _field_enables_rels(self, fields: List[Dict]) -> List[Dict]:
        rels: List[Dict] = []
        seen: set[tuple] = set()
        for f in fields:
            desc = f.get("description", "").lower()
            if not any(kw in desc for kw in _ENABLE_KWS):
                continue
            feature = None
            m = re.search(r"enable\s+(?:the\s+)?([a-z_][a-z0-9_\s]*)", desc)
            if m:
                feature = m.group(1).strip().replace(" ", "_")
            if not feature:
                m = re.search(r"([a-z_][a-z0-9_\s]*)\s+enable", desc)
                if m:
                    feature = m.group(1).strip().replace(" ", "_")
            if not feature:
                feature = (
                    re.sub(r"(_EN|_ENABLE|_ENABLED|EN)$", "", f["name"], flags=re.I).lower()
                    + "_feature"
                )
            key = (f["name"], f["parent_register"], feature)
            if key not in seen:
                seen.add(key)
                rels.append({
                    "field": f["name"],
                    "register": f["parent_register"],
                    "bits": f["bits"],
                    "feature": feature,
                    "enable_value": 1,
                    "disable_value": 0,
                    "description": f.get("description", "")[:100],
                })
        return rels

    def _op_trigger_rels(self, interrupts: List[Dict]) -> List[Dict]:
        rels: List[Dict] = []
        seen: set[tuple] = set()
        for intr in interrupts:
            name = intr["name"]
            desc = intr.get("description", "").lower()
            operation = self._detect_operation(name, desc)
            timing = self._detect_timing(name, desc)
            if operation:
                key = (operation, name)
                if key not in seen:
                    seen.add(key)
                    rels.append({
                        "operation": operation,
                        "interrupt": name,
                        "interrupt_register": intr["register"],
                        "interrupt_bit": intr["bit"],
                        "timing": timing,
                        "description": intr.get("description", "")[:100],
                    })
        return rels

    @staticmethod
    def _detect_operation(name: str, desc: str) -> str | None:
        for tkw in _TIMING_KWS:
            if tkw in desc:
                remaining = desc.split(tkw, 1)[1]
                for ok, on in _OP_KWS.items():
                    if ok in remaining:
                        return on
        upper = name.upper()
        if "TX" in upper or "TRANSMIT" in upper:
            return "transmit"
        if "RX" in upper or "RECEIVE" in upper:
            return "receive"
        if "SEND" in upper:
            return "send"
        parts = name.lower().split("_")
        op_parts = [p for p in parts if p not in _STATUS_WORDS]
        return "_".join(op_parts) if op_parts else None

    @staticmethod
    def _detect_timing(name: str, desc: str) -> str:
        if "after" in desc or "complete" in desc or "done" in name.lower():
            return "after_completion"
        if "before" in desc or "start" in desc:
            return "before_start"
        if "during" in desc:
            return "during_operation"
        if "error" in name.lower() or "error" in desc:
            return "on_error"
        return "on_event"

    # ── top-level ────────────────────────────────────────────────────

    def extract_all(self) -> Dict[str, Any]:
        registers = self.extract_registers()
        fields = self.extract_fields(registers)
        interrupts = self.extract_interrupts(fields)
        errors = self.extract_errors(fields)
        formulas = self.extract_formulas()
        relationships = self.build_relationships(
            registers, fields, interrupts, errors, formulas,
        )
        return {
            "metadata": {
                "source_file": self._source,
                "total_lines": len(self._lines),
                "extraction_date": datetime.now().isoformat(),
                "counts": {
                    "registers": len(registers),
                    "fields": len(fields),
                    "interrupts": len(interrupts),
                    "errors": len(errors),
                    "formulas": len(formulas),
                },
            },
            "registers": registers,
            "fields": fields,
            "interrupts": interrupts,
            "errors": errors,
            "formulas": formulas,
            "relationships": relationships,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(path: str) -> Dict[str, Any]:
    """
    Extract hardware specifications from a TC44x User Manual Markdown file.

    The input should be a Markdown file produced by ``pdf_parser.parse()``
    from an Infineon TC44x (or similar) User Manual PDF.

    Args:
        path: Path to the ``.md`` file.

    Returns:
        A dict with keys ``metadata``, ``registers``, ``fields``,
        ``interrupts``, ``errors``, ``formulas``, and ``relationships``.
        Format is identical to ``hw_spec_parser.parse()``.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    content = p.read_text(encoding="utf-8")
    logger.info(
        "Parsing TC44x HW UM from %s (%d lines)", p.name, content.count("\n") + 1,
    )

    return _TC44xExtractor(content, p.name).extract_all()

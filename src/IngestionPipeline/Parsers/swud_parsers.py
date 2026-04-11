"""
SWUD (Software Unit Design) Markdown Parsers
=============================================

Document-agnostic, section-agnostic parsers that extract structured nodes
from SWUD markdown files for ingestion into the Neo4j knowledge graph.

Supported Node Types (from ontology):
    - SWUD_DesignDecision       (design decisions with [req] tags)
    - SWUD_DerivedConfigParam   (preprocessor macros / #define)
    - SWUD_ConfigStructure      (configuration structure variables)
    - SWUD_CodeGenMacro         (code generation x-path macros)
    - SWUD_TypeDefinition       (C struct/enum/typedef definitions)
    - SWUD_Function             (API/callback/scheduled/interrupt/local)
    - SWUD_DataVariable         (runtime RAM/ROM global variables)
    - SWUD_CriticalSection      (SchM Enter/Exit exclusive areas)
    - SWUD_MemorySection        (memory section allocations)

Detection is **pattern-based** — the parsers infer content type from
structural markers in the markdown (headings, [req] tags, spec table
layouts, keywords) rather than relying on hardcoded section numbers.
This ensures the pipeline works for any MCAL module's SWUD document.

Usage::

    from swud_parsers import parse_swud_directory

    nodes_by_type = parse_swud_directory(
        swud_dir="path/to/swud/",
        module="ADC",
    )
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("swud_parsers")

# ---------------------------------------------------------------------------
# Regex patterns  (generic — no section-number assumptions)
# ---------------------------------------------------------------------------

# [req featureID={...} parentID=...] ... [/req]
# Brackets around GUIDs vary: {GUID}, (GUID), [GUID], or bare GUID
_REQ_TAG_RE = re.compile(
    r"\\?\[_?req\s+featureID\s*=\s*"
    r"\\?[\[{(]?(?P<fid>[0-9A-Fa-f\-]+)\\?[\]})]?\s*"
    r"parentID\s*=\s*(?P<pids>[^\]]+?)"
    r"\\?\](?P<title>[^\[]*?)\\?\[/?req\\?\]",
    re.IGNORECASE | re.DOTALL,
)

# Broader fallback for featureID/parentID extraction
_REQ_TAG_ALT_RE = re.compile(
    r"featureID\s*=?\s*\\?[\[{(]?\s*(?P<fid>[0-9A-Fa-f]{6,}[\-0-9A-Fa-f]*)\s*\\?[\]})]?\s*"
    r"parentID\s*=?\s*(?P<pids>[^\]\n]+)",
    re.IGNORECASE,
)

# PRQ reference:  AU3GM-PRQ-xxxxx
_PRQ_REF_RE = re.compile(r"AU3GM-PRQ-\d+", re.IGNORECASE)

# Feature GUID reference
_GUID_RE = re.compile(
    r"\{?\s*([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\s*\}?",
)

# Section heading: level + number + title
_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<secnum>\d+(?:\.\d+)*)\s+(?P<title>.+)$",
    re.MULTILINE,
)

# Any heading (numbered or not)
_ANY_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<title>.+)$",
    re.MULTILINE,
)

# Stereotypes
_STEREOTYPE_RE = re.compile(
    r"[«<\*_`]+\s*(?P<stereo>design_decision|information|context)\s*[»>\*_`]+",
    re.IGNORECASE,
)

# Table row  (pipe-delimited)
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)

# Rationale section
_RATIONALE_RE = re.compile(
    r"(?:\*\*)?Rationale(?:\*\*)?[:\s]*\n?(.*?)(?=\n\n|\n(?:#{1,6}\s)|\n---|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Detection patterns (document-agnostic keywords/structures)
# ---------------------------------------------------------------------------

# Function specification: "Specification for <name>" heading
_FUNC_SPEC_RE = re.compile(
    r"Specification\s+(?:for|of)\s+[`'\"]*(?P<fname>[A-Z]\w+)[`'\"]*",
    re.IGNORECASE,
)

# Numbered heading with a function-like name: "4.3.1 Port_Init"
_NUMBERED_FUNC_HEADING_RE = re.compile(
    r"(?:\d+\.)+\d+\s+(?P<fname>[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)"
)


def _has_func_spec_indicators(body_lower: str) -> bool:
    """Return True if lowered text has the indicators of a function spec table."""
    has_syntax = "syntax" in body_lower
    has_func_fields = (
        "service id" in body_lower
        or "parameters (in)" in body_lower
        or "reentrancy" in body_lower
        or "sync/async" in body_lower
    )
    return has_syntax and has_func_fields

# Derived Configuration Parameter heading
_DERIVED_PARAM_RE = re.compile(
    r"Derived\s+Configuration\s+Parameter\s*[-–—:]\s*[`*]*(?P<pname>\w+)",
    re.IGNORECASE,
)

# Type Definition heading
_TYPE_DEF_RE = re.compile(
    r"Type\s+Definition\s+[`*]*(?P<tname>\w+)[`*]*",
    re.IGNORECASE,
)

# Global Variable heading
_GLOBAL_VAR_RE = re.compile(
    r"Global\s+Variable:?\s*[`*]*(?P<vname>[A-Z]\w*(?:<[^>]+>)*(?:\w|<[^>]+>)*)[`*]*",
    re.IGNORECASE,
)

# Exclusive Area / Critical Section heading
_EXCLUSIVE_AREA_RE = re.compile(
    r"(?:Specification\s+of\s+)?Exclusive\s+Area\s*[-–—:]\s*[`*]*(?P<eaname>\w+(?:\\?<[^>]+>)*)[`*]*",
    re.IGNORECASE,
)

# Configuration Macros / Code gen macro heading
_CODEGEN_MACRO_RE = re.compile(
    r"Configuration\s+Macros?\s*[-–—:]\s*[`*]*(?P<mname>\w+)[`*]*",
    re.IGNORECASE,
)

# Memory Section Name table header
_MEM_SECTION_TABLE_RE = re.compile(
    r"Memory\s+Section\s+Name",
    re.IGNORECASE,
)

# Design decision chapter heading
_DESIGN_DECISION_HEADING_RE = re.compile(
    r"^#{1,3}\s+(?:\d+(?:\.\d+)*\s+)?(?P<title>[A-Z]\w+:\s+.+)$",
    re.MULTILINE,
)

# Config structure variable heading pattern
_CONFIG_STRUCT_RE = re.compile(
    r"Configuration\s+(?:structure\s+)?variables?\s*[-–—:]?\s*[`*]*(?P<sname>[A-Z]\w*(?:<[^>]+>)*(?:\w|<[^>]+>)*)[`*]*",
    re.IGNORECASE,
)

# Page-break heading (artifact from PDF→MD conversion, e.g. "## Pages 198-199")
_PAGE_BREAK_RE = re.compile(
    r"^Pages?\s+\d+(?:\s*[-–—]\s*\d+)?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_prq_references(text: str) -> List[str]:
    """Extract all AU3GM-PRQ-xxxxx references from text."""
    return sorted(set(_PRQ_REF_RE.findall(text)))


def _extract_guid_references(text: str) -> List[str]:
    """Extract all GUID references from text."""
    return sorted(set(_GUID_RE.findall(text)))


def _clean_text(text: str) -> str:
    """Remove markdown formatting artefacts, collapse whitespace.

    Preserves underscores that appear *inside* identifiers
    (e.g. ``ADC_SEC_CODE_ASIL_D``) while stripping those used for
    markdown italic formatting (word-boundary underscores).
    """
    text = re.sub(r"[`*]", "", text)
    # Strip markdown-italic underscores (at word boundaries) but keep
    # internal underscores that are part of C identifiers.
    text = re.sub(r"(?<!\w)_|_(?!\w)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_table_cell(cell: str) -> str:
    """Strip a table cell value, preserving internal underscores."""
    cell = re.sub(r"[`*]", "", cell)
    cell = re.sub(r"(?<!\w)_|_(?!\w)", "", cell)
    return cell.strip().strip("|").strip()


def _extract_req_tag(text: str) -> Optional[Tuple[str, str]]:
    """Extract (featureID, parentIDs_raw) from [req] tag in text."""
    m = _REQ_TAG_RE.search(text)
    if not m:
        m = _REQ_TAG_ALT_RE.search(text)
    if m:
        return m.group("fid"), m.group("pids")
    return None


def _parse_spec_table(body: str) -> dict:
    """
    Parse a spec table (key-value pairs in pipe-delimited rows)
    into a dictionary.

    Handles multiple formats:
      - 2-column:  | Key: | Value |
      - 4-column:  | Key: | Value | Key: | Value |
      - Bold keys: | **Key:** | value |
    """
    spec: dict = {}
    rows = _TABLE_ROW_RE.findall(body)

    for row_text in rows:
        cells = [_strip_table_cell(c) for c in row_text.split("|")]
        cells = [c for c in cells if c]

        # Skip separator rows
        if all(re.match(r"^[-:]+$", c) for c in cells):
            continue

        i = 0
        while i < len(cells):
            cell = cells[i]
            key, value = _extract_key_value(cell, cells[i + 1] if i + 1 < len(cells) else "")
            if key:
                if key in spec and spec[key]:
                    spec[key] = spec[key] + " " + value
                else:
                    spec[key] = value
                i += 2
            else:
                i += 1

    return spec


def _extract_key_value(cell: str, next_cell: str) -> Tuple[Optional[str], str]:
    """Extract key-value pair from table cells."""
    clean = re.sub(r"\*\*", "", cell).strip()
    if clean.endswith(":"):
        key = clean[:-1].strip().lower()
        return key, next_cell
    kv_match = re.match(r"^(.+?):\s+(.+)$", clean)
    if kv_match:
        key = kv_match.group(1).strip().lower()
        value = kv_match.group(2).strip()
        return key, value
    return None, ""


def _split_into_blocks(content: str) -> List[dict]:
    """
    Split markdown content into blocks delimited by headings and
    horizontal rules.  Each block has: level, title, body, full_text.

    Page-break headings (``## Pages X-Y``) inserted by the PDF→MD
    converter are **merged** into the preceding block so that they
    do not break logical section boundaries used by downstream parsers.
    """
    matches = list(_ANY_HEADING_RE.finditer(content))
    if not matches:
        return [{"level": 0, "title": "", "body": content, "full_text": content}]

    # 1. Build raw block list from heading matches
    raw_blocks: List[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        raw_blocks.append({
            "level": len(m.group("hashes")),
            "title": m.group("title").strip(),
            "body": body,
            "full_text": m.group(0).strip() + "\n" + body,
        })

    # 2. Merge page-break blocks (e.g. "## Pages 198-199") into the
    #    previous block – they are conversion artifacts, not real sections.
    blocks: List[dict] = []
    for blk in raw_blocks:
        if _PAGE_BREAK_RE.match(blk["title"]) and blocks:
            prev = blocks[-1]
            prev["body"] += "\n" + blk["body"]
            prev["full_text"] += "\n" + blk["body"]
        else:
            blocks.append(blk)

    return blocks


def _extract_name_from_req_tag(text: str) -> Optional[str]:
    """
    Extract an element name from text that contains [req ...] tags.
    Looks for the name between the closing ] and [/req], or after
    removing [req]/[/req] wrappers.
    """
    # Strategy 1: grab the name between ] and [/req]
    inner = re.search(
        r"(?:\\?\]|\])\s*"
        r"([A-Za-z]\w+)"
        r"\s*(?:\\?\[/?req)",
        text,
    )
    if inner:
        return inner.group(1)

    # Strategy 2: remove [req]/[/req] tags and take what remains
    clean = re.sub(r"\\\[/?req[^\]]*?\\?\]", "", text)
    clean = re.sub(r"\[/?req[^\]]*\]", "", clean)
    clean = clean.strip().lstrip(".\\ ")

    # Detect doubled name: "NameName" → "Name"
    if clean and len(clean) % 2 == 0:
        half = len(clean) // 2
        if clean[:half] == clean[half:]:
            return clean[:half]

    parts = clean.split()
    if parts:
        return parts[-1].lstrip(".\\ ")
    return None


def _truncate(text: Optional[str], max_len: int = 2000) -> Optional[str]:
    """Truncate text to max_len characters."""
    if not text:
        return text
    text = _clean_text(text)
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


# ---------------------------------------------------------------------------
# Parser: Design Decisions  (SWUD_DesignDecision)
# ---------------------------------------------------------------------------

def parse_design_decisions(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_DesignDecision nodes from markdown content.

    Detection: standalone sections with [req] tags that are NOT inside
    a function spec, type def, config param, or other structured block.
    The presence of «design_decision» stereotype is a strong signal.
    Also detects design-decision-style headings followed by descriptive
    prose with [req] tags.
    """
    blocks = _split_into_blocks(content)
    decisions: List[dict] = []
    seen_fids: set = set()

    for blk in blocks:
        full = blk["full_text"]

        # Skip blocks that belong to other parser domains
        if _FUNC_SPEC_RE.search(blk["title"]):
            continue
        # Also skip function blocks detected via body or numbered heading
        if _FUNC_SPEC_RE.search(blk["body"]):
            body_lower = blk["body"].lower()
            if _has_func_spec_indicators(body_lower):
                continue
        if _NUMBERED_FUNC_HEADING_RE.search(blk["title"]):
            body_lower = blk["body"].lower()
            if _has_func_spec_indicators(body_lower):
                continue
        if _DERIVED_PARAM_RE.search(blk["title"]):
            continue
        if _TYPE_DEF_RE.search(blk["title"]):
            continue
        if _GLOBAL_VAR_RE.search(blk["title"]):
            continue
        if _EXCLUSIVE_AREA_RE.search(blk["title"]):
            continue
        if _CODEGEN_MACRO_RE.search(blk["title"]):
            continue

        # Must have a [req] tag
        tag = _extract_req_tag(full)
        if not tag:
            continue

        fid, raw_pids = tag

        # Detect stereotype — design_decision is the primary signal
        stereo_match = _STEREOTYPE_RE.search(full)
        stereotype = stereo_match.group("stereo").lower() if stereo_match else None

        # Without a stereotype, require that body looks like a decision
        # (prose text, not mainly a spec table)
        if not stereotype:
            # If the body is dominated by table rows → likely a config/func spec
            table_lines = len(_TABLE_ROW_RE.findall(blk["body"]))
            total_lines = max(1, len(blk["body"].split("\n")))
            if table_lines > total_lines * 0.4:
                continue

            # Must have at least some descriptive prose
            prose_len = len(re.sub(r"\|[^\n]+\|", "", blk["body"]).strip())
            if prose_len < 50:
                continue

        if fid in seen_fids:
            continue
        seen_fids.add(fid)

        prqs = _extract_prq_references(raw_pids)
        guids = _extract_guid_references(raw_pids)

        # Extract rationale
        rationale_match = _RATIONALE_RE.search(blk["body"])
        rationale = _truncate(rationale_match.group(1)) if rationale_match else None

        # Clean title — strip markdown formatting and page numbers
        title = re.sub(r"\s+\d+$", "", blk["title"]).strip()
        title = _clean_text(title)

        # Build description
        desc = _truncate(blk["body"])

        decisions.append({
            "decision_id": fid,
            "title": title,
            "decision_type": stereotype or "design_decision",
            "description": desc if desc else None,
            "rationale": rationale,
            "prq_references": prqs if prqs else None,
            "feature_id_references": guids if guids else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return decisions


# ---------------------------------------------------------------------------
# Parser: Derived Configuration Parameters  (SWUD_DerivedConfigParam)
# ---------------------------------------------------------------------------

def parse_derived_config_params(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_DerivedConfigParam nodes from markdown content.

    Detection: "Derived Configuration Parameter" headings followed by
    spec tables with Name/Type/File/Description/Range/Algorithm fields.
    The Type field is typically ``#define``.
    """
    blocks = _split_into_blocks(content)
    params: List[dict] = []
    seen_names: set = set()

    for i, blk in enumerate(blocks):
        match = _DERIVED_PARAM_RE.search(blk["title"])
        if not match:
            # Also detect by spec table content: Type = #define
            if "#define" not in blk["body"]:
                continue
            match = _DERIVED_PARAM_RE.search(blk["body"])
            if not match:
                continue

        # Collect body including child blocks
        body = blk["body"]
        for j in range(i + 1, min(i + 5, len(blocks))):
            if blocks[j]["level"] <= blk["level"] and blocks[j]["level"] > 0:
                break
            body += "\n" + blocks[j]["body"]

        spec = _parse_spec_table(body)

        # Extract name
        raw_name = spec.get("name", "")
        param_name = match.group("pname") if match else None

        # Try extracting from [req] tag in the name field
        if raw_name:
            extracted = _extract_name_from_req_tag(raw_name)
            if extracted:
                param_name = extracted

        if not param_name or param_name in seen_names:
            continue
        # Reject bad entries: partial [req] tags, page numbers, etc.
        if param_name.startswith("parentID") or re.match(r"^\d+$", param_name):
            continue
        seen_names.add(param_name)

        # Extract [req] tag from full text
        tag = _extract_req_tag(blk["full_text"] + "\n" + body)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        params.append({
            "param_name": param_name,
            "param_type": spec.get("type", "#define"),
            "file": spec.get("file"),
            "description": _truncate(spec.get("description")),
            "range": spec.get("range"),
            "algorithm": _truncate(spec.get("algorithm"), 3000),
            "design_decisions": spec.get("design decisions"),
            "dependencies": spec.get("dependencies") or spec.get("dependency"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return params


# ---------------------------------------------------------------------------
# Parser: Configuration Structure Variables  (SWUD_ConfigStructure)
# ---------------------------------------------------------------------------

def parse_config_structures(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_ConfigStructure nodes from markdown content.

    Detection: Global Variable spec blocks for configuration structures
    (typically array/struct types with const qualifier, used in
    configuration data). Distinguished from runtime data variables by
    the presence of 'k' prefix (const) or CONFIG in memory section.
    Handles both heading (``### Global Variable:``) and bold inline
    (``**Global Variable:**``) formats.
    """
    structures: List[dict] = []
    seen_names: set = set()

    # Pattern that matches BOTH heading and bold-inline formats
    _GV_RE = re.compile(
        r"(?:^#{1,6}\s+|\*\*)"
        r"(?:Global\s+Variable:?\s*(?:\*\*)?\s*|Configuration\s+(?:structure\s+)?variables?\s*[-–—:]?\s*)"
        r"[`*]*(?P<vname>[A-Za-z]\w*(?:\\?<[^>]+>)*(?:\w|\\?<[^>]+>)*)[`*]*",
        re.IGNORECASE | re.MULTILINE,
    )

    for m in _GV_RE.finditer(content):
        var_name = m.group("vname").replace("\\", "")
        if not var_name:
            continue

        # Grab body
        start = m.end()
        next_m = _GV_RE.search(content, start)
        next_heading = _ANY_HEADING_RE.search(content, start)
        end = len(content)
        if next_m:
            end = min(end, next_m.start())
        if next_heading:
            end = min(end, next_heading.start())
        body = content[start:end].strip()

        spec = _parse_spec_table(body)

        # Only keep config structures (const pattern)
        mem_section = spec.get("memory section", "")
        var_type = spec.get("type", "")
        is_config = (
            "_k" in var_name or
            var_name.startswith("k") or
            "CONFIG" in mem_section.upper() or
            "POSTBUILD" in mem_section.upper() or
            "const" in var_type.lower()
        )
        if not is_config:
            continue

        if var_name in seen_names:
            continue
        seen_names.add(var_name)

        full_text = m.group(0) + "\n" + body
        tag = _extract_req_tag(full_text)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        structures.append({
            "structure_name": var_name,
            "c_type": var_type if var_type else None,
            "file": spec.get("file"),
            "memory_section": mem_section if mem_section else None,
            "description": _truncate(spec.get("description")),
            "range": spec.get("range"),
            "algorithm": _truncate(spec.get("algorithm"), 3000),
            "design_decisions": spec.get("design decisions"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return structures


# ---------------------------------------------------------------------------
# Parser: Code Generation Macros  (SWUD_CodeGenMacro)
# ---------------------------------------------------------------------------

def parse_codegen_macros(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_CodeGenMacro nodes from markdown content.

    Detection: "Configuration Macros" headings or spec tables where
    the Type field contains "Code generation x-path macro" or similar.
    """
    blocks = _split_into_blocks(content)
    macros: List[dict] = []
    seen_names: set = set()

    for i, blk in enumerate(blocks):
        match = _CODEGEN_MACRO_RE.search(blk["title"])
        is_codegen_body = "code generation" in blk["body"].lower() and "x-path" in blk["body"].lower()

        if not match and not is_codegen_body:
            continue

        # Collect body including child blocks
        body = blk["body"]
        for j in range(i + 1, min(i + 8, len(blocks))):
            if blocks[j]["level"] <= blk["level"] and blocks[j]["level"] > 0:
                break
            body += "\n" + blocks[j]["body"]

        spec = _parse_spec_table(body)

        # Verify it's actually a code gen macro
        macro_type = spec.get("type", "")
        if not match and "code generation" not in macro_type.lower():
            continue

        macro_name = match.group("mname") if match else None
        if not macro_name:
            raw_name = spec.get("name", "")
            macro_name = _extract_name_from_req_tag(raw_name)
        if not macro_name:
            continue

        if macro_name in seen_names:
            continue
        seen_names.add(macro_name)

        tag = _extract_req_tag(blk["full_text"] + "\n" + body)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        # Extract parameters
        params_in = _extract_param_list(body, "Parameters (in)")
        params_out = _extract_param_list(body, "Parameters (out)")

        macros.append({
            "macro_name": macro_name,
            "macro_type": macro_type if macro_type else "Code generation x-path macro",
            "file": spec.get("file"),
            "parameters_in": params_in if params_in else None,
            "parameters_out": params_out if params_out else None,
            "description": _truncate(spec.get("description")),
            "algorithm": _truncate(spec.get("algorithm"), 3000),
            "design_decisions": spec.get("design decisions"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return macros


# ---------------------------------------------------------------------------
# Parser: Type Definitions  (SWUD_TypeDefinition)
# ---------------------------------------------------------------------------

def parse_type_definitions(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_TypeDefinition nodes from markdown content.

    Detection: "Type Definition <name>" headings, or blocks with
    Syntax/Type/File/Range structure that define C types (struct/
    enum/typedef/uint8/uint16/uint32/etc.).
    """
    blocks = _split_into_blocks(content)
    type_defs: List[dict] = []
    seen_names: set = set()

    for i, blk in enumerate(blocks):
        # Primary: "Type Definition <name>" heading
        td_match = _TYPE_DEF_RE.search(blk["title"])

        # Secondary: backtick-wrapped type name in heading + Syntax: in body
        if not td_match:
            name_in_title = re.search(r"`(\w+Type)`", blk["title"])
            if name_in_title and ("Syntax:" in blk["body"] or "Type:" in blk["body"]):
                td_match = name_in_title

        if not td_match:
            continue

        type_name = td_match.group(1) if hasattr(td_match, 'group') else None
        if not type_name:
            type_name = td_match.group("tname") if "tname" in (td_match.groupdict() or {}) else None
        if not type_name:
            continue

        # Reject bad entries: pure numbers (page numbers from TOC)
        if re.match(r"^\d+$", type_name):
            continue

        # Collect body including child blocks (Range tables can span)
        body = blk["body"]
        for j in range(i + 1, min(i + 10, len(blocks))):
            child = blocks[j]
            if child["level"] <= blk["level"] and child["level"] > 0:
                break
            # Stop at next Type Definition
            if _TYPE_DEF_RE.search(child["title"]):
                break
            body += "\n" + child["full_text"]

        spec = _parse_spec_table(body)

        if type_name in seen_names:
            continue
        seen_names.add(type_name)

        tag = _extract_req_tag(blk["full_text"] + "\n" + body)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        # Extract C type (uint8, uint16, uint32, struct, enum, etc.)
        c_type = spec.get("type")
        if not c_type:
            type_match = re.search(r"\*\*Type:\*\*\s*`?(\w+)`?", body)
            if type_match:
                c_type = type_match.group(1)

        # Extract file
        file_val = spec.get("file")
        if not file_val:
            file_match = re.search(r"\*\*File:\*\*\s*`?(\w+\.\w+)`?", body)
            if file_match:
                file_val = file_match.group(1)

        # Extract description
        desc = spec.get("description")
        if not desc:
            desc_match = re.search(
                r"(?:\*\*)?Description(?:\*\*)?[:\s]*\n(.*?)(?=\n##|\n---|\n\*\*\w+:\*\*|\Z)",
                body, re.IGNORECASE | re.DOTALL,
            )
            if desc_match:
                desc = desc_match.group(1).strip()

        type_defs.append({
            "type_name": type_name,
            "c_type": c_type,
            "file": file_val,
            "description": _truncate(desc),
            "range": _truncate(spec.get("range"), 1000),
            "design_decisions": spec.get("design decisions"),
            "source": spec.get("source"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return type_defs


# ---------------------------------------------------------------------------
# Parser: Functions  (SWUD_Function)
# ---------------------------------------------------------------------------

def parse_functions(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_Function nodes from markdown content.

    Detection: "Specification for <FuncName>" headings, or blocks
    with the combination of Syntax + (Service ID | Parameters | Return)
    fields.  Each function has a spec table with fields like:
    Syntax, Service ID, ASIL Level, Sync/Async, Reentrancy,
    Parameters (in/out), Return, Description, Algorithm, Error Handling,
    Configuration Dependencies, File, Memory Section.
    """
    blocks = _split_into_blocks(content)
    functions: List[dict] = []
    seen_names: set = set()

    for i, blk in enumerate(blocks):
        # Detection 1: "Specification for <name>" heading
        spec_match = _FUNC_SPEC_RE.search(blk["title"])

        # Detection 2: Function name in backticks in heading + Syntax in body
        func_name_match = None
        if not spec_match:
            func_name_match = re.search(r"`([A-Z]\w+)`", blk["title"])
            if func_name_match:
                body_lower = blk["body"].lower()
                if not _has_func_spec_indicators(body_lower):
                    func_name_match = None

        # Detection 3: "Specification for <name>" in body text (bold
        # table captions, plain text, etc.) — handles PORT-style docs
        # where the heading is "4.3.1 Port_Init" and the spec label
        # sits in the body as  **Table N: Specification for Port_Init**
        body_spec_match = None
        if not spec_match and not func_name_match:
            body_spec_match = _FUNC_SPEC_RE.search(blk["body"])
            if body_spec_match:
                body_lower = blk["body"].lower()
                if not _has_func_spec_indicators(body_lower):
                    body_spec_match = None

        # Detection 4: Numbered heading like "4.3.1 Port_Init" with
        # function-spec indicators in body — most general fallback.
        # The regex requires an underscore so it won't match prose
        # headings like "4.1 Overview".
        heading_name_match = None
        if not spec_match and not func_name_match and not body_spec_match:
            heading_name_match = _NUMBERED_FUNC_HEADING_RE.search(blk["title"])
            if heading_name_match:
                body_lower = blk["body"].lower()
                if not _has_func_spec_indicators(body_lower):
                    heading_name_match = None

        if not spec_match and not func_name_match \
                and not body_spec_match and not heading_name_match:
            continue

        func_name = None
        if spec_match:
            func_name = spec_match.group("fname")
        elif body_spec_match:
            func_name = body_spec_match.group("fname")
        elif func_name_match:
            func_name = func_name_match.group(1)
        elif heading_name_match:
            func_name = heading_name_match.group("fname")

        if not func_name:
            continue

        # Skip if this looks like a Type Definition
        if func_name.endswith("Type"):
            continue

        # Collect body including child blocks (specs can span many sub-headings)
        body = blk["body"]
        for j in range(i + 1, min(i + 20, len(blocks))):
            child = blocks[j]
            child_body_lower = child["body"].lower()

            # Always stop at next function spec (any detection method),
            # regardless of heading level — handles inconsistent heading
            # levels from PDF→markdown conversion.
            if _FUNC_SPEC_RE.search(child["title"]):
                break
            if _FUNC_SPEC_RE.search(child["body"]) \
                    and _has_func_spec_indicators(child_body_lower):
                break
            if re.search(r"`[A-Z]\w+`", child["title"]):
                if "syntax:" in child_body_lower or "service id:" in child_body_lower:
                    break
            if _NUMBERED_FUNC_HEADING_RE.search(child["title"]):
                if _has_func_spec_indicators(child_body_lower) \
                        or "specification for" in child_body_lower:
                    break

            # Stop at same/higher level heading (sibling or parent)
            if child["level"] <= blk["level"] and child["level"] > 0:
                break

            body += "\n" + child["full_text"]

        if func_name in seen_names:
            continue
        seen_names.add(func_name)

        spec = _parse_spec_table(body)

        # Extract [req] tag
        tag = _extract_req_tag(blk["full_text"] + "\n" + body)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])
        # Also scan algorithm field for PRQ references
        algo_prqs = _extract_prq_references(spec.get("algorithm", ""))
        if algo_prqs:
            prqs = sorted(set(prqs + algo_prqs))

        # Classify function type from context
        func_type = _classify_function_type(func_name, body, spec)

        # Extract parameters
        params_in = _extract_param_list(body, "Parameters (in)")
        params_out = _extract_param_list(body, "Parameters (out)")

        # Extract service ID
        service_id = spec.get("service id")
        if not service_id:
            sid_match = re.search(r"Service\s+ID:?\s*[`]*([0-9x][0-9a-fA-Fx]*)", body, re.IGNORECASE)
            if sid_match:
                service_id = sid_match.group(1)

        # Extract ASIL level
        asil = spec.get("asil level") or spec.get("asil")
        if not asil:
            asil_match = re.search(r"ASIL\s+(?:Level:?\s*)?([A-D](?:\([A-D]\))?)", body, re.IGNORECASE)
            if asil_match:
                asil = asil_match.group(1)

        # Extract return type
        return_type = spec.get("return")
        if not return_type:
            ret_match = re.search(r"(?:\*\*)?Return(?:\*\*)?[:\s]*`?(\w+)`?", body, re.IGNORECASE)
            if ret_match:
                return_type = ret_match.group(1)

        # Extract memory section
        mem_section = spec.get("memory section")
        if not mem_section:
            mem_match = re.search(r"Memory\s+Section[:\s]*`?([A-Z_]+)`?", body, re.IGNORECASE)
            if mem_match:
                mem_section = mem_match.group(1)

        # Extract file
        file_val = spec.get("file")
        if not file_val:
            file_match = re.search(r"(?:\*\*)?File(?:\*\*)?[:\s]*`?(\w+\.\w+)`?", body, re.IGNORECASE)
            if file_match:
                file_val = file_match.group(1)

        # Extract error handling
        error_handling = spec.get("error handling")

        # Extract config dependencies
        config_deps = spec.get("configuration dependencies")

        functions.append({
            "function_name": func_name,
            "function_category": func_type,
            "syntax": _truncate(spec.get("syntax"), 500),
            "service_id": service_id,
            "asil_level": asil,
            "sync_async": spec.get("sync/async"),
            "reentrancy": spec.get("reentrancy"),
            "parameters_in": params_in if params_in else None,
            "parameters_out": params_out if params_out else None,
            "return_type": return_type,
            "description": _truncate(spec.get("description")),
            "algorithm": _truncate(spec.get("algorithm"), 3000),
            "error_handling": _truncate(error_handling),
            "configuration_dependencies": config_deps,
            "file": file_val,
            "memory_section": mem_section,
            "design_decisions": spec.get("design decisions"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return functions


def _classify_function_type(name: str, body: str, spec: dict) -> str:
    """
    Infer function category from name patterns/context:
      api, callback, notification, scheduled, interrupt, local
    """
    name_lower = name.lower()
    body_lower = body.lower()

    # Local functions: internal prefix patterns (module-specific _I prefix)
    if re.match(r"^[A-Z]\w+_I[A-Z]", name):
        return "local"
    if "_l" in name_lower or "_i" in name_lower:
        # e.g. Adc_lReportError, Adc_IGetPartitionIndex
        if re.search(r"_[lI][A-Z]", name):
            return "local"

    # Interrupt handlers
    if "interrupt" in body_lower or "EventHandler" in name or "IrqHandler" in name:
        return "interrupt"

    # Callbacks / notifications
    if "callback" in body_lower or "notification" in body_lower:
        if "Handler" in name or "Notification" in name or "Callback" in name:
            return "callback"

    # Scheduled functions
    if "scheduled" in body_lower or "MainFunction" in name:
        return "scheduled"

    # Macros (SFR_WRITE, etc.)
    if name.isupper() or name.startswith("ADC_SFR_") or name.startswith("SFR_"):
        return "macro"

    # Default: API function
    return "api"


def _extract_param_list(body: str, section_name: str) -> Optional[List[str]]:
    """
    Extract parameter names from a Parameters section.
    Handles both table format and list format.
    """
    # Find the section
    pattern = re.compile(
        rf"(?:\*\*)?{re.escape(section_name)}(?:\*\*)?[:\s]*\n(.*?)(?=\n(?:###?\s|Parameters\s*\(|Return|\*\*[A-Z])|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return None

    section_text = match.group(1).strip()

    # Check for "none" / empty
    if re.match(r"^\s*\*?\(?\s*none\s*\)?\*?\s*$", section_text, re.IGNORECASE):
        return None

    # Extract from table rows
    params = []
    for row in _TABLE_ROW_RE.findall(section_text):
        cells = [_strip_table_cell(c) for c in row.split("|")]
        cells = [c for c in cells if c and not re.match(r"^[-:]+$", c)]
        if cells:
            # Find the cell that looks like a parameter name
            for cell in cells:
                clean = re.sub(r"[`*]", "", cell).strip()
                if re.match(r"^[A-Z]\w+$", clean) or re.match(r"^[a-z]\w+$", clean):
                    params.append(clean)
                    break

    return params if params else None


# ---------------------------------------------------------------------------
# Parser: Data Variables  (SWUD_DataVariable)
# ---------------------------------------------------------------------------

def parse_data_variables(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_DataVariable nodes from markdown content.

    Detection: "Global Variable:" patterns — either as markdown headings
    (``### Global Variable: `Name```) or as bold inline text
    (``**Global Variable:** `Name```).  Distinguished from config
    structures by the absence of 'k' prefix / CONFIG / POSTBUILD
    memory sections.
    """
    variables: List[dict] = []
    seen_names: set = set()

    # Pattern that matches BOTH heading and bold-inline formats:
    #   ### Global Variable: `Name`       (heading)
    #   **Global Variable:** `Name`       (inline bold)
    _GV_INLINE_RE = re.compile(
        r"(?:^#{1,6}\s+|\*\*)"
        r"Global\s+Variable:?\s*"
        r"(?:\*\*)?\s*"
        r"[`*]*(?P<vname>[A-Za-z]\w*(?:\\?<[^>]+>)*(?:\w|\\?<[^>]+>)*)[`*]*",
        re.IGNORECASE | re.MULTILINE,
    )

    for m in _GV_INLINE_RE.finditer(content):
        var_name = m.group("vname").replace("\\", "")
        if not var_name:
            continue

        # Grab the body after this match until next heading or bold pattern
        start = m.end()
        next_m = _GV_INLINE_RE.search(content, start)
        next_heading = _ANY_HEADING_RE.search(content, start)
        end = len(content)
        if next_m:
            end = min(end, next_m.start())
        if next_heading:
            end = min(end, next_heading.start())
        body = content[start:end].strip()

        spec = _parse_spec_table(body)

        # Distinguish runtime data vars from config structures:
        mem_section = spec.get("memory section", "")
        is_config = (
            "_k" in var_name or
            var_name.startswith("k") or
            "CONFIG" in mem_section.upper() or
            "POSTBUILD" in mem_section.upper() or
            "const" in spec.get("type", "").lower()
        )
        if is_config:
            continue  # handled by parse_config_structures

        if var_name in seen_names:
            continue
        seen_names.add(var_name)

        full_text = m.group(0) + "\n" + body
        tag = _extract_req_tag(full_text)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        variables.append({
            "variable_name": var_name,
            "c_type": spec.get("type"),
            "file": spec.get("file"),
            "memory_section": mem_section if mem_section else None,
            "description": _truncate(spec.get("description")),
            "range": spec.get("range"),
            "algorithm": _truncate(spec.get("algorithm"), 3000),
            "design_decisions": spec.get("design decisions"),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return variables


# ---------------------------------------------------------------------------
# Parser: Critical Sections  (SWUD_CriticalSection)
# ---------------------------------------------------------------------------

def parse_critical_sections(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_CriticalSection nodes from markdown content.

    Detection: "Exclusive Area" headings or "Critical section" headings
    with spec tables containing API Name, Resource Name, ISR Name,
    Comment fields.
    """
    blocks = _split_into_blocks(content)
    sections: List[dict] = []
    seen_names: set = set()

    for i, blk in enumerate(blocks):
        ea_match = _EXCLUSIVE_AREA_RE.search(blk["title"])
        if not ea_match:
            # Also check body for "Exclusive Area" pattern
            ea_match = _EXCLUSIVE_AREA_RE.search(blk["body"])
        if not ea_match:
            continue

        cs_name = ea_match.group("eaname").replace("\\", "")
        if not cs_name or cs_name in seen_names:
            continue
        seen_names.add(cs_name)

        # Collect body including child blocks
        body = blk["body"]
        for j in range(i + 1, min(i + 5, len(blocks))):
            if blocks[j]["level"] <= blk["level"] and blocks[j]["level"] > 0:
                break
            body += "\n" + blocks[j]["body"]

        spec = _parse_spec_table(body)

        tag = _extract_req_tag(blk["full_text"] + "\n" + body)
        prqs = []
        fid = None
        if tag:
            fid = tag[0]
            prqs = _extract_prq_references(tag[1])

        api_names = spec.get("api name", "")
        # Split comma/newline-separated API names
        api_list = [a.strip() for a in re.split(r"[,\n]", api_names) if a.strip()] if api_names else []

        sections.append({
            "critical_section_name": cs_name,
            "using_functions": api_list if api_list else None,
            "protected_resources": spec.get("resource name"),
            "isr_name": spec.get("isr name"),
            "description": _truncate(spec.get("comment")),
            "feature_id": fid,
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        })

    return sections


# ---------------------------------------------------------------------------
# Parser: Memory Sections  (SWUD_MemorySection)
# ---------------------------------------------------------------------------

def parse_memory_sections(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWUD_MemorySection nodes from markdown content.

    Detection: Tables with "Memory Section Name" column header.
    Each row gives a section name (often inside [req] tags) and
    its description.  Handles page breaks (``## Pages X-Y``)
    within the table.
    """
    sections: List[dict] = []
    seen_names: set = set()

    # Collapse page-break markers so the table is contiguous
    cleaned = re.sub(
        r"\n+---\n+##\s+Pages?\s+\d+[-–]\d+\n+\|\s*\|\s*\|\n+\|[-:|\s]+\|\n*",
        "\n",
        content,
    )

    # Find all table blocks that have "Memory Section Name" header
    lines = cleaned.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if _MEM_SECTION_TABLE_RE.search(line) and "|" in line:
            # Skip separator row
            i += 1
            if i < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i].strip()):
                i += 1

            # Accumulate all raw data rows (first pass)
            raw_rows: List[str] = []
            while i < len(lines):
                row = lines[i].strip()
                if not row.startswith("|"):
                    # Allow blank lines within the table
                    if not row and i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
                        i += 1
                        continue
                    break
                if re.match(r"^\|[\s\-:|]+\|$", row):
                    i += 1
                    continue
                raw_rows.append(row)
                i += 1

            # Parse rows — extract memory section names from [req] tags
            # Some names span continuation rows
            for row in raw_rows:
                cells = [c.strip() for c in row.split("|")]
                cells = [c for c in cells if c is not None]

                if len(cells) < 2:
                    continue

                first_cell = cells[0]
                second_cell = cells[1] if len(cells) > 1 else ""

                # Skip empty first cell (continuation of previous row)
                if not first_cell.strip():
                    continue

                # Extract memory section name patterns:
                #   ADC_SEC_*, *_SEC_*, SEC_*
                sec_names = re.findall(
                    r"[A-Z][A-Z0-9_]*_SEC_[A-Z0-9_]+",
                    first_cell,
                )

                # Also try extracting from [req] tag
                if not sec_names:
                    name = _extract_name_from_req_tag(first_cell)
                    if name and "_SEC_" in name.upper():
                        sec_names = [name]

                # Also check second cell for overflow
                if not sec_names and second_cell and "_SEC_" in second_cell.upper():
                    sec_names = re.findall(
                        r"[A-Z][A-Z0-9_]*_SEC_[A-Z0-9_]+",
                        second_cell,
                    )

                for sec_name in sec_names:
                    # Clean up — remove duplicated name halves
                    if sec_name not in seen_names:
                        seen_names.add(sec_name)

                        prqs = _extract_prq_references(first_cell)

                        sections.append({
                            "section_name": sec_name,
                            "description": _truncate(second_cell) if second_cell else None,
                            "prq_references": prqs if prqs else None,
                            "module": module.upper(),
                            "source_document": source_document,
                        })
        else:
            i += 1

    # Also extract memory section names referenced in function specs
    # (Memory Section: ADC_SEC_*)  — these may not be in the table
    func_mem_secs = re.findall(
        r"Memory\s+Section[:\s]*[`|]*\s*([A-Z][A-Z0-9_]*_SEC_[A-Z0-9_]+)",
        content,
    )
    for sec_name in func_mem_secs:
        if sec_name not in seen_names:
            seen_names.add(sec_name)
            sections.append({
                "section_name": sec_name,
                "description": None,
                "prq_references": None,
                "module": module.upper(),
                "source_document": source_document,
            })

    return sections


# ---------------------------------------------------------------------------
# High-level: parse an entire SWUD directory
# ---------------------------------------------------------------------------

def detect_module_from_files(swud_dir: Path) -> Optional[str]:
    """Auto-detect the MCAL module name from SWUD filenames."""
    for f in swud_dir.iterdir():
        m = re.search(r"SWUD_(\w+)\.", f.name, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def detect_source_document(swud_dir: Path) -> Optional[str]:
    """Auto-detect the source document name from SWUD directory."""
    for f in swud_dir.iterdir():
        if f.suffix in (".pdf", ".docx") and "SWUD" in f.name.upper():
            return f.stem
    for f in swud_dir.iterdir():
        if f.suffix == ".md" and "SWUD" in f.name.upper() and not f.name.startswith("section_"):
            return f.stem
    return None


def parse_swud_directory(
    swud_dir: str | Path,
    module: Optional[str] = None,
    source_document: Optional[str] = None,
) -> Dict[str, List[dict]]:
    """
    Parse all SWUD section markdown files in a directory.

    Auto-detects the module and source document if not provided.
    Returns a dict mapping node type names → list of node property dicts.

    The parser reads BOTH ``section_*_raw.md`` files AND the full
    combined markdown (``*_SWUD_*.md``).  When section files exist,
    only the full markdown is used for content that the section
    splitter may have placed in the full file (e.g. design decisions
    in early sections, data variables in later sections).

    Parameters
    ----------
    swud_dir : path
        Directory containing SWUD markdown files.
    module : str, optional
        MCAL module name (e.g. "ADC"). Auto-detected if omitted.
    source_document : str, optional
        Source document name. Auto-detected if omitted.
    """
    swud_dir = Path(swud_dir)
    if not swud_dir.exists():
        logger.error("SWUD directory does not exist: %s", swud_dir)
        return {}

    # Auto-detect module and source document
    if not module:
        module = detect_module_from_files(swud_dir) or "UNKNOWN"
        logger.info("Auto-detected module: %s", module)
    if not source_document:
        source_document = detect_source_document(swud_dir) or "unknown_swud_document"
        logger.info("Auto-detected source document: %s", source_document)

    # Find markdown files to parse
    # Prefer the full combined markdown (contains all sections)
    full_md_files = sorted(swud_dir.glob("*SWUD*.md"))
    full_md_files = [f for f in full_md_files if not f.name.startswith("section_")]

    section_files = sorted(swud_dir.glob("section_*_raw.md"))

    # Use the full MD for parsing — it has all content in one place
    # Also parse section files for additional content
    files_to_parse = list(full_md_files)
    if section_files:
        # Add section files that might have content not in the full MD
        files_to_parse.extend(section_files)

    if not files_to_parse:
        logger.warning("No SWUD markdown files found in %s", swud_dir)
        return {}

    logger.info("Found %d SWUD file(s) to parse in %s", len(files_to_parse), swud_dir.name)

    # Accumulate nodes from all files
    all_decisions: List[dict] = []
    all_derived_params: List[dict] = []
    all_config_structs: List[dict] = []
    all_codegen_macros: List[dict] = []
    all_type_defs: List[dict] = []
    all_functions: List[dict] = []
    all_data_vars: List[dict] = []
    all_critical_sections: List[dict] = []
    all_memory_sections: List[dict] = []

    for fpath in files_to_parse:
        logger.info("  Parsing %s (%d KB)…", fpath.name, fpath.stat().st_size // 1024)
        content = fpath.read_text(encoding="utf-8")

        # Run all parsers — each uses pattern detection (fully agnostic)
        decisions = parse_design_decisions(content, module, source_document)
        derived_params = parse_derived_config_params(content, module, source_document)
        config_structs = parse_config_structures(content, module, source_document)
        codegen_macros = parse_codegen_macros(content, module, source_document)
        type_defs = parse_type_definitions(content, module, source_document)
        functions = parse_functions(content, module, source_document)
        data_vars = parse_data_variables(content, module, source_document)
        critical_secs = parse_critical_sections(content, module, source_document)
        memory_secs = parse_memory_sections(content, module, source_document)

        if decisions:
            logger.info("    → %d design decisions", len(decisions))
        if derived_params:
            logger.info("    → %d derived config params", len(derived_params))
        if config_structs:
            logger.info("    → %d config structures", len(config_structs))
        if codegen_macros:
            logger.info("    → %d code gen macros", len(codegen_macros))
        if type_defs:
            logger.info("    → %d type definitions", len(type_defs))
        if functions:
            logger.info("    → %d functions", len(functions))
        if data_vars:
            logger.info("    → %d data variables", len(data_vars))
        if critical_secs:
            logger.info("    → %d critical sections", len(critical_secs))
        if memory_secs:
            logger.info("    → %d memory sections", len(memory_secs))

        all_decisions.extend(decisions)
        all_derived_params.extend(derived_params)
        all_config_structs.extend(config_structs)
        all_codegen_macros.extend(codegen_macros)
        all_type_defs.extend(type_defs)
        all_functions.extend(functions)
        all_data_vars.extend(data_vars)
        all_critical_sections.extend(critical_secs)
        all_memory_sections.extend(memory_secs)

    # Deduplicate across files (by unique key)
    all_decisions = _deduplicate(all_decisions, "decision_id")
    all_derived_params = _deduplicate(all_derived_params, "param_name")
    all_config_structs = _deduplicate(all_config_structs, "structure_name")
    all_codegen_macros = _deduplicate(all_codegen_macros, "macro_name")
    all_type_defs = _deduplicate(all_type_defs, "type_name")
    all_functions = _deduplicate(all_functions, "function_name")
    all_data_vars = _deduplicate(all_data_vars, "variable_name")
    all_critical_sections = _deduplicate(all_critical_sections, "critical_section_name")
    all_memory_sections = _deduplicate(all_memory_sections, "section_name")

    result: Dict[str, List[dict]] = {}
    if all_decisions:
        result["SWUD_DesignDecision"] = all_decisions
    if all_derived_params:
        result["SWUD_DerivedConfigParam"] = all_derived_params
    if all_config_structs:
        result["SWUD_ConfigStructure"] = all_config_structs
    if all_codegen_macros:
        result["SWUD_CodeGenMacro"] = all_codegen_macros
    if all_type_defs:
        result["SWUD_TypeDefinition"] = all_type_defs
    if all_functions:
        result["SWUD_Function"] = all_functions
    if all_data_vars:
        result["SWUD_DataVariable"] = all_data_vars
    if all_critical_sections:
        result["SWUD_CriticalSection"] = all_critical_sections
    if all_memory_sections:
        result["SWUD_MemorySection"] = all_memory_sections

    total = sum(len(v) for v in result.values())
    logger.info(
        "SWUD parsing complete: %d nodes across %d types",
        total,
        len(result),
    )

    return result


def _richness(item: dict) -> int:
    """Count non-empty, non-None values — higher = richer content."""
    return sum(
        1 for v in item.values()
        if v is not None and v != "" and v != []
    )


def _deduplicate(items: List[dict], key: str) -> List[dict]:
    """Remove duplicates by unique key, keeping the richest entry.

    When a PDF produces a stub heading (TOC) followed by the real
    content later, the first occurrence may have empty fields.
    Keeping the entry with more non-empty values ensures we preserve
    the real content (feature_id, description, etc.).
    """
    best: dict = {}          # key-value  -> best item so far
    best_score: dict = {}    # key-value  -> richness score
    order: list = []         # preserve insertion order
    for item in items:
        val = item.get(key)
        if not val:
            continue
        score = _richness(item)
        if val not in best:
            best[val] = item
            best_score[val] = score
            order.append(val)
        elif score > best_score[val]:
            best[val] = item
            best_score[val] = score
    return [best[v] for v in order]

"""
SWA (Software Architecture) Markdown Parsers
=============================================

Document-agnostic, section-agnostic parsers that extract structured nodes
from SWA markdown files for ingestion into the Neo4j knowledge graph.

Supported Node Types (from ontology):
    - SWA_ArchitecturalDecision   (any section with [req] tags + stereotypes)
    - SWA_HwPeripheral            (HW-SW interface peripherals)
    - SWA_SwDependency            (SW-SW interface API dependencies)
    - SWA_ConfigContainer         (ECUC configuration containers)
    - SWA_ConfigParam             (ECUC configuration parameters)
    - SWA_Function                (Exported API function specifications)
    - SWA_DataType                (Exported data type specifications)
    - SWA_Macro                   (Exported macro specifications)

Detection is **pattern-based** — the parsers infer content type from
structural markers in the markdown (headings, [req] tags, table layouts,
stereotypes, etc.) rather than relying on hardcoded section numbers.
This ensures the pipeline works for any MCAL module's SWA document
(ADC, SPI, CAN, GPT, …) and for any combination of converted sections.

Usage::

    from swa_parsers import parse_swa_directory

    nodes_by_type = parse_swa_directory(
        swa_dir="path/to/swa/",
        module="ADC",
        source_document="TC4xx_SW_MCAL_SWA_Adc",
    )
    # nodes_by_type == {
    #     "SWA_ArchitecturalDecision": [...],
    #     "SWA_HwPeripheral": [...],
    #     ...
    # }
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("swa_parsers")

# ---------------------------------------------------------------------------
# Regex patterns  (generic — no section-number assumptions)
# ---------------------------------------------------------------------------

# Matches [req featureID={...} parentID=...]...[/req]   (multiple variants)
# Handles various markdown escaping artefacts (\_[, \[, backticks, etc.)
_REQ_TAG_RE = re.compile(
    r"\\?\[_?req\s+featureID\s*=\s*"
    r"\\?[\[{]?(?P<fid>[0-9A-Fa-f\-]+)\\?[\]}]?\s*"
    r"parentID\s*=\s*(?P<pids>[^\]]+?)"
    r"\\?\](?P<title>[^\[]*?)\\?\[/?req\\?\]",
    re.IGNORECASE | re.DOTALL,
)

# Broader fallback that also catches escaped/backtick-wrapped variants
_REQ_TAG_ALT_RE = re.compile(
    r"featureID\s*=?\s*\\?[\[{]?\s*(?P<fid>[0-9A-Fa-f]{6,}[\-0-9A-Fa-f]*)\s*\\?[\]}]?\s*"
    r"parentID\s*=?\s*(?P<pids>[^\]\n]+)",
    re.IGNORECASE,
)

# Stereotypes: «design_decision», «information», «context», etc.
_STEREOTYPE_RE = re.compile(
    r"[«<\*_`]+\s*(?P<stereo>design_decision|information|context)\s*[»>\*_`]+",
    re.IGNORECASE,
)

# Section heading: captures level and number  e.g. "### 3.1.3.4 ADC: ..."
_HEADING_RE = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<secnum>\d+(?:\.\d+)+)\s+(?P<title>.+)$",
    re.MULTILINE,
)

# HW peripheral heading pattern:  "XXX: dependent|primary hardware peripheral"
_HW_PERIPHERAL_RE = re.compile(
    r"(?P<name>\w[\w/\s]*?):\s*(?P<ptype>dependent|primary|shared)\s+hardware\s+peripheral",
    re.IGNORECASE,
)

# Table row  (pipe-delimited)
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)

# Container heading: "Container: Name" or "Container - Name"
_CONTAINER_HEADING_RE = re.compile(
    r"Container[\s:\-]+(?P<cname>\w+)",
    re.IGNORECASE,
)

# PRQ reference:  AU3GM-PRQ-xxxxx
_PRQ_REF_RE = re.compile(r"AU3GM-PRQ-\d+", re.IGNORECASE)

# Feature GUID reference (non-PRQ parentID)
_GUID_RE = re.compile(
    r"\{?\s*([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\s*\}?",
)

# Rationale section
_RATIONALE_RE = re.compile(
    r"(?:\*\*)?Rationale(?:\*\*)?[:\s]*\n?(.*?)(?=\n\n|\n(?:#{1,6}\s)|\n---|\Z)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_prq_references(text: str) -> List[str]:
    """Extract all AU3GM-PRQ-xxxxx references from text."""
    return sorted(set(_PRQ_REF_RE.findall(text)))


def _extract_guid_references(text: str) -> List[str]:
    """Extract all GUID references (non-PRQ parent IDs) from text."""
    return sorted(set(_GUID_RE.findall(text)))


def _parse_parent_ids(raw: str) -> Tuple[List[str], List[str]]:
    """Split parentID string into PRQ references and GUID references."""
    prqs = _extract_prq_references(raw)
    guids = _extract_guid_references(raw)
    return prqs, guids


def _clean_text(text: str) -> str:
    """Remove markdown formatting artefacts, collapse whitespace."""
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_table_cell(cell: str) -> str:
    """Strip a table cell value."""
    return re.sub(r"[`*_]", "", cell).strip().strip("|").strip()


def _split_sections_by_headings(content: str) -> List[dict]:
    """
    Split markdown content into sections based on headings.

    Returns a list of dicts with keys:
        level, section_number, title, heading_line, body
    """
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return []

    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        sections.append({
            "level": len(m.group("hashes")),
            "section_number": m.group("secnum"),
            "title": _clean_text(m.group("title")),
            "heading_line": m.group(0).strip(),
            "body": body,
        })
    return sections


# ---------------------------------------------------------------------------
# Parser: Architectural Decisions
# ---------------------------------------------------------------------------

def parse_architectural_decisions(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWA_ArchitecturalDecision nodes from markdown content.

    Detection is based on the presence of ``[req featureID=... parentID=...]``
    tags combined with stereotypes (``«design_decision»``, ``«information»``).
    Works for any section — not tied to 3.1.3.
    """
    sections = _split_sections_by_headings(content)
    decisions: List[dict] = []
    seen_fids: set = set()

    for sec in sections:
        full_text = sec["heading_line"] + "\n" + sec["body"]

        # Try primary regex first, then fallback
        req_match = _REQ_TAG_RE.search(full_text)
        if not req_match:
            req_match = _REQ_TAG_ALT_RE.search(full_text)
        if not req_match:
            continue

        fid = req_match.group("fid")
        if fid in seen_fids:
            continue
        seen_fids.add(fid)

        raw_pids = req_match.group("pids")
        prqs, guid_refs = _parse_parent_ids(raw_pids)

        # Detect stereotype
        stereo_match = _STEREOTYPE_RE.search(full_text)
        stereotype = stereo_match.group("stereo").lower() if stereo_match else None

        # Skip sections that look like config containers/params (table-heavy)
        # by checking if the section is primarily a spec table
        if _is_config_spec_table(sec["body"]):
            continue

        # Skip sections that match HW peripheral pattern
        if _HW_PERIPHERAL_RE.search(sec["title"]):
            continue

        # Extract rationale
        rationale_match = _RATIONALE_RE.search(sec["body"])
        rationale = _clean_text(rationale_match.group(1)) if rationale_match else None

        # Build description: body text minus the rationale and req tag lines
        desc_text = sec["body"]
        # Remove page markers
        desc_text = re.sub(r"^##\s+Pages?\s+\d+.*$", "", desc_text, flags=re.MULTILINE)
        # Remove figure/diagram explanation blocks (they're contextual, not the decision)
        desc_text = re.sub(
            r"(?:^|\n)###?\s+(?:Diagram explanation|Figure).*?(?=\n#{1,3}\s|\Z)",
            "", desc_text, flags=re.DOTALL | re.IGNORECASE,
        )
        desc_text = _clean_text(desc_text)
        if len(desc_text) > 2000:
            desc_text = desc_text[:2000] + "…"

        decision = {
            "decision_id": fid,
            "title": sec["title"],
            "section_number": sec["section_number"],
            "stereotype": stereotype,
            "description": desc_text if desc_text else None,
            "rationale": rationale,
            "prq_references": prqs if prqs else None,
            "feature_id_references": guid_refs if guid_refs else None,
            "module": module.upper(),
            "source_document": source_document,
        }
        decisions.append(decision)

    return decisions


def _is_config_spec_table(body: str) -> bool:
    """
    Heuristic: does the body look like an ECUC spec table?

    Config spec tables contain keywords like Multiplicity, Type, Origin,
    Post-Build, EcuC, etc.
    """
    indicators = ["Multiplicity:", "EcuC", "Post-Build", "Origin:", "Scope:", "Sub-Containers:"]
    count = sum(1 for ind in indicators if ind.lower() in body.lower())
    return count >= 3


# ---------------------------------------------------------------------------
# Parser: HW-SW Interface Peripherals
# ---------------------------------------------------------------------------

def parse_hw_peripherals(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWA_HwPeripheral nodes from markdown content.

    Detection: heading matches ``XXX: dependent|primary hardware peripheral``.
    Sub-sections are parsed for functional features, users, diagnostics, events.
    """
    sections = _split_sections_by_headings(content)
    peripherals: List[dict] = []
    seen_names: set = set()

    for i, sec in enumerate(sections):
        hw_match = _HW_PERIPHERAL_RE.search(sec["title"])
        if not hw_match:
            continue

        periph_name = hw_match.group("name").strip()
        periph_type = hw_match.group("ptype").lower()

        if periph_name in seen_names:
            continue
        seen_names.add(periph_name)

        # Collect sub-section content belonging to this peripheral
        subsection_body = sec["body"]
        # Also gather child sections (deeper heading level)
        for j in range(i + 1, len(sections)):
            if sections[j]["level"] <= sec["level"]:
                break
            subsection_body += "\n" + sections[j]["body"]

        # Extract structured fields by keyword matching
        functional_features = _extract_subsection(subsection_body, "Hardware functional features")
        users_of_hw = _extract_subsection(subsection_body, "Users of the hardware")
        diagnostic_features = _extract_subsection(subsection_body, "Hardware diagnostic features")
        hw_events_text = _extract_subsection(subsection_body, "Hardware events")

        # Parse HW events as a list
        hw_events = []
        if hw_events_text:
            for line in hw_events_text.split("\n"):
                line = line.strip().lstrip("-•*").strip()
                if line and not line.lower().startswith("hardware events"):
                    hw_events.append(_clean_text(line))

        # Extract PRQ references from the entire section
        prqs = _extract_prq_references(sec["heading_line"] + "\n" + subsection_body)

        peripheral = {
            "peripheral_name": periph_name,
            "peripheral_type": periph_type,
            "functional_features": _clean_text(functional_features) if functional_features else None,
            "unsupported_features": None,  # extracted from functional_features if present
            "users_of_hardware": _clean_text(users_of_hw) if users_of_hw else None,
            "diagnostic_features": _clean_text(diagnostic_features) if diagnostic_features else None,
            "hw_events": hw_events if hw_events else None,
            "section_number": sec["section_number"],
            "prq_references": prqs if prqs else None,
            "module": module.upper(),
            "source_document": source_document,
        }

        # Separate unsupported features if present
        if functional_features and "unsupported" in functional_features.lower():
            parts = re.split(r"(?i)unsupported\s+features?\s*(?:of\s+the\s+\w+\s+IP)?\s*(?:are)?:", functional_features)
            if len(parts) >= 2:
                peripheral["functional_features"] = _clean_text(parts[0])
                peripheral["unsupported_features"] = _clean_text(parts[1])

        peripherals.append(peripheral)

    return peripherals


def _extract_subsection(body: str, keyword: str) -> Optional[str]:
    """
    Extract text following a keyword header (bold or heading) until the
    next header or end of content.
    """
    # Try matching as a markdown heading or bold line
    pattern = re.compile(
        rf"(?:^|\n)(?:#+\s*)?(?:\*\*)?{re.escape(keyword)}(?:\*\*)?[:\s]*\n(.*?)(?=\n(?:#+\s|\*\*\w)|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(body)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Parser: SW-SW Interface Dependencies
# ---------------------------------------------------------------------------

def parse_sw_dependencies(
    content: str,
    module: str,
    source_document: str,
) -> List[dict]:
    """
    Extract SWA_SwDependency nodes from markdown content.

    Detection: pipe-delimited tables where the first column header
    matches ``API`` (or similar) and a second column like ``Description``.
    Also handles inline macro usage patterns.
    """
    dependencies: List[dict] = []
    seen_apis: set = set()

    # Find all table blocks in the content
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Detect table header row with API-like columns
        if (
            line.startswith("|")
            and _is_api_table_header(line)
        ):
            # Skip separator row
            i += 1
            if i < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i].strip()):
                i += 1

            # Parse data rows
            while i < len(lines):
                row = lines[i].strip()
                if not row.startswith("|") or re.match(r"^\|[\s\-:|]+\|$", row):
                    break

                cells = [_strip_table_cell(c) for c in row.split("|")[1:-1]]
                if len(cells) >= 2:
                    api_name = cells[0]
                    description = cells[1] if len(cells) > 1 else ""

                    # Clean up api_name
                    api_name = re.sub(r"[`*]", "", api_name).strip()
                    if not api_name or api_name in seen_apis:
                        i += 1
                        continue

                    # Handle multi-API rows (e.g. "API1\nAPI2")
                    api_names = [a.strip() for a in re.split(r"<br>|\n", api_name) if a.strip()]

                    for aname in api_names:
                        if aname in seen_apis:
                            continue
                        seen_apis.add(aname)

                        # Infer provider module from API name prefix
                        provider = _infer_provider_module(aname)
                        dep_type = _infer_dependency_type(aname)

                        dep = {
                            "api_name": aname,
                            "provider_module": provider,
                            "description": _clean_text(description) if description else None,
                            "dependency_type": dep_type,
                            "module": module.upper(),
                            "source_document": source_document,
                        }
                        dependencies.append(dep)
                i += 1
        else:
            i += 1

    return dependencies


def _is_api_table_header(line: str) -> bool:
    """Check if a table header row looks like an API dependency table."""
    cells = [c.strip().lower() for c in line.split("|")]
    api_headers = {"api", "api / macro", "api/macro", "item", "function"}
    desc_headers = {"description", "description / usage in adc driver", "usage", "purpose"}
    has_api = any(c in api_headers for c in cells)
    has_desc = any(any(d in c for d in desc_headers) for c in cells)
    return has_api and has_desc


def _infer_provider_module(api_name: str) -> Optional[str]:
    """Infer the provider module from an API/macro name prefix."""
    # Common patterns: Cdsp_Setup → Cdsp, Dma_ChUpdate → Dma, etc.
    if api_name.startswith("SchM_"):
        return "SchM"
    if api_name.startswith("MCAL"):
        return "McalLib"
    match = re.match(r"^([A-Z][a-z]+(?:[A-Z][a-z]*)?)_", api_name)
    if match:
        return match.group(1)
    return None


def _infer_dependency_type(api_name: str) -> str:
    """Infer the dependency type from the API/macro name."""
    upper = api_name.upper()
    if upper.startswith("MCALUTIL_") or upper.startswith("MCAL_"):
        return "macro"
    if api_name.startswith("SchM_Enter") or api_name.startswith("SchM_Exit"):
        return "protection"
    if "callback" in api_name.lower() or "notify" in api_name.lower():
        return "callback"
    return "function_call"


# ---------------------------------------------------------------------------
# Parser: Configuration Containers & Parameters
# ---------------------------------------------------------------------------

def parse_config_elements(
    content: str,
    module: str,
    source_document: str,
) -> Tuple[List[dict], List[dict]]:
    """
    Extract SWA_ConfigContainer and SWA_ConfigParam nodes from markdown.

    Detection: specification tables with keywords like Multiplicity, Type,
    Origin, Scope, Sub-Containers (for containers) or Range, Default Value,
    Value Configuration Class (for parameters).

    Returns (containers, parameters) tuple.
    """
    sections = _split_sections_by_headings(content)
    containers: List[dict] = []
    parameters: List[dict] = []
    seen_containers: set = set()
    seen_params: set = set()

    # Track most recent container for parent assignment
    container_stack: List[str] = []

    for sec in sections:
        body = sec["body"]
        if not _is_config_spec_table(body):
            continue

        # Determine if this is a container or parameter
        is_container = _detect_container(sec["title"], body)

        # Extract [req] tag if present
        full_text = sec["heading_line"] + "\n" + body
        fid = None
        prqs = []
        req_match = _REQ_TAG_RE.search(full_text) or _REQ_TAG_ALT_RE.search(full_text)
        if req_match:
            fid = req_match.group("fid")
            prqs = _extract_prq_references(req_match.group("pids"))

        # Parse the specification table
        spec = _parse_spec_table(body)

        # Extract the element name from the title or spec table
        element_name = _extract_element_name(sec["title"], spec)

        if is_container:
            if element_name in seen_containers or not element_name:
                continue
            seen_containers.add(element_name)

            container = {
                "container_name": element_name,
                "container_type": spec.get("type"),
                "description": spec.get("description"),
                "multiplicity_min": _parse_multiplicity_min(spec.get("multiplicity")),
                "multiplicity_max": _parse_multiplicity_max(spec.get("multiplicity")),
                "post_build_variant_multiplicity": _parse_bool(spec.get("post-build variant multiplicity")),
                "multiplicity_config_class": spec.get("multiplicity configuration class"),
                "origin": spec.get("origin"),
                "scope": spec.get("scope"),
                "dependencies": _parse_list(spec.get("dependency")),
                "sub_containers": _parse_list(spec.get("sub-containers")),
                "section_number": sec["section_number"],
                "prq_references": prqs if prqs else None,
                "module": module.upper(),
                "source_document": source_document,
            }
            containers.append(container)
            container_stack = [element_name]  # reset stack
        else:
            if element_name in seen_params or not element_name:
                continue
            seen_params.add(element_name)

            param = {
                "param_name": element_name,
                "param_type": spec.get("type"),
                "description": spec.get("description"),
                "range_min": None,
                "range_max": None,
                "range_values": spec.get("range"),
                "default_value": spec.get("default value"),
                "multiplicity": spec.get("multiplicity"),
                "post_build_variant_value": _parse_bool(spec.get("post-build variant value")),
                "value_config_class": spec.get("value configuration class"),
                "origin": spec.get("origin"),
                "scope": spec.get("scope"),
                "dependencies": _parse_list(spec.get("dependency")),
                "design_decisions": spec.get("design decisions"),
                "section_number": sec["section_number"],
                "prq_references": prqs if prqs else None,
                "parent_container": container_stack[-1] if container_stack else None,
                "module": module.upper(),
                "source_document": source_document,
            }

            # Parse range min/max for integer types
            range_val = spec.get("range", "")
            if range_val:
                range_parts = re.match(r"(\d+)\s*[-–]\s*(\d+)", range_val)
                if range_parts:
                    param["range_min"] = range_parts.group(1)
                    param["range_max"] = range_parts.group(2)

            parameters.append(param)

    return containers, parameters


def _detect_container(title: str, body: str) -> bool:
    """Detect whether a spec section describes a container vs a parameter."""
    # Explicit "Container:" in the title
    if _CONTAINER_HEADING_RE.search(title):
        return True
    # Has "Sub-Containers:" in the body (only containers have this)
    if re.search(r"Sub-Containers?:", body, re.IGNORECASE):
        return True
    # Has "EcuCModuleDef" or "EcuCParamConfContainerDef" type
    if re.search(r"EcuC(?:Module|ParamConf|Choice)(?:Container)?Def", body, re.IGNORECASE):
        return True
    # Parameters typically have Default Value or Range with numeric values
    has_default = bool(re.search(r"Default Value:", body, re.IGNORECASE))
    has_range = bool(re.search(r"Range:", body, re.IGNORECASE))
    if has_default or has_range:
        return False
    return False


def _parse_spec_table(body: str) -> dict:
    """
    Parse an ECUC specification table into a key-value dict.

    Handles multiple table formats:
      - 2-column:  | Key: | Value |
      - 4-column:  | Key: | Value | Key: | Value |
      - Bold keys: | **Key:** | value |
    """
    spec: dict = {}
    rows = _TABLE_ROW_RE.findall(body)

    for row_text in rows:
        cells = [_strip_table_cell(c) for c in row_text.split("|")]
        cells = [c for c in cells if c]  # remove empty

        # Skip separator rows
        if all(re.match(r"^[-:]+$", c) for c in cells):
            continue
        # Skip pure header rows
        if all(c.lower() in ("field", "value", "content", "item", "") for c in cells):
            continue

        # Process pairs: (key, value) from adjacent cells
        i = 0
        while i < len(cells):
            cell = cells[i]
            # Check if this cell looks like a key (ends with ":")
            key, value = _extract_key_value(cell, cells[i + 1] if i + 1 < len(cells) else "")
            if key:
                # Append to existing value if key already exists (multi-row values)
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
    # Remove bold markers
    clean = re.sub(r"\*\*", "", cell).strip()

    # Check for "Key:" pattern
    if clean.endswith(":"):
        key = clean[:-1].strip().lower()
        return key, next_cell
    # Check for "Key: Value" within the same cell
    kv_match = re.match(r"^(.+?):\s+(.+)$", clean)
    if kv_match:
        key = kv_match.group(1).strip().lower()
        value = kv_match.group(2).strip()
        return key, value

    return None, ""


def _extract_element_name(title: str, spec: dict) -> Optional[str]:
    """Extract the configuration element name from title or spec.

    Handles three markdown variants of the name field:
      1. "[req ...]ElementName[/req] ElementName"        (space-separated)
      2. ".[req ...]ElementName[/req]ElementName"         (no space → doubled)
      3. "\\[req ...\\]ElementName\\[/req\\]ElementName"  (escaped brackets)
    """
    # Try from spec "name" field first
    name_val = spec.get("name", "")
    if name_val:
        # ---- Strategy 1: extract the name from *inside* the [req] tag ----
        # Pattern: (prefix)[req ...]NAME[/req]  — grab NAME between ] and [/req]
        inner_match = re.search(
            r"(?:\\?\]|\])"       # closing ] of the opening [req ...] tag
            r"([A-Za-z]\w+)"     # the actual element name (capture group 1)
            r"(?:\\?\[/?req)",   # start of [/req] closing tag
            name_val,
        )
        if inner_match:
            return inner_match.group(1)

        # ---- Strategy 2: remove [req]/[/req] tags (normal + escaped) ----
        # Remove escaped: \[req...\] and \[/req\]
        clean = re.sub(r"\\\[/?req[^\]]*?\\?\]", "", name_val)
        # Remove normal:  [req...]  and [/req]
        clean = re.sub(r"\[/?req[^\]]*\]", "", clean)
        clean = clean.strip()

        # Strip leading punctuation artefacts (., \, space)
        clean = clean.lstrip(".\\" + " ")

        # Detect doubled name: "NameName" → "Name"
        if clean and len(clean) % 2 == 0:
            half = len(clean) // 2
            if clean[:half] == clean[half:]:
                return clean[:half]

        # Take the last whitespace-separated word
        parts = clean.split()
        if parts:
            return parts[-1].lstrip(".\\" + " ")

    # Fallback: extract from title
    # "Container: AdcHwUnit" → "AdcHwUnit"
    container_match = _CONTAINER_HEADING_RE.search(title)
    if container_match:
        return container_match.group("cname")

    # Title like "AdcGroupConversionMode" or "3.1.7.1.15.6 AdcGroupConversionMode"
    # Remove section numbers and common prefixes
    clean_title = re.sub(r"^\d+(\.\d+)*\s*", "", title).strip()
    clean_title = re.sub(r"^Container[\s:\-]+", "", clean_title, flags=re.IGNORECASE).strip()
    # Remove backticks
    clean_title = clean_title.strip("`")
    if clean_title and re.match(r"^[A-Z]", clean_title):
        return clean_title

    return None


def _parse_multiplicity_min(mult: Optional[str]) -> Optional[int]:
    """Parse the min from a multiplicity string like '0..8'."""
    if not mult:
        return None
    m = re.match(r"(\d+)\s*\.\.\s*", mult)
    return int(m.group(1)) if m else None


def _parse_multiplicity_max(mult: Optional[str]) -> Optional[int]:
    """Parse the max from a multiplicity string like '0..8'."""
    if not mult:
        return None
    m = re.search(r"\.\.\s*(\d+|\*)", mult)
    if m:
        val = m.group(1)
        return None if val == "*" else int(val)
    return None


def _parse_bool(val: Optional[str]) -> Optional[bool]:
    """Parse TRUE/FALSE/NA string to bool."""
    if not val:
        return None
    v = val.strip().upper()
    if v == "TRUE":
        return True
    if v in ("FALSE", "NA", "N/A"):
        return False
    return None


def _parse_list(val: Optional[str]) -> Optional[List[str]]:
    """Parse a newline/BR-separated list from a spec field."""
    if not val or val.strip() == "":
        return None
    items = re.split(r"<br>|\n|,", val)
    items = [i.strip() for i in items if i.strip()]
    return items if items else None


# ---------------------------------------------------------------------------
# Parser: Architectural Decisions – hierarchy builder
# ---------------------------------------------------------------------------

def build_decision_hierarchy(decisions: List[dict]) -> List[dict]:
    """
    Compute parent-child edges for architectural decisions based on
    section_number hierarchy: 3.1.3.5 is parent of 3.1.3.5.1.

    Returns a list of edge dicts: {child_id, parent_id}.
    """
    edges: List[dict] = []
    by_section: Dict[str, str] = {
        d["section_number"]: d["decision_id"]
        for d in decisions
        if d.get("section_number")
    }

    for dec in decisions:
        sec = dec.get("section_number", "")
        if not sec or "." not in sec:
            continue
        parent_sec = sec.rsplit(".", 1)[0]
        parent_id = by_section.get(parent_sec)
        if parent_id and parent_id != dec["decision_id"]:
            edges.append({
                "child_id": dec["decision_id"],
                "parent_id": parent_id,
            })

    return edges


# ---------------------------------------------------------------------------
# Parser: Exported SW Interface (SWA_Function, SWA_DataType, SWA_Macro)
# ---------------------------------------------------------------------------
#
# Detection is **pattern-based** — no hardcoded section numbers.
# The parser scans all content for "Specification for ..." tables and
# classifies each table as a function, data-type, or macro based on
# which rows appear (Service ID, Sync/Async → function;
# Type = Macro → macro; otherwise data type).
# ---------------------------------------------------------------------------

# Matches: **Table 180: Specification for `Adc_Init`**
_SPEC_TABLE_RE = re.compile(
    r"\*{0,2}Table\s+\d+:\s*Specification\s+for\s+[`*]*"
    r"(?P<name>[A-Za-z_]\w*)"
    r"[`*]*\*{0,2}",
    re.IGNORECASE,
)

# Keys that distinguish a **function** spec table
_FUNC_KEYS = {"service id", "sync/async", "parameters (in)", "parameters (out)"}
# Keys that distinguish a **data-type** spec table
_DTYPE_KEYS = {"range", "file"}
# Keys exclusive to configuration parameters — used as negative filter
_CONFPARAM_KEYS = {
    "multiplicity", "default value", "scope", "dependency", "origin",
    "post-build variant value", "value configuration class",
    "multiplicity configuration class", "post-build variant multiplicity",
}
# Literal "Macro" in the Type field signals a macro


def _parse_spec_table_rows(body: str) -> Dict[str, str]:
    """Parse pipe-delimited spec table rows into a key→value dict.

    Handles ``| Key | Value |`` (2-col) and ``| K | V | K | V |`` (4-col)
    variants, with optional colons on keys.  Continues through blank lines
    to capture consecutive tables (e.g. Service-ID table + Parameters table).
    Also handles "header-data" tables where the first row is a column header
    (e.g. ``| Parameters (in) | Description |``) and data is in subsequent rows.

    Resilient to PDF→markdown page-break headings (``## Pages X-Y``) and
    other non-table content that may interrupt spec tables.  Scanning
    continues past such interruptions until a new spec-table heading or
    numbered section heading is encountered.
    """
    _HEADER_WORDS = {"description", "content", "details"}
    # Keys that can appear as header-data table headers
    _HEADERDATA_KEYS = {
        "parameters (in)", "parameters (out)", "parameters (in-out)",
        "return",
    }
    kv: Dict[str, str] = {}
    table_seen = False
    gap_lines = 0  # non-table lines since last table row
    # For header-data table accumulation
    pending_header_key: Optional[str] = None
    pending_data_rows: List[str] = []

    def _flush_pending():
        nonlocal pending_header_key, pending_data_rows
        if pending_header_key and pending_data_rows:
            kv[pending_header_key] = " ".join(pending_data_rows)
        pending_header_key = None
        pending_data_rows = []

    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            if table_seen:
                _flush_pending()
                gap_lines += 1
            continue
        if stripped.startswith("|"):
            gap_lines = 0
            # Skip separator rows  |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                table_seen = True
                continue
            table_seen = True
            cells = [c.strip() for c in stripped.split("|")[1:-1]]

            # Check if we're in a header-data table (accumulate data rows)
            if pending_header_key:
                # Accumulate: first cell is the name, second is description
                row_text = " ".join(c for c in cells if c and c != "—")
                if row_text:
                    pending_data_rows.append(row_text)
                continue

            # Process pairs: (key, value), handling both 2-col and 4-col
            for i in range(0, len(cells) - 1, 2):
                key = re.sub(r"[`*:]", "", cells[i]).strip().lower()
                value = cells[i + 1].strip()

                # When the paired value cell is empty, scan remaining
                # cells for the actual value — handles both 3-col
                # (| Key: |  | value |) and 4-col (| Key: |  |  | value |)
                # table variants from inconsistent PDF conversion.
                if key and not value:
                    for k in range(i + 2, len(cells)):
                        candidate = re.sub(r"[`*]", "", cells[k]).strip()
                        if candidate:
                            # Stop if this looks like another key (trailing colon)
                            if re.match(r'^[A-Za-z].*:\s*$', candidate):
                                break
                            value = cells[k].strip()
                            break

                if key:
                    # Detect header-data tables — key is a known spec field
                    # and value is a generic column header
                    if (key in _HEADERDATA_KEYS
                            and value.lower() in _HEADER_WORDS):
                        _flush_pending()
                        pending_header_key = key
                    else:
                        kv[key] = value

            # Handle odd leftover cell containing "Key: Value" combined,
            # e.g. "| Service ID: | 0x00 | ASIL Level: D |" (3 cells)
            if len(cells) % 2 == 1:
                leftover = cells[-1].strip()
                if leftover:
                    m_kv = re.match(
                        r'^([`*]*[A-Za-z][A-Za-z /()-]*?)[`*]*\s*:\s*(.+)$',
                        leftover,
                    )
                    if m_kv:
                        lk = re.sub(r"[`*:]", "", m_kv.group(1)).strip().lower()
                        lv = re.sub(r"[`*]", "", m_kv.group(2)).strip()
                        if lk and lv:
                            kv[lk] = lv
        elif table_seen:
            _flush_pending()
            # Stop at a new spec-table heading (another function's spec)
            if re.match(
                r"\*{0,2}Table\s+\d+:\s*Specification\s+for\s+",
                stripped, re.IGNORECASE,
            ):
                break
            # Stop at a numbered section heading (new section boundary)
            if re.match(r"^#{1,6}\s+\d+(?:\.\d+)+\s+", stripped):
                break
            # Otherwise skip non-table content (page breaks, prose, code
            # fences) and keep scanning for more table rows.
            gap_lines += 1
            if gap_lines > 40:
                break
            continue

    _flush_pending()
    return kv


def _extract_feature_id_from_syntax(syntax_cell: str) -> Optional[str]:
    """Extract the first featureID GUID from a Syntax table cell.

    Handles standard 8-4-4-4-12 GUIDs as well as truncated / OCR-corrupted
    variants where one or more hex segments are shorter than expected.
    """
    # Try standard GUID first
    m = re.search(
        r"featureID\s*=\s*\\?[\[{]?\s*"
        r"(?P<fid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})",
        syntax_cell, re.IGNORECASE,
    )
    if m:
        return m.group("fid")
    # Fallback: relaxed GUID — at least 3 hyphen-separated hex segments
    m = re.search(
        r"featureID\s*=\s*\\?[\[{]?\s*"
        r"(?P<fid>[0-9A-Fa-f]{4,8}(?:-[0-9A-Fa-f]{2,12}){2,4})",
        syntax_cell, re.IGNORECASE,
    )
    return m.group("fid") if m else None


def _extract_return_type(raw: str) -> Optional[str]:
    """Extract just the C type name from a Return field value.

    The field may contain ``Std_ReturnType E_OK: ... E_NOT_OK: ...`` when
    parsed from a header-data table. We want only the first C identifier.
    """
    cleaned = re.sub(r"[`*]", "", raw).strip()
    if not cleaned:
        return None
    # Take the first C identifier token (e.g. "void", "Std_ReturnType")
    m = re.match(r"([A-Za-z_]\w*)", cleaned)
    return m.group(1) if m else cleaned


def _extract_name_from_syntax(syntax_cell: str) -> Optional[str]:
    """Extract the element name from inside [req ...]Name[/req] in Syntax.

    Handles variants:
    - ``]Name[/req]``
    - ``]Name/[req]``  (OCR/PDF artefact with swapped /)
    - ``\\]Name\\[/req\\]``  (escaped brackets)
    """
    # Standard: ]Name[/req]
    m = re.search(r"\]([A-Za-z_]\w*)\s*\[/?req", syntax_cell)
    if m:
        return m.group(1)
    # OCR variant: ]Name/[req]
    m = re.search(r"\]([A-Za-z_]\w*)\s*/\[req\]", syntax_cell)
    if m:
        return m.group(1)
    # Escaped variant: \]Name\[/req\]
    m = re.search(r"\\\]([A-Za-z_]\w*)\s*\\\[/?req", syntax_cell)
    if m:
        return m.group(1)
    return None


def _extract_c_signature(syntax_cell: str) -> Optional[str]:
    """Extract the C function signature from a Syntax cell (after the req tag)."""
    # Remove the [req ...] ... [/req] block and grab the rest
    cleaned = re.sub(
        r"\[_?req\s.*?\[/?req\]", "", syntax_cell,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"[`*]", "", cleaned).strip()
    if cleaned and "(" in cleaned:
        return cleaned
    return None


def parse_exported_interfaces(
    content: str,
    module: str,
    source_document: str,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Extract SWA_Function, SWA_DataType, and SWA_Macro nodes from
    SWA markdown content.

    Detection is pattern-based: scans for ``Specification for`` tables
    and classifies each by its table rows (no section numbers used).

    Returns
    -------
    tuple of (functions, datatypes, macros)
        Each element is a list of property dicts ready for Neo4j.
    """
    sections = _split_sections_by_headings(content)
    functions: List[dict] = []
    datatypes: List[dict] = []
    macros: List[dict] = []
    seen_fids: set = set()

    for sec in sections:
        full_text = sec["heading_line"] + "\n" + sec["body"]

        # Look for a spec table in this section
        spec_match = _SPEC_TABLE_RE.search(full_text)
        if not spec_match:
            continue

        element_name = spec_match.group("name")

        # Parse the table rows into key-value pairs
        # Start parsing from after the table heading
        table_start = full_text.index(spec_match.group(0)) + len(spec_match.group(0))
        kv = _parse_spec_table_rows(full_text[table_start:])
        if not kv:
            continue

        keys_lower = set(kv.keys())

        # ── Also look for Syntax / Description as standalone paragraphs ──
        # Some spec formats place Syntax and Description outside the
        # pipe-delimited table: as **Syntax:** bold, ### Syntax heading,
        # or inside code blocks.  Grab the full section text.
        section_text = full_text[table_start:]

        # Also parse any "continued" tables (split by --- separators)
        # by scanning past the first table break.
        cont_m = re.search(
            r"\(continued\)",
            section_text, re.IGNORECASE,
        )
        if cont_m:
            extra_kv = _parse_spec_table_rows(section_text[cont_m.end():])
            for k, v in extra_kv.items():
                if k not in kv:
                    kv[k] = v
            keys_lower = set(kv.keys())

        if "syntax" not in keys_lower:
            # Match **Syntax:** or ### Syntax or ## Syntax heading formats
            syn_m = re.search(
                r"(?:\*{2}Syntax:?\*{2}|^#{2,4}\s+Syntax\b)"
                r"\s*(.*?)(?=\n\s*\n\*{2}[A-Z]|\n\||\Z)",
                section_text, re.DOTALL | re.IGNORECASE | re.MULTILINE,
            )
            if syn_m:
                kv["syntax"] = syn_m.group(1).strip()
                keys_lower.add("syntax")

        if "description" not in keys_lower:
            desc_m = re.search(
                r"(?:\*{2}Description:?\*{2}|^#{2,4}\s+Description\b)"
                r"\s*(.*?)(?=\n\s*\*{2}[A-Z]|\n\s*#{1,4}\s|\Z)",
                section_text, re.DOTALL | re.IGNORECASE | re.MULTILINE,
            )
            if desc_m:
                kv["description"] = desc_m.group(1).strip()
                keys_lower.add("description")

        if "error handling" not in keys_lower:
            err_m = re.search(
                r"(?:\*{2}Error\s+Handling:?\*{2}|^#{2,4}\s+Error\s+Handling\b)"
                r"\s*(.*?)(?=\n\s*\*{2}[A-Z]|\n\s*#{1,4}\s|\Z)",
                section_text, re.DOTALL | re.IGNORECASE | re.MULTILINE,
            )
            if err_m:
                kv["error handling"] = err_m.group(1).strip()
                keys_lower.add("error handling")

        # Extract feature_id & PRQs — try the Syntax cell first, then
        # fall back to searching the entire section text (handles cases
        # where [req] tags appear in headings or code blocks).
        syntax_cell = kv.get("syntax", "")
        feature_id = _extract_feature_id_from_syntax(syntax_cell)
        if not feature_id:
            feature_id = _extract_feature_id_from_syntax(section_text)

        # Skip if already seen (dedup by feature_id)
        if feature_id:
            if feature_id in seen_fids:
                continue
            seen_fids.add(feature_id)

        # Extract PRQ references from the Syntax [req] tag
        prqs = _extract_prq_references(syntax_cell)
        if not prqs:
            prqs = _extract_prq_references(section_text)

        # Also try the element name from inside the [req] tag (more reliable)
        req_name = _extract_name_from_syntax(syntax_cell)
        if not req_name:
            req_name = _extract_name_from_syntax(section_text)
        if req_name:
            element_name = req_name

        # ── Classify ──────────────────────────────────────────────

        is_function = bool(keys_lower & _FUNC_KEYS)
        is_confparam = bool(keys_lower & _CONFPARAM_KEYS)
        type_val = re.sub(r"[`*]", "", kv.get("type", "")).strip().lower()
        is_macro = type_val == "macro"

        if is_function:
            # ── SWA_Function ──────────────────────────────────────
            c_sig = _extract_c_signature(syntax_cell)

            # Parse parameters
            params_in = kv.get("parameters (in)", "").strip() or None
            params_out = kv.get("parameters (out)", "").strip() or None
            params_inout = kv.get("parameters (in-out)", "").strip() or None

            func = {
                "function_name": element_name,
                "feature_id": feature_id,
                "section_number": sec["section_number"],
                "syntax": c_sig,
                "service_id": kv.get("service id"),
                "sync_async": kv.get("sync/async"),
                "reentrancy": kv.get("reentrancy"),
                "asil_level": kv.get("asil level"),
                "function_type": kv.get("type"),
                "parameters_in": params_in,
                "parameters_out": params_out,
                "parameters_inout": params_inout,
                "return_type": _extract_return_type(kv.get("return", "")),
                "description": _clean_text(kv.get("description", "")) or None,
                "error_handling": kv.get("error handling"),
                "configuration_dependencies": kv.get("configuration dependencies"),
                "file": kv.get("file"),
                "memory_section": kv.get("memory section"),
                "source": kv.get("source"),
                "prq_references": prqs if prqs else None,
                "module": module.upper(),
                "source_document": source_document,
            }
            functions.append(func)

        elif is_macro:
            # ── SWA_Macro ─────────────────────────────────────────
            macro = {
                "macro_name": element_name,
                "feature_id": feature_id,
                "section_number": sec["section_number"],
                "file": kv.get("file"),
                "value": kv.get("range"),
                "description": _clean_text(kv.get("description", "")) or None,
                "prq_references": prqs if prqs else None,
                "module": module.upper(),
                "source_document": source_document,
            }
            macros.append(macro)

        elif keys_lower & _DTYPE_KEYS and not is_confparam:
            # ── SWA_DataType ──────────────────────────────────────
            dtype = {
                "type_name": element_name,
                "feature_id": feature_id,
                "section_number": sec["section_number"],
                "c_type": re.sub(r"[`*]", "", kv.get("type", "")).strip() or None,
                "file": kv.get("file"),
                "range": kv.get("range"),
                "description": _clean_text(kv.get("description", "")) or None,
                "design_decisions": kv.get("design decisions"),
                "source": kv.get("source"),
                "prq_references": prqs if prqs else None,
                "module": module.upper(),
                "source_document": source_document,
            }
            datatypes.append(dtype)

    return functions, datatypes, macros


# ---------------------------------------------------------------------------
# High-level: parse an entire SWA directory
# ---------------------------------------------------------------------------

def detect_module_from_files(swa_dir: Path) -> Optional[str]:
    """
    Auto-detect the MCAL module name from SWA filenames.

    Looks for patterns like ``TC4xx_SW_MCAL_SWA_Adc.md`` or
    ``TC4xx_SW_MCAL_SWA_Spi.pdf`` and extracts the module suffix.
    """
    for f in swa_dir.iterdir():
        m = re.search(r"SWA_(\w+)\.", f.name, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def detect_source_document(swa_dir: Path) -> Optional[str]:
    """
    Auto-detect the source document name from SWA directory.
    """
    for f in swa_dir.iterdir():
        if f.suffix in (".pdf", ".docx") and "SWA" in f.name.upper():
            return f.stem
    # Fallback: try .md files
    for f in swa_dir.iterdir():
        if f.suffix == ".md" and "SWA" in f.name.upper() and not f.name.startswith("section_"):
            return f.stem
    return None


def parse_swa_directory(
    swa_dir: str | Path,
    module: Optional[str] = None,
    source_document: Optional[str] = None,
) -> Dict[str, List[dict]]:
    """
    Parse all SWA section markdown files in a directory.

    Auto-detects the module and source document if not provided.
    Returns a dict mapping node type names → list of node property dicts.

    Parameters
    ----------
    swa_dir : path
        Directory containing ``section_*_raw.md`` files.
    module : str, optional
        MCAL module name (e.g. "ADC"). Auto-detected from filenames if omitted.
    source_document : str, optional
        Source document name. Auto-detected if omitted.

    Returns
    -------
    dict
        Keys: node type names (e.g. "SWA_ArchitecturalDecision").
        Values: lists of property dicts ready for Neo4j ingestion.
    """
    swa_dir = Path(swa_dir)
    if not swa_dir.exists():
        logger.error("SWA directory does not exist: %s", swa_dir)
        return {}

    # Auto-detect module and source document
    if not module:
        module = detect_module_from_files(swa_dir) or "UNKNOWN"
        logger.info("Auto-detected module: %s", module)
    if not source_document:
        source_document = detect_source_document(swa_dir) or "unknown_swa_document"
        logger.info("Auto-detected source document: %s", source_document)

    # Find all section markdown files
    section_files = sorted(swa_dir.glob("section_*_raw.md"))
    if not section_files:
        logger.warning("No section_*_raw.md files found in %s", swa_dir)
        return {}

    logger.info("Found %d SWA section files in %s", len(section_files), swa_dir.name)

    # Accumulate nodes from all files
    all_decisions: List[dict] = []
    all_peripherals: List[dict] = []
    all_dependencies: List[dict] = []
    all_containers: List[dict] = []
    all_params: List[dict] = []
    all_functions: List[dict] = []
    all_datatypes: List[dict] = []
    all_macros: List[dict] = []

    for fpath in section_files:
        logger.info("  Parsing %s …", fpath.name)
        content = fpath.read_text(encoding="utf-8")

        # Run all parsers on every file — each parser uses pattern detection
        # to decide what to extract (fully section-agnostic)
        decisions = parse_architectural_decisions(content, module, source_document)
        peripherals = parse_hw_peripherals(content, module, source_document)
        dependencies = parse_sw_dependencies(content, module, source_document)
        containers, params = parse_config_elements(content, module, source_document)
        functions, datatypes, macros_nodes = parse_exported_interfaces(
            content, module, source_document,
        )

        if decisions:
            logger.info("    → %d architectural decisions", len(decisions))
        if peripherals:
            logger.info("    → %d HW peripherals", len(peripherals))
        if dependencies:
            logger.info("    → %d SW dependencies", len(dependencies))
        if containers:
            logger.info("    → %d config containers", len(containers))
        if params:
            logger.info("    → %d config parameters", len(params))
        if functions:
            logger.info("    → %d exported functions", len(functions))
        if datatypes:
            logger.info("    → %d data types", len(datatypes))
        if macros_nodes:
            logger.info("    → %d macros", len(macros_nodes))

        all_decisions.extend(decisions)
        all_peripherals.extend(peripherals)
        all_dependencies.extend(dependencies)
        all_containers.extend(containers)
        all_params.extend(params)
        all_functions.extend(functions)
        all_datatypes.extend(datatypes)
        all_macros.extend(macros_nodes)

    # Build hierarchy edges for decisions
    decision_hierarchy = build_decision_hierarchy(all_decisions)

    result: Dict[str, List[dict]] = {}
    if all_decisions:
        result["SWA_ArchitecturalDecision"] = all_decisions
    if all_peripherals:
        result["SWA_HwPeripheral"] = all_peripherals
    if all_dependencies:
        result["SWA_SwDependency"] = all_dependencies
    if all_containers:
        result["SWA_ConfigContainer"] = all_containers
    if all_params:
        result["SWA_ConfigParam"] = all_params
    if all_functions:
        result["SWA_Function"] = all_functions
    if all_datatypes:
        result["SWA_DataType"] = all_datatypes
    if all_macros:
        result["SWA_Macro"] = all_macros
    if decision_hierarchy:
        result["_edges_SWA_ARCH_DECISION_PARENT"] = decision_hierarchy

    total = sum(len(v) for k, v in result.items() if not k.startswith("_"))
    logger.info(
        "SWA parsing complete: %d nodes across %d types",
        total,
        sum(1 for k in result if not k.startswith("_")),
    )

    return result

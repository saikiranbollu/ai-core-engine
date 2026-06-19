"""
AUTOSAR XML (ARXML) Parser
===========================

Parses ``.arxml`` files — both pure AUTOSAR XML and EB tresos template
ARXML (containing ``[!IF!]``, ``[!FOR!]`` etc. code-generation macros) —
into a hybrid structure suitable for both RAG embedding and knowledge-graph
ingestion.

The output contains:

* **modules** – hierarchical dict preserving the AUTOSAR package / container
  tree (``ECUC-MODULE-CONFIGURATION-VALUES``, ``BSW-MODULE-DESCRIPTION``,
  ``BSW-IMPLEMENTATION``, …).
* **chunks** – a flat list of self-contained text snippets, each prefixed
  with its full AUTOSAR path, ready for vector-store embedding.
* **cross_references** – every ``DEFINITION-REF`` and ``*-REF`` collected
  for downstream relationship creation.

Usage::

    from IngestionPipeline.parsers import arxml_parser

    result = arxml_parser.parse("EcuC_001.arxml")
    # result["modules"]           → hierarchical tree
    # result["chunks"]            → flat list for RAG
    # result["cross_references"]  → DEFINITION-REF edges
"""

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# AUTOSAR R4.0 namespace
_NS = "{http://autosar.org/schema/r4.0}"

# EB tresos / code-gen template macros ─ block-level and inline
_TEMPLATE_BLOCK_RE = re.compile(
    r"^\s*\[!(NOCODE|ENDNOCODE|CODE|ENDCODE|IF|ENDIF|ELSE|"
    r"FOR|ENDFOR|SELECT|ENDSELECT|LOOP|ENDLOOP|VAR|CALL|INCLUDE)"
    r"[^\]]*\].*$",
    re.MULTILINE,
)
_TEMPLATE_INLINE_RE = re.compile(r'\[!"[^"]*"!\]')
# Catch remaining [!...!] expressions not matched above
_TEMPLATE_REMAINING_RE = re.compile(r"\[!.*?!\]", re.DOTALL)

# ---------------------------------------------------------------------------
# Parameter-type extraction
# ---------------------------------------------------------------------------

_PARAM_SUFFIX_RE = re.compile(r"^(?:.*?)-?(.+)-PARAM-(?:VALUE|DEF)$")


def _extract_param_type(tag: str) -> str:
    """Derive parameter type from an AUTOSAR XML tag name.

    Strips any prefix (``ECUC-``, or others) and the
    ``-PARAM-VALUE`` / ``-PARAM-DEF`` suffix, returning only the
    semantic type portion.  No prefix is assumed — works for any
    current or future AUTOSAR tag pattern.

    Examples::

        '{ns}ECUC-NUMERICAL-PARAM-VALUE'  → 'NUMERICAL'
        'ECUC-INTEGER-PARAM-DEF'          → 'INTEGER'
        'ECUC-BOOLEAN-PARAM-DEF'          → 'BOOLEAN'
        'ECUC-ADD-INFO-PARAM-VALUE'       → 'ADD-INFO'
        'CUSTOM-FLOAT-PARAM-DEF'          → 'FLOAT'
    """
    local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
    m = _PARAM_SUFFIX_RE.match(local)
    if not m:
        logger.warning("Could not extract param type from tag: %s — returning raw tag", local)
        return local
    # The captured group may still contain a leading prefix (e.g. "ECUC-").
    # Strip the first segment when separated by '-' and followed by the type.
    captured = m.group(1)
    # If the captured part starts with a known prefix token, remove it.
    # e.g. "ECUC-INTEGER" → take everything after the first '-' only if
    # a '-' exists and the part before it is all-alpha (a prefix tag).
    parts = captured.split("-", 1)
    if len(parts) == 2 and parts[0].isalpha():
        return parts[1] if parts[1] else captured
    return captured


# ===================================================================
# Helper – XML text extraction
# ===================================================================

def _text(element: Optional[ET.Element]) -> Optional[str]:
    """Return stripped text of *element*, or ``None``."""
    if element is not None and element.text:
        return element.text.strip()
    return None


def _short_name(element: ET.Element) -> Optional[str]:
    return _text(element.find(f"{_NS}SHORT-NAME"))


def _definition_ref(element: ET.Element) -> Optional[str]:
    return _text(element.find(f"{_NS}DEFINITION-REF"))


# ===================================================================
# Template pre-processing
# ===================================================================

def _strip_template_macros(raw: str) -> tuple[str, bool, int]:
    """Strip EB tresos code-gen macros from *raw* ARXML content.

    Returns ``(cleaned_xml, is_template, macros_stripped)``.
    """
    count = 0

    def _count_block(m: re.Match) -> str:
        nonlocal count
        count += 1
        return ""

    text = _TEMPLATE_BLOCK_RE.sub(_count_block, raw)

    for pattern in (_TEMPLATE_INLINE_RE, _TEMPLATE_REMAINING_RE):
        def _count_inline(m: re.Match) -> str:
            nonlocal count
            count += 1
            return ""
        text = pattern.sub(_count_inline, text)

    return text, count > 0, count


# ===================================================================
# Container / parameter extraction
# ===================================================================

def _parse_parameters(container: ET.Element) -> List[Dict[str, Any]]:
    """Extract PARAMETER-VALUES from a container element."""
    params: List[Dict[str, Any]] = []
    pv = container.find(f"{_NS}PARAMETER-VALUES")
    if pv is None:
        return params

    for child in pv:
        def_ref = _definition_ref(child)

        # Prefer the more specific type from DEFINITION-REF DEST, fall
        # back to the element tag name (both follow the same pattern).
        def_ref_elem = child.find(f"{_NS}DEFINITION-REF")
        dest = def_ref_elem.get("DEST", "") if def_ref_elem is not None else ""
        param_type = _extract_param_type(dest) if dest else _extract_param_type(child.tag)

        # Derive a human-friendly name from the definition ref
        name = def_ref.rsplit("/", 1)[-1] if def_ref else None

        value = _text(child.find(f"{_NS}VALUE"))
        params.append({
            "name": name,
            "definition_ref": def_ref,
            "param_type": param_type,
            "value": value,
        })
    return params


def _parse_references(container: ET.Element) -> List[Dict[str, Any]]:
    """Extract REFERENCE-VALUES from a container element."""
    refs: List[Dict[str, Any]] = []
    rv = container.find(f"{_NS}REFERENCE-VALUES")
    if rv is None:
        return refs
    for child in rv:
        def_ref = _definition_ref(child)
        value_ref = _text(child.find(f"{_NS}VALUE-REF"))
        refs.append({
            "definition_ref": def_ref,
            "value_ref": value_ref,
        })
    return refs


def _parse_container(
    container: ET.Element,
    parent_path: str,
) -> Dict[str, Any]:
    """Recursively parse an ECUC-CONTAINER-VALUE."""
    name = _short_name(container) or "UNNAMED"
    path = f"{parent_path}/{name}"
    def_ref = _definition_ref(container)

    node: Dict[str, Any] = {
        "short_name": name,
        "path": path,
        "definition_ref": def_ref,
        "type": "ECUC-CONTAINER-VALUE",
        "parameters": _parse_parameters(container),
        "references": _parse_references(container),
        "sub_containers": [],
    }

    sub_c = container.find(f"{_NS}SUB-CONTAINERS")
    if sub_c is not None:
        for child in sub_c.findall(f"{_NS}ECUC-CONTAINER-VALUE"):
            node["sub_containers"].append(_parse_container(child, path))

    return node


# ===================================================================
# BSW element extraction
# ===================================================================

def _parse_exclusive_areas(behavior: ET.Element) -> List[Dict[str, Any]]:
    """Extract EXCLUSIVE-AREAs from a BSW-INTERNAL-BEHAVIOR."""
    areas: List[Dict[str, Any]] = []
    eas = behavior.find(f"{_NS}EXCLUSIVE-AREAS")
    if eas is None:
        return areas
    for ea in eas.findall(f"{_NS}EXCLUSIVE-AREA"):
        areas.append({"short_name": _short_name(ea) or "UNNAMED"})
    return areas


def _parse_memory_sections(resource: ET.Element) -> List[Dict[str, Any]]:
    """Extract MEMORY-SECTIONs from a RESOURCE-CONSUMPTION element."""
    sections: List[Dict[str, Any]] = []
    ms_parent = resource.find(f"{_NS}MEMORY-SECTIONS")
    if ms_parent is None:
        return sections
    for ms in ms_parent.findall(f"{_NS}MEMORY-SECTION"):
        alignment = _text(ms.find(f"{_NS}ALIGNMENT"))
        symbol = _text(ms.find(f"{_NS}SYMBOL"))
        prefix_ref = _text(ms.find(f"{_NS}PREFIX-REF"))
        sw_addr_ref = _text(ms.find(f"{_NS}SW-ADDRMETHOD-REF"))
        sections.append({
            "short_name": _short_name(ms) or "UNNAMED",
            "alignment": alignment,
            "symbol": symbol,
            "prefix_ref": prefix_ref,
            "sw_addr_method_ref": sw_addr_ref,
        })
    return sections


def _parse_section_name_prefixes(resource: ET.Element) -> List[Dict[str, Any]]:
    """Extract SECTION-NAME-PREFIXs from a RESOURCE-CONSUMPTION element."""
    prefixes: List[Dict[str, Any]] = []
    snp_parent = resource.find(f"{_NS}SECTION-NAME-PREFIXS")
    if snp_parent is None:
        return prefixes
    for snp in snp_parent.findall(f"{_NS}SECTION-NAME-PREFIX"):
        prefixes.append({
            "short_name": _short_name(snp) or "UNNAMED",
            "symbol": _text(snp.find(f"{_NS}SYMBOL")),
        })
    return prefixes


def _parse_bsw_module_description(
    element: ET.Element, parent_path: str,
) -> Dict[str, Any]:
    """Parse a BSW-MODULE-DESCRIPTION element."""
    name = _short_name(element) or "UNNAMED"
    path = f"{parent_path}/{name}"

    node: Dict[str, Any] = {
        "short_name": name,
        "path": path,
        "type": "BSW-MODULE-DESCRIPTION",
        "exclusive_areas": [],
        "metadata": {},
    }

    for ib in element.findall(f".//{_NS}BSW-INTERNAL-BEHAVIOR"):
        node["exclusive_areas"].extend(_parse_exclusive_areas(ib))

    return node


def _parse_bsw_implementation(
    element: ET.Element, parent_path: str,
) -> Dict[str, Any]:
    """Parse a BSW-IMPLEMENTATION element."""
    name = _short_name(element) or "UNNAMED"
    path = f"{parent_path}/{name}"

    node: Dict[str, Any] = {
        "short_name": name,
        "path": path,
        "type": "BSW-IMPLEMENTATION",
        "programming_language": _text(element.find(f"{_NS}PROGRAMMING-LANGUAGE")),
        "sw_version": _text(element.find(f"{_NS}SW-VERSION")),
        "vendor_id": _text(element.find(f"{_NS}VENDOR-ID")),
        "ar_release_version": _text(element.find(f"{_NS}AR-RELEASE-VERSION")),
        "memory_sections": [],
        "section_name_prefixes": [],
    }

    rc = element.find(f".//{_NS}RESOURCE-CONSUMPTION")
    if rc is not None:
        node["memory_sections"] = _parse_memory_sections(rc)
        node["section_name_prefixes"] = _parse_section_name_prefixes(rc)

    behavior_ref = _text(element.find(f"{_NS}BEHAVIOR-REF"))
    if behavior_ref:
        node["behavior_ref"] = behavior_ref

    vendor_ref = element.find(f".//{_NS}VENDOR-SPECIFIC-MODULE-DEF-REF")
    if vendor_ref is not None:
        node["vendor_module_def_ref"] = _text(vendor_ref)

    return node


# ===================================================================
# Package / module walking
# ===================================================================

def _walk_package(
    package: ET.Element,
    parent_path: str,
    modules: Dict[str, Any],
    all_cross_refs: List[Dict[str, Any]],
) -> None:
    """Recursively walk an AR-PACKAGE and populate *modules*."""
    pkg_name = _short_name(package) or "UNNAMED"
    pkg_path = f"{parent_path}/{pkg_name}"

    elements = package.find(f"{_NS}ELEMENTS")
    if elements is not None:
        for elem in elements:
            tag_local = elem.tag.replace(_NS, "")
            name = _short_name(elem) or "UNNAMED"
            elem_path = f"{pkg_path}/{name}"

            if tag_local == "ECUC-MODULE-CONFIGURATION-VALUES":
                mod = _parse_ecuc_module(elem, pkg_path, all_cross_refs)
                modules[name] = mod

            elif tag_local == "BSW-MODULE-DESCRIPTION":
                mod = _parse_bsw_module_description(elem, pkg_path)
                modules[f"{name}__bsw_desc"] = mod

            elif tag_local == "BSW-IMPLEMENTATION":
                mod = _parse_bsw_implementation(elem, pkg_path)
                modules[f"{name}__bsw_impl"] = mod

            else:
                # Generic element — store minimal info
                modules[f"{name}__{tag_local}"] = {
                    "short_name": name,
                    "path": elem_path,
                    "type": tag_local,
                }

    # Recurse into sub-packages
    sub_pkgs = package.find(f"{_NS}AR-PACKAGES")
    if sub_pkgs is not None:
        for sub in sub_pkgs.findall(f"{_NS}AR-PACKAGE"):
            _walk_package(sub, pkg_path, modules, all_cross_refs)


def _parse_ecuc_module(
    element: ET.Element,
    parent_path: str,
    all_cross_refs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Parse an ECUC-MODULE-CONFIGURATION-VALUES element."""
    name = _short_name(element) or "UNNAMED"
    path = f"{parent_path}/{name}"
    def_ref = _definition_ref(element)

    mod: Dict[str, Any] = {
        "short_name": name,
        "path": path,
        "definition_ref": def_ref,
        "type": "ECUC-MODULE-CONFIGURATION-VALUES",
        "containers": [],
        "metadata": {
            "implementation_variant": _text(
                element.find(f"{_NS}IMPLEMENTATION-CONFIG-VARIANT")
            ),
        },
    }

    if def_ref:
        all_cross_refs.append({
            "source_path": path,
            "target_ref": def_ref,
            "ref_type": "definition",
        })

    containers_elem = element.find(f"{_NS}CONTAINERS")
    if containers_elem is not None:
        for cv in containers_elem.findall(f"{_NS}ECUC-CONTAINER-VALUE"):
            container = _parse_container(cv, path)
            mod["containers"].append(container)
            _collect_cross_refs(container, all_cross_refs)

    return mod


# ===================================================================
# Cross-reference collection
# ===================================================================

def _collect_cross_refs(
    container: Dict[str, Any],
    refs: List[Dict[str, Any]],
) -> None:
    """Recursively collect all DEFINITION-REFs and VALUE-REFs."""
    src = container["path"]

    if container.get("definition_ref"):
        refs.append({
            "source_path": src,
            "target_ref": container["definition_ref"],
            "ref_type": "definition",
        })

    for p in container.get("parameters", []):
        if p.get("definition_ref"):
            refs.append({
                "source_path": src,
                "target_ref": p["definition_ref"],
                "ref_type": "param_definition",
            })

    for r in container.get("references", []):
        if r.get("definition_ref"):
            refs.append({
                "source_path": src,
                "target_ref": r["definition_ref"],
                "ref_type": "reference_definition",
            })
        if r.get("value_ref"):
            refs.append({
                "source_path": src,
                "target_ref": r["value_ref"],
                "ref_type": "reference_value",
            })

    for sub in container.get("sub_containers", []):
        _collect_cross_refs(sub, refs)


# ===================================================================
# Chunk generation
# ===================================================================

def _generate_chunks(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten the module tree into a list of self-contained text chunks."""
    chunks: List[Dict[str, Any]] = []

    for mod_key, mod in modules.items():
        mod_type = mod.get("type", "")

        # Module-level chunk
        chunks.append({
            "path": mod["path"],
            "type": mod_type,
            "content": _module_summary(mod),
            "parameters": None,
            "references": None,
        })

        if mod_type == "ECUC-MODULE-CONFIGURATION-VALUES":
            for c in mod.get("containers", []):
                _flatten_container_chunks(c, chunks)

        elif mod_type == "BSW-IMPLEMENTATION":
            for ms in mod.get("memory_sections", []):
                chunks.append({
                    "path": f"{mod['path']}/{ms['short_name']}",
                    "type": "MEMORY-SECTION",
                    "content": (
                        f"Memory section {ms['short_name']} | "
                        f"Alignment: {ms.get('alignment', 'N/A')} | "
                        f"Symbol: {ms.get('symbol', 'N/A')} | "
                        f"Addr method: {ms.get('sw_addr_method_ref', 'N/A')}"
                    ),
                    "parameters": None,
                    "references": None,
                })

        elif mod_type == "BSW-MODULE-DESCRIPTION":
            for ea in mod.get("exclusive_areas", []):
                chunks.append({
                    "path": f"{mod['path']}/{ea['short_name']}",
                    "type": "EXCLUSIVE-AREA",
                    "content": f"Exclusive area: {ea['short_name']}",
                    "parameters": None,
                    "references": None,
                })

    return chunks


def _module_summary(mod: Dict[str, Any]) -> str:
    """One-line textual summary for a module-level chunk."""
    parts = [f"{mod.get('type', 'MODULE')} {mod.get('short_name', '?')}"]
    meta = mod.get("metadata", {})
    if meta:
        for k, v in meta.items():
            if v:
                parts.append(f"{k}: {v}")
    d = mod.get("definition_ref")
    if d:
        parts.append(f"Defined by: {d}")
    return " | ".join(parts)


def _flatten_container_chunks(
    container: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> None:
    """Recursively flatten a container into text chunks."""
    # Build content string
    parts = [f"{container['type']} {container['short_name']}"]

    param_dict: Optional[Dict[str, str]] = None
    if container["parameters"]:
        param_dict = {}
        for p in container["parameters"]:
            label = p.get("name") or "?"
            ptype = p.get("param_type", "")
            val = p.get("value", "?")
            parts.append(f"{label} ({ptype}) = {val}")
            param_dict[label] = val

    if container.get("definition_ref"):
        parts.append(f"Defined by: {container['definition_ref']}")

    ref_list = container.get("references")
    ref_out = None
    if ref_list:
        ref_out = ref_list
        for r in ref_list:
            parts.append(f"Ref: {r.get('value_ref', r.get('definition_ref', '?'))}")

    chunks.append({
        "path": container["path"],
        "type": container["type"],
        "content": " | ".join(parts),
        "parameters": param_dict,
        "references": ref_out,
    })

    for sub in container.get("sub_containers", []):
        _flatten_container_chunks(sub, chunks)


# ===================================================================
# Statistics helpers
# ===================================================================

def _count_containers(containers: List[Dict[str, Any]]) -> int:
    total = len(containers)
    for c in containers:
        total += _count_containers(c.get("sub_containers", []))
    return total


def _count_parameters(containers: List[Dict[str, Any]]) -> int:
    total = sum(len(c.get("parameters", [])) for c in containers)
    for c in containers:
        total += _count_parameters(c.get("sub_containers", []))
    return total


def _count_references(containers: List[Dict[str, Any]]) -> int:
    total = sum(len(c.get("references", [])) for c in containers)
    for c in containers:
        total += _count_references(c.get("sub_containers", []))
    return total


# ===================================================================
# Public API
# ===================================================================

def parse(path: str, **kwargs: Any) -> Dict[str, Any]:
    """Parse an ARXML file into a hybrid tree + chunks structure.

    Args:
        path: Path to an ``.arxml`` file.

    Returns:
        A dict with keys ``file_path``, ``file_type``, ``is_template``,
        ``autosar_schema``, ``modules``, ``chunks``, ``cross_references``,
        and ``statistics``.

    Raises:
        FileNotFoundError: If *path* does not exist.
        xml.etree.ElementTree.ParseError: If the XML is malformed after
            template macro stripping.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    raw = p.read_text(encoding="utf-8")

    # --- Template pre-processing ---
    cleaned, is_template, macros_stripped = _strip_template_macros(raw)

    if is_template:
        logger.info(
            "Detected EB tresos template ARXML; stripped %d macro expressions",
            macros_stripped,
        )

    # --- XML parsing ---
    root = ET.fromstring(cleaned)

    # Detect schema version from namespace
    schema = "unknown"
    ns_match = re.search(r"http://autosar\.org/schema/(r[\d.]+)", root.tag)
    if ns_match:
        schema = ns_match.group(1)

    # --- Walk packages ---
    modules: Dict[str, Any] = {}
    cross_refs: List[Dict[str, Any]] = []

    ar_packages = root.find(f"{_NS}AR-PACKAGES")
    if ar_packages is not None:
        for pkg in ar_packages.findall(f"{_NS}AR-PACKAGE"):
            _walk_package(pkg, "", modules, cross_refs)

    # --- Generate chunks ---
    chunks = _generate_chunks(modules)

    # --- Statistics ---
    total_containers = 0
    total_params = 0
    total_refs_count = 0
    for mod in modules.values():
        cl = mod.get("containers", [])
        total_containers += _count_containers(cl)
        total_params += _count_parameters(cl)
        total_refs_count += _count_references(cl)
        total_containers += len(mod.get("memory_sections", []))
        total_containers += len(mod.get("exclusive_areas", []))

    return {
        "file_path": str(p),
        "file_type": "arxml",
        "is_template": is_template,
        "autosar_schema": schema,
        "modules": modules,
        "chunks": chunks,
        "cross_references": cross_refs,
        "statistics": {
            "total_modules": len(modules),
            "total_containers": total_containers,
            "total_parameters": total_params,
            "total_references": total_refs_count,
            "total_chunks": len(chunks),
            "template_macros_stripped": macros_stripped,
        },
    }

"""
KG Node Utilities — Sprint 8
===============================
Label-specific property maps and node serialization helpers for the
MCAL/iLLD Knowledge Graph. Extracted from graphrag_query.py so that
SearchService, RLMOrchestrator, and ContextBuilder all share the same
rich node formatting.

Three map families:
  _LABEL_NAME_PROPS   → which property holds the "display name"
  _LABEL_ID_PROPS     → which property is the unique-id for dedup
  _LABEL_DISPLAY_PROPS→ ordered (key, label) pairs for rich text
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .context_builder import ContextSlot

logger = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  KG Label → Property Maps                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

LABEL_NAME_PROPS: Dict[str, str] = {
    "SWUD_Function":            "function_name",
    "SWA_Function":             "function_name",
    "SWA_ConfigParam":          "param_name",
    "SWUD_DerivedConfigParam":  "param_name",
    "SWA_ArchitecturalDecision":"title",
    "SWUD_DesignDecision":      "title",
    "ProductRequirement":       "name",
    "StakeholderRequirement":   "name",
    "SWA_SwDependency":         "api_name",
    "SWA_DataType":             "type_name",
    "SWA_Macro":                "macro_name",
    "MCALModule":               "module_name",
    "Folder":                   "name",
    "TS_FunctionalTestCase":    "test_case_id",
    "TS_WCETTestCase":          "test_case_id",
    # Source code node types
    "SRC_Function":             "name",
    "SRC_SourceFile":           "file_name",
    "SRC_DataType":             "name",
    "SRC_Macro":                "name",
    "SRC_GlobalVariable":       "name",
    "SRC_LocalVariable":        "name",
    "SWA_SourceFile":           "file_name",
    # SFR (Special Function Register) node types
    "SFR_Register":             "name",
    "SFR_BitField":             "name",
    "SFR_BaseAddress":          "name",
    "SFR_File":                 "file_name",
}

LABEL_ID_PROPS: Dict[str, str] = {
    "SWUD_Function":            "function_name",
    "SWA_Function":             "function_name",
    "SWA_ConfigParam":          "param_name",
    "SWUD_DerivedConfigParam":  "param_name",
    "SWA_ArchitecturalDecision":"decision_id",
    "SWUD_DesignDecision":      "decision_id",
    "ProductRequirement":       "requirement_id",
    "StakeholderRequirement":   "requirement_id",
    "SWA_SwDependency":         "api_name",
    "SWA_DataType":             "type_name",
    "SWA_Macro":                "macro_name",
    "MCALModule":               "module_name",
    "Folder":                   "name",
    "TS_FunctionalTestCase":    "test_case_id",
    "TS_WCETTestCase":          "test_case_id",
    # Source code node types
    "SRC_Function":             "function_id",
    "SRC_SourceFile":           "file_id",
    "SRC_DataType":             "type_id",
    "SRC_Macro":                "macro_id",
    "SRC_GlobalVariable":       "variable_id",
    "SRC_LocalVariable":        "variable_id",
    "SWA_SourceFile":           "file_name",
    # SFR node types
    "SFR_Register":             "register_id",
    "SFR_BitField":             "bitfield_id",
    "SFR_BaseAddress":          "base_address_id",
    "SFR_File":                 "file_id",
}

LABEL_DISPLAY_PROPS: Dict[str, List[tuple]] = {
    "SWUD_Function": [
        ("function_name",  "Function"),
        ("sync_async",     "Sync/Async"),
        ("reentrancy",     "Reentrancy"),
        ("asil_level",     "ASIL Level"),
        ("return_type",    "Return Type"),
        ("service_id",     "Service ID"),
        ("file",           "Source File"),
        ("memory_section", "Memory Section"),
        ("error_handling", "Error Handling"),
        ("function_category", "Category"),
        ("prq_references", "Requirement Refs"),
        ("algorithm",      "Algorithm"),
        ("configuration_dependencies", "Config Dependencies"),
        ("syntax",         "Syntax"),
        ("design_decisions", "Design Decisions"),
    ],
    "SWA_ConfigParam": [
        ("param_name",     "Parameter"),
        ("param_type",     "Type"),
        ("default_value",  "Default Value"),
        ("range_values",   "Allowed Range"),
        ("parent_container", "Container"),
        ("scope",          "Scope"),
        ("origin",         "Origin"),
        ("value_config_class", "Config Class"),
        ("multiplicity",   "Multiplicity"),
        ("section_number", "Section"),
        ("prq_references", "Requirement Refs"),
        ("design_decisions", "Design Decisions"),
        ("post_build_variant_value", "Post-Build Variant"),
    ],
    "SWUD_DerivedConfigParam": [
        ("param_name",     "Parameter"),
        ("description",    "Description"),
        ("module",         "Module"),
    ],
    "SWA_ArchitecturalDecision": [
        ("decision_id",    "Decision ID"),
        ("title",          "Title"),
        ("description",    "Description"),
    ],
    "SWUD_DesignDecision": [
        ("decision_id",    "Decision ID"),
        ("title",          "Title"),
        ("description",    "Description"),
    ],
    "ProductRequirement": [
        ("requirement_id", "Requirement ID"),
        ("name",           "Title"),
        ("description",    "Description"),
        ("status",         "Status"),
    ],
    "StakeholderRequirement": [
        ("requirement_id", "Requirement ID"),
        ("name",           "Title"),
        ("description",    "Description"),
        ("status",         "Status"),
    ],
    "SWA_SwDependency": [
        ("api_name",       "Dependency"),
        ("description",    "Description"),
    ],
    "TS_FunctionalTestCase": [
        ("test_case_id",     "Test Case ID"),
        ("test_objective",   "Test Objective"),
        ("expected_results", "Expected Results"),
        ("preconditions",    "Preconditions"),
        ("test_method",      "Test Method"),
        ("priority",         "Priority"),
        ("asil_level",       "ASIL Level"),
        ("module",           "Module"),
        ("description",      "Description"),
    ],
    "TS_WCETTestCase": [
        ("test_case_id",     "Test Case ID"),
        ("test_objective",   "Test Objective"),
        ("expected_results", "Expected Results"),
        ("preconditions",    "Preconditions"),
        ("test_method",      "Test Method"),
        ("api_name",         "API Name"),
        ("wcet_budget",      "WCET Budget"),
        ("description",      "Description"),
    ],
    # Source code node types
    "SRC_Function": [
        ("name",              "Function Name"),
        ("return_type",       "Return Type"),
        ("parameters",        "Parameters"),
        ("signature",         "Signature"),
        ("sync_async",        "Sync/Async"),
        ("reentrancy",        "Reentrancy"),
        ("is_static",         "Static"),
        ("is_inline",         "Inline"),
        ("start_line",        "Start Line"),
        ("end_line",          "End Line"),
        ("compile_condition", "Compile Condition"),
        ("service_id",        "Service ID"),
        ("traceability_ids",  "Traceability IDs"),
        ("description",       "Description"),
    ],
    "SRC_SourceFile": [
        ("file_name",         "File Name"),
        ("relative_path",     "Path"),
        ("file_type",         "File Type"),
        ("subtree",           "Subtree"),
        ("line_count",        "Lines"),
        ("size_bytes",        "Size (bytes)"),
        ("includes",          "Includes"),
        ("traceability_ids",  "Traceability IDs"),
    ],
    "SRC_DataType": [
        ("name",              "Type Name"),
        ("kind",              "Kind"),
        ("members",           "Members"),
        ("base_type",         "Base Type"),
        ("description",       "Description"),
        ("traceability_ids",  "Traceability IDs"),
    ],
    "SRC_Macro": [
        ("name",              "Macro Name"),
        ("value",             "Value"),
        ("macro_category",    "Category"),
        ("description",       "Description"),
        ("traceability_ids",  "Traceability IDs"),
    ],
    "SRC_GlobalVariable": [
        ("name",              "Variable Name"),
        ("data_type",         "Data Type"),
        ("is_static",         "Static"),
        ("is_const",          "Const"),
        ("is_extern",         "Extern"),
        ("is_array",          "Array"),
        ("array_size",        "Array Size"),
        ("initializer",       "Initializer"),
        ("memory_section",    "Memory Section"),
        ("compile_condition", "Compile Condition"),
        ("description",       "Description"),
    ],
    "SWA_SourceFile": [
        ("file_name",         "File Name"),
        ("file_category",     "Category"),
        ("description",       "Description"),
        ("section_number",    "Section"),
        ("prq_references",    "Requirement Refs"),
    ],
    # SFR (Special Function Register) node types
    "SFR_Register": [
        ("name",              "Register Name"),
        ("description",       "Description"),
        ("struct_name",       "Struct Name"),
        ("device",            "Device"),
        ("version",           "Version"),
        ("module",            "Module"),
    ],
    "SFR_BitField": [
        ("name",              "Bitfield Name"),
        ("register_name",     "Register"),
        ("bits",              "Bit Range"),
        ("width",             "Width"),
        ("access",            "Access"),
        ("mask",              "Mask"),
        ("description",       "Description"),
        ("device",            "Device"),
    ],
    "SFR_BaseAddress": [
        ("name",              "Register Name"),
        ("address",           "Address"),
        ("address_type",      "Address Type"),
        ("description",       "Description"),
        ("device",            "Device"),
    ],
    "SFR_File": [
        ("file_name",         "File Name"),
        ("file_type",         "File Type"),
        ("device",            "Device"),
        ("version",           "Version"),
        ("register_count",    "Registers"),
        ("bitfield_count",    "Bitfields"),
        ("base_address_count","Base Addresses"),
    ],
}

# Compact serialization keys for aggregation queries
COMPACT_PROPS: Dict[str, List[str]] = {
    "SWUD_Function": [
        "function_name", "sync_async", "reentrancy", "asil_level",
        "return_type", "memory_section",
    ],
    "SWA_ConfigParam": [
        "param_name", "param_type", "default_value", "range_values",
        "parent_container", "scope",
    ],
    "SWA_SwDependency":            ["api_name", "description"],
    "ProductRequirement":          ["requirement_id", "name", "status"],
    "StakeholderRequirement":      ["requirement_id", "name", "status"],
    "SWA_ArchitecturalDecision":   ["decision_id", "title"],
    "SWUD_DesignDecision":         ["decision_id", "title"],
    "TS_FunctionalTestCase":       ["test_case_id", "test_objective", "priority", "asil_level"],
    "TS_WCETTestCase":             ["test_case_id", "test_objective", "wcet_budget"],
}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Source dataclass                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@dataclass
class Source:
    """A single retrieval source (vector or graph)."""
    origin: str                # "vector" | "graph" | "fused"
    score: float
    heading: str
    text: str
    collection: str = ""       # Qdrant collection
    node_label: str = ""       # Neo4j node label
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin,
            "score": round(self.score, 4),
            "heading": self.heading,
            "text": self.text[:500],
            "collection": self.collection,
            "node_label": self.node_label,
        }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Node helper functions                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def node_display_name(label: str, props: Dict[str, Any]) -> str:
    """Extract the human-readable display name for a KG node.

    Uses the label-specific property map so SWUD_Function returns
    ``function_name``, SWA_ConfigParam returns ``param_name``, etc.
    """
    prop_key = LABEL_NAME_PROPS.get(label, "name")
    name = str(props.get(prop_key, ""))
    if name:
        return name
    for fallback in ("name", "title", "function_name", "param_name"):
        val = props.get(fallback, "")
        if val:
            return str(val)
    return ""


def node_unique_id(label: str, props: Dict[str, Any]) -> str:
    """Return a deterministic dedup key for a KG node."""
    id_key = LABEL_ID_PROPS.get(label, "name")
    node_id = props.get(id_key, "")
    if node_id:
        return f"{label}::{node_id}"
    for fallback in ("jama_id", "requirement_id", "decision_id",
                     "feature_id", "name"):
        val = props.get(fallback, "")
        if val:
            return f"{label}::{val}"
    return f"{label}::{hashlib.md5(str(sorted(props.items())).encode()).hexdigest()[:12]}"


def serialize_node(label: str, props: Dict[str, Any]) -> str:
    """Serialize all relevant node properties into a rich text block.

    Uses :data:`LABEL_DISPLAY_PROPS` to emit structured key/value pairs
    so the LLM receives the full metadata (ASIL level, sync/async, etc.).
    """
    display_props = LABEL_DISPLAY_PROPS.get(label, [])
    parts: List[str] = [f"[{label}]"]

    if display_props:
        for prop_key, display_label in display_props:
            val = props.get(prop_key, "")
            if val:
                val_str = str(val)
                if len(val_str) > 800:
                    val_str = val_str[:800] + "…"
                parts.append(f"  {display_label}: {val_str}")
    else:
        for k, v in props.items():
            if isinstance(v, (str, int, float, bool)) and v:
                v_str = str(v)
                if len(v_str) > 500:
                    v_str = v_str[:500] + "…"
                parts.append(f"  {k}: {v_str}")

    desc = str(props.get("description", ""))
    if desc and ("description", "Description") not in display_props:
        parts.append(f"  Description: {desc[:800]}")

    return "\n".join(parts)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Search helper functions                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def extract_keywords(question: str) -> List[str]:
    """Extract meaningful keywords from a question.

    Prioritises specific identifiers (CamelCase, underscore tokens)
    over generic English words.
    """
    stop_words = {
        "how", "does", "what", "is", "the", "a", "an", "in", "of",
        "to", "for", "and", "or", "are", "with", "from", "by",
        "on", "at", "this", "that", "it", "its", "be", "can",
        "do", "which", "when", "where", "why", "has", "have",
        "will", "would", "should", "could", "about", "into",
        "describe", "explain", "tell", "me", "show", "list",
        "give", "provide", "all", "each", "every", "any",
    }
    words = question.lower().split()
    keywords = [w.strip("?.,!:;\"'") for w in words
                if w.strip("?.,!:;\"'") not in stop_words]
    keywords = [w for w in keywords if len(w) >= 3]

    def _is_identifier(w: str) -> bool:
        return ("_" in w or any(c.isupper() for c in w[1:])
                or any(c.isdigit() for c in w) or len(w) > 12)

    raw_idents = re.findall(r'[A-Za-z][A-Za-z0-9_]{5,}', question)
    raw_idents = [r for r in raw_idents if r.lower() not in stop_words]

    identifiers = [w for w in raw_idents if _is_identifier(w)]
    generic = [w for w in keywords if not _is_identifier(w)]

    merged: List[str] = []
    seen: Set[str] = set()
    for w in identifiers + generic:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            merged.append(w)

    if len(generic) > 1:
        phrase = " ".join(generic[:3])
        merged.append(phrase)

    return merged[:7]


# ── Cached ontology label index (built once per profile) ──────────────────
_label_index: Dict[str, List[tuple]] = {}  # profile → [(label, name_tokens, desc_tokens)]


def _tokenize_camel_underscore(name: str) -> Set[str]:
    """Split CamelCase / underscore identifiers into lowercase tokens.

    ``SWA_ConfigParam`` → {"swa", "config", "param"}
    ``APIFunction``     → {"api", "function"}
    """
    # split on underscores first
    parts = name.replace("_", " ").split()
    tokens: Set[str] = set()
    for part in parts:
        # split CamelCase
        sub = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", part)
        for t in sub.lower().split():
            if len(t) >= 2:
                tokens.add(t)
    return tokens


def _build_label_index(profile: str) -> List[tuple]:
    """Build ``[(label, name_tokens, desc_tokens), ...]`` from ontology."""
    try:
        from src.MemoryLayer.memory.ontology_loader import get_ontology
        ontology = get_ontology()
        node_types = ontology.get_node_types(profile)
    except Exception:
        logger.warning("Could not load ontology node types for profile=%s", profile)
        return []

    entries: List[tuple] = []
    for nt in node_types:
        label = nt["name"]
        name_tokens = _tokenize_camel_underscore(label)
        desc = nt.get("description", "")
        desc_tokens = {w for w in re.findall(r"[a-z]{3,}", desc.lower())}
        entries.append((label, name_tokens, desc_tokens))
    return entries


def _get_label_index(profile: str) -> List[tuple]:
    """Return cached label index, building it on first call."""
    if profile not in _label_index:
        _label_index[profile] = _build_label_index(profile)
    return _label_index[profile]


def _stem(word: str) -> str:
    """Minimal suffix stripping for matching (function/functions/functional)."""
    w = word.lower()
    for suffix in ("ations", "ation", "ional", "ment", "ness",
                    "ting", "ing", "ions", "ion", "ous", "ive",
                    "ies", "es", "ed", "ly", "al", "er", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _stem_set(tokens: Set[str]) -> Set[str]:
    """Return stems for a set of tokens."""
    return {_stem(t) for t in tokens}


def infer_labels(question: str, profile: str = "mcal") -> List[str]:
    """Infer which Neo4j node labels to search based on question content.

    Uses node type **names** and **descriptions** from ``ontology.yaml``
    to score each label against the query.  No extra YAML fields needed —
    adding a new node type to the ontology automatically makes it
    discoverable by the search pipeline.

    Parameters
    ----------
    question : str
        The natural-language search query.
    profile : str
        ``"mcal"`` or ``"illd"`` (must match a profile in ontology.yaml).
    """
    index = _get_label_index(profile)
    if not index:
        return []

    q_lower = question.lower()
    q_tokens = {w for w in re.findall(r"[a-z]{3,}", q_lower)}
    q_stems = _stem_set(q_tokens)

    scored: List[tuple] = []
    for label, name_tok, desc_tok in index:
        score = 0.0
        # Exact substring of the label name in the query  (strongest signal)
        if label.lower() in q_lower:
            score += 5.0
        # Token overlap with the label name  (stem-aware)
        name_stems = _stem_set(name_tok)
        name_overlap = q_stems & name_stems
        score += len(name_overlap) * 2.0
        # Token overlap with the description  (stem-aware)
        desc_stems = _stem_set(desc_tok)
        desc_overlap = q_stems & desc_stems
        score += len(desc_overlap) * 0.5
        if score > 0:
            scored.append((label, score))

    # Sort by score descending, then alphabetically for ties
    scored.sort(key=lambda x: (-x[1], x[0]))

    # Take top labels; always include at least the first 3 node types as defaults
    top = [lbl for lbl, _ in scored[:7]]
    seen: Set[str] = set(top)
    defaults = [entry[0] for entry in index[:3]]
    for d in defaults:
        if d not in seen:
            seen.add(d)
            top.append(d)
    return top[:10]


def score_node(keyword: str, name: str, description: str) -> float:
    """Score a graph node based on keyword match quality.

    * 2.0  – keyword matches the whole name (case-insensitive)
    * 1.0  – keyword is a substring of the name
    * 0.6  – keyword is a substring of the description
    * 0.3  – partial token overlap with name
    * 0.2  – fallback
    """
    kw = keyword.lower()
    name_lower = name.lower()

    if kw == name_lower:
        return 2.0
    if kw in name_lower:
        return 1.0
    if kw in description.lower():
        return 0.6

    kw_tokens = set(kw.split())
    name_tokens = set(name_lower.split())
    overlap = kw_tokens & name_tokens
    if overlap:
        return 0.3 + 0.05 * len(overlap)

    return 0.2


def normalise_scores(sources: List[Source]) -> List[Source]:
    """Normalise scores to [0, 1]."""
    if not sources:
        return sources
    max_score = max(s.score for s in sources)
    if max_score <= 0:
        return sources
    for s in sources:
        s.score = s.score / max_score
    return sources


def extract_named_entities(question: str, module: str = "") -> List[str]:
    """Extract specific named entities (function names, config params, PRQ IDs)
    from the question using pattern matching.

    Recognises:
      - CamelCase identifiers (``Adc_Init``, ``AdcDevErrorDetect``)
      - Underscore-separated tokens (``ADC_E_ALREADY_INITIALIZED``)
      - Short CamelCase (``DeInit``, ``InitCheck`` — 2+ humps)
      - PRQ / requirement IDs (``PRQ-42633``)
    """
    entities: List[str] = []
    seen: Set[str] = set()

    # CamelCase or underscore identifiers
    for m in re.finditer(r'\b([A-Z][a-zA-Z0-9]*(?:_[A-Za-z0-9]+)+)\b', question):
        ent = m.group(1)
        if ent.lower() not in seen:
            seen.add(ent.lower())
            entities.append(ent)

    # CamelCase without underscores (>= 5 chars, 2+ humps)
    for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]*)+)\b', question):
        ent = m.group(1)
        if len(ent) >= 5 and ent.lower() not in seen:
            seen.add(ent.lower())
            entities.append(ent)

    # PRQ / requirement IDs
    for m in re.finditer(r'\b((?:AU3GM-)?PRQ-\d+)\b', question):
        ent = m.group(1)
        if ent.lower() not in seen:
            seen.add(ent.lower())
            entities.append(ent)

    # Module-prefix expansion: "Init" → "Adc_Init" when module=ADC
    if module:
        prefix = module.capitalize()
        expanded: List[str] = []
        for ent in entities:
            prefixed = f"{prefix}_{ent}"
            if prefixed.lower() not in seen:
                seen.add(prefixed.lower())
                expanded.append(prefixed)
        entities.extend(expanded)

    return entities


def is_aggregation_query(question: str) -> bool:
    """Detect if a question is an aggregation query that needs complete
    enumeration rather than top-k similarity results."""
    q = question.lower()
    agg_patterns = [
        "list all", "list the", "list every",
        "what are the", "what are all",
        "how many", "count of", "count the", "total number",
        "which functions", "which test", "which config",
        "which requirement", "which dependenc",
        "for each one", "for each function", "for each param",
        "for each test", "for each requirement",
        "all the ", "every ", "enumerate", "show all",
        "find all", "get all", "give me all", "return all",
        "complete list", "full list", "exhaustive",
        r"all .* functions", r"all .* parameters", r"all .* test",
        r"all .* requirement", r"all .* dependenc", r"all .* decision",
    ]
    for pat in agg_patterns:
        if re.search(pat, q):
            return True
    return False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Context-slot classification                                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def classify_source(src: Source) -> str:
    """Map a source to a ContextSlot based on node_label and metadata."""
    label = src.node_label.lower() if src.node_label else ""

    if label:
        if "src_function" in label:
            return ContextSlot.CODE_EXAMPLES
        if "src_sourcefile" in label or "swa_sourcefile" in label:
            return ContextSlot.CODE_EXAMPLES
        if "src_datatype" in label or "src_macro" in label:
            return ContextSlot.CODE_EXAMPLES
        if "src_globalvariable" in label or "src_localvariable" in label:
            return ContextSlot.CODE_EXAMPLES
        if "sfr_" in label:
            return ContextSlot.REGISTERS
        if "function" in label:
            return ContextSlot.API_FUNCTIONS
        if "testcase" in label or "test" in label:
            return ContextSlot.TESTS
        if "config" in label or "derived" in label:
            return ContextSlot.API_FUNCTIONS
        if "requirement" in label:
            return ContextSlot.REQUIREMENTS
        if "decision" in label or "architectural" in label:
            return ContextSlot.DEPENDENCIES
        if "safety" in label:
            return ContextSlot.SAFETY
        if "dependency" in label or "swdependency" in label:
            return ContextSlot.DEPENDENCIES
        if "module" in label or "folder" in label:
            return ContextSlot.RELATIONSHIPS

    heading_lower = src.heading.lower()
    meta = src.metadata
    doc_type = str(meta.get("type", "")).lower()

    if "interface" in heading_lower or "exported" in heading_lower:
        return ContextSlot.API_FUNCTIONS
    if "safety" in heading_lower:
        return ContextSlot.SAFETY
    if "config" in heading_lower:
        return ContextSlot.API_FUNCTIONS
    if "architectural" in doc_type or "decision" in doc_type:
        return ContextSlot.DEPENDENCIES
    if "safety" in doc_type:
        return ContextSlot.SAFETY
    if "config" in doc_type:
        return ContextSlot.API_FUNCTIONS
    if "requirement" in doc_type:
        return ContextSlot.REQUIREMENTS

    return ContextSlot.CUSTOM


def format_source_for_context(src: Source) -> str:
    """Format a source for inclusion in the LLM context."""
    parts: List[str] = []
    tag = f"[{src.origin.upper()}]"

    if src.heading:
        parts.append(f"{tag} {src.heading}")

    if src.origin == "graph" and src.node_label:
        prq = src.metadata.get("prq_references", "")
        if prq:
            parts.append(f"Requirement refs: {prq}")
        dd = src.metadata.get("design_decisions", "")
        if dd:
            parts.append(f"Design decisions: {dd}")
    else:
        for meta_key, display_label in [
            ("tags", "Traceability"), ("functions", "Functions"),
            ("jama_refs", "Jama refs"), ("section_number", "Section"),
            ("module", "Module"), ("source_document", "Source"),
        ]:
            val = src.metadata.get(meta_key, "")
            if val:
                parts.append(f"{display_label}: {val}")

    text = src.text.strip()
    if len(text) > 2000:
        text = text[:2000] + "\n... (truncated)"
    parts.append(text)

    return "\n".join(parts)

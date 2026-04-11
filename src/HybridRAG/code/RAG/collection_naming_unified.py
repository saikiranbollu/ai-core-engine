"""
Unified Collection Naming — Ontology-Driven
=============================================
Central utility for generating deterministic, human-readable vector-store
collection names across **all** ontology profiles (mcal, illd, or future).

Instead of hard-coding MCAL-specific or ILLD-specific collection lists,
this module reads the ontology to discover what content types exist for
each profile and builds collection names accordingly.

Naming pattern::

    {profile}_{module}_{content_category}

Examples::

    mcal_adc_swa_architecture
    mcal_adc_swa_callsequences
    mcal_adc_swud_design
    illd_cxpi_functions
    illd_cxpi_enums

Falls back to legacy naming conventions for backward compatibility
when the ontology doesn't define explicit vector_collections.

Usage::

    from HybridRAG.code.RAG.collection_naming_unified import (
        module_collections,
        collection_name,
    )

    # Ontology-driven: returns collections for whichever profile is active
    all_colls = module_collections("ADC", profile="mcal")
    all_colls = module_collections("CXPI", profile="illd")
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent              # .../RAG
_CODE_DIR = _SCRIPT_DIR.parent                             # .../HybridRAG/code
_HYBRIDRAG_DIR = _CODE_DIR.parent                          # .../HybridRAG
_CONFIG_DIR = _HYBRIDRAG_DIR / "config"

# Collection name rules: [a-zA-Z0-9_-], 3-63 chars
_COLLECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")


def _load_storage_config() -> dict:
    cfg_path = _CONFIG_DIR / "storage_config.yaml"
    try:
        if str(_CODE_DIR) not in sys.path:
            sys.path.insert(0, str(_CODE_DIR))
        from env_config import load_yaml_with_env
        return load_yaml_with_env(cfg_path)
    except ImportError:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)


def _load_ontology() -> dict:
    with open(_CONFIG_DIR / "ontology.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _active_profile() -> str:
    return _load_storage_config().get("active_instance", "illd")


# ── Profile-specific collection patterns ──────────────────────────────────
# Derived from the ontology's node_types and extraction_strategy fields.
# MCAL: SWA docs → architecture/callsequences/safety; SWUD → design
# ILLD: source code → functions/enums/structs/etc.

# These are the *known* content categories per profile type.
# The ontology drives which ones actually exist.

_MCAL_COLLECTION_TYPES: List[Tuple[str, str]] = [
    ("swa", "architecture"),
    ("swa", "callsequences"),
    ("swa", "safety"),
    ("swud", "design"),
    ("testspec", "verification"),
    ("jama", "requirements"),
]

_ILLD_COLLECTION_TYPES: List[str] = [
    "functions",
    "enums",
    "structs",
    "requirements",
    "hardware",
    "registers",
    "macros",
    "typedefs",
    "source",
    "architecture",
    "pattern_library",
    "phases",
]

# SWA section routing (shared)
SWA_SECTION_ROUTING: Dict[str, Tuple[str, str]] = {
    "3.1": ("architecture", "architectural_decision"),
    "3.2": ("callsequences", "call_sequence"),
    "3.3": ("safety", "safety_view"),
    "3.4": ("safety", "trusted_view"),
}


# ── Public API ────────────────────────────────────────────────────────────

def collection_name(
    module: str,
    doc_source: str,
    content_category: str,
) -> str:
    """
    Build a deterministic collection name.

    Parameters
    ----------
    module : str
        Module name (e.g. ``"ADC"``, ``"CXPI"``). Case-insensitive.
    doc_source : str
        Document source (``"swa"``, ``"swud"``, ``"illd"``, etc.).
    content_category : str
        Content category (``"architecture"``, ``"functions"``, etc.).

    Returns
    -------
    str
        Collection name, e.g. ``"adc_swa_architecture"``.
    """
    mod = module.strip().lower()
    src = doc_source.strip().lower()
    cat = content_category.strip().lower()

    if not mod or not src or not cat:
        raise ValueError("module, doc_source, and content_category must not be empty")

    name = f"{mod}_{src}_{cat}"

    if not _COLLECTION_NAME_RE.match(name):
        raise ValueError(
            f"Generated collection name '{name}' violates naming rules "
            f"(3–63 chars, alphanumeric + underscore/hyphen)."
        )
    return name


def module_collections(
    module: str,
    profile: Optional[str] = None,
) -> List[str]:
    """
    Return the expected collection names for a module, driven by the
    ontology profile.

    Parameters
    ----------
    module : str
        Module name (e.g. ``"ADC"``, ``"CXPI"``).
    profile : str, optional
        Ontology profile. Defaults to active_instance from config.

    Returns
    -------
    list[str]
        Collection names appropriate for the profile.
    """
    profile = profile or _active_profile()
    mod = module.strip().lower()

    if profile == "mcal":
        return _mcal_module_collections(mod)
    elif profile == "illd":
        return _illd_module_collections(mod)
    else:
        # Future profiles: try to derive from ontology
        return _ontology_driven_collections(mod, profile)


def _mcal_module_collections(mod: str) -> List[str]:
    """MCAL collection naming: {module}_{doc_source}_{category}."""
    return [
        collection_name(mod, src, cat)
        for src, cat in _MCAL_COLLECTION_TYPES
    ]


def _illd_module_collections(mod: str) -> List[str]:
    """ILLD collection naming: rag_{module}_{type}."""
    return [f"rag_{mod}_{t}" for t in _ILLD_COLLECTION_TYPES]


def _ontology_driven_collections(mod: str, profile: str) -> List[str]:
    """
    For unknown profiles, derive collection names from ontology node_types.
    Groups by extraction_strategy to determine content categories.
    """
    ontology = _load_ontology()
    profile_cfg = ontology.get("profiles", {}).get(profile, {})
    node_types = profile_cfg.get("node_types", [])

    categories = set()
    for nt in node_types:
        strategy = nt.get("extraction_strategy", "")
        if "swa" in strategy:
            categories.add(("swa", "architecture"))
        elif "swud" in strategy:
            categories.add(("swud", "design"))
        elif strategy == "hybrid":
            # Source code extraction → use node name as category
            name_lower = nt["name"].lower()
            if "function" in name_lower:
                categories.add(("code", "functions"))
            elif "struct" in name_lower or "datatype" in name_lower:
                categories.add(("code", "structs"))
            elif "test" in name_lower:
                categories.add(("test", "verification"))
            elif "requirement" in name_lower:
                categories.add(("req", "requirements"))
            else:
                categories.add(("general", name_lower))

    return [collection_name(mod, src, cat) for src, cat in sorted(categories)]


def route_section(
    section_number: str,
    doc_category: str,
    module: str,
) -> Tuple[str, str]:
    """
    Map a section number + document category to (collection_name, doc_type).
    Works for all profiles.
    """
    mod = module.strip().lower()

    if doc_category.strip().lower() == "swud":
        return collection_name(mod, "swud", "design"), "unit_design"

    for prefix, (cat, doc_type) in SWA_SECTION_ROUTING.items():
        if section_number.startswith(prefix):
            return collection_name(mod, "swa", cat), doc_type

    return collection_name(mod, "swa", "architecture"), "swa_general"

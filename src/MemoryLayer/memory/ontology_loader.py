"""
OntologyLoader
==============
Single source of truth for everything defined in ontology.yaml.

ALL other modules in the Memory Layer import from here — never read
the YAML themselves.  This means if the ontology changes, you update
the YAML file, and every module picks up the changes automatically.

What it does:
  - Loads and parses ontology.yaml
  - Provides typed accessors for profiles, node types, relationships,
    validation rules, extraction patterns, and query templates
  - Resolves which properties are required / unique per node type
  - Resolves which properties hold embeddings (vector fields)
  - Is a singleton — loaded once, cached for the lifetime of the process

Usage:
    from memory.ontology_loader import OntologyLoader

    loader = OntologyLoader()                        # uses default path
    # or
    loader = OntologyLoader("/path/to/ontology.yaml")

    profile  = loader.get_profile("illd")
    nodes    = loader.get_node_types("illd")
    rels     = loader.get_relationships("illd")
    patterns = loader.get_extraction_patterns("illd")
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Canonical location: ai-core-engine/src/HybridRAG/config/ontology.yaml
_MEMORY_LAYER_ROOT = Path(__file__).resolve().parent.parent          # .../MemoryLayer/
_DEFAULT_ONTOLOGY_PATH = _MEMORY_LAYER_ROOT.parent / "HybridRAG" / "config" / "ontology.yaml"

class OntologyLoader:
    """
    Loads and exposes ontology.yaml in a structured, typed way.

    Parameters
    ----------
    ontology_path : str or Path, optional
        Path to the ontology YAML file.
        Defaults to ai-core-engine/Src/HybridRAG/config/ontology.yaml
    """

    def __init__(self, ontology_path: Optional[str] = None):
        path = Path(ontology_path) if ontology_path else _DEFAULT_ONTOLOGY_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"[OntologyLoader] ontology.yaml not found at: {path}\n"
                f"Expected canonical location: {_DEFAULT_ONTOLOGY_PATH}"
            )
        with open(path, "r", encoding="utf-8") as fh:
            self._raw: Dict[str, Any] = yaml.safe_load(fh)
        logger.info(f"[OntologyLoader] Loaded ontology v{self.version} from {path}")

    # ─────────────────────────────────────────────────────────────────────────
    # TOP-LEVEL METADATA
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def version(self) -> str:
        return self._raw.get("metadata", {}).get("version", "unknown")

    @property
    def domain(self) -> str:
        return self._raw.get("metadata", {}).get("domain", "unknown")

    @property
    def available_profiles(self) -> List[str]:
        """Return list of profile names defined in ontology.yaml."""
        return list(self._raw.get("profiles", {}).keys())

    # ─────────────────────────────────────────────────────────────────────────
    # PROFILE ACCESS
    # ─────────────────────────────────────────────────────────────────────────

    def get_profile(self, profile_name: str) -> Dict[str, Any]:
        """Return the full profile dict for the given profile name."""
        profiles = self._raw.get("profiles", {})
        if profile_name not in profiles:
            raise ValueError(
                f"[OntologyLoader] Unknown profile '{profile_name}'. "
                f"Available: {list(profiles.keys())}"
            )
        return profiles[profile_name]

    def get_profile_metadata(self, profile_name: str) -> Dict[str, Any]:
        """Return the metadata block for a profile."""
        return self.get_profile(profile_name).get("metadata", {})

    def get_supported_modules(self, profile_name: str) -> List[str]:
        """Return the list of supported module names for a profile."""
        return self.get_profile_metadata(profile_name).get("supported_modules", [])

    # ─────────────────────────────────────────────────────────────────────────
    # NODE TYPES
    # ─────────────────────────────────────────────────────────────────────────

    def get_node_types(self, profile_name: str) -> List[Dict[str, Any]]:
        """Return the full list of node type definitions for a profile."""
        return self.get_profile(profile_name).get("node_types", [])

    def get_node_type_names(self, profile_name: str) -> List[str]:
        """Return just the node label names (e.g. ['StakeholderRequirement', ...])."""
        return [n["name"] for n in self.get_node_types(profile_name)]

    def get_node_type(self, profile_name: str, type_name: str) -> Optional[Dict[str, Any]]:
        """Return a single node type definition by label name, or None."""
        for nt in self.get_node_types(profile_name):
            if nt["name"] == type_name:
                return nt
        return None

    def get_node_properties(self, profile_name: str, type_name: str) -> List[Dict[str, Any]]:
        """Return the property list for a specific node type."""
        nt = self.get_node_type(profile_name, type_name)
        return nt.get("properties", []) if nt else []

    def get_required_properties(self, profile_name: str, type_name: str) -> List[str]:
        """Return property names that are marked required=true."""
        return [
            p["name"]
            for p in self.get_node_properties(profile_name, type_name)
            if p.get("required", False)
        ]

    def get_unique_properties(self, profile_name: str, type_name: str) -> List[str]:
        """Return property names that are marked unique=true."""
        return [
            p["name"]
            for p in self.get_node_properties(profile_name, type_name)
            if p.get("unique", False)
        ]

    def get_embedding_property(self, profile_name: str, type_name: str) -> Optional[str]:
        """
        Return the name of the vector/embedding property for a node type,
        or None if the type has no embedding field.
        """
        for p in self.get_node_properties(profile_name, type_name):
            if p.get("data_type") == "vector":
                return p["name"]
        return None

    def get_embedding_dimensions(self, profile_name: str, type_name: str) -> int:
        """Return the declared embedding dimension for a node type (default 384)."""
        for p in self.get_node_properties(profile_name, type_name):
            if p.get("data_type") == "vector":
                return p.get("dimensions", 384)
        return 384

    def get_embeddable_node_types(self, profile_name: str) -> List[str]:
        """Return names of all node types that have an embedding property."""
        return [
            nt["name"]
            for nt in self.get_node_types(profile_name)
            if self.get_embedding_property(profile_name, nt["name"]) is not None
        ]

    def get_text_properties(self, profile_name: str, type_name: str) -> List[str]:
        """Return property names whose data_type is 'text' or 'string'."""
        return [
            p["name"]
            for p in self.get_node_properties(profile_name, type_name)
            if p.get("data_type") in ("text", "string")
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # RELATIONSHIPS
    # ─────────────────────────────────────────────────────────────────────────

    def get_relationships(self, profile_name: str) -> List[Dict[str, Any]]:
        """Return the full list of relationship type definitions."""
        return self.get_profile(profile_name).get("relationship_types", [])

    def get_relationship_names(self, profile_name: str) -> List[str]:
        """Return just the relationship type names."""
        return [r["name"] for r in self.get_relationships(profile_name)]

    def get_relationship(self, profile_name: str, rel_name: str) -> Optional[Dict[str, Any]]:
        """Return a single relationship definition by name, or None."""
        for rel in self.get_relationships(profile_name):
            if rel["name"] == rel_name:
                return rel
        return None

    def get_outgoing_relationships(self, profile_name: str, node_type: str) -> List[Dict[str, Any]]:
        """Return all relationships where node_type appears in from_types."""
        return [
            rel for rel in self.get_relationships(profile_name)
            if node_type in rel.get("from_types", [])
        ]

    def get_incoming_relationships(self, profile_name: str, node_type: str) -> List[Dict[str, Any]]:
        """Return all relationships where node_type appears in to_types."""
        return [
            rel for rel in self.get_relationships(profile_name)
            if node_type in rel.get("to_types", [])
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # EXTRACTION PATTERNS & RULES
    # ─────────────────────────────────────────────────────────────────────────

    def get_extraction_patterns(self, profile_name: str) -> Dict[str, str]:
        """Return extraction regex patterns defined in the profile."""
        return self.get_profile(profile_name).get("extraction_patterns", {})

    def get_extraction_rules(self, profile_name: str) -> Dict[str, Any]:
        """Return extraction rules for a profile (e.g. module_prefix_from_name)."""
        return self.get_profile(profile_name).get("extraction_rules", {})

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDATION RULES
    # ─────────────────────────────────────────────────────────────────────────

    def get_validation_rules(self, profile_name: str) -> Dict[str, Any]:
        """Return the validation rules block for a profile."""
        return self.get_profile(profile_name).get("validation_rules", {})

    def get_allowed_values(self, profile_name: str, type_name: str, prop_name: str) -> Optional[List[Any]]:
        """
        Return the allowed_values list for a specific property if defined,
        or None if no constraint exists.
        """
        for p in self.get_node_properties(profile_name, type_name):
            if p["name"] == prop_name:
                return (
                    p.get("validation_rules", {}).get("allowed_values")
                )
        return None

    def get_value_map(self, profile_name: str, type_name: str, prop_name: str) -> Dict[Any, str]:
        """
        Return the value_map dict for a property (e.g. {293: 'Approved', ...}).
        Returns empty dict if none defined.
        """
        for p in self.get_node_properties(profile_name, type_name):
            if p["name"] == prop_name:
                return p.get("value_map", {})
        return {}

    # ─────────────────────────────────────────────────────────────────────────
    # QUERY TEMPLATES
    # ─────────────────────────────────────────────────────────────────────────

    def get_query_templates(self, profile_name: str) -> Dict[str, Any]:
        """Return the Cypher query templates defined in the profile."""
        return self.get_profile(profile_name).get("query_templates", {})

    def get_query_template(self, profile_name: str, template_name: str) -> Optional[str]:
        """Return a specific Cypher query template string, or None."""
        templates = self.get_query_templates(profile_name)
        entry = templates.get(template_name)
        if entry is None:
            return None
        # Some templates are dicts with 'cypher' key, some are plain strings
        if isinstance(entry, dict):
            return entry.get("cypher")
        return str(entry)

    # ─────────────────────────────────────────────────────────────────────────
    # CONVENIENCE: QDRANT COLLECTION SCHEMA
    # ─────────────────────────────────────────────────────────────────────────

    def get_qdrant_data_types(self, profile_name: str) -> List[str]:
        """
        Return the snake_case data_type payload values for Qdrant,
        derived from node type names that have embedding fields.

        e.g. APIFunction → 'api_function', SoftwareRequirement → 'software_requirement'
        """
        import re
        result = []
        for name in self.get_embeddable_node_types(profile_name):
            # Convert CamelCase → snake_case
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
            result.append(snake)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # FULL SCHEMA SUMMARY
    # ─────────────────────────────────────────────────────────────────────────

    def schema_summary(self, profile_name: str) -> Dict[str, Any]:
        """
        Return a compact summary of the profile schema.
        Useful for logging / debugging.
        """
        node_types  = self.get_node_type_names(profile_name)
        rel_names   = self.get_relationship_names(profile_name)
        embeddable  = self.get_embeddable_node_types(profile_name)
        return {
            "profile":          profile_name,
            "node_types":       node_types,
            "node_type_count":  len(node_types),
            "relationships":    rel_names,
            "relationship_count": len(rel_names),
            "embeddable_types": embeddable,
            "supported_modules": self.get_supported_modules(profile_name),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Process-level singleton — avoids re-reading disk on every import
# ─────────────────────────────────────────────────────────────────────────────

_singleton: Optional[OntologyLoader] = None


def get_ontology(ontology_path: Optional[str] = None) -> OntologyLoader:
    """
    Return the process-level OntologyLoader singleton.
    First call reads the YAML; subsequent calls return the cached instance.

    Usage:
        from memory.ontology_loader import get_ontology
        ontology = get_ontology()
        nodes = ontology.get_node_type_names("illd")
    """
    global _singleton
    if _singleton is None:
        _singleton = OntologyLoader(ontology_path)
    return _singleton

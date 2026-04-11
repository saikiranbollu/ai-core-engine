"""
Unified KG Querier — Ontology-Driven
======================================
Single entry-point for Knowledge Graph queries across all ontology profiles
(mcal, illd, or any future profile).  Reads the active profile from
``storage_config.yaml`` (or accepts an explicit override) and delegates
to the core ``KnowledgeGraphQuerier`` engine.

The querier inspects the ontology to determine:
  - Which node types exist for the profile
  - Which properties to search across (keyword_search)
  - Which relationship types are valid

Usage::

    from HybridRAG.code.KG.kg_query import UnifiedKGQuerier

    # Auto-detect profile from storage_config.yaml
    kg = UnifiedKGQuerier(module="ADC")

    # Explicit profile
    kg = UnifiedKGQuerier(profile="mcal", module="ADC")

    results = kg.keyword_search("Adc_Init", limit=10)
    deps = kg.get_function_dependencies("Adc_Init")
    kg.close()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent              # .../KG
_CODE_DIR = _SCRIPT_DIR.parent                             # .../HybridRAG/code
_HYBRIDRAG_DIR = _CODE_DIR.parent                          # .../HybridRAG
_CONFIG_DIR = _HYBRIDRAG_DIR / "config"

for p in (_CODE_DIR, _SCRIPT_DIR / "mcal"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load_active_profile() -> str:
    """Read the active_instance from storage_config.yaml."""
    cfg_path = _CONFIG_DIR / "storage_config.yaml"
    try:
        from env_config import load_yaml_with_env
        cfg = load_yaml_with_env(cfg_path)
    except ImportError:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    return cfg.get("active_instance", "illd")


def _load_ontology() -> dict:
    """Load the unified ontology."""
    with open(_CONFIG_DIR / "ontology.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_searchable_properties(ontology: dict, profile: str) -> List[str]:
    """Extract text-searchable property names from the ontology profile."""
    profiles = ontology.get("profiles", {})
    profile_cfg = profiles.get(profile, {})
    node_types = profile_cfg.get("node_types", [])

    searchable = set()
    for nt in node_types:
        for prop in nt.get("properties", []):
            dt = prop.get("data_type", "")
            if dt in ("string", "text") and prop.get("name"):
                searchable.add(prop["name"])
    return sorted(searchable)


class UnifiedKGQuerier:
    """
    Ontology-driven Knowledge Graph querier.

    Automatically loads the correct profile configuration and provides
    uniform search/query methods regardless of whether the underlying
    graph is MCAL, ILLD, or any future profile.

    Parameters
    ----------
    profile : str, optional
        Ontology profile (``"mcal"`` or ``"illd"``).
        Defaults to ``active_instance`` from storage_config.yaml.
    module : str, optional
        Module name for filtering (e.g. ``"ADC"``, ``"CXPI"``).
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        module: Optional[str] = None,
    ):
        self.profile = profile or _load_active_profile()
        self.module = (module or "").upper()
        self._ontology = _load_ontology()
        self._profile_cfg = self._ontology.get("profiles", {}).get(self.profile, {})
        self._node_types = self._profile_cfg.get("node_types", [])
        self._searchable_props = _get_searchable_properties(self._ontology, self.profile)
        self._querier = None

    def _get_querier(self):
        """Lazy-init the core KnowledgeGraphQuerier."""
        if self._querier is None:
            from KG.query_knowledge_graph import KnowledgeGraphQuerier
            self._querier = KnowledgeGraphQuerier(profile=self.profile)
            self._querier.connect()
        return self._querier

    # -- Ontology-driven keyword search ------------------------------------

    def keyword_search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Full-text keyword search across all node types defined in the
        ontology profile. Dynamically builds CONTAINS clauses from the
        ontology's text/string properties.

        Returns a list of dicts with: name, label, description, score, module.
        """
        querier = self._get_querier()

        # Build UNION ALL across node labels defined in ontology
        union_parts = []
        node_labels_with_scores = self._get_node_labels_with_search_scores()

        for label, base_score, name_prop, desc_prop in node_labels_with_scores:
            where_clauses = []
            if name_prop:
                where_clauses.append(f"toLower(coalesce(n.{name_prop}, '')) CONTAINS toLower($kw)")
            if desc_prop:
                where_clauses.append(f"toLower(coalesce(n.{desc_prop}, '')) CONTAINS toLower($kw)")
            if not where_clauses:
                continue
            where = " OR ".join(where_clauses)

            name_expr = f"n.{name_prop}" if name_prop else "n.name"
            desc_expr = f"n.{desc_prop}" if desc_prop else "''"
            module_expr = "coalesce(n.module, n.module_name, n.module_prefix, '')"

            union_parts.append(
                f"MATCH (n:{label}) WHERE {where} "
                f"RETURN coalesce({name_expr}, '') AS name, "
                f"'{label}' AS label, "
                f"coalesce({desc_expr}, '') AS description, "
                f"{module_expr} AS module, "
                f"{base_score} AS score "
                f"LIMIT $lim"
            )

        if not union_parts:
            return []

        query = " UNION ALL ".join(union_parts)
        full_query = (
            f"CALL {{ {query} }} "
            f"RETURN name, label, description, module, score "
            f"ORDER BY score DESC LIMIT $lim"
        )

        return querier.run(full_query, {"kw": keyword, "lim": limit})

    def _get_node_labels_with_search_scores(self) -> List[tuple]:
        """
        Derive (label, base_score, name_property, description_property) tuples
        from the ontology. Higher scores for primary content nodes.
        """
        results = []
        for nt in self._node_types:
            label = nt["name"]
            props = {p["name"]: p for p in nt.get("properties", [])}

            # Find the best "name" property
            name_prop = None
            for candidate in ("function_name", "name", "type_name", "macro_name",
                              "param_name", "api_name", "title", "test_case_id",
                              "requirement_id", "document_name"):
                if candidate in props:
                    name_prop = candidate
                    break

            # Find the best "description" property
            desc_prop = None
            for candidate in ("description", "brief", "purpose", "test_objective",
                              "detailed_description"):
                if candidate in props:
                    desc_prop = candidate
                    break

            if not name_prop and not desc_prop:
                continue

            # Assign relevance scores based on node type characteristics
            if "Function" in label or "API" in label:
                score = 1.0
            elif "Requirement" in label:
                score = 0.9
            elif "DataType" in label or "DataStructure" in label or "Struct" in label:
                score = 0.8
            elif "Test" in label:
                score = 0.75
            elif "Decision" in label or "Architecture" in label:
                score = 0.7
            else:
                score = 0.6

            results.append((label, score, name_prop, desc_prop))
        return results

    # -- Function dependency query (works for both profiles) ----------------

    def get_function_dependencies(self, func_name: str) -> List[Dict[str, Any]]:
        """
        Get functions that *func_name* depends on. Works across profiles
        by querying common dependency relationship types.
        """
        querier = self._get_querier()
        query = """
            MATCH (f)
            WHERE f.name = $func_name OR f.function_name = $func_name
            MATCH (f)-[rel:DEPENDS_ON|CALLS_INTERNALLY|CALLS|SW_DEPENDENCY]->(dep)
            RETURN coalesce(dep.name, dep.function_name, dep.api_name) AS dependency,
                   type(rel) AS relationship,
                   coalesce(dep.return_type, '') AS return_type
            ORDER BY dependency
        """
        return querier.run(query, {"func_name": func_name})

    # -- Generic query pass-through ----------------------------------------

    def query(self, cypher: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Run an arbitrary Cypher query."""
        return self._get_querier().run(cypher, parameters or {})

    # -- Schema introspection (ontology-aware) -----------------------------

    def get_schema(self) -> Dict[str, Any]:
        """Return ontology-aware schema summary."""
        return self._get_querier().get_schema_summary()

    def get_node_types(self) -> List[str]:
        """Return node type labels defined in the ontology profile."""
        return [nt["name"] for nt in self._node_types]

    def get_relationship_types(self) -> List[str]:
        """Return relationship type names from the ontology profile."""
        return [
            rt["name"]
            for rt in self._profile_cfg.get("relationship_types", [])
        ]

    # -- Search nodes (delegates to core querier) --------------------------

    def search_nodes(
        self,
        label: str,
        keyword: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 25,
    ) -> List[dict]:
        """Search nodes by label with optional keyword/filters."""
        return self._get_querier().search_nodes(
            label, keyword=keyword, filters=filters, limit=limit,
        )

    # -- Traceability (delegates to core querier) --------------------------

    def trace_requirement(self, requirement_id: str, depth: int = 4) -> dict:
        """Full V-model traceability for a requirement."""
        return self._get_querier().trace_requirement(requirement_id, depth=depth)

    # -- Lifecycle ---------------------------------------------------------

    def close(self):
        """Close the underlying Neo4j connection."""
        if self._querier:
            self._querier.close()
            self._querier = None

"""
Knowledge Graph Querier for Automotive Embedded Software Ontology
=================================================================

Ontology-aware query engine for the Neo4j knowledge graph built by
``build_knowledge_graph.py``.  Supports schema introspection, node
search, V-model traceability, module-level analytics, relationship
traversal, and coverage / status statistics.

Can be used as a library or from the command line.

Usage (CLI – interactive):
    python query_knowledge_graph.py                          # default profile
    python query_knowledge_graph.py --profile mcal           # explicit profile

Usage (CLI – single query):
    python query_knowledge_graph.py --profile mcal schema
    python query_knowledge_graph.py --profile mcal stats
    python query_knowledge_graph.py --profile mcal search --label ProductRequirement --keyword "Adc"
    python query_knowledge_graph.py --profile mcal trace --id AU3GM-PRQ-29969
    python query_knowledge_graph.py --profile mcal module --name ADC
    python query_knowledge_graph.py --profile mcal neighbors --jama-id 12345
    python query_knowledge_graph.py --profile mcal cypher "MATCH (n) RETURN labels(n), count(*)"

Usage (library):
    from query_knowledge_graph import KnowledgeGraphQuerier

    querier = KnowledgeGraphQuerier(profile="mcal")
    querier.connect()
    results = querier.search_nodes("ProductRequirement", keyword="Adc", limit=10)
    trace   = querier.trace_requirement("AU3GM-PRQ-29969")
    querier.close()
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

# ---------------------------------------------------------------------------
# Paths  (file lives at HybridRAG/code/KG/)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG
CONFIG_DIR = HYBRIDRAG_DIR / "config"
ONTOLOGY_PATH = CONFIG_DIR / "ontology.yaml"
STORAGE_CONFIG_PATH = CONFIG_DIR / "storage_config.yaml"

# Ensure the code dir is on sys.path so sibling modules (env_config, etc.) resolve
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("kg_querier")


LABEL_ALIASES: Dict[str, List[str]] = {
    "APIFunction": ["APIFunction", "Function"],
    "Function": ["Function", "APIFunction"],
    "DataStructure": ["DataStructure", "Struct"],
    "Struct": ["Struct", "DataStructure"],
    "BitField": ["BitField", "RegisterField"],
    "RegisterField": ["RegisterField", "BitField"],
    "SoftwareRequirement": ["SoftwareRequirement", "ProductRequirement", "Requirement"],
    "ProductRequirement": ["ProductRequirement", "SoftwareRequirement", "Requirement"],
    "Requirement": ["Requirement", "SoftwareRequirement", "ProductRequirement"],
}

REQUIREMENT_LABEL_ALIASES = LABEL_ALIASES["ProductRequirement"]


# ---------------------------------------------------------------------------
# Configuration Helpers
# ---------------------------------------------------------------------------
def load_ontology(path: Path = ONTOLOGY_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_storage_config(path: Path = STORAGE_CONFIG_PATH) -> dict:
    from env_config import load_yaml_with_env
    return load_yaml_with_env(path)


def get_neo4j_settings(profile: str, storage_cfg: dict) -> dict:
    neo4j_section = storage_cfg.get("neo4j", {})
    if profile not in neo4j_section:
        raise ValueError(
            f"No Neo4j config for profile '{profile}'. "
            f"Available: {list(neo4j_section.keys())}"
        )
    return neo4j_section[profile]


# ---------------------------------------------------------------------------
# Ontology Helpers
# ---------------------------------------------------------------------------
def get_profile_config(ontology: dict, profile: str) -> dict:
    profiles = ontology.get("profiles", {})
    if profile not in profiles:
        raise ValueError(
            f"Unknown profile '{profile}'. Available: {list(profiles.keys())}"
        )
    return profiles[profile]


def _unique_id_property(node_type: dict) -> Optional[str]:
    """Return the first property marked ``unique: true``."""
    for p in node_type.get("properties", []):
        if p.get("unique"):
            return p["name"]
    return None


def _value_map_for(node_type: dict, prop_name: str) -> dict:
    """Return the value_map dict for a property, or empty dict."""
    for p in node_type.get("properties", []):
        if p["name"] == prop_name:
            return p.get("value_map", {})
    return {}


# ---------------------------------------------------------------------------
# KnowledgeGraphQuerier
# ---------------------------------------------------------------------------
class KnowledgeGraphQuerier:
    """
    Ontology-aware query engine for the automotive knowledge graph.

    Provides high-level query methods that understand the domain schema
    (node types, relationship types, value maps) so callers don't need
    to write raw Cypher for common tasks.

    Parameters
    ----------
    profile : str
        Ontology profile key (``mcal`` or ``illd``).
    ontology : dict, optional
        Pre-loaded ontology dict.  Loaded from disk if *None*.
    storage_cfg : dict, optional
        Pre-loaded storage config dict.  Loaded from disk if *None*.
    """

    def __init__(
        self,
        profile: str,
        ontology: Optional[dict] = None,
        storage_cfg: Optional[dict] = None,
    ):
        self.profile = profile
        self.ontology = ontology or load_ontology()
        self.storage_cfg = storage_cfg or load_storage_config()

        self.profile_cfg = get_profile_config(self.ontology, profile)
        self.neo4j_cfg = get_neo4j_settings(profile, self.storage_cfg)

        # Ontology lookups
        self.node_types: List[dict] = self.profile_cfg.get("node_types", [])
        self.relationship_types: List[dict] = self.profile_cfg.get(
            "relationship_types", []
        )

        # Quick-access maps
        self._label_to_nt: Dict[str, dict] = {
            nt["name"]: nt for nt in self.node_types
        }
        self._rel_names: List[str] = [
            rt["name"] for rt in self.relationship_types
        ]

        self._driver = None

    def _resolve_labels(self, label: str) -> List[str]:
        return LABEL_ALIASES.get(label, [label])

    @staticmethod
    def _label_match(alias: str, labels_param: str) -> str:
        return f"any(lbl IN labels({alias}) WHERE lbl IN ${labels_param})"

    # -- Connection ---------------------------------------------------------

    def connect(self) -> "KnowledgeGraphQuerier":
        """Establish the Neo4j driver connection."""
        cfg = self.neo4j_cfg
        logger.info("Connecting to Neo4j at %s …", cfg["uri"])
        try:
            uri = cfg["uri"]
            driver_kwargs = {
                "auth": (cfg["username"], cfg["password"]),
                "max_connection_lifetime": cfg.get("max_connection_lifetime", 3600),
                "max_connection_pool_size": cfg.get("max_connection_pool_size", 50),
            }
            # encrypted param is only valid for plain bolt:// or neo4j:// schemes
            # bolt+ssc / bolt+s / neo4j+ssc / neo4j+s handle encryption via URI scheme
            if not any(scheme in uri for scheme in ["+ssc", "+s"]):
                driver_kwargs["encrypted"] = cfg.get("encrypted", False)
            self._driver = GraphDatabase.driver(uri, **driver_kwargs)
            self._driver.verify_connectivity()
        except (ServiceUnavailable, AuthError, OSError) as exc:
            logger.error("Could not connect to Neo4j: %s", exc)
            raise
        logger.info("Connected (database: %s)", cfg["database"])
        return self

    def close(self) -> None:
        """Close the Neo4j driver."""
        if self._driver:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> "KnowledgeGraphQuerier":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Low-level Cypher execution -----------------------------------------

    def run(
        self,
        cypher: str,
        parameters: Optional[dict] = None,
    ) -> List[dict]:
        """Execute a Cypher query and return a list of record dicts."""
        if self._driver is None:
            raise RuntimeError("Not connected – call connect() first.")
        db = self.neo4j_cfg["database"]
        with self._driver.session(database=db) as session:
            result = session.run(cypher, parameters or {})
            return [rec.data() for rec in result]

    # ===================================================================
    # 1. SCHEMA INTROSPECTION
    # ===================================================================

    def get_schema_summary(self) -> dict:
        """
        Return a combined view of the ontology schema and the live
        database state (node/relationship counts per label).
        """
        # Live counts
        label_counts = {}
        try:
            rows = self.run(
                "MATCH (n) "
                "UNWIND labels(n) AS lbl "
                "RETURN lbl, count(*) AS cnt "
                "ORDER BY cnt DESC"
            )
            label_counts = {r["lbl"]: r["cnt"] for r in rows}
        except Exception as exc:
            logger.warning("Could not fetch label counts: %s", exc)

        rel_counts = {}
        try:
            rows = self.run(
                "MATCH ()-[r]->() "
                "RETURN type(r) AS rel, count(*) AS cnt "
                "ORDER BY cnt DESC"
            )
            rel_counts = {r["rel"]: r["cnt"] for r in rows}
        except Exception as exc:
            logger.warning("Could not fetch relationship counts: %s", exc)

        return {
            "profile": self.profile,
            "ontology_node_types": [
                {
                    "label": nt["name"],
                    "unique_key": _unique_id_property(nt),
                    "properties": [p["name"] for p in nt.get("properties", [])],
                    "db_count": label_counts.get(nt["name"], 0),
                }
                for nt in self.node_types
            ],
            "ontology_relationship_types": [
                {
                    "name": rt["name"],
                    "from": rt.get("from_types", []),
                    "to": rt.get("to_types", []),
                    "source": rt.get("extraction_source", ""),
                    "db_count": rel_counts.get(rt["name"], 0),
                }
                for rt in self.relationship_types
            ],
            "db_label_counts": label_counts,
            "db_relationship_counts": rel_counts,
        }

    def get_node_type_info(self, label: str) -> Optional[dict]:
        """Return the ontology definition for a node label."""
        for candidate in self._resolve_labels(label):
            node_type = self._label_to_nt.get(candidate)
            if node_type:
                return node_type
        return None

    def list_labels(self) -> List[str]:
        """Return live labels present in the database."""
        rows = self.run(
            "CALL db.labels() YIELD label RETURN label ORDER BY label"
        )
        return [r["label"] for r in rows]

    def list_relationship_types_live(self) -> List[str]:
        """Return live relationship types present in the database."""
        rows = self.run(
            "CALL db.relationshipTypes() YIELD relationshipType "
            "RETURN relationshipType ORDER BY relationshipType"
        )
        return [r["relationshipType"] for r in rows]

    # ===================================================================
    # 2. NODE SEARCH
    # ===================================================================

    def search_nodes(
        self,
        label: str,
        keyword: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        return_properties: Optional[List[str]] = None,
        limit: int = 25,
    ) -> List[dict]:
        """
        Search for nodes by label with optional keyword and property filters.

        Parameters
        ----------
        label : str
            Neo4j node label (e.g. ``ProductRequirement``).
        keyword : str, optional
            Case-insensitive substring match against ``name`` and
            ``description`` properties.
        filters : dict, optional
            Exact-match property filters (e.g. ``{"status": "Approved"}``).
        return_properties : list[str], optional
            Properties to include in results.  If *None*, returns all.
        limit : int
            Maximum results.
        """
        where_clauses: List[str] = []
        params: dict = {"limit": limit}

        if keyword:
            where_clauses.append(
                "("
                "toLower(coalesce(n.name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.description, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.function_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.param_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.title, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.requirement_id, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.decision_id, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.feature_id, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.test_case_id, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.test_objective, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.config_path, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.api_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.document_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.device, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.file_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.struct_name, '')) CONTAINS toLower($keyword) "
                "OR toLower(coalesce(n.register_name, '')) CONTAINS toLower($keyword)"
                ")"
            )
            params["keyword"] = keyword

        if filters:
            for i, (key, val) in enumerate(filters.items()):
                pname = f"fval_{i}"
                # Allow nodes that lack the property (IS NULL) to pass
                # through — prevents excluding untagged nodes.
                where_clauses.append(
                    f"(n.{key} IS NULL OR n.{key} = ${pname})"
                )
                params[pname] = val

        where = " AND ".join(where_clauses)
        where_stmt = f"WHERE {where}" if where else ""

        if return_properties:
            props_expr = ", ".join(f"n.{p} AS {p}" for p in return_properties)
            ret = props_expr
        else:
            ret = "n"

        labels = self._resolve_labels(label)
        params["labels"] = labels
        cypher = (
            f"MATCH (n) WHERE {self._label_match('n', 'labels')}"
            f" {'AND ' + where if where else ''} "
            f"RETURN {ret} "
            f"LIMIT $limit"
        )
        return self.run(cypher, params)

    def find_by_id(
        self,
        requirement_id: str,
        label: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Find a single node by its unique requirement / document key
        (e.g. ``AU3GM-PRQ-29969``).

        Searches across all node types unless *label* is specified.
        """
        if label:
            for candidate in self._resolve_labels(label):
                uid = _unique_id_property(self._label_to_nt.get(candidate, {}))
                if not uid:
                    continue
                rows = self.run(
                    f"MATCH (n:{candidate} {{{uid}: $rid}}) RETURN n LIMIT 1",
                    {"rid": requirement_id},
                )
                if rows:
                    return rows[0]
            return None

        # Search across all node types
        for nt in self.node_types:
            uid = _unique_id_property(nt)
            if not uid:
                continue
            rows = self.run(
                f"MATCH (n:{nt['name']} {{{uid}: $rid}}) RETURN n LIMIT 1",
                {"rid": requirement_id},
            )
            if rows:
                return rows[0]
        return None

    def find_by_jama_id(self, jama_id: int) -> Optional[dict]:
        """Find a node by its numeric Jama ID (searches all labels)."""
        rows = self.run(
            "MATCH (n {jama_id: $jid}) "
            "RETURN n, labels(n) AS labels LIMIT 1",
            {"jid": jama_id},
        )
        return rows[0] if rows else None

    def find_by_global_id(self, global_id: str) -> Optional[dict]:
        """Find a single node by its globally unique ``global_id``."""
        rows = self.run(
            "MATCH (n {global_id: $gid}) "
            "RETURN n, labels(n) AS labels LIMIT 1",
            {"gid": global_id},
        )
        return rows[0] if rows else None

    def fulltext_search(
        self,
        text: str,
        labels: Optional[List[str]] = None,
        limit: int = 25,
    ) -> List[dict]:
        """
        Case-insensitive substring search across ``name`` and
        ``description`` for the given labels (or all if *None*).
        """
        if labels:
            union_parts = []
            for lbl in labels:
                union_parts.append(
                    f"MATCH (n:{lbl}) "
                    f"WHERE toLower(n.name) CONTAINS toLower($text) "
                    f"   OR toLower(n.description) CONTAINS toLower($text) "
                    f"RETURN n, labels(n) AS labels"
                )
            cypher = " UNION ".join(union_parts) + " LIMIT $limit"
        else:
            cypher = (
                "MATCH (n) "
                "WHERE toLower(n.name) CONTAINS toLower($text) "
                "   OR toLower(n.description) CONTAINS toLower($text) "
                "RETURN n, labels(n) AS labels "
                "LIMIT $limit"
            )
        return self.run(cypher, {"text": text, "limit": limit})

    # ===================================================================
    # 3. V-MODEL TRACEABILITY
    # ===================================================================

    def trace_requirement(
        self,
        requirement_id: str,
        depth: int = 4,
    ) -> dict:
        """
        Full V-model traceability for a requirement.

        Starting from the given requirement ID (PRQ or SHRQ key), traverses
        the graph in both directions to build a traceability chain:

            Stakeholder → SHRQ → PRQ → PVS → PVR
                                  ↓
                         ArchitecturalAssumption

        Returns a dict with:
            - ``origin``: the starting node
            - ``upstream``: nodes reachable by following relationships *to*
              the origin (e.g. SHRQ → PRQ means PRQ has upstream SHRQs)
            - ``downstream``: nodes reachable *from* the origin
            - ``paths``: raw path data for further processing
        """
        node = self.find_by_id(requirement_id)
        if not node:
            return {"error": f"Node '{requirement_id}' not found."}

        # Upstream (inbound relationships reaching this node)
        upstream = self.run(
            "MATCH (n {jama_id: $jid})<-[r*1..$depth]-(m) "
            "RETURN m, labels(m) AS labels, "
            "  [rel IN r | type(rel)] AS rel_types "
            "LIMIT 100",
            {"jid": node["n"]["jama_id"], "depth": depth},
        )

        # Downstream (outbound relationships from this node)
        downstream = self.run(
            "MATCH (n {jama_id: $jid})-[r*1..$depth]->(m) "
            "RETURN m, labels(m) AS labels, "
            "  [rel IN r | type(rel)] AS rel_types "
            "LIMIT 100",
            {"jid": node["n"]["jama_id"], "depth": depth},
        )

        return {
            "origin": node,
            "upstream": upstream,
            "downstream": downstream,
        }

    def get_traceability_chain(
        self,
        requirement_id: str,
    ) -> dict:
        """
        Build a structured ASPICE V-model traceability chain for a
        single requirement (PRQ or SHRQ).

        Returns categorised connected nodes:
            - ``stakeholder_requirements``: upstream SHRQs
            - ``product_requirements``: derived PRQs
            - ``verification_steps``: linked PVS
            - ``verification_reports``: linked PVR
            - ``assumptions``: linked AAs
            - ``module``: parent MCAL module
            - ``folder``: parent folder
            - ``release``: targeted release
        """
        node = self.find_by_id(requirement_id)
        if not node:
            return {"error": f"Node '{requirement_id}' not found."}

        jid = node["n"]["jama_id"]

        def _fetch(rel: str, direction: str = "out") -> List[dict]:
            if direction == "out":
                cypher = (
                    f"MATCH (n {{jama_id: $jid}})-[:{rel}]->(m) "
                    f"RETURN m, labels(m) AS labels"
                )
            else:
                cypher = (
                    f"MATCH (n {{jama_id: $jid}})<-[:{rel}]-(m) "
                    f"RETURN m, labels(m) AS labels"
                )
            return self.run(cypher, {"jid": jid})

        return {
            "origin": node,
            "stakeholder_requirements": _fetch("DERIVES_FROM", "out"),
            "product_requirements": _fetch("DERIVES_FROM", "in"),
            "verification_steps": _fetch("VERIFIED_BY", "out"),
            "verification_reports": _fetch("HAS_RESULT", "out"),
            "assumptions": _fetch("ASSUMES", "out"),
            "module": _fetch("BELONGS_TO_MODULE", "out"),
            "folder": _fetch("BELONGS_TO_FOLDER", "out"),
            "release": _fetch("TARGETED_FOR", "out"),
            "raised_by": _fetch("RAISED_BY", "out"),
            "sourced_from": _fetch("SOURCED_FROM", "out"),
        }

    def find_orphan_requirements(
        self,
        label: str = "ProductRequirement",
        relationship: str = "DERIVES_FROM",
    ) -> List[dict]:
        """
        Find requirements that have *no* upstream traceability link.

        For PRQs, this means no ``DERIVES_FROM`` to any SHRQ.
        Useful for ASPICE compliance auditing.
        """
        labels = self._resolve_labels(label)
        cypher = (
            f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
            f"AND NOT (n)-[:{relationship}]->() "
            f"RETURN n ORDER BY n.name"
        )
        return self.run(cypher, {"labels": labels})

    def find_unverified_requirements(
        self,
        label: str = "ProductRequirement",
    ) -> List[dict]:
        """Find requirements with no VERIFIED_BY link to any PVS."""
        labels = self._resolve_labels(label)
        cypher = (
            f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
            f"AND NOT (n)-[:VERIFIED_BY]->() "
            f"RETURN n ORDER BY n.name"
        )
        return self.run(cypher, {"labels": labels})

    # ===================================================================
    # 4. MODULE-LEVEL QUERIES
    # ===================================================================

    def list_modules(self) -> List[dict]:
        """List modules using either explicit module nodes or per-node module properties."""
        cypher = (
            "MATCH (n) "
            "WHERE coalesce(n.module_name, n.module, n.module_prefix) IS NOT NULL "
            "WITH toUpper(coalesce(n.module_name, n.module, n.module_prefix)) AS module, n "
            "RETURN module, "
            "  count(n) AS total_items, "
            "  count(CASE WHEN any(lbl IN labels(n) WHERE lbl IN $requirement_labels) THEN 1 END) AS prq_count, "
            "  count(CASE WHEN 'StakeholderRequirement' IN labels(n) THEN 1 END) AS shrq_count, "
            "  count(CASE WHEN 'VerificationStep' IN labels(n) THEN 1 END) AS pvs_count "
            "ORDER BY module"
        )
        return self.run(cypher, {"requirement_labels": REQUIREMENT_LABEL_ALIASES})

    def get_module_details(
        self,
        module_name: str,
        include_items: bool = False,
        limit: int = 50,
    ) -> dict:
        """
        Return detailed information for a single MCAL module.

        Parameters
        ----------
        module_name : str
            Module name (case-insensitive, e.g. ``Adc``, ``ADC``).
        include_items : bool
            If *True*, include lists of individual requirement nodes.
        limit : int
            Max items to return per category when *include_items* is True.
        """
        mod_upper = module_name.upper()

        mod_rows = self.run(
            "MATCH (m) "
            "WHERE m:MCALModule AND toUpper(coalesce(m.module_name, m.name, '')) = $name "
            "RETURN m",
            {"name": mod_upper},
        )

        # Counts by label
        counts = self.run(
            "MATCH (n) "
            "WHERE toUpper(coalesce(n.module, n.module_name, n.module_prefix, '')) = $name "
            "UNWIND labels(n) AS lbl "
            "RETURN lbl, count(*) AS cnt ORDER BY cnt DESC",
            {"name": mod_upper},
        )
        if not counts:
            return {"error": f"Module '{module_name}' not found."}

        # Status distribution for PRQs
        status_dist = self.run(
            "MATCH (n) "
            "WHERE toUpper(coalesce(n.module, n.module_name, n.module_prefix, '')) = $name "
            "AND any(lbl IN labels(n) WHERE lbl IN $requirement_labels) "
            "RETURN n.status AS status, count(*) AS cnt "
            "ORDER BY cnt DESC",
            {"name": mod_upper, "requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )

        # Category distribution for PRQs
        category_dist = self.run(
            "MATCH (n) "
            "WHERE toUpper(coalesce(n.module, n.module_name, n.module_prefix, '')) = $name "
            "AND any(lbl IN labels(n) WHERE lbl IN $requirement_labels) "
            "RETURN n.requirement_category AS category, count(*) AS cnt "
            "ORDER BY cnt DESC",
            {"name": mod_upper, "requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )

        result: dict = {
            "module": mod_rows[0]["m"] if mod_rows else {"module_name": mod_upper},
            "item_counts": {r["lbl"]: r["cnt"] for r in counts},
            "prq_status_distribution": status_dist,
            "prq_category_distribution": category_dist,
        }

        if include_items:
            for lbl in [
                "ProductRequirement",
                "StakeholderRequirement",
                "VerificationStep",
            ]:
                labels = self._resolve_labels(lbl)
                items = self.run(
                    f"MATCH (n) "
                    f"WHERE toUpper(coalesce(n.module, n.module_name, n.module_prefix, '')) = $name "
                    f"AND {self._label_match('n', 'labels')} "
                    f"RETURN n ORDER BY n.name LIMIT $limit",
                    {"name": mod_upper, "limit": limit, "labels": labels},
                )
                result[f"{lbl}_items"] = items

        return result

    def get_module_folder_tree(self, module_name: str) -> List[dict]:
        """
        Return the folder hierarchy for a module as a flat list with
        depth/parent information.
        """
        mod_upper = module_name.upper()
        cypher = (
            "MATCH (f:Folder) "
            "WHERE toLower(f.name) = toLower($name) "
            "   OR f.name = $upper "
            "WITH f "
            "MATCH path = (leaf:Folder)-[:CHILD_OF*0..5]->(f) "
            "RETURN leaf.name AS folder, leaf.jama_id AS jama_id, "
            "  leaf.folder_level AS level, "
            "  leaf.requirement_category AS category, "
            "  length(path) AS depth "
            "ORDER BY depth, folder"
        )
        return self.run(cypher, {"name": module_name, "upper": mod_upper})

    # ===================================================================
    # 5. RELATIONSHIP / NEIGHBOR QUERIES
    # ===================================================================

    def get_neighbors(
        self,
        jama_id: Optional[int] = None,
        global_id: Optional[str] = None,
        direction: str = "both",
        rel_types: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[dict]:
        """
        Return all nodes connected to a seed node.

        The seed node can be identified by *jama_id* or *global_id*.

        Parameters
        ----------
        jama_id : int, optional
            Numeric Jama ID of the seed node.
        global_id : str, optional
            Universal ``global_id`` of the seed node (preferred).
        direction : str
            ``in``, ``out``, or ``both``.
        rel_types : list[str], optional
            Filter to specific relationship types.
        limit : int
            Max results.
        """
        rel_filter = ""
        if rel_types:
            rel_filter = ":" + "|".join(rel_types)

        # Build the node match clause based on available identifier
        if global_id:
            node_match = "{global_id: $seed_id}"
            seed_val = global_id
        elif jama_id is not None:
            node_match = "{jama_id: $seed_id}"
            seed_val = jama_id
        else:
            raise ValueError("Provide jama_id or global_id.")

        if direction == "out":
            pattern = f"(n {node_match})-[r{rel_filter}]->(m)"
        elif direction == "in":
            pattern = f"(n {node_match})<-[r{rel_filter}]-(m)"
        else:
            pattern = f"(n {node_match})-[r{rel_filter}]-(m)"

        cypher = (
            f"MATCH {pattern} "
            f"RETURN m, labels(m) AS labels, type(r) AS rel_type, "
            f"  startNode(r) = n AS is_outgoing "
            f"LIMIT $limit"
        )
        return self.run(cypher, {"seed_id": seed_val, "limit": limit})

    def shortest_path(
        self,
        from_jama_id: Optional[int] = None,
        to_jama_id: Optional[int] = None,
        from_global_id: Optional[str] = None,
        to_global_id: Optional[str] = None,
        max_depth: int = 10,
    ) -> List[dict]:
        """Find the shortest path between two nodes by jama_id or global_id."""
        # Resolve 'from' node match
        if from_global_id:
            from_match = "{global_id: $from_id}"
            from_val = from_global_id
        elif from_jama_id is not None:
            from_match = "{jama_id: $from_id}"
            from_val = from_jama_id
        else:
            raise ValueError("Provide from_jama_id or from_global_id.")

        # Resolve 'to' node match
        if to_global_id:
            to_match = "{global_id: $to_id}"
            to_val = to_global_id
        elif to_jama_id is not None:
            to_match = "{jama_id: $to_id}"
            to_val = to_jama_id
        else:
            raise ValueError("Provide to_jama_id or to_global_id.")

        cypher = (
            f"MATCH (a {from_match}), (b {to_match}), "
            f"path = shortestPath((a)-[*..{max_depth}]-(b)) "
            "RETURN [n IN nodes(path) | {global_id: n.global_id, jama_id: n.jama_id, name: n.name, "
            "  labels: labels(n)}] AS nodes, "
            "[r IN relationships(path) | {type: type(r), "
            "  from: startNode(r).global_id, to: endNode(r).global_id}] AS rels"
        )
        return self.run(
            cypher, {"from_id": from_val, "to_id": to_val}
        )

    # ===================================================================
    # 6. STATISTICS & ANALYTICS
    # ===================================================================

    def get_database_stats(self) -> dict:
        """Comprehensive database statistics."""
        node_count = self.run("MATCH (n) RETURN count(n) AS cnt")[0]["cnt"]
        rel_count = self.run(
            "MATCH ()-[r]->() RETURN count(r) AS cnt"
        )[0]["cnt"]
        labels = self.list_labels()
        rel_types = self.list_relationship_types_live()

        # Per-label counts
        label_counts = self.run(
            "MATCH (n) UNWIND labels(n) AS lbl "
            "RETURN lbl, count(*) AS cnt ORDER BY cnt DESC"
        )

        # Per-relationship counts
        rel_type_counts = self.run(
            "MATCH ()-[r]->() "
            "RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC"
        )

        return {
            "profile": self.profile,
            "database": self.neo4j_cfg["database"],
            "total_nodes": node_count,
            "total_relationships": rel_count,
            "labels": labels,
            "relationship_types": rel_types,
            "nodes_per_label": {r["lbl"]: r["cnt"] for r in label_counts},
            "relationships_per_type": {
                r["rel"]: r["cnt"] for r in rel_type_counts
            },
        }

    def get_status_distribution(
        self,
        label: str = "ProductRequirement",
    ) -> List[dict]:
        """
        Return the status distribution for a node type.

        Resolves numeric status IDs to human-readable names using the
        ontology value map.
        """
        labels = self._resolve_labels(label)
        rows = self.run(
            f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
            f"RETURN n.status AS status, count(*) AS count "
            f"ORDER BY count DESC",
            {"labels": labels},
        )

        # Resolve value map
        vmap = _value_map_for(
            self.get_node_type_info(label) or {}, "status"
        )
        for row in rows:
            raw = row["status"]
            if vmap and raw is not None:
                row["status_label"] = vmap.get(int(raw), str(raw))
            else:
                row["status_label"] = str(raw) if raw is not None else "N/A"
        return rows

    def get_asil_distribution(
        self,
        label: str = "ProductRequirement",
    ) -> List[dict]:
        """ASIL classification distribution for a node type."""
        labels = self._resolve_labels(label)
        rows = self.run(
            f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
            f"RETURN n.asil AS asil, count(*) AS count "
            f"ORDER BY count DESC",
            {"labels": labels},
        )
        vmap = _value_map_for(self.get_node_type_info(label) or {}, "asil")
        for row in rows:
            raw = row["asil"]
            if vmap and raw is not None:
                row["asil_label"] = vmap.get(int(raw), str(raw))
            else:
                row["asil_label"] = str(raw) if raw is not None else "N/A"
        return rows

    def get_coverage_report(self) -> dict:
        """
        Traceability coverage report:
        - PRQs linked to SHRQs (DERIVES_FROM)
        - PRQs linked to PVS (VERIFIED_BY)
        - PRQs linked to modules (BELONGS_TO_MODULE)
        """
        total_prq = self.run(
            "MATCH (n) WHERE any(lbl IN labels(n) WHERE lbl IN $requirement_labels) RETURN count(n) AS cnt",
            {"requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )[0]["cnt"]

        prq_with_shrq = self.run(
            "MATCH (n)-[:DERIVES_FROM]->(:StakeholderRequirement) "
            "WHERE any(lbl IN labels(n) WHERE lbl IN $requirement_labels) "
            "RETURN count(DISTINCT n) AS cnt",
            {"requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )[0]["cnt"]

        prq_with_pvs = self.run(
            "MATCH (n)-[:VERIFIED_BY]->(:VerificationStep) "
            "WHERE any(lbl IN labels(n) WHERE lbl IN $requirement_labels) "
            "RETURN count(DISTINCT n) AS cnt",
            {"requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )[0]["cnt"]

        prq_with_module = self.run(
            "MATCH (n) "
            "WHERE any(lbl IN labels(n) WHERE lbl IN $requirement_labels) "
            "AND ((n)-[:BELONGS_TO_MODULE]->(:MCALModule) "
            "     OR coalesce(n.module, n.module_name, n.module_prefix) IS NOT NULL) "
            "RETURN count(DISTINCT n) AS cnt",
            {"requirement_labels": REQUIREMENT_LABEL_ALIASES},
        )[0]["cnt"]

        total_shrq = self.run(
            "MATCH (n:StakeholderRequirement) RETURN count(n) AS cnt"
        )[0]["cnt"]

        shrq_with_prq = self.run(
            "MATCH (n:StakeholderRequirement)<-[:DERIVES_FROM]-(:ProductRequirement) "
            "RETURN count(DISTINCT n) AS cnt"
        )[0]["cnt"]

        def _pct(num: int, den: int) -> str:
            return f"{num / den * 100:.1f}%" if den else "N/A"

        return {
            "total_prq": total_prq,
            "prq_traced_to_shrq": prq_with_shrq,
            "prq_traced_to_shrq_pct": _pct(prq_with_shrq, total_prq),
            "prq_verified": prq_with_pvs,
            "prq_verified_pct": _pct(prq_with_pvs, total_prq),
            "prq_assigned_to_module": prq_with_module,
            "prq_assigned_to_module_pct": _pct(prq_with_module, total_prq),
            "total_shrq": total_shrq,
            "shrq_with_derived_prq": shrq_with_prq,
            "shrq_with_derived_prq_pct": _pct(shrq_with_prq, total_shrq),
        }

    def get_domain_distribution(
        self,
        label: str = "ProductRequirement",
    ) -> List[dict]:
        """Domain classification distribution for a node type."""
        labels = self._resolve_labels(label)
        rows = self.run(
            f"MATCH (n) WHERE {self._label_match('n', 'labels')} "
            f"RETURN n.domain AS domain, count(*) AS count "
            f"ORDER BY count DESC",
            {"labels": labels},
        )
        vmap = _value_map_for(self.get_node_type_info(label) or {}, "domain")
        for row in rows:
            raw = row["domain"]
            if vmap and raw is not None:
                row["domain_label"] = vmap.get(int(raw), str(raw))
            else:
                row["domain_label"] = str(raw) if raw is not None else "N/A"
        return rows

    # ===================================================================
    # 7. RAW CYPHER (advanced)
    # ===================================================================

    def execute_cypher(self, cypher: str, params: Optional[dict] = None) -> List[dict]:
        """Execute arbitrary Cypher.  Thin wrapper around ``run()``."""
        return self.run(cypher, params)


# ---------------------------------------------------------------------------
# CLI Formatters
# ---------------------------------------------------------------------------
def _fmt_table(rows: List[dict], max_col_width: int = 60) -> str:
    """Simple ASCII table formatter."""
    if not rows:
        return "  (no results)\n"
    keys = list(rows[0].keys())
    # Compute column widths
    widths = {k: len(k) for k in keys}
    str_rows = []
    for row in rows:
        sr = {}
        for k in keys:
            val = row[k]
            if isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            s = str(val) if val is not None else ""
            if len(s) > max_col_width:
                s = s[: max_col_width - 3] + "..."
            sr[k] = s
            widths[k] = max(widths[k], len(s))
        str_rows.append(sr)

    header = "  " + "  ".join(k.ljust(widths[k]) for k in keys)
    sep = "  " + "  ".join("-" * widths[k] for k in keys)
    lines = [header, sep]
    for sr in str_rows:
        lines.append("  " + "  ".join(sr[k].ljust(widths[k]) for k in keys))
    return "\n".join(lines) + "\n"


def _fmt_node(node_dict: dict, indent: int = 4) -> str:
    """Pretty-print a single node dict."""
    lines = []
    n = node_dict.get("n", node_dict)
    prefix = " " * indent
    for k, v in sorted(n.items()):
        v_str = str(v) if v is not None else ""
        if len(v_str) > 120:
            v_str = v_str[:120] + "…"
        lines.append(f"{prefix}{k}: {v_str}")
    return "\n".join(lines)


def _print_schema(querier: KnowledgeGraphQuerier) -> None:
    schema = querier.get_schema_summary()
    print("\n" + "=" * 70)
    print(f"  SCHEMA – Profile: {schema['profile']}")
    print("=" * 70)

    print("\n  Node Types:")
    for nt in schema["ontology_node_types"]:
        count = nt["db_count"]
        print(
            f"    :{nt['label']:<30s}  key={nt['unique_key'] or 'N/A':<20s}  "
            f"db_count={count:>6,d}"
        )

    print("\n  Relationship Types:")
    for rt in schema["ontology_relationship_types"]:
        count = rt["db_count"]
        src = rt["source"]
        fr = "/".join(rt["from"])
        to = "/".join(rt["to"])
        print(
            f"    (:{fr})-[:{rt['name']}]->(:{to})  "
            f"source={src:<25s}  db_count={count:>6,d}"
        )

    print("\n  Live DB Labels:", ", ".join(schema["db_label_counts"].keys()) or "(none)")
    print(
        "  Live Relationship Types:",
        ", ".join(schema["db_relationship_counts"].keys()) or "(none)",
    )
    print("=" * 70 + "\n")


def _print_stats(querier: KnowledgeGraphQuerier) -> None:
    stats = querier.get_database_stats()
    print("\n" + "=" * 70)
    print(f"  DATABASE STATS – Profile: {stats['profile']}")
    print("=" * 70)
    print(f"\n  Database       : {stats['database']}")
    print(f"  Total nodes    : {stats['total_nodes']:,d}")
    print(f"  Total relations: {stats['total_relationships']:,d}")

    print("\n  Nodes per label:")
    for lbl, cnt in stats["nodes_per_label"].items():
        print(f"    {lbl:<35s}  {cnt:>6,d}")

    print("\n  Relationships per type:")
    for rel, cnt in stats["relationships_per_type"].items():
        print(f"    {rel:<35s}  {cnt:>6,d}")
    print("=" * 70 + "\n")


def _print_coverage(querier: KnowledgeGraphQuerier) -> None:
    cov = querier.get_coverage_report()
    print("\n" + "=" * 70)
    print("  TRACEABILITY COVERAGE REPORT")
    print("=" * 70)
    print(f"\n  Product Requirements (PRQ):")
    print(f"    Total                       : {cov['total_prq']:>6,d}")
    print(
        f"    Traced to SHRQ (DERIVES_FROM): {cov['prq_traced_to_shrq']:>6,d}  "
        f"({cov['prq_traced_to_shrq_pct']})"
    )
    print(
        f"    Verified (VERIFIED_BY)       : {cov['prq_verified']:>6,d}  "
        f"({cov['prq_verified_pct']})"
    )
    print(
        f"    Assigned to module           : {cov['prq_assigned_to_module']:>6,d}  "
        f"({cov['prq_assigned_to_module_pct']})"
    )
    print(f"\n  Stakeholder Requirements (SHRQ):")
    print(f"    Total                       : {cov['total_shrq']:>6,d}")
    print(
        f"    With derived PRQ             : {cov['shrq_with_derived_prq']:>6,d}  "
        f"({cov['shrq_with_derived_prq_pct']})"
    )
    print("=" * 70 + "\n")


def _print_trace(querier: KnowledgeGraphQuerier, req_id: str) -> None:
    chain = querier.get_traceability_chain(req_id)
    if "error" in chain:
        print(f"\n  ERROR: {chain['error']}\n")
        return

    print("\n" + "=" * 70)
    print(f"  TRACEABILITY CHAIN – {req_id}")
    print("=" * 70)

    origin = chain["origin"]
    n = origin.get("n", origin)
    print(f"\n  Origin: {n.get('name', 'N/A')}")
    print(f"    jama_id      : {n.get('jama_id')}")
    print(f"    status       : {n.get('status')}")
    print(f"    module_prefix: {n.get('module_prefix', 'N/A')}")

    section_map = {
        "stakeholder_requirements": "Upstream SHRQs (DERIVES_FROM →)",
        "product_requirements": "Downstream PRQs (← DERIVES_FROM)",
        "verification_steps": "Verification Steps (VERIFIED_BY →)",
        "verification_reports": "Verification Reports (HAS_RESULT →)",
        "assumptions": "Architectural Assumptions (ASSUMES →)",
        "module": "Module (BELONGS_TO_MODULE →)",
        "folder": "Folder (BELONGS_TO_FOLDER →)",
        "release": "Release (TARGETED_FOR →)",
        "raised_by": "Raised By (RAISED_BY →)",
        "sourced_from": "Sourced From (SOURCED_FROM →)",
    }

    for key, title in section_map.items():
        items = chain.get(key, [])
        if items:
            print(f"\n  {title} ({len(items)}):")
            for item in items:
                m = item.get("m", item)
                lbls = item.get("labels", [])
                name = m.get("name", "N/A")
                uid = (
                    m.get("requirement_id")
                    or m.get("verification_id")
                    or m.get("assumption_id")
                    or m.get("module_name")
                    or m.get("folder_id")
                    or m.get("release_id")
                    or m.get("stakeholder_id")
                    or m.get("document_id")
                    or str(m.get("jama_id", ""))
                )
                print(f"    [{', '.join(lbls)}] {uid}  –  {name}")

    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# SFR command handler
# ---------------------------------------------------------------------------
def _handle_sfr_command(querier: KnowledgeGraphQuerier, arg: str) -> None:
    """Handle the ``sfr`` interactive command.

    Usage forms::
        sfr                        – summary (register counts per device)
        sfr ADC_SUPLLEV            – register detail + bitfields
        sfr ADC_SUPLLEV --device TC49xN  – device-specific lookup
        sfr --device TC49xN        – all registers for a device
    """
    import re as _re

    parts = arg.split() if arg else []
    device = None
    reg_name = None

    # Parse --device flag
    for i, p in enumerate(parts):
        if p == "--device" and i + 1 < len(parts):
            device = parts[i + 1]
            parts = parts[:i] + parts[i + 2:]
            break

    if parts:
        reg_name = parts[0]

    if not reg_name and not device:
        # Summary: register counts per device
        rows = querier.run(
            "MATCH (r:SFR_Register) "
            "RETURN r.device AS device, count(*) AS registers "
            "ORDER BY device"
        )
        print("\n" + "=" * 60)
        print("  SFR REGISTER SUMMARY")
        print("=" * 60)
        total = 0
        for row in rows:
            print(f"    {row['device']:15s}  {row['registers']:>5d} registers")
            total += row['registers']
        print(f"    {'TOTAL':15s}  {total:>5d}")

        # Cross-link summary
        xrows = querier.run(
            "MATCH (f:SRC_Function)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
            "RETURN count(DISTINCT f) AS funcs, count(DISTINCT r) AS regs"
        )
        if xrows:
            x = xrows[0]
            print(f"\n    Source→SFR cross-links: {x['funcs']} functions → {x['regs']} registers")
        print("=" * 60 + "\n")

    elif reg_name:
        # Specific register lookup
        cypher = (
            "MATCH (r:SFR_Register) "
            "WHERE toLower(r.name) CONTAINS toLower($rname) "
            + ("AND r.device = $dev " if device else "")
            + "OPTIONAL MATCH (r)-[:SFR_HAS_BITFIELD]->(bf:SFR_BitField) "
            "RETURN r, collect(bf) AS bitfields ORDER BY r.device "
            "LIMIT 30"
        )
        params: dict = {"rname": reg_name}
        if device:
            params["dev"] = device
        rows = querier.run(cypher, params)
        if not rows:
            print(f"  No SFR register matching '{reg_name}' found.")
            return

        for row in rows:
            rprops = dict(row["r"])
            print(f"\n  Register: {rprops.get('name')}  [{rprops.get('device')}]")
            print(f"    Description : {rprops.get('description', 'N/A')}")
            print(f"    Struct      : {rprops.get('struct_name', 'N/A')}")
            print(f"    Version     : {rprops.get('version', 'N/A')}")
            bfs = row.get("bitfields", [])
            if bfs:
                print(f"    Bitfields ({len(bfs)}):")
                for bf_node in sorted(bfs, key=lambda b: dict(b).get("lsb", 0)):
                    bfp = dict(bf_node)
                    print(
                        f"      {str(bfp.get('name','?')):20s} "
                        f"{str(bfp.get('bits','')):10s} "
                        f"w={str(bfp.get('width','')):2s} "
                        f"access={str(bfp.get('access','')):4s} "
                        f"mask={bfp.get('mask','')}"
                    )

        # Show which functions access this register
        cypher_x = (
            "MATCH (f:SRC_Function)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
            "WHERE toLower(r.name) CONTAINS toLower($rname) "
            + ("AND r.device = $dev " if device else "")
            + "RETURN DISTINCT f.name AS func ORDER BY func"
        )
        xrows = querier.run(cypher_x, params)
        if xrows:
            print(f"\n    Accessed by source functions:")
            for xr in xrows:
                print(f"      → {xr['func']}")
        print()

    elif device:
        # All registers for a device
        rows = querier.run(
            "MATCH (r:SFR_Register) "
            "WHERE r.device = $dev "
            "RETURN r.name AS name, r.description AS description "
            "ORDER BY name",
            {"dev": device},
        )
        print(f"\n  SFR Registers for {device} ({len(rows)} total):")
        for i, row in enumerate(rows, 1):
            desc = (row['description'] or '')[:50]
            print(f"    {i:3d}. {row['name']:35s}  {desc}")
        print()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------
def _interactive(querier: KnowledgeGraphQuerier) -> None:
    """Simple interactive query REPL."""
    print("\n" + "=" * 70)
    print("  Knowledge Graph Querier – Interactive Mode")
    print(f"  Profile: {querier.profile}  |  Database: {querier.neo4j_cfg['database']}")
    print("=" * 70)
    print(
        textwrap.dedent("""\
        Commands:
          schema                       – show ontology schema & DB counts
          stats                        – database statistics
          coverage                     – traceability coverage report
          labels                       – list live labels
          modules                      – list MCAL modules
          module <NAME>                – module detail (e.g. module ADC)
          search <LABEL> <KEYWORD>     – search nodes
          find <ID>                    – find node by requirement ID
          trace <ID>                   – full traceability chain
          neighbors <JAMA_ID>          – neighbors of a node
          path <FROM_ID> <TO_ID>       – shortest path between two jama_ids
          status <LABEL>               – status distribution
          asil <LABEL>                 – ASIL distribution
          domain <LABEL>               – domain distribution
          sfr [REGISTER] [--device X]  – SFR register/bitfield lookup
          cypher <QUERY>               – raw Cypher query
          help                         – show this help
          quit / exit                  – exit
        """)
    )

    while True:
        try:
            line = input("  kg> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        try:
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                _interactive(querier)  # re-print help then return
                return
            elif cmd == "schema":
                _print_schema(querier)
            elif cmd == "stats":
                _print_stats(querier)
            elif cmd == "coverage":
                _print_coverage(querier)
            elif cmd == "labels":
                labels = querier.list_labels()
                print("  Labels:", ", ".join(labels) if labels else "(none)")
            elif cmd == "modules":
                rows = querier.list_modules()
                print(_fmt_table(rows))
            elif cmd == "module" and arg:
                details = querier.get_module_details(arg.strip(), include_items=False)
                if "error" in details:
                    print(f"  {details['error']}")
                else:
                    print(f"\n  Module: {details['module']}")
                    print(f"  Item counts: {details['item_counts']}")
                    print("  PRQ status distribution:")
                    print(_fmt_table(details["prq_status_distribution"]))
                    print("  PRQ category distribution:")
                    print(_fmt_table(details["prq_category_distribution"]))
            elif cmd == "search" and arg:
                search_parts = arg.split(maxsplit=1)
                label = search_parts[0]
                keyword = search_parts[1] if len(search_parts) > 1 else None
                rows = querier.search_nodes(label, keyword=keyword, limit=20)
                if rows:
                    for i, row in enumerate(rows, 1):
                        n = row.get("n", row)
                        print(f"\n  [{i}] {n.get('name', 'N/A')}")
                        print(f"      jama_id: {n.get('jama_id')}  "
                              f"status: {n.get('status')}")
                else:
                    print("  (no results)")
            elif cmd == "find" and arg:
                result = querier.find_by_id(arg.strip())
                if result:
                    print(_fmt_node(result))
                else:
                    print(f"  Node '{arg.strip()}' not found.")
            elif cmd == "trace" and arg:
                _print_trace(querier, arg.strip())
            elif cmd == "neighbors" and arg:
                try:
                    jid = int(arg.strip())
                except ValueError:
                    print("  neighbors requires a numeric jama_id")
                    continue
                rows = querier.get_neighbors(jid)
                for row in rows:
                    m = row.get("m", row)
                    direction = "→" if row.get("is_outgoing") else "←"
                    print(
                        f"  {direction} [:{row.get('rel_type')}] "
                        f"{', '.join(row.get('labels', []))}  "
                        f"{m.get('name', 'N/A')}  (jama_id={m.get('jama_id')})"
                    )
                if not rows:
                    print("  (no neighbors)")
            elif cmd == "path" and arg:
                ids = arg.split()
                if len(ids) < 2:
                    print("  Usage: path <FROM_JAMA_ID> <TO_JAMA_ID>")
                    continue
                try:
                    fid, tid = int(ids[0]), int(ids[1])
                except ValueError:
                    print("  Both IDs must be integers (jama_id).")
                    continue
                paths = querier.shortest_path(fid, tid)
                if paths:
                    p = paths[0]
                    print("\n  Shortest path:")
                    for node in p.get("nodes", []):
                        print(
                            f"    ({', '.join(node.get('labels', []))}) "
                            f"{node.get('name', 'N/A')} [jama_id={node.get('jama_id')}]"
                        )
                    print("  Relationships:")
                    for rel in p.get("rels", []):
                        print(
                            f"    ({rel.get('from')}) -[:{rel.get('type')}]-> ({rel.get('to')})"
                        )
                else:
                    print("  No path found.")
            elif cmd == "status":
                label = arg.strip() if arg else "ProductRequirement"
                rows = querier.get_status_distribution(label)
                print(_fmt_table(rows))
            elif cmd == "asil":
                label = arg.strip() if arg else "ProductRequirement"
                rows = querier.get_asil_distribution(label)
                print(_fmt_table(rows))
            elif cmd == "domain":
                label = arg.strip() if arg else "ProductRequirement"
                rows = querier.get_domain_distribution(label)
                print(_fmt_table(rows))
            elif cmd == "cypher" and arg:
                rows = querier.execute_cypher(arg)
                if rows:
                    print(_fmt_table(rows))
                else:
                    print("  (no results)")
            elif cmd == "sfr":
                _handle_sfr_command(querier, arg)
            else:
                print(f"  Unknown command: {cmd}. Type 'help' for usage.")
        except Exception as exc:
            print(f"  ERROR: {exc}")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Query the Neo4j Knowledge Graph built from the automotive ontology.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Sub-commands:
          (none)          interactive REPL
          schema          show ontology schema and DB counts
          stats           database statistics
          coverage        traceability coverage report
          search          search nodes by label and keyword
          trace           V-model traceability chain for a requirement
          module          module-level details
          neighbors       neighbors of a node by jama_id
          cypher          execute raw Cypher

        Examples:
          python query_knowledge_graph.py --profile mcal
          python query_knowledge_graph.py --profile mcal schema
          python query_knowledge_graph.py --profile mcal stats
          python query_knowledge_graph.py --profile mcal coverage
          python query_knowledge_graph.py --profile mcal search --label ProductRequirement --keyword "Adc"
          python query_knowledge_graph.py --profile mcal trace --id AU3GM-PRQ-29969
          python query_knowledge_graph.py --profile mcal module --name ADC
          python query_knowledge_graph.py --profile mcal neighbors --jama-id 12345
          python query_knowledge_graph.py --profile mcal cypher "MATCH (n) RETURN labels(n) AS lbl, count(*) AS cnt"
        """),
    )
    parser.add_argument(
        "--profile",
        choices=["mcal", "illd"],
        default=None,
        help="Ontology profile. If omitted, uses active_instance from config.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )

    # Sub-commands
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("schema", help="Show ontology schema and DB counts")
    sub.add_parser("stats", help="Database statistics")
    sub.add_parser("coverage", help="Traceability coverage report")

    p_search = sub.add_parser("search", help="Search nodes")
    p_search.add_argument("--label", required=True, help="Node label")
    p_search.add_argument("--keyword", default=None, help="Keyword search")
    p_search.add_argument("--limit", type=int, default=25)

    p_trace = sub.add_parser("trace", help="Traceability chain")
    p_trace.add_argument("--id", required=True, dest="req_id", help="Requirement ID")

    p_module = sub.add_parser("module", help="Module details")
    p_module.add_argument("--name", required=True, help="Module name")
    p_module.add_argument(
        "--items", action="store_true", help="Include item lists"
    )

    p_neighbors = sub.add_parser("neighbors", help="Node neighbors")
    p_neighbors.add_argument(
        "--jama-id", type=int, required=True, help="Jama numeric ID"
    )
    p_neighbors.add_argument(
        "--direction", choices=["in", "out", "both"], default="both"
    )

    p_cypher = sub.add_parser("cypher", help="Raw Cypher query")
    p_cypher.add_argument("query", help="Cypher statement")

    p_find = sub.add_parser("find", help="Find node by ID")
    p_find.add_argument("--id", required=True, dest="req_id", help="Requirement ID")

    p_orphans = sub.add_parser("orphans", help="Find orphan requirements")
    p_orphans.add_argument(
        "--label", default="ProductRequirement", help="Node label"
    )

    p_unverified = sub.add_parser(
        "unverified", help="Find unverified requirements"
    )
    p_unverified.add_argument(
        "--label", default="ProductRequirement", help="Node label"
    )

    p_sfr = sub.add_parser("sfr", help="SFR register/bitfield lookup")
    p_sfr.add_argument("--register", default=None, help="Register name (substring match)")
    p_sfr.add_argument("--device", default=None, help="Device variant (e.g. TC49xN)")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve profile
    storage_cfg = load_storage_config()
    profile = args.profile or storage_cfg.get("active_instance", "mcal")

    # Connect
    querier = KnowledgeGraphQuerier(
        profile=profile, storage_cfg=storage_cfg
    )
    try:
        querier.connect()
    except Exception as exc:
        print(f"\n  ERROR: Could not connect to Neo4j – {exc}")
        print(
            f"  Ensure Neo4j is running and config in "
            f"{STORAGE_CONFIG_PATH} is correct.\n"
        )
        sys.exit(1)

    try:
        if args.command is None:
            _interactive(querier)
        elif args.command == "schema":
            _print_schema(querier)
        elif args.command == "stats":
            _print_stats(querier)
        elif args.command == "coverage":
            _print_coverage(querier)
        elif args.command == "search":
            rows = querier.search_nodes(
                args.label, keyword=args.keyword, limit=args.limit
            )
            for i, row in enumerate(rows, 1):
                n = row.get("n", row)
                print(f"\n  [{i}] {n.get('name', 'N/A')}")
                uid = (
                    n.get("requirement_id")
                    or n.get("verification_id")
                    or n.get("assumption_id")
                    or n.get("document_id")
                    or n.get("stakeholder_id")
                    or str(n.get("jama_id", ""))
                )
                print(f"      ID: {uid}  |  jama_id: {n.get('jama_id')}  |  status: {n.get('status')}")
            if not rows:
                print("  (no results)")
        elif args.command == "find":
            result = querier.find_by_id(args.req_id)
            if result:
                print(_fmt_node(result))
            else:
                print(f"  Node '{args.req_id}' not found.")
        elif args.command == "trace":
            _print_trace(querier, args.req_id)
        elif args.command == "module":
            details = querier.get_module_details(
                args.name, include_items=args.items
            )
            if "error" in details:
                print(f"  {details['error']}")
            else:
                mod = details["module"]
                print(f"\n  Module: {mod.get('module_name', 'N/A')}")
                print(f"  Item counts:")
                for lbl, cnt in details["item_counts"].items():
                    print(f"    {lbl:<35s}  {cnt:>5,d}")
                print("\n  PRQ status distribution:")
                print(_fmt_table(details["prq_status_distribution"]))
                print("  PRQ category distribution:")
                print(_fmt_table(details["prq_category_distribution"]))
                if args.items:
                    for key in [
                        "ProductRequirement_items",
                        "StakeholderRequirement_items",
                        "VerificationStep_items",
                    ]:
                        items = details.get(key, [])
                        if items:
                            print(f"\n  {key.replace('_items', '')} ({len(items)}):")
                            for item in items:
                                n = item.get("n", item)
                                print(f"    {n.get('name', 'N/A')}")
        elif args.command == "neighbors":
            rows = querier.get_neighbors(
                args.jama_id, direction=args.direction
            )
            for row in rows:
                m = row.get("m", row)
                direction = "→" if row.get("is_outgoing") else "←"
                print(
                    f"  {direction} [:{row.get('rel_type')}]  "
                    f"{', '.join(row.get('labels', []))}  "
                    f"{m.get('name', 'N/A')}  (jama_id={m.get('jama_id')})"
                )
            if not rows:
                print("  (no neighbors)")
        elif args.command == "cypher":
            rows = querier.execute_cypher(args.query)
            if rows:
                print(_fmt_table(rows))
            else:
                print("  (no results)")
        elif args.command == "orphans":
            rows = querier.find_orphan_requirements(label=args.label)
            print(f"\n  Orphan {args.label} nodes (no DERIVES_FROM): {len(rows)}")
            for row in rows[:30]:
                n = row.get("n", row)
                print(f"    {n.get('name', 'N/A')}  (jama_id={n.get('jama_id')})")
            if len(rows) > 30:
                print(f"    … and {len(rows) - 30} more")
        elif args.command == "unverified":
            rows = querier.find_unverified_requirements(label=args.label)
            print(
                f"\n  Unverified {args.label} nodes (no VERIFIED_BY): {len(rows)}"
            )
            for row in rows[:30]:
                n = row.get("n", row)
                print(f"    {n.get('name', 'N/A')}  (jama_id={n.get('jama_id')})")
            if len(rows) > 30:
                print(f"    … and {len(rows) - 30} more")
        elif args.command == "sfr":
            sfr_arg_parts = []
            if args.register:
                sfr_arg_parts.append(args.register)
            if args.device:
                sfr_arg_parts.extend(["--device", args.device])
            _handle_sfr_command(querier, " ".join(sfr_arg_parts))
    finally:
        querier.close()


if __name__ == "__main__":
    main()

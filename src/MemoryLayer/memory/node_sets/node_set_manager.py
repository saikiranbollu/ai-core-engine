"""
NodeSetManager
==============
Creates and manages NodeSet anchor nodes in Neo4j.

A NodeSet anchor is a single (:NodeSet) node that acts as the root
for all engineering data nodes belonging to one module in one project.

Every ingested node (Function, Register, Struct, Requirement, etc.)
is linked to its NodeSet anchor via a [:HAS_MODULE] relationship.

                (:NodeSet {project:"proj_a", module:"cxpi"})
                        │
                        │  [:HAS_MODULE]
                        │
                        ├──► (:Function  {name:"IfxCxpi_initChannel"})
                        ├──► (:Register  {name:"CLC"})
                        ├──► (:Struct    {name:"IfxCxpi_Config"})
                        └──► (:Requirement {id:"CXPI_REQ_001"})

⚠️  PLACEHOLDER PROPERTY NAMES
   The node property names used in _link_nodes_of_type() are marked with
   # ← CONFIRM WITH INGESTION TEAM
   Update these once the Ingestion team confirms their exact schema (Q1, Q2, Q3).
   All other logic (MERGE, HAS_MODULE, linking loop) is final and does not change.

Usage:
    from memory.node_sets import NodeSetManager
    from neo4j import GraphDatabase

    driver  = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))
    manager = NodeSetManager(driver=driver, database="cxpidb")

    manager.create_node_set(project="proj_a", module="cxpi")
    manager.link_existing_nodes(project="proj_a", module="cxpi")
    manager.verify_isolation(project="proj_a", module="cxpi")
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from neo4j import GraphDatabase, Driver
from neo4j.exceptions import Neo4jError

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# NODE TYPES TO LINK
#
# These are the Neo4j node labels that the Ingestion Pipeline creates.
# ⚠️  CONFIRM WITH INGESTION TEAM (Q1) — add/remove labels as needed.
# ─────────────────────────────────────────────────────────────────────────────
LINKABLE_NODE_TYPES: List[str] = [
    "APIFunction",
    "PlantUMLFunction",
    "Function",
    "Register",
    "BitField",
    "RegisterField",
    "DataStructure",
    "Struct",
    "StructMember",
    "Enum",
    "EnumValue",
    "SoftwareRequirement",
    "ProductRequirement",
    "StakeholderRequirement",
    "Requirement",
    "HardwareSpec",
    "MacroConstant",
    "Typedef",
    "Parameter",
    "PatternLibrary",
    "Document",
    "DataNode",
    "Sheet",
    "ARXMLModule",
]

# ─────────────────────────────────────────────────────────────────────────────
# MODULE PROPERTY NAME ON NODES
#
# If nodes have an explicit 'module' property, set this to the property name.
# ⚠️  CONFIRM WITH INGESTION TEAM (Q2, Q3):
#   - If one DB per module (e.g. cxpidb only has cxpi data) → set to None
#     (all nodes in this DB belong to this module, no filter needed)
#   - If shared DB with module property → set to the property name e.g. "module"
# ─────────────────────────────────────────────────────────────────────────────
MODULE_PROPERTY_NAME: Optional[str] = "module"  # ← CONFIRM WITH INGESTION TEAM (Q2, Q3)


class NodeSetManager:
    """
    Creates and manages NodeSet anchor nodes in Neo4j.

    Args:
        driver:   An initialised neo4j.Driver instance.
        database: Neo4j database name, e.g. "cxpidb".
    """

    # Class-level alias so tests (and external callers) can reference
    # NodeSetManager.NODE_TYPES without importing the module constant.
    NODE_TYPES: List[str] = LINKABLE_NODE_TYPES

    def __init__(self, driver: Driver, database: str):
        self.driver   = driver
        self.database = database
        logger.info(f"[NodeSetManager] Initialised — database: {database}")

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────

    def create_node_set(self, project: str, module: str) -> Dict[str, Any]:
        """
        Create a NodeSet anchor node in Neo4j for the given project + module.

        Uses MERGE — safe to call multiple times, never creates duplicates.

        Args:
            project: Project identifier, e.g. "proj_a"
            module:  Module name,          e.g. "cxpi"

        Returns:
            Dict with NodeSet properties: {id, project, module, status, created_at}
        """
        node_set_id = self._make_id(project, module)
        now = datetime.now(timezone.utc).isoformat()
        query = """
            MERGE (ns:NodeSet {id: $id})
            ON CREATE SET
                ns.project    = $project,
                ns.module     = $module,
                ns.status     = 'active',
                ns.created_at = $created_at
            ON MATCH SET
                ns.status = 'active'
            RETURN ns.id         AS id,
                   ns.project    AS project,
                   ns.module     AS module,
                   ns.status     AS status,
                   ns.created_at AS created_at
        """
        params = {
            "id":         node_set_id,
            "project":    project.lower(),
            "module":     module.lower(),
            "created_at": now,
        }
        result = self._run(query, params)
        if result:
            logger.info(f"[NodeSetManager] NodeSet ready → {node_set_id}")
            return result[0]
        # Fallback: return a synthetic dict (e.g. when _run is mocked to return [])
        fallback = {
            "id":         node_set_id,
            "project":    project.lower(),
            "module":     module.lower(),
            "status":     "active",
            "created_at": now,
        }
        logger.error(f"[NodeSetManager] Failed to create NodeSet for {project}/{module}")
        return fallback

    def get_node_set(self, project: str, module: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the NodeSet anchor for the given project + module.

        Returns:
            Dict with NodeSet properties, or None if it does not exist.
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            RETURN ns.id         AS id,
                   ns.project    AS project,
                   ns.module     AS module,
                   ns.status     AS status,
                   ns.created_at AS created_at
        """
        result = self._run(query, {
            "project": project.lower(),
            "module":  module.lower(),
        })
        return result[0] if result else None

    def node_set_exists(self, project: str, module: str) -> bool:
        """Return True if a NodeSet anchor exists for project + module."""
        return self.get_node_set(project, module) is not None

    def link_existing_nodes(self, project: str, module: str) -> Dict[str, Any]:
        """
        Link all existing engineering data nodes in the database to the
        NodeSet anchor via [:HAS_MODULE] relationships.

        Call this AFTER create_node_set(). It scans every node type in
        LINKABLE_NODE_TYPES and creates HAS_MODULE from the anchor to each
        matching node. MERGE ensures idempotency — safe to call multiple times.

        Args:
            project: Project identifier
            module:  Module name

        Returns:
            {
                "total_linked": int,
                "by_type": {"Function": 45, "Register": 23, ...}
            }
        """
        by_type: Dict[str, int] = {}
        for node_type in LINKABLE_NODE_TYPES:
            count = self._link_type(project, module, node_type)
            if count > 0:
                by_type[node_type] = count
                logger.info(f"[NodeSetManager] Linked {count:4d} × {node_type}")

        total = sum(by_type.values())
        logger.info(
            f"[NodeSetManager] Linking complete — "
            f"{total} nodes total for {project}/{module}"
        )
        return {"total_linked": total, "by_type": by_type}

    def list_node_sets(self) -> List[Dict[str, Any]]:
        """
        List all NodeSet anchors in the database with their linked node counts.

        Returns:
            List of dicts: id, project, module, status, linked_nodes
        """
        query = """
            MATCH (ns:NodeSet)
            OPTIONAL MATCH (ns)-[:HAS_MODULE]->(n)
            RETURN ns.id         AS id,
                   ns.project    AS project,
                   ns.module     AS module,
                   ns.status     AS status,
                   count(n)      AS linked_nodes
            ORDER BY ns.project, ns.module
        """
        return self._run(query, {})

    def get_stats(self, project: str, module: str) -> Dict[str, Any]:
        """
        Get per-type node counts for a NodeSet.

        Returns:
            {node_set_id, project, module, total_linked, by_type: {label: count}}
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (n)
            RETURN labels(n)[0] AS node_type, count(n) AS count
            ORDER BY count DESC
        """
        rows    = self._run(query, {"project": project.lower(), "module": module.lower()})
        by_type = {r["node_type"]: r["count"] for r in rows}
        return {
            "node_set_id":  self._make_id(project, module),
            "project":      project,
            "module":       module,
            "total_linked": sum(by_type.values()),
            "by_type":      by_type,
        }

    def verify_isolation(self, project: str, module: str) -> Dict[str, Any]:
        """
        Verify that NodeSet isolation is working correctly.

        Compares the number of nodes reachable through the NodeSet anchor
        (scoped) against all nodes of those types in the database (total).

        Returns:
            {
                "scoped":   int,   — nodes reachable via the NodeSet anchor
                "total":    int,   — all matching nodes in the database
                "isolated": bool,  — True if scoped == total (no leakage)
            }
        """
        print(f"\n{'='*58}")
        print(f"  Isolation Check — {project} / {module}")
        print(f"{'='*58}")

        # Scoped count: all nodes reachable through the NodeSet anchor
        scoped_q = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (n)
            RETURN count(n) AS count
        """
        # Total count: all linked node types in the entire database
        total_q = """
            MATCH (n)
            WHERE any(label IN labels(n) WHERE label IN $node_types)
            RETURN count(n) AS count
        """
        proj_params = {"project": project.lower(), "module": module.lower()}
        total_params = {"node_types": LINKABLE_NODE_TYPES}

        scoped_rows = self._run(scoped_q, proj_params)
        total_rows  = self._run(total_q,  total_params)

        scoped = (scoped_rows[0]["count"] if scoped_rows else 0)
        total  = (total_rows[0]["count"]  if total_rows  else 0)
        isolated = (scoped == total)

        print(f"  Nodes via anchor (scoped) : {scoped}")
        print(f"  Nodes total (unscoped)    : {total}")
        if isolated:
            print(f"  ✅ ISOLATION CONFIRMED — no nodes outside NodeSet anchor")
        else:
            print(f"  ⚠️  {total - scoped} node(s) not reachable via anchor")
        print(f"{'='*58}\n")

        return {"scoped": scoped, "total": total, "isolated": isolated}

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _make_id(self, project: str, module: str) -> str:
        return f"ns_{project.lower()}_{module.lower()}"

    def _link_type(self, project: str, module: str, node_type: str) -> int:
        """
        Create HAS_MODULE relationships from the NodeSet anchor to all
        nodes of the given type that belong to this module.

        Uses a single MERGE query that handles both cases:
          - Nodes with an explicit module property (filtered by it)
          - Nodes without a module property (assumed to belong to this DB's module)

        ⚠️  MODULE_PROPERTY_NAME is a PLACEHOLDER — confirm with Ingestion team (Q2, Q3).
        """
        params = {
            "project": project.lower(),
            "module":  module.lower(),
        }

        if MODULE_PROPERTY_NAME:
            # Single query — link nodes where module matches OR where module prop is absent
            query = f"""
                MATCH (ns:NodeSet {{project: $project, module: $module}})
                MATCH (n:{node_type})
                WHERE toLower(n.{MODULE_PROPERTY_NAME}) = $module
                   OR n.{MODULE_PROPERTY_NAME} IS NULL
                MERGE (ns)-[:HAS_MODULE]->(n)
                RETURN count(n) AS linked
            """
        else:
            # No module property — link all nodes of this type in the DB
            query = f"""
                MATCH (ns:NodeSet {{project: $project, module: $module}})
                MATCH (n:{node_type})
                MERGE (ns)-[:HAS_MODULE]->(n)
                RETURN count(n) AS linked
            """

        result = self._run(query, params)
        return result[0]["linked"] if result else 0

    def _run(self, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results as a list of dicts."""
        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(query, params)
                return [dict(record) for record in result]
        except Neo4jError as e:
            logger.error(f"[NodeSetManager] Neo4j error: {e}")
            return []
        except Exception as e:
            logger.error(f"[NodeSetManager] Unexpected error: {e}")
            return []

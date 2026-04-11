"""
ScopedQuery
===========
All Neo4j Knowledge Graph queries scoped through the NodeSet anchor.

Every method here enforces the pattern:
    MATCH (ns:NodeSet {project, module}) -[:HAS_MODULE]-> (node)

This guarantees module isolation — a cxpi query never returns can data,
a proj_A query never returns proj_B data.

⚠️  PLACEHOLDER PROPERTY NAMES
   Properties like `f.name`, `f.return_type`, `r.address` etc. are marked with
   # ← CONFIRM WITH INGESTION TEAM
   Update these once the Ingestion team confirms their exact schema (Q2).
   The anchor scoping logic (NodeSet → HAS_MODULE → node) is final.

Usage:
    from memory.node_sets import ScopedQuery
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))
    sq     = ScopedQuery(driver=driver, database="cxpidb")

    functions = sq.get_functions(project="proj_a", module="cxpi")
    registers = sq.get_registers(project="proj_a", module="cxpi")
    summary   = sq.get_summary(project="proj_a",   module="cxpi")
"""

import logging
from typing import Dict, Any, List

from neo4j import Driver
from neo4j.exceptions import Neo4jError

logger = logging.getLogger(__name__)


NODE_TYPE_ALIASES = {
    "functions": ["APIFunction", "PlantUMLFunction", "Function"],
    "registers": ["Register"],
    "structs": ["DataStructure", "Struct"],
    "enums": ["Enum"],
    "requirements": [
        "SoftwareRequirement",
        "ProductRequirement",
        "StakeholderRequirement",
        "Requirement",
    ],
    "register_fields": ["BitField", "RegisterField"],
    "struct_members": ["StructMember", "Parameter"],
}


class ScopedQuery:
    """
    All Neo4j queries scoped through the NodeSet anchor.

    Args:
        driver:   An initialised neo4j.Driver instance.
        database: Neo4j database name, e.g. "cxpidb".
    """

    def __init__(self, driver: Driver, database: str):
        self.driver   = driver
        self.database = database
        logger.info(f"[ScopedQuery] Initialised — database: {database}")

    # ─────────────────────────────────────────────────────────────────────
    # SCOPED NODE RETRIEVAL
    # ─────────────────────────────────────────────────────────────────────

    def get_functions(
        self, project: str, module: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get all Function nodes for project + module, scoped through NodeSet.

        Returns: List of dicts with function properties.

        ⚠️  Property names (f.name, f.return_type, f.source_file, f.brief)
            are PLACEHOLDERS — confirm with Ingestion team (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (f)
            WHERE any(label IN labels(f) WHERE label IN $labels)
            RETURN coalesce(f.name, f.function_name) AS name,
                   f.return_type AS return_type,
                   f.source_file AS source_file,
                   coalesce(f.brief, f.description, '') AS description,
                   coalesce(f.id, f.node_id, f.name, f.function_name) AS id
            ORDER BY coalesce(f.name, f.function_name)
            LIMIT $limit
        """
        return self._run(query, {
            "project": project.lower(),
            "module": module.lower(),
            "limit": limit,
            "labels": NODE_TYPE_ALIASES["functions"],
        })

    def get_registers(
        self, project: str, module: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get all Register nodes for project + module, scoped through NodeSet.

        ⚠️  Property names (r.name, r.address, r.description) are PLACEHOLDERS.
            Confirm with Ingestion team (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (r)
            WHERE any(label IN labels(r) WHERE label IN $labels)
            RETURN r.name        AS name,
                   r.address     AS address,
                   coalesce(r.description, r.brief, '') AS description,
                   coalesce(r.id, r.node_id, r.name) AS id
            ORDER BY r.name
            LIMIT $limit
        """
        return self._run(query, {
            "project": project.lower(),
            "module": module.lower(),
            "limit": limit,
            "labels": NODE_TYPE_ALIASES["registers"],
        })

    def get_structs(
        self, project: str, module: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get all Struct nodes for project + module, scoped through NodeSet.

        ⚠️  Property names (s.name, s.brief) are PLACEHOLDERS — confirm (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (s)
            WHERE any(label IN labels(s) WHERE label IN $labels)
            RETURN s.name  AS name,
                   coalesce(s.brief, s.description, '') AS description,
                   coalesce(s.id, s.node_id, s.name) AS id
            ORDER BY s.name
            LIMIT $limit
        """
        return self._run(query, {
            "project": project.lower(),
            "module": module.lower(),
            "limit": limit,
            "labels": NODE_TYPE_ALIASES["structs"],
        })

    def get_enums(
        self, project: str, module: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get all Enum nodes for project + module, scoped through NodeSet.

        ⚠️  Property names (e.name, e.brief) are PLACEHOLDERS — confirm (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (e)
            WHERE any(label IN labels(e) WHERE label IN $labels)
            RETURN e.name  AS name,
                   coalesce(e.brief, e.description, '') AS description,
                   coalesce(e.id, e.node_id, e.name) AS id
            ORDER BY e.name
            LIMIT $limit
        """
        return self._run(query, {
            "project": project.lower(),
            "module": module.lower(),
            "limit": limit,
            "labels": NODE_TYPE_ALIASES["enums"],
        })

    def get_requirements(
        self, project: str, module: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get all Requirement nodes for project + module, scoped through NodeSet.

        ⚠️  Property names (req.id, req.name, req.description, req.asil)
            are PLACEHOLDERS — confirm with Ingestion team (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (req)
            WHERE any(label IN labels(req) WHERE label IN $labels)
            RETURN coalesce(req.requirement_id, req.id, req.document_id, req.name) AS id,
                   coalesce(req.name, req.requirement_id, req.id) AS name,
                   coalesce(req.description, req.text, '') AS text,
                   req.asil        AS asil
            ORDER BY coalesce(req.requirement_id, req.id, req.name)
            LIMIT $limit
        """
        return self._run(query, {
            "project": project.lower(),
            "module": module.lower(),
            "limit": limit,
            "labels": NODE_TYPE_ALIASES["requirements"],
        })

    # ─────────────────────────────────────────────────────────────────────
    # RELATIONSHIP TRAVERSAL — scoped through NodeSet
    # ─────────────────────────────────────────────────────────────────────

    def get_function_calls(
        self, project: str, module: str, function_name: str
    ) -> List[Dict[str, Any]]:
        """
        Get all functions called by a specific function (CALLS_INTERNALLY).
        Both caller and called must belong to the same NodeSet.

        ⚠️  Relationship name CALLS_INTERNALLY — confirm with Ingestion team (Q1).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
                        -[:HAS_MODULE]-> (caller)
                        WHERE any(label IN labels(caller) WHERE label IN $function_labels)
                            AND coalesce(caller.name, caller.function_name) = $func_name
                        MATCH (caller)-[:CALLS_INTERNALLY|CALLS|DEPENDS_ON]->(called)
                        WHERE (ns)-[:HAS_MODULE]->(called)
                            AND any(label IN labels(called) WHERE label IN $function_labels)
                        RETURN coalesce(caller.name, caller.function_name) AS caller,
                                     coalesce(called.name, called.function_name) AS called_function,
                   called.return_type AS return_type
                        ORDER BY coalesce(called.name, called.function_name)
        """
        return self._run(query, {
            "project":   project.lower(),
            "module":    module.lower(),
            "func_name": function_name,
                        "function_labels": NODE_TYPE_ALIASES["functions"],
        })

    def get_functions_for_requirement(
        self, project: str, module: str, requirement_id: str
    ) -> List[Dict[str, Any]]:
        """
        Find all functions that IMPLEMENT a given requirement.
        Both Requirement and Function are anchored to the same NodeSet.

        ⚠️  Relationship name IMPLEMENTS — confirm with Ingestion team (Q1).
        ⚠️  req.id property name — confirm with Ingestion team (Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (req)
            WHERE any(label IN labels(req) WHERE label IN $requirement_labels)
              AND coalesce(req.requirement_id, req.id, req.name) = $req_id
            MATCH (ns)-[:HAS_MODULE]-> (fn)
            WHERE any(label IN labels(fn) WHERE label IN $function_labels)
            MATCH (fn)-[:IMPLEMENTS]-> (req)
            RETURN coalesce(fn.name, fn.function_name) AS function_name,
                   fn.return_type AS return_type,
                   fn.source_file AS source_file,
                   coalesce(req.requirement_id, req.id, req.name) AS requirement_id
        """
        return self._run(query, {
            "project": project.lower(),
            "module":  module.lower(),
            "req_id":  requirement_id,
            "requirement_labels": NODE_TYPE_ALIASES["requirements"],
            "function_labels": NODE_TYPE_ALIASES["functions"],
        })

    def get_register_fields(
        self, project: str, module: str, register_name: str
    ) -> List[Dict[str, Any]]:
        """
        Get all RegisterField nodes for a Register, scoped through NodeSet.

        ⚠️  Relationship HAS_FIELD and property names — confirm (Q1, Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (r)
            WHERE any(label IN labels(r) WHERE label IN $register_labels)
              AND r.name = $reg_name
            MATCH (r)-[:HAS_FIELD|HAS_BITFIELD]->  (rf)
            WHERE any(label IN labels(rf) WHERE label IN $field_labels)
            RETURN r.name         AS register,
                   rf.name        AS field_name,
                   coalesce(rf.description, rf.brief, '') AS description,
                   coalesce(rf.bit_offset, rf.offset, 0) AS bit_offset,
                   coalesce(rf.bit_length, rf.width, 0) AS bit_length
            ORDER BY coalesce(rf.bit_offset, rf.offset, 0)
        """
        return self._run(query, {
            "project":  project.lower(),
            "module":   module.lower(),
            "reg_name": register_name,
            "register_labels": NODE_TYPE_ALIASES["registers"],
            "field_labels": NODE_TYPE_ALIASES["register_fields"],
        })

    def get_struct_members(
        self, project: str, module: str, struct_name: str
    ) -> List[Dict[str, Any]]:
        """
        Get all StructMember nodes for a Struct, scoped through NodeSet.

        ⚠️  Relationship HAS_MEMBER and property names — confirm (Q1, Q2).
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (s)
            WHERE any(label IN labels(s) WHERE label IN $struct_labels)
              AND s.name = $struct_name
            MATCH (s)-[:HAS_MEMBER|HAS_FIELD]-> (m)
            WHERE any(label IN labels(m) WHERE label IN $member_labels)
            RETURN s.name       AS struct,
                   m.name       AS member_name,
                   coalesce(m.type, m.data_type, '') AS member_type,
                   coalesce(m.bit_offset, m.offset, 0) AS bit_offset
            ORDER BY coalesce(m.bit_offset, m.offset, 0)
        """
        return self._run(query, {
            "project":     project.lower(),
            "module":      module.lower(),
            "struct_name": struct_name,
            "struct_labels": NODE_TYPE_ALIASES["structs"],
            "member_labels": NODE_TYPE_ALIASES["struct_members"],
        })

    # ─────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────────────────────────────

    def get_summary(self, project: str, module: str) -> Dict[str, Any]:
        """
        Get a count of all nodes linked to a NodeSet, broken down by type.

        Returns:
            {node_set, project, module, total, counts: {label: count}}
        """
        query = """
            MATCH (ns:NodeSet {project: $project, module: $module})
            -[:HAS_MODULE]-> (n)
            RETURN labels(n)[0] AS node_type, count(n) AS count
            ORDER BY count DESC
        """
        rows   = self._run(query, {"project": project.lower(), "module": module.lower()})
        counts = {r["node_type"]: r["count"] for r in rows}
        return {
            "node_set": f"ns_{project.lower()}_{module.lower()}",
            "project":  project,
            "module":   module,
            "total":    sum(counts.values()),
            "counts":   counts,
        }

    # ─────────────────────────────────────────────────────────────────────
    # CROSS-MODULE — always explicit, never default
    # ─────────────────────────────────────────────────────────────────────

    def get_cross_module(
        self,
        project:        str,
        source_module:  str,
        target_modules: List[str],
        node_type:      str = "Function",
        limit:          int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Cross-module traversal — explicit ONLY. Never called by default.

        Queries multiple NodeSets (source + targets) and returns combined
        results tagged with their source module.

        The DEPENDS_ON relationship between NodeSet anchors must exist first.
        ⚠️  DEPENDS_ON relationship — confirm with Ingestion team (Q1).

        Args:
            project:        Project identifier
            source_module:  Primary module, e.g. "cxpi"
            target_modules: Additional modules, e.g. ["spi", "can"]
            node_type:      Node label to retrieve
            limit:          Max results per module
        """
        all_results: List[Dict[str, Any]] = []
        for module in [source_module] + target_modules:
            query = f"""
                MATCH (ns:NodeSet {{project: $project, module: $module}})
                -[:HAS_MODULE]-> (n)
                WHERE any(label IN labels(n) WHERE label IN $labels)
                RETURN coalesce(n.name, n.function_name, n.requirement_id, n.document_id) AS name,
                       coalesce(n.id, n.node_id, n.requirement_id, n.document_id, n.name) AS id,
                       $module AS source_module
                LIMIT $limit
            """
            rows = self._run(query, {
                "project": project.lower(),
                "module":  module.lower(),
                "limit":   limit,
                "labels":  NODE_TYPE_ALIASES.get(node_type.lower() + "s", [node_type]),
            })
            all_results.extend(rows)

        logger.info(
            f"[ScopedQuery] Cross-module: {source_module} + {target_modules} "
            f"→ {len(all_results)} results"
        )
        return all_results

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE
    # ─────────────────────────────────────────────────────────────────────

    def _run(self, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(query, params)
                return [dict(record) for record in result]
        except Neo4jError as e:
            logger.error(f"[ScopedQuery] Neo4j error: {e}")
            return []
        except Exception as e:
            logger.error(f"[ScopedQuery] Unexpected error: {e}")
            return []

"""
Knowledge Intelligence Service — Sprint 7
============================================
Backend for Categories 2-4:
  Cat 2: API Intelligence (3 tools) — deep function understanding
  Cat 3: Dependency Analysis (3 tools) — call graphs, init ordering
  Cat 4: Traceability (4 tools) — ASPICE V-Model chains

All tools follow the "fuzzy search → pick best → enrich" pattern.
Works with SearchService for Neo4j queries, degrades gracefully without it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from src.HybridRAG.code.KG._kg_safety import sanitize_label

logger = logging.getLogger(__name__)


TRACEABILITY_REQUIREMENT_LABELS = [
    "SoftwareRequirement",
    "ProductRequirement",
    "StakeholderRequirement",
]


class KnowledgeIntelligenceService:
    """
    Combined service for API Intelligence, Dependency Analysis, and Traceability.

    Parameters
    ----------
    neo4j_driver : optional
        Neo4j driver for graph queries.
    search_service : optional
        SearchService instance for hybrid search.
    """

    def __init__(self, neo4j_driver=None, search_service=None, default_database: str = "neo4j"):
        self._neo4j = neo4j_driver
        self._search = search_service
        self._default_db = default_database
        self._use_element_id: Optional[bool] = None  # detected lazily

    def _db(self, ws: str) -> str:
        return self._default_db

    def _eid_fn(self) -> str:
        """Return the correct node-ID function name for this Neo4j version."""
        if self._use_element_id is None:
            if not self._neo4j:
                self._use_element_id = False
            else:
                try:
                    with self._neo4j.session(database=self._default_db) as s:
                        ver = s.run("CALL dbms.components() YIELD versions RETURN versions[0] AS v").single()["v"]
                    major = int(str(ver).split(".")[0])
                    self._use_element_id = major >= 5
                except Exception:
                    self._use_element_id = False
        return "elementId" if self._use_element_id else "id"

    def _run_cypher(self, cypher: str, params: Dict, ws: str = "illd") -> List[Dict]:
        """Execute a read-only Cypher query, return list of row dicts."""
        if not self._neo4j:
            return []
        try:
            from neo4j import READ_ACCESS
            with self._neo4j.session(database=self._db(ws), default_access_mode=READ_ACCESS) as s:
                return [dict(r) for r in s.run(cypher, params)]
        except Exception as e:
            logger.error("Cypher failed: %s", e)
            return []

    @staticmethod
    def _label_filter(alias: str, labels_param: str) -> str:
        return f"any(label IN labels({alias}) WHERE label IN ${labels_param})"

    def _fuzzy_find(self, name: str, labels: List[str], ws: str, limit: int = 5) -> List[Dict]:
        """Search across multiple labels for a fuzzy name match."""
        kw = name.lower()
        for label in labels:
            safe_label = sanitize_label(label)
            rows = self._run_cypher(
                f"MATCH (n:{safe_label}) WHERE toLower(coalesce(n.name,'')) CONTAINS $kw "
                f"OR toLower(coalesce(n.function_name,'')) CONTAINS $kw "
                f"OR toLower(coalesce(n.type_name,'')) CONTAINS $kw "
                f"OR toLower(coalesce(n.macro_name,'')) CONTAINS $kw "
                f"RETURN n, labels(n) AS lbl, {self._eid_fn()}(n) AS nid ORDER BY "
                f"CASE WHEN toLower(coalesce(n.name, n.type_name, n.macro_name, n.function_name, '')) = $exact THEN 0 "
                f"WHEN toLower(coalesce(n.name, n.type_name, n.macro_name, n.function_name, '')) STARTS WITH $kw THEN 1 ELSE 2 END "
                f"LIMIT $limit",
                {"kw": kw, "exact": kw, "limit": limit}, ws
            )
            if rows:
                results = []
                for r in rows:
                    props = dict(r["n"].items()) if hasattr(r["n"], "items") else r["n"]
                    props["_labels"] = r.get("lbl", [])
                    props["_node_id"] = r.get("nid")
                    # Keep _element_id for backward compat, prefer _node_id
                    props["_element_id"] = r["n"].element_id if hasattr(r["n"], "element_id") else None
                    results.append(props)
                return results
        return []

    def _get_neighbors(self, element_id, direction: str = "both",
                       rel_types: Optional[List[str]] = None, limit: int = 50,
                       ws: str = "illd") -> List[Dict]:
        """Get neighbors of a node by node ID (integer) or element ID (string)."""
        if element_id is None:
            return []
        if direction == "out":
            pattern = "(a)-[r]->(b)"
        elif direction == "in":
            pattern = "(a)<-[r]-(b)"
        else:
            pattern = "(a)-[r]-(b)"

        rel_filter = ""
        if rel_types:
            rel_filter = "AND type(r) IN $rels"

        eid_fn = self._eid_fn()
        id_clause = f"{eid_fn}(a) = $nid"
        params = {"nid": element_id, "rels": rel_types or [], "limit": limit}

        rows = self._run_cypher(
            f"MATCH {pattern} WHERE {id_clause} {rel_filter} "
            f"RETURN type(r) AS rel, labels(b)[0] AS lbl, "
            f"coalesce(b.name, b.function_name, b.type_name, b.macro_name, b.requirement_id) AS name, "
            f"b AS node, {eid_fn}(b) AS beid LIMIT $limit",
            params, ws
        )
        neighbors = []
        for r in rows:
            props = dict(r["node"].items()) if hasattr(r["node"], "items") else {}
            props["_node_id"] = r.get("beid")
            props["_element_id"] = r.get("beid")  # backward compat
            neighbors.append({
                "relationship": r["rel"],
                "type": r["lbl"],
                "name": r["name"],
                "properties": props,
            })
        return neighbors

    # ═══════════════════════════════════════════════════════════════════
    #  Category 2: API Intelligence
    # ═══════════════════════════════════════════════════════════════════

    def query_api_function(self, function_name: str, ws: str = "illd") -> Dict:
        """Fuzzy search → best match → enrich with 25+ fields."""
        labels = ["APIFunction", "DriverFunction", "Function", "SWUD_Function"]
        nodes = self._fuzzy_find(function_name, labels, ws)

        if not nodes:
            # Fallback: use SearchService hybrid search
            if self._search and self._search.available:
                sr = self._search.hybrid_search(function_name, max_results=5,
                                                 filter_by_node_type=labels, workspace_id=ws)
                if sr.get("results"):
                    return {"api_function": sr["results"][0], "matches_found": len(sr["results"]),
                            "source": "hybrid_search"}
            return {"api_function": None, "matches_found": 0}

        best = nodes[0]
        # Enrich with relationships
        eid = best.get("_node_id") or best.get("_element_id")
        if eid is not None:
            neighbors = self._get_neighbors(eid, ws=ws)
            best["relationships"] = neighbors
            best["related_requirements"] = [n for n in neighbors if "Requirement" in n.get("type", "")]
            best["called_by"] = [n["name"] for n in neighbors if n["relationship"] in ("CALLS", "CALLS_INTERNALLY") and n.get("name")]
            best["calls"] = [n["name"] for n in self._get_neighbors(eid, direction="out",
                              rel_types=["CALLS", "CALLS_INTERNALLY", "DEPENDS_ON"], ws=ws) if n.get("name")]

        return {"api_function": best, "matches_found": len(nodes)}

    def get_type_definition(self, struct_name: str, module: Optional[str] = None,
                            ws: str = "illd") -> Dict:
        """Fuzzy search for struct/enum/typedef → enrich with fields + related functions."""
        labels = ["DataStructure", "SWA_DataType", "Struct", "Enum", "TypeDef", "SWUD_TypeDefinition"]
        nodes = self._fuzzy_find(struct_name, labels, ws)

        if not nodes:
            if self._search and self._search.available:
                sr = self._search.hybrid_search(struct_name, max_results=5, workspace_id=ws)
                if sr.get("results"):
                    return sr["results"][0]
            return {"found": False, "struct_name": struct_name}

        best = nodes[0]
        eid = best.get("_node_id") or best.get("_element_id")
        if eid is not None:
            neighbors = self._get_neighbors(eid, ws=ws)
            best["related_functions"] = [n["name"] for n in neighbors
                                          if "Function" in n.get("type", "") and n.get("name")]
            # Collect fields from multiple possible relationship types
            # iLLD: HAS_FIELD, HAS_MEMBER | MCAL: may use additional types like CONTAINS_MEMBER, HAS_PARAMETER
            field_relationships = ("HAS_FIELD", "HAS_MEMBER", "CONTAINS_MEMBER", "HAS_PARAMETER", 
                                  "SWA_HAS_MEMBER", "SWUD_HAS_FIELD")
            best["fields"] = [n for n in neighbors if n["relationship"] in field_relationships]

        return best

    def generate_initialization_code(self, struct_name: str,
                                      user_overrides: Optional[Dict] = None,
                                      variable_name: Optional[str] = None,
                                      ws: str = "illd") -> Dict:
        """Generate C initializer code with KG defaults + user overrides. iLLD-specific."""
        type_def = self.get_type_definition(struct_name, ws=ws)
        var = variable_name or struct_name.split("_")[-1].lower() + "_config"
        overrides = user_overrides or {}

        # Build C code from type definition
        lines = [f"/* Auto-generated initializer for {struct_name} */",
                 f"{struct_name} {var};"]

        # Track which overrides were actually applied and which were rejected
        applied_overrides = []
        rejected_overrides = []

        # If we found fields, generate member assignments
        fields = type_def.get("fields", [])
        if fields:
            lines.append(f"/* Initialize with defaults + overrides */")
            field_names = set()
            for f in fields:
                fname = f.get("name", f.get("properties", {}).get("name", ""))
                if fname:
                    field_names.add(fname)
                    if fname in overrides:
                        lines.append(f"{var}.{fname} = {overrides[fname]};  /* user override */")
                        applied_overrides.append(fname)
                    else:
                        default = f.get("properties", {}).get("default_value", "0")
                        lines.append(f"{var}.{fname} = {default};  /* KG default */")
            
            # Track which override keys don't correspond to struct fields
            for k in overrides.keys():
                if k not in field_names:
                    rejected_overrides.append(k)
        else:
            # No field info — generate placeholder with overrides
            lines.append(f"/* Field details not available — applying overrides only */")
            for k, v in overrides.items():
                lines.append(f"{var}.{k} = {v};")
                applied_overrides.append(k)

        c_code = "\n".join(lines)
        return {"struct_name": struct_name, "variable_name": var,
                "c_code": c_code, "overrides_applied": applied_overrides,
                "overrides_rejected": rejected_overrides,
                "fields_from_kg": len(fields)}

    # ═══════════════════════════════════════════════════════════════════
    #  Category 3: Dependency Analysis
    # ═══════════════════════════════════════════════════════════════════

    def query_dependencies(self, function_name: str, module_name: Optional[str] = None,
                           max_depth: int = 3, include_hardware: bool = False,
                           ws: str = "illd") -> Dict:
        """Resolve direct + transitive dependencies using all outgoing relationships.

        For MCAL: discovers SWA_USES_TYPE, SWA_CONTAINS_PARAM, SWUD_ALLOCATED_IN, etc.
        For ILLD: discovers CALLS, DEPENDS_ON, HAS_PARAMETER, OF_TYPE, etc.
        Uses Cypher variable-length paths to find ALL upstream links.
        """
        # Broader label set for MCAL (SWA_Function, SWUD_Function) and ILLD
        labels = ["APIFunction", "DriverFunction", "Function",
                  "SWA_Function", "SWUD_Function"]
        nodes = self._fuzzy_find(function_name, labels, ws)
        if not nodes:
            return {"function_name": function_name, "direct_dependencies": [],
                    "transitive_dependencies": [], "call_order": [], "found": False}

        nid = nodes[0].get("_node_id")
        if nid is None:
            return {"function_name": function_name, "direct_dependencies": [],
                    "transitive_dependencies": [], "call_order": [], "found": True}

        # Direct dependencies: ALL outgoing relationships from the matched node
        eid_fn = self._eid_fn()
        direct_rows = self._run_cypher(
            f"MATCH (a)-[r]->(b) WHERE {eid_fn}(a) = $nid "
            f"RETURN type(r) AS rel, labels(b) AS lbls, "
            f"coalesce(b.name, b.function_name, b.type_name, b.macro_name, "
            f"  b.requirement_id, b.decision_id) AS name, "
            f"properties(b) AS props, {eid_fn}(b) AS beid "
            f"LIMIT 100",
            {"nid": nid}, ws
        )
        direct = []
        for r in direct_rows:
            direct.append({
                "name": r.get("name") or "?",
                "relationship": r["rel"],
                "type": (r.get("lbls") or [""])[0],
                "element_id": r.get("beid"),
            })

        # Transitive dependencies: variable-length paths (depth 2..max_depth)
        transitive = []
        seen_names = {d["name"] for d in direct}
        if max_depth >= 2:
            transitive_rows = self._run_cypher(
                f"MATCH path = (a)-[*2..{int(max_depth)}]->(b) WHERE {eid_fn}(a) = $nid "
                f"AND {eid_fn}(b) <> $nid "
                "WITH b, min(length(path)) AS depth "
                "RETURN labels(b) AS lbls, "
                "coalesce(b.name, b.function_name, b.type_name, b.macro_name, "
                "  b.requirement_id) AS name, "
                "depth "
                "ORDER BY depth LIMIT 100",
                {"nid": nid}, ws
            )
            for r in transitive_rows:
                name = r.get("name") or "?"
                if name not in seen_names:
                    transitive.append({
                        "name": name,
                        "type": (r.get("lbls") or [""])[0],
                        "depth": r.get("depth", 2),
                    })
                    seen_names.add(name)

        # Topological init sequence (reverse of dependency order)
        all_deps = [d["name"] for d in direct] + [d["name"] for d in transitive]
        init_sequence = list(reversed(list(dict.fromkeys(all_deps)))) + [function_name]

        return {
            "function_name": function_name, "found": True,
            "direct_dependencies": direct, "transitive_dependencies": transitive,
            "init_sequence": init_sequence, "call_order": init_sequence,
        }

    def validate_api_usage(self, function_sequence: List[str], ws: str = "illd") -> Dict:
        """Validate call sequence against dependency graph."""
        if len(function_sequence) < 2:
            return {"is_valid": True, "violations": [], "function_sequence": function_sequence}

        violations = []
        # For each function, check if its dependencies appear before it
        for i, fn in enumerate(function_sequence):
            deps = self.query_dependencies(fn, ws=ws)
            required_before = {d["name"] for d in deps.get("direct_dependencies", [])}
            already_called = set(function_sequence[:i])
            missing = required_before - already_called
            if missing:
                violations.append({
                    "function": fn, "position": i,
                    "missing_prerequisites": list(missing),
                    "message": f"'{fn}' requires {missing} to be called first",
                })

        suggested = self._suggest_order(function_sequence, ws)

        return {
            "is_valid": len(violations) == 0,
            "violations": violations,
            "function_sequence": function_sequence,
            "suggested_order": suggested,
        }

    def _suggest_order(self, functions: List[str], ws: str) -> List[str]:
        """Suggest correct order based on dependency graph."""
        # Simple: resolve deps for all, topological sort
        dep_map: Dict[str, set] = {}
        for fn in functions:
            deps = self.query_dependencies(fn, ws=ws)
            dep_names = {d["name"] for d in deps.get("direct_dependencies", [])}
            dep_map[fn] = dep_names & set(functions)  # Only deps within our set

        # Kahn's algorithm
        in_degree = {fn: 0 for fn in functions}
        for fn, deps in dep_map.items():
            for d in deps:
                if d in in_degree:
                    in_degree[fn] = in_degree.get(fn, 0) + 1

        queue = [fn for fn, deg in in_degree.items() if deg == 0]
        result = []
        while queue:
            fn = queue.pop(0)
            result.append(fn)
            for other, deps in dep_map.items():
                if fn in deps:
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        # Add any remaining (cycle or disconnected)
        for fn in functions:
            if fn not in result:
                result.append(fn)
        return result

    def detect_polling_requirements(self, function_names: List[str],
                                     module: Optional[str] = None,
                                     ws: str = "illd") -> Dict:
        """Detect which APIs require status polling after invocation."""
        results = {}
        # Patterns indicating async operations needing polling
        ASYNC_PATTERNS = re.compile(r'(send|transmit|transfer|start|trigger|request|enable)', re.IGNORECASE)
        STATUS_PATTERNS = re.compile(r'(getStatus|isReady|isBusy|waitFor|pollStatus|checkComplete)', re.IGNORECASE)

        for fn_name in function_names:
            needs_polling = bool(ASYNC_PATTERNS.search(fn_name))
            status_fn = None
            completion_value = None

            if needs_polling:
                # Search for related status functions
                deps = self.query_dependencies(fn_name, ws=ws)
                for d in deps.get("direct_dependencies", []) + deps.get("transitive_dependencies", []):
                    if STATUS_PATTERNS.search(d.get("name", "")):
                        status_fn = d["name"]
                        break

                # If no status function found in graph, infer from naming
                if not status_fn:
                    base = fn_name.rsplit("_", 1)[0] if "_" in fn_name else fn_name
                    status_fn = f"{base}_getStatus"
                    completion_value = "IfxSts_Status_complete"

            results[fn_name] = {
                "needs_polling": needs_polling,
                "status_function": status_fn,
                "completion_value": completion_value or "TRUE",
                "timeout_recommended": needs_polling,
            }

        return {"polling": results, "functions_checked": len(function_names)}

    # ═══════════════════════════════════════════════════════════════════
    #  Category 4: Traceability
    # ═══════════════════════════════════════════════════════════════════

    def find_requirement_traces(self, requirement_id: str, include_tests: bool = True,
                                 include_results: bool = True, ws: str = "illd") -> Dict:
        """Full V-Model chain: Req → Code → Test → Result."""
        # Find the requirement node
        rows = self._run_cypher(
            "MATCH (r) WHERE r.requirement_id = $rid OR r.document_id = $rid OR r.name = $rid "
            "RETURN r, labels(r) AS lbl, id(r) AS nid LIMIT 1",
            {"rid": requirement_id}, ws
        )
        if not rows:
            return {"requirement_id": requirement_id, "found": False, "chain": []}

        req_node = dict(rows[0]["r"].items()) if hasattr(rows[0]["r"], "items") else {}
        req_nid = rows[0]["nid"]
        chain = [{"level": "requirement", "id": requirement_id, "properties": req_node}]

        # Follow IMPLEMENTS → code
        eid_fn = self._eid_fn()
        code_rows = self._run_cypher(
            f"MATCH (r)-[:IMPLEMENTS]->(f) WHERE {eid_fn}(r) = $nid "
            f"RETURN f, labels(f)[0] AS lbl, {eid_fn}(f) AS fnid",
            {"nid": req_nid}, ws
        )
        for cr in code_rows:
            props = dict(cr["f"].items()) if hasattr(cr["f"], "items") else {}
            chain.append({"level": "code", "type": cr["lbl"],
                          "name": props.get("name", props.get("function_name", "?")), "properties": props})

            # Follow TRACES_TO → test
            if include_tests:
                test_rows = self._run_cypher(
                    f"MATCH (f)-[:TRACES_TO]->(t) WHERE {eid_fn}(f) = $fnid "
                    f"RETURN t, labels(t)[0] AS lbl",
                    {"fnid": cr["fnid"]}, ws
                )
                for tr in test_rows:
                    tprops = dict(tr["t"].items()) if hasattr(tr["t"], "items") else {}
                    chain.append({"level": "test", "type": tr["lbl"],
                                  "name": tprops.get("name", tprops.get("test_case_id", tprops.get("global_id", "?"))), "properties": tprops})

        return {"requirement_id": requirement_id, "found": True, "chain": chain,
                "chain_length": len(chain)}

    def build_traceability_matrix(self, module_name: str, output_format: str = "json",
                                   ws: str = "illd") -> Dict:
        """Module-wide coverage matrix."""
        rows = self._run_cypher(
            "MATCH (r) "
            "WHERE any(label IN labels(r) WHERE label IN $requirement_labels) "
            "AND toLower(coalesce(r.module, '')) = toLower($mod) "
            "OPTIONAL MATCH (r)-[:IMPLEMENTS]->(f) "
            "OPTIONAL MATCH (f)-[:TRACES_TO]->(t) "
            "RETURN r.requirement_id AS req_id, r.name AS req_name, "
            "collect(DISTINCT coalesce(f.name, f.function_name)) AS implementations, "
            "collect(DISTINCT coalesce(t.name, t.test_case_id, t.global_id)) AS tests",
            {"mod": module_name, "requirement_labels": TRACEABILITY_REQUIREMENT_LABELS}, ws
        )

        matrix = []
        for r in rows:
            matrix.append({
                "requirement_id": r["req_id"],
                "requirement_name": r["req_name"],
                "implementations": r["implementations"],
                "tests": r["tests"],
                "has_code": len(r["implementations"]) > 0,
                "has_tests": len(r["tests"]) > 0,
            })

        coverage = {
            "total_requirements": len(matrix),
            "with_code": sum(1 for m in matrix if m["has_code"]),
            "with_tests": sum(1 for m in matrix if m["has_tests"]),
        }

        return {"module": module_name, "matrix": matrix, "coverage": coverage,
                "format": output_format}

    def find_coverage_gaps(self, module_name: str, gap_type: str = "all",
                           severity: str = "all", ws: str = "illd") -> Dict:
        """Missing links in requirement-code-test chains by severity."""
        matrix = self.build_traceability_matrix(module_name, ws=ws)
        gaps = []

        for entry in matrix.get("matrix", []):
            rid = entry.get("requirement_id", "?")
            if not entry["has_code"]:
                gaps.append({"requirement_id": rid, "gap_type": "missing_code",
                             "severity": "high", "message": f"Requirement {rid} has no implementation"})
            if entry["has_code"] and not entry["has_tests"]:
                gaps.append({"requirement_id": rid, "gap_type": "missing_test",
                             "severity": "medium", "message": f"Requirement {rid} implemented but not tested"})

        # Filter
        if gap_type != "all":
            gaps = [g for g in gaps if g["gap_type"] == gap_type]
        if severity != "all":
            gaps = [g for g in gaps if g["severity"] == severity]

        return {"module": module_name, "gaps": gaps, "total_gaps": len(gaps)}

    def analyze_hw_sw_links(self, module_name: str, include_undocumented: bool = True,
                             include_peripheral_map: bool = False, ws: str = "illd") -> Dict:
        """Map HW register usage to functions, detect undocumented accesses.

        Supports two graph schemas:
        - ILLD: Function -[:HAS_PARAMETER]-> Parameter -[:OF_TYPE]-> Register/Struct
        - MCAL: SWA_Function -[:SWA_USES_TYPE]-> SWA_DataType + SWA_HwPeripheral + SWA_SwDependency
        """
        if ws.lower() == "mcal":
            return self._analyze_hw_sw_links_mcal(module_name, include_undocumented,
                                                   include_peripheral_map, ws)
        return self._analyze_hw_sw_links_illd(module_name, include_undocumented,
                                               include_peripheral_map, ws)

    # ── ILLD schema ──────────────────────────────────────────────────
    def _analyze_hw_sw_links_illd(self, module_name: str, include_undocumented: bool,
                                   include_peripheral_map: bool, ws: str) -> Dict:
        prefix = f"Ifx{module_name.capitalize()}"

        # --- Direct ACCESSES links ---
        direct_rows = self._run_cypher(
            "MATCH (f:Function)-[:ACCESSES]->(r:Register) "
            "WHERE toLower(coalesce(f.module, '')) = toLower($mod) "
            "   OR f.name STARTS WITH $prefix "
            "RETURN DISTINCT f.name AS function_name, r.name AS register_name, "
            "r.description AS register_desc",
            {"mod": module_name, "prefix": prefix}, ws
        )

        # --- Indirect links: Function -> Parameter -> OF_TYPE -> Register/Struct ---
        indirect_rows = self._run_cypher(
            "MATCH (f:Function)-[:HAS_PARAMETER]->(p:Parameter)-[:OF_TYPE]->(t) "
            "WHERE (toLower(coalesce(f.module, '')) = toLower($mod) "
            "       OR f.name STARTS WITH $prefix) "
            "  AND (t:Register OR t:Struct) "
            "RETURN DISTINCT f.name AS function_name, t.name AS register_name, "
            "coalesce(t.description, '') AS register_desc, p.name AS param_name",
            {"mod": module_name, "prefix": prefix}, ws
        )

        seen = set()
        links = []
        for r in direct_rows:
            key = (r["function_name"], r["register_name"])
            if key not in seen:
                seen.add(key)
                links.append({"function": r["function_name"], "register": r["register_name"],
                              "description": r.get("register_desc", ""), "link_type": "direct"})
        for r in indirect_rows:
            key = (r["function_name"], r["register_name"])
            if key not in seen:
                seen.add(key)
                links.append({"function": r["function_name"], "register": r["register_name"],
                              "description": r.get("register_desc", ""),
                              "link_type": "via_parameter", "parameter": r["param_name"]})

        undocumented = []
        if include_undocumented:
            unlinked = self._run_cypher(
                "MATCH (f:Function) "
                "WHERE (toLower(coalesce(f.module, '')) = toLower($mod) "
                "       OR f.name STARTS WITH $prefix) "
                "  AND toLower(f.name) CONTAINS 'init' "
                "  AND NOT (f)-[:HAS_PARAMETER]->(:Parameter)-[:OF_TYPE]->(:Register) "
                "  AND NOT (f)-[:HAS_PARAMETER]->(:Parameter)-[:OF_TYPE]->(:Struct) "
                "  AND NOT (f)-[:ACCESSES]->(:Register) "
                "RETURN f.name AS fn",
                {"mod": module_name, "prefix": prefix}, ws
            )
            undocumented = [{"function": r["fn"],
                            "issue": "Init function without documented register access"}
                           for r in unlinked]

        peripheral_map = []
        if include_peripheral_map:
            pmap_rows = self._run_cypher(
                "MATCH (hr:HardwareRegister)-[:HAS_FIELD]->(rf:RegisterField) "
                "OPTIONAL MATCH (rf)-[:HAS_ACCESS_TYPE]->(am:AccessMode) "
                "RETURN hr.name AS hw_register, rf.name AS field, "
                "coalesce(rf.description, '') AS field_desc, "
                "coalesce(am.name, 'unknown') AS access_mode "
                "ORDER BY hr.name, rf.name",
                {}, ws
            )
            peripheral_map = [{"hw_register": r["hw_register"], "field": r["field"],
                               "field_desc": r["field_desc"], "access_mode": r["access_mode"]}
                              for r in pmap_rows]

        result: Dict = {
            "module": module_name, "hw_sw_links": links,
            "undocumented_accesses": undocumented,
            "total_links": len(links), "total_undocumented": len(undocumented),
        }
        if include_peripheral_map:
            result["peripheral_map"] = peripheral_map
            result["total_peripheral_fields"] = len(peripheral_map)
        return result

    # ── MCAL schema (SWA_*/SWUD_* labels) ───────────────────────────
    def _analyze_hw_sw_links_mcal(self, module_name: str, include_undocumented: bool,
                                   include_peripheral_map: bool, ws: str) -> Dict:
        mod_upper = module_name.upper()

        # --- SWA_Function -> SWA_USES_TYPE -> SWA_DataType ---
        type_rows = self._run_cypher(
            "MATCH (f:SWA_Function)-[:SWA_USES_TYPE]->(dt:SWA_DataType) "
            "WHERE toLower(coalesce(f.module, '')) = toLower($mod) "
            "RETURN DISTINCT f.function_name AS function_name, "
            "dt.type_name AS type_name, coalesce(dt.description, '') AS type_desc",
            {"mod": mod_upper}, ws
        )

        links = [{"function": r["function_name"], "register": r["type_name"],
                  "description": r["type_desc"], "link_type": "swa_uses_type"}
                 for r in type_rows]

        # --- SWA_SwDependency (register-access macros/APIs) ---
        dep_rows = self._run_cypher(
            "MATCH (d:SWA_SwDependency) "
            "WHERE d.module = $mod OR d.source_document CONTAINS $mod "
            "RETURN DISTINCT d.api_name AS api_name, d.dependency_type AS dep_type, "
            "coalesce(d.description, '') AS dep_desc",
            {"mod": mod_upper}, ws
        )

        hw_deps = [{"api_name": r["api_name"], "dependency_type": r["dep_type"],
                    "description": r["dep_desc"]}
                   for r in dep_rows]

        # --- Undocumented: init functions with no type links ---
        undocumented = []
        if include_undocumented:
            unlinked = self._run_cypher(
                "MATCH (f:SWA_Function) "
                "WHERE toLower(coalesce(f.module, '')) = toLower($mod) "
                "  AND toLower(coalesce(f.function_name, '')) CONTAINS 'init' "
                "  AND NOT (f)-[:SWA_USES_TYPE]->(:SWA_DataType) "
                "RETURN f.function_name AS fn",
                {"mod": mod_upper}, ws
            )
            undocumented = [{"function": r["fn"],
                            "issue": "Init function without documented type usage"}
                           for r in unlinked]

        # --- Peripheral map: SWA_HwPeripheral ---
        peripheral_map = []
        if include_peripheral_map:
            hw_rows = self._run_cypher(
                "MATCH (hp:SWA_HwPeripheral) "
                "WHERE toLower(coalesce(hp.module, '')) = toLower($mod) "
                "RETURN hp.peripheral_name AS name, hp.peripheral_type AS type, "
                "coalesce(hp.hw_events, '[]') AS hw_events, "
                "coalesce(hp.prq_references, '[]') AS prq_references",
                {"mod": mod_upper}, ws
            )
            peripheral_map = [{"peripheral_name": r["name"], "peripheral_type": r["type"],
                               "hw_events": r["hw_events"], "prq_references": r["prq_references"]}
                              for r in hw_rows]

        result: Dict = {
            "module": module_name, "hw_sw_links": links,
            "hw_dependencies": hw_deps,
            "undocumented_accesses": undocumented,
            "total_links": len(links), "total_hw_dependencies": len(hw_deps),
            "total_undocumented": len(undocumented),
        }
        if include_peripheral_map:
            result["peripheral_map"] = peripheral_map
            result["total_peripherals"] = len(peripheral_map)
        return result

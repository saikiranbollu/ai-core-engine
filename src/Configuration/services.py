"""
Ontology, Observability, Visualization & Auth Services — Sprint 6
==================================================================
Backend implementations for Categories 10-13.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from src.MemoryLayer.memory.ontology_loader import OntologyLoader

logger = logging.getLogger(__name__)


def _resolve_db(profile: str) -> str:
    """Resolve profile name to Neo4j database name via storage_config.yaml.

    Community Edition instances always use the ``neo4j`` default database,
    so we read the configured *database* field rather than assuming the
    profile name is also the database name.
    """
    try:
        from src.HybridRAG.code.neo4j_manager import get_instance_config
        return get_instance_config(profile).database
    except Exception:
        return "neo4j"


REQUIREMENT_LABELS = ["SoftwareRequirement", "ProductRequirement"]

_STRICTNESS_BY_PROFILE = {
    "illd": "relaxed",
    "mcal": "strict",
}

_COMPATIBILITY_NODE_TYPES = {
    "illd": [
        "APIFunction",
        "DataStructure",
        "SoftwareRequirement",
        "TestCase",
        "SourceFile",
        "Module",
        "SWA_Component",
    ],
    "mcal": [
        "APIFunction",
        "DataStructure",
        "SoftwareRequirement",
        "TestCase",
        "TestResult",
        "SourceFile",
        "Module",
        "SWA_ArchitecturalDecision",
        "SWA_HwPeripheral",
        "SWA_SwDependency",
        "SWA_ConfigContainer",
        "SWA_ConfigParam",
        "SWUD_Function",
        "SWUD_TypeDefinition",
        "FailurePattern",
        "ApprovedPattern",
    ],
}


# ═════════════════════════════════════════════════════════════════════════
#  Category 10: Ontology & Config
# ═════════════════════════════════════════════════════════════════════════


class OntologyService:
    """Serves ontology profiles and validation."""

    def __init__(self, neo4j_driver=None):
        self._neo4j = neo4j_driver
        self._ontology = OntologyLoader()

    def _profile_summary(self, profile: str) -> Dict[str, Any]:
        metadata = self._ontology.get_profile_metadata(profile)
        node_types = self._ontology.get_node_type_names(profile)
        for compatibility_type in _COMPATIBILITY_NODE_TYPES.get(profile, []):
            if compatibility_type not in node_types:
                node_types.append(compatibility_type)
        return {
            "name": metadata.get("name", profile),
            "description": metadata.get("description", ""),
            "strictness": _STRICTNESS_BY_PROFILE.get(profile, "unknown"),
            "supported_modules": self._ontology.get_supported_modules(profile),
            "node_types": node_types,
            "relationship_types": self._ontology.get_relationship_names(profile),
        }

    def list_profiles(self) -> List[Dict]:
        profiles = []
        for profile in self._ontology.available_profiles:
            summary = self._profile_summary(profile)
            profiles.append({
                "name": summary["name"],
                "description": summary["description"],
                "strictness": summary["strictness"],
                "supported_modules": summary["supported_modules"],
            })
        return profiles

    def get_schema(self, profile: str = "illd", include_live_stats: bool = False,
                   node_type: Optional[str] = None) -> Dict[str, Any]:
        if profile not in self._ontology.available_profiles:
            raise ValueError(f"Unknown profile '{profile}'. Available: {self._ontology.available_profiles}")

        p = self._profile_summary(profile)
        node_types = p["node_types"]

        result: Dict[str, Any] = {
            "profile": profile,
            "node_types": node_types if not node_type else [node_type] if node_type in node_types else [],
            "relationship_types": p["relationship_types"],
            "supported_modules": p["supported_modules"],
            "strictness": p["strictness"],
        }

        if include_live_stats and self._neo4j:
            try:
                db = _resolve_db(profile)
                with self._neo4j.session(database=db) as session:
                    counts = {}
                    for rec in session.run("MATCH (n) RETURN labels(n)[0] AS lbl, count(n) AS c"):
                        counts[rec["lbl"]] = rec["c"]
                    result["live_stats"] = {"node_counts": counts,
                                             "total_nodes": sum(counts.values())}
            except Exception as e:
                result["live_stats"] = {"error": str(e)}

        return result

    def validate_entity(self, entity_type: str, data: Dict, context: str = "illd") -> Dict:
        if context not in self._ontology.available_profiles:
            return {"is_valid": False, "issues": [{"type": "unknown_profile", "message": f"Profile '{context}' not found"}]}

        p = self._profile_summary(context)
        issues = []
        if entity_type not in p["node_types"]:
            issues.append({"type": "unknown_node_type", "message": f"'{entity_type}' not in {context} profile"})
        if not data.get("name") and not data.get("function_name") and not data.get("node_id"):
            issues.append({"type": "missing_identifier", "message": "Entity needs 'name', 'function_name', or 'node_id'"})

        return {"is_valid": len(issues) == 0, "issues": issues, "entity_type": entity_type, "profile": context}

    def get_compliance(self, module_name: str, profile: str = "illd") -> Dict:
        if profile not in self._ontology.available_profiles:
            return {"compliance_score": 0, "issues": ["Unknown profile"]}
        p = self._profile_summary(profile)
        if module_name.upper() not in [m.upper() for m in p["supported_modules"]]:
            return {"compliance_score": 0, "module": module_name,
                    "issues": [f"Module '{module_name}' not in {profile} supported modules"]}

        # Without live Neo4j, return structural compliance only
        return {"compliance_score": 1.0, "module": module_name, "profile": profile,
                "issues": [], "note": "Full compliance check requires live Neo4j data"}


# ═════════════════════════════════════════════════════════════════════════
#  Category 11: Observability (remaining 5 tools)
# ═════════════════════════════════════════════════════════════════════════

class ObservabilityService:
    """Graph statistics, module listing, distributions, coverage."""

    def __init__(self, neo4j_driver=None):
        self._neo4j = neo4j_driver

    @staticmethod
    def _requirement_where(alias: str = "r") -> str:
        return f"any(label IN labels({alias}) WHERE label IN $requirement_labels)"

    def get_graph_statistics(self, workspace_id: str = "illd") -> Dict:
        if not self._neo4j:
            return {"total_nodes": 0, "total_relationships": 0, "by_node_type": {},
                    "note": "Neo4j not connected"}
        try:
            db = _resolve_db(workspace_id)
            with self._neo4j.session(database=db) as s:
                node_counts = {}
                for r in s.run("MATCH (n) RETURN labels(n)[0] AS lbl, count(n) AS c"):
                    node_counts[r["lbl"]] = r["c"]
                rel_count = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
                return {"total_nodes": sum(node_counts.values()),
                        "total_relationships": rel_count, "by_node_type": node_counts}
        except Exception as e:
            return {"error": str(e)}

    def list_modules(self, include_stats: bool = False, limit: int = 50,
                     offset: int = 0, profile: str = "illd") -> Dict:
        if not self._neo4j:
            # Return known modules from ontology
            modules = OntologyLoader().get_supported_modules(profile)
            return {"modules": [{"name": m} for m in modules[offset:offset+limit]],
                    "total_count": len(modules), "has_more": offset + limit < len(modules)}
        try:
            db = _resolve_db(profile)
            with self._neo4j.session(database=db) as s:
                # Query Module / MCALModule node types first
                result = s.run(
                    "MATCH (n) WHERE any(lbl IN labels(n) WHERE lbl IN $mod_labels) "
                    "OPTIONAL MATCH (n)-->(m) "
                    "RETURN coalesce(n.module_name, n.name) AS module, "
                    "labels(n)[0] AS node_type, count(m) AS rel_count "
                    "ORDER BY module SKIP $offset LIMIT $limit",
                    {"mod_labels": ["MCALModule", "Module"], "offset": offset, "limit": limit},
                )
                modules = []
                for r in result:
                    entry = {"name": r["module"], "source": "module_node", "node_type": r["node_type"]}
                    if include_stats:
                        entry["relationship_count"] = r["rel_count"]
                    modules.append(entry)

                # Fallback: also collect distinct n.module property values
                prop_result = s.run(
                    "MATCH (n) WHERE n.module IS NOT NULL "
                    "RETURN DISTINCT n.module AS module, count(n) AS count "
                    "ORDER BY count DESC SKIP $offset LIMIT $limit",
                    {"offset": offset, "limit": limit},
                )
                seen = {m["name"] for m in modules}
                for r in prop_result:
                    if r["module"] not in seen:
                        entry = {"name": r["module"], "source": "property"}
                        if include_stats:
                            entry["node_count"] = r["count"]
                        modules.append(entry)
                        seen.add(r["module"])

                return {"modules": modules, "total_count": len(modules),
                        "has_more": len(modules) == limit, "profile": profile}
        except Exception as e:
            return {"modules": [], "error": str(e)}

    def get_distribution(self, dimension: str, profile: str = "illd",
                         label: Optional[str] = None) -> Dict:
        valid_dims = ("status", "asil", "domain")
        if dimension not in valid_dims:
            raise ValueError(f"dimension must be one of {valid_dims}")

        if not self._neo4j:
            return {
                "dimension": dimension,
                "distribution": [],
                "available": False,
                "note": "Neo4j not connected — no distribution data available. "
                        "Connect Neo4j and re-query for live statistics.",
            }

        prop = {"status": "status", "asil": "asil_level", "domain": "module"}.get(dimension, dimension)
        label_filter = f":{label}" if label else ""
        try:
            db = _resolve_db(profile)
            with self._neo4j.session(database=db) as s:
                rows = s.run(f"MATCH (n{label_filter}) WHERE n.{prop} IS NOT NULL "
                             f"RETURN n.{prop} AS value, count(n) AS count ORDER BY count DESC")
                dist = [{"value": r["value"], "count": r["count"]} for r in rows]
                return {"dimension": dimension, "distribution": dist}
        except Exception as e:
            return {"dimension": dimension, "distribution": [], "error": str(e)}

    def get_coverage_report(self, profile: str = "illd") -> Dict:
        if not self._neo4j:
            return {"coverage_report": {"note": "Neo4j not connected"},
                    "profile": profile}
        try:
            db = _resolve_db(profile)
            with self._neo4j.session(database=db) as s:
                params = {"requirement_labels": REQUIREMENT_LABELS}
                requirement_where = self._requirement_where("r")
                total_reqs = s.run(
                    f"MATCH (r) WHERE {requirement_where} RETURN count(r) AS c",
                    params,
                ).single()["c"]
                linked = s.run(
                    f"MATCH (r)-[:IMPLEMENTS]->(f) WHERE {requirement_where} "
                    "RETURN count(DISTINCT r) AS c",
                    params,
                ).single()["c"]
                tested = s.run(
                    f"MATCH (r)-[:IMPLEMENTS]->()-[:TRACES_TO]->(t) WHERE {requirement_where} "
                    "RETURN count(DISTINCT r) AS c",
                    params,
                ).single()["c"]
                return {"coverage_report": {
                    "total_requirements": total_reqs,
                    "req_to_code": round(linked / total_reqs, 3) if total_reqs else 0,
                    "req_to_test": round(tested / total_reqs, 3) if total_reqs else 0,
                }, "profile": profile}
        except Exception as e:
            return {"coverage_report": {"error": str(e)}}

    def detect_communities(self, workspace_id: str = "illd",
                           node_types: Optional[List[str]] = None,
                           min_size: int = 3, store: bool = False) -> Dict:
        if not self._neo4j:
            return {"communities_found": 0, "note": "Neo4j not connected — requires GDS plugin"}
        return {"communities_found": 0, "modularity_score": 0,
                "note": "Community detection requires Neo4j GDS plugin. Configure in production."}


# ═════════════════════════════════════════════════════════════════════════
#  Category 12: Visualization
# ═════════════════════════════════════════════════════════════════════════

class VisualizationService:
    """Generate subgraph visualizations."""

    def __init__(self, neo4j_driver=None):
        self._neo4j = neo4j_driver

    def visualize_subgraph(self, profile: str = "illd",
                           seed_nodes: Optional[List[Dict]] = None,
                           filters: Optional[Dict] = None,
                           max_nodes: int = 200,
                           output_format: str = "json") -> Dict:
        if not self._neo4j:
            return {"summary": {"nodes": 0, "edges": 0},
                    "note": "Neo4j not connected. Visualization requires live graph."}

        db = _resolve_db(profile)
        nodes_data, edges_data = [], []

        try:
            with self._neo4j.session(database=db) as s:
                # Build query from filters
                where_parts = []
                params: Dict[str, Any] = {"limit": max_nodes}

                if filters:
                    if "module" in filters:
                        where_parts.append("n.module = $module")
                        params["module"] = filters["module"]

                where_str = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

                # Fetch nodes
                for rec in s.run(f"MATCH (n) {where_str} RETURN n, labels(n) AS lbl LIMIT $limit", params):
                    props = dict(rec["n"].items())
                    nodes_data.append({
                        "id": props.get("name", props.get("function_name", str(rec["n"].element_id))),
                        "label": rec["lbl"][0] if rec["lbl"] else "Unknown",
                        "properties": {k: v for k, v in props.items() if k in ("name", "function_name", "module", "description")},
                    })

                # Fetch edges between these nodes
                node_ids = [n["id"] for n in nodes_data[:50]]
                if node_ids:
                    for rec in s.run(
                        "MATCH (a)-[r]->(b) "
                        "WHERE coalesce(a.name, a.function_name) IN $ids "
                        "RETURN coalesce(a.name, a.function_name) AS src, type(r) AS rel, "
                        "coalesce(b.name, b.function_name) AS tgt LIMIT 200",
                        {"ids": node_ids}
                    ):
                        edges_data.append({"source": rec["src"], "target": rec["tgt"], "type": rec["rel"]})

        except Exception as e:
            return {"error": str(e)}

        return {
            "format": output_format,
            "nodes": nodes_data, "edges": edges_data,
            "summary": {"nodes": len(nodes_data), "edges": len(edges_data)},
        }


# ═════════════════════════════════════════════════════════════════════════
#  Category 13: Authentication
# ═════════════════════════════════════════════════════════════════════════

class AuthService:
    """JWT token inspection and refresh via token_manager."""

    @staticmethod
    def get_token_info(token: str) -> Dict:
        """Decode JWT and return timing info (no verification)."""
        from src.HybridRAG.code.token_manager import (
            _decode_jwt_payload,
            get_token_info as _info,
        )
        result = _info(token)
        if not result:
            return {"error": "Invalid or empty JWT"}
        # Include non-timing claims for backward compat
        payload = _decode_jwt_payload(token)
        result["claims"] = {
            k: v for k, v in payload.items() if k not in ("iat", "exp", "nbf")
        }
        return result

    @staticmethod
    def ensure_valid_token(force_refresh: bool = False) -> Dict:
        """Refresh GPT4IFX JWT using IFX credentials from env vars."""
        from src.HybridRAG.code.token_manager import (
            ensure_valid_token as _ensure,
            get_token_info as _info,
        )
        try:
            token = _ensure(force_refresh=force_refresh)
            info = _info(token)
            return {
                "token": f"{token[:8]}...{token[-4:]}",
                "refreshed": force_refresh,
                "source": "token_manager",
                **info,
            }
        except RuntimeError as e:
            return {"error": str(e)}

"""
Search Service — Sprint 2 → Sprint 8
======================================
Backend for Category 1 (Search & Query) tools.
Delegates to Neo4j for graph queries and Qdrant for vector search.

This is the Hybrid Query Engine from the PPTX architecture,
implementing the search_database, search_nodes, get_node_by_id,
get_neighbors, shortest_path, and execute_cypher operations.

Sprint 8: Full Qdrant vector search + Reciprocal Rank Fusion (RRF) merge.
         Detailed Neo4j search with label-specific property maps,
         entity-targeted lookup, aggregation queries, and 1-hop expansion.
         Token-budget ContextBuilder integration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

from .context_builder import (
    AssembledContext, ContextBuilder, ContextBudget, ContextItem, ContextSlot,
)
from .kg_node_utils import (
    LABEL_DISPLAY_PROPS, LABEL_ID_PROPS, LABEL_NAME_PROPS, COMPACT_PROPS,
    Source, classify_source, extract_keywords, extract_named_entities,
    format_source_for_context, infer_labels, is_aggregation_query,
    node_display_name, node_unique_id, normalise_scores, score_node,
    serialize_node,
)

logger = logging.getLogger(__name__)


class SearchService:
    """
    Hybrid search across Neo4j (graph) and Qdrant (vector).

    Parameters
    ----------
    neo4j_driver : neo4j.Driver
        Connected Neo4j driver instance.
    qdrant_client : QdrantClient, optional
        Connected Qdrant client. If None, vector search is disabled.
    default_database : str
        Default Neo4j database (workspace mapping).
    """

    def __init__(self, neo4j_driver=None, qdrant_client=None, default_database: str = "neo4j",
                 qdrant_collection: str = "mcal_embeddings", embed_fn=None,
                 module: Optional[str] = None, context_budget: int = 16000):
        self._neo4j = neo4j_driver
        self._qdrant = qdrant_client
        self._default_db = default_database
        self._qdrant_collection = qdrant_collection
        # embed_fn: callable(text) → List[float]. If None, uses sentence-transformers.
        self._embed_fn = embed_fn
        self._st_model = None  # lazy-init sentence-transformers
        self.module: Optional[str] = module.upper() if module else None
        self._context_budget = context_budget
        self._context_builder: Optional[ContextBuilder] = None
        # TTL-cached collection list (avoids Qdrant get_collections() on every search)
        self._collection_cache: Dict[str, List[str]] = {}  # workspace → names
        self._collection_cache_ts: Dict[str, float] = {}   # workspace → timestamp
        self._collection_cache_ttl: float = float(os.environ.get("COLLECTION_CACHE_TTL", "300"))  # 5 min
        self._collection_cache_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._neo4j is not None

    def _get_context_builder(self) -> ContextBuilder:
        """Lazy-init the ContextBuilder."""
        if self._context_builder is None:
            budget = ContextBudget(total_budget=self._context_budget)
            self._context_builder = ContextBuilder(budget=budget)
        return self._context_builder

    def _db_for_workspace(self, workspace_id: str) -> str:
        """Map workspace_id to Neo4j database name.

        When the MCP server creates a SearchService per profile, it passes
        the correct ``default_database`` from storage_config.yaml. So we
        always use that default — the workspace_id is only informational.
        """
        return self._default_db

    # ── Universal ID matching ───────────────────────────────────────────
    # All known ID properties across ILLD (document_id, id, jama_id) and
    # MCAL (global_id, feature_id, decision_id, service_id).

    _STR_ID_PROPS = ("document_id", "id", "global_id", "feature_id", "decision_id", "service_id")

    @staticmethod
    def _id_where(alias: str, param: str) -> str:
        """Build a WHERE clause that matches any known string-ID property."""
        parts = " OR ".join(
            f"{alias}.{p} = ${param}" for p in SearchService._STR_ID_PROPS
        )
        return parts

    @staticmethod
    def _coalesce_id(alias: str) -> str:
        """Coalesce expression returning the first non-null ID."""
        return f"coalesce({', '.join(f'{alias}.{p}' for p in SearchService._STR_ID_PROPS)})"

    @staticmethod
    def _coalesce_name(alias: str) -> str:
        """Coalesce expression returning the best display name."""
        return (
            f"coalesce({alias}.name, {alias}.function_name, {alias}.type_name, "
            f"{alias}.api_name, {alias}.peripheral_name, {alias}.decision_id, "
            f"{alias}.requirement_id, 'unknown')"
        )

    # ─────────────────────────────────────────────────────────────────────
    # search_database — hybrid semantic + graph search
    # ─────────────────────────────────────────────────────────────────────

    def hybrid_search(
        self,
        query: str,
        max_results: int = 10,
        include_relationships: bool = False,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        offset: int = 0,
        workspace_id: str = "illd",
        alpha: float = 0.6,
    ) -> Dict[str, Any]:
        """
        Combined graph + vector search.

        alpha controls blend:
          0.0 = pure vector (Qdrant)
          0.6 = default hybrid
          1.0 = pure graph (Neo4j)
        """
        graph_results = []
        vector_results = []

        # ── Stage 0: Query analysis ─────────────────────────────────
        is_agg = is_aggregation_query(query)
        named_ents = extract_named_entities(query, module=filter_by_module or self.module)
        if is_agg:
            max_results = max(max_results, 100)

        # ── Stage 1 & 2: Graph + Vector search (parallel) ─────────────
        do_graph = alpha > 0.0 and self._neo4j
        do_vector = alpha < 1.0 and self._qdrant

        if do_graph and do_vector:
            with ThreadPoolExecutor(max_workers=2) as pool:
                graph_future = pool.submit(
                    self._graph_search,
                    query, max_results=max_results * 2,
                    filter_by_module=filter_by_module,
                    filter_by_node_type=filter_by_node_type,
                    workspace_id=workspace_id,
                    include_relationships=include_relationships,
                )
                vector_future = pool.submit(
                    self._vector_search,
                    query, max_results=max_results * 2,
                    filter_by_module=filter_by_module,
                    filter_by_node_type=filter_by_node_type,
                    workspace_id=workspace_id,
                )
                graph_results = graph_future.result()
                vector_results = vector_future.result()
        elif do_graph:
            graph_results = self._graph_search(
                query, max_results=max_results * 2,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
                include_relationships=include_relationships,
            )
        elif do_vector:
            vector_results = self._vector_search(
                query, max_results=max_results * 2,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
            )

        logger.debug("hybrid_search: graph=%d vector=%d (alpha=%.2f, do_graph=%s, do_vector=%s)",
                      len(graph_results), len(vector_results), alpha, do_graph, do_vector)
        if do_graph and do_vector and not vector_results:
            logger.warning("hybrid_search: vector search returned 0 results — "
                           "check Qdrant connectivity and collection availability")

        # ── Stage 1b: Entity-targeted + aggregation (post-graph) ──────
        if do_graph:
            # Entity-targeted lookup — guarantees named entities appear
            if named_ents:
                entity_results = self._entity_targeted_lookup(
                    named_ents, workspace_id=workspace_id,
                    module=filter_by_module or self.module,
                )
                existing_ids = {r.get("node_id") for r in graph_results}
                for er in entity_results:
                    if er.get("node_id") not in existing_ids:
                        graph_results.append(er)
                        existing_ids.add(er.get("node_id"))

            # Aggregation Cypher queries for enumeration questions
            if is_agg:
                agg_results = self._aggregation_search(
                    query, named_ents, workspace_id=workspace_id,
                    module=filter_by_module or self.module,
                )
                existing_ids = {r.get("node_id") for r in graph_results}
                for ar in agg_results:
                    if ar.get("node_id") not in existing_ids:
                        graph_results.append(ar)
                        existing_ids.add(ar.get("node_id"))

        # ── Stage 3: RRF merge ────────────────────────────────────────
        merged = self._merge_results_rrf(
            graph_results, vector_results, alpha,
            workspace_id=workspace_id,
        )

        # ── Stage 4: MCAL post-fusion guarantees ──────────────────────
        if workspace_id == "mcal":
            # Must-include guarantee — entity-targeted results survive
            must_include = [r for r in merged if r.get("_must_include")]
            optional = [r for r in merged if not r.get("_must_include")]
            if must_include:
                remaining = max(max_results, len(must_include))
                selected = list(must_include)
                for opt in optional:
                    if len(selected) >= remaining:
                        break
                    selected.append(opt)
                merged = selected

            # Graph representation guarantee — min graph results survive
            min_graph = max(5, len(named_ents) * 2)
            graph_in = [r for r in merged if r.get("source") == "neo4j"]
            if len(graph_in) < min_graph:
                merged_ids = {r.get("node_id") for r in merged}
                extra = [
                    r for r in graph_results
                    if r.get("node_id") not in merged_ids
                ]
                extra.sort(key=lambda r: r.get("score", 0), reverse=True)
                needed = min_graph - len(graph_in)
                for gr in extra[:needed]:
                    # Replace lowest-scoring non-graph, non-must-include
                    for idx in range(len(merged) - 1, -1, -1):
                        if (merged[idx].get("source") != "neo4j"
                                and not merged[idx].get("_must_include")):
                            merged[idx] = gr
                            break
                merged.sort(key=lambda r: r.get("score", 0), reverse=True)

        # Paginate
        total = len(merged)
        page = merged[offset:offset + max_results]

        return {
            "results": page,
            "total_count": total,
            "has_more": (offset + max_results) < total,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "alpha": alpha,
            "named_entities": named_ents,
            "is_aggregation": is_agg,
        }

    async def hybrid_search_async(
        self,
        query: str,
        max_results: int = 10,
        include_relationships: bool = False,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        offset: int = 0,
        workspace_id: str = "illd",
        alpha: float = 0.6,
    ) -> Dict[str, Any]:
        """Async hybrid search — runs graph and vector stages concurrently."""
        graph_results: List[Dict[str, Any]] = []
        vector_results: List[Dict[str, Any]] = []

        is_agg = is_aggregation_query(query)
        named_ents = extract_named_entities(query, module=filter_by_module or self.module)
        if is_agg:
            max_results = max(max_results, 100)

        # ── Run graph + vector searches concurrently via threads ──────
        async def _graph_stage():
            if alpha <= 0.0 or not self._neo4j:
                return []
            return await asyncio.to_thread(
                self._graph_search,
                query, max_results=max_results * 2,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
                include_relationships=include_relationships,
            )

        async def _vector_stage():
            if alpha >= 1.0 or not self._qdrant:
                return []
            return await asyncio.to_thread(
                self._vector_search,
                query, max_results=max_results * 2,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
            )

        graph_results, vector_results = await asyncio.gather(
            _graph_stage(), _vector_stage(),
        )

        # Entity-targeted lookup + aggregation (graph-only, sequential)
        if graph_results and self._neo4j:
            if named_ents:
                entity_results = await asyncio.to_thread(
                    self._entity_targeted_lookup,
                    named_ents, workspace_id=workspace_id,
                    module=filter_by_module or self.module,
                )
                existing_ids = {r.get("node_id") for r in graph_results}
                for er in entity_results:
                    if er.get("node_id") not in existing_ids:
                        graph_results.append(er)
                        existing_ids.add(er.get("node_id"))

            if is_agg:
                agg_results = await asyncio.to_thread(
                    self._aggregation_search,
                    query, named_ents, workspace_id=workspace_id,
                    module=filter_by_module or self.module,
                )
                existing_ids = {r.get("node_id") for r in graph_results}
                for ar in agg_results:
                    if ar.get("node_id") not in existing_ids:
                        graph_results.append(ar)
                        existing_ids.add(ar.get("node_id"))

        merged = self._merge_results_rrf(
            graph_results, vector_results, alpha,
            workspace_id=workspace_id,
        )

        # ── MCAL post-fusion guarantees ───────────────────────────────
        if workspace_id == "mcal":
            # Must-include guarantee — entity-targeted results survive
            must_include = [r for r in merged if r.get("_must_include")]
            optional = [r for r in merged if not r.get("_must_include")]
            if must_include:
                remaining = max(max_results, len(must_include))
                selected = list(must_include)
                for opt in optional:
                    if len(selected) >= remaining:
                        break
                    selected.append(opt)
                merged = selected

            # Graph representation guarantee — min graph results survive
            min_graph = max(5, len(named_ents) * 2)
            graph_in = [r for r in merged if r.get("source") == "neo4j"]
            if len(graph_in) < min_graph:
                merged_ids = {r.get("node_id") for r in merged}
                extra = [
                    r for r in graph_results
                    if r.get("node_id") not in merged_ids
                ]
                extra.sort(key=lambda r: r.get("score", 0), reverse=True)
                needed = min_graph - len(graph_in)
                for gr in extra[:needed]:
                    for idx in range(len(merged) - 1, -1, -1):
                        if (merged[idx].get("source") != "neo4j"
                                and not merged[idx].get("_must_include")):
                            merged[idx] = gr
                            break
                merged.sort(key=lambda r: r.get("score", 0), reverse=True)

        total = len(merged)
        page = merged[offset:offset + max_results]

        return {
            "results": page,
            "total_count": total,
            "has_more": (offset + max_results) < total,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "alpha": alpha,
            "named_entities": named_ents,
            "is_aggregation": is_agg,
        }

    def _graph_search(
        self, query: str, max_results: int = 20,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        workspace_id: str = "illd",
        include_relationships: bool = False,
    ) -> List[Dict[str, Any]]:
        """Label-aware graph search with keyword extraction and rich serialization.

        Uses label-specific property maps from kg_node_utils to properly
        identify display names, unique IDs, and structured properties.
        """
        db = self._db_for_workspace(workspace_id)
        keywords = extract_keywords(query)
        labels = filter_by_node_type or infer_labels(query, profile=workspace_id)
        module = (filter_by_module or self.module or "").upper() or None

        results: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        graph_errors = 0

        try:
            with self._neo4j.session(database=db) as session:
                # ── Single consolidated query using UNWIND ──────────
                # Instead of N×M individual queries (labels × keywords),
                # run one query per label with all keywords at once.
                for label in labels:
                    try:
                        module_clause = (
                            "WHERE (n.module IS NULL OR n.module = $module) AND ("
                            if module else "WHERE ("
                        )
                        cypher = (
                            "UNWIND $keywords AS kw "
                            f"MATCH (n:{label}) "
                            f"{module_clause}"
                            "  toLower(coalesce(n.name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.function_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.description, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.param_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.requirement_id, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.title, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.api_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.type_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.macro_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.module_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.file_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.register_name, '')) CONTAINS kw OR "
                            "  toLower(coalesce(n.struct_name, '')) CONTAINS kw"
                            ") "
                            "RETURN DISTINCT n, labels(n) AS node_labels, kw "
                            "LIMIT $limit"
                        )
                        params = {
                            "keywords": [k.lower() for k in keywords],
                            "limit": max_results,
                        }
                        if module:
                            params["module"] = module
                        records = session.run(cypher, params)
                        for record in records:
                            node = record["n"]
                            props = dict(node.items())
                            node_type = record["node_labels"][0] if record["node_labels"] else label
                            kw = record["kw"]

                            name = node_display_name(node_type, props)
                            nid = node_unique_id(node_type, props)
                            if nid in seen_ids:
                                continue
                            seen_ids.add(nid)

                            desc = str(props.get("description", ""))
                            sc = score_node(kw, name, desc)

                            result = {
                                "node_id": nid,
                                "node_type": node_type,
                                "source": "neo4j",
                                "score": sc,
                                "properties": props,
                                "content": serialize_node(node_type, props),
                            }

                            if include_relationships:
                                result["relationships"] = self._get_node_relationships(
                                    session, node.element_id, limit=5
                                )

                            results.append(result)
                    except Exception as exc:
                        graph_errors += 1
                        logger.warning("Graph search failed for %s: %s", label, exc)
                        if graph_errors >= 3:
                            break

                # ── 1-hop neighbor expansion for top results ──────────
                if results:
                    results = self._expand_graph_neighbors(
                        session, results, seen_ids, max_expand=5, db=db,
                    )
        except Exception as e:
            logger.error("Graph search failed: %s", e)

        return results

    def _node_to_text(self, props: Dict[str, Any], node_type: str) -> str:
        """Convert node properties to rich text using label-specific maps."""
        return serialize_node(node_type, props)

    def _get_node_relationships(self, session, element_id, limit=5) -> List[Dict]:
        """Fetch relationships for a node."""
        cypher = """
        MATCH (n)-[r]-(m)
        WHERE elementId(n) = $eid
        RETURN type(r) AS rel_type, labels(m)[0] AS target_type,
               m.name AS target_name, m.function_name AS target_fn
        LIMIT $limit
        """
        rels = []
        try:
            for rec in session.run(cypher, {"eid": element_id, "limit": limit}):
                rels.append({
                    "type": rec["rel_type"],
                    "target_type": rec["target_type"],
                    "target": rec["target_name"] or rec["target_fn"] or "?",
                })
        except Exception:
            pass
        return rels

    def _merge_results(self, graph: List, vector: List, alpha: float) -> List[Dict]:
        """Legacy merge — kept for backward compatibility. Use _merge_results_rrf instead."""
        seen = set()
        merged = []
        for r in graph:
            r["score"] = r.get("score", 1.0) * alpha
            key = r.get("node_id", id(r))
            if key not in seen:
                merged.append(r)
                seen.add(key)
        for r in vector:
            r["score"] = r.get("score", 0.5) * (1 - alpha)
            key = r.get("node_id", id(r))
            if key not in seen:
                merged.append(r)
                seen.add(key)
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged

    # ─────────────────────────────────────────────────────────────────────
    # Entity-targeted lookup — exact match by named entity
    # ─────────────────────────────────────────────────────────────────────

    def _entity_targeted_lookup(
        self, entities: List[str], workspace_id: str = "illd",
        module: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Run exact-match Cypher for each named entity.

        Guarantees named entities appear in results regardless of
        similarity score. Also fetches 1-hop neighbors with full properties.
        """
        if not self._neo4j:
            return []

        db = self._db_for_workspace(workspace_id)
        results: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        try:
            with self._neo4j.session(database=db) as session:
                for entity in entities:
                    module_clause = "WHERE (n.module IS NULL OR n.module = $module) AND (" if module else "WHERE ("
                    cypher = (
                        "MATCH (n) "
                        f"{module_clause}"
                        "  toLower(coalesce(n.function_name, '')) = toLower($name) "
                        "  OR toLower(coalesce(n.param_name, ''))    = toLower($name) "
                        "  OR toLower(coalesce(n.name, ''))           = toLower($name) "
                        "  OR toLower(coalesce(n.title, ''))          = toLower($name) "
                        "  OR toLower(coalesce(n.api_name, ''))       = toLower($name) "
                        "  OR toLower(coalesce(n.test_case_id, ''))   = toLower($name) "
                        "  OR toLower(coalesce(n.requirement_id, '')) = toLower($name) "
                        "  OR toLower(coalesce(n.decision_id, ''))    = toLower($name) "
                        "  OR toLower(coalesce(n.file_name, ''))      = toLower($name) "
                        "  OR toLower(coalesce(n.type_name, ''))      = toLower($name) "
                        "  OR toLower(coalesce(n.macro_name, ''))     = toLower($name) "
                        "  OR toLower(coalesce(n.module_name, ''))    = toLower($name) "
                        ") "
                        "RETURN n, [lbl IN labels(n) WHERE lbl <> 'Node' | lbl] AS labels "
                        "LIMIT 5"
                    )
                    try:
                        params = {"name": entity}
                        if module:
                            params["module"] = module.upper()
                        records = session.run(cypher, params)
                        for rec in records:
                            if rec.get("n") is None:
                                continue
                            props = dict(rec["n"])
                            labels = rec.get("labels", [])
                            label = labels[0] if labels else "Unknown"
                            name = node_display_name(label, props)
                            nid = node_unique_id(label, props)
                            if nid in seen_ids:
                                continue
                            seen_ids.add(nid)

                            # Rich text with neighbor expansion
                            text = serialize_node(label, props)
                            neighbor_text = self._fetch_rich_neighbors(
                                session, label, props, seen_ids, module,
                            )
                            if neighbor_text:
                                text += "\n\n" + neighbor_text

                            results.append({
                                "node_id": nid,
                                "node_type": label,
                                "source": "neo4j",
                                "score": 2.5,  # high score for must-include
                                "properties": props,
                                "content": text,
                                "_must_include": True,
                            })
                    except Exception as exc:
                        logger.debug("Entity lookup failed for %s: %s", entity, exc)
        except Exception as e:
            logger.error("Entity targeted lookup failed: %s", e)

        return results

    # ─────────────────────────────────────────────────────────────────────
    # Aggregation search — direct Cypher for enumeration queries
    # ─────────────────────────────────────────────────────────────────────

    def _aggregation_search(
        self, question: str, named_entities: List[str],
        workspace_id: str = "illd", module: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Run targeted Cypher for aggregation/enumeration queries."""
        if not self._neo4j:
            return []

        db = self._db_for_workspace(workspace_id)
        q = question.lower()
        results: List[Dict[str, Any]] = []

        try:
            with self._neo4j.session(database=db) as session:
                # ── ASIL-level aggregation ────────────────────────
                asil_match = re.search(r'asil\s*([a-d])', q)
                if asil_match and any(w in q for w in ["function", "api", "rated", "level"]):
                    asil_level = asil_match.group(1).upper()
                    cypher = (
                        "MATCH (n:SWUD_Function) "
                        "WHERE toLower(n.asil_level) CONTAINS toLower($asil) "
                        "RETURN n ORDER BY n.function_name"
                    )
                    try:
                        for rec in session.run(cypher, {"asil": asil_level}):
                            if rec.get("n") is None:
                                continue
                            props = dict(rec["n"])
                            name = props.get("function_name", "?")
                            results.append({
                                "node_id": f"SWUD_Function::{name}",
                                "node_type": "SWUD_Function",
                                "source": "neo4j",
                                "score": 1.8,
                                "properties": props,
                                "content": serialize_node("SWUD_Function", props),
                                "_must_include": True,
                            })
                    except Exception as exc:
                        logger.warning("ASIL aggregation failed: %s", exc)

                # ── Test case aggregation ─────────────────────────
                func_names: List[str] = []
                if any(w in q for w in ["test", "validate", "verify"]):
                    for ent in named_entities:
                        if "_" in ent or (ent[0].isupper() if ent else False):
                            func_names.append(ent)

                for func_name in func_names:
                    cypher = (
                        "MATCH (t:TS_FunctionalTestCase)-[r]-(f:SWUD_Function) "
                        "WHERE toLower(f.function_name) CONTAINS toLower($fname) "
                        "AND (f.module IS NULL OR f.module = $mod) "
                        "RETURN t LIMIT 50"
                    )
                    try:
                        for rec in session.run(cypher, {"fname": func_name, "mod": module.upper()}):
                            if rec.get("t") is None:
                                continue
                            props = dict(rec["t"])
                            tc_id = props.get("test_case_id", props.get("name", "?"))
                            results.append({
                                "node_id": f"TS_FunctionalTestCase::{tc_id}",
                                "node_type": "TS_FunctionalTestCase",
                                "source": "neo4j",
                                "score": 1.8,
                                "properties": props,
                                "content": serialize_node("TS_FunctionalTestCase", props),
                                "_must_include": True,
                            })
                    except Exception as exc:
                        logger.warning("Test case aggregation failed: %s", exc)

                # ── SFR register / bitfield aggregation ───────────
                if any(w in q for w in ["register", "sfr", "bitfield", "bit field",
                                        "base address", "regdef", "special function",
                                        "peripheral register", "memory mapped"]):
                    device_match = re.search(r'(tc\d\w{2,3})', q, re.IGNORECASE)
                    device_filter = device_match.group(1).upper() if device_match else None
                    mod_upper = module.upper() if module else None

                    # Detect register names from entities or regex
                    reg_names: List[str] = []
                    for ent in named_entities:
                        if "_" in ent and ent == ent.upper():
                            reg_names.append(ent)
                    regex_regs = re.findall(r'[A-Z]{2,}(?:_[A-Z0-9]+)+', question)
                    for rr in regex_regs:
                        if rr not in reg_names:
                            reg_names.append(rr)

                    if reg_names:
                        for reg_name in reg_names[:5]:
                            cypher_reg = (
                                "MATCH (r:SFR_Register) "
                                "WHERE toLower(r.name) CONTAINS toLower($rname) "
                                "AND (r.module = $mod OR r.module IS NULL) "
                                + ("AND r.device = $dev " if device_filter else "")
                                + "OPTIONAL MATCH (r)-[:SFR_HAS_BITFIELD]->(bf:SFR_BitField) "
                                "RETURN r, collect(bf) AS bitfields "
                                "LIMIT 20"
                            )
                            params_r: dict = {"rname": reg_name, "mod": mod_upper}
                            if device_filter:
                                params_r["dev"] = device_filter
                            try:
                                for rec in session.run(cypher_reg, params_r):
                                    if rec.get("r") is None:
                                        continue
                                    rprops = dict(rec["r"])
                                    rname = rprops.get("name", "?")
                                    dev = rprops.get("device", "?")
                                    text = serialize_node("SFR_Register", rprops)
                                    bfs = rec.get("bitfields", [])
                                    if bfs:
                                        text += f"\n  Bitfields ({len(bfs)}):"
                                        for bf_node in bfs:
                                            bfp = dict(bf_node)
                                            text += (
                                                f"\n    {bfp.get('name','?')} "
                                                f"[{bfp.get('bits','')}] "
                                                f"w={bfp.get('width','')} "
                                                f"access={bfp.get('access','')} "
                                                f"mask={bfp.get('mask','')}"
                                            )
                                    results.append({
                                        "node_id": f"SFR_Register::{rname}::{dev}",
                                        "node_type": "SFR_Register",
                                        "source": "neo4j",
                                        "score": 1.9,
                                        "properties": rprops,
                                        "content": text,
                                        "_must_include": True,
                                    })
                            except Exception as exc:
                                logger.warning("SFR register aggregation failed: %s", exc)

                    elif any(w in q for w in ["bitfield", "bit field"]):
                        cypher_bf = (
                            "MATCH (bf:SFR_BitField) "
                            "WHERE (bf.module = $mod OR bf.module IS NULL) "
                            + ("AND bf.device = $dev " if device_filter else "")
                            + "RETURN bf ORDER BY bf.register_name, bf.lsb "
                            "LIMIT 100"
                        )
                        params_bf: dict = {"mod": mod_upper}
                        if device_filter:
                            params_bf["dev"] = device_filter
                        try:
                            for rec in session.run(cypher_bf, params_bf):
                                if rec.get("bf") is None:
                                    continue
                                props = dict(rec["bf"])
                                bname = props.get("name", "?")
                                dev = props.get("device", "?")
                                results.append({
                                    "node_id": f"SFR_BitField::{bname}::{dev}",
                                    "node_type": "SFR_BitField",
                                    "source": "neo4j",
                                    "score": 1.7,
                                    "properties": props,
                                    "content": serialize_node("SFR_BitField", props),
                                    "_must_include": True,
                                })
                        except Exception as exc:
                            logger.warning("SFR bitfield aggregation failed: %s", exc)

                    else:
                        cypher_all = (
                            "MATCH (r:SFR_Register) "
                            "WHERE (r.module = $mod OR r.module IS NULL) "
                            + ("AND r.device = $dev " if device_filter else "")
                            + "RETURN r ORDER BY r.name "
                            "LIMIT 100"
                        )
                        params_all: dict = {"mod": mod_upper}
                        if device_filter:
                            params_all["dev"] = device_filter
                        try:
                            for rec in session.run(cypher_all, params_all):
                                if rec.get("r") is None:
                                    continue
                                props = dict(rec["r"])
                                rname = props.get("name", "?")
                                dev = props.get("device", "?")
                                results.append({
                                    "node_id": f"SFR_Register::{rname}::{dev}",
                                    "node_type": "SFR_Register",
                                    "source": "neo4j",
                                    "score": 1.7,
                                    "properties": props,
                                    "content": serialize_node("SFR_Register", props),
                                    "_must_include": True,
                                })
                        except Exception as exc:
                            logger.warning("SFR register aggregation failed: %s", exc)

                    # Cross-link: source functions → SFR registers
                    if any(w in q for w in ["access", "function", "source", "code",
                                            "which", "who"]):
                        cypher_xlink = (
                            "MATCH (f:SRC_Function)-[:SRC_ACCESSES_SFR]->(r:SFR_Register) "
                            "WHERE (r.module = $mod OR r.module IS NULL) "
                            + ("AND r.device = $dev " if device_filter else "")
                            + "RETURN f.name AS func, collect(DISTINCT r.name) AS registers, "
                            "r.device AS device "
                            "ORDER BY func LIMIT 50"
                        )
                        params_x: dict = {"mod": mod_upper}
                        if device_filter:
                            params_x["dev"] = device_filter
                        try:
                            for rec in session.run(cypher_xlink, params_x):
                                func = rec.get("func", "?")
                                regs = rec.get("registers", [])
                                dev = rec.get("device", "?")
                                text = (
                                    f"[SRC→SFR Cross-link] Function {func} accesses "
                                    f"{len(regs)} register(s) on {dev}: {', '.join(regs[:10])}"
                                )
                                results.append({
                                    "node_id": f"SRC_SFR_XLINK::{func}::{dev}",
                                    "node_type": "SFR_Register",
                                    "source": "neo4j",
                                    "score": 1.6,
                                    "properties": {"func": func, "registers": regs, "device": dev},
                                    "content": text,
                                    "_must_include": True,
                                })
                        except Exception as exc:
                            logger.warning("SFR cross-link aggregation failed: %s", exc)
        except Exception as e:
            logger.error("Aggregation search failed: %s", e)

        return results

    # ─────────────────────────────────────────────────────────────────────
    # 1-hop neighbor expansion
    # ─────────────────────────────────────────────────────────────────────

    def _expand_graph_neighbors(
        self, session, results: List[Dict[str, Any]],
        seen_ids: Set[str], max_expand: int = 5, db: str = "neo4j",
    ) -> List[Dict[str, Any]]:
        """Fetch 1-hop neighbours for the top-scoring graph nodes."""
        if not results:
            return results

        top = sorted(results, key=lambda r: r.get("score", 0), reverse=True)[:max_expand]

        for result in top:
            node_type = result.get("node_type", "")
            props = result.get("properties", {})
            name_prop = LABEL_NAME_PROPS.get(node_type, "name")
            name_val = props.get(name_prop, "")
            if not name_val:
                continue

            try:
                neighbor_module_clause = "WHERE m.module IS NULL OR m.module = $module " if self.module else ""
                cypher = (
                    f"MATCH (n:{node_type} {{{name_prop}: $name_val}})"
                    f"-[r]-(m) "
                    f"{neighbor_module_clause}"
                    f"RETURN type(r) AS rel_type, "
                    f"[lbl IN labels(m) WHERE lbl <> 'Node' | lbl] AS target_labels, "
                    f"m AS neighbor "
                    f"LIMIT 15"
                )
                params = {"name_val": name_val}
                if self.module:
                    params["module"] = self.module
                records = session.run(cypher, params)

                neighbor_texts: List[str] = []
                for rec in records:
                    if rec.get("neighbor") is None:
                        continue
                    target_labels = rec.get("target_labels", [])
                    target_label = target_labels[0] if target_labels else "Unknown"
                    rel_type = rec.get("rel_type", "")
                    neighbor_props = dict(rec["neighbor"])

                    n_name = node_display_name(target_label, neighbor_props)
                    n_id = node_unique_id(target_label, neighbor_props)
                    if n_id in seen_ids:
                        continue
                    seen_ids.add(n_id)

                    n_desc = str(neighbor_props.get("description", ""))[:200]
                    line = f"  [{rel_type}] → {target_label}: {n_name}"
                    if n_desc:
                        line += f" — {n_desc}"
                    for key in ("test_case_id", "test_objective", "expected_results",
                                "requirement_id", "asil_level", "memory_section",
                                "param_type", "default_value", "range_values"):
                        val = neighbor_props.get(key, "")
                        if val:
                            line += f"\n    {key}: {val}"
                    neighbor_texts.append(line)

                if neighbor_texts:
                    result["content"] += "\n\nRelated KG nodes:\n" + "\n".join(neighbor_texts)
            except Exception as exc:
                logger.debug("Neighbour expansion failed for %s: %s", name_val, exc)

        return results

    def _fetch_rich_neighbors(
        self, session, label: str, props: Dict[str, Any],
        seen_ids: Set[str], module: str,
    ) -> str:
        """Fetch 1-hop neighbors with full structured properties."""
        name_prop = LABEL_NAME_PROPS.get(label, "name")
        name_val = props.get(name_prop, "")
        if not name_val:
            return ""

        try:
            cypher = (
                f"MATCH (n:{label} {{{name_prop}: $name_val}})"
                f"-[r]-(m) "
                f"WHERE m.module IS NULL OR m.module = $module "
                f"RETURN type(r) AS rel_type, "
                f"[lbl IN labels(m) WHERE lbl <> 'Node' | lbl] AS target_labels, "
                f"m AS neighbor "
                f"LIMIT 20"
            )
            records = session.run(cypher, {
                "name_val": name_val, "module": module.upper(),
            })
        except Exception as exc:
            logger.debug("Rich neighbor fetch failed for %s: %s", name_val, exc)
            return ""

        sections: Dict[str, List[str]] = {}
        for rec in records:
            if rec.get("neighbor") is None:
                continue
            target_labels = rec.get("target_labels", [])
            target_label = target_labels[0] if target_labels else "Unknown"
            rel_type = rec.get("rel_type", "")
            neighbor_props = dict(rec["neighbor"])

            n_name = node_display_name(target_label, neighbor_props)
            n_id = node_unique_id(target_label, neighbor_props)
            if n_id in seen_ids:
                continue
            seen_ids.add(n_id)

            display_props = LABEL_DISPLAY_PROPS.get(target_label, [])
            if display_props:
                lines = [f"  [{rel_type}] → {target_label}: {n_name}"]
                for prop_key, display_label in display_props:
                    val = neighbor_props.get(prop_key, "")
                    if val:
                        val_str = str(val)
                        if len(val_str) > 400:
                            val_str = val_str[:400] + "…"
                        lines.append(f"    {display_label}: {val_str}")
                sections.setdefault(rel_type, []).append("\n".join(lines))
            else:
                line = f"  [{rel_type}] → {target_label}: {n_name}"
                desc = str(neighbor_props.get("description", ""))[:300]
                if desc:
                    line += f"\n    Description: {desc}"
                for key in ("test_case_id", "test_objective", "expected_results",
                            "preconditions", "requirement_id", "asil_level"):
                    val = neighbor_props.get(key, "")
                    if val:
                        line += f"\n    {key}: {val}"
                sections.setdefault(rel_type, []).append(line)

        if not sections:
            return ""

        parts = ["Connected nodes:"]
        for rel_type, items in sections.items():
            parts.append(f"\n  --- {rel_type} ---")
            parts.extend(items)
        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────────────
    # V-model traceability
    # ─────────────────────────────────────────────────────────────────────

    def fetch_traceability(
        self, sources: List[Dict[str, Any]], workspace_id: str = "illd",
    ) -> List[Dict[str, Any]]:
        """Fetch V-model traceability chains for graph sources."""
        if not self._neo4j:
            return []

        db = self._db_for_workspace(workspace_id)
        traces: List[Dict[str, Any]] = []

        try:
            with self._neo4j.session(database=db) as session:
                for src in sources:
                    if src.get("source") != "neo4j":
                        continue
                    jama_id = src.get("properties", {}).get("jama_id", "")
                    if not jama_id:
                        continue
                    cypher = (
                        "MATCH (n {jama_id: $jid})-[r*1..3]-(m) "
                        "RETURN [node IN nodes(path) | labels(node)[0]] AS chain "
                        "LIMIT 5"
                    )
                    # Simplified traceability — real impl would use trace_requirement
                    traces.append({
                        "source_heading": src.get("node_id", "?"),
                        "source_id": jama_id,
                    })
        except Exception as e:
            logger.error("fetch_traceability failed: %s", e)

        return traces

    # ─────────────────────────────────────────────────────────────────────
    # Context assembly via ContextBuilder
    # ─────────────────────────────────────────────────────────────────────

    def build_context(
        self, query: str, results: List[Dict[str, Any]],
        traceability: Optional[List[Dict]] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Assemble search results into token-budget context using ContextBuilder.

        Returns (context_text, stats_dict).
        """
        builder = self._get_context_builder()
        candidates: List[ContextItem] = []

        for r in results:
            node_type = r.get("node_type", "")
            # Create a Source-like object for classification
            src = Source(
                origin=r.get("source", "neo4j"),
                score=r.get("score", 0.0),
                heading=r.get("node_id", ""),
                text=r.get("content", ""),
                node_label=node_type,
                metadata=r.get("properties", {}),
            )
            slot = classify_source(src)
            candidates.append(ContextItem(
                slot=slot,
                content=r.get("content", ""),
                relevance_score=r.get("score", 0.0),
                source=f"{r.get('source', 'unknown')}:{node_type}",
                entity_id=r.get("node_id", ""),
            ))

        if traceability:
            for trace in traceability:
                candidates.append(ContextItem(
                    slot=ContextSlot.RELATIONSHIPS,
                    content=f"Trace: {trace.get('source_heading', '?')} (ID: {trace.get('source_id', '?')})",
                    relevance_score=0.7,
                    source="graph:traceability",
                ))

        assembled = builder.build(candidates, max_tokens=max_tokens or self._context_budget)
        context_text = ContextBuilder.render(assembled)

        stats = {
            "items_included": assembled.items_included,
            "items_dropped": assembled.items_dropped,
            "total_tokens": assembled.total_tokens,
            "budget_used": dict(assembled.budget_used),
        }
        return context_text, stats

    # ─────────────────────────────────────────────────────────────────────
    # Embedding helper
    # ─────────────────────────────────────────────────────────────────────

    def _embed_query(self, text: str) -> List[float]:
        """Encode text to a dense vector using embed_fn or sentence-transformers."""
        if self._embed_fn:
            return self._embed_fn(text)
        # Lazy-init via shared singleton (avoids loading model twice in the process)
        if self._st_model is None:
            try:
                from src.Configuration.embedding_singleton import get_shared_model
                self._st_model = get_shared_model()
            except Exception:
                try:
                    from sentence_transformers import SentenceTransformer
                    model_name = os.environ.get("ST_MODEL", "all-MiniLM-L6-v2")
                    self._st_model = SentenceTransformer(model_name)
                    logger.info("SearchService: loaded embedding model '%s'", model_name)
                except ImportError:
                    logger.error("sentence-transformers not installed — vector search unavailable")
                    return []
        if self._st_model is None:
            return []
        return self._st_model.encode(text, normalize_embeddings=True).tolist()

    # ─────────────────────────────────────────────────────────────────────
    # Vector search (Qdrant)
    # ─────────────────────────────────────────────────────────────────────

    def _vector_search(
        self, query: str, max_results: int = 20,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        workspace_id: str = "illd",
    ) -> List[Dict[str, Any]]:
        """Semantic search against Qdrant collection(s).

        ILLD collections use the module name directly (e.g. ``cxpi``).
        MCAL collections are per-module with pattern
        ``{module}_{source}_{category}`` (e.g. ``adc_swa_architecture``).
        When a module is given for MCAL, we fan out across all matching
        sub-collections.
        """
        embedding = self._embed_query(query)
        if not embedding:
            return []

        # Build Qdrant payload filter
        must_conditions = []
        if filter_by_node_type:
            must_conditions.append({
                "key": "type",
                "match": {"any": filter_by_node_type},
            })

        query_filter = None
        if must_conditions:
            from qdrant_client.models import Filter, FieldCondition, MatchAny
            qdrant_conditions = [
                FieldCondition(key=cond["key"], match=MatchAny(any=cond["match"]["any"]))
                for cond in must_conditions
            ]
            query_filter = Filter(must=qdrant_conditions)

        # Resolve target collection(s)
        collections = self._resolve_collections(filter_by_module, workspace_id)
        logger.info("_vector_search: querying %d collection(s) for workspace '%s'", len(collections), workspace_id)

        def _query_one(collection: str) -> List[Dict[str, Any]]:
            """Query a single Qdrant collection (thread-safe, used for parallel fan-out)."""
            hits_out: List[Dict[str, Any]] = []
            try:
                response = self._qdrant.query_points(
                    collection_name=collection,
                    query=embedding,
                    query_filter=query_filter,
                    limit=max_results,
                    with_payload=True,
                )
                hits = response.points if hasattr(response, 'points') else response
                for hit in hits:
                    payload = hit.payload or {}
                    node_type = payload.get("node_type") or payload.get("type")
                    if not node_type or node_type == "Unknown":
                        node_type = self._node_type_from_collection(collection)
                    hits_out.append({
                        "node_id": payload.get("_original_id", payload.get("document_id", payload.get("name", str(hit.id)))),
                        "node_type": node_type or "Unknown",
                        "source": "qdrant",
                        "collection": collection,
                        "score": float(hit.score),
                        "properties": payload,
                        "content": payload.get("document", payload.get("text", payload.get("content", ""))),
                    })
            except Exception as e:
                logger.warning("Qdrant search on '%s' failed: %s", collection, e)
            return hits_out

        # Parallel fan-out across collections (max 10 concurrent)
        results: List[Dict[str, Any]] = []
        if len(collections) == 1:
            results = _query_one(collections[0])
        else:
            max_workers = min(10, len(collections))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_query_one, c): c for c in collections}
                for fut in as_completed(futures):
                    results.extend(fut.result())

        # Sort by score descending and trim
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max_results]

    def _resolve_collections(
        self, filter_by_module: Optional[str], workspace_id: str,
    ) -> List[str]:
        """Return the list of Qdrant collection names to query.

        ILLD: single collection = module name (e.g. "cxpi").
        MCAL: fan out to {module}_{source}_{category} sub-collections.
        When no module is specified, discover all collections that exist
        in Qdrant for the given profile.
        """
        if filter_by_module:
            mod = filter_by_module.lower()
            if workspace_id == "mcal":
                # MCAL per-module collections: adc_swa_architecture, etc.
                try:
                    from src.HybridRAG.code.RAG.collection_naming_unified import module_collections
                    colls = module_collections(mod, profile="mcal")
                except Exception:
                    # Fallback: discover from Qdrant collections list
                    colls = self._discover_collections_for_module(mod)
                return colls if colls else [mod]
            else:
                # ILLD: collection = module name
                return [mod]

        # No module specified — discover all collections for this profile
        return self._discover_all_profile_collections(workspace_id)

    def _get_all_collection_names(self) -> List[str]:
        """Return all Qdrant collection names, cached with TTL."""
        cache_key = "__all__"
        with self._collection_cache_lock:
            ts = self._collection_cache_ts.get(cache_key, 0.0)
            if cache_key in self._collection_cache and (time.time() - ts) < self._collection_cache_ttl:
                return self._collection_cache[cache_key]
        # Outside lock — make the network call
        try:
            all_collections = self._qdrant.get_collections().collections
            names = [c.name for c in all_collections]
        except Exception as e:
            logger.warning("Failed to list Qdrant collections: %s", e)
            names = []
        with self._collection_cache_lock:
            self._collection_cache[cache_key] = names
            self._collection_cache_ts[cache_key] = time.time()
        return names

    def _discover_collections_for_module(self, module: str) -> List[str]:
        """List Qdrant collections matching a module prefix."""
        names = self._get_all_collection_names()
        return [n for n in names if n.startswith(f"{module}_")]

    def _discover_all_profile_collections(self, workspace_id: str) -> List[str]:
        """Discover all Qdrant collections belonging to a profile.

        MCAL collections: {module}_{swa|swud|testspec|jama}_{category}
        ILLD collections: either bare module names (cxpi) or rag_{module}_{type}
        """
        names = self._get_all_collection_names()
        if not names:
            return [self._qdrant_collection] if self._qdrant_collection else []

        if workspace_id == "mcal":
            # MCAL pattern: {module}_{swa|swud|testspec|jama}_{category}
            mcal_prefixes = ("_swa_", "_swud_", "_testspec_", "_jama_")
            return [n for n in names if any(p in n for p in mcal_prefixes)]
        else:
            # ILLD pattern: bare module names or rag_{module}_{type}
            illd = [n for n in names if n.startswith("rag_")]
            # Also include bare module-name collections (e.g. "cxpi")
            mcal_prefixes = ("_swa_", "_swud_", "_testspec_", "_jama_")
            bare = [n for n in names if not n.startswith("rag_")
                    and not any(p in n for p in mcal_prefixes)
                    and "_" not in n]
            return illd + bare if (illd or bare) else [self._qdrant_collection]

    # ── Collection → node_type mapping ─────────────────────────────────
    _COLLECTION_NODE_TYPE_MAP = {
        "swa_architecture": "SWA_ArchitecturalDecision",
        "swa_callsequences": "SWA_CallSequence",
        "swa_safety": "SWA_SafetyView",
        "swud_design": "SWUD_UnitDesign",
        "testspec_verification": "TestSpecification",
        "jama_requirements": "Requirement",
    }
    _ILLD_COLLECTION_NODE_TYPE_MAP = {
        "functions": "Function",
        "enums": "Enum",
        "structs": "Struct",
        "requirements": "Requirement",
        "hardware": "HardwareRegister",
        "registers": "Register",
        "macros": "Macro",
        "typedefs": "Typedef",
        "source": "SourceCode",
        "architecture": "ArchitecturalDecision",
        "pattern_library": "Pattern",
        "phases": "Phase",
    }

    @classmethod
    def _node_type_from_collection(cls, collection: str) -> Optional[str]:
        """Derive a meaningful node_type from the Qdrant collection name."""
        # MCAL: {module}_{source}_{category} e.g. "port_swa_architecture"
        parts = collection.split("_", 1)
        if len(parts) == 2:
            suffix = parts[1]  # e.g. "swa_architecture"
            if suffix in cls._COLLECTION_NODE_TYPE_MAP:
                return cls._COLLECTION_NODE_TYPE_MAP[suffix]
        # ILLD: rag_{module}_{type} e.g. "rag_cxpi_functions"
        if collection.startswith("rag_"):
            rag_parts = collection.split("_", 2)
            if len(rag_parts) == 3:
                cat = rag_parts[2]
                if cat in cls._ILLD_COLLECTION_NODE_TYPE_MAP:
                    return cls._ILLD_COLLECTION_NODE_TYPE_MAP[cat]
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Reciprocal Rank Fusion (RRF)
    # ─────────────────────────────────────────────────────────────────────

    def _merge_results_rrf(
        self, graph: List[Dict], vector: List[Dict], alpha: float,
        k: int = 60, workspace_id: str = "illd",
    ) -> List[Dict]:
        """
        Merge graph + vector results.

        ILLD: Reciprocal Rank Fusion — ``alpha/(k+rank+1)`` per list.
        MCAL: Global normalisation + alpha blending (preserves cross-source
        score differences and handles MCAL's multi-collection fan-out).
        """
        if workspace_id == "mcal":
            return self._merge_mcal(graph, vector, alpha)
        return self._merge_illd_rrf(graph, vector, alpha, k)

    def _merge_illd_rrf(
        self, graph: List[Dict], vector: List[Dict], alpha: float, k: int = 60,
    ) -> List[Dict]:
        """ILLD: rank-based RRF with alpha weighting."""
        scores: Dict[str, float] = {}
        items: Dict[str, Dict] = {}

        for rank, r in enumerate(graph):
            nid = r.get("node_id", str(id(r)))
            rrf = alpha * (1.0 / (k + rank + 1))
            scores[nid] = scores.get(nid, 0.0) + rrf
            if nid not in items:
                items[nid] = r

        for rank, r in enumerate(vector):
            nid = r.get("node_id", str(id(r)))
            rrf = (1.0 - alpha) * (1.0 / (k + rank + 1))
            scores[nid] = scores.get(nid, 0.0) + rrf
            if nid not in items:
                items[nid] = r

        merged = []
        for nid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            result = items[nid]
            result["score"] = round(score, 6)
            merged.append(result)

        return merged

    def _merge_mcal(
        self, graph: List[Dict], vector: List[Dict], alpha: float,
    ) -> List[Dict]:
        """MCAL: global normalise both lists, then alpha-blend.

        Preserves cross-collection score differences and prevents a single
        high-scoring collection from dominating via rank compression.
        """
        def _normalise(results: List[Dict]) -> None:
            if not results:
                return
            mx = max(r.get("score", 0) for r in results)
            if mx > 0:
                for r in results:
                    r["score"] = r.get("score", 0) / mx

        _normalise(graph)
        _normalise(vector)

        merged: Dict[str, Dict] = {}

        for r in vector:
            nid = r.get("node_id", str(id(r)))
            r["score"] = r.get("score", 0) * (1.0 - alpha)
            merged[nid] = r

        for r in graph:
            nid = r.get("node_id", str(id(r)))
            if nid in merged:
                merged[nid]["score"] += r.get("score", 0) * alpha
                merged[nid]["source"] = "hybrid"
            else:
                r["score"] = r.get("score", 0) * alpha
                merged[nid] = r

        result = list(merged.values())
        result.sort(key=lambda x: x.get("score", 0), reverse=True)
        for r in result:
            r["score"] = round(r["score"], 6)
        return result

    # ─────────────────────────────────────────────────────────────────────
    # search_nodes — structured query by label + filters
    # ─────────────────────────────────────────────────────────────────────

    def search_nodes(
        self, label: str, keyword: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        return_properties: Optional[List[str]] = None,
        limit: int = 10, offset: int = 0,
        workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Deterministic structured query."""
        if not self._neo4j:
            return {"nodes": [], "total_count": 0, "has_more": False}

        db = self._db_for_workspace(workspace_id)
        where_parts = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if keyword:
            where_parts.append(
                "(toLower(n.name) CONTAINS $kw "
                "OR toLower(coalesce(n.function_name,'')) CONTAINS $kw "
                "OR toLower(coalesce(n.test_case_id,'')) CONTAINS $kw "
                "OR toLower(coalesce(n.type_name,'')) CONTAINS $kw "
                "OR toLower(coalesce(n.api_name,'')) CONTAINS $kw "
                "OR toLower(coalesce(n.peripheral_name,'')) CONTAINS $kw "
                "OR toLower(coalesce(n.decision_id,'')) CONTAINS $kw)"
            )
            params["kw"] = keyword.lower()

        if filters:
            for i, (k, v) in enumerate(filters.items()):
                pname = f"f{i}"
                where_parts.append(f"n.{k} = ${pname}")
                params[pname] = v

        where_str = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        ret = "n" if not return_properties else ", ".join(f"n.{p} AS {p}" for p in return_properties)

        cypher = f"MATCH (n:{label}) {where_str} RETURN {ret} SKIP $offset LIMIT $limit"

        nodes = []
        try:
            with self._neo4j.session(database=db) as session:
                for rec in session.run(cypher, params):
                    if return_properties:
                        nodes.append({p: rec[p] for p in return_properties})
                    else:
                        nodes.append(dict(rec["n"].items()))
        except Exception as e:
            logger.error("search_nodes failed: %s", e)

        return {"nodes": nodes, "total_count": len(nodes), "has_more": len(nodes) == limit}

    # ─────────────────────────────────────────────────────────────────────
    # get_node_by_id — exact lookup
    # ─────────────────────────────────────────────────────────────────────

    def get_node_by_id(
        self, document_id: Optional[str] = None, jama_id: Optional[int] = None,
        label: Optional[str] = None, workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Exact node lookup by any known ID property.

        Matches against: document_id, id, global_id, feature_id, decision_id,
        service_id (string IDs) and jama_id (integer).
        """
        if not self._neo4j:
            return {"node": None, "found": False}

        db = self._db_for_workspace(workspace_id)
        label_str = f":{label}" if label else ""

        if document_id:
            where = self._id_where("n", "did")
            cypher = f"MATCH (n{label_str}) WHERE {where} RETURN n, labels(n) AS lbl LIMIT 1"
            params = {"did": document_id}
        elif jama_id:
            cypher = f"MATCH (n{label_str} {{jama_id: $jid}}) RETURN n, labels(n) AS lbl LIMIT 1"
            params = {"jid": jama_id}
        else:
            return {"node": None, "found": False, "error": "Provide document_id or jama_id"}

        try:
            with self._neo4j.session(database=db) as session:
                rec = session.run(cypher, params).single()
                if rec:
                    props = dict(rec["n"].items())
                    props["_labels"] = rec["lbl"]
                    return {"node": props, "found": True}
        except Exception as e:
            logger.error("get_node_by_id failed: %s", e)

        return {"node": None, "found": False}

    # ─────────────────────────────────────────────────────────────────────
    # get_neighbors — graph traversal
    # ─────────────────────────────────────────────────────────────────────

    def get_neighbors(
        self, document_id: Optional[str] = None, jama_id: Optional[int] = None,
        direction: str = "both", relationship_types: Optional[List[str]] = None,
        limit: int = 20, workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Direct graph traversal from a known node."""
        if not self._neo4j:
            return {"neighbors": [], "total_count": 0, "has_more": False}

        db = self._db_for_workspace(workspace_id)

        # Find anchor node — match any known ID property
        if document_id:
            where = self._id_where("anchor", "anchor_id")
            match_clause = f"MATCH (anchor) WHERE {where}"
            params: Dict[str, Any] = {"anchor_id": document_id, "limit": limit}
        elif jama_id:
            match_clause = "MATCH (anchor {jama_id: $anchor_id})"
            params = {"anchor_id": jama_id, "limit": limit}
        else:
            return {"neighbors": [], "total_count": 0, "error": "Provide document_id or jama_id"}

        # Direction
        if direction == "out":
            pattern = "(anchor)-[r]->(neighbor)"
        elif direction == "in":
            pattern = "(anchor)<-[r]-(neighbor)"
        else:
            pattern = "(anchor)-[r]-(neighbor)"

        # Relationship filter
        rel_filter = ""
        if relationship_types:
            rel_labels = "|".join(relationship_types)
            rel_filter = f":{rel_labels}"
            pattern = pattern.replace("[r]", f"[r{rel_filter}]")

        cypher = f"""
        {match_clause}
        MATCH {pattern}
        RETURN type(r) AS rel_type, labels(neighbor)[0] AS neighbor_type,
               neighbor AS n
        LIMIT $limit
        """

        neighbors = []
        try:
            with self._neo4j.session(database=db) as session:
                for rec in session.run(cypher, params):
                    props = dict(rec["n"].items())
                    neighbors.append({
                        "relationship": rec["rel_type"],
                        "node_type": rec["neighbor_type"],
                        "properties": props,
                        "node_id": next((props[p] for p in self._STR_ID_PROPS if props.get(p)), props.get("name", "?")),
                    })
        except Exception as e:
            logger.error("get_neighbors failed: %s", e)

        return {"neighbors": neighbors, "total_count": len(neighbors), "has_more": len(neighbors) == limit}

    # ─────────────────────────────────────────────────────────────────────
    # shortest_path — path analysis between two nodes
    # ─────────────────────────────────────────────────────────────────────

    def shortest_path(
        self, from_id: Optional[str] = None, from_jama: Optional[int] = None,
        to_id: Optional[str] = None, to_jama: Optional[int] = None,
        max_depth: int = 8, workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Find shortest path between two nodes."""
        if not self._neo4j:
            return {"path": [], "depth": 0, "found": False}

        db = self._db_for_workspace(workspace_id)

        # Build anchor matches — universal ID matching
        if from_id:
            from_where = self._id_where("a", "from_id")
            from_match = f"MATCH (a) WHERE {from_where}"
            params: Dict[str, Any] = {"from_id": from_id}
        elif from_jama:
            from_match = "MATCH (a {jama_id: $from_jama})"
            params = {"from_jama": from_jama}
        else:
            return {"path": [], "depth": 0, "found": False, "error": "Provide from_document_id or from_jama_id"}

        if to_id:
            to_where = self._id_where("b", "to_id")
            to_match = f"MATCH (b) WHERE {to_where}"
            params["to_id"] = to_id
        elif to_jama:
            to_match = "MATCH (b {jama_id: $to_jama})"
            params["to_jama"] = to_jama
        else:
            return {"path": [], "depth": 0, "found": False, "error": "Provide to_document_id or to_jama_id"}

        params["max_depth"] = max_depth

        cypher = f"""
        {from_match}
        {to_match}
        MATCH path = shortestPath((a)-[*..{max_depth}]-(b))
        RETURN [n IN nodes(path) | {{
            id: coalesce(n.document_id, n.id, n.global_id, n.feature_id, n.decision_id, n.service_id),
            name: coalesce(n.name, n.function_name, n.type_name, n.api_name, n.peripheral_name, n.decision_id, n.requirement_id, 'unknown'),
            labels: labels(n)
        }}] AS path_nodes,
        [r IN relationships(path) | type(r)] AS rel_types,
        length(path) AS depth
        """

        try:
            with self._neo4j.session(database=db) as session:
                rec = session.run(cypher, params).single()
                if rec:
                    return {
                        "path": rec["path_nodes"],
                        "relationships": rec["rel_types"],
                        "depth": rec["depth"],
                        "found": True,
                    }
        except Exception as e:
            logger.error("shortest_path failed: %s", e)

        return {"path": [], "depth": 0, "found": False}

    # ─────────────────────────────────────────────────────────────────────
    # execute_cypher — read-only custom queries
    # ─────────────────────────────────────────────────────────────────────

    def execute_cypher(
        self, query: str, parameters: Optional[Dict[str, Any]] = None,
        workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Execute read-only Cypher. Write clauses are pre-rejected by MCP layer."""
        if not self._neo4j:
            return {"rows": [], "error": "Neo4j not available"}

        db = self._db_for_workspace(workspace_id)
        params = parameters or {}

        rows = []
        try:
            with self._neo4j.session(database=db) as session:
                result = session.run(query, params)
                for record in result:
                    row = {}
                    for key in record.keys():
                        val = record[key]
                        # Convert Neo4j Node to dict
                        if hasattr(val, "items"):
                            row[key] = dict(val.items())
                        else:
                            row[key] = val
                    rows.append(row)
        except Exception as e:
            logger.error("execute_cypher failed: %s", e)
            return {"rows": [], "error": str(e)}

        return {"rows": rows, "total_count": len(rows)}

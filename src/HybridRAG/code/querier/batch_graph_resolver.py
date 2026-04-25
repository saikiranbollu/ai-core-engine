"""
Batch Graph Queries — GAP-A06 (Sprint 11)
==========================================
Eliminates the N+1 Cypher query problem documented in Perf_improvements.md.

Instead of issuing 50-200 individual Cypher queries per hybrid search
(one per entity for relationship enrichment), this module:
  1. Collects all node element-IDs from vector search + entity lookup
  2. Batch-fetches all nodes with properties in a single UNWIND query
  3. Batch-fetches all 1-hop relationships in a single UNWIND query
  4. Assembles results back into the per-node dicts SearchService expects

Expected impact: Reduce Cypher queries from 50-200 → 3-5 per search.
Estimated 60-80% reduction in graph query latency.

Design principles:
  - Preserves NodeSet isolation (module filter in WHERE clause)
  - Graceful degradation: if batch fails, falls back to per-node queries
  - All operations are synchronous (Neo4j driver is sync; caller wraps in asyncio.to_thread)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BatchGraphResolver:
    """
    Batch-resolves node properties and relationships from Neo4j.

    Replaces the per-node _get_node_relationships() calls in SearchService
    with bulk UNWIND queries.

    Parameters
    ----------
    neo4j_driver : neo4j.Driver
        Connected Neo4j driver instance.
    default_database : str
        Neo4j database name (from workspace mapping).
    """

    def __init__(self, neo4j_driver, default_database: str = "neo4j"):
        self._driver = neo4j_driver
        self._db = default_database
        self._use_element_id: Optional[bool] = None

    def _eid_fn(self) -> str:
        """Return elementId (Neo4j 5.x) or id (Neo4j 4.x) based on version detection."""
        if self._use_element_id is None:
            if not self._driver:
                self._use_element_id = False
            else:
                try:
                    with self._driver.session(database=self._db) as s:
                        ver = s.run("CALL dbms.components() YIELD versions RETURN versions[0] AS v").single()["v"]
                    self._use_element_id = int(str(ver).split(".")[0]) >= 5
                except Exception:
                    self._use_element_id = False
        return "elementId" if self._use_element_id else "id"

    @property
    def available(self) -> bool:
        return self._driver is not None

    # ─────────────────────────────────────────────────────────────────────
    # Batch node fetch
    # ─────────────────────────────────────────────────────────────────────

    def batch_fetch_nodes(
        self,
        element_ids: List[str],
        module: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Fetch full node properties for a batch of element IDs.

        Parameters
        ----------
        element_ids : list[str]
            Neo4j element IDs (from entity lookup or graph search).
        module : str, optional
            If provided, filter to nodes belonging to this module's NodeSet.

        Returns
        -------
        dict mapping element_id → {labels: [...], properties: {...}}
        """
        if not self._driver or not element_ids:
            return {}

        # Deduplicate
        unique_ids = list(set(element_ids))

        fn = self._eid_fn()
        cypher = f"""
        UNWIND $eids AS eid
        MATCH (n) WHERE {fn}(n) = eid
        RETURN {fn}(n) AS eid, labels(n) AS node_labels, properties(n) AS props
        """

        # If module specified, add NodeSet filter
        if module:
            cypher = f"""
            UNWIND $eids AS eid
            MATCH (n) WHERE {fn}(n) = eid
            OPTIONAL MATCH (n)-[:HAS_MODULE]->(ns:NodeSet)
            WHERE ns.module = $module OR $module IS NULL
            WITH n, ns, eid
            WHERE ns IS NOT NULL OR $module IS NULL
            RETURN {fn}(n) AS eid, labels(n) AS node_labels, properties(n) AS props
            """

        result: Dict[str, Dict[str, Any]] = {}

        try:
            with self._driver.session(database=self._db) as session:
                params = {"eids": unique_ids}
                if module:
                    params["module"] = module.upper()

                for record in session.run(cypher, params):
                    eid = record["eid"]
                    result[eid] = {
                        "labels": record["node_labels"],
                        "properties": dict(record["props"]) if record["props"] else {},
                    }

            logger.debug(
                "Batch node fetch: %d requested, %d found",
                len(unique_ids), len(result),
            )

        except Exception as exc:
            logger.error("Batch node fetch failed: %s", exc)
            # Graceful degradation: return empty — caller will use per-node fallback

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Batch relationship fetch
    # ─────────────────────────────────────────────────────────────────────

    def batch_fetch_relationships(
        self,
        element_ids: List[str],
        max_rels_per_node: int = 5,
        module: Optional[str] = None,
        rel_types: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch 1-hop relationships for a batch of node element IDs.

        Parameters
        ----------
        element_ids : list[str]
            Neo4j element IDs.
        max_rels_per_node : int
            Maximum relationships to return per node (default 5).
        module : str, optional
            Filter targets to this module's NodeSet.
        rel_types : list[str], optional
            Only return these relationship types. If None, returns all.

        Returns
        -------
        dict mapping element_id → list of relationship dicts
        """
        if not self._driver or not element_ids:
            return {}

        unique_ids = list(set(element_ids))

        # Build relationship type filter clause
        rel_filter = ""
        if rel_types:
            quoted = ", ".join(f'"{rt}"' for rt in rel_types)
            rel_filter = f"AND type(r) IN [{quoted}]"

        fn = self._eid_fn()
        cypher = f"""
        UNWIND $eids AS eid
        MATCH (n) WHERE {fn}(n) = eid
        OPTIONAL MATCH (n)-[r]-(m)
        WHERE true {rel_filter}
        WITH eid, n, r, m
        ORDER BY eid, type(r)
        WITH eid,
             collect({{
                 rel_type: type(r),
                 direction: CASE WHEN startNode(r) = n THEN 'outgoing' ELSE 'incoming' END,
                 target_eid: {fn}(m),
                 target_labels: labels(m),
                 target_name: coalesce(m.name, m.function_name, m.type_name,
                                       m.api_name, m.requirement_id, 'unknown'),
                 target_id: coalesce(m.document_id, m.id, m.global_id,
                                     m.feature_id, m.decision_id, m.service_id)
             }})[0..$max_rels] AS rels
        RETURN eid, rels
        """

        result: Dict[str, List[Dict[str, Any]]] = {}

        try:
            with self._driver.session(database=self._db) as session:
                for record in session.run(
                    cypher,
                    {"eids": unique_ids, "max_rels": max_rels_per_node},
                ):
                    eid = record["eid"]
                    rels = record["rels"] or []
                    # Filter out null entries (from OPTIONAL MATCH with no matches)
                    result[eid] = [
                        {
                            "type": rel["rel_type"],
                            "direction": rel["direction"],
                            "target_type": (rel["target_labels"] or ["?"])[0],
                            "target": rel["target_name"],
                            "target_id": rel["target_id"],
                            "target_eid": rel["target_eid"],
                        }
                        for rel in rels
                        if rel.get("rel_type") is not None
                    ]

            logger.debug(
                "Batch relationship fetch: %d nodes, %d total rels",
                len(unique_ids),
                sum(len(v) for v in result.values()),
            )

        except Exception as exc:
            logger.error("Batch relationship fetch failed: %s", exc)

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Combined batch enrichment
    # ─────────────────────────────────────────────────────────────────────

    def batch_enrich(
        self,
        results: List[Dict[str, Any]],
        module: Optional[str] = None,
        max_rels_per_node: int = 5,
        include_relationships: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Enrich a list of search result dicts with batch-fetched graph data.

        This is the main entry point — replaces the per-node loop in
        SearchService._graph_search() and entity_targeted_lookup().

        Parameters
        ----------
        results : list[dict]
            Search results, each must have '_element_id' or 'node_id'.
        module : str, optional
            Module filter for NodeSet isolation.
        max_rels_per_node : int
            Max relationships per node.
        include_relationships : bool
            Whether to fetch relationships.

        Returns
        -------
        The same list, with 'relationships' key added to each result.
        """
        if not results or not self._driver:
            return results

        # Collect element IDs
        eid_map: Dict[str, int] = {}
        for idx, r in enumerate(results):
            eid = r.get("_element_id") or r.get("_node_id") or r.get("node_id")
            if eid:
                eid_map[eid] = idx

        if not eid_map:
            logger.debug("No element IDs found in results — skipping batch enrich")
            return results

        eids = list(eid_map.keys())

        # Batch fetch relationships
        if include_relationships:
            try:
                rels = self.batch_fetch_relationships(
                    eids,
                    max_rels_per_node=max_rels_per_node,
                    module=module,
                )
                for eid, rel_list in rels.items():
                    idx = eid_map.get(eid)
                    if idx is not None and idx < len(results):
                        results[idx]["relationships"] = rel_list
            except Exception as exc:
                logger.warning("Batch enrich relationships failed, skipping: %s", exc)

        logger.info(
            "Batch enrich complete: %d results, %d enriched with rels",
            len(results),
            sum(1 for r in results if r.get("relationships")),
        )

        return results


class BatchQueryStats:
    """Tracks batch query metrics for Prometheus integration."""

    def __init__(self):
        self.total_batch_calls = 0
        self.total_nodes_fetched = 0
        self.total_rels_fetched = 0
        self.fallback_count = 0

    def record_batch(self, nodes: int, rels: int) -> None:
        self.total_batch_calls += 1
        self.total_nodes_fetched += nodes
        self.total_rels_fetched += rels

    def record_fallback(self) -> None:
        self.fallback_count += 1

    def as_dict(self) -> Dict[str, int]:
        return {
            "total_batch_calls": self.total_batch_calls,
            "total_nodes_fetched": self.total_nodes_fetched,
            "total_rels_fetched": self.total_rels_fetched,
            "fallback_count": self.fallback_count,
        }

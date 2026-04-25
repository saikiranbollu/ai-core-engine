"""
Hybrid Sandbox Querier — Mixed Sandbox + Production DB RAG
==========================================================

Allows users to query their sandbox (arch/code under development) AND
the main production DB (HW specs, datasheets) simultaneously.  Results
are merged with weighted scoring and origin tracking.

Usage::

    from src.MemoryLayer.memory.hybrid_sandbox_querier import HybridSandboxQuerier

    querier = HybridSandboxQuerier(sandbox, search_service)
    results = querier.hybrid_query("Adc_Init configuration", branch_tag="release/2.0")
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HybridSandboxQuerier:
    """Merge sandbox (ephemeral) and production DB search results.

    Parameters
    ----------
    sandbox : EphemeralSandbox
        The user's active ephemeral sandbox.
    search_service : SearchService
        Production-DB search service (Qdrant + Neo4j).
    sandbox_weight : float
        Weight for sandbox results in the merge (0..1). Default 0.6.
    db_weight : float
        Weight for production DB results in the merge (0..1). Default 0.4.
    """

    def __init__(
        self,
        sandbox,
        search_service,
        sandbox_weight: float = 0.6,
        db_weight: float = 0.4,
    ):
        from .ephemeral_sandbox import SandboxQuerier

        self._sandbox = sandbox
        self._sandbox_querier = SandboxQuerier(sandbox)
        self._search_service = search_service
        self._sandbox_weight = sandbox_weight
        self._db_weight = db_weight

    def hybrid_query(
        self,
        query: str,
        top_k: int = 10,
        branch_tag: Optional[str] = None,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Run query against both sandbox and production DB, merge results.

        Parameters
        ----------
        query : str
            Natural language search query.
        top_k : int
            Maximum total results to return.
        branch_tag : str | None
            Production DB branch filter.
        filter_by_module : str | None
            Filter production DB results by module.
        filter_by_node_type : list[str] | None
            Filter production DB results by node labels.
        workspace_id : str
            Target workspace for production DB search.

        Returns
        -------
        dict
            ``{"results": [...], "total_count": int,
              "sandbox_hits": int, "db_hits": int}``
        """
        # 1. Query sandbox
        sandbox_results = []
        try:
            sandbox_results = self._sandbox_querier.search(
                query, top_k=top_k, alpha=0.5,
            )
        except Exception as e:
            logger.warning("[HybridSandboxQuerier] Sandbox search failed: %s", e)

        # 2. Query production DB
        db_results_raw = []
        try:
            db_response = self._search_service.hybrid_search(
                query=query,
                max_results=top_k,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
                branch_tag=branch_tag,
            )
            db_results_raw = db_response.get("results", [])
        except Exception as e:
            logger.warning("[HybridSandboxQuerier] DB search failed: %s", e)

        # 3. Normalize + weight
        sandbox_scored = []
        for r in sandbox_results:
            sandbox_scored.append({
                "node_id": r.node_id,
                "content": r.content,
                "score": r.score * self._sandbox_weight,
                "node_type": r.node_type,
                "origin": "sandbox",
                "metadata": r.metadata,
            })

        db_scored = []
        for r in db_results_raw:
            node_id = r.get("node_id", r.get("id", ""))
            db_scored.append({
                "node_id": node_id,
                "content": r.get("content", r.get("text", "")),
                "score": r.get("score", 0.0) * self._db_weight,
                "node_type": r.get("label", r.get("node_type", "")),
                "origin": "db",
                "metadata": r.get("properties", r.get("metadata", {})),
            })

        # 4. Merge + dedup by node_id (keep highest score)
        seen: Dict[str, Dict] = {}
        for item in sandbox_scored + db_scored:
            nid = item["node_id"]
            if nid not in seen or item["score"] > seen[nid]["score"]:
                seen[nid] = item

        merged = sorted(seen.values(), key=lambda x: -x["score"])[:top_k]

        sandbox_count = sum(1 for r in merged if r["origin"] == "sandbox")
        db_count = sum(1 for r in merged if r["origin"] == "db")

        return {
            "results": merged,
            "total_count": len(merged),
            "sandbox_hits": sandbox_count,
            "db_hits": db_count,
            "query": query,
        }

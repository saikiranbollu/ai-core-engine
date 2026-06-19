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

# Config-driven default alpha (MEG_SW-308)
try:
    from env_config import get_default_search_alpha as _get_default_alpha
    _DEFAULT_SEARCH_ALPHA = _get_default_alpha()
except Exception:
    _DEFAULT_SEARCH_ALPHA = 0.6
from .context_compressor import ContextCompressor
from .context_refiner import ContextRefiner
from .kg_node_utils import (
    LABEL_DISPLAY_PROPS, LABEL_ID_PROPS, LABEL_NAME_PROPS, COMPACT_PROPS,
    Source, classify_source, extract_keywords, extract_named_entities,
    format_source_for_context, infer_labels, is_aggregation_query,
    node_display_name, node_unique_id, normalise_scores, score_node,
    serialize_node,
)
from .query_enhancer import QueryEnhancer
from .mcal_query_enhancer import McalEnhancedQuery, McalPatternExecutor, McalQueryEnhancer
from .query_logger import log_query as _log_query
from .relevance_judge import RelevanceJudge
from .reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)

# Structural/navigational labels that should score lower in keyword search.
# These appear frequently with generic names (e.g. Folder "Adc_Init") but
# carry little domain content compared to SRC_Function, SFR_Register, etc.
_LOW_VALUE_LABELS = frozenset({
    "Folder", "EA_Diagram", "EA_ActivityNode",
})

# ── LP-variant alias resolution for Neo4j hardware queries ──────────────
# LPBTM and LPCAN are low-power variants of BTM/CAN that share the same
# register map.  All HardwareRegister / RegisterField / etc. nodes are
# stored under module='BTM' or module='CAN'.  Any query that references
# LPBTM or LPCAN must be redirected to the parent module for Neo4j lookups.
# Qdrant collections (lpbtm / lpcan) are intentionally kept separate.
_NEO4J_MODULE_ALIASES: Dict[str, str] = {
    "LPBTM": "BTM",
    "LPCAN": "CAN",
}

# ── Regex patterns for extracting citation metadata from Cypher queries ──
_CYPHER_LABEL_RE = re.compile(r'\([\w]*:([\w]+)\)', re.IGNORECASE)
_CYPHER_REL_RE = re.compile(r'\[[\w]*:?([\w]+)?\]', re.IGNORECASE)
_CYPHER_REL_TYPE_RE = re.compile(r'\[:(\w+)\]', re.IGNORECASE)


def _build_cypher_citations(
    query: str,
    rows: List[Dict[str, Any]],
    workspace_id: str,
    database: str,
) -> Dict[str, Any]:
    """Extract citation metadata from a Cypher query and its results."""
    # Parse labels and relationship types from query
    labels_in_query = _CYPHER_LABEL_RE.findall(query)
    rel_types_in_query = _CYPHER_REL_TYPE_RE.findall(query)

    # Collect unique node citations from result rows
    node_citations = []
    seen_ids = set()
    for row in rows:
        for key, val in row.items():
            if isinstance(val, dict) and "_node_id" in val:
                nid = val["_node_id"]
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    node_citations.append({
                        "node_id": nid,
                        "label": val.get("_label", ""),
                        "name": val.get("_name", ""),
                    })

    citations = {
        "source": "neo4j",
        "database": database,
        "profile": workspace_id,
        "cypher": query,
        "labels_queried": list(dict.fromkeys(labels_in_query)),
        "relationships_traversed": list(dict.fromkeys(rel_types_in_query)),
        "result_count": len(rows),
        "nodes": node_citations,
    }
    return citations


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
        self._query_enhancer: Optional[McalQueryEnhancer] = None
        self._pattern_executor: Optional[McalPatternExecutor] = None
        # TTL-cached collection list (avoids Qdrant get_collections() on every search)
        self._collection_cache: Dict[str, List[str]] = {}  # workspace → names
        self._collection_cache_ts: Dict[str, float] = {}   # workspace → timestamp
        self._collection_cache_ttl: float = float(os.environ.get("COLLECTION_CACHE_TTL", "300"))  # 5 min
        self._collection_cache_lock = threading.Lock()

        # ── GAP pipeline components ─────────────────────────────────
        self._enhancer = QueryEnhancer()
        self._compressor = ContextCompressor()
        self._judge = RelevanceJudge()
        self._refiner = ContextRefiner()
        self._reranker = CrossEncoderReranker()
        self._batch_resolver = None

    @property
    def available(self) -> bool:
        return self._neo4j is not None

    def set_query_enhancer(self, enhancer: McalQueryEnhancer) -> None:
        """Attach a MCAL QueryEnhancer instance (LLM-powered, for mcal workspace)."""
        self._query_enhancer = enhancer
        if self._neo4j:
            self._pattern_executor = McalPatternExecutor(self._neo4j, self._default_db)

    def set_llm_fn(self, llm_fn):
        """Wire an LLM callable into all pipeline components that need it."""
        if self._compressor and hasattr(self._compressor, '_abstractive'):
            self._compressor._abstractive._llm_fn = llm_fn
            self._compressor._abstractive._enabled = True
        if self._judge and hasattr(self._judge, '_custom_backend'):
            self._judge._custom_backend._llm_fn = llm_fn
        if self._refiner:
            self._refiner._llm_fn = llm_fn
            self._refiner._search_fn = self._search_fn if hasattr(self, '_search_fn') else None

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
    # All known ID properties across ILLD (document_id, id, jama_id),
    # MCAL (global_id, feature_id, decision_id, service_id),
    # SRC  (function_id, file_id, type_id, macro_id, variable_id),
    # SFR  (register_id, bitfield_id, base_address_id),
    # and shared name-based IDs (function_name, name, requirement_id,
    # test_case_id, param_name, type_name, module_name).

    _STR_ID_PROPS = (
        # Original ILLD / MCAL IDs
        "document_id", "id", "global_id", "feature_id", "decision_id", "service_id",
        # SRC node IDs
        "function_id", "file_id", "type_id", "macro_id", "variable_id",
        # SFR node IDs
        "register_id", "bitfield_id", "base_address_id",
        # Name-based IDs (used as primary key for many labels)
        "requirement_id", "test_case_id", "function_name", "param_name",
        "type_name", "macro_name", "api_name", "module_name",
        # Fallback
        "name",
    )

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

    @staticmethod
    def _neo4j_module(module: Optional[str]) -> Optional[str]:
        """Resolve LP-variant aliases to their parent module for Neo4j queries.

        LPBTM → BTM, LPCAN → CAN.  All hardware nodes (HardwareRegister,
        RegisterField, etc.) are stored under the parent module.  Qdrant
        collections for LP variants are intentionally separate and are NOT
        affected by this resolution.
        """
        if module is None:
            return None
        return _NEO4J_MODULE_ALIASES.get(module.upper(), module.upper())

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
        alpha: float = _DEFAULT_SEARCH_ALPHA,
        skip_judge: bool = False,
    ) -> Dict[str, Any]:
        """
        Combined graph + vector search.

        alpha controls blend:
          0.0 = pure vector (Qdrant)
          config default = balanced hybrid
          1.0 = pure graph (Neo4j)
        """
        _qs_t0 = time.perf_counter()
        graph_results = []
        vector_results = []
        pattern_results = []

        # ── Stage 0: Query analysis ─────────────────────────────────
        is_agg = is_aggregation_query(query)
        named_ents = extract_named_entities(query, module=filter_by_module or self.module)
        if is_agg:
            max_results = max(max_results, 100)

        # ── Stage 0b: Query enhancement (MCAL LLM expansion) ────────
        enhanced: Optional[McalEnhancedQuery] = None
        if self._query_enhancer and not filter_by_node_type:
            try:
                enhanced = self._query_enhancer.enhance(
                    query, module=filter_by_module or self.module,
                )
            except Exception as e:
                logger.warning("Query enhancement failed (continuing with basic search): %s", e)

        # Execute Cypher patterns from enhancer (if any)
        if enhanced and enhanced.cypher_patterns and self._pattern_executor:
            try:
                pattern_results = self._pattern_executor.execute_patterns(
                    enhanced.cypher_patterns,
                )
                logger.debug("Pattern execution returned %d results", len(pattern_results))
            except Exception as e:
                logger.warning("Pattern execution failed: %s", e)

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
                    enhanced=enhanced,
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
                enhanced=enhanced,
            )
        elif do_vector:
            vector_results = self._vector_search(
                query, max_results=max_results * 2,
                filter_by_module=filter_by_module,
                filter_by_node_type=filter_by_node_type,
                workspace_id=workspace_id,
            )

        logger.debug("hybrid_search: graph=%d vector=%d pattern=%d (alpha=%.2f, enhanced=%s)",
                      len(graph_results), len(vector_results), len(pattern_results),
                      alpha, enhanced.enhanced if enhanced else False)
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

        # ── Stage 2b: Merge pattern results into graph results ─────
        # Pattern results have richer content (relationship properties) and
        # score 1.5, so they should override keyword results with same node_id.
        # Exception: keep keyword results that have neighbor expansion (longer content).
        if pattern_results:
            pattern_by_id = {pr.get("node_id"): pr for pr in pattern_results}
            # Replace existing keyword results with pattern versions,
            # but only if the pattern version has richer content.
            for i, gr in enumerate(graph_results):
                nid = gr.get("node_id")
                if nid in pattern_by_id:
                    pr = pattern_by_id.pop(nid)
                    # Keep the keyword version if it has neighbor expansion
                    # (typically much longer due to "Related KG nodes" section).
                    gr_len = len(gr.get("content", ""))
                    pr_len = len(pr.get("content", ""))
                    if pr_len > gr_len:
                        graph_results[i] = pr
                    else:
                        # Keep keyword version but boost its score
                        graph_results[i]["score"] = max(gr.get("score", 0), pr.get("score", 0))
            # Append remaining pattern results that had no keyword match
            for pr in pattern_by_id.values():
                graph_results.append(pr)
            # Ensure max_results covers all pattern results
            max_results = max(max_results, len(pattern_results) + 10)

        # Sort graph results by score so pattern results (score 1.5) rank
        # above keyword matches before RRF rank-based fusion.
        graph_results.sort(key=lambda r: r.get("score", 0), reverse=True)

        # ── Stage 3: RRF merge ────────────────────────────────────────
        merged = self._merge_results_weighted(
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

        # ── Stage 5: GAP pipeline (compress → judge → refine) ────────
        pipeline_stats: Dict[str, Any] = {}
        complexity = "medium"

        # Query enhancement (for complexity detection)
        if hasattr(self, '_enhancer') and self._enhancer is not None:
            try:
                enhanced = self._enhancer.enhance(query)
                complexity = enhanced.complexity.value if hasattr(enhanced.complexity, 'value') else str(enhanced.complexity)
            except Exception:
                logger.debug("Query enhancer failed, using default complexity", exc_info=True)

        # 5a. Compress
        if hasattr(self, '_compressor') and self._compressor is not None and getattr(self._compressor, 'available', False):
            try:
                comp_result = self._compressor.compress(merged, query, complexity=complexity)
                merged = comp_result.compressed_items
                pipeline_stats["compression"] = {
                    "compression_ratio": comp_result.compression_ratio,
                    "original_tokens": comp_result.original_tokens,
                    "compressed_tokens": comp_result.compressed_tokens,
                    "stages_applied": comp_result.stages_applied,
                    "items_before": comp_result.items_before,
                    "items_after": comp_result.items_after,
                }
            except Exception:
                logger.debug("Compressor failed, skipping", exc_info=True)

        # 5b. Judge
        if (not skip_judge
                and hasattr(self, '_judge') and self._judge is not None
                and getattr(self._judge, 'available', False)):
            try:
                judge_result = self._judge.judge(query, merged)
                merged = judge_result.results
                pipeline_stats["relevance_judging"] = {
                    "judged": judge_result.judged,
                    "original_count": judge_result.original_count,
                    "kept_count": judge_result.kept_count,
                    "dropped_count": judge_result.dropped_count,
                    "backend": judge_result.backend,
                }
            except Exception:
                logger.debug("Judge failed, skipping", exc_info=True)
        elif skip_judge:
            pipeline_stats["relevance_judging"] = {
                "judged": False,
                "skip_reason": "skip_judge enabled",
            }

        # 5c. Refine (only for complex queries)
        if (hasattr(self, '_refiner') and self._refiner is not None
                and getattr(self._refiner, 'available', False)
                and complexity == "complex"):
            try:
                ref_result = self._refiner.refine(query, merged, complexity=complexity)
                merged = ref_result.refined_items
                pipeline_stats["refinement"] = {
                    "iterations": ref_result.iterations,
                    "agents_used": ref_result.agents_used,
                    "gaps_found": ref_result.gaps_found,
                    "gaps_resolved": ref_result.gaps_resolved,
                    "completeness_score": ref_result.completeness_score,
                    "crag_corrections": ref_result.crag_corrections,
                    "self_rag_retrievals": ref_result.self_rag_retrievals,
                }
            except Exception:
                logger.debug("Refiner failed, skipping", exc_info=True)

        # ── Stage 6: HSI enrichment — EA_Register trust zone info ─────
        pre_enrich = len(merged)
        merged = self._enrich_hsi_registers(merged)
        if len(merged) > pre_enrich:
            # Re-sort so enrichment results slot in by score
            merged.sort(key=lambda r: r.get("score", 0), reverse=True)
            # Ensure enriched results fit within page
            max_results += (len(merged) - pre_enrich)

        # Paginate
        total = len(merged)
        page = merged[offset:offset + max_results]

        # ── Build citations ────────────────────────────────────────────
        db = self._db_for_workspace(workspace_id)
        citations = {
            "source": "neo4j+qdrant" if alpha < 1.0 else "neo4j",
            "database": db,
            "profile": workspace_id,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "result_count": len(page),
            "nodes": [
                {
                    "node_id": r.get("node_id", ""),
                    "node_type": r.get("node_type", ""),
                    "name": r.get("properties", {}).get("name", "")
                           or r.get("properties", {}).get("function_name", ""),
                    "source": r.get("source", ""),
                    "score": round(r.get("score", 0), 4),
                }
                for r in page
            ],
        }
        if filter_by_module:
            citations["module_filter"] = filter_by_module
        if filter_by_node_type:
            citations["node_type_filter"] = filter_by_node_type

        result = {
            "results": page,
            "total_count": total,
            "has_more": (offset + max_results) < total,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "alpha": alpha,
            "named_entities": named_ents,
            "is_aggregation": is_agg,
            "citations": citations,
        }
        result.update(pipeline_stats)
        _log_query(
            method="hybrid_search", cypher=f"hybrid:{query[:200]}",
            elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=result.get("total_count", 0),
            module=filter_by_module or self.module,
            profile=workspace_id,
        )
        return result

    async def hybrid_search_async(
        self,
        query: str,
        max_results: int = 10,
        include_relationships: bool = False,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        offset: int = 0,
        workspace_id: str = "illd",
        alpha: float = _DEFAULT_SEARCH_ALPHA,
    ) -> Dict[str, Any]:
        """Async hybrid search — runs graph and vector stages concurrently."""
        graph_results: List[Dict[str, Any]] = []
        vector_results: List[Dict[str, Any]] = []
        pattern_results: List[Dict[str, Any]] = []

        is_agg = is_aggregation_query(query)
        named_ents = extract_named_entities(query, module=filter_by_module or self.module)
        if is_agg:
            max_results = max(max_results, 100)

        # ── Query enhancement (MCAL LLM expansion) ───────────────────
        enhanced: Optional[McalEnhancedQuery] = None
        if self._query_enhancer and not filter_by_node_type:
            try:
                enhanced = await asyncio.to_thread(
                    self._query_enhancer.enhance,
                    query, filter_by_module or self.module,
                )
            except Exception as e:
                logger.warning("Async query enhancement failed: %s", e)

        if enhanced and enhanced.cypher_patterns and self._pattern_executor:
            try:
                pattern_results = await asyncio.to_thread(
                    self._pattern_executor.execute_patterns,
                    enhanced.cypher_patterns,
                )
            except Exception as e:
                logger.warning("Async pattern execution failed: %s", e)

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
                enhanced=enhanced,
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

        # Merge pattern results into graph results
        if pattern_results:
            pattern_by_id = {pr.get("node_id"): pr for pr in pattern_results}
            for i, gr in enumerate(graph_results):
                nid = gr.get("node_id")
                if nid in pattern_by_id:
                    pr = pattern_by_id.pop(nid)
                    gr_len = len(gr.get("content", ""))
                    pr_len = len(pr.get("content", ""))
                    if pr_len > gr_len:
                        graph_results[i] = pr
                    else:
                        graph_results[i]["score"] = max(gr.get("score", 0), pr.get("score", 0))
            for pr in pattern_by_id.values():
                graph_results.append(pr)
            max_results = max(max_results, len(pattern_results) + 10)

        # Sort graph results by score so pattern results (score 1.5) rank
        # above keyword matches before RRF rank-based fusion.
        graph_results.sort(key=lambda r: r.get("score", 0), reverse=True)

        merged = self._merge_results_weighted(
            graph_results, vector_results, alpha,
            workspace_id=workspace_id,
        )

        # ── Stage 4: MCAL post-fusion guarantees ───────────────────────
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

        # ── Stage 5: HSI enrichment — EA_Register trust zone info ─────
        pre_enrich = len(merged)
        merged = self._enrich_hsi_registers(merged)
        if len(merged) > pre_enrich:
            merged.sort(key=lambda r: r.get("score", 0), reverse=True)
            max_results += (len(merged) - pre_enrich)

        total = len(merged)
        page = merged[offset:offset + max_results]

        # ── Build citations ────────────────────────────────────────────
        db = self._db_for_workspace(workspace_id)
        citations = {
            "source": "neo4j+qdrant" if alpha < 1.0 else "neo4j",
            "database": db,
            "profile": workspace_id,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "result_count": len(page),
            "nodes": [
                {
                    "node_id": r.get("node_id", ""),
                    "node_type": r.get("node_type", ""),
                    "name": r.get("properties", {}).get("name", "")
                           or r.get("properties", {}).get("function_name", ""),
                    "source": r.get("source", ""),
                    "score": round(r.get("score", 0), 4),
                }
                for r in page
            ],
        }
        if filter_by_module:
            citations["module_filter"] = filter_by_module
        if filter_by_node_type:
            citations["node_type_filter"] = filter_by_node_type

        result = {
            "results": page,
            "total_count": total,
            "has_more": (offset + max_results) < total,
            "search_strategy": "graph" if alpha == 1.0 else ("vector" if alpha == 0.0 else "hybrid"),
            "alpha": alpha,
            "named_entities": named_ents,
            "is_aggregation": is_agg,
            "citations": citations,
        }
        if enhanced and enhanced.enhanced:
            result["query_enhancement"] = {
                "intent": enhanced.intent,
                "expanded_keywords": enhanced.expanded_keywords,
                "target_labels": enhanced.target_labels,
                "cypher_patterns": enhanced.cypher_patterns,
                "cypher_patterns_executed": len(enhanced.cypher_patterns),
                "pattern_results": len(pattern_results),
                "enhancement_time_ms": round(enhanced.enhancement_time_ms, 1),
            }
        return result

    def _graph_search(
        self, query: str, max_results: int = 20,
        filter_by_module: Optional[str] = None,
        filter_by_node_type: Optional[List[str]] = None,
        workspace_id: str = "illd",
        include_relationships: bool = False,
        enhanced: Optional[McalEnhancedQuery] = None,
    ) -> List[Dict[str, Any]]:
        """Label-aware graph search with keyword extraction and rich serialization.

        Uses label-specific property maps from kg_node_utils to properly
        identify display names, unique IDs, and structured properties.
        When an EnhancedQuery is provided, uses its target_labels instead of
        infer_labels and filters out compound keywords to reduce noise.
        """
        db = self._db_for_workspace(workspace_id)
        keywords = extract_keywords(query)

        # When enhanced, use LLM-chosen labels (more focused) and drop compound keywords
        if enhanced and enhanced.enhanced and enhanced.target_labels and not filter_by_node_type:
            labels = enhanced.target_labels
            # Drop compound keywords (multi-word phrases) that cause noisy CONTAINS matches
            keywords = [kw for kw in keywords if ' ' not in kw]
            logger.debug("_graph_search: using enhanced labels=%s, keywords=%s", labels, keywords)
        else:
            labels = filter_by_node_type or infer_labels(query, profile=workspace_id)
        module = self._neo4j_module(filter_by_module or self.module) or None

        results: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        graph_errors = 0

        try:
            with self._neo4j.session(database=db) as session:
                # ── Consolidated query: single Cypher for all labels ────
                # Instead of N queries (one per label), run a single query
                # that matches any node whose labels overlap with the
                # inferred set. This reduces 50-200 queries to 1.
                try:
                    module_clause = (
                        "AND (n.module IS NULL OR n.module = $module) "
                        if module else ""
                    )
                    cypher = (
                        "UNWIND $keywords AS kw "
                        "MATCH (n) "
                        "WHERE any(lbl IN labels(n) WHERE lbl IN $labels) "
                        f"{module_clause}"
                        "AND ("
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
                        "labels": list(labels),
                        "limit": max_results * 2,  # Over-fetch for dedup
                    }
                    if module:
                        params["module"] = module

                    records = session.run(cypher, params)
                    for record in records:
                        node = record["n"]
                        props = dict(node.items())
                        node_type = record["node_labels"][0] if record["node_labels"] else labels[0]
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
                    logger.warning("Consolidated graph search failed: %s", exc)

                # ── Fulltext index fallback (if available) ────────────
                # If a fulltext index 'aice_search_idx' exists, use it
                # for a secondary pass to catch matches the CONTAINS
                # scan might miss (handles stemming, fuzzy matching).
                if len(results) < max_results:
                    try:
                        ft_cypher = (
                            "CALL db.index.fulltext.queryNodes("
                            "  'aice_search_idx', $query_text"
                            ") YIELD node, score "
                            "WHERE any(lbl IN labels(node) WHERE lbl IN $labels) "
                            "RETURN node, labels(node) AS node_labels, score "
                            "ORDER BY score DESC "
                            "LIMIT $limit"
                        )
                        ft_params = {
                            "query_text": " ".join(keywords),
                            "labels": list(labels),
                            "limit": max_results - len(results),
                        }
                        for record in session.run(ft_cypher, ft_params):
                            node = record["node"]
                            props = dict(node.items())
                            node_type = record["node_labels"][0] if record["node_labels"] else labels[0]
                            nid = node_unique_id(node_type, props)
                            if nid in seen_ids:
                                continue
                            seen_ids.add(nid)

                            desc = str(props.get("description", ""))
                            sc = score_node(kw, name, desc)

                            # Deprioritize navigational/structural labels that add noise
                            if node_type in _LOW_VALUE_LABELS:
                                sc *= 0.3

                            results.append({
                                "node_id": nid,
                                "node_type": node_type,
                                "source": "neo4j",
                                "score": sc,
                                "properties": props,
                                "content": serialize_node(node_type, props),
                            })
                    except Exception:
                        # Fulltext index may not exist — silently skip
                        pass

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
        """Legacy merge — kept for backward compatibility. Use _merge_results_weighted instead."""
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
                                    f"[SRC->SFR Cross-link] Function {func} accesses "
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
    # HSI enrichment — fetch EA_Register trust zone info for SFR results
    # ─────────────────────────────────────────────────────────────────────

    def _enrich_hsi_registers(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """When SFR_Register results are present, fetch corresponding EA_Register
        nodes (trust zone, APU, CPU mode) and merge trust-zone info directly
        into SFR_Register content so the LLM sees it in one place."""
        if not self._neo4j:
            return results

        # Collect SFR register names AND function names from results
        sfr_names: Set[str] = set()
        function_names: Set[str] = set()
        sfr_idx_map: Dict[str, int] = {}  # sfr_name -> index in results
        for idx, r in enumerate(results):
            ntype = r.get("node_type", "")
            if ntype == "SFR_Register":
                name = (r.get("properties") or {}).get("name", "")
                if name:
                    sfr_names.add(name)
                    sfr_idx_map[name] = idx
            elif ntype == "SRC_Function":
                name = (r.get("properties") or {}).get("name", "")
                if name:
                    function_names.add(name)

        if not sfr_names and not function_names:
            return results

        existing_ids = {r.get("node_id") for r in results}
        db = self._default_db

        try:
            with self._neo4j.session(database=db) as session:
                ea_by_sfr: Dict[str, Dict[str, Any]] = {}

                # Path 1: Direct SFR name match on EA_Register
                for sfr_name in sfr_names:
                    cypher = (
                        "MATCH (reg:EA_Register) "
                        "WHERE reg.sfr_id = $sfr_name OR reg.name = $short_name "
                        "RETURN reg LIMIT 1"
                    )
                    short_name = sfr_name.split("_", 1)[1] if "_" in sfr_name else sfr_name
                    records = list(session.run(cypher, {"sfr_name": sfr_name, "short_name": short_name}))
                    for rec in records:
                        node = rec["reg"]
                        if node is None:
                            continue
                        ea_by_sfr[sfr_name] = dict(node.items())

                # Path 2: EA_Function -> EA_ACCESSES_REGISTER -> EA_Register
                # (more reliable when sfr_id matching is fragile)
                for fn_name in function_names:
                    cypher = (
                        "MATCH (f:EA_Function {name: $fn_name})"
                        "-[r:EA_ACCESSES_REGISTER]->(reg:EA_Register) "
                        "RETURN reg, r.access_type AS access_type LIMIT 50"
                    )
                    records = list(session.run(cypher, {"fn_name": fn_name}))
                    for rec in records:
                        node = rec["reg"]
                        if node is None:
                            continue
                        props = dict(node.items())
                        sfr_id = props.get("sfr_id", "")
                        reg_name = props.get("name", "")
                        # Match EA_Register to any SFR name we have
                        matched_sfr = None
                        for sn in sfr_names:
                            if sfr_id == sn or reg_name in sn or sn.endswith(reg_name):
                                matched_sfr = sn
                                break
                        if matched_sfr and matched_sfr not in ea_by_sfr:
                            props["_ea_access_type"] = rec.get("access_type", "")
                            ea_by_sfr[matched_sfr] = props

                # Merge EA trust zone info INTO SFR_Register content
                for sfr_name, ea_props in ea_by_sfr.items():
                    trust_lines = []
                    read_apu = ea_props.get("read_apu", "")
                    write_apu = ea_props.get("write_apu", "")
                    read_cpu = ea_props.get("read_cpu_mode", "")
                    write_cpu = ea_props.get("write_cpu_mode", "")
                    ea_access = ea_props.get("access_type", ea_props.get("_ea_access_type", ""))
                    if read_apu:
                        trust_lines.append(f"  Read APU: {read_apu}")
                    if write_apu:
                        trust_lines.append(f"  Write APU: {write_apu}")
                    if read_cpu:
                        trust_lines.append(f"  Read CPU Mode: {read_cpu}")
                    if write_cpu:
                        trust_lines.append(f"  Write CPU Mode: {write_cpu}")
                    if ea_access:
                        trust_lines.append(f"  EA Access Type: {ea_access}")

                    # Inject into the SFR_Register result content
                    if trust_lines and sfr_name in sfr_idx_map:
                        idx = sfr_idx_map[sfr_name]
                        old_content = results[idx].get("content", "")
                        trust_block = "\n  --- Trust Zone (EA_Register) ---\n" + "\n".join(trust_lines)
                        results[idx]["content"] = old_content + trust_block
                        # Also add to properties for structured access
                        props = results[idx].get("properties", {})
                        if read_apu:
                            props["read_apu"] = read_apu
                        if write_apu:
                            props["write_apu"] = write_apu
                        if read_cpu:
                            props["read_cpu_mode"] = read_cpu
                        if write_cpu:
                            props["write_cpu_mode"] = write_cpu

                    # Still add EA_Register as separate result for completeness
                    ntype = "EA_Register"
                    nid = node_unique_id(ntype, ea_props)
                    if nid not in existing_ids:
                        display = node_display_name(ntype, ea_props)
                        ea_props["_label"] = ntype
                        ea_props["_node_id"] = nid
                        ea_props["_name"] = display
                        content = serialize_node(ntype, ea_props)
                        results.append({
                            "node_id": nid,
                            "node_type": ntype,
                            "source": "neo4j_hsi",
                            "score": 1.4,
                            "properties": ea_props,
                            "content": content,
                        })
                        existing_ids.add(nid)
        except Exception as exc:
            logger.debug("HSI register enrichment failed: %s", exc)

        return results

    # ─────────────────────────────────────────────────────────────────────
    # Dedicated HSI extraction — SWUD-format register + global data
    # ─────────────────────────────────────────────────────────────────────

    def get_function_hsi(
        self,
        function_name: str,
        module: Optional[str] = None,
        workspace_id: str = "mcal",
    ) -> Dict[str, Any]:
        """Extract structured HSI data for a function — matching SWUD format.

        Returns registers accessed (with access type, trust zone) and
        global/shared variables used (with access type, via_chain).
        """
        _qs_t0 = time.perf_counter()
        if not self._neo4j:
            return {"error": "Neo4j not available"}

        db = self._default_db
        hsi: Dict[str, Any] = {
            "function_name": function_name,
            "module": module or self.module or "",
            "registers": [],
            "global_variables": [],
            "events": [],
        }

        try:
            with self._neo4j.session(database=db) as session:
                # 1. SRC_Function metadata (register_accesses, registers_written/read)
                fn_cypher = (
                    "MATCH (f:SRC_Function {name: $fn_name}) "
                    "RETURN f.register_accesses AS register_accesses, "
                    "f.registers_written AS registers_written, "
                    "f.registers_read AS registers_read, "
                    "f.register_access_count AS register_access_count, "
                    "f.parameters AS parameters, "
                    "f.return_type AS return_type "
                    "LIMIT 1"
                )
                fn_recs = list(session.run(fn_cypher, {"fn_name": function_name}))
                fn_meta = {}
                if fn_recs:
                    fn_meta = {
                        "register_accesses": fn_recs[0].get("register_accesses"),
                        "registers_written": fn_recs[0].get("registers_written"),
                        "registers_read": fn_recs[0].get("registers_read"),
                        "register_access_count": fn_recs[0].get("register_access_count"),
                        "parameters": fn_recs[0].get("parameters"),
                        "return_type": fn_recs[0].get("return_type"),
                    }
                hsi["function_metadata"] = fn_meta

                # Parse register_accesses JSON if available
                reg_accesses_raw = fn_meta.get("register_accesses")
                parsed_accesses = []
                if reg_accesses_raw:
                    try:
                        if isinstance(reg_accesses_raw, str):
                            parsed_accesses = json.loads(reg_accesses_raw)
                        elif isinstance(reg_accesses_raw, list):
                            parsed_accesses = reg_accesses_raw
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 2. SRC_ACCESSES_SFR relationships
                sfr_cypher = (
                    "MATCH (f:SRC_Function {name: $fn_name})"
                    "-[r:SRC_ACCESSES_SFR]->(reg:SFR_Register) "
                    "RETURN reg.name AS reg_name, reg.description AS description, "
                    "reg.module AS module, reg.device AS device, "
                    "reg.struct_name AS struct_name, "
                    "r.access_type AS access_type, r.line AS line, "
                    "r.field AS field, r.access_context AS access_context "
                    "ORDER BY reg.name"
                )
                sfr_recs = list(session.run(sfr_cypher, {"fn_name": function_name}))
                seen_regs: Set[str] = set()
                reg_map: dict = {}  # rname -> entry (deduplicate across device variants)
                for rec in sfr_recs:
                    rname = rec.get("reg_name") or ""
                    access = (rec.get("access_type") or "").upper()
                    # Enrich from parsed_accesses
                    if not access and parsed_accesses:
                        for pa in parsed_accesses:
                            if pa.get("register") == rname:
                                access = (pa.get("access_type") or "").upper()
                                break
                    # Deduplicate: keep first entry per register, merge devices
                    if rname in reg_map:
                        existing = reg_map[rname]
                        dev = rec.get("device", "")
                        if dev and dev not in existing.get("device", ""):
                            existing["device"] += f", {dev}"
                        # Merge field if first was None
                        if not existing.get("field") and rec.get("field"):
                            existing["field"] = rec.get("field")
                        # Merge line if first was None
                        if existing.get("line") is None and rec.get("line") is not None:
                            existing["line"] = rec.get("line")
                        continue
                    entry = {
                        "register": rname,
                        "access_type": access or "UNKNOWN",
                        "description": rec.get("description", ""),
                        "module": rec.get("module", ""),
                        "device": rec.get("device", ""),
                        "field": rec.get("field", ""),
                        "line": rec.get("line"),
                        "trust_zone": "",
                    }
                    reg_map[rname] = entry
                    seen_regs.add(rname)
                    hsi["registers"].append(entry)

                # If no SRC_ACCESSES_SFR, fall back to parsed register_accesses
                if not hsi["registers"] and parsed_accesses:
                    for pa in parsed_accesses:
                        rname = pa.get("register", "")
                        if rname and rname not in seen_regs:
                            hsi["registers"].append({
                                "register": rname,
                                "access_type": (pa.get("access_type") or "UNKNOWN").upper(),
                                "description": "",
                                "module": module or "",
                                "device": "",
                                "field": pa.get("field", ""),
                                "line": pa.get("line"),
                                "trust_zone": "",
                            })
                            seen_regs.add(rname)

                # Also check registers_written/registers_read properties
                rw_raw = fn_meta.get("registers_written") or []
                if isinstance(rw_raw, str):
                    try:
                        rw_raw = json.loads(rw_raw)
                    except (json.JSONDecodeError, TypeError):
                        rw_raw = []
                for rw_name in rw_raw:
                    if isinstance(rw_name, str) and rw_name not in seen_regs:
                        hsi["registers"].append({
                            "register": rw_name,
                            "access_type": "WRITE",
                            "description": "", "module": module or "",
                            "device": "", "field": "", "line": None,
                            "trust_zone": "",
                        })
                        seen_regs.add(rw_name)
                rr_raw = fn_meta.get("registers_read") or []
                if isinstance(rr_raw, str):
                    try:
                        rr_raw = json.loads(rr_raw)
                    except (json.JSONDecodeError, TypeError):
                        rr_raw = []
                for rr_name in rr_raw:
                    if isinstance(rr_name, str) and rr_name not in seen_regs:
                        hsi["registers"].append({
                            "register": rr_name,
                            "access_type": "READ",
                            "description": "", "module": module or "",
                            "device": "", "field": "", "line": None,
                            "trust_zone": "",
                        })
                        seen_regs.add(rr_name)

                # 3. EA_Register trust zone enrichment for each register
                for entry in hsi["registers"]:
                    rname = entry["register"]
                    ea_cypher = (
                        "MATCH (reg:EA_Register) "
                        "WHERE reg.sfr_id = $sfr_name OR reg.name = $short_name "
                        "RETURN reg.read_apu AS read_apu, reg.write_apu AS write_apu, "
                        "reg.read_cpu_mode AS read_cpu, reg.write_cpu_mode AS write_cpu "
                        "LIMIT 1"
                    )
                    short_name = rname.split("_", 1)[1] if "_" in rname else rname
                    ea_recs = list(session.run(ea_cypher, {"sfr_name": rname, "short_name": short_name}))
                    if ea_recs:
                        read_apu = ea_recs[0].get("read_apu", "")
                        write_apu = ea_recs[0].get("write_apu", "")
                        # Determine trust zone from APU
                        if read_apu or write_apu:
                            apu_vals = [v for v in [read_apu, write_apu] if v]
                            # PTOP = Untrusted, PCPU = Trusted
                            if any("PTOP" in str(v).upper() for v in apu_vals):
                                entry["trust_zone"] = "Untrusted"
                            elif any("PCPU" in str(v).upper() for v in apu_vals):
                                entry["trust_zone"] = "Trusted"
                            else:
                                entry["trust_zone"] = f"APU: {', '.join(apu_vals)}"
                            entry["read_apu"] = read_apu
                            entry["write_apu"] = write_apu
                            entry["read_cpu_mode"] = ea_recs[0].get("read_cpu", "")
                            entry["write_cpu_mode"] = ea_recs[0].get("write_cpu", "")

                # Also try EA_Function -> EA_ACCESSES_REGISTER path
                ea_fn_cypher = (
                    "MATCH (f:EA_Function {name: $fn_name})"
                    "-[r:EA_ACCESSES_REGISTER]->(reg:EA_Register) "
                    "RETURN reg.name AS name, reg.sfr_id AS sfr_id, "
                    "reg.read_apu AS read_apu, reg.write_apu AS write_apu, "
                    "reg.read_cpu_mode AS read_cpu, reg.write_cpu_mode AS write_cpu, "
                    "r.access_type AS access_type "
                    "LIMIT 50"
                )
                ea_fn_recs = list(session.run(ea_fn_cypher, {"fn_name": function_name}))
                for rec in ea_fn_recs:
                    sfr_id = rec.get("sfr_id", "")
                    ea_name = rec.get("name", "")
                    # Try to match to existing register entries
                    matched = False
                    for entry in hsi["registers"]:
                        rname = entry["register"]
                        if sfr_id == rname or ea_name in rname or rname.endswith(ea_name):
                            if not entry.get("trust_zone"):
                                read_apu = rec.get("read_apu", "")
                                write_apu = rec.get("write_apu", "")
                                if read_apu or write_apu:
                                    apu_vals = [v for v in [read_apu, write_apu] if v]
                                    if any("PTOP" in str(v).upper() for v in apu_vals):
                                        entry["trust_zone"] = "Untrusted"
                                    elif any("PCPU" in str(v).upper() for v in apu_vals):
                                        entry["trust_zone"] = "Trusted"
                                    else:
                                        entry["trust_zone"] = f"APU: {', '.join(apu_vals)}"
                                    entry["read_apu"] = read_apu
                                    entry["write_apu"] = write_apu
                            matched = True
                            break
                    if not matched and (sfr_id or ea_name):
                        # New register not in SRC_ACCESSES_SFR
                        read_apu = rec.get("read_apu", "")
                        write_apu = rec.get("write_apu", "")
                        tz = ""
                        if read_apu or write_apu:
                            apu_vals = [v for v in [read_apu, write_apu] if v]
                            if any("PTOP" in str(v).upper() for v in apu_vals):
                                tz = "Untrusted"
                            elif any("PCPU" in str(v).upper() for v in apu_vals):
                                tz = "Trusted"
                            else:
                                tz = f"APU: {', '.join(apu_vals)}"
                        hsi["registers"].append({
                            "register": sfr_id or ea_name,
                            "access_type": (rec.get("access_type") or "UNKNOWN").upper(),
                            "description": "", "module": module or "",
                            "device": "", "field": "", "line": None,
                            "trust_zone": tz,
                            "read_apu": read_apu,
                            "write_apu": write_apu,
                            "source": "EA_Register",
                        })

                # 4. Global variables via SRC_USES_GLOBAL
                glob_cypher = (
                    "MATCH (f:SRC_Function {name: $fn_name})"
                    "-[r:SRC_USES_GLOBAL]->(g:SRC_GlobalVariable) "
                    "RETURN g.name AS name, g.data_type AS data_type, "
                    "g.is_const AS is_const, g.is_extern AS is_extern, "
                    "g.memory_section AS memory_section, "
                    "g.description AS description, "
                    "r.access_type AS access_type, "
                    "r.via_chain AS via_chain, "
                    "r.access_context AS access_context "
                    "ORDER BY g.name"
                )
                glob_recs = list(session.run(glob_cypher, {"fn_name": function_name}))
                # Filter out noise: boolean constants, non-module globals
                _GLOBAL_NOISE = {"TRUE", "FALSE", "NULL", "STD_ON", "STD_OFF", "E_OK", "E_NOT_OK"}
                mod_prefix = module.lower() if module else ""
                for rec in glob_recs:
                    gname = rec.get("name", "")
                    # Skip boolean/standard constants
                    if gname.upper() in _GLOBAL_NOISE:
                        continue
                    # Skip globals that belong to other modules (e.g. Dma_k*, Gtm_k*, Mcu_k*)
                    if mod_prefix and "_" in gname:
                        gname_lower = gname.lower()
                        # If global starts with a known module prefix that isn't ours, skip
                        if (gname_lower.startswith(("dma_", "gtm_", "mcu_", "spi_", "can_", "port_", "dio_", "icu_", "pwm_", "gpt_", "wdg_", "fee_", "fls_"))
                            and not gname_lower.startswith(mod_prefix + "_") and not gname_lower.startswith(mod_prefix + "k")):
                            continue
                    access = (rec.get("access_type") or "").upper()
                    # Normalize READ_WRITE to separate
                    if access == "READ_WRITE":
                        access = "READ, WRITE"
                    hsi["global_variables"].append({
                        "variable": rec.get("name", ""),
                        "access_type": access or "UNKNOWN",
                        "data_type": rec.get("data_type", ""),
                        "is_const": rec.get("is_const", False),
                        "is_extern": rec.get("is_extern", False),
                        "via_chain": rec.get("via_chain", ""),
                        "memory_section": rec.get("memory_section", ""),
                        "description": rec.get("description", ""),
                    })

        except Exception as exc:
            logger.warning("HSI extraction failed for %s: %s", function_name, exc)
            hsi["error"] = str(exc)

        # Build summary text matching SWUD HSI section format
        lines = [
            f"=== HSI (Hardware-Software Interface) for {function_name} ===",
            "",
        ]

        if hsi["registers"]:
            lines.append("## SFR Registers Accessed")
            lines.append("| Register | Access | Field | Line | Trust Zone | Device |")
            lines.append("|----------|--------|-------|------|------------|--------|")
            for r in hsi["registers"]:
                access_str = r["access_type"]
                suffix = f"({access_str[0].lower()})" if access_str and access_str != "UNKNOWN" else ""
                tz = r.get("trust_zone", "")
                lines.append(
                    f"| {r['register']}{suffix} | {access_str} | "
                    f"{r.get('field', '')} | {r.get('line', '')} | "
                    f"{tz} | {r.get('device', '')} |"
                )
        else:
            lines.append("## SFR Registers Accessed\nNone found in graph.")

        lines.append("")

        if hsi["global_variables"]:
            lines.append("## Global/Shared Variables")
            lines.append("| Variable | Access | Data Type | Via Chain | Const | Extern |")
            lines.append("|----------|--------|-----------|-----------|-------|--------|")
            for g in hsi["global_variables"]:
                lines.append(
                    f"| {g['variable']} | {g['access_type']} | "
                    f"{g.get('data_type', '')} | {g.get('via_chain', '')} | "
                    f"{g.get('is_const', '')} | {g.get('is_extern', '')} |"
                )
        else:
            lines.append("## Global/Shared Variables\nNone found in graph.")

        lines.append("")
        lines.append("## Events")
        lines.append("None" if not hsi["events"] else "\n".join(str(e) for e in hsi["events"]))

        hsi["summary_text"] = "\n".join(lines)
        _log_query(
            method="get_function_hsi", cypher=f"hsi:{function_name}",
            elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=len(hsi.get("registers", [])) + len(hsi.get("global_variables", [])),
            module=module or self.module, profile=workspace_id,
        )
        return hsi

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
                    f"properties(r) AS r_props, "
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
                    line = f"  [{rel_type}] -> {target_label}: {n_name}"
                    if n_desc:
                        line += f" — {n_desc}"
                    # Include relationship properties (access_type, via_chain, etc.)
                    rel_props = dict(rec["r_props"]) if rec.get("r_props") else {}
                    for rk in ("access_type", "access_context", "via_chain"):
                        rv = rel_props.get(rk, "")
                        if rv:
                            line += f"\n    {rk}: {rv}"
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
                lines = [f"  [{rel_type}] -> {target_label}: {n_name}"]
                for prop_key, display_label in display_props:
                    val = neighbor_props.get(prop_key, "")
                    if val:
                        val_str = str(val)
                        if len(val_str) > 400:
                            val_str = val_str[:400] + "…"
                        lines.append(f"    {display_label}: {val_str}")
                sections.setdefault(rel_type, []).append("\n".join(lines))
            else:
                line = f"  [{rel_type}] -> {target_label}: {n_name}"
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
                # Also include the source code collection: mcal_{module}_sourcecode
                src_coll = f"mcal_{mod}_sourcecode"
                if src_coll not in colls:
                    colls.append(src_coll)
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
        # Match both {module}_* (e.g. adc_swa_architecture) and mcal_{module}_sourcecode
        return [n for n in names if n.startswith(f"{module}_") or n == f"mcal_{module}_sourcecode"]

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
            # Also includes mcal_{module}_sourcecode collections
            mcal_prefixes = ("_swa_", "_swud_", "_testspec_", "_jama_")
            doc_colls = [n for n in names if any(p in n for p in mcal_prefixes)]
            src_colls = [n for n in names if n.startswith("mcal_") and n.endswith("_sourcecode")]
            return doc_colls + src_colls
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
        "sourcecode": "SRC_Function",
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
        # MCAL sourcecode: mcal_{module}_sourcecode e.g. "mcal_spi_sourcecode"
        if collection.startswith("mcal_") and collection.endswith("_sourcecode"):
            return cls._COLLECTION_NODE_TYPE_MAP.get("sourcecode", "SRC_Function")
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

    def _merge_results_weighted(
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
        _qs_t0 = time.perf_counter()
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
                # Resolve LP-variant aliases for module filter (LPBTM→BTM, LPCAN→CAN)
                if k == "module" and isinstance(v, str):
                    v = self._neo4j_module(v)
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
                        props = dict(rec["n"].items())
                        props["_label"] = label
                        props["_node_id"] = node_unique_id(label, props)
                        props["_name"] = node_display_name(label, props)
                        nodes.append(props)
        except Exception as e:
            logger.error("search_nodes failed: %s", e)

        # ── Build citations ────────────────────────────────────────────
        citations = {
            "source": "neo4j",
            "database": db,
            "profile": workspace_id,
            "cypher": cypher,
            "label": label,
            "node_count": len(nodes),
            "nodes": [
                {"node_id": n.get("_node_id", ""), "name": n.get("_name", ""), "label": label}
                for n in nodes if n.get("_node_id")
            ],
        }
        if keyword:
            citations["keyword"] = keyword
        if filters:
            citations["filters"] = filters

        _result = {"nodes": nodes, "total_count": len(nodes), "has_more": len(nodes) == limit,
                    "citations": citations}
        _log_query(
            method="search_nodes", cypher=cypher,
            params=params, elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=len(nodes), module=self.module, profile=workspace_id,
        )
        return _result

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
        _qs_t0 = time.perf_counter()
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
                    labels = rec["lbl"]
                    props["_labels"] = labels
                    primary_label = labels[0] if labels else ""
                    props["_label"] = primary_label
                    props["_node_id"] = node_unique_id(primary_label, props)
                    props["_name"] = node_display_name(primary_label, props)
                    _log_query(
                        method="get_node_by_id", cypher=cypher,
                        params=params, elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
                        row_count=1, module=self.module, profile=workspace_id,
                    )
                    return {"node": props, "found": True}
        except Exception as e:
            logger.error("get_node_by_id failed: %s", e)

        _log_query(
            method="get_node_by_id", cypher=cypher,
            params=params, elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=0, module=self.module, profile=workspace_id,
        )
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
        _qs_t0 = time.perf_counter()
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
                    ntype = rec["neighbor_type"] or ""
                    neighbors.append({
                        "relationship": rec["rel_type"],
                        "node_type": ntype,
                        "properties": props,
                        "node_id": node_unique_id(ntype, props),
                        "_name": node_display_name(ntype, props),
                    })
        except Exception as e:
            logger.error("get_neighbors failed: %s", e)

        _result = {"neighbors": neighbors, "total_count": len(neighbors), "has_more": len(neighbors) == limit}
        _log_query(
            method="get_neighbors", cypher=cypher,
            params=params, elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=len(neighbors), module=self.module, profile=workspace_id,
        )
        return _result

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
        _qs_t0 = time.perf_counter()
        if not self._neo4j:
            return {"rows": [], "error": "Neo4j not available"}

        db = self._db_for_workspace(workspace_id)
        params = parameters or {}

        # Resolve LP-variant aliases in raw Cypher: rewrite literal module strings
        # and any parameter value that equals an aliased module name.
        # Handles both WHERE n.module = 'LPBTM' and {module: 'LPBTM'} forms.
        _alias_pattern = re.compile(
            r"(module\s*[:=]\s*['\"])(" + "|".join(_NEO4J_MODULE_ALIASES) + r")(['\"])",
            re.IGNORECASE,
        )
        query = _alias_pattern.sub(
            lambda m: m.group(1) + _NEO4J_MODULE_ALIASES[m.group(2).upper()] + m.group(3),
            query,
        )
        params = {
            k: _NEO4J_MODULE_ALIASES.get(v.upper(), v) if isinstance(v, str) else v
            for k, v in params.items()
        }

        rows = []
        try:
            with self._neo4j.session(database=db) as session:
                result = session.run(query, params)
                for record in result:
                    row = {}
                    for key in record.keys():
                        val = record[key]
                        # Convert Neo4j Node to dict + inject citation metadata
                        if hasattr(val, "labels") and hasattr(val, "items"):
                            props = dict(val.items())
                            lbl = list(val.labels)[0] if val.labels else ""
                            props["_label"] = lbl
                            props["_node_id"] = node_unique_id(lbl, props)
                            props["_name"] = node_display_name(lbl, props)
                            row[key] = props
                        elif hasattr(val, "items"):
                            row[key] = dict(val.items())
                        else:
                            row[key] = val
                    rows.append(row)
        except Exception as e:
            logger.error("execute_cypher failed: %s", e)
            return {"rows": [], "error": str(e)}

        _log_query(
            method="execute_cypher", cypher=query,
            params=params, elapsed_ms=(time.perf_counter() - _qs_t0) * 1000,
            row_count=len(rows), module=self.module, profile=workspace_id,
        )

        # ── Build citations ────────────────────────────────────────────
        citations = _build_cypher_citations(
            query, rows, workspace_id, db,
        )

        return {"rows": rows, "total_count": len(rows), "citations": citations}

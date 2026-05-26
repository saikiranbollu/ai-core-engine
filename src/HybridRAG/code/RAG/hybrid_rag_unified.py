"""
Unified Hybrid RAG Orchestrator — Profile-Agnostic
====================================================
Combines Knowledge-Graph search (Neo4j) with vector-store search
(Qdrant) for **any** ontology profile (mcal, illd, or future).

Replaces ``ILLDHybridRAGOrchestrator`` and provides the same interface
the MCP server expects.

Uses:
- ``UnifiedRAGQuerier`` for vector search (profile auto-detected)
- ``UnifiedKGQuerier`` for graph search (profile auto-detected)

Usage::

    from HybridRAG.code.RAG.hybrid_rag_unified import HybridRAGOrchestrator

    orch = HybridRAGOrchestrator(module="CXPI")        # active profile
    orch = HybridRAGOrchestrator(module="ADC", profile="mcal")

    result = orch.query("How does Adc_Init configure the hardware?", top_k=5)
    for src in result.sources:
        print(src.origin, src.score, src.heading)
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent          # .../RAG
_CODE_DIR = _SCRIPT_DIR.parent                         # .../HybridRAG/code
_HYBRIDRAG_DIR = _CODE_DIR.parent                      # .../HybridRAG
_CONFIG_DIR = _HYBRIDRAG_DIR / "config"

for p in (_SCRIPT_DIR, _CODE_DIR, _CODE_DIR / "KG"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Import shared helpers ported from the MCAL graphrag_query.py branch
try:
    from querier.kg_node_utils import (
        extract_named_entities, is_aggregation_query,
        extract_keywords, infer_labels, score_node,
        node_display_name, node_unique_id, serialize_node,
        normalise_scores, LABEL_NAME_PROPS, LABEL_DISPLAY_PROPS,
    )
    _HAS_KG_UTILS = True
except ImportError:
    _HAS_KG_UTILS = False


def _active_profile() -> str:
    try:
        from env_config import load_yaml_with_env
        cfg = load_yaml_with_env(_CONFIG_DIR / "storage_config.yaml")
    except ImportError:
        with open(_CONFIG_DIR / "storage_config.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    return cfg.get("active_instance", "illd")


def _config_default_alpha() -> float:
    """Return the default RRF blend factor from storage_config.yaml."""
    try:
        from env_config import get_default_search_alpha
        return get_default_search_alpha()
    except Exception:
        pass
    try:
        from env_config import load_yaml_with_env
        cfg = load_yaml_with_env(_CONFIG_DIR / "storage_config.yaml")
    except ImportError:
        with open(_CONFIG_DIR / "storage_config.yaml", "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    return float(cfg.get("hybrid_search", {}).get("default_alpha", 0.6))


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class HybridSource:
    origin: str = ""              # "vector" | "graph" | "hybrid"
    score: float = 0.0
    heading: str = ""
    text: str = ""
    collection: Optional[str] = None
    node_label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HybridResult:
    query: str = ""
    profile: str = ""
    sources: List[HybridSource] = field(default_factory=list)
    context: str = ""
    graph_traceability: List[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    alpha: float = field(default_factory=_config_default_alpha)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class HybridRAGOrchestrator:
    """
    Profile-agnostic hybrid search combining KG + RAG.

    Parameters
    ----------
    profile : str, optional
        Ontology profile. Defaults to ``active_instance`` from config.
    module : str
        Module name (e.g. ``"ADC"``, ``"CXPI"``).
    alpha : float
        Blending weight: ``0.0`` = pure vector, ``1.0`` = pure graph.
    neo4j_enabled : bool
        If False, skip graph search entirely.
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        module: str = "ADC",
        alpha: Optional[float] = None,
        neo4j_enabled: bool = True,
    ):
        self.profile = profile or _active_profile()
        self.module = module.upper()
        self.alpha = alpha if alpha is not None else _config_default_alpha()
        self.neo4j_enabled = neo4j_enabled

        self._rag_querier = None
        self._kg_querier = None

    # ── lazy init ─────────────────────────────────────────────────────────

    def _get_rag_querier(self):
        if self._rag_querier is None:
            from RAG.rag_query_unified import UnifiedRAGQuerier
            self._rag_querier = UnifiedRAGQuerier(
                module=self.module, profile=self.profile,
            )
        return self._rag_querier

    def _get_kg_querier(self):
        if self._kg_querier is None:
            try:
                from KG.kg_query import UnifiedKGQuerier
                self._kg_querier = UnifiedKGQuerier(
                    module=self.module, profile=self.profile,
                )
            except Exception as exc:
                logger.warning("KG querier init failed: %s", exc)
                self.neo4j_enabled = False
        return self._kg_querier

    # ── main query ────────────────────────────────────────────────────────

    def query(
        self,
        question: str,
        top_k: int = 5,
        alpha: Optional[float] = None,
        metadata_filter: Optional[dict] = None,
        include_traceability: bool = True,
    ) -> HybridResult:
        """
        Execute a full hybrid search pipeline (ported from MCAL graphrag_query).

        Steps:
          0. Query analysis (aggregation detection, named entity extraction)
          1. Vector search (Qdrant)
          2. Graph search (Neo4j) — keyword + label-aware
          2a. Entity-targeted KG lookup (must-include guarantee)
          2b. Aggregation Cypher queries (for enumeration questions)
          3. Score fusion (normalise + weighted alpha blending)
          4. Must-include guarantee + graph representation guarantee
          5. Context assembly
        """
        t0 = time.time()
        blend = alpha if alpha is not None else self.alpha

        logger.info(
            "Hybrid query  |  profile=%s  |  alpha=%.2f  |  module=%s  |  neo4j=%s",
            self.profile, blend, self.module, self.neo4j_enabled,
        )

        # ── Stage 0: Query analysis ──────────────────────────────────
        is_agg = False
        named_ents: List[str] = []
        must_include_headings: set = set()

        if _HAS_KG_UTILS:
            is_agg = is_aggregation_query(question)
            named_ents = extract_named_entities(question, module=self.module)
            if named_ents:
                logger.info("  Named entities: %s", named_ents)
            if is_agg:
                logger.info("  Aggregation query detected — expanding top_k")
                top_k = max(top_k, 100)

        # ── Stage 1: Vector search ───────────────────────────────────
        vector_sources = self._vector_search(question, top_k=top_k * 2, where=metadata_filter)

        # ── Stage 2: Graph search ────────────────────────────────────
        graph_sources: List[HybridSource] = []
        traceability: List[dict] = []

        if self.neo4j_enabled and blend > 0.0:
            graph_sources = self._graph_search(question, top_k=top_k * 2)

            # ── Stage 2a: Entity-targeted KG lookup ──────────────────
            if named_ents and _HAS_KG_UTILS:
                entity_sources = self._entity_targeted_lookup(named_ents)
                existing_headings = {s.heading for s in graph_sources}
                for es in entity_sources:
                    if es.heading not in existing_headings:
                        graph_sources.append(es)
                        existing_headings.add(es.heading)
                    must_include_headings.add(es.heading.lower().strip()[:120])
                logger.info("  Entity lookup: added %d targeted results",
                            len(entity_sources))

            # ── Stage 2b: Aggregation Cypher queries ─────────────────
            if is_agg and _HAS_KG_UTILS:
                agg_sources = self._aggregation_search(question, named_ents)
                existing_headings = {s.heading for s in graph_sources}
                for ag in agg_sources:
                    if ag.heading not in existing_headings:
                        graph_sources.append(ag)
                        existing_headings.add(ag.heading)
                    must_include_headings.add(ag.heading.lower().strip()[:120])
                logger.info("  Aggregation search: added %d results",
                            len(agg_sources))

            if include_traceability and graph_sources:
                traceability = self._fetch_traceability(graph_sources[:5])

        # ── Stage 3: Score fusion ────────────────────────────────────
        if self.profile == "mcal":
            fused = self._fuse_scores_mcal(vector_sources, graph_sources, blend)
        else:
            fused = self._fuse_scores_illd(vector_sources, graph_sources, blend)
        fused.sort(key=lambda s: s.score, reverse=True)

        # ── Stage 4: Must-include + graph guarantee (MCAL only) ──────
        # ILLD uses simple top-k truncation (original behavior).
        if self.profile == "mcal":
            # Entity-targeted and aggregation results MUST appear in final
            # selection regardless of their fused score.
            if must_include_headings:
                must_include = [
                    s for s in fused
                    if s.heading.lower().strip()[:120] in must_include_headings
                ]
                optional = [
                    s for s in fused
                    if s.heading.lower().strip()[:120] not in must_include_headings
                ]
                remaining_budget = max(top_k, len(must_include))
                selected = list(must_include)
                optional.sort(key=lambda s: s.score, reverse=True)
                for opt in optional:
                    if len(selected) >= remaining_budget:
                        break
                    selected.append(opt)

                logger.info("  Must-include sources: %d (of %d total)",
                            len(must_include), len(fused))
            else:
                selected = fused[:top_k]

            # Graph representation guarantee — ensure minimum graph results
            min_graph = max(5, len(named_ents) * 2)
            graph_in_selected = [s for s in selected if s.origin == "graph"]
            if len(graph_in_selected) < min_graph:
                selected_headings = {s.heading for s in selected}
                extra_graph = [
                    s for s in fused
                    if s.origin == "graph" and s.heading not in selected_headings
                ]
                extra_graph.sort(key=lambda s: s.score, reverse=True)
                needed = min_graph - len(graph_in_selected)
                for gs in extra_graph[:needed]:
                    for idx in range(len(selected) - 1, -1, -1):
                        if (selected[idx].origin != "graph"
                                and not selected[idx].metadata.get("_must_include")):
                            selected[idx] = gs
                            break
                selected.sort(key=lambda s: s.score, reverse=True)

            fused = selected
        else:
            # ILLD: simple top-k truncation (original behavior)
            fused = fused[:top_k]

        logger.info("Fusion: %d vector + %d graph -> %d fused sources",
                     len(vector_sources), len(graph_sources), len(fused))

        # ── Stage 5: Context assembly ────────────────────────────────
        context = self._assemble_context(question, fused)

        elapsed = time.time() - t0
        return HybridResult(
            query=question,
            profile=self.profile,
            sources=fused,
            context=context,
            graph_traceability=traceability,
            elapsed_seconds=elapsed,
            alpha=blend,
        )

    # ── vector search stage ──────────────────────────────────────────────

    def _vector_search(
        self, question: str, top_k: int, where: Optional[dict] = None,
    ) -> List[HybridSource]:
        try:
            rag = self._get_rag_querier()
            results = rag.search(question, top_k=top_k, where=where)
            return [
                HybridSource(
                    origin="vector",
                    score=r.score,
                    heading=r.heading,
                    text=r.text,
                    collection=r.collection,
                    node_label=r.doc_type,
                    metadata=r.metadata,
                )
                for r in results
            ]
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return []

    # ── graph search stage ───────────────────────────────────────────────

    def _graph_search(self, question: str, top_k: int) -> List[HybridSource]:
        """Label-aware graph search with keyword extraction and rich serialization.

        Ported from the MCAL graphrag_query.py branch: uses label-specific
        property maps, keyword scoring, and 1-hop neighbor expansion.
        """
        try:
            kg = self._get_kg_querier()
            if kg is None:
                return []

            if _HAS_KG_UTILS:
                return self._graph_search_rich(kg, question, top_k)

            # Fallback: simple keyword search
            results = kg.keyword_search(question, limit=top_k)
            return [
                HybridSource(
                    origin="graph",
                    score=r.get("score", 0.5),
                    heading=r.get("name") or r.get("function_name", ""),
                    text=r.get("description") or r.get("brief", ""),
                    node_label=r.get("label"),
                    metadata=r,
                )
                for r in results
            ]
        except Exception as exc:
            logger.warning("Graph search failed: %s", exc)
            return []

    def _graph_search_rich(
        self, kg, question: str, top_k: int,
    ) -> List[HybridSource]:
        """Full label-aware graph search (requires kg_node_utils)."""
        keywords = extract_keywords(question)
        labels = infer_labels(question, profile=self.profile)
        sources: List[HybridSource] = []
        seen_ids: set = set()
        graph_errors = 0

        for label in labels:
            for kw in keywords:
                try:
                    nodes = kg.search_nodes(
                        label=label, keyword=kw,
                        filters={"module": self.module}, limit=top_k,
                    )
                except Exception as exc:
                    graph_errors += 1
                    logger.warning("Graph search failed for %s/%s: %s",
                                   label, kw, exc)
                    if graph_errors >= 3:
                        logger.warning("Multiple graph failures — resetting KG")
                        self._kg_querier = None
                        kg = self._get_kg_querier()
                        if kg is None:
                            return sources
                        graph_errors = 0
                    continue

                for node in nodes:
                    props = (dict(node["n"]) if isinstance(node, dict) and "n" in node
                             else (node if isinstance(node, dict) else {}))
                    if not props:
                        continue

                    name = node_display_name(label, props)
                    nid = node_unique_id(label, props)
                    if nid in seen_ids:
                        continue
                    seen_ids.add(nid)

                    desc = str(props.get("description", ""))
                    score = score_node(kw, name, desc)
                    text = serialize_node(label, props)

                    sources.append(HybridSource(
                        origin="graph",
                        score=score,
                        heading=f"{label}: {name}",
                        text=text,
                        node_label=label,
                        metadata={
                            k: v for k, v in props.items()
                            if isinstance(v, (str, int, float, bool)) and v
                        },
                    ))

        # 1-hop neighbor expansion for top graph hits
        sources = self._expand_graph_neighbors(kg, sources, seen_ids)
        return sources

    def _expand_graph_neighbors(
        self, kg, sources: List[HybridSource], seen_ids: set,
    ) -> List[HybridSource]:
        """Expand top graph results with 1-hop neighbors."""
        if not sources or not _HAS_KG_UTILS:
            return sources
        top_n = min(5, len(sources))
        for src in list(sources[:top_n]):
            label = src.node_label or ""
            name_prop = LABEL_NAME_PROPS.get(label, "name")
            name_val = src.metadata.get(name_prop, "")
            if not name_val:
                continue
            try:
                cypher = (
                    f"MATCH (n:{label} {{{name_prop}: $name_val}})"
                    f"-[r]-(m) "
                    f"WHERE m.module IS NULL OR m.module = $module "
                    f"RETURN type(r) AS rel_type, "
                    f"[lbl IN labels(m) WHERE lbl <> 'Node' | lbl] AS target_labels, "
                    f"m AS neighbor "
                    f"LIMIT 15"
                )
                results = kg.run(cypher, {"name_val": name_val,
                                          "module": self.module})
            except Exception:
                continue

            for rec in results:
                if rec.get("neighbor") is None:
                    continue
                nprops = dict(rec["neighbor"])
                target_labels = rec.get("target_labels", [])
                tlabel = target_labels[0] if target_labels else "Unknown"
                nname = node_display_name(tlabel, nprops)
                nid = node_unique_id(tlabel, nprops)
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                ntext = serialize_node(tlabel, nprops)
                sources.append(HybridSource(
                    origin="graph",
                    score=src.score * 0.6,
                    heading=f"{tlabel}: {nname}",
                    text=ntext,
                    node_label=tlabel,
                    metadata={
                        k: v for k, v in nprops.items()
                        if isinstance(v, (str, int, float, bool)) and v
                    },
                ))
        return sources

    # ── entity-targeted lookup ────────────────────────────────────────────

    def _entity_targeted_lookup(self, entities: List[str]) -> List[HybridSource]:
        """Exact-match Cypher queries for named entities — guarantees they
        appear in retrieval results regardless of similarity score.
        Ported from MCAL graphrag_query.py.
        """
        kg = self._get_kg_querier()
        if kg is None:
            return []

        sources: List[HybridSource] = []
        seen_ids: set = set()

        for entity in entities:
            cypher = (
                "MATCH (n) "
                "WHERE (n.module IS NULL OR n.module = $module) "
                "AND ("
                "  toLower(coalesce(n.function_name, '')) = toLower($name) "
                "  OR toLower(coalesce(n.param_name, ''))    = toLower($name) "
                "  OR toLower(coalesce(n.name, ''))           = toLower($name) "
                "  OR toLower(coalesce(n.title, ''))          = toLower($name) "
                "  OR toLower(coalesce(n.api_name, ''))       = toLower($name) "
                "  OR toLower(coalesce(n.test_case_id, ''))   = toLower($name) "
                "  OR toLower(coalesce(n.requirement_id, '')) = toLower($name) "
                "  OR toLower(coalesce(n.decision_id, ''))    = toLower($name) "
                "  OR toLower(coalesce(n.file_name, ''))      = toLower($name) "
                "  OR toLower(coalesce(n.macro_name, ''))     = toLower($name) "
                ") "
                "RETURN n, [lbl IN labels(n) WHERE lbl <> 'Node' | lbl] AS labels "
                "LIMIT 5"
            )
            try:
                results = kg.run(cypher, {"name": entity,
                                          "module": self.module})
            except Exception as exc:
                logger.debug("Entity lookup failed for %s: %s", entity, exc)
                continue

            for rec in results:
                if rec.get("n") is None:
                    continue
                props = dict(rec["n"])
                rec_labels = rec.get("labels", [])
                label = rec_labels[0] if rec_labels else "Unknown"
                name = node_display_name(label, props)
                nid = node_unique_id(label, props)
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)

                text = serialize_node(label, props)

                sources.append(HybridSource(
                    origin="graph",
                    score=2.5,  # High score to guarantee inclusion
                    heading=f"{label}: {name}",
                    text=text,
                    node_label=label,
                    metadata={
                        "_must_include": True,
                        **{k: v for k, v in props.items()
                           if isinstance(v, (str, int, float, bool)) and v},
                    },
                ))

        return sources

    # ── aggregation search ────────────────────────────────────────────────

    def _aggregation_search(
        self, question: str, named_entities: List[str],
    ) -> List[HybridSource]:
        """Direct Cypher queries for enumeration questions.
        Ported from MCAL graphrag_query.py.
        """
        kg = self._get_kg_querier()
        if kg is None:
            return []

        import re as _re
        q = question.lower()
        sources: List[HybridSource] = []

        # ASIL-level aggregation
        asil_match = _re.search(r'asil\s*([a-d])', q)
        if asil_match and any(w in q for w in ["function", "api", "rated", "level"]):
            asil_level = asil_match.group(1).upper()
            cypher = (
                "MATCH (n:SWUD_Function) "
                "WHERE toLower(n.asil_level) CONTAINS toLower($asil) "
                "RETURN n ORDER BY n.function_name"
            )
            try:
                results = kg.run(cypher, {"asil": asil_level})
                for rec in results:
                    if rec.get("n") is None:
                        continue
                    props = dict(rec["n"])
                    name = props.get("function_name", "?")
                    text = serialize_node("SWUD_Function", props)
                    sources.append(HybridSource(
                        origin="graph",
                        score=1.8,
                        heading=f"SWUD_Function: {name}",
                        text=text,
                        node_label="SWUD_Function",
                        metadata={
                            "_must_include": True,
                            **{k: v for k, v in props.items()
                               if isinstance(v, (str, int, float, bool)) and v},
                        },
                    ))
                logger.info("  ASIL %s aggregation: %d functions",
                            asil_level, len(results))
            except Exception as exc:
                logger.warning("ASIL aggregation failed: %s", exc)

        # Test case aggregation
        func_names: List[str] = []
        if any(w in q for w in ["test", "validate", "verify"]):
            for ent in named_entities:
                if "_" in ent or (ent[0].isupper() if ent else False):
                    func_names.append(ent)

        for func_name in func_names:
            cypher = (
                "MATCH (tc)-[:TS_VERIFIES]->(prq:ProductRequirement) "
                "WHERE toLower(prq.name) CONTAINS toLower($fname) "
                "RETURN tc, labels(tc) AS tc_labels, prq.name AS prq_name "
                "LIMIT 20"
            )
            try:
                results = kg.run(cypher, {"fname": func_name})
                for rec in results:
                    if rec.get("tc") is None:
                        continue
                    props = dict(rec["tc"])
                    tc_labels = rec.get("tc_labels", [])
                    label = tc_labels[0] if tc_labels else "TS_FunctionalTestCase"
                    name = props.get("test_case_id") or props.get("name", "?")
                    text = serialize_node(label, props)
                    sources.append(HybridSource(
                        origin="graph",
                        score=1.8,
                        heading=f"{label}: {name}",
                        text=text,
                        node_label=label,
                        metadata={
                            "_must_include": True,
                            "verified_prq": rec.get("prq_name", ""),
                            **{k: v for k, v in props.items()
                               if isinstance(v, (str, int, float, bool)) and v},
                        },
                    ))
                logger.info("  Test case aggregation for %s: %d results",
                            func_name, len(results))
            except Exception as exc:
                logger.warning("Test case aggregation failed for %s: %s",
                               func_name, exc)

        return sources

    # ── traceability ──────────────────────────────────────────────────────

    def _fetch_traceability(self, sources: List[HybridSource]) -> List[dict]:
        """Fetch V-model traceability for top graph sources."""
        kg = self._get_kg_querier()
        if kg is None:
            return []

        traces = []
        for src in sources:
            if not src.node_label or not _HAS_KG_UTILS:
                continue
            name_prop = LABEL_NAME_PROPS.get(src.node_label, "name")
            name_val = src.metadata.get(name_prop, "")
            if not name_val:
                continue
            try:
                cypher = (
                    f"MATCH (n:{src.node_label} {{{name_prop}: $name_val}})"
                    f"-[r]-(m) "
                    f"RETURN type(r) AS rel, "
                    f"[lbl IN labels(m) WHERE lbl <> 'Node' | lbl][0] AS target_label, "
                    f"coalesce(m.name, m.function_name, m.requirement_id, m.test_case_id, '') AS target_name "
                    f"LIMIT 10"
                )
                results = kg.run(cypher, {"name_val": name_val})
                for rec in results:
                    traces.append({
                        "source": src.heading,
                        "relationship": rec.get("rel", ""),
                        "target_label": rec.get("target_label", ""),
                        "target_name": rec.get("target_name", ""),
                    })
            except Exception:
                continue
        return traces

    # ── score normalisation ────────────────────────────────────────────────

    @staticmethod
    def _normalise(sources: List[HybridSource]) -> List[HybridSource]:
        """Normalise scores to [0, 1] by dividing by the max score."""
        if not sources:
            return sources
        max_score = max(s.score for s in sources)
        if max_score <= 0:
            return sources
        for s in sources:
            s.score = s.score / max_score
        return sources

    # ── score fusion ─────────────────────────────────────────────────────

    @staticmethod
    def _fuse_scores_mcal(
        vector: List[HybridSource],
        graph: List[HybridSource],
        alpha: float,
    ) -> List[HybridSource]:
        """MCAL fusion: pre-normalise both lists, then alpha-blend.

        Both lists are normalised to [0, 1] before blending so that
        neither source dominates due to different raw score scales.
        Overlapping sources (matched by heading) accumulate both scores.

        alpha=0.0 → pure vector, alpha=1.0 → pure graph.
        """
        HybridRAGOrchestrator._normalise(vector)
        HybridRAGOrchestrator._normalise(graph)

        merged: Dict[str, HybridSource] = {}

        for src in vector:
            key = (src.heading or src.text[:40]).lower().strip()[:120]
            src.score = src.score * (1 - alpha)
            merged[key] = src

        for src in graph:
            key = (src.heading or src.text[:40]).lower().strip()[:120]
            if key in merged:
                merged[key].score += src.score * alpha
                merged[key].origin = "hybrid"
                # Enrich with graph metadata
                merged[key].node_label = src.node_label
                merged[key].metadata.update(
                    {k: v for k, v in src.metadata.items()
                     if k not in merged[key].metadata}
                )
            else:
                src.score = src.score * alpha
                merged[key] = src

        return list(merged.values())

    @staticmethod
    def _fuse_scores_illd(
        vector: List[HybridSource],
        graph: List[HybridSource],
        alpha: float,
    ) -> List[HybridSource]:
        """ILLD fusion: raw alpha-blend without pre-normalisation.

        Duplicate items (same heading) are merged.
        alpha=0.0 → pure vector, alpha=1.0 → pure graph.
        """
        merged: Dict[str, HybridSource] = {}

        for src in vector:
            key = src.heading or src.text[:40]
            src.score = src.score * (1 - alpha)
            merged[key] = src

        for src in graph:
            key = src.heading or src.text[:40]
            if key in merged:
                merged[key].score += src.score * alpha
                merged[key].origin = "hybrid"
            else:
                src.score = src.score * alpha
                merged[key] = src

        return list(merged.values())

    # ── context assembly ──────────────────────────────────────────────────

    @staticmethod
    def _assemble_context(question: str, sources: List[HybridSource]) -> str:
        lines = [
            f"## Query: {question}\n",
            f"**{len(sources)} relevant source(s) found**\n",
        ]
        for i, src in enumerate(sources, 1):
            lines.append(
                f"\n### {i}. {src.heading}  (score={src.score:.3f}, origin={src.origin})\n"
                f"{src.text}"
            )
        return "\n".join(lines)

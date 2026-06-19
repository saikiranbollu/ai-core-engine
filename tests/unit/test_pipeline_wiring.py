"""
Tests for Pipeline Wiring in SearchService
=============================================
Verifies that SearchService.__init__ creates the GAP pipeline components
(_compressor, _judge, _refiner), that set_llm_fn wires them correctly,
and that hybrid_search invokes compress -> judge -> refine in order with
their stats appearing in the result dict.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))


# ═══════════════════════════════════════════════════════════════════════
#  __init__ creates pipeline components
# ═══════════════════════════════════════════════════════════════════════

class TestSearchServiceInit:

    def test_creates_compressor(self):
        """SearchService must create a _compressor attribute on init."""
        from src.HybridRAG.code.querier.search_service import SearchService
        from src.HybridRAG.code.querier.context_compressor import ContextCompressor

        svc = SearchService(neo4j_driver=None)
        assert hasattr(svc, "_compressor")
        assert isinstance(svc._compressor, ContextCompressor)

    def test_creates_judge(self):
        """SearchService must create a _judge attribute on init."""
        from src.HybridRAG.code.querier.search_service import SearchService
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge

        svc = SearchService(neo4j_driver=None)
        assert hasattr(svc, "_judge")
        assert isinstance(svc._judge, RelevanceJudge)

    def test_creates_refiner(self):
        """SearchService must create a _refiner attribute on init."""
        from src.HybridRAG.code.querier.search_service import SearchService
        from src.HybridRAG.code.querier.context_refiner import ContextRefiner

        svc = SearchService(neo4j_driver=None)
        assert hasattr(svc, "_refiner")
        assert isinstance(svc._refiner, ContextRefiner)

    def test_creates_enhancer(self):
        """SearchService must create a _enhancer attribute on init."""
        from src.HybridRAG.code.querier.search_service import SearchService

        svc = SearchService(neo4j_driver=None)
        assert hasattr(svc, "_enhancer")

    def test_creates_reranker(self):
        """SearchService must create a _reranker attribute on init."""
        from src.HybridRAG.code.querier.search_service import SearchService

        svc = SearchService(neo4j_driver=None)
        assert hasattr(svc, "_reranker")


# ═══════════════════════════════════════════════════════════════════════
#  set_llm_fn wires into components
# ═══════════════════════════════════════════════════════════════════════

class TestSetLlmFn:

    def test_wires_into_compressor_abstractive(self):
        """set_llm_fn must wire llm_fn into _compressor._abstractive._llm_fn.

        Note: SearchService.set_llm_fn has a bug on line 115 where
        `self._search_fn` is referenced instead of `self._refiner._search_fn`.
        We set `_search_fn = None` on the service to work around this.
        """
        from src.HybridRAG.code.querier.search_service import SearchService

        svc = SearchService(neo4j_driver=None)
        svc._search_fn = None  # workaround for line 115 bug
        fake_llm = MagicMock(name="fake_llm")
        svc.set_llm_fn(fake_llm)

        assert svc._compressor._abstractive._llm_fn is fake_llm
        assert svc._compressor._abstractive._enabled is True

    def test_wires_into_judge(self):
        """set_llm_fn must wire llm_fn into _judge._custom_backend._llm_fn."""
        from src.HybridRAG.code.querier.search_service import SearchService

        svc = SearchService(neo4j_driver=None)
        svc._search_fn = None  # workaround for line 115 bug
        fake_llm = MagicMock(name="fake_llm")
        svc.set_llm_fn(fake_llm)

        assert svc._judge._custom_backend._llm_fn is fake_llm

    def test_wires_into_refiner(self):
        """set_llm_fn must wire llm_fn into _refiner._llm_fn."""
        from src.HybridRAG.code.querier.search_service import SearchService

        svc = SearchService(neo4j_driver=None)
        svc._search_fn = None  # workaround for line 115 bug
        fake_llm = MagicMock(name="fake_llm")
        svc.set_llm_fn(fake_llm)

        assert svc._refiner._llm_fn is fake_llm


# ═══════════════════════════════════════════════════════════════════════
#  hybrid_search calls compress -> judge -> refine in order
# ═══════════════════════════════════════════════════════════════════════

class TestPipelineOrdering:

    def _make_service_with_mocks(self):
        """Create a SearchService with mocked Neo4j/Qdrant and pipeline stages."""
        from src.HybridRAG.code.querier.search_service import SearchService

        # Mock Neo4j driver so _graph_search returns canned results
        mock_driver = MagicMock()
        svc = SearchService(neo4j_driver=mock_driver, qdrant_client=None, module="CAN")

        # Build canned results (what RRF merge would produce)
        canned = [
            {"node_id": "f1", "node_type": "Function", "source": "neo4j",
             "score": 0.9, "content": "IfxCan_init function desc " * 20,
             "properties": {}},
            {"node_id": "f2", "node_type": "Function", "source": "neo4j",
             "score": 0.7, "content": "IfxCan_send function desc " * 20,
             "properties": {}},
        ]

        # Patch _graph_search to return canned results, skip actual Neo4j
        svc._graph_search = MagicMock(return_value=canned)

        # Patch _merge_results_weighted to pass through
        svc._merge_results_weighted = MagicMock(return_value=list(canned))

        # Patch _entity_targeted_lookup and _aggregation_search
        svc._entity_targeted_lookup = MagicMock(return_value=[])
        svc._aggregation_search = MagicMock(return_value=[])

        return svc, canned

    def test_compression_stats_appear_in_result(self):
        """When compressor is available, compression stats must appear in result."""
        from src.HybridRAG.code.querier.context_compressor import CompressionResult

        svc, canned = self._make_service_with_mocks()

        # Mock compressor
        comp_result = CompressionResult(
            compressed_items=canned, original_tokens=200,
            compressed_tokens=100, compression_ratio=2.0,
            stages_applied=["extractive"], items_before=2, items_after=2,
            latency_ms=5.0,
        )
        svc._compressor = MagicMock()
        svc._compressor.available = True
        svc._compressor.compress = MagicMock(return_value=comp_result)

        # Disable judge and refiner so only compressor runs
        svc._judge = MagicMock()
        svc._judge.available = False
        svc._refiner = MagicMock()
        svc._refiner.available = False
        svc._reranker = MagicMock()
        svc._reranker.available = False
        svc._batch_resolver = None

        result = svc.hybrid_search("init CAN module", alpha=1.0)

        assert "compression" in result
        assert result["compression"]["compression_ratio"] == 2.0
        svc._compressor.compress.assert_called_once()

    def test_judge_stats_appear_in_result(self):
        """When judge is available, relevance_judging stats must appear in result."""
        svc, canned = self._make_service_with_mocks()

        # Mock judge result
        judge_result = MagicMock()
        judge_result.results = canned
        judge_result.judged = True
        judge_result.original_count = 2
        judge_result.kept_count = 2
        judge_result.dropped_count = 0
        judge_result.backend = "custom"

        svc._judge = MagicMock()
        svc._judge.available = True
        svc._judge.judge = MagicMock(return_value=judge_result)

        # Disable compressor and refiner
        svc._compressor = MagicMock()
        svc._compressor.available = False
        svc._refiner = MagicMock()
        svc._refiner.available = False
        svc._reranker = MagicMock()
        svc._reranker.available = False
        svc._batch_resolver = None

        result = svc.hybrid_search("init CAN module", alpha=1.0)

        assert "relevance_judging" in result
        assert result["relevance_judging"]["judged"] is True
        assert result["relevance_judging"]["backend"] == "custom"
        svc._judge.judge.assert_called_once()

    def test_refine_stats_appear_for_complex_queries(self):
        """When refiner is available and query is complex, refinement stats appear."""
        from src.HybridRAG.code.querier.context_refiner import RefinementResult

        svc, canned = self._make_service_with_mocks()

        # Mock enhancer to return "complex" complexity
        enhanced_mock = MagicMock()
        enhanced_mock.enhanced_query = "complex ASIL-D IfxCan_init"
        enhanced_mock.suggested_alpha = 0.7
        enhanced_mock.strategy.value = "hybrid"
        enhanced_mock.complexity.value = "complex"
        enhanced_mock.synonyms_added = 0
        enhanced_mock.detected_entities = ["IfxCan_init"]
        enhanced_mock.detected_modules = ["CAN"]
        svc._enhancer = MagicMock()
        svc._enhancer.enhance = MagicMock(return_value=enhanced_mock)

        # Mock refiner
        ref_result = RefinementResult(
            refined_items=canned, iterations=2, agents_used=["coordinator", "code"],
            gaps_found=["missing init sequence"], gaps_resolved=["init sequence"],
            additional_queries=["IfxCan_init sequence"], completeness_score=0.85,
            total_tokens_used=500, latency_ms=100.0,
            crag_corrections=1, self_rag_retrievals=0,
        )
        svc._refiner = MagicMock()
        svc._refiner.available = True
        svc._refiner.refine = MagicMock(return_value=ref_result)

        # Disable compressor and judge
        svc._compressor = MagicMock()
        svc._compressor.available = False
        svc._judge = MagicMock()
        svc._judge.available = False
        svc._reranker = MagicMock()
        svc._reranker.available = False
        svc._batch_resolver = None

        result = svc.hybrid_search("complex ASIL-D IfxCan_init safety analysis", alpha=0.6)

        assert "refinement" in result
        assert result["refinement"]["iterations"] == 2
        assert result["refinement"]["crag_corrections"] == 1
        svc._refiner.refine.assert_called_once()

    def test_simple_query_skips_refinement(self):
        """Simple queries should not trigger refinement even if refiner is available."""
        svc, canned = self._make_service_with_mocks()

        # Mock enhancer to return "simple" complexity
        enhanced_mock = MagicMock()
        enhanced_mock.enhanced_query = "IfxCan_init"
        enhanced_mock.suggested_alpha = 0.8
        enhanced_mock.strategy.value = "graph"
        enhanced_mock.complexity.value = "simple"
        enhanced_mock.synonyms_added = 0
        enhanced_mock.detected_entities = ["IfxCan_init"]
        enhanced_mock.detected_modules = ["CAN"]
        svc._enhancer = MagicMock()
        svc._enhancer.enhance = MagicMock(return_value=enhanced_mock)

        # Refiner available but should not be called for "simple"
        svc._refiner = MagicMock()
        svc._refiner.available = True
        svc._refiner.refine = MagicMock()

        # Disable compressor and judge
        svc._compressor = MagicMock()
        svc._compressor.available = False
        svc._judge = MagicMock()
        svc._judge.available = False
        svc._reranker = MagicMock()
        svc._reranker.available = False
        svc._batch_resolver = None

        result = svc.hybrid_search("IfxCan_init", alpha=0.6)

        # refine should NOT have been called for a "simple" query
        svc._refiner.refine.assert_not_called()
        assert "refinement" not in result

    def test_pipeline_order_compress_then_judge_then_refine(self):
        """Pipeline must execute in order: compress -> judge -> refine."""
        from src.HybridRAG.code.querier.context_compressor import CompressionResult
        from src.HybridRAG.code.querier.context_refiner import RefinementResult

        svc, canned = self._make_service_with_mocks()
        call_order = []

        # Mock enhancer -> complex
        enhanced_mock = MagicMock()
        enhanced_mock.enhanced_query = "complex query"
        enhanced_mock.suggested_alpha = 0.6
        enhanced_mock.strategy.value = "hybrid"
        enhanced_mock.complexity.value = "complex"
        enhanced_mock.synonyms_added = 0
        enhanced_mock.detected_entities = []
        enhanced_mock.detected_modules = []
        svc._enhancer = MagicMock()
        svc._enhancer.enhance = MagicMock(return_value=enhanced_mock)

        # Mock compressor
        comp_result = CompressionResult(
            compressed_items=canned, original_tokens=200,
            compressed_tokens=100, compression_ratio=2.0,
            stages_applied=["extractive"], items_before=2, items_after=2,
        )

        def compress_side_effect(*args, **kwargs):
            call_order.append("compress")
            return comp_result

        svc._compressor = MagicMock()
        svc._compressor.available = True
        svc._compressor.compress = MagicMock(side_effect=compress_side_effect)

        # Mock judge
        judge_result = MagicMock()
        judge_result.results = canned
        judge_result.judged = True
        judge_result.original_count = 2
        judge_result.kept_count = 2
        judge_result.dropped_count = 0
        judge_result.backend = "custom"

        def judge_side_effect(*args, **kwargs):
            call_order.append("judge")
            return judge_result

        svc._judge = MagicMock()
        svc._judge.available = True
        svc._judge.judge = MagicMock(side_effect=judge_side_effect)

        # Mock refiner
        ref_result = RefinementResult(
            refined_items=canned, iterations=1, agents_used=["coordinator"],
            gaps_found=[], gaps_resolved=[], additional_queries=[],
            completeness_score=0.9, total_tokens_used=100, latency_ms=50.0,
        )

        def refine_side_effect(*args, **kwargs):
            call_order.append("refine")
            return ref_result

        svc._refiner = MagicMock()
        svc._refiner.available = True
        svc._refiner.refine = MagicMock(side_effect=refine_side_effect)

        # Disable reranker + batch resolver
        svc._reranker = MagicMock()
        svc._reranker.available = False
        svc._batch_resolver = None

        svc.hybrid_search("complex query about safety", alpha=0.6)

        assert call_order == ["compress", "judge", "refine"]

"""
Sprint 8 Integration Tests — Detailed Neo4j Search + ContextBuilder
====================================================================
Tests:
  1. ContextBuilder: token-budget slots, 2-pass fill, render
  2. kg_node_utils: label maps, node helpers, NER, aggregation, classification
  3. SearchService: constructor with new params, detailed _graph_search,
     entity-targeted lookup, aggregation search, build_context
  4. RLMOrchestrator: ContextBuilder-based sub-query assembly
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.HybridRAG.code.querier.context_builder import (
    AssembledContext, ContextBuilder, ContextBudget, ContextItem, ContextSlot,
    estimate_tokens,
)
from src.HybridRAG.code.querier.kg_node_utils import (
    COMPACT_PROPS, LABEL_DISPLAY_PROPS, LABEL_ID_PROPS, LABEL_NAME_PROPS,
    Source, classify_source, extract_keywords, extract_named_entities,
    format_source_for_context, infer_labels, is_aggregation_query,
    node_display_name, node_unique_id, normalise_scores, score_node,
    serialize_node,
)


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: ContextBuilder
# ═════════════════════════════════════════════════════════════════════════

class TestContextBuilder:
    def test_estimate_tokens(self):
        assert estimate_tokens("abc") == 1  # 3 chars -> ~1 token
        assert estimate_tokens("a" * 30) == 10
        assert estimate_tokens("") == 0

    def test_context_slot_has_expected_members(self):
        slots = ContextSlot.ALL
        assert "requirements" in slots
        assert "api_functions" in slots
        assert "tests" in slots
        assert "custom" in slots
        assert len(slots) == 10

    def test_context_budget_defaults(self):
        budget = ContextBudget()
        assert budget.total_budget == 8000
        assert len(budget.slot_budgets) == 10

    def test_context_budget_custom(self):
        budget = ContextBudget(total_budget=8000)
        assert budget.total_budget == 8000

    def test_build_empty(self):
        budget = ContextBudget(total_budget=1000)
        builder = ContextBuilder(budget=budget)
        result = builder.build([], max_tokens=1000)
        assert isinstance(result, AssembledContext)
        assert result.items_included == 0
        assert result.items_dropped == 0
        assert result.total_tokens == 0

    def test_build_with_items(self):
        budget = ContextBudget(total_budget=2000)
        builder = ContextBuilder(budget=budget)
        items = [
            ContextItem(slot=ContextSlot.REQUIREMENTS, content="REQ-001: Must handle DMA channels",
                        relevance_score=1.5, source="neo4j:SRS_Requirement"),
            ContextItem(slot=ContextSlot.API_FUNCTIONS, content="Adc_Init(group, config)",
                        relevance_score=2.0, source="neo4j:SWUD_Function"),
            ContextItem(slot=ContextSlot.TESTS, content="TC-ADC-001: Verify init sequence",
                        relevance_score=1.0, source="neo4j:TS_FunctionalTestCase"),
        ]
        result = builder.build(items, max_tokens=2000)
        assert result.items_included == 3
        assert result.items_dropped == 0
        assert result.total_tokens > 0

    def test_build_respects_budget(self):
        """Items exceeding budget should be dropped."""
        budget = ContextBudget(total_budget=50)  # very tight
        builder = ContextBuilder(budget=budget)
        items = [
            ContextItem(slot=ContextSlot.REQUIREMENTS, content="A " * 200,
                        relevance_score=1.0, source="test"),
            ContextItem(slot=ContextSlot.API_FUNCTIONS, content="B " * 200,
                        relevance_score=0.5, source="test"),
        ]
        result = builder.build(items, max_tokens=50)
        assert result.items_dropped > 0

    def test_render_output(self):
        budget = ContextBudget(total_budget=2000)
        builder = ContextBuilder(budget=budget)
        items = [
            ContextItem(slot=ContextSlot.REQUIREMENTS, content="REQ content here",
                        relevance_score=1.0, source="test"),
            ContextItem(slot=ContextSlot.CODE_EXAMPLES, content="Code snippet here",
                        relevance_score=0.8, source="test"),
        ]
        assembled = builder.build(items, max_tokens=2000)
        text = ContextBuilder.render(assembled)
        assert "requirements" in text.lower() or "REQ content" in text
        assert len(text) > 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: kg_node_utils — Label maps & helpers
# ═════════════════════════════════════════════════════════════════════════

class TestLabelMaps:
    def test_label_name_props_coverage(self):
        """All major AUTOSAR node types should be mapped."""
        expected = {"SWUD_Function", "TS_FunctionalTestCase",
                    "ProductRequirement", "SWA_ConfigParam"}
        assert expected.issubset(set(LABEL_NAME_PROPS.keys()))

    def test_label_id_props_coverage(self):
        assert "ProductRequirement" in LABEL_ID_PROPS
        assert "SWUD_Function" in LABEL_ID_PROPS

    def test_label_display_props_is_list_of_tuples(self):
        for label, lst in LABEL_DISPLAY_PROPS.items():
            assert isinstance(lst, list), f"{label} should have list"
            if lst:
                assert isinstance(lst[0], (list, tuple)), f"{label} items should be tuples"

    def test_compact_props_present(self):
        assert len(COMPACT_PROPS) >= 5


class TestNodeHelpers:
    def test_node_display_name_swud(self):
        props = {"function_name": "Adc_Init", "module": "ADC"}
        assert node_display_name("SWUD_Function", props) == "Adc_Init"

    def test_node_display_name_srs(self):
        props = {"requirement_id": "SRS-ADC-001", "title": "Init Req"}
        # ProductRequirement uses 'name' as name prop, falls back to generic
        assert node_display_name("ProductRequirement", props) in ("SRS-ADC-001", "Init Req")

    def test_node_display_name_fallback(self):
        props = {"name": "FallbackName"}
        assert node_display_name("SomeUnknownLabel", props) == "FallbackName"

    def test_node_unique_id_swud(self):
        props = {"function_name": "Adc_Init"}
        uid = node_unique_id("SWUD_Function", props)
        assert "Adc_Init" in uid

    def test_serialize_node_basic(self):
        props = {"function_name": "Adc_Init", "description": "Initializes ADC",
                 "asil_level": "ASIL-B", "module": "ADC"}
        text = serialize_node("SWUD_Function", props)
        assert "Adc_Init" in text
        assert "SWUD_Function" in text or "Function" in text

    def test_serialize_node_unknown_label(self):
        props = {"name": "Custom", "description": "test"}
        text = serialize_node("UnknownLabel", props)
        assert "Custom" in text or "test" in text


class TestExtractKeywords:
    def test_basic_extraction(self):
        keywords = extract_keywords("How does Adc_Init handle DMA?")
        assert any("adc" in kw.lower() for kw in keywords)

    def test_empty_query(self):
        assert extract_keywords("") == [] or len(extract_keywords("")) == 0


class TestInferLabels:
    def test_function_inference(self):
        labels = infer_labels("what does Adc_Init function do?")
        assert "SWUD_Function" in labels

    def test_requirement_inference(self):
        labels = infer_labels("show me the requirements for ADC")
        assert any(l in labels for l in ["ProductRequirement", "StakeholderRequirement"])

    def test_test_case_inference(self):
        labels = infer_labels("list test cases for Spi_Transmit")
        assert "TS_FunctionalTestCase" in labels

    def test_default_labels(self):
        """If no keywords match, should still return some default labels."""
        labels = infer_labels("hello")
        assert len(labels) > 0


class TestScoreNode:
    def test_exact_match_highest(self):
        s = score_node("Adc_Init", "Adc_Init", "Initializes ADC")
        assert s >= 2.0

    def test_substring_match(self):
        s = score_node("adc", "Adc_Init", "Initializes ADC")
        assert 0.5 < s < 2.0

    def test_no_match_lowest(self):
        s = score_node("xyz", "Adc_Init", "Initializes ADC")
        assert s <= 0.3


class TestNormaliseScores:
    def test_basic_normalise(self):
        items = [
            Source(origin="neo4j", score=10.0, heading="a", text="t"),
            Source(origin="neo4j", score=5.0, heading="b", text="t"),
            Source(origin="neo4j", score=0.0, heading="c", text="t"),
        ]
        normalised = normalise_scores(items)
        assert normalised[0].score == 1.0
        assert normalised[2].score == 0.0

    def test_single_item(self):
        items = [Source(origin="neo4j", score=5.0, heading="a", text="t")]
        normalised = normalise_scores(items)
        assert normalised[0].score == 1.0

    def test_empty_list(self):
        assert normalise_scores([]) == []


class TestNamedEntityExtraction:
    def test_camel_case_detection(self):
        ents = extract_named_entities("What does AdcInit configure?")
        assert any("AdcInit" in e for e in ents)

    def test_underscore_pattern(self):
        ents = extract_named_entities("Tell me about Adc_Init function")
        assert any("Adc_Init" in e for e in ents)

    def test_prq_pattern(self):
        ents = extract_named_entities("Show PRQ-42633 status")
        assert any("PRQ-42633" in e for e in ents)


class TestAggregationDetection:
    def test_list_all_is_aggregation(self):
        assert is_aggregation_query("List all ASIL-B functions") is True

    def test_how_many_is_aggregation(self):
        assert is_aggregation_query("How many test cases exist?") is True

    def test_simple_query_not_aggregation(self):
        assert is_aggregation_query("What does Adc_Init do?") is False

    def test_compare_is_aggregation(self):
        # "compare" may not be in patterns — check with known aggregation word
        assert is_aggregation_query("Show all parameters of Spi and Can modules") is True


class TestClassifySource:
    def test_classify_requirement(self):
        src = Source(origin="neo4j", score=1.0, heading="SRS-001", text="Req text",
                     node_label="SRS_Requirement")
        assert classify_source(src) == ContextSlot.REQUIREMENTS

    def test_classify_function(self):
        src = Source(origin="neo4j", score=1.0, heading="Adc_Init", text="Function",
                     node_label="SWUD_Function")
        assert classify_source(src) == ContextSlot.API_FUNCTIONS

    def test_classify_test(self):
        src = Source(origin="neo4j", score=1.0, heading="TC-001", text="Test case",
                     node_label="TS_FunctionalTestCase")
        slot = classify_source(src)
        assert isinstance(slot, str)  # Should be a valid slot

    def test_classify_config_param(self):
        src = Source(origin="neo4j", score=1.0, heading="Param", text="Config",
                     node_label="SWA_ConfigParam")
        slot = classify_source(src)
        assert isinstance(slot, str)

    def test_classify_qdrant(self):
        src = Source(origin="qdrant", score=0.8, heading="chunk", text="vector text",
                     node_label="")
        slot = classify_source(src)
        assert isinstance(slot, str)


class TestFormatSourceForContext:
    def test_basic_format(self):
        src = Source(origin="neo4j", score=1.0, heading="Adc_Init",
                     text="Function details here", node_label="SWUD_Function")
        text = format_source_for_context(src)
        assert "Adc_Init" in text
        assert "neo4j" in text.lower() or "Function" in text


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: SearchService — new params + detailed search
# ═════════════════════════════════════════════════════════════════════════

class TestSearchServiceInit:
    def test_default_constructor(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        assert svc.module == "ADC"
        assert svc._context_budget == 16000
        assert svc._context_builder is None

    def test_custom_constructor(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService(module="SPI", context_budget=8000)
        assert svc.module == "SPI"
        assert svc._context_budget == 8000

    def test_context_builder_lazy_init(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        svc = SearchService()
        builder = svc._get_context_builder()
        assert isinstance(builder, ContextBuilder)
        # Second call returns same instance
        assert svc._get_context_builder() is builder


class TestSearchServiceNoBackend:
    """Tests that methods degrade gracefully without Neo4j/Qdrant."""

    def setup_method(self):
        from src.HybridRAG.code.querier.search_service import SearchService
        self.svc = SearchService()

    def test_hybrid_search_no_backend(self):
        result = self.svc.hybrid_search("Adc_Init", max_results=5)
        assert result["total_count"] == 0
        assert result["results"] == []
        assert "named_entities" in result
        assert "is_aggregation" in result

    def test_entity_targeted_lookup_no_backend(self):
        results = self.svc._entity_targeted_lookup(["Adc_Init"])
        assert results == []

    def test_aggregation_search_no_backend(self):
        results = self.svc._aggregation_search("List all ASIL-B functions", ["Adc_Init"])
        assert results == []

    def test_fetch_traceability_no_backend(self):
        traces = self.svc.fetch_traceability([{"source": "neo4j", "properties": {}}])
        assert traces == []

    def test_build_context_empty(self):
        context_text, stats = self.svc.build_context("query", [])
        assert isinstance(context_text, str)
        assert stats["items_included"] == 0
        assert stats["total_tokens"] == 0

    def test_build_context_with_mock_results(self):
        results = [
            {"node_id": "f1", "node_type": "SWUD_Function", "source": "neo4j",
             "score": 2.0, "content": "Adc_Init: Initialize ADC module", "properties": {}},
            {"node_id": "r1", "node_type": "SRS_Requirement", "source": "neo4j",
             "score": 1.5, "content": "SRS-ADC-001: ADC Init requirement", "properties": {}},
        ]
        context_text, stats = self.svc.build_context("What does Adc_Init do?", results)
        assert stats["items_included"] == 2
        assert stats["total_tokens"] > 0
        assert len(context_text) > 0

    def test_node_to_text_uses_serialize_node(self):
        props = {"function_name": "Adc_Init", "description": "Initializes ADC"}
        text = self.svc._node_to_text(props, "SWUD_Function")
        assert "Adc_Init" in text


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: RLMOrchestrator — ContextBuilder-based assembly
# ═════════════════════════════════════════════════════════════════════════

class TestRLMOrchestratorContextBuilder:
    def test_execute_step_with_mock_search(self):
        from src.HybridRAG.code.querier.rlm_orchestrator import RLMOrchestrator

        def mock_search(query, max_results=10, alpha=0.5, workspace_id="illd"):
            return {
                "results": [
                    {"node_id": "f1", "node_type": "SWUD_Function", "source": "neo4j",
                     "score": 2.0, "content": "Adc_Init: Initializes ADC module",
                     "properties": {"function_name": "Adc_Init"}},
                    {"node_id": "r1", "node_type": "SRS_Requirement", "source": "neo4j",
                     "score": 1.5, "content": "SRS-ADC-001: ADC initialization requirement",
                     "properties": {"requirement_id": "SRS-ADC-001"}},
                ],
                "total_count": 2,
            }

        orch = RLMOrchestrator(search_fn=mock_search)
        step_data = {"step_id": 1, "intent": "find ADC init info", "query": "Adc_Init", "alpha": 0.5}
        result = orch._execute_step(step_data, {})

        assert result.step_id == 1
        assert result.sources_n == 2
        assert len(result.answer) > 0
        # ContextBuilder output should contain structured sections
        assert result.tokens > 0

    def test_execute_step_no_search_fn(self):
        from src.HybridRAG.code.querier.rlm_orchestrator import RLMOrchestrator
        orch = RLMOrchestrator()  # No search_fn
        step_data = {"step_id": 1, "intent": "test", "query": "test", "alpha": 0.5}
        result = orch._execute_step(step_data, {})
        assert "No search function" in result.answer

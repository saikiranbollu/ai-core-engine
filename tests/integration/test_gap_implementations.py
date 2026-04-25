"""
Integration Tests — GAP Implementations v2 (Research Upgrades + New Features)
==============================================================================
Tests: 73 total
  Existing (upgraded): A01, A02, A03, A04, A05, A06, A07, A08, A09, A11, A13, A14
  New features: MISRA Remediation, Unit Test Gen, FMEA, Formal Verification
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ═══════ GAP-A06: BatchGraphResolver (unchanged) ═══════
class TestBatchGraphResolver:
    def test_import(self):
        from src.HybridRAG.code.querier.batch_graph_resolver import BatchGraphResolver, BatchQueryStats
        assert BatchGraphResolver is not None

    def test_not_available(self):
        from src.HybridRAG.code.querier.batch_graph_resolver import BatchGraphResolver
        assert not BatchGraphResolver(neo4j_driver=None).available

    def test_batch_enrich_no_driver(self):
        from src.HybridRAG.code.querier.batch_graph_resolver import BatchGraphResolver
        r = BatchGraphResolver(neo4j_driver=None)
        assert r.batch_enrich([{"node_id": "1"}]) == [{"node_id": "1"}]

    def test_stats(self):
        from src.HybridRAG.code.querier.batch_graph_resolver import BatchQueryStats
        s = BatchQueryStats()
        s.record_batch(10, 25)
        s.record_fallback()
        assert s.as_dict()["total_batch_calls"] == 1


# ═══════ GAP-A03: QueryEnhancer (upgraded: LLM expansion) ═══════
class TestQueryEnhancer:
    def test_import(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer, QueryComplexity
        assert QueryEnhancer is not None

    def test_simple_entity(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        r = QueryEnhancer().enhance("What is IfxCan_Node_init?")
        assert r.complexity.value == "simple"
        assert "IfxCan_Node_init" in r.detected_entities

    def test_structural_graph_heavy(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        r = QueryEnhancer().enhance("What calls IfxCan_Node_init and what are the dependencies?")
        assert r.strategy.value in ("graph_heavy", "hybrid")

    def test_complex_query(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        r = QueryEnhancer().enhance(
            "Compare ADC and SPI init sequences including registers and ASIL "
            "requirements and show dependency chain for IfxAdc_Adc_init and IfxQspi_SpiMaster_init")
        assert r.complexity.value in ("medium", "complex")
        assert r.token_budget_hint >= 8000

    def test_module_detection(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        r = QueryEnhancer().enhance("ADC and CAN driver init")
        assert "ADC" in r.detected_modules
        assert "CAN" in r.detected_modules

    def test_aggregation(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        assert QueryEnhancer().enhance("List all ASIL-D functions").is_aggregation

    def test_empty(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        assert QueryEnhancer().enhance("").enhanced_query == ""

    def test_llm_fn_accepted(self):
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer
        qe = QueryEnhancer(llm_fn=lambda s, u, m: '["expanded query 1"]')
        assert qe._llm_fn is not None


# ═══════ GAP-A01: CrossEncoderReranker (FlashRank upgrade) ═══════
class TestCrossEncoderReranker:
    def test_import(self):
        from src.HybridRAG.code.querier.reranker import CrossEncoderReranker, RerankResult
        assert CrossEncoderReranker is not None

    def test_skip_structural(self):
        from src.HybridRAG.code.querier.reranker import CrossEncoderReranker
        rr = CrossEncoderReranker(enabled=True)
        r = rr.rerank("test", [{"content": "x"}] * 5, search_strategy="graph_heavy")
        assert not r.reranked
        assert "structural" in (r.skip_reason or "").lower() or "strategy" in (r.skip_reason or "").lower()

    def test_disabled(self):
        from src.HybridRAG.code.querier.reranker import CrossEncoderReranker
        assert not CrossEncoderReranker(enabled=False).available

    def test_too_few(self):
        from src.HybridRAG.code.querier.reranker import CrossEncoderReranker
        rr = CrossEncoderReranker(enabled=True)
        r = rr.rerank("test", [{"content": "x"}], search_strategy="hybrid")
        assert not r.reranked

    def test_result_dataclass(self):
        from src.HybridRAG.code.querier.reranker import RerankResult
        r = RerankResult(results=[], reranked=True, backend="flashrank",
                         model_used="test", original_count=5, reranked_count=3, latency_ms=10.0)
        d = r.as_dict()
        assert d["backend"] == "flashrank"

    def test_fallback_chain(self):
        from src.HybridRAG.code.querier.reranker import CrossEncoderReranker
        rr = CrossEncoderReranker(backend="flashrank")
        assert len(rr._backends) == 2
        assert rr._backends[0].name == "flashrank"
        assert rr._backends[1].name == "crossencoder"


# ═══════ GAP-A02: MCP Streaming (SDK upgrade) ═══════
class TestStreamingSDK:
    def test_import(self):
        from mcp.core.streaming import MCPStreamNotifier, StreamMetrics
        assert MCPStreamNotifier is not None

    def test_metrics(self):
        from mcp.core.streaming import StreamMetrics
        m = StreamMetrics(stream_id="t", started_at=100.0, first_event_at=100.5)
        assert m.time_to_first_token_ms == 500.0

    def test_notifier_no_server(self):
        from mcp.core.streaming import MCPStreamNotifier
        n = MCPStreamNotifier(server=None)
        assert n.get_active_streams() == []

    def test_complete(self):
        from mcp.core.streaming import MCPStreamNotifier, StreamMetrics
        n = MCPStreamNotifier()
        n._metrics["test"] = StreamMetrics(stream_id="test", started_at=1.0)
        m = n.complete("test")
        assert m is not None
        assert m.completed


# ═══════ GAP-A04+A09: ContextCompressor + DynamicTokenBudget (LLMLingua upgrade) ═══════
class TestContextCompressor:
    def test_import(self):
        from src.HybridRAG.code.querier.context_compressor import (
            ContextCompressor, DynamicTokenBudget, LLMLinguaCompressor, ExtractiveCompressor)
        assert ContextCompressor is not None

    def test_dynamic_budget_simple(self):
        from src.HybridRAG.code.querier.context_compressor import DynamicTokenBudget
        assert DynamicTokenBudget.compute("simple")["total_budget"] == 4000

    def test_dynamic_budget_complex(self):
        from src.HybridRAG.code.querier.context_compressor import DynamicTokenBudget
        assert DynamicTokenBudget.compute("complex")["total_budget"] == 12000

    def test_extractive_fallback(self):
        from src.HybridRAG.code.querier.context_compressor import ExtractiveCompressor
        c = ExtractiveCompressor(max_sentences=2)
        items = [{"content": "A. B. C. D. E."}]
        r = c.compress(items, "test", 1000)
        assert len(r) >= 1

    def test_pipeline_disabled(self):
        from src.HybridRAG.code.querier.context_compressor import ContextCompressor
        r = ContextCompressor(enabled=False).compress([{"content": "x"}], "q")
        assert r.compression_ratio == 1.0

    def test_pipeline_extractive_only(self):
        from src.HybridRAG.code.querier.context_compressor import ContextCompressor
        r = ContextCompressor(llm_fn=None, enabled=True).compress(
            [{"content": "A. B. C. D.", "relevance_score": 0.9}], "test", "simple")
        assert "extractive" in r.stages_applied or "llmlingua" in r.stages_applied


# ═══════ GAP-A08: RelevanceJudge (DeepEval upgrade) ═══════
class TestRelevanceJudge:
    def test_import(self):
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge, JudgeResult
        assert RelevanceJudge is not None

    def test_skip_auto(self):
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge
        j = RelevanceJudge(llm_fn=lambda s, u, m: "[]", enabled=True)
        r = j.judge("q", [{"content": "x"}] * 5, review_type="AUTO")
        assert not r.judged
        assert "AUTO" in (r.skip_reason or "")

    def test_disabled(self):
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge
        assert not RelevanceJudge(enabled=False).available

    def test_too_few(self):
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge
        j = RelevanceJudge(llm_fn=lambda s, u, m: "[]", enabled=True)
        r = j.judge("q", [{"content": "x"}])
        assert not r.judged

    def test_backend_order_deepeval_first(self):
        from src.HybridRAG.code.querier.relevance_judge import RelevanceJudge
        j = RelevanceJudge(backend="deepeval")
        assert j._backends[0].name == "deepeval"


# ═══════ GAP-A07: ContextRefiner (CRAG + Self-RAG upgrade) ═══════
class TestContextRefiner:
    def test_import(self):
        from src.HybridRAG.code.querier.context_refiner import ContextRefiner, RefinementResult
        assert ContextRefiner is not None

    def test_skip_non_complex(self):
        from src.HybridRAG.code.querier.context_refiner import ContextRefiner
        r = ContextRefiner(llm_fn=lambda s, u, m: "{}").refine("q", [{"content": "x"}], "simple")
        assert not r.refined

    def test_not_available(self):
        from src.HybridRAG.code.querier.context_refiner import ContextRefiner
        assert not ContextRefiner(llm_fn=None).available

    def test_has_crag_fields(self):
        from src.HybridRAG.code.querier.context_refiner import RefinementResult
        r = RefinementResult(refined_items=[], iterations=0, agents_used=[],
                             gaps_found=[], gaps_resolved=[], additional_queries=[],
                             completeness_score=0.5, total_tokens_used=0, latency_ms=0.0,
                             crag_corrections=2, self_rag_retrievals=1)
        d = r.as_dict()
        assert d["crag_corrections"] == 2
        assert d["self_rag_retrievals"] == 1


# ═══════ GAP-A05: BatchIngestion (unchanged) ═══════
class TestBatchIngestion:
    def test_import(self):
        from src.IngestionPipeline.batch_ingestion import BatchIngestionPipeline, IngestionJob
        assert BatchIngestionPipeline is not None

    def test_job(self):
        from src.IngestionPipeline.batch_ingestion import IngestionJob
        j = IngestionJob(total_items=100, processed_items=50)
        assert j.progress == 0.5

    def test_embedder(self):
        from src.IngestionPipeline.batch_ingestion import BatchEmbedder
        e = BatchEmbedder(embed_fn=lambda t: [0.1] * 384, batch_size=2)
        assert len(e.embed_batch(["a", "b", "c"])) == 3


# ═══════ GAP-A13: CitationVerifier (RAGChecker upgrade) ═══════
class TestCitationVerifier:
    def test_import(self):
        from src.ReviewGate.citation_verifier import CitationVerifier, VerificationResult
        assert CitationVerifier is not None

    def test_regex_extraction(self):
        from src.ReviewGate.citation_verifier import CitationVerifier
        v = CitationVerifier(enabled=True)
        r = v.verify("IfxCan_Node_init does initialization. ADC_REQ_001 requires calibration.",
                      [{"content": "IfxCan_Node_init is the CAN init function."}])
        assert r.total_claims >= 1
        assert r.verified_claims >= 1
        assert "text_overlap" in r.methods_used

    def test_disabled(self):
        from src.ReviewGate.citation_verifier import CitationVerifier
        r = CitationVerifier(enabled=False).verify("test", [])
        assert not r.verified

    def test_confidence_signal(self):
        from src.ReviewGate.citation_verifier import VerificationResult
        r = VerificationResult(claims=[], total_claims=10, verified_claims=8,
                               unverified_claims=2, verification_rate=0.8,
                               flagged_claims=[], latency_ms=0.0, methods_used=["text_overlap"])
        assert r.as_confidence_signal()["citation_verification_rate"] == 0.8


# ═══════ GAP-A14: FewShotLibrary (unchanged) ═══════
class TestFewShotLibrary:
    def test_import(self):
        from src.MemoryLayer.memory.few_shot_library import FewShotLibrary, FewShotExample
        assert FewShotLibrary is not None

    def test_not_available(self):
        from src.MemoryLayer.memory.few_shot_library import FewShotLibrary
        assert not FewShotLibrary(qdrant_client=None).available

    def test_render(self):
        from src.MemoryLayer.memory.few_shot_library import FewShotExample
        e = FewShotExample(example_id="1", question="Q?", answer="A.", task_type="t")
        assert "Example Q:" in e.render()


# ═══════ GAP-A11: OCR (unchanged) ═══════
class TestOCR:
    def test_import(self):
        from src.IngestionPipeline.parsers.ocr_processor import OCRProcessor
        assert OCRProcessor is not None

    def test_is_scanned(self):
        from src.IngestionPipeline.parsers.ocr_processor import OCRProcessor
        o = OCRProcessor()
        assert o.is_scanned_page("") is True
        assert o.is_scanned_page("This has enough text to not be scanned.") is False

    def test_confidence(self):
        from src.IngestionPipeline.parsers.ocr_processor import OCRProcessor
        assert OCRProcessor._estimate_confidence("The function initializes register.") > 0.5
        assert OCRProcessor._estimate_confidence("") == 0.0


# ═══════ NEW: MISRA Remediation ═══════
class TestMISRARemediation:
    def test_import(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import (
            MISRARemediationEngine, ViolationParser, MISRARuleRetriever, MISRAViolation)
        assert MISRARemediationEngine is not None

    def test_parse_polyspace(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import ViolationParser
        output = 'main.c:42:10: warning: MISRA C:2012 Rule 10.1 - Operands inappropriate type'
        vs = ViolationParser.parse(output)
        assert len(vs) == 1
        assert vs[0].rule_id == "10.1"
        assert vs[0].line_number == 42

    def test_parse_gcc(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import ViolationParser
        output = 'file.c:10:5: warning: [misra-c2012-8.4] function missing declaration'
        vs = ViolationParser.parse(output)
        assert len(vs) == 1
        assert vs[0].rule_id == "8.4"

    def test_rule_retriever_builtin(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import MISRARuleRetriever
        r = MISRARuleRetriever()
        rule = r.get_rule("8.4")
        assert rule["category"] == "Required"
        assert "declaration" in rule["text"].lower()

    def test_rule_retriever_unknown(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import MISRARuleRetriever
        rule = MISRARuleRetriever().get_rule("99.99")
        assert rule["source"] == "none"

    def test_violation_dataclass(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import MISRAViolation
        v = MISRAViolation(rule_id="10.1")
        assert v.full_rule_id == "MISRA-C:2012 Rule 10.1"

    def test_compliance_matrix(self):
        from src.DomainAssistants.misra_remediation.misra_remediation_engine import MISRARemediationEngine
        e = MISRARemediationEngine(llm_fn=lambda s, u, m: "")
        matrix = e.generate_compliance_matrix(
            'file.c:1:1: warning: MISRA C:2012 Rule 8.4 - test\n'
            'file.c:2:1: warning: MISRA C:2012 Rule 8.4 - test2\n'
            'file.c:3:1: warning: MISRA C:2012 Rule 10.1 - test3\n',
            module="ADC")
        assert matrix.violated_rules == 2
        assert len(matrix.entries) == 2


# ═══════ NEW: Unit Test Generation ═══════
class TestUnitTestGen:
    def test_import(self):
        from src.DomainAssistants.unit_test_gen.test_generation_engine import (
            TestGenerationEngine, TestSuite, TestCase, MockTarget)
        assert TestGenerationEngine is not None

    def test_not_available(self):
        from src.DomainAssistants.unit_test_gen.test_generation_engine import TestGenerationEngine
        assert not TestGenerationEngine(llm_fn=None).available

    def test_testcase_render(self):
        from src.DomainAssistants.unit_test_gen.test_generation_engine import TestCase
        tc = TestCase(test_name="test_init", function_under_test="Adc_Init",
                      test_body="TEST_ASSERT(result == E_OK);")
        rendered = tc.render()
        assert "test_init" in rendered
        assert "TEST_ASSERT" in rendered

    def test_testsuite_render(self):
        from src.DomainAssistants.unit_test_gen.test_generation_engine import TestSuite, TestCase
        tc = TestCase(test_name="test_a", function_under_test="f", test_body="/* test */")
        suite = TestSuite(module="ADC", function_name="Adc_Init", test_cases=[tc],
                          includes=["IfxAdc.h"])
        rendered = suite.render()
        assert "IfxAdc.h" in rendered
        assert "RUN_TEST(test_a)" in rendered
        assert "et.h" in rendered

    def test_mock_target(self):
        from src.DomainAssistants.unit_test_gen.test_generation_engine import MockTarget
        m = MockTarget(name="CLC", mock_type="register",
                       declaration="static volatile uint32 mock_CLC;")
        assert m.mock_type == "register"


# ═══════ NEW: FMEA ═══════
class TestFMEA:
    def test_import(self):
        from src.DomainAssistants.fmea_assistant.fmea_engine import (
            FMEAEngine, FMEAReport, FailureMode)
        assert FMEAEngine is not None

    def test_failure_mode_rpn(self):
        from src.DomainAssistants.fmea_assistant.fmea_engine import FailureMode
        fm = FailureMode(fm_id="FM-001", component="CAN", function="IfxCan_init",
                         failure_mode="stuck", failure_effect="loss",
                         failure_cause="hw", severity=8, occurrence=3, detection=5)
        assert fm.rpn == 120

    def test_report_dict(self):
        from src.DomainAssistants.fmea_assistant.fmea_engine import FMEAReport
        r = FMEAReport(module="ADC", review_required=True)
        assert r.as_dict()["review_required"] is True

    def test_not_available(self):
        from src.DomainAssistants.fmea_assistant.fmea_engine import FMEAEngine
        assert not FMEAEngine(llm_fn=None).available


# ═══════ NEW: Formal Verification (CBMC) ═══════
class TestFormalVerification:
    def test_import(self):
        from src.DomainAssistants.formal_verification.cbmc_bridge import (
            CBMCBridge, FormalAssertion, FormalVerificationReport)
        assert CBMCBridge is not None

    def test_assertion_dataclass(self):
        from src.DomainAssistants.formal_verification.cbmc_bridge import FormalAssertion
        a = FormalAssertion(assertion_id="FA-01", requirement_id="REQ-001",
                            requirement_text="Buffer must not overflow",
                            assertion_code='__CPROVER_assert(idx < SIZE, "bounds");',
                            assertion_type="bounds")
        assert "CPROVER" in a.assertion_code

    def test_report_dict(self):
        from src.DomainAssistants.formal_verification.cbmc_bridge import FormalVerificationReport
        r = FormalVerificationReport(module="ADC", verified_count=3, failed_count=1)
        d = r.as_dict()
        assert d["verified"] == 3
        assert d["failed"] == 1

    def test_not_available_without_llm(self):
        from src.DomainAssistants.formal_verification.cbmc_bridge import CBMCBridge
        assert not CBMCBridge(llm_fn=None).available

"""
Sprint 5 Integration Tests — RLM Orchestrator + Ingestion Pipeline
===================================================================
Tests:
  1. RLMOrchestrator: plan, execute with mock search, synthesis
  2. Complexity heuristic: should_use_rlm detection
  3. RLM task types: 24 types covering all 21 DAs
  4. IngestionService: file parse, module discovery, job tracking
  5. Full pipeline: ingest → search → RLM context assembly
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.HybridRAG.code.querier.rlm_orchestrator import (
    RLMOrchestrator, RLMTaskType, RLMContext, SubQueryStep,
    should_use_rlm, DA_TASK_MAPPING,
)
from src.IngestionPipeline.ingestion_service import (
    IngestionService, IngestionJobTracker,
)


# ═════════════════════════════════════════════════════════════════════════
#  Mock search + LLM functions (no real backends needed)
# ═════════════════════════════════════════════════════════════════════════

def mock_search(query, max_results=10, alpha=0.5, workspace_id="illd"):
    """Simulates search_database returning results."""
    return {
        "results": [
            {"node_id": f"node_{i}", "node_type": "APIFunction",
             "content": f"Result {i} for: {query[:40]}", "score": 0.9 - i * 0.1}
            for i in range(min(3, max_results))
        ],
        "total_count": 3,
    }

def mock_llm(system, user, max_tokens=1500):
    """Simulates LLM returning a plan or synthesis."""
    if "planner" in system.lower() or "decompose" in system.lower():
        return json.dumps({
            "reasoning": "Test plan",
            "steps": [
                {"step_id": 1, "intent": "Get requirements", "query": "CAN requirements", "alpha": 0.8},
                {"step_id": 2, "intent": "Get API functions", "query": "CAN init functions", "alpha": 0.5},
                {"step_id": 3, "intent": "Get HW registers", "query": "CAN registers CLC", "alpha": 0.7},
            ]
        })
    else:
        return "Synthesized answer: CAN initialization requires CLC register setup followed by IfxCan_init call."


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: RLM Orchestrator Core
# ═════════════════════════════════════════════════════════════════════════

class TestRLMOrchestrator:

    def test_run_with_mocks(self):
        rlm = RLMOrchestrator(module="CAN", profile="mcal",
                               search_fn=mock_search, llm_fn=mock_llm)
        result = rlm.run("Generate CAN_Init function", task_type="code_generation")

        assert isinstance(result, RLMContext)
        assert result.module == "CAN"
        assert result.profile == "mcal"
        assert result.task_type == "code_generation"
        assert len(result.sub_query_trace) == 3  # Mock plan has 3 steps
        assert result.total_tokens > 0
        assert result.total_elapsed_s > 0
        assert "CAN" in result.assembled_context

    def test_plan_preview(self):
        rlm = RLMOrchestrator(module="SPI", profile="illd", llm_fn=mock_llm)
        preview = rlm.plan_preview("Generate SPI test", task_type="test_generation")

        assert "plan" in preview
        assert preview["step_count"] == 3

    def test_to_dict(self):
        rlm = RLMOrchestrator(module="ADC", search_fn=mock_search, llm_fn=mock_llm)
        result = rlm.run("ADC init", task_type="generic")
        d = result.to_dict()

        assert d["module"] == "ADC"
        assert d["sub_queries"] == 3
        assert isinstance(d["sub_query_trace"], list)
        assert all("step" in s for s in d["sub_query_trace"])

    def test_fallback_on_bad_plan(self):
        """If LLM returns garbage, should fallback to single query."""
        def bad_llm(system, user, max_tokens=1500):
            return "This is not JSON at all!"

        rlm = RLMOrchestrator(module="CAN", search_fn=mock_search, llm_fn=bad_llm)
        result = rlm.run("test query")
        # Should still work with fallback single step
        assert len(result.sub_query_trace) >= 1

    def test_no_search_function(self):
        """Without search_fn, steps produce placeholder answers."""
        rlm = RLMOrchestrator(module="CAN", search_fn=None, llm_fn=mock_llm)
        result = rlm.run("test query")
        assert len(result.sub_query_trace) == 3
        for sq in result.sub_query_trace:
            assert "No search function" in sq.answer

    def test_sub_query_step_fields(self):
        rlm = RLMOrchestrator(module="CAN", search_fn=mock_search, llm_fn=mock_llm)
        result = rlm.run("Generate init", task_type="code_generation")

        sq = result.sub_query_trace[0]
        assert isinstance(sq, SubQueryStep)
        assert sq.step_id == 1
        assert sq.intent == "Get requirements"
        assert sq.sources_n == 3
        assert sq.tokens > 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: Complexity Heuristic
# ═════════════════════════════════════════════════════════════════════════

class TestComplexityHeuristic:

    def test_simple_query_no_rlm(self):
        assert not should_use_rlm("What is IfxCan_init?")

    def test_complex_multi_function_register(self):
        """3+ functions + register keywords → RLM."""
        assert should_use_rlm(
            "Generate init using IfxCan_init, IfxCan_setMode, IfxCan_enableModule "
            "with CLC register configuration"
        )

    def test_asil_with_register(self):
        """ASIL + register → RLM."""
        assert should_use_rlm(
            "Generate ASIL-B compliant DMA transfer with register setup"
        )

    def test_traceability_always_rlm(self):
        """Traceability task type → always RLM."""
        assert should_use_rlm("Verify requirement coverage", task_type="traceability")

    def test_debug_always_rlm(self):
        assert should_use_rlm("ADC fails intermittently", task_type="debug_analysis")


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Task Types Coverage
# ═════════════════════════════════════════════════════════════════════════

class TestTaskTypes:

    def test_all_24_types_defined(self):
        assert len(RLMTaskType) == 24  # 24 enum values

    def test_all_21_das_mapped(self):
        """Every DA has at least one task type mapping."""
        expected_das = {
            "GEST", "ACRA", "CIA", "CTA", "SAGA", "PAGE", "TripleA", "KW",
            "SAVA", "SASA", "DaFaA", "HazopA", "GECA", "GEVT", "ATRA", "ATQA",
            "VoltAI", "REVA", "StopTyping", "PRQ_Drafter", "RMA",
        }
        assert set(DA_TASK_MAPPING.keys()) == expected_das

    def test_each_mapping_has_valid_types(self):
        valid_types = {t.value for t in RLMTaskType}
        for da, types in DA_TASK_MAPPING.items():
            for t in types:
                assert t in valid_types, f"DA '{da}' has invalid task type '{t}'"


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Ingestion Service
# ═════════════════════════════════════════════════════════════════════════

class TestIngestionService:

    def test_ingest_c_header(self):
        svc = IngestionService()
        with tempfile.NamedTemporaryFile(suffix=".h", mode="w", delete=False) as f:
            f.write("void IfxCan_init(IfxCan_Config *cfg);\nuint8 IfxCan_transmit(uint8 *data);\n")
            tmp = f.name

        result = svc.ingest_file(tmp, "CAN")
        assert result["status"] == "completed"
        assert result["module_name"] == "CAN"
        assert result["extension"] == ".h"
        assert "job_id" in result
        Path(tmp).unlink()

    def test_ingest_json_file(self):
        svc = IngestionService()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"requirements": [{"id": "REQ_001", "text": "The CAN driver shall init"}]}, f)
            tmp = f.name

        result = svc.ingest_file(tmp, "CAN")
        assert result["status"] == "completed"
        Path(tmp).unlink()

    def test_ingest_missing_file(self):
        svc = IngestionService()
        with pytest.raises(FileNotFoundError):
            svc.ingest_file("/nonexistent/file.h", "CAN")

    def test_ingest_unsupported_extension(self):
        svc = IngestionService()
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            tmp = f.name
        with pytest.raises(ValueError, match="Unsupported"):
            svc.ingest_file(tmp, "CAN")
        Path(tmp).unlink()

    def test_job_tracker(self):
        tracker = IngestionJobTracker()
        jid = tracker.create_job("test", {"file": "x.h"})
        assert tracker.get(jid)["status"] == "queued"

        tracker.update(jid, status="processing", progress=50)
        assert tracker.get(jid)["progress"] == 50

        tracker.complete(jid, {"nodes": 5})
        assert tracker.get(jid)["status"] == "completed"

    def test_job_failure(self):
        tracker = IngestionJobTracker()
        jid = tracker.create_job("test", {})
        tracker.fail(jid, "Parse error")
        assert tracker.get(jid)["status"] == "failed"
        assert "Parse error" in tracker.get(jid)["error"]

    def test_ingest_module_with_temp_dir(self):
        svc = IngestionService()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a module directory
            mod_dir = Path(tmpdir) / "CAN"
            mod_dir.mkdir()
            (mod_dir / "can_init.h").write_text("void IfxCan_init(void);\n")
            (mod_dir / "can_tx.h").write_text("void IfxCan_transmit(uint8 *d);\n")

            result = svc.ingest_module(tmpdir, "CAN")
            assert result["files_found"] >= 2
            assert result["files_processed"] >= 2

    def test_batch_ingest(self):
        svc = IngestionService()
        with tempfile.TemporaryDirectory() as tmpdir:
            for mod in ("CAN", "SPI"):
                d = Path(tmpdir) / mod
                d.mkdir()
                (d / "init.h").write_text(f"void Ifx{mod}_init(void);\n")

            result = svc.batch_ingest(tmpdir, modules=["CAN", "SPI"])
            assert result["modules_processed"] == 2
            assert len(result["per_module"]) == 2


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: Full Pipeline — Ingest → Search → RLM
# ═════════════════════════════════════════════════════════════════════════

class TestFullPipeline:

    def test_ingest_then_rlm(self):
        """Simulate: ingest files → search produces results → RLM assembles context."""
        # Step 1: Ingest a file
        svc = IngestionService()
        with tempfile.NamedTemporaryFile(suffix=".h", mode="w", delete=False) as f:
            f.write("""
void IfxCan_init(IfxCan_Config *cfg);
void IfxCan_setMode(IfxCan_Node *node, uint8 mode);
void IfxCan_enableModule(void);
""")
            tmp = f.name

        ingest_result = svc.ingest_file(tmp, "CAN")
        assert ingest_result["status"] == "completed"

        # Step 2: RLM with mock search (simulates post-ingest search)
        rlm = RLMOrchestrator(module="CAN", profile="mcal",
                               search_fn=mock_search, llm_fn=mock_llm)
        rlm_result = rlm.run(
            "Generate CAN initialization with register setup",
            task_type="code_generation",
        )
        assert rlm_result.assembled_context != ""
        assert len(rlm_result.sub_query_trace) >= 1

        Path(tmp).unlink()

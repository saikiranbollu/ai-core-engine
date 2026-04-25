"""
Sprint 5 Integration Tests — RLM Orchestrator & Ephemeral Sandbox
==================================================================
Validates:
    1. RLM complexity routing heuristic (should_use_rlm)
    2. RLMOrchestrator plan generation (without LLM)
    3. RLMOrchestrator preview mode
    4. RLMOrchestrator run with mock search
    5. Sandbox tools registered in TOOL_TIERS
    6. RLM tools registered in TOOL_TIERS
    7. Task type planning prompts coverage
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.HybridRAG.code.querier.rlm_orchestrator import (
    RLMOrchestrator,
    should_use_rlm,
    _PLAN_CONTEXT,
    _SYNTH_INSTRUCTIONS,
    MAX_STEPS,
    REGISTER_KEYWORDS,
    ASIL_KEYWORDS,
)


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: Complexity Routing Heuristic
# ═════════════════════════════════════════════════════════════════════════

class TestComplexityRouting:
    """Verify should_use_rlm correctly identifies complex queries."""

    def test_simple_query_no_rlm(self):
        """Single-function query should NOT trigger RLM."""
        assert should_use_rlm("What does Adc_Init do?") is False

    def test_multiple_functions_triggers(self):
        """3+ function names should trigger RLM (signal 1)."""
        query = "Compare IfxCan_Can_initModule, IfxCan_Can_sendMessage, and IfxCan_Can_receiveMessage"
        assert should_use_rlm(query) is True

    def test_register_keywords_trigger(self):
        """Register-level keywords contribute a signal."""
        query = "How do I configure the SFR register for DMA interrupt"
        # register + sfr + dma + interrupt = signal 2
        has_reg = any(kw in query.lower() for kw in REGISTER_KEYWORDS)
        assert has_reg is True

    def test_asil_keywords_trigger(self):
        """ASIL keywords contribute a signal."""
        query = "Generate ASIL-D safety-critical code for ADC"
        has_asil = any(kw in query.lower() for kw in ASIL_KEYWORDS)
        assert has_asil is True

    def test_traceability_task_always_rlm(self):
        """Traceability task type should always use RLM."""
        assert should_use_rlm("Show traces", task_type="traceability") is True

    def test_debug_analysis_always_rlm(self):
        """Debug analysis task type should always use RLM."""
        assert should_use_rlm("Debug this", task_type="debug_analysis") is True

    def test_generic_short_query_no_rlm(self):
        """Generic short query should NOT trigger RLM."""
        assert should_use_rlm("hello", task_type="generic") is False


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: Task Type Planning Prompts
# ═════════════════════════════════════════════════════════════════════════

class TestTaskTypeCoverage:
    """Verify all task types have planning prompts and synthesis instructions."""

    def test_plan_context_has_entries(self):
        assert len(_PLAN_CONTEXT) >= 20, f"Expected 20+ task types, got {len(_PLAN_CONTEXT)}"

    def test_synth_instructions_has_entries(self):
        assert len(_SYNTH_INSTRUCTIONS) >= 20

    def test_all_plan_contexts_have_synth(self):
        """Every plan context key should have a corresponding synthesis instruction."""
        for key in _PLAN_CONTEXT:
            assert key in _SYNTH_INSTRUCTIONS, f"Missing synth for task type: {key}"

    def test_generic_task_type_exists(self):
        assert "generic" in _PLAN_CONTEXT
        assert "generic" in _SYNTH_INSTRUCTIONS

    def test_code_generation_task_exists(self):
        assert "code_generation" in _PLAN_CONTEXT

    def test_test_generation_task_exists(self):
        assert "test_generation" in _PLAN_CONTEXT

    def test_requirement_review_task_exists(self):
        assert "requirement_review" in _PLAN_CONTEXT


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: RLM Orchestrator with Mock LLM
# ═════════════════════════════════════════════════════════════════════════

class TestRLMOrchestratorMocked:
    """Test RLM orchestrator with mocked LLM and search."""

    def _mock_llm(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Return a valid JSON plan."""
        return json.dumps({
            "reasoning": "Test plan",
            "steps": [
                {"step_id": 1, "intent": "Find API functions", "query": "ADC API functions", "alpha": 0.3},
                {"step_id": 2, "intent": "Find requirements", "query": "ADC requirements", "alpha": 0.7},
            ]
        })

    def _mock_search(self, query="", max_results=10, alpha=0.5, workspace_id="illd"):
        """Return mock search results."""
        return [
            {"node_id": "Adc_Init", "node_type": "APIFunction", "score": 0.9,
             "content": "Adc_Init initializes the ADC module", "source": "neo4j",
             "properties": {"name": "Adc_Init"}},
        ]

    def test_preview_returns_plan(self):
        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=self._mock_search, llm_fn=self._mock_llm)
        result = rlm.preview("Generate ADC init code", task_type="code_generation")
        assert "plan" in result
        assert "step_count" in result
        assert result["step_count"] >= 1

    def test_run_returns_context(self):
        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=self._mock_search, llm_fn=self._mock_llm)
        result = rlm.run("Generate ADC init code", task_type="code_generation")
        # RLMContext dataclass
        assert hasattr(result, "steps") or hasattr(result, "plan")

    def test_run_with_progress_callback(self):
        progress_calls = []

        def on_progress(step, total, msg):
            progress_calls.append((step, total, msg))

        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=self._mock_search, llm_fn=self._mock_llm)
        rlm.run("Generate ADC init code", task_type="code_generation",
                on_progress=on_progress)
        assert len(progress_calls) >= 1, "Progress callback should be invoked"

    def test_max_steps_respected(self):
        assert MAX_STEPS == 6, f"MAX_STEPS should be 6, got {MAX_STEPS}"


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Tool Tier Verification
# ═════════════════════════════════════════════════════════════════════════

class TestRLMAndSandboxToolTiers:
    """Verify RLM and Sandbox tools are registered with correct tiers."""

    def test_rlm_tools_developer_tier(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        assert TOOL_TIERS.get("rlm_orchestrate") == "developer"
        assert TOOL_TIERS.get("rlm_plan_preview") == "developer"

    def test_sandbox_tools_exist(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        sandbox_tools = ["sandbox_upload", "sandbox_query", "sandbox_status", "sandbox_clear"]
        for tool in sandbox_tools:
            assert tool in TOOL_TIERS, f"Sandbox tool {tool} not in TOOL_TIERS"

    def test_sandbox_tools_public_tier(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        sandbox_tools = ["sandbox_upload", "sandbox_query", "sandbox_status", "sandbox_clear"]
        for tool in sandbox_tools:
            assert TOOL_TIERS[tool] == "public", f"Sandbox tool {tool} should be public"

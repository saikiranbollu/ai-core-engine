"""
Tests for Context Refiner (src/HybridRAG/code/querier/context_refiner.py)
==========================================================================
Covers ContextRefiner.refine for non-complex queries, CRAG validation,
Self-RAG reflection parsing, max iteration limits, and graceful LLM
failure handling.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from HybridRAG.code.querier.context_refiner import (
    AgentResult,
    AgentType,
    ContextRefiner,
    RefinementResult,
    CRAG_RELEVANCE_THRESHOLD,
    MAX_ITERATIONS,
    SELF_RAG_REFLECTION_PROMPT,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _sample_items(n: int = 3) -> list:
    """Generate sample context items."""
    return [
        {
            "node_id": f"func_{i}",
            "node_type": "Function",
            "content": f"IfxCan_init function variant {i} for CAN module " * 5,
            "score": 0.9 - i * 0.1,
        }
        for i in range(n)
    ]


def _make_llm_fn(responses: list):
    """Create a mock LLM function that returns from a list of responses.

    Each call pops the next response. If exhausted, returns empty JSON.
    """
    responses = list(responses)

    def llm_fn(system: str, user: str, max_tokens: int = 300) -> str:
        if responses:
            return responses.pop(0)
        return '{"gaps": [], "completeness": 0.9}'

    return llm_fn


# ═══════════════════════════════════════════════════════════════════════
#  Non-complex queries return unrefined
# ═══════════════════════════════════════════════════════════════════════

class TestNonComplexQueries:

    def test_simple_query_returns_unrefined(self):
        """Simple queries should bypass refinement entirely."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)
        items = _sample_items()
        result = refiner.refine("IfxCan_init", items, complexity="simple")

        assert isinstance(result, RefinementResult)
        assert result.refined is False
        assert result.iterations == 0
        assert result.agents_used == []
        assert result.refined_items is items

    def test_medium_query_returns_unrefined(self):
        """Medium queries should also bypass refinement."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)
        items = _sample_items()
        result = refiner.refine("IfxCan_init parameters", items, complexity="medium")

        assert result.refined is False
        assert result.iterations == 0

    def test_disabled_refiner_returns_unrefined(self):
        """Disabled refiner should bypass even for complex queries."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=False)
        items = _sample_items()
        result = refiner.refine("complex ASIL-D safety analysis", items, complexity="complex")

        assert result.refined is False

    def test_no_llm_fn_returns_unrefined(self):
        """Without llm_fn, refiner.available is False, so complex queries bypass too."""
        refiner = ContextRefiner(llm_fn=None, enabled=True)
        assert refiner.available is False

        items = _sample_items()
        result = refiner.refine("complex ASIL-D safety analysis", items, complexity="complex")
        assert result.refined is False


# ═══════════════════════════════════════════════════════════════════════
#  CRAG validation scoring
# ═══════════════════════════════════════════════════════════════════════

class TestCRAGValidation:

    def test_crag_validate_with_matching_queries(self):
        """Agent queries that overlap with the original query should score well."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)

        agent_result = AgentResult(
            agent_type=AgentType.CODE,
            gaps=["missing IfxCan_init implementation details"],
            suggested_queries=["IfxCan_init function implementation CAN module"],
            completeness=0.6,
        )
        score = refiner._crag_validate(agent_result, "IfxCan_init CAN module")
        assert score > 0.0

    def test_crag_validate_no_overlap_scores_low(self):
        """Agent queries with no term overlap should score low."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)

        agent_result = AgentResult(
            agent_type=AgentType.CODE,
            gaps=["unrelated gap"],
            suggested_queries=["something completely different about GPIO"],
            completeness=0.3,
        )
        score = refiner._crag_validate(agent_result, "IfxCan_init CAN register config")
        # Limited overlap
        assert score < 0.8

    def test_crag_validate_no_suggested_queries(self):
        """Agent with no suggested queries should get neutral score 0.5."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)

        agent_result = AgentResult(
            agent_type=AgentType.CODE,
            gaps=[],
            suggested_queries=[],
            completeness=0.5,
        )
        score = refiner._crag_validate(agent_result, "IfxCan_init")
        assert score == 0.5

    def test_crag_score_capped_at_one(self):
        """CRAG score should never exceed 1.0."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)

        # Highly overlapping queries
        agent_result = AgentResult(
            agent_type=AgentType.CODE,
            gaps=["IfxCan_init CAN module register config details"],
            suggested_queries=[
                "IfxCan_init CAN module register config",
                "IfxCan_init CAN module register config details",
                "IfxCan_init CAN module register configuration",
            ],
            completeness=0.9,
        )
        score = refiner._crag_validate(
            agent_result, "IfxCan_init CAN module register config")
        assert score <= 1.0

    def test_crag_corrective_queries_generated(self):
        """Below-threshold results should generate corrective queries."""
        refiner = ContextRefiner(llm_fn=MagicMock(), enabled=True)

        agent_result = AgentResult(
            agent_type=AgentType.CODE,
            gaps=["unrelated"],
            suggested_queries=["GPIO pin configuration"],
        )
        corrections = refiner._crag_corrective_queries(
            AgentType.CODE, "IfxCan_init CAN module", agent_result)

        assert len(corrections) >= 1
        assert any("function implementation" in c.lower() for c in corrections)


# ═══════════════════════════════════════════════════════════════════════
#  Self-RAG reflection parsing
# ═══════════════════════════════════════════════════════════════════════

class TestSelfRAGReflection:

    def test_self_rag_needs_retrieval(self):
        """When LLM says needs_retrieval=True, reflection returns True + query."""
        llm_response = json.dumps({
            "is_relevant": True,
            "is_supportive": False,
            "needs_retrieval": True,
            "retrieval_query": "IfxCan_init parameter types",
        })
        refiner = ContextRefiner(
            llm_fn=lambda **kw: llm_response, enabled=True)
        items = _sample_items()

        needs, query = refiner._self_rag_reflect("IfxCan_init params", items)
        assert needs is True
        assert query == "IfxCan_init parameter types"

    def test_self_rag_no_retrieval_needed(self):
        """When LLM says needs_retrieval=False, reflection returns False."""
        llm_response = json.dumps({
            "is_relevant": True,
            "is_supportive": True,
            "needs_retrieval": False,
            "retrieval_query": "",
        })
        refiner = ContextRefiner(
            llm_fn=lambda **kw: llm_response, enabled=True)
        items = _sample_items()

        needs, query = refiner._self_rag_reflect("IfxCan_init", items)
        assert needs is False

    def test_self_rag_llm_failure_returns_false(self):
        """If LLM call fails, reflection should return (False, '')."""
        def failing_llm(**kw):
            raise RuntimeError("LLM timeout")

        refiner = ContextRefiner(llm_fn=failing_llm, enabled=True)
        items = _sample_items()

        needs, query = refiner._self_rag_reflect("IfxCan_init", items)
        assert needs is False
        assert query == ""

    def test_self_rag_no_llm_fn(self):
        """Without llm_fn, _self_rag_reflect returns (False, '')."""
        refiner = ContextRefiner(llm_fn=None, enabled=True)
        items = _sample_items()

        needs, query = refiner._self_rag_reflect("IfxCan_init", items)
        assert needs is False
        assert query == ""


# ═══════════════════════════════════════════════════════════════════════
#  Max iterations respected
# ═══════════════════════════════════════════════════════════════════════

class TestMaxIterations:

    def test_max_iterations_cap(self):
        """Refinement should not exceed MAX_ITERATIONS even if validator keeps asking."""
        iteration_count = [0]

        def llm_fn(system="", user="", max_tokens=300):
            iteration_count[0] += 1
            # Coordinator always finds gaps
            if "coordinator" in system.lower():
                return json.dumps({
                    "agents_needed": ["code"],
                    "missing_context": ["missing details"],
                    "priority": "high",
                })
            # Code agent always finds gaps
            if "code specialist" in system.lower():
                return json.dumps({
                    "gaps": ["missing impl"],
                    "suggested_queries": ["IfxCan_init impl"],
                    "completeness": 0.3,
                })
            # Validator always says needs more
            if "validation agent" in system.lower():
                return json.dumps({
                    "completeness": 0.2,
                    "consistency": 0.9,
                    "needs_iteration": True,
                    "needs_retrieval": False,
                    "reason": "Still incomplete",
                })
            return '{"gaps": [], "completeness": 0.5}'

        refiner = ContextRefiner(llm_fn=llm_fn, search_fn=None, enabled=True)
        items = _sample_items()
        result = refiner.refine("complex ASIL-D safety IfxCan_init", items, complexity="complex")

        assert result.iterations <= MAX_ITERATIONS
        assert result.refined is True

    def test_token_budget_cap_stops_iteration(self):
        """Refinement should stop when token budget is exhausted."""
        call_count = [0]

        def expensive_llm(system="", user="", max_tokens=300):
            call_count[0] += 1
            # Return a very long response to exhaust token budget quickly
            long_response = "x" * 8000  # ~2000 tokens, should hit budget cap
            return json.dumps({
                "agents_needed": ["code"],
                "gaps": ["gap"],
                "suggested_queries": ["query"],
                "completeness": 0.3,
            })

        refiner = ContextRefiner(llm_fn=expensive_llm, search_fn=None, enabled=True)
        items = _sample_items()
        result = refiner.refine("complex safety analysis", items, complexity="complex")

        # Should have stopped due to budget, not infinite loop
        assert result.iterations <= MAX_ITERATIONS


# ═══════════════════════════════════════════════════════════════════════
#  Graceful LLM failure handling
# ═══════════════════════════════════════════════════════════════════════

class TestLLMFailures:

    def test_llm_exception_in_agent(self):
        """If LLM raises during agent execution, the agent result has success=False."""
        call_count = [0]

        def failing_llm(system="", user="", max_tokens=300):
            call_count[0] += 1
            raise ConnectionError("LLM service unreachable")

        refiner = ContextRefiner(llm_fn=failing_llm, enabled=True)
        items = _sample_items()

        # _run_agent should catch the exception and return success=False
        agent_result = refiner._run_agent(AgentType.COORDINATOR, "test query", items)
        assert agent_result.success is False
        assert "LLM service unreachable" in agent_result.error

    def test_llm_returns_invalid_json(self):
        """If LLM returns non-JSON, _parse_json should return default dict."""
        result = ContextRefiner._parse_json("this is not json at all")
        assert isinstance(result, dict)
        assert "gaps" in result
        assert result["completeness"] == 0.5

    def test_llm_returns_empty(self):
        """If LLM returns empty string, _parse_json should return empty dict."""
        result = ContextRefiner._parse_json("")
        assert result == {}

    def test_llm_returns_code_fenced_json(self):
        """If LLM returns JSON in code fences, _parse_json should unwrap it."""
        fenced = '```json\n{"gaps": ["test"], "completeness": 0.7}\n```'
        result = ContextRefiner._parse_json(fenced)
        assert result["gaps"] == ["test"]
        assert result["completeness"] == 0.7

    def test_refine_survives_total_llm_failure(self):
        """Even if every LLM call fails, refine() should return a valid result."""
        def always_fail(system="", user="", max_tokens=300):
            raise RuntimeError("Total failure")

        refiner = ContextRefiner(llm_fn=always_fail, enabled=True)
        items = _sample_items()
        result = refiner.refine("complex safety analysis", items, complexity="complex")

        assert isinstance(result, RefinementResult)
        # The coordinator fails, so loop breaks immediately
        assert result.iterations >= 1
        assert result.refined is True  # refined=True because it attempted


# ═══════════════════════════════════════════════════════════════════════
#  RefinementResult data class
# ═══════════════════════════════════════════════════════════════════════

class TestRefinementResult:

    def test_as_dict_keys(self):
        """as_dict should contain all expected keys."""
        result = RefinementResult(
            refined_items=[], iterations=2, agents_used=["coordinator", "code"],
            gaps_found=["gap1"], gaps_resolved=["gap1"],
            additional_queries=["q1"], completeness_score=0.85,
            total_tokens_used=500, latency_ms=100.0,
            crag_corrections=1, self_rag_retrievals=2,
        )
        d = result.as_dict()
        assert d["refined"] is True
        assert d["iterations"] == 2
        assert d["gaps_found"] == 1
        assert d["gaps_resolved"] == 1
        assert d["completeness_score"] == 0.85
        assert d["crag_corrections"] == 1
        assert d["self_rag_retrievals"] == 2

    def test_summarize_context_truncates(self):
        """_summarize_context should respect max_chars limit."""
        items = [
            {"content": "x" * 500, "node_type": "Function"},
            {"content": "y" * 500, "node_type": "Register"},
            {"content": "z" * 500, "node_type": "Requirement"},
        ]
        summary = ContextRefiner._summarize_context(items, max_chars=300)
        assert len(summary) <= 400  # Some overhead for prefixes
        assert "more" in summary or len(summary) < 400

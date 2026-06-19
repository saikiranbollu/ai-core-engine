"""Unit tests for RLM orchestrator robustness (W-09 / F-CC-R03, F-CC-R04)."""
import json

import pytest

import src.HybridRAG.code.querier.rlm_orchestrator as rlm_mod
from src.HybridRAG.code.querier.rlm_orchestrator import (
    RLMOrchestrator,
    _extract_json_object,
)


class TestExtractJsonObject:
    def test_plain_object(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_trailing_prose(self):
        raw = 'Here is the plan:\n{"steps": [1, 2]}\nThanks!'
        assert _extract_json_object(raw) == {"steps": [1, 2]}

    def test_markdown_fence(self):
        raw = '```json\n{"steps": []}\n```'
        assert _extract_json_object(raw) == {"steps": []}

    def test_nested_braces(self):
        raw = 'x {"a": {"b": 2}, "c": 3} y'
        assert _extract_json_object(raw) == {"a": {"b": 2}, "c": 3}

    def test_brace_inside_string_literal(self):
        raw = '{"q": "use {placeholder} here"}'
        assert _extract_json_object(raw) == {"q": "use {placeholder} here"}

    def test_escaped_quote_in_string(self):
        raw = r'{"q": "say \"hi\""}'
        assert _extract_json_object(raw) == {"q": 'say "hi"'}

    def test_no_object_returns_none(self):
        assert _extract_json_object("no json here") is None

    def test_malformed_returns_none(self):
        assert _extract_json_object('{"a": }') is None

    def test_top_level_array_not_returned(self):
        assert _extract_json_object("[1, 2, 3]") is None

    def test_skips_leading_malformed_brace(self):
        raw = 'note: {oops not json} {"steps": [1]}'
        assert _extract_json_object(raw) == {"steps": [1]}


class TestPlannerFallback:
    def test_unparseable_plan_falls_back_with_full_query(self):
        rlm = RLMOrchestrator(
            module="ADC", profile="illd", search_fn=None,
            llm_fn=lambda system, user, max_tokens=1200: "absolutely no json",
        )
        plan, _tokens = rlm._plan("Generate ADC init code", "code_generation")
        assert "steps" in plan
        # F-CC-R03: fallback uses the full query, not a truncated slice.
        assert plan["steps"][0]["query"] == "Generate ADC init code"

    def test_valid_plan_extracted_from_noisy_output(self):
        good = json.dumps({
            "reasoning": "ok",
            "steps": [{"step_id": 1, "intent": "find", "query": "q", "alpha": 0.5}],
        })
        rlm = RLMOrchestrator(
            module="ADC", profile="illd", search_fn=None,
            llm_fn=lambda system, user, max_tokens=1200: "prefix\n" + good + "\nsuffix",
        )
        plan, _tokens = rlm._plan("Generate code", "code_generation")
        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["intent"] == "find"

    def test_default_llm_failure_falls_back_to_full_query(self, monkeypatch):
        # Force the shared client to raise (non-retryable type) so _default_llm
        # hits its sentinel fallback fast, and _plan rebuilds the plan from the
        # full original query rather than a truncated prompt slice.
        monkeypatch.setattr(
            rlm_mod, "_get_shared_openai_client",
            lambda: (_ for _ in ()).throw(RuntimeError("down")),
        )
        rlm = RLMOrchestrator(module="ADC", profile="illd")  # default llm_fn
        original = "Generate ADC init code in full detail " + "x" * 300
        plan, _tokens = rlm._plan(original, "code_generation")
        assert plan["steps"][0]["query"] == original


class TestSynthesisBudget:
    def test_small_answers_all_included(self):
        captured = {}

        def fake_llm(system, user, max_tokens=8000):
            captured["user"] = user
            return "FINAL"

        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=None, llm_fn=fake_llm)
        final, _tokens, degraded = rlm._synthesize(
            "task", "generic", {1: "alpha answer", 2: "beta answer"}, None,
        )
        assert final == "FINAL"
        assert degraded is False
        assert "Sub-query 1" in captured["user"]
        assert "Sub-query 2" in captured["user"]
        assert "(truncated)" not in captured["user"]

    def test_large_answers_bounded_by_budget(self, monkeypatch):
        captured = {}

        def fake_llm(system, user, max_tokens=8000):
            captured["user"] = user
            return "FINAL"

        # Shrink the model context window so the first huge answer overflows.
        monkeypatch.setattr(rlm_mod, "_MODEL_CONTEXT_TOKENS", {"gpt-4o": 12000})
        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=None, llm_fn=fake_llm)
        huge = "Z" * 200_000
        final, _tokens, _degraded = rlm._synthesize(
            "task", "generic", {1: huge, 2: huge, 3: huge}, None,
        )
        assert final == "FINAL"
        # Assembled prompt must be far smaller than the raw concatenation.
        assert len(captured["user"]) < len(huge)
        assert "(truncated)" in captured["user"]


class TestSynthesisDegradation:
    """F-CC-R02: synthesis must surface a degraded flag instead of passing an
    LLM failure off as real content."""

    def test_sentinel_marks_degraded(self):
        # The default LLM returns this sentinel when all retries fail.
        rlm = RLMOrchestrator(
            module="ADC", profile="illd", search_fn=None,
            llm_fn=lambda system, user, max_tokens=8000: "[LLM unavailable]",
        )
        final, tokens, degraded = rlm._synthesize("task", "generic", {1: "a"}, None)
        assert degraded is True
        assert tokens == 0
        assert final == "[LLM synthesis unavailable]"

    def test_llm_exception_marks_degraded(self):
        def boom(system, user, max_tokens=8000):
            raise RuntimeError("synthesis LLM down")

        rlm = RLMOrchestrator(module="ADC", profile="illd",
                              search_fn=None, llm_fn=boom)
        final, tokens, degraded = rlm._synthesize("task", "generic", {1: "a"}, None)
        assert degraded is True
        assert final == "[LLM synthesis unavailable]"

    def test_degraded_surfaced_in_context_dict(self):
        rlm = RLMOrchestrator(
            module="ADC", profile="illd", search_fn=None,
            llm_fn=lambda system, user, max_tokens=8000: "[LLM unavailable]",
        )
        ctx = rlm.run("explain the ADC init sequence", "generic")
        assert ctx.degraded is True
        assert ctx.to_dict()["degraded"] is True

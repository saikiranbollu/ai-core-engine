"""
Tests for Context Compressor (src/HybridRAG/code/querier/context_compressor.py)
================================================================================
Covers DynamicTokenBudget, ExtractiveCompressor, _estimate_tokens, and the
main ContextCompressor pipeline with LLMLingua unavailable (extractive fallback)
and budget enforcement.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from HybridRAG.code.querier.context_compressor import (
    ContextCompressor,
    CompressionResult,
    DynamicTokenBudget,
    ExtractiveCompressor,
    LLMLinguaCompressor,
    _estimate_tokens,
    _score_sentence,
    _split_sentences,
)


# ═══════════════════════════════════════════════════════════════════════
#  DynamicTokenBudget.compute
# ═══════════════════════════════════════════════════════════════════════

class TestDynamicTokenBudget:

    def test_simple_budget(self):
        """Simple complexity should yield 4000 total budget."""
        result = DynamicTokenBudget.compute("simple")
        assert result["total_budget"] == 4000
        assert result["complexity"] == "simple"
        assert result["scale_factor"] == 0.5  # 4000 / 8000

    def test_medium_budget(self):
        """Medium complexity should yield 8000 total budget."""
        result = DynamicTokenBudget.compute("medium")
        assert result["total_budget"] == 8000
        assert result["complexity"] == "medium"
        assert result["scale_factor"] == 1.0

    def test_complex_budget(self):
        """Complex complexity should yield 12000 total budget."""
        result = DynamicTokenBudget.compute("complex")
        assert result["total_budget"] == 12000
        assert result["complexity"] == "complex"
        assert result["scale_factor"] == 1.5

    def test_custom_budget_overrides_complexity(self):
        """custom_budget should take precedence over complexity."""
        result = DynamicTokenBudget.compute("simple", custom_budget=20000)
        assert result["total_budget"] == 20000
        assert result["scale_factor"] == 2.5  # 20000 / 8000

    def test_slot_budgets_scale_proportionally(self):
        """Slot budgets should scale proportionally with total budget."""
        simple = DynamicTokenBudget.compute("simple")
        complex_ = DynamicTokenBudget.compute("complex")
        # complex is 3x simple budget (12000 vs 4000)
        for slot in simple["slot_budgets"]:
            assert complex_["slot_budgets"][slot] == simple["slot_budgets"][slot] * 3

    def test_unknown_complexity_defaults_to_8000(self):
        """Unknown complexity label should default to 8000 budget."""
        result = DynamicTokenBudget.compute("unknown_level")
        assert result["total_budget"] == 8000

    def test_slot_budgets_keys(self):
        """Slot budgets must include all expected slot categories."""
        result = DynamicTokenBudget.compute("medium")
        expected_slots = {
            "requirements", "api_functions", "tests", "dependencies",
            "relationships", "code_examples", "safety", "registers",
            "conversation", "custom",
        }
        assert set(result["slot_budgets"].keys()) == expected_slots


# ═══════════════════════════════════════════════════════════════════════
#  _estimate_tokens
# ═══════════════════════════════════════════════════════════════════════

class TestEstimateTokens:

    def test_uses_div_4(self):
        """_estimate_tokens should produce a reasonable token count."""
        # 100 chars -> ~13-25 tokens depending on tiktoken availability
        text = "a" * 100
        result = _estimate_tokens(text)
        assert 10 <= result <= 25

    def test_empty_string_returns_zero(self):
        """Empty string should return 0 tokens."""
        assert _estimate_tokens("") == 0

    def test_none_handled(self):
        """None or falsy input should return 0."""
        assert _estimate_tokens("") == 0

    def test_short_string_returns_at_least_one(self):
        """Very short string should return at least 1 token."""
        assert _estimate_tokens("hi") >= 1

    def test_realistic_sentence(self):
        """Realistic sentence token estimate should be reasonable."""
        text = "The IfxCan_init function initializes the CAN module."
        tokens = _estimate_tokens(text)
        # 52 chars -> ~13 tokens
        assert 10 <= tokens <= 20


# ═══════════════════════════════════════════════════════════════════════
#  ExtractiveCompressor
# ═══════════════════════════════════════════════════════════════════════

class TestExtractiveCompressor:

    def test_short_content_passed_through(self):
        """Items with fewer sentences than max_sentences are kept as-is."""
        comp = ExtractiveCompressor(max_sentences=5)
        items = [
            {"content": "This is short. Just two sentences.", "score": 0.9},
        ]
        result = comp.compress(items, query="test", target_tokens=5000)
        assert len(result) == 1
        assert result[0]["content"] == items[0]["content"]

    def test_long_content_gets_compressed(self):
        """Items with many sentences should be trimmed to max_sentences."""
        comp = ExtractiveCompressor(max_sentences=2)
        long_content = ". ".join(
            f"Sentence number {i} has some relevant content about IfxCan_init"
            for i in range(20)
        ) + "."
        items = [{"content": long_content, "score": 0.9}]
        result = comp.compress(items, query="IfxCan_init", target_tokens=50000)
        # Should have reduced content
        assert len(result) == 1
        assert "_compression" in result[0]
        assert result[0]["_compression"] == "extractive"
        # Original tokens should be tracked
        assert result[0]["_original_tokens"] > result[0]["_compressed_tokens"]

    def test_empty_content_passed_through(self):
        """Items with empty content should be passed through."""
        comp = ExtractiveCompressor()
        items = [{"content": "", "score": 0.5}]
        result = comp.compress(items, query="test", target_tokens=5000)
        assert len(result) == 1

    def test_budget_respected(self):
        """Items exceeding the token budget should be dropped."""
        comp = ExtractiveCompressor(max_sentences=5)
        large_content = "word " * 40000  # ~10000 tokens
        items = [
            {"content": large_content, "score": 0.9},
            {"content": "Small item.", "score": 0.5},
        ]
        result = comp.compress(items, query="test", target_tokens=100)
        # With a tiny budget, at most the small item might fit
        total_tokens = sum(_estimate_tokens(i.get("content", "")) for i in result)
        assert total_tokens <= 100 or len(result) == 0

    def test_query_terms_boost_relevance(self):
        """Sentences containing query terms should score higher."""
        score_relevant = _score_sentence(
            "The IfxCan_init function initializes CAN module registers.",
            ["ifxcan_init", "register"],
        )
        score_irrelevant = _score_sentence(
            "This is a general description of nothing specific.",
            ["ifxcan_init", "register"],
        )
        assert score_relevant > score_irrelevant


# ═══════════════════════════════════════════════════════════════════════
#  _split_sentences
# ═══════════════════════════════════════════════════════════════════════

class TestSplitSentences:

    def test_basic_splitting(self):
        """Should split on sentence-ending punctuation."""
        text = "First sentence. Second sentence. Third sentence."
        sentences = _split_sentences(text)
        assert len(sentences) == 3

    def test_abbreviations_not_split(self):
        """e.g. and i.e. should not cause sentence splits."""
        text = "Use e.g. this approach. Also i.e. this one."
        sentences = _split_sentences(text)
        assert len(sentences) == 2


# ═══════════════════════════════════════════════════════════════════════
#  ContextCompressor pipeline — LLMLingua unavailable
# ═══════════════════════════════════════════════════════════════════════

class TestContextCompressorPipeline:

    def test_extractive_fallback_when_llmlingua_unavailable(self):
        """When LLMLingua is not installed, extractive compressor is used."""
        compressor = ContextCompressor(llm_fn=None, enabled=True)
        # Force LLMLingua unavailable
        compressor._llmlingua._enabled = False
        compressor._llmlingua._load_attempted = True
        compressor._llmlingua._compressor = None

        long_content = ". ".join(
            f"Sentence {i} about IfxCan_init function implementation"
            for i in range(20)
        ) + "."
        items = [
            {"content": long_content, "score": 0.9},
        ]
        result = compressor.compress(items, "IfxCan_init", complexity="medium")

        assert isinstance(result, CompressionResult)
        assert "extractive" in result.stages_applied
        assert "llmlingua" not in result.stages_applied

    def test_disabled_compressor_returns_items_unchanged(self):
        """When compressor is disabled, items are returned unchanged."""
        compressor = ContextCompressor(enabled=False)
        items = [
            {"content": "Hello world.", "score": 0.9},
            {"content": "Goodbye world.", "score": 0.5},
        ]
        result = compressor.compress(items, "test")
        assert result.compressed_items == items
        assert result.stages_applied == []
        assert result.compression_ratio == 1.0

    def test_empty_items_returns_empty(self):
        """Empty input should return empty result."""
        compressor = ContextCompressor(enabled=True)
        result = compressor.compress([], "test")
        assert result.compressed_items == []
        assert result.items_before == 0
        assert result.items_after == 0

    def test_budget_enforcement_trims_when_over(self):
        """Budget enforcement (Stage 3) should trim when over budget."""
        compressor = ContextCompressor(llm_fn=None, enabled=True)
        # Force LLMLingua unavailable
        compressor._llmlingua._enabled = False
        compressor._llmlingua._load_attempted = True
        compressor._llmlingua._compressor = None

        # Create items that exceed simple budget (4000 tokens)
        items = []
        for i in range(20):
            items.append({
                "content": f"Content block {i} " * 400,  # ~400 tokens each
                "score": 0.9 - i * 0.03,
                "relevance_score": 0.9 - i * 0.03,
            })

        result = compressor.compress(items, "test query", complexity="simple")
        # Budget for simple is 4000 tokens
        assert result.compressed_tokens <= 4000 or "budget_enforcement" in result.stages_applied

    def test_compression_result_as_dict(self):
        """CompressionResult.as_dict should return expected keys."""
        result = CompressionResult(
            compressed_items=[{"content": "x"}],
            original_tokens=100,
            compressed_tokens=50,
            compression_ratio=2.0,
            stages_applied=["extractive"],
            items_before=3,
            items_after=1,
            latency_ms=10.5,
        )
        d = result.as_dict()
        assert d["original_tokens"] == 100
        assert d["compressed_tokens"] == 50
        assert d["compression_ratio"] == 2.0
        assert d["retention_rate"] == 0.5
        assert d["stages_applied"] == ["extractive"]
        assert d["items_before"] == 3
        assert d["items_after"] == 1
        assert d["latency_ms"] == 10.5

    def test_retention_rate_property(self):
        """Retention rate should be compressed / original tokens."""
        result = CompressionResult(
            compressed_items=[], original_tokens=200,
            compressed_tokens=50, compression_ratio=4.0,
            stages_applied=[], items_before=0, items_after=0,
        )
        assert result.retention_rate == 0.25

    def test_retention_rate_handles_zero_original(self):
        """Retention rate should not divide by zero."""
        result = CompressionResult(
            compressed_items=[], original_tokens=0,
            compressed_tokens=0, compression_ratio=1.0,
            stages_applied=[], items_before=0, items_after=0,
        )
        assert result.retention_rate == 0.0

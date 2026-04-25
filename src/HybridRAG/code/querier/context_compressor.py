"""
Advanced Context Compressor — GAP-A04 + GAP-A09 (Research Upgrade v2)
======================================================================
**Research Upgrade:** Replaced extractive sentence heuristics with
microsoft/LongLLMLingua perplexity-based token pruning.

LongLLMLingua uses a small efficient LM to calculate per-token perplexity
conditioned on the query. Low-perplexity tokens (redundant boilerplate)
are pruned; high-perplexity tokens (unique identifiers like IfxCan_init,
register addresses 0xF0036000, requirement IDs) are preserved by
mathematical principle — not regex heuristics.

Pipeline (3-stage):
  Stage 1: LongLLMLingua perplexity-based pruning (preferred)
           Fallback: extractive sentence selection (if LLMLingua unavailable)
  Stage 2: GPT4IFX abstractive compression (optional, for large contexts)
  Stage 3: Dynamic token budget enforcement

Dynamic Token Budget (GAP-A09):
  simple → 4K, medium → 8K, complex → 12K tokens
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

COMPRESSOR_ENABLED = os.getenv("CONTEXT_COMPRESSOR_ENABLED", "true").lower() == "true"
ABSTRACTIVE_ENABLED = os.getenv("ABSTRACTIVE_COMPRESSION_ENABLED", "true").lower() == "true"
LLMLINGUA_ENABLED = os.getenv("LLMLINGUA_ENABLED", "true").lower() == "true"
LLMLINGUA_MODEL = os.getenv("LLMLINGUA_MODEL", "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank")
LLMLINGUA_TARGET_RATIO = float(os.getenv("LLMLINGUA_TARGET_RATIO", "0.33"))
EXTRACTIVE_SENTENCE_LIMIT = int(os.getenv("EXTRACTIVE_SENTENCE_LIMIT", "5"))


# ═══════════════════════════════════════════════════════════════════════
#  Dynamic Token Budget (GAP-A09)
# ═══════════════════════════════════════════════════════════════════════

class DynamicTokenBudget:
    _BASE_TOTAL = 8000
    _BASE_SLOTS = {
        "requirements": 3000, "api_functions": 5000, "tests": 3000,
        "dependencies": 2500, "relationships": 1500, "code_examples": 4000,
        "safety": 1200, "registers": 3000, "conversation": 300, "custom": 1000,
    }
    _COMPLEXITY_BUDGETS = {"simple": 4000, "medium": 8000, "complex": 12000}

    @classmethod
    def compute(cls, complexity: str = "medium", custom_budget: Optional[int] = None) -> Dict[str, Any]:
        total = custom_budget or cls._COMPLEXITY_BUDGETS.get(complexity, 8000)
        scale = total / cls._BASE_TOTAL
        return {
            "total_budget": total,
            "slot_budgets": {s: int(b * scale) for s, b in cls._BASE_SLOTS.items()},
            "complexity": complexity,
            "scale_factor": round(scale, 2),
        }


@dataclass
class CompressionResult:
    compressed_items: List[Dict[str, Any]]
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    stages_applied: List[str]
    items_before: int
    items_after: int
    latency_ms: float = 0.0

    @property
    def retention_rate(self) -> float:
        return self.compressed_tokens / max(self.original_tokens, 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "compression_ratio": round(self.compression_ratio, 2),
            "retention_rate": round(self.retention_rate, 2),
            "stages_applied": self.stages_applied,
            "items_before": self.items_before,
            "items_after": self.items_after,
            "latency_ms": round(self.latency_ms, 2),
        }


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)  # M09 fix: ~4 chars/token standard


# ═══════════════════════════════════════════════════════════════════════
#  Stage 1a: LongLLMLingua Compressor (preferred)
# ═══════════════════════════════════════════════════════════════════════

class LLMLinguaCompressor:
    """
    Perplexity-based token pruning via microsoft/LongLLMLingua.

    Tokens with low perplexity (predictable, redundant: articles, transitions,
    boilerplate) are pruned. Tokens with high perplexity (unique: function names,
    register addresses, hex values, requirement IDs) are preserved.

    This is mathematically grounded — not regex heuristics.
    """

    def __init__(self, model_name: str = LLMLINGUA_MODEL,
                 target_ratio: float = LLMLINGUA_TARGET_RATIO,
                 enabled: bool = LLMLINGUA_ENABLED):
        self._model_name = model_name
        self._target_ratio = target_ratio
        self._enabled = enabled
        self._compressor = None
        self._load_attempted = False

    @property
    def available(self) -> bool:
        if not self._enabled:
            return False
        if self._compressor is not None:
            return True
        if not self._load_attempted:
            self._lazy_load()
        return self._compressor is not None

    def _lazy_load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            from llmlingua import PromptCompressor
            start = time.monotonic()
            self._compressor = PromptCompressor(
                model_name=self._model_name,
                use_llmlingua2=True,
                device_map="cpu",  # CPU-only for AICE (no GPU requirement)
            )
            elapsed = (time.monotonic() - start) * 1000
            logger.info("LLMLingua loaded in %.0f ms: %s", elapsed, self._model_name)
        except ImportError:
            logger.info(
                "llmlingua not installed — using extractive fallback. "
                "Install with: pip install llmlingua"
            )
        except Exception as exc:
            logger.warning("LLMLingua init failed: %s", exc)

    def compress(self, items: List[Dict[str, Any]], query: str,
                 target_tokens: int) -> List[Dict[str, Any]]:
        if not self.available or not items:
            return items

        compressed = []
        total_tokens = 0

        for item in items:
            content = item.get("content", "")
            if not content or _estimate_tokens(content) < 30:
                if total_tokens + _estimate_tokens(content) <= target_tokens:
                    compressed.append(item)
                    total_tokens += _estimate_tokens(content)
                continue

            try:
                target = int(_estimate_tokens(content) * self._target_ratio)

                # Try LLMLingua2 API first (simpler, recommended)
                try:
                    result = self._compressor.compress_prompt(
                        context=[content],
                        rate=self._target_ratio,
                        target_token=target,
                    )
                except TypeError:
                    # Fallback to LLMLingua1 API (more params)
                    result = self._compressor.compress_prompt(
                        context=[content],
                        instruction=f"Query: {query}",
                        question="",
                        target_token=target,
                    )

                compressed_text = ""
                if isinstance(result, dict):
                    compressed_text = result.get("compressed_prompt", "")
                    if not compressed_text:
                        compressed_text = result.get("compressed_context", content)
                elif isinstance(result, str):
                    compressed_text = result

                if not compressed_text:
                    compressed_text = content  # safety fallback

                # Remove instruction prefix if LLMLingua prepended it
                for prefix in [f"Query: {query}", query]:
                    if compressed_text.startswith(prefix):
                        compressed_text = compressed_text[len(prefix):].strip()
                        break

                tokens = _estimate_tokens(compressed_text)
                if total_tokens + tokens <= target_tokens:
                    new_item = dict(item)
                    new_item["content"] = compressed_text
                    new_item["_compression"] = "llmlingua"
                    new_item["_original_tokens"] = _estimate_tokens(content)
                    new_item["_compressed_tokens"] = tokens
                    compressed.append(new_item)
                    total_tokens += tokens

            except Exception as exc:
                logger.warning("LLMLingua compression failed for item: %s", exc)
                tokens = _estimate_tokens(content)
                if total_tokens + tokens <= target_tokens:
                    compressed.append(item)
                    total_tokens += tokens

        return compressed


# ═══════════════════════════════════════════════════════════════════════
#  Stage 1b: Extractive Compressor (fallback)
# ═══════════════════════════════════════════════════════════════════════

def _split_sentences(text: str) -> List[str]:
    text = re.sub(r'\b(e\.g|i\.e|etc|vs|Dr|Mr|Mrs|Ms|Sr|Jr)\.',
                  lambda m: m.group().replace('.', '<DOT>'), text)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.replace('<DOT>', '.').strip() for s in sentences if s.strip()]


def _score_sentence(sentence: str, query_terms: List[str]) -> float:
    score = 0.0
    s_lower = sentence.lower()
    for term in query_terms:
        if term in s_lower:
            score += 1.0
    words = len(sentence.split())
    if 10 <= words <= 40:
        score += 0.5
    elif words < 5:
        score -= 0.3
    if re.search(r'\b(?:function|register|struct|enum|typedef|macro)\b', s_lower):
        score += 0.5
    if re.search(r'\bIfx[A-Z]', sentence):
        score += 0.8
    if re.search(r'\b(?:ASIL|ISO|MISRA|AUTOSAR)\b', sentence):
        score += 0.3
    if re.search(r'[{};()]', sentence) or sentence.strip().startswith('#'):
        score += 0.7
    return score


class ExtractiveCompressor:
    """Fallback: sentence-level extractive selection when LLMLingua unavailable."""

    def __init__(self, max_sentences: int = EXTRACTIVE_SENTENCE_LIMIT):
        self._max_sentences = max_sentences

    def compress(self, items: List[Dict[str, Any]], query: str,
                 target_tokens: int) -> List[Dict[str, Any]]:
        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        compressed = []
        total_tokens = 0

        for item in items:
            content = item.get("content", "")
            if not content:
                compressed.append(item)
                continue

            sentences = _split_sentences(content)
            if len(sentences) <= self._max_sentences:
                tokens = _estimate_tokens(content)
                if total_tokens + tokens <= target_tokens:
                    compressed.append(item)
                    total_tokens += tokens
                continue

            scored = [(s, _score_sentence(s, query_terms)) for s in sentences]
            scored.sort(key=lambda x: x[1], reverse=True)
            selected = scored[:self._max_sentences]
            orig_order = {s: i for i, s in enumerate(sentences)}
            selected.sort(key=lambda x: orig_order.get(x[0], 0))

            compressed_content = " ".join(s for s, _ in selected)
            tokens = _estimate_tokens(compressed_content)
            if total_tokens + tokens <= target_tokens:
                new_item = dict(item)
                new_item["content"] = compressed_content
                new_item["_compression"] = "extractive"
                new_item["_original_tokens"] = _estimate_tokens(content)
                new_item["_compressed_tokens"] = tokens
                compressed.append(new_item)
                total_tokens += tokens

        return compressed


# ═══════════════════════════════════════════════════════════════════════
#  Stage 2: Abstractive Compressor (GPT4IFX, optional)
# ═══════════════════════════════════════════════════════════════════════

class AbstractiveCompressor:
    _SYSTEM_PROMPT = """You are a technical documentation compressor for automotive embedded software.
Compress the chunk to preserve ONLY query-relevant information.
Rules: preserve function names, register names, requirement IDs, numerical values,
code snippets verbatim. Remove boilerplate. Keep safety info (ASIL).
Respond ONLY with compressed text."""

    def __init__(self, llm_fn: Optional[Callable] = None,
                 enabled: bool = ABSTRACTIVE_ENABLED):
        self._llm_fn = llm_fn
        self._enabled = enabled

    @property
    def available(self) -> bool:
        return self._enabled and self._llm_fn is not None

    def compress(self, items: List[Dict[str, Any]], query: str,
                 target_tokens: int, max_chunks: int = 10) -> List[Dict[str, Any]]:
        if not self.available:
            return items

        compressed = []
        total_tokens = 0

        for item in items[:max_chunks]:
            content = item.get("content", "")
            current_tokens = _estimate_tokens(content)

            if current_tokens < 100:
                if total_tokens + current_tokens <= target_tokens:
                    compressed.append(item)
                    total_tokens += current_tokens
                continue

            remaining = target_tokens - total_tokens
            items_left = max_chunks - len(compressed)
            per_item = max(50, remaining // max(items_left, 1))

            try:
                result = self._llm_fn(
                    system=self._SYSTEM_PROMPT,
                    user=f"Query: {query}\n\nChunk (~{per_item * 3} chars target):\n{content}",
                    max_tokens=per_item * 2,
                )
                if result and isinstance(result, str):
                    new_item = dict(item)
                    new_item["content"] = result
                    new_item["_compression"] = "abstractive"
                    new_item["_original_tokens"] = current_tokens
                    new_item["_compressed_tokens"] = _estimate_tokens(result)
                    compressed.append(new_item)
                    total_tokens += _estimate_tokens(result)
                else:
                    if total_tokens + current_tokens <= target_tokens:
                        compressed.append(item)
                        total_tokens += current_tokens
            except Exception as exc:
                logger.warning("Abstractive compression failed: %s", exc)
                if total_tokens + current_tokens <= target_tokens:
                    compressed.append(item)
                    total_tokens += current_tokens

        return compressed


# ═══════════════════════════════════════════════════════════════════════
#  Main Pipeline
# ═══════════════════════════════════════════════════════════════════════

class ContextCompressor:
    """
    3-stage context compression pipeline.

    Stage 1: LongLLMLingua perplexity pruning (preferred) / extractive (fallback)
    Stage 2: GPT4IFX abstractive compression (optional, for large contexts)
    Stage 3: Dynamic budget enforcement (hard trim)
    """

    def __init__(self, llm_fn: Optional[Callable] = None,
                 enabled: bool = COMPRESSOR_ENABLED):
        self._llmlingua = LLMLinguaCompressor()
        self._extractive = ExtractiveCompressor()
        self._abstractive = AbstractiveCompressor(llm_fn=llm_fn)
        self._enabled = enabled

    @property
    def available(self) -> bool:
        return self._enabled

    def compress(self, items: List[Dict[str, Any]], query: str,
                 complexity: str = "medium",
                 custom_budget: Optional[int] = None) -> CompressionResult:
        start = time.monotonic()
        stages = []

        budget_info = DynamicTokenBudget.compute(complexity, custom_budget)
        target_tokens = budget_info["total_budget"]

        original_tokens = sum(_estimate_tokens(i.get("content", "")) for i in items)
        items_before = len(items)

        if not self._enabled or not items:
            return CompressionResult(
                compressed_items=items, original_tokens=original_tokens,
                compressed_tokens=original_tokens, compression_ratio=1.0,
                stages_applied=[], items_before=items_before, items_after=len(items))

        current = items

        # Stage 1: Perplexity-based (preferred) or extractive (fallback)
        try:
            if self._llmlingua.available:
                current = self._llmlingua.compress(current, query, target_tokens)
                stages.append("llmlingua")
            else:
                current = self._extractive.compress(current, query, target_tokens)
                stages.append("extractive")
        except Exception as exc:
            logger.warning("Stage 1 compression failed: %s", exc)

        # Stage 2: Abstractive (if still over budget and LLM available)
        current_tokens = sum(_estimate_tokens(i.get("content", "")) for i in current)
        if self._abstractive.available and current_tokens > target_tokens * 1.2:
            try:
                current = self._abstractive.compress(current, query, target_tokens)
                stages.append("abstractive")
            except Exception as exc:
                logger.warning("Stage 2 compression failed: %s", exc)

        # Stage 3: Budget enforcement (hard trim)
        current_tokens = sum(_estimate_tokens(i.get("content", "")) for i in current)
        if current_tokens > target_tokens:
            scored = sorted(current,
                            key=lambda x: x.get("relevance_score", x.get("score", 0)),
                            reverse=True)
            trimmed, running = [], 0
            for item in scored:
                t = _estimate_tokens(item.get("content", ""))
                if running + t <= target_tokens or item.get("_must_include"):
                    trimmed.append(item)
                    running += t
            current = trimmed
            stages.append("budget_enforcement")

        compressed_tokens = sum(_estimate_tokens(i.get("content", "")) for i in current)
        ratio = original_tokens / max(compressed_tokens, 1)
        elapsed = (time.monotonic() - start) * 1000

        logger.info("Compression: %d->%d tokens (%.1fx), %d->%d items, stages=%s, %.0fms",
                    original_tokens, compressed_tokens, ratio,
                    items_before, len(current), "+".join(stages), elapsed)

        return CompressionResult(
            compressed_items=current, original_tokens=original_tokens,
            compressed_tokens=compressed_tokens, compression_ratio=ratio,
            stages_applied=stages, items_before=items_before,
            items_after=len(current), latency_ms=elapsed)

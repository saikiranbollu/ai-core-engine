"""
Relevance Judge — GAP-A08 (Research Upgrade v2)
==================================================
**Research Upgrade:** Replaced custom GPT4IFX 2-metric judge with
confident-ai/deepeval framework providing 14+ evaluation metrics.

Key metrics now available:
  - ContextualRelevancyMetric: Does chunk answer the query?
  - FaithfulnessMetric: Is response grounded in context?
  - HallucinationMetric: Does response contain unsupported claims?
  - BiasMetric: Is response biased?
  - AnswerRelevancyMetric: Is response relevant to query?

Fallback chain:
  1. DeepEval framework (preferred — 14+ metrics, CI/CD integration)
  2. Custom GPT4IFX judge (if deepeval unavailable)
  3. Passthrough (if neither available)

Guard: Only for QUICK/FULL review routes (not AUTO).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

JUDGE_ENABLED = os.getenv("RELEVANCE_JUDGE_ENABLED", "true").lower() == "true"
JUDGE_THRESHOLD = float(os.getenv("RELEVANCE_JUDGE_THRESHOLD", "0.5"))
JUDGE_TOP_N = int(os.getenv("RELEVANCE_JUDGE_TOP_N", "10"))
JUDGE_BACKEND = os.getenv("RELEVANCE_JUDGE_BACKEND", "deepeval")
DEEPEVAL_MAX_WORKERS = int(os.getenv("DEEPEVAL_MAX_WORKERS", "4"))


@dataclass
class ChunkJudgment:
    chunk_index: int
    relevancy_score: float = 0.0
    faithfulness_score: float = 0.0
    hallucination_score: float = 0.0
    overall_score: float = 0.0
    reasoning: str = ""
    keep: bool = True


@dataclass
class JudgeResult:
    results: List[Dict[str, Any]]
    judged: bool
    judgments: List[ChunkJudgment] = field(default_factory=list)
    original_count: int = 0
    kept_count: int = 0
    dropped_count: int = 0
    latency_ms: float = 0.0
    skip_reason: Optional[str] = None
    backend: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "judged": self.judged, "backend": self.backend,
            "original_count": self.original_count,
            "kept_count": self.kept_count,
            "dropped_count": self.dropped_count,
            "latency_ms": round(self.latency_ms, 2),
            "skip_reason": self.skip_reason,
            "avg_score": (
                round(sum(j.overall_score for j in self.judgments) / len(self.judgments), 2)
                if self.judgments else 0.0
            ),
        }


# ═══════════════════════════════════════════════════════════════════════
#  DeepEval Backend (preferred)
# ═══════════════════════════════════════════════════════════════════════

class _DeepEvalBackend:
    """confident-ai/deepeval framework — 14+ metrics, CI/CD integration.

    Configuration: DeepEval internally calls an LLM for evaluation.
    We configure it to use GPT4IFX by setting OPENAI_API_BASE and
    OPENAI_API_KEY from AICE's existing environment variables.
    """

    def __init__(self):
        self._available = None
        self._configured = False
        self.name = "deepeval"

    def _configure_llm(self) -> None:
        """Configure DeepEval to use GPT4IFX proxy."""
        if self._configured:
            return
        self._configured = True

        # Align DeepEval TLS trust with other GPT4IFX clients.
        # Bundle location mirrors token_manager and rlm_orchestrator usage.
        ca_bundle = Path(__file__).resolve().parent.parent / "ca-bundle.crt"
        if ca_bundle.exists():
            ca_path = str(ca_bundle)
            os.environ.setdefault("SSL_CERT_FILE", ca_path)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_path)
            os.environ.setdefault("CURL_CA_BUNDLE", ca_path)

        # DeepEval uses OpenAI SDK internally. Configure it to hit GPT4IFX.
        gpt4ifx_base = os.getenv(
            "GPT4IFX_BASE_URL",
            os.getenv("OPENAI_BASE_URL", os.getenv("OPENAI_API_BASE", "https://gpt4ifx.icp.infineon.com")),
        )
        gpt4ifx_key = os.getenv("GPT4IFX_API_KEY", os.getenv("OPENAI_API_KEY", ""))
        gpt4ifx_model = os.getenv("GPT4IFX_MODEL", os.getenv("OPENAI_MODEL_NAME", "gpt-4o"))

        # If no API key is set, fetch/reuse the shared GPT4IFX JWT token.
        if not gpt4ifx_key:
            try:
                from src.HybridRAG.code.token_manager import ensure_valid_token

                gpt4ifx_key = ensure_valid_token()
            except Exception as exc:
                logger.warning("Could not obtain GPT4IFX token for DeepEval: %s", exc)

        if gpt4ifx_base:
            os.environ.setdefault("OPENAI_BASE_URL", gpt4ifx_base)
            os.environ.setdefault("OPENAI_API_BASE", gpt4ifx_base)
        if gpt4ifx_key:
            os.environ.setdefault("OPENAI_API_KEY", gpt4ifx_key)
        if gpt4ifx_model:
            os.environ.setdefault("OPENAI_MODEL_NAME", gpt4ifx_model)

    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import deepeval
            self._configure_llm()
            self._available = True
            logger.info("DeepEval framework available (v%s), configured for GPT4IFX",
                        getattr(deepeval, '__version__', '?'))
        except ImportError:
            self._available = False
            logger.info("deepeval not installed — using custom LLM judge fallback")
        return self._available

    def judge_chunks(self, query: str, chunks: List[str],
                     threshold: float = 0.5) -> List[ChunkJudgment]:
        """Evaluate chunks using DeepEval's ContextualRelevancyMetric."""
        if not self.available:
            return []

        try:
            from deepeval.metrics import ContextualRelevancyMetric
            from deepeval.test_case import LLMTestCase

            def _eval_one(i: int, chunk: str) -> ChunkJudgment:
                test_case = LLMTestCase(
                    input=query,
                    actual_output="",  # We're evaluating retrieval, not generation
                    retrieval_context=[chunk],
                )
                metric = ContextualRelevancyMetric(
                    threshold=threshold,
                    include_reason=True,
                )
                try:
                    metric.measure(test_case)
                    score = metric.score or 0.0
                    reason = metric.reason or ""
                    return ChunkJudgment(
                        chunk_index=i,
                        relevancy_score=score,
                        overall_score=score,
                        reasoning=reason,
                        keep=score >= threshold,
                    )
                except Exception as exc:
                    logger.debug("DeepEval metric failed for chunk %d: %s", i, exc)
                    return ChunkJudgment(
                        chunk_index=i, overall_score=threshold,
                        keep=True, reasoning=f"Eval failed: {exc}")

            worker_count = max(1, min(DEEPEVAL_MAX_WORKERS, len(chunks)))
            judgments: List[ChunkJudgment] = []
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = [pool.submit(_eval_one, i, chunk) for i, chunk in enumerate(chunks)]
                for fut in as_completed(futures):
                    try:
                        judgments.append(fut.result())
                    except Exception as exc:
                        logger.debug("DeepEval parallel worker failed: %s", exc)

            judgments.sort(key=lambda j: j.chunk_index)

            return judgments

        except Exception as exc:
            logger.warning("DeepEval evaluation failed: %s", exc)
            return []


class _CustomLLMBackend:
    """Fallback: custom GPT4IFX single-call judge (from v1)."""

    _SYSTEM_PROMPT = """You are a relevance judge for automotive embedded software.
Score each chunk on two axes (1-10): factual (answers the query?) and
contextual (appropriate for the query intent?).
Respond ONLY with JSON array: [{"index": N, "factual": N, "contextual": N, "reason": "..."}]"""

    def __init__(self, llm_fn: Optional[Callable] = None):
        self._llm_fn = llm_fn
        self.name = "custom_llm"

    @property
    def available(self) -> bool:
        return self._llm_fn is not None

    def judge_chunks(self, query: str, chunks: List[str],
                     threshold: float = 0.5) -> List[ChunkJudgment]:
        if not self.available:
            return []

        try:
            chunk_texts = [f"[{i}] {c[:500]}" for i, c in enumerate(chunks)]
            user_prompt = f"Query: {query}\n\nChunks:\n" + "\n\n".join(chunk_texts)

            response = self._llm_fn(
                system=self._SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=500,
            )

            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

            parsed = json.loads(text)
            if not isinstance(parsed, list):
                parsed = [parsed]

            judgments = []
            for item in parsed:
                idx = item.get("index", -1)
                if 0 <= idx < len(chunks):
                    factual = float(item.get("factual", 5))
                    contextual = float(item.get("contextual", 5))
                    overall = (factual + contextual) / 2.0
                    judgments.append(ChunkJudgment(
                        chunk_index=idx,
                        relevancy_score=factual / 10.0,
                        faithfulness_score=contextual / 10.0,
                        overall_score=overall / 10.0,
                        reasoning=item.get("reason", ""),
                        keep=(overall / 10.0) >= threshold,
                    ))

            # Fill missing indices
            seen = {j.chunk_index for j in judgments}
            for i in range(len(chunks)):
                if i not in seen:
                    judgments.append(ChunkJudgment(
                        chunk_index=i, overall_score=threshold,
                        keep=True, reasoning="Not scored — default keep"))

            return judgments

        except Exception as exc:
            logger.warning("Custom LLM judge failed: %s", exc)
            return [ChunkJudgment(chunk_index=i, overall_score=0.5,
                                  keep=True, reasoning="Judge error — keep")
                    for i in range(len(chunks))]


# ═══════════════════════════════════════════════════════════════════════
#  RelevanceJudge (main class)
# ═══════════════════════════════════════════════════════════════════════

class RelevanceJudge:
    """
    Multi-backend relevance judge with DeepEval (preferred) or custom fallback.

    DeepEval advantages:
      - 14+ metrics (relevancy, faithfulness, hallucination, bias, toxicity)
      - CI/CD test runner integration
      - Structured evaluation with automatic threshold management
      - Plugin-agnostic (works with any LLM backend)
    """

    def __init__(self, llm_fn: Optional[Callable] = None,
                 threshold: float = JUDGE_THRESHOLD,
                 top_n: int = JUDGE_TOP_N,
                 enabled: bool = JUDGE_ENABLED,
                 backend: str = JUDGE_BACKEND):
        self._threshold = threshold
        self._top_n = top_n
        self._enabled = enabled
        self._active_backend = None

        self._custom_backend = _CustomLLMBackend(llm_fn)
        self._backends = []
        if backend == "deepeval":
            self._backends = [_DeepEvalBackend(), self._custom_backend]
        else:
            self._backends = [self._custom_backend, _DeepEvalBackend()]

    @property
    def available(self) -> bool:
        if not self._enabled:
            return False
        if self._active_backend is not None:
            return True
        for be in self._backends:
            if be.available:
                self._active_backend = be
                return True
        return False

    def judge(self, query: str, results: List[Dict[str, Any]],
              review_type: Optional[str] = None) -> JudgeResult:
        n = len(results)

        if review_type == "AUTO":
            return JudgeResult(results=results, judged=False, original_count=n,
                               kept_count=n, skip_reason="Skipped: AUTO review (fast path)")

        if not self.available:
            reason = "disabled" if not self._enabled else "no backend"
            return JudgeResult(results=results, judged=False, original_count=n,
                               kept_count=n, skip_reason=f"Skipped: {reason}")

        if n <= 2:
            return JudgeResult(results=results, judged=False, original_count=n,
                               kept_count=n, skip_reason="Skipped: <= 2 results")

        start = time.monotonic()
        to_judge = results[:self._top_n]
        passthrough = results[self._top_n:]

        chunks = [self._extract_text(r) for r in to_judge]
        judgments = self._active_backend.judge_chunks(query, chunks, self._threshold)

        if not judgments:
            elapsed = (time.monotonic() - start) * 1000
            return JudgeResult(results=results, judged=False, original_count=n,
                               kept_count=n, latency_ms=elapsed,
                               skip_reason="Judge returned no results — keeping all")

        kept, dropped = [], 0
        for i, r in enumerate(to_judge):
            j = next((jj for jj in judgments if jj.chunk_index == i), None)
            if j and j.keep:
                r_copy = dict(r)
                r_copy["judge_score"] = j.overall_score
                r_copy["judge_relevancy"] = j.relevancy_score
                kept.append(r_copy)
            elif r.get("_must_include"):
                kept.append(r)
            else:
                dropped += 1

        final = kept + passthrough
        elapsed = (time.monotonic() - start) * 1000

        logger.info("Judge [%s]: %d judged, %d kept, %d dropped, %.0fms",
                    self._active_backend.name, len(to_judge), len(kept), dropped, elapsed)

        return JudgeResult(
            results=final, judged=True, judgments=judgments,
            original_count=n, kept_count=len(final), dropped_count=dropped,
            latency_ms=elapsed, backend=self._active_backend.name)

    @staticmethod
    def _extract_text(result: Dict[str, Any]) -> str:
        for key in ("content", "text", "rendered", "description"):
            if key in result and isinstance(result[key], str) and result[key].strip():
                return result[key][:500]
        props = result.get("properties", {})
        if isinstance(props, dict):
            parts = [str(props[k]) for k in ("name", "function_name", "description", "content")
                     if k in props and isinstance(props[k], str)]
            if parts:
                return " | ".join(parts)[:500]
        return str(result)[:500]

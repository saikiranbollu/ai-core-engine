"""
Cross-Encoder Reranker — GAP-A01 (Sprint 12, Research Upgrade v2)
================================================================
**Research Upgrade:** Replaced sentence-transformers CrossEncoder (PyTorch ~2GB)
with PrithivirajDamodaran/FlashRank (ONNX runtime ~50MB, CPU-native, 10x faster).
Same ms-marco-MiniLM-L-12-v2 model, packaged as ONNX.

Fallback chain:
  1. FlashRank (preferred — ONNX, CPU-native, 15-25ms for 50 chunks)
  2. sentence-transformers CrossEncoder (if FlashRank unavailable)
  3. Passthrough (if neither available)

Guard: Only activates for non-structural queries (~25% of AICE traffic).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "ms-marco-MiniLM-L-12-v2")
RERANKER_TOP_K = int(os.getenv("RERANKER_TOP_K", "20"))
RERANKER_BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "32"))
RERANKER_MIN_SCORE = float(os.getenv("RERANKER_MIN_SCORE", "0.01"))
RERANKER_BACKEND = os.getenv("RERANKER_BACKEND", "flashrank")


@dataclass
class RerankResult:
    results: List[Dict[str, Any]]
    reranked: bool
    model_used: Optional[str] = None
    backend: Optional[str] = None
    original_count: int = 0
    reranked_count: int = 0
    latency_ms: float = 0.0
    skip_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reranked": self.reranked, "model_used": self.model_used,
            "backend": self.backend, "original_count": self.original_count,
            "reranked_count": self.reranked_count,
            "latency_ms": round(self.latency_ms, 2),
            "skip_reason": self.skip_reason,
        }


class _FlashRankBackend:
    """FlashRank ONNX backend — preferred. ~50MB, CPU-native, 15-25ms/50 chunks."""

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._ranker = None
        self._load_attempted = False
        self.name = "flashrank"

    @property
    def available(self) -> bool:
        if self._ranker is not None:
            return True
        if not self._load_attempted:
            self._lazy_load()
        return self._ranker is not None

    def _lazy_load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            from flashrank import Ranker
            start = time.monotonic()
            self._ranker = Ranker(model_name=self._model_name)
            elapsed = (time.monotonic() - start) * 1000
            logger.info("FlashRank loaded in %.0f ms: %s", elapsed, self._model_name)
        except ImportError:
            logger.info("flashrank not installed — trying CrossEncoder fallback")
        except Exception as exc:
            logger.warning("FlashRank init failed: %s", exc)

    def score(self, query: str, texts: List[str]) -> List[float]:
        if not self.available:
            return [0.0] * len(texts)
        from flashrank import RerankRequest
        passages = [{"id": i, "text": t} for i, t in enumerate(texts)]
        request = RerankRequest(query=query, passages=passages)
        ranked = self._ranker.rerank(request)
        score_map = {r["id"]: r["score"] for r in ranked}
        return [score_map.get(i, 0.0) for i in range(len(texts))]


class _CrossEncoderBackend:
    """sentence-transformers CrossEncoder fallback — requires PyTorch."""

    def __init__(self, model_name: str):
        self._model_name = f"cross-encoder/{model_name}"
        self._model = None
        self._load_attempted = False
        self.name = "crossencoder"

    @property
    def available(self) -> bool:
        if self._model is not None:
            return True
        if not self._load_attempted:
            self._lazy_load()
        return self._model is not None

    def _lazy_load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            from sentence_transformers import CrossEncoder
            start = time.monotonic()
            self._model = CrossEncoder(self._model_name)
            elapsed = (time.monotonic() - start) * 1000
            logger.info("CrossEncoder loaded in %.0f ms: %s", elapsed, self._model_name)
        except ImportError:
            logger.info("sentence-transformers not installed — reranking disabled")
        except Exception as exc:
            logger.warning("CrossEncoder init failed: %s", exc)

    def score(self, query: str, texts: List[str]) -> List[float]:
        if not self.available:
            return [0.0] * len(texts)
        pairs = [(query, t) for t in texts]
        scores = self._model.predict(pairs, batch_size=RERANKER_BATCH_SIZE,
                                     show_progress_bar=False)
        return [float(s) for s in scores]


class CrossEncoderReranker:
    """
    Reranker with FlashRank (preferred) or sentence-transformers fallback.

    FlashRank advantages:
      - 50MB vs 2GB (no PyTorch)
      - 15-25ms vs 180ms for 50 chunks on CPU (ONNX + SIMD)
      - Same ms-marco model quality (NDCG@10 = 0.86)
    """

    def __init__(self, model_name: str = RERANKER_MODEL, top_k: int = RERANKER_TOP_K,
                 min_score: float = RERANKER_MIN_SCORE, enabled: bool = RERANKER_ENABLED,
                 backend: str = RERANKER_BACKEND):
        self._model_name = model_name
        self._top_k = top_k
        self._min_score = min_score
        self._enabled = enabled
        self._backend = None

        self._backends: List = []
        if backend == "flashrank":
            self._backends = [_FlashRankBackend(model_name), _CrossEncoderBackend(model_name)]
        else:
            self._backends = [_CrossEncoderBackend(model_name), _FlashRankBackend(model_name)]

    @property
    def available(self) -> bool:
        if not self._enabled:
            return False
        if self._backend is not None:
            return True
        for be in self._backends:
            if be.available:
                self._backend = be
                logger.info("Reranker backend: %s", be.name)
                return True
        return False

    def rerank(self, query: str, results: List[Dict[str, Any]],
               top_k: Optional[int] = None, skip_structural: bool = True,
               search_strategy: Optional[str] = None) -> RerankResult:
        k = top_k or self._top_k
        n = len(results)

        if skip_structural and search_strategy in ("graph_heavy", "exact"):
            return RerankResult(results=results[:k], reranked=False, original_count=n,
                                reranked_count=min(k, n),
                                skip_reason=f"Skipped: strategy={search_strategy}")

        if not self.available:
            reason = "disabled" if not self._enabled else "no backend"
            return RerankResult(results=results[:k], reranked=False, original_count=n,
                                reranked_count=min(k, n), skip_reason=f"Skipped: {reason}")

        if n <= 3:
            return RerankResult(results=results, reranked=False, original_count=n,
                                reranked_count=n, skip_reason="Skipped: <= 3 results")

        try:
            start = time.monotonic()
            texts, valid_idx = [], []
            for i, r in enumerate(results):
                t = self._extract_text(r)
                if t:
                    texts.append(t)
                    valid_idx.append(i)

            if not texts:
                return RerankResult(results=results[:k], reranked=False, original_count=n,
                                    reranked_count=min(k, n), skip_reason="No scorable content")

            scores = self._backend.score(query, texts)

            scored = []
            for idx, score in zip(valid_idx, scores):
                r = results[idx].copy()
                r["reranker_score"] = float(score)
                r["original_rank"] = idx
                r["rrf_score"] = r.get("score", 0.0)
                r["score"] = float(score)
                scored.append(r)

            scored.sort(key=lambda x: x["reranker_score"], reverse=True)
            filtered = [r for r in scored if r["reranker_score"] >= self._min_score][:k]

            # Preserve must-include items
            kept_ids = {f.get("node_id") for f in filtered}
            for r in results:
                if r.get("_must_include") and r.get("node_id") not in kept_ids:
                    filtered.append(r)

            elapsed = (time.monotonic() - start) * 1000
            logger.info("Reranking [%s]: %d->%d in %.0fms", self._backend.name,
                        n, len(filtered), elapsed)

            return RerankResult(results=filtered, reranked=True, model_used=self._model_name,
                                backend=self._backend.name, original_count=n,
                                reranked_count=len(filtered), latency_ms=elapsed)
        except Exception as exc:
            logger.error("Reranking failed: %s", exc)
            return RerankResult(results=results[:k], reranked=False, original_count=n,
                                reranked_count=min(k, n), skip_reason=f"Error: {exc}")

    @staticmethod
    def _extract_text(result: Dict[str, Any]) -> Optional[str]:
        for key in ("content", "text", "rendered", "description"):
            if key in result and isinstance(result[key], str) and result[key].strip():
                return result[key][:2000]
        props = result.get("properties", {})
        if isinstance(props, dict):
            parts = [str(props[k]) for k in ("name", "function_name", "description",
                     "content", "documentation", "body", "requirement_text")
                     if k in props and isinstance(props[k], str)]
            if parts:
                return " | ".join(parts)[:2000]
        return None

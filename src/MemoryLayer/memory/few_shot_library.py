"""
Few-Shot Learning Library — GAP-A14 (Sprint 16)
===================================================
Maintains a library of high-quality Q&A pairs per task type.
Injects 2-3 most-similar examples into DA prompts.

Architecture:
  DA query → FewShotLibrary.retrieve(query, task_type)
    → Qdrant similarity search on few_shot_examples collection
    → Return top-3 examples
    → DA template includes {{few_shot_examples}} placeholder

Storage: PatternStore collection 'few_shot_examples' in Qdrant.

Design principles:
  - Examples stored as (question, answer, task_type, quality_score) tuples
  - Similarity search via Qdrant (reuses existing infrastructure)
  - Graceful degradation: if Qdrant unavailable, returns empty list
  - Examples populated from APPROVED feedback (FeedbackSink integration)
  - Quality threshold: only examples with score >= 80 are included
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────

FEWSHOT_ENABLED = os.getenv("FEWSHOT_ENABLED", "true").lower() == "true"
FEWSHOT_COLLECTION = os.getenv("FEWSHOT_COLLECTION", "few_shot_examples")
FEWSHOT_TOP_K = int(os.getenv("FEWSHOT_TOP_K", "3"))
FEWSHOT_MIN_QUALITY = float(os.getenv("FEWSHOT_MIN_QUALITY", "80.0"))
FEWSHOT_MIN_SIMILARITY = float(os.getenv("FEWSHOT_MIN_SIMILARITY", "0.65"))


# ═════════════════════════════════════════════════════════════════════════
#  Data classes
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class FewShotExample:
    """A Q&A example for few-shot prompting."""
    example_id: str
    question: str
    answer: str
    task_type: str
    module: str = ""
    quality_score: float = 0.0
    source: str = "feedback"  # feedback | manual | synthetic
    similarity: float = 0.0

    def render(self, max_answer_chars: int = 500) -> str:
        """Render example for prompt injection."""
        answer = self.answer[:max_answer_chars]
        if len(self.answer) > max_answer_chars:
            answer += "..."
        return (
            f"Example Q: {self.question}\n"
            f"Example A: {answer}"
        )


@dataclass
class FewShotRetrievalResult:
    """Result of few-shot example retrieval."""
    examples: List[FewShotExample]
    rendered: str
    count: int
    latency_ms: float
    collection: str = FEWSHOT_COLLECTION

    def as_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "task_type": self.examples[0].task_type if self.examples else "",
            "avg_similarity": (
                round(sum(e.similarity for e in self.examples) / len(self.examples), 3)
                if self.examples else 0.0
            ),
            "latency_ms": round(self.latency_ms, 2),
        }


# ═════════════════════════════════════════════════════════════════════════
#  FewShotLibrary
# ═════════════════════════════════════════════════════════════════════════

class FewShotLibrary:
    """
    Maintains and retrieves few-shot examples for DA prompts.

    Parameters
    ----------
    qdrant_client : QdrantClient, optional
        Qdrant client for similarity search.
    embed_fn : callable, optional
        Function(text) → list[float] for embedding queries.
    enabled : bool
        Whether few-shot is active.
    """

    def __init__(
        self,
        qdrant_client=None,
        embed_fn: Optional[Callable] = None,
        enabled: bool = FEWSHOT_ENABLED,
    ):
        self._qdrant = qdrant_client
        self._embed_fn = embed_fn
        self._enabled = enabled
        self._collection = FEWSHOT_COLLECTION

    @property
    def available(self) -> bool:
        return self._enabled and self._qdrant is not None and self._embed_fn is not None

    def retrieve(
        self,
        query: str,
        task_type: str,
        module: Optional[str] = None,
        top_k: int = FEWSHOT_TOP_K,
    ) -> FewShotRetrievalResult:
        """
        Retrieve the most similar few-shot examples.

        Parameters
        ----------
        query : str
            Current DA query.
        task_type : str
            DA task type (e.g., "code_generation", "test_generation").
        module : str, optional
            Filter examples to this module.
        top_k : int
            Number of examples to retrieve.

        Returns
        -------
        FewShotRetrievalResult with rendered examples.
        """
        start = time.monotonic()

        if not self.available:
            return FewShotRetrievalResult(
                examples=[], rendered="", count=0, latency_ms=0.0,
            )

        try:
            # Embed query
            query_vector = self._embed_fn(query)

            # Build Qdrant filter
            from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

            qdrant_filter = Filter(
                must=[
                    FieldCondition(key="task_type", match=MatchValue(value=task_type)),
                    FieldCondition(key="quality_score", range=Range(gte=FEWSHOT_MIN_QUALITY)),
                ] + (
                    [FieldCondition(key="module", match=MatchValue(value=module.upper()))]
                    if module else []
                )
            )

            results = self._qdrant.search(
                collection_name=self._collection,
                query_vector=query_vector,
                query_filter=qdrant_filter,
                limit=top_k,
                score_threshold=FEWSHOT_MIN_SIMILARITY,
            )

            examples = []
            for hit in results:
                payload = hit.payload or {}
                examples.append(FewShotExample(
                    example_id=str(hit.id),
                    question=payload.get("question", ""),
                    answer=payload.get("answer", ""),
                    task_type=payload.get("task_type", task_type),
                    module=payload.get("module", ""),
                    quality_score=payload.get("quality_score", 0.0),
                    source=payload.get("source", "feedback"),
                    similarity=hit.score,
                ))

            # Render for prompt injection
            rendered_parts = []
            for i, ex in enumerate(examples):
                rendered_parts.append(f"--- Example {i+1} ---")
                rendered_parts.append(ex.render())

            rendered = "\n".join(rendered_parts) if rendered_parts else ""

            elapsed = (time.monotonic() - start) * 1000
            logger.debug(
                "Few-shot retrieval: %d examples for task=%s, %.0f ms",
                len(examples), task_type, elapsed,
            )

            return FewShotRetrievalResult(
                examples=examples,
                rendered=rendered,
                count=len(examples),
                latency_ms=elapsed,
            )

        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("Few-shot retrieval failed: %s", exc)
            return FewShotRetrievalResult(
                examples=[], rendered="", count=0, latency_ms=elapsed,
            )

    def add_example(
        self,
        question: str,
        answer: str,
        task_type: str,
        module: str = "",
        quality_score: float = 80.0,
        source: str = "feedback",
    ) -> bool:
        """
        Add a new few-shot example to the library.

        Called by FeedbackSink when a response is APPROVED with
        quality_score >= threshold.

        Returns True on success.
        """
        if not self.available:
            return False

        try:
            # Generate stable ID from question hash (UUID format required by Qdrant)
            md5_hex = hashlib.md5(
                f"{task_type}:{question}".encode()
            ).hexdigest()
            example_id = str(uuid.UUID(hex=md5_hex))

            # Embed the question
            vector = self._embed_fn(question)

            from qdrant_client.models import PointStruct

            point = PointStruct(
                id=example_id,
                vector=vector,
                payload={
                    "question": question,
                    "answer": answer[:2000],  # Cap answer size
                    "task_type": task_type,
                    "module": module.upper() if module else "",
                    "quality_score": quality_score,
                    "source": source,
                },
            )

            self._qdrant.upsert(
                collection_name=self._collection,
                points=[point],
            )

            logger.info(
                "Added few-shot example: task=%s, module=%s, score=%.0f",
                task_type, module, quality_score,
            )
            return True

        except Exception as exc:
            logger.error("Failed to add few-shot example: %s", exc)
            return False

    def ensure_collection(self) -> bool:
        """Create the few-shot collection if it doesn't exist."""
        if not self._qdrant:
            return False

        try:
            from qdrant_client.models import VectorParams, Distance

            collections = self._qdrant.get_collections().collections
            exists = any(c.name == self._collection for c in collections)

            if not exists:
                self._qdrant.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=384,  # all-MiniLM-L6-v2
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created few-shot collection: %s", self._collection)

            return True
        except Exception as exc:
            logger.error("Failed to ensure few-shot collection: %s", exc)
            return False

"""
semantic_memory
===============
Persistent pattern storage — second half of the Memory Layer.

Stores ApprovedPattern data permanently in Qdrant:
  - Full pattern payload (text, metadata, usage counts) in Qdrant payload
  - Vector embeddings for similarity search

Jira Tickets implemented here
------------------------------
  Ticket 3 — Design semantic memory schema (ApprovedPattern, collection naming)
  Ticket 4 — Pattern storage in Qdrant      (PatternStore)
  Ticket 5 — Pattern indexing in Qdrant      (PatternStore, Embedder)
  Ticket 6 — Metadata and usage tracking     (PatternStore.increment_usage)
  Ticket 7 — Similarity query 0.8 threshold  (PatternStore.find_similar)

Usage
-----
    from memory.semantic_memory import (
        ApprovedPattern,
        PatternStore,
        SimilarPattern,
        Embedder,
    )

    # Embed
    embedder = Embedder()

    # Store + search in Qdrant
    store = PatternStore(
        embedder=embedder,
        collection="approved_patterns",
    )
    pattern = ApprovedPattern(
        pattern_text="IfxCxpi_initChannel(&cxpi, &config);",
        pattern_type="api_usage",
        module="cxpi",
        profile="illd",
        confidence=0.95,
        approver_id="engineer_01",
    )
    store.store(pattern)

    # Search for similar patterns
    results = store.find_similar("init cxpi channel", module="cxpi")

    # Track usage
    new_count = store.increment_usage(pattern.pattern_id, source_request_id="req-001")
"""

from .embedder       import Embedder
from .pattern_store  import ApprovedPattern, PatternStore, SimilarPattern, DEFAULT_SIMILARITY_THRESHOLD
from .pattern_index  import PatternIndex

__all__ = [
    "Embedder",
    "ApprovedPattern",
    "PatternStore",
    "PatternIndex",
    "SimilarPattern",
    "DEFAULT_SIMILARITY_THRESHOLD",
]

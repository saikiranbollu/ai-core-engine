"""
PatternIndex
============
Backward-compatible wrapper around PatternStore for similarity search.

All storage and search is now handled by PatternStore (Qdrant-backed).
This module re-exports the types and provides the PatternIndex class
so that existing imports continue to work.

Previous architecture (two backends):
  - PatternStore → Neo4j  (structured CRUD)
  - PatternIndex → Qdrant (vector search)

Current architecture (single backend):
  - PatternStore → Qdrant (everything: CRUD + vectors + search)
  - PatternIndex → thin wrapper around PatternStore
"""

import logging
from typing import List, Optional

from .embedder import Embedder
from .pattern_store import (
    ApprovedPattern,
    PatternStore,
    SimilarPattern,
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TOP_K,
)

logger = logging.getLogger(__name__)


class PatternIndex:
    """
    Backward-compatible wrapper around PatternStore for similarity search.

    Existing code that uses PatternIndex for indexing and search continues
    to work.  Internally, all operations delegate to PatternStore.

    Parameters
    ----------
    qdrant_url : str
        URL of the Qdrant instance.
    embedder : Embedder
        Loaded Embedder instance used to generate vectors.
    collection : str
        Name of the Qdrant collection.
    _client : optional
        Inject a pre-constructed QdrantClient (or mock).  Used in tests.
    """

    def __init__(
        self,
        qdrant_url: str,
        embedder:   Embedder,
        collection: str,
        _client=None,
    ):
        self._store = PatternStore(
            embedder=embedder,
            collection=collection,
            qdrant_url=qdrant_url,
            _client=_client,
        )

    @staticmethod
    def make_point_id(module: str, pattern_id: str) -> str:
        return PatternStore.make_point_id(module, pattern_id)

    def index_pattern(
        self,
        pattern: ApprovedPattern,
        embedding: Optional[List[float]] = None,
    ) -> None:
        """Add or update a pattern in Qdrant (delegates to PatternStore.store)."""
        self._store.store(pattern, embedding=embedding)

    def update_usage_in_payload(
        self, pattern_id: str, module: str, usage_count: int
    ) -> None:
        """Sync usage_count in Qdrant payload."""
        point_id = self.make_point_id(module, pattern_id)
        self._store._client.set_payload(
            collection_name=self._store._collection,
            payload={"usage_count": usage_count},
            points=[point_id],
        )

    def find_similar(
        self,
        query_text: str,
        module:     str,
        threshold:  float = DEFAULT_SIMILARITY_THRESHOLD,
        top_k:      int   = DEFAULT_TOP_K,
    ) -> List[SimilarPattern]:
        """Find patterns similar to query_text (delegates to PatternStore.find_similar)."""
        return self._store.find_similar(query_text, module, threshold, top_k)

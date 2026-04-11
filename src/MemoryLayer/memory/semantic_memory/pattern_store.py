"""
PatternStore
============
Manages ApprovedPattern storage entirely in Qdrant (vector database).

All pattern data — text, metadata, usage counts — is stored as Qdrant point
payloads alongside vector embeddings.  No Neo4j dependency.

Implements Jira Tickets 3–7:
  - Ticket 3 (schema design): ApprovedPattern with all required fields
  - Ticket 4 (storage):       Upsert-based store, query by pattern_id
  - Ticket 5 (indexing):       Embed pattern text, store as Qdrant point
  - Ticket 6 (usage tracking): usage_count increment, source_request_id
  - Ticket 7 (similarity):     Cosine similarity search with 0.8 threshold

Qdrant point structure
----------------------
  Point ID:  {module_lowercase}_{pattern_uuid}
  Vector:    384-dim embedding (all-MiniLM-L6-v2)
  Payload:   All ApprovedPattern fields stored as flat dict
"""

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD: float = 0.8
DEFAULT_TOP_K:                 int   = 5


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ApprovedPattern:
    """
    Represents one approved pattern stored in Qdrant.

    Attributes
    ----------
    pattern_text : str
        The actual pattern content — code snippet, structural template, etc.
    pattern_type : str
        Semantic category.
        Examples: 'api_usage', 'test_structure', 'error_handling',
                  'initialization', 'register_access'.
    module : str
        Module name from ontology (lowercased on store).
    profile : str
        Ontology profile ('illd' | 'mcal').
    confidence : float
        Approval confidence score (0.0–1.0).
    pattern_id : str
        UUID string.  Auto-generated if not supplied.
    approval_date : datetime, optional
        When the pattern was approved.  Defaults to now (UTC).
    approver_id : str, optional
        Identifier of the human reviewer who approved this pattern.
    usage_count : int
        Number of times this pattern has been used as a few-shot example.
        Managed by PatternStore.increment_usage().
    source_request_id : str, optional
        The session_id or request_id that originally produced this pattern.
    created_at : datetime
        UTC timestamp of creation.  Auto-set.
    """

    # Required fields
    pattern_text:       str
    pattern_type:       str
    module:             str
    profile:            str
    confidence:         float

    # Auto-generated / defaulted fields
    pattern_id:         str                 = field(default_factory=lambda: str(uuid.uuid4()))
    approval_date:      Optional[datetime]  = field(default_factory=lambda: datetime.now(timezone.utc))
    approver_id:        Optional[str]       = None
    usage_count:        int                 = 0
    source_request_id:  Optional[str]       = None
    created_at:         datetime            = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_payload(self) -> Dict[str, Any]:
        """
        Serialise to a flat dict for Qdrant payload storage.

        All datetimes are converted to ISO-8601 strings.
        module and profile are lowercased.
        """
        return {
            "pattern_id":        self.pattern_id,
            "pattern_text":      self.pattern_text,
            "pattern_type":      self.pattern_type,
            "module":            self.module.lower(),
            "profile":           self.profile.lower(),
            "confidence":        self.confidence,
            "approval_date":     self.approval_date.isoformat() if self.approval_date else None,
            "approver_id":       self.approver_id,
            "usage_count":       self.usage_count,
            "source_request_id": self.source_request_id,
            "created_at":        self.created_at.isoformat(),
        }

    # Backward-compat aliases
    to_neo4j_params = to_payload

    @classmethod
    def from_payload(cls, record: Dict[str, Any]) -> "ApprovedPattern":
        """Reconstruct an ApprovedPattern from a Qdrant payload dict."""
        return cls(
            pattern_id=record["pattern_id"],
            pattern_text=record["pattern_text"],
            pattern_type=record["pattern_type"],
            module=record["module"],
            profile=record["profile"],
            confidence=record["confidence"],
            approval_date=(
                datetime.fromisoformat(record["approval_date"])
                if record.get("approval_date") else None
            ),
            approver_id=record.get("approver_id"),
            usage_count=record.get("usage_count", 0),
            source_request_id=record.get("source_request_id"),
            created_at=datetime.fromisoformat(record["created_at"]),
        )

    # Backward-compat alias
    from_neo4j_record = from_payload


@dataclass
class SimilarPattern:
    """
    One result from a similarity search.

    Attributes
    ----------
    pattern_id : str
        UUID of the matching pattern.
    score : float
        Cosine similarity score (0.0–1.0).  Higher = more similar.
    pattern_type : str
        Semantic category of the pattern.
    profile : str
        Ontology profile (illd | mcal).
    module : str
        Module name.
    confidence : float
        Approval confidence stored at index time.
    usage_count : int
        Usage count from Qdrant payload.
    """
    pattern_id:   str
    score:        float
    pattern_type: str
    profile:      str
    module:       str
    confidence:   float
    usage_count:  int


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN STORE (Qdrant-backed)
# ─────────────────────────────────────────────────────────────────────────────

class PatternStore:
    """
    CRUD + similarity search for ApprovedPattern, backed entirely by Qdrant.

    All pattern data (text, metadata, usage counts) is stored in Qdrant
    payloads.  Vector embeddings enable similarity search.  No Neo4j needed.

    Parameters
    ----------
    embedder : Embedder
        Loaded Embedder instance used to generate vectors.
    collection : str
        Name of the Qdrant collection for approved patterns.
    qdrant_url : str, optional
        URL of the Qdrant instance.  Falls back to QDRANT_URL env var.
        Raises if neither is set — check env/.env.
    qdrant_api_key : str, optional
        API key for Qdrant authentication.  Falls back to QDRANT_API_KEY env var.
        Raises if neither is set — check env/.env.
    _client : optional
        Inject a pre-constructed QdrantClient (or mock).  Used in tests.

    Example
    -------
        from memory.semantic_memory.embedder import Embedder
        embedder = Embedder()
        store = PatternStore(embedder=embedder, collection="approved_patterns")

        pattern_id = store.store(ApprovedPattern(
            pattern_text="IfxCxpi_initChannel(&cxpi, &config);",
            pattern_type="api_usage",
            module="cxpi",
            profile="illd",
            confidence=0.95,
            approver_id="engineer_01",
        ))
    """

    def __init__(
        self,
        embedder,
        collection:    str,
        qdrant_url:    Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        _client=None,
    ):
        self._embedder   = embedder
        self._collection = collection

        if _client is not None:
            self._client = _client
        else:
            try:
                from qdrant_client import QdrantClient
            except ImportError:
                raise ImportError(
                    "[PatternStore] 'qdrant-client' is not installed. "
                    "Run: pip install qdrant-client"
                )
            url = qdrant_url or os.environ.get("QDRANT_URL")
            if not url:
                raise ValueError(
                    "[PatternStore] QDRANT_URL is not set. "
                    "Set it in env/.env or pass qdrant_url to PatternStore()."
                )
            api_key = qdrant_api_key or os.environ.get("QDRANT_API_KEY")
            if not api_key:
                raise ValueError(
                    "[PatternStore] QDRANT_API_KEY is not set. "
                    "Set it in env/.env or pass qdrant_api_key to PatternStore()."
                )
            self._client = QdrantClient(url=url, api_key=api_key)
            logger.info(f"[PatternStore] Connected to Qdrant at {url}")

    # ── point ID convention ──────────────────────────────────────────────────

    @staticmethod
    def make_point_id(module: str, pattern_id: str) -> str:
        """
        Build the Qdrant point ID with the module prefix.

        Format:  {module_lowercase}_{pattern_uuid}
        Example: cxpi_a1b2c3d4-e5f6-7890-abcd-ef1234567890
        """
        normalised_uuid = str(uuid.UUID(pattern_id))
        return f"{module.lower()}_{normalised_uuid}"

    # ── write ────────────────────────────────────────────────────────────────

    def store(
        self,
        pattern:   ApprovedPattern,
        embedding: Optional[List[float]] = None,
    ) -> str:
        """
        Persist an ApprovedPattern as a Qdrant point (idempotent upsert).

        Generates the embedding from pattern.pattern_text if not provided.
        All pattern fields are stored in the Qdrant payload.

        Returns
        -------
        str
            The pattern_id of the stored pattern.
        """
        try:
            from qdrant_client.models import PointStruct
        except ImportError:
            PointStruct = dict  # type: ignore

        vector   = embedding if embedding is not None else self._embedder.embed(pattern.pattern_text)
        point_id = self.make_point_id(pattern.module, pattern.pattern_id)
        payload  = pattern.to_payload()

        point = PointStruct(id=point_id, vector=vector, payload=payload)
        self._client.upsert(collection_name=self._collection, points=[point])
        logger.info(
            f"[PatternStore] Stored pattern {pattern.pattern_id[:8]}… "
            f"type={pattern.pattern_type} module={pattern.module}"
        )
        return pattern.pattern_id

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, pattern_id: str) -> Optional[ApprovedPattern]:
        """
        Retrieve an ApprovedPattern by its pattern_id.

        Uses a scroll query with payload filter.
        Returns None if not found.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError:
            FieldCondition = Filter = MatchValue = None  # type: ignore

        scroll_filter = Filter(
            must=[FieldCondition(key="pattern_id", match=MatchValue(value=pattern_id))]
        )
        results, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=scroll_filter,
            limit=1,
            with_payload=True,
        )
        if not results:
            return None
        return ApprovedPattern.from_payload(results[0].payload)

    def query_by_module(self, module: str) -> List[ApprovedPattern]:
        """
        Retrieve all ApprovedPatterns for a specific module,
        ordered by confidence descending.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError:
            FieldCondition = Filter = MatchValue = None  # type: ignore

        scroll_filter = Filter(
            must=[FieldCondition(key="module", match=MatchValue(value=module.lower()))]
        )
        results, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=scroll_filter,
            limit=10000,
            with_payload=True,
        )
        patterns = [ApprovedPattern.from_payload(r.payload) for r in results]
        return sorted(patterns, key=lambda p: p.confidence, reverse=True)

    def query_by_min_usage(self, min_usage: int) -> List[ApprovedPattern]:
        """
        Retrieve all patterns with usage_count >= min_usage,
        ordered by usage_count descending.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, Range
        except ImportError:
            FieldCondition = Filter = Range = None  # type: ignore

        scroll_filter = Filter(
            must=[FieldCondition(key="usage_count", range=Range(gte=min_usage))]
        )
        results, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=scroll_filter,
            limit=10000,
            with_payload=True,
        )
        patterns = [ApprovedPattern.from_payload(r.payload) for r in results]
        return sorted(patterns, key=lambda p: p.usage_count, reverse=True)

    # ── usage tracking (Ticket 6) ────────────────────────────────────────────

    def increment_usage(
        self, pattern_id: str, source_request_id: Optional[str] = None
    ) -> int:
        """
        Increment the usage_count of an ApprovedPattern by 1.

        Reads the current count from Qdrant, increments, and updates
        the payload in place.

        Parameters
        ----------
        pattern_id : str
            The pattern to increment.
        source_request_id : str, optional
            The session_id or request_id of the caller using this pattern
            as a few-shot example.

        Returns
        -------
        int
            The updated usage_count value.
            Returns -1 if the pattern was not found.
        """
        pattern = self.get(pattern_id)
        if pattern is None:
            logger.warning(
                f"[PatternStore] Pattern '{pattern_id}' not found — "
                f"usage_count not incremented."
            )
            return -1

        new_count = pattern.usage_count + 1
        point_id  = self.make_point_id(pattern.module, pattern_id)
        payload_update: Dict[str, Any] = {"usage_count": new_count}
        if source_request_id:
            payload_update["last_used_request_id"] = source_request_id

        self._client.set_payload(
            collection_name=self._collection,
            payload=payload_update,
            points=[point_id],
        )
        logger.debug(f"[PatternStore] Pattern {pattern_id[:8]}… usage_count → {new_count}")
        return new_count

    # ── similarity search (Ticket 7) ─────────────────────────────────────────

    def find_similar(
        self,
        query_text: str,
        module:     str,
        threshold:  float = DEFAULT_SIMILARITY_THRESHOLD,
        top_k:      int   = DEFAULT_TOP_K,
    ) -> List[SimilarPattern]:
        """
        Find patterns semantically similar to query_text, filtered by module.

        Parameters
        ----------
        query_text : str
            The context or code snippet to search against.
        module : str
            Module name — filters results to this module only.
        threshold : float
            Minimum cosine similarity score (0.0–1.0).  Default: 0.8.
        top_k : int
            Maximum number of results to return.  Default: 5.

        Returns
        -------
        List[SimilarPattern]
            Patterns with score >= threshold, ordered by score descending.
        """
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError:
            FieldCondition = Filter = MatchValue = None  # type: ignore

        module_filter = Filter(
            must=[
                FieldCondition(
                    key="module",
                    match=MatchValue(value=module.lower()),
                )
            ]
        )

        query_vector = self._embedder.embed(query_text)
        hits = self._client.search(
            collection_name=self._collection,
            query_vector=query_vector,
            query_filter=module_filter,
            limit=top_k,
            score_threshold=threshold,
            with_payload=True,
        )

        results = [
            SimilarPattern(
                pattern_id=hit.payload.get("pattern_id", ""),
                score=hit.score,
                pattern_type=hit.payload.get("pattern_type", ""),
                profile=hit.payload.get("profile", ""),
                module=hit.payload.get("module", ""),
                confidence=hit.payload.get("confidence", 0.0),
                usage_count=hit.payload.get("usage_count", 0),
            )
            for hit in hits
        ]

        logger.info(
            f"[PatternStore] find_similar: query='{query_text[:60]}' "
            f"module='{module}' threshold={threshold} → {len(results)} results"
        )
        return results

"""
Session
=======
Represents one active working memory session.

A session belongs to one project + one module (both from ontology.yaml),
has a TTL (time-to-live), and holds a list of context entries.
Each context entry represents one piece of information the AI retrieved
during this session — e.g. a function it looked up, a requirement it read.

The session structure is fully driven by the ontology:
  - Allowed node types come from ontology.yaml profile → node_types
  - Nothing is hard-coded

Key design rules:
  - project and module are always lowercased
  - TTL is checked on every read — expired sessions raise SessionExpiredError
  - Context entries carry the node_type label (from ontology)
    so that consumers know what kind of data they are dealing with
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionExpiredError(Exception):
    """Raised when an operation is attempted on an expired session."""
    pass


@dataclass
class ContextEntry:
    """
    One piece of context retrieved and stored during a session.

    Attributes
    ----------
    node_type : str
        Neo4j label of the node this entry represents.
        Must be a valid type from the active ontology profile.
        e.g. 'APIFunction', 'SoftwareRequirement', 'Register'
    node_id : str
        The unique identifier of the node (e.g. requirement_id, function_name).
    data : dict
        The actual node properties returned from Neo4j or Qdrant.
    source : str
        Where this entry came from: 'neo4j' | 'qdrant' | 'manual'
    retrieved_at : datetime
        UTC timestamp when this entry was added.
    relevance_score : float
        Optional score (0.0–1.0) indicating how relevant this entry is.
        For Qdrant results this is the cosine similarity score.
        For Neo4j results default is 1.0.
    query_text : str
        The original query that caused this entry to be retrieved.
    """
    node_type:       str
    node_id:         str
    data:            Dict[str, Any]
    source:          str                  = "neo4j"
    retrieved_at:    datetime             = field(default_factory=lambda: datetime.now(timezone.utc))
    relevance_score: float                = 1.0
    query_text:      str                  = ""


@dataclass
class Session:
    """
    One working memory session.

    Attributes
    ----------
    session_id : str
        Auto-generated UUID for this session.
    project : str
        Project identifier (lowercased).
    module : str
        Module identifier (lowercased).
    profile : str
        Ontology profile name ('mcal' or 'illd').
    ttl_seconds : int
        Time-to-live in seconds. Session expires after this many seconds
        from created_at.
    created_at : datetime
        UTC timestamp when this session was created.
    last_accessed : datetime
        UTC timestamp of the last read or write operation.
    context : list[ContextEntry]
        All context entries accumulated during this session.
    metadata : dict
        Arbitrary key-value metadata (e.g. user_id, task_description).
    """
    session_id:    str                   = field(default_factory=lambda: str(uuid.uuid4()))
    project:       str                   = ""
    module:        str                   = ""
    profile:       str                   = "illd"
    ttl_seconds:   int                   = 3600
    created_at:    datetime              = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime              = field(default_factory=lambda: datetime.now(timezone.utc))
    context:       List[ContextEntry]    = field(default_factory=list)
    metadata:      Dict[str, Any]        = field(default_factory=dict)

    # ─────────────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def expires_at(self) -> datetime:
        """Absolute UTC datetime when this session expires."""
        return self.created_at + timedelta(seconds=self.ttl_seconds)

    @property
    def is_expired(self) -> bool:
        """True if the session has passed its TTL."""
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def remaining_seconds(self) -> float:
        """Seconds until expiry. Negative means already expired."""
        delta = self.expires_at - datetime.now(timezone.utc)
        return delta.total_seconds()

    def touch(self) -> None:
        """Update last_accessed timestamp. Call after every read/write."""
        self.last_accessed = datetime.now(timezone.utc)

    def _check_not_expired(self) -> None:
        if self.is_expired:
            raise SessionExpiredError(
                f"Session {self.session_id} expired at {self.expires_at.isoformat()} "
                f"(project={self.project}, module={self.module})"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXT MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def add_entry(self, entry: ContextEntry) -> None:
        """
        Add a context entry to this session.

        Raises SessionExpiredError if the session has expired.
        """
        self._check_not_expired()
        self.context.append(entry)
        self.touch()
        logger.debug(
            f"[Session {self.session_id[:8]}] Added {entry.node_type} "
            f"'{entry.node_id}' from {entry.source}"
        )

    def get_entries(
        self,
        node_type: Optional[str] = None,
        source: Optional[str] = None,
        min_score: float = 0.0,
    ) -> List[ContextEntry]:
        """
        Retrieve context entries, optionally filtered.

        Parameters
        ----------
        node_type : str, optional
            Filter to entries of this node type (e.g. 'APIFunction').
        source : str, optional
            Filter to entries from this source ('neo4j' | 'qdrant').
        min_score : float
            Only return entries with relevance_score >= min_score.

        Returns
        -------
        List of ContextEntry, sorted by retrieved_at descending (newest first).

        Raises SessionExpiredError if the session has expired.
        """
        self._check_not_expired()
        self.touch()
        entries = self.context
        if node_type:
            entries = [e for e in entries if e.node_type == node_type]
        if source:
            entries = [e for e in entries if e.source == source]
        if min_score > 0.0:
            entries = [e for e in entries if e.relevance_score >= min_score]
        return sorted(entries, key=lambda e: e.retrieved_at, reverse=True)

    def clear_context(self) -> int:
        """
        Remove all context entries from this session.
        Returns the number of entries removed.
        """
        count = len(self.context)
        self.context = []
        self.touch()
        logger.info(f"[Session {self.session_id[:8]}] Cleared {count} context entries")
        return count

    def get_node_type_counts(self) -> Dict[str, int]:
        """Return a count of context entries grouped by node_type."""
        counts: Dict[str, int] = {}
        for entry in self.context:
            counts[entry.node_type] = counts.get(entry.node_type, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the session to a plain dict (for storage / logging)."""
        return {
            "session_id":    self.session_id,
            "project":       self.project,
            "module":        self.module,
            "profile":       self.profile,
            "ttl_seconds":   self.ttl_seconds,
            "created_at":    self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "expires_at":    self.expires_at.isoformat(),
            "is_expired":    self.is_expired,
            "context_count": len(self.context),
            "node_type_counts": self.get_node_type_counts(),
            "metadata":      self.metadata,
        }

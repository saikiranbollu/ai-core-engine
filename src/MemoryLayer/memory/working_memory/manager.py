"""
WorkingMemoryManager
====================
Manages all active working memory sessions.

This implements Jira Tickets 1 + 2:
  - Ticket 1 (AICE-MEM-002): project + module scoping per session
  - Ticket 2 (AICE-MEM-004/005): TTL-based expiry + pluggable backend

Everything is driven from ontology.yaml — the allowed node types,
profiles, and module names all come from the ontology at runtime.

Architecture
------------
  WorkingMemoryManager
      │
      ├── validates project/module against ontology.yaml
      ├── creates Session objects with correct profile
      ├── stores sessions in a backend (InMemoryBackend or RedisBackend)
      └── auto-purges expired sessions on every operation

Backends
--------
  InMemoryBackend   — dict in RAM, sessions lost on process restart
                      Good for: development, testing, single-process
  RedisBackend      — persists sessions in Redis with native TTL
                      Good for: production, multi-process, restarts

Usage:
    from memory.working_memory import WorkingMemoryManager
    from memory.ontology_loader import get_ontology

    ontology = get_ontology()
    wm = WorkingMemoryManager(ontology=ontology, profile="illd")

    # Create a session
    session_id = wm.create_session(
        project="proj_a", module="cxpi", ttl_seconds=3600
    )

    # Add context from a Neo4j query result
    wm.add_context(
        session_id=session_id,
        node_type="Function",
        node_id="IfxCxpi_initChannel",
        data={"function_name": "IfxCxpi_initChannel", "return_type": "void"},
        source="neo4j",
        query_text="initialise cxpi channel",
    )

    # Retrieve context later
    entries = wm.get_context(session_id, node_type="Function")

    # Close when done
    wm.close_session(session_id)
"""

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .session import ContextEntry, Session, SessionExpiredError
from ..ontology_loader import OntologyLoader

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STORAGE BACKENDS
# ─────────────────────────────────────────────────────────────────────────────

class SessionBackend(ABC):
    """Abstract base — all backends implement these 5 methods."""

    @abstractmethod
    def save(self, session: Session) -> None: ...

    @abstractmethod
    def load(self, session_id: str) -> Optional[Session]: ...

    @abstractmethod
    def delete(self, session_id: str) -> bool: ...

    @abstractmethod
    def list_ids(self) -> List[str]: ...

    @abstractmethod
    def close(self) -> None: ...


class InMemoryBackend(SessionBackend):
    """
    Thread-safe in-memory session store.
    Sessions are lost when the process exits.
    Use for development, testing, and single-process deployments.
    """

    def __init__(self):
        self._store: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def save(self, session: Session) -> None:
        with self._lock:
            self._store[session.session_id] = session

    def load(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._store.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._store:
                del self._store[session_id]
                return True
            return False

    def list_ids(self) -> List[str]:
        with self._lock:
            return list(self._store.keys())

    def close(self) -> None:
        pass  # nothing to release


class RedisBackend(SessionBackend):
    """
    Redis-backed session store with native TTL support.

    Sessions are serialised to JSON and stored in Redis with an expiry
    matching the session TTL.  Across process restarts or multiple
    workers, sessions are shared via Redis.

    Parameters
    ----------
    host : str
        Redis host.
    port : int
        Redis port (default 6379).
    db : int
        Redis database index (default 0).
    key_prefix : str
        Key prefix for all session keys in Redis (default 'wm:session:').
    password : str, optional
        Redis AUTH password.
    """

    def __init__(
        self,
        host:       str = "localhost",
        port:       int = 6379,
        db:         int = 0,
        key_prefix: str = "wm:session:",
        password:   Optional[str] = None,
    ):
        try:
            import redis
            import json as _json
            self._json = _json
        except ImportError:
            raise ImportError(
                "[RedisBackend] 'redis' package not installed. "
                "Run: pip install redis"
            )
        self._prefix = key_prefix
        self._redis = redis.Redis(
            host=host, port=port, db=db,
            password=password,
            decode_responses=True,
        )
        logger.info(f"[RedisBackend] Connected to Redis {host}:{port} db={db}")

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def save(self, session: Session) -> None:
        import dataclasses
        data = self._serialise(session)
        ttl = max(1, int(session.remaining_seconds))
        self._redis.setex(self._key(session.session_id), ttl, self._json.dumps(data))

    def load(self, session_id: str) -> Optional[Session]:
        raw = self._redis.get(self._key(session_id))
        if raw is None:
            return None
        return self._deserialise(self._json.loads(raw))

    def delete(self, session_id: str) -> bool:
        return bool(self._redis.delete(self._key(session_id)))

    def list_ids(self) -> List[str]:
        prefix_len = len(self._prefix)
        return [k[prefix_len:] for k in self._redis.keys(f"{self._prefix}*")]

    def close(self) -> None:
        self._redis.close()

    # ── serialisation helpers ────────────────────────────────────────────────

    def _serialise(self, session: Session) -> Dict[str, Any]:
        return {
            "session_id":    session.session_id,
            "project":       session.project,
            "module":        session.module,
            "profile":       session.profile,
            "ttl_seconds":   session.ttl_seconds,
            "created_at":    session.created_at.isoformat(),
            "last_accessed": session.last_accessed.isoformat(),
            "metadata":      session.metadata,
            "context": [
                {
                    "node_type":       e.node_type,
                    "node_id":         e.node_id,
                    "data":            e.data,
                    "source":          e.source,
                    "retrieved_at":    e.retrieved_at.isoformat(),
                    "relevance_score": e.relevance_score,
                    "query_text":      e.query_text,
                }
                for e in session.context
            ],
        }

    def _deserialise(self, data: Dict[str, Any]) -> Session:
        from datetime import datetime
        context = [
            ContextEntry(
                node_type=e["node_type"],
                node_id=e["node_id"],
                data=e["data"],
                source=e["source"],
                retrieved_at=datetime.fromisoformat(e["retrieved_at"]),
                relevance_score=e["relevance_score"],
                query_text=e["query_text"],
            )
            for e in data.get("context", [])
        ]
        return Session(
            session_id=data["session_id"],
            project=data["project"],
            module=data["module"],
            profile=data["profile"],
            ttl_seconds=data["ttl_seconds"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_accessed=datetime.fromisoformat(data["last_accessed"]),
            context=context,
            metadata=data.get("metadata", {}),
        )


# ─────────────────────────────────────────────────────────────────────────────
# WORKING MEMORY MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class WorkingMemoryManager:
    """
    Creates and manages working memory sessions.

    Parameters
    ----------
    ontology : OntologyLoader
        The loaded ontology — used to validate node types and modules.
    profile : str
        Ontology profile to use ('illd' or 'mcal').
    backend : SessionBackend, optional
        Storage backend.  Defaults to InMemoryBackend.
    default_ttl_seconds : int
        Default session TTL if not specified per-session.  Default: 3600 (1 hour).
    """

    def __init__(
        self,
        ontology:             OntologyLoader,
        profile:              str            = "illd",
        backend:              Optional[SessionBackend] = None,
        default_ttl_seconds:  int            = 3600,
    ):
        self._ontology    = ontology
        self._profile     = profile
        self._backend     = backend or InMemoryBackend()
        self._default_ttl = default_ttl_seconds

        # Load valid node types and modules from ontology — no hardcoding
        self._valid_node_types = set(ontology.get_node_type_names(profile))
        self._valid_modules    = {m.lower() for m in ontology.get_supported_modules(profile)}

        logger.info(
            f"[WorkingMemoryManager] Ready — profile={profile}, "
            f"node_types={len(self._valid_node_types)}, "
            f"backend={type(self._backend).__name__}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS — auto-purge expired sessions on access
    # ─────────────────────────────────────────────────────────────────────────

    def _load_active_or_none(self, session_id: str) -> Optional[Session]:
        """
        Load a session.  If expired, auto-delete from backend and return None.
        Returns None if the session does not exist or has expired.
        """
        session = self._backend.load(session_id)
        if session is not None and session.is_expired:
            self._backend.delete(session_id)
            logger.info(
                f"[WorkingMemoryManager] Auto-purged expired session {session_id[:8]}…"
            )
            return None
        return session

    def _load_active(self, session_id: str) -> Session:
        """
        Load a session.  Auto-deletes expired sessions and raises.

        Raises
        ------
        ValueError
            If the session does not exist.
        SessionExpiredError
            If the session has expired (also deletes it from backend).
        """
        session = self._backend.load(session_id)
        if session is None:
            raise ValueError(f"[WorkingMemoryManager] Session '{session_id}' not found.")
        if session.is_expired:
            self._backend.delete(session_id)
            raise SessionExpiredError(
                f"Session {session_id} expired at {session.expires_at.isoformat()} "
                f"and has been removed (project={session.project}, module={session.module})"
            )
        return session

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    def create_session(
        self,
        project:      str,
        module:       str,
        ttl_seconds:  Optional[int]        = None,
        metadata:     Optional[Dict[str, Any]] = None,
        session_id:   Optional[str]        = None,
    ) -> str:
        """
        Create a new working memory session for a project + module.

        The module is validated against the ontology's supported_modules list.
        If the module is not in the ontology yet (partial ontology), a warning
        is logged but the session is still created — this supports the
        "partial ontology, add more later" workflow.

        Parameters
        ----------
        project : str
            Project identifier (case-insensitive, stored lowercase).
        module : str
            Module name (case-insensitive, stored lowercase).
            Should match a module in ontology.yaml supported_modules.
        ttl_seconds : int, optional
            Session TTL.  Uses default_ttl_seconds if not provided.
        metadata : dict, optional
            Arbitrary metadata to attach to the session.

        Returns
        -------
        str
            The new session_id (UUID string).
        """
        project = project.lower()
        module  = module.lower()

        if module not in self._valid_modules:
            logger.warning(
                f"[WorkingMemoryManager] Module '{module}' is not in ontology "
                f"supported_modules for profile '{self._profile}'. "
                f"Session created anyway — update ontology.yaml when confirmed."
            )

        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl

        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            project=project,
            module=module,
            profile=self._profile,
            ttl_seconds=ttl,
            metadata=metadata or {},
        )
        self._backend.save(session)

        logger.info(
            f"[WorkingMemoryManager] Created session {session.session_id[:8]}… "
            f"project={project} module={module} ttl={ttl}s"
        )
        return session.session_id

    def get_session(self, session_id: str) -> Optional[Session]:
        """
        Retrieve a session by ID.

        Returns None if not found or if the session has expired.
        Expired sessions are automatically removed from the backend.
        """
        return self._load_active_or_none(session_id)

    def close_session(self, session_id: str) -> bool:
        """
        Explicitly close and delete a session.

        Returns True if deleted, False if not found.
        """
        deleted = self._backend.delete(session_id)
        if deleted:
            logger.info(f"[WorkingMemoryManager] Closed session {session_id[:8]}…")
        return deleted

    def extend_session(self, session_id: str, extra_seconds: int) -> bool:
        """
        Extend the TTL of an existing session.

        Adds extra_seconds to the original TTL.
        Returns True if successful, False if session not found.
        """
        session = self._backend.load(session_id)
        if session is None:
            return False
        if session.is_expired:
            self._backend.delete(session_id)
            return False
        session.ttl_seconds += extra_seconds
        self._backend.save(session)
        logger.info(
            f"[WorkingMemoryManager] Extended session {session_id[:8]}… "
            f"by {extra_seconds}s → new TTL={session.ttl_seconds}s"
        )
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXT MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def add_context(
        self,
        session_id:      str,
        node_type:       str,
        node_id:         str,
        data:            Dict[str, Any],
        source:          str   = "neo4j",
        relevance_score: float = 1.0,
        query_text:      str   = "",
    ) -> None:
        """
        Add a context entry to an existing session.

        The node_type is validated against the ontology — unknown types
        log a warning but are still stored (supports partial ontology).

        Parameters
        ----------
        session_id : str
            Target session.
        node_type : str
            Neo4j label (must be in ontology node_types for current profile).
        node_id : str
            Unique identifier of the node.
        data : dict
            Node properties.
        source : str
            'neo4j' | 'qdrant' | 'manual'
        relevance_score : float
            Cosine similarity (Qdrant) or 1.0 for exact graph matches.
        query_text : str
            The original query that triggered this retrieval.

        Raises
        ------
        ValueError
            If session_id does not exist.
        SessionExpiredError
            If the session has expired.
        """
        session = self._load_active(session_id)

        if node_type not in self._valid_node_types:
            logger.warning(
                f"[WorkingMemoryManager] node_type '{node_type}' not in ontology "
                f"for profile '{self._profile}'. Adding anyway."
            )

        entry = ContextEntry(
            node_type=node_type,
            node_id=node_id,
            data=data,
            source=source,
            relevance_score=relevance_score,
            query_text=query_text,
        )
        session.add_entry(entry)
        self._backend.save(session)

    def get_context(
        self,
        session_id: str,
        node_type:  Optional[str]  = None,
        source:     Optional[str]  = None,
        min_score:  float          = 0.0,
    ) -> List[ContextEntry]:
        """
        Retrieve context entries from a session with optional filtering.

        Parameters
        ----------
        session_id : str
            Target session.
        node_type : str, optional
            Filter by ontology node type label.
        source : str, optional
            Filter by source ('neo4j' | 'qdrant').
        min_score : float
            Minimum relevance_score threshold.

        Returns
        -------
        List[ContextEntry], newest first.

        Raises
        ------
        ValueError
            If session not found.
        SessionExpiredError
            If the session has expired.
        """
        session = self._load_active(session_id)
        return session.get_entries(node_type=node_type, source=source, min_score=min_score)

    def clear_context(self, session_id: str) -> int:
        """
        Clear all context entries from a session.
        Returns the number of entries removed.
        """
        session = self._load_active(session_id)
        count = session.clear_context()
        self._backend.save(session)
        return count

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION LISTING AND CLEANUP
    # ─────────────────────────────────────────────────────────────────────────

    def list_active_sessions(
        self,
        project: Optional[str] = None,
        module:  Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List all non-expired sessions, optionally filtered by project/module.

        Returns a list of session summary dicts (not full Session objects).
        """
        result = []
        for sid in self._backend.list_ids():
            session = self._backend.load(sid)
            if session is None:
                continue
            if session.is_expired:
                self._backend.delete(sid)
                continue
            if project and session.project != project.lower():
                continue
            if module and session.module != module.lower():
                continue
            result.append(session.to_dict())
        return result

    def purge_expired_sessions(self) -> int:
        """
        Delete all sessions that have passed their TTL.

        Returns the number of sessions deleted.
        """
        purged = 0
        for sid in self._backend.list_ids():
            session = self._backend.load(sid)
            if session is not None and session.is_expired:
                self._backend.delete(sid)
                purged += 1
                logger.debug(f"[WorkingMemoryManager] Purged expired session {sid[:8]}…")
        if purged:
            logger.info(f"[WorkingMemoryManager] Purged {purged} expired sessions")
        return purged

    # ─────────────────────────────────────────────────────────────────────────
    # KEY-VALUE DATA STORAGE (via Session.metadata)
    # ─────────────────────────────────────────────────────────────────────────

    def store_data(self, session_id: str, key: str, value: Any) -> None:
        """
        Store an arbitrary key-value pair in a session's metadata dict.

        This is the generic storage counterpart to the structured
        ``add_context`` method — used by MCP ``session_store`` and by
        domain-assistant helpers (store_rag_results, etc.).

        Raises
        ------
        ValueError
            If session_id does not exist.
        SessionExpiredError
            If the session has expired.
        """
        session = self._load_active(session_id)
        session.metadata[key] = value
        session.touch()
        self._backend.save(session)

    def retrieve_data(self, session_id: str, key: str) -> Any:
        """
        Retrieve a value from a session's metadata dict.

        Returns ``None`` if the key does not exist.

        Raises
        ------
        ValueError
            If session_id does not exist.
        SessionExpiredError
            If the session has expired.
        """
        session = self._load_active(session_id)
        session.touch()
        self._backend.save(session)
        return session.metadata.get(key)

    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return a summary dict for one session, or None if not found or expired."""
        session = self._load_active_or_none(session_id)
        return session.to_dict() if session else None

    # ─────────────────────────────────────────────────────────────────────────
    # INTROSPECTION
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def valid_node_types(self) -> List[str]:
        """Node types accepted by this manager (from ontology)."""
        return sorted(self._valid_node_types)

    @property
    def valid_modules(self) -> List[str]:
        """Module names accepted by this manager (from ontology)."""
        return sorted(self._valid_modules)

    @property
    def profile(self) -> str:
        """Active ontology profile name."""
        return self._profile

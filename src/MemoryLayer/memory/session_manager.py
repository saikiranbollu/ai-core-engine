"""
Session Manager — Sprint 2 Lightweight Implementation
======================================================
Provides session lifecycle for MCP tools without requiring ontology.yaml.
Compatible with the full WorkingMemoryManager API so we can swap later.

Supports two backends:
  - DictBackend: in-memory dict (dev/testing)
  - RedisBackend: Redis with TTL (production)

The full WorkingMemoryManager (ontology-validated, TTL-enforced) from
src/MemoryLayer/memory/working_memory/ will replace this in Sprint 3
when we integrate the complete Memory Layer package.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionExpiredError(Exception):
    pass


@dataclass
class SessionData:
    """Lightweight session container."""
    session_id: str
    assistant_name: str = ""
    module_context: str = ""
    workspace_id: str = "illd"
    ttl_seconds: int = 3600
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    store: Dict[str, Any] = field(default_factory=dict)
    context_entries: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return time.time() > (self.created_at + self.ttl_seconds)

    @property
    def remaining_seconds(self) -> float:
        return max(0, (self.created_at + self.ttl_seconds) - time.time())

    def touch(self):
        self.last_accessed = time.time()

    def check_alive(self):
        if self.is_expired:
            raise SessionExpiredError(f"Session '{self.session_id}' has expired.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "assistant_name": self.assistant_name,
            "module_context": self.module_context,
            "workspace_id": self.workspace_id,
            "ttl_seconds": self.ttl_seconds,
            "created_at": datetime.fromtimestamp(self.created_at, tz=timezone.utc).isoformat(),
            "remaining_seconds": round(self.remaining_seconds, 1),
            "is_expired": self.is_expired,
            "store_keys": list(self.store.keys()),
            "context_entry_count": len(self.context_entries),
        }


class DictBackend:
    """Thread-safe in-memory session store."""
    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}
        self._lock = threading.Lock()

    def save(self, session: SessionData):
        with self._lock:
            self._sessions[session.session_id] = session

    def load(self, session_id: str) -> Optional[SessionData]:
        with self._lock:
            return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def list_ids(self) -> List[str]:
        with self._lock:
            return list(self._sessions.keys())


class RedisSessionBackend:
    """Redis-backed session store with native TTL."""
    def __init__(self, redis_client, prefix: str = "aice:session:"):
        self._redis = redis_client
        self._prefix = prefix

    def _key(self, sid: str) -> str:
        return f"{self._prefix}{sid}"

    def save(self, session: SessionData):
        data = {
            "session_id": session.session_id,
            "assistant_name": session.assistant_name,
            "module_context": session.module_context,
            "workspace_id": session.workspace_id,
            "ttl_seconds": session.ttl_seconds,
            "created_at": session.created_at,
            "last_accessed": session.last_accessed,
            "store": session.store,
            "context_entries": session.context_entries,
        }
        ttl = max(1, int(session.remaining_seconds))
        self._redis.setex(self._key(session.session_id), ttl, json.dumps(data, default=str))

    def load(self, session_id: str) -> Optional[SessionData]:
        raw = self._redis.get(self._key(session_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return SessionData(**{k: v for k, v in d.items() if k in SessionData.__dataclass_fields__})

    def delete(self, session_id: str) -> bool:
        return bool(self._redis.delete(self._key(session_id)))

    def list_ids(self) -> List[str]:
        plen = len(self._prefix)
        return [k[plen:] for k in self._redis.keys(f"{self._prefix}*")]


class SessionManager:
    """
    Lightweight session manager for MCP server.
    Sprint 8: Optional PostgreSQL write-through for cross-process visibility.

    Usage:
        mgr = SessionManager()  # DictBackend
        mgr = SessionManager(backend=RedisSessionBackend(redis_client))
        mgr = SessionManager(postgres_client=pg_client)  # With PostgreSQL persistence
    """
    def __init__(self, backend=None, postgres_client=None):
        self._backend = backend or DictBackend()
        self._pg = postgres_client  # Optional PostgresClient for session metadata

    def create(self, session_id: str, assistant_name: str = "",
               module_context: str = "", ttl_seconds: int = 3600,
               workspace_id: str = "illd") -> SessionData:
        if self._backend.load(session_id) is not None:
            raise ValueError(f"Session '{session_id}' already exists.")
        session = SessionData(
            session_id=session_id,
            assistant_name=assistant_name,
            module_context=module_context,
            workspace_id=workspace_id,
            ttl_seconds=ttl_seconds,
        )
        self._backend.save(session)
        if self._pg:
            self._pg.save_session_meta(
                session_id=session_id, assistant_name=assistant_name,
                module_context=module_context, workspace_id=workspace_id,
                ttl_seconds=ttl_seconds,
            )
        logger.info("[SessionMgr] Created %s (module=%s, ttl=%ds)", session_id, module_context, ttl_seconds)
        return session

    def get(self, session_id: str) -> Optional[SessionData]:
        s = self._backend.load(session_id)
        if s and s.is_expired:
            self._backend.delete(session_id)
            return None
        return s

    def get_or_raise(self, session_id: str) -> SessionData:
        s = self.get(session_id)
        if s is None:
            raise ValueError(f"Session '{session_id}' not found or expired.")
        s.check_alive()
        return s

    def store(self, session_id: str, key: str, value: Any):
        s = self.get_or_raise(session_id)
        s.store[key] = value
        s.touch()
        self._backend.save(s)

    def retrieve(self, session_id: str, key: str) -> Any:
        s = self.get_or_raise(session_id)
        s.touch()
        self._backend.save(s)
        return s.store.get(key)

    def add_context(self, session_id: str, entries: List[Dict[str, Any]]):
        s = self.get_or_raise(session_id)
        s.context_entries.extend(entries)
        s.touch()
        self._backend.save(s)

    def close(self, session_id: str, persist_audit: bool = True) -> Dict[str, Any]:
        s = self._backend.load(session_id)
        summary = s.to_dict() if s else {"session_id": session_id, "found": False}
        if s:
            summary["total_store_keys"] = len(s.store)
            summary["total_context_entries"] = len(s.context_entries)
            if self._pg:
                self._pg.close_session_meta(
                    session_id=session_id,
                    store_keys=list(s.store.keys()),
                    context_count=len(s.context_entries),
                )
        self._backend.delete(session_id)
        logger.info("[SessionMgr] Closed %s (persist_audit=%s)", session_id, persist_audit)
        return summary

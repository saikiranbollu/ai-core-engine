"""
Working Memory package
======================
Session-scoped, TTL-based context storage.

Implements Jira Tickets 1 (project/module scoping) and 2 (TTL management).
"""

from .session import ContextEntry, Session, SessionExpiredError
from .manager import WorkingMemoryManager, InMemoryBackend, RedisBackend, SessionBackend

__all__ = [
    "ContextEntry",
    "Session",
    "SessionExpiredError",
    "WorkingMemoryManager",
    "InMemoryBackend",
    "RedisBackend",
    "SessionBackend",
]

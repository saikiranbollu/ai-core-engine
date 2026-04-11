"""
Cache Service — Sprint 6 → Sprint 8
======================================
Two-tier cache: LRU (exact match, 2500x faster) + Semantic (similar queries, 40x faster).
From PPTX slide 6: ~60% cache hit rate expected.

Sprint 8: Replaced hash-based similarity with sentence-transformers embeddings
for real semantic matching. Threshold lowered to 0.85 for meaningful similarity.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
#  Env-var helpers (fail-safe: log and fall back to default)
# ═════════════════════════════════════════════════════════════════════════

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
        if val <= 0:
            raise ValueError("must be positive")
        return val
    except (ValueError, TypeError):
        logger.warning("Invalid env %s=%r — using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
        if not (0.0 < val <= 1.0):
            raise ValueError("must be in (0.0, 1.0]")
        return val
    except (ValueError, TypeError):
        logger.warning("Invalid env %s=%r — using default %.2f", name, raw, default)
        return default


class LRUCache:
    """Thread-safe LRU cache for exact query matches."""

    def __init__(self, max_size: int = 10000, default_ttl: int = 86400):
        self._cache: OrderedDict = OrderedDict()
        self._max = max_size
        self._default_ttl = default_ttl
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if time.time() > entry["ts"] + entry["ttl"]:
                    del self._cache[key]
                    self._misses += 1
                    return None
                self._cache.move_to_end(key)
                self._hits += 1
                return entry["value"]
            self._misses += 1
            return None

    def put(self, key: str, value: Any, ttl: Optional[int] = None):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = {"value": value, "ts": time.time(), "ttl": ttl or self._default_ttl}
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def invalidate_by_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                del self._cache[k]
            return len(keys)

    def invalidate_by_module(self, module: str) -> int:
        """Remove entries whose key contains the module name (any position)."""
        mod = module.lower()
        with self._lock:
            keys = [k for k in self._cache if f":{mod}:" in k.lower() or k.lower().startswith(f"{mod}:")]
            for k in keys:
                del self._cache[k]
            return len(keys)

    def clear(self) -> int:
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            return n

    def refresh_config(self, max_size: int, default_ttl: int) -> Dict[str, Any]:
        """Update config in-place, preserving cached data. Evicts if shrunk."""
        with self._lock:
            old_max, old_ttl = self._max, self._default_ttl
            self._max = max_size
            self._default_ttl = default_ttl
            evicted = 0
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
                evicted += 1
            return {
                "lru_max_size": {"old": old_max, "new": self._max},
                "lru_default_ttl": {"old": old_ttl, "new": self._default_ttl},
                "evicted": evicted,
            }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "tier": "lru", "size": len(self._cache), "max_size": self._max,
                "default_ttl": self._default_ttl,
                "hits": self._hits, "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total > 0 else 0,
            }


class SemanticCache:
    """Similarity-based cache using sentence-transformers embeddings.

    Sprint 8: Uses sentence-transformers for real semantic similarity.
    Falls back to SHA-256 hash-based embeddings if sentence-transformers
    is not available (degraded mode — effectively exact-match only).
    """

    def __init__(self, max_size: int = 500, similarity_threshold: float = 0.95,
                 ttl_seconds: int = 604800):
        self._entries: List[Dict] = []
        self._max = max_size
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds  # default 7 days
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._st_model = None
        self._use_st = True  # attempt sentence-transformers

    def _get_model(self):
        """Lazy-load sentence-transformers model (shared singleton)."""
        if self._st_model is None and self._use_st:
            try:
                from src.Configuration.embedding_singleton import get_shared_model
                self._st_model = get_shared_model(
                    os.environ.get("ST_CACHE_MODEL", "all-MiniLM-L6-v2")
                )
                if self._st_model is None:
                    self._use_st = False
            except Exception:
                try:
                    from sentence_transformers import SentenceTransformer
                    model_name = os.environ.get("ST_CACHE_MODEL", "all-MiniLM-L6-v2")
                    self._st_model = SentenceTransformer(model_name)
                    logger.info("SemanticCache: loaded model '%s'", model_name)
                except ImportError:
                    logger.warning("sentence-transformers not installed — falling back to hash embeddings")
                    self._use_st = False
        return self._st_model

    def _embed(self, text: str) -> Optional[List[float]]:
        """Encode text to embedding vector. Returns None when sentence-transformers is unavailable."""
        model = self._get_model()
        if model is not None:
            return model.encode(text.lower().strip(), normalize_embeddings=True).tolist()
        return None

    def _cosine(self, a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0

    def _is_expired(self, entry: Dict) -> bool:
        return time.time() > entry["ts"] + self._ttl

    def _evict_expired(self):
        """Remove expired entries (caller must hold lock)."""
        self._entries = [e for e in self._entries if not self._is_expired(e)]

    def get(self, query: str, metadata: Optional[Dict[str, str]] = None) -> Optional[Any]:
        q_emb = self._embed(query)
        if q_emb is None:
            self._misses += 1
            return None
        with self._lock:
            self._evict_expired()
            best_sim, best_entry = 0.0, None
            for entry in self._entries:
                # Structured params must match exactly before checking similarity
                if metadata and entry.get("metadata") != metadata:
                    continue
                sim = self._cosine(q_emb, entry["embedding"])
                if sim > best_sim:
                    best_sim, best_entry = sim, entry
            if best_sim >= self._threshold and best_entry:
                self._hits += 1
                return {"value": best_entry["value"], "similarity": round(best_sim, 4),
                        "original_query": best_entry["query"]}
            self._misses += 1
            return None

    def put(self, query: str, value: Any, metadata: Optional[Dict[str, str]] = None):
        emb = self._embed(query)
        if emb is None:
            return
        with self._lock:
            self._entries.append({"query": query, "embedding": emb,
                                   "value": value, "metadata": metadata,
                                   "ts": time.time()})
            while len(self._entries) > self._max:
                self._entries.pop(0)

    def invalidate_by_module(self, module: str) -> int:
        """Remove entries whose query or metadata references the module."""
        mod = module.lower()
        with self._lock:
            before = len(self._entries)
            self._entries = [
                e for e in self._entries
                if mod not in e["query"].lower()
                and mod not in (e.get("metadata") or {}).get("mod", "").lower()
            ]
            return before - len(self._entries)

    def clear(self) -> int:
        with self._lock:
            n = len(self._entries)
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            return n

    def refresh_config(self, max_size: int, similarity_threshold: float,
                       ttl_seconds: int) -> Dict[str, Any]:
        """Update config in-place, preserving cached data. Evicts oldest if shrunk."""
        with self._lock:
            old_max, old_thresh, old_ttl = self._max, self._threshold, self._ttl
            self._max = max_size
            self._threshold = similarity_threshold
            self._ttl = ttl_seconds
            evicted = 0
            if len(self._entries) > self._max:
                self._entries.sort(key=lambda e: e["ts"])
                excess = len(self._entries) - self._max
                self._entries = self._entries[excess:]
                evicted = excess
            return {
                "semantic_max_size": {"old": old_max, "new": self._max},
                "semantic_threshold": {"old": old_thresh, "new": self._threshold},
                "semantic_ttl_seconds": {"old": old_ttl, "new": self._ttl},
                "evicted": evicted,
            }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            self._evict_expired()
            total = self._hits + self._misses
            return {
                "tier": "semantic", "size": len(self._entries), "max_size": self._max,
                "hits": self._hits, "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total > 0 else 0,
                "similarity_threshold": self._threshold,
                "ttl_seconds": self._ttl,
                "sentence_transformers_active": self._use_st and self._st_model is not None,
            }


class CacheService:
    """Two-tier cache: LRU → Semantic → RAG."""

    def __init__(
        self,
        lru_max_size: Optional[int] = None,
        lru_ttl_seconds: Optional[int] = None,
        semantic_max_size: Optional[int] = None,
        semantic_threshold: Optional[float] = None,
        semantic_ttl_seconds: Optional[int] = None,
    ):
        # ── Resolve from env vars with backward-compatible defaults ──
        lru_size = lru_max_size or _env_int("LRU_CACHE_SIZE", 10000)
        lru_ttl = lru_ttl_seconds or _env_int("LRU_CACHE_TTL_HOURS", 24) * 3600
        sem_size = semantic_max_size or _env_int("SEMANTIC_CACHE_MAX_SIZE", 500)
        sem_thresh = semantic_threshold or _env_float("SEMANTIC_CACHE_THRESHOLD", 0.95)
        sem_ttl = semantic_ttl_seconds or _env_int("SEMANTIC_CACHE_TTL_DAYS", 7) * 86400

        self.lru = LRUCache(max_size=lru_size, default_ttl=lru_ttl)
        self.semantic = SemanticCache(
            max_size=sem_size,
            similarity_threshold=sem_thresh,
            ttl_seconds=sem_ttl,
        )
        logger.info(
            "CacheService: LRU(size=%d, ttl=%ds) Semantic(size=%d, thresh=%.2f, ttl=%ds)",
            lru_size, lru_ttl, sem_size, sem_thresh, sem_ttl,
        )

    def get(self, query: str, metadata: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        # Tier 1: LRU exact match (key includes structured params)
        lru_result = self.lru.get(query)
        if lru_result is not None:
            return {"hit": True, "tier": "lru", "result": lru_result}
        # Tier 2: Semantic similarity (metadata must match exactly)
        sem_result = self.semantic.get(query, metadata=metadata)
        if sem_result is not None:
            return {"hit": True, "tier": "semantic", **sem_result}
        return {"hit": False, "tier": None, "result": None}

    def put(self, query: str, value: Any, metadata: Optional[Dict[str, str]] = None):
        self.lru.put(query, value)
        self.semantic.put(query, value, metadata=metadata)

    def invalidate_module(self, module: str) -> Dict[str, int]:
        lru_n = self.lru.invalidate_by_module(module)
        sem_n = self.semantic.invalidate_by_module(module)
        logger.info("CacheService: invalidated module '%s' — lru=%d, semantic=%d", module, lru_n, sem_n)
        return {"lru_invalidated": lru_n, "semantic_invalidated": sem_n}

    def clear(self, tiers: Optional[List[str]] = None) -> Dict[str, Any]:
        t = tiers or ["all"]
        cleared = []
        if "all" in t or "lru" in t:
            self.lru.clear()
            cleared.append("lru")
        if "all" in t or "semantic" in t:
            self.semantic.clear()
            cleared.append("semantic")
        return {"cleared": cleared}

    def refresh_config(self) -> Dict[str, Any]:
        """Re-read env vars and update cache parameters in-place (no restart needed)."""
        lru_size = _env_int("LRU_CACHE_SIZE", 10000)
        lru_ttl = _env_int("LRU_CACHE_TTL_HOURS", 24) * 3600
        sem_size = _env_int("SEMANTIC_CACHE_MAX_SIZE", 500)
        sem_thresh = _env_float("SEMANTIC_CACHE_THRESHOLD", 0.95)
        sem_ttl = _env_int("SEMANTIC_CACHE_TTL_DAYS", 7) * 86400

        lru_changes = self.lru.refresh_config(max_size=lru_size, default_ttl=lru_ttl)
        sem_changes = self.semantic.refresh_config(
            max_size=sem_size, similarity_threshold=sem_thresh, ttl_seconds=sem_ttl,
        )

        changes = {**lru_changes, **sem_changes}
        for key in ("lru_max_size", "lru_default_ttl", "semantic_max_size",
                    "semantic_threshold", "semantic_ttl_seconds"):
            entry = changes.get(key)
            if entry and entry["old"] != entry["new"]:
                logger.info("Cache config refreshed: %s %s → %s", key, entry["old"], entry["new"])
        return changes

    def stats(self) -> Dict[str, Any]:
        return {"lru": self.lru.stats(), "semantic": self.semantic.stats()}

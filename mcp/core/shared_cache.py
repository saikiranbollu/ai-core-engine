"""
Shared Cache Manager — Redis-backed L1 + L2 cache for multi-replica MCP
=========================================================================

Replaces process-local caches (_collection_cache in SearchService) with
a two-tier design:

  L1  Process-local dict (30s TTL, evicts on size cap)
  L2  Redis (5min TTL, shared across all MCP replicas)

Serialization: msgpack for speed; falls back to json if msgpack unavailable.

Usage::

    cache = SharedCacheManager(redis_client)
    await cache.set("key", {"data": 123}, ttl=300)
    result = await cache.get("key")
    await cache.invalidate_module("Adc")
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Serialization ────────────────────────────────────────────────────
try:
    import msgpack

    def _serialize(obj: Any) -> bytes:
        return msgpack.packb(obj, use_bin_type=True, default=str)

    def _deserialize(data: bytes) -> Any:
        return msgpack.unpackb(data, raw=False)

    _SERIALIZER = "msgpack"
except ImportError:
    logger.info("[SharedCache] msgpack not available — falling back to json")

    def _serialize(obj: Any) -> bytes:
        return json.dumps(obj, default=str).encode("utf-8")

    def _deserialize(data: bytes) -> Any:
        return json.loads(data)

    _SERIALIZER = "json"


class SharedCacheManager:
    """Two-tier cache shared across all MCP replicas via Redis.

    Parameters
    ----------
    redis_client
        An async Redis client (``redis.asyncio.Redis``).
        If None, the cache operates as L1-only (process-local).
    l1_max_size : int
        Maximum entries in the L1 local cache. Default 500.
    l1_ttl : int
        L1 TTL in seconds. Default 30.
    l2_ttl : int
        L2 (Redis) TTL in seconds. Default 300 (5 min).
    key_prefix : str
        Redis key prefix for namespace isolation. Default ``"mcp_cache:"``.
    """

    def __init__(
        self,
        redis_client=None,
        l1_max_size: int = 500,
        l1_ttl: int = 30,
        l2_ttl: int = 300,
        key_prefix: str = "mcp_cache:",
    ):
        self._redis = redis_client
        self._l1_max_size = l1_max_size
        self._l1_ttl = l1_ttl
        self._l2_ttl = l2_ttl
        self._prefix = key_prefix

        # L1: {key: (value, expire_ts)}
        self._l1: Dict[str, tuple] = {}

        # Stats
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0, "sets": 0}

    # ── Core operations ───────────────────────────────────────────────

    async def get(self, key: str) -> Optional[Any]:
        """Lookup *key* through L1 → L2, returning None on miss."""
        # L1 check
        entry = self._l1.get(key)
        if entry is not None:
            value, expire_ts = entry
            if time.time() < expire_ts:
                self._stats["l1_hits"] += 1
                return value
            else:
                del self._l1[key]

        # L2 check (Redis)
        if self._redis:
            try:
                rkey = self._prefix + key
                data = await self._redis.get(rkey)
                if data is not None:
                    value = _deserialize(data if isinstance(data, bytes) else data.encode("utf-8"))
                    # Populate L1
                    self._l1_put(key, value)
                    self._stats["l2_hits"] += 1
                    return value
            except Exception as e:
                logger.warning("[SharedCache] L2 get failed for '%s': %s", key, e)

        self._stats["misses"] += 1
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store *key* → *value* in both L1 and L2.

        Parameters
        ----------
        ttl : int | None
            Override L2 TTL. If None, uses default ``l2_ttl``.
        """
        l2_ttl = ttl if ttl is not None else self._l2_ttl

        # L1
        self._l1_put(key, value)

        # L2 (Redis)
        if self._redis:
            try:
                rkey = self._prefix + key
                serialized = _serialize(value)
                await self._redis.setex(rkey, l2_ttl, serialized)
            except Exception as e:
                logger.warning("[SharedCache] L2 set failed for '%s': %s", key, e)

        self._stats["sets"] += 1

    async def delete(self, key: str):
        """Remove *key* from both L1 and L2."""
        self._l1.pop(key, None)
        if self._redis:
            try:
                await self._redis.delete(self._prefix + key)
            except Exception:
                pass

    # ── Module-level invalidation ─────────────────────────────────────

    async def invalidate_module(self, module: str) -> int:
        """Invalidate all cache entries for a specific module.

        Scans L1 for module-matching keys and uses Redis SCAN for L2.

        Returns
        -------
        int
            Number of entries removed (L1 + L2 combined).
        """
        removed = 0
        pattern = module.lower()

        # L1
        to_remove = [k for k in self._l1 if pattern in k.lower()]
        for k in to_remove:
            del self._l1[k]
            removed += 1

        # L2: SCAN for keys matching pattern
        if self._redis:
            try:
                cursor = 0
                redis_pattern = f"{self._prefix}*{module}*"
                while True:
                    cursor, keys = await self._redis.scan(
                        cursor=cursor, match=redis_pattern, count=100
                    )
                    if keys:
                        await self._redis.delete(*keys)
                        removed += len(keys)
                    if cursor == 0:
                        break
            except Exception as e:
                logger.warning("[SharedCache] L2 invalidate_module '%s': %s", module, e)

        logger.info("[SharedCache] Invalidated %d entries for module '%s'", removed, module)
        return removed

    # ── Bulk clear ────────────────────────────────────────────────────

    async def clear(self) -> int:
        """Clear all entries from L1 and L2.

        Returns
        -------
        int
            Number of entries removed.
        """
        count = len(self._l1)
        self._l1.clear()

        if self._redis:
            try:
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(
                        cursor=cursor, match=f"{self._prefix}*", count=200
                    )
                    if keys:
                        await self._redis.delete(*keys)
                        count += len(keys)
                    if cursor == 0:
                        break
            except Exception as e:
                logger.warning("[SharedCache] L2 clear: %s", e)

        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0, "sets": 0}
        return count

    # ── Statistics ────────────────────────────────────────────────────

    async def stats(self) -> Dict[str, Any]:
        """Return cache performance metrics."""
        total_hits = self._stats["l1_hits"] + self._stats["l2_hits"]
        total_requests = total_hits + self._stats["misses"]
        hit_rate = total_hits / total_requests if total_requests > 0 else 0.0

        result = {
            "serializer": _SERIALIZER,
            "l1_entries": len(self._l1),
            "l1_max_size": self._l1_max_size,
            "l1_ttl": self._l1_ttl,
            "l2_ttl": self._l2_ttl,
            "l2_available": self._redis is not None,
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "misses": self._stats["misses"],
            "total_sets": self._stats["sets"],
            "hit_rate": round(hit_rate, 4),
        }

        # L2 memory usage (if Redis available)
        if self._redis:
            try:
                info = await self._redis.info("memory")
                result["l2_used_memory"] = info.get("used_memory_human", "unknown")
            except Exception:
                pass

        return result

    # ── Internal ──────────────────────────────────────────────────────

    def _l1_put(self, key: str, value: Any):
        """Insert into L1 with TTL, evicting oldest entries if full."""
        if len(self._l1) >= self._l1_max_size:
            self._l1_evict()
        self._l1[key] = (value, time.time() + self._l1_ttl)

    def _l1_evict(self):
        """Evict expired entries first, then oldest entries if still over cap."""
        now = time.time()
        # Phase 1: remove expired
        expired = [k for k, (_, ts) in self._l1.items() if ts <= now]
        for k in expired:
            del self._l1[k]

        # Phase 2: if still over 80% cap, remove oldest 20%
        if len(self._l1) >= int(self._l1_max_size * 0.8):
            sorted_entries = sorted(self._l1.items(), key=lambda x: x[1][1])
            evict_count = len(self._l1) // 5  # 20%
            for k, _ in sorted_entries[:evict_count]:
                del self._l1[k]

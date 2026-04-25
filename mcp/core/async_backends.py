"""
Async Backend Pool — Shared async clients for Neo4j, Qdrant, Redis
====================================================================

Replaces the sync _get_neo4j(), _get_qdrant(), _get_redis() singletons
with async connection-pooled clients suitable for multi-replica MCP.

Usage::

    pool = AsyncBackendPool()
    await pool.init()

    driver = await pool.neo4j("illd")
    qclient = await pool.qdrant()
    rclient = await pool.redis()

    # On shutdown:
    await pool.close()
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class AsyncBackendPool:
    """Async connection pool for Neo4j, Qdrant, and Redis.

    Provides:
    - Lazy initialization (clients are created on first access)
    - Double-checked locking for thread/coroutine safety
    - Health monitoring per backend
    - Graceful degradation if a backend is unavailable
    """

    def __init__(self):
        self._neo4j_drivers: Dict[str, Any] = {}
        self._qdrant_client: Optional[Any] = None
        self._redis_client: Optional[Any] = None

        self._neo4j_lock = asyncio.Lock()
        self._qdrant_lock = asyncio.Lock()
        self._redis_lock = asyncio.Lock()

        self._health: Dict[str, Dict[str, Any]] = {
            "neo4j": {"available": False, "last_check": 0},
            "qdrant": {"available": False, "last_check": 0},
            "redis": {"available": False, "last_check": 0},
        }

    # ── Neo4j (async driver) ──────────────────────────────────────────

    async def neo4j(self, profile: str = "illd"):
        """Return an async Neo4j driver for *profile*, creating on first call."""
        if profile in self._neo4j_drivers:
            return self._neo4j_drivers[profile]

        async with self._neo4j_lock:
            if profile in self._neo4j_drivers:
                return self._neo4j_drivers[profile]
            try:
                from neo4j import AsyncGraphDatabase

                cfg = _load_neo4j_profile_config(profile)
                if cfg:
                    uri = cfg["uri"]
                    auth = (cfg["username"], cfg["password"])
                    logger.info("[AsyncPool/Neo4j] Connecting profile '%s' → %s", profile, uri)
                else:
                    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
                    auth = (
                        os.environ.get("NEO4J_USERNAME", "neo4j"),
                        os.environ.get("NEO4J_PASSWORD", ""),
                    )
                    logger.info("[AsyncPool/Neo4j] Connecting profile '%s' → %s (env)", profile, uri)

                pool_kwargs = _load_neo4j_pool_config(profile)

                driver = AsyncGraphDatabase.driver(uri, auth=auth, **pool_kwargs)
                # Verify connectivity
                await driver.verify_connectivity()

                self._neo4j_drivers[profile] = driver
                self._health["neo4j"]["available"] = True
                self._health["neo4j"]["last_check"] = time.time()
                return driver
            except Exception as e:
                logger.error("[AsyncPool/Neo4j] Init for '%s': %s", profile, e)
                self._health["neo4j"]["available"] = False
                self._health["neo4j"]["last_check"] = time.time()
                return None

    # ── Qdrant (async client) ─────────────────────────────────────────

    async def qdrant(self):
        """Return an AsyncQdrantClient, creating on first call."""
        if self._qdrant_client is not None:
            return self._qdrant_client

        async with self._qdrant_lock:
            if self._qdrant_client is not None:
                return self._qdrant_client
            try:
                from qdrant_client import AsyncQdrantClient

                qdrant_url, api_key, kwargs = _resolve_qdrant_config()
                self._qdrant_client = AsyncQdrantClient(
                    url=qdrant_url, api_key=api_key or None, **kwargs
                )

                logger.info("[AsyncPool/Qdrant] Connected → %s", qdrant_url)
                self._health["qdrant"]["available"] = True
                self._health["qdrant"]["last_check"] = time.time()
                return self._qdrant_client
            except Exception as e:
                logger.error("[AsyncPool/Qdrant] Init: %s", e)
                self._health["qdrant"]["available"] = False
                self._health["qdrant"]["last_check"] = time.time()
                return None

    # ── Redis (async client) ──────────────────────────────────────────

    async def redis(self):
        """Return an async Redis client, creating on first call."""
        if self._redis_client is not None:
            return self._redis_client

        async with self._redis_lock:
            if self._redis_client is not None:
                return self._redis_client
            try:
                import redis.asyncio as aioredis

                redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
                self._redis_client = aioredis.from_url(
                    redis_url, decode_responses=True,
                    max_connections=20,
                )
                # Verify connectivity
                await self._redis_client.ping()

                logger.info("[AsyncPool/Redis] Connected → %s", redis_url[:40])
                self._health["redis"]["available"] = True
                self._health["redis"]["last_check"] = time.time()
                return self._redis_client
            except Exception as e:
                logger.error("[AsyncPool/Redis] Init: %s", e)
                self._health["redis"]["available"] = False
                self._health["redis"]["last_check"] = time.time()
                return None

    # ── Health check ──────────────────────────────────────────────────

    async def health(self) -> Dict[str, Any]:
        """Return health status for all backends.

        Returns
        -------
        dict
            ``{"neo4j": {"available": bool, ...}, "qdrant": {...}, "redis": {...},
              "overall": "healthy"|"degraded"|"unavailable"}``
        """
        checks = {}

        # Neo4j
        try:
            for profile, driver in self._neo4j_drivers.items():
                info = await driver.get_server_info()
                checks.setdefault("neo4j", {})["available"] = True
                checks["neo4j"]["profiles"] = list(self._neo4j_drivers.keys())
                self._health["neo4j"]["available"] = True
        except Exception:
            checks["neo4j"] = {"available": False}
            self._health["neo4j"]["available"] = False

        # Qdrant
        try:
            if self._qdrant_client:
                colls = await self._qdrant_client.get_collections()
                checks["qdrant"] = {"available": True, "collections": len(colls.collections)}
                self._health["qdrant"]["available"] = True
            else:
                checks["qdrant"] = {"available": False}
        except Exception:
            checks["qdrant"] = {"available": False}
            self._health["qdrant"]["available"] = False

        # Redis
        try:
            if self._redis_client:
                pong = await self._redis_client.ping()
                checks["redis"] = {"available": bool(pong)}
                self._health["redis"]["available"] = bool(pong)
            else:
                checks["redis"] = {"available": False}
        except Exception:
            checks["redis"] = {"available": False}
            self._health["redis"]["available"] = False

        available_count = sum(1 for v in checks.values() if v.get("available"))
        if available_count == 3:
            checks["overall"] = "healthy"
        elif available_count > 0:
            checks["overall"] = "degraded"
        else:
            checks["overall"] = "unavailable"

        for k in ("neo4j", "qdrant", "redis"):
            self._health[k]["last_check"] = time.time()

        return checks

    # ── Shutdown ──────────────────────────────────────────────────────

    async def close(self):
        """Close all backend connections."""
        errors = []

        for profile, driver in list(self._neo4j_drivers.items()):
            try:
                await driver.close()
                logger.info("[AsyncPool] Neo4j driver '%s' closed", profile)
            except Exception as e:
                errors.append(f"neo4j/{profile}: {e}")
        self._neo4j_drivers.clear()

        if self._qdrant_client:
            try:
                await self._qdrant_client.close()
                logger.info("[AsyncPool] Qdrant client closed")
            except Exception as e:
                errors.append(f"qdrant: {e}")
            self._qdrant_client = None

        if self._redis_client:
            try:
                await self._redis_client.close()
                logger.info("[AsyncPool] Redis client closed")
            except Exception as e:
                errors.append(f"redis: {e}")
            self._redis_client = None

        if errors:
            logger.warning("[AsyncPool] Close errors: %s", errors)


# ═════════════════════════════════════════════════════════════════════════
#  Config helpers (shared with sync path)
# ═════════════════════════════════════════════════════════════════════════

def _load_neo4j_profile_config(profile: str) -> Optional[Dict[str, str]]:
    """Load Neo4j URI + creds from storage_config.yaml for a given profile."""
    try:
        from src.HybridRAG.code.neo4j_manager import load_config
        cfg = load_config()
        profiles = cfg.get("neo4j", {}).get("profiles", {})
        pcfg = profiles.get(profile, {})
        if pcfg.get("uri"):
            return {
                "uri": pcfg["uri"],
                "username": pcfg.get("username", "neo4j"),
                "password": pcfg.get("password", ""),
            }
    except Exception:
        pass
    return None


def _load_neo4j_pool_config(profile: str) -> Dict[str, Any]:
    """Load Neo4j connection-pool kwargs from config or defaults."""
    try:
        from src.HybridRAG.code.neo4j_manager import get_instance_config
        inst = get_instance_config(profile)
        return {
            "max_connection_pool_size": inst.max_connection_pool_size,
            "max_connection_lifetime": inst.max_connection_lifetime,
            "connection_acquisition_timeout": inst.connection_acquisition_timeout,
        }
    except Exception:
        return {
            "max_connection_pool_size": 50,
            "max_connection_lifetime": 3600,
            "connection_acquisition_timeout": 60,
        }


def _resolve_qdrant_config():
    """Resolve Qdrant URL, API key, and client kwargs from config + env.

    Returns
    -------
    tuple[str, str | None, dict]
        (qdrant_url, api_key, extra_kwargs)
    """
    qdrant_url = None
    api_key = None
    timeout = 30
    verify_ssl = True
    prefer_grpc = False

    try:
        from src.HybridRAG.code.neo4j_manager import load_config
        cfg = load_config()
        qcfg = cfg.get("qdrant", {})
        qdrant_url = qcfg.get("url")
        api_key = qcfg.get("api_key")
        timeout = qcfg.get("timeout", 30)
        verify_ssl = qcfg.get("verify_ssl", False)
        prefer_grpc = qcfg.get("grpc", False)
        in_cluster_url = qcfg.get("in_cluster_url")
        in_cluster_grpc_port = int(qcfg.get("in_cluster_grpc_port", 6334))

        # In-cluster gRPC fast path
        if bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and prefer_grpc and in_cluster_url:
            return in_cluster_url, api_key, {
                "prefer_grpc": True,
                "grpc_port": in_cluster_grpc_port,
                "https": False,
                "timeout": timeout,
            }
    except Exception:
        pass

    # Env-var fallback
    qdrant_url = qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333")
    api_key = api_key or os.environ.get("QDRANT_API_KEY")

    use_https = qdrant_url.startswith("https")
    if prefer_grpc and use_https:
        prefer_grpc = False

    # CA bundle for HTTPS
    ca_bundle = None
    if use_https:
        _ca = _REPO_ROOT / "src" / "HybridRAG" / "code" / "ca-bundle.crt"
        if _ca.exists():
            ca_bundle = str(_ca)

    return qdrant_url, api_key, {
        "https": use_https,
        "prefer_grpc": prefer_grpc,
        "timeout": timeout,
        "verify": ca_bundle if ca_bundle and verify_ssl else verify_ssl,
    }

"""
Rate Limiting — C05 Fix
========================
Per-API-key rate limiting for MCP tool invocations.

Uses slowapi (wraps the `limits` library) for ASGI-compatible rate limiting.
Falls back to a no-op if slowapi is not installed.

Configuration via environment variables:
    RATE_LIMIT_SEARCH    — search/query tools (default: 60/minute)
    RATE_LIMIT_ADMIN     — admin tools       (default: 10/minute)
    RATE_LIMIT_INGESTION — ingestion tools    (default: 5/minute)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("aice_mcp.rate_limiter")

# Rate limit defaults (configurable via env vars)
RATE_LIMIT_SEARCH = os.environ.get("RATE_LIMIT_SEARCH", "60/minute")
RATE_LIMIT_ADMIN = os.environ.get("RATE_LIMIT_ADMIN", "10/minute")
RATE_LIMIT_INGESTION = os.environ.get("RATE_LIMIT_INGESTION", "5/minute")

# Tool → rate limit category mapping
_ADMIN_TOOLS = {
    "ingest_file", "ingest_module_from_repo", "batch_ingest_modules",
    "ingest_repository", "cache_invalidate_module", "cache_clear",
    "ensure_valid_token", "process_results",
}
_INGESTION_TOOLS = {
    "ingest_file", "ingest_module_from_repo",
    "batch_ingest_modules", "ingest_repository",
}


class RateLimiter:
    """Per-API-key rate limiter for MCP tools.

    Tracks request counts in memory using a sliding window.
    Gracefully degrades to no-op if limits are not parseable.
    """

    def __init__(self):
        self._enabled = False
        try:
            from limits import parse as parse_limit
            from limits.storage import MemoryStorage
            from limits.strategies import MovingWindowRateLimiter

            self._storage = MemoryStorage()
            self._limiter = MovingWindowRateLimiter(self._storage)
            self._search_limit = parse_limit(RATE_LIMIT_SEARCH)
            self._admin_limit = parse_limit(RATE_LIMIT_ADMIN)
            self._ingestion_limit = parse_limit(RATE_LIMIT_INGESTION)
            self._enabled = True
            logger.info(
                "Rate limiter enabled: search=%s, admin=%s, ingestion=%s",
                RATE_LIMIT_SEARCH, RATE_LIMIT_ADMIN, RATE_LIMIT_INGESTION,
            )
        except ImportError:
            logger.info("slowapi/limits not installed — rate limiting disabled")
        except Exception as e:
            logger.warning("Rate limiter initialization failed: %s", e)

    def check(self, api_key: str, tool_name: str) -> Optional[str]:
        """Check if the request is within rate limits.

        Returns None if allowed, or an error message if rate-limited.
        """
        if not self._enabled:
            return None

        key = api_key[:16] if api_key else "anonymous"

        if tool_name in _INGESTION_TOOLS:
            limit = self._ingestion_limit
        elif tool_name in _ADMIN_TOOLS:
            limit = self._admin_limit
        else:
            limit = self._search_limit

        if not self._limiter.hit(limit, key, tool_name):
            logger.warning("Rate limit exceeded for key=%s… tool=%s", key[:8], tool_name)
            return f"Rate limit exceeded for tool '{tool_name}'. Please retry later."

        return None


# Module-level singleton
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the singleton rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter

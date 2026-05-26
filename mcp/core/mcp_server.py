"""
AI Core Engine — MCP Server
============================
56 Tools across 13 categories (including Ephemeral Sandbox + RLM in Cat 6)
12 Resources · 8 Prompts

Sprint 9 changes:
  - process_results: Full implementation (VP, Polyspace, JUnit, coverage, compiler)
  - FeedbackSink: Learning loop wired to PatternStore (Neo4j) + PatternIndex (Qdrant)
  - ResultProcessor: New service for CI/CD result ingestion

Previous bug fixes (Sprint 8):
  1. Tool naming: search_database (not search_databases) per PPTX v3
  2. Cerbos RBAC re-enabled via auth_middleware
  3. All credentials from env vars (no hardcoded tokens)

Usage:  python -m mcp.core.mcp_server
"""
from __future__ import annotations

import asyncio
import contextvars
import json, logging, os, sys, time
import uuid as _uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# ── SDK import shim ────────────────────────────────────────────────────────
# The repo has a top-level ``mcp/`` package (this directory) which shadows
# the installed ``mcp`` SDK (mcp.server.fastmcp).  We temporarily evict the
# local ``mcp`` from sys.modules, strip repo-root from sys.path, import the
# SDK, then restore everything so relative imports (.auth_middleware etc.)
# continue to work.
import importlib as _il

_repo_root = str(Path(__file__).resolve().parents[2])
_saved_paths: list[tuple[int, str]] = []
for _i in range(len(sys.path) - 1, -1, -1):
    _abs = os.path.normcase(os.path.abspath(sys.path[_i])) if sys.path[_i] else os.path.normcase(os.getcwd())
    if _abs == os.path.normcase(_repo_root):
        _saved_paths.append((_i, sys.path.pop(_i)))

_saved_mcp = {k: v for k, v in sys.modules.items() if k == "mcp" or k.startswith("mcp.")}
for _k in _saved_mcp:
    del sys.modules[_k]

FastMCP = _il.import_module("mcp.server.fastmcp").FastMCP       # installed SDK

# Restore local mcp package
for _k in list(sys.modules):
    if _k == "mcp" or _k.startswith("mcp."):
        if _k.startswith("mcp.server") or _k.startswith("mcp.client"):
            continue                                             # keep SDK sub-modules
        del sys.modules[_k]
sys.modules.update(_saved_mcp)

for _i, _p in reversed(_saved_paths):
    sys.path.insert(_i, _p)
del _saved_paths, _saved_mcp, _repo_root, _il
# ── end SDK import shim ───────────────────────────────────────────────────

from .auth_middleware import check_authorization, extract_workspace_id, _err_permission_denied
from .tool_tiers import TOOL_TIERS

# ── Path bootstrapping: make src/ importable ──────────────────────────────
# Must happen BEFORE the metrics import, which uses `from src.Observability...`
_MCP_DIR = Path(__file__).resolve().parent          # mcp/core/
_REPO_ROOT = _MCP_DIR.parents[1]                    # repo root
_SRC_DIR = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── Prometheus metrics (graceful degradation) ─────────────────────────────
try:
    from src.Observability.metrics import (
        TOOL_REQUESTS_TOTAL, TOOL_REQUEST_DURATION,
        SEARCH_REQUESTS_TOTAL, SEARCH_DURATION,
        CACHE_REQUESTS_TOTAL, ACTIVE_SESSIONS,
        RLM_REQUESTS_TOTAL, RLM_SUBQUERIES,
        INGESTION_FILES_TOTAL, BACKEND_UP,
        REVIEW_ROUTING_TOTAL, PROMETHEUS_AVAILABLE,
        QUERY_LATENCY, CACHE_HIT_RATE, CACHE_SIZE, ERROR_TOTAL,
        INGESTION_DURATION,
        make_metrics_app,
    )
except ImportError:
    PROMETHEUS_AVAILABLE = False
    make_metrics_app = lambda: None

    # No-op stubs so metric calls don't crash when prometheus_client is absent
    class _NoOpMetric:
        """Silently ignores .labels(), .inc(), .dec(), .set(), .observe()."""
        def labels(self, **kw): return self
        def inc(self, amount=1): pass
        def dec(self, amount=1): pass
        def set(self, value): pass
        def observe(self, value): pass

    _noop = _NoOpMetric()
    TOOL_REQUESTS_TOTAL = _noop
    TOOL_REQUEST_DURATION = _noop
    SEARCH_REQUESTS_TOTAL = _noop
    SEARCH_DURATION = _noop
    CACHE_REQUESTS_TOTAL = _noop
    ACTIVE_SESSIONS = _noop
    RLM_REQUESTS_TOTAL = _noop
    RLM_SUBQUERIES = _noop
    INGESTION_FILES_TOTAL = _noop
    BACKEND_UP = _noop
    REVIEW_ROUTING_TOTAL = _noop
    QUERY_LATENCY = _noop
    CACHE_HIT_RATE = _noop
    CACHE_SIZE = _noop
    ERROR_TOTAL = _noop
    INGESTION_DURATION = _noop

from .config import get_settings as _get_settings
_get_settings.cache_clear()  # ensure fresh settings on module reload

logging.basicConfig(
    stream=sys.stderr,
    level=getattr(logging, _get_settings().log_level),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)

# Install log sanitizer before any other logging occurs
try:
    from src.Observability.log_sanitizer import install_log_sanitizer
    install_log_sanitizer()
except ImportError:
    pass  # sanitizer not available — continue without it

logger = logging.getLogger("aice_mcp")

# ── Per-tool timing context vars (for _finish_tool) ─────────────────────────
_tool_name_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_tool_name_ctx", default="",
)
_tool_start_time: contextvars.ContextVar[float] = contextvars.ContextVar(
    "_tool_start_time", default=0.0,
)

def _finish_tool(status: str) -> None:
    """Record Prometheus metrics for the just-completed tool call."""
    if not PROMETHEUS_AVAILABLE:
        return
    name = _tool_name_ctx.get("")
    t0 = _tool_start_time.get(0.0)
    da = _current_da_name.get("unknown")
    if name:
        TOOL_REQUESTS_TOTAL.labels(da_name=da, tool=name, status=status).inc()
        TOOL_REQUEST_DURATION.labels(da_name=da, tool=name).observe(time.time() - t0)

# ── Envelope helpers ───────────────────────────────────────────────────────
def _ok(data: Any) -> str:
    """Wrap a successful result — wire-compatible with base repo contract."""
    _finish_tool("ok")
    result: Dict[str, Any] = {"error": False, "data": data}
    rid = _current_request_id.get("")
    if rid:
        result["request_id"] = rid
    return json.dumps(result, indent=2, default=str)

def _err(code: str, message: str, *, _raw_exception: BaseException | None = None) -> str:
    """Return JSON error response. In production, sanitize internal details."""
    _finish_tool("error")
    correlation_id = _current_request_id.get("") or str(_uuid.uuid4())[:8]

    if _SANITIZE_ERRORS and code == "INTERNAL_ERROR":
        logger.error(
            "Tool error [%s]: %s", correlation_id, message,
            exc_info=_raw_exception,
        )
        safe_message = f"Internal error occurred. Reference: {correlation_id}"
    else:
        safe_message = message

    return json.dumps({
        "error": True, "error_code": code,
        "message": safe_message, "correlation_id": correlation_id,
    })


# ── Metrics helpers (Tickets 7 & 8) ──────────────────────────────────────

def _update_cache_gauges(cache) -> None:
    """Push current cache size and hit rate into Prometheus gauges."""
    try:
        if hasattr(cache, '_lru') and cache._lru:
            lru = cache._lru
            CACHE_SIZE.labels(cache_type="lru").set(len(getattr(lru, '_cache', {})))
            hits = getattr(lru, '_hits', 0)
            misses = getattr(lru, '_misses', 0)
            total = hits + misses
            if total > 0:
                CACHE_HIT_RATE.labels(cache_type="lru").set(hits / total)
        if hasattr(cache, '_semantic') and cache._semantic:
            sem = cache._semantic
            CACHE_SIZE.labels(cache_type="semantic").set(len(getattr(sem, '_store', {})))
            hits = getattr(sem, '_hits', 0)
            misses = getattr(sem, '_misses', 0)
            total = hits + misses
            if total > 0:
                CACHE_HIT_RATE.labels(cache_type="semantic").set(hits / total)
    except Exception:
        pass  # best-effort — never break request path for metrics


def _classify_and_record_error(exc: Exception, component: str) -> None:
    """Classify an exception and increment the ERROR_TOTAL counter."""
    exc_name = type(exc).__name__.lower()
    if "timeout" in exc_name or "timed out" in str(exc).lower():
        error_type = "timeout"
    elif any(kw in exc_name for kw in ("connect", "refused", "unreachable", "dns")):
        error_type = "connection"
    elif any(kw in exc_name for kw in ("auth", "permission", "forbidden", "401", "403")):
        error_type = "auth"
    elif any(kw in exc_name for kw in ("validation", "value", "type", "key")):
        error_type = "validation"
    else:
        error_type = "internal"
    ERROR_TOTAL.labels(error_type=error_type, component=component).inc()

# ── Per-request API key propagation ────────────────────────────────────────
# For HTTP transports (streamable-http, sse) the API key is extracted from
# the Authorization header by _APIKeyMiddleware and stored in this context
# variable.  For stdio transport MCP_API_KEY env var is the fallback.
_current_api_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_api_key", default="",
)
_current_da_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_da_name", default="unknown",
)
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_request_id", default="",
)

_SANITIZE_ERRORS = _get_settings().sanitize_errors

def _resolve_da_name(api_key: str) -> str:
    """Look up the principal_id (DA name) for an API key."""
    from .auth_middleware import load_api_keys
    registry = load_api_keys()
    entry = registry.get(api_key)
    if entry is None:
        return "unknown"
    return entry.get("principal_id", "unknown")

def _authorize(tool_name: str, **kw) -> Optional[str]:
    """Run Cerbos authorization for *tool_name*.

    Returns ``None`` if allowed, or a JSON error string (PERMISSION_DENIED)
    if denied.  The caller should return the error string immediately.
    """
    # ── Set per-tool timing context for Prometheus metrics ──
    _tool_name_ctx.set(tool_name)
    _tool_start_time.set(time.time())

    api_key = _current_api_key.get("") or _get_settings().mcp_api_key
    if not api_key:
        return _err_permission_denied("No API key provided (set MCP_API_KEY env var or send Authorization header)")

    # Resolve DA name from API key for metrics labelling
    _current_da_name.set(_resolve_da_name(api_key))

    ws = extract_workspace_id(**kw)
    module_name = kw.get("module_name", None)
    allowed, message = check_authorization(api_key, tool_name, ws, module_name)

    # Audit logging (best-effort, never blocks the tool)
    pg = _get_postgres_client()
    if pg and pg.available:
        try:
            pg.log_audit(
                tool_name=tool_name, workspace_id=ws,
                caller_api_key="sha256:" + __import__('hashlib').sha256(api_key.encode()).hexdigest()[:16],
                response_code="ok" if allowed else "denied",
            )
        except Exception:
            pass

    if not allowed:
        return _err_permission_denied(message)
    return None


# ── Plan 2 Phase 6: Session-based query routing decorator ─────────────────
def with_session_routing(tool_name: str):
    """Decorator that adds session_id routing to any MCP tool.

    When session_id is provided and points to an active sandbox:
      - Shallow tools → route to sandbox NetworkX + vectors
      - Deep tools → route to prod Neo4j + patch with sandbox overrides
    When session_id is absent → existing production flow (unchanged).
    """
    from functools import wraps
    import inspect

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, session_id: Optional[str] = None, **kwargs):
            if session_id:
                sm = _get_sandbox_manager()
                sandbox = sm.get_sandbox(session_id) if sm else None
                if sandbox:
                    from src.MemoryLayer.memory.ephemeral_sandbox import (
                        HybridGraphService, HybridTraversal,
                    )
                    ws = kwargs.get("workspace_id", "mcal")
                    driver = _get_neo4j(ws)
                    hybrid = HybridGraphService(sandbox, driver,
                                                qdrant_client=_get_qdrant(),
                                                workspace_id=ws)
                    traversal = HybridTraversal(sandbox, driver, ws)
                    classification = HybridGraphService.classify_tool(tool_name)

                    kwargs["graph_service"] = hybrid
                    kwargs["hybrid_traversal"] = traversal
                    kwargs["query_mode"] = classification
                    kwargs["sandbox_ctx"] = sandbox

            # Only forward kwargs supported by the target function unless it has **kwargs.
            sig = inspect.signature(fn)
            has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if has_varkw:
                filtered_kwargs = kwargs
            else:
                filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

            if "session_id" in sig.parameters:
                coro = fn(*args, session_id=session_id, **filtered_kwargs)
            else:
                coro = fn(*args, **filtered_kwargs)

            # ── MEG_SW-333: Async cancellation & timeout ──
            try:
                return await asyncio.wait_for(
                    coro,
                    timeout=_get_settings().tool_timeout_seconds,
                )
            except asyncio.TimeoutError:
                timeout = _get_settings().tool_timeout_seconds
                logger.warning("Tool %s timed out after %ds", tool_name, timeout)
                return _err("TIMEOUT", f"Tool execution exceeded {timeout}s limit")
            except asyncio.CancelledError:
                logger.warning("Tool %s was cancelled", tool_name)
                return _err("CANCELLED", "Tool execution was cancelled by client")
        return wrapper
    return decorator


# ── Lazy backend connections (BUG FIX #3: creds from env only) ─────────────
#
# Neo4j instance binding:
#   The workspace_id ("illd"/"mcal"/"test") determines which Neo4j instance
#   to connect to, resolved from storage_config.yaml per profile.
#   MCP_NEO4J_INSTANCE env var provides a fallback override.
#   Drivers are cached per-profile for connection reuse.
#
_NEO4J_INSTANCE: str = _get_settings().mcp_neo4j_instance
_DEFAULT_MAX_DEPTH = _get_settings().max_dependencies_depth
_SESSION_TTL_SECONDS = _get_settings().session_ttl_seconds
_neo4j_drivers: Dict[str, Any] = {}   # per-profile driver pool
_qdrant_client = None

# ── Negative caching for failed service init (MEG_SW-321) ──────────────
_INIT_COOLDOWN_SECONDS = int(os.environ.get("AICE_INIT_COOLDOWN", "30"))
_neo4j_fail_ts: Dict[str, float] = {}   # profile → timestamp of last failure
_qdrant_fail_ts: float = 0.0
_redis_fail_ts: float = 0.0

# Config-driven default alpha (MEG_SW-308)
try:
    from src.HybridRAG.code.env_config import get_default_search_alpha
    _DEFAULT_SEARCH_ALPHA = get_default_search_alpha()
except Exception:
    _DEFAULT_SEARCH_ALPHA = 0.6
_redis_client = None

def _load_neo4j_profile_config(profile: str) -> Optional[Dict[str, Any]]:
    """Load Neo4j connection config for *profile* from storage_config.yaml."""
    try:
        import sys as _sys
        _code_dir = str(Path(__file__).resolve().parents[2] / "src" / "HybridRAG" / "code")
        if _code_dir not in _sys.path:
            _sys.path.insert(0, _code_dir)
        from src.HybridRAG.code.neo4j_manager import get_instance_config
        cfg = get_instance_config(profile)
        # Prefer in_cluster_uri when running inside Kubernetes
        uri = cfg.uri
        if os.environ.get("KUBERNETES_SERVICE_HOST") and getattr(cfg, "in_cluster_uri", None):
            uri = cfg.in_cluster_uri
        return {"uri": uri, "username": cfg.username, "password": cfg.password,
                "database": cfg.database}
    except Exception as e:
        logger.debug("Could not load neo4j config for profile '%s' from storage_config: %s", profile, e)
        return None

def _get_neo4j(profile: str = "illd"):
    """Return a Neo4j driver for the given workspace profile.

    Maintains a per-profile driver pool so that different workspaces
    (illd, mcal, test) connect to their respective Neo4j instances
    as defined in storage_config.yaml. Thread/async-safe via asyncio.Lock.

    Resolution order for the connection URI:
      0. NEO4J_URI env var override (skips storage_config entirely)
      1. storage_config.yaml[neo4j][<profile>] (preferred)
      2. MCP_NEO4J_INSTANCE env var → storage_config.yaml[neo4j][<instance>]
      3. NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD env vars (K8s / legacy)
      4. bolt://localhost:7687 default fallback
    """
    # Determine the effective instance key
    instance = profile or _NEO4J_INSTANCE or "illd"

    # Return cached driver if available (fast path, no lock)
    if instance in _neo4j_drivers:
        return _neo4j_drivers[instance]

    # Negative cache: skip retry if last failure was recent (MEG_SW-321)
    last_fail = _neo4j_fail_ts.get(instance, 0.0)
    if last_fail and (time.time() - last_fail) < _INIT_COOLDOWN_SECONDS:
        return None

    return _create_neo4j_driver(instance)


def _create_neo4j_driver(instance: str):
    """Create and cache a Neo4j driver for the given instance (sync helper)."""
    # Double-check after potential await
    if instance in _neo4j_drivers:
        return _neo4j_drivers[instance]

    try:
        from neo4j import GraphDatabase
        # Step 0: NEO4J_URI env var override (skips storage_config entirely)
        env_uri = os.environ.get("NEO4J_URI")
        if env_uri:
            uri = env_uri
            auth = (os.environ.get("NEO4J_USERNAME", "neo4j"),
                    os.environ.get("NEO4J_PASSWORD", ""))
            logger.info("[Neo4j] Connecting profile '%s' → %s (NEO4J_URI override)", instance, uri)
        else:
            # Try profile-specific config from storage_config.yaml
            cfg = _load_neo4j_profile_config(instance)
            if not cfg and instance != _NEO4J_INSTANCE and _NEO4J_INSTANCE:
                # Fallback: try the MCP_NEO4J_INSTANCE override
                cfg = _load_neo4j_profile_config(_NEO4J_INSTANCE)
            if cfg:
                uri = cfg["uri"]
                auth = (cfg["username"], cfg["password"])
                logger.info("[Neo4j] Connecting profile '%s' → %s", instance, uri)
            else:
                uri = "bolt://localhost:7687"
                auth = (os.environ.get("NEO4J_USERNAME", "neo4j"),
                        os.environ.get("NEO4J_PASSWORD", ""))
                logger.info("[Neo4j] Connecting profile '%s' → %s (default fallback)", instance, uri)

        # Pool config from storage_config.yaml (or sensible defaults)
        pool_kwargs = {}
        try:
            from src.HybridRAG.code.neo4j_manager import get_instance_config
            inst_cfg = get_instance_config(instance) if instance else None
            if inst_cfg:
                pool_kwargs["max_connection_pool_size"] = inst_cfg.max_connection_pool_size
                pool_kwargs["max_connection_lifetime"] = inst_cfg.max_connection_lifetime
                pool_kwargs["connection_acquisition_timeout"] = inst_cfg.connection_acquisition_timeout
        except Exception:
            pass
        if not pool_kwargs:
            pool_kwargs = {
                "max_connection_pool_size": 50,
                "max_connection_lifetime": 3600,
                "connection_acquisition_timeout": 60,
            }

        driver = GraphDatabase.driver(uri, auth=auth, **pool_kwargs)
        _neo4j_drivers[instance] = driver
        return driver
    except Exception as e:
        logger.error("Neo4j init for profile '%s': %s", instance, e)
        _classify_and_record_error(e, "neo4j")
        _neo4j_fail_ts[instance] = time.time()
        return None

def _get_qdrant():
    """Return a Qdrant client, creating one if needed.

    Resolution order:
      1. storage_config.yaml  (qdrant.url + qdrant.api_key + port + verify_ssl)
      2. QDRANT_URL / QDRANT_API_KEY env vars (legacy fallback)

    gRPC auto-detection:
      - In-cluster (KUBERNETES_SERVICE_HOST set): connect via plain gRPC to
        the K8s service (fast, no TLS overhead).
      - External (HTTPS URL): use REST — the OpenShift edge-terminated
        ingress only supports HTTP/1.1.
    """
    global _qdrant_client, _qdrant_fail_ts
    if _qdrant_client is None:
        # Negative cache: skip retry if last failure was recent (MEG_SW-321)
        if _qdrant_fail_ts and (time.time() - _qdrant_fail_ts) < _INIT_COOLDOWN_SECONDS:
            return None
        try:
            from qdrant_client import QdrantClient
            qdrant_url = None
            api_key = None
            timeout = 30
            verify_ssl = True
            port = None
            prefer_grpc = False
            in_cluster_url = None
            in_cluster_grpc_port = 6334
            try:
                import sys as _sys
                _code_dir = str(Path(__file__).resolve().parents[2] / "src" / "HybridRAG" / "code")
                if _code_dir not in _sys.path:
                    _sys.path.insert(0, _code_dir)
                from src.HybridRAG.code.neo4j_manager import load_config
                cfg = load_config()
                qdrant_cfg = cfg.get("qdrant", {})
                qdrant_url = qdrant_cfg.get("url")
                api_key = qdrant_cfg.get("api_key")
                timeout = qdrant_cfg.get("timeout", 30)
                verify_ssl = qdrant_cfg.get("verify_ssl", False)
                port = qdrant_cfg.get("port")
                prefer_grpc = qdrant_cfg.get("grpc", False)
                in_cluster_url = qdrant_cfg.get("in_cluster_url")
                in_cluster_grpc_port = int(qdrant_cfg.get("in_cluster_grpc_port", 6334))
            except Exception:
                pass

            # ── In-cluster gRPC fast path ──────────────────────────────────
            _in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
            if _in_cluster and prefer_grpc and in_cluster_url:
                qdrant_url = in_cluster_url
                port = None  # already in the URL or use default
                use_https = False
                logger.info("[Qdrant] In-cluster detected — using gRPC → %s:%d",
                            qdrant_url, in_cluster_grpc_port)
                _qdrant_client = QdrantClient(
                    url=qdrant_url,
                    api_key=api_key or None,
                    prefer_grpc=True,
                    grpc_port=in_cluster_grpc_port,
                    https=False,
                    timeout=timeout,
                )
                logger.info("[Qdrant] Connected → %s (grpc=True, in-cluster)", qdrant_url)
                return _qdrant_client

            # ── External / fallback path ───────────────────────────────────
            # Env-var fallback
            qdrant_url = qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333")
            api_key = api_key or os.environ.get("QDRANT_API_KEY")
            # Append explicit port if URL doesn't already contain one
            if port and ":" not in qdrant_url.split("//", 1)[-1]:
                qdrant_url = f"{qdrant_url}:{port}"
            use_https = qdrant_url.startswith("https")

            # HTTPS ingress uses edge TLS termination (HTTP/1.1 only)
            # → gRPC requires HTTP/2, so fall back to REST for external access
            if prefer_grpc and use_https:
                logger.info("[Qdrant] External HTTPS endpoint — falling back to REST "
                            "(edge-terminated ingress does not support gRPC/HTTP2)")
                prefer_grpc = False

            # Load Infineon CA bundle for HTTPS TLS verification
            _ca_bundle_path = None
            if use_https:
                _ca_bundle = Path(__file__).resolve().parents[1] / "src" / "HybridRAG" / "code" / "ca-bundle.crt"
                if not _ca_bundle.exists():
                    _ca_bundle = _REPO_ROOT / "src" / "HybridRAG" / "code" / "ca-bundle.crt"
                if _ca_bundle.exists():
                    _ca_bundle_path = _ca_bundle
                    logger.info("[Qdrant] CA bundle loaded: %s", _ca_bundle)

            _qdrant_client = QdrantClient(
                url=qdrant_url,
                api_key=api_key or None,
                https=use_https,
                prefer_grpc=prefer_grpc,
                timeout=timeout,
                verify=str(_ca_bundle_path) if _ca_bundle_path and verify_ssl else verify_ssl,
            )
            logger.info("[Qdrant] Connected → %s (grpc=%s)", qdrant_url, prefer_grpc)
        except Exception as e:
            logger.error("Qdrant init: %s", e)
            _classify_and_record_error(e, "qdrant")
            _qdrant_fail_ts = time.time()
    return _qdrant_client

def _get_redis():
    global _redis_client, _redis_fail_ts
    if _redis_client is None:
        # Negative cache: skip retry if last failure was recent (MEG_SW-321)
        if _redis_fail_ts and (time.time() - _redis_fail_ts) < _INIT_COOLDOWN_SECONDS:
            return None
        try:
            import redis as _redis
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            kwargs = {"decode_responses": True}
            # Enable TLS for rediss:// scheme or explicit REDIS_TLS=true
            if redis_url.startswith("rediss://") or os.environ.get("REDIS_TLS", "").lower() in ("true", "1"):
                kwargs["ssl"] = True
                kwargs["ssl_cert_reqs"] = os.environ.get("REDIS_SSL_CERT_REQS", "required")
                ca_path = os.environ.get("REDIS_SSL_CA_CERTS")
                if ca_path:
                    kwargs["ssl_ca_certs"] = ca_path
            _redis_client = _redis.from_url(redis_url, **kwargs)
        except Exception as e:
            logger.error("Redis init: %s", e)
            _classify_and_record_error(e, "redis")
            _redis_fail_ts = time.time()
    return _redis_client

# ── PostgreSQL (audit, feedback, sessions, ingestion tracking) ─────────────
_postgres_client = None

def _get_postgres_client():
    global _postgres_client
    if _postgres_client is None:
        try:
            from src.Observability.postgres_schema import PostgresClient
            _postgres_client = PostgresClient()
            if _postgres_client.available:
                _postgres_client.init_schema()
                logger.info("[MCP] PostgresClient initialized")
            else:
                logger.info("[MCP] PostgresClient unavailable — audit logging disabled")
        except Exception as e:
            logger.warning("[MCP] PostgresClient init failed: %s", e)
    return _postgres_client

# ── Sprint 2: Service layer (lazy init) ───────────────────────────────────
_session_manager = None
_context_builder_cls = None


class _WorkingMemorySessionAdapter:
    """Compatibility adapter exposing the legacy session tool API on top of WorkingMemoryManager."""

    def __init__(self, working_memory_manager, postgres_client=None, backend=None, workspace_id: str = "illd"):
        self._wm = working_memory_manager
        self._pg = postgres_client
        self._backend = backend
        self._workspace_id = workspace_id

    def _purge(self):
        purged = self._wm.purge_expired_sessions()
        if purged:
            for _ in range(purged):
                ACTIVE_SESSIONS.dec()

    def create(self, session_id: str, assistant_name: str = "",
               module_context: str = "", ttl_seconds: int = 3600,
               workspace_id: Optional[str] = None):
        self._purge()
        existing = self._wm.get_session(session_id)
        if existing and not existing.is_expired:
            raise ValueError(f"Session '{session_id}' already exists.")

        module_name = (module_context or "default").lower()
        active_workspace = workspace_id or self._workspace_id
        metadata = {
            "assistant_name": assistant_name,
            "workspace_id": active_workspace,
        }
        created_id = self._wm.create_session(
            project="default",
            module=module_name,
            ttl_seconds=ttl_seconds,
            metadata=metadata,
            session_id=session_id,
        )
        if self._pg:
            self._pg.save_session_meta(
                session_id=created_id,
                assistant_name=assistant_name,
                module_context=module_name,
                workspace_id=active_workspace,
                ttl_seconds=ttl_seconds,
            )
        return SimpleNamespace(session_id=created_id)

    def get(self, session_id: str):
        self._purge()
        session = self._wm.get_session(session_id)
        if session is None or session.is_expired:
            if session is not None:
                self._wm.close_session(session_id)
                ACTIVE_SESSIONS.dec()
            return None
        return SimpleNamespace(
            session_id=session.session_id,
            assistant_name=session.metadata.get("assistant_name", ""),
            module_context=session.module,
            workspace_id=session.metadata.get("workspace_id", session.profile),
            store=session.metadata,
            context_entries=session.context,
        )

    def store(self, session_id: str, key: str, value: Any):
        self._purge()
        self._wm.store_data(session_id, key, value)

    def retrieve(self, session_id: str, key: str) -> Any:
        self._purge()
        return self._wm.retrieve_data(session_id, key)

    def close(self, session_id: str, persist_audit: bool = True) -> Dict[str, Any]:
        self._purge()
        session = self._wm.get_session(session_id)
        if session is None:
            return {"session_id": session_id, "found": False}

        summary = session.to_dict()
        summary["assistant_name"] = session.metadata.get("assistant_name", "")
        summary["workspace_id"] = session.metadata.get("workspace_id", session.profile)
        summary["total_store_keys"] = len(session.metadata)
        summary["total_context_entries"] = len(session.context)

        if persist_audit and self._pg:
            self._pg.close_session_meta(
                session_id=session_id,
                store_keys=list(session.metadata.keys()),
                context_count=len(session.context),
            )

        self._wm.close_session(session_id)
        return summary

_search_services: Dict[str, Any] = {}    # keyed by profile

def _get_search_service(profile: str = "illd"):
    if profile in _search_services:
        return _search_services[profile]
    try:
        from src.HybridRAG.code.querier.search_service import SearchService
        # Resolve the database name from the bound Neo4j instance
        db_name = None
        try:
            from src.HybridRAG.code.neo4j_manager import get_instance_config
            inst = _NEO4J_INSTANCE or profile
            db_name = get_instance_config(inst).database
        except Exception:
            pass
        # Resolve Qdrant collection name from storage_config.yaml
        qdrant_col = None
        try:
            from src.HybridRAG.code.neo4j_manager import load_config as _load_cfg
            _cfg = _load_cfg()
            qdrant_profile = _cfg.get("qdrant", {}).get(profile, {})
            qdrant_col = qdrant_profile.get("collection_name")
        except Exception:
            pass
        svc = SearchService(
            neo4j_driver=_get_neo4j(profile),
            qdrant_client=_get_qdrant(),
            default_database=db_name or profile,
            qdrant_collection=qdrant_col or profile,
        )
        # Attach query enhancer based on workspace profile:
        #   - mcal: LLM-powered McalQueryEnhancer (GraphProbe + CoT expansion + Cypher patterns)
        #   - illd: rule-based QueryEnhancer (domain synonyms, complexity classifier — built into SearchService)
        if profile == "mcal" and os.environ.get("QUERY_ENHANCER_ENABLED", "1") not in ("0", "false", "no"):
            try:
                from src.HybridRAG.code.querier.mcal_query_enhancer import McalQueryEnhancer
                enhancer = McalQueryEnhancer(
                    neo4j_driver=_get_neo4j(profile),
                    database=db_name or profile,
                    enabled=True,
                )
                svc.set_query_enhancer(enhancer)
                logger.info("[MCP] McalQueryEnhancer (LLM) attached for profile '%s'", profile)
            except Exception as qe_err:
                logger.warning("[MCP] McalQueryEnhancer init failed for '%s': %s", profile, qe_err)
        else:
            logger.info("[MCP] Using built-in rule-based QueryEnhancer for profile '%s'", profile)
        _search_services[profile] = svc
        logger.info("[MCP] SearchService initialized for profile '%s' (database='%s')", profile, db_name or profile)
        return svc
    except Exception as e:
        logger.warning("[MCP] SearchService init failed for profile '%s': %s — search tools will return errors", profile, e)
    return None

async def _warmup():
    """Eagerly initialize heavy resources so the first user request is fast.

    Called once at server startup. Failures are logged but non-fatal.
    """
    import time as _time
    t0 = _time.monotonic()
    logger.info("[Warmup] Starting eager resource initialization...")

    # 1. Qdrant client (TLS handshake)
    try:
        _get_qdrant()
        logger.info("[Warmup] Qdrant client ready (%.1fs)", _time.monotonic() - t0)
    except Exception as e:
        logger.warning("[Warmup] Qdrant client failed: %s", e)

    # 2. SearchService per profile (includes Neo4j driver)
    for profile in ("illd", "mcal"):
        try:
            svc = _get_search_service(profile)
            if svc:
                # 3. Force-load the SentenceTransformer embedding model
                svc._embed_query("warmup")
                logger.info("[Warmup] SearchService('%s') + embedding model ready (%.1fs)",
                            profile, _time.monotonic() - t0)
        except Exception as e:
            logger.warning("[Warmup] SearchService('%s') failed: %s", profile, e)

    # 4. CacheService + its semantic tier model
    try:
        cs = _get_cache_service()
        if cs and hasattr(cs, 'semantic'):
            cs.semantic._get_model()
            logger.info("[Warmup] CacheService + SemanticCache model ready (%.1fs)",
                        _time.monotonic() - t0)
    except Exception as e:
        logger.warning("[Warmup] CacheService failed: %s", e)

    logger.info("[Warmup] Complete in %.1fs", _time.monotonic() - t0)


def _get_session_manager(workspace_id: str = "illd"):
    global _session_manager
    if _session_manager is None:
        try:
            from src.MemoryLayer.memory.ontology_loader import get_ontology
            from src.MemoryLayer.memory.working_memory.manager import (
                InMemoryBackend,
                RedisBackend,
                WorkingMemoryManager,
            )

            backend = None
            backend_name = "InMemoryBackend"
            rc = _get_redis()
            if rc:
                try:
                    rc.ping()
                    from urllib.parse import urlparse

                    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
                    parsed = urlparse(redis_url)
                    backend = RedisBackend(
                        host=parsed.hostname or "localhost",
                        port=parsed.port or 6379,
                        db=int((parsed.path or "/0").lstrip("/") or "0"),
                        password=parsed.password,
                    )
                    backend_name = "RedisBackend"
                    logger.info("[MCP] WorkingMemoryManager using RedisBackend")
                except Exception:
                    backend = InMemoryBackend()
                    logger.info("[MCP] WorkingMemoryManager using InMemoryBackend (Redis unavailable)")
            else:
                backend = InMemoryBackend()
                logger.info("[MCP] WorkingMemoryManager using InMemoryBackend")

            ontology = get_ontology()
            wm = WorkingMemoryManager(
                ontology=ontology,
                profile=workspace_id,
                backend=backend,
            )
            _session_manager = _WorkingMemorySessionAdapter(
                working_memory_manager=wm,
                postgres_client=_get_postgres_client(),
                backend=backend,
                workspace_id=workspace_id,
            )
            _session_manager._backend_name = backend_name
        except Exception as e:
            logger.warning("[MCP] SessionManager init failed: %s", e)
    return _session_manager

def _get_context_builder(max_tokens=8000, budget_unit="tokens"):
    try:
        from src.MemoryLayer.memory.context_builder import LegacyContextBuilder
        # Wire approved-pattern lookup (Ticket 1)
        pattern_store = None
        try:
            from src.MemoryLayer.memory.semantic_memory import PatternStore, Embedder
            embedder = Embedder()
            qdrant = _get_qdrant()
            if qdrant:
                pattern_store = PatternStore(
                    embedder=embedder,
                    collection="approved_patterns",
                    _client=qdrant,
                )
        except Exception:
            pass  # graceful degradation — patterns simply won't be included
        return LegacyContextBuilder(
            max_tokens=max_tokens,
            budget_unit=budget_unit,
            pattern_store=pattern_store,
        )
    except Exception as e:
        logger.error("ContextBuilder import failed: %s", e)
        return None

# ── Sprint 3: Ephemeral Sandbox (lazy init) ───────────────────────────────
_sandbox_manager = None

def _get_sandbox_manager():
    global _sandbox_manager
    if _sandbox_manager is None:
        try:
            from src.MemoryLayer.memory.ephemeral_sandbox import (
                SandboxManager, _SentenceTransformerEmbedder,
            )
            embedder = _SentenceTransformerEmbedder()
            _sandbox_manager = SandboxManager(embedder=embedder, max_chunks=5000)
            logger.info("[MCP] SandboxManager initialized (with real embedder)")
        except Exception as e:
            logger.warning("[MCP] SandboxManager init failed: %s", e)
    return _sandbox_manager

# ── Sprint 4: Review Gate + Feedback (lazy init) ──────────────────────────
_confidence_calc = None
_feedback_sink = None

def _get_confidence_calc():
    global _confidence_calc
    if _confidence_calc is None:
        try:
            from src.ReviewGate.confidence import ConfidenceCalculator
            _confidence_calc = ConfidenceCalculator()
            logger.info("[MCP] ConfidenceCalculator initialized")
        except Exception as e:
            logger.warning("[MCP] ConfidenceCalculator init failed: %s", e)
    return _confidence_calc

def _get_feedback_sink():
    global _feedback_sink
    if _feedback_sink is None:
        try:
            from src.ReviewGate.confidence import FeedbackSink
            pg = _get_postgres_client()

            # Wire learning loop: PatternStore (Neo4j) + PatternIndex (Qdrant)
            pattern_store = None
            pattern_index = None
            try:
                from src.MemoryLayer.memory.semantic_memory import PatternStore, PatternIndex, Embedder
                embedder = Embedder()

                neo4j_driver = _get_neo4j()
                if neo4j_driver:
                    pattern_store = PatternStore(neo4j_driver=neo4j_driver, embedder=embedder)
                    logger.info("[MCP] PatternStore initialized for learning loop")

                qdrant = _get_qdrant()
                qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
                if qdrant:
                    pattern_index = PatternIndex(qdrant_url=qdrant_url, embedder=embedder)
                    logger.info("[MCP] PatternIndex initialized for learning loop")
            except ImportError as ie:
                logger.info("[MCP] Semantic memory not available — learning loop disabled: %s", ie)
            except Exception as e:
                logger.warning("[MCP] Learning loop init failed: %s", e)

            _feedback_sink = FeedbackSink(
                postgres_client=pg if pg and pg.available else None,
                pattern_store=pattern_store,
                pattern_index=pattern_index,
            )
            logger.info("[MCP] FeedbackSink initialized (learning_loop=%s)",
                        "enabled" if pattern_store else "disabled")
        except Exception as e:
            logger.warning("[MCP] FeedbackSink init failed: %s", e)
    return _feedback_sink

# ── Sprint 5: RLM + Ingestion (lazy init) ─────────────────────────────────
_rlm_orchestrator_cls = None
_ingestion_service = None

def _get_rlm_orchestrator(module="CAN", profile="mcal"):
    try:
        from src.HybridRAG.code.querier.rlm_orchestrator import RLMOrchestrator
        svc = _get_search_service(profile)
        search_fn = svc.hybrid_search if svc and svc.available else None
        return RLMOrchestrator(module=module, profile=profile, search_fn=search_fn)
    except Exception as e:
        logger.warning("[MCP] RLMOrchestrator init failed: %s", e)
        return None

def _get_ingestion_service():
    global _ingestion_service
    if _ingestion_service is None:
        try:
            from src.IngestionPipeline.ingestion_service import IngestionService
            pg = _get_postgres_client()
            from src.IngestionPipeline.ingestion_service import IngestionJobTracker
            tracker = IngestionJobTracker(postgres_client=pg)
            _ingestion_service = IngestionService(
                neo4j_driver=_get_neo4j(),
                job_tracker=tracker,
                on_module_ingested=_on_module_ingested,
            )
            logger.info("[MCP] IngestionService initialized (with cache invalidation hook)")
        except Exception as e:
            logger.warning("[MCP] IngestionService init failed: %s", e)
    return _ingestion_service


def _on_module_ingested(module_name: str, workspace_id: str):
    """Cache invalidation hook called by IngestionService after a module completes."""
    cs = _get_cache_service()
    if cs:
        result = cs.invalidate_module(module_name)
        logger.info(
            "[MCP] Cache invalidated for module '%s' (workspace=%s): %s",
            module_name, workspace_id, result,
        )
    else:
        logger.debug("[MCP] CacheService unavailable — skipping post-ingestion invalidation for '%s'", module_name)

# ── Sprint 6: Cache, Ontology, Observability, Viz, Auth ────────────────────
_cache_service = None

def _get_cache_service():
    global _cache_service
    if _cache_service is None:
        try:
            from src.Configuration.cache_service import CacheService
            _cache_service = CacheService()
            logger.info("[MCP] CacheService initialized")
        except Exception as e:
            logger.warning("[MCP] CacheService init failed: %s", e)
    return _cache_service

_ontology_services: Dict[str, Any] = {}   # keyed by profile

def _get_ontology_service(profile: str = "illd"):
    if profile in _ontology_services:
        return _ontology_services[profile]
    try:
        from src.Configuration.services import OntologyService
        svc = OntologyService(neo4j_driver=_get_neo4j(profile))
        _ontology_services[profile] = svc
        return svc
    except Exception as e:
        logger.warning("[MCP] OntologyService init failed for '%s': %s", profile, e)
    return None

_observability_services: Dict[str, Any] = {}   # keyed by profile

def _get_observability_service(profile: str = "illd"):
    if profile in _observability_services:
        return _observability_services[profile]
    try:
        from src.Configuration.services import ObservabilityService
        svc = ObservabilityService(neo4j_driver=_get_neo4j(profile))
        _observability_services[profile] = svc
        return svc
    except Exception as e:
        logger.warning("[MCP] ObservabilityService init failed for '%s': %s", profile, e)
    return None

# ── Sprint 7: Knowledge Intelligence (API + Deps + Traceability) ──────────
_ki_services: Dict[str, Any] = {}   # keyed by profile

def _get_ki_service(profile: str = "illd"):
    if profile in _ki_services:
        return _ki_services[profile]
    try:
        from src.HybridRAG.code.querier.knowledge_intelligence import KnowledgeIntelligenceService
        # Resolve the database name from the bound Neo4j instance
        db_name = None
        try:
            from src.HybridRAG.code.neo4j_manager import get_instance_config
            inst = _NEO4J_INSTANCE or profile
            db_name = get_instance_config(inst).database
        except Exception:
            pass
        svc = KnowledgeIntelligenceService(
            neo4j_driver=_get_neo4j(profile),
            search_service=_get_search_service(profile),
            default_database=db_name or "neo4j",
        )
        _ki_services[profile] = svc
        logger.info("[MCP] KnowledgeIntelligenceService initialized for profile '%s'", profile)
        return svc
    except Exception as e:
        logger.warning("[MCP] KnowledgeIntelligenceService init failed for '%s': %s", profile, e)
    return None

# ═════════════════════════════════════════════════════════════════════════
#  MCP SERVER
# ═════════════════════════════════════════════════════════════════════════
def _env_bool(n: str, d: bool) -> bool:
    r = os.environ.get(n)
    return r.strip().lower() in ("1", "true", "yes") if r else d

mcp = FastMCP(
    "AI Core Engine MCP Server",
    host=os.environ.get("FASTMCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("FASTMCP_PORT", os.environ.get("PORT", "8000"))),
    streamable_http_path=os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp"),
    stateless_http=_env_bool("FASTMCP_STATELESS_HTTP", True),
)

# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 11 — OBSERVABILITY & HEALTH  (Sprint 1: IMPLEMENTED)
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def health_check(verbose: bool = False, include_test_query: bool = False) -> str:
    """Check platform health: Neo4j, Qdrant, Redis, GPT4IFX.

    Call at DA startup to verify all backend services are reachable.
    Access tier: public.

    Parameters:
        verbose (bool): Include detailed connection info such as URIs,
            memory usage, and collection names. Default False.
        include_test_query (bool): Run a test Cypher query against Neo4j
            (MATCH (n) RETURN count(n)) to verify read access. Default False.

    Returns (JSON):
        {
          "status": "healthy" | "degraded",
          "timestamp": "<ISO-8601>",
          "components": {
            "neo4j":   {"status": "healthy"|"unavailable", "connected": bool, ...},
            "qdrant":  {"status": "healthy"|"unavailable", "collections": int, ...},
            "redis":   {"status": "healthy"|"unavailable", "ping": bool, ...},
            "gpt4ifx": {"status": "healthy"|"unavailable", "token_valid": bool, ...}
          }
        }
    """
    denied = _authorize("health_check")
    if denied:
        return denied

    def _run_health_check():
        results: Dict[str, Any] = {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": {},
        }
        all_ok = True

        # Neo4j
        neo = {"status": "unknown"}
        try:
            drv = _get_neo4j()
            if drv:
                with drv.session() as s:
                    s.run("RETURN 1").single()
                    neo = {"status": "healthy", "connected": True}
                    if include_test_query:
                        neo["node_count"] = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
                    if verbose:
                        neo["uri"] = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
            else:
                neo = {"status": "unavailable", "error": "Driver not initialized"}
                all_ok = False
        except Exception as e:
            neo = {"status": "unavailable", "error": str(e)}
            all_ok = False
        results["components"]["neo4j"] = neo

        # Qdrant
        qdr = {"status": "unknown"}
        try:
            qc = _get_qdrant()
            if qc:
                cols = qc.get_collections()
                qdr = {"status": "healthy", "connected": True, "collections": len(cols.collections)}
                if verbose:
                    qdr["collection_names"] = [c.name for c in cols.collections]
            else:
                qdr = {"status": "unavailable", "error": "Client not initialized"}
                all_ok = False
        except Exception as e:
            qdr = {"status": "unavailable", "error": str(e)}
            all_ok = False
        results["components"]["qdrant"] = qdr

        # Redis
        rds = {"status": "unknown"}
        try:
            rc = _get_redis()
            if rc:
                rds = {"status": "healthy", "connected": True, "ping": rc.ping()}
                if verbose:
                    info = rc.info("memory")
                    rds["used_memory_human"] = info.get("used_memory_human", "?")
            else:
                rds = {"status": "unavailable", "error": "Client not initialized"}
                all_ok = False
        except Exception as e:
            rds = {"status": "unavailable", "error": str(e)}
            all_ok = False
        results["components"]["redis"] = rds

        # GPT4IFX (LLM endpoint)
        gpt = {"status": "unknown"}
        try:
            from src.HybridRAG.code.token_manager import get_token, get_token_info
            try:
                token = get_token()
                info = get_token_info(token)
                gpt = {
                    "status": "healthy",
                    "token_valid": True,
                    "token_expires": info.get("exp", "?"),
                    "token_remaining": info.get("remaining", "?"),
                }
            except RuntimeError as tok_err:
                gpt = {"status": "unavailable", "error": str(tok_err)}
                all_ok = False
        except Exception as e:
            gpt = {"status": "unavailable", "error": str(e)}
            all_ok = False

        return results, all_ok, gpt

    results, all_ok, gpt = await asyncio.to_thread(_run_health_check)
    results["components"]["gpt4ifx"] = gpt

    # Update Prometheus backend health gauges
    for backend_name in ("neo4j", "qdrant", "redis"):
        comp = results["components"].get(backend_name, {})
        BACKEND_UP.labels(backend=backend_name).set(1 if comp.get("status") == "healthy" else 0)

    if not all_ok:
        results["status"] = "degraded"
    return _ok(results)


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 1 — SEARCH & QUERY (6 tools) — Sprint 2
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("search_database")
async def search_database(
    query: str, max_results: int = 10, include_relationships: bool = False,
    filter_by_module: Optional[str] = None, filter_by_node_type: Optional[List[str]] = None,
    offset: int = 0, workspace_id: str = "illd", alpha: float = _DEFAULT_SEARCH_ALPHA,
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Hybrid semantic (Qdrant vector) + graph (Neo4j) search across the knowledge graph.

    Primary search entry point. Combines vector similarity with graph traversal
    for best relevance. Results are cached (LRU → Semantic → RAG tiers).
    When session_id is provided with an active sandbox, queries route through
    the sandbox overlay. Access tier: public.

    Parameters:
        query (str): Natural language search query. Required.
        max_results (int): Maximum results to return. Default 10.
        include_relationships (bool): Include relationship data in results. Default False.
        filter_by_module (str | None): Filter by module name (e.g. "Adc", "Spi", "Can").
            Run `list_available_modules` to get valid module names.
        filter_by_node_type (list[str] | None): Filter by node labels. Valid labels
            for illd: APIFunction, DataStructure, SoftwareRequirement, TestCase,
            TestResult, SourceFile, Module, Register, Bitfield, HardwareSpec, etc.
            For mcal: StakeholderRequirement, ProductRequirement, EA_Function,
            EA_DataType, MCALModule, etc.
            Run `get_ontology_schema` to get the full list of valid node labels.
        offset (int): Pagination offset for results. Default 0.
        workspace_id (str): Target workspace — "illd" (default) or "mcal".
            Run `list_ontology_profiles` to see all available profiles.
        alpha (float): Vector-vs-graph blend weight. 0.0 = pure vector search,
            1.0 = pure graph search, 0.6 = default balanced blend.
        session_id (str | None): Link to an active sandbox session for overlay queries.
            When provided with an active sandbox, routes through HybridGraphService.

    Returns (JSON):
        {
          "results": [{"node_id": str, "label": str, "score": float,
                        "properties": {...}, "relationships": [...] | None}],
          "total_count": int, "query": str
        }
    """
    denied = _authorize("search_database", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        # ── Plan 2: Sandbox routing (shallow) ──
        query_mode = query_mode
        if query_mode == "sandbox":
            hybrid = graph_service
            results = hybrid.search(query, top_k=max_results, alpha=alpha)
            return _ok({
                "results": [{"node_id": r.node_id, "content": r.content,
                              "score": round(r.score, 4), "origin": r.origin,
                              "node_type": r.node_type} for r in results],
                "total_count": len(results), "query": query, "source": "sandbox",
            })

        # ── Cache check (LRU → Semantic → RAG) ──
        cache = _get_cache_service()
        cache_meta = {"ws": workspace_id, "mod": filter_by_module or "", "alpha": str(alpha)}
        cache_key = f"{workspace_id}:{filter_by_module or ''}:{alpha}:{query}"
        if cache:
            hit = cache.get(cache_key, metadata=cache_meta)
            if hit and hit.get("hit"):
                cache_type = hit.get("cache_type", "lru")
                CACHE_REQUESTS_TOTAL.labels(cache_type=cache_type, result="hit").inc()
                SEARCH_REQUESTS_TOTAL.labels(workspace=workspace_id).inc()
                # Update cache gauges
                _update_cache_gauges(cache)
                return _ok(hit.get("result") or hit.get("value"))
            else:
                CACHE_REQUESTS_TOTAL.labels(cache_type="lru", result="miss").inc()

        SEARCH_REQUESTS_TOTAL.labels(workspace=workspace_id).inc()
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            ERROR_TOTAL.labels(error_type="connection", component="search").inc()
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected — run health_check for details")

        # ── Measure per-backend query latency (Ticket 7) ──
        search_t0 = time.time()
        result = await svc.hybrid_search_async(
            query=query, max_results=max_results,
            include_relationships=include_relationships,
            filter_by_module=filter_by_module,
            filter_by_node_type=filter_by_node_type,
            offset=offset, workspace_id=workspace_id, alpha=alpha,
        )
        search_elapsed = time.time() - search_t0
        QUERY_LATENCY.labels(backend="total").observe(search_elapsed)
        SEARCH_DURATION.labels(stage="total").observe(search_elapsed)

        # ── Store in cache ──
        if cache and result:
            cache.put(cache_key, result, metadata=cache_meta)
            _update_cache_gauges(cache)

        return _ok(result)
    except Exception as exc:
        logger.exception("search_database failed")
        _classify_and_record_error(exc, "search")
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("search_nodes")
async def search_nodes(
    label: str, keyword: Optional[str] = None, filters: Optional[Dict[str, Any]] = None,
    return_properties: Optional[List[str]] = None, limit: int = 10, offset: int = 0,
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Deterministic structured query by node label, keyword, and property filters.

    Use this instead of search_database when you know the exact node type and
    want precise, non-semantic filtering. Access tier: public.

    Parameters:
        label (str): Node label to query. Required.
            illd labels: APIFunction, DataStructure, SoftwareRequirement, TestCase,
            TestResult, SourceFile, Module, Register, Bitfield, HardwareSpec, etc.
            mcal labels: StakeholderRequirement, ProductRequirement, EA_Function,
            EA_DataType, MCALModule, VerificationStep, etc.
            Run `get_ontology_schema` to get the full list of valid node labels.
        keyword (str | None): Full-text keyword filter applied to node properties.
        filters (dict | None): Property-level filters, e.g. {"module": "Adc",
            "status": "Approved"}. Keys must be valid properties for the given label.
        return_properties (list[str] | None): Specific properties to include in
            results. None returns all properties.
        limit (int): Max results to return. Default 10.
        offset (int): Pagination offset. Default 0.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "nodes": [{"node_id": str, "label": str, "properties": {...}}],
          "total_count": int
        }
    """
    denied = _authorize("search_nodes", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        # ── Plan 2: Sandbox routing (shallow) ──
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                keywords = [keyword] if keyword else []
                node_types = [label] if label else None
                search_results = sandbox.graph.keyword_search(
                    keywords=keywords, node_types=node_types, top_k=limit)
                nodes = [
                    {
                        "node_id": r.node_id,
                        "label": r.node_type,
                        "properties": {
                            "content": r.content,
                            "score": r.score,
                            "_origin": r.origin,
                        }
                    }
                    for r in search_results
                ]
                return _ok({"nodes": nodes, "total_count": len(nodes)})
        
        # ── No session → existing production path (unchanged) ──
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.search_nodes, label=label, keyword=keyword, filters=filters,
            return_properties=return_properties,
            limit=limit, offset=offset, workspace_id=workspace_id)
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("get_node_by_id")
async def get_node_by_id(
    document_id: Optional[str] = None, jama_id: Optional[int] = None,
    label: Optional[str] = None, workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Exact lookup of a single node by document ID or Jama item ID.

    Provide at least one of document_id or jama_id. Access tier: public.

    Parameters:
        document_id (str | None): The unique document_id property of the node.
            Obtain from search_database or search_nodes results.
        jama_id (int | None): The Jama item ID (integer). Used primarily in
            mcal workspace for requirement traceability.
        label (str | None): Optional node label hint to narrow the lookup.
            Run `get_ontology_schema` for valid labels.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "node": {"node_id": str, "label": str, "properties": {...},
                   "relationships": [...]}
        }
    """
    denied = _authorize("get_node_by_id", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        # ── Plan 2: Sandbox routing (shallow) ──
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox and document_id:
                node = sandbox.graph.get_node(document_id)
                if node:
                    return _ok({
                        "node": {
                            "node_id": node.get("_node_id"),
                            "label": node.get("_node_type"),
                            "properties": {k: v for k, v in node.items() if not k.startswith("_")},
                            "relationships": []
                        }
                    })
        
        # ── No session → existing production path (unchanged) ──
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.get_node_by_id, document_id=document_id, jama_id=jama_id,
            label=label, workspace_id=workspace_id)
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("get_neighbors")
async def get_neighbors(
    document_id: Optional[str] = None, jama_id: Optional[int] = None,
    direction: str = "both", relationship_types: Optional[List[str]] = None,
    depth: int = 1,
    limit: int = 20, workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    hybrid_traversal: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Direct graph traversal — get all nodes connected to a known node.

    Provide at least one of document_id or jama_id. Access tier: developer.

    Parameters:
        document_id (str | None): The document_id of the source node.
            Obtain from search_database, search_nodes, or get_node_by_id.
        jama_id (int | None): The Jama item ID of the source node.
        direction (str): Traversal direction — "in", "out", or "both" (default).
        relationship_types (list[str] | None): Filter by relationship types.
            illd types: IMPLEMENTS, TRACES_TO, VERIFIES, DEPENDS_ON,
            USES_STRUCTURE, ACCESSES_REGISTER, BELONGS_TO, CALLS_INTERNALLY, etc.
            mcal types: DERIVES_FROM, VERIFIED_BY, HAS_RESULT, BELONGS_TO_MODULE,
            EA_DEPENDS_ON, EA_ACCESSES_REGISTER, EA_IMPLEMENTS, etc.
            Run `get_ontology_schema` to get the full list of relationship types.
        depth (int): Number of hops to traverse. Default 1.
            When depth > 1 and a sandbox session is active, uses hybrid traversal
            that seamlessly continues into production Neo4j at boundary leaves.
        limit (int): Max neighbor nodes to return. Default 20.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "source": {"node_id": str, "label": str},
          "neighbors": [{"node_id": str, "label": str, "relationship": str,
                          "direction": str, "properties": {...}}],
          "total_count": int
        }
    """
    denied = _authorize("get_neighbors", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        # ── Plan 2: Sandbox routing with hybrid traversal ──
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox and document_id:
                source = sandbox.graph.get_node(document_id)
                if source:
                    # Multi-hop: use HybridTraversal (shadow → prod continuation)
                    if depth > 1 and hybrid_traversal:
                        result = hybrid_traversal.traverse(
                            start_id=document_id,
                            direction=direction,
                            max_depth=depth,
                            rel_types=relationship_types,
                            limit=limit,
                        )
                        return _ok({
                            "source": {"node_id": document_id, "label": source.get("_node_type", "Unknown")},
                            "nodes": result["nodes"],
                            "edges": result["edges"],
                            "total_nodes": result["total_nodes"],
                            "boundary_continuations": result["boundary_continuations"],
                            "continued_from": result["continued_from"],
                            "truncated": result["truncated"],
                        })

                    # Single-hop: direct NetworkX traversal (fast path)
                    neighbors = []
                    g = sandbox.graph._graph
                    if direction in ("out", "both"):
                        for _, target_id, edge_data in g.out_edges(document_id, data=True):
                            if relationship_types and edge_data.get("_rel_type") not in relationship_types:
                                continue
                            neighbor = sandbox.graph.get_node(target_id)
                            if neighbor:
                                neighbors.append({
                                    "node_id": target_id,
                                    "label": neighbor.get("_node_type", "Unknown"),
                                    "relationship": edge_data.get("_rel_type", "RELATED_TO"),
                                    "direction": "out",
                                    "origin": neighbor.get("_origin", "unknown"),
                                    "properties": {k: v for k, v in neighbor.items() if not k.startswith("_")}
                                })
                    if direction in ("in", "both"):
                        for source_id, _, edge_data in g.in_edges(document_id, data=True):
                            if relationship_types and edge_data.get("_rel_type") not in relationship_types:
                                continue
                            neighbor = sandbox.graph.get_node(source_id)
                            if neighbor:
                                neighbors.append({
                                    "node_id": source_id,
                                    "label": neighbor.get("_node_type", "Unknown"),
                                    "relationship": edge_data.get("_rel_type", "RELATED_TO"),
                                    "direction": "in",
                                    "origin": neighbor.get("_origin", "unknown"),
                                    "properties": {k: v for k, v in neighbor.items() if not k.startswith("_")}
                                })
                    return _ok({
                        "source": {"node_id": document_id, "label": source.get("_node_type", "Unknown")},
                        "neighbors": neighbors[:limit],
                        "total_count": len(neighbors)
                    })
        
        # ── No session → existing production path (unchanged) ──
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.get_neighbors, document_id=document_id, jama_id=jama_id,
            direction=direction, relationship_types=relationship_types,
            limit=limit, workspace_id=workspace_id)
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("shortest_path")
async def shortest_path(
    from_document_id: Optional[str] = None, from_jama_id: Optional[int] = None,
    to_document_id: Optional[str] = None, to_jama_id: Optional[int] = None,
    max_depth: int = 8, workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    hybrid_traversal: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Find the shortest path between two nodes in the knowledge graph.

    Provide at least one identifier for both the source and target node.
    Access tier: developer.

    Parameters:
        from_document_id (str | None): Document ID of the source node.
            Obtain from search_database, search_nodes, or get_node_by_id.
        from_jama_id (int | None): Jama item ID of the source node.
        to_document_id (str | None): Document ID of the target node.
        to_jama_id (int | None): Jama item ID of the target node.
        max_depth (int): Maximum path length (hops). Default 8.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "path": [{"node_id": str, "label": str, ...}],
          "relationships": [{"type": str, "from": str, "to": str}],
          "length": int,
          "found": bool
        }
    """
    denied = _authorize("shortest_path", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        # ── Plan 2: Sandbox routing — HybridTraversal shortest_path ──
        query_mode = query_mode
        if query_mode in ("hybrid", "sandbox"):
            if hybrid_traversal and from_document_id and to_document_id:
                result = hybrid_traversal.shortest_path(
                    start_id=from_document_id,
                    end_id=to_document_id,
                    max_depth=max_depth,
                    rel_types=None,
                )
                if result:
                    return _ok({
                        "path": result["path_nodes"],
                        "relationships": result["path_edges"],
                        "length": result["total_hops"],
                        "found": True,
                        "segments": result["segments"],
                    })
                else:
                    return _ok({"path": [], "relationships": [], "length": 0, "found": False})
        
        # ── No session → existing production path (unchanged) ──
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.shortest_path, from_id=from_document_id, from_jama=from_jama_id,
            to_id=to_document_id, to_jama=to_jama_id,
            max_depth=max_depth, workspace_id=workspace_id)
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("execute_cypher")
async def execute_cypher(
    query: str, parameters: Optional[Dict[str, Any]] = None, workspace_id: str = "illd",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Execute a read-only Cypher query against the Neo4j knowledge graph.

    Write clauses (CREATE, MERGE, DELETE, SET, REMOVE, DETACH, DROP) are
    automatically rejected for safety. Access tier: developer.

    Parameters:
        query (str): A valid Neo4j Cypher read query. Required.
            Example: "MATCH (n:APIFunction) WHERE n.module = $mod RETURN n LIMIT 10"
            Use `get_ontology_schema` to discover valid node labels and relationship types.
        parameters (dict | None): Parameterized query values, e.g. {"mod": "Adc"}.
            Always use parameters instead of string interpolation for safety.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "records": [{...}, ...],
          "count": int
        }
    """
    denied = _authorize("execute_cypher", workspace_id=workspace_id)
    if denied:
        return denied
    # Safety: reject write clauses using word boundaries to avoid false positives
    # on property names like RESET_VALUE, CREATED_AT, OFFSET, etc.
    import re as _re
    upper = query.upper()
    _write_patterns = [
        (r'\bCREATE\b', 'CREATE'), (r'\bMERGE\b', 'MERGE'),
        (r'\bDELETE\b', 'DELETE'), (r'\bSET\b', 'SET'),
        (r'\bREMOVE\b', 'REMOVE'), (r'\bDETACH\b', 'DETACH'),
        (r'\bDROP\b', 'DROP'), (r'CALL\s*\{', 'CALL {'),
    ]
    for pattern, label in _write_patterns:
        if _re.search(pattern, upper):
            return _err("QUERY_REJECTED", f"Write clause '{label}' is not allowed. Only read queries permitted.")
    try:
        query_mode = query_mode
        # Hybrid query path: run Cypher on prod Neo4j + patch with sandbox overrides
        if query_mode in ("sandbox", "hybrid") and graph_service and sandbox_ctx:
            hybrid = graph_service
            try:
                result = await asyncio.to_thread(
                    hybrid.deep_query, cypher=query, params=parameters or {}, workspace_id=workspace_id
                )
                return _ok({"records": result if isinstance(result, list) else [result], "count": len(result) if isinstance(result, list) else 1, "_origin": "sandbox"})
            except Exception:
                pass  # Fall through to prod query
        
        # Default path: query production Neo4j (full graph, always available)
        svc = _get_search_service(workspace_id)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.execute_cypher, query=query, parameters=parameters, workspace_id=workspace_id)
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 2 — API INTELLIGENCE (3 tools) — Sprint 3
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("query_api_function")
async def query_api_function(function_name: str, workspace_id: str = "illd",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Retrieve 25+ enriched fields for an API function from the knowledge graph.

    Returns signature, parameters, return type, dependencies, usage patterns,
    traceability links, and related requirements. Access tier: public.

    Parameters:
        function_name (str): Exact API function name (e.g. "Adc_Init",
            "Can_Write", "Spi_SetupEB"). Required.
            Run `search_nodes` with label="APIFunction" to discover function names.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "function_name": str, "signature": str, "return_type": str,
          "parameters": [{"name": str, "type": str, "description": str}],
          "module": str, "dependencies": [...], "callers": [...],
          "requirements": [...], "test_cases": [...], "description": str, ...
        }
    """
    denied = _authorize("query_api_function")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                terms = [function_name] + [t for t in function_name.replace("_", " ").split() if t]
                results = sandbox.graph.keyword_search(terms, top_k=10)
                matches = []
                fn_lower = function_name.lower()
                for r in results:
                    if fn_lower in (r.node_id or "").lower() or fn_lower in (r.content or "").lower():
                        matches.append({
                            "node_id": r.node_id,
                            "node_type": r.node_type,
                            "summary": r.content,
                            "score": round(r.score, 4),
                            "origin": r.origin,
                        })
                if matches:
                    return _ok({
                        "function_name": function_name,
                        "source": "sandbox",
                        "matches": matches,
                    })
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.query_api_function, function_name, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("get_type_definition")
async def get_type_definition(struct_name: str, module: Optional[str] = None, workspace_id: str = "illd",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Retrieve struct/enum/typedef definition with fields, defaults, and related functions.

    Returns the C type definition, member fields, default values, and
    functions that use or produce this type. Access tier: public.

    Parameters:
        struct_name (str): Type name (e.g. "Adc_ConfigType", "Can_PduType",
            "Spi_ChannelType"). Required.
            Run `search_nodes` with label="DataStructure" to discover type names.
        module (str | None): Module name to narrow the search (e.g. "Adc", "Can").
            Run `list_available_modules` to get valid module names.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "name": str, "kind": "struct"|"enum"|"typedef",
          "c_definition": str, "module": str,
          "fields": [{"name": str, "type": str, "default": str|None}],
          "related_functions": [{"name": str, "usage": "parameter"|"return"}]
        }
    """
    denied = _authorize("get_type_definition")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                terms = [struct_name] + [t for t in struct_name.replace("_", " ").split() if t]
                results = sandbox.graph.keyword_search(terms, top_k=10)
                entries = []
                struct_lower = struct_name.lower()
                for r in results:
                    if struct_lower in (r.node_id or "").lower() or struct_lower in (r.content or "").lower():
                        entries.append({
                            "node_id": r.node_id,
                            "kind": r.node_type,
                            "definition": r.content,
                            "score": round(r.score, 4),
                            "origin": r.origin,
                        })
                if entries:
                    return _ok({"name": struct_name, "module": module, "source": "sandbox", "matches": entries})
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.get_type_definition, struct_name, module, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("generate_initialization_code")
async def generate_initialization_code(
    struct_name: str, user_overrides: Optional[Dict] = None, variable_name: Optional[str] = None,
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Generate C struct initializer code merging KG defaults with user overrides.

    Produces compilable C initialization code for iLLD/MCAL configuration
    structs, using knowledge-graph defaults for any fields not overridden.
    Access tier: public.

    Parameters:
        struct_name (str): Struct type name (e.g. "IfxAsc_Asc_Config",
            "Adc_ConfigType"). Required.
            Run `search_nodes` with label="DataStructure" to discover struct names,
            or run `get_type_definition` to inspect a struct's fields first.
        user_overrides (dict | None): Field-value overrides, e.g.
            {"baudrate": 115200, "rxPin": "IfxAsc_RX_P14_1"}.
        variable_name (str | None): C variable name for the declaration.
            Defaults to "config" if not specified.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "code": "<C initializer source code>",
          "struct_name": str, "variable_name": str,
          "fields_from_kg": int, "fields_overridden": int
        }
    """
    denied = _authorize("generate_initialization_code")
    if denied: return denied
    try:
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.generate_initialization_code, struct_name, user_overrides, variable_name, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 3 — DEPENDENCY ANALYSIS (3 tools) — Sprint 3
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("query_dependencies")
async def query_dependencies(
    function_name: str, module_name: Optional[str] = None, max_depth: int = _DEFAULT_MAX_DEPTH,
    include_hardware: bool = False, workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None
) -> str:
    """Resolve direct and transitive dependencies with topological init sequence.

    Walks the DEPENDS_ON / CALLS_INTERNALLY / CALLS_EXTERNAL relationship
    chains to build a full dependency tree. Access tier: public.

    Parameters:
        function_name (str): API function name (e.g. "Adc_Init"). Required.
            Run `search_nodes` with label="APIFunction" to discover function names.
        module_name (str | None): Module name to scope the search (e.g. "Adc").
            Run `list_available_modules` to get valid module names.
        max_depth (int): Maximum traversal depth. Default from MAX_DEPENDENCIES_DEPTH
            env var (fallback: 3). Per-request override supported.
        include_hardware (bool): Include hardware register dependencies
            (ACCESSES_REGISTER relationships). Default False.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "function": str, "module": str,
          "dependencies": [{"name": str, "type": "direct"|"transitive", "depth": int}],
          "init_sequence": [str],  // topologically sorted initialization order
          "hardware_deps": [{"register": str, "access": str}] | None
        }
    """
    denied = _authorize("query_dependencies")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                seed = sandbox.graph.keyword_search([function_name], top_k=1)
                if seed:
                    seed_id = seed[0].node_id
                    deps = []
                    g = sandbox.graph._graph
                    for _, target, edge_data in g.out_edges(seed_id, data=True):
                        rel = edge_data.get("_rel_type", "RELATED_TO")
                        if rel in ("DEPENDS_ON", "CALLS_INTERNALLY", "CALLS_EXTERNAL", "USES_STRUCTURE"):
                            deps.append({"name": target, "type": "direct", "depth": 1, "relationship": rel})
                    return _ok({
                        "function": function_name,
                        "module": module_name or "unknown",
                        "dependencies": deps,
                        "init_sequence": [function_name] + [d["name"] for d in deps],
                        "hardware_deps": [] if include_hardware else None,
                        "source": "sandbox",
                    })
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.query_dependencies, function_name, module_name, max_depth, include_hardware, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("validate_api_usage")
async def validate_api_usage(function_sequence: List[str], workspace_id: str = "illd",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Validate a function call sequence against the dependency graph.

    Checks that the provided calling order respects initialization
    dependencies (e.g. Adc_Init must come before Adc_StartGroupConversion).
    Access tier: public.

    Parameters:
        function_sequence (list[str]): Ordered list of function names representing
            the intended call sequence (e.g. ["Adc_Init", "Adc_SetupResultBuffer",
            "Adc_StartGroupConversion"]). Required.
            Run `search_nodes` with label="APIFunction" to discover function names.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "valid": bool, "sequence": [str],
          "violations": [{"function": str, "missing_dependency": str,
                          "position": int, "message": str}]
        }
    """
    denied = _authorize("validate_api_usage")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                violations = []
                for i, fn in enumerate(function_sequence):
                    exists = sandbox.graph.keyword_search([fn], top_k=1)
                    if not exists:
                        violations.append({
                            "function": fn,
                            "missing_dependency": "Function not found in sandbox overlay",
                            "position": i,
                            "message": f"{fn} not present in sandbox/prod overlay context",
                        })
                return _ok({"valid": len(violations) == 0, "sequence": function_sequence, "violations": violations, "source": "sandbox"})
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.validate_api_usage, function_sequence, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("detect_polling_requirements")
async def detect_polling_requirements(function_names: List[str], module: Optional[str] = None,
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Detect which API functions require status polling after invocation.

    Identifies functions that need a polling loop to check completion status
    before proceeding (common in MCAL/iLLD async peripherals). Access tier: public.

    Parameters:
        function_names (list[str]): List of function names to analyze
            (e.g. ["Adc_StartGroupConversion", "Spi_AsyncTransmit"]). Required.
            Run `search_nodes` with label="APIFunction" to discover function names.
        module (str | None): Module name to scope the analysis (e.g. "Adc", "Spi").
            Run `list_available_modules` to get valid module names.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "functions": [{
            "name": str, "requires_polling": bool,
            "polling_function": str | None,
            "status_check": str | None, "notes": str | None
          }]
        }
    """
    denied = _authorize("detect_polling_requirements")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "sandbox":
            sandbox = sandbox_ctx
            if sandbox:
                out = []
                for fn in function_names:
                    hits = sandbox.graph.keyword_search([fn], top_k=3)
                    text = " ".join(h.content for h in hits).lower()
                    requires = any(tok in text for tok in ("status", "busy", "complete", "poll"))
                    out.append({
                        "name": fn,
                        "requires_polling": requires,
                        "polling_function": f"{fn}_GetStatus" if requires else None,
                        "status_check": "!= BUSY" if requires else None,
                        "notes": "Heuristic from sandbox-local content",
                    })
                return _ok({"functions": out, "source": "sandbox"})
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.detect_polling_requirements, function_names, module, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 4 — TRACEABILITY (4 tools) — Sprint 3
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("find_requirement_traces")
async def find_requirement_traces(
    requirement_id: str, include_tests: bool = True, include_results: bool = True,
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None
) -> str:
    """Trace a requirement through the full V-Model chain: Req → Arch → Code → Test → Result.

    Walks TRACES_TO, IMPLEMENTS, VERIFIES, and HAS_RESULT relationships
    to build the complete traceability chain. Access tier: public.

    Parameters:
        requirement_id (str): The requirement's document_id or Jama ID as string.
            Required. Run `search_nodes` with label="SoftwareRequirement" (illd)
            or label="StakeholderRequirement"/"ProductRequirement" (mcal) to find IDs.
        include_tests (bool): Include linked TestCase nodes. Default True.
        include_results (bool): Include linked TestResult nodes. Default True.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "requirement": {"id": str, "title": str, "status": str},
          "architecture": [...], "code": [...],
          "tests": [...] | None, "results": [...] | None,
          "coverage": {"has_code": bool, "has_tests": bool, "has_results": bool}
        }
    """
    denied = _authorize("find_requirement_traces")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "hybrid":
            hybrid = graph_service
            if hybrid:
                cypher = """
                MATCH (req)
                WHERE req.requirement_id = $rid OR req.document_id = $rid OR toString(req.jama_id) = $rid
                OPTIONAL MATCH (req)-[:TRACES_TO|IMPLEMENTS|REALIZES*1..3]-(arch)
                OPTIONAL MATCH (arch)-[:IMPLEMENTS|CALLS_INTERNALLY|USES_STRUCTURE*1..2]-(code)
                RETURN labels(req)[0] as label, req.name as name, req.requirement_id as requirement_id,
                       req.module as module, count(DISTINCT arch) as architecture_count,
                       count(DISTINCT code) as code_count
                LIMIT 50
                """
                records = hybrid.deep_query(cypher, {"rid": requirement_id}, workspace_id)
                if records:
                    return _ok({"requirement": requirement_id, "records": records, "source": "hybrid"})
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.find_requirement_traces, requirement_id, include_tests, include_results, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("build_traceability_matrix")
async def build_traceability_matrix(module_name: str, output_format: str = "json",
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Build a module-wide requirement traceability coverage matrix.

    Generates a matrix mapping every requirement to its architecture,
    code, test, and result links. Access tier: public.

    Parameters:
        module_name (str): Module name (e.g. "Adc", "Can", "Spi"). Required.
            Run `list_available_modules` to get valid module names.
        output_format (str): Output format — "json" (default), "csv", or "html".
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "module": str, "format": str,
          "matrix": [{"requirement_id": str, "title": str,
                      "architecture": bool, "code": bool,
                      "test": bool, "result": bool}],
          "summary": {"total": int, "fully_covered": int, "gaps": int}
        }
    """
    denied = _authorize("build_traceability_matrix")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "hybrid":
            hybrid = graph_service
            if hybrid:
                cypher = """
                MATCH (req)
                WHERE req.module = $module AND (req.requirement_id IS NOT NULL OR req.jama_id IS NOT NULL)
                OPTIONAL MATCH (req)-[:TRACES_TO|IMPLEMENTS*1..3]-(code)
                OPTIONAL MATCH (code)<-[:VERIFIES]-(tc)
                RETURN coalesce(req.requirement_id, toString(req.jama_id), req.document_id) as requirement_id,
                       req.name as title,
                       count(DISTINCT code) > 0 as code,
                       count(DISTINCT tc) > 0 as test
                LIMIT 500
                """
                matrix = hybrid.deep_query(cypher, {"module": module_name}, workspace_id)
                if matrix:
                    covered = sum(1 for r in matrix if r.get("code") and r.get("test"))
                    return _ok({
                        "module": module_name,
                        "format": output_format,
                        "matrix": matrix,
                        "summary": {"total": len(matrix), "fully_covered": covered, "gaps": len(matrix) - covered},
                        "source": "hybrid",
                    })
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.build_traceability_matrix, module_name, output_format, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("find_coverage_gaps")
async def find_coverage_gaps(module_name: str, gap_type: str = "all", severity: str = "all",
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Find missing links in requirement-code-test traceability chains.

    Identifies requirements without code, tests without requirements,
    and other coverage gaps. Access tier: public.

    Parameters:
        module_name (str): Module name (e.g. "Adc", "Can"). Required.
            Run `list_available_modules` to get valid module names.
        gap_type (str): Type of gap to search for — "all" (default),
            "no_code", "no_test", "no_result", "no_requirement".
        severity (str): Filter by severity level — "all" (default),
            "critical", "major", "minor".
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "module": str, "gaps": [{
            "type": str, "severity": str, "node_id": str,
            "title": str, "missing": str
          }],
          "summary": {"total_gaps": int, "by_type": {...}, "by_severity": {...}}
        }
    """
    denied = _authorize("find_coverage_gaps")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "hybrid":
            hybrid = graph_service
            if hybrid:
                cypher = """
                MATCH (req)
                WHERE req.module = $module AND (req.requirement_id IS NOT NULL OR req.jama_id IS NOT NULL)
                OPTIONAL MATCH (req)-[:TRACES_TO|IMPLEMENTS*1..3]-(code)
                OPTIONAL MATCH (code)<-[:VERIFIES]-(tc)
                WITH req, count(DISTINCT code) AS c, count(DISTINCT tc) AS t
                WHERE c = 0 OR t = 0
                RETURN coalesce(req.requirement_id, toString(req.jama_id), req.document_id) as node_id,
                       req.name as title,
                       CASE WHEN c = 0 THEN 'no_code' ELSE 'no_test' END as type,
                       CASE WHEN c = 0 THEN 'critical' ELSE 'major' END as severity,
                       CASE WHEN c = 0 THEN 'code' ELSE 'test' END as missing
                LIMIT 500
                """
                gaps = hybrid.deep_query(cypher, {"module": module_name}, workspace_id)
                if gaps:
                    by_type = {}
                    by_sev = {}
                    for g in gaps:
                        by_type[g.get("type", "unknown")] = by_type.get(g.get("type", "unknown"), 0) + 1
                        by_sev[g.get("severity", "unknown")] = by_sev.get(g.get("severity", "unknown"), 0) + 1
                    return _ok({
                        "module": module_name,
                        "gaps": gaps,
                        "summary": {"total_gaps": len(gaps), "by_type": by_type, "by_severity": by_sev},
                        "source": "hybrid",
                    })
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.find_coverage_gaps, module_name, gap_type, severity, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("analyze_hw_sw_links")
async def analyze_hw_sw_links(
    module_name: str, include_undocumented: bool = True, include_peripheral_map: bool = False,
    workspace_id: str = "illd", session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None
) -> str:
    """Map hardware register usage to software functions and detect undocumented accesses.

    Walks EA_ACCESSES_REGISTER and EA_DEPENDS_ON relationships to build
    HW-SW mapping. Flags register accesses not linked to requirements.
    Access tier: public.

    Parameters:
        module_name (str): Module name (e.g. "Adc", "Spi"). Required.
            Run `list_available_modules` to get valid module names.
        include_undocumented (bool): Include register accesses that have no
            requirement traceability. Default True.
        include_peripheral_map (bool): Include a peripheral-to-function mapping.
            Default False.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "module": str,
          "hw_sw_links": [{"register": str, "function": str, "access_type": str,
                           "documented": bool}],
          "undocumented": [...] | None,
          "peripheral_map": {...} | None,
          "summary": {"total_accesses": int, "documented": int, "undocumented": int}
        }
    """
    denied = _authorize("analyze_hw_sw_links")
    if denied: return denied
    try:
        query_mode = query_mode
        if query_mode == "hybrid":
            hybrid = graph_service
            if hybrid:
                cypher = """
                MATCH (f)-[r:ACCESSES|ACCESSES_REGISTER]->(reg)
                WHERE coalesce(f.module, reg.module) = $module
                RETURN coalesce(reg.name, reg.register_name, reg.document_id) as register,
                       coalesce(f.name, f.function_name, f.document_id) as function,
                       type(r) as access_type,
                       true as documented
                LIMIT 500
                """
                links = hybrid.deep_query(cypher, {"module": module_name}, workspace_id)
                if links:
                    return _ok({
                        "module": module_name,
                        "hw_sw_links": links,
                        "undocumented": [] if include_undocumented else None,
                        "peripheral_map": {} if include_peripheral_map else None,
                        "summary": {"total_accesses": len(links), "documented": len(links), "undocumented": 0},
                        "source": "hybrid",
                    })
        ki = _get_ki_service(workspace_id)
        if not ki: return _err("INTERNAL_ERROR", "KnowledgeIntelligenceService unavailable")
        return _ok(await asyncio.to_thread(ki.analyze_hw_sw_links, module_name, include_undocumented, include_peripheral_map, ws=workspace_id))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 5 — INGESTION PIPELINE (4 tools) — Sprint 5
#  Plan 2: Removed from MCP tool registration. Use sandbox_upload instead.
#  Underlying IngestionService + parsers kept as library code.
# ═════════════════════════════════════════════════════════════════════════

# @mcp.tool()  # Removed: Plan 2 Phase 2 — prod ingestion via sandbox_upload only
async def _ingest_file(file_path: str, module_name: str, overwrite: bool = False, workspace_id: str = "illd") -> str:
    """Parse a single file and ingest into the knowledge graph.

    Supports C source/header files, requirement docs, SWA docs, and test
    files. Access tier: admin.

    Parameters:
        file_path (str): Absolute path to the file to ingest. Required.
        module_name (str): Target module name (e.g. "Adc", "Can"). Required.
            Run `list_available_modules` to see existing modules.
        overwrite (bool): Overwrite existing nodes if the file was previously
            ingested. Default False.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "ingested": bool, "file": str, "module": str,
          "nodes_created": int, "relationships_created": int
        }
    """
    denied = _authorize("ingest_file", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        svc = _get_ingestion_service()
        if not svc:
            return _err("INTERNAL_ERROR", "IngestionService not available")
        result = await asyncio.to_thread(
            svc.ingest_file, file_path, module_name, overwrite=overwrite, workspace_id=workspace_id)
        return _ok(result)
    except FileNotFoundError as e:
        return _err("INVALID_INPUT", str(e))
    except ValueError as e:
        return _err("INVALID_INPUT", str(e))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

# @mcp.tool()  # Removed: Plan 2 Phase 2
async def _ingest_module_from_repo(repo_root: str, module_name: str, workspace_id: str = "illd") -> str:
    """Ingest all artifacts for a module from a repository root directory.

    Scans the repo for source files, headers, docs, and test files
    belonging to the specified module. Access tier: admin.

    Parameters:
        repo_root (str): Absolute path to the repository root. Required.
        module_name (str): Module name to ingest (e.g. "Adc", "Can"). Required.
            Run `list_available_modules` to see existing modules.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "module": str, "repo_root": str,
          "files_processed": int, "nodes_created": int,
          "relationships_created": int, "errors": [...]
        }
    """
    denied = _authorize("ingest_module_from_repo", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        svc = _get_ingestion_service()
        if not svc:
            return _err("INTERNAL_ERROR", "IngestionService not available")
        result = await asyncio.to_thread(
            svc.ingest_module, repo_root, module_name, workspace_id=workspace_id)
        return _ok(result)
    except FileNotFoundError as e:
        return _err("INVALID_INPUT", str(e))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

# @mcp.tool()  # Removed: Plan 2 Phase 2
async def _batch_ingest_modules(
    lld_path: str, modules: Optional[List[str]] = None, parallel: bool = True,
    max_workers: int = 4, workspace_id: str = "illd",
) -> str:
    """Ingest multiple modules in one parallel operation.

    Processes all (or specified) modules from the LLD repo path
    concurrently. Access tier: admin.

    Parameters:
        lld_path (str): Absolute path to the LLD/MCAL repository root. Required.
        modules (list[str] | None): Specific module names to ingest
            (e.g. ["Adc", "Can", "Spi"]). None ingests all discovered modules.
            Run `list_available_modules` to get valid module names.
        parallel (bool): Run ingestion in parallel. Default True.
        max_workers (int): Max parallel ingestion workers. Default 4.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "modules_processed": int, "total_nodes": int,
          "total_relationships": int,
          "per_module": [{"module": str, "nodes": int, "relationships": int}],
          "errors": [...]
        }
    """
    denied = _authorize("batch_ingest_modules", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        svc = _get_ingestion_service()
        if not svc:
            return _err("INTERNAL_ERROR", "IngestionService not available")
        result = await asyncio.to_thread(
            svc.batch_ingest, lld_path, modules=modules, workspace_id=workspace_id)
        return _ok(result)
    except FileNotFoundError as e:
        return _err("INVALID_INPUT", str(e))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

# @mcp.tool()  # Removed: Plan 2 Phase 2
async def _ingest_repository(
    repo_path: str, modules: Optional[List[str]] = None,
    include_tests: bool = False, workspace_id: str = "illd",
) -> str:
    """Run repository-wide ingestion across all modules.

    Full repo scan: source, headers, requirements, architecture docs,
    SWA, and optionally test files. Access tier: admin.

    Parameters:
        repo_path (str): Absolute path to the repository. Required.
        modules (list[str] | None): Specific modules to ingest.
            None ingests all discovered modules.
            Run `list_available_modules` to get valid module names.
        include_tests (bool): Also ingest test files and create TestCase nodes.
            Default False.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "repo_path": str, "modules_processed": int,
          "total_nodes": int, "total_relationships": int,
          "test_nodes": int | None, "errors": [...]
        }
    """
    denied = _authorize("ingest_repository", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        svc = _get_ingestion_service()
        if not svc:
            return _err("INTERNAL_ERROR", "IngestionService not available")
        result = await asyncio.to_thread(
            svc.ingest_repository, repo_path, modules=modules,
            include_tests=include_tests, workspace_id=workspace_id)
        return _ok(result)
    except FileNotFoundError as e:
        return _err("INVALID_INPUT", str(e))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 6 — MEMORY & CONTEXT (5 core tools) — Sprint 2
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def session_start(
    session_id: str, assistant_name: Optional[str] = None,
    module_context: Optional[str] = None,
) -> str:
    """Start a working-memory session for context accumulation.

    Sessions persist key-value data and context across multiple tool calls.
    Convention: use "{ASSISTANT}_{timestamp}" format for session IDs.
    TTL is enforced server-side via SESSION_TTL_SECONDS env var (default: 3600s).
    Sessions automatically expire after this period.
    Access tier: public.

    Parameters:
        session_id (str): Unique session identifier. Required.
            Convention: "{assistant_name}_{unix_timestamp}".
        assistant_name (str | None): Name of the calling assistant/agent.
        module_context (str | None): Module scope for this session (e.g. "Adc").
            Run `list_available_modules` to get valid module names.

    Returns (JSON):
        {
          "session_id": str, "created": true,
          "store_type": "RedisBackend"|"InMemoryBackend",
          "ttl_seconds": 3600
        }
    """
    denied = _authorize("session_start")
    if denied:
        return denied
    try:
        mgr = _get_session_manager()
        if not mgr:
            return _err("INTERNAL_ERROR", "SessionManager not initialized")
        session = mgr.create(session_id=session_id, assistant_name=assistant_name or "",
                              module_context=module_context or "", ttl_seconds=_SESSION_TTL_SECONDS)
        ACTIVE_SESSIONS.inc()
        return _ok({"session_id": session.session_id, "created": True,
                     "store_type": type(mgr._backend).__name__,
                     "ttl_seconds": _SESSION_TTL_SECONDS})
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def session_store(session_id: str, key: str, value: Any) -> str:
    """Store a key-value pair in a session's working memory.

    Data persists until session_end is called or TTL expires.
    Access tier: public.

    Parameters:
        session_id (str): Active session ID. Required.
            Must be a session created via `session_start`.
        key (str): Storage key name. Required.
        value (Any): Value to store (string, number, dict, list, etc.).
            Must be JSON-serializable. Required.

    Returns (JSON):
        {"stored": true, "session_id": str, "key": str}
    """
    denied = _authorize("session_store")
    if denied:
        return denied
    try:
        mgr = _get_session_manager()
        if not mgr:
            return _err("INTERNAL_ERROR", "SessionManager not initialized")
        mgr.store(session_id, key, value)
        return _ok({"stored": True, "session_id": session_id, "key": key})
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def session_retrieve(session_id: str, key: str) -> str:
    """Retrieve a value from session working memory by key.

    Returns None if the key does not exist. Access tier: public.

    Parameters:
        session_id (str): Active session ID. Required.
            Must be a session created via `session_start`.
        key (str): Storage key to retrieve. Required.

    Returns (JSON):
        {"found": bool, "value": Any | null, "session_id": str, "key": str}
    """
    denied = _authorize("session_retrieve")
    if denied:
        return denied
    try:
        mgr = _get_session_manager()
        if not mgr:
            return _err("INTERNAL_ERROR", "SessionManager not initialized")
        val = mgr.retrieve(session_id, key)
        return _ok({"found": val is not None, "value": val, "session_id": session_id, "key": key})
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def build_context(
    session_id: str, rag_results: Optional[List[Dict]] = None,
    conversation_history: Optional[List[Dict]] = None,
    max_tokens: int = 8000, budget_unit: str = "tokens",
) -> str:
    """Assemble token-budget-aware context from RAG results and conversation history.

    Combines search results from `search_database` with conversation history
    and session context, respecting the token budget. Access tier: public.

    Parameters:
        session_id (str): Active session ID. Required.
            Must be a session created via `session_start`.
        rag_results (list[dict] | None): Search results from `search_database`
            tool (the "results" array from the response).
        conversation_history (list[dict] | None): Previous conversation turns,
            each as {"role": "user"|"assistant", "content": str}.
        max_tokens (int): Maximum context budget. Default 8000.
        budget_unit (str): Budget unit — "tokens" (default) or "characters".

    Returns (JSON):
        {
          "context": str, "total_tokens": int,
          "items_included": int, "items_dropped": int,
          "budget_remaining": int
        }
    """
    denied = _authorize("build_context")
    if denied:
        return denied
    try:
        builder = _get_context_builder(max_tokens=max_tokens, budget_unit=budget_unit)
        if not builder:
            return _err("INTERNAL_ERROR", "ContextBuilder not available")
        # Get session context if available
        session_ctx = None
        mgr = _get_session_manager()
        if mgr:
            s = mgr.get(session_id)
            if s:
                session_ctx = {"module": s.module_context, "assistant": s.assistant_name,
                               "workspace": s.workspace_id}
        result = builder.build(
            rag_results=rag_results,
            conversation_history=conversation_history,
            session_context=session_ctx,
        )
        # Store context in session for audit
        if mgr and mgr.get(session_id):
            mgr.store(session_id, "_last_context_tokens", result.get("total_tokens"))
            mgr.store(session_id, "_last_context_items", result.get("items_included"))
        return _ok(result)
    except Exception as exc:
        logger.exception("build_context failed")
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def session_end(session_id: str, persist_audit: bool = True) -> str:
    """Close a working-memory session and persist its audit trail.

    Frees all session data and optionally writes an audit record to
    PostgreSQL. Access tier: public.

    Parameters:
        session_id (str): Session ID to close. Required.
            Must be a session created via `session_start`.
        persist_audit (bool): Write session audit trail to PostgreSQL.
            Default True.

    Returns (JSON):
        {
          "closed": true,
          "stats": {"session_id": str, "total_store_keys": int,
                    "total_context_entries": int, ...}
        }
    """
    denied = _authorize("session_end")
    if denied:
        return denied
    try:
        mgr = _get_session_manager()
        if not mgr:
            return _err("INTERNAL_ERROR", "SessionManager not initialized")
        summary = mgr.close(session_id, persist_audit=persist_audit)
        ACTIVE_SESSIONS.dec()

        # Plan 2 Phase 3: cleanup sandbox and temp dirs on session end
        sm = _get_sandbox_manager()
        sandbox_stats = {}
        if sm:
            sandbox_stats = sm.destroy_sandbox(session_id)
        import shutil
        tmp_dir = Path(f"/tmp/sandbox_{session_id}")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return _ok({"closed": True, "stats": summary, "sandbox_cleanup": sandbox_stats})
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
# #  CATEGORY 6+ — EPHEMERAL SANDBOX (4 tools) — Sprint 3
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def sandbox_upload(
    session_id: str,
    documents: Optional[List[Dict[str, str]]] = None,
    file_paths: Optional[List[str]] = None,
    trace_depth: int = 1,
    module: Optional[str] = None,
    workspace_id: str = "illd",
    include_paths: Optional[List[str]] = None,
) -> str:
    """Upload documents into an ephemeral KG + vector store scoped to a session.

    Creates a temporary, isolated sandbox for analyzing documents that are
    not in the main knowledge graph. Automatically pulls ±N traceability
    neighbors from production Neo4j (controlled by trace_depth).
    Preferred: pass content directly via 'documents'. Access tier: public.

    Parameters:
        session_id (str): Active session ID. Required.
            Must be a session created via `session_start`.
        documents (list[dict] | None): List of document dicts with keys:
            - "filename" (str): File name (e.g. "my_module.c")
            - "content" (str): Full text content of the file.
            - "encoding" (str, optional): "utf-8" (default) or "base64".
            Preferred for server deployments.
        file_paths (list[str] | None): List of absolute file paths.
            Only works when files are local to the MCP server.
            Provide either documents or file_paths, not both.
        trace_depth (int): Pull ±N traceability layers from production Neo4j.
            0 = no pull (pure sandbox isolation), 1 = ±1 layer (default),
            2 = ±2 layers (deeper traceability). Default 1.
        module (str | None): Module name (e.g. "Adc", "Can"). Auto-detected
            from content if not set.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
        include_paths (list[str] | None): Additional -I include paths for
            clang C parsing. When provided, enables full SFR access and
            global variable detection.  Pass "auto" as the single element
            to auto-discover from pipeline temp data.

    Returns (JSON):
        {
          "session_id": str, "files_ingested": int, "nodes_created": int,
          "node_names_extracted": int,
          "prod_nodes_loaded": int, "prod_relationships_loaded": int,
          "sandbox_status": {"active": bool, "chunks": int, ...}
        }
    """
    denied = _authorize("sandbox_upload")
    if denied:
        return denied
    if not documents and not file_paths:
        return _err("INVALID_INPUT", "Provide either 'documents' (list of {filename, content}) or 'file_paths'.")
    if trace_depth < 0 or trace_depth > 2:
        return _err("INVALID_INPUT", "trace_depth must be 0, 1, or 2.")
    try:
        sm = _get_sandbox_manager()
        if not sm:
            return _err("INTERNAL_ERROR", "SandboxManager not available")
        mgr = _get_session_manager()
        if not mgr or not mgr.get(session_id):
            return _err("INVALID_INPUT", f"Session '{session_id}' not found. Call session_start first.")

        from src.MemoryLayer.memory.ephemeral_sandbox import (
            SandboxAdapter, SandboxParserDispatcher, TraceabilityPuller,
            EphemeralGraph,
        )
        import base64
        import shutil

        sandbox = sm.create_sandbox(session_id)

        # ── Resolve include paths (auto-discover or explicit) ──
        resolved_include_paths: List[str] = []
        skip_default_stubs = False
        if include_paths and len(include_paths) == 1 and include_paths[0].lower() == "auto":
            # Auto-discover from pipeline temp data once module is known
            # (deferred until after module detection below)
            _auto_discover_includes = True
        else:
            _auto_discover_includes = False
            if include_paths:
                resolved_include_paths = [p for p in include_paths if Path(p).is_dir()]
                skip_default_stubs = bool(resolved_include_paths)

        dispatcher = SandboxParserDispatcher(
            include_paths=resolved_include_paths,
            skip_default_stubs=skip_default_stubs,
            workspace_id=workspace_id,
        )
        adapter = SandboxAdapter(workspace_id=workspace_id)
        tmp_dir = Path(f"/tmp/sandbox_{session_id}")

        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            all_node_names = []
            parsed_files = []  # Store parsed results before ingesting

            # ── Plan 2 Phase 5 FIX: Parse all files FIRST (don't ingest yet) ──
            if documents:
                for doc in documents:
                    filename = doc.get("filename", "untitled.txt")
                    content = doc.get("content", "")
                    encoding = doc.get("encoding", "utf-8")

                    if not content:
                        continue

                    # MEG_SW-309: Block path traversal in filenames
                    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
                        logger.warning("sandbox_upload: path traversal blocked in filename %s", filename)
                        continue

                    # Base64 decode if needed
                    if encoding == "base64":
                        content_bytes = base64.b64decode(content)
                    else:
                        content_bytes = content.encode("utf-8")

                    # Size guard: 10 MB max per document
                    if len(content_bytes) > SandboxParserDispatcher.MAX_FILE_SIZE:
                        logger.warning("sandbox_upload: %s exceeds 10 MB, skipping", filename)
                        continue

                    # Materialize to tempfile for parser dispatch
                    tmp_path = tmp_dir / filename
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path.write_bytes(content_bytes)

                    # Parse with rich parser — store but DON'T ingest yet
                    parsed = await asyncio.to_thread(dispatcher.parse, tmp_path)
                    # Extract node names for traceability pull (without ingesting)
                    extracted_names = adapter._extract_node_names_from_parsed(parsed)
                    all_node_names.extend(extracted_names)
                    parsed_files.append((parsed, filename))

            elif file_paths:
                for fp in file_paths:
                    p = Path(fp).resolve()
                    # MEG_SW-328: Block path traversal — only allow files under
                    # known safe directories (tmp sandbox or repo root)
                    _allowed_roots = [tmp_dir.resolve()]
                    _repo_root = Path(__file__).resolve().parents[2]
                    _allowed_roots.append(_repo_root)
                    if not any(str(p).startswith(str(root)) for root in _allowed_roots):
                        logger.warning("sandbox_upload: path traversal blocked for %s", fp)
                        continue
                    if not p.exists():
                        logger.warning("sandbox_upload: %s not found, skipping", fp)
                        continue
                    parsed = await asyncio.to_thread(dispatcher.parse, p)
                    extracted_names = adapter._extract_node_names_from_parsed(parsed)
                    all_node_names.extend(extracted_names)
                    parsed_files.append((parsed, p.name))

            # ── Resolve module name (uppercase to match Neo4j convention) ──
            detected_module = module or _detect_module_from_names(all_node_names)

            # F-CB-10: refuse to pull traceability when module could not be
            # detected — otherwise the prod-overlay Cypher matches against
            # module="UNKNOWN" and silently returns zero nodes.
            if detected_module == "unknown" and trace_depth > 0:
                return _err(
                    "INVALID_INPUT",
                    "Could not detect module from uploaded files. "
                    "Provide module= explicitly (e.g., module='Can').",
                )

            # ── Auto-discover include paths if requested (MCAL only) ──
            if _auto_discover_includes and detected_module and workspace_id != "illd":
                resolved_include_paths = _auto_discover_module_include_paths(detected_module)
                if resolved_include_paths:
                    skip_default_stubs = True
                    # Re-create dispatcher with discovered paths
                    dispatcher = SandboxParserDispatcher(
                        include_paths=resolved_include_paths,
                        skip_default_stubs=True,
                        workspace_id=workspace_id,
                    )
                    # Re-parse files with proper include paths
                    all_node_names = []
                    parsed_files = []
                    if documents:
                        for doc in documents:
                            filename = doc.get("filename", "untitled.txt")
                            content = doc.get("content", "")
                            if not content:
                                continue
                            encoding = doc.get("encoding", "utf-8")
                            if encoding == "base64":
                                content_bytes = base64.b64decode(content)
                            else:
                                content_bytes = content.encode("utf-8")
                            tmp_path = tmp_dir / filename
                            tmp_path.parent.mkdir(parents=True, exist_ok=True)
                            tmp_path.write_bytes(content_bytes)
                            parsed = await asyncio.to_thread(dispatcher.parse, tmp_path)
                            extracted_names = adapter._extract_node_names_from_parsed(parsed)
                            all_node_names.extend(extracted_names)
                            parsed_files.append((parsed, filename))
                    elif file_paths:
                        for fp in file_paths:
                            p = Path(fp)
                            if not p.exists():
                                continue
                            parsed = await asyncio.to_thread(dispatcher.parse, p)
                            extracted_names = adapter._extract_node_names_from_parsed(parsed)
                            all_node_names.extend(extracted_names)
                            parsed_files.append((parsed, p.name))
                    logger.info("sandbox_upload: auto-discovered %d include paths for %s",
                                len(resolved_include_paths), detected_module)
            has_includes = bool(resolved_include_paths)

            # ── Plan 2 Phase 5 FIX: Load PRODUCTION nodes FIRST (before sandbox ingest) ──
            prod_stats = {"prod_nodes_loaded": 0, "prod_relationships_loaded": 0}
            if trace_depth > 0 and all_node_names:
                driver = _get_neo4j(workspace_id)
                if driver:
                    puller = TraceabilityPuller(driver)
                    nodes, rels = await asyncio.to_thread(
                        puller.pull_neighbors,
                        all_node_names, detected_module, workspace_id, trace_depth,
                    )
                    # Load prod nodes with _origin=production
                    sandbox.graph.load_prod_nodes(nodes, rels)
                    prod_stats = {"prod_nodes_loaded": len(nodes),
                                  "prod_relationships_loaded": len(rels)}
                else:
                    logger.warning("sandbox_upload: Neo4j driver unavailable for trace_depth=%d", trace_depth)

            # ── Plan 2 Phase 5 FIX: NOW ingest sandbox files (shadow detection works correctly) ──
            parsed_results = []
            parser_diagnostics = []
            for parsed, filename in parsed_files:
                node_names = adapter.ingest_parsed(sandbox, parsed, filename,
                                                   module=detected_module,
                                                   has_include_paths=has_includes)
                file_info = {"filename": filename, "nodes": len(node_names)}
                # Surface clang parser statistics (SFR, globals, calls)
                stats = parsed.get("statistics")
                if isinstance(stats, dict):
                    file_info["parser_stats"] = {
                        k: v for k, v in stats.items()
                        if k in ("total_functions", "total_sfr_accesses",
                                 "total_global_refs", "total_calls")
                    }
                # Surface critical clang diagnostics (errors only, capped)
                diags = parsed.get("diagnostics")
                if isinstance(diags, list):
                    errors = [d for d in diags
                              if isinstance(d, dict) and d.get("severity") == "error"]
                    if errors:
                        parser_diagnostics.append({
                            "filename": filename,
                            "error_count": len(errors),
                            "first_errors": [
                                d.get("message", "")[:120] for d in errors[:5]
                            ],
                        })
                parsed_results.append(file_info)

            # ── Phase 2: Boundary Resolution (cross-module Unknown → typed prod nodes) ──
            boundary_stats = {"boundary_candidates": 0, "boundary_resolved": 0}
            if trace_depth > 0:
                boundary_nodes = sandbox.graph.get_boundary_nodes()
                boundary_stats["boundary_candidates"] = len(boundary_nodes)
                if boundary_nodes:
                    boundary_names = [b["name"] for b in boundary_nodes]
                    driver = _get_neo4j(workspace_id)
                    if driver:
                        puller = TraceabilityPuller(driver)
                        resolved_nodes = await asyncio.to_thread(
                            puller.pull_boundary_nodes,
                            boundary_names, workspace_id,
                        )
                        if resolved_nodes:
                            sandbox.graph.load_prod_nodes(resolved_nodes, [])
                            name_to_canonical = {}
                            for node in resolved_nodes:
                                props = node["properties"]
                                name = props.get("name") or props.get("function_name") or ""
                                if name and name in boundary_names:
                                    canonical = EphemeralGraph._canonical_id(
                                        node["node_type"], props
                                    )
                                    name_to_canonical[name] = canonical
                            resolution = sandbox.graph.resolve_boundary(name_to_canonical)
                            boundary_stats["boundary_resolved"] = resolution.get("boundary_resolved", 0)

            response = {
                "session_id": session_id,
                "files_ingested": len(parsed_results),
                "nodes_created": sandbox.graph.node_count,
                "node_names_extracted": len(all_node_names),
                "include_paths_used": len(resolved_include_paths),
                **prod_stats,
                **boundary_stats,
                "parsed_files": parsed_results,
                "sandbox_status": sandbox.status(),
            }
            if parser_diagnostics:
                response["parser_diagnostics"] = parser_diagnostics
            return _ok(response)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as exc:
        logger.exception("sandbox_upload failed")
        return _err("INTERNAL_ERROR", str(exc))


def _auto_discover_module_include_paths(module: str) -> List[str]:
    """Auto-discover C include paths for a module from baked-in Docker headers
    or the local pipeline temporary_data directory.

    Search order (first existing base wins):
    1. ``INCLUDE_HEADERS_DIR`` env var  (default ``/app/include_headers``)
    2. Local pipeline ``temporary_data/`` relative to this source tree

    Within the chosen base directory the function searches for
    (mirrors production _build_sum_include_paths order):
    1. Module CfgMcal config headers  (``<mod>_cfgmcal/inc``)
    2. MemMap generated headers        (``<mod>_cfgmcal/MemMap_GenFiles``)
    3. SchM generated headers          (``<mod>_cfgmcal/SchM_GenFiles``)
    4. Shared dependency headers       (``rc1_deps/``)
    5. Platform headers                (``aurix3g_…_platform``)
    6. Cross-module source headers     (``ssc/inc`` from other ``*_src`` repos)
    7. SFR device headers              (a3g or rc1 layout)
    8. Module source API headers       (``ssc/{inc,src}``)

    The logic is project-agnostic — it checks both a3g and rc1 naming
    conventions and includes whichever directories exist.

    Returns a list of existing directory paths to pass as ``-I`` flags to clang.
    """
    # ── Resolve base directory ──
    docker_base = Path(os.environ.get("INCLUDE_HEADERS_DIR", "/app/include_headers"))
    local_base = Path(__file__).resolve().parents[2] / "src" / "HybridRAG" / "temp" / "temporary_data"

    base: Optional[Path] = None
    if docker_base.is_dir():
        base = docker_base
    elif local_base.is_dir():
        base = local_base

    if base is None:
        return []

    mod_lower = module.lower()
    include_paths: List[str] = []

    # Determine the module source directory name (for exclusion from cross-module scan)
    own_src_names = {
        f"aurix3g_sw_mcal_tc4xx_{mod_lower}_src",
        f"aurix_rc1_sw_mcal_dev_{mod_lower}",
    }

    # 1. CfgMcal headers: <mod>_cfgmcal/inc
    cfgmcal_base = base / f"{mod_lower}_cfgmcal"
    cfgmcal_inc = cfgmcal_base / "inc"
    if cfgmcal_inc.is_dir():
        include_paths.append(str(cfgmcal_inc))

    # 2-3. Sum config generated files (SchM + MemMap) — aggregated directory
    #      Contains all SchM_*.h and *_MemMap.h from all modules' ver repos.
    sum_gen = base / "sum_gen_files"
    if sum_gen.is_dir():
        include_paths.append(str(sum_gen))
    else:
        # Fallback: try module-specific cfgmcal directories (legacy layout)
        memmap = cfgmcal_base / "MemMap_GenFiles"
        if memmap.is_dir():
            include_paths.append(str(memmap))
        schm = cfgmcal_base / "SchM_GenFiles"
        if schm.is_dir():
            include_paths.append(str(schm))

    # 4. Shared dependency headers: rc1_deps
    deps = base / "rc1_deps"
    if deps.is_dir():
        include_paths.append(str(deps))

    # 5. Platform headers (Std_Types.h, Mcal_ErrorTypes.h, etc.)
    for platform_name in ("aurix3g_sw_mcal_tc4xx_platform",
                          "aurix_rc1_sw_mcal_platform"):
        platform_dir = base / platform_name
        if platform_dir.is_dir():
            include_paths.append(str(platform_dir))
            break

    # 6. Cross-module source headers (ssc/inc from other *_src repos)
    #    Ensures Dma.h, Gtm.h, McalLib.h, etc. resolve without stubs
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if not child.name.endswith("_src"):
                continue
            if child.name in own_src_names:
                continue
            ssc_inc = child / "ssc" / "inc"
            if ssc_inc.is_dir():
                include_paths.append(str(ssc_inc))

    # 7. SFR device headers — supports both a3g and rc1 layouts
    #    a3g:  aurix3g_sw_mcal_tc4xx_infra_sfr/<TC49xN>/  (headers directly in device dir)
    #    rc1:  aurix_rc1_sw_mcal_sfr/ssc/<RC1S16>/inc/    (headers under inc/)
    sfr_candidates = [
        ("aurix3g_sw_mcal_tc4xx_infra_sfr", None),       # a3g: device dirs at top level
        ("aurix_rc1_sw_mcal_sfr", Path("ssc")),           # rc1: device dirs under ssc/
    ]
    for sfr_dirname, sub_path in sfr_candidates:
        sfr_root = base / sfr_dirname
        if sub_path:
            sfr_root = sfr_root / sub_path
        if not sfr_root.is_dir():
            continue
        device_dirs = sorted([d for d in sfr_root.iterdir() if d.is_dir()])
        if not device_dirs:
            continue
        # Prefer TC49* device (a3g) or first available (rc1)
        chosen = device_dirs[0]
        for d in device_dirs:
            if "TC49" in d.name:
                chosen = d
                break
        # rc1 has an extra /inc level; a3g has headers directly in device dir
        inc_subdir = chosen / "inc"
        include_paths.append(str(inc_subdir) if inc_subdir.is_dir() else str(chosen))

    # 8. Module source headers — supports both a3g and rc1 naming
    #    a3g: aurix3g_sw_mcal_tc4xx_<mod>_src/ssc/{inc,src}
    #    rc1: aurix_rc1_sw_mcal_dev_<mod>/ssc/{inc,src}
    src_patterns = [
        f"aurix3g_sw_mcal_tc4xx_{mod_lower}_src",   # a3g
        f"aurix_rc1_sw_mcal_dev_{mod_lower}",        # rc1
    ]
    for src_pattern in src_patterns:
        src_base = base / src_pattern
        if not src_base.is_dir():
            continue
        ssc_inc = src_base / "ssc" / "inc"
        ssc_src = src_base / "ssc" / "src"
        if ssc_inc.is_dir():
            include_paths.append(str(ssc_inc))
        if ssc_src.is_dir():
            include_paths.append(str(ssc_src))

    logger.info("_auto_discover_module_include_paths(%s): %d paths found", module, len(include_paths))
    for p in include_paths:
        logger.info("  include: %s", p)

    return include_paths


def _detect_module_from_names(node_names: List[str]) -> str:
    """Heuristic: extract module name from function names.

    Handles both MCAL (``Adc_Init``) and iLLD (``IfxCan_init``,
    ``IfxCan_Node_init``) naming. F-CB-10: returns the *mode* (most common
    prefix) instead of the first one, and skips noise prefixes that come
    from std/utility headers (``Std_*``, bare ``Ifx_*``). Returns uppercase
    to match the Neo4j ``module`` property convention. Falls back to
    ``"unknown"`` when no usable prefix can be found.
    """
    prefixes: List[str] = []
    for name in node_names:
        if "_" not in name:
            continue
        prefix = name.split("_", 1)[0]
        # Strip iLLD "Ifx" prefix (IfxCan -> Can, IfxAdc -> Adc).
        if prefix.startswith("Ifx") and len(prefix) > 3:
            prefix = prefix[3:]
        # Skip too-short prefixes and known noise tokens.
        if len(prefix) < 2:
            continue
        if prefix in ("Std", "Ifx"):
            continue
        prefixes.append(prefix.upper())
    if not prefixes:
        return "unknown"
    return Counter(prefixes).most_common(1)[0][0]

# @mcp.tool()  # Deprecated: use search_database(session_id=...) instead (Plan 2 Phase 6)
async def _sandbox_query(session_id: str, query: str, top_k: int = 10) -> str:
    """Run a semantic search against the ephemeral sandbox stores.

    DEPRECATED — use search_database with session_id instead.
    Kept as internal function for backward compatibility.

    Parameters:
        session_id (str): Active session ID with an active sandbox. Required.
            Must have called `sandbox_upload` first.
        query (str): Natural language search query. Required.
        top_k (int): Maximum results to return. Default 10.

    Returns (JSON):
        {
          "results": [{"node_id": str, "content": str, "score": float,
                        "origin": str, "node_type": str}],
          "total_count": int
        }
    """
    denied = _authorize("sandbox_query")
    if denied:
        return denied
    try:
        sm = _get_sandbox_manager()
        if not sm:
            return _err("INTERNAL_ERROR", "SandboxManager not available")
        sandbox = sm.get_sandbox(session_id)
        if not sandbox:
            return _err("INVALID_INPUT", f"No active sandbox for session '{session_id}'. Call sandbox_upload first.")
        from src.MemoryLayer.memory.ephemeral_sandbox import SandboxQuerier
        querier = SandboxQuerier(sandbox)
        results = querier.search(query, top_k=top_k)
        return _ok({"results": [{"node_id": r.node_id, "content": r.content, "score": round(r.score, 4),
                                  "origin": r.origin, "node_type": r.node_type} for r in results],
                     "total_count": len(results)})
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def sandbox_status(session_id: str) -> str:
    """Inspect the current state of a session's ephemeral sandbox.

    Shows loaded files, chunk counts, and sandbox health. Access tier: public.

    Parameters:
        session_id (str): Session ID to check. Required.
            Must be a session created via `session_start`.

    Returns (JSON):
        {
          "active": bool, "files_loaded": [str],
          "total_chunks": int, "created_at": str
        }
    """
    denied = _authorize("sandbox_status")
    if denied:
        return denied
    try:
        sm = _get_sandbox_manager()
        if not sm:
            return _err("INTERNAL_ERROR", "SandboxManager not available")
        return _ok(sm.get_status(session_id))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def sandbox_clear(session_id: str) -> str:
    """Explicitly destroy a session's sandbox before TTL expiry.

    Frees all ephemeral KG and vector data. The sandbox cannot be
    queried after clearing. Access tier: public.

    Parameters:
        session_id (str): Session ID whose sandbox to destroy. Required.

    Returns (JSON):
        {"cleared": true, "session_id": str}
    """
    denied = _authorize("sandbox_clear")
    if denied:
        return denied
    try:
        sm = _get_sandbox_manager()
        if not sm:
            return _err("INTERNAL_ERROR", "SandboxManager not available")
        return _ok(sm.destroy_sandbox(session_id))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
async def sandbox_diff(session_id: str) -> str:
    """Show what changed in the sandbox vs production.

    Compares sandbox nodes against their production counterparts and
    reports nodes added, modified (with original properties), and
    unchanged production nodes. Access tier: public.

    Parameters:
        session_id (str): Active session ID with a sandbox. Required.

    Returns (JSON):
        {
          "nodes_added": [str],
          "nodes_modified": [{"node_id": str, "original": {...}, "current": {...}}],
          "nodes_unchanged": int,
          "edges_added": int,
          "edges_total": int
        }
    """
    denied = _authorize("sandbox_diff")
    if denied:
        return denied
    try:
        sm = _get_sandbox_manager()
        if not sm:
            return _err("INTERNAL_ERROR", "SandboxManager not available")
        sandbox = sm.get_sandbox(session_id)
        if not sandbox:
            return _err("INVALID_INPUT", f"No active sandbox for session '{session_id}'.")
        diff = {
            "nodes_added": [],
            "nodes_modified": [],
            "nodes_unchanged": 0,
            "edges_added": 0,
            "edges_total": sandbox.graph.edge_count,
        }
        for node_id, data in sandbox.graph.get_all_nodes():
            if data.get("_origin") == "sandbox":
                if data.get("_shadows"):
                    diff["nodes_modified"].append({
                        "node_id": node_id,
                        "original": data.get("_original_prod_properties", {}),
                        "current": {k: v for k, v in data.items() if not k.startswith("_")},
                    })
                else:
                    diff["nodes_added"].append(node_id)
            else:
                diff["nodes_unchanged"] += 1
        # Count sandbox-origin edges
        for u, v, edata in sandbox.graph._graph.edges(data=True):
            if edata.get("_origin") == "sandbox":
                diff["edges_added"] += 1
        return _ok(diff)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 5b — HSI (Hardware-Software Interface) — Sprint 8+
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_function_hsi(
    function_name: str,
    module: str = "Adc",
    profile: str = "mcal",
) -> str:
    """Extract the HSI (Hardware-Software Interface) section for a function.

    Returns structured SWUD-format data: SFR registers accessed (with access
    type, trust zone, line numbers), global/shared variables used (with access
    type, via_chain), and events. This is the dedicated HSI extraction tool
    — use it when you need the exact HSI constituents of a function.
    Access tier: public.

    Parameters:
        function_name (str): Exact function name (e.g. "Adc_Init", "Can_Write"). Required.
        module (str): Module name (e.g. "Adc", "Can", "Spi"). Default "Adc".
            Run `list_available_modules` to get valid module names.
        profile (str): Ontology profile — "mcal" (default) or "illd".

    Returns (JSON):
        {
          "function_name": str,
          "registers": [{"register": str, "access_type": str, "trust_zone": str, "line": int, ...}],
          "global_variables": [{"variable": str, "access_type": str, "data_type": str, "via_chain": str, ...}],
          "events": [],
          "summary_text": str  // Markdown-formatted SWUD HSI section
        }
    """
    denied = _authorize("get_function_hsi")
    if denied:
        return denied
    try:
        svc = _get_search_service(profile)
        if not svc or not svc.available:
            return _err("BACKEND_UNAVAILABLE", "Neo4j not connected")
        result = await asyncio.to_thread(
            svc.get_function_hsi,
            function_name=function_name,
            module=module,
            workspace_id=profile,
        )
        return _ok(result)
    except Exception as exc:
        logger.exception("get_function_hsi failed")
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 6+ — RLM (2 tools) — Sprint 5
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("rlm_orchestrate")
async def rlm_orchestrate(
    query: str, task_type: str = "generic", module: str = "CAN",
    session_id: Optional[str] = None, profile: str = "mcal",
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Multi-step context build via Recursive Language Model orchestration.

    Decomposes complex queries into up to 6 targeted sub-queries, each
    within an 8K token budget, executes them via hybrid search, then
    synthesizes a unified result. Access tier: public.

    Parameters:
        query (str): Complex natural language query to decompose. Required.
        task_type (str): Task classification — "generic" (default),
            "initialization", "debugging", "traceability", "dependency".
        module (str): Target module (e.g. "CAN", "Adc", "Spi"). Default "CAN".
            Run `list_available_modules` to get valid module names.
        session_id (str | None): Link to an active working-memory session
            for context continuity. Created via `session_start`.
        profile (str): Ontology profile — "mcal" (default) or "illd".
            Run `list_ontology_profiles` to see all profiles.

    Returns (JSON):
        {
          "answer": str, "confidence": float,
          "steps": [{"sub_query": str, "results_count": int, "tokens_used": int}],
          "total_sub_queries": int, "total_tokens": int
        }
    """
    denied = _authorize("rlm_orchestrate")
    if denied:
        return denied
    try:
        query_mode = query_mode
        # Note: RLM Orchestrator internally uses SearchService 
        # Any hybrid/sandbox routing is handled through the session context

        # Auto-detect HSI queries when task_type is generic
        if task_type == "generic":
            q_lower = query.lower()
            hsi_signals = sum([
                "hsi" in q_lower,
                "trust zone" in q_lower,
                "hardware-software interface" in q_lower,
                ("register" in q_lower and "global" in q_lower),
                ("sfr" in q_lower and "access" in q_lower),
                ("register" in q_lower and "access type" in q_lower),
                ("apu" in q_lower),
            ])
            if hsi_signals >= 1:
                task_type = "hsi_analysis"
                logger.info("[MCP] Auto-detected HSI query, routing to hsi_analysis")


        rlm = _get_rlm_orchestrator(module=module, profile=profile)
        if not rlm:
            return _err("INTERNAL_ERROR", "RLMOrchestrator not available")
        RLM_REQUESTS_TOTAL.labels(task_type=task_type).inc()
        # Pass session context and task type; RLM handles strategy selection internally
        result = await asyncio.to_thread(
            rlm.run, query=query, task_type=task_type, session_context=None
        )
        sub_q_count = len(result.steps) if hasattr(result, "steps") else 0
        RLM_SUBQUERIES.labels(task_type=task_type).observe(sub_q_count)
        result_dict = result.to_dict() if hasattr(result, "to_dict") else result
        if query_mode == "sandbox":
            result_dict["_origin"] = "hybrid"
        return _ok(result_dict)
    except Exception as exc:
        logger.exception("rlm_orchestrate failed")
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("rlm_plan_preview")
async def rlm_plan_preview(
    query: str, task_type: str = "generic", module: str = "CAN", profile: str = "mcal",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Preview the RLM query decomposition plan without executing sub-queries.

    Useful for understanding how a complex query will be broken down
    before committing to full execution. Access tier: public.

    Parameters:
        query (str): Complex query to plan. Required.
        task_type (str): Task classification — "generic" (default),
            "initialization", "debugging", "traceability", "dependency".
        module (str): Target module (e.g. "CAN", "Adc"). Default "CAN".
            Run `list_available_modules` to get valid module names.
        profile (str): Ontology profile — "mcal" (default) or "illd".
            Run `list_ontology_profiles` to see all profiles.

    Returns (JSON):
        {
          "plan": [{"step": int, "sub_query": str, "rationale": str}],
          "total_steps": int, "task_type": str
        }
    """
    denied = _authorize("rlm_plan_preview")
    if denied:
        return denied
    try:
        query_mode = query_mode
        # Note: RLM's plan_preview internally determines strategy
        # Session context and task type are sufficient for planning
        rlm = _get_rlm_orchestrator(module=module, profile=profile)
        if not rlm:
            return _err("INTERNAL_ERROR", "RLMOrchestrator not available")
        result = await asyncio.to_thread(
            rlm.plan_preview, query=query, task_type=task_type
        )
        result_dict = result if isinstance(result, dict) else (result.to_dict() if hasattr(result, "to_dict") else result)
        if query_mode == "sandbox":
            result_dict["_origin"] = "hybrid"
        return _ok(result_dict)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 7 — CACHE MANAGEMENT (4 tools) — Sprint 6
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("cache_get")
async def cache_get(query: str, session_id: Optional[str] = None) -> str:
    """Inspect cache entries for a specific query string.

    Checks LRU, semantic, and RAG cache tiers. Access tier: developer.

    Parameters:
        query (str): The query string to look up in the cache. Required.

    Returns (JSON):
        {
          "hit": bool, "cache_type": "lru"|"semantic"|"rag"|null,
          "result": {...} | null, "age_seconds": float | null
        }
    """
    denied = _authorize("cache_get")
    if denied: return denied
    try:
        cs = _get_cache_service()
        return _ok(cs.get(query)) if cs else _err("INTERNAL_ERROR", "CacheService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("cache_stats")
async def cache_stats(session_id: Optional[str] = None) -> str:
    """Retrieve cache performance metrics across all tiers.

    Shows hit/miss rates, entry counts, and memory usage per cache tier.
    Access tier: developer.

    Parameters: None.

    Returns (JSON):
        {
          "tiers": {
            "lru": {"entries": int, "hits": int, "misses": int, "hit_rate": float},
            "semantic": {...}, "rag": {...}
          },
          "total_hits": int, "total_misses": int
        }
    """
    denied = _authorize("cache_stats")
    if denied: return denied
    try:
        cs = _get_cache_service()
        return _ok(cs.stats()) if cs else _err("INTERNAL_ERROR", "CacheService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("cache_invalidate_module")
async def cache_invalidate_module(module_name: str, session_id: Optional[str] = None) -> str:
    """Invalidate all cache entries for a specific module.

    Use after ingesting new data for a module to ensure stale results
    are not returned. Access tier: admin.

    Parameters:
        module_name (str): Module name whose cache entries to invalidate
            (e.g. "Adc", "Can"). Required.
            Run `list_available_modules` to get valid module names.

    Returns (JSON):
        {"invalidated": true, "module": str, "entries_removed": int}
    """
    denied = _authorize("cache_invalidate_module")
    if denied: return denied
    try:
        cs = _get_cache_service()
        return _ok(cs.invalidate_module(module_name)) if cs else _err("INTERNAL_ERROR", "CacheService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("cache_clear")
async def cache_clear(tiers: Optional[List[str]] = None, session_id: Optional[str] = None) -> str:
    """Clear selected or all cache tiers.

    Use with caution — clears cached search results. Access tier: admin.

    Parameters:
        tiers (list[str] | None): Specific tiers to clear
            (e.g. ["lru"], ["semantic", "rag"]). Valid tier names: "lru",
            "semantic", "rag". None clears all tiers.

    Returns (JSON):
        {"cleared": true, "tiers": [str], "entries_removed": int}
    """
    denied = _authorize("cache_clear")
    if denied: return denied
    try:
        cs = _get_cache_service()
        return _ok(cs.clear(tiers)) if cs else _err("INTERNAL_ERROR", "CacheService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


@mcp.tool()
@with_session_routing("cache_refresh_config")
async def cache_refresh_config(session_id: Optional[str] = None) -> str:
    """Reload cache configuration from environment variables without restarting.

    Re-reads LRU_CACHE_SIZE, LRU_CACHE_TTL_HOURS, SEMANTIC_CACHE_MAX_SIZE,
    SEMANTIC_CACHE_THRESHOLD, and SEMANTIC_CACHE_TTL_DAYS. Updates parameters
    in-place — cached data is preserved (evicted only if size shrinks).
    Access tier: admin.

    Parameters: None.

    Returns (JSON):
        {
          "lru_max_size": {"old": int, "new": int},
          "lru_default_ttl": {"old": int, "new": int},
          "semantic_max_size": {"old": int, "new": int},
          "semantic_threshold": {"old": float, "new": float},
          "semantic_ttl_seconds": {"old": int, "new": int},
          "evicted": int
        }
    """
    denied = _authorize("cache_refresh_config")
    if denied: return denied
    try:
        cs = _get_cache_service()
        return _ok(cs.refresh_config()) if cs else _err("INTERNAL_ERROR", "CacheService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 8 — FEEDBACK & LEARNING (4 tools) — Sprint 4
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("submit_human_feedback")
async def submit_human_feedback(
    response_id: str, decision: str, reviewer_id: Optional[str] = None,
    issues_found: int = 0, correction_notes: Optional[str] = None,
    module: Optional[str] = None, task_type: Optional[str] = None,
    response_context: Optional[str] = None, workspace_id: str = "illd",
    session_id: Optional[str] = None,
) -> str:
    """Record a human review decision for a generated response.

    Feeds into the learning loop: patterns are stored in Neo4j (PatternStore)
    and Qdrant (PatternIndex) to improve future responses. Access tier: public.

    Parameters:
        response_id (str): Unique ID of the response being reviewed. Required.
            Obtained from `evaluate_confidence` output.
        decision (str): Review decision. Required.
            One of: "APPROVE", "APPROVE_WITH_EDITS", "REJECT", "ESCALATE".
        reviewer_id (str | None): Identifier of the human reviewer.
        issues_found (int): Number of issues found in the response. Default 0.
        correction_notes (str | None): Free-text notes about corrections made.
        module (str | None): Module context (e.g. "Adc").
            Run `list_available_modules` for valid module names.
        task_type (str | None): Task classification (e.g. "initialization",
            "debugging", "traceability").
        response_context (str | None): The original response text being reviewed.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "recorded": true, "response_id": str, "decision": str,
          "pattern_stored": bool, "learning_updated": bool
        }
    """
    denied = _authorize("submit_human_feedback")
    if denied:
        return denied
    try:
        if session_id:
            sm = _get_sandbox_manager()
            if sm and sm.get_sandbox(session_id):
                return _err(
                    "SANDBOX_WRITE_BLOCKED",
                    "Cannot write to production from an active sandbox session. "
                    "Use sandbox_upload for sandbox changes or end the sandbox session first.",
                )
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        result = await asyncio.to_thread(
            sink.submit_feedback,
            response_id=response_id, decision=decision,
            reviewer_id=reviewer_id, issues_found=issues_found,
            correction_notes=correction_notes,
            module=module, task_type=task_type,
            response_context=response_context,
            profile=workspace_id,
        )
        return _ok(result)
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("get_learning_metrics")
async def get_learning_metrics(include_pattern_details: bool = False, session_id: Optional[str] = None) -> str:
    """Retrieve learning-loop improvement metrics.

    Shows how feedback has influenced response quality and pattern
    detection over time. Access tier: developer.

    Parameters:
        include_pattern_details (bool): Include detailed breakdown of
            learned patterns by module and category. Default False.

    Returns (JSON):
        {
          "total_feedback": int, "approval_rate": float,
          "patterns_learned": int,
          "improvement_trend": float,
          "pattern_details": [...] | None
        }
    """
    denied = _authorize("get_learning_metrics")
    if denied:
        return denied
    try:
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        return _ok(await asyncio.to_thread(sink.get_learning_metrics, include_pattern_details=include_pattern_details))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("get_failure_patterns")
async def get_failure_patterns(module: Optional[str] = None, category: Optional[str] = None,
    session_id: Optional[str] = None) -> str:
    """Query learned failure patterns by module and/or category.

    Returns patterns extracted from REJECT and APPROVE_WITH_EDITS feedback
    to help avoid recurring issues. Access tier: developer.

    Parameters:
        module (str | None): Filter by module name (e.g. "Adc", "Can").
            Run `list_available_modules` for valid module names.
        category (str | None): Filter by failure category
            (e.g. "initialization", "dependency", "traceability").

    Returns (JSON):
        {
          "patterns": [{"pattern_id": str, "module": str,
                         "category": str, "description": str,
                         "frequency": int, "last_seen": str}]
        }
    """
    denied = _authorize("get_failure_patterns")
    if denied:
        return denied
    try:
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        return _ok({"patterns": await asyncio.to_thread(sink.get_failure_patterns, module=module, category=category)})
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

_result_processor = None

def _get_result_processor():
    global _result_processor
    if _result_processor is None:
        try:
            from src.ReviewGate.result_processors import ResultProcessor
            _result_processor = ResultProcessor(
                neo4j_driver=_get_neo4j(),
                feedback_sink=_get_feedback_sink(),
                postgres_client=_get_postgres_client(),
            )
            logger.info("[MCP] ResultProcessor initialized")
        except Exception as e:
            logger.warning("[MCP] ResultProcessor init failed: %s", e)
    return _result_processor

@mcp.tool()
@with_session_routing("process_results")
async def process_results(
    results_dir: str, module_name: Optional[str] = None, result_type: str = "vp",
    learn_from_failures: bool = True, update_graph: bool = True,
    workspace_id: str = "illd", session_id: Optional[str] = None,
) -> str:
    """Process test/analysis result files and ingest into the knowledge graph.

    Parses result files, creates TestResult nodes in Neo4j, links them to
    TestCase/Requirement nodes, and feeds failures into the learning loop.
    Access tier: admin.

    Parameters:
        results_dir (str): Path to result file(s) — single file or directory. Required.
        module_name (str | None): MCAL module name (e.g. "Adc", "Spi", "Can").
            Run `list_available_modules` to get valid module names.
        result_type (str): Result file format. Required.
            One of: "vp" (Verification Plan), "polyspace", "junit",
            "coverage", "compiler".
        learn_from_failures (bool): Record failures in FeedbackSink for the
            learning loop. Default True.
        update_graph (bool): Create/update TestResult nodes in Neo4j.
            Default True.
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "processed": true, "result_type": str, "module": str,
          "files_processed": int, "results_created": int,
          "failures_recorded": int, "graph_updated": bool
        }
    """
    denied = _authorize("process_results", workspace_id=workspace_id)
    if denied:
        return denied
    try:
        if session_id:
            sm = _get_sandbox_manager()
            if sm and sm.get_sandbox(session_id):
                return _err(
                    "SANDBOX_WRITE_BLOCKED",
                    "Cannot write processing results to production from an active sandbox session.",
                )
        processor = _get_result_processor()
        if not processor:
            return _err("INTERNAL_ERROR", "ResultProcessor not available")

        result = await asyncio.to_thread(
            processor.process,
            results_dir=results_dir,
            result_type=result_type,
            module_name=module_name,
            learn_from_failures=learn_from_failures,
            update_graph=update_graph,
            workspace_id=workspace_id,
        )
        return _ok(result)
    except FileNotFoundError as e:
        return _err("INVALID_INPUT", str(e))
    except Exception as exc:
        logger.exception("process_results failed")
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 9 — REVIEW GATE (4 tools) — Sprint 4
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("evaluate_confidence")
async def evaluate_confidence(signals: Dict[str, Any], response_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Compute a deterministic confidence score and route to review type.

    Scoring: AUTO (score >= 80), QUICK (50-79), FULL (< 50).
    Access tier: public.

    Parameters:
        signals (dict): Signal dict used for confidence calculation. Required.
            Expected keys include: "rag_score" (float), "sources_count" (int),
            "has_code" (bool), "has_traceability" (bool), "module" (str),
            "task_type" (str), etc.
        response_id (str | None): Optional response ID to track this evaluation.
            If not provided, one will be generated.

    Returns (JSON):
        {
          "response_id": str, "confidence_score": float,
          "review_type": "AUTO"|"QUICK"|"FULL",
          "signal_breakdown": {"rag_score": float, ...}
        }
    """
    denied = _authorize("evaluate_confidence")
    if denied:
        return denied
    try:
        calc = _get_confidence_calc()
        if not calc:
            return _err("INTERNAL_ERROR", "ConfidenceCalculator not available")
        result = calc.evaluate(signals=signals, response_id=response_id)
        route = result.get("review_type", "FULL") if isinstance(result, dict) else "FULL"
        REVIEW_ROUTING_TOTAL.labels(route=route).inc()
        return _ok(result)
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("complete_review")
async def complete_review(
    response_id: str, decision: str, reviewer_id: Optional[str] = None,
    issues_found: int = 0, rationale: Optional[str] = None, session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None
) -> str:
    """Mark a review as completed and close the review gate.

    Distinct from `submit_human_feedback` — this tool closes the review
    gate, while submit_human_feedback records feedback for learning.
    Access tier: public.

    Parameters:
        response_id (str): The response ID to close the review for. Required.
            Obtained from `evaluate_confidence` output.
        decision (str): Final review decision. Required.
            One of: "APPROVE", "APPROVE_WITH_EDITS", "REJECT", "ESCALATE".
        reviewer_id (str | None): Identifier of the reviewer.
        issues_found (int): Number of issues found. Default 0.
        rationale (str | None): Free-text explanation of the decision.

    Returns (JSON):
        {
          "completed": true, "response_id": str,
          "decision": str, "reviewer_id": str | null
        }
    """
    denied = _authorize("complete_review")
    if denied:
        return denied
    try:
        if session_id:
            sm = _get_sandbox_manager()
            if sm and sm.get_sandbox(session_id):
                return _err(
                    "SANDBOX_WRITE_BLOCKED",
                    "Cannot complete production review from an active sandbox session.",
                )
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        result = sink.complete_review(response_id=response_id, decision=decision,
                                       reviewer_id=reviewer_id, issues_found=issues_found,
                                       rationale=rationale)
        return _ok(result)
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("override_review_routing")
async def override_review_routing(response_id: str, new_review_type: str, reason: str,
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None) -> str:
    """Override the automatic review routing for a response.

    Used to escalate (e.g. AUTO → FULL) or downgrade review level.
    All overrides are logged for audit. Access tier: developer.

    Parameters:
        response_id (str): The response ID to override routing for. Required.
            Obtained from `evaluate_confidence` output.
        new_review_type (str): New review type. Required.
            One of: "AUTO", "QUICK", "FULL".
        reason (str): Mandatory justification for the override. Required.

    Returns (JSON):
        {
          "overridden": true, "response_id": str,
          "previous_type": str, "new_type": str,
          "reason": str, "logged": true
        }
    """
    denied = _authorize("override_review_routing")
    if denied:
        return denied
    try:
        if session_id:
            sm = _get_sandbox_manager()
            if sm and sm.get_sandbox(session_id):
                return _err(
                    "SANDBOX_WRITE_BLOCKED",
                    "Cannot override production review routing from an active sandbox session.",
                )
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        result = sink.override_routing(response_id=response_id, new_review_type=new_review_type, reason=reason)
        return _ok(result)
    except ValueError as ve:
        return _err("INVALID_INPUT", str(ve))
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))

@mcp.tool()
@with_session_routing("get_review_analytics")
async def get_review_analytics(session_id: Optional[str] = None) -> str:
    """Retrieve review-gate metrics, accuracy rates, and routing statistics.

    Shows how many reviews were AUTO/QUICK/FULL, approval rates, and
    whether automatic routing was accurate. Access tier: developer.

    Parameters: None.

    Returns (JSON):
        {
          "total_reviews": int,
          "by_type": {"AUTO": int, "QUICK": int, "FULL": int},
          "approval_rate": float, "override_count": int,
          "accuracy": float
        }
    """
    denied = _authorize("get_review_analytics")
    if denied:
        return denied
    try:
        sink = _get_feedback_sink()
        if not sink:
            return _err("INTERNAL_ERROR", "FeedbackSink not available")
        return _ok(sink.get_review_analytics())
    except Exception as exc:
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 10 — ONTOLOGY & CONFIG (4 tools) — Sprint 6
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("list_ontology_profiles")
async def list_ontology_profiles(session_id: Optional[str] = None) -> str:
    """List all available ontology profiles.

    Profiles define the node types, relationship types, and validation
    rules for a specific workspace. Access tier: public.

    Parameters: None.

    Returns (JSON):
        {"profiles": ["illd", "mcal", ...]}
    """
    denied = _authorize("list_ontology_profiles")
    if denied: return denied
    try:
        os_svc = _get_ontology_service("illd")
        return _ok({"profiles": os_svc.list_profiles()}) if os_svc else _err("INTERNAL_ERROR", "OntologyService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("get_ontology_schema")
async def get_ontology_schema(
    workspace_id: str = "illd", include_live_stats: bool = False, node_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Return the ontology schema with node labels, relationship types, and property definitions.

    Use this tool to discover valid node labels and relationship types for use
    in search_nodes, get_neighbors, execute_cypher, and other tools.
    Access tier: public.

    Parameters:
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
            Run `list_ontology_profiles` to see all profiles.
        include_live_stats (bool): Include live node/relationship counts from
            Neo4j for each type. Default False.
        node_type (str | None): Filter to a specific node type
            (e.g. "APIFunction", "EA_Function").
            When None, returns the complete schema.

    Returns (JSON):
        {
          "profile": str,
          "node_types": [{"label": str, "properties": [...],
                          "count": int | null}],
          "relationship_types": [{"type": str, "from": str,
                                   "to": str, "count": int | null}]
        }
    """
    denied = _authorize("get_ontology_schema")
    if denied: return denied
    try:
        os_svc = _get_ontology_service(workspace_id)
        if not os_svc: return _err("INTERNAL_ERROR", "OntologyService unavailable")
        return _ok(os_svc.get_schema(workspace_id, include_live_stats, node_type))
    except ValueError as ve: return _err("INVALID_INPUT", str(ve))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("validate_entity")
async def validate_entity(entity_type: str, data: Dict[str, Any], context: str = "illd",
    session_id: Optional[str] = None,
) -> str:
    """Validate an entity's data against ontology rules.

    Checks required properties, valid values, and relationship constraints
    defined in the ontology profile. Access tier: developer.

    Parameters:
        entity_type (str): Node label to validate against. Required.
            illd labels: APIFunction, DataStructure, SoftwareRequirement,
            TestCase, Module, Register, etc.
            mcal labels: StakeholderRequirement, ProductRequirement,
            EA_Function, MCALModule, etc.
            Run `get_ontology_schema` to get the full list.
        data (dict): Entity property data to validate. Required.
            Keys should match the expected properties for the entity_type.
        context (str): Ontology profile — "illd" (default) or "mcal".

    Returns (JSON):
        {
          "valid": bool, "entity_type": str,
          "errors": [{"field": str, "message": str}],
          "warnings": [{"field": str, "message": str}]
        }
    """
    denied = _authorize("validate_entity")
    if denied: return denied
    try:
        os_svc = _get_ontology_service(context)
        return _ok(os_svc.validate_entity(entity_type, data, context)) if os_svc else _err("INTERNAL_ERROR", "OntologyService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("get_ontology_compliance")
async def get_ontology_compliance(module_name: str, ontology_profile: str = "illd",
    session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Compute module-level ontology compliance score and list issues.

    Checks that all nodes and relationships in a module conform to the
    ontology schema. Access tier: developer.

    Parameters:
        module_name (str): Module name to check (e.g. "Adc", "Can"). Required.
            Run `list_available_modules` to get valid module names.
        ontology_profile (str): Ontology profile — "illd" (default) or "mcal".
            Run `list_ontology_profiles` to see all profiles.

    Returns (JSON):
        {
          "module": str, "profile": str,
          "compliance_score": float,
          "issues": [{"node_id": str, "type": str, "message": str}],
          "total_nodes_checked": int
        }
    """
    denied = _authorize("get_ontology_compliance")
    if denied: return denied
    try:
        query_mode = query_mode
        # Deep query: needs full module consistency check against prod
        os_svc = _get_ontology_service(ontology_profile)
        if not os_svc:
            return _err("INTERNAL_ERROR", "OntologyService unavailable")
        
        result = os_svc.get_compliance(module_name, ontology_profile)
        if query_mode == "sandbox":
            result["_origin"] = "production"
            result["_note"] = "Compliance checked against production DB"
        return _ok(result)
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 11 — OBSERVABILITY (remaining 5 tools) — Sprint 6
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("get_graph_statistics")
async def get_graph_statistics(workspace_id: str = "illd", session_id: Optional[str] = None) -> str:
    """Get graph-wide statistics: counts by node type and relationship type.

    Use this for a high-level overview of what's in the knowledge graph.
    Access tier: public.

    Parameters:
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
            Run `list_ontology_profiles` to see all profiles.

    Returns (JSON):
        {
          "total_nodes": int, "total_relationships": int,
          "by_label": {"APIFunction": int, "DataStructure": int, ...},
          "by_relationship": {"DEPENDS_ON": int, "IMPLEMENTS": int, ...}
        }
    """
    denied = _authorize("get_graph_statistics")
    if denied: return denied
    try:
        obs = _get_observability_service(workspace_id)
        return _ok(await asyncio.to_thread(obs.get_graph_statistics, workspace_id)) if obs else _err("INTERNAL_ERROR", "ObservabilityService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("list_available_modules")
async def list_available_modules(include_stats: bool = False,
    workspace_id: str = "illd", session_id: Optional[str] = None) -> str:
    """List all available modules for the given workspace.

    - **ILLD workspace**: queries Qdrant and returns all single-word
      collection names (no underscores) — these are the ILLD modules.
    - **MCAL workspace**: queries the MCAL Neo4j instance and returns
      distinct module names from the knowledge graph.

    No pagination — always returns the complete list.
    Use this tool to discover valid module names for other tools
    that require a module_name parameter. Access tier: public.

    Parameters:
        include_stats (bool): Include per-module statistics
            (vector point counts for ILLD, node counts for MCAL).
            Default False.
        workspace_id (str): Target workspace — "illd" or "mcal".
            Default "illd".

    Returns (JSON):
        {
          "modules": [{"module": str, "source": str, ...}],
          "total_count": int,
          "workspace": str
        }
    """
    denied = _authorize("list_available_modules")
    if denied: return denied
    try:
        if workspace_id == "illd":
            return await _list_illd_modules(include_stats)
        elif workspace_id == "mcal":
            return await _list_mcal_modules(include_stats)
        else:
            return _err("INVALID_INPUT", f"Unknown workspace_id '{workspace_id}'. Use 'illd' or 'mcal'.")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


async def _list_illd_modules(include_stats: bool) -> str:
    """ILLD modules: all single-word (no underscore) Qdrant collections."""
    modules: list = []
    try:
        qc = _get_qdrant()
        if not qc:
            return _err("SERVICE_UNAVAILABLE", "Qdrant client is not available.")
        cols = await asyncio.to_thread(qc.get_collections)
        for col in cols.collections:
            col_name = col.name
            # ILLD collections are single-word, no underscores
            if "_" in col_name:
                continue
            entry: dict = {"module": col_name.upper(), "source": "qdrant",
                           "collection": col_name}
            if include_stats:
                try:
                    ci = await asyncio.to_thread(qc.get_collection, col_name)
                    entry["vector_points"] = getattr(ci, "points_count", None)
                except Exception:
                    entry["vector_points"] = None
            modules.append(entry)
    except Exception as exc:
        logger.warning("list_available_modules(illd): Qdrant query failed: %s", exc)
        return _err("INTERNAL_ERROR", f"Qdrant query failed: {exc}")

    modules.sort(key=lambda m: m["module"])
    return _ok({"modules": modules, "total_count": len(modules),
                "workspace": "illd"})


async def _list_mcal_modules(include_stats: bool) -> str:
    """MCAL modules: EA_Function modules that have matching SRC_Function or SRC_Macro."""
    modules: list = []
    try:
        driver = _get_neo4j("mcal")
        if not driver:
            return _err("SERVICE_UNAVAILABLE", "Neo4j MCAL driver is not available.")

        from src.HybridRAG.code.neo4j_manager import get_instance_config
        db_name = get_instance_config("mcal").database

        def _query():
            with driver.session(database=db_name) as s:
                result = s.run(
                    "MATCH (ea:EA_Function) "
                    "WHERE ea.module IS NOT NULL AND ea.project IS NOT NULL "
                    "WITH DISTINCT ea.project AS project, ea.module AS module "
                    "OPTIONAL MATCH (src:SRC_Function {module: module, project: project}) "
                    "WITH project, module, count(src) > 0 AS has_src_func "
                    "OPTIONAL MATCH (srcm:SRC_Macro {module: module, project: project}) "
                    "WITH project, module, has_src_func, count(srcm) > 0 AS has_src_macro "
                    "WHERE has_src_func OR has_src_macro "
                    "RETURN project, module "
                    "ORDER BY project, module"
                )
                return [{"project": r["project"], "module": r["module"]} for r in result]

        rows = await asyncio.to_thread(_query)
        for r in rows:
            modules.append({"module": r["module"], "project": r["project"], "source": "neo4j"})
    except Exception as exc:
        logger.warning("list_available_modules(mcal): Neo4j query failed: %s", exc)
        return _err("INTERNAL_ERROR", f"Neo4j MCAL query failed: {exc}")

    return _ok({"modules": modules, "total_count": len(modules),
                "workspace": "mcal"})

@mcp.tool()
@with_session_routing("get_distribution")
async def get_distribution(dimension: str, workspace_id: str = "illd", label: Optional[str] = None,
    session_id: Optional[str] = None) -> str:
    """Get parametric distribution of nodes across a dimension.

    Useful for understanding the composition of the knowledge graph
    by status, safety level, or domain. Access tier: public.

    Parameters:
        dimension (str): Distribution dimension to query. Required.
            One of: "status" (Draft/Approved/Rejected/Deferred/Obsolete),
            "asil" (QM/ASIL_A/ASIL_B/ASIL_D), or
            "domain" (General/Safety/Cybersecurity/AUTOSAR/Platform/Tool/etc.).
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
        label (str | None): Optional node label filter. Only count nodes
            of this label type.
            Run `get_ontology_schema` for valid node labels.

    Returns (JSON):
        {
          "dimension": str, "workspace_id": str,
          "distribution": {"<value>": int, ...},
          "total": int
        }
    """
    denied = _authorize("get_distribution")
    if denied: return denied
    try:
        obs = _get_observability_service(workspace_id)
        if not obs: return _err("INTERNAL_ERROR", "ObservabilityService unavailable")
        return _ok(await asyncio.to_thread(obs.get_distribution, dimension, workspace_id, label))
    except ValueError as ve: return _err("INVALID_INPUT", str(ve))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("get_coverage_report")
async def get_coverage_report(workspace_id: str = "illd", session_id: Optional[str] = None) -> str:
    """Generate an aggregate traceability coverage report.

    Shows percentage of requirements with architecture, code, test,
    and result traceability links. Access tier: public.

    Parameters:
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".

    Returns (JSON):
        {
          "workspace_id": str,
          "coverage": {
            "req_to_arch": float, "req_to_code": float,
            "req_to_test": float, "req_to_result": float,
            "overall": float
          },
          "total_requirements": int
        }
    """
    denied = _authorize("get_coverage_report")
    if denied: return denied
    try:
        obs = _get_observability_service(workspace_id)
        return _ok(await asyncio.to_thread(obs.get_coverage_report, workspace_id)) if obs else _err("INTERNAL_ERROR", "ObservabilityService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("detect_communities")
async def detect_communities(
    workspace_id: str = "illd", node_types: Optional[List[str]] = None,
    min_community_size: int = 3, store_in_graph: bool = False, session_id: Optional[str] = None,
    query_mode: Optional[str] = None,
    graph_service: Optional[Any] = None,
    sandbox_ctx: Optional[Any] = None,
) -> str:
    """Run graph community detection algorithms on the knowledge graph.

    Identifies clusters of closely related nodes using graph algorithms.
    Useful for discovering module boundaries or related requirements.
    Access tier: developer.

    Parameters:
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
        node_types (list[str] | None): Restrict to specific node labels.
            illd labels: APIFunction, DataStructure, SoftwareRequirement, etc.
            mcal labels: StakeholderRequirement, EA_Function, MCALModule, etc.
            Run `get_ontology_schema` to get valid node labels.
            None includes all node types.
        min_community_size (int): Minimum nodes per community. Default 3.
        store_in_graph (bool): Persist community assignments as node properties
            in Neo4j. Default False.

    Returns (JSON):
        {
          "communities": [{"id": int, "size": int,
                            "members": [{"node_id": str, "label": str}]}],
          "total_communities": int, "stored": bool
        }
    """
    denied = _authorize("detect_communities")
    if denied: return denied
    try:
        obs = _get_observability_service(workspace_id)
        return _ok(obs.detect_communities(workspace_id, node_types, min_community_size, store_in_graph)) if obs else _err("INTERNAL_ERROR", "ObservabilityService unavailable")
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 12 — VISUALIZATION (1 tool) — Sprint 6
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("visualize_subgraph")
async def visualize_subgraph(
    workspace_id: str = "illd", seed_nodes: Optional[List[Dict]] = None,
    filters: Optional[Dict] = None, max_nodes: int = 200, output_format: str = "html",
    session_id: Optional[str] = None,
) -> str:
    """Render a subgraph as an interactive pyvis HTML visualization.

    Starts from seed nodes and expands outward, or applies filters to
    select a subgraph. Access tier: developer.

    Parameters:
        workspace_id (str): Target workspace — "illd" or "mcal". Default "illd".
        seed_nodes (list[dict] | None): Starting nodes for expansion. Each dict:
            {"document_id": str} or {"jama_id": int}.
            Obtain IDs from `search_database`, `search_nodes`, or `get_node_by_id`.
        filters (dict | None): Filter criteria, e.g.
            {"label": "APIFunction", "module": "Adc"}.
            Run `get_ontology_schema` for valid labels.
            Run `list_available_modules` for valid module names.
        max_nodes (int): Maximum nodes to include in the visualization.
            Default 200.
        output_format (str): Output format — "html" (default interactive pyvis).

    Returns (JSON):
        {
          "html": "<interactive HTML content>",
          "nodes_count": int, "edges_count": int
        }
    """
    denied = _authorize("visualize_subgraph")
    if denied: return denied
    try:
        from src.Configuration.services import VisualizationService
        viz = VisualizationService(neo4j_driver=_get_neo4j(workspace_id))
        return _ok(viz.visualize_subgraph(workspace_id, seed_nodes, filters, max_nodes, output_format))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 13 — AUTHENTICATION (2 tools) — Sprint 6
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("get_token_info")
async def get_token_info(token: str, session_id: Optional[str] = None) -> str:
    """Inspect JWT token timing information.

    Decodes a JWT to show issued-at, expiration, remaining lifetime,
    and whether it's expired. Use "current" or "auto" to inspect the
    cached GPT4IFX token. Access tier: developer.

    Parameters:
        token (str): JWT token string, or "current"/"auto" to inspect
            the currently cached GPT4IFX token. Required.
            If using "current", call `ensure_valid_token` first if no token is cached.

    Returns (JSON):
        {
          "iat": str, "exp": str, "remaining": str,
          "expired": bool, "subject": str | null
        }
    """
    denied = _authorize("get_token_info")
    if denied: return denied
    try:
        # Resolve "current"/"auto" to the actual cached JWT
        if token.lower() in ("current", "auto"):
            from src.HybridRAG.code.token_manager import get_token
            token = get_token()
            if not token:
                return _err("INVALID_INPUT", "No cached token available. Call ensure_valid_token first.")
        from src.Configuration.services import AuthService
        return _ok(AuthService.get_token_info(token))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))

@mcp.tool()
@with_session_routing("ensure_valid_token")
async def ensure_valid_token(force_refresh: bool = False, session_id: Optional[str] = None) -> str:
    """Refresh or validate the GPT4IFX JWT token.

    Reads credentials from environment variables (GPT4IFX_USERNAME,
    GPT4IFX_PASSWORD). Never pass credentials directly. Access tier: admin.

    Parameters:
        force_refresh (bool): Force a new token even if the current one
            is still valid. Default False.

    Returns (JSON):
        {
          "valid": true, "refreshed": bool,
          "expires_at": str, "remaining_seconds": int
        }
    """
    denied = _authorize("ensure_valid_token")
    if denied: return denied
    try:
        from src.Configuration.services import AuthService
        return _ok(AuthService.ensure_valid_token(force_refresh))
    except Exception as e: return _err("INTERNAL_ERROR", str(e))


# ═════════════════════════════════════════════════════════════════════════
#  CATEGORY 14 — GAP v2 TOOLS
# ═════════════════════════════════════════════════════════════════════════

@mcp.tool()
@with_session_routing("query_enhance")
async def query_enhance(query: str, include_synonyms: bool = False, session_id: Optional[str] = None) -> str:
    """Classify query complexity and predict optimal search strategy.

    Exposes the QueryEnhancer preprocessing stage for upstream analysis.
    Returns complexity classification, recommended search strategy, detected
    entities/modules, and token budget hints — all rule-based with zero LLM
    dependency and sub-millisecond latency. Access tier: developer.

    Parameters:
        query (str): Natural language query to analyze. Required.
        include_synonyms (bool): Include expanded domain synonyms in the
            response. Default False.

    Returns (JSON):
        {
          "original_query": str,
          "enhanced_query": str,
          "complexity": "SIMPLE" | "MEDIUM" | "COMPLEX",
          "strategy": "GRAPH_HEAVY" | "VECTOR_HEAVY" | "HYBRID" | "EXACT",
          "suggested_alpha": float,
          "suggested_max_results": int,
          "detected_entities": [str],
          "detected_modules": [str],
          "is_aggregation": bool,
          "token_budget_hint": int
        }
    """
    denied = _authorize("query_enhance")
    if denied:
        return denied
    try:
        from src.HybridRAG.code.querier.query_enhancer import QueryEnhancer

        enhancer = QueryEnhancer()
        result = enhancer.enhance(query)
        output = result.as_dict()
        if not include_synonyms:
            output.pop("synonyms_added", None)
        return _ok(output)
    except Exception as exc:
        logger.exception("query_enhance failed")
        return _err("INTERNAL_ERROR", str(exc))


# ═════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═════════════════════════════════════════════════════════════════════════

# Validate that every @mcp.tool() has a TOOL_TIERS entry (fail-fast)
from .tool_tiers import validate_tool_registration
validate_tool_registration(mcp)

class _APIKeyMiddleware:
    """ASGI middleware that reads ``Authorization: Bearer <key>`` from
    incoming HTTP requests and stores it in the ``_current_api_key``
    context variable so ``_authorize()`` can use it per-request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))
            auth = (headers.get(b"authorization", b"")).decode("utf-8", errors="ignore")
            api_key = auth.removeprefix("Bearer ").strip() if auth else ""

            # Extract or generate X-Request-ID for distributed tracing
            request_id = (headers.get(b"x-request-id", b"")).decode("utf-8", errors="ignore")
            if not request_id:
                request_id = str(_uuid.uuid4())[:8]

            token_key = _current_api_key.set(api_key)
            token_rid = _current_request_id.set(request_id)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_api_key.reset(token_key)
                _current_request_id.reset(token_rid)
        else:
            await self.app(scope, receive, send)


def _build_asgi_app(transport: str):
    """Build the ASGI app stack with Swagger UI, optional /metrics, and MCP."""
    from contextlib import asynccontextmanager
    from starlette.routing import Mount, Route
    from starlette.applications import Starlette
    from starlette.responses import Response

    from .swagger_ui import swagger_routes

    if transport == "streamable-http":
        inner = mcp.streamable_http_app()
    else:
        inner = mcp.sse_app()

    # Collect tool functions for OpenAPI introspection
    _g = globals()
    tool_functions = {name: _g[name] for name in TOOL_TIERS if name in _g and callable(_g[name])}

    # ── Swagger UI + OpenAPI spec at /, /docs, /openapi.json ──
    routes: list = swagger_routes(tool_functions)

    # Mount Prometheus /metrics alongside the MCP app
    if PROMETHEUS_AVAILABLE:
        async def _metrics_handler(request):
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            from src.Observability.metrics import REGISTRY
            body = generate_latest(REGISTRY)
            return Response(content=body, media_type=CONTENT_TYPE_LATEST)

        routes.append(Route("/metrics", _metrics_handler))
        logger.info("[Metrics] /metrics endpoint enabled for Prometheus scraping")

    # MCP protocol lives under /mcp (or /sse)
    routes.append(Mount("/", app=inner))

    # Propagate the inner MCP app's lifespan (initializes the
    # StreamableHTTPSessionManager task group) to the outer app.
    _inner_lifespan = inner.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(app):
        async with _inner_lifespan(app):
            yield

    app = Starlette(routes=routes, lifespan=_lifespan)
    logger.info("[Website] Landing page at /")
    logger.info("[Swagger] API docs available at /docs")

    return _APIKeyMiddleware(app)


def main() -> None:
    """Entry point — run the FastMCP server.

    Transport is controlled by MCP_TRANSPORT env var:
      - stdio (default for local MCP clients)
      - sse
      - streamable-http (recommended for Kubernetes)
    """
    import asyncio

    logger.info("AI Core Engine MCP Server starting...")
    logger.info("Tools registered: %d", len(TOOL_TIERS))
    logger.info("Auth enabled: %s", os.environ.get("CERBOS_ENABLED", "true"))
    logger.info("Prometheus metrics: %s", "enabled" if PROMETHEUS_AVAILABLE else "disabled")

    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    if transport in ("streamable-http", "sse"):
        import uvicorn

        app = _build_asgi_app(transport)

        async def _serve_with_warmup():
            # Warm up heavy resources before accepting requests
            await _warmup()
            config = uvicorn.Config(
                app,
                host=mcp.settings.host,
                port=mcp.settings.port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(_serve_with_warmup())
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()


"""
Prometheus Metrics — Sprint 10
===============================
Centralised metrics registry for the AI Core Engine MCP Server.

Exposes Counters, Histograms, and Gauges that are incremented/observed
by the MCP tool layer, search pipeline, cache, and session subsystems.

Usage:
    from src.Observability.metrics import TOOL_REQUEST_DURATION, TOOL_REQUESTS_TOTAL

    with TOOL_REQUEST_DURATION.labels(tool="search_database").time():
        result = await do_work()

    TOOL_REQUESTS_TOTAL.labels(tool="search_database", status="ok").inc()
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── Feature flag ───────────────────────────────────────────────────────
# Set ENABLE_METRICS=true to activate Prometheus instrumentation.
# Disabled by default — enable when running with the monitoring profile.
ENABLE_METRICS = os.environ.get("ENABLE_METRICS", "false").lower() in ("true", "1", "yes")

if ENABLE_METRICS:
    try:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            make_asgi_app,
        )

        REGISTRY = CollectorRegistry()

        # ── Tool invocation metrics ────────────────────────────────────────
        # BREAKING CHANGE (Sprint 27): Added "tier" label (was ["da_name", "tool", "status"]).
        # Pre-existing Grafana dashboards or PromQL queries referencing this metric
        # WITHOUT the "tier" label will stop matching after upgrade. Update dashboards
        # to include tier=~".*" or explicit tier values at deployment time.
        TOOL_REQUESTS_TOTAL = Counter(
            "aice_tool_requests_total",
            "Total MCP tool invocations",
            ["da_name", "tier", "tool", "status"],
            registry=REGISTRY,
        )

        TOOL_REQUEST_DURATION = Histogram(
            "aice_tool_request_duration_seconds",
            "MCP tool invocation latency in seconds",
            ["da_name", "tool"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=REGISTRY,
        )

        # ── Search pipeline metrics ────────────────────────────────────────
        SEARCH_REQUESTS_TOTAL = Counter(
            "aice_search_requests_total",
            "Total hybrid search requests",
            ["workspace"],
            registry=REGISTRY,
        )

        SEARCH_DURATION = Histogram(
            "aice_search_duration_seconds",
            "Hybrid search latency (vector + graph + merge)",
            ["stage"],  # "vector", "graph", "merge", "total"
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=REGISTRY,
        )

        # ── Cache metrics ──────────────────────────────────────────────────
        CACHE_REQUESTS_TOTAL = Counter(
            "aice_cache_requests_total",
            "Cache lookup attempts",
            ["cache_type", "result"],  # cache_type: "lru"|"semantic", result: "hit"|"miss"
            registry=REGISTRY,
        )

        # ── Session metrics ────────────────────────────────────────────────
        ACTIVE_SESSIONS = Gauge(
            "aice_active_sessions",
            "Number of active sessions",
            registry=REGISTRY,
        )

        # ── RLM metrics ────────────────────────────────────────────────────
        RLM_REQUESTS_TOTAL = Counter(
            "aice_rlm_requests_total",
            "Total RLM orchestration requests",
            ["task_type"],
            registry=REGISTRY,
        )

        RLM_SUBQUERIES = Histogram(
            "aice_rlm_subquery_count",
            "Number of sub-queries generated per RLM request",
            ["task_type"],
            buckets=(1, 2, 3, 4, 5, 6),
            registry=REGISTRY,
        )

        # F-CC-R01: planner LLM fallbacks (all retries exhausted, sentinel used).
        RLM_PLANNER_FALLBACKS = Counter(
            "aice_rlm_planner_fallbacks_total",
            "RLM planner LLM calls that exhausted retries and fell back",
            ["reason"],  # "exhausted"|"auth"
            registry=REGISTRY,
        )

        # F-CA-A04: Cerbos PDP availability (1 = reachable, 0 = unreachable).
        CERBOS_UP = Gauge(
            "aice_cerbos_up",
            "Cerbos PDP availability (1 = up, 0 = down/fallback)",
            registry=REGISTRY,
        )

        # ── Ingestion metrics ──────────────────────────────────────────────
        INGESTION_FILES_TOTAL = Counter(
            "aice_ingestion_files_total",
            "Files ingested",
            ["parser_type", "status"],  # status: "ok"|"error"
            registry=REGISTRY,
        )

        # ── Backend health ─────────────────────────────────────────────────
        BACKEND_UP = Gauge(
            "aice_backend_up",
            "Backend availability (1 = up, 0 = down)",
            ["backend"],  # "neo4j", "qdrant", "redis", "postgres"
            registry=REGISTRY,
        )

        # ── Confidence / Review Gate ───────────────────────────────────────
        REVIEW_ROUTING_TOTAL = Counter(
            "aice_review_routing_total",
            "Confidence-based review routing decisions",
            ["route"],  # "AUTO", "QUICK", "FULL"
            registry=REGISTRY,
        )

        # ── Query metrics (Ticket 7) ──────────────────────────────────────
        QUERY_TOTAL = Counter(
            "aice_query_total",
            "Total queries by type",
            ["query_type"],  # "hybrid", "vector", "graph", "pattern"
            registry=REGISTRY,
        )

        QUERY_LATENCY = Histogram(
            "aice_query_latency_seconds",
            "Per-backend query latency breakdown",
            ["backend"],  # "vector", "graph", "merge", "total"
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=REGISTRY,
        )

        # ── Cache gauges (Ticket 7) ────────────────────────────────────────
        CACHE_HIT_RATE = Gauge(
            "aice_cache_hit_rate",
            "Rolling cache hit ratio (0.0–1.0)",
            ["cache_type"],  # "lru", "semantic", "combined"
            registry=REGISTRY,
        )

        CACHE_SIZE = Gauge(
            "aice_cache_size",
            "Current number of entries in cache",
            ["cache_type"],  # "lru", "semantic"
            registry=REGISTRY,
        )

        # ── Error metrics (Ticket 8) ───────────────────────────────────────
        ERROR_TOTAL = Counter(
            "aice_error_total",
            "Total errors by type and component",
            ["error_type", "component"],
            # error_type: "timeout", "connection", "auth", "validation", "internal"
            # component:  "neo4j", "qdrant", "redis", "llm", "cache", "search", "mcp"
            registry=REGISTRY,
        )

        # ── Ingestion duration (Ticket 9 dashboard support) ────────────────
        INGESTION_DURATION = Histogram(
            "aice_ingestion_duration_seconds",
            "Total ingestion pipeline run duration",
            ["module"],
            buckets=(10, 30, 60, 120, 300, 600, 1200, 1800, 3600),
            registry=REGISTRY,
        )
        # ── Per-DA productivity metrics (F-P5-M01, Pass 5 §4.2) ──────────
        # Label `da_name` is used (not the plan's `da`) for consistency with the
        # existing per-DA tool metrics and the `$da_name` Grafana variable.
        DA_SESSION_DURATION = Histogram(
            "aice_da_session_duration_seconds",
            "DA working-session duration (session_start → session_end)",
            ["da_name", "task_type"],
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=REGISTRY,
        )
        DA_SESSION_OUTCOMES = Counter(
            "aice_da_session_outcomes_total",
            "Human-review outcomes per DA",
            ["da_name", "task_type", "outcome"],  # outcome: APPROVE|REJECT|...
            registry=REGISTRY,
        )
        DA_CONTEXT_TOKENS = Histogram(
            "aice_da_context_assembly_tokens",
            "Tokens assembled into context per DA",
            ["da_name", "task_type"],
            buckets=(256, 512, 1024, 2048, 4096, 8192, 16384, 32768),
            registry=REGISTRY,
        )
        DA_FIRST_RESULT_LATENCY = Histogram(
            "aice_da_first_result_latency_seconds",
            "Latency from session_start to first evaluate_confidence per DA",
            ["da_name", "task_type"],
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
            registry=REGISTRY,
        )
        DA_PATTERN_HITS = Counter(
            "aice_da_pattern_hits_total",
            "Learned patterns surfaced (reused) per DA",
            ["da_name", "task_type"],
            registry=REGISTRY,
        )
        DA_LLM_TOKENS = Counter(
            "aice_da_session_llm_tokens_total",
            "LLM tokens consumed per DA",
            ["da_name", "task_type", "llm_call_type"],
            registry=REGISTRY,
        )
        PROMETHEUS_AVAILABLE = True
        logger.info("[Metrics] prometheus_client loaded — metrics enabled")

    except ImportError:
        ENABLE_METRICS = False
        PROMETHEUS_AVAILABLE = False
        logger.warning("[Metrics] ENABLE_METRICS=true but prometheus_client not installed — metrics disabled")

if not ENABLE_METRICS or not globals().get("PROMETHEUS_AVAILABLE", False):
    # Metrics are disabled via flag OR prometheus_client is not installed.
    PROMETHEUS_AVAILABLE = False
    if not ENABLE_METRICS:
        logger.info("[Metrics] ENABLE_METRICS is not set — metrics disabled (set ENABLE_METRICS=true to enable)")

    class _NoOp:
        """No-op stub that silently ignores all method calls."""
        def __getattr__(self, name):
            return self
        def __call__(self, *a, **kw):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    _noop = _NoOp()
    REGISTRY = None
    TOOL_REQUESTS_TOTAL = _noop
    TOOL_REQUEST_DURATION = _noop
    SEARCH_REQUESTS_TOTAL = _noop
    SEARCH_DURATION = _noop
    CACHE_REQUESTS_TOTAL = _noop
    ACTIVE_SESSIONS = _noop
    RLM_REQUESTS_TOTAL = _noop
    RLM_SUBQUERIES = _noop
    RLM_PLANNER_FALLBACKS = _noop
    CERBOS_UP = _noop
    INGESTION_FILES_TOTAL = _noop
    BACKEND_UP = _noop
    REVIEW_ROUTING_TOTAL = _noop
    QUERY_TOTAL = _noop
    QUERY_LATENCY = _noop
    CACHE_HIT_RATE = _noop
    CACHE_SIZE = _noop
    ERROR_TOTAL = _noop
    INGESTION_DURATION = _noop
    DA_SESSION_DURATION = _noop
    DA_SESSION_OUTCOMES = _noop
    DA_CONTEXT_TOKENS = _noop
    DA_FIRST_RESULT_LATENCY = _noop
    DA_PATTERN_HITS = _noop
    DA_LLM_TOKENS = _noop


def make_metrics_app():
    """Create an ASGI app that serves ``/metrics`` for Prometheus scraping.

    Returns ``None`` if prometheus_client is not available.
    """
    if not PROMETHEUS_AVAILABLE:
        return None
    return make_asgi_app(registry=REGISTRY)

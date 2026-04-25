"""
OpenTelemetry Tracing — GAP-A12 (ADR-036 revised)
====================================================
MCP tool-layer tracing only. Exports to Grafana Tempo via OTLP.

Usage:
    from src.Observability.otel_tracing import trace_tool

    @trace_tool("search_database")
    def search_database(...):
        ...

Env vars:
    ENABLE_OTEL=true           — master switch (default: false)
    OTEL_EXPORTER_OTLP_ENDPOINT — e.g. http://tempo:4317
    OTEL_SERVICE_NAME          — default: aice-mcp-server
"""
from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ENABLE_OTEL = os.environ.get("ENABLE_OTEL", "false").lower() in ("true", "1", "yes")
_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "aice-mcp-server")

# ── Lazy initialisation ───────────────────────────────────────────────
_tracer = None
_init_attempted = False


def _get_tracer():
    """Lazy-load the OpenTelemetry tracer. Returns None if unavailable."""
    global _tracer, _init_attempted
    if _tracer is not None:
        return _tracer
    if _init_attempted:
        return None
    _init_attempted = True

    if not ENABLE_OTEL:
        logger.debug("OpenTelemetry disabled (ENABLE_OTEL != true)")
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": _SERVICE_NAME})
        provider = TracerProvider(resource=resource)

        # Try OTLP gRPC exporter first, then console fallback
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info("OTLP exporter → %s", endpoint)
            except ImportError:
                logger.warning(
                    "opentelemetry-exporter-otlp not installed; tracing to logs only"
                )
        else:
            logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; tracing to logs only")

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("aice.mcp", "1.0.0")
        logger.info("OpenTelemetry tracer initialised: service=%s", _SERVICE_NAME)
        return _tracer

    except ImportError:
        logger.info(
            "opentelemetry SDK not installed — tracing disabled. "
            "Install with: pip install opentelemetry-api opentelemetry-sdk"
        )
        return None
    except Exception as exc:
        logger.warning("OpenTelemetry init failed: %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────


def trace_tool(tool_name: str, attributes: Optional[dict] = None):
    """Decorator: wrap an MCP tool function with an OTel span.

    If OTel is unavailable the original function runs unmodified — zero overhead.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _get_tracer()
            if tracer is None:
                return fn(*args, **kwargs)

            span_attrs = {"mcp.tool": tool_name}
            if attributes:
                span_attrs.update(attributes)

            with tracer.start_as_current_span(
                f"mcp.tool.{tool_name}", attributes=span_attrs
            ) as span:
                try:
                    result = fn(*args, **kwargs)
                    span.set_attribute("mcp.status", "ok")
                    return result
                except Exception as exc:
                    span.set_attribute("mcp.status", "error")
                    span.set_attribute("mcp.error", str(exc)[:200])
                    span.record_exception(exc)
                    raise

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _get_tracer()
            if tracer is None:
                return await fn(*args, **kwargs)

            span_attrs = {"mcp.tool": tool_name}
            if attributes:
                span_attrs.update(attributes)

            with tracer.start_as_current_span(
                f"mcp.tool.{tool_name}", attributes=span_attrs
            ) as span:
                try:
                    result = await fn(*args, **kwargs)
                    span.set_attribute("mcp.status", "ok")
                    return result
                except Exception as exc:
                    span.set_attribute("mcp.status", "error")
                    span.set_attribute("mcp.error", str(exc)[:200])
                    span.record_exception(exc)
                    raise

        import asyncio
        import inspect
        if inspect.iscoroutinefunction(fn):
            return async_wrapper
        return wrapper

    return decorator


def current_span():
    """Return the current active span (or a no-op span if OTel unavailable)."""
    try:
        from opentelemetry import trace
        return trace.get_current_span()
    except ImportError:
        return None

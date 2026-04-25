"""
MCP Streaming Transport — GAP-A02 (Research Upgrade v2)
=========================================================
**Research Upgrade:** Replaced custom SSE wrapper with official MCP Python SDK
StreamableHTTPServerTransport (modelcontextprotocol/python-sdk).

Reference implementation: invariantlabs-ai/mcp-streamable-http

Architecture:
  FastMCP server → StreamableHTTPServerTransport → async notifications
  - DAs opt-in via stream=true parameter
  - Progress streamed via server.request_context.session.send_notification()
  - Backward-compatible: non-streaming DAs get standard JSON-RPC response

Key difference from v1 (custom SSE):
  - v1 used custom StreamEvent/SSE format that no MCP client understood
  - v2 uses official SDK notifications — auto-compatible with VS Code,
    Claude Desktop, and any MCP SDK client
  - Session resumption and bidirectional communication supported natively

Design principles:
  - Official SDK transport (not custom protocol)
  - Metrics (TTFT, completion rate) wired into SDK hooks
  - Graceful degradation: if SDK unavailable, falls back to synchronous
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Stream Metrics (preserved from v1 — wired into SDK hooks)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class StreamMetrics:
    """Metrics for a single streaming response."""
    stream_id: str = ""
    tool_name: str = ""
    started_at: float = 0.0
    first_event_at: float = 0.0
    completed_at: float = 0.0
    events_sent: int = 0
    errors: int = 0
    completed: bool = False

    @property
    def time_to_first_token_ms(self) -> float:
        if self.first_event_at and self.started_at:
            return (self.first_event_at - self.started_at) * 1000
        return 0.0

    @property
    def total_duration_ms(self) -> float:
        end = self.completed_at or time.time()
        return (end - self.started_at) * 1000 if self.started_at else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "tool_name": self.tool_name,
            "time_to_first_token_ms": round(self.time_to_first_token_ms, 2),
            "total_duration_ms": round(self.total_duration_ms, 2),
            "events_sent": self.events_sent,
            "errors": self.errors,
            "completed": self.completed,
        }


# ═══════════════════════════════════════════════════════════════════════
#  MCP SDK Notification Helper
# ═══════════════════════════════════════════════════════════════════════

class MCPStreamNotifier:
    """
    Sends progress notifications via the official MCP SDK session.

    Usage in tool handlers:
      notifier = MCPStreamNotifier(server)
      await notifier.send_progress("search", "Graph search complete", 0.3)
      await notifier.send_progress("search", "Reranking...", 0.6)
      await notifier.send_result(final_result)

    The MCP SDK handles serialization, transport, and delivery to the client.
    No custom SSE format needed — clients receive standard MCP notifications.
    """

    def __init__(self, server=None):
        """
        Parameters
        ----------
        server : FastMCP server instance
            Used to access request_context.session for sending notifications.
            If None, notifications are logged but not sent (graceful degradation).
        """
        self._server = server
        self._metrics: Dict[str, StreamMetrics] = {}

    async def send_progress(
        self,
        stream_id: str,
        message: str,
        progress: float = 0.0,
        tool_name: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a progress notification to the MCP client.

        Parameters
        ----------
        stream_id : str
            Unique stream identifier (e.g., response_id).
        message : str
            Human-readable progress message.
        progress : float
            Progress fraction (0.0 to 1.0).
        tool_name : str
            Name of the tool (for metrics).
        data : dict, optional
            Additional structured data to include.

        Returns
        -------
        bool : True if notification was sent successfully.
        """
        # Track metrics
        if stream_id not in self._metrics:
            self._metrics[stream_id] = StreamMetrics(
                stream_id=stream_id,
                tool_name=tool_name,
                started_at=time.time(),
            )

        metrics = self._metrics[stream_id]
        if metrics.first_event_at == 0.0:
            metrics.first_event_at = time.time()
        metrics.events_sent += 1

        notification_data = {
            "stream_id": stream_id,
            "message": message,
            "progress": progress,
            "tool": tool_name,
        }
        if data:
            notification_data["data"] = data

        # Send via MCP SDK session
        try:
            if self._server is not None:
                ctx = self._server.request_context
                if ctx and hasattr(ctx, 'session') and ctx.session:
                    await ctx.session.send_notification(
                        method="notifications/progress",
                        params=notification_data,
                    )
                    return True
                else:
                    logger.debug("No active MCP session — progress logged only: %s", message)
            else:
                logger.debug("Stream [%s]: %s (%.0f%%)", stream_id, message, progress * 100)
            return False

        except Exception as exc:
            logger.warning("Failed to send progress notification: %s", exc)
            metrics.errors += 1
            return False

    async def send_partial_result(
        self,
        stream_id: str,
        result: Dict[str, Any],
    ) -> bool:
        """Send a partial result notification."""
        return await self.send_progress(
            stream_id=stream_id,
            message="partial_result",
            data=result,
        )

    def complete(self, stream_id: str) -> Optional[StreamMetrics]:
        """Mark a stream as complete and return its metrics."""
        metrics = self._metrics.pop(stream_id, None)
        if metrics:
            metrics.completed = True
            metrics.completed_at = time.time()
            logger.info(
                "Stream complete: %s — %d events, %.0f ms TTFT, %.0f ms total",
                stream_id, metrics.events_sent,
                metrics.time_to_first_token_ms, metrics.total_duration_ms,
            )
        return metrics

    def get_active_streams(self) -> List[Dict[str, Any]]:
        """Return metrics for currently active streams."""
        return [m.as_dict() for m in self._metrics.values()]


# ═══════════════════════════════════════════════════════════════════════
#  Streaming Tool Handlers
# ═══════════════════════════════════════════════════════════════════════

async def stream_hybrid_search(
    notifier: MCPStreamNotifier,
    search_service,
    query: str,
    stream_id: str,
    max_results: int = 10,
    include_relationships: bool = False,
    filter_by_module: Optional[str] = None,
    workspace_id: str = "illd",
    alpha: float = 0.6,
    enhancer=None,
    reranker=None,
) -> Dict[str, Any]:
    """
    Streaming-aware hybrid search that sends progress notifications.

    Unlike v1 which returned an async generator of custom SSE events,
    this version executes the full pipeline and sends MCP-standard
    progress notifications at each stage. Returns the final result dict
    (same format as non-streaming hybrid_search).
    """
    # Stage 1: Query enhancement
    if enhancer:
        enhanced = enhancer.enhance(query)
        await notifier.send_progress(stream_id, "Query enhanced", 0.1,
            tool_name="search_database",
            data={"complexity": enhanced.complexity.value,
                  "strategy": enhanced.strategy.value})
        effective_query = enhanced.enhanced_query
        effective_alpha = enhanced.suggested_alpha if alpha == 0.6 else alpha
    else:
        effective_query = query
        effective_alpha = alpha

    # Stage 2: Execute hybrid search
    await notifier.send_progress(stream_id, "Searching knowledge graph and vector store",
                                 0.3, tool_name="search_database")

    results = await asyncio.to_thread(
        search_service.hybrid_search,
        effective_query,
        max_results=max(max_results * 3, 50),
        include_relationships=include_relationships,
        filter_by_module=filter_by_module,
        workspace_id=workspace_id,
        alpha=effective_alpha,
    )

    await notifier.send_progress(stream_id, "Search complete", 0.6,
        tool_name="search_database",
        data={"results_count": results.get("total_count", 0)})

    # Stage 3: Reranking
    search_results = results.get("results", [])
    if reranker and reranker.available:
        await notifier.send_progress(stream_id, "Reranking results", 0.7,
                                     tool_name="search_database")
        rerank_result = reranker.rerank(
            query=query, results=search_results, top_k=max_results,
            search_strategy=results.get("search_strategy"),
        )
        search_results = rerank_result.results
        await notifier.send_progress(stream_id, "Reranking complete", 0.85,
            data={"reranked": rerank_result.reranked,
                  "count": rerank_result.reranked_count})
    else:
        search_results = search_results[:max_results]

    await notifier.send_progress(stream_id, "Results ready", 1.0,
                                 tool_name="search_database")
    notifier.complete(stream_id)

    results["results"] = search_results
    results["total_count"] = len(search_results)
    return results


async def stream_rlm_orchestrate(
    notifier: MCPStreamNotifier,
    rlm_orchestrator,
    query: str,
    task_type: str,
    stream_id: str,
    module: Optional[str] = None,
    workspace_id: str = "illd",
) -> Any:
    """
    Streaming-aware RLM orchestration.

    Uses RLMOrchestrator.run(on_progress=callback) and bridges
    the sync callback to async MCP notifications.
    """
    await notifier.send_progress(stream_id, "Planning sub-queries", 0.05,
                                 tool_name="rlm_orchestrate")

    # Capture the running loop before entering the worker thread
    _loop = asyncio.get_running_loop()

    def on_progress(step: int, total: int, msg: str) -> None:
        """Sync callback from RLM — schedule async notification on the main loop."""
        progress = step / max(total, 1)
        try:
            asyncio.run_coroutine_threadsafe(
                notifier.send_progress(stream_id, msg, progress,
                                       tool_name="rlm_orchestrate",
                                       data={"step": step, "total": total}),
                _loop,
            )
        except RuntimeError:
            pass  # Event loop closed — skip notification

    result = await asyncio.to_thread(
        rlm_orchestrator.run,
        query,
        task_type=task_type,
        on_progress=on_progress,
    )

    await notifier.send_progress(stream_id, "Orchestration complete", 1.0,
                                 tool_name="rlm_orchestrate")
    notifier.complete(stream_id)
    return result


# ═══════════════════════════════════════════════════════════════════════
#  Transport Configuration Helper
# ═══════════════════════════════════════════════════════════════════════

def configure_streamable_http_transport(app, mcp_server):
    """
    Configure the official MCP SDK StreamableHTTPServerTransport on a FastAPI app.

    Call this in mcp_server.py after creating the FastAPI app:

        from mcp.core.streaming import configure_streamable_http_transport
        configure_streamable_http_transport(app, mcp)

    This registers the /mcp endpoint for Streamable HTTP transport,
    which handles:
      - HTTP POST for JSON-RPC requests (standard tool calls)
      - HTTP GET with Accept: text/event-stream for SSE (streaming)
      - Session management (session-id header)
      - Bidirectional notifications

    Reference: invariantlabs-ai/mcp-streamable-http
    """
    try:
        from mcp.server.streamable_http import StreamableHTTPServerTransport
        from starlette.routing import Route

        transport = StreamableHTTPServerTransport(
            mcp_server=mcp_server,
            # Session timeout: 30 minutes (for long RLM orchestrations)
            session_timeout=1800,
        )

        # Register transport routes on the FastAPI app
        app.routes.append(
            Route("/mcp", endpoint=transport.handle_request, methods=["GET", "POST"])
        )

        logger.info("StreamableHTTP transport configured on /mcp")
        return transport

    except ImportError:
        logger.warning(
            "MCP SDK StreamableHTTPServerTransport not available. "
            "Install with: pip install mcp[server] "
            "Falling back to standard HTTP JSON-RPC."
        )
        return None
    except Exception as exc:
        logger.error("Failed to configure StreamableHTTP transport: %s", exc)
        return None

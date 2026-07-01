"""
MCP Test Client — Streamable HTTP transport for E2E testing.

Connects to a deployed AICE MCP server via the JSON-RPC 2.0 / MCP protocol
over HTTP POST (streamable-http transport).

Environment variables:
    MCP_TEST_URL     — Base URL of the MCP server (default: http://test-mcp:8000)
    MCP_TEST_API_KEY — Bearer API key for authentication
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import httpx


class MCPTestClient:
    """Async MCP client that communicates over streamable-http (POST /mcp)."""

    _MAX_RETRIES = 5
    _RETRY_BACKOFF = 2.0  # seconds, multiplied by attempt number

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get(
            "MCP_TEST_URL", "http://test-mcp:8000"
        )).rstrip("/")
        self.api_key = api_key or os.environ.get("MCP_TEST_API_KEY", "")
        self.timeout = timeout
        self._request_id = 0
        self._session_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MCPTestClient":
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            verify=False,  # test env may use self-signed certs
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a JSON-RPC 2.0 request and return the parsed response.

        Retries on transient server errors (502/503/504), which the shared
        deployment can return when many requests arrive in quick succession.
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_id(),
        }
        if params is not None:
            payload["params"] = params

        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            try:
                # Use streaming to handle SSE responses without hanging
                async with self._client.stream(
                    "POST", "/mcp", json=payload, headers=headers
                ) as resp:
                    if resp.status_code in (502, 503, 504):
                        await resp.aread()  # drain so the connection is reusable
                        raise httpx.HTTPStatusError(
                            f"Transient server error {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()

                    # Capture session ID from response headers
                    if "mcp-session-id" in resp.headers:
                        self._session_id = resp.headers["mcp-session-id"]

                    content_type = resp.headers.get("content-type", "")

                    if "text/event-stream" in content_type:
                        # Read SSE stream line-by-line, extract last JSON-RPC result
                        last_data = None
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                last_data = line[6:]
                        if last_data:
                            return json.loads(last_data)
                        return {"error": {"code": -1, "message": "No data in SSE response"}}
                    else:
                        # Regular JSON response
                        body = await resp.aread()
                        return json.loads(body)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in (502, 503, 504):
                    last_exc = exc
                    if attempt < self._MAX_RETRIES - 1:
                        await asyncio.sleep(self._RETRY_BACKOFF * (attempt + 1))
                        continue
                raise
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                # Server dropped the connection mid-response or refused it —
                # treat as transient (pod restart, OOM-kill, HAProxy timeout).
                last_exc = exc
                if attempt < self._MAX_RETRIES - 1:
                    await asyncio.sleep(self._RETRY_BACKOFF * (attempt + 1))
                    continue
                raise

        assert last_exc is not None
        raise last_exc

    # ── High-level MCP protocol methods ─────────────────────────────────────

    async def initialize(self) -> dict:
        """Perform MCP client-server handshake."""
        result = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "aice-test-client", "version": "1.0.0"},
        })
        # Send initialized notification
        await self._rpc("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        """List all available tools on the server."""
        result = await self._rpc("tools/list")
        if "result" in result:
            return result["result"].get("tools", [])
        return []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Call an MCP tool and return its result."""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        # Use custom timeout if specified
        if timeout and self._client:
            original_timeout = self._client.timeout
            self._client.timeout = httpx.Timeout(timeout)
            try:
                result = await self._rpc("tools/call", params)
            finally:
                self._client.timeout = original_timeout
        else:
            result = await self._rpc("tools/call", params)

        return result

    # ── Response validation helpers ─────────────────────────────────────────

    @staticmethod
    def is_success(response: dict) -> bool:
        """Check if the MCP response indicates success (no JSON-RPC error)."""
        if "error" in response:
            return False
        result = response.get("result", {})
        # Tool results have content array
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                text = content[0].get("text", "")
                try:
                    parsed = json.loads(text)
                    return parsed.get("status") != "error"
                except (json.JSONDecodeError, AttributeError):
                    pass
        return True

    @staticmethod
    def get_tool_result(response: dict) -> Any:
        """Extract the tool result payload from an MCP response."""
        result = response.get("result", {})
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, AttributeError):
                    return text
        return result

    @staticmethod
    def get_error(response: dict) -> str | None:
        """Extract error message from a failed response."""
        if "error" in response:
            return response["error"].get("message", "Unknown error")
        result = response.get("result", {})
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                text = content[0].get("text", "")
                try:
                    parsed = json.loads(text)
                    if parsed.get("status") == "error":
                        return parsed.get("message", "Tool returned error")
                except (json.JSONDecodeError, AttributeError):
                    pass
        return None

"""
E2E Test Fixtures for MCP Server Integration Testing.

Provides session-scoped MCP client, automatic server reachability checks,
and shared test session management.

Environment variables:
    MCP_TEST_URL     — Base URL of deployed MCP server
    MCP_TEST_API_KEY — API key for authentication
"""

import os
from pathlib import Path
import uuid

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

from .mcp_test_client import MCPTestClient

# Load credentials from env/.env if available
_ENV_PATH = Path(__file__).resolve().parents[2] / "env" / ".env"
load_dotenv(_ENV_PATH)


def pytest_collection_modifyitems(config, items):
    """Auto-skip E2E tests if MCP_TEST_URL is not set or unreachable."""
    url = os.environ.get("MCP_TEST_URL", "")
    if not url:
        skip_marker = pytest.mark.skip(reason="MCP_TEST_URL not set")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_marker)
        return

    # Quick reachability check
    try:
        resp = httpx.get(f"{url.rstrip('/')}/docs", timeout=5.0, verify=False)
        if resp.status_code >= 500:
            raise ConnectionError(f"Server error: {resp.status_code}")
    except Exception as e:
        skip_marker = pytest.mark.skip(reason=f"MCP server unreachable: {e}")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_marker)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mcp_client():
    """Session-scoped MCP client connected to the test server."""
    async with MCPTestClient() as client:
        await client.initialize()
        yield client


@pytest_asyncio.fixture(scope="class", loop_scope="session")
async def mcp_session(mcp_client: MCPTestClient):
    """Class-scoped MCP working-memory session (start → yield → end)."""
    session_id = f"e2e_{uuid.uuid4().hex[:12]}"
    resp = await mcp_client.call_tool("session_start", {
        "session_id": session_id,
        "assistant_name": "aice-e2e",
        "module_context": "Adc",
    })
    result = mcp_client.get_tool_result(resp)
    payload = result.get("data", result) if isinstance(result, dict) else {}
    session_id = payload.get("session_id", session_id) if isinstance(payload, dict) else session_id

    yield session_id

    # Teardown: end the session
    await mcp_client.call_tool("session_end", {"session_id": session_id})

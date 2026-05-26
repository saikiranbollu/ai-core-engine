"""Validate X-Request-ID header extraction and propagation."""
import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_request_id_extracted_from_header():
    """X-Request-ID header should be stored in context var."""
    from mcp.core.mcp_server import _APIKeyMiddleware, _current_request_id

    middleware = _APIKeyMiddleware(AsyncMock())

    scope = {
        "type": "http",
        "headers": [
            (b"authorization", b"Bearer test-key"),
            (b"x-request-id", b"abc-123-def"),
        ],
    }

    captured_rid = None

    async def capture_app(scope, receive, send):
        nonlocal captured_rid
        captured_rid = _current_request_id.get("")

    middleware.app = capture_app
    await middleware(scope, AsyncMock(), AsyncMock())

    assert captured_rid == "abc-123-def"


@pytest.mark.asyncio
async def test_request_id_generated_when_missing():
    """If no X-Request-ID header, generate one."""
    from mcp.core.mcp_server import _APIKeyMiddleware, _current_request_id

    middleware = _APIKeyMiddleware(AsyncMock())

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer test-key")],
    }

    captured_rid = None

    async def capture_app(scope, receive, send):
        nonlocal captured_rid
        captured_rid = _current_request_id.get("")

    middleware.app = capture_app
    await middleware(scope, AsyncMock(), AsyncMock())

    assert captured_rid  # non-empty
    assert len(captured_rid) == 8  # short UUID


@pytest.mark.asyncio
async def test_request_id_in_ok_response():
    """_ok() includes request_id when set in context."""
    from mcp.core.mcp_server import _ok, _current_request_id
    import json

    token = _current_request_id.set("test-rid-456")
    try:
        result = json.loads(_ok({"foo": "bar"}))
        assert result["request_id"] == "test-rid-456"
    finally:
        _current_request_id.reset(token)

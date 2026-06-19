"""W-13 tests — typed error classification (F-CB-08) + correlation-id propagation (F-CB-04).

`mcp.core.mcp_server` is importable in unit tests (see test_request_tracing.py /
test_error_sanitization.py), so these exercise the helpers directly.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest


def _code(envelope: str) -> str:
    return json.loads(envelope)["error_code"]


# ---------------------------------------------------------------------------
# F-CB-08 — typed error classification
# ---------------------------------------------------------------------------

class TestErrFromExc:
    def test_timeout_maps_to_internal_timeout(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(TimeoutError("slow"), "neo4j")) == "INTERNAL_TIMEOUT"

    def test_value_error_maps_to_invalid_input(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(ValueError("bad arg"), "search")) == "INVALID_INPUT"

    def test_file_not_found_maps_to_invalid_input(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(FileNotFoundError("nope"), "ingest")) == "INVALID_INPUT"

    def test_connection_error_maps_to_backend_unavailable(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(ConnectionError("refused"), "qdrant")) == "BACKEND_UNAVAILABLE"

    def test_permission_error_maps_to_permission_denied(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(PermissionError("nope"), "cerbos")) == "PERMISSION_DENIED"

    def test_generic_maps_to_internal_error(self):
        from mcp.core.mcp_server import _err_from_exc
        assert _code(_err_from_exc(RuntimeError("boom"), "mcp")) == "INTERNAL_ERROR"

    def test_cancelled_error_is_reraised(self):
        from mcp.core.mcp_server import _err_from_exc
        with pytest.raises(asyncio.CancelledError):
            _err_from_exc(asyncio.CancelledError(), "mcp")

    def test_backend_unavailable_does_not_leak_infra_detail(self):
        from mcp.core.mcp_server import _err_from_exc
        env = json.loads(_err_from_exc(ConnectionError("bolt://neo4j-legato:7687 refused"), "neo4j"))
        assert env["error_code"] == "BACKEND_UNAVAILABLE"
        assert "bolt://" not in env["message"]
        assert "neo4j-legato" not in env["message"]

    def test_envelope_has_correlation_id(self):
        from mcp.core.mcp_server import _err_from_exc
        env = json.loads(_err_from_exc(ValueError("x"), "search"))
        assert env["correlation_id"]


# ---------------------------------------------------------------------------
# F-CB-04 — correlation-id propagation
# ---------------------------------------------------------------------------

class TestCorrelationId:
    def test_ensure_generates_and_persists(self):
        from mcp.core.mcp_server import _ensure_correlation_id, _current_request_id
        token = _current_request_id.set("")
        try:
            cid = _ensure_correlation_id()
            assert cid and len(cid) == 8
            # second call returns the same persisted id
            assert _ensure_correlation_id() == cid
            assert _current_request_id.get("") == cid
        finally:
            _current_request_id.reset(token)

    def test_ok_includes_correlation_id_and_keeps_request_id(self):
        from mcp.core.mcp_server import _ok, _current_request_id
        token = _current_request_id.set("rid-12345")
        try:
            env = json.loads(_ok({"k": "v"}))
            assert env["correlation_id"] == "rid-12345"
            assert env["request_id"] == "rid-12345"  # back-compat preserved
        finally:
            _current_request_id.reset(token)

    def test_err_includes_correlation_id(self):
        from mcp.core.mcp_server import _err, _current_request_id
        token = _current_request_id.set("rid-err-1")
        try:
            env = json.loads(_err("INVALID_INPUT", "bad"))
            assert env["correlation_id"] == "rid-err-1"
        finally:
            _current_request_id.reset(token)


# ---------------------------------------------------------------------------
# F-CB-04 — audit log carries correlation_id (and the previously-dropped api key)
# ---------------------------------------------------------------------------

class TestLogAuditSignature:
    def test_log_audit_accepts_correlation_and_api_key(self):
        from src.Observability.postgres_schema import PostgresClient
        params = inspect.signature(PostgresClient.log_audit).parameters
        assert "correlation_id" in params
        assert "caller_api_key" in params

    def test_log_audit_no_raise_when_unavailable(self):
        from src.Observability.postgres_schema import PostgresClient
        client = PostgresClient(dsn=None)  # no DSN -> unavailable, returns early
        assert client.available is False
        client.log_audit(
            tool_name="search_database",
            workspace_id="illd",
            caller_api_key="sha256:abc123",
            correlation_id="rid-xyz",
            response_code="ok",
        )

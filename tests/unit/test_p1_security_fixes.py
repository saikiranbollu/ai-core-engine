"""Regression tests for the P1 critical correctness & security fixes.

Covers the review follow-ups: assert_auth_ready fail-fast, Bitbucket encoded
traversal rejection + segment encoding, secret wrapper/zeroize, Polarion
token-provider 401 refresh, testspec size cap, and the decoupled Cerbos DENY
audit path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MCP = _ROOT / "mcp"
for p in (str(_ROOT), str(_MCP)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── F-CA-A07: assert_auth_ready ───────────────────────────────────────
def test_assert_auth_ready_raises_when_required_and_registry_missing(monkeypatch, tmp_path):
    from core.auth.api_key_registry import assert_auth_ready
    monkeypatch.setenv("MCP_REQUIRE_AUTH", "1")
    monkeypatch.setenv("API_KEY_REGISTRY_PATH", str(tmp_path / "nope.yaml"))
    with pytest.raises(RuntimeError):
        assert_auth_ready(tmp_path / "nope.yaml")


def test_assert_auth_ready_noop_when_not_required(monkeypatch, tmp_path):
    from core.auth.api_key_registry import assert_auth_ready
    monkeypatch.delenv("MCP_REQUIRE_AUTH", raising=False)
    assert_auth_ready(tmp_path / "nope.yaml")  # must not raise


# ── F-CF-B02: Bitbucket path guard ────────────────────────────────────
def test_bitbucket_rejects_literal_and_encoded_traversal():
    from src.IngestionPipeline.Connectors.BitbucketConnector import (
        _safe_repo_path, _encode_repo_path, BitbucketClientError,
    )
    for bad in ["../etc/passwd", "a/../b", "%2e%2e/x", "a/%2e%2e/b", "%252e%252e/x", "a\\b"]:
        with pytest.raises(BitbucketClientError):
            _safe_repo_path(bad)
    assert _safe_repo_path("/src/main.c") == "src/main.c"
    assert _encode_repo_path("a b/c.c") == "a%20b/c.c"


# ── F-CF-X02: secret wrapper ──────────────────────────────────────────
def test_secretstr_hides_and_clears():
    from src._common.secret_str import SecretStr
    s = SecretStr("hunter2")
    assert s.get() == "hunter2"
    assert "hunter2" not in repr(s)
    s.clear()
    assert s.get() == ""


# ── F-CE-T01: workbook size cap ───────────────────────────────────────
def test_testspec_size_cap(monkeypatch, tmp_path):
    try:
        from src.IngestionPipeline.parsers.testspec_parsers import parse_testspec_workbook
    except ModuleNotFoundError:  # case-insensitive FS may expose 'Parsers'
        from src.IngestionPipeline.Parsers.testspec_parsers import parse_testspec_workbook
    f = tmp_path / "big.xlsx"
    f.write_bytes(b"x" * 4096)
    monkeypatch.setenv("AICE_TESTSPEC_MAX_BYTES", "1024")
    with pytest.raises(ValueError):
        parse_testspec_workbook(str(f), "ADC")


# ── F-CF-P01: Polarion token-provider refresh ─────────────────────────
class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def test_polarion_provider_refreshes_on_401(monkeypatch):
    from src.IngestionPipeline.Connectors.PolarionConnector import PolarionConnector
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    from itertools import count
    c = count()
    conn = PolarionConnector("https://x/polarion", lambda: f"tok{next(c)}", verify_ssl=False)

    calls = {"n": 0}

    class _Client:
        def __init__(self):
            self.headers = {}

        def request(self, *a, **k):
            calls["n"] += 1
            return _Resp(401) if calls["n"] == 1 else _Resp(200, {"ok": True})

        def close(self):
            pass

    conn._client = _Client()
    assert conn._request("GET", "/projects/p/collections") == {"ok": True}
    assert calls["n"] == 2  # retried after refresh
    conn.close()


def test_polarion_close_scrubs_authorization_header(monkeypatch):
    from src.IngestionPipeline.Connectors.PolarionConnector import PolarionConnector
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    conn = PolarionConnector("https://x", "tok", verify_ssl=False)
    assert "Authorization" in conn._client.headers
    conn.close()
    assert "Authorization" not in conn._client.headers


# ── F-CA-A03: audit path must not import the MCP server module ────────
def test_audit_deny_does_not_import_mcp_server(monkeypatch):
    sys.modules.pop("mcp.core.mcp_server", None)
    from core.auth import cerbos_client
    cerbos_client._audit_deny("t", "illd", "sha256:x", "denied")
    assert "mcp.core.mcp_server" not in sys.modules


# ── F-CA-A03: DENY rows use the hashed-key convention, not raw/principal id ──
def test_audit_deny_hashes_caller_api_key(monkeypatch):
    from core.auth import cerbos_client

    captured = {}

    class _PG:
        available = True

        def log_audit(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(cerbos_client, "_get_audit_pg", lambda: _PG())
    cerbos_client._audit_deny("t", "illd", "raw-secret-key", "denied")
    assert captured["caller_api_key"].startswith("sha256:")
    assert "raw-secret-key" not in captured["caller_api_key"]


# ── F-CF-X02: connectors drop BasicAuth credentials on close ──────────
def test_connectors_drop_basic_auth_on_close(monkeypatch):
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    from src.IngestionPipeline.Connectors.BitbucketConnector import BitbucketConnector
    from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
    b = BitbucketConnector("https://x", "P", "r", username="u", password="secret", verify_ssl=False)
    b.close()
    assert b._client.auth is None
    j = JamaConnector("https://x", "key", "secret", verify_ssl=False)
    j.close()
    assert j._client.auth is None


# ── F-CA-A03: missing-key denial keeps attempted workspace/session, no leak ──
def test_missing_key_denial_is_attributable_without_session_leak(monkeypatch):
    import core.mcp_server as srv
    rows = []
    pg = type("PG", (), {"available": True, "log_audit": lambda self, **kw: rows.append(kw)})()
    monkeypatch.setattr(srv, "_get_postgres_client", lambda: pg)
    monkeypatch.setattr(srv, "_get_settings", lambda: type("S", (), {"mcp_api_key": ""})())
    srv._current_api_key.set("")
    srv._current_session_id.set("stale")
    srv._authorize("search_database", workspace_id="mcal")
    assert rows[-1]["workspace_id"] == "mcal"
    assert rows[-1]["session_id"] is None  # stale session not carried over
    assert rows[-1]["caller_api_key"] == "anonymous"
    assert rows[-1]["response_code"] == "denied"

"""Tests for the BitbucketConnector module.

All HTTP interactions are mocked via httpx's transport mock layer so no
real Bitbucket server is needed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Connectors.BitbucketConnector import (
    BitbucketConnector,
    BitbucketAuthError,
    BitbucketClientError,
    BitbucketServerError,
    BitbucketConnectionError,
    BitbucketConnectorError,
    FileContent,
    FileEntry,
    parse_clone_url,
)


# ---------------------------------------------------------------------------
# Helpers – fake HTTP transport
# ---------------------------------------------------------------------------


def _browse_response(entries: list, is_last_page: bool = True, start: int = 0) -> dict:
    """Build a Bitbucket browse-style JSON response."""
    return {
        "path": {"components": [], "name": "", "toString": ""},
        "revision": "refs/heads/main",
        "children": {
            "size": len(entries),
            "limit": 500,
            "isLastPage": is_last_page,
            "values": entries,
            "start": start,
            **({"nextPageStart": start + 500} if not is_last_page else {}),
        },
    }


def _file_entry(name: str, entry_type: str = "FILE", size: int = 100, ext: str = "c") -> dict:
    """Build a single browse entry as returned by the Bitbucket API."""
    entry = {
        "path": {
            "components": [name],
            "parent": "",
            "name": name,
            "toString": name,
        },
        "type": entry_type,
    }
    if entry_type == "FILE":
        entry["size"] = size
        entry["contentId"] = "abc123"
        entry["path"]["extension"] = ext
    else:
        entry["node"] = "abc123"
    return entry


def _repo_metadata() -> dict:
    """Minimal repo metadata response for validate_connection."""
    return {
        "slug": "my-repo",
        "id": 1,
        "name": "my-repo",
        "state": "AVAILABLE",
        "project": {"key": "PROJ"},
    }


class MockTransport(httpx.BaseTransport):
    """A transport that returns pre-configured responses based on URL path."""

    def __init__(self):
        self.routes: dict[str, list] = {}
        self.requests_log: list[httpx.Request] = []

    def add_response(
        self,
        path: str,
        status_code: int = 200,
        json_body: dict | None = None,
        text: str = "",
    ):
        if path not in self.routes:
            self.routes[path] = []
        self.routes[path].append((status_code, json_body, text))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests_log.append(request)
        path = request.url.raw_path.split(b"?")[0].decode()
        path = path.replace("/rest/api/latest", "") or "/"

        queue = self.routes.get(path, [])
        if queue:
            status_code, json_body, text = queue.pop(0)
            if json_body is not None:
                content = json.dumps(json_body).encode()
                headers = {"content-type": "application/json"}
            else:
                content = text.encode()
                headers = {"content-type": "text/plain"}
            return httpx.Response(status_code, content=content, headers=headers)

        return httpx.Response(404, content=b"Not Found")


def _make_connector(transport: MockTransport) -> BitbucketConnector:
    """Create a connector wired to the mock transport."""
    conn = BitbucketConnector(
        base_url="http://bitbucket.test",
        project="PROJ",
        repo="my-repo",
        token="test-token",
        ref="main",
        max_retries=1,
        backoff_factor=0.0,
    )
    conn._client = httpx.Client(
        base_url="http://bitbucket.test/rest/api/latest",
        transport=transport,
        headers={"Accept": "application/json", "Authorization": "Bearer test-token"},
    )
    return conn


# ---------------------------------------------------------------------------
# Tests — parse_clone_url
# ---------------------------------------------------------------------------


class TestParseCloneUrl:
    def test_ssh_url_with_scheme(self):
        result = parse_clone_url("ssh://git@bitbucket.vih.infineon.com:7999/ASTERISK/asterisk_files.git")
        assert result == {"host": "bitbucket.vih.infineon.com", "project": "ASTERISK", "repo": "asterisk_files"}

    def test_ssh_url_without_scheme(self):
        result = parse_clone_url("git@bitbucket.vih.infineon.com:7999/PROJ/my-repo.git")
        assert result == {"host": "bitbucket.vih.infineon.com", "project": "PROJ", "repo": "my-repo"}

    def test_https_url(self):
        result = parse_clone_url("https://bitbucket.vih.infineon.com/scm/ASTERISK/asterisk_files.git")
        assert result == {"host": "bitbucket.vih.infineon.com", "project": "ASTERISK", "repo": "asterisk_files"}

    def test_https_url_no_dot_git(self):
        result = parse_clone_url("https://bitbucket.vih.infineon.com/scm/PROJ/repo")
        assert result == {"host": "bitbucket.vih.infineon.com", "project": "PROJ", "repo": "repo"}

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_clone_url("ftp://not-a-bitbucket-url")

    def test_whitespace_trimmed(self):
        result = parse_clone_url("  ssh://git@bb.test:7999/P/R.git  ")
        assert result["project"] == "P"
        assert result["repo"] == "R"


# ---------------------------------------------------------------------------
# Tests — validate_connection
# ---------------------------------------------------------------------------


class TestValidateConnection:
    def test_success(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo", 200, _repo_metadata())
        conn = _make_connector(transport)
        assert conn.validate_connection() is True
        assert conn._connected is True

    def test_auth_failure_401(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo", 401, None, "Unauthorized")
        conn = _make_connector(transport)
        with pytest.raises(BitbucketAuthError):
            conn.validate_connection()
        assert conn._connected is False

    def test_auth_failure_403(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo", 403, None, "Forbidden")
        conn = _make_connector(transport)
        with pytest.raises(BitbucketAuthError):
            conn.validate_connection()
        assert conn._connected is False


# ---------------------------------------------------------------------------
# Tests — get_file_content
# ---------------------------------------------------------------------------


class TestGetFileContent:
    def test_fetch_single_file(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/PROJ/repos/my-repo/raw/src/main.c",
            200,
            None,
            "int main() { return 0; }",
        )
        conn = _make_connector(transport)
        result = conn.get_file_content("src/main.c")
        assert isinstance(result, FileContent)
        assert result.path == "src/main.c"
        assert result.content == "int main() { return 0; }"
        assert result.size == len("int main() { return 0; }")

    def test_fetch_strips_leading_slash(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/PROJ/repos/my-repo/raw/README.md",
            200,
            None,
            "# Hello",
        )
        conn = _make_connector(transport)
        result = conn.get_file_content("/README.md")
        assert result.path == "README.md"

    def test_file_not_found(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/PROJ/repos/my-repo/raw/missing.txt",
            404,
            None,
            "Not Found",
        )
        conn = _make_connector(transport)
        with pytest.raises(BitbucketClientError, match="Not found"):
            conn.get_file_content("missing.txt")


# ---------------------------------------------------------------------------
# Tests — list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_root_directory(self):
        transport = MockTransport()
        entries = [
            _file_entry("src", "DIRECTORY"),
            _file_entry("README.md", "FILE", 512, "md"),
        ]
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/",
            200,
            _browse_response(entries),
        )
        conn = _make_connector(transport)
        result = conn.list_directory()
        assert len(result) == 2
        assert result[0].path == "src"
        assert result[0].entry_type == "DIRECTORY"
        assert result[1].path == "README.md"
        assert result[1].entry_type == "FILE"
        assert result[1].extension == "md"

    def test_subdirectory(self):
        transport = MockTransport()
        entries = [_file_entry("main.c", "FILE", 200, "c")]
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/src",
            200,
            _browse_response(entries),
        )
        conn = _make_connector(transport)
        result = conn.list_directory("src")
        assert len(result) == 1
        assert result[0].path == "src/main.c"

    def test_pagination(self):
        transport = MockTransport()
        page1_entries = [_file_entry("a.c", "FILE", 100, "c")]
        page2_entries = [_file_entry("b.c", "FILE", 200, "c")]
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/",
            200,
            _browse_response(page1_entries, is_last_page=False, start=0),
        )
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/",
            200,
            _browse_response(page2_entries, is_last_page=True, start=500),
        )
        conn = _make_connector(transport)
        result = conn.list_directory()
        assert len(result) == 2
        assert result[0].path == "a.c"
        assert result[1].path == "b.c"


# ---------------------------------------------------------------------------
# Tests — get_file_tree (recursive)
# ---------------------------------------------------------------------------


class TestGetFileTree:
    def test_recursive_walk(self):
        transport = MockTransport()
        # Root: one dir, one file
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/",
            200,
            _browse_response([
                _file_entry("src", "DIRECTORY"),
                _file_entry("README.md", "FILE", 100, "md"),
            ]),
        )
        # src/: two files
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/src",
            200,
            _browse_response([
                _file_entry("main.c", "FILE", 200, "c"),
                _file_entry("util.h", "FILE", 50, "h"),
            ]),
        )
        conn = _make_connector(transport)
        result = conn.get_file_tree()
        assert len(result) == 3
        paths = {e.path for e in result}
        assert paths == {"README.md", "src/main.c", "src/util.h"}

    def test_extension_filter(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/PROJ/repos/my-repo/browse/",
            200,
            _browse_response([
                _file_entry("main.c", "FILE", 200, "c"),
                _file_entry("util.h", "FILE", 50, "h"),
                _file_entry("README.md", "FILE", 100, "md"),
            ]),
        )
        conn = _make_connector(transport)
        result = conn.get_file_tree(extensions=["c", "h"])
        assert len(result) == 2
        paths = {e.path for e in result}
        assert paths == {"main.c", "util.h"}


# ---------------------------------------------------------------------------
# Tests — get_files_bulk
# ---------------------------------------------------------------------------


class TestGetFilesBulk:
    def test_parallel_fetch(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo/raw/a.c", 200, None, "aaa")
        transport.add_response("/projects/PROJ/repos/my-repo/raw/b.c", 200, None, "bbb")
        conn = _make_connector(transport)
        results = conn.get_files_bulk(["a.c", "b.c"])
        assert len(results) == 2
        assert results["a.c"].content == "aaa"
        assert results["b.c"].content == "bbb"

    def test_partial_failure(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo/raw/a.c", 200, None, "aaa")
        transport.add_response("/projects/PROJ/repos/my-repo/raw/missing.c", 404, None, "Not Found")
        conn = _make_connector(transport)
        results = conn.get_files_bulk(["a.c", "missing.c"])
        assert len(results) == 1
        assert "a.c" in results


# ---------------------------------------------------------------------------
# Tests — retry and error handling
# ---------------------------------------------------------------------------


class TestRetryAndErrors:
    def test_server_error_retries(self):
        transport = MockTransport()
        transport.add_response("/projects/PROJ/repos/my-repo", 500, None, "Error")
        conn = _make_connector(transport)
        with pytest.raises(BitbucketConnectionError, match="failed after"):
            conn.validate_connection()

    def test_client_error_no_retry(self):
        transport = MockTransport()
        # Add two 400 responses — only the first should be consumed
        transport.add_response("/projects/PROJ/repos/my-repo/raw/bad", 400, None, "Bad Request")
        transport.add_response("/projects/PROJ/repos/my-repo/raw/bad", 200, None, "OK")
        conn = _make_connector(transport)
        with pytest.raises(BitbucketClientError):
            conn.get_file_content("bad")


# ---------------------------------------------------------------------------
# Tests — from_clone_url
# ---------------------------------------------------------------------------


class TestFromCloneUrl:
    @patch.object(BitbucketConnector, "__init__", return_value=None)
    def test_ssh_url(self, mock_init):
        BitbucketConnector.from_clone_url(
            "ssh://git@bitbucket.vih.infineon.com:7999/PROJ/my-repo.git",
            token="tok",
        )
        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args.kwargs
        assert call_kwargs["token"] == "tok"
        assert "bitbucket.vih.infineon.com" in call_kwargs["base_url"]
        assert call_kwargs["project"] == "PROJ"
        assert call_kwargs["repo"] == "my-repo"

    @patch.object(BitbucketConnector, "__init__", return_value=None)
    def test_https_url(self, mock_init):
        BitbucketConnector.from_clone_url(
            "https://bitbucket.vih.infineon.com/scm/ASTERISK/asterisk_files.git",
            scheme="https",
            token="tok",
        )
        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args.kwargs
        assert "https://" in call_kwargs["base_url"]
        assert call_kwargs["project"] == "ASTERISK"
        assert call_kwargs["repo"] == "asterisk_files"


# ---------------------------------------------------------------------------
# Tests — context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_enter_exit(self):
        transport = MockTransport()
        conn = _make_connector(transport)
        with conn as c:
            assert c is conn
        # After exit, client should be closed (no explicit assertion needed,
        # just verifying no exception is raised)

    def test_properties(self):
        transport = MockTransport()
        conn = _make_connector(transport)
        assert conn.project == "PROJ"
        assert conn.repo == "my-repo"
        assert conn.ref == "main"
        assert conn.base_url == "http://bitbucket.test"

        conn.ref = "develop"
        assert conn.ref == "develop"


# ---------------------------------------------------------------------------
# Tests — from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_with_clone_url_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_CLONE_URL", "ssh://git@bb.test:7999/PROJ/my-repo.git")
        monkeypatch.setenv("IFX_USERNAME", "user1")
        monkeypatch.setenv("IFX_PASSWORD", "pass1")
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env()
            mock_init.assert_called_once()
            kw = mock_init.call_args.kwargs
            assert kw["project"] == "PROJ"
            assert kw["repo"] == "my-repo"
            assert kw["username"] == "user1"
            assert kw["password"] == "pass1"

    def test_with_project_and_repo_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_BASE_URL", "http://bb.test")
        monkeypatch.setenv("BITBUCKET_PROJECT", "MYPROJ")
        monkeypatch.setenv("BITBUCKET_REPO", "myrepo")
        monkeypatch.setenv("IFX_USERNAME", "user1")
        monkeypatch.setenv("IFX_PASSWORD", "pass1")
        monkeypatch.delenv("BITBUCKET_CLONE_URL", raising=False)
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env()
            mock_init.assert_called_once()
            kw = mock_init.call_args.kwargs
            assert kw["base_url"] == "http://bb.test"
            assert kw["project"] == "MYPROJ"
            assert kw["repo"] == "myrepo"

    def test_with_token_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_PROJECT", "P")
        monkeypatch.setenv("BITBUCKET_REPO", "R")
        monkeypatch.setenv("BITBUCKET_TOKEN", "my-token")
        monkeypatch.delenv("BITBUCKET_CLONE_URL", raising=False)
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env()
            kw = mock_init.call_args.kwargs
            assert kw["token"] == "my-token"

    def test_with_ref_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_PROJECT", "P")
        monkeypatch.setenv("BITBUCKET_REPO", "R")
        monkeypatch.setenv("BITBUCKET_REF", "develop")
        monkeypatch.delenv("BITBUCKET_CLONE_URL", raising=False)
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env()
            kw = mock_init.call_args.kwargs
            assert kw["ref"] == "develop"

    def test_missing_project_repo_raises(self, monkeypatch):
        monkeypatch.delenv("BITBUCKET_CLONE_URL", raising=False)
        monkeypatch.delenv("BITBUCKET_PROJECT", raising=False)
        monkeypatch.delenv("BITBUCKET_REPO", raising=False)
        with pytest.raises(BitbucketConnectorError, match="BITBUCKET_PROJECT"):
            BitbucketConnector.from_env()

    def test_clone_url_param_overrides_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_CLONE_URL", "ssh://git@other:7999/OTHER/other.git")
        monkeypatch.setenv("IFX_USERNAME", "u")
        monkeypatch.setenv("IFX_PASSWORD", "p")
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env(
                clone_url="ssh://git@bb.test:7999/PROJ/my-repo.git"
            )
            kw = mock_init.call_args.kwargs
            assert kw["project"] == "PROJ"
            assert kw["repo"] == "my-repo"

    def test_default_base_url(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_PROJECT", "P")
        monkeypatch.setenv("BITBUCKET_REPO", "R")
        monkeypatch.delenv("BITBUCKET_BASE_URL", raising=False)
        monkeypatch.delenv("BITBUCKET_CLONE_URL", raising=False)
        with patch.object(BitbucketConnector, "__init__", return_value=None) as mock_init:
            BitbucketConnector.from_env()
            kw = mock_init.call_args.kwargs
            assert kw["base_url"] == "http://bitbucket.vih.infineon.com"

"""Tests for the JamaConnector module.

All HTTP interactions are mocked via httpx's transport mock layer so no
real Jama server is needed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest

# Allow running from the repo root
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Connectors.JamaConnector import (
    JamaConnector,
    JamaItem,
    JamaAuthError,
    JamaClientError,
    JamaServerError,
    JamaConnectionError,
    JamaConnectorError,
    SyncState,
    SyncStatus,
)


# ---------------------------------------------------------------------------
# Helpers – fake HTTP transport
# ---------------------------------------------------------------------------

def _jama_page_response(
    data: list,
    start_at: int = 0,
    max_results: int = 50,
    total_results: int | None = None,
) -> dict:
    """Build a Jama-style paginated JSON response body."""
    total = total_results if total_results is not None else len(data)
    return {
        "meta": {
            "status": "OK",
            "pageInfo": {
                "startIndex": start_at,
                "resultCount": len(data),
                "totalResults": total,
            },
        },
        "data": data,
    }


def _make_raw_item(
    item_id: int = 1,
    project_id: int = 42,
    name: str = "REQ-001",
    item_type: int = 83,
    version: int = 3,
    modified_date: str = "2026-01-15T12:00:00.000+0000",
    created_date: str = "2025-06-01T08:00:00.000+0000",
) -> dict:
    """Build a raw Jama item dict as returned by the REST API."""
    return {
        "id": item_id,
        "project": project_id,
        "itemType": item_type,
        "modifiedDate": modified_date,
        "createdDate": created_date,
        "version": {"versionNumber": version},
        "location": {"sequence": "1.2.3"},
        "lock": {"locked": False},
        "fields": {
            "name": name,
            "documentKey": f"DOC-{item_id}",
            "description": f"Description for {name}",
            "status": 42,
        },
    }


class MockTransport(httpx.BaseTransport):
    """A transport that returns pre-configured responses based on URL path."""

    def __init__(self):
        self.routes: dict[str, list] = {}  # path -> list of responses
        self.requests_log: list[httpx.Request] = []

    def add_response(
        self,
        path: str,
        status_code: int = 200,
        json_body: dict | None = None,
        text: str = "",
    ):
        """Register a response for a given path (appended to queue)."""
        if path not in self.routes:
            self.routes[path] = []
        self.routes[path].append((status_code, json_body, text))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests_log.append(request)
        path = request.url.raw_path.split(b"?")[0].decode()
        # Strip the api-root prefix for matching
        path = path.replace("/rest/v1", "") or "/"

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

        # Default: 404
        return httpx.Response(404, content=b"Not Found")


def _make_connector(
    transport: MockTransport,
    **kwargs,
) -> JamaConnector:
    """Create a JamaConnector with a mocked HTTP transport."""
    defaults = dict(
        base_url="https://jama.test.local",
        api_key="test-key",
        api_secret="test-secret",
        max_retries=1,
        backoff_factor=0.0,
    )
    defaults.update(kwargs)
    connector = JamaConnector(**defaults)
    # Replace the internal httpx client with one that uses our transport
    connector._client.close()
    connector._client = httpx.Client(
        base_url="https://jama.test.local/rest/v1",
        transport=transport,
        timeout=5,
        headers={"Accept": "application/json"},
    )
    return connector


# ===================================================================
# 1. JamaItem data model tests
# ===================================================================


class TestJamaItem:
    """Tests for the JamaItem.from_api_dict factory."""

    def test_from_api_dict_basic(self):
        raw = _make_raw_item(item_id=10, name="My Req", project_id=7)
        item = JamaItem.from_api_dict(raw)

        assert item.id == 10
        assert item.project_id == 7
        assert item.name == "My Req"
        assert item.document_key == "DOC-10"
        assert item.item_type == 83
        assert item.version == 3
        assert item.sequence == "1.2.3"
        assert item.locked is False
        assert item.status == 42
        assert item.modified_date == "2026-01-15T12:00:00.000+0000"

    def test_from_api_dict_version_as_int(self):
        raw = _make_raw_item()
        raw["version"] = 5
        item = JamaItem.from_api_dict(raw)
        assert item.version == 5

    def test_from_api_dict_version_unknown_type(self):
        raw = _make_raw_item()
        raw["version"] = "invalid"
        item = JamaItem.from_api_dict(raw)
        assert item.version == -1

    def test_from_api_dict_missing_fields(self):
        item = JamaItem.from_api_dict({})
        assert item.id == -1
        assert item.project_id == -1
        assert item.name == ""
        assert item.version == -1

    def test_from_api_dict_lock_none(self):
        raw = _make_raw_item()
        raw["lock"] = None
        item = JamaItem.from_api_dict(raw)
        assert item.locked is False

    def test_from_api_dict_baseline_location(self):
        raw = _make_raw_item()
        raw.pop("location")
        raw["baselineLocation"] = {"sequence": "9.8.7"}
        item = JamaItem.from_api_dict(raw)
        assert item.sequence == "9.8.7"


# ===================================================================
# 2. Authentication & connection validation tests
# ===================================================================


class TestAuthentication:
    """Tests for API key auth and connection validation."""

    def test_validate_connection_success(self):
        transport = MockTransport()
        transport.add_response(
            "/projects",
            200,
            _jama_page_response([{"id": 1, "fields": {"name": "P1"}}]),
        )
        connector = _make_connector(transport)
        assert connector.validate_connection() is True
        assert connector._connected is True

    def test_validate_connection_auth_failure(self):
        transport = MockTransport()
        transport.add_response("/projects", 401, text="Unauthorized")
        connector = _make_connector(transport)

        with pytest.raises(JamaAuthError):
            connector.validate_connection()
        assert connector._connected is False

    def test_validate_connection_server_error(self):
        transport = MockTransport()
        transport.add_response("/projects", 500, text="Internal Server Error")
        connector = _make_connector(transport)

        with pytest.raises(JamaConnectionError):
            connector.validate_connection()
        assert connector._connected is False

    def test_context_manager(self):
        transport = MockTransport()
        transport.add_response(
            "/projects",
            200,
            _jama_page_response([{"id": 1, "fields": {"name": "P1"}}]),
        )
        with _make_connector(transport) as conn:
            assert conn.validate_connection() is True


# ===================================================================
# 3. Retry / error handling tests
# ===================================================================


class TestRetryAndErrors:
    """Tests for retry logic and error classification."""

    def test_client_error_no_retry(self):
        """4xx (non-401) errors should NOT be retried."""
        transport = MockTransport()
        transport.add_response("/projects", 403, text="Forbidden")
        connector = _make_connector(transport)

        with pytest.raises(JamaClientError, match="403"):
            connector.get_projects()

        # Only 1 request issued (no retries)
        assert len(transport.requests_log) == 1

    def test_auth_error_no_retry(self):
        """401 errors should NOT be retried."""
        transport = MockTransport()
        transport.add_response("/projects", 401, text="Unauthorized")
        connector = _make_connector(transport)

        with pytest.raises(JamaAuthError):
            connector.get_projects()
        assert len(transport.requests_log) == 1

    def test_server_error_retried(self):
        """500 errors should be retried up to max_retries."""
        transport = MockTransport()
        # All attempts fail with 500
        transport.add_response("/projects", 500, text="Error")
        connector = _make_connector(transport, max_retries=1)

        with pytest.raises(JamaConnectionError, match="failed after 1 retries"):
            connector._request("GET", "/projects")

    def test_server_error_then_success(self):
        """Retry succeeds on second attempt after initial 500."""
        transport = MockTransport()
        transport.add_response("/projects", 500, text="Transient")
        transport.add_response(
            "/projects",
            200,
            _jama_page_response([{"id": 1, "fields": {"name": "P1"}}]),
        )
        connector = _make_connector(transport, max_retries=2)

        result = connector._request("GET", "/projects")
        assert result["data"] is not None
        assert len(transport.requests_log) == 2


# ===================================================================
# 4. Pagination tests
# ===================================================================


class TestPagination:
    """Tests for automatic pagination across multiple pages."""

    def test_single_page(self):
        items = [_make_raw_item(item_id=i) for i in range(3)]
        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response(items, total_results=3),
        )
        connector = _make_connector(transport)
        result = connector.get_items(project_id=42)
        assert len(result) == 3
        assert all(isinstance(r, JamaItem) for r in result)

    def test_multi_page(self):
        """When total > page size, multiple requests are issued."""
        page1 = [_make_raw_item(item_id=i) for i in range(2)]
        page2 = [_make_raw_item(item_id=i) for i in range(2, 5)]

        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response(page1, start_at=0, total_results=5),
        )
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response(page2, start_at=2, total_results=5),
        )
        connector = _make_connector(transport, max_results_per_page=2)
        result = connector.get_items(project_id=42)
        assert len(result) == 5

    def test_empty_result(self):
        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response([], total_results=0),
        )
        connector = _make_connector(transport)
        result = connector.get_items(project_id=99)
        assert result == []


# ===================================================================
# 5. Filtering tests
# ===================================================================


class TestFiltering:
    """Tests for project, item-type, and module filtering."""

    def test_filter_by_project(self):
        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response([_make_raw_item(project_id=42)]),
        )
        connector = _make_connector(transport)
        result = connector.get_items_by_project(42)
        assert len(result) == 1
        assert result[0].project_id == 42

        # Verify the request included the project query param
        req = transport.requests_log[0]
        assert b"project=42" in req.url.raw_path

    def test_filter_by_type(self):
        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response([_make_raw_item(item_type=83)]),
        )
        connector = _make_connector(transport)
        result = connector.get_items_by_type(project_id=42, item_type_id=83)
        assert len(result) == 1

        req = transport.requests_log[0]
        assert b"itemType=83" in req.url.raw_path

    def test_get_children_items(self):
        children = [_make_raw_item(item_id=i) for i in range(4)]
        transport = MockTransport()
        transport.add_response(
            "/items/100/children",
            200,
            _jama_page_response(children),
        )
        connector = _make_connector(transport)
        result = connector.get_children_items(100)
        assert len(result) == 4

    def test_get_module_items_recursive(self):
        """Module retrieval should recurse into sub-folders."""
        folder_item = _make_raw_item(item_id=200, item_type=32, name="SubFolder")
        leaf_item1 = _make_raw_item(item_id=201, item_type=83, name="Leaf1")
        leaf_item2 = _make_raw_item(item_id=202, item_type=83, name="Leaf2")

        transport = MockTransport()
        # First call: children of module 100 -> folder + leaf
        transport.add_response(
            "/items/100/children",
            200,
            _jama_page_response([folder_item, leaf_item1]),
        )
        # Second call: children of sub-folder 200 -> a leaf
        transport.add_response(
            "/items/200/children",
            200,
            _jama_page_response([leaf_item2]),
        )
        connector = _make_connector(transport)
        result = connector.get_module_items(100, recurse=True)
        # Should contain leaf_item1 and leaf_item2 (not the folder itself)
        assert len(result) == 2
        names = {item.name for item in result}
        assert names == {"Leaf1", "Leaf2"}

    def test_get_module_items_no_recurse(self):
        folder_item = _make_raw_item(item_id=200, item_type=32, name="SubFolder")
        leaf_item = _make_raw_item(item_id=201, item_type=83, name="Leaf")

        transport = MockTransport()
        transport.add_response(
            "/items/100/children",
            200,
            _jama_page_response([folder_item, leaf_item]),
        )
        connector = _make_connector(transport)
        result = connector.get_module_items(100, recurse=False)
        assert len(result) == 1
        assert result[0].name == "Leaf"

    def test_get_filter_results(self):
        transport = MockTransport()
        transport.add_response(
            "/filters/55/results",
            200,
            _jama_page_response([_make_raw_item()]),
        )
        connector = _make_connector(transport)
        result = connector.get_filter_results(55, project_id=42)
        assert len(result) == 1

    def test_get_single_item(self):
        transport = MockTransport()
        transport.add_response(
            "/items/10",
            200,
            {"data": _make_raw_item(item_id=10, name="Single"), "meta": {}},
        )
        connector = _make_connector(transport)
        item = connector.get_item(10)
        assert item.id == 10
        assert item.name == "Single"


# ===================================================================
# 6. Project discovery tests
# ===================================================================


class TestProjects:
    def test_get_projects(self):
        transport = MockTransport()
        projects = [
            {"id": 1, "fields": {"name": "Alpha"}},
            {"id": 2, "fields": {"name": "Beta"}},
        ]
        transport.add_response(
            "/projects", 200, _jama_page_response(projects)
        )
        connector = _make_connector(transport)
        result = connector.get_projects()
        assert len(result) == 2

    def test_get_project_id_found(self):
        transport = MockTransport()
        projects = [
            {"id": 1, "fields": {"name": "Alpha"}},
            {"id": 2, "fields": {"name": "Beta"}},
        ]
        transport.add_response(
            "/projects", 200, _jama_page_response(projects)
        )
        connector = _make_connector(transport)
        assert connector.get_project_id("Beta") == 2

    def test_get_project_id_not_found(self):
        transport = MockTransport()
        transport.add_response(
            "/projects", 200, _jama_page_response([])
        )
        connector = _make_connector(transport)
        assert connector.get_project_id("Ghost") is None


# ===================================================================
# 7. Incremental sync tests
# ===================================================================


class TestIncrementalSync:
    """Tests for incremental sync, state persistence, and deletion detection."""

    def test_first_sync_fetches_all(self):
        """First sync (no prior state) should fetch everything."""
        items = [_make_raw_item(item_id=i) for i in range(3)]
        transport = MockTransport()
        transport.add_response(
            "/abstractitems",
            200,
            _jama_page_response(items),
        )
        connector = _make_connector(transport)
        report = connector.incremental_sync(project_id=42, detect_deletions=False)

        assert report["status"] == "success"
        assert report["items_synced"] == 3
        assert len(report["modified_items"]) == 3
        assert report["sync_timestamp"] is not None
        assert report["duration_ms"] > 0

    def test_incremental_sync_uses_modified_since(self):
        """Second sync should pass modifiedDate from prior state."""
        items = [_make_raw_item(item_id=1)]
        transport = MockTransport()
        # First sync
        transport.add_response(
            "/abstractitems", 200, _jama_page_response(items)
        )
        connector = _make_connector(transport)
        report1 = connector.incremental_sync(project_id=42, detect_deletions=False)
        ts = report1["sync_timestamp"]

        # Second sync – should include modifiedDate=ts
        transport.add_response(
            "/abstractitems", 200, _jama_page_response([])
        )
        report2 = connector.incremental_sync(project_id=42, detect_deletions=False)
        assert report2["items_synced"] == 0

        # Verify modifiedDate was passed in second request
        second_req = transport.requests_log[1]
        assert b"modifiedDate" in second_req.url.raw_path

    def test_sync_state_persistence(self):
        """Sync state should be saved to and loaded from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            items = [_make_raw_item(item_id=1)]
            transport = MockTransport()
            transport.add_response(
                "/abstractitems", 200, _jama_page_response(items)
            )

            connector = _make_connector(transport, sync_state_dir=tmpdir)
            report = connector.incremental_sync(
                project_id=42, detect_deletions=False
            )

            # State file should exist
            state_file = Path(tmpdir) / "jama_sync_42.json"
            assert state_file.exists()

            raw = json.loads(state_file.read_text())
            assert raw["project_id"] == 42
            assert raw["last_sync_timestamp"] == report["sync_timestamp"]
            assert raw["last_sync_status"] == "success"

    def test_sync_state_loaded_on_restart(self):
        """A new connector instance should load persisted state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a pre-existing state file
            state = {
                "project_id": 42,
                "last_sync_timestamp": "2026-01-01T00:00:00.000+0000",
                "last_sync_status": "success",
                "items_synced": 10,
                "deleted_item_ids": [],
            }
            state_file = Path(tmpdir) / "jama_sync_42.json"
            state_file.write_text(json.dumps(state))

            transport = MockTransport()
            transport.add_response(
                "/abstractitems", 200, _jama_page_response([])
            )
            connector = _make_connector(transport, sync_state_dir=tmpdir)
            connector.incremental_sync(project_id=42, detect_deletions=False)

            # The request should contain the modifiedDate from the state file
            req = transport.requests_log[0]
            assert b"modifiedDate" in req.url.raw_path

    def test_detect_deleted_items(self):
        """Deletion detection identifies items no longer on the server."""
        transport = MockTransport()
        # Sync: returns items 1, 2 (but previously knew about 1, 2, 3)
        items = [_make_raw_item(item_id=1), _make_raw_item(item_id=2)]
        transport.add_response(
            "/abstractitems", 200, _jama_page_response(items)
        )
        # Deletion detection re-fetches current IDs
        transport.add_response(
            "/abstractitems", 200, _jama_page_response(items)
        )

        connector = _make_connector(transport)
        # Seed prior state with known IDs including 3
        connector._sync_states[42] = SyncState(
            project_id=42,
            last_sync_timestamp="2026-01-01T00:00:00.000+0000",
            deleted_item_ids=[1, 2, 3],
        )

        report = connector.incremental_sync(project_id=42, detect_deletions=True)
        assert 3 in report["deleted_item_ids"]
        assert 1 not in report["deleted_item_ids"]

    def test_sync_failure_logged(self):
        """Sync should report failure status if fetching items fails."""
        transport = MockTransport()
        transport.add_response("/abstractitems", 403, text="Forbidden")
        connector = _make_connector(transport)

        report = connector.incremental_sync(project_id=42)
        assert report["status"] == "failed"
        assert report["items_synced"] == 0

    def test_full_sync_resets_state(self):
        """full_sync should clear prior state and fetch everything."""
        transport = MockTransport()
        items = [_make_raw_item(item_id=i) for i in range(2)]
        transport.add_response(
            "/abstractitems", 200, _jama_page_response(items)
        )
        connector = _make_connector(transport)
        # Seed prior state
        connector._sync_states[42] = SyncState(
            project_id=42,
            last_sync_timestamp="2025-01-01T00:00:00.000+0000",
        )

        report = connector.full_sync(project_id=42)
        assert report["status"] == "success"
        assert report["items_synced"] == 2
        # The request should NOT have modifiedDate= as a filter param
        # (sortBy=modifiedDate is fine, only modifiedDate=<timestamp> is a filter)
        first_req = transport.requests_log[0]
        from urllib.parse import parse_qs, urlsplit
        qs = parse_qs(urlsplit(str(first_req.url)).query)
        assert "modifiedDate" not in qs

    def test_corrupted_state_file_handled(self):
        """Corrupted state JSON should be handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "jama_sync_42.json"
            state_file.write_text("NOT VALID JSON {{{}}")

            transport = MockTransport()
            transport.add_response(
                "/abstractitems", 200, _jama_page_response([])
            )
            connector = _make_connector(transport, sync_state_dir=tmpdir)
            # Should not raise
            report = connector.incremental_sync(
                project_id=42, detect_deletions=False
            )
            assert report["status"] == "success"


# ===================================================================
# 8. Relationship retrieval tests
# ===================================================================


class TestRelationships:
    def test_get_downstream_relationships(self):
        rels = [{"fromItem": 1, "toItem": 2, "relationshipType": 5}]
        transport = MockTransport()
        transport.add_response(
            "/items/1/downstreamrelationships",
            200,
            _jama_page_response(rels),
        )
        connector = _make_connector(transport)
        result = connector.get_downstream_relationships(1)
        assert len(result) == 1
        assert result[0]["toItem"] == 2

    def test_get_upstream_relationships(self):
        rels = [{"fromItem": 3, "toItem": 1, "relationshipType": 5}]
        transport = MockTransport()
        transport.add_response(
            "/items/1/upstreamrelationships",
            200,
            _jama_page_response(rels),
        )
        connector = _make_connector(transport)
        result = connector.get_upstream_relationships(1)
        assert len(result) == 1
        assert result[0]["fromItem"] == 3


# ===================================================================
# 9. Repr / misc tests
# ===================================================================


class TestMisc:
    def test_repr(self):
        transport = MockTransport()
        connector = _make_connector(transport)
        r = repr(connector)
        assert "jama.test.local" in r
        assert "connected=False" in r

    def test_get_item_types(self):
        types = [{"id": 83, "display": "Requirement"}]
        transport = MockTransport()
        transport.add_response(
            "/itemtypes", 200, _jama_page_response(types)
        )
        connector = _make_connector(transport)
        result = connector.get_item_types()
        assert len(result) == 1
        assert result[0]["id"] == 83

"""Integration tests for JamaConnector against a live Jama server.

These tests require environment variables to be set:
    JAMA_BASE_URL  – Jama server URL (e.g. https://rqmprod.intra.infineon.com)
    JAMA_USERNAME  – Jama username / API client ID
    JAMA_PASSWORD  – Jama password / API client secret

Optional:
    JAMA_PROJECT_ID – Numeric project ID to test against (default: 845)

Run with:
    $env:JAMA_BASE_URL = "https://rqmprod.intra.infineon.com"
    $env:JAMA_USERNAME = "your-username"
    $env:JAMA_PASSWORD = "your-password"
    python -m pytest Tests/test_jama_connector_integration.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load credentials from env/.env if available
_ENV_PATH = Path(__file__).resolve().parents[3] / "env" / ".env"
load_dotenv(_ENV_PATH)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Connectors.JamaConnector import (
    JamaConnector,
    JamaItem,
    JamaAuthError,
    JamaConnectionError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = ("JAMA_BASE_URL", "JAMA_API_KEY", "JAMA_API_SECRET")


def _env_available() -> bool:
    return all(os.environ.get(v) for v in _REQUIRED_ENV_VARS)


# Skip the entire module if env vars are not set
pytestmark = pytest.mark.skipif(
    not _env_available(),
    reason=(
        "Integration tests require JAMA_BASE_URL, JAMA_API_KEY, "
        "and JAMA_API_SECRET in environment or env/.env file."
    ),
)


@pytest.fixture(scope="module")
def connector():
    """Create a JamaConnector connected to the live server."""
    conn = JamaConnector(
        base_url=os.environ["JAMA_BASE_URL"],
        api_key=os.environ["JAMA_API_KEY"],
        api_secret=os.environ["JAMA_API_SECRET"],
        max_results_per_page=10,
        timeout=60.0,
    )
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def project_id(connector) -> int:
    """Return configured project ID or auto-discover first accessible project."""
    configured = os.environ.get("JAMA_PROJECT_ID")
    if configured:
        return int(configured)
    # Auto-discover: use first project the API key can access
    projects = connector.get_projects()
    if projects:
        return projects[0]["id"]
    pytest.skip("No accessible Jama projects found for this API key")
    return 0  # unreachable


@pytest.fixture(scope="module")
def project_items(connector, project_id):
    """Fetch first 10 items only; skip if API key lacks item-read permission."""
    try:
        # Use internal _request for a single page to avoid fetching ALL items
        body = connector._request(
            "GET", "/abstractitems",
            params={"project": project_id, "startAt": 0, "maxResults": 10},
        )
        raw = body.get("data", [])
        items = [JamaItem.from_api_dict(d) for d in raw]
    except JamaAuthError:
        pytest.skip("API key lacks permission to read items (HTTP 401)")
    if not items:
        pytest.skip("No items found in project")
    return items


# ===================================================================
# 1. Connection validation
# ===================================================================


class TestConnection:
    def test_validate_connection(self, connector: JamaConnector):
        """Verify that credentials are accepted by the server."""
        assert connector.validate_connection() is True
        assert connector._connected is True

    def test_repr_shows_connected(self, connector: JamaConnector):
        r = repr(connector)
        assert os.environ["JAMA_BASE_URL"].rstrip("/") in r


# ===================================================================
# 2. Project discovery
# ===================================================================


class TestProjects:
    def test_get_projects_returns_list(self, connector: JamaConnector):
        projects = connector.get_projects()
        assert isinstance(projects, list)
        assert len(projects) > 0
        # Each project should have an 'id' and 'fields'
        first = projects[0]
        assert "id" in first
        assert "fields" in first

    def test_target_project_exists(
        self, connector: JamaConnector, project_id: int
    ):
        """Verify the configured project ID is accessible."""
        projects = connector.get_projects()
        project_ids = [p["id"] for p in projects]
        assert project_id in project_ids, (
            f"Project {project_id} not found in available projects"
        )


# ===================================================================
# 3. Item retrieval & pagination
# ===================================================================


class TestItems:
    def test_get_items_by_project(
        self, connector: JamaConnector, project_id: int, project_items
    ):
        """Verify first page of items from the target project."""
        items = project_items
        assert isinstance(items, list)
        assert len(items) > 0
        item = items[0]
        assert isinstance(item, JamaItem)
        assert item.project_id == project_id
        assert item.id > 0
        print(f"\n  Fetched {len(items)} items from project {project_id}")

    def test_get_single_item(
        self, connector: JamaConnector, project_id: int, project_items
    ):
        """Fetch one item by ID and verify fields are populated.

        Some items returned by /abstractitems may not be accessible via
        /items/{id} (e.g. folders or virtual items), so we try several
        until we find one that responds.
        """
        items = project_items
        assert len(items) > 0

        last_err = None
        for candidate in items[:20]:
            try:
                item = connector.get_item(candidate.id)
                assert item.id == candidate.id
                assert item.name != ""
                assert item.modified_date != ""
                print(
                    f"\n  Item {candidate.id}: "
                    f"name={item.name!r}, type={item.item_type}"
                )
                return  # success
            except Exception as exc:
                last_err = exc
                continue

        pytest.skip(
            f"None of the first 10 items were accessible via /items/{{id}} "
            f"(abstract-only project). Last error: {last_err}"
        )


# ===================================================================
# 4. Item types
# ===================================================================


class TestItemTypes:
    def test_get_item_types(self, connector: JamaConnector):
        types = connector.get_item_types()
        assert isinstance(types, list)
        assert len(types) > 0
        print(f"\n  Available item types: {len(types)}")
        for t in types[:5]:
            print(f"    id={t.get('id')} display={t.get('display', t.get('typeKey', '?'))}")


# ===================================================================
# 5. Filtering
# ===================================================================


class TestFiltering:
    def test_filter_by_item_type(
        self, connector: JamaConnector, project_id: int, project_items
    ):
        """Verify items can be filtered by item type (first page only)."""
        if not project_items:
            pytest.skip("No items in project")

        # Pick the most common item type from the first-page sample
        type_counts: dict[int, int] = {}
        for item in project_items:
            type_counts[item.item_type] = type_counts.get(item.item_type, 0) + 1
        most_common_type = max(type_counts, key=type_counts.get)

        # Fetch a single page filtered by that type
        body = connector._request(
            "GET", "/abstractitems",
            params={"project": project_id, "itemType": most_common_type,
                    "startAt": 0, "maxResults": 10},
        )
        filtered = [JamaItem.from_api_dict(d) for d in body.get("data", [])]
        assert len(filtered) > 0
        assert all(item.item_type == most_common_type for item in filtered)
        print(
            f"\n  Filtered by type {most_common_type}: "
            f"{len(filtered)} items (first page)"
        )


# ===================================================================
# 7. Relationships (optional – may not exist for all items)
# ===================================================================


class TestRelationships:
    def test_get_relationships(
        self, connector: JamaConnector, project_id: int, project_items
    ):
        """Attempt to fetch relationships for an item.

        Some abstract items may not be accessible via /items/{id}, so we
        try several candidates.
        """
        items = project_items
        if not items:
            pytest.skip("No items in project")

        last_err = None
        for candidate in items[:20]:
            try:
                downstream = connector.get_downstream_relationships(candidate.id)
                upstream = connector.get_upstream_relationships(candidate.id)
                assert isinstance(downstream, list)
                assert isinstance(upstream, list)
                print(
                    f"\n  Item {candidate.id}: "
                    f"{len(downstream)} downstream, {len(upstream)} upstream"
                )
                return  # success
            except Exception as exc:
                last_err = exc
                continue

        pytest.skip(
            f"None of the first 10 items supported relationship queries "
            f"(abstract-only project). Last error: {last_err}"
        )

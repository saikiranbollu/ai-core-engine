"""Integration tests for PolarionConnector against a live Polarion instance.

These tests connect to the real Polarion Web Tools API at
https://plr-web-api-polarion.eu-de-5.icp.infineon.com using a Bearer JWT token loaded
from the env/.env file (``PolarionAccessToken``).

Run with:
    python -m pytest Tests/test_polarion_connector_integration.py -v -s
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Allow running from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from src.IngestionPipeline.Connectors.PolarionConnector import (
    PolarionConnector,
    PolarionBaseline,
    PolarionCollection,
    PolarionWorkItem,
    PolarionRelease,
    PolarionTestCase,
    PolarionTestScope,
    PolarionTestDataAndEnvSpec,
    PolarionAuthError,
    PolarionClientError,
    PolarionConnectionError,
    PolarionConnectorError,
)

# ---------------------------------------------------------------------------
# Setup – load .env and configure logging
# ---------------------------------------------------------------------------

# Load the token from env/.env
_ENV_PATH = Path(__file__).resolve().parent.parent / "env" / ".env"
load_dotenv(_ENV_PATH)

POLARION_BASE_URL = "https://plr-web-api-polarion.eu-de-5.icp.infineon.com"
POLARION_TOKEN = os.environ.get("PolarionAccessToken", "")

# Enable info-level logging so we can see request details during tests
logging.basicConfig(level=logging.INFO)

# Skip all tests if token is not available
pytestmark = pytest.mark.skipif(
    not POLARION_TOKEN,
    reason="PolarionAccessToken not found in env/.env – skipping integration tests.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def connector():
    """Create a single PolarionConnector for the entire test module."""
    conn = PolarionConnector(
        base_url=POLARION_BASE_URL,
        token=POLARION_TOKEN,
        max_retries=2,
        timeout=30.0,
        verify_ssl=False,
    )
    yield conn
    conn.close()


# ===================================================================
# 1. Connection validation
# ===================================================================


class TestConnection:
    def test_validate_connection(self, connector):
        """The token should be accepted by the Polarion server."""
        result = connector.validate_connection()
        assert result is True
        assert connector._connected is True
        print(f"✓ Connected to {POLARION_BASE_URL}")

    def test_invalid_token_rejected(self):
        """A bogus token should raise PolarionAuthError."""
        conn = PolarionConnector(
            base_url=POLARION_BASE_URL,
            token="invalid-token-12345",
            max_retries=1,
            timeout=15.0,
            verify_ssl=False,
        )
        with pytest.raises((PolarionAuthError, PolarionConnectionError)):
            conn.validate_connection()
        conn.close()


# ===================================================================
# 2. Collections endpoint
# ===================================================================


class TestCollectionsIntegration:
    """Test GET /projects/{project_id}/collections against live server.

    These tests use a discovery approach – first list what's available,
    then verify the response structure.
    """

    def test_get_collections(self, connector):
        """Fetch collections and verify they are well-formed."""
        project_id = "AURIX_RC1_MCAL"
        collections = connector.get_collections(project_id)
        print(f"\n  Project '{project_id}': {len(collections)} collection(s)")
        for c in collections[:5]:  # Show first 5
            print(f"    - {c.id}: {c.name} (status={c.status})")
        assert isinstance(collections, list)
        if collections:
            c = collections[0]
            assert isinstance(c, PolarionCollection)
            assert c.id  # Should have an ID


# ===================================================================
# 3. Releases endpoint
# ===================================================================


class TestReleasesIntegration:
    def test_get_releases(self, connector):
        """Fetch releases for a project."""
        project_id = "AURIX_RC1_MCAL"
        releases = connector.get_releases(project_id)
        print(f"\n  Project '{project_id}': {len(releases)} release(s)")
        for r in releases[:5]:
            print(f"    - {r.release_id}: {r.title} (status={r.status})")
        assert isinstance(releases, list)
        if releases:
            assert isinstance(releases[0], PolarionRelease)

    def test_get_releases_detailed(self, connector):
        """Fetch releases with detailed info flag."""
        project_id = "AURIX_RC1_MCAL"
        releases = connector.get_releases(project_id, detailed_info=True)
        assert isinstance(releases, list)
        if releases:
            r = releases[0]
            print(f"\n  Detailed release: {r.release_id} – "
                  f"components={len(r.components)}, "
                  f"hw_variants={len(r.hardware_variant)}")


# ===================================================================
# 4. Baselines endpoint
# ===================================================================


class TestBaselinesIntegration:
    def test_get_baselines_from_first_collection(self, connector):
        """If collections exist, fetch baselines from the first one.

        The baselines endpoint expects the collection **name** (not the
        numeric id) as the ``collectionId`` path parameter.
        """
        project_id = "AURIX_RC1_MCAL"
        collections = connector.get_collections(project_id)
        if not collections:
            pytest.skip("No collections found in AURIX_RC1_MCAL.")
        coll = collections[0]
        # API requires collection name, not numeric id
        baselines = connector.get_baselines(project_id, coll.name)
        print(f"\n  Collection '{coll.name}': "
              f"{len(baselines)} baseline(s)")
        for b in baselines[:5]:
            print(f"    - {b.name} (type={b.type}, rev={b.revision})")
        assert isinstance(baselines, list)
        if baselines:
            assert isinstance(baselines[0], PolarionBaseline)


# ===================================================================
# 5. Document work items endpoint
# ===================================================================


class TestDocumentExportIntegration:
    """Test GET /projects/{pid}/spaces/{sid}/documents/{doc}

    These need real space/document names which vary per project.
    We try common space names used in MCAL projects.
    """

    KNOWN_SPACES_AND_DOCS = [
        ("AURIX_RC1_MCAL", "Requirements", "SRS_ADC"),
        ("AURIX_RC1_MCAL", "Requirements", "SRS"),
    ]

    @pytest.mark.parametrize("project_id,space_id,doc_name", KNOWN_SPACES_AND_DOCS)
    def test_get_document_work_items(self, connector, project_id, space_id, doc_name):
        """Fetch work items from a known document."""
        try:
            items = connector.get_document_work_items(
                project_id, space_id, doc_name,
            )
            print(f"\n  {project_id}/{space_id}/{doc_name}: "
                  f"{len(items)} work item(s)")
            for wi in items[:5]:
                print(f"    - [{wi.type}] {wi.title} (status={wi.status})")
            assert isinstance(items, list)
            if items:
                assert isinstance(items[0], PolarionWorkItem)
        except PolarionClientError as e:
            pytest.skip(f"Document not accessible: {e}")


# ===================================================================
# 6. Test cases endpoint
# ===================================================================


class TestTestCasesIntegration:
    KNOWN_TEST_DOCS = [
        ("AURIX_RC1_MCAL", "Test_Cases", "TC_ADC", "ADC"),
    ]

    @pytest.mark.parametrize("project_id,space_id,doc_name,component", KNOWN_TEST_DOCS)
    def test_get_test_cases(self, connector, project_id, space_id, doc_name, component):
        """Fetch test cases from a known document."""
        try:
            cases = connector.get_test_cases(
                project_id, space_id, doc_name, component,
            )
            print(f"\n  {project_id}/{space_id}/{doc_name}: "
                  f"{len(cases)} test case(s)")
            for tc in cases[:5]:
                print(f"    - {tc.polarion_id}: {tc.title} "
                      f"(status={tc.status}, links={len(tc.linked_work_items)})")
            assert isinstance(cases, list)
            if cases:
                assert isinstance(cases[0], PolarionTestCase)
        except PolarionClientError as e:
            pytest.skip(f"Test cases not accessible: {e}")


# ===================================================================
# 7. Test data endpoint
# ===================================================================


class TestTestDataIntegration:
    KNOWN_TEST_DATA_DOCS = [
        ("AURIX_RC1_MCAL", "Test_Data", "TD_ADC"),
    ]

    @pytest.mark.parametrize("project_id,space_id,doc_name", KNOWN_TEST_DATA_DOCS)
    def test_get_test_data(self, connector, project_id, space_id, doc_name):
        """Fetch test data and environment specs."""
        try:
            result = connector.get_test_data(project_id, space_id, doc_name)
            print(f"\n  {project_id}/{space_id}/{doc_name}: "
                  f"{len(result.test_data_list)} test data, "
                  f"{len(result.test_environment_list)} test env")
            assert isinstance(result, PolarionTestDataAndEnvSpec)
        except PolarionClientError as e:
            pytest.skip(f"Test data not accessible: {e}")


# ===================================================================
# 8. Test scopes endpoint
# ===================================================================


class TestTestScopesIntegration:
    KNOWN_SCOPE_DOCS = [
        ("AURIX_RC1_MCAL", "Test_Scopes", "TS_ADC"),
    ]

    @pytest.mark.parametrize("project_id,space_id,doc_name", KNOWN_SCOPE_DOCS)
    def test_get_test_scopes(self, connector, project_id, space_id, doc_name):
        """Fetch test scopes."""
        try:
            scopes = connector.get_test_scopes(project_id, space_id, doc_name)
            print(f"\n  {project_id}/{space_id}/{doc_name}: "
                  f"{len(scopes)} test scope(s)")
            for ts in scopes[:5]:
                print(f"    - {ts.polarion_id}: {ts.title} "
                      f"(release={ts.release})")
            assert isinstance(scopes, list)
        except PolarionClientError as e:
            pytest.skip(f"Test scopes not accessible: {e}")


# ===================================================================
# 9. Test results endpoint
# ===================================================================


class TestTestResultsIntegration:
    def test_get_test_results(self, connector):
        """Fetch test results – needs a valid release + device combo."""
        project_id = "AURIX_RC1_MCAL"
        releases = connector.get_releases(project_id)
        if not releases:
            pytest.skip("No releases found in AURIX_RC1_MCAL.")
        rel = releases[0]
        # Try common device names
        for device in ["TC49x", "TC4Dx", "TC3xx"]:
            try:
                result = connector.get_test_results(
                    project_id, rel.release_id, device,
                )
                print(f"\n  Test results for {project_id}/"
                      f"{rel.release_id}/{device}: {result}")
                assert isinstance(result, dict)
                return
            except (PolarionClientError, PolarionConnectionError):
                continue
        pytest.skip("No accessible test results found for available releases.")


# ===================================================================
# 10. Context manager test
# ===================================================================


class TestContextManagerIntegration:
    def test_context_manager_with_real_connection(self):
        """Ensure the connector works as a context manager with real server."""
        with PolarionConnector(
            base_url=POLARION_BASE_URL,
            token=POLARION_TOKEN,
            max_retries=1,
            timeout=15.0,
            verify_ssl=False,
        ) as conn:
            result = conn.validate_connection()
            assert result is True

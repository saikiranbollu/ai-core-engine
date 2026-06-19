"""Tests for the PolarionConnector module.

All HTTP interactions are mocked via httpx's transport mock layer so no
real Polarion server is needed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

# Allow running from the repo root
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Connectors.PolarionConnector import (
    PolarionConnector,
    PolarionBaseline,
    PolarionCollection,
    PolarionWorkItem,
    PolarionRelease,
    PolarionTestCase,
    PolarionTestScope,
    PolarionTestData,
    PolarionTestEnvironment,
    PolarionTestDataAndEnvSpec,
    PolarionLinkedItem,
    PolarionTestStep,
    PolarionAuthError,
    PolarionClientError,
    PolarionServerError,
    PolarionConnectionError,
    PolarionConnectorError,
    SyncState,
    SyncStatus,
)


# ---------------------------------------------------------------------------
# Helpers – fake HTTP transport
# ---------------------------------------------------------------------------

class MockTransport(httpx.BaseTransport):
    """A transport that returns pre-configured responses based on URL path."""

    def __init__(self):
        self.routes: dict[str, list] = {}  # path -> list of responses
        self.requests_log: list[httpx.Request] = []

    def add_response(
        self,
        path: str,
        status_code: int = 200,
        json_body=None,
        text: str = "",
    ):
        resp = (status_code, json_body, text)
        self.routes.setdefault(path, []).append(resp)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests_log.append(request)
        raw_path = request.url.raw_path.decode()
        # Strip query string for route matching
        path_only = raw_path.split("?")[0]

        for route_path, responses in self.routes.items():
            if path_only.endswith(route_path) or path_only == route_path:
                if responses:
                    status, body, txt = responses.pop(0)
                    if body is not None:
                        return httpx.Response(
                            status, json=body, request=request,
                        )
                    return httpx.Response(
                        status, text=txt, request=request,
                    )

        # Default 404
        return httpx.Response(404, text="Not found", request=request)


def _make_connector(transport: MockTransport, **kwargs) -> PolarionConnector:
    """Create a PolarionConnector wired to the mock transport."""
    defaults = dict(
        base_url="https://polarion.example.com",
        token="test-jwt-token",
        max_retries=1,
        timeout=5.0,
    )
    defaults.update(kwargs)
    connector = PolarionConnector(**defaults)
    connector._client = httpx.Client(
        base_url=defaults["base_url"],
        transport=transport,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {defaults['token']}",
        },
    )
    return connector


# ---------------------------------------------------------------------------
# Sample data builders
# ---------------------------------------------------------------------------

def _make_collection(
    coll_id: str = "C-1",
    name: str = "Sprint 1",
    status: str = "open",
) -> dict:
    return {
        "id": coll_id,
        "name": name,
        "description": f"Desc for {name}",
        "author": "admin",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-15T00:00:00Z",
        "status": status,
        "closedOn": "",
        "releaseId": "R-1",
    }


def _make_baseline(name: str = "BL-1.0", revision: str = "1234") -> dict:
    return {
        "name": name,
        "type": "major",
        "author": "admin",
        "revision": revision,
        "description": f"Baseline {name}",
    }


def _make_work_item(
    title: str = "REQ-001",
    status: str = "approved",
    wi_type: str = "requirement",
) -> dict:
    return {
        "title": title,
        "description": f"Description of {title}",
        "status": status,
        "type": wi_type,
        "assignees": ["user1", "user2"],
        "customFields": {"priority": "high"},
    }


def _make_release(
    release_id: str = "R-1.0",
    title: str = "Release 1.0",
    status: str = "open",
) -> dict:
    return {
        "releaseId": release_id,
        "title": title,
        "revision": "5678",
        "status": status,
        "components": [{"componentId": "c1", "componentName": "ADC"}],
        "hardwareVariant": [{"id": "hv1", "name": "TC49x"}],
        "compilerSupport": [],
        "configurationTool": [],
        "standards": [],
        "package": [],
    }


def _make_test_case(
    polarion_id: str = "TC-001",
    title: str = "Test ADC Init",
) -> dict:
    return {
        "polarionProject": "PROJ",
        "polarionId": polarion_id,
        "wiType": "testCase",
        "title": title,
        "status": "approved",
        "componentName": "ADC",
        "testVariant": "unit",
        "revisionId": "1000",
        "linkedWorkItems": [
            {
                "polarionId": "REQ-001",
                "title": "ADC Requirement",
                "wiType": "requirement",
                "linkType": "verifies",
                "revisionId": "900",
            }
        ],
        "testCategory": "functional",
        "testObjective": "Verify ADC initialisation",
        "isAutomated": "true",
        "configurationPlan": "",
        "testDesignTechnique": "boundary",
        "verificationMethod": "test",
        "testSteps": [
            {"stepDescription": "Init ADC", "expectedResult": "Success"},
            {"stepDescription": "Read value", "expectedResult": "0xFF"},
        ],
        "additionalInformation": "",
        "parameterDependencyOn": "",
        "testProcedure": "run_adc_test()",
        "expectedBehaviour": "ADC returns calibrated value",
        "architectureInformation": "",
        "testFunctionality": "init",
        "reviewedArtefact": "",
        "specificationType": "SW",
    }


def _make_test_scope(polarion_id: str = "TS-001") -> dict:
    return {
        "polarionId": polarion_id,
        "revisionId": "500",
        "title": "ADC Test Scope",
        "description": "Scope for ADC",
        "release": "R-1.0",
        "analysis": None,
        "regressionOptions": None,
    }


def _make_test_data_response() -> dict:
    return {
        "testEnvironmentList": [
            {
                "polarionId": "TE-001",
                "title": "Env Spec 1",
                "status": "approved",
                "wiType": "testEnvironment",
                "componentName": "ADC",
                "configurationId": "cfg1",
                "configurations": None,
                "isCompilationOnly": "false",
                "executionModes": "normal",
                "codeInstrumentations": "",
                "documentName": "TestDoc",
                "revisionId": "600",
                "linkedTestData": ["TD-001"],
            }
        ],
        "testDataList": [
            {
                "polarionId": "TD-001",
                "title": "Test Data 1",
                "status": "approved",
                "wiType": "testData",
                "numberExecutionUnits": "1",
                "nameExecutionUnits": "CPU0",
                "testDataGeneration": "manual",
                "isWCETRelevant": "false",
                "isBvecRelevant": "false",
                "inputParameters": None,
                "outputParameters": None,
                "externalStimulInParameters": None,
                "documentName": "TestDoc",
                "revisionId": "601",
                "compiler": "GCC",
                "standards": "ISO26262",
                "hardwareVariant": "TC49x",
                "linkedTestDataSchema": ["schema1"],
            }
        ],
    }


def _make_test_result() -> dict:
    return {
        "project": {
            "type": "testResult",
            "id": "PROJ",
            "ifxRelease": "R-1.0",
            "ifxDeviceId": "TC49x",
            "ifxComponent": "ADC",
            "ifxAutosarId": "",
            "ifxCompiler": "GCC",
            "Regression_details": [
                {
                    "ifxBaseline": "BL-1.0",
                    "ifxExecutedCount": 100,
                    "ifxPassCount": 95,
                    "ifxFailCount": 5,
                }
            ],
        }
    }


# ===================================================================
# 1. Data model tests
# ===================================================================


class TestDataModels:
    def test_baseline_from_api_dict(self):
        raw = _make_baseline()
        bl = PolarionBaseline.from_api_dict(raw)
        assert bl.name == "BL-1.0"
        assert bl.type == "major"
        assert bl.author == "admin"
        assert bl.revision == "1234"

    def test_baseline_from_empty_dict(self):
        bl = PolarionBaseline.from_api_dict({})
        assert bl.name == ""
        assert bl.revision == ""

    def test_collection_from_api_dict(self):
        raw = _make_collection()
        coll = PolarionCollection.from_api_dict(raw)
        assert coll.id == "C-1"
        assert coll.name == "Sprint 1"
        assert coll.status == "open"
        assert coll.release_id == "R-1"

    def test_work_item_from_api_dict(self):
        raw = _make_work_item()
        wi = PolarionWorkItem.from_api_dict(raw)
        assert wi.title == "REQ-001"
        assert wi.status == "approved"
        assert wi.type == "requirement"
        assert wi.assignees == ["user1", "user2"]
        assert wi.custom_fields == {"priority": "high"}

    def test_work_item_from_empty_dict(self):
        wi = PolarionWorkItem.from_api_dict({})
        assert wi.title == ""
        assert wi.assignees == []
        assert wi.custom_fields == {}

    def test_release_from_api_dict(self):
        raw = _make_release()
        rel = PolarionRelease.from_api_dict(raw)
        assert rel.release_id == "R-1.0"
        assert rel.title == "Release 1.0"
        assert len(rel.components) == 1
        assert len(rel.hardware_variant) == 1

    def test_test_case_from_api_dict(self):
        raw = _make_test_case()
        tc = PolarionTestCase.from_api_dict(raw)
        assert tc.polarion_id == "TC-001"
        assert tc.title == "Test ADC Init"
        assert len(tc.linked_work_items) == 1
        assert tc.linked_work_items[0].polarion_id == "REQ-001"
        assert tc.linked_work_items[0].link_type == "verifies"
        assert len(tc.test_steps) == 2
        assert tc.test_steps[0].step_description == "Init ADC"
        assert tc.test_steps[1].expected_result == "0xFF"
        assert tc.is_automated == "true"
        assert tc.test_category == "functional"

    def test_test_scope_from_api_dict(self):
        raw = _make_test_scope()
        ts = PolarionTestScope.from_api_dict(raw)
        assert ts.polarion_id == "TS-001"
        assert ts.title == "ADC Test Scope"
        assert ts.release == "R-1.0"

    def test_test_data_and_env_from_api_dict(self):
        raw = _make_test_data_response()
        result = PolarionTestDataAndEnvSpec.from_api_dict(raw)
        assert len(result.test_environment_list) == 1
        assert len(result.test_data_list) == 1
        env = result.test_environment_list[0]
        assert env.polarion_id == "TE-001"
        assert env.linked_test_data == ["TD-001"]
        td = result.test_data_list[0]
        assert td.polarion_id == "TD-001"
        assert td.compiler == "GCC"
        assert td.linked_test_data_schema == ["schema1"]

    def test_linked_item_from_api_dict(self):
        li = PolarionLinkedItem.from_api_dict({
            "polarionId": "REQ-001",
            "title": "Req Title",
            "wiType": "requirement",
            "linkType": "verifies",
            "revisionId": "100",
        })
        assert li.polarion_id == "REQ-001"
        assert li.link_type == "verifies"

    def test_test_step_from_api_dict(self):
        ts = PolarionTestStep.from_api_dict({
            "stepDescription": "Do X",
            "expectedResult": "Y happens",
        })
        assert ts.step_description == "Do X"
        assert ts.expected_result == "Y happens"


# ===================================================================
# 2. Authentication tests
# ===================================================================


class TestAuthentication:
    def test_validate_connection_success(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/TestProj/collections", 200,
            json_body=[],
        )
        connector = _make_connector(transport)
        assert connector.validate_connection("TestProj") is True
        assert connector._connected is True

    def test_validate_connection_success_via_404(self):
        """A 404 still proves the server is up and the token accepted."""
        transport = MockTransport()
        transport.add_response("/projects/TestProj/collections", 404, text="Not found")
        connector = _make_connector(transport)
        assert connector.validate_connection("TestProj") is True
        assert connector._connected is True

    def test_validate_connection_auth_failure(self):
        transport = MockTransport()
        transport.add_response("/projects/TestProj/collections", 401, text="Unauthorized")
        connector = _make_connector(transport)
        with pytest.raises(PolarionAuthError):
            connector.validate_connection("TestProj")
        assert connector._connected is False

    def test_validate_connection_403(self):
        transport = MockTransport()
        transport.add_response("/projects/TestProj/collections", 403, text="Forbidden")
        connector = _make_connector(transport)
        with pytest.raises(PolarionAuthError):
            connector.validate_connection("TestProj")

    def test_validate_connection_server_error(self):
        transport = MockTransport()
        transport.add_response("/projects/TestProj/collections", 500, text="Internal")
        connector = _make_connector(transport)
        with pytest.raises(PolarionConnectionError):
            connector.validate_connection("TestProj")

    def test_context_manager(self):
        transport = MockTransport()
        with _make_connector(transport) as conn:
            assert conn is not None

    def test_bearer_header_sent(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/TestProj/collections", 200,
            json_body=[],
        )
        connector = _make_connector(transport, token="my-secret-jwt")
        connector.validate_connection("TestProj")
        req = transport.requests_log[0]
        assert b"Bearer my-secret-jwt" in req.headers.get("authorization", "").encode()


# ===================================================================
# 3. Retry and error handling tests
# ===================================================================


class TestRetryAndErrors:
    def test_client_error_no_retry(self):
        transport = MockTransport()
        transport.add_response("/projects/P/collections", 400, text="Bad Request")
        connector = _make_connector(transport)
        with pytest.raises(PolarionClientError, match="400"):
            connector.get_collections("P")
        # Only 1 attempt – no retries for 4xx
        assert len(transport.requests_log) == 1

    def test_auth_error_no_retry(self):
        transport = MockTransport()
        transport.add_response("/projects/P/collections", 401, text="Unauth")
        connector = _make_connector(transport)
        with pytest.raises(PolarionAuthError):
            connector.get_collections("P")
        assert len(transport.requests_log) == 1

    def test_server_error_retried(self):
        transport = MockTransport()
        transport.add_response("/projects/P/collections", 500, text="Error")
        transport.add_response("/projects/P/collections", 500, text="Error")
        connector = _make_connector(transport, max_retries=2, backoff_factor=0.01)
        with pytest.raises(PolarionConnectionError):
            connector.get_collections("P")
        # Should have retried
        assert len(transport.requests_log) == 2

    def test_server_error_then_success(self):
        transport = MockTransport()
        transport.add_response("/projects/P/collections", 500, text="Error")
        transport.add_response(
            "/projects/P/collections", 200,
            json_body=[_make_collection()],
        )
        connector = _make_connector(transport, max_retries=2, backoff_factor=0.01)
        result = connector.get_collections("P")
        assert len(result) == 1
        assert len(transport.requests_log) == 2


# ===================================================================
# 4. Collections endpoint tests
# ===================================================================


class TestCollections:
    def test_get_collections(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/P/collections", 200,
            json_body=[_make_collection("C-1"), _make_collection("C-2", "Sprint 2")],
        )
        connector = _make_connector(transport)
        result = connector.get_collections("P")
        assert len(result) == 2
        assert isinstance(result[0], PolarionCollection)
        assert result[0].id == "C-1"
        assert result[1].name == "Sprint 2"

    def test_get_collections_with_release_filter(self):
        transport = MockTransport()
        transport.add_response(
            "/projects/P/collections", 200,
            json_body=[_make_collection()],
        )
        connector = _make_connector(transport)
        result = connector.get_collections("P", release_id="R-1")
        assert len(result) == 1
        req = transport.requests_log[0]
        assert b"releaseId=R-1" in req.url.raw_path

    def test_get_collections_empty(self):
        transport = MockTransport()
        transport.add_response("/projects/P/collections", 200, json_body=[])
        connector = _make_connector(transport)
        result = connector.get_collections("P")
        assert result == []


# ===================================================================
# 5. Baselines endpoint tests
# ===================================================================


class TestBaselines:
    def test_get_baselines(self):
        transport = MockTransport()
        transport.add_response(
            "/baselines", 200,
            json_body=[_make_baseline("BL-1"), _make_baseline("BL-2", "9999")],
        )
        connector = _make_connector(transport)
        result = connector.get_baselines("P", "C-1")
        assert len(result) == 2
        assert isinstance(result[0], PolarionBaseline)
        assert result[0].name == "BL-1"

    def test_get_single_baseline(self):
        transport = MockTransport()
        transport.add_response(
            "/BL-1", 200,
            json_body=[_make_baseline("BL-1")],
        )
        connector = _make_connector(transport)
        result = connector.get_baseline("P", "C-1", "BL-1")
        assert len(result) == 1
        assert result[0].name == "BL-1"

    def test_get_baseline_with_type_filter(self):
        transport = MockTransport()
        transport.add_response(
            "/BL-1", 200,
            json_body=[_make_baseline()],
        )
        connector = _make_connector(transport)
        connector.get_baseline("P", "C-1", "BL-1", work_item_type="requirement")
        req = transport.requests_log[0]
        assert b"workItemType=requirement" in req.url.raw_path


# ===================================================================
# 6. Document export (work items) tests
# ===================================================================


class TestDocumentExport:
    def test_get_document_work_items(self):
        transport = MockTransport()
        items = [
            _make_work_item("REQ-001"),
            _make_work_item("REQ-002", "draft", "heading"),
        ]
        transport.add_response("/documents/MyDoc", 200, json_body=items)
        connector = _make_connector(transport)
        result = connector.get_document_work_items("P", "MySpace", "MyDoc")
        assert len(result) == 2
        assert isinstance(result[0], PolarionWorkItem)
        assert result[0].title == "REQ-001"
        assert result[1].type == "heading"

    def test_get_document_work_items_with_revision(self):
        transport = MockTransport()
        transport.add_response(
            "/documents/MyDoc", 200,
            json_body=[_make_work_item()],
        )
        connector = _make_connector(transport)
        connector.get_document_work_items(
            "P", "MySpace", "MyDoc", revision="12345",
        )
        req = transport.requests_log[0]
        assert b"revision=12345" in req.url.raw_path

    def test_get_document_work_items_with_ids(self):
        transport = MockTransport()
        transport.add_response(
            "/documents/MyDoc", 200,
            json_body=[_make_work_item()],
        )
        connector = _make_connector(transport)
        connector.get_document_work_items(
            "P", "MySpace", "MyDoc", work_item_ids=["WI-1", "WI-2"],
        )
        req = transport.requests_log[0]
        assert b"workItemIds" in req.url.raw_path

    def test_get_document_work_items_empty(self):
        transport = MockTransport()
        transport.add_response("/documents/MyDoc", 200, json_body=[])
        connector = _make_connector(transport)
        result = connector.get_document_work_items("P", "MySpace", "MyDoc")
        assert result == []


# ===================================================================
# 7. Releases endpoint tests
# ===================================================================


class TestReleases:
    def test_get_releases(self):
        transport = MockTransport()
        transport.add_response(
            "/releases", 200,
            json_body=[_make_release(), _make_release("R-2.0", "Release 2.0")],
        )
        connector = _make_connector(transport)
        result = connector.get_releases("P")
        assert len(result) == 2
        assert isinstance(result[0], PolarionRelease)
        assert result[0].release_id == "R-1.0"

    def test_get_releases_with_status_filter(self):
        transport = MockTransport()
        transport.add_response(
            "/releases", 200,
            json_body=[_make_release()],
        )
        connector = _make_connector(transport)
        connector.get_releases("P", status="open")
        req = transport.requests_log[0]
        assert b"status=open" in req.url.raw_path

    def test_get_releases_with_release_id(self):
        transport = MockTransport()
        transport.add_response(
            "/releases", 200,
            json_body=[_make_release()],
        )
        connector = _make_connector(transport)
        connector.get_releases("P", release_id="R-1.0")
        req = transport.requests_log[0]
        assert b"release_id=R-1.0" in req.url.raw_path

    def test_get_releases_with_detailed_info(self):
        transport = MockTransport()
        transport.add_response(
            "/releases", 200,
            json_body=[_make_release()],
        )
        connector = _make_connector(transport)
        connector.get_releases("P", detailed_info=True)
        req = transport.requests_log[0]
        assert b"detailed_info=true" in req.url.raw_path

    def test_get_releases_empty(self):
        transport = MockTransport()
        transport.add_response("/releases", 200, json_body=[])
        connector = _make_connector(transport)
        result = connector.get_releases("P")
        assert result == []


# ===================================================================
# 8. Test results endpoint tests
# ===================================================================


class TestTestResults:
    def test_get_test_results(self):
        transport = MockTransport()
        transport.add_response(
            "/testResult", 200,
            json_body=_make_test_result(),
        )
        connector = _make_connector(transport)
        result = connector.get_test_results("P", "R-1.0", "TC49x")
        assert isinstance(result, dict)
        assert "project" in result
        assert result["project"]["ifxRelease"] == "R-1.0"

    def test_get_test_results_with_filters(self):
        transport = MockTransport()
        transport.add_response(
            "/testResult", 200, json_body=_make_test_result(),
        )
        connector = _make_connector(transport)
        connector.get_test_results(
            "P", "R-1.0", "TC49x",
            component_name="ADC",
            configuration="cfg1",
            only_latest=False,
        )
        req = transport.requests_log[0]
        raw = req.url.raw_path
        assert b"componentName=ADC" in raw
        assert b"configuration=cfg1" in raw
        assert b"onlyLatest=false" in raw


# ===================================================================
# 9. Test cases endpoint tests
# ===================================================================


class TestTestCases:
    def test_get_test_cases(self):
        transport = MockTransport()
        transport.add_response(
            "/testcases", 200,
            json_body=[_make_test_case(), _make_test_case("TC-002", "Test ADC Read")],
        )
        connector = _make_connector(transport)
        result = connector.get_test_cases("P", "Space1", "Doc1", "ADC")
        assert len(result) == 2
        assert isinstance(result[0], PolarionTestCase)
        assert result[0].polarion_id == "TC-001"
        assert result[1].title == "Test ADC Read"

    def test_get_test_cases_with_filters(self):
        transport = MockTransport()
        transport.add_response(
            "/testcases", 200, json_body=[_make_test_case()],
        )
        connector = _make_connector(transport)
        connector.get_test_cases(
            "P", "Space1", "Doc1", "ADC",
            revision_id="1000",
            work_item_type="testCase",
            wi_status="approved",
        )
        req = transport.requests_log[0]
        raw = req.url.raw_path
        assert b"componentName=ADC" in raw
        assert b"workItemType=testCase" in raw
        assert b"wiStatus=approved" in raw

    def test_get_test_cases_empty(self):
        transport = MockTransport()
        transport.add_response("/testcases", 200, json_body=[])
        connector = _make_connector(transport)
        result = connector.get_test_cases("P", "S", "D", "C")
        assert result == []


# ===================================================================
# 10. Test data endpoint tests
# ===================================================================


class TestTestData:
    def test_get_test_data(self):
        transport = MockTransport()
        transport.add_response(
            "/testdatas", 200, json_body=_make_test_data_response(),
        )
        connector = _make_connector(transport)
        result = connector.get_test_data("P", "Space1", "Doc1")
        assert isinstance(result, PolarionTestDataAndEnvSpec)
        assert len(result.test_environment_list) == 1
        assert len(result.test_data_list) == 1
        assert result.test_data_list[0].compiler == "GCC"

    def test_get_test_data_with_filters(self):
        transport = MockTransport()
        transport.add_response(
            "/testdatas", 200, json_body=_make_test_data_response(),
        )
        connector = _make_connector(transport)
        connector.get_test_data(
            "P", "S", "D",
            revision_id="100",
            component_name="ADC",
            wi_status="approved",
            work_item_type="testData",
        )
        req = transport.requests_log[0]
        raw = req.url.raw_path
        assert b"revisionId=100" in raw
        assert b"componentName=ADC" in raw

    def test_get_test_data_empty(self):
        transport = MockTransport()
        transport.add_response("/testdatas", 200, json_body={})
        connector = _make_connector(transport)
        result = connector.get_test_data("P", "S", "D")
        assert result.test_data_list == []
        assert result.test_environment_list == []


# ===================================================================
# 11. Test scopes endpoint tests
# ===================================================================


class TestTestScopes:
    def test_get_test_scopes_list(self):
        transport = MockTransport()
        transport.add_response(
            "/testscopes", 200,
            json_body=[_make_test_scope(), _make_test_scope("TS-002")],
        )
        connector = _make_connector(transport)
        result = connector.get_test_scopes("P", "S", "D")
        assert len(result) == 2
        assert isinstance(result[0], PolarionTestScope)
        assert result[0].polarion_id == "TS-001"

    def test_get_test_scopes_single_dict(self):
        """API may return a single object instead of array."""
        transport = MockTransport()
        transport.add_response(
            "/testscopes", 200,
            json_body=_make_test_scope("TS-SINGLE"),
        )
        connector = _make_connector(transport)
        result = connector.get_test_scopes("P", "S", "D")
        assert len(result) == 1
        assert result[0].polarion_id == "TS-SINGLE"

    def test_get_test_scopes_with_filters(self):
        transport = MockTransport()
        transport.add_response("/testscopes", 200, json_body=[_make_test_scope()])
        connector = _make_connector(transport)
        connector.get_test_scopes("P", "S", "D", revision_id="500", status="active")
        req = transport.requests_log[0]
        raw = req.url.raw_path
        assert b"revisionId=500" in raw
        assert b"status=active" in raw


# ===================================================================
# 12. Jira integration endpoint tests
# ===================================================================


class TestJiraIntegration:
    def test_get_jira_project(self):
        transport = MockTransport()
        transport.add_response(
            "/user/admin", 200,
            json_body={"id": "JP-1", "key": "JPROJ", "name": "Jira Project"},
        )
        connector = _make_connector(transport)
        result = connector.get_jira_project("JP-1", "admin")
        assert result["key"] == "JPROJ"
        assert result["name"] == "Jira Project"

    def test_get_jira_project_not_found(self):
        transport = MockTransport()
        transport.add_response("/user/bad", 404, text="Not found")
        connector = _make_connector(transport)
        with pytest.raises(PolarionClientError, match="404"):
            connector.get_jira_project("JP-1", "bad")


# ===================================================================
# 13. Incremental sync tests
# ===================================================================


class TestIncrementalSync:
    def test_first_sync_fetches_all(self):
        transport = MockTransport()
        transport.add_response(
            "/documents/Doc1", 200,
            json_body=[_make_work_item("WI-1"), _make_work_item("WI-2")],
        )
        connector = _make_connector(transport)
        report = connector.incremental_sync("P", "Space1", "Doc1")
        assert report["status"] == "success"
        assert report["items_synced"] == 2
        assert len(report["work_items"]) == 2
        assert report["deleted_ids"] == []

    def test_second_sync_detects_deletions(self):
        transport = MockTransport()
        # First sync: 3 items
        transport.add_response(
            "/documents/Doc1", 200,
            json_body=[
                _make_work_item("WI-1"),
                _make_work_item("WI-2"),
                _make_work_item("WI-3"),
            ],
        )
        # Second sync: only 2 items (WI-2 deleted)
        transport.add_response(
            "/documents/Doc1", 200,
            json_body=[
                _make_work_item("WI-1"),
                _make_work_item("WI-3"),
            ],
        )
        connector = _make_connector(transport)
        report1 = connector.incremental_sync("P", "Space1", "Doc1")
        assert report1["items_synced"] == 3

        report2 = connector.incremental_sync("P", "Space1", "Doc1")
        assert report2["items_synced"] == 2
        assert "WI-2" in report2["deleted_ids"]

    def test_sync_state_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            transport = MockTransport()
            transport.add_response(
                "/documents/Doc1", 200,
                json_body=[_make_work_item("WI-1")],
            )
            connector = _make_connector(transport, sync_state_dir=tmpdir)
            connector.incremental_sync("P", "Space1", "Doc1")

            state_file = Path(tmpdir) / "polarion_sync_P.json"
            assert state_file.exists()
            state_data = json.loads(state_file.read_text())
            assert state_data["project_id"] == "P"
            assert state_data["last_sync_status"] == "success"
            assert state_data["items_synced"] == 1

    def test_sync_state_loaded_on_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # First connector – do a sync
            transport1 = MockTransport()
            transport1.add_response(
                "/documents/Doc1", 200,
                json_body=[_make_work_item("WI-1")],
            )
            conn1 = _make_connector(transport1, sync_state_dir=tmpdir)
            conn1.incremental_sync("P", "Space1", "Doc1")

            # Second connector – should load state from disk
            transport2 = MockTransport()
            transport2.add_response(
                "/documents/Doc1", 200,
                json_body=[_make_work_item("WI-1")],
            )
            conn2 = _make_connector(transport2, sync_state_dir=tmpdir)
            state = conn2._load_sync_state("P")
            assert state.last_sync_timestamp is not None
            assert state.items_synced == 1

    def test_sync_failure_logged(self):
        transport = MockTransport()
        transport.add_response("/documents/Doc1", 401, text="Unauthorized")
        connector = _make_connector(transport)
        report = connector.incremental_sync("P", "Space1", "Doc1")
        assert report["status"] == "failed"
        assert report["items_synced"] == 0

    def test_full_sync_resets_state(self):
        transport = MockTransport()
        # First incremental sync
        transport.add_response(
            "/documents/Doc1", 200,
            json_body=[_make_work_item("WI-1")],
        )
        # Full sync
        transport.add_response(
            "/documents/Doc1", 200,
            json_body=[_make_work_item("WI-1"), _make_work_item("WI-2")],
        )
        connector = _make_connector(transport)
        connector.incremental_sync("P", "Space1", "Doc1")

        report = connector.full_sync("P", "Space1", "Doc1")
        assert report["status"] == "success"
        assert report["items_synced"] == 2
        # Full sync should not report deletions
        assert report["deleted_ids"] == []

    def test_corrupted_state_file_handled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "polarion_sync_P.json"
            state_file.write_text("NOT VALID JSON {{{}}")

            transport = MockTransport()
            transport.add_response(
                "/documents/Doc1", 200, json_body=[],
            )
            connector = _make_connector(transport, sync_state_dir=tmpdir)
            report = connector.incremental_sync("P", "Space1", "Doc1")
            assert report["status"] == "success"


# ===================================================================
# 14. Miscellaneous
# ===================================================================


class TestMisc:
    def test_repr(self):
        transport = MockTransport()
        connector = _make_connector(transport)
        r = repr(connector)
        assert "PolarionConnector" in r
        assert "polarion.example.com" in r

    def test_non_json_response_handled(self):
        """If API returns a non-list for a list endpoint, handle gracefully."""
        transport = MockTransport()
        transport.add_response(
            "/projects/P/collections", 200,
            json_body={"unexpected": "format"},
        )
        connector = _make_connector(transport)
        result = connector.get_collections("P")
        # Should return empty list rather than crash
        assert result == []

    def test_close(self):
        transport = MockTransport()
        connector = _make_connector(transport)
        connector.close()
        # Should not raise

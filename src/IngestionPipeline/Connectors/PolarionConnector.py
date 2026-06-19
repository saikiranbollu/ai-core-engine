"""Polarion REST API Connector for the AICE Ingestion Pipeline.

Implements AICE-ING-004 – Polarion Connector to fetch requirements,
work items, baselines, collections, releases, test cases, test data,
test scopes, and test results via the Polarion Web Tools REST API.

All 10 GET endpoints from the Polarion Web Tools Swagger specification
are implemented:

    1. GET /projects/{project_id}/collections
    2. GET /projects/{project_id}/collections/{collectionId}/baselines
    3. GET /projects/{project_id}/collections/{collectionId}/baselines/{baselineId}
    4. GET /projects/{project_id}/spaces/{spaceId}/documents/{documentName}
    5. GET /projects/{project_id}/releases
    6. GET /projects/{project_id}/testResult
    7. GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testcases
    8. GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testdatas
    9. GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testscopes
   10. GET /jira/projects/{jira_project_id}/user/{polarion_user}

Authentication uses Bearer JWT tokens as specified in the API's
``securitySchemes``.
"""

from __future__ import annotations

import json
import logging
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx

from ..config import get_max_workers
from src._common.path_safety import allowed_roots_from_env, safe_path_under
from src._common.tls_config import enforce_tls_policy


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("aice.ingestion.polarion")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PolarionConnectorError(Exception):
    """Base exception for all Polarion connector errors."""


class PolarionAuthError(PolarionConnectorError):
    """Raised when authentication with the Polarion API fails."""


class PolarionClientError(PolarionConnectorError):
    """Raised for client-side (4xx) HTTP errors."""


class PolarionServerError(PolarionConnectorError):
    """Raised for server-side (5xx) HTTP errors."""


class PolarionConnectionError(PolarionConnectorError):
    """Raised when a connection to the Polarion API cannot be established."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SyncStatus(str, Enum):
    """Status of an incremental sync operation."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class PolarionBaseline:
    """Represents a Polarion baseline."""
    name: str = ""
    type: str = ""
    author: str = ""
    revision: str = ""
    description: str = ""

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionBaseline":
        return cls(
            name=data.get("name", "") or "",
            type=data.get("type", "") or "",
            author=data.get("author", "") or "",
            revision=data.get("revision", "") or "",
            description=data.get("description", "") or "",
        )


@dataclass
class PolarionCollection:
    """Represents a Polarion collection."""
    id: str = ""
    name: str = ""
    description: str = ""
    author: str = ""
    created: str = ""
    updated: str = ""
    status: str = ""
    closed_on: str = ""
    release_id: str = ""

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionCollection":
        return cls(
            id=data.get("id", "") or "",
            name=data.get("name", "") or "",
            description=data.get("description", "") or "",
            author=data.get("author", "") or "",
            created=data.get("created", "") or "",
            updated=data.get("updated", "") or "",
            status=data.get("status", "") or "",
            closed_on=data.get("closedOn", "") or "",
            release_id=data.get("releaseId", "") or "",
        )


@dataclass
class PolarionWorkItem:
    """Represents a Polarion work item with custom fields.

    This corresponds to the ``WorkItemWithCustomFields`` schema in the
    Polarion Web Tools API – returned by the Document Export endpoint.
    """
    title: str = ""
    description: str = ""
    status: str = ""
    type: str = ""
    assignees: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionWorkItem":
        return cls(
            title=data.get("title", "") or "",
            description=data.get("description", "") or "",
            status=data.get("status", "") or "",
            type=data.get("type", "") or "",
            assignees=data.get("assignees") or [],
            custom_fields=data.get("customFields") or {},
        )


@dataclass
class PolarionRelease:
    """Represents a Polarion release."""
    release_id: str = ""
    title: str = ""
    revision: str = ""
    status: str = ""
    components: List[dict] = field(default_factory=list)
    hardware_variant: List[dict] = field(default_factory=list)
    compiler_support: List[dict] = field(default_factory=list)
    configuration_tool: List[dict] = field(default_factory=list)
    standards: List[dict] = field(default_factory=list)
    package: List[dict] = field(default_factory=list)

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionRelease":
        return cls(
            release_id=data.get("releaseId", "") or "",
            title=data.get("title", "") or "",
            revision=data.get("revision", "") or "",
            status=data.get("status", "") or "",
            components=data.get("components") or [],
            hardware_variant=data.get("hardwareVariant") or [],
            compiler_support=data.get("compilerSupport") or [],
            configuration_tool=data.get("configurationTool") or [],
            standards=data.get("standards") or [],
            package=data.get("package") or [],
        )


@dataclass
class PolarionLinkedItem:
    """A linked work item reference within test cases."""
    polarion_id: str = ""
    title: str = ""
    wi_type: str = ""
    link_type: str = ""
    revision_id: str = ""

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionLinkedItem":
        return cls(
            polarion_id=data.get("polarionId", "") or "",
            title=data.get("title", "") or "",
            wi_type=data.get("wiType", "") or "",
            link_type=data.get("linkType", "") or "",
            revision_id=data.get("revisionId", "") or "",
        )


@dataclass
class PolarionTestStep:
    """A single test step within a test case."""
    step_description: str = ""
    expected_result: str = ""

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestStep":
        return cls(
            step_description=data.get("stepDescription", "") or "",
            expected_result=data.get("expectedResult", "") or "",
        )


@dataclass
class PolarionTestCase:
    """Represents a Polarion test case."""
    polarion_project: str = ""
    polarion_id: str = ""
    wi_type: str = ""
    title: str = ""
    status: str = ""
    component_name: str = ""
    test_variant: str = ""
    revision_id: str = ""
    linked_work_items: List[PolarionLinkedItem] = field(default_factory=list)
    test_category: str = ""
    test_objective: str = ""
    is_automated: str = ""
    configuration_plan: str = ""
    test_design_technique: str = ""
    verification_method: str = ""
    test_steps: List[PolarionTestStep] = field(default_factory=list)
    additional_information: str = ""
    parameter_dependency_on: str = ""
    test_procedure: str = ""
    expected_behaviour: str = ""
    architecture_information: str = ""
    test_functionality: str = ""
    reviewed_artefact: str = ""
    specification_type: str = ""

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestCase":
        linked = [
            PolarionLinkedItem.from_api_dict(li)
            for li in (data.get("linkedWorkItems") or [])
        ]
        steps = [
            PolarionTestStep.from_api_dict(s)
            for s in (data.get("testSteps") or [])
        ]
        return cls(
            polarion_project=data.get("polarionProject", "") or "",
            polarion_id=data.get("polarionId", "") or "",
            wi_type=data.get("wiType", "") or "",
            title=data.get("title", "") or "",
            status=data.get("status", "") or "",
            component_name=data.get("componentName", "") or "",
            test_variant=data.get("testVariant", "") or "",
            revision_id=data.get("revisionId", "") or "",
            linked_work_items=linked,
            test_category=data.get("testCategory", "") or "",
            test_objective=data.get("testObjective", "") or "",
            is_automated=data.get("isAutomated", "") or "",
            configuration_plan=data.get("configurationPlan", "") or "",
            test_design_technique=data.get("testDesignTechnique", "") or "",
            verification_method=data.get("verificationMethod", "") or "",
            test_steps=steps,
            additional_information=data.get("additionalInformation", "") or "",
            parameter_dependency_on=data.get("parameterDependencyOn", "") or "",
            test_procedure=data.get("testProcedure", "") or "",
            expected_behaviour=data.get("expectedBehaviour", "") or "",
            architecture_information=data.get("architectureInformation", "") or "",
            test_functionality=data.get("testFunctionality", "") or "",
            reviewed_artefact=data.get("reviewedArtefact", "") or "",
            specification_type=data.get("specificationType", "") or "",
        )


@dataclass
class PolarionTestScope:
    """Represents a Polarion test scope."""
    polarion_id: str = ""
    revision_id: str = ""
    title: str = ""
    description: Any = None
    release: str = ""
    analysis: Any = None
    regression_options: Any = None

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestScope":
        return cls(
            polarion_id=data.get("polarionId", "") or "",
            revision_id=data.get("revisionId", "") or "",
            title=data.get("title", "") or "",
            description=data.get("description"),
            release=data.get("release", "") or "",
            analysis=data.get("analysis"),
            regression_options=data.get("regressionOptions"),
        )


@dataclass
class PolarionTestData:
    """Represents a Polarion test data item."""
    polarion_id: str = ""
    title: str = ""
    status: str = ""
    wi_type: str = ""
    number_execution_units: str = ""
    name_execution_units: str = ""
    test_data_generation: str = ""
    is_wcet_relevant: str = ""
    is_bvec_relevant: str = ""
    input_parameters: Any = None
    output_parameters: Any = None
    external_stimul_in_parameters: Any = None
    document_name: str = ""
    revision_id: str = ""
    compiler: str = ""
    standards: str = ""
    hardware_variant: str = ""
    linked_test_data_schema: List[str] = field(default_factory=list)

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestData":
        return cls(
            polarion_id=data.get("polarionId", "") or "",
            title=data.get("title", "") or "",
            status=data.get("status", "") or "",
            wi_type=data.get("wiType", "") or "",
            number_execution_units=data.get("numberExecutionUnits", "") or "",
            name_execution_units=data.get("nameExecutionUnits", "") or "",
            test_data_generation=data.get("testDataGeneration", "") or "",
            is_wcet_relevant=data.get("isWCETRelevant", "") or "",
            is_bvec_relevant=data.get("isBvecRelevant", "") or "",
            input_parameters=data.get("inputParameters"),
            output_parameters=data.get("outputParameters"),
            external_stimul_in_parameters=data.get("externalStimulInParameters"),
            document_name=data.get("documentName", "") or "",
            revision_id=data.get("revisionId", "") or "",
            compiler=data.get("compiler", "") or "",
            standards=data.get("standards", "") or "",
            hardware_variant=data.get("hardwareVariant", "") or "",
            linked_test_data_schema=data.get("linkedTestDataSchema") or [],
        )


@dataclass
class PolarionTestEnvironment:
    """Represents a Polarion test environment item."""
    polarion_id: str = ""
    title: str = ""
    status: str = ""
    wi_type: str = ""
    component_name: str = ""
    configuration_id: str = ""
    configurations: Any = None
    is_compilation_only: str = ""
    execution_modes: str = ""
    code_instrumentations: str = ""
    document_name: str = ""
    revision_id: str = ""
    linked_test_data: List[str] = field(default_factory=list)

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestEnvironment":
        return cls(
            polarion_id=data.get("polarionId", "") or "",
            title=data.get("title", "") or "",
            status=data.get("status", "") or "",
            wi_type=data.get("wiType", "") or "",
            component_name=data.get("componentName", "") or "",
            configuration_id=data.get("configurationId", "") or "",
            configurations=data.get("configurations"),
            is_compilation_only=data.get("isCompilationOnly", "") or "",
            execution_modes=data.get("executionModes", "") or "",
            code_instrumentations=data.get("codeInstrumentations", "") or "",
            document_name=data.get("documentName", "") or "",
            revision_id=data.get("revisionId", "") or "",
            linked_test_data=data.get("linkedTestData") or [],
        )


@dataclass
class PolarionTestDataAndEnvSpec:
    """Container for test data + test environment lists returned by the
    ``testdatas`` endpoint."""
    test_environment_list: List[PolarionTestEnvironment] = field(
        default_factory=list,
    )
    test_data_list: List[PolarionTestData] = field(default_factory=list)

    @classmethod
    def from_api_dict(cls, data: dict) -> "PolarionTestDataAndEnvSpec":
        envs = [
            PolarionTestEnvironment.from_api_dict(e)
            for e in (data.get("testEnvironmentList") or [])
        ]
        tds = [
            PolarionTestData.from_api_dict(d)
            for d in (data.get("testDataList") or [])
        ]
        return cls(test_environment_list=envs, test_data_list=tds)


@dataclass
class SyncState:
    """Tracks incremental sync state for a specific project.

    Persisted to disk so that subsequent runs only fetch items modified
    since the last successful sync.
    """
    project_id: str
    last_sync_timestamp: Optional[str] = None  # ISO-8601 UTC
    last_sync_status: SyncStatus = SyncStatus.SUCCESS
    items_synced: int = 0
    known_work_item_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------

class PolarionConnector:
    """Synchronous Polarion REST API connector with Bearer-token auth.

    This connector is designed for the HybridRAG ingestion pipeline.
    It wraps the Polarion Web Tools REST API (OAS3 v1) and provides:

      * Bearer JWT authentication.
      * All 10 GET endpoints from the Swagger specification.
      * Project filtering on every endpoint.
      * Incremental sync for work-item-based document exports.
      * Retry logic with exponential back-off (AICE-ING-010).
      * Structured logging (AICE-ING-011).

    Parameters
    ----------
    base_url : str
        Root URL of the Polarion instance
        (e.g. ``https://plr-web-api-polarion-qa.eu-de-5.icp.infineon.com``).
    token : str
        Bearer JWT token for authentication.
    max_retries : int
        Number of retry attempts for transient errors.
    backoff_factor : float
        Exponential back-off multiplier (seconds).
    timeout : float | None
        HTTP request timeout in seconds. ``None`` disables timeouts.
    verify_ssl : bool
        Whether to verify SSL certificates.
    sync_state_dir : str | Path | None
        Directory where sync-state JSON files are persisted. When ``None``
        sync state is kept in-memory only.
    """

    _DEFAULT_MAX_RETRIES = 3
    _DEFAULT_BACKOFF_FACTOR = 1.0

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        timeout: Optional[float] = 30.0,
        verify_ssl: bool = True,
        sync_state_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        verify_ssl = enforce_tls_policy(verify_ssl)
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._timeout = timeout

        ssl_context = ssl.create_default_context() if verify_ssl else False
        # Trailing '/' ensures httpx treats relative paths as appended
        # to the full base_url path (important when base_url has a
        # sub-path like /polarion).
        self._client = httpx.Client(
            base_url=self._base_url + "/",
            verify=ssl_context,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

        # Sync-state persistence (F-CF-X04: contain under an allowed root before
        # creating it; AICE_SYNC_STATE_ROOTS overrides the defaults).
        if sync_state_dir is not None:
            sync_roots = allowed_roots_from_env(
                "AICE_SYNC_STATE_ROOTS",
                ["/data/aice/sync_state", tempfile.gettempdir()],
            )
            self._sync_state_dir: Optional[Path] = safe_path_under(
                sync_state_dir, sync_roots
            )
            self._sync_state_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._sync_state_dir = None

        # In-memory sync state cache  {project_id: SyncState}
        self._sync_states: Dict[str, SyncState] = {}

        self._connected: bool = False
        self._max_workers = get_max_workers("connectors.polarion")
        logger.info(
            "PolarionConnector initialised (base_url=%s)",
            self._base_url,
        )

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> "PolarionConnector":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
        logger.debug("HTTP client closed.")

    # ------------------------------------------------------------------
    # Connection validation
    # ------------------------------------------------------------------
    def validate_connection(self, project_id: str = "AURIX_RC1_MCAL") -> bool:
        """Validate that the Bearer token is accepted by Polarion.

        Performs a lightweight ``GET /projects/{project_id}/collections``
        call.  Using a real project ID ensures the server checks the
        Bearer token (non-existent projects return 400 *without* auth
        validation on this API).

        Parameters
        ----------
        project_id : str
            A known Polarion project ID used for the probe request.

        Returns
        -------
        bool
            ``True`` if the connection is valid.

        Raises
        ------
        PolarionAuthError
            If authentication fails (HTTP 401 / 403).
        PolarionConnectionError
            If the server is unreachable.
        """
        t0 = time.perf_counter()
        try:
            # Use the collections endpoint on a real project so the
            # server actually validates the Bearer token.
            self._request("GET", f"/projects/{project_id}/collections")
            self._connected = True
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "Connection validated successfully (duration_ms=%.1f)",
                elapsed,
            )
            return True
        except PolarionAuthError:
            self._connected = False
            raise
        except PolarionClientError:
            # 4xx (e.g. 400 for a project the token can access but
            # that has no data) still means the server is reachable
            # and the token was accepted.
            self._connected = True
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "Connection validated successfully via 4xx probe "
                "(duration_ms=%.1f)",
                elapsed,
            )
            return True
        except PolarionConnectorError:
            self._connected = False
            raise
        except Exception as exc:
            self._connected = False
            raise PolarionConnectionError(
                f"Failed to connect to Polarion at {self._base_url}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Low-level HTTP with retries  (AICE-ING-010)
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        """Issue an HTTP request with retry / back-off and error handling.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, …).
        path : str
            API path relative to ``base_url``
            (e.g. ``/projects/MyProj/collections``).
        params : dict, optional
            Query parameters.
        json_body : dict, optional
            JSON body for POST / PATCH.

        Returns
        -------
        Any
            Parsed JSON response body (list or dict).

        Raises
        ------
        PolarionAuthError, PolarionClientError, PolarionServerError,
        PolarionConnectionError
        """
        last_exc: Optional[Exception] = None
        # Strip leading '/' so httpx treats the path as relative to
        # base_url (preserving any sub-path like /polarion).
        rel_path = path.lstrip("/")

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    rel_path,
                    params=params,
                    json=json_body,
                )

                # -- handle HTTP errors -----------------------------------
                if response.status_code in (401, 403):
                    raise PolarionAuthError(
                        f"Authentication failed (HTTP {response.status_code}). "
                        "Please verify your Bearer token."
                    )
                if 400 <= response.status_code < 500:
                    raise PolarionClientError(
                        f"Client error {response.status_code}: "
                        f"{response.text}"
                    )
                if response.status_code >= 500:
                    raise PolarionServerError(
                        f"Server error {response.status_code}: "
                        f"{response.text}"
                    )

                response.raise_for_status()
                return response.json()

            except (PolarionAuthError, PolarionClientError):
                # Non-retryable errors – propagate immediately
                raise

            except (httpx.RequestError, PolarionServerError) as exc:
                last_exc = exc
                wait = self._backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Request %s %s failed (attempt %d/%d): %s  – "
                    "retrying in %.1fs",
                    method, path, attempt, self._max_retries, exc, wait,
                )
                time.sleep(wait)

        # Exhausted retries
        logger.error(
            "Request %s %s failed after %d retries.",
            method, path, self._max_retries,
        )
        raise PolarionConnectionError(
            f"Request {method} {path} failed after "
            f"{self._max_retries} retries: {last_exc}"
        ) from last_exc

    # ==================================================================
    # GET endpoint 1: Collections
    # ==================================================================
    def get_collections(
        self,
        project_id: str,
        *,
        release_id: Optional[str] = None,
    ) -> List[PolarionCollection]:
        """Retrieve all collections for a project.

        ``GET /projects/{project_id}/collections``

        Parameters
        ----------
        project_id : str
            Polarion project ID.
        release_id : str, optional
            Filter collections by release.

        Returns
        -------
        list[PolarionCollection]
        """
        params: Dict[str, Any] = {}
        if release_id is not None:
            params["releaseId"] = release_id

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/collections",
            params=params or None,
        )
        items = [
            PolarionCollection.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_collections project_id=%s "
            "items_fetched=%d duration_ms=%.1f",
            project_id, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 2: Collection baselines
    # ==================================================================
    def get_baselines(
        self,
        project_id: str,
        collection_id: str,
    ) -> List[PolarionBaseline]:
        """Retrieve all baselines for a collection.

        ``GET /projects/{project_id}/collections/{collectionId}/baselines``

        Parameters
        ----------
        project_id : str
        collection_id : str

        Returns
        -------
        list[PolarionBaseline]
        """
        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/collections/{collection_id}/baselines",
        )
        items = [
            PolarionBaseline.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_baselines project_id=%s "
            "collection_id=%s items_fetched=%d duration_ms=%.1f",
            project_id, collection_id, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 3: Single baseline
    # ==================================================================
    def get_baseline(
        self,
        project_id: str,
        collection_id: str,
        baseline_id: str,
        *,
        work_item_type: Optional[str] = None,
    ) -> List[PolarionBaseline]:
        """Retrieve a specific baseline (optionally filtered by work item type).

        ``GET /projects/{project_id}/collections/{collectionId}/baselines/{baselineId}``

        Parameters
        ----------
        project_id : str
        collection_id : str
        baseline_id : str
        work_item_type : str, optional
            Filter by work item type.

        Returns
        -------
        list[PolarionBaseline]
            The API returns an array even for a single baseline ID.
        """
        params: Dict[str, Any] = {}
        if work_item_type is not None:
            params["workItemType"] = work_item_type

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/collections/{collection_id}"
            f"/baselines/{baseline_id}",
            params=params or None,
        )
        items = [
            PolarionBaseline.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_baseline project_id=%s "
            "collection_id=%s baseline_id=%s items_fetched=%d "
            "duration_ms=%.1f",
            project_id, collection_id, baseline_id, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 4: Document export (work items)
    # ==================================================================
    def get_document_work_items(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
        *,
        revision: Optional[str] = None,
        work_item_ids: Optional[List[str]] = None,
    ) -> List[PolarionWorkItem]:
        """Retrieve work items from a Polarion document.

        ``GET /projects/{project_id}/spaces/{spaceId}/documents/{documentName}``

        Parameters
        ----------
        project_id : str
        space_id : str
        document_name : str
        revision : str, optional
            Specific revision of the document.
        work_item_ids : list[str], optional
            Restrict to specific work item IDs.

        Returns
        -------
        list[PolarionWorkItem]
        """
        params: Dict[str, Any] = {}
        if revision is not None:
            params["revision"] = revision
        if work_item_ids is not None:
            params["workItemIds"] = work_item_ids

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/spaces/{space_id}"
            f"/documents/{document_name}",
            params=params or None,
        )
        items = [
            PolarionWorkItem.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_document_work_items "
            "project_id=%s space_id=%s document=%s items_fetched=%d "
            "duration_ms=%.1f",
            project_id, space_id, document_name, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 5: Releases
    # ==================================================================
    def get_releases(
        self,
        project_id: str,
        *,
        release_id: Optional[str] = None,
        status: Optional[str] = None,
        detailed_info: Optional[bool] = None,
    ) -> List[PolarionRelease]:
        """Retrieve releases for a project.

        ``GET /projects/{project_id}/releases``

        Parameters
        ----------
        project_id : str
        release_id : str, optional
            Filter by specific release ID.
        status : str, optional
            Filter by release status.
        detailed_info : bool, optional
            When ``True``, include detailed release information.

        Returns
        -------
        list[PolarionRelease]
        """
        params: Dict[str, Any] = {}
        if release_id is not None:
            params["release_id"] = release_id
        if status is not None:
            params["status"] = status
        if detailed_info is not None:
            params["detailed_info"] = str(detailed_info).lower()

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/releases",
            params=params or None,
        )
        items = [
            PolarionRelease.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_releases project_id=%s "
            "items_fetched=%d duration_ms=%.1f",
            project_id, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 6: Test results
    # ==================================================================
    def get_test_results(
        self,
        project_id: str,
        release_name: str,
        device_name: str,
        *,
        component_name: Optional[str] = None,
        configuration: Optional[str] = None,
        only_latest: Optional[bool] = None,
    ) -> dict:
        """Retrieve test results for a project release + device.

        ``GET /projects/{project_id}/testResult``

        Parameters
        ----------
        project_id : str
        release_name : str
            Required – the release name to query.
        device_name : str
            Required – the device name to query.
        component_name : str, optional
        configuration : str, optional
        only_latest : bool, optional
            Default ``True`` on the server side.

        Returns
        -------
        dict
            Raw ``TestResult+Root`` JSON (contains ``project`` key
            with nested regression details).
        """
        params: Dict[str, Any] = {
            "releaseName": release_name,
            "deviceName": device_name,
        }
        if component_name is not None:
            params["componentName"] = component_name
        if configuration is not None:
            params["configuration"] = configuration
        if only_latest is not None:
            params["onlyLatest"] = str(only_latest).lower()

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/testResult",
            params=params,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_test_results project_id=%s "
            "release=%s device=%s duration_ms=%.1f",
            project_id, release_name, device_name, elapsed,
        )
        return data if isinstance(data, dict) else {}

    # ==================================================================
    # GET endpoint 7: Test cases
    # ==================================================================
    def get_test_cases(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
        component_name: str,
        *,
        revision_id: Optional[str] = None,
        work_item_type: Optional[str] = None,
        wi_status: Optional[str] = None,
    ) -> List[PolarionTestCase]:
        """Retrieve test cases from a document.

        ``GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testcases``

        Parameters
        ----------
        project_id : str
        space_id : str
        document_name : str
        component_name : str
            Required – component name filter.
        revision_id : str, optional
        work_item_type : str, optional
        wi_status : str, optional

        Returns
        -------
        list[PolarionTestCase]
        """
        params: Dict[str, Any] = {
            "componentName": component_name,
        }
        if revision_id is not None:
            params["revisiondId"] = revision_id  # Note: API typo in Swagger
        if work_item_type is not None:
            params["workItemType"] = work_item_type
        if wi_status is not None:
            params["wiStatus"] = wi_status

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/spaces/{space_id}"
            f"/documents/{document_name}/testcases",
            params=params,
        )
        items = [
            PolarionTestCase.from_api_dict(d)
            for d in (data if isinstance(data, list) else [])
        ]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_test_cases project_id=%s "
            "document=%s component=%s items_fetched=%d duration_ms=%.1f",
            project_id, document_name, component_name, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 8: Test data + environment specs
    # ==================================================================
    def get_test_data(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
        *,
        revision_id: Optional[str] = None,
        wi_status: Optional[str] = None,
        component_name: Optional[str] = None,
        work_item_type: Optional[str] = None,
    ) -> PolarionTestDataAndEnvSpec:
        """Retrieve test data and environment specs from a document.

        ``GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testdatas``

        Parameters
        ----------
        project_id : str
        space_id : str
        document_name : str
        revision_id : str, optional
        wi_status : str, optional
        component_name : str, optional
        work_item_type : str, optional

        Returns
        -------
        PolarionTestDataAndEnvSpec
        """
        params: Dict[str, Any] = {}
        if revision_id is not None:
            params["revisionId"] = revision_id
        if wi_status is not None:
            params["wiStatus"] = wi_status
        if component_name is not None:
            params["componentName"] = component_name
        if work_item_type is not None:
            params["workItemType"] = work_item_type

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/spaces/{space_id}"
            f"/documents/{document_name}/testdatas",
            params=params or None,
        )
        result = PolarionTestDataAndEnvSpec.from_api_dict(
            data if isinstance(data, dict) else {}
        )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_test_data project_id=%s "
            "document=%s test_data=%d test_env=%d duration_ms=%.1f",
            project_id, document_name,
            len(result.test_data_list),
            len(result.test_environment_list),
            elapsed,
        )
        return result

    # ==================================================================
    # GET endpoint 9: Test scopes
    # ==================================================================
    def get_test_scopes(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
        *,
        revision_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Any:
        """Retrieve test scopes from a document.

        ``GET /projects/{projectId}/spaces/{spaceId}/documents/{documentName}/testscopes``

        Parameters
        ----------
        project_id : str
        space_id : str
        document_name : str
        revision_id : str, optional
        status : str, optional

        Returns
        -------
        PolarionTestScope | list[PolarionTestScope] | dict
            The API can return a single object or an array depending
            on the response schema version.
        """
        params: Dict[str, Any] = {}
        if revision_id is not None:
            params["revisionId"] = revision_id
        if status is not None:
            params["status"] = status

        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/projects/{project_id}/spaces/{space_id}"
            f"/documents/{document_name}/testscopes",
            params=params or None,
        )

        # Response may be a single TestScope dict or a list
        if isinstance(data, list):
            items = [PolarionTestScope.from_api_dict(d) for d in data]
        elif isinstance(data, dict):
            items = [PolarionTestScope.from_api_dict(data)]
        else:
            items = []

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_test_scopes project_id=%s "
            "document=%s items_fetched=%d duration_ms=%.1f",
            project_id, document_name, len(items), elapsed,
        )
        return items

    # ==================================================================
    # GET endpoint 10: Jira integration – project lookup
    # ==================================================================
    def get_jira_project(
        self,
        jira_project_id: str,
        polarion_user: str,
    ) -> dict:
        """Look up a Jira project mapping for a Polarion user.

        ``GET /jira/projects/{jira_project_id}/user/{polarion_user}``

        Parameters
        ----------
        jira_project_id : str
        polarion_user : str

        Returns
        -------
        dict
            Raw ``JiraProject`` JSON with ``id``, ``key``, ``name``.
        """
        t0 = time.perf_counter()
        data = self._request(
            "GET",
            f"/jira/projects/{jira_project_id}/user/{polarion_user}",
        )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=get_jira_project "
            "jira_project=%s polarion_user=%s duration_ms=%.1f",
            jira_project_id, polarion_user, elapsed,
        )
        return data if isinstance(data, dict) else {}

    # ==================================================================
    # Incremental sync  (AICE-ING-004 – incremental sync)
    # ==================================================================
    def _sync_state_path(self, project_id: str) -> Optional[Path]:
        """Return the file path used to persist sync state for a project."""
        if self._sync_state_dir is None:
            return None
        safe_id = project_id.replace("/", "_").replace("\\", "_")
        return self._sync_state_dir / f"polarion_sync_{safe_id}.json"

    def _load_sync_state(self, project_id: str) -> SyncState:
        """Load persisted sync state, or create a fresh one."""
        if project_id in self._sync_states:
            return self._sync_states[project_id]

        state_path = self._sync_state_path(project_id)
        if state_path is not None and state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                state = SyncState(
                    project_id=raw["project_id"],
                    last_sync_timestamp=raw.get("last_sync_timestamp"),
                    last_sync_status=SyncStatus(
                        raw.get("last_sync_status", "success")
                    ),
                    items_synced=raw.get("items_synced", 0),
                    known_work_item_ids=raw.get("known_work_item_ids", []),
                )
                self._sync_states[project_id] = state
                logger.info(
                    "Loaded sync state for project %s: last_sync=%s",
                    project_id, state.last_sync_timestamp,
                )
                return state
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning(
                    "Corrupted sync state file %s, starting fresh: %s",
                    state_path, exc,
                )

        state = SyncState(project_id=project_id)
        self._sync_states[project_id] = state
        return state

    def _save_sync_state(self, state: SyncState) -> None:
        """Persist sync state to disk (if configured)."""
        self._sync_states[state.project_id] = state
        state_path = self._sync_state_path(state.project_id)
        if state_path is not None:
            state_path.write_text(
                json.dumps(asdict(state), indent=2),
                encoding="utf-8",
            )
            logger.debug("Sync state saved to %s", state_path)

    def incremental_sync(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
        *,
        revision: Optional[str] = None,
        detect_deletions: bool = True,
    ) -> Dict[str, Any]:
        """Perform an incremental sync for document work items.

        On first run (no prior sync state) **all** work items are fetched.
        On subsequent runs the ``revision`` parameter can be used to
        fetch a specific snapshot.  Deletion detection compares the
        current work item set with the previously known set.

        Parameters
        ----------
        project_id : str
            Polarion project ID.
        space_id : str
            Space within the project.
        document_name : str
            Document to sync.
        revision : str, optional
            Specific document revision to sync.
        detect_deletions : bool
            When ``True``, find work items that disappeared since last sync.

        Returns
        -------
        dict
            Sync report with keys: ``work_items``, ``deleted_ids``,
            ``status``, ``items_synced``, ``sync_timestamp``,
            ``duration_ms``.
        """
        t0 = time.perf_counter()
        state = self._load_sync_state(project_id)

        sync_timestamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )

        logger.info(
            "source_type=polarion action=incremental_sync project_id=%s "
            "space_id=%s document=%s last_sync=%s",
            project_id, space_id, document_name,
            state.last_sync_timestamp,
        )

        # -- Fetch work items -------------------------------------------
        try:
            work_items = self.get_document_work_items(
                project_id=project_id,
                space_id=space_id,
                document_name=document_name,
                revision=revision,
            )
        except PolarionConnectorError as exc:
            state.last_sync_status = SyncStatus.FAILED
            self._save_sync_state(state)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                "source_type=polarion action=incremental_sync "
                "project_id=%s status=failed duration_ms=%.1f errors=%s",
                project_id, elapsed, exc,
            )
            return {
                "work_items": [],
                "deleted_ids": [],
                "status": SyncStatus.FAILED.value,
                "items_synced": 0,
                "sync_timestamp": None,
                "duration_ms": elapsed,
            }

        # -- Detect deletions -------------------------------------------
        deleted_ids: List[str] = []
        if detect_deletions and state.known_work_item_ids:
            current_titles = {wi.title for wi in work_items}
            deleted_ids = [
                wid for wid in state.known_work_item_ids
                if wid not in current_titles
            ]
            if deleted_ids:
                logger.info(
                    "Detected %d deleted work items in project %s",
                    len(deleted_ids), project_id,
                )

        # -- Update sync state ------------------------------------------
        state.last_sync_timestamp = sync_timestamp
        state.last_sync_status = SyncStatus.SUCCESS
        state.items_synced = len(work_items)
        state.known_work_item_ids = [wi.title for wi in work_items]
        self._save_sync_state(state)

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=polarion action=incremental_sync project_id=%s "
            "status=success items_fetched=%d deleted=%d duration_ms=%.1f",
            project_id, len(work_items), len(deleted_ids), elapsed,
        )

        return {
            "work_items": work_items,
            "deleted_ids": deleted_ids,
            "status": SyncStatus.SUCCESS.value,
            "items_synced": len(work_items),
            "sync_timestamp": sync_timestamp,
            "duration_ms": elapsed,
        }

    def full_sync(
        self,
        project_id: str,
        space_id: str,
        document_name: str,
    ) -> Dict[str, Any]:
        """Perform a **full** sync (ignoring previous sync state).

        Resets the sync timestamp so the next ``incremental_sync`` will
        start fresh from this point in time.

        Returns the same report dict as ``incremental_sync``.
        """
        state = SyncState(project_id=project_id)
        self._save_sync_state(state)

        return self.incremental_sync(
            project_id=project_id,
            space_id=space_id,
            document_name=document_name,
            detect_deletions=False,
        )

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"PolarionConnector(base_url={self._base_url!r}, "
            f"connected={self._connected})"
        )

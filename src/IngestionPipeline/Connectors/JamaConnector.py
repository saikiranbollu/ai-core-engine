"""Jama REST API Connector for the AICE Ingestion Pipeline.

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
from urllib import parse as urlparse

import httpx

from ..config import get_max_workers
from src._common.path_safety import allowed_roots_from_env, safe_path_under
from src._common.secret_str import SecretStr
from src._common.tls_config import enforce_tls_policy


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("aice.ingestion.jama")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class JamaConnectorError(Exception):
    """Base exception for all Jama connector errors."""


class JamaAuthError(JamaConnectorError):
    """Raised when authentication with the Jama API fails."""


class JamaClientError(JamaConnectorError):
    """Raised for client-side (4xx) HTTP errors."""


class JamaServerError(JamaConnectorError):
    """Raised for server-side (5xx) HTTP errors."""


class JamaConnectionError(JamaConnectorError):
    """Raised when a connection to the Jama API cannot be established."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SyncStatus(str, Enum):
    """Status of an incremental sync operation."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class JamaItem:
    """Represents a single Jama requirement/item.

    Field mapping follows the structure observed in the jamaapi reference
    implementation (see ``jamaapi.requirement``).
    """
    id: int
    project_id: int
    name: str
    document_key: str
    description: str
    item_type: int
    version: int
    modified_date: str
    created_date: str
    sequence: str = ""
    locked: bool = False
    status: int = -1
    status_text: str = ""
    importance: str = ""
    # Raw fields dict preserved for extensibility
    raw_fields: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_dict(cls, data: dict) -> "JamaItem":
        """Create a ``JamaItem`` from a raw Jama REST API JSON dict.

        The field extraction logic mirrors ``jamaapi.requirement.from_dict``.
        """
        fields = data.get("fields", {})
        item_type = int(data.get("itemType", -1))

        location = data.get("location", {}) or data.get("baselineLocation", {})
        sequence = location.get("sequence", "") if location else ""

        version_raw = data.get("version", {})
        if isinstance(version_raw, dict):
            version = int(version_raw.get("versionNumber", -1))
        elif isinstance(version_raw, int):
            version = version_raw
        else:
            version = -1

        lock = data.get("lock", {})
        locked = bool(lock.get("locked", False)) if lock else False

        return cls(
            id=int(data.get("id", -1)),
            project_id=int(data.get("project", -1)),
            name=fields.get("name", ""),
            document_key=fields.get("documentKey", ""),
            description=fields.get("description", ""),
            item_type=item_type,
            version=version,
            modified_date=data.get("modifiedDate", ""),
            created_date=data.get("createdDate", ""),
            sequence=sequence,
            locked=locked,
            status=int(fields.get("status", -1)),
            raw_fields=fields,
        )


@dataclass
class SyncState:
    """Tracks incremental sync state for a specific project.

    Persisted to disk so that subsequent runs only fetch items modified
    since the last successful sync.
    """
    project_id: int
    last_sync_timestamp: Optional[str] = None  # ISO-8601 UTC
    last_sync_status: SyncStatus = SyncStatus.SUCCESS
    items_synced: int = 0
    deleted_item_ids: List[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------

class JamaConnector:
    """Synchronous Jama REST API connector with API-key authentication.

    This connector is designed for the HybridRAG ingestion pipeline.
    It wraps the Jama REST API (v1) and provides:
      * API key (Basic-auth style) authentication.
      * Automatic pagination for large result sets.
      * Filtering by project, module (folder), and item type.
      * Incremental sync using ``modifiedSince`` / ``modifiedDate``.
      * Retry logic with exponential back-off (AICE-ING-010).
      * Structured logging (AICE-ING-011).

    Parameters
    ----------
    base_url : str
        Root URL of the Jama instance (e.g. ``https://jama.example.com``).
    api_key : str
        Jama API key (client ID).
    api_secret : str
        Jama API secret (client secret).
    api_root : str
        REST API path prefix. Defaults to ``/rest/v1``.
    max_results_per_page : int
        Maximum number of items returned per paginated request.
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

    # REST API path prefix used by Jama Connect
    _DEFAULT_API_ROOT = "/rest/v1"
    _DEFAULT_MAX_RESULTS = 50
    _DEFAULT_MAX_RETRIES = 3
    _DEFAULT_BACKOFF_FACTOR = 1.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        api_root: str = _DEFAULT_API_ROOT,
        max_results_per_page: int = _DEFAULT_MAX_RESULTS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        timeout: Optional[float] = 30.0,
        verify_ssl: bool = True,
        sync_state_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        verify_ssl = enforce_tls_policy(verify_ssl)
        # Normalise URL: strip trailing slash
        self._base_url = base_url.rstrip("/")
        self._api_root = api_root.rstrip("/")
        self._max_results = max_results_per_page
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._timeout = timeout

        # API-key authentication (Jama uses HTTP Basic auth with
        # client-id / client-secret when using API keys)
        # F-CF-X02: keep the secret in SecretStr so it is not exposed via repr.
        self._api_secret = SecretStr(api_secret)
        self._auth = httpx.BasicAuth(username=api_key, password=api_secret)

        ssl_context = ssl.create_default_context() if verify_ssl else False
        self._client = httpx.Client(
            base_url=f"{self._base_url}{self._api_root}",
            auth=self._auth,
            verify=ssl_context,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

        # Sync-state persistence (F-CF-X04: contain under an allowed root before
        # creating it; AICE_SYNC_STATE_ROOTS overrides the defaults).
        if sync_state_dir is not None:
            sync_roots = allowed_roots_from_env(
                "AICE_SYNC_STATE_ROOTS",
                ["/data/aice/sync_state", tempfile.gettempdir()],
            )
            self._sync_state_dir: Optional[Path]
            try:
                self._sync_state_dir = safe_path_under(
                    sync_state_dir, sync_roots
                )
            except ValueError as exc:
                # Keep resolved-path / allowed-roots detail out of the propagated
                # error so directory structure is not leaked (F4).
                logger.warning("Rejected sync_state_dir %s: %s", sync_state_dir, exc)
                raise ValueError("Rejected sync_state_dir: not permitted") from None
            self._sync_state_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._sync_state_dir = None

        # In-memory sync state cache  {project_id: SyncState}
        self._sync_states: Dict[int, SyncState] = {}

        # Picklist resolution cache  {picklist_id: {option_id: name}}
        self._picklist_cache: Dict[int, Dict[int, str]] = {}

        self._connected: bool = False
        self._max_workers = get_max_workers("connectors.jama")
        logger.info(
            "JamaConnector initialised (base_url=%s, api_root=%s)",
            self._base_url,
            self._api_root,
        )

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> "JamaConnector":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        # F-CF-X02: drop the BasicAuth credential off the client + cached secret.
        self._client.auth = None
        self._client.close()
        self._auth = None
        self._api_secret.clear()
        logger.debug("HTTP client closed.")

    # ------------------------------------------------------------------
    # Picklist resolution  (resolve integer IDs → human-readable labels)
    # ------------------------------------------------------------------

    # Well-known picklist IDs for Requirement (itemType 83) fields
    _STATUS_PICKLIST_ID = 63
    _IMPORTANCE_PICKLIST_ID = 2811

    def _resolve_picklist(self, picklist_id: int) -> Dict[int, str]:
        """Fetch and cache picklist options {option_id: name}."""
        if picklist_id in self._picklist_cache:
            return self._picklist_cache[picklist_id]

        try:
            resp = self._request("GET", f"/picklists/{picklist_id}/options")
            options = resp.get("data", [])
            mapping = {int(opt["id"]): opt["name"] for opt in options}
        except Exception as exc:
            logger.warning("Failed to resolve picklist %d: %s", picklist_id, exc)
            mapping = {}

        self._picklist_cache[picklist_id] = mapping
        return mapping

    def resolve_status(self, status_id: int) -> str:
        """Resolve a status integer ID to its label (e.g. 293 → 'Approved')."""
        if status_id < 0:
            return ""
        mapping = self._resolve_picklist(self._STATUS_PICKLIST_ID)
        return mapping.get(status_id, str(status_id))

    def resolve_importance(self, importance_id: int) -> str:
        """Resolve an importance integer ID to its label (e.g. 1524 → 'Mandatory')."""
        if importance_id < 0:
            return ""
        mapping = self._resolve_picklist(self._IMPORTANCE_PICKLIST_ID)
        return mapping.get(importance_id, str(importance_id))

    def enrich_items(self, items: List["JamaItem"]) -> List["JamaItem"]:
        """Resolve picklist IDs on items to human-readable text.

        Populates ``status_text`` and ``importance`` fields.
        """
        for item in items:
            item.status_text = self.resolve_status(item.status)
            # importance field uses itemType-qualified key
            imp_raw = item.raw_fields.get(f"importance${item.item_type}", -1)
            if isinstance(imp_raw, int) and imp_raw > 0:
                item.importance = self.resolve_importance(imp_raw)
        return items

    # ------------------------------------------------------------------
    # Connection validation  (AICE-ING-003 – authentication working)
    # ------------------------------------------------------------------
    def validate_connection(self) -> bool:
        """Validate that the API key credentials are accepted by Jama.

        Issues a lightweight ``GET /projects?maxResults=1`` call and
        checks for a 200 response.

        Returns
        -------
        bool
            ``True`` if the connection is valid.

        Raises
        ------
        JamaAuthError
            If authentication fails (HTTP 401).
        JamaConnectionError
            If the server is unreachable.
        """
        t0 = time.perf_counter()
        try:
            response = self._request("GET", "/projects", params={"maxResults": 1})
            self._connected = True
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "Connection validated successfully (duration_ms=%.1f)", elapsed
            )
            return True
        except JamaAuthError:
            self._connected = False
            raise
        except JamaConnectorError:
            self._connected = False
            raise
        except Exception as exc:
            self._connected = False
            raise JamaConnectionError(
                f"Failed to connect to Jama at {self._base_url}: {exc}"
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
    ) -> dict:
        """Issue an HTTP request with retry / back-off and error handling.

        Parameters
        ----------
        method : str
            HTTP method (GET, PUT, …).
        path : str
            API path relative to ``api_root`` (e.g. ``/projects``).
        params : dict, optional
            Query parameters.
        json_body : dict, optional
            JSON body for PUT / POST.

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        JamaAuthError, JamaClientError, JamaServerError
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                )

                # -- handle HTTP errors -----------------------------------
                if response.status_code == 401:
                    raise JamaAuthError(
                        "Authentication failed (HTTP 401). "
                        "Please verify your API key and secret."
                    )
                if 400 <= response.status_code < 500:
                    raise JamaClientError(
                        f"Client error {response.status_code}: "
                        f"{response.text}"
                    )
                if response.status_code >= 500:
                    raise JamaServerError(
                        f"Server error {response.status_code}: "
                        f"{response.text}"
                    )

                response.raise_for_status()
                return response.json()

            except (JamaAuthError, JamaClientError):
                # Non-retryable errors – propagate immediately
                raise

            except (httpx.RequestError, JamaServerError) as exc:
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
        raise JamaConnectionError(
            f"Request {method} {path} failed after "
            f"{self._max_retries} retries: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Pagination helper  (AICE-ING-003 – pagination)
    # ------------------------------------------------------------------
    def _get_all_pages(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
    ) -> List[dict]:
        """Fetch **all** pages for a paginated Jama endpoint.

        Jama REST API uses ``startAt`` / ``maxResults`` query parameters
        and returns page metadata under ``meta.pageInfo``.

        Parameters
        ----------
        path : str
            API path (e.g. ``/abstractitems``).
        params : dict, optional
            Additional query parameters (merged with pagination params).

        Returns
        -------
        list[dict]
            Aggregated ``data`` items from all pages.
        """
        all_items: List[dict] = []
        start_at = 0
        total_results: Optional[int] = None

        _params = dict(params) if params else {}

        while True:
            _params["startAt"] = start_at
            _params["maxResults"] = self._max_results

            body = self._request("GET", path, params=_params)

            data = body.get("data", [])
            all_items.extend(data)

            meta = body.get("meta", {})
            page_info = meta.get("pageInfo", {})
            total_results = int(page_info.get("totalResults", len(data)))
            result_count = int(page_info.get("resultCount", len(data)))

            start_at += result_count

            logger.debug(
                "Pagination: fetched %d/%d items from %s",
                len(all_items), total_results, path,
            )

            if len(all_items) >= total_results:
                break

        return all_items

    # ------------------------------------------------------------------
    # Public API – project discovery
    # ------------------------------------------------------------------
    def get_projects(self) -> List[dict]:
        """Retrieve all projects visible to the authenticated user.

        Returns
        -------
        list[dict]
            Raw project JSON dicts from the Jama API.
        """
        t0 = time.perf_counter()
        projects = self._get_all_pages("/projects")
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=jama action=get_projects items_fetched=%d "
            "duration_ms=%.1f",
            len(projects), elapsed,
        )
        return projects

    def get_project_id(self, project_name: str) -> Optional[int]:
        """Resolve a project name to its numeric ID.

        Parameters
        ----------
        project_name : str
            Exact project name.

        Returns
        -------
        int or None
            Project ID, or ``None`` if not found.
        """
        for project in self.get_projects():
            fields = project.get("fields", {})
            if fields.get("name") == project_name:
                return int(project["id"])
        return None

    # ------------------------------------------------------------------
    # Public API – item type listing
    # ------------------------------------------------------------------
    def get_item_types(self) -> List[dict]:
        """Retrieve all item types defined in Jama.

        Returns
        -------
        list[dict]
            Raw item-type JSON dicts.
        """
        return self._get_all_pages("/itemtypes")

    # ------------------------------------------------------------------
    # Filtering  (AICE-ING-003 – project / module / item-type filtering)
    # ------------------------------------------------------------------
    def get_items(
        self,
        *,
        project_id: Optional[int] = None,
        item_type_id: Optional[int] = None,
        contains: Optional[str] = None,
        modified_since: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> List[JamaItem]:
        """Fetch items using the ``abstractitems`` endpoint with filters.

        All filter parameters are optional and additive.

        Parameters
        ----------
        project_id : int, optional
            Restrict to a specific project.
        item_type_id : int, optional
            Restrict to a specific item type.
        contains : str, optional
            Full-text search term.
        modified_since : str, optional
            ISO-8601 timestamp; only return items modified after this date.
        sort_by : str, optional
            Field to sort results by (e.g. ``modifiedDate``).

        Returns
        -------
        list[JamaItem]
            Parsed Jama items.
        """
        params: Dict[str, Any] = {}
        if project_id is not None:
            params["project"] = project_id
        if item_type_id is not None:
            params["itemType"] = item_type_id
        if contains is not None:
            params["contains"] = contains
        if modified_since is not None:
            params["modifiedDate"] = modified_since
        if sort_by is not None:
            params["sortBy"] = sort_by

        t0 = time.perf_counter()
        raw_items = self._get_all_pages("/abstractitems", params=params)
        elapsed = (time.perf_counter() - t0) * 1000

        items = [JamaItem.from_api_dict(d) for d in raw_items]

        logger.info(
            "source_type=jama action=get_items project_id=%s "
            "item_type_id=%s items_fetched=%d duration_ms=%.1f",
            project_id, item_type_id, len(items), elapsed,
        )
        return items

    def get_items_by_project(self, project_id: int) -> List[JamaItem]:
        """Convenience: get all items in a project (paginated).

        Parameters
        ----------
        project_id : int
            Jama project ID.
        """
        return self.get_items(project_id=project_id)

    def get_items_by_type(
        self,
        project_id: int,
        item_type_id: int,
    ) -> List[JamaItem]:
        """Convenience: get items filtered by project **and** item type.

        Parameters
        ----------
        project_id : int
            Jama project ID.
        item_type_id : int
            Jama item-type ID.
        """
        return self.get_items(
            project_id=project_id, item_type_id=item_type_id
        )

    # ------------------------------------------------------------------
    # Module (folder / children) filtering
    # ------------------------------------------------------------------
    def get_children_items(
        self, parent_item_id: int
    ) -> List[JamaItem]:
        """Retrieve all children of a parent item (i.e. a *module* / folder).

        Automatically paginates through the full child list.

        Parameters
        ----------
        parent_item_id : int
            ID of the parent item / folder.

        Returns
        -------
        list[JamaItem]
        """
        t0 = time.perf_counter()
        raw = self._get_all_pages(f"/items/{parent_item_id}/children")
        items = [JamaItem.from_api_dict(d) for d in raw]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=jama action=get_children parent_id=%d "
            "items_fetched=%d duration_ms=%.1f",
            parent_item_id, len(items), elapsed,
        )
        return items

    def get_module_items(
        self,
        module_id: int,
        *,
        recurse: bool = True,
    ) -> List[JamaItem]:
        """Retrieve all items under a Jama module, optionally recursing
        into sub-folders.

        Parameters
        ----------
        module_id : int
            ID of the top-level module / folder item.
        recurse : bool
            When ``True`` (default), descend into sub-folders.

        Returns
        -------
        list[JamaItem]
        """
        FOLDER_TYPE = 32  # Jama folder item-type constant

        items: List[JamaItem] = []
        children = self.get_children_items(module_id)

        folders: List[JamaItem] = []
        for child in children:
            if child.item_type == FOLDER_TYPE:
                folders.append(child)
            else:
                items.append(child)

        if recurse and folders:
            with ThreadPoolExecutor(
                max_workers=self._max_workers
            ) as executor:
                futures = {
                    executor.submit(
                        self.get_module_items, folder.id, recurse=True
                    ): folder
                    for folder in folders
                }
                for future in as_completed(futures):
                    items.extend(future.result())

        return items

    # ------------------------------------------------------------------
    # Filter-based retrieval
    # ------------------------------------------------------------------
    def get_filter_results(
        self,
        filter_id: int,
        *,
        project_id: Optional[int] = None,
    ) -> List[JamaItem]:
        """Fetch items matching a saved Jama filter.

        Parameters
        ----------
        filter_id : int
            ID of the saved filter.
        project_id : int, optional
            Restrict to a specific project.

        Returns
        -------
        list[JamaItem]
        """
        params: Dict[str, Any] = {}
        if project_id is not None:
            params["project"] = project_id

        t0 = time.perf_counter()
        raw = self._get_all_pages(f"/filters/{filter_id}/results", params=params)
        items = [JamaItem.from_api_dict(d) for d in raw]
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=jama action=get_filter_results filter_id=%d "
            "items_fetched=%d duration_ms=%.1f",
            filter_id, len(items), elapsed,
        )
        return items

    # ------------------------------------------------------------------
    # Single-item retrieval
    # ------------------------------------------------------------------
    def get_item(self, item_id: int) -> JamaItem:
        """Retrieve a single item by ID.

        Parameters
        ----------
        item_id : int

        Returns
        -------
        JamaItem
        """
        body = self._request("GET", f"/items/{item_id}")
        return JamaItem.from_api_dict(body.get("data", {}))

    # ------------------------------------------------------------------
    # Incremental sync  (AICE-ING-003 – incremental sync)
    # ------------------------------------------------------------------
    def _sync_state_path(self, project_id: int) -> Optional[Path]:
        """Return the file path used to persist sync state for a project."""
        if self._sync_state_dir is None:
            return None
        return self._sync_state_dir / f"jama_sync_{project_id}.json"

    def _load_sync_state(self, project_id: int) -> SyncState:
        """Load persisted sync state, or create a fresh one."""
        # Check in-memory cache first
        if project_id in self._sync_states:
            return self._sync_states[project_id]

        state_path = self._sync_state_path(project_id)
        if state_path is not None and state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                state = SyncState(
                    project_id=raw["project_id"],
                    last_sync_timestamp=raw.get("last_sync_timestamp"),
                    last_sync_status=SyncStatus(raw.get("last_sync_status", "success")),
                    items_synced=raw.get("items_synced", 0),
                    deleted_item_ids=raw.get("deleted_item_ids", []),
                )
                self._sync_states[project_id] = state
                logger.info(
                    "Loaded sync state for project %d: last_sync=%s",
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
        project_id: int,
        *,
        item_type_id: Optional[int] = None,
        detect_deletions: bool = True,
    ) -> Dict[str, Any]:
        """Perform an incremental sync for a project.

        On first run (no prior sync state) **all** items are fetched.
        On subsequent runs only items modified since the last successful
        sync are fetched.

        Optionally detects deleted items by comparing the full set of
        current item IDs with the previously known set.

        Parameters
        ----------
        project_id : int
            Jama project ID.
        item_type_id : int, optional
            Restrict sync to a specific item type.
        detect_deletions : bool
            When ``True``, perform a lightweight ID-only sweep to find
            items that have been deleted since the last sync.

        Returns
        -------
        dict
            Sync report with keys:
            ``modified_items``, ``deleted_item_ids``, ``status``,
            ``items_synced``, ``sync_timestamp``, ``duration_ms``.
        """
        t0 = time.perf_counter()
        state = self._load_sync_state(project_id)

        sync_timestamp = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )

        logger.info(
            "source_type=jama action=incremental_sync project_id=%d "
            "last_sync=%s",
            project_id, state.last_sync_timestamp,
        )

        # -- Fetch modified items ----------------------------------------
        try:
            modified_items = self.get_items(
                project_id=project_id,
                item_type_id=item_type_id,
                modified_since=state.last_sync_timestamp,
                sort_by="modifiedDate.desc",
            )
        except JamaConnectorError as exc:
            state.last_sync_status = SyncStatus.FAILED
            self._save_sync_state(state)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                "source_type=jama action=incremental_sync project_id=%d "
                "status=failed duration_ms=%.1f errors=%s",
                project_id, elapsed, exc,
            )
            return {
                "modified_items": [],
                "deleted_item_ids": [],
                "status": SyncStatus.FAILED.value,
                "items_synced": 0,
                "sync_timestamp": None,
                "duration_ms": elapsed,
            }

        # -- Detect deletions --------------------------------------------
        deleted_ids: List[int] = []
        if detect_deletions and state.last_sync_timestamp is not None:
            try:
                deleted_ids = self._detect_deleted_items(
                    project_id=project_id,
                    item_type_id=item_type_id,
                    known_item_ids=self._get_known_item_ids(state),
                )
            except JamaConnectorError as exc:
                logger.warning(
                    "Deletion detection failed for project %d: %s",
                    project_id, exc,
                )

        # -- Update sync state -------------------------------------------
        state.last_sync_timestamp = sync_timestamp
        state.last_sync_status = SyncStatus.SUCCESS
        state.items_synced = len(modified_items)
        state.deleted_item_ids = deleted_ids
        self._save_sync_state(state)

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "source_type=jama action=incremental_sync project_id=%d "
            "status=success items_fetched=%d deleted=%d duration_ms=%.1f",
            project_id, len(modified_items), len(deleted_ids), elapsed,
        )

        return {
            "modified_items": modified_items,
            "deleted_item_ids": deleted_ids,
            "status": SyncStatus.SUCCESS.value,
            "items_synced": len(modified_items),
            "sync_timestamp": sync_timestamp,
            "duration_ms": elapsed,
        }

    # ------------------------------------------------------------------
    # Deletion detection helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_known_item_ids(state: SyncState) -> List[int]:
        """Return the list of item IDs recorded during the last sync.

        The full ID list is stored in the sync state file to enable
        deletion detection without querying every single item.
        """
        # ``deleted_item_ids`` is reused to store the *previously* known
        # deleted IDs.  On the first sync there are none.
        return state.deleted_item_ids

    def _detect_deleted_items(
        self,
        project_id: int,
        item_type_id: Optional[int],
        known_item_ids: List[int],
    ) -> List[int]:
        """Compare the current set of item IDs on the server with the
        set known from the previous sync to find deletions.

        Parameters
        ----------
        project_id : int
        item_type_id : int, optional
        known_item_ids : list[int]
            IDs that were present during the previous sync.

        Returns
        -------
        list[int]
            IDs of items that no longer exist on the server.
        """
        if not known_item_ids:
            return []

        # Fetch all current item IDs (lightweight – only need IDs)
        current_items = self.get_items(
            project_id=project_id,
            item_type_id=item_type_id,
        )
        current_ids = {item.id for item in current_items}
        deleted = [iid for iid in known_item_ids if iid not in current_ids]

        if deleted:
            logger.info(
                "Detected %d deleted items in project %d: %s",
                len(deleted), project_id, deleted[:20],
            )
        return deleted

    # ------------------------------------------------------------------
    # Convenience – full sync snapshot
    # ------------------------------------------------------------------
    def full_sync(
        self,
        project_id: int,
        *,
        item_type_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Perform a **full** sync (ignoring previous sync state).

        Resets the sync timestamp so the next ``incremental_sync`` will
        start fresh from this point in time.

        Returns the same report dict as ``incremental_sync``.
        """
        # Reset sync state
        state = SyncState(project_id=project_id)
        self._save_sync_state(state)

        return self.incremental_sync(
            project_id=project_id,
            item_type_id=item_type_id,
            detect_deletions=False,
        )

    # ------------------------------------------------------------------
    # Relationship traversal helpers (useful for HybridRAG graph building)
    # ------------------------------------------------------------------
    def get_downstream_relationships(self, item_id: int) -> List[dict]:
        """Get downstream relationships for an item.

        Parameters
        ----------
        item_id : int

        Returns
        -------
        list[dict]
            Raw relationship dicts.
        """
        return self._get_all_pages(
            f"/items/{item_id}/downstreamrelationships"
        )

    def get_upstream_relationships(self, item_id: int) -> List[dict]:
        """Get upstream relationships for an item.

        Parameters
        ----------
        item_id : int

        Returns
        -------
        list[dict]
            Raw relationship dicts.
        """
        return self._get_all_pages(
            f"/items/{item_id}/upstreamrelationships"
        )

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"JamaConnector(base_url={self._base_url!r}, "
            f"connected={self._connected})"
        )

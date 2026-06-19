"""Bitbucket Server REST API Connector for the AICE Ingestion Pipeline.

Provides file-content retrieval and directory browsing against
Bitbucket Server / Data Center instances (``/rest/api/latest``).

Supports:
  * Personal-access-token (Bearer) **and** HTTP Basic authentication.
  * Initialisation from an SSH or HTTPS clone URL via ``from_clone_url``.
  * Single-file raw content retrieval.
  * Directory listing (flat or recursive tree walk).
  * Bulk parallel file fetch via ``ThreadPoolExecutor``.
  * Retry with exponential back-off (AICE-ING-010).
  * Structured logging (AICE-ING-011).
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import httpx

from ..config import get_max_workers
from src._common.tls_config import enforce_tls_policy

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("aice.ingestion.bitbucket")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BitbucketConnectorError(Exception):
    """Base exception for all Bitbucket connector errors."""


class BitbucketAuthError(BitbucketConnectorError):
    """Raised when authentication with the Bitbucket API fails."""


class BitbucketClientError(BitbucketConnectorError):
    """Raised for client-side (4xx) HTTP errors."""


class BitbucketServerError(BitbucketConnectorError):
    """Raised for server-side (5xx) HTTP errors."""


class BitbucketConnectionError(BitbucketConnectorError):
    """Raised when a connection to the Bitbucket API cannot be established."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    """A single entry returned by the Bitbucket browse API."""

    path: str
    entry_type: str  # "FILE" or "DIRECTORY"
    size: Optional[int] = None
    content_id: Optional[str] = None
    extension: Optional[str] = None


@dataclass
class FileContent:
    """Raw content of a file fetched from Bitbucket."""

    path: str
    content: str
    size: int


# ---------------------------------------------------------------------------
# Clone-URL parser
# ---------------------------------------------------------------------------

# SSH:   ssh://git@bitbucket.vih.infineon.com:7999/PROJECT/repo.git
#        git@bitbucket.vih.infineon.com:7999/PROJECT/repo.git
# HTTPS: https://bitbucket.vih.infineon.com/scm/PROJECT/repo.git

_SSH_URL_PATTERN = re.compile(
    r"(?:ssh://)?git@(?P<host>[^:/]+)(?::(?P<port>\d+))?[:/](?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
_HTTPS_URL_PATTERN = re.compile(
    r"https?://(?P<host>[^/]+)/scm/(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)


def parse_clone_url(clone_url: str) -> dict:
    """Extract ``host``, ``project``, and ``repo`` from a clone URL.

    Supports both SSH and HTTPS Bitbucket Server clone URLs.

    Returns
    -------
    dict
        Keys: ``host``, ``project``, ``repo``.

    Raises
    ------
    ValueError
        If the URL cannot be parsed.
    """
    clone_url = clone_url.strip()

    match = _SSH_URL_PATTERN.match(clone_url)
    if match:
        return {
            "host": match.group("host"),
            "project": match.group("project"),
            "repo": match.group("repo"),
        }

    match = _HTTPS_URL_PATTERN.match(clone_url)
    if match:
        return {
            "host": match.group("host"),
            "project": match.group("project"),
            "repo": match.group("repo"),
        }

    raise ValueError(
        f"Cannot parse Bitbucket clone URL: {clone_url!r}. "
        "Expected SSH (ssh://git@host:port/PROJECT/repo.git) "
        "or HTTPS (https://host/scm/PROJECT/repo.git) format."
    )


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------


class BitbucketConnector:
    """Synchronous Bitbucket Server REST API connector.

    Parameters
    ----------
    base_url : str
        Root URL of the Bitbucket Server instance
        (e.g. ``http://bitbucket.vih.infineon.com``).
    project : str
        Bitbucket project key (e.g. ``ASTERISK``).
    repo : str
        Repository slug (e.g. ``asterisk_files``).
    token : str | None
        Personal access token for Bearer authentication.
    username : str | None
        Username for HTTP Basic authentication.
    password : str | None
        Password for HTTP Basic authentication.
    ref : str
        Git ref (branch, tag, or commit) to operate on.
        Defaults to ``"main"``.
    max_retries : int
        Number of retry attempts for transient errors.
    backoff_factor : float
        Exponential back-off multiplier (seconds).
    timeout : float | None
        HTTP request timeout in seconds.
    verify_ssl : bool
        Whether to verify SSL certificates.
    """

    _API_PREFIX = "/rest/api/latest"
    _DEFAULT_MAX_RETRIES = 3
    _DEFAULT_BACKOFF_FACTOR = 1.0
    _BROWSE_PAGE_LIMIT = 500

    def __init__(
        self,
        base_url: str,
        project: str,
        repo: str,
        *,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ref: str = "main",
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        timeout: Optional[float] = 30.0,
        verify_ssl: bool = True,
    ) -> None:
        verify_ssl = enforce_tls_policy(verify_ssl)
        self._base_url = base_url.rstrip("/")
        self._project = project
        self._repo = repo
        self._ref = ref
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor

        # Authentication — prefer token (Bearer); fall back to Basic
        auth: Optional[httpx.Auth] = None
        headers: Dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            auth = httpx.BasicAuth(username=username, password=password)

        self._client = httpx.Client(
            base_url=f"{self._base_url}{self._API_PREFIX}",
            auth=auth,
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )

        self._connected: bool = False
        self._max_workers = get_max_workers("connectors.bitbucket")

        logger.info(
            "BitbucketConnector initialised (base_url=%s, project=%s, "
            "repo=%s, ref=%s)",
            self._base_url,
            self._project,
            self._repo,
            self._ref,
        )

    # ------------------------------------------------------------------
    # Alternate constructor: from environment variables
    # ------------------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        clone_url: Optional[str] = None,
        *,
        ref: str = "main",
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        timeout: Optional[float] = 30.0,
        verify_ssl: bool = True,
    ) -> "BitbucketConnector":
        """Create a connector using IFX credentials from env / ``.env``.

        Reads the following environment variables:

        ==========================  =========================================
        Variable                    Purpose
        ==========================  =========================================
        ``BITBUCKET_BASE_URL``      Base URL (default: ``http://bitbucket.vih.infineon.com``)
        ``BITBUCKET_CLONE_URL``     SSH / HTTPS clone URL (optional, overrides project/repo)
        ``BITBUCKET_PROJECT``       Project key (e.g. ``ASTERISK``)
        ``BITBUCKET_REPO``          Repository slug
        ``BITBUCKET_TOKEN``         Personal access token (preferred auth)
        ``IFX_USERNAME``            Infineon LDAP username (Basic auth fallback)
        ``IFX_PASSWORD``            Infineon LDAP password (Basic auth fallback)
        ``BITBUCKET_REF``           Git ref override (default: *ref* param)
        ==========================  =========================================

        Parameters
        ----------
        clone_url : str, optional
            Clone URL. If not given, falls back to ``BITBUCKET_CLONE_URL``
            env var, then to ``BITBUCKET_BASE_URL`` + project/repo.
        """
        clone_url = clone_url or os.environ.get("BITBUCKET_CLONE_URL", "")
        token = os.environ.get("BITBUCKET_TOKEN")
        username = os.environ.get("IFX_USERNAME")
        password = os.environ.get("IFX_PASSWORD")
        ref = os.environ.get("BITBUCKET_REF", ref)

        if clone_url:
            return cls.from_clone_url(
                clone_url,
                token=token,
                username=username,
                password=password,
                ref=ref,
                max_retries=max_retries,
                backoff_factor=backoff_factor,
                timeout=timeout,
                verify_ssl=verify_ssl,
            )

        base_url = os.environ.get(
            "BITBUCKET_BASE_URL", "http://bitbucket.vih.infineon.com"
        )
        project = os.environ.get("BITBUCKET_PROJECT", "")
        repo = os.environ.get("BITBUCKET_REPO", "")
        if not project or not repo:
            raise BitbucketConnectorError(
                "Either BITBUCKET_CLONE_URL or both BITBUCKET_PROJECT and "
                "BITBUCKET_REPO must be set in environment variables."
            )
        return cls(
            base_url=base_url,
            project=project,
            repo=repo,
            token=token,
            username=username,
            password=password,
            ref=ref,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )

    # ------------------------------------------------------------------
    # Alternate constructor: from a clone URL
    # ------------------------------------------------------------------
    @classmethod
    def from_clone_url(
        cls,
        clone_url: str,
        *,
        scheme: str = "http",
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ref: str = "main",
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        timeout: Optional[float] = 30.0,
        verify_ssl: bool = True,
    ) -> "BitbucketConnector":
        """Create a connector by parsing an SSH or HTTPS clone URL.

        Parameters
        ----------
        clone_url : str
            SSH or HTTPS clone URL, e.g.
            ``ssh://git@bitbucket.vih.infineon.com:7999/PROJ/repo.git``
        scheme : str
            HTTP scheme for the REST API (``"http"`` or ``"https"``).
            Defaults to ``"http"``.

        All other parameters are forwarded to the main constructor.
        """
        parsed = parse_clone_url(clone_url)
        base_url = f"{scheme}://{parsed['host']}"
        return cls(
            base_url=base_url,
            project=parsed["project"],
            repo=parsed["repo"],
            token=token,
            username=username,
            password=password,
            ref=ref,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> "BitbucketConnector":
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
    def validate_connection(self) -> bool:
        """Validate credentials by fetching the repository metadata.

        Returns
        -------
        bool
            ``True`` if the connection is valid.

        Raises
        ------
        BitbucketAuthError
            If authentication fails (HTTP 401/403).
        BitbucketConnectionError
            If the server is unreachable.
        """
        t0 = time.perf_counter()
        try:
            self._request(
                "GET",
                f"/projects/{self._project}/repos/{self._repo}",
            )
            self._connected = True
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "Connection validated successfully (duration_ms=%.1f)",
                elapsed,
            )
            return True
        except BitbucketAuthError:
            self._connected = False
            raise
        except BitbucketConnectorError:
            self._connected = False
            raise
        except Exception as exc:
            self._connected = False
            raise BitbucketConnectionError(
                f"Failed to connect to Bitbucket at {self._base_url}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API — file content
    # ------------------------------------------------------------------
    def get_file_content(
        self,
        path: str,
        *,
        ref: Optional[str] = None,
        project: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> FileContent:
        """Fetch the raw content of a single file.

        Parameters
        ----------
        path : str
            File path relative to the repository root
            (e.g. ``"src/main.c"``).
        ref : str, optional
            Git ref override. Uses the connector default if omitted.
        project : str, optional
            Project key override.
        repo : str, optional
            Repo slug override.

        Returns
        -------
        FileContent
            The file content and metadata.
        """
        project = project or self._project
        repo = repo or self._repo
        ref = ref or self._ref
        path = path.lstrip("/")

        raw = self._request_raw(
            "GET",
            f"/projects/{project}/repos/{repo}/raw/{path}",
            params={"at": ref},
        )
        return FileContent(path=path, content=raw, size=len(raw))

    def get_files_bulk(
        self,
        paths: List[str],
        *,
        ref: Optional[str] = None,
    ) -> Dict[str, FileContent]:
        """Fetch multiple files in parallel.

        Parameters
        ----------
        paths : list[str]
            File paths relative to the repo root.
        ref : str, optional
            Git ref override.

        Returns
        -------
        dict[str, FileContent]
            Mapping of path → content. Failed fetches are logged and
            omitted from the result.
        """
        results: Dict[str, FileContent] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self.get_file_content, p, ref=ref): p
                for p in paths
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    results[path] = future.result()
                except BitbucketConnectorError as exc:
                    logger.warning(
                        "Failed to fetch %s: %s", path, exc
                    )
        return results

    # ------------------------------------------------------------------
    # Public API — directory browsing
    # ------------------------------------------------------------------
    def list_directory(
        self,
        path: str = "",
        *,
        ref: Optional[str] = None,
        project: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> List[FileEntry]:
        """List entries in a directory (non-recursive).

        Parameters
        ----------
        path : str
            Directory path relative to the repo root.
            Empty string for the root directory.
        ref : str, optional
            Git ref override.

        Returns
        -------
        list[FileEntry]
            Entries in the directory.
        """
        project = project or self._project
        repo = repo or self._repo
        ref = ref or self._ref
        path = path.strip("/")

        entries: List[FileEntry] = []
        start = 0
        while True:
            data = self._request(
                "GET",
                f"/projects/{project}/repos/{repo}/browse/{path}",
                params={"at": ref, "start": start, "limit": self._BROWSE_PAGE_LIMIT},
            )
            children = data.get("children", {})
            for val in children.get("values", []):
                entry_path_parts = val.get("path", {})
                name = entry_path_parts.get("toString", entry_path_parts.get("name", ""))
                full_path = f"{path}/{name}" if path else name
                entries.append(
                    FileEntry(
                        path=full_path,
                        entry_type=val.get("type", "UNKNOWN"),
                        size=val.get("size"),
                        content_id=val.get("contentId"),
                        extension=entry_path_parts.get("extension"),
                    )
                )
            if children.get("isLastPage", True):
                break
            start = children.get("nextPageStart", start + self._BROWSE_PAGE_LIMIT)
        return entries

    def get_file_tree(
        self,
        path: str = "",
        *,
        ref: Optional[str] = None,
        extensions: Optional[List[str]] = None,
    ) -> List[FileEntry]:
        """Recursively walk a directory and return all file entries.

        Parameters
        ----------
        path : str
            Starting directory path (empty for repo root).
        ref : str, optional
            Git ref override.
        extensions : list[str], optional
            If provided, only return files matching these extensions
            (e.g. ``["c", "h"]``). Case-insensitive.

        Returns
        -------
        list[FileEntry]
            All files found recursively under *path*.
        """
        ref = ref or self._ref
        ext_set = {e.lower().lstrip(".") for e in extensions} if extensions else None

        result: List[FileEntry] = []
        dirs_to_visit = [path]

        while dirs_to_visit:
            current = dirs_to_visit.pop()
            entries = self.list_directory(current, ref=ref)
            for entry in entries:
                if entry.entry_type == "DIRECTORY":
                    dirs_to_visit.append(entry.path)
                elif entry.entry_type == "FILE":
                    if ext_set is None or (entry.extension and entry.extension.lower() in ext_set):
                        result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Low-level HTTP with retries (AICE-ING-010)
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
    ) -> dict:
        """Issue an HTTP request returning parsed JSON, with retry logic.

        Raises
        ------
        BitbucketAuthError, BitbucketClientError, BitbucketServerError
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, path, params=params)
                self._handle_http_errors(response)
                return response.json()

            except (BitbucketAuthError, BitbucketClientError):
                raise

            except (httpx.RequestError, BitbucketServerError) as exc:
                last_exc = exc
                wait = self._backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Request %s %s failed (attempt %d/%d): %s – "
                    "retrying in %.1fs",
                    method,
                    path,
                    attempt,
                    self._max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)

        raise BitbucketConnectionError(
            f"Request {method} {path} failed after {self._max_retries} "
            f"attempts: {last_exc}"
        ) from last_exc

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
    ) -> str:
        """Issue an HTTP request returning the raw text body, with retries."""
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, path, params=params)
                self._handle_http_errors(response)
                return response.text

            except (BitbucketAuthError, BitbucketClientError):
                raise

            except (httpx.RequestError, BitbucketServerError) as exc:
                last_exc = exc
                wait = self._backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Raw request %s %s failed (attempt %d/%d): %s – "
                    "retrying in %.1fs",
                    method,
                    path,
                    attempt,
                    self._max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)

        raise BitbucketConnectionError(
            f"Raw request {method} {path} failed after {self._max_retries} "
            f"attempts: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # HTTP error classification
    # ------------------------------------------------------------------
    @staticmethod
    def _handle_http_errors(response: httpx.Response) -> None:
        """Raise the appropriate connector exception for HTTP error codes."""
        status = response.status_code
        if status == 401 or status == 403:
            raise BitbucketAuthError(
                f"Authentication failed (HTTP {status}). "
                "Verify your token or credentials."
            )
        if status == 404:
            raise BitbucketClientError(
                f"Not found (HTTP 404): {response.url} – "
                "verify project, repo, path, and ref."
            )
        if 400 <= status < 500:
            raise BitbucketClientError(
                f"Client error {status}: {response.text}"
            )
        if status >= 500:
            raise BitbucketServerError(
                f"Server error {status}: {response.text}"
            )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def project(self) -> str:
        return self._project

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def ref(self) -> str:
        return self._ref

    @ref.setter
    def ref(self, value: str) -> None:
        self._ref = value

    @property
    def base_url(self) -> str:
        return self._base_url

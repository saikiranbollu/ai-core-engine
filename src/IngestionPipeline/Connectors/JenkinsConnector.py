"""Jenkins Connector for the AICE Ingestion Pipeline.

Uses the ``jenkinsapi`` library to communicate with Jenkins CI servers.
Provides:

  * API-token (or password) authentication via HTTP Basic Auth.
  * Connection validation.
  * Job listing and lookup.
  * Build retrieval (latest, by number, etc.).
  * JUnit XML test-result parsing from builds.
  * Build-log (console output) retrieval.
  * Test status and duration extraction.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from jenkinsapi.jenkins import Jenkins
from jenkinsapi.job import Job as JenkinsJob
from jenkinsapi.build import Build as JenkinsBuild
from jenkinsapi.result_set import ResultSet
from jenkinsapi.result import Result
from jenkinsapi.custom_exceptions import (
    JenkinsAPIException,
    NoResults,
    NoBuildData,
    NotFound,
    UnknownJob,
    NotAuthorized,
)

from ..config import get_max_workers

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("aice.ingestion.jenkins")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JenkinsConnectorError(Exception):
    """Base exception for all Jenkins connector errors."""


class JenkinsAuthError(JenkinsConnectorError):
    """Raised when authentication with Jenkins fails."""


class JenkinsClientError(JenkinsConnectorError):
    """Raised for client-side HTTP errors (4xx)."""


class JenkinsServerError(JenkinsConnectorError):
    """Raised for server-side HTTP errors (5xx)."""


class JenkinsConnectionError(JenkinsConnectorError):
    """Raised when a connection to Jenkins cannot be established."""


class JenkinsJobNotFoundError(JenkinsConnectorError):
    """Raised when the requested job does not exist."""


class JenkinsBuildNotFoundError(JenkinsConnectorError):
    """Raised when the requested build does not exist."""


class JenkinsNoResultsError(JenkinsConnectorError):
    """Raised when a build has no published test results."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestStatus(str, Enum):
    """Normalised test-case status values."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    REGRESSION = "REGRESSION"
    FIXED = "FIXED"
    UNKNOWN = "UNKNOWN"


class BuildResult(str, Enum):
    """Jenkins build result values."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNSTABLE = "UNSTABLE"
    ABORTED = "ABORTED"
    NOT_BUILT = "NOT_BUILT"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TestCaseResult:
    """Represents a single parsed JUnit test-case result."""

    class_name: str
    test_name: str
    status: TestStatus
    duration: float  # seconds
    error_message: Optional[str] = None
    error_stacktrace: Optional[str] = None
    identifier: str = ""

    def __post_init__(self) -> None:
        if not self.identifier:
            self.identifier = f"{self.class_name}.{self.test_name}"


@dataclass
class TestSuiteResult:
    """Aggregated results for one test suite (class) within a build."""

    suite_name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0  # seconds
    test_cases: List[TestCaseResult] = field(default_factory=list)


@dataclass
class BuildTestReport:
    """Complete test report extracted from a Jenkins build."""

    job_name: str
    build_number: int
    build_url: str
    build_result: BuildResult
    build_timestamp: Optional[datetime] = None
    build_duration: Optional[timedelta] = None
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    skipped_tests: int = 0
    total_duration: float = 0.0  # seconds, sum of all test durations
    suites: List[TestSuiteResult] = field(default_factory=list)
    test_cases: List[TestCaseResult] = field(default_factory=list)
    console_log: Optional[str] = None


@dataclass
class JobInfo:
    """Lightweight summary of a Jenkins job."""

    name: str
    url: str
    is_running: bool = False
    is_enabled: bool = True
    last_build_number: Optional[int] = None
    last_good_build_number: Optional[int] = None
    last_failed_build_number: Optional[int] = None


# ---------------------------------------------------------------------------
# Core connector
# ---------------------------------------------------------------------------


class JenkinsConnector:
    """Jenkins CI connector using the ``jenkinsapi`` library.

    Wraps ``jenkinsapi.jenkins.Jenkins`` and exposes a clean interface
    for the AICE ingestion pipeline.  Supports:

      * API-token or password-based authentication (HTTP Basic Auth).
      * Connection validation (``validate_connection``).
      * Job listing and lookup.
      * Build retrieval and metadata extraction.
      * JUnit XML test-result parsing from builds.
      * Build-log (console output) retrieval.

    Parameters
    ----------
    base_url : str
        Jenkins server URL (e.g. ``https://jenkins.example.com``).
    username : str
        Jenkins username.
    api_token : str
        Jenkins API token (or password).
    ssl_verify : bool
        Whether to verify SSL certificates.
    timeout : int
        HTTP request timeout in seconds.
    use_crumb : bool
        Whether to use CSRF crumb protection.
    lazy : bool
        If ``True``, skip fetching the full job list on connection.
    max_retries : int | None
        Maximum number of HTTP request retries for transient errors.
    """

    _DEFAULT_TIMEOUT = 30
    _DEFAULT_MAX_RETRIES = 3
    _DEFAULT_BACKOFF_FACTOR = 1.0

    def __init__(
        self,
        base_url: str,
        username: str,
        api_token: str,
        *,
        ssl_verify: bool = True,
        timeout: int = _DEFAULT_TIMEOUT,
        use_crumb: bool = True,
        lazy: bool = True,
        max_retries: Optional[int] = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._api_token = api_token
        self._ssl_verify = ssl_verify
        self._timeout = timeout
        self._use_crumb = use_crumb
        self._lazy = lazy
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor

        self._client: Optional[Jenkins] = None
        self._connected: bool = False
        self._max_workers = get_max_workers("connectors.jenkins")

        logger.info(
            "JenkinsConnector initialised (base_url=%s, user=%s, lazy=%s)",
            self._base_url,
            self._username,
            self._lazy,
        )

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "JenkinsConnector":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        self.close()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> "JenkinsConnector":
        """Create the underlying ``jenkinsapi.Jenkins`` client.

        Returns
        -------
        JenkinsConnector
            ``self`` for method chaining.

        Raises
        ------
        JenkinsAuthError
            If authentication fails.
        JenkinsConnectionError
            If the server is unreachable.
        """
        try:
            self._client = Jenkins(
                baseurl=self._base_url,
                username=self._username,
                password=self._api_token,
                ssl_verify=self._ssl_verify,
                timeout=self._timeout,
                use_crumb=self._use_crumb,
                lazy=self._lazy,
                max_retries=self._max_retries,
            )
            self._connected = True
            logger.info("Connected to Jenkins at %s", self._base_url)
            return self
        except NotAuthorized as exc:
            self._connected = False
            raise JenkinsAuthError(
                f"Authentication failed for user '{self._username}' "
                f"at {self._base_url}: {exc}"
            ) from exc
        except JenkinsAPIException as exc:
            self._connected = False
            raise JenkinsConnectionError(
                f"Failed to connect to Jenkins at {self._base_url}: {exc}"
            ) from exc
        except Exception as exc:
            self._connected = False
            raise JenkinsConnectionError(
                f"Failed to connect to Jenkins at {self._base_url}: {exc}"
            ) from exc

    def close(self) -> None:
        """Release the underlying client."""
        self._client = None
        self._connected = False
        logger.debug("Jenkins client released.")

    @property
    def is_connected(self) -> bool:
        """Whether the connector has an active client."""
        return self._connected and self._client is not None

    def _ensure_connected(self) -> Jenkins:
        """Return the live client, connecting if necessary.

        Raises
        ------
        JenkinsConnectionError
            If no client can be established.
        """
        if self._client is None:
            self.connect()
        assert self._client is not None  # mypy hint
        return self._client

    # ------------------------------------------------------------------
    # Retry wrapper  (AICE-ING-010)
    # ------------------------------------------------------------------

    def _with_retry(self, operation: str, fn, *args, **kwargs):
        """Execute *fn* with retry / exponential back-off.

        Parameters
        ----------
        operation : str
            Human-readable label for logging (e.g. ``"list_jobs"``).
        fn : callable
            The function to invoke.
        *args, **kwargs
            Forwarded to *fn*.

        Non-retryable errors (authentication, not-found) are propagated
        immediately.  Transient errors (``JenkinsAPIException``,
        ``ConnectionError``, ``TimeoutError``, ``OSError``) are retried
        up to ``max_retries`` times with exponential back-off.

        Raises
        ------
        JenkinsConnectionError
            After all retries are exhausted.
        """
        max_retries = self._max_retries or self._DEFAULT_MAX_RETRIES
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)

            except (JenkinsAuthError, JenkinsJobNotFoundError,
                    JenkinsBuildNotFoundError, JenkinsNoResultsError,
                    NotAuthorized):
                # Non-retryable – propagate immediately
                raise

            except (JenkinsAPIException, ConnectionError,
                    TimeoutError, OSError) as exc:
                last_exc = exc
                wait = self._backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s  – retrying in %.1fs",
                    operation, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)

            except Exception as exc:
                last_exc = exc
                wait = self._backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s  – retrying in %.1fs",
                    operation, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)

        logger.error(
            "%s failed after %d retries.", operation, max_retries,
        )
        raise JenkinsConnectionError(
            f"{operation} failed after {max_retries} retries: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Connection validation
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        """Validate that the stored credentials are accepted by Jenkins.

        Performs a lightweight request (version check) against the server.
        Uses retry with exponential back-off for transient failures
        (AICE-ING-010).

        Returns
        -------
        bool
            ``True`` if the connection is valid.

        Raises
        ------
        JenkinsAuthError
            If authentication fails (HTTP 401 / 403).
        JenkinsConnectionError
            If the server cannot be reached.
        """
        def _validate() -> bool:
            client = self._ensure_connected()
            version = client.version
            self._connected = True
            logger.info(
                "Connection validated – Jenkins version %s at %s",
                version,
                self._base_url,
            )
            return True

        try:
            return self._with_retry("validate_connection", _validate)
        except (JenkinsAuthError, JenkinsConnectionError):
            self._connected = False
            raise
        except NotAuthorized as exc:
            self._connected = False
            raise JenkinsAuthError(
                f"Authentication failed for user '{self._username}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Job listing & lookup
    # ------------------------------------------------------------------

    def list_jobs(self) -> List[JobInfo]:
        """Return a list of all jobs visible to the authenticated user.

        .. note:: This can be slow on servers with many jobs because
           it fetches full metadata for each one.  Use
           :meth:`list_job_names` for a lightweight alternative.

        Returns
        -------
        list[JobInfo]
            Summary information for every job.
        """
        def _list() -> List[JobInfo]:
            client = self._ensure_connected()
            jobs: List[JobInfo] = []
            for name, job_obj in client.get_jobs():
                info = self._job_to_info(name, job_obj)
                jobs.append(info)
            logger.info("Listed %d jobs from %s", len(jobs), self._base_url)
            return jobs

        try:
            return self._with_retry("list_jobs", _list)
        except NotAuthorized as exc:
            raise JenkinsAuthError(str(exc)) from exc

    def list_job_names(self) -> List[str]:
        """Return job names visible to the authenticated user (lightweight).

        Unlike :meth:`list_jobs` this only fetches the top-level API
        response and does **not** make per-job HTTP requests, making it
        orders of magnitude faster on servers with many jobs.

        Returns
        -------
        list[str]
            Job names.
        """
        def _list_names() -> List[str]:
            client = self._ensure_connected()
            # client.jobs.keys() uses iterkeys() which does a single
            # poll(tree="jobs[name,color,url]") call and extracts names
            # from the URL locally — no per-job HTTP requests.
            names: List[str] = client.jobs.keys()
            logger.info(
                "Listed %d job names from %s", len(names), self._base_url
            )
            return names

        try:
            return self._with_retry("list_job_names", _list_names)
        except NotAuthorized as exc:
            raise JenkinsAuthError(str(exc)) from exc

    def get_job(self, job_name: str) -> JenkinsJob:
        """Retrieve a single ``jenkinsapi.job.Job`` by name.

        Parameters
        ----------
        job_name : str
            Exact job name as shown in the Jenkins UI.

        Raises
        ------
        JenkinsJobNotFoundError
            If the job does not exist.
        """
        def _get() -> JenkinsJob:
            client = self._ensure_connected()
            try:
                return client.get_job(job_name)
            except (UnknownJob, NotFound) as exc:
                raise JenkinsJobNotFoundError(
                    f"Job '{job_name}' not found on {self._base_url}"
                ) from exc
            except NotAuthorized as exc:
                raise JenkinsAuthError(str(exc)) from exc

        return self._with_retry(f"get_job({job_name})", _get)

    def get_job_info(self, job_name: str) -> JobInfo:
        """Return lightweight metadata for a single job.

        Parameters
        ----------
        job_name : str
            Exact job name.
        """
        job = self.get_job(job_name)
        return self._job_to_info(job_name, job)

    def has_job(self, job_name: str) -> bool:
        """Check whether a job exists on the Jenkins server."""
        client = self._ensure_connected()
        return client.has_job(job_name)

    # ------------------------------------------------------------------
    # Build retrieval
    # ------------------------------------------------------------------

    def get_build(self, job_name: str, build_number: int) -> JenkinsBuild:
        """Return a specific build for a job.

        Raises
        ------
        JenkinsBuildNotFoundError
            If the build number does not exist.
        """
        job = self.get_job(job_name)
        try:
            return job.get_build(build_number)
        except (NoBuildData, NotFound) as exc:
            raise JenkinsBuildNotFoundError(
                f"Build #{build_number} not found for job '{job_name}'"
            ) from exc

    def get_last_build(self, job_name: str) -> JenkinsBuild:
        """Return the most recent build of a job."""
        job = self.get_job(job_name)
        try:
            return job.get_last_build()
        except NoBuildData as exc:
            raise JenkinsBuildNotFoundError(
                f"No builds found for job '{job_name}'"
            ) from exc

    def get_last_completed_build(self, job_name: str) -> JenkinsBuild:
        """Return the most recent completed build of a job."""
        job = self.get_job(job_name)
        try:
            return job.get_last_completed_build()
        except NoBuildData as exc:
            raise JenkinsBuildNotFoundError(
                f"No completed builds found for job '{job_name}'"
            ) from exc

    def get_last_good_build(self, job_name: str) -> JenkinsBuild:
        """Return the most recent successful build of a job."""
        job = self.get_job(job_name)
        try:
            return job.get_last_good_build()
        except NoBuildData as exc:
            raise JenkinsBuildNotFoundError(
                f"No good builds found for job '{job_name}'"
            ) from exc

    def get_build_ids(self, job_name: str) -> List[int]:
        """Return all available build numbers for a job (descending)."""
        job = self.get_job(job_name)
        return sorted(job.get_build_ids(), reverse=True)

    # ------------------------------------------------------------------
    # Build-log (console output) retrieval
    # ------------------------------------------------------------------

    def get_build_console(
        self, job_name: str, build_number: int
    ) -> str:
        """Retrieve the full console output (build log) for a build.

        Parameters
        ----------
        job_name : str
            Jenkins job name.
        build_number : int
            Build number.

        Returns
        -------
        str
            Complete console text.
        """
        build = self.get_build(job_name, build_number)
        log_text = build.get_console()
        logger.debug(
            "Retrieved console log for %s #%d (%d chars)",
            job_name,
            build_number,
            len(log_text),
        )
        return log_text

    def get_last_build_console(self, job_name: str) -> str:
        """Retrieve console output for the most recent build."""
        build = self.get_last_build(job_name)
        return build.get_console()

    # ------------------------------------------------------------------
    # Test-result parsing (JUnit XML via jenkinsapi)
    # ------------------------------------------------------------------

    def get_build_test_report(
        self,
        job_name: str,
        build_number: int,
        *,
        include_console: bool = False,
    ) -> BuildTestReport:
        """Parse JUnit test results from a Jenkins build.

        Uses ``jenkinsapi``'s built-in ``ResultSet`` which reads the
        JUnit XML report published by the build.

        Parameters
        ----------
        job_name : str
            Jenkins job name.
        build_number : int
            Build number to inspect.
        include_console : bool
            If ``True``, also attach the full console log to the report.

        Returns
        -------
        BuildTestReport
            Fully populated test report.

        Raises
        ------
        JenkinsNoResultsError
            If no test results are published for the build.
        """
        build = self.get_build(job_name, build_number)
        return self._parse_build_results(build, job_name, include_console=include_console)

    def get_last_build_test_report(
        self,
        job_name: str,
        *,
        include_console: bool = False,
    ) -> BuildTestReport:
        """Parse JUnit test results from the most recent build."""
        build = self.get_last_build(job_name)
        return self._parse_build_results(build, job_name, include_console=include_console)

    def get_last_completed_build_test_report(
        self,
        job_name: str,
        *,
        include_console: bool = False,
    ) -> BuildTestReport:
        """Parse JUnit test results from the most recent completed build."""
        build = self.get_last_completed_build(job_name)
        return self._parse_build_results(build, job_name, include_console=include_console)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def get_test_reports_for_range(
        self,
        job_name: str,
        build_numbers: Sequence[int],
        *,
        include_console: bool = False,
        skip_missing: bool = True,
    ) -> List[BuildTestReport]:
        """Retrieve test reports for multiple builds.

        Parameters
        ----------
        job_name : str
            Jenkins job name.
        build_numbers : Sequence[int]
            Build numbers to inspect.
        include_console : bool
            Attach console log to each report.
        skip_missing : bool
            If ``True``, silently skip builds that have no test results
            instead of raising ``JenkinsNoResultsError``.

        Returns
        -------
        list[BuildTestReport]
            One report per build that has results.
        """
        reports: List[BuildTestReport] = []

        def _fetch_report(num: int) -> Optional[BuildTestReport]:
            try:
                return self.get_build_test_report(
                    job_name, num, include_console=include_console
                )
            except (JenkinsNoResultsError, JenkinsBuildNotFoundError) as exc:
                if skip_missing:
                    logger.debug(
                        "Skipping build #%d for '%s': %s", num, job_name, exc
                    )
                    return None
                raise

        with ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as executor:
            futures = {
                executor.submit(_fetch_report, num): num
                for num in build_numbers
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    reports.append(result)

        logger.info(
            "Retrieved %d test reports for '%s' (requested %d builds)",
            len(reports),
            job_name,
            len(build_numbers),
        )
        return reports

    # ------------------------------------------------------------------
    # Static / utility: JUnit XML parsing from raw XML string
    # ------------------------------------------------------------------

    @staticmethod
    def parse_junit_xml(xml_content: str) -> List[TestCaseResult]:
        """Parse JUnit XML content into ``TestCaseResult`` objects.

        This method can be used independently of a live Jenkins
        connection to parse locally stored JUnit XML files downloaded
        from build artifacts.

        Parameters
        ----------
        xml_content : str
            Raw JUnit XML string (``<testsuite>`` or ``<testsuites>``).

        Returns
        -------
        list[TestCaseResult]
            Parsed test-case results.
        """
        results: List[TestCaseResult] = []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            logger.error("Failed to parse JUnit XML: %s", exc)
            return results

        # Handle both <testsuites><testsuite>... and bare <testsuite>
        if root.tag == "testsuites":
            suites = root.findall("testsuite")
        elif root.tag == "testsuite":
            suites = [root]
        else:
            logger.warning("Unexpected JUnit XML root tag: %s", root.tag)
            return results

        for suite in suites:
            for testcase in suite.findall("testcase"):
                class_name = testcase.get("classname", "")
                test_name = testcase.get("name", "")
                duration = float(testcase.get("time", "0") or "0")

                # Determine status
                failure = testcase.find("failure")
                error = testcase.find("error")
                skipped = testcase.find("skipped")

                if failure is not None:
                    status = TestStatus.FAILED
                    error_msg = failure.get("message", "")
                    error_trace = failure.text or ""
                elif error is not None:
                    status = TestStatus.FAILED
                    error_msg = error.get("message", "")
                    error_trace = error.text or ""
                elif skipped is not None:
                    status = TestStatus.SKIPPED
                    error_msg = skipped.get("message")
                    error_trace = None
                else:
                    status = TestStatus.PASSED
                    error_msg = None
                    error_trace = None

                results.append(
                    TestCaseResult(
                        class_name=class_name,
                        test_name=test_name,
                        status=status,
                        duration=duration,
                        error_message=error_msg,
                        error_stacktrace=error_trace,
                    )
                )
        logger.debug("Parsed %d test cases from JUnit XML", len(results))
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_build_results(
        self,
        build: JenkinsBuild,
        job_name: str,
        *,
        include_console: bool = False,
    ) -> BuildTestReport:
        """Extract test results from a ``jenkinsapi.build.Build``.

        Parameters
        ----------
        build : JenkinsBuild
            The build object.
        job_name : str
            Job name (for logging / report metadata).
        include_console : bool
            Attach the full console log.

        Returns
        -------
        BuildTestReport
        """
        build_number = build.get_number()
        build_url = build.get_build_url()

        # -- Build metadata -------------------------------------------
        build_result = self._normalise_build_result(build)
        build_timestamp = self._safe_get_timestamp(build)
        build_duration = self._safe_get_duration(build)

        # -- Console log (optional) -----------------------------------
        console_log: Optional[str] = None
        if include_console:
            try:
                console_log = build.get_console()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not retrieve console log for %s #%d: %s",
                    job_name,
                    build_number,
                    exc,
                )

        # -- Test results ---------------------------------------------
        if not build.has_resultset():
            raise JenkinsNoResultsError(
                f"No test results published for '{job_name}' build #{build_number}"
            )

        try:
            result_set: ResultSet = build.get_resultset()
        except NoResults as exc:
            raise JenkinsNoResultsError(
                f"No test results for '{job_name}' build #{build_number}"
            ) from exc

        # Walk all test cases returned by jenkinsapi
        test_cases: List[TestCaseResult] = []
        suites_map: Dict[str, TestSuiteResult] = {}

        for identifier, result in result_set.iteritems():
            tc = self._result_to_test_case(identifier, result)
            test_cases.append(tc)

            # Aggregate into suites by class name
            suite = suites_map.setdefault(
                tc.class_name,
                TestSuiteResult(suite_name=tc.class_name),
            )
            suite.test_cases.append(tc)
            suite.total += 1
            suite.duration += tc.duration
            if tc.status == TestStatus.PASSED:
                suite.passed += 1
            elif tc.status == TestStatus.FAILED:
                suite.failed += 1
            elif tc.status == TestStatus.SKIPPED:
                suite.skipped += 1

        suites = list(suites_map.values())

        total = len(test_cases)
        passed = sum(1 for tc in test_cases if tc.status == TestStatus.PASSED)
        failed = sum(1 for tc in test_cases if tc.status == TestStatus.FAILED)
        skipped = sum(1 for tc in test_cases if tc.status == TestStatus.SKIPPED)
        total_dur = sum(tc.duration for tc in test_cases)

        report = BuildTestReport(
            job_name=job_name,
            build_number=build_number,
            build_url=build_url,
            build_result=build_result,
            build_timestamp=build_timestamp,
            build_duration=build_duration,
            total_tests=total,
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            total_duration=total_dur,
            suites=suites,
            test_cases=test_cases,
            console_log=console_log,
        )

        logger.info(
            "Parsed test report for '%s' #%d – "
            "total=%d passed=%d failed=%d skipped=%d (%.2fs)",
            job_name,
            build_number,
            total,
            passed,
            failed,
            skipped,
            total_dur,
        )
        return report

    # -- Mapping helpers -----------------------------------------------

    @staticmethod
    def _result_to_test_case(
        identifier: str, result: Result
    ) -> TestCaseResult:
        """Convert a ``jenkinsapi.result.Result`` into a ``TestCaseResult``."""
        class_name = getattr(result, "className", "") or ""
        test_name = getattr(result, "name", "") or ""
        raw_status = getattr(result, "status", "UNKNOWN") or "UNKNOWN"

        status_map = {
            "PASSED": TestStatus.PASSED,
            "FIXED": TestStatus.FIXED,
            "FAILED": TestStatus.FAILED,
            "REGRESSION": TestStatus.REGRESSION,
            "SKIPPED": TestStatus.SKIPPED,
        }
        status = status_map.get(raw_status.upper(), TestStatus.UNKNOWN)

        duration = float(getattr(result, "duration", 0) or 0)
        error_msg = getattr(result, "errorDetails", None)
        error_trace = getattr(result, "errorStackTrace", None)

        return TestCaseResult(
            class_name=class_name,
            test_name=test_name,
            status=status,
            duration=duration,
            error_message=error_msg if error_msg else None,
            error_stacktrace=error_trace if error_trace else None,
            identifier=identifier,
        )

    @staticmethod
    def _normalise_build_result(build: JenkinsBuild) -> BuildResult:
        """Map a ``jenkinsapi`` build status string to ``BuildResult``."""
        if build.is_running():
            return BuildResult.RUNNING
        raw = build.get_status()
        if raw is None:
            return BuildResult.UNKNOWN
        mapping = {
            "SUCCESS": BuildResult.SUCCESS,
            "FAILURE": BuildResult.FAILURE,
            "UNSTABLE": BuildResult.UNSTABLE,
            "ABORTED": BuildResult.ABORTED,
            "NOT_BUILT": BuildResult.NOT_BUILT,
        }
        return mapping.get(raw, BuildResult.UNKNOWN)

    @staticmethod
    def _safe_get_timestamp(build: JenkinsBuild) -> Optional[datetime]:
        """Safely extract the build start timestamp."""
        try:
            return build.get_timestamp()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_get_duration(build: JenkinsBuild) -> Optional[timedelta]:
        """Safely extract the build duration."""
        try:
            return build.get_duration()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _job_to_info(name: str, job: JenkinsJob) -> JobInfo:
        """Convert a ``jenkinsapi.job.Job`` to a ``JobInfo`` dataclass."""
        last_build = None
        last_good = None
        last_failed = None
        is_running = False
        is_enabled = True

        try:
            is_running = job.is_running()
        except Exception:  # noqa: BLE001
            pass

        try:
            is_enabled = job.is_enabled()
        except Exception:  # noqa: BLE001
            pass

        try:
            last_build = job.get_last_buildnumber()
        except (NoBuildData, Exception):  # noqa: BLE001
            pass

        try:
            last_good = job.get_last_good_buildnumber()
        except (NoBuildData, Exception):  # noqa: BLE001
            pass

        try:
            last_failed = job.get_last_failed_buildnumber()
        except (NoBuildData, Exception):  # noqa: BLE001
            pass

        return JobInfo(
            name=name,
            url=job.url,
            is_running=is_running,
            is_enabled=is_enabled,
            last_build_number=last_build,
            last_good_build_number=last_good,
            last_failed_build_number=last_failed,
        )

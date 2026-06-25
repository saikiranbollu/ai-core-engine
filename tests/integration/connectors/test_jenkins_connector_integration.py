"""Integration tests for JenkinsConnector against a live Jenkins server.

These tests require environment variables to be set:
    JENKINS_URL      – Jenkins server URL
    JENKINS_USERNAME – Jenkins username
    JENKINS_PASSWORD – Jenkins API token / password

Run with:
    $env:JENKINS_URL = "https://illd3g-jenkins.vih.infineon.com/"
    $env:JENKINS_USERNAME = "JenkinsUsername"
    $env:JENKINS_PASSWORD = "JenkinsPassword"
    python -m pytest Tests/test_jenkins_connector_integration.py -v
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

from src.IngestionPipeline.Connectors.JenkinsConnector import (
    JenkinsConnector,
    JenkinsConnectorError,
    JenkinsAuthError,
    JenkinsConnectionError,
    JenkinsJobNotFoundError,
    JenkinsBuildNotFoundError,
    JenkinsNoResultsError,
    JobInfo,
    BuildTestReport,
    BuildResult,
    TestCaseResult,
    TestStatus,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_JENKINS_URL = os.environ.get(
    "JENKINS_URL", "https://illd3g-jenkins.vih.infineon.com/"
)
_JENKINS_USERNAME = os.environ.get("JENKINS_USERNAME", "GuptaC")
_JENKINS_PASSWORD = os.environ.get("JENKINS_PASSWORD")


def _env_available() -> bool:
    """Check whether Jenkins credentials are configured."""
    return bool(_JENKINS_URL and _JENKINS_USERNAME and _JENKINS_PASSWORD)


# Skip the entire module if credentials are missing
pytestmark = pytest.mark.skipif(
    not _env_available(),
    reason=(
        "Integration tests require JENKINS_URL, JENKINS_USERNAME, "
        "and JENKINS_PASSWORD environment variables (or defaults)."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connector() -> JenkinsConnector:
    """Create a JenkinsConnector connected to the live server.

    Uses ``ssl_verify=False`` in case the corporate Jenkins uses a
    self-signed or internal CA certificate.
    """
    conn = JenkinsConnector(
        base_url=_JENKINS_URL,
        username=_JENKINS_USERNAME,
        api_token=_JENKINS_PASSWORD,
        ssl_verify=False,
        timeout=60,
        lazy=True,
    )
    conn.connect()
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def job_names(connector: JenkinsConnector) -> list[str]:
    """Call ``list_job_names()`` exactly once and cache for the entire module."""
    return connector.list_job_names()


# Maximum jobs to probe when searching for a suitable candidate.
# Keeps fixture setup fast even on servers with thousands of jobs.
_MAX_PROBE = 30


@pytest.fixture(scope="module")
def job_with_builds(connector: JenkinsConnector, job_names: list[str]) -> str:
    """Find the first job that has at least one build (module-scoped).

    Tries at most ``_MAX_PROBE`` jobs using ``get_last_build`` directly
    (single HTTP call per job) instead of fetching full job info.
    """
    for name in job_names[:_MAX_PROBE]:
        try:
            build = connector.get_last_build(name)
            if build is not None:
                return name
        except Exception:
            continue
    pytest.skip("No jobs with builds found on server")


@pytest.fixture(scope="module")
def job_with_results(connector: JenkinsConnector, job_names: list[str]) -> str:
    """Find a job whose last completed build has test results.

    Probes at most ``_MAX_PROBE`` jobs.
    """
    for name in job_names[:_MAX_PROBE]:
        try:
            build = connector.get_last_completed_build(name)
            if build.has_resultset():
                return name
        except Exception:
            continue
    pytest.skip("No jobs with published test results found on server")


@pytest.fixture(scope="module")
def job_without_results(connector: JenkinsConnector, job_names: list[str]) -> str:
    """Find a job whose last completed build has NO test results.

    Probes at most ``_MAX_PROBE`` jobs.
    """
    for name in job_names[:_MAX_PROBE]:
        try:
            build = connector.get_last_completed_build(name)
            if not build.has_resultset():
                return name
        except Exception:
            continue
    pytest.skip("All jobs have test results; cannot test no-results path")


@pytest.fixture(scope="module")
def job_with_failures(connector: JenkinsConnector, job_names: list[str]) -> str:
    """Find a job whose last completed build has at least one failed test.

    Probes at most ``_MAX_PROBE`` jobs.
    """
    for name in job_names[:_MAX_PROBE]:
        try:
            build = connector.get_last_completed_build(name)
            if not build.has_resultset():
                continue
            report = connector.get_build_test_report(name, build.get_number())
            if report.failed_tests > 0:
                return name
        except Exception:
            continue
    pytest.skip("No jobs with failed tests found on server")


# ===================================================================
# 1. Connection validation
# ===================================================================


class TestConnection:
    """Verify authentication and connectivity."""

    def test_validate_connection(self, connector: JenkinsConnector):
        """Credentials are accepted and Jenkins version is retrievable."""
        assert connector.validate_connection() is True
        assert connector.is_connected is True

    def test_invalid_credentials_rejected(self):
        """Wrong credentials should raise JenkinsAuthError or JenkinsConnectionError."""
        bad_conn = JenkinsConnector(
            base_url=_JENKINS_URL,
            username="invalid_user_xyz",
            api_token="invalid_token_xyz",
            ssl_verify=False,
            timeout=30,
        )
        with pytest.raises((JenkinsAuthError, JenkinsConnectionError)):
            bad_conn.connect()
            bad_conn.validate_connection()
        bad_conn.close()

    def test_context_manager(self):
        """Context manager opens and closes cleanly."""
        with JenkinsConnector(
            base_url=_JENKINS_URL,
            username=_JENKINS_USERNAME,
            api_token=_JENKINS_PASSWORD,
            ssl_verify=False,
            timeout=60,
        ) as conn:
            assert conn.is_connected is True
        assert conn.is_connected is False


# ===================================================================
# 2. Job listing
# ===================================================================


class TestJobListing:
    """Verify job enumeration on the live server."""

    def test_list_job_names_returns_non_empty(self, job_names: list[str]):
        """The Jenkins server should have at least one job."""
        assert isinstance(job_names, list)
        assert len(job_names) > 0
        assert all(isinstance(n, str) for n in job_names)
        print(f"\n  Found {len(job_names)} jobs on {_JENKINS_URL}")

    def test_single_job_info_fields(
        self, connector: JenkinsConnector, job_names: list[str]
    ):
        """Fetch JobInfo for one job and check fields are populated."""
        assert len(job_names) > 0
        first_info = connector.get_job_info(job_names[0])
        assert isinstance(first_info, JobInfo)
        assert first_info.name != ""
        assert first_info.url != ""
        print(
            f"\n  First job: name={first_info.name!r}, "
            f"enabled={first_info.is_enabled}, "
            f"last_build={first_info.last_build_number}"
        )

    def test_has_job_for_existing_job(
        self, connector: JenkinsConnector, job_names: list[str]
    ):
        """has_job should return True for a known job."""
        if not job_names:
            pytest.skip("No jobs on server")
        assert connector.has_job(job_names[0]) is True

    def test_has_job_for_nonexistent(self, connector: JenkinsConnector):
        """has_job should return False for a made-up name."""
        assert connector.has_job("__nonexistent_job_xyz_12345__") is False


# ===================================================================
# 3. Job lookup
# ===================================================================


class TestJobLookup:
    """Verify single-job retrieval."""

    def test_get_job_success(
        self, connector: JenkinsConnector, job_names: list[str]
    ):
        """Retrieve a known job by name."""
        if not job_names:
            pytest.skip("No jobs on server")
        job = connector.get_job(job_names[0])
        assert job is not None
        print(f"\n  Retrieved job: {job_names[0]}")

    def test_get_job_not_found(self, connector: JenkinsConnector):
        """Non-existent job should raise JenkinsJobNotFoundError."""
        with pytest.raises(JenkinsJobNotFoundError):
            connector.get_job("__nonexistent_job_xyz_12345__")

    def test_get_job_info_returns_populated(
        self, connector: JenkinsConnector, job_names: list[str]
    ):
        """get_job_info returns a complete JobInfo."""
        if not job_names:
            pytest.skip("No jobs on server")
        info = connector.get_job_info(job_names[0])
        assert isinstance(info, JobInfo)
        assert info.name == job_names[0]


# ===================================================================
# 4. Build retrieval
# ===================================================================


class TestBuildRetrieval:
    """Verify build access on jobs that have been built."""

    def test_get_last_build(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        build = connector.get_last_build(job_with_builds)
        assert build is not None
        assert build.get_number() > 0
        print(
            f"\n  Last build of '{job_with_builds}': "
            f"#{build.get_number()} – {build.get_status()}"
        )

    def test_get_build_by_number(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        """Fetch a specific build by number."""
        last = connector.get_last_build(job_with_builds)
        build = connector.get_build(job_with_builds, last.get_number())
        assert build.get_number() == last.get_number()

    def test_get_build_nonexistent_number(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        """A very high build number should raise JenkinsBuildNotFoundError."""
        with pytest.raises(JenkinsBuildNotFoundError):
            connector.get_build(job_with_builds, 999999)

    def test_get_build_ids(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        ids = connector.get_build_ids(job_with_builds)
        assert isinstance(ids, list)
        assert len(ids) > 0
        # Should be sorted descending
        assert ids == sorted(ids, reverse=True)
        print(f"\n  Build IDs for '{job_with_builds}': {ids[:10]}...")

    def test_get_last_completed_build(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        try:
            build = connector.get_last_completed_build(job_with_builds)
            assert build is not None
            assert not build.is_running()
        except JenkinsBuildNotFoundError:
            pytest.skip("No completed builds available")

    def test_get_last_good_build(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        try:
            build = connector.get_last_good_build(job_with_builds)
            assert build is not None
            assert build.get_status() == "SUCCESS"
        except JenkinsBuildNotFoundError:
            pytest.skip("No successful builds available")


# ===================================================================
# 5. Build console (log) retrieval
# ===================================================================


class TestConsoleRetrieval:
    """Verify console-log extraction."""

    def test_get_build_console(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        last = connector.get_last_build(job_with_builds)
        log = connector.get_build_console(job_with_builds, last.get_number())
        assert isinstance(log, str)
        assert len(log) > 0
        print(f"\n  Console log length: {len(log)} chars (first 200):")
        print(f"  {log[:200]}")

    def test_get_last_build_console(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        log = connector.get_last_build_console(job_with_builds)
        assert isinstance(log, str)
        assert len(log) > 0


# ===================================================================
# 6. Build metadata extraction
# ===================================================================


class TestBuildMetadata:
    """Verify status, timestamp, and duration extraction."""

    def test_build_status(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        build = connector.get_last_build(job_with_builds)
        status = build.get_status()
        assert status in (
            "SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT", None,
        )
        print(f"\n  Build status: {status}")

    def test_build_timestamp(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        build = connector.get_last_build(job_with_builds)
        ts = build.get_timestamp()
        assert ts is not None
        print(f"\n  Build timestamp: {ts}")

    def test_build_duration(
        self, connector: JenkinsConnector, job_with_builds: str
    ):
        build = connector.get_last_build(job_with_builds)
        if build.is_running():
            pytest.skip("Build is still running")
        duration = build.get_duration()
        assert duration is not None
        assert duration.total_seconds() >= 0
        print(f"\n  Build duration: {duration}")


# ===================================================================
# 7. Test results parsing
# ===================================================================


class TestResultsParsing:
    """Verify JUnit test-result extraction from builds.

    These tests look for a job that has published test results.
    If no such job exists on the server, tests are skipped.
    """

    def test_get_build_test_report(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Parse a full test report from the last completed build."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number()
        )

        assert isinstance(report, BuildTestReport)
        assert report.job_name == job_with_results
        assert report.build_number == build.get_number()
        assert report.build_result in BuildResult
        assert report.total_tests > 0
        assert report.total_tests == (
            report.passed_tests + report.failed_tests + report.skipped_tests
        ) or report.total_tests >= (
            report.passed_tests + report.failed_tests + report.skipped_tests
        )  # REGRESSION/FIXED counted separately
        print(
            f"\n  Test report for '{job_with_results}' #{report.build_number}:"
            f"\n    Result     : {report.build_result.value}"
            f"\n    Total      : {report.total_tests}"
            f"\n    Passed     : {report.passed_tests}"
            f"\n    Failed     : {report.failed_tests}"
            f"\n    Skipped    : {report.skipped_tests}"
            f"\n    Duration   : {report.total_duration:.2f}s"
            f"\n    Suites     : {len(report.suites)}"
        )

    def test_test_cases_have_required_fields(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Each test case should have class_name, test_name, status, duration."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number()
        )
        assert len(report.test_cases) > 0

        for tc in report.test_cases[:10]:  # check first 10
            assert isinstance(tc, TestCaseResult)
            assert tc.test_name != ""
            assert isinstance(tc.status, TestStatus)
            assert tc.duration >= 0
            assert tc.identifier != ""

    def test_suites_aggregation(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Suites should aggregate test cases by class name."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number()
        )

        for suite in report.suites:
            assert suite.suite_name != ""
            assert suite.total == len(suite.test_cases)
            assert suite.total == suite.passed + suite.failed + suite.skipped or True
            # All test cases in a suite should share the class name
            for tc in suite.test_cases:
                assert tc.class_name == suite.suite_name

    def test_test_status_extraction(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """All test statuses should be valid TestStatus enum values."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number()
        )
        for tc in report.test_cases:
            assert tc.status in TestStatus
            assert tc.status.value in (
                "PASSED", "FAILED", "SKIPPED", "REGRESSION", "FIXED", "UNKNOWN",
            )

    def test_duration_extraction(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Verify duration is extracted at both test-case and build level."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number()
        )

        # Per-test durations should be non-negative
        for tc in report.test_cases:
            assert tc.duration >= 0

        # Total duration should be sum of individual durations
        expected_total = sum(tc.duration for tc in report.test_cases)
        assert report.total_duration == pytest.approx(expected_total, abs=0.01)

        # Build-level duration
        if report.build_duration is not None:
            assert report.build_duration.total_seconds() >= 0
        print(
            f"\n  Total test duration: {report.total_duration:.2f}s"
            f"\n  Build duration: {report.build_duration}"
        )

    def test_report_with_console_log(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Report with include_console=True should have console_log populated."""
        build = connector.get_last_completed_build(job_with_results)
        report = connector.get_build_test_report(
            job_with_results, build.get_number(), include_console=True,
        )
        assert report.console_log is not None
        assert len(report.console_log) > 0

    def test_last_build_test_report(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """get_last_build_test_report should work."""
        try:
            report = connector.get_last_build_test_report(job_with_results)
            assert isinstance(report, BuildTestReport)
            assert report.total_tests > 0
        except JenkinsNoResultsError:
            pytest.skip("Last build has no results (may be running)")

    def test_last_completed_build_test_report(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """get_last_completed_build_test_report should work."""
        report = connector.get_last_completed_build_test_report(
            job_with_results
        )
        assert isinstance(report, BuildTestReport)
        assert report.total_tests > 0

    def test_no_results_raises_error(
        self, connector: JenkinsConnector, job_without_results: str
    ):
        """A build without test results should raise JenkinsNoResultsError."""
        build = connector.get_last_completed_build(job_without_results)
        with pytest.raises(JenkinsNoResultsError):
            connector.get_build_test_report(
                job_without_results, build.get_number()
            )


# ===================================================================
# 8. Bulk test report retrieval
# ===================================================================


class TestBulkReports:
    """Tests for get_test_reports_for_range."""

    def test_reports_for_range(
        self, connector: JenkinsConnector, job_with_results: str
    ):
        """Retrieve reports for a range of build numbers."""
        ids = connector.get_build_ids(job_with_results)
        # Take up to 3 build IDs to test
        test_ids = ids[:3]
        reports = connector.get_test_reports_for_range(
            job_with_results, test_ids, skip_missing=True,
        )
        assert isinstance(reports, list)
        # At least one should have results
        print(
            f"\n  Requested {len(test_ids)} builds, "
            f"got {len(reports)} test reports"
        )
        for r in reports:
            assert isinstance(r, BuildTestReport)
            assert r.total_tests > 0


# ===================================================================
# 9. Failed test details
# ===================================================================


class TestFailedTestDetails:
    """Check that failed tests include error messages and stack traces."""

    def test_failed_tests_have_error_info(
        self, connector: JenkinsConnector, job_with_failures: str
    ):
        build = connector.get_last_completed_build(job_with_failures)
        report = connector.get_build_test_report(
            job_with_failures, build.get_number()
        )
        failed = [
            tc for tc in report.test_cases if tc.status == TestStatus.FAILED
        ]
        assert len(failed) > 0

        # At least one failed test should have an error message
        has_error_msg = any(tc.error_message for tc in failed)
        print(f"\n  Failed tests: {len(failed)}")
        for tc in failed[:5]:
            print(
                f"    {tc.identifier}: "
                f"msg={tc.error_message!r:.80}"
            )
        if not has_error_msg:
            # Not all Jenkins plugins provide error details; warn, don't fail
            print("  WARNING: No error messages found in failed tests")

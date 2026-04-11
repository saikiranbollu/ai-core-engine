"""Unit tests for the JenkinsConnector module.

All Jenkins API interactions are mocked via ``unittest.mock`` so no
real Jenkins server is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, PropertyMock, patch, call

import pytest

# Allow running from the repo root
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.Connectors.JenkinsConnector import (
    JenkinsConnector,
    JenkinsConnectorError,
    JenkinsAuthError,
    JenkinsClientError,
    JenkinsServerError,
    JenkinsConnectionError,
    JenkinsJobNotFoundError,
    JenkinsBuildNotFoundError,
    JenkinsNoResultsError,
    TestCaseResult,
    TestSuiteResult,
    BuildTestReport,
    JobInfo,
    TestStatus,
    BuildResult,
)

# We also need the jenkinsapi exceptions for mocking
from jenkinsapi.custom_exceptions import (
    JenkinsAPIException,
    NoResults,
    NoBuildData,
    NotFound,
    UnknownJob,
    NotAuthorized,
)


# ---------------------------------------------------------------------------
# Helpers – mock factories
# ---------------------------------------------------------------------------


def _make_mock_result(
    class_name: str = "com.example.Tests",
    name: str = "testSomething",
    status: str = "PASSED",
    duration: float = 0.5,
    error_details: str | None = None,
    error_stack_trace: str | None = None,
) -> MagicMock:
    """Create a mock ``jenkinsapi.result.Result`` object."""
    result = MagicMock()
    result.className = class_name
    result.name = name
    result.status = status
    result.duration = duration
    result.errorDetails = error_details
    result.errorStackTrace = error_stack_trace
    return result


def _make_mock_result_set(
    results: list[tuple[str, MagicMock]] | None = None,
) -> MagicMock:
    """Create a mock ``jenkinsapi.result_set.ResultSet``."""
    rs = MagicMock()
    if results is None:
        results = [
            (
                "com.example.Tests.testPass",
                _make_mock_result(name="testPass", status="PASSED", duration=0.3),
            ),
            (
                "com.example.Tests.testFail",
                _make_mock_result(
                    name="testFail",
                    status="FAILED",
                    duration=1.2,
                    error_details="expected 1 got 2",
                    error_stack_trace="at line 42",
                ),
            ),
            (
                "com.example.Tests.testSkip",
                _make_mock_result(name="testSkip", status="SKIPPED", duration=0.0),
            ),
        ]
    rs.iteritems.return_value = iter(results)
    rs.__len__ = lambda self: len(results)
    return rs


def _make_mock_build(
    build_number: int = 42,
    status: str = "SUCCESS",
    is_running: bool = False,
    has_resultset: bool = True,
    result_set: MagicMock | None = None,
    console: str = "Build log output here",
    timestamp: datetime | None = None,
    duration: timedelta | None = None,
    url: str = "https://jenkins.test/job/test-job/42/",
) -> MagicMock:
    """Create a mock ``jenkinsapi.build.Build``."""
    build = MagicMock()
    build.get_number.return_value = build_number
    build.get_status.return_value = status
    build.is_running.return_value = is_running
    build.get_build_url.return_value = url
    build.get_console.return_value = console
    build.has_resultset.return_value = has_resultset

    if timestamp is None:
        timestamp = datetime(2026, 2, 25, 10, 0, 0, tzinfo=timezone.utc)
    build.get_timestamp.return_value = timestamp

    if duration is None:
        duration = timedelta(minutes=5, seconds=30)
    build.get_duration.return_value = duration

    if result_set is None and has_resultset:
        result_set = _make_mock_result_set()
    build.get_resultset.return_value = result_set
    return build


def _make_mock_job(
    name: str = "test-job",
    url: str = "https://jenkins.test/job/test-job/",
    is_running: bool = False,
    is_enabled: bool = True,
    last_build_number: int = 42,
    last_good_build_number: int = 41,
    last_failed_build_number: int = 40,
    build: MagicMock | None = None,
) -> MagicMock:
    """Create a mock ``jenkinsapi.job.Job``."""
    job = MagicMock()
    job.name = name
    job.url = url
    job.is_running.return_value = is_running
    job.is_enabled.return_value = is_enabled
    job.get_last_buildnumber.return_value = last_build_number
    job.get_last_good_buildnumber.return_value = last_good_build_number
    job.get_last_failed_buildnumber.return_value = last_failed_build_number
    job.get_build_ids.return_value = [42, 41, 40, 39]

    if build is None:
        build = _make_mock_build()
    job.get_build.return_value = build
    job.get_last_build.return_value = build
    job.get_last_completed_build.return_value = build
    job.get_last_good_build.return_value = build
    return job


# ===================================================================
# 1. Data model tests
# ===================================================================


class TestDataModels:
    """Tests for dataclass construction and defaults."""

    def test_test_case_result_auto_identifier(self):
        tc = TestCaseResult(
            class_name="com.Foo", test_name="testBar",
            status=TestStatus.PASSED, duration=1.0,
        )
        assert tc.identifier == "com.Foo.testBar"

    def test_test_case_result_explicit_identifier(self):
        tc = TestCaseResult(
            class_name="com.Foo", test_name="testBar",
            status=TestStatus.PASSED, duration=1.0,
            identifier="custom.id",
        )
        assert tc.identifier == "custom.id"

    def test_test_case_result_with_error(self):
        tc = TestCaseResult(
            class_name="com.Foo", test_name="testBad",
            status=TestStatus.FAILED, duration=2.5,
            error_message="assertion failed",
            error_stacktrace="at line 10",
        )
        assert tc.error_message == "assertion failed"
        assert tc.error_stacktrace == "at line 10"

    def test_test_suite_result_defaults(self):
        suite = TestSuiteResult(suite_name="MySuite")
        assert suite.total == 0
        assert suite.passed == 0
        assert suite.failed == 0
        assert suite.skipped == 0
        assert suite.duration == 0.0
        assert suite.test_cases == []

    def test_build_test_report_defaults(self):
        report = BuildTestReport(
            job_name="job", build_number=1,
            build_url="http://x", build_result=BuildResult.SUCCESS,
        )
        assert report.total_tests == 0
        assert report.console_log is None
        assert report.suites == []
        assert report.test_cases == []

    def test_job_info_defaults(self):
        info = JobInfo(name="j", url="http://x")
        assert info.is_running is False
        assert info.is_enabled is True
        assert info.last_build_number is None

    def test_test_status_enum_values(self):
        assert TestStatus.PASSED == "PASSED"
        assert TestStatus.FAILED == "FAILED"
        assert TestStatus.SKIPPED == "SKIPPED"
        assert TestStatus.REGRESSION == "REGRESSION"
        assert TestStatus.FIXED == "FIXED"
        assert TestStatus.UNKNOWN == "UNKNOWN"

    def test_build_result_enum_values(self):
        assert BuildResult.SUCCESS == "SUCCESS"
        assert BuildResult.FAILURE == "FAILURE"
        assert BuildResult.UNSTABLE == "UNSTABLE"
        assert BuildResult.ABORTED == "ABORTED"
        assert BuildResult.NOT_BUILT == "NOT_BUILT"
        assert BuildResult.RUNNING == "RUNNING"


# ===================================================================
# 2. Exception hierarchy tests
# ===================================================================


class TestExceptions:
    """Verify exception inheritance chain."""

    def test_base_exception(self):
        assert issubclass(JenkinsConnectorError, Exception)

    def test_auth_error(self):
        assert issubclass(JenkinsAuthError, JenkinsConnectorError)

    def test_client_error(self):
        assert issubclass(JenkinsClientError, JenkinsConnectorError)

    def test_server_error(self):
        assert issubclass(JenkinsServerError, JenkinsConnectorError)

    def test_connection_error(self):
        assert issubclass(JenkinsConnectionError, JenkinsConnectorError)

    def test_job_not_found_error(self):
        assert issubclass(JenkinsJobNotFoundError, JenkinsConnectorError)

    def test_build_not_found_error(self):
        assert issubclass(JenkinsBuildNotFoundError, JenkinsConnectorError)

    def test_no_results_error(self):
        assert issubclass(JenkinsNoResultsError, JenkinsConnectorError)


# ===================================================================
# 2b. Retry logic and exponential backoff tests  (AICE-ING-010)
# ===================================================================


class TestRetryAndBackoff:
    """Tests for _with_retry: exponential back-off, transient handling, logging."""

    def test_transient_error_retried_up_to_max(self):
        """JenkinsAPIException is retried max_retries times, then raises."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsAPIException("server error"))

        with pytest.raises(JenkinsConnectionError, match="failed after 3 retries"):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 3

    def test_connection_error_retried(self):
        """ConnectionError is retried."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=2, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=ConnectionError("refused"))

        with pytest.raises(JenkinsConnectionError, match="failed after 2 retries"):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 2

    def test_timeout_error_retried(self):
        """TimeoutError is retried."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=2, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=TimeoutError("timed out"))

        with pytest.raises(JenkinsConnectionError, match="failed after 2 retries"):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 2

    def test_transient_then_success(self):
        """Retry succeeds after transient failure clears."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(
            side_effect=[JenkinsAPIException("blip"), "ok"],
        )

        result = conn._with_retry("test_op", fn)
        assert result == "ok"
        assert fn.call_count == 2

    def test_auth_error_not_retried(self):
        """JenkinsAuthError propagates immediately without retry."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsAuthError("bad creds"))

        with pytest.raises(JenkinsAuthError, match="bad creds"):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 1  # no retry

    def test_not_authorized_not_retried(self):
        """NotAuthorized (jenkinsapi) propagates immediately."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=NotAuthorized("401"))

        with pytest.raises(NotAuthorized):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 1

    def test_job_not_found_not_retried(self):
        """JenkinsJobNotFoundError propagates immediately."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsJobNotFoundError("no such job"))

        with pytest.raises(JenkinsJobNotFoundError):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 1

    def test_build_not_found_not_retried(self):
        """JenkinsBuildNotFoundError propagates immediately."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsBuildNotFoundError("no build"))

        with pytest.raises(JenkinsBuildNotFoundError):
            conn._with_retry("test_op", fn)
        assert fn.call_count == 1

    @patch("IngestionPipeline.Connectors.JenkinsConnector.time.sleep")
    def test_exponential_backoff_timing(self, mock_sleep):
        """Verify sleep durations follow 2^(attempt-1) * factor."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=3, backoff_factor=1.0,
        )
        fn = MagicMock(side_effect=JenkinsAPIException("fail"))

        with pytest.raises(JenkinsConnectionError):
            conn._with_retry("test_op", fn)

        # 3 attempts → 3 sleeps: 1.0, 2.0, 4.0
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(1.0)  # 1.0 * 2^0
        mock_sleep.assert_any_call(2.0)  # 1.0 * 2^1
        mock_sleep.assert_any_call(4.0)  # 1.0 * 2^2

    @patch("IngestionPipeline.Connectors.JenkinsConnector.time.sleep")
    def test_backoff_factor_scaling(self, mock_sleep):
        """Custom backoff_factor scales wait times."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=2, backoff_factor=0.5,
        )
        fn = MagicMock(side_effect=ConnectionError("refused"))

        with pytest.raises(JenkinsConnectionError):
            conn._with_retry("test_op", fn)

        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.5)  # 0.5 * 2^0
        mock_sleep.assert_any_call(1.0)  # 0.5 * 2^1

    def test_retry_logging(self, caplog):
        """Verify warning logs are emitted on each retry attempt."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=2, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsAPIException("transient"))

        import logging
        with caplog.at_level(logging.WARNING, logger="aice.ingestion.jenkins"):
            with pytest.raises(JenkinsConnectionError):
                conn._with_retry("my_operation", fn)

        # Check that warning messages mention the operation and attempt
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) == 2
        assert "my_operation" in warning_msgs[0]
        assert "attempt 1/2" in warning_msgs[0]
        assert "attempt 2/2" in warning_msgs[1]

    def test_retry_error_logging(self, caplog):
        """Verify error log is emitted when all retries exhausted."""
        conn = JenkinsConnector(
            "https://j.test", "u", "t",
            max_retries=1, backoff_factor=0.0,
        )
        fn = MagicMock(side_effect=JenkinsAPIException("fail"))

        import logging
        with caplog.at_level(logging.ERROR, logger="aice.ingestion.jenkins"):
            with pytest.raises(JenkinsConnectionError):
                conn._with_retry("my_operation", fn)

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert any("my_operation" in m and "failed after 1 retries" in m for m in error_msgs)

    def test_default_max_retries_is_3(self):
        """Default max_retries is 3."""
        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn._max_retries == 3

    def test_default_backoff_factor_is_1(self):
        """Default backoff_factor is 1.0."""
        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn._backoff_factor == 1.0


# ===================================================================
# 3. Connector initialisation & connection lifecycle
# ===================================================================


class TestConnectorInit:
    """Test constructor, connect, close, context manager."""

    def test_init_sets_attributes(self):
        conn = JenkinsConnector(
            base_url="https://jenkins.test/",
            username="user",
            api_token="tok",
            ssl_verify=False,
            timeout=60,
        )
        assert conn._base_url == "https://jenkins.test"  # trailing / stripped
        assert conn._username == "user"
        assert conn._api_token == "tok"
        assert conn._ssl_verify is False
        assert conn._timeout == 60
        assert conn._client is None
        assert conn.is_connected is False

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_connect_success(self, mock_jenkins_cls):
        mock_jenkins_cls.return_value = MagicMock()
        conn = JenkinsConnector("https://j.test", "u", "t")
        result = conn.connect()

        assert result is conn  # returns self
        assert conn.is_connected is True
        mock_jenkins_cls.assert_called_once()

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_connect_auth_failure(self, mock_jenkins_cls):
        mock_jenkins_cls.side_effect = NotAuthorized("401")
        conn = JenkinsConnector("https://j.test", "u", "bad")

        with pytest.raises(JenkinsAuthError, match="Authentication failed"):
            conn.connect()
        assert conn.is_connected is False

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_connect_api_exception(self, mock_jenkins_cls):
        mock_jenkins_cls.side_effect = JenkinsAPIException("server down")
        conn = JenkinsConnector("https://j.test", "u", "t")

        with pytest.raises(JenkinsConnectionError, match="Failed to connect"):
            conn.connect()
        assert conn.is_connected is False

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_connect_generic_exception(self, mock_jenkins_cls):
        mock_jenkins_cls.side_effect = ConnectionError("network")
        conn = JenkinsConnector("https://j.test", "u", "t")

        with pytest.raises(JenkinsConnectionError):
            conn.connect()

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_close(self, mock_jenkins_cls):
        mock_jenkins_cls.return_value = MagicMock()
        conn = JenkinsConnector("https://j.test", "u", "t")
        conn.connect()
        assert conn.is_connected is True

        conn.close()
        assert conn.is_connected is False
        assert conn._client is None

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_context_manager(self, mock_jenkins_cls):
        mock_jenkins_cls.return_value = MagicMock()

        with JenkinsConnector("https://j.test", "u", "t") as conn:
            assert conn.is_connected is True
        # After exiting, should be closed
        assert conn.is_connected is False

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_ensure_connected_auto_connects(self, mock_jenkins_cls):
        mock_jenkins_cls.return_value = MagicMock()
        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn._client is None

        client = conn._ensure_connected()
        assert client is not None
        assert conn.is_connected is True


# ===================================================================
# 4. Connection validation
# ===================================================================


class TestValidateConnection:
    """Tests for validate_connection()."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_validate_success(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.version = "2.401.1"
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn.validate_connection() is True
        assert conn.is_connected is True

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_validate_auth_failure(self, mock_jenkins_cls):
        mock_client = MagicMock()
        type(mock_client).version = PropertyMock(
            side_effect=NotAuthorized("403")
        )
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "bad")
        with pytest.raises(JenkinsAuthError):
            conn.validate_connection()
        assert conn.is_connected is False

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_validate_generic_failure(self, mock_jenkins_cls):
        mock_client = MagicMock()
        type(mock_client).version = PropertyMock(
            side_effect=RuntimeError("unreachable")
        )
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t", backoff_factor=0.0)
        with pytest.raises(JenkinsConnectionError, match="failed after 3 retries"):
            conn.validate_connection()


# ===================================================================
# 5. Job listing & lookup
# ===================================================================


class TestJobOperations:
    """Tests for list_jobs, get_job, get_job_info, has_job."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_list_jobs(self, mock_jenkins_cls):
        job1 = _make_mock_job(name="job-a")
        job2 = _make_mock_job(name="job-b", is_running=True)
        mock_client = MagicMock()
        mock_client.get_jobs.return_value = iter([("job-a", job1), ("job-b", job2)])
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        jobs = conn.list_jobs()

        assert len(jobs) == 2
        assert jobs[0].name == "job-a"
        assert jobs[0].is_running is False
        assert jobs[1].name == "job-b"
        assert jobs[1].is_running is True
        assert all(isinstance(j, JobInfo) for j in jobs)

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_list_jobs_auth_error(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.get_jobs.side_effect = NotAuthorized("forbidden")
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsAuthError):
            conn.list_jobs()

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_list_job_names(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.jobs.keys.return_value = ["alpha", "beta", "gamma"]
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t", backoff_factor=0.0)
        names = conn.list_job_names()

        assert names == ["alpha", "beta", "gamma"]
        mock_client.jobs.keys.assert_called_once()

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_list_job_names_empty(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.jobs.keys.return_value = []
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t", backoff_factor=0.0)
        names = conn.list_job_names()

        assert names == []

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_job_success(self, mock_jenkins_cls):
        mock_job = _make_mock_job(name="my-job")
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        job = conn.get_job("my-job")
        assert job is mock_job

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_job_not_found(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.get_job.side_effect = UnknownJob("no-exist")
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsJobNotFoundError, match="no-exist"):
            conn.get_job("no-exist")

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_job_info(self, mock_jenkins_cls):
        mock_job = _make_mock_job(
            name="info-job", last_build_number=99,
            last_good_build_number=98, is_enabled=True,
        )
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        info = conn.get_job_info("info-job")
        assert isinstance(info, JobInfo)
        assert info.name == "info-job"
        assert info.last_build_number == 99
        assert info.last_good_build_number == 98
        assert info.is_enabled is True

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_has_job(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.has_job.return_value = True
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn.has_job("exists") is True
        mock_client.has_job.assert_called_with("exists")

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_has_job_false(self, mock_jenkins_cls):
        mock_client = MagicMock()
        mock_client.has_job.return_value = False
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        assert conn.has_job("nope") is False


# ===================================================================
# 6. Build retrieval
# ===================================================================


class TestBuildRetrieval:
    """Tests for get_build, get_last_build, etc."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_build(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=10)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        build = conn.get_build("job", 10)
        assert build is mock_build
        mock_job.get_build.assert_called_with(10)

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_build_not_found(self, mock_jenkins_cls):
        mock_job = MagicMock()
        mock_job.get_build.side_effect = NoBuildData("job")
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsBuildNotFoundError, match="Build #999"):
            conn.get_build("job", 999)

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_last_build(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=50)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        build = conn.get_last_build("job")
        assert build is mock_build

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_last_build_no_builds(self, mock_jenkins_cls):
        mock_job = MagicMock()
        mock_job.get_last_build.side_effect = NoBuildData("job")
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsBuildNotFoundError, match="No builds found"):
            conn.get_last_build("job")

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_last_completed_build(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=48)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        build = conn.get_last_completed_build("job")
        assert build is mock_build

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_last_good_build(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=47, status="SUCCESS")
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        build = conn.get_last_good_build("job")
        assert build is mock_build

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_build_ids(self, mock_jenkins_cls):
        mock_job = _make_mock_job()
        mock_job.get_build_ids.return_value = [39, 40, 41, 42]
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        ids = conn.get_build_ids("job")
        assert ids == [42, 41, 40, 39]  # descending


# ===================================================================
# 7. Console (build log) retrieval
# ===================================================================


class TestConsoleRetrieval:
    """Tests for get_build_console, get_last_build_console."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_build_console(self, mock_jenkins_cls):
        mock_build = _make_mock_build(console="Started by user admin\nFinished: SUCCESS")
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        log = conn.get_build_console("job", 42)
        assert "Started by user admin" in log
        assert "Finished: SUCCESS" in log

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_get_last_build_console(self, mock_jenkins_cls):
        mock_build = _make_mock_build(console="Last build log")
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        log = conn.get_last_build_console("job")
        assert log == "Last build log"


# ===================================================================
# 8. Test-result parsing (via jenkinsapi ResultSet)
# ===================================================================


class TestBuildTestReport:
    """Tests for get_build_test_report and _parse_build_results."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_basic_test_report(self, mock_jenkins_cls):
        mock_build = _make_mock_build(
            build_number=42, status="UNSTABLE", has_resultset=True,
        )
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_build_test_report("job", 42)

        assert isinstance(report, BuildTestReport)
        assert report.job_name == "job"
        assert report.build_number == 42
        assert report.build_result == BuildResult.UNSTABLE
        assert report.total_tests == 3
        assert report.passed_tests == 1
        assert report.failed_tests == 1
        assert report.skipped_tests == 1
        assert report.console_log is None  # not requested

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_test_report_with_console(self, mock_jenkins_cls):
        mock_build = _make_mock_build(
            build_number=42, console="Build output",
        )
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_build_test_report("job", 42, include_console=True)
        assert report.console_log == "Build output"

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_test_report_no_results(self, mock_jenkins_cls):
        mock_build = _make_mock_build(has_resultset=False)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsNoResultsError, match="No test results"):
            conn.get_build_test_report("job", 42)

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_test_report_suites_aggregation(self, mock_jenkins_cls):
        """Verify test cases are aggregated into suites by class name."""
        results = [
            ("com.A.test1", _make_mock_result(class_name="com.A", name="test1", status="PASSED", duration=0.1)),
            ("com.A.test2", _make_mock_result(class_name="com.A", name="test2", status="FAILED", duration=0.2)),
            ("com.B.test3", _make_mock_result(class_name="com.B", name="test3", status="PASSED", duration=0.3)),
        ]
        rs = _make_mock_result_set(results)
        mock_build = _make_mock_build(has_resultset=True, result_set=rs)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_build_test_report("job", 42)

        assert len(report.suites) == 2
        suite_a = next(s for s in report.suites if s.suite_name == "com.A")
        suite_b = next(s for s in report.suites if s.suite_name == "com.B")
        assert suite_a.total == 2
        assert suite_a.passed == 1
        assert suite_a.failed == 1
        assert suite_b.total == 1
        assert suite_b.passed == 1

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_test_report_duration_extraction(self, mock_jenkins_cls):
        results = [
            ("com.X.t1", _make_mock_result(class_name="com.X", name="t1", duration=1.5)),
            ("com.X.t2", _make_mock_result(class_name="com.X", name="t2", duration=2.5)),
        ]
        rs = _make_mock_result_set(results)
        mock_build = _make_mock_build(
            has_resultset=True, result_set=rs,
            duration=timedelta(minutes=3),
        )
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_build_test_report("job", 42)

        assert report.total_duration == pytest.approx(4.0)
        assert report.build_duration == timedelta(minutes=3)

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_test_report_timestamp_extraction(self, mock_jenkins_cls):
        ts = datetime(2026, 1, 15, 8, 30, 0, tzinfo=timezone.utc)
        mock_build = _make_mock_build(timestamp=ts)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_build_test_report("job", 42)
        assert report.build_timestamp == ts

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_last_build_test_report(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=99)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_last_build_test_report("job")
        assert report.build_number == 99

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_last_completed_build_test_report(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=98)
        mock_job = _make_mock_job(build=mock_build)
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        report = conn.get_last_completed_build_test_report("job")
        assert report.build_number == 98


# ===================================================================
# 9. Bulk test report retrieval
# ===================================================================


class TestBulkReports:
    """Tests for get_test_reports_for_range."""

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_range_skips_missing(self, mock_jenkins_cls):
        build_ok = _make_mock_build(build_number=1, has_resultset=True)
        build_no_results = _make_mock_build(build_number=2, has_resultset=False)

        mock_job = MagicMock()
        mock_job.get_build.side_effect = lambda n: build_ok if n == 1 else build_no_results
        mock_job.get_last_build.return_value = build_ok
        mock_job.get_last_completed_build.return_value = build_ok
        mock_job.get_last_good_build.return_value = build_ok
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        reports = conn.get_test_reports_for_range("job", [1, 2])
        assert len(reports) == 1
        assert reports[0].build_number == 1

    @patch("IngestionPipeline.Connectors.JenkinsConnector.Jenkins")
    def test_range_raises_when_not_skipping(self, mock_jenkins_cls):
        mock_build = _make_mock_build(build_number=1, has_resultset=False)
        mock_job = MagicMock()
        mock_job.get_build.return_value = mock_build
        mock_client = MagicMock()
        mock_client.get_job.return_value = mock_job
        mock_jenkins_cls.return_value = mock_client

        conn = JenkinsConnector("https://j.test", "u", "t")
        with pytest.raises(JenkinsNoResultsError):
            conn.get_test_reports_for_range("job", [1], skip_missing=False)


# ===================================================================
# 10. JUnit XML static parser
# ===================================================================


class TestJunitXmlParser:
    """Tests for the static parse_junit_xml method."""

    def test_single_testsuite_all_pass(self):
        xml = (
            '<testsuite name="MySuite" tests="2">'
            '  <testcase classname="com.Foo" name="test1" time="0.5"/>'
            '  <testcase classname="com.Foo" name="test2" time="1.0"/>'
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 2
        assert all(r.status == TestStatus.PASSED for r in results)
        assert results[0].class_name == "com.Foo"
        assert results[0].test_name == "test1"
        assert results[0].duration == pytest.approx(0.5)

    def test_failure_element(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="t" time="1.2">'
            '    <failure message="expected 1 got 2">stack trace</failure>'
            "  </testcase>"
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 1
        assert results[0].status == TestStatus.FAILED
        assert results[0].error_message == "expected 1 got 2"
        assert results[0].error_stacktrace == "stack trace"

    def test_error_element(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="t" time="0.1">'
            '    <error message="NPE">NullPointerException at ...</error>'
            "  </testcase>"
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 1
        assert results[0].status == TestStatus.FAILED
        assert results[0].error_message == "NPE"

    def test_skipped_element(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="t" time="0">'
            "    <skipped/>"
            "  </testcase>"
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 1
        assert results[0].status == TestStatus.SKIPPED

    def test_testsuites_wrapper(self):
        xml = (
            "<testsuites>"
            '  <testsuite name="S1">'
            '    <testcase classname="A" name="t1" time="0.1"/>'
            "  </testsuite>"
            '  <testsuite name="S2">'
            '    <testcase classname="B" name="t2" time="0.2"/>'
            "  </testsuite>"
            "</testsuites>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 2
        assert results[0].class_name == "A"
        assert results[1].class_name == "B"

    def test_mixed_results(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="pass" time="0.5"/>'
            '  <testcase classname="C" name="fail" time="1.0">'
            '    <failure message="bad"/>'
            "  </testcase>"
            '  <testcase classname="C" name="skip" time="0">'
            '    <skipped message="TODO"/>'
            "  </testcase>"
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert len(results) == 3
        statuses = {r.test_name: r.status for r in results}
        assert statuses["pass"] == TestStatus.PASSED
        assert statuses["fail"] == TestStatus.FAILED
        assert statuses["skip"] == TestStatus.SKIPPED

    def test_invalid_xml_returns_empty(self):
        results = JenkinsConnector.parse_junit_xml("not valid xml <<>")
        assert results == []

    def test_unexpected_root_tag(self):
        results = JenkinsConnector.parse_junit_xml("<root><child/></root>")
        assert results == []

    def test_empty_time_attribute(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="t" time=""/>'
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert results[0].duration == 0.0

    def test_missing_time_attribute(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="C" name="t"/>'
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert results[0].duration == 0.0

    def test_auto_identifier(self):
        xml = (
            "<testsuite>"
            '  <testcase classname="com.example.Suite" name="testMethod" time="0.1"/>'
            "</testsuite>"
        )
        results = JenkinsConnector.parse_junit_xml(xml)
        assert results[0].identifier == "com.example.Suite.testMethod"


# ===================================================================
# 11. Internal helper: _result_to_test_case
# ===================================================================


class TestResultMapping:
    """Tests for _result_to_test_case static method."""

    def test_passed_result(self):
        mock_r = _make_mock_result(status="PASSED", duration=0.42)
        tc = JenkinsConnector._result_to_test_case("id.1", mock_r)
        assert tc.status == TestStatus.PASSED
        assert tc.duration == pytest.approx(0.42)
        assert tc.error_message is None

    def test_failed_result_with_details(self):
        mock_r = _make_mock_result(
            status="FAILED",
            error_details="assertion error",
            error_stack_trace="at line 5",
        )
        tc = JenkinsConnector._result_to_test_case("id.2", mock_r)
        assert tc.status == TestStatus.FAILED
        assert tc.error_message == "assertion error"
        assert tc.error_stacktrace == "at line 5"

    def test_regression_status(self):
        mock_r = _make_mock_result(status="REGRESSION")
        tc = JenkinsConnector._result_to_test_case("id.3", mock_r)
        assert tc.status == TestStatus.REGRESSION

    def test_fixed_status(self):
        mock_r = _make_mock_result(status="FIXED")
        tc = JenkinsConnector._result_to_test_case("id.4", mock_r)
        assert tc.status == TestStatus.FIXED

    def test_unknown_status(self):
        mock_r = _make_mock_result(status="WEIRD")
        tc = JenkinsConnector._result_to_test_case("id.5", mock_r)
        assert tc.status == TestStatus.UNKNOWN

    def test_none_duration_defaults_to_zero(self):
        mock_r = _make_mock_result()
        mock_r.duration = None
        tc = JenkinsConnector._result_to_test_case("id.6", mock_r)
        assert tc.duration == 0.0


# ===================================================================
# 12. Internal helper: _normalise_build_result
# ===================================================================


class TestBuildResultNormalisation:
    """Tests for _normalise_build_result static method."""

    def test_running_build(self):
        build = MagicMock()
        build.is_running.return_value = True
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.RUNNING

    def test_success(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = "SUCCESS"
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.SUCCESS

    def test_failure(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = "FAILURE"
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.FAILURE

    def test_unstable(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = "UNSTABLE"
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.UNSTABLE

    def test_aborted(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = "ABORTED"
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.ABORTED

    def test_none_status(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = None
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.UNKNOWN

    def test_unrecognised_status(self):
        build = MagicMock()
        build.is_running.return_value = False
        build.get_status.return_value = "NEW_STATUS"
        assert JenkinsConnector._normalise_build_result(build) == BuildResult.UNKNOWN


# ===================================================================
# 13. Internal helper: _job_to_info
# ===================================================================


class TestJobToInfo:
    """Tests for _job_to_info static method."""

    def test_full_info(self):
        job = _make_mock_job(
            name="j1", is_running=True, is_enabled=False,
            last_build_number=100, last_good_build_number=99,
            last_failed_build_number=98,
        )
        info = JenkinsConnector._job_to_info("j1", job)
        assert info.name == "j1"
        assert info.is_running is True
        assert info.is_enabled is False
        assert info.last_build_number == 100
        assert info.last_good_build_number == 99
        assert info.last_failed_build_number == 98

    def test_job_with_no_builds(self):
        job = MagicMock()
        job.url = "http://j/job/x/"
        job.is_running.return_value = False
        job.is_enabled.return_value = True
        job.get_last_buildnumber.side_effect = NoBuildData("x")
        job.get_last_good_buildnumber.side_effect = NoBuildData("x")
        job.get_last_failed_buildnumber.side_effect = NoBuildData("x")

        info = JenkinsConnector._job_to_info("x", job)
        assert info.last_build_number is None
        assert info.last_good_build_number is None
        assert info.last_failed_build_number is None

    def test_job_with_exceptions_in_accessors(self):
        """Ensure _job_to_info is resilient to unexpected errors."""
        job = MagicMock()
        job.url = "http://j/job/y/"
        job.is_running.side_effect = RuntimeError("boom")
        job.is_enabled.side_effect = RuntimeError("boom")
        job.get_last_buildnumber.side_effect = RuntimeError("boom")
        job.get_last_good_buildnumber.side_effect = RuntimeError("boom")
        job.get_last_failed_buildnumber.side_effect = RuntimeError("boom")

        info = JenkinsConnector._job_to_info("y", job)
        assert info.is_running is False
        assert info.is_enabled is True
        assert info.last_build_number is None


# ===================================================================
# 14. Safe helpers
# ===================================================================


class TestSafeHelpers:
    """Tests for _safe_get_timestamp and _safe_get_duration."""

    def test_safe_timestamp_ok(self):
        build = MagicMock()
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        build.get_timestamp.return_value = ts
        assert JenkinsConnector._safe_get_timestamp(build) == ts

    def test_safe_timestamp_error(self):
        build = MagicMock()
        build.get_timestamp.side_effect = RuntimeError("no ts")
        assert JenkinsConnector._safe_get_timestamp(build) is None

    def test_safe_duration_ok(self):
        build = MagicMock()
        dur = timedelta(seconds=90)
        build.get_duration.return_value = dur
        assert JenkinsConnector._safe_get_duration(build) == dur

    def test_safe_duration_error(self):
        build = MagicMock()
        build.get_duration.side_effect = RuntimeError("no dur")
        assert JenkinsConnector._safe_get_duration(build) is None

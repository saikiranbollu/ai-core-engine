"""Tests for log sanitizer (Ticket 4)."""

import logging

from src.Observability.log_sanitizer import SensitiveDataFilter, install_log_sanitizer


class TestSensitiveDataFilter:
    """Verify that the filter masks secrets in log output."""

    def setup_method(self):
        self.filt = SensitiveDataFilter()

    def _sanitize(self, text: str) -> str:
        return self.filt._sanitize(text)

    # ── URL-embedded credentials ──────────────────────────────────────
    def test_redis_url_password(self):
        result = self._sanitize("redis://:MySecretPwd@redis.cluster:6379/0")
        assert "MySecretPwd" not in result
        assert "*****" in result
        assert "redis.cluster:6379/0" in result

    def test_https_user_password(self):
        result = self._sanitize("https://admin:s3cret@example.com/api")
        assert "s3cret" not in result
        assert "*****" in result

    # ── Key=value pairs ───────────────────────────────────────────────
    def test_password_equals(self):
        result = self._sanitize("Connecting with password=hunter2 to db")
        assert "hunter2" not in result
        assert "password=*****" in result

    def test_api_key_colon(self):
        result = self._sanitize("api_key: 5gVeIkhyIVOFaXpeAjSz903aZ5hSpf05")
        assert "5gVeIkhyIVOFaXpeAjSz903aZ5hSpf05" not in result
        assert "api_key: *****" in result

    def test_token_equals(self):
        result = self._sanitize("token=abc123xyz")
        assert "abc123xyz" not in result

    def test_client_secret(self):
        result = self._sanitize("client_secret=top-secret-value")
        assert "top-secret-value" not in result

    # ── Bearer tokens ─────────────────────────────────────────────────
    def test_bearer_token(self):
        result = self._sanitize("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9")
        assert "eyJhbGciOiJSUzI1NiJ9" not in result
        assert "Bearer *****" in result

    # ── Safe text passes through unchanged ────────────────────────────
    def test_normal_text_unchanged(self):
        text = "Connected to Neo4j at bolt+ssc://example.com:443"
        assert self._sanitize(text) == text

    def test_empty_string(self):
        assert self._sanitize("") == ""

    # ── install_log_sanitizer is idempotent ───────────────────────────
    def test_install_idempotent(self):
        root = logging.getLogger()
        before = len(root.filters)
        install_log_sanitizer()
        install_log_sanitizer()  # second call should not add duplicate
        after = len(root.filters)
        assert after - before <= 1

    # ── Filter works on LogRecord ─────────────────────────────────────
    def test_filter_log_record(self):
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Connecting to redis://:secret123@host:6379",
            args=(), exc_info=None,
        )
        self.filt.filter(record)
        assert "secret123" not in record.msg

"""
Log Sanitizer — Sprint 10
==========================
Logging filter that automatically masks sensitive data (passwords, API keys,
tokens, credentials) in log output before it reaches any handler.

Usage:
    from src.Observability.log_sanitizer import install_log_sanitizer
    install_log_sanitizer()   # call once at startup

This attaches a SensitiveDataFilter to the root logger so ALL log messages
across the entire application are sanitized transparently.
"""
from __future__ import annotations

import logging
import re
from typing import List, Pattern

# ── Patterns that match sensitive data in log messages ─────────────────
# Each tuple: (compiled regex, replacement string)
# Order matters — more specific patterns first.

_PATTERNS: List[tuple[Pattern, str]] = [
    # URL-embedded credentials:  redis://:password@host  or  https://user:pass@host
    (re.compile(
        r"(://[^:/@]*:)[^@/]+(@)",
        re.IGNORECASE,
    ), r"\1*****\2"),

    # Key=value pairs (query strings, config, log messages)
    # Matches: password=xxx, api_key=xxx, secret=xxx, token=xxx, etc.
    (re.compile(
        r"((?:password|passwd|pwd|api_key|apikey|api_secret|apisecret"
        r"|secret|token|access_token|refresh_token|auth_token"
        r"|credentials?|private_key|client_secret)\s*[=:]\s*)"
        r"(\S+)",
        re.IGNORECASE,
    ), r"\1*****"),

    # Bearer tokens:  Authorization: Bearer eyJhbG...
    (re.compile(
        r"(Bearer\s+)\S+",
        re.IGNORECASE,
    ), r"\1*****"),

    # Standalone JWT-like strings (eyJ...base64...base64)
    (re.compile(
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b",
    ), "*****"),
]


class SensitiveDataFilter(logging.Filter):
    """Logging filter that masks secrets in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._sanitize(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._sanitize(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    @staticmethod
    def _sanitize(value) -> str:
        if not isinstance(value, str):
            return value
        result = value
        for pattern, replacement in _PATTERNS:
            result = pattern.sub(replacement, result)
        return result


def install_log_sanitizer() -> None:
    """Attach the SensitiveDataFilter to the root logger (idempotent)."""
    root = logging.getLogger()
    # Avoid duplicate installation
    for f in root.filters:
        if isinstance(f, SensitiveDataFilter):
            return
    root.addFilter(SensitiveDataFilter())

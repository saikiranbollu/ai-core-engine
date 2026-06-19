"""TLS verification configuration — secure-by-default using the Infineon CA bundle.

Centralises the decision of what to pass to ``httpx`` / ``requests`` as the
``verify=`` parameter so that every outbound connector makes the same,
secure-by-default choice (finding F-CF-X01, workstream W-06).

``get_verify_setting`` precedence:

1. ``AICE_ALLOW_INSECURE_TLS=1``  → verification DISABLED (development only).
2. Infineon CA bundle present     → return its path (``str``).
3. Otherwise                      → ``True`` (use the system trust store).

``enforce_tls_policy`` is the construction-time guard used by connectors: it
refuses an explicit ``verify_ssl=False`` unless ``AICE_ALLOW_INSECURE_TLS=1``
is set, so credentials and source code are never transmitted with TLS
validation silently disabled.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# Repo-root-relative path to the Infineon CA bundle shipped with HybridRAG.
# This file lives at ``<root>/src/_common/tls_config.py`` so ``parents[2]`` is
# the repository root.
_CA_BUNDLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "HybridRAG" / "code" / "ca-bundle.crt"
)

# Environment flag that explicitly permits insecure TLS (development only).
_INSECURE_ENV = "AICE_ALLOW_INSECURE_TLS"


def insecure_tls_allowed() -> bool:
    """Return ``True`` when ``AICE_ALLOW_INSECURE_TLS=1`` is set."""
    return os.environ.get(_INSECURE_ENV) == "1"


def get_verify_setting() -> Union[bool, str]:
    """Return a value suitable for the httpx/requests ``verify=`` parameter.

    See the module docstring for the precedence rules.
    """
    if insecure_tls_allowed():
        logger.warning(
            "[TLS] %s=1 — TLS verification DISABLED. Credentials and source "
            "code in flight are NOT protected. Do not use in production.",
            _INSECURE_ENV,
        )
        return False
    if _CA_BUNDLE_PATH.is_file():
        return str(_CA_BUNDLE_PATH)
    logger.info(
        "[TLS] Infineon CA bundle not found at %s — using system trust store.",
        _CA_BUNDLE_PATH,
    )
    return True


def enforce_tls_policy(verify_ssl: Union[bool, str]) -> Union[bool, str]:
    """Refuse insecure TLS unless explicitly allowed via env.

    Connectors call this at construction time. ``verify_ssl=False`` is only
    permitted when ``AICE_ALLOW_INSECURE_TLS=1`` is set; otherwise a
    ``ValueError`` is raised. A CA-bundle path (``str``) or ``True`` always
    passes through unchanged.
    """
    if verify_ssl is False and not insecure_tls_allowed():
        raise ValueError(
            "verify_ssl=False requires AICE_ALLOW_INSECURE_TLS=1 (development "
            "only). In production, use the Infineon CA bundle "
            "(verify_ssl defaults to True)."
        )
    return verify_ssl


__all__ = [
    "get_verify_setting",
    "enforce_tls_policy",
    "insecure_tls_allowed",
]

"""Unit tests for src/_common/tls_config.py and connector TLS enforcement.

Workstream W-06 / finding F-CF-X01: outbound connectors must verify TLS by
default and may only disable it via the explicit AICE_ALLOW_INSECURE_TLS=1
development override.
"""
from __future__ import annotations

import pytest

from src._common import tls_config


@pytest.fixture(autouse=True)
def _clear_insecure_env(monkeypatch):
    """Ensure a clean, secure-by-default environment for every test."""
    monkeypatch.delenv("AICE_ALLOW_INSECURE_TLS", raising=False)
    yield


# ---------------------------------------------------------------------------
# get_verify_setting
# ---------------------------------------------------------------------------

def test_get_verify_setting_secure_default():
    """Without the override, returns the CA bundle path or True (never False)."""
    result = tls_config.get_verify_setting()
    assert result is True or (
        isinstance(result, str) and result.endswith("ca-bundle.crt")
    )
    assert result is not False


def test_get_verify_setting_insecure_env(monkeypatch):
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    assert tls_config.get_verify_setting() is False


def test_get_verify_setting_returns_ca_bundle_path_when_present(monkeypatch, tmp_path):
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("dummy")
    monkeypatch.setattr(tls_config, "_CA_BUNDLE_PATH", bundle)
    assert tls_config.get_verify_setting() == str(bundle)


def test_get_verify_setting_missing_bundle_returns_true(monkeypatch, tmp_path):
    monkeypatch.setattr(tls_config, "_CA_BUNDLE_PATH", tmp_path / "nope.crt")
    assert tls_config.get_verify_setting() is True


# ---------------------------------------------------------------------------
# enforce_tls_policy
# ---------------------------------------------------------------------------

def test_enforce_tls_policy_true_passes():
    assert tls_config.enforce_tls_policy(True) is True


def test_enforce_tls_policy_ca_path_passes():
    assert tls_config.enforce_tls_policy("/etc/ssl/ca.crt") == "/etc/ssl/ca.crt"


def test_enforce_tls_policy_false_refused_without_env():
    with pytest.raises(ValueError, match="AICE_ALLOW_INSECURE_TLS"):
        tls_config.enforce_tls_policy(False)


def test_enforce_tls_policy_false_allowed_with_env(monkeypatch):
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    assert tls_config.enforce_tls_policy(False) is False


def test_insecure_tls_allowed(monkeypatch):
    assert tls_config.insecure_tls_allowed() is False
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    assert tls_config.insecure_tls_allowed() is True


# ---------------------------------------------------------------------------
# Connector wiring — the guard is enforced at construction time
# ---------------------------------------------------------------------------

def test_bitbucket_connector_refuses_insecure_tls():
    from src.IngestionPipeline.Connectors.BitbucketConnector import BitbucketConnector

    with pytest.raises(ValueError, match="AICE_ALLOW_INSECURE_TLS"):
        BitbucketConnector(
            base_url="https://bitbucket.example.com",
            project="P",
            repo="r",
            verify_ssl=False,
        )


def test_bitbucket_connector_allows_insecure_tls_with_env(monkeypatch):
    monkeypatch.setenv("AICE_ALLOW_INSECURE_TLS", "1")
    from src.IngestionPipeline.Connectors.BitbucketConnector import BitbucketConnector

    conn = BitbucketConnector(
        base_url="https://bitbucket.example.com",
        project="P",
        repo="r",
        verify_ssl=False,
    )
    assert conn is not None


def test_jama_connector_refuses_insecure_tls():
    from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector

    with pytest.raises(ValueError, match="AICE_ALLOW_INSECURE_TLS"):
        JamaConnector(
            base_url="https://jama.example.com",
            api_key="id",
            api_secret="secret",
            verify_ssl=False,
        )


def test_polarion_connector_refuses_insecure_tls():
    from src.IngestionPipeline.Connectors.PolarionConnector import PolarionConnector

    with pytest.raises(ValueError, match="AICE_ALLOW_INSECURE_TLS"):
        PolarionConnector(
            base_url="https://polarion.example.com",
            token="t",
            verify_ssl=False,
        )


def test_connectors_construct_securely_by_default():
    """Default construction (verify_ssl=True) must not raise."""
    from src.IngestionPipeline.Connectors.BitbucketConnector import BitbucketConnector

    conn = BitbucketConnector(base_url="https://x", project="P", repo="r")
    assert conn is not None

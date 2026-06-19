"""Validation tests for the W-12 silent-failure monitoring artifacts (Sprint 27).

These are config-only deliverables (Prometheus alert rules + their wiring into
prometheus.yml + a Grafana dashboard), so the tests parse the files and assert
structure — and crucially that every alert targets a metric that is *actually
emitted* by the MCP server, not an aspirational name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_RULES = _ROOT / "monitoring" / "prometheus" / "aice_silent_failures.yml"
_PROM = _ROOT / "monitoring" / "prometheus.yml"
_DASH = _ROOT / "monitoring" / "grafana" / "dashboards" / "aice-silent-failures.json"

# Metrics actually emitted by the MCP server — see src/Observability/metrics.py
# and mcp/core/mcp_server.py. Alerts must reference these (plus Prometheus's
# built-in ``up``), never aspirational names.
_REAL_METRICS = {
    "up",
    "aice_backend_up",
    "aice_tool_requests_total",
    "aice_error_total",
    "aice_ingestion_files_total",
}


def _load_rules() -> dict:
    return yaml.safe_load(_RULES.read_text(encoding="utf-8"))


def _all_rules() -> list[dict]:
    data = _load_rules()
    return [r for g in data.get("groups", []) for r in g.get("rules", [])]


class TestSilentFailureRules:
    def test_rules_file_exists(self):
        assert _RULES.exists(), f"alert rules not found at {_RULES}"

    def test_rules_valid_yaml_with_groups(self):
        data = _load_rules()
        assert isinstance(data, dict)
        groups = data.get("groups")
        assert groups and isinstance(groups, list), "no rule groups defined"

    def test_every_rule_well_formed(self):
        rules = _all_rules()
        assert len(rules) >= 5, f"expected >=5 alerts, found {len(rules)}"
        for r in rules:
            name = r.get("alert")
            assert name, f"rule missing alert name: {r}"
            assert r.get("expr"), f"{name} missing expr"
            assert "for" in r, f"{name} missing 'for'"
            assert r.get("labels", {}).get("severity") in {"critical", "warning", "info"}, \
                f"{name} missing/invalid severity"
            ann = r.get("annotations", {})
            assert ann.get("summary") and ann.get("description"), \
                f"{name} missing summary/description"

    def test_alerts_reference_real_metrics(self):
        for r in _all_rules():
            expr = r["expr"]
            assert any(m in expr for m in _REAL_METRICS), \
                f"{r['alert']} references no known-emitted metric: {expr!r}"

    def test_has_service_and_backend_down_alerts(self):
        names = {r["alert"] for r in _all_rules()}
        assert "AICEServiceDown" in names
        assert "AICEBackendDown" in names


class TestPrometheusWiring:
    def test_rule_files_wired(self):
        cfg = yaml.safe_load(_PROM.read_text(encoding="utf-8"))
        assert "rule_files" in cfg, "prometheus.yml missing rule_files"
        assert cfg["rule_files"], "rule_files is empty"

    def test_scrape_config_preserved(self):
        # W-12 must not break the existing Sprint 10 scrape config.
        content = _PROM.read_text(encoding="utf-8")
        assert "mcp-server:8000" in content
        assert "/metrics" in content


class TestSilentFailureDashboard:
    def test_dashboard_exists_and_valid_json(self):
        assert _DASH.exists(), f"dashboard not found at {_DASH}"
        data = json.loads(_DASH.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_dashboard_panels_and_uid(self):
        data = json.loads(_DASH.read_text(encoding="utf-8"))
        assert len(data.get("panels", [])) >= 6, "expected >=6 panels"
        assert data.get("uid") == "aice-silent-failures"

    def test_dashboard_references_real_metrics(self):
        content = _DASH.read_text(encoding="utf-8")
        assert "aice_backend_up" in content
        assert "aice_tool_requests_total" in content

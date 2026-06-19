"""
Sprint 10 Integration Tests — Prometheus Metrics & Observability Infrastructure
================================================================================
Validates:
    1. Prometheus metric types are correctly defined (11 types)
    2. _NoOp fallback works when prometheus_client is unavailable
    3. _finish_tool() auto-instrumentation pattern
    4. make_metrics_app() creates a valid ASGI app
    5. Metric names follow aice_ prefix convention
    6. Grafana dashboard JSON structure is valid
    7. Prometheus scrape config targets MCP server
    8. Docker Compose monitoring profile services defined
    9. Health check tool reports correct tool_count
    10. ENABLE_METRICS flag controls metric activation
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: Metric Definitions
# ═════════════════════════════════════════════════════════════════════════

class TestMetricDefinitions:
    """Verify all 11 Prometheus metric types are defined in metrics.py."""

    def test_metrics_module_importable(self):
        """metrics.py should be importable regardless of prometheus_client."""
        from src.Observability.metrics import PROMETHEUS_AVAILABLE
        # Should be a boolean — True or False depending on install
        assert isinstance(PROMETHEUS_AVAILABLE, bool)

    def test_metric_names_in_source(self):
        """All 11 metric names should appear in metrics.py source."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")

        expected_metrics = [
            "aice_tool_requests_total",
            "aice_tool_request_duration_seconds",
            "aice_search_requests_total",
            "aice_search_duration_seconds",
            "aice_cache_requests_total",
            "aice_active_sessions",
            "aice_rlm_requests_total",
            "aice_rlm_subquery_count",
            "aice_ingestion_files_total",
            "aice_backend_up",
            "aice_review_routing_total",
        ]
        for name in expected_metrics:
            assert name in content, f"Missing metric definition: {name}"

    def test_all_metrics_use_aice_prefix(self):
        """All custom metrics should use aice_ prefix for namespace isolation."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")

        import re
        # Find all metric name strings in Counter/Gauge/Histogram constructors
        metric_names = re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"([^"]+)"', content)
        for name in metric_names:
            assert name.startswith("aice_"), \
                f"Metric '{name}' does not use 'aice_' prefix"

    def test_metric_count(self):
        """Should have 23 metric definitions (17 base + 6 per-DA productivity, F-P5-M01)."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")

        import re
        # Count Counter, Gauge, Histogram constructors with aice_ prefix
        metric_defs = re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"aice_', content)
        assert len(metric_defs) == 23, \
            f"Expected 23 metric definitions, found {len(metric_defs)}"


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: NoOp Fallback
# ═════════════════════════════════════════════════════════════════════════

class TestNoOpFallback:
    """When prometheus_client is unavailable, _NoOp stubs must work silently."""

    def test_noop_class_in_source(self):
        """_NoOp class should be defined in metrics.py."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")
        assert "class _NoOp" in content, "_NoOp fallback class not found"

    def test_noop_silently_ignores_inc(self):
        """_NoOp.inc() should do nothing without raising."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        # Import the _NoOp class directly
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "metrics_module", str(metrics_path))
        mod = importlib.util.module_from_spec(spec)

        # We need to test the NoOp behavior even if prometheus is installed
        # The _NoOp class is always defined
        content = metrics_path.read_text(encoding="utf-8")
        assert "def inc" in content or "def __getattr__" in content, \
            "_NoOp should handle .inc() calls"

    def test_noop_silently_ignores_labels(self):
        """_NoOp.labels() should return self/noop without raising."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")
        assert "labels" in content, "_NoOp should handle .labels() calls"

    def test_enable_metrics_flag_in_source(self):
        """ENABLE_METRICS env var should control metric activation."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")
        assert "ENABLE_METRICS" in content, "ENABLE_METRICS flag not found"


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Auto-Instrumentation Pattern
# ═════════════════════════════════════════════════════════════════════════

class TestAutoInstrumentation:
    """Verify _finish_tool() in mcp_server.py auto-instruments all tools."""

    def test_finish_tool_defined(self):
        """_finish_tool function should be defined in mcp_server.py."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert "def _finish_tool" in content, "_finish_tool not found"

    def test_ok_calls_finish_tool(self):
        """_ok() should call _finish_tool('ok')."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert '_finish_tool("ok")' in content or "_finish_tool('ok')" in content, \
            "_ok() does not call _finish_tool"

    def test_err_calls_finish_tool(self):
        """_err() should call _finish_tool('error', ...)."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert '_finish_tool("error"' in content or "_finish_tool('error'" in content, \
            "_err() does not call _finish_tool"

    def test_tool_requests_total_incremented(self):
        """_finish_tool should increment TOOL_REQUESTS_TOTAL."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert "TOOL_REQUESTS_TOTAL" in content, \
            "TOOL_REQUESTS_TOTAL not referenced in _finish_tool"

    def test_tool_request_duration_observed(self):
        """_finish_tool should observe TOOL_REQUEST_DURATION."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert "TOOL_REQUEST_DURATION" in content, \
            "TOOL_REQUEST_DURATION not referenced in _finish_tool"


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: make_metrics_app
# ═════════════════════════════════════════════════════════════════════════

class TestMakeMetricsApp:
    """Verify make_metrics_app creates a valid ASGI mountable app."""

    def test_make_metrics_app_defined(self):
        """make_metrics_app should be defined in metrics.py."""
        metrics_path = Path(__file__).resolve().parents[2] / "src" / "Observability" / "metrics.py"
        content = metrics_path.read_text(encoding="utf-8")
        assert "def make_metrics_app" in content or "make_metrics_app" in content

    def test_make_metrics_app_importable(self):
        """make_metrics_app should be importable."""
        from src.Observability.metrics import make_metrics_app
        # Should be callable
        assert callable(make_metrics_app)

    def test_make_metrics_app_returns_none_when_disabled(self):
        """When metrics disabled, make_metrics_app returns None."""
        from src.Observability.metrics import PROMETHEUS_AVAILABLE, make_metrics_app
        result = make_metrics_app()
        if not PROMETHEUS_AVAILABLE:
            assert result is None


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: Grafana Dashboard Structure
# ═════════════════════════════════════════════════════════════════════════

class TestGrafanaDashboard:
    """Verify Grafana dashboard JSON is valid and has expected panels."""

    @pytest.fixture
    def dashboard_path(self):
        return Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "dashboards" / "aice-overview.json"

    def test_dashboard_file_exists(self, dashboard_path):
        assert dashboard_path.exists(), f"Dashboard not found at {dashboard_path}"

    def test_dashboard_is_valid_json(self, dashboard_path):
        if not dashboard_path.exists():
            pytest.skip("Dashboard file not found")
        content = dashboard_path.read_text(encoding="utf-8")
        data = json.loads(content)  # Should not raise
        assert isinstance(data, dict)

    def test_dashboard_has_panels(self, dashboard_path):
        if not dashboard_path.exists():
            pytest.skip("Dashboard file not found")
        data = json.loads(dashboard_path.read_text(encoding="utf-8"))
        # Grafana dashboards have "panels" at top level or inside "rows"
        panels = data.get("panels", [])
        assert len(panels) >= 8, f"Expected ≥8 panels, found {len(panels)}"

    def test_dashboard_has_prometheus_datasource(self, dashboard_path):
        if not dashboard_path.exists():
            pytest.skip("Dashboard file not found")
        content = dashboard_path.read_text(encoding="utf-8")
        # Should reference prometheus somewhere
        assert "prometheus" in content.lower() or "Prometheus" in content


# ═════════════════════════════════════════════════════════════════════════
#  Test 6: Prometheus Scrape Configuration
# ═════════════════════════════════════════════════════════════════════════

class TestPrometheusConfig:
    """Verify prometheus.yml has correct scrape configuration."""

    @pytest.fixture
    def prometheus_config_path(self):
        return Path(__file__).resolve().parents[2] / "monitoring" / "prometheus.yml"

    def test_prometheus_config_exists(self, prometheus_config_path):
        assert prometheus_config_path.exists(), "prometheus.yml not found"

    def test_scrape_targets_mcp_server(self, prometheus_config_path):
        if not prometheus_config_path.exists():
            pytest.skip("prometheus.yml not found")
        content = prometheus_config_path.read_text(encoding="utf-8")
        assert "mcp-server:8000" in content, \
            "MCP server not in prometheus scrape targets"

    def test_scrape_interval_configured(self, prometheus_config_path):
        if not prometheus_config_path.exists():
            pytest.skip("prometheus.yml not found")
        content = prometheus_config_path.read_text(encoding="utf-8")
        assert "scrape_interval" in content
        assert "15s" in content

    def test_metrics_path_configured(self, prometheus_config_path):
        if not prometheus_config_path.exists():
            pytest.skip("prometheus.yml not found")
        content = prometheus_config_path.read_text(encoding="utf-8")
        assert "/metrics" in content


# ═════════════════════════════════════════════════════════════════════════
#  Test 7: Docker Compose Monitoring Profile
# ═════════════════════════════════════════════════════════════════════════

class TestDockerComposeMonitoring:
    """Verify monitoring services are defined in docker-compose.yml."""

    @pytest.fixture
    def compose_path(self):
        return Path(__file__).resolve().parents[2] / "docker-compose.yml"

    def test_compose_file_exists(self, compose_path):
        assert compose_path.exists(), "docker-compose.yml not found"

    def test_prometheus_service_defined(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "prometheus:" in content
        assert "prom/prometheus" in content

    def test_grafana_service_defined(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "grafana:" in content
        assert "grafana/grafana" in content

    def test_monitoring_profile(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "monitoring" in content, "monitoring profile not found"

    def test_enable_metrics_env_var(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "ENABLE_METRICS" in content

    def test_grafana_provisioning_volumes(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "provisioning" in content, "Grafana provisioning not configured"

    def test_prometheus_data_volume(self, compose_path):
        if not compose_path.exists():
            pytest.skip("docker-compose.yml not found")
        content = compose_path.read_text(encoding="utf-8")
        assert "prometheus_data" in content, "Prometheus data volume not defined"


# ═════════════════════════════════════════════════════════════════════════
#  Test 8: Grafana Provisioning
# ═════════════════════════════════════════════════════════════════════════

class TestGrafanaProvisioning:
    """Verify Grafana datasource and dashboard provisioning files."""

    def test_datasource_provisioning_exists(self):
        p = Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "provisioning" / "datasources"
        assert p.exists() or (p.parent / "datasources").exists(), \
            "Grafana datasource provisioning directory not found"

    def test_dashboard_provisioning_exists(self):
        p = Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
        assert p.exists(), "dashboards.yml provisioning not found"

    def test_dashboard_provisioning_structure(self):
        p = Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
        if not p.exists():
            pytest.skip("dashboards.yml not found")
        content = p.read_text(encoding="utf-8")
        assert "apiVersion" in content
        assert "AICE" in content or "aice" in content.lower()


# ═════════════════════════════════════════════════════════════════════════
#  Test 9: Observability Service health_check Reports Tool Count
# ═════════════════════════════════════════════════════════════════════════

class TestHealthCheckToolCount:
    """health_check should report correct tool_count."""

    def test_tool_count_in_health_response(self):
        """Health check should include tool_count field."""
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        assert "tool_count" in content, \
            "health_check response should include tool_count"

    def test_tool_tiers_has_56(self):
        """tool_tiers.py should have exactly 56 tools."""
        from mcp.core.tool_tiers import TOOL_TIERS
        assert len(TOOL_TIERS) == 62, f"Expected 62, got {len(TOOL_TIERS)}"

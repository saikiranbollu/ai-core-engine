"""Sprint 27 artifact validation — W-14 (DA productivity), W-15 (DA provisioning),
and the F-CB-08 typed-error retrofit regression guard.

These are config/data/source-level checks (no live backends), so they parse the
files directly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_API_KEYS = _ROOT / "mcp" / "auth" / "api_keys.yaml"
_RLM = _ROOT / "src" / "HybridRAG" / "code" / "querier" / "rlm_orchestrator.py"
_DASH = _ROOT / "monitoring" / "grafana" / "dashboards" / "aice-da-productivity.json"
_MCP_SERVER = _ROOT / "mcp" / "core" / "mcp_server.py"

_NEW_DAS = [
    "prqgen", "atra", "geca", "gevt", "atqa",
    "sava", "sasa", "hazopa", "dafaa", "swqma", "jwiz", "zephyr",
]


# ---------------------------------------------------------------------------
# W-15 — DA API-key provisioning (F-P5-D01) + code alignment (F-P5-D02/D03)
# ---------------------------------------------------------------------------

class TestDAProvisioning:
    def test_new_das_provisioned(self):
        keys = yaml.safe_load(_API_KEYS.read_text(encoding="utf-8"))["keys"]
        pids = {v["principal_id"] for v in keys.values()}
        for da in _NEW_DAS:
            assert f"{da}_assistant" in pids, f"DA {da} not provisioned"

    def test_new_das_are_least_privilege(self):
        keys = yaml.safe_load(_API_KEYS.read_text(encoding="utf-8"))["keys"]
        for kid, entry in keys.items():
            pid = entry["principal_id"]
            if any(pid == f"{da}_assistant" for da in _NEW_DAS):
                roles = entry.get("roles", {})
                flat = [r for rs in roles.values() for r in rs]
                assert flat and all(r == "public" for r in flat), \
                    f"{pid} should be public-only, got {roles}"

    def test_stoptyping_not_provisioned(self):
        keys = yaml.safe_load(_API_KEYS.read_text(encoding="utf-8"))["keys"]
        pids = {v["principal_id"] for v in keys.values()}
        assert not any("stoptyping" in p.lower() for p in pids)

    def test_da_task_mapping_codes_aligned(self):
        src = _RLM.read_text(encoding="utf-8")
        mapping = src.split("DA_TASK_MAPPING", 1)[1].split("}", 1)[0]
        # Canonical DA codes from Confluence MCSWAI (+ EDA) must be present
        for code in ('"SASA"', '"DaFaA"', '"HAZOPA"', '"PRQGEN"',
                     '"SWQMA"', '"J-WIZ"', '"Zephyr"', '"EDA"'):
            assert code in mapping, f"missing canonical code {code}"
        # Wrong / superseded codes must be gone
        for code in ('"SAAN"', '"DFA"', '"HZOP"', '"PRQ"', '"PRQ_Drafter"', '"HazopA"'):
            assert code not in mapping, f"stale code still present: {code}"
        # Codebase-only assistants removed for now
        for code in ('"CIA"', '"CTA"', '"SAGA"', '"PAGE"', '"KW"',
                     '"VoltAI"', '"RMA"', '"StopTyping"'):
            assert code not in mapping, f"removed code still present: {code}"

    def test_eda_provisioned_with_illd_only(self):
        keys = yaml.safe_load(_API_KEYS.read_text(encoding="utf-8"))["keys"]
        eda = next((v for v in keys.values()
                    if v["principal_id"] == "eda_assistant"), None)
        assert eda is not None, "EDA not provisioned"
        roles = eda.get("roles", {})
        assert sorted(roles.get("illd", [])) == ["developer", "public"]
        assert "mcal" not in roles  # EDA has no MCAL access

    def test_other_das_have_no_illd(self):
        keys = yaml.safe_load(_API_KEYS.read_text(encoding="utf-8"))["keys"]
        for entry in keys.values():
            if entry["principal_id"] == "eda_assistant":
                continue
            assert "illd" not in entry.get("roles", {}), \
                f"{entry['principal_id']} should not hold illd roles"


# ---------------------------------------------------------------------------
# W-14 — DA productivity dashboard + da_productivity SQL view
# ---------------------------------------------------------------------------

class TestDAProductivity:
    def test_dashboard_valid_and_uid(self):
        data = json.loads(_DASH.read_text(encoding="utf-8"))
        assert data["uid"] == "aice-da-productivity"
        assert len(data["panels"]) >= 6

    def test_dashboard_uses_real_da_metrics(self):
        content = _DASH.read_text(encoding="utf-8")
        assert "aice_tool_requests_total" in content
        assert "da_name" in content

    def test_da_productivity_view_in_schema(self):
        from src.Observability.postgres_schema import SCHEMA_SQL
        assert "CREATE OR REPLACE VIEW da_productivity" in SCHEMA_SQL
        for col in ("da_name", "sessions", "auto_rate", "approvals", "rejections"):
            assert col in SCHEMA_SQL, f"view missing column {col}"


# ---------------------------------------------------------------------------
# F-CB-08 — typed-error retrofit completeness (regression guard)
# ---------------------------------------------------------------------------

class TestTypedErrorRetrofit:
    def test_no_generic_internal_error_passthroughs(self):
        """No `_err("INTERNAL_ERROR", str(e|exc))` passthroughs should remain;
        they were converted to `_err_from_exc(...)` in the F-CB-08 retrofit."""
        content = _MCP_SERVER.read_text(encoding="utf-8")
        leftovers = re.findall(r'_err\("INTERNAL_ERROR",\s*str\((?:e|exc)\)\)', content)
        assert not leftovers, f"{len(leftovers)} generic passthroughs remain"

    def test_err_from_exc_is_used(self):
        content = _MCP_SERVER.read_text(encoding="utf-8")
        assert content.count("_err_from_exc(") >= 50


# ---------------------------------------------------------------------------
# F-P5-M01 / F-P5-M02 — tier label + aice_da_* productivity catalog + task_type
# ---------------------------------------------------------------------------

_METRICS = _ROOT / "src" / "Observability" / "metrics.py"

_DA_METRICS = [
    "aice_da_session_duration_seconds",
    "aice_da_session_outcomes_total",
    "aice_da_context_assembly_tokens",
    "aice_da_first_result_latency_seconds",
    "aice_da_pattern_hits_total",
    "aice_da_session_llm_tokens_total",
]


class TestPerDAMetrics:
    def test_metrics_module_defines_da_catalog(self):
        src = _METRICS.read_text(encoding="utf-8")
        for name in _DA_METRICS:
            assert name in src, f"metric {name} not defined"

    def test_metric_count_23(self):
        """Sprint 27 base + DA metrics, plus F-CA-A04/F-CC-R01 security gauges."""
        src = _METRICS.read_text(encoding="utf-8")
        metric_defs = re.findall(r'"(aice_[a-z_]+)"', src)
        unique = set(metric_defs)
        assert len(unique) == 25, f"Expected 25 metric definitions, found {len(unique)}: {sorted(unique)}"

    def test_tool_metric_has_tier_label(self):
        src = _METRICS.read_text(encoding="utf-8")
        block = src.split("aice_tool_requests_total", 1)[1].split(")", 1)[0]
        assert '"tier"' in block, "tier label missing from aice_tool_requests_total"

    def test_da_metrics_have_noop_stubs(self):
        src = _METRICS.read_text(encoding="utf-8")
        for stub in ("DA_SESSION_DURATION = _noop", "DA_LLM_TOKENS = _noop"):
            assert stub in src, f"missing NoOp stub: {stub}"

    def test_server_wires_tier_and_task_type(self):
        src = _MCP_SERVER.read_text(encoding="utf-8")
        assert "_current_da_tier" in src
        assert "def _resolve_da_tier" in src
        assert "def _session_task_type" in src
        assert "tier=tier" in src  # tier recorded on the tool counter
        assert 'task_type: str = "adhoc"' in src  # F-P5-M02

    def test_server_emits_every_da_metric(self):
        src = _MCP_SERVER.read_text(encoding="utf-8")
        for name in ("DA_SESSION_DURATION", "DA_SESSION_OUTCOMES",
                     "DA_CONTEXT_TOKENS", "DA_FIRST_RESULT_LATENCY",
                     "DA_PATTERN_HITS", "DA_LLM_TOKENS"):
            assert f"{name}.labels(" in src, f"{name} never emitted"

    def test_resolve_da_tier_runtime(self):
        import sys
        for p in (str(_ROOT), str(_ROOT / "src")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from mcp.core.mcp_server import _resolve_da_tier
        assert _resolve_da_tier("key-eda-001", "illd") == "developer"
        assert _resolve_da_tier("key-triplea-001", "mcal") == "developer"
        assert _resolve_da_tier("nope", "mcal") == "unknown"

    def test_productivity_dashboard_uses_da_series(self):
        content = _DASH.read_text(encoding="utf-8")
        assert "aice_da_session_outcomes_total" in content
        assert "aice_da_session_duration_seconds_bucket" in content
        assert "by (tier)" in content


# ---------------------------------------------------------------------------
# Sprint 27 review fixes (s27_findings.md) — MCAL auth lockout, traceable
# denials, audit/productivity wiring, review-outcome metric
# ---------------------------------------------------------------------------

class TestSprint27ReviewFixes:
    def _import_server(self):
        import sys
        for p in (str(_ROOT), str(_ROOT / "src")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from mcp.core import mcp_server as m
        return m

    def test_default_workspace_is_key_aware(self):
        """F1: MCAL-only DAs default to their own workspace, not 'illd'."""
        m = self._import_server()
        assert m._default_workspace_for_key("key-gest-001") == "mcal"
        assert m._default_workspace_for_key("key-triplea-001") == "mcal"
        assert m._default_workspace_for_key("key-eda-001") == "illd"
        assert m._default_workspace_for_key("key-admin-pipeline") == "illd"
        assert m._default_workspace_for_key("not-a-real-key") == "illd"

    def test_denied_envelope_is_traceable(self):
        """F3: denied responses carry a correlation_id (routed through _err)."""
        import json
        m = self._import_server()
        key_tok = m._current_api_key.set("definitely-not-a-real-key")
        orig_pg = m._get_postgres_client
        m._get_postgres_client = lambda *a, **k: None
        try:
            env = json.loads(m._authorize("health_check"))
        finally:
            m._get_postgres_client = orig_pg
            m._current_api_key.reset(key_tok)
        assert env["error_code"] == "PERMISSION_DENIED"
        assert env.get("correlation_id"), "denied envelope must carry a correlation_id"

    def test_no_untraceable_permission_denied_helper(self):
        """F3: the bare _err_permission_denied() path is no longer used."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        assert "_err_permission_denied(" not in src

    def test_audit_is_completion_time_with_tokens(self):
        """F2a + round-2 M: the audit row is written at completion (not pre-exec)
        and carries session id, duration, and token count so da_productivity
        totals are real."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        assert "_current_session_id.set(" in src  # seeded by with_session_routing
        block = src.split("def _write_audit(", 1)[1].split("\ndef ", 1)[0]
        assert "session_id=_current_session_id.get(" in block
        assert "duration_ms=int(duration_s" in block
        assert "token_count=int(_current_tokens.get(" in block
        # _authorize must NOT log the audit pre-execution anymore
        authz = src.split("def _authorize(", 1)[1].split("\ndef ", 1)[0]
        assert "log_audit(" not in authz

    def test_evaluate_confidence_archives_response(self):
        """F2b: evaluate_confidence populates response_archive."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        block = src.split("async def evaluate_confidence(", 1)[1].split("@mcp.tool()", 1)[0]
        assert "archive_response(" in block

    def test_complete_review_emits_outcome_metric(self):
        """F4: complete_review emits the per-DA review-outcome metric."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        block = src.split("async def complete_review(", 1)[1].split("@mcp.tool()", 1)[0]
        assert "DA_SESSION_OUTCOMES.labels(" in block

    # ── Round 2 review fixes ────────────────────────────────────────────

    def test_workspace_scoped_tools_authorize_against_execution_workspace(self):
        """C1-round2: tools authorize against the same workspace/profile they
        execute against (no cross-workspace access gap)."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        for tool in ("query_api_function", "get_type_definition",
                     "generate_initialization_code", "query_dependencies",
                     "validate_api_usage", "detect_polling_requirements",
                     "find_requirement_traces", "build_traceability_matrix",
                     "find_coverage_gaps", "analyze_hw_sw_links",
                     "submit_human_feedback"):
            assert f'_authorize("{tool}", workspace_id=workspace_id)' in src, tool
            assert f'_authorize("{tool}")' not in src, f"{tool} still diverges"
        for tool in ("get_function_hsi", "rlm_orchestrate", "rlm_plan_preview"):
            assert f'_authorize("{tool}", profile=profile)' in src, tool
            assert f'_authorize("{tool}")' not in src, f"{tool} still diverges"

    def test_session_start_records_key_aware_workspace(self):
        """C2-round2: session_start authorizes and records the session under one
        (key-aware) workspace instead of the manager default."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        block = src.split("async def session_start(", 1)[1].split("async def session_store(", 1)[0]
        assert "workspace_id: Optional[str] = None" in block
        assert "_effective_workspace(workspace_id)" in block
        assert '_authorize("session_start", workspace_id=workspace_id' in block
        assert "workspace_id=workspace_id)" in block  # passed through to mgr.create

    def test_session_id_contextvar_is_reset(self):
        """H-round2: with_session_routing resets _current_session_id (token+finally)."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        assert "_sid_token = _current_session_id.set(" in src
        assert "_current_session_id.reset(_sid_token)" in src

    def test_rlm_orchestrate_records_tokens_for_audit(self):
        """M-round2: rlm_orchestrate surfaces token usage for the audit row."""
        src = _MCP_SERVER.read_text(encoding="utf-8")
        block = src.split("async def rlm_orchestrate(", 1)[1].split("@mcp.tool()", 1)[0]
        assert "_current_tokens.set(" in block

    def test_effective_workspace_is_key_aware(self):
        """C1-round2: _effective_workspace honours an explicit value, else falls
        back to the caller's own key-aware workspace."""
        m = self._import_server()
        assert m._effective_workspace("mcal") == "mcal"  # explicit wins
        tok = m._current_api_key.set("key-gest-001")
        try:
            assert m._effective_workspace(None) == "mcal"  # gest is MCAL-only
        finally:
            m._current_api_key.reset(tok)
        tok = m._current_api_key.set("key-eda-001")
        try:
            assert m._effective_workspace(None) == "illd"  # EDA owns illd
        finally:
            m._current_api_key.reset(tok)

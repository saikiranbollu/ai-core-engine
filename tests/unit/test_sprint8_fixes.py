"""
Sprint 8 — Unit Tests for Review Fixes
========================================
Validates all fixes from Review.md / Review_solutions.md plus additional
observations identified during code review.

Phases covered:
  0 — _ok() envelope contract
  1 — Auth model (tool_tiers, auth_middleware)
  2 — Cache on search path
  3 — Cache correctness (LRU TTL, SemanticCache without hash fallback)
  4 — PostgreSQL wiring
  5 — Structured parser dispatch
  6 — Ingestion write semantics
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# ═════════════════════════════════════════════════════════════════════════
#  Phase 0 — Envelope contract
# ═════════════════════════════════════════════════════════════════════════

class TestEnvelope:
    """_ok() must return {"error": False, "data": ...}."""

    @staticmethod
    def _ok(data):
        """Re-implement _ok logic to test the contract without importing mcp_server
        (which needs mcp.server.fastmcp which may not be installed)."""
        return json.dumps({"error": False, "data": data}, indent=2, default=str)

    @staticmethod
    def _err(code, message):
        return json.dumps({"error": True, "error_code": code, "message": message})

    def test_ok_envelope_shape(self):
        raw = self._ok({"count": 3})
        obj = json.loads(raw)
        assert obj["error"] is False
        assert "data" in obj
        assert obj["data"] == {"count": 3}

    def test_ok_envelope_no_legacy_fields(self):
        obj = json.loads(self._ok("hello"))
        assert "ok" not in obj
        assert "result" not in obj

    def test_err_envelope_shape(self):
        obj = json.loads(self._err("TEST_ERR", "something broke"))
        assert obj["error"] is True
        assert obj["error_code"] == "TEST_ERR"
        assert obj["message"] == "something broke"

    def test_ok_source_matches_contract(self):
        """Verify the actual _ok() in mcp_server.py matches the expected pattern."""
        src = (Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py").read_text(encoding="utf-8")
        assert '"error": False, "data": data' in src
        assert '"ok": True' not in src

# ═════════════════════════════════════════════════════════════════════════
#  Phase 1 — Auth model
# ═════════════════════════════════════════════════════════════════════════

class TestToolTiers:
    """tool_tiers.py: plain strings, hierarchy, helpers."""

    def test_tier_constants_are_strings(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        for tool, tier in TOOL_TIERS.items():
            assert isinstance(tool, str)
            assert tier in ("public", "developer", "admin")

    def test_get_tool_tier(self):
        from mcp.core.tool_tiers import get_tool_tier
        assert get_tool_tier("search_database") == "public"
        assert get_tool_tier("execute_cypher") == "developer"
        assert get_tool_tier("cache_clear") == "admin"
        assert get_tool_tier("unknown_tool_xyz") is None  # unknown tools return None

    def test_role_may_invoke(self):
        from mcp.core.tool_tiers import role_may_invoke
        # public can invoke public tools
        assert role_may_invoke("public", "search_database") is True
        # public cannot invoke developer or admin tools
        assert role_may_invoke("public", "execute_cypher") is False
        assert role_may_invoke("public", "cache_clear") is False
        # developer can invoke public + developer
        assert role_may_invoke("developer", "search_database") is True
        assert role_may_invoke("developer", "execute_cypher") is True
        assert role_may_invoke("developer", "cache_clear") is False
        # admin can invoke anything
        assert role_may_invoke("admin", "cache_clear") is True
        assert role_may_invoke("admin", "execute_cypher") is True
        assert role_may_invoke("admin", "search_database") is True

    def test_all_56_tools_covered(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        # 55 tools across 14 categories (Plan 2 Phase 2 removed 4 ingestion tools)
        assert len(TOOL_TIERS) == 55

    def test_sandbox_and_rlm_tools_present(self):
        from mcp.core.tool_tiers import TOOL_TIERS
        for t in ("sandbox_upload", "sandbox_status",
                   "sandbox_clear", "rlm_orchestrate", "rlm_plan_preview"):
            assert t in TOOL_TIERS


class TestAuthMiddleware:
    """auth_middleware.py: Cerbos integration, workspace-scoped roles."""

    def test_check_authorization_signature(self):
        """Signature must be (api_key, tool_name, workspace_id)."""
        from mcp.core.auth_middleware import check_authorization
        import inspect
        sig = inspect.signature(check_authorization)
        params = list(sig.parameters.keys())
        assert params[:3] == ["api_key", "tool_name", "workspace_id"]

    def test_check_authorization_returns_tuple(self):
        from mcp.core.auth_middleware import check_authorization
        result = check_authorization("nonexistent-key", "search_database", "illd")
        assert isinstance(result, tuple)
        assert len(result) == 2
        allowed, msg = result
        assert isinstance(allowed, bool)
        assert isinstance(msg, str)

    def test_extract_workspace_id(self):
        from mcp.core.auth_middleware import extract_workspace_id
        assert extract_workspace_id(workspace_id="mcal") == "mcal"
        assert extract_workspace_id(profile="mcal") == "mcal"
        assert extract_workspace_id() == "illd"  # default


# ═════════════════════════════════════════════════════════════════════════
#  Phase 3 — Cache correctness
# ═════════════════════════════════════════════════════════════════════════

class TestLRUCacheTTL:
    """LRUCache must evict entries whose TTL has expired."""

    def test_ttl_enforced(self):
        from src.Configuration.cache_service import LRUCache
        cache = LRUCache(max_size=100)
        cache.put("k1", "v1", ttl=1)
        assert cache.get("k1") == "v1"  # fresh
        time.sleep(1.1)
        assert cache.get("k1") is None  # expired

    def test_ttl_not_expired(self):
        from src.Configuration.cache_service import LRUCache
        cache = LRUCache(max_size=100)
        cache.put("k1", "v1", ttl=3600)
        assert cache.get("k1") == "v1"


class TestSemanticCacheNoHashFallback:
    """SemanticCache must NOT fall back to SHA-256 hash embeddings."""

    def test_no_hash_embed_method(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache()
        assert not hasattr(sc, "_hash_embed")

    def test_embed_returns_none_without_st(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache()
        sc._use_st = False  # force no sentence-transformers
        result = sc._embed("test query")
        assert result is None

    def test_get_returns_none_without_st(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache()
        sc._use_st = False
        # put should be no-op, get should return None
        sc.put("test query", {"data": 1})
        assert sc.get("test query") is None

    def test_stats_reports_st_active(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache()
        stats = sc.stats()
        assert "sentence_transformers_active" in stats


class TestCacheService:
    """CacheService two-tier get/put."""

    def test_lru_hit(self):
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        svc.put("q1", {"count": 5})
        result = svc.get("q1")
        assert result["hit"] is True
        assert result["tier"] == "lru"
        assert result["result"] == {"count": 5}

    def test_miss(self):
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        result = svc.get("never_cached")
        assert result["hit"] is False


class TestCacheEnvConfig:
    """CacheService respects env-var configuration (MEG_SW-74, MEG_SW-75)."""

    def test_lru_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("LRU_CACHE_SIZE", "5000")
        monkeypatch.setenv("LRU_CACHE_TTL_HOURS", "12")
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        assert svc.lru._max == 5000
        assert svc.lru._default_ttl == 12 * 3600

    def test_semantic_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.90")
        monkeypatch.setenv("SEMANTIC_CACHE_TTL_DAYS", "3")
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        assert svc.semantic._threshold == 0.90
        assert svc.semantic._ttl == 3 * 86400

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("LRU_CACHE_SIZE", "not_a_number")
        monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "5.0")
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        assert svc.lru._max == 10000  # default
        assert svc.semantic._threshold == 0.95  # default

    def test_constructor_args_override_env(self, monkeypatch):
        monkeypatch.setenv("LRU_CACHE_SIZE", "9999")
        from src.Configuration.cache_service import CacheService
        svc = CacheService(lru_max_size=200)
        assert svc.lru._max == 200

    def test_default_values_match_jira(self):
        """Without env vars, defaults must match MEG_SW-74/75 acceptance criteria."""
        from src.Configuration.cache_service import CacheService
        svc = CacheService()
        assert svc.lru._max == 10000
        assert svc.lru._default_ttl == 24 * 3600
        assert svc.semantic._threshold == 0.95
        assert svc.semantic._ttl == 7 * 86400


class TestSemanticCacheTTL:
    """Semantic cache entries expire after TTL (MEG_SW-75)."""

    def test_entry_expires(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache(ttl_seconds=1, similarity_threshold=0.5)
        sc._use_st = False  # no model needed for expiry test
        # Manually insert an entry with old timestamp
        sc._entries.append({
            "query": "test", "embedding": [1.0],
            "value": "old", "metadata": None,
            "ts": time.time() - 10,
        })
        assert len(sc._entries) == 1
        sc._evict_expired()
        assert len(sc._entries) == 0

    def test_stats_reports_ttl(self):
        from src.Configuration.cache_service import SemanticCache
        sc = SemanticCache(ttl_seconds=999)
        stats = sc.stats()
        assert stats["ttl_seconds"] == 999


class TestLRUModuleInvalidation:
    """LRU invalidation finds module anywhere in composite keys."""

    def test_invalidate_search_database_keys(self):
        from src.Configuration.cache_service import LRUCache
        c = LRUCache(max_size=100)
        # Keys match search_database format: {workspace}:{module}:{alpha}:{query}
        c.put("illd:adc:0.6:init sequence", "result1")
        c.put("mcal:adc:0.6:adc setup", "result2")
        c.put("illd:can:0.6:transmit", "result3")
        n = c.invalidate_by_module("adc")
        assert n == 2
        assert c.get("illd:can:0.6:transmit") == "result3"

    def test_prefix_invalidation_still_works(self):
        from src.Configuration.cache_service import LRUCache
        c = LRUCache(max_size=100)
        c.put("adc:init", "v1")
        n = c.invalidate_by_prefix("adc")
        assert n == 1


class TestIngestionCompletionCallback:
    """IngestionService fires on_module_ingested callback on success (MEG_SW-112)."""

    def test_callback_fires_on_ingest_file(self, tmp_path, monkeypatch):
        from src.IngestionPipeline.ingestion_service import IngestionService
        monkeypatch.setenv("INGEST_ALLOWED_ROOTS", str(tmp_path))
        fired = []
        svc = IngestionService(on_module_ingested=lambda m, w: fired.append((m, w)))
        f = tmp_path / "test.json"
        f.write_text('{"key": "val"}')
        svc.ingest_file(str(f), "TestMod", workspace_id="illd")
        assert ("TestMod", "illd") in fired

    def test_callback_failure_does_not_break_ingestion(self, tmp_path, monkeypatch):
        from src.IngestionPipeline.ingestion_service import IngestionService
        monkeypatch.setenv("INGEST_ALLOWED_ROOTS", str(tmp_path))
        def bad_callback(m, w):
            raise RuntimeError("callback boom")
        svc = IngestionService(on_module_ingested=bad_callback)
        f = tmp_path / "test.json"
        f.write_text('{"key": "val"}')
        result = svc.ingest_file(str(f), "TestMod")
        assert result["status"] == "completed"

    def test_no_callback_is_safe(self, tmp_path, monkeypatch):
        from src.IngestionPipeline.ingestion_service import IngestionService
        monkeypatch.setenv("INGEST_ALLOWED_ROOTS", str(tmp_path))
        svc = IngestionService()
        f = tmp_path / "test.json"
        f.write_text('{"key": "val"}')
        result = svc.ingest_file(str(f), "TestMod")
        assert result["status"] == "completed"

    def test_ingest_rejects_path_outside_allowed_roots(self, tmp_path, monkeypatch):
        """F-CA-I01: with containment on by default, a supported file outside the
        allowed roots is rejected (not silently ingested)."""
        from src.IngestionPipeline.ingestion_service import IngestionService
        # No INGEST_ALLOWED_ROOTS -> defaults to /data,/repos, which tmp_path is
        # not under, so ingestion must be refused before any parsing.
        monkeypatch.delenv("INGEST_ALLOWED_ROOTS", raising=False)
        svc = IngestionService()
        f = tmp_path / "outside.json"
        f.write_text('{"key": "val"}')
        with pytest.raises(ValueError, match="Rejected file path"):
            svc.ingest_file(str(f), "TestMod")


# ═════════════════════════════════════════════════════════════════════════
#  MEG_SW-108 — Cache config runtime refresh
# ═════════════════════════════════════════════════════════════════════════

class TestCacheRuntimeRefresh:
    """CacheService.refresh_config() re-reads env vars and updates in-place."""

    def _make(self, **kw):
        from src.Configuration.cache_service import CacheService
        return CacheService(**kw)

    # ── LRU refresh ──

    def test_lru_refresh_updates_ttl(self):
        cs = self._make(lru_ttl_seconds=100)
        assert cs.lru._default_ttl == 100
        result = cs.lru.refresh_config(max_size=cs.lru._max, default_ttl=200)
        assert cs.lru._default_ttl == 200
        assert result["lru_default_ttl"] == {"old": 100, "new": 200}

    def test_lru_refresh_shrinks_evicts(self):
        cs = self._make(lru_max_size=5, lru_ttl_seconds=3600)
        for i in range(5):
            cs.lru.put(f"key{i}", f"val{i}")
        assert cs.lru.stats()["size"] == 5
        result = cs.lru.refresh_config(max_size=2, default_ttl=3600)
        assert cs.lru.stats()["size"] == 2
        assert result["evicted"] == 3
        # Oldest entries evicted (FIFO), newest preserved
        assert cs.lru.get("key3") == "val3"
        assert cs.lru.get("key4") == "val4"
        assert cs.lru.get("key0") is None

    def test_lru_refresh_grows_no_eviction(self):
        cs = self._make(lru_max_size=2, lru_ttl_seconds=3600)
        cs.lru.put("a", 1)
        cs.lru.put("b", 2)
        result = cs.lru.refresh_config(max_size=100, default_ttl=3600)
        assert result["evicted"] == 0
        assert cs.lru.stats()["size"] == 2
        assert cs.lru.get("a") == 1

    # ── Semantic refresh ──

    def test_semantic_refresh_updates_threshold(self):
        cs = self._make(semantic_threshold=0.95)
        assert cs.semantic._threshold == 0.95
        result = cs.semantic.refresh_config(max_size=500, similarity_threshold=0.80,
                                             ttl_seconds=cs.semantic._ttl)
        assert cs.semantic._threshold == 0.80
        assert result["semantic_threshold"] == {"old": 0.95, "new": 0.80}
        # Verify reflected in stats
        assert cs.semantic.stats()["similarity_threshold"] == 0.80

    def test_semantic_refresh_updates_ttl(self):
        cs = self._make(semantic_ttl_seconds=1000)
        result = cs.semantic.refresh_config(max_size=500, similarity_threshold=0.95,
                                             ttl_seconds=5000)
        assert cs.semantic._ttl == 5000
        assert result["semantic_ttl_seconds"] == {"old": 1000, "new": 5000}

    def test_semantic_refresh_shrinks_evicts(self):
        import time as _time
        cs = self._make(semantic_max_size=5, semantic_ttl_seconds=86400)
        # Manually insert entries with staggered timestamps
        for i in range(5):
            cs.semantic._entries.append({
                "query": f"q{i}", "embedding": [float(i)],
                "value": f"v{i}", "metadata": None,
                "ts": _time.time() + i,  # each slightly newer
            })
        result = cs.semantic.refresh_config(max_size=2, similarity_threshold=0.95,
                                             ttl_seconds=86400)
        assert len(cs.semantic._entries) == 2
        assert result["evicted"] == 3
        # Newest entries preserved
        assert cs.semantic._entries[0]["query"] == "q3"
        assert cs.semantic._entries[1]["query"] == "q4"

    # ── CacheService.refresh_config() reads env vars ──

    def test_cache_service_refresh_reads_env(self, monkeypatch):
        cs = self._make(lru_max_size=100, lru_ttl_seconds=3600,
                        semantic_max_size=50, semantic_threshold=0.95,
                        semantic_ttl_seconds=86400)
        monkeypatch.setenv("LRU_CACHE_SIZE", "200")
        monkeypatch.setenv("LRU_CACHE_TTL_HOURS", "48")
        monkeypatch.setenv("SEMANTIC_CACHE_MAX_SIZE", "100")
        monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.85")
        monkeypatch.setenv("SEMANTIC_CACHE_TTL_DAYS", "14")
        changes = cs.refresh_config()
        assert changes["lru_max_size"]["new"] == 200
        assert changes["lru_default_ttl"]["new"] == 48 * 3600
        assert changes["semantic_max_size"]["new"] == 100
        assert changes["semantic_threshold"]["new"] == 0.85
        assert changes["semantic_ttl_seconds"]["new"] == 14 * 86400

    def test_cache_service_refresh_logs_changes(self, monkeypatch, caplog):
        import logging
        cs = self._make(lru_ttl_seconds=3600)
        monkeypatch.setenv("LRU_CACHE_TTL_HOURS", "48")
        with caplog.at_level(logging.INFO, logger="src.Configuration.cache_service"):
            cs.refresh_config()
        assert "lru_default_ttl" in caplog.text

    def test_refresh_returns_old_new_values(self):
        cs = self._make(lru_max_size=100, lru_ttl_seconds=3600)
        changes = cs.lru.refresh_config(max_size=200, default_ttl=7200)
        assert changes["lru_max_size"] == {"old": 100, "new": 200}
        assert changes["lru_default_ttl"] == {"old": 3600, "new": 7200}
        assert changes["evicted"] == 0

    def test_refresh_no_change_returns_unchanged(self):
        cs = self._make(lru_max_size=100, lru_ttl_seconds=3600,
                        semantic_max_size=50, semantic_threshold=0.95,
                        semantic_ttl_seconds=86400)
        changes = cs.lru.refresh_config(max_size=100, default_ttl=3600)
        assert changes["lru_max_size"]["old"] == changes["lru_max_size"]["new"]
        assert changes["lru_default_ttl"]["old"] == changes["lru_default_ttl"]["new"]
        assert changes["evicted"] == 0


# ═════════════════════════════════════════════════════════════════════════
#  Phase 5 — Structured parser dispatch
# ═════════════════════════════════════════════════════════════════════════

class TestParserDispatch:
    """_parse_file delegates to the correct parser module."""

    def _svc(self):
        from src.IngestionPipeline.ingestion_service import IngestionService
        return IngestionService()

    def test_c_file(self):
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as f:
            f.write("int foo(int x) { return x; }\n")
            f.flush()
            result = svc._parse_file(Path(f.name), ".c")
        assert result is not None
        assert result.get("type") in ("c_header", "c_source")
        # Should contain function info
        assert "functions" in result or "function_count" in result or "file" in result

    def test_json_file(self):
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"key": "val"}, f)
            f.flush()
            result = svc._parse_file(Path(f.name), ".json")
        assert result["type"] == "json"
        assert result["data"] == {"key": "val"}

    def test_txt_file(self):
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("Hello World")
            f.flush()
            result = svc._parse_file(Path(f.name), ".txt")
        assert result["type"] == "text"
        assert "Hello World" in result["content"]

    def test_pdf_dispatch(self):
        """PDF should attempt pdf_parser, fall back to generic on ImportError."""
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".pdf", mode="wb", delete=False) as f:
            f.write(b"%PDF-1.4 dummy")
            f.flush()
            # Force ImportError by patching the import mechanism
            import builtins
            _real_import = builtins.__import__
            def _mock_import(name, *args, **kwargs):
                if "pdf_parser" in name:
                    raise ImportError("mocked")
                return _real_import(name, *args, **kwargs)
            with patch.object(builtins, "__import__", side_effect=_mock_import):
                result = svc._parse_file(Path(f.name), ".pdf")
        assert result is not None
        assert result["type"] in ("pdf", "generic")

    def test_xlsx_dispatch(self):
        """XLSX should attempt xlsx_parser."""
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(b"dummy")
            f.flush()
            with patch.dict("sys.modules", {"src.IngestionPipeline.parsers.xlsx_parser": None}):
                result = svc._parse_file(Path(f.name), ".xlsx")
        assert result is not None
        assert result.get("type") in ("xlsx", "generic")

    def test_puml_dispatch(self):
        svc = self._svc()
        with tempfile.NamedTemporaryFile(suffix=".puml", mode="w", delete=False) as f:
            f.write("@startuml\nAlice -> Bob : hello\n@enduml\n")
            f.flush()
            result = svc._parse_file(Path(f.name), ".puml")
        assert result is not None


# ═════════════════════════════════════════════════════════════════════════
#  Phase 6 — Ingestion write semantics
# ═════════════════════════════════════════════════════════════════════════

class TestWriteToKG:
    """_write_to_kg uses the correct Cypher operation for overwrite flag."""

    def _svc_with_mock_neo(self):
        from src.IngestionPipeline.ingestion_service import IngestionService
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        return IngestionService(neo4j_driver=mock_driver), mock_session

    def test_merge_mode_without_overwrite(self):
        svc, session = self._svc_with_mock_neo()
        parsed = {"type": "c_header", "functions": [
            {"name": "foo", "return_type": "void", "parameters": "int x"},
        ], "file": "test.h"}
        nodes, rels = svc._write_to_kg(parsed, "CAN", "illd", overwrite=False)
        assert nodes == 1
        # Verify MERGE was used
        call_args = session.run.call_args[0][0]
        assert "MERGE" in call_args

    def test_create_mode_with_overwrite(self):
        svc, session = self._svc_with_mock_neo()
        parsed = {"type": "c_header", "functions": [
            {"name": "foo", "return_type": "void", "parameters": "int x"},
        ], "file": "test.h"}
        nodes, rels = svc._write_to_kg(parsed, "CAN", "illd", overwrite=True)
        assert nodes == 1
        call_args = session.run.call_args[0][0]
        assert "CREATE" in call_args

    def test_json_type_writes_datanode(self):
        svc, session = self._svc_with_mock_neo()
        parsed = {"type": "json", "data": {"key": "val"}, "file": "test.json"}
        nodes, _ = svc._write_to_kg(parsed, "CAN", "illd", overwrite=False)
        assert nodes == 1
        call_args = session.run.call_args[0][0]
        assert "DataNode" in call_args

    def test_pdf_type_writes_document(self):
        svc, session = self._svc_with_mock_neo()
        parsed = {"type": "pdf", "pages": ["page 1 text"], "file": "test.pdf"}
        nodes, _ = svc._write_to_kg(parsed, "CAN", "illd", overwrite=False)
        assert nodes == 1
        call_args = session.run.call_args[0][0]
        assert "Document" in call_args

    def test_xlsx_type_writes_sheets(self):
        svc, session = self._svc_with_mock_neo()
        parsed = {"type": "xlsx", "sheets": {"Sheet1": [{"A": 1}]}, "file": "test.xlsx"}
        nodes, _ = svc._write_to_kg(parsed, "CAN", "illd", overwrite=False)
        assert nodes == 1
        call_args = session.run.call_args[0][0]
        assert "Sheet" in call_args


# ═════════════════════════════════════════════════════════════════════════
#  Phase 4 — PostgreSQL wiring
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresClient:
    """PostgresClient degrades gracefully without a DSN."""

    def test_graceful_degradation_no_dsn(self):
        from src.Observability.postgres_schema import PostgresClient
        client = PostgresClient(dsn="")
        assert client.available is False
        # All writes should be no-ops
        client.log_audit("test_tool")
        client.save_feedback("fb1", "resp1", "APPROVE")
        client.close()

    def test_schema_sql_valid(self):
        from src.Observability.postgres_schema import SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS audit_logs" in SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS feedback_records" in SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS ingestion_jobs" in SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS sessions_meta" in SCHEMA_SQL


class TestFeedbackSinkPostgresWiring:
    """FeedbackSink writes through to PostgresClient when provided."""

    def test_pg_called_on_submit(self):
        from src.ReviewGate.confidence import FeedbackSink
        mock_pg = MagicMock()
        sink = FeedbackSink(postgres_client=mock_pg)
        sink.submit_feedback("resp1", "APPROVE", reviewer_id="user1")
        mock_pg.save_feedback.assert_called_once()

    def test_pg_not_called_without_client(self):
        from src.ReviewGate.confidence import FeedbackSink
        sink = FeedbackSink()
        # Should not fail
        result = sink.submit_feedback("resp1", "APPROVE")
        assert "feedback_id" in result
        assert result["recorded"] is True


class TestSessionManagerPostgresWiring:
    """SessionManager writes through to PostgresClient when provided."""

    def test_pg_called_on_create(self):
        from src.MemoryLayer.memory.session_manager import SessionManager, DictBackend
        mock_pg = MagicMock()
        mgr = SessionManager(backend=DictBackend(), postgres_client=mock_pg)
        mgr.create("test-session-1", assistant_name="DA")
        mock_pg.save_session_meta.assert_called_once()


class TestIngestionJobTrackerPostgresWiring:
    """IngestionJobTracker writes through to PostgresClient when provided."""

    def test_pg_called_on_create_job(self):
        from src.IngestionPipeline.ingestion_service import IngestionJobTracker
        mock_pg = MagicMock()
        tracker = IngestionJobTracker(postgres_client=mock_pg)
        job_id = tracker.create_job("ingest_file", {"file": "test.c"})
        mock_pg.save_ingestion_job.assert_called_once()
        assert job_id.startswith("ingest_")

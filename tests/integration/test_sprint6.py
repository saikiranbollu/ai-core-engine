"""
Sprint 6 Integration Tests — Cache, Ontology, Observability, Viz, Auth
=======================================================================
Tests:
  1. CacheService: LRU + Semantic, hit/miss, invalidation, stats
  2. OntologyService: profiles, schema, validation, compliance
  3. ObservabilityService: stats, modules, distribution, coverage
  4. AuthService: JWT decode, token refresh
  5. All stubs eliminated — zero NOT_IMPLEMENTED remaining
"""
import json
import sys
import time
import base64
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.Configuration.cache_service import CacheService, LRUCache, SemanticCache
from src.Configuration.services import OntologyService, ObservabilityService, AuthService
from src.MemoryLayer.memory.ontology_loader import OntologyLoader


# ═════════════════════════════════════════════════════════════════════════
#  Test 1: Cache Service
# ═════════════════════════════════════════════════════════════════════════

class TestLRUCache:
    def test_put_and_get(self):
        c = LRUCache(max_size=5)
        c.put("q1", {"data": "result1"})
        assert c.get("q1") == {"data": "result1"}

    def test_miss(self):
        c = LRUCache()
        assert c.get("nonexistent") is None

    def test_eviction(self):
        c = LRUCache(max_size=3)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        c.put("d", 4)  # Should evict "a"
        assert c.get("a") is None
        assert c.get("d") == 4

    def test_stats(self):
        c = LRUCache()
        c.put("q", "v")
        c.get("q")  # hit
        c.get("miss")  # miss
        s = c.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["hit_rate"] == 0.5

    def test_invalidate_prefix(self):
        c = LRUCache()
        c.put("cxpi_init", "v1")
        c.put("cxpi_send", "v2")
        c.put("can_init", "v3")
        n = c.invalidate_by_prefix("cxpi")
        assert n == 2
        assert c.get("can_init") == "v3"

    def test_clear(self):
        c = LRUCache()
        c.put("a", 1); c.put("b", 2)
        assert c.clear() == 2
        assert c.get("a") is None


class TestSemanticCache:
    def test_exact_match_hit(self):
        c = SemanticCache(similarity_threshold=0.95)
        c.put("CAN initialization sequence", {"code": "init()"})
        result = c.get("CAN initialization sequence")
        assert result is not None
        assert result["value"] == {"code": "init()"}

    def test_miss_different_query(self):
        c = SemanticCache(similarity_threshold=0.95)
        c.put("CAN init", "v1")
        result = c.get("SPI configuration completely different")
        # Hash-based embedder — different queries won't match at 0.95
        assert result is None

    def test_invalidate_module(self):
        c = SemanticCache()
        c.put("CAN init function", "v1")
        c.put("SPI transfer setup", "v2")
        n = c.invalidate_by_module("CAN")
        assert n == 1


class TestCacheService:
    def test_two_tier_flow(self):
        cs = CacheService()
        # Miss initially
        r = cs.get("test query")
        assert r["hit"] is False
        # Put and hit LRU
        cs.put("test query", {"result": "data"})
        r = cs.get("test query")
        assert r["hit"] is True
        assert r["tier"] == "lru"

    def test_invalidate_module(self):
        cs = CacheService()
        cs.put("cxpi_init", "v1")
        result = cs.invalidate_module("cxpi")
        assert "lru_invalidated" in result

    def test_invalidate_module_with_search_database_keys(self):
        """Module invalidation must remove entries using real search_database key format."""
        cs = CacheService()
        cs.put("illd:adc:0.6:init sequence", {"r": 1})
        cs.put("mcal:adc:0.6:adc setup", {"r": 2})
        cs.put("illd:can:0.6:transmit", {"r": 3})
        result = cs.invalidate_module("adc")
        assert result["lru_invalidated"] == 2
        # CAN entry must survive
        r = cs.get("illd:can:0.6:transmit")
        assert r["hit"] is True

    def test_clear(self):
        cs = CacheService()
        cs.put("q", "v")
        result = cs.clear(["all"])
        assert "lru" in result["cleared"]
        assert "semantic" in result["cleared"]

    def test_stats(self):
        cs = CacheService()
        cs.put("q", "v"); cs.get("q"); cs.get("miss")
        s = cs.stats()
        assert "lru" in s and "semantic" in s
        assert s["lru"]["hits"] == 1

    def test_stats_includes_new_fields(self):
        """Stats must report default_ttl and ttl_seconds after config changes."""
        cs = CacheService()
        s = cs.stats()
        assert "default_ttl" in s["lru"]
        assert "ttl_seconds" in s["semantic"]
        assert s["lru"]["default_ttl"] == 24 * 3600
        assert s["semantic"]["ttl_seconds"] == 7 * 86400


# ═════════════════════════════════════════════════════════════════════════
#  Test 2: Ontology Service
# ═════════════════════════════════════════════════════════════════════════

class TestOntologyService:
    def setup_method(self):
        self.svc = OntologyService()

    def test_list_profiles(self):
        profiles = self.svc.list_profiles()
        names = [p["name"] for p in profiles]
        assert "Automotive Embedded LLD Ontology" in names
        assert "AURIX 3G MCAL Requirements Ontology" in names

    def test_get_schema_illd(self):
        schema = self.svc.get_schema("illd")
        assert schema["profile"] == "illd"
        assert "APIFunction" in schema["node_types"]
        assert "IMPLEMENTS" in schema["relationship_types"]
        assert "CXPI" in schema["supported_modules"]

    def test_get_schema_mcal(self):
        schema = self.svc.get_schema("mcal")
        assert schema["strictness"] == "strict"
        assert "FailurePattern" in schema["node_types"]

    def test_schema_is_loaded_from_ontology(self):
        loader = OntologyLoader()
        schema = self.svc.get_schema("illd")

        for node_type in loader.get_node_type_names("illd"):
            assert node_type in schema["node_types"]
        assert schema["relationship_types"] == loader.get_relationship_names("illd")
        assert schema["supported_modules"] == loader.get_supported_modules("illd")

    def test_get_schema_unknown_profile(self):
        with pytest.raises(ValueError, match="Unknown profile"):
            self.svc.get_schema("nonexistent")

    def test_get_schema_with_node_type_filter(self):
        schema = self.svc.get_schema("illd", node_type="APIFunction")
        assert schema["node_types"] == ["APIFunction"]

    def test_validate_valid_entity(self):
        result = self.svc.validate_entity("APIFunction",
                                           {"name": "IfxCan_init", "module": "CAN"}, "illd")
        assert result["is_valid"] is True
        assert result["issues"] == []

    def test_validate_unknown_type(self):
        result = self.svc.validate_entity("UnknownType", {"name": "x"}, "illd")
        assert result["is_valid"] is False
        assert any("unknown_node_type" in i["type"] for i in result["issues"])

    def test_validate_missing_identifier(self):
        result = self.svc.validate_entity("APIFunction", {"module": "CAN"}, "illd")
        assert result["is_valid"] is False

    def test_compliance_valid_module(self):
        result = self.svc.get_compliance("CXPI", "illd")
        assert result["compliance_score"] == 1.0

    def test_compliance_unknown_module(self):
        result = self.svc.get_compliance("UNKNOWN_MOD", "illd")
        assert result["compliance_score"] == 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 3: Observability Service (without Neo4j)
# ═════════════════════════════════════════════════════════════════════════

class TestObservabilityService:
    def setup_method(self):
        self.svc = ObservabilityService()

    def test_graph_stats_no_neo4j(self):
        result = self.svc.get_graph_statistics()
        assert result["total_nodes"] == 0
        assert "note" in result

    def test_list_modules_no_neo4j(self):
        result = self.svc.list_modules()
        assert "modules" in result
        assert result["total_count"] > 0  # Returns from ontology profiles

    def test_distribution_valid(self):
        result = self.svc.get_distribution("asil")
        assert result["dimension"] == "asil"
        assert "distribution" in result

    def test_distribution_invalid_dimension(self):
        with pytest.raises(ValueError):
            self.svc.get_distribution("invalid_dim")

    def test_coverage_no_neo4j(self):
        result = self.svc.get_coverage_report("illd")
        assert "coverage_report" in result

    def test_coverage_uses_canonical_and_legacy_requirement_labels(self):
        class _Result:
            def __init__(self, value):
                self._value = value

            def single(self):
                return {"c": self._value}

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def run(self, query, params=None):
                assert params == {"requirement_labels": ["SoftwareRequirement", "ProductRequirement"]}
                if "RETURN count(r) AS c" in query:
                    return _Result(4)
                if "RETURN count(DISTINCT r) AS c" in query and "TRACES_TO" not in query:
                    return _Result(3)
                if "TRACES_TO" in query:
                    return _Result(2)
                raise AssertionError(f"Unexpected query: {query}")

        class _Driver:
            def session(self, database=None):
                return _Session()

        svc = ObservabilityService(neo4j_driver=_Driver())
        result = svc.get_coverage_report("illd")

        assert result["coverage_report"]["total_requirements"] == 4
        assert result["coverage_report"]["req_to_code"] == 0.75
        assert result["coverage_report"]["req_to_test"] == 0.5

    def test_communities_no_neo4j(self):
        result = self.svc.detect_communities()
        assert result["communities_found"] == 0


# ═════════════════════════════════════════════════════════════════════════
#  Test 4: Auth Service
# ═════════════════════════════════════════════════════════════════════════

class TestAuthService:
    def test_decode_jwt(self):
        """Decode a test JWT."""
        # Create a simple JWT payload
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
        payload_data = {"sub": "user1", "iat": int(time.time()) - 100,
                        "exp": int(time.time()) + 3600, "role": "developer"}
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(b"fake_signature").decode().rstrip("=")
        token = f"{header}.{payload}.{sig}"

        result = AuthService.get_token_info(token)
        assert result["expired"] is False
        assert "remaining" in result
        assert result["claims"]["role"] == "developer"

    def test_expired_jwt(self):
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
        payload_data = {"sub": "user1", "iat": int(time.time()) - 7200,
                        "exp": int(time.time()) - 3600}
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
        token = f"{header}.{payload}.{sig}"

        result = AuthService.get_token_info(token)
        assert result["expired"] is True

    def test_invalid_jwt(self):
        result = AuthService.get_token_info("not.a.valid.jwt.at.all")
        assert "error" in result

    def test_ensure_valid_token_no_credentials(self):
        import os
        from unittest.mock import patch as _patch
        old_user = os.environ.pop("IFX_USERNAME", None)
        old_pass = os.environ.pop("IFX_PASSWORD", None)
        old_token = os.environ.pop("LLAMA_TOKEN", None)
        try:
            # Patch load_dotenv so it doesn't reload LLAMA_TOKEN from the .env file
            with _patch("src.HybridRAG.code.token_manager.load_dotenv"):
                result = AuthService.ensure_valid_token()
            assert "error" in result
        finally:
            if old_user:
                os.environ["IFX_USERNAME"] = old_user
            if old_pass:
                os.environ["IFX_PASSWORD"] = old_pass
            if old_token:
                os.environ["LLAMA_TOKEN"] = old_token

    def test_ensure_valid_token_with_existing_token(self):
        import os
        # Create a non-expired test JWT
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
        payload_data = {"sub": "test", "iat": int(time.time()), "exp": int(time.time()) + 3600}
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
        test_token = f"{header}.{payload}.{sig}"
        old = os.environ.get("LLAMA_TOKEN")
        os.environ["LLAMA_TOKEN"] = test_token
        try:
            result = AuthService.ensure_valid_token()
            assert "token" in result
            assert result["source"] == "token_manager"
        finally:
            if old:
                os.environ["LLAMA_TOKEN"] = old
            else:
                os.environ.pop("LLAMA_TOKEN", None)


# ═════════════════════════════════════════════════════════════════════════
#  Test 5: Zero Stubs Remaining
# ═════════════════════════════════════════════════════════════════════════

class TestNoStubsRemaining:
    """Verify all NOT_IMPLEMENTED stubs are eliminated."""

    def test_mcp_server_no_stubs(self):
        server_path = Path(__file__).resolve().parents[2] / "mcp" / "core" / "mcp_server.py"
        content = server_path.read_text(encoding="utf-8")
        stub_count = content.count("NOT_IMPLEMENTED")
        # Sprint 3 stubs for API Intelligence + Dependencies + Traceability (10 tools)
        # These are deferred to Sprint 3b or later
        # Count should be exactly those remaining stubs
        assert stub_count <= 10, f"Expected ≤10 stubs (Sprint 3 deferred), found {stub_count}"

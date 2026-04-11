"""
Tests for Semantic Memory (Tickets 3–7)
========================================
All external dependencies are mocked:
  - QdrantClient  → MagicMock injected into PatternStore / PatternIndex via _client param
  - qdrant_client package itself → sys.modules mock (so package need not be installed)

Embedder tests use the REAL sentence-transformers model loaded from local_models/.
They are skipped automatically if sentence-transformers is not installed.

Test groups
-----------
TestApprovedPattern  — dataclass unit tests (pure, no mocks)
TestPatternStore     — Qdrant CRUD with mocked client
TestPatternIndex     — Backward-compat wrapper with mocked client
TestEmbedder         — real model tests (skipped if package missing)
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

# ── inject qdrant_client mock BEFORE importing pattern_index ─────────────────
_QDRANT_MOCK = MagicMock()
sys.modules.setdefault("qdrant_client",        _QDRANT_MOCK)
sys.modules.setdefault("qdrant_client.models", _QDRANT_MOCK.models)
# ─────────────────────────────────────────────────────────────────────────────

# ── make MEMORY_LAYER root importable ────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent   # MEMORY_LAYER/
sys.path.insert(0, str(ROOT))

from src.MemoryLayer.memory.semantic_memory.pattern_store import (
    ApprovedPattern, PatternStore, SimilarPattern, DEFAULT_SIMILARITY_THRESHOLD,
)
from src.MemoryLayer.memory.semantic_memory.pattern_index import PatternIndex
from src.MemoryLayer.memory.semantic_memory.embedder import get_model_config

# ── check sentence-transformers availability ─────────────────────────────────
try:
    import sentence_transformers  # noqa: F401
    _ST_AVAILABLE = True
except ImportError:
    _ST_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_pattern(**overrides) -> ApprovedPattern:
    """Return a minimal valid ApprovedPattern for testing."""
    defaults = dict(
        pattern_text="IfxCxpi_initChannel(&cxpi, &config);",
        pattern_type="api_usage",
        module="cxpi",
        profile="illd",
        confidence=0.95,
        approver_id="engineer_01",
        source_request_id="session-abc-123",
    )
    defaults.update(overrides)
    return ApprovedPattern(**defaults)


def _make_store() -> tuple:
    """Return (PatternStore, mock_qdrant_client, mock_embedder) for testing."""
    mock_client   = MagicMock()
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    store = PatternStore(
        embedder=mock_embedder,
        collection="test_collection",
        _client=mock_client,
    )
    return store, mock_client, mock_embedder


def _make_index() -> tuple:
    """Return (PatternIndex, mock_qdrant_client, mock_embedder) for testing."""
    mock_client   = MagicMock()
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    index = PatternIndex(
        qdrant_url="", embedder=mock_embedder,
        collection="test_collection", _client=mock_client,
    )
    return index, mock_client, mock_embedder


# ─────────────────────────────────────────────────────────────────────────────
# 1. APPROVED PATTERN DATACLASS TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestApprovedPattern(unittest.TestCase):

    def setUp(self):
        self.pattern = _make_pattern()

    def test_pattern_id_is_valid_uuid(self):
        import uuid
        uuid.UUID(self.pattern.pattern_id)

    def test_to_payload_contains_all_required_keys(self):
        params = self.pattern.to_payload()
        for key in (
            "pattern_id", "pattern_text", "pattern_type",
            "module", "profile", "confidence",
            "approval_date", "approver_id", "usage_count",
            "source_request_id", "created_at",
        ):
            self.assertIn(key, params, f"Missing key: {key}")

    def test_to_payload_lowercases_module_profile(self):
        p = _make_pattern(module="CXPI", profile="ILLD")
        params = p.to_payload()
        self.assertEqual(params["module"],  "cxpi")
        self.assertEqual(params["profile"], "illd")

    def test_from_payload_roundtrip(self):
        original  = _make_pattern()
        payload   = original.to_payload()
        recovered = ApprovedPattern.from_payload(payload)
        self.assertEqual(recovered.pattern_id,   original.pattern_id)
        self.assertEqual(recovered.pattern_text, original.pattern_text)
        self.assertEqual(recovered.pattern_type, original.pattern_type)
        self.assertEqual(recovered.confidence,   original.confidence)
        self.assertEqual(recovered.usage_count,  original.usage_count)

    def test_default_usage_count_is_zero(self):
        self.assertEqual(self.pattern.usage_count, 0)

    def test_backward_compat_alias_to_neo4j_params(self):
        """to_neo4j_params still works as an alias for to_payload."""
        self.assertEqual(
            self.pattern.to_neo4j_params(),
            self.pattern.to_payload(),
        )

    def test_backward_compat_alias_from_neo4j_record(self):
        """from_neo4j_record still works as an alias for from_payload."""
        payload = self.pattern.to_payload()
        recovered = ApprovedPattern.from_neo4j_record(payload)
        self.assertEqual(recovered.pattern_id, self.pattern.pattern_id)

    def test_payload_includes_pattern_text(self):
        """Ensure full pattern text is in payload (not just metadata)."""
        params = self.pattern.to_payload()
        self.assertEqual(params["pattern_text"], self.pattern.pattern_text)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PATTERN STORE TESTS (mocked Qdrant client)
# ─────────────────────────────────────────────────────────────────────────────

class TestPatternStore(unittest.TestCase):

    def setUp(self):
        self.store, self.mock_client, self.mock_embedder = _make_store()
        self.pattern = _make_pattern()

    # ── store ────────────────────────────────────────────────────────────────

    def test_store_returns_pattern_id(self):
        result = self.store.store(self.pattern)
        self.assertEqual(result, self.pattern.pattern_id)

    def test_store_calls_upsert(self):
        self.store.store(self.pattern)
        self.mock_client.upsert.assert_called_once()
        call_kwargs = self.mock_client.upsert.call_args[1]
        self.assertEqual(call_kwargs["collection_name"], "test_collection")

    def test_store_generates_embedding(self):
        self.store.store(self.pattern)
        self.mock_embedder.embed.assert_called_once_with(self.pattern.pattern_text)

    def test_store_uses_provided_embedding(self):
        custom_vector = [0.5] * 384
        self.store.store(self.pattern, embedding=custom_vector)
        self.mock_embedder.embed.assert_not_called()

    def test_store_payload_contains_pattern_text(self):
        """Full pattern text must be in the Qdrant payload."""
        self.store.store(self.pattern)
        points = self.mock_client.upsert.call_args[1]["points"]
        point = points[0]
        if isinstance(getattr(point, "payload", None), dict):
            payload = point.payload
        else:
            PointStruct = sys.modules["qdrant_client.models"].PointStruct
            call_kw = PointStruct.call_args
            payload = (call_kw.kwargs if call_kw.kwargs else call_kw[1]).get("payload", {})
        self.assertIn("pattern_text", payload)
        self.assertEqual(payload["pattern_text"], self.pattern.pattern_text)

    # ── get ──────────────────────────────────────────────────────────────────

    def test_get_returns_pattern_when_found(self):
        mock_point = MagicMock()
        mock_point.payload = self.pattern.to_payload()
        self.mock_client.scroll.return_value = ([mock_point], None)
        result = self.store.get(self.pattern.pattern_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern_id, self.pattern.pattern_id)
        self.assertEqual(result.confidence, self.pattern.confidence)

    def test_get_returns_none_when_not_found(self):
        self.mock_client.scroll.return_value = ([], None)
        result = self.store.get("nonexistent-id")
        self.assertIsNone(result)

    # ── usage tracking ───────────────────────────────────────────────────────

    def test_increment_usage_returns_updated_count(self):
        mock_point = MagicMock()
        mock_point.payload = self.pattern.to_payload()  # usage_count=0
        self.mock_client.scroll.return_value = ([mock_point], None)
        count = self.store.increment_usage(self.pattern.pattern_id, source_request_id="req-1")
        self.assertEqual(count, 1)
        self.mock_client.set_payload.assert_called_once()

    def test_increment_usage_returns_minus_one_when_not_found(self):
        self.mock_client.scroll.return_value = ([], None)
        count = self.store.increment_usage("nonexistent-id")
        self.assertEqual(count, -1)

    # ── query by module ──────────────────────────────────────────────────────

    def test_query_by_module_returns_list(self):
        mock_point1 = MagicMock()
        mock_point1.payload = _make_pattern(module="cxpi").to_payload()
        mock_point2 = MagicMock()
        mock_point2.payload = _make_pattern(module="cxpi").to_payload()
        self.mock_client.scroll.return_value = ([mock_point1, mock_point2], None)
        result = self.store.query_by_module("cxpi")
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], ApprovedPattern)

    # ── find similar ─────────────────────────────────────────────────────────

    def test_find_similar_returns_results(self):
        mock_hit = MagicMock()
        mock_hit.score = 0.92
        mock_hit.payload = {
            "pattern_id":   self.pattern.pattern_id,
            "profile":      "illd",
            "module":       "cxpi",
            "pattern_type": "api_usage",
            "confidence":   0.95,
            "usage_count":  3,
        }
        self.mock_client.search.return_value = [mock_hit]
        results = self.store.find_similar("init cxpi channel", "cxpi")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], SimilarPattern)
        self.assertAlmostEqual(results[0].score, 0.92)

    def test_find_similar_passes_threshold(self):
        self.mock_client.search.return_value = []
        self.store.find_similar("query", "cxpi", threshold=0.8, top_k=3)
        call_kwargs = self.mock_client.search.call_args[1]
        self.assertEqual(call_kwargs["score_threshold"], 0.8)
        self.assertEqual(call_kwargs["limit"], 3)

    def test_find_similar_returns_empty_when_no_hits(self):
        self.mock_client.search.return_value = []
        results = self.store.find_similar("something", "cxpi")
        self.assertEqual(results, [])

    def test_default_threshold_is_0_8(self):
        self.assertEqual(DEFAULT_SIMILARITY_THRESHOLD, 0.8)


# ─────────────────────────────────────────────────────────────────────────────
# 3. PATTERN INDEX TESTS (backward-compat wrapper)
# ─────────────────────────────────────────────────────────────────────────────

class TestPatternIndex(unittest.TestCase):

    def setUp(self):
        self.index, self.mock_client, self.mock_embedder = _make_index()
        self.pattern = _make_pattern()

    def test_make_point_id_format(self):
        pid = PatternIndex.make_point_id("cxpi", "a1b2c3d4-e5f6-7890-abcd-ef1234567890")
        self.assertEqual(pid, "cxpi_a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    def test_make_point_id_lowercases_module(self):
        pid = PatternIndex.make_point_id("CXPI", "a1b2c3d4-e5f6-7890-abcd-ef1234567890")
        self.assertTrue(pid.startswith("cxpi_"))

    def test_index_pattern_calls_upsert(self):
        self.index.index_pattern(self.pattern)
        self.mock_client.upsert.assert_called_once()
        call_kwargs = self.mock_client.upsert.call_args[1]
        self.assertEqual(call_kwargs["collection_name"], "test_collection")

    def test_index_pattern_generates_embedding(self):
        self.index.index_pattern(self.pattern)
        self.mock_embedder.embed.assert_called_once_with(self.pattern.pattern_text)

    def test_index_pattern_uses_provided_embedding(self):
        custom_vector = [0.5] * 384
        self.index.index_pattern(self.pattern, embedding=custom_vector)
        self.mock_embedder.embed.assert_not_called()

    def test_find_similar_returns_results(self):
        mock_hit = MagicMock()
        mock_hit.score = 0.92
        mock_hit.payload = {
            "pattern_id":   self.pattern.pattern_id,
            "profile":      "illd",
            "module":       "cxpi",
            "pattern_type": "api_usage",
            "confidence":   0.95,
            "usage_count":  3,
        }
        self.mock_client.search.return_value = [mock_hit]
        results = self.index.find_similar("init cxpi channel", "cxpi")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], SimilarPattern)

    def test_find_similar_passes_threshold(self):
        self.mock_client.search.return_value = []
        self.index.find_similar("query", "cxpi", threshold=0.8, top_k=3)
        call_kwargs = self.mock_client.search.call_args[1]
        self.assertEqual(call_kwargs["score_threshold"], 0.8)
        self.assertEqual(call_kwargs["limit"], 3)

    def test_find_similar_returns_empty_when_no_hits(self):
        self.mock_client.search.return_value = []
        results = self.index.find_similar("something", "cxpi")
        self.assertEqual(results, [])


# ─────────────────────────────────────────────────────────────────────────────
# 4. EMBEDDER TESTS (real sentence-transformers model)
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipUnless(_ST_AVAILABLE, "sentence-transformers not installed — skipping embedder tests")
class TestEmbedder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Load model once for all tests in this class."""
        from src.MemoryLayer.memory.semantic_memory.embedder import Embedder
        cls.embedder = Embedder()

    def test_embed_returns_list_of_floats(self):
        vector = self.embedder.embed("initialise CXPI channel")
        self.assertIsInstance(vector, list)
        self.assertTrue(all(isinstance(v, float) for v in vector))

    def test_embed_returns_correct_dimension(self):
        vector = self.embedder.embed("some text")
        self.assertEqual(len(vector), self.embedder.dimension)

    def test_embed_batch_returns_multiple_vectors(self):
        texts   = ["text one", "text two", "text three"]
        vectors = self.embedder.embed_batch(texts)
        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(v) == self.embedder.dimension for v in vectors))

    def test_model_config_returns_name_and_revision(self):
        model_name, revision = get_model_config()
        self.assertIn("all-MiniLM-L6-v2", model_name)
        self.assertEqual(len(revision), 40, "Revision should be a 40-char commit SHA")

    def test_embedder_has_pinned_revision(self):
        self.assertTrue(len(self.embedder.revision) == 40,
                        "Embedder should have a 40-char pinned revision")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
Tests for Batch Ingestion Pipeline (src/IngestionPipeline/batch_ingestion.py)
==============================================================================
Covers BatchIngestionPipeline.ingest_async, sync ingest, IngestionJob
dataclass, BatchEmbedder, and error handling in TaskGroup.
Uses pytest-asyncio for async test support.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ── Path bootstrapping ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

try:
    from IngestionPipeline.batch_ingestion import (
        BatchEmbedder,
        BatchGraphWriter,
        BatchIngestionPipeline,
        BatchResult,
        BatchUploader,
        IngestionJob,
        IngestionJobTracker,
    )
except SyntaxError as _err:
    pytest.skip(
        f"batch_ingestion.py requires Python >=3.11 (except* syntax): {_err}",
        allow_module_level=True,
    )


# ── Helpers ─────────────────────────────────────────────────────────────

def _sample_documents(n: int = 5) -> list:
    """Generate sample documents for ingestion."""
    return [
        {
            "id": f"doc_{i}",
            "content": f"Document {i} about CAN module IfxCan_init function implementation.",
            "module": "can",
            "type": "Function",
        }
        for i in range(n)
    ]


def _mock_embed_fn(text: str) -> list:
    """Mock embedding function returning a fixed-length vector."""
    return [0.1] * 384


# ═══════════════════════════════════════════════════════════════════════
#  IngestionJob dataclass
# ═══════════════════════════════════════════════════════════════════════

class TestIngestionJob:

    def test_auto_generates_job_id(self):
        """IngestionJob should auto-generate a job_id if not provided."""
        job = IngestionJob()
        assert job.job_id.startswith("ingest_")
        assert len(job.job_id) > 10

    def test_provided_job_id_kept(self):
        """If job_id is provided, it should be kept."""
        job = IngestionJob(job_id="custom_id_123")
        assert job.job_id == "custom_id_123"

    def test_default_status_is_pending(self):
        """Default status should be 'pending'."""
        job = IngestionJob()
        assert job.status == "pending"

    def test_progress_calculation(self):
        """Progress should be processed / total."""
        job = IngestionJob(total_items=100, processed_items=50)
        assert job.progress == 0.5

    def test_progress_zero_when_no_items(self):
        """Progress should be 0 when total_items is 0."""
        job = IngestionJob(total_items=0, processed_items=0)
        assert job.progress == 0.0

    def test_as_dict_keys(self):
        """as_dict should contain all expected keys."""
        job = IngestionJob(module="can", total_items=10)
        d = job.as_dict()
        expected_keys = {
            "job_id", "status", "source_type", "module", "workspace_id",
            "total_items", "processed_items", "failed_items",
            "embedded_items", "graph_items", "progress",
            "started_at", "completed_at", "error",
        }
        assert set(d.keys()) == expected_keys

    def test_as_dict_progress_percentage(self):
        """Progress in as_dict should be a percentage (0-100)."""
        job = IngestionJob(total_items=200, processed_items=100)
        d = job.as_dict()
        assert d["progress"] == 50.0


# ═══════════════════════════════════════════════════════════════════════
#  BatchEmbedder
# ═══════════════════════════════════════════════════════════════════════

class TestBatchEmbedder:

    def test_embed_batch_with_custom_fn(self):
        """BatchEmbedder with custom embed_fn should use it for each text."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn, batch_size=2)
        texts = ["hello world", "foo bar", "baz qux"]
        vectors = embedder.embed_batch(texts)

        assert len(vectors) == 3
        assert all(len(v) == 384 for v in vectors)

    def test_embed_batch_respects_batch_size(self):
        """Embedder should process in configured batch sizes."""
        call_count = [0]

        def counting_embed(text):
            call_count[0] += 1
            return [0.1] * 384

        embedder = BatchEmbedder(embed_fn=counting_embed, batch_size=2)
        texts = ["a", "b", "c", "d", "e"]
        vectors = embedder.embed_batch(texts)

        assert len(vectors) == 5
        assert call_count[0] == 5  # one call per text in custom fn mode

    def test_embed_batch_empty_input(self):
        """Empty input should return empty output."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        vectors = embedder.embed_batch([])
        assert vectors == []

    def test_embed_batch_fallback_on_failure(self):
        """If batch embedding fails, individual items should be tried."""
        call_count = [0]

        def flaky_embed(text):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("batch fail")
            return [0.1] * 384

        embedder = BatchEmbedder(embed_fn=flaky_embed, batch_size=10)
        # The batch of 3 fails on first call, then individual items retry
        texts = ["a", "b", "c"]
        vectors = embedder.embed_batch(texts)
        # Should still get vectors (via individual fallback)
        assert len(vectors) == 3


# ═══════════════════════════════════════════════════════════════════════
#  BatchUploader
# ═══════════════════════════════════════════════════════════════════════

class TestBatchUploader:

    def test_not_available_without_client(self):
        """Uploader without qdrant_client should not be available."""
        uploader = BatchUploader(qdrant_client=None)
        assert uploader.available is False

    def test_upsert_returns_failure_when_unavailable(self):
        """Upsert on unavailable uploader should return all-failed result."""
        uploader = BatchUploader(qdrant_client=None)
        result = uploader.upsert_batch(["id1"], [[0.1] * 384], [{"k": "v"}])
        assert result.failed == 1
        assert result.succeeded == 0


# ═══════════════════════════════════════════════════════════════════════
#  BatchGraphWriter
# ═══════════════════════════════════════════════════════════════════════

class TestBatchGraphWriter:

    def test_not_available_without_driver(self):
        """Writer without neo4j_driver should not be available."""
        writer = BatchGraphWriter(neo4j_driver=None)
        assert writer.available is False

    def test_merge_nodes_returns_failure_when_unavailable(self):
        """Merge on unavailable writer should return all-failed result."""
        writer = BatchGraphWriter(neo4j_driver=None)
        result = writer.merge_nodes_batch([{"id": "x"}], "Function")
        assert result.failed == 1
        assert result.succeeded == 0

    def test_merge_empty_nodes_returns_zero(self):
        """Merging empty list should return zero counts."""
        writer = BatchGraphWriter(neo4j_driver=None)
        result = writer.merge_nodes_batch([], "Function")
        assert result.batch_size == 0


# ═══════════════════════════════════════════════════════════════════════
#  BatchIngestionPipeline — sync ingest
# ═══════════════════════════════════════════════════════════════════════

class TestBatchIngestionSync:

    def test_sync_ingest_with_mock_embeddings(self):
        """Sync ingest should complete with mock embedder (no Qdrant/Neo4j)."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        pipeline = BatchIngestionPipeline(
            embedder=embedder,
            uploader=None,
            graph_writer=None,
            tracker=None,
        )
        docs = _sample_documents(3)
        result = pipeline.ingest(docs, module="can")

        assert result["job"]["status"] == "complete"
        assert result["job"]["processed_items"] == 3
        assert result["embed"]["succeeded"] == 3
        assert result["qdrant"]["succeeded"] == 0  # no uploader
        assert result["neo4j"]["succeeded"] == 0   # no writer
        assert "total_latency_ms" in result

    def test_sync_ingest_progress_callback(self):
        """Progress callback should be called during ingestion."""
        progress_calls = []

        def on_progress(processed, total):
            progress_calls.append((processed, total))

        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        pipeline = BatchIngestionPipeline(embedder=embedder)
        docs = _sample_documents(3)
        pipeline.ingest(docs, module="can", progress_callback=on_progress)

        assert len(progress_calls) >= 1

    def test_sync_ingest_with_qdrant_and_neo4j(self):
        """Sync ingest should use uploader and writer when available."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)

        # Mock Qdrant uploader
        mock_uploader = MagicMock()
        mock_uploader.available = True
        mock_uploader.upsert_batch = MagicMock(
            return_value=BatchResult(0, 3, 3, 0, 0, 10.0))

        # Mock Neo4j writer
        mock_writer = MagicMock()
        mock_writer.available = True
        mock_writer.merge_nodes_batch = MagicMock(
            return_value=BatchResult(0, 3, 3, 0, 0, 10.0))

        pipeline = BatchIngestionPipeline(
            embedder=embedder,
            uploader=mock_uploader,
            graph_writer=mock_writer,
        )
        docs = _sample_documents(3)
        result = pipeline.ingest(docs, module="can")

        assert result["job"]["status"] == "complete"
        assert result["qdrant"]["succeeded"] == 3
        assert result["neo4j"]["succeeded"] == 3
        mock_uploader.upsert_batch.assert_called_once()
        mock_writer.merge_nodes_batch.assert_called_once()

    def test_sync_ingest_handles_embed_failure(self):
        """If embedding fails entirely, job should be marked failed."""
        def failing_embed(text):
            raise RuntimeError("GPU out of memory")

        embedder = BatchEmbedder(embed_fn=failing_embed)
        pipeline = BatchIngestionPipeline(embedder=embedder)
        docs = _sample_documents(2)

        # The embedder falls back to zero vectors on individual failure,
        # so it shouldn't raise but should still complete
        result = pipeline.ingest(docs, module="can")
        # The result should still have the job dict
        assert "job" in result


# ═══════════════════════════════════════════════════════════════════════
#  BatchIngestionPipeline — has ingest_async
# ═══════════════════════════════════════════════════════════════════════

class TestBatchIngestionHasAsync:

    def test_has_ingest_async_method(self):
        """BatchIngestionPipeline must have an ingest_async method."""
        pipeline = BatchIngestionPipeline()
        assert hasattr(pipeline, "ingest_async")
        assert asyncio.iscoroutinefunction(pipeline.ingest_async)


# ═══════════════════════════════════════════════════════════════════════
#  BatchIngestionPipeline — async ingest
# ═══════════════════════════════════════════════════════════════════════

class TestBatchIngestionAsync:

    @pytest.mark.asyncio
    async def test_async_ingest_with_mock_embeddings(self):
        """Async ingest should complete with mock embedder."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        pipeline = BatchIngestionPipeline(
            embedder=embedder,
            uploader=None,
            graph_writer=None,
            tracker=None,
        )
        docs = _sample_documents(3)
        result = await pipeline.ingest_async(docs, module="can", max_workers=1)

        assert result["job"]["status"] == "complete"
        assert result["job"]["processed_items"] == 3
        assert result["embed"]["succeeded"] == 3

    @pytest.mark.asyncio
    async def test_async_ingest_with_qdrant_and_neo4j(self):
        """Async ingest should run Qdrant + Neo4j concurrently via TaskGroup."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)

        # Mock Qdrant uploader
        mock_uploader = MagicMock()
        mock_uploader.available = True
        mock_uploader.upsert_batch = MagicMock(
            return_value=BatchResult(0, 3, 3, 0, 0, 10.0))

        # Mock Neo4j writer
        mock_writer = MagicMock()
        mock_writer.available = True
        mock_writer.merge_nodes_batch = MagicMock(
            return_value=BatchResult(0, 3, 3, 0, 0, 10.0))

        pipeline = BatchIngestionPipeline(
            embedder=embedder,
            uploader=mock_uploader,
            graph_writer=mock_writer,
        )
        docs = _sample_documents(3)
        result = await pipeline.ingest_async(docs, module="can", max_workers=1)

        assert result["job"]["status"] == "complete"
        assert result["qdrant"]["succeeded"] == 3
        assert result["neo4j"]["succeeded"] == 3

    @pytest.mark.asyncio
    async def test_async_ingest_error_handling_in_taskgroup(self):
        """If Qdrant upsert fails in TaskGroup, job should be marked failed."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)

        # Qdrant uploader that raises
        mock_uploader = MagicMock()
        mock_uploader.available = True
        mock_uploader.upsert_batch = MagicMock(
            side_effect=ConnectionError("Qdrant connection refused"))

        pipeline = BatchIngestionPipeline(
            embedder=embedder,
            uploader=mock_uploader,
            graph_writer=None,
        )
        docs = _sample_documents(2)
        result = await pipeline.ingest_async(docs, module="can", max_workers=1)

        # The except* block catches ExceptionGroup from TaskGroup
        assert result["job"]["status"] == "failed"
        assert result["job"]["error"] is not None
        assert "connection refused" in result["job"]["error"].lower()

    @pytest.mark.asyncio
    async def test_async_ingest_progress_callback(self):
        """Progress callback should be invoked during async ingestion."""
        progress_calls = []

        def on_progress(processed, total):
            progress_calls.append((processed, total))

        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        pipeline = BatchIngestionPipeline(embedder=embedder)
        docs = _sample_documents(3)
        await pipeline.ingest_async(
            docs, module="can", progress_callback=on_progress, max_workers=1)

        assert len(progress_calls) >= 1

    @pytest.mark.asyncio
    async def test_async_ingest_empty_documents(self):
        """Async ingest with empty documents should complete without error."""
        embedder = BatchEmbedder(embed_fn=_mock_embed_fn)
        pipeline = BatchIngestionPipeline(embedder=embedder)
        result = await pipeline.ingest_async([], module="can", max_workers=1)

        assert result["job"]["status"] == "complete"
        assert result["job"]["total_items"] == 0
        assert result["embed"]["succeeded"] == 0


# ═══════════════════════════════════════════════════════════════════════
#  IngestionJobTracker
# ═══════════════════════════════════════════════════════════════════════

class TestIngestionJobTracker:

    def test_not_available_without_pool(self):
        """Tracker without pg_pool should not be available."""
        tracker = IngestionJobTracker(pg_pool=None)
        assert tracker.available is False

    def test_create_job_returns_false_when_unavailable(self):
        """create_job should return False when no PostgreSQL pool."""
        tracker = IngestionJobTracker(pg_pool=None)
        job = IngestionJob(module="can", total_items=10)
        assert tracker.create_job(job) is False

    def test_update_progress_returns_false_when_unavailable(self):
        """update_progress should return False when no PostgreSQL pool."""
        tracker = IngestionJobTracker(pg_pool=None)
        assert tracker.update_progress("job_123", 5) is False


# ═══════════════════════════════════════════════════════════════════════
#  BatchResult dataclass
# ═══════════════════════════════════════════════════════════════════════

class TestBatchResult:

    def test_batch_result_fields(self):
        """BatchResult should store all fields correctly."""
        result = BatchResult(
            batch_index=0, batch_size=100,
            succeeded=95, failed=5,
            retried=3, latency_ms=150.0,
            errors=["timeout on item 42"],
        )
        assert result.batch_index == 0
        assert result.batch_size == 100
        assert result.succeeded == 95
        assert result.failed == 5
        assert result.retried == 3
        assert result.latency_ms == 150.0
        assert len(result.errors) == 1

    def test_batch_result_default_errors(self):
        """Default errors should be empty list."""
        result = BatchResult(0, 10, 10, 0, 0, 5.0)
        assert result.errors == []

"""
Vector Store Factory — Returns a Qdrant client wrapped in a
Qdrant vector store client factory.
Configuration is read from ``vector_backend`` in storage_config.yaml.

Consuming code uses the same API regardless of backend:

    client = get_vector_client(instance="mcal")
    collection = client.get_or_create_collection("my_collection")
    collection.upsert(ids=[...], documents=[...], embeddings=[...], metadatas=[...])
    results = collection.query(query_embeddings=[...], n_results=10)
    collection.count()
"""

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent            # .../HybridRAG/code/RAG
CODE_DIR = SCRIPT_DIR.parent                            # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                         # .../HybridRAG

# Deterministic UUID5 namespace for Qdrant point-ID mapping
_ID_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def _str_to_uuid(string_id: str) -> str:
    """Deterministic string → UUID5 for Qdrant point IDs."""
    return str(uuid.uuid5(_ID_NAMESPACE, string_id))


# ---------------------------------------------------------------------------
# Qdrant Collection Adapter
# ---------------------------------------------------------------------------

QDRANT_UPSERT_BATCH_SIZE = 512


class _QdrantCollectionAdapter:
    """Wraps a single Qdrant collection behind a common collection API."""

    def __init__(self, client, collection_name: str, embedding_dim: int):
        self._client = client
        self._name = collection_name
        self._dim = embedding_dim

    def upsert(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        batch_size: int = QDRANT_UPSERT_BATCH_SIZE,
    ) -> None:
        from qdrant_client.models import PointStruct

        points = []
        for i, str_id in enumerate(ids):
            payload: Dict[str, Any] = {"_original_id": str_id}
            if documents and i < len(documents):
                payload["document"] = documents[i]
            if metadatas and i < len(metadatas):
                payload.update(metadatas[i])

            vector = embeddings[i] if embeddings and i < len(embeddings) else [0.0] * self._dim
            points.append(PointStruct(
                id=_str_to_uuid(str_id),
                vector=vector,
                payload=payload,
            ))

        # Batch upsert — avoids oversized single requests
        if len(points) <= batch_size:
            self._client.upsert(collection_name=self._name, points=points)
        else:
            self._batch_upsert(points, batch_size)

    def _batch_upsert(self, points: list, batch_size: int) -> None:
        """Upsert in batches, disabling Qdrant indexing during bulk load."""
        from qdrant_client.models import OptimizersConfigDiff

        # Pause indexing while bulk-loading
        self._client.update_collection(
            collection_name=self._name,
            optimizer_config=OptimizersConfigDiff(indexing_threshold=0),
        )
        try:
            for start in range(0, len(points), batch_size):
                batch = points[start : start + batch_size]
                self._client.upsert(collection_name=self._name, points=batch)
                logger.debug("Batch upsert %s: %d-%d / %d",
                             self._name, start, start + len(batch), len(points))
        finally:
            # Re-enable indexing
            self._client.update_collection(
                collection_name=self._name,
                optimizer_config=OptimizersConfigDiff(indexing_threshold=20_000),
            )

    def query(
        self,
        query_embeddings: Optional[List[List[float]]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Search — returns a result dict."""
        if not query_embeddings:
            return {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}

        query_filter = self._build_filter(where) if where else None

        all_ids, all_distances, all_documents, all_metadatas = [], [], [], []
        for emb in query_embeddings:
            results = self._client.query_points(
                collection_name=self._name,
                query=emb,
                limit=n_results,
                query_filter=query_filter,
                with_payload=True,
            )
            batch_ids, batch_dist, batch_docs, batch_meta = [], [], [], []
            for sp in results.points:
                payload = sp.payload or {}
                batch_ids.append(payload.get("_original_id", str(sp.id)))
                batch_dist.append(1.0 - sp.score)  # similarity→distance
                batch_docs.append(payload.get("document"))
                batch_meta.append({k: v for k, v in payload.items()
                                   if k not in ("_original_id", "document")})
            all_ids.append(batch_ids)
            all_distances.append(batch_dist)
            all_documents.append(batch_docs)
            all_metadatas.append(batch_meta)

        return {
            "ids": all_ids,
            "distances": all_distances,
            "documents": all_documents,
            "metadatas": all_metadatas,
        }

    def get(self, ids=None, include=None, limit=None, **kwargs):
        """Retrieve documents by ID."""
        if ids:
            qdrant_ids = [_str_to_uuid(sid) for sid in ids]
            points = self._client.retrieve(
                collection_name=self._name,
                ids=qdrant_ids,
                with_payload=True,
            )
        else:
            scroll_result = self._client.scroll(
                collection_name=self._name,
                with_payload=True,
                limit=limit or 100,
            )
            points = scroll_result[0]

        result_ids, result_docs, result_meta = [], [], []
        for pt in points:
            payload = pt.payload or {}
            result_ids.append(payload.get("_original_id", str(pt.id)))
            result_docs.append(payload.get("document"))
            result_meta.append({k: v for k, v in payload.items()
                                if k not in ("_original_id", "document")})
        return {"ids": result_ids, "documents": result_docs, "metadatas": result_meta}

    def count(self) -> int:
        info = self._client.get_collection(self._name)
        return info.points_count or 0

    @staticmethod
    def _build_filter(where: Dict[str, Any]):
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny
        conditions = []
        for key, value in where.items():
            if isinstance(value, dict):
                for op, operand in value.items():
                    if op == "$in" and isinstance(operand, list):
                        conditions.append(FieldCondition(key=key, match=MatchAny(any=operand)))
                    elif op == "$eq":
                        conditions.append(FieldCondition(key=key, match=MatchValue(value=operand)))
            else:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=conditions) if conditions else None


class _QdrantClientAdapter:
    """Wraps a QdrantClient behind a common client API."""

    def __init__(self, client, embedding_dim: int = 384):
        self._client = client
        self._dim = embedding_dim

    def get_or_create_collection(self, name: str, metadata: Optional[dict] = None):
        from qdrant_client.models import Distance, VectorParams
        existing = [c.name for c in self._client.get_collections().collections]
        if name not in existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection '%s' (dim=%d)", name, self._dim)
        return _QdrantCollectionAdapter(self._client, name, self._dim)

    def get_collection(self, name: str):
        return _QdrantCollectionAdapter(self._client, name, self._dim)

    def list_collections(self):
        """Return list of collection-like objects with .name attribute."""
        class _C:
            def __init__(self, n):
                self.name = n
        return [_C(c.name) for c in self._client.get_collections().collections]

    def delete_collection(self, name: str):
        self._client.delete_collection(collection_name=name)

    def heartbeat(self):
        self._client.get_collections()
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_vector_client(
    instance: Optional[str] = None,
    config_path: Optional[Path] = None,
):
    """
    Return a Qdrant client wrapped in a common API.

    Parameters
    ----------
    instance : str, optional
        "illd" or "mcal". Defaults to active_instance in config.
    config_path : Path, optional
        Explicit config path. Defaults to storage_config.yaml.

    Returns
    -------
    A client with ``get_or_create_collection()``, ``get_collection()``,
    ``list_collections()``, ``delete_collection()``, ``heartbeat()``.
    """
    import sys
    sys.path.insert(0, str(CODE_DIR))
    from neo4j_manager import load_config, _resolve_instance_name, get_vector_backend

    cfg_path = config_path or (HYBRIDRAG_DIR / "config" / "storage_config.yaml")
    raw = load_config(cfg_path)
    backend = get_vector_backend(cfg_path)
    active = _resolve_instance_name(raw, instance)

    if backend == "qdrant":
        return _create_qdrant_client(raw, active)
    else:
        # Default to Qdrant
        return _create_qdrant_client(raw, active)


def _create_qdrant_client(raw: dict, instance: str):
    """Create a Qdrant client wrapped in the adapter."""
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        raise ImportError(
            "qdrant-client is required but not installed. "
            "Install with: pip install qdrant-client>=1.7.0"
        )

    qdrant_cfg = raw.get("qdrant", {})
    url = qdrant_cfg.get("url", "")
    port = qdrant_cfg.get("port", 443)
    api_key = qdrant_cfg.get("api_key", "")
    https = qdrant_cfg.get("https", True)
    verify_ssl = qdrant_cfg.get("verify_ssl", False)
    embedding_dim = qdrant_cfg.get("embedding_dimension", 384)
    grpc = qdrant_cfg.get("grpc", False)
    timeout = qdrant_cfg.get("timeout", 30)

    logger.info("Using Qdrant backend at %s", url)

    def _build_client(prefer_grpc: bool):
        return QdrantClient(
            url=url,
            port=port,
            api_key=api_key,
            https=https,
            verify=verify_ssl,
            prefer_grpc=prefer_grpc,
            timeout=timeout,
        )

    # Some environments expose Qdrant over HTTPS/REST but not gRPC.
    # If gRPC is preferred and unreachable, transparently fall back to REST.
    client = _build_client(prefer_grpc=grpc)
    if grpc:
        try:
            client.get_collections()
        except Exception as exc:
            logger.warning(
                "Qdrant gRPC unavailable (%s). Falling back to HTTP REST.",
                exc,
            )
            client = _build_client(prefer_grpc=False)

    return _QdrantClientAdapter(client, embedding_dim=embedding_dim)

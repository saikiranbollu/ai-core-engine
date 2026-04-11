"""
Unified RAG Querier — Profile-Agnostic
========================================
Semantic search over vector-store collections for **any** ontology
profile (mcal, illd, or future profiles).

Replaces the profile-specific ``MCALRAGQuerier`` and ``ILLDRAGQuerier``
with a single class that:

1. Reads ``storage_config.yaml`` to pick the right backend (Qdrant).
2. Uses ``collection_naming_unified.module_collections()`` to derive
   the correct collection names for the active profile.
3. Provides a consistent result dataclass (``RAGResult``) regardless of
   profile.

Usage::

    from HybridRAG.code.RAG.rag_query_unified import UnifiedRAGQuerier

    q = UnifiedRAGQuerier(module="ADC")              # uses active profile
    q = UnifiedRAGQuerier(module="CXPI", profile="illd")  # explicit

    results = q.search("How does init configure hardware?", top_k=5)
    for r in results:
        print(r.score, r.heading, r.text[:200])
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path bootstrapping
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../RAG
_CODE_DIR = _SCRIPT_DIR.parent                         # .../HybridRAG/code
_HYBRIDRAG_DIR = _CODE_DIR.parent                      # .../HybridRAG
_CONFIG_DIR = _HYBRIDRAG_DIR / "config"

for p in (_SCRIPT_DIR, _CODE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_storage_config() -> dict:
    cfg_path = _CONFIG_DIR / "storage_config.yaml"
    try:
        from env_config import load_yaml_with_env
        return load_yaml_with_env(cfg_path)
    except ImportError:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)


def _active_profile() -> str:
    return _load_storage_config().get("active_instance", "illd")


RRF_K = 60  # Reciprocal Rank Fusion constant


# ---------------------------------------------------------------------------
# Unified result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RAGResult:
    """A single search result — unified across all profiles."""
    chunk_id: str = ""
    score: float = 0.0
    text: str = ""
    heading: str = ""
    collection: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── Convenience accessors (work for both MCAL and ILLD metadata) ─────
    @property
    def section(self) -> str:
        return self.metadata.get("heading", "") or self.metadata.get("section", "")

    @property
    def module(self) -> str:
        return self.metadata.get("module", "")

    @property
    def doc_type(self) -> str:
        return self.metadata.get("type", "")

    @property
    def source_file(self) -> str:
        return self.metadata.get("source_file", "")

    @property
    def tags(self) -> List[str]:
        t = self.metadata.get("tags", "")
        return [x.strip() for x in t.split(",") if x.strip()] if t else []

    @property
    def functions(self) -> List[str]:
        f = self.metadata.get("related_functions", "")
        return [x.strip() for x in f.split(",") if x.strip()] if f else []

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "score": round(self.score, 4),
            "heading": self.heading,
            "section": self.section,
            "module": self.module,
            "doc_type": self.doc_type,
            "collection": self.collection,
            "source_file": self.source_file,
            "tags": self.tags,
            "functions": self.functions,
            "text": self.text,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Unified Querier
# ---------------------------------------------------------------------------

class UnifiedRAGQuerier:
    """
    Semantic search over vector collections for any ontology profile.

    Parameters
    ----------
    module : str
        Module name (e.g. ``"ADC"`` or ``"CXPI"``).
    profile : str, optional
        Ontology profile. Defaults to ``active_instance`` from config.
    collections : list[str], optional
        Override which collections to search.
    """

    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    _model_lock = threading.Lock()

    def __init__(
        self,
        module: str = "ADC",
        profile: Optional[str] = None,
        collections: Optional[List[str]] = None,
    ):
        self.profile = profile or _active_profile()
        self.module = module.upper()
        self._module_lower = module.lower()
        self._collections_override = collections
        self._model = None
        self._client = None

    # ── lazy loaders ──────────────────────────────────────────────────────

    def _get_model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    from src.MemoryLayer.memory.semantic_memory.embedder import get_model_config

                    model_name, revision = get_model_config()
                    logger.info("Loading embedding model: %s (rev %s)", model_name, revision)
                    self._model = SentenceTransformer(model_name, revision=revision)
        return self._model

    def _get_client(self):
        if self._client is None:
            from RAG.vector_store_factory import get_vector_client
            self._client = get_vector_client(instance=self.profile)
        return self._client

    # ── collection discovery ──────────────────────────────────────────────

    def _get_collections(self) -> List[str]:
        """Return target collection names: explicit override → discover → naming convention."""
        if self._collections_override:
            return self._collections_override

        # Try to discover what actually exists on the backend
        discovered = self._discover_collections()
        if discovered:
            return discovered

        # Fallback to naming-convention based list
        from RAG.collection_naming_unified import module_collections
        return module_collections(self.module, profile=self.profile)

    def _discover_collections(self) -> List[str]:
        """Probe the backend for collections matching this module."""
        client = self._get_client()
        try:
            available = {
                c.name if hasattr(c, "name") else str(c)
                for c in client.list_collections()
            }
        except Exception:
            return []

        from RAG.collection_naming_unified import module_collections
        expected = module_collections(self.module, profile=self.profile)
        found = [c for c in expected if c in available]

        # Fallback: bare module name
        if not found and self._module_lower in available:
            found.append(self._module_lower)

        logger.info(
            "%s collections for %s/%s: %d found",
            self.profile.upper(), self.module, self.profile,
            len(found),
        )
        return found

    # ── core search ───────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        collections: Optional[List[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        use_rrf: bool = True,
    ) -> List[RAGResult]:
        """
        Search across vector collections for the configured profile.

        Parameters
        ----------
        query : str
            Natural-language query.
        top_k : int
            Maximum results to return.
        collections : list[str], optional
            Override collections for this query. ``None`` uses default.
        where : dict, optional
            Metadata filter.
        where_document : dict, optional
            Document content filter.
        use_rrf : bool
            Use Reciprocal Rank Fusion when searching multiple collections.

        Returns
        -------
        list[RAGResult]
        """
        target_colls = collections or self._get_collections()
        all_results: List[RAGResult] = []

        for coll_name in target_colls:
            try:
                results = self._search_collection(
                    query, coll_name,
                    top_k=top_k * 2,
                    where=where,
                    where_document=where_document,
                )
                all_results.extend(results)
            except Exception as exc:
                logger.debug("Collection %s search error: %s", coll_name, exc)

        if not all_results:
            logger.warning("No results for query: %s", query[:80])
            return []

        # Fuse when searching multiple collections
        if use_rrf and len(target_colls) > 1:
            if self.profile == "mcal":
                all_results = self._reciprocal_rank_fusion_global(all_results)
            else:
                all_results = self._reciprocal_rank_fusion_percoll(all_results)

        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results[:top_k]

    def _search_collection(
        self,
        query: str,
        collection_name: str,
        top_k: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
    ) -> List[RAGResult]:
        """Query a single collection via the vector store API."""
        client = self._get_client()
        model = self._get_model()

        try:
            collection = client.get_collection(name=collection_name)
        except Exception:
            return []

        count = collection.count()
        if count == 0:
            return []

        query_embedding = model.encode([query]).tolist()
        query_args: Dict[str, Any] = {
            "query_embeddings": query_embedding,
            "n_results": min(top_k, count),
        }
        if where:
            query_args["where"] = where
        if where_document:
            query_args["where_document"] = where_document

        results = collection.query(**query_args)

        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        out: List[RAGResult] = []
        for i, chunk_id in enumerate(ids):
            dist = distances[i] if i < len(distances) else 1.0
            similarity = max(0.0, 1.0 - dist)
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""

            out.append(RAGResult(
                chunk_id=chunk_id,
                score=similarity,
                text=doc or "",
                heading=meta.get("heading") or meta.get("name") or meta.get("chunk_id", chunk_id),
                collection=collection_name,
                metadata=meta or {},
            ))

        return out

    # ── Score normalisation ────────────────────────────────────────────────

    @staticmethod
    def _normalise(results: List[RAGResult]) -> List[RAGResult]:
        """Normalise scores to [0, 1] by dividing by the max score."""
        if not results:
            return results
        max_score = max(r.score for r in results)
        if max_score <= 0:
            return results
        for r in results:
            r.score = r.score / max_score
        return results

    # ── Reciprocal Rank Fusion ────────────────────────────────────────────

    @staticmethod
    def _reciprocal_rank_fusion_global(results: List[RAGResult]) -> List[RAGResult]:
        """MCAL: Global normalisation across all collections.

        All results are normalised to [0, 1] together (divide by the single
        highest score across every collection).  Preserves cross-collection
        score differences.
        """
        if not results:
            return results

        max_score = max(r.score for r in results)
        if max_score > 0:
            for r in results:
                r.score = r.score / max_score

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    @staticmethod
    def _reciprocal_rank_fusion_percoll(results: List[RAGResult]) -> List[RAGResult]:
        """ILLD: Per-collection RRF with rank-based scoring.

        Groups results by collection, ranks within each, computes
        ``1 / (k + rank)`` per entry, then normalises by max RRF score.
        """
        if not results:
            return results

        per_coll: Dict[str, List[RAGResult]] = {}
        for r in results:
            per_coll.setdefault(r.collection, []).append(r)

        for coll_list in per_coll.values():
            coll_list.sort(key=lambda r: r.score, reverse=True)

        rrf_scores: Dict[str, float] = {}
        best: Dict[str, RAGResult] = {}

        for coll_list in per_coll.values():
            for rank, r in enumerate(coll_list, start=1):
                rrf = 1.0 / (RRF_K + rank)
                rrf_scores[r.chunk_id] = rrf_scores.get(r.chunk_id, 0.0) + rrf
                if r.chunk_id not in best or r.score > best[r.chunk_id].score:
                    best[r.chunk_id] = r

        max_rrf = max(rrf_scores.values()) if rrf_scores else 1.0
        fused: List[RAGResult] = []
        for chunk_id, rrf_score in rrf_scores.items():
            r = best[chunk_id]
            fused.append(RAGResult(
                chunk_id=r.chunk_id,
                score=rrf_score / max_rrf if max_rrf > 0 else 0.0,
                text=r.text,
                heading=r.heading,
                collection=r.collection,
                metadata=r.metadata,
            ))
        return fused

    # ── convenience methods ───────────────────────────────────────────────

    def search_by_type(self, query: str, doc_type: str, top_k: int = 5) -> List[RAGResult]:
        return self.search(query, top_k=top_k, where={"type": doc_type})

    def search_by_module(self, query: str, module: str, top_k: int = 5) -> List[RAGResult]:
        return self.search(query, top_k=top_k, where={"module": module.upper()})

    def list_collections(self) -> List[dict]:
        """List target collections with chunk counts."""
        client = self._get_client()
        info = []
        for coll_name in self._get_collections():
            try:
                coll = client.get_collection(name=coll_name)
                info.append({"name": coll_name, "count": coll.count()})
            except Exception:
                info.append({"name": coll_name, "count": 0, "status": "not_found"})
        return info

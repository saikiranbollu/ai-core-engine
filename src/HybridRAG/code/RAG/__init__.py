"""
RAG Package — Unified exports.

Unified, profile-agnostic modules:
  - ``UnifiedRAGQuerier``     — semantic vector search for any profile
  - ``HybridRAGOrchestrator`` — combined KG + RAG search for any profile
  - ``RAGResult``             — search result dataclass

Collection naming:
  - ``module_collections``    — ontology-driven collection names
  - ``collection_name``       — single collection name builder
"""
from RAG.rag_query_unified import UnifiedRAGQuerier, RAGResult           # noqa: F401
from RAG.hybrid_rag_unified import HybridRAGOrchestrator, HybridResult   # noqa: F401
from RAG.collection_naming_unified import (                               # noqa: F401
    module_collections,
    collection_name,
)

__all__ = [
    "UnifiedRAGQuerier",
    "RAGResult",
    "HybridRAGOrchestrator",
    "HybridResult",
    "module_collections",
    "collection_name",
]

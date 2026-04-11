"""
Ephemeral Sandbox — Sprint 3 (P1 Feature)
==========================================
Temporary KG (NetworkX) + Vector (in-memory) for document exploration.
Third storage tier: Working Memory → Ephemeral Sandbox → Semantic Memory.

Self-contained: no dependency on OntologyLoader or sentence-transformers.
Falls back to hash-based embeddings for dev; real embedder pluggable.

Components:
  EphemeralGraph   — NetworkX in-memory knowledge graph with keyword index
  EphemeralVectors — In-memory vector store with fallback embedder
  SandboxManager   — Lifecycle tied to sessions (auto-cleanup on session_end)
  SandboxIngester  — File parser dispatch → graph + vector population
  SandboxQuerier   — Unified query interface for GraphRAG integration
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
#  Data Classes
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class Chunk:
    """A text chunk ready for embedding."""
    text: str
    chunk_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_file: str = ""
    section: str = ""

    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = f"chunk_{uuid.uuid4().hex[:12]}"


@dataclass
class SearchResult:
    """Unified search result from graph or vector search."""
    node_id: str
    content: str
    score: float
    node_type: str = ""
    origin: str = "ephemeral"  # eph_graph | eph_vector
    metadata: Dict[str, Any] = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════════════
#  Fallback Embedder (hash-based, for dev without sentence-transformers)
# ═════════════════════════════════════════════════════════════════════════

class _FallbackEmbedder:
    """Deterministic hash-based embedder. NOT for production."""
    def __init__(self, dim: int = 384):
        self._dim = dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        results = []
        for text in texts:
            h = hashlib.sha384(text.encode()).digest()
            vec = [float(b) / 255.0 for b in h[:self._dim]]
            norm = sum(v * v for v in vec) ** 0.5
            vec = [v / norm for v in vec] if norm > 0 else vec
            results.append(vec)
        return results


# ═════════════════════════════════════════════════════════════════════════
#  EphemeralGraph — NetworkX in-memory knowledge graph
# ═════════════════════════════════════════════════════════════════════════

class EphemeralGraph:
    """In-memory knowledge graph using NetworkX with keyword index."""

    def __init__(self):
        try:
            import networkx as nx
            self._nx = nx
        except ImportError:
            raise ImportError("networkx required: pip install networkx")
        self._graph = nx.DiGraph()
        self._keyword_index: Dict[str, set] = {}

    def add_node(self, node_type: str, node_id: str, props: Optional[Dict] = None):
        props = props or {}
        props["_node_type"] = node_type
        props["_node_id"] = node_id
        self._graph.add_node(node_id, **props)
        self._index_node(node_id, node_type, props)

    def add_relationship(self, source: str, target: str, rel_type: str, props: Optional[Dict] = None):
        for nid in (source, target):
            if nid not in self._graph:
                self._graph.add_node(nid, _node_type="Unknown", _node_id=nid)
        p = props or {}
        p["_rel_type"] = rel_type
        self._graph.add_edge(source, target, **p)

    def add_nodes_bulk(self, nodes: List[Dict]) -> int:
        count = 0
        for n in nodes:
            nt, nid = n.get("node_type", "Unknown"), n.get("node_id", "")
            if nid:
                props = {k: v for k, v in n.items() if k not in ("node_type", "node_id")}
                self.add_node(nt, nid, props)
                count += 1
        return count

    def add_relationships_bulk(self, rels: List[Dict]) -> int:
        count = 0
        for r in rels:
            src, tgt, rt = r.get("source", ""), r.get("target", ""), r.get("rel_type", "RELATED_TO")
            if src and tgt:
                props = {k: v for k, v in r.items() if k not in ("source", "target", "rel_type")}
                self.add_relationship(src, tgt, rt, props)
                count += 1
        return count

    def _index_node(self, node_id: str, node_type: str, props: Dict):
        tokens = set()
        tokens.add(node_id.lower())
        tokens.add(node_type.lower())
        for part in re.split(r'[_\s]+', node_id):
            tokens.add(part.lower())
            for c in re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', part):
                tokens.add(c.lower())
        for key in ("function_name", "name", "description", "register_name",
                     "short_name", "requirement_id", "test_id", "module"):
            val = props.get(key)
            if isinstance(val, str):
                for word in re.split(r'[\s_,;]+', val):
                    if len(word) > 2:
                        tokens.add(word.lower())
        for token in tokens:
            if token:
                self._keyword_index.setdefault(token, set()).add(node_id)

    def keyword_search(self, keywords: List[str], node_types: Optional[List[str]] = None,
                       top_k: int = 20) -> List[SearchResult]:
        scores: Dict[str, int] = {}
        for kw in keywords:
            for nid in self._keyword_index.get(kw.lower(), set()):
                scores[nid] = scores.get(nid, 0) + 1
        results = []
        for nid, hit_count in sorted(scores.items(), key=lambda x: -x[1])[:top_k * 2]:
            data = self._graph.nodes.get(nid, {})
            nt = data.get("_node_type", "Unknown")
            if node_types and nt not in node_types:
                continue
            content = " | ".join(f"{k}: {v}" for k, v in data.items() if not k.startswith("_") and v)
            results.append(SearchResult(
                node_id=nid, content=content[:500], score=hit_count / max(len(keywords), 1),
                node_type=nt, origin="eph_graph", metadata={"hit_count": hit_count},
            ))
        return results[:top_k]

    def get_traceability(self, node_ids: List[str]) -> List[Dict]:
        TRACE_RELS = {"SWA_TRACES_TO", "SWUD_REALIZES_SWA", "TS_VERIFIES",
                      "TRACES_TO", "VERIFIES", "REALIZES", "IMPLEMENTS"}
        chain = []
        for nid in node_ids:
            if nid not in self._graph:
                continue
            for _, tgt, edata in self._graph.out_edges(nid, data=True):
                rt = edata.get("_rel_type", "")
                if rt in TRACE_RELS:
                    chain.append({"from": nid, "to": tgt, "relationship": rt})
        return chain

    def stats(self) -> Dict[str, Any]:
        type_counts: Dict[str, int] = {}
        for _, data in self._graph.nodes(data=True):
            nt = data.get("_node_type", "Unknown")
            type_counts[nt] = type_counts.get(nt, 0) + 1
        return {"total_nodes": self._graph.number_of_nodes(),
                "total_edges": self._graph.number_of_edges(),
                "node_types": type_counts, "keyword_index_size": len(self._keyword_index)}

    def clear(self) -> Dict[str, int]:
        stats = {"nodes_removed": self._graph.number_of_nodes(),
                 "edges_removed": self._graph.number_of_edges()}
        self._graph.clear()
        self._keyword_index.clear()
        return stats

    @property
    def node_count(self): return self._graph.number_of_nodes()
    @property
    def edge_count(self): return self._graph.number_of_edges()


# ═════════════════════════════════════════════════════════════════════════
#  EphemeralVectors — In-memory vector store
# ═════════════════════════════════════════════════════════════════════════

class EphemeralVectors:
    """In-memory vector store. Uses cosine similarity on raw embeddings."""

    def __init__(self, session_id: str, embedder=None, max_chunks: int = 5000):
        self._session_id = session_id
        self._embedder = embedder or _FallbackEmbedder()
        self._max_chunks = max_chunks
        self._chunks: List[Dict] = []  # {id, text, embedding, metadata}

    def add_chunks(self, chunks: List[Chunk]) -> int:
        if not chunks:
            return 0
        if len(self._chunks) + len(chunks) > self._max_chunks:
            raise ValueError(f"Would exceed sandbox limit ({len(self._chunks)}/{self._max_chunks})")
        texts = [c.text for c in chunks]
        embeddings = self._embedder.embed(texts)
        for chunk, emb in zip(chunks, embeddings):
            self._chunks.append({
                "id": chunk.chunk_id, "text": chunk.text, "embedding": emb,
                "metadata": {**chunk.metadata, "source_file": chunk.source_file, "section": chunk.section},
            })
        return len(chunks)

    def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        if not self._chunks:
            return []
        q_emb = self._embedder.embed([query])[0]
        scored = []
        for chunk in self._chunks:
            sim = self._cosine_sim(q_emb, chunk["embedding"])
            scored.append((sim, chunk))
        scored.sort(key=lambda x: -x[0])
        results = []
        for sim, chunk in scored[:top_k]:
            results.append(SearchResult(
                node_id=chunk["id"], content=chunk["text"][:500], score=max(0, sim),
                origin="eph_vector",
                metadata={**chunk["metadata"], "_session": self._session_id},
            ))
        return results

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0

    def stats(self) -> Dict[str, Any]:
        return {"chunk_count": len(self._chunks), "max_chunks": self._max_chunks,
                "session_id": self._session_id}

    def clear(self) -> Dict[str, int]:
        n = len(self._chunks)
        self._chunks.clear()
        return {"chunks_removed": n}


# ═════════════════════════════════════════════════════════════════════════
#  EphemeralSandbox Container + SandboxManager
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class EphemeralSandbox:
    """Container for one session's ephemeral stores."""
    session_id: str
    graph: EphemeralGraph
    vectors: EphemeralVectors
    files_ingested: List[Dict[str, Any]] = field(default_factory=list)
    active: bool = True

    def status(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "active": self.active,
                "files": [f.get("filename", "?") for f in self.files_ingested],
                "file_count": len(self.files_ingested),
                "graph_stats": self.graph.stats(), "vector_stats": self.vectors.stats()}

    def clear(self) -> Dict[str, Any]:
        gs = self.graph.clear()
        vs = self.vectors.clear()
        fc = len(self.files_ingested)
        self.files_ingested.clear()
        self.active = False
        return {"graph": gs, "vectors": vs, "files_cleared": fc}


class SandboxManager:
    """Lifecycle manager — one sandbox per session, auto-cleanup."""

    def __init__(self, embedder=None, max_chunks: int = 5000):
        self._embedder = embedder
        self._max_chunks = max_chunks
        self._sandboxes: Dict[str, EphemeralSandbox] = {}
        self._lock = threading.Lock()

    def create_sandbox(self, session_id: str) -> EphemeralSandbox:
        with self._lock:
            if session_id in self._sandboxes and self._sandboxes[session_id].active:
                return self._sandboxes[session_id]
            sb = EphemeralSandbox(
                session_id=session_id,
                graph=EphemeralGraph(),
                vectors=EphemeralVectors(session_id, self._embedder, self._max_chunks),
            )
            self._sandboxes[session_id] = sb
            logger.info("[SandboxManager] Created sandbox for %s", session_id)
            return sb

    def get_sandbox(self, session_id: str) -> Optional[EphemeralSandbox]:
        with self._lock:
            sb = self._sandboxes.get(session_id)
            return sb if sb and sb.active else None

    def destroy_sandbox(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sb = self._sandboxes.pop(session_id, None)
            if not sb:
                return {"cleared": False, "reason": "no sandbox found"}
            stats = sb.clear()
            return {"cleared": True, "session_id": session_id, **stats}

    def get_status(self, session_id: str) -> Dict[str, Any]:
        sb = self.get_sandbox(session_id)
        if not sb:
            return {"active": False, "session_id": session_id, "files": [], "file_count": 0}
        return sb.status()

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sandboxes.values() if s.active)


# ═════════════════════════════════════════════════════════════════════════
#  SandboxIngester — Parse files into graph + vectors
# ═════════════════════════════════════════════════════════════════════════

class SandboxIngester:
    """Dispatch file parsing → populate sandbox graph + vectors."""

    SUPPORTED_EXTENSIONS = {".c", ".h", ".txt", ".md", ".rst", ".csv", ".json", ".pdf", ".xlsx", ".puml", ".arxml"}

    def __init__(self, sandbox: EphemeralSandbox, chunk_size: int = 500):
        self._sandbox = sandbox
        self._chunk_size = chunk_size

    def ingest_files(self, file_paths: List[str]) -> Dict[str, Any]:
        stats = {"files_processed": 0, "nodes_created": 0, "edges_created": 0,
                 "chunks_embedded": 0, "errors": []}
        for fp in file_paths:
            try:
                result = self._ingest_one(fp)
                stats["files_processed"] += 1
                stats["nodes_created"] += result.get("nodes", 0)
                stats["edges_created"] += result.get("edges", 0)
                stats["chunks_embedded"] += result.get("chunks", 0)
                self._sandbox.files_ingested.append({"filename": Path(fp).name, "path": fp, **result})
            except Exception as e:
                stats["errors"].append({"file": fp, "error": str(e)})
                logger.error("[SandboxIngester] Failed %s: %s", fp, e)
        return stats

    def ingest_documents(self, documents: List[Dict[str, str]]) -> Dict[str, Any]:
        """Ingest documents provided as text content (no filesystem access needed).

        Parameters
        ----------
        documents : list of dict
            Each dict must have ``filename`` (str) and ``content`` (str).
        """
        stats = {"files_processed": 0, "nodes_created": 0, "edges_created": 0,
                 "chunks_embedded": 0, "errors": []}
        for doc in documents:
            filename = doc.get("filename", "untitled.txt")
            content = doc.get("content", "")
            if not content:
                stats["errors"].append({"file": filename, "error": "Empty content"})
                continue
            try:
                result = self._ingest_content(filename, content)
                stats["files_processed"] += 1
                stats["nodes_created"] += result.get("nodes", 0)
                stats["edges_created"] += result.get("edges", 0)
                stats["chunks_embedded"] += result.get("chunks", 0)
                self._sandbox.files_ingested.append({"filename": filename, "source": "text_upload", **result})
            except Exception as e:
                stats["errors"].append({"file": filename, "error": str(e)})
                logger.error("[SandboxIngester] Failed %s: %s", filename, e)
        return stats

    def _ingest_one(self, file_path: str) -> Dict[str, Any]:
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        ext = p.suffix.lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}")

        # Read file
        if ext in (".pdf", ".xlsx"):
            content = f"[Binary file: {p.name} — full parsing requires specialized parser]"
        else:
            content = p.read_text(encoding="utf-8", errors="replace")

        # Create graph nodes
        file_node_id = f"file:{p.name}"
        self._sandbox.graph.add_node("SourceFile", file_node_id, {
            "name": p.name, "path": str(p), "extension": ext, "size_bytes": p.stat().st_size,
        })

        nodes_created = 1
        edges_created = 0

        # Parse C/H files into function nodes
        if ext in (".c", ".h"):
            nodes, edges = self._parse_c_header(content, p.name)
            nodes_created += self._sandbox.graph.add_nodes_bulk(nodes)
            edges_created += self._sandbox.graph.add_relationships_bulk(edges)

        # Chunk content for vectors
        chunks = self._chunk_text(content, p.name)
        chunks_embedded = self._sandbox.vectors.add_chunks(chunks)

        return {"nodes": nodes_created, "edges": edges_created, "chunks": chunks_embedded}

    def _ingest_content(self, filename: str, content: str) -> Dict[str, Any]:
        """Ingest raw text content under a given filename."""
        ext = Path(filename).suffix.lower() if "." in filename else ".txt"

        file_node_id = f"file:{filename}"
        self._sandbox.graph.add_node("SourceFile", file_node_id, {
            "name": filename, "path": "(uploaded text)", "extension": ext,
            "size_bytes": len(content.encode("utf-8")),
        })

        nodes_created = 1
        edges_created = 0

        if ext in (".c", ".h"):
            nodes, edges = self._parse_c_header(content, filename)
            nodes_created += self._sandbox.graph.add_nodes_bulk(nodes)
            edges_created += self._sandbox.graph.add_relationships_bulk(edges)

        chunks = self._chunk_text(content, filename)
        chunks_embedded = self._sandbox.vectors.add_chunks(chunks)

        return {"nodes": nodes_created, "edges": edges_created, "chunks": chunks_embedded}

    def _parse_c_header(self, content: str, filename: str) -> tuple:
        """Extract function declarations from C/H files."""
        nodes, edges = [], []
        # Match function declarations: return_type function_name(params)
        fn_pattern = re.compile(
            r'(?:IFX_EXTERN\s+|IFX_INLINE\s+)?(\w[\w\s\*]+?)\s+(\w+)\s*\(([^)]*)\)\s*[;{]',
            re.MULTILINE
        )
        for m in fn_pattern.finditer(content):
            ret_type, fn_name, params = m.group(1).strip(), m.group(2), m.group(3).strip()
            if fn_name.startswith("_") or len(fn_name) < 3:
                continue
            nodes.append({
                "node_type": "APIFunction", "node_id": fn_name,
                "function_name": fn_name, "return_type": ret_type,
                "parameters": params, "source_file": filename,
            })
            edges.append({"source": fn_name, "target": f"file:{filename}", "rel_type": "DEFINED_IN"})
        return nodes, edges

    def _chunk_text(self, content: str, filename: str) -> List[Chunk]:
        chunks = []
        lines = content.split("\n")
        buf, section = [], ""
        for line in lines:
            if re.match(r'^(#{1,3}|/\*\*|\*\s+@|//\s*={3,})', line):
                if buf:
                    chunks.append(Chunk(
                        text="\n".join(buf), source_file=filename, section=section,
                    ))
                    buf = []
                section = line.strip()[:80]
            buf.append(line)
            if len("\n".join(buf)) > self._chunk_size:
                chunks.append(Chunk(text="\n".join(buf), source_file=filename, section=section))
                buf = []
        if buf:
            chunks.append(Chunk(text="\n".join(buf), source_file=filename, section=section))
        return chunks


# ═════════════════════════════════════════════════════════════════════════
#  SandboxQuerier — Unified query interface
# ═════════════════════════════════════════════════════════════════════════

class SandboxQuerier:
    """Query ephemeral graph + vectors, return unified results."""

    def __init__(self, sandbox: EphemeralSandbox, boost: float = 0.05):
        self._sandbox = sandbox
        self._boost = boost

    def search(self, query: str, top_k: int = 15, alpha: float = 0.5) -> List[SearchResult]:
        """Combined graph keyword + vector semantic search."""
        graph_results = []
        vector_results = []

        if alpha > 0:
            keywords = re.split(r'[\s_,]+', query)
            keywords = [k for k in keywords if len(k) > 2]
            graph_results = self._sandbox.graph.keyword_search(keywords, top_k=top_k)
            for r in graph_results:
                r.score = r.score * alpha + self._boost

        if alpha < 1.0:
            vector_results = self._sandbox.vectors.search(query, top_k=top_k)
            for r in vector_results:
                r.score = r.score * (1 - alpha) + self._boost

        # Merge by node_id, keep highest score
        seen = {}
        for r in graph_results + vector_results:
            if r.node_id not in seen or r.score > seen[r.node_id].score:
                seen[r.node_id] = r
        merged = sorted(seen.values(), key=lambda x: -x.score)
        return merged[:top_k]

    def get_traceability(self, node_ids: List[str]) -> List[Dict]:
        return self._sandbox.graph.get_traceability(node_ids)

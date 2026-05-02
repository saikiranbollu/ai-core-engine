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
            # H18 fix: Expand hash to fill full dim (384) by repeated hashing
            vec = []
            seed = text.encode()
            while len(vec) < self._dim:
                h = hashlib.sha384(seed).digest()
                vec.extend(float(b) / 255.0 for b in h)
                seed = h  # chain hashes
            vec = vec[:self._dim]
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

    # ── Phase 8 (Plan 2): Enhanced accessors for graph overlay ────────────

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Return properties of a single node, or None if not found."""
        if node_id not in self._graph:
            return None
        return dict(self._graph.nodes[node_id])

    def update_node(self, node_id: str, properties: Dict[str, Any]):
        """Update existing node properties (preserves edges)."""
        if node_id not in self._graph:
            raise KeyError(f"Node {node_id} not found")
        self._graph.nodes[node_id].update(properties)
        nt = properties.get("_node_type", self._graph.nodes[node_id].get("_node_type", "Unknown"))
        self._index_node(node_id, nt, dict(self._graph.nodes[node_id]))

    def get_edge(self, source: str, target: str, rel_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return edge data between two nodes, or None if not found."""
        data = self._graph.get_edge_data(source, target)
        if data and (rel_type is None or data.get("_rel_type") == rel_type):
            return dict(data)
        return None

    def update_edge(self, source: str, target: str, rel_type: str, properties: Dict[str, Any]):
        """Update an existing edge's properties."""
        if self._graph.has_edge(source, target):
            self._graph.edges[source, target].update(properties)

    def get_all_nodes(self):
        """Return iterator of (node_id, data_dict) for all nodes."""
        return self._graph.nodes(data=True)

    def get_nodes_by_origin(self, origin: str) -> List[tuple]:
        """Return [(node_id, data)] filtered by _origin metadata."""
        return [(nid, dict(data)) for nid, data in self._graph.nodes(data=True)
                if data.get("_origin") == origin]

    def get_boundary_nodes(self) -> List[Dict[str, Any]]:
        """Return Unknown-type nodes suitable for cross-module resolution.

        Filters out ALL_CAPS names (macros/HW register accesses) that
        won't have corresponding nodes in the production KG.
        """
        boundary = []
        for nid, data in self._graph.nodes(data=True):
            if data.get("_node_type") != "Unknown":
                continue
            # Extract name from canonical ID: "SRC_Function:Dma_ChUpdate:Adc"
            parts = nid.split(":")
            name = parts[1] if len(parts) >= 2 else nid
            # Skip ALL_CAPS names (macros/HW register accesses)
            if re.match(r'^[A-Z][A-Z0-9_]+$', name):
                continue
            boundary.append({"node_id": nid, "name": name})
        return boundary

    def resolve_boundary(self, name_to_resolved: Dict[str, str]) -> Dict[str, int]:
        """Rewire edges from Unknown stubs to resolved production nodes.

        For each name that was resolved against prod, find the Unknown stub
        in the sandbox, redirect all its edges to the resolved prod node,
        and remove the stub.

        Parameters
        ----------
        name_to_resolved : dict
            Maps function name -> canonical ID of the resolved prod node.

        Returns
        -------
        dict with {"boundary_resolved": int}
        """
        resolved_count = 0
        for name, resolved_id in name_to_resolved.items():
            if resolved_id not in self._graph:
                continue
            # Find Unknown stubs matching this name
            stubs = []
            for nid, data in list(self._graph.nodes(data=True)):
                if data.get("_node_type") != "Unknown":
                    continue
                parts = nid.split(":")
                stub_name = parts[1] if len(parts) >= 2 else nid
                if stub_name == name and nid != resolved_id:
                    stubs.append(nid)
            for stub_id in stubs:
                # Redirect incoming edges
                for pred in list(self._graph.predecessors(stub_id)):
                    edata = dict(self._graph.edges[pred, stub_id])
                    if not self._graph.has_edge(pred, resolved_id):
                        self._graph.add_edge(pred, resolved_id, **edata)
                # Redirect outgoing edges
                for succ in list(self._graph.successors(stub_id)):
                    edata = dict(self._graph.edges[stub_id, succ])
                    if not self._graph.has_edge(resolved_id, succ):
                        self._graph.add_edge(resolved_id, succ, **edata)
                # Remove stub from graph and keyword index
                self._graph.remove_node(stub_id)
                for token_set in self._keyword_index.values():
                    token_set.discard(stub_id)
                resolved_count += 1
        return {"boundary_resolved": resolved_count}

    def load_prod_nodes(self, nodes: List[Dict], relationships: List[Dict]):
        """Load production traceability neighbors into ephemeral graph."""
        id_map = {}  # neo4j elementId → canonical node_id

        for node in nodes:
            canonical = self._canonical_id(node["node_type"], node["properties"])
            id_map[node["node_id"]] = canonical

            props = {**node["properties"],
                     "_origin": "production",
                     "_neo4j_id": node["node_id"]}
            self.add_node(node["node_type"], canonical, props)

        for rel in relationships:
            src = id_map.get(rel["source"])
            tgt = id_map.get(rel["target"])
            if src and tgt:
                self.add_relationship(src, tgt, rel["rel_type"],
                                      {**rel.get("properties", {}),
                                       "_origin": "production"})

    @staticmethod
    def _canonical_id(node_type: str, props: Dict) -> str:
        """Consistent identity matching prod KG naming."""
        name = (props.get("name") or props.get("function_name")
                or props.get("param_name") or props.get("requirement_id")
                or props.get("test_case_id") or str(props.get("_neo4j_id", "")))
        module = props.get("module", "unknown")
        return f"{node_type}:{name}:{module}"

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

    def remove_by_metadata(self, **filters):
        """Remove chunks matching all filter criteria."""
        self._chunks = [
            c for c in self._chunks
            if not all(c["metadata"].get(k) == v for k, v in filters.items())
        ]


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
#  SandboxParserDispatcher — Plan 2 Phase 1
#  Routes files to IngestionPipeline parsers without IngestionService
# ═════════════════════════════════════════════════════════════════════════

class SandboxParserDispatcher:
    """Route files by extension to real IngestionPipeline parsers.

    Returns the standardized dict format used by IngestionService._parse_file():
      {"type": str, "functions": [...], "pages": [...], "file": str, ...}
    """

    SUPPORTED_EXTENSIONS = {
        ".c", ".h", ".txt", ".md", ".rst", ".csv", ".json",
        ".pdf", ".xlsx", ".puml", ".arxml",
    }
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    def parse(self, file_path: Path) -> Dict[str, Any]:
        """Parse a file and return standardized dict."""
        p = Path(file_path) if not isinstance(file_path, Path) else file_path
        ext = p.suffix.lower()
        fname_lower = p.name.lower()

        if ext in (".c", ".h"):
            return self._parse_c(p, ext, fname_lower)
        elif ext == ".pdf":
            return self._parse_pdf(p)
        elif ext == ".xlsx":
            return self._parse_xlsx(p, fname_lower)
        elif ext == ".arxml":
            return self._parse_arxml(p)
        elif ext == ".puml":
            return self._parse_puml(p)
        elif ext == ".rst":
            return self._parse_rst(p)
        elif ext == ".json":
            return self._parse_json(p)
        elif ext in (".txt", ".md", ".csv"):
            return self._parse_text(p, ext)
        else:
            return {"type": "generic", "content": "", "file": str(p)}

    def _parse_c(self, p: Path, ext: str, fname_lower: str) -> Dict:
        try:
            from src.IngestionPipeline.Parsers.c_parser import parse as c_parse
            parsed = c_parse(str(p))
            if isinstance(parsed, dict):
                parsed.setdefault("type", "c_source" if ext == ".c" else "c_header")
                parsed.setdefault("file", str(p))
                # Ensure raw content is available for vector chunking
                if "content" not in parsed:
                    parsed["content"] = p.read_text(encoding="utf-8", errors="replace")
            return parsed
        except ImportError:
            # Fallback to basic regex extraction
            content = p.read_text(encoding="utf-8", errors="replace")
            return {"type": "c_source" if ext == ".c" else "c_header",
                    "functions": [], "content": content, "file": str(p)}

    def _parse_pdf(self, p: Path) -> Dict:
        try:
            from src.IngestionPipeline.Parsers.pdf_parser import parse as pdf_parse
            pages = pdf_parse(str(p))
            return {"type": "pdf", "pages": pages, "file": str(p)}
        except ImportError:
            return {"type": "pdf", "pages": [], "file": str(p)}

    def _parse_xlsx(self, p: Path, fname_lower: str) -> Dict:
        if "_ts_" in fname_lower or "testspec" in fname_lower:
            try:
                from src.IngestionPipeline.Parsers.testspec_parsers import parse_testspec_workbook
                nodes = parse_testspec_workbook(str(p))
                return {"type": "testspec", "nodes": nodes, "file": str(p)}
            except (ImportError, Exception):
                pass
        try:
            from src.IngestionPipeline.Parsers.xlsx_parser import parse as xlsx_parse
            sheets = xlsx_parse(str(p))
            return {"type": "xlsx", "sheets": sheets, "file": str(p)}
        except ImportError:
            return {"type": "xlsx", "sheets": {}, "file": str(p)}

    def _parse_arxml(self, p: Path) -> Dict:
        try:
            from src.IngestionPipeline.Parsers.arxml_parser import parse as arxml_parse
            return arxml_parse(str(p))
        except ImportError:
            return {"type": "arxml", "modules": [], "file": str(p)}

    def _parse_puml(self, p: Path) -> Dict:
        try:
            from src.IngestionPipeline.Parsers.puml_parser import parse as puml_parse
            return puml_parse(str(p))
        except ImportError:
            return {"type": "puml", "functions": [], "file": str(p)}

    def _parse_rst(self, p: Path) -> Dict:
        try:
            from src.IngestionPipeline.Parsers.rst_parser import parse as rst_parse
            sections = rst_parse(str(p))
            return {"type": "rst", "sections": sections, "file": str(p)}
        except ImportError:
            content = p.read_text(encoding="utf-8", errors="replace")
            return {"type": "rst", "content": content, "file": str(p)}

    def _parse_json(self, p: Path) -> Dict:
        import json as _json
        content = p.read_text(encoding="utf-8", errors="replace")
        try:
            data = _json.loads(content)
        except _json.JSONDecodeError:
            data = {}
        return {"type": "json", "data": data, "file": str(p)}

    def _parse_text(self, p: Path, ext: str) -> Dict:
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"type": "text", "content": content, "file": str(p)}


# ═════════════════════════════════════════════════════════════════════════
#  SandboxAdapter — Plan 2 Phase 1 / Phase 5
#  Transforms parser output → EphemeralGraph nodes + EphemeralVectors chunks
#  Implements shadow/override logic for prod node conflicts (Phase 5)
# ═════════════════════════════════════════════════════════════════════════

class SandboxAdapter:
    """Transform parser output into EphemeralGraph nodes/edges + EphemeralVectors chunks.

    When a parsed node conflicts with a production node already in the graph,
    the sandbox version *shadows* it (preserving the original properties).
    """

    def __init__(self, chunk_size: int = 500):
        self._chunk_size = chunk_size

    def _extract_node_names_from_parsed(self, parsed: Dict) -> List[str]:
        """Extract node names from parsed data WITHOUT ingesting into sandbox.
        
        Used by sandbox_upload to collect names for traceability pull before
        ingesting (Plan 2 Phase 5: correct load order).
        """
        ptype = parsed.get("type", "")
        node_names = []
        
        if ptype in ("c_source", "c_header"):
            for fn in self._iter_c_functions(parsed):
                name = fn.get("name")
                if name:
                    node_names.append(name)
        elif ptype == "json":
            data = parsed.get("data", {})
            reqs = data.get("requirements") if isinstance(data, dict) else None
            if isinstance(reqs, list):
                for req in reqs:
                    if isinstance(req, dict):
                        req_id = req.get("requirement_id") or req.get("id") or req.get("document_key")
                        if req_id:
                            node_names.append(req_id)
        elif ptype == "testspec":
            for tc in parsed.get("nodes", []):
                if isinstance(tc, dict):
                    tc_id = tc.get("test_case_id") or tc.get("id") or tc.get("name")
                    if tc_id:
                        node_names.append(tc_id)
        
        return node_names

    def ingest_parsed(self, sandbox: 'EphemeralSandbox', parsed: Dict, filename: str,
                      module: Optional[str] = None):
        """Convert one parser result dict into sandbox graph nodes + vector chunks."""
        ptype = parsed.get("type", "")
        node_names = []

        if ptype in ("c_source", "c_header"):
            node_names = self._ingest_c(sandbox, parsed, filename, module)
        elif ptype == "json":
            node_names = self._ingest_json(sandbox, parsed, filename, module)
        elif ptype in ("pdf", "text", "rst", "generic"):
            self._ingest_document(sandbox, parsed, filename, module)
        elif ptype == "xlsx":
            self._ingest_xlsx(sandbox, parsed, filename, module)
        elif ptype == "testspec":
            node_names = self._ingest_testspec(sandbox, parsed, filename, module)
        elif ptype == "arxml":
            self._ingest_arxml(sandbox, parsed, filename, module)

        # Always create vector chunks for textual content
        content = self._extract_text_content(parsed)
        if content:
            chunks = self._chunk_text(content, filename)
            # Remove existing chunks for the same source file (vector shadow)
            sandbox.vectors.remove_by_metadata(source_file=filename)
            sandbox.vectors.add_chunks(chunks)

        sandbox.files_ingested.append({
            "filename": filename, "source": "parser_dispatch",
            "type": ptype, "node_names_extracted": len(node_names),
        })
        return node_names

    def _ingest_c(self, sandbox, parsed, filename, module) -> List[str]:
        """Ingest C/H parsed functions into graph."""
        node_names = []
        mod = module or "unknown"
        for fn in self._iter_c_functions(parsed):
            name = fn.get("name")
            if not name:
                continue
            node_type = "SRC_Function"
            node_id = EphemeralGraph._canonical_id(node_type, {"name": name, "module": mod})
            props = {
                "name": name,
                "module": mod,
                "return_type": fn.get("return_type", ""),
                "parameters": fn.get("parameters", ""),
                "source_file": filename,
                "function_name": name,
            }
            self._add_node_with_shadow(sandbox, node_type, node_id, props)
            node_names.append(name)

            # Internal call relationships
            for call_entry in fn.get("internal_calls", []):
                # Handle both formats: dict {"function": name, ...} / switch-case or plain string
                if isinstance(call_entry, dict):
                    if "function" in call_entry:
                        callee = call_entry["function"]
                    elif "calls" in call_entry:  # switch-case format
                        for sub in call_entry.get("calls", []):
                            callee_name = sub.get("function") if isinstance(sub, dict) else sub
                            if callee_name and callee_name != name:
                                cid = EphemeralGraph._canonical_id("SRC_Function", {"name": callee_name, "module": mod})
                                sandbox.graph.add_relationship(node_id, cid, "CALLS_INTERNALLY",
                                                               {"_origin": "sandbox"})
                        continue
                    else:
                        continue
                else:
                    callee = str(call_entry)
                if callee and callee != name:
                    callee_id = EphemeralGraph._canonical_id("SRC_Function", {"name": callee, "module": mod})
                    sandbox.graph.add_relationship(node_id, callee_id, "CALLS_INTERNALLY",
                                                   {"_origin": "sandbox"})

        return node_names

    def _ingest_json(self, sandbox, parsed, filename, module) -> List[str]:
        """Ingest JSON (requirements or data)."""
        node_names = []
        mod = module or "unknown"
        data = parsed.get("data", {})
        reqs = data.get("requirements") if isinstance(data, dict) else None
        if isinstance(reqs, list):
            for req in reqs:
                if not isinstance(req, dict):
                    continue
                req_id = req.get("requirement_id") or req.get("id") or req.get("document_key")
                if not req_id:
                    continue
                node_type = "SoftwareRequirement"
                node_id = EphemeralGraph._canonical_id(node_type, {"requirement_id": req_id, "module": mod})
                props = {
                    "requirement_id": req_id,
                    "name": req.get("name") or req_id,
                    "description": req.get("text") or req.get("description") or "",
                    "module": mod,
                    "source_file": filename,
                }
                self._add_node_with_shadow(sandbox, node_type, node_id, props)
                node_names.append(req_id)
        return node_names

    def _ingest_document(self, sandbox, parsed, filename, module):
        """Ingest PDF/text/rst document."""
        mod = module or "unknown"
        node_type = "Document"
        node_id = EphemeralGraph._canonical_id(node_type, {"name": filename, "module": mod})
        content = ""
        ptype = parsed.get("type", "")
        if ptype == "pdf":
            content = "\n".join(parsed.get("pages", []))[:50000]
        elif ptype == "rst":
            import json as _json
            content = _json.dumps(parsed.get("sections", []))[:50000]
        else:
            content = (parsed.get("content") or "")[:50000]
        props = {"name": filename, "module": mod, "content": content,
                 "doc_type": ptype, "source_file": filename}
        self._add_node_with_shadow(sandbox, node_type, node_id, props)

    def _ingest_xlsx(self, sandbox, parsed, filename, module):
        """Ingest XLSX sheets."""
        mod = module or "unknown"
        for sheet_name, rows in (parsed.get("sheets") or {}).items():
            node_type = "Sheet"
            node_id = EphemeralGraph._canonical_id(node_type, {"name": sheet_name, "module": mod})
            props = {"name": sheet_name, "source_file": filename,
                     "module": mod, "row_count": len(rows)}
            self._add_node_with_shadow(sandbox, node_type, node_id, props)

    def _ingest_testspec(self, sandbox, parsed, filename, module) -> List[str]:
        """Ingest test specification nodes."""
        node_names = []
        mod = module or "unknown"
        for tc in parsed.get("nodes", []):
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("test_case_id") or tc.get("id") or tc.get("name")
            if not tc_id:
                continue
            node_type = "TS_FunctionalTestCase"
            node_id = EphemeralGraph._canonical_id(node_type, {"test_case_id": tc_id, "module": mod})
            props = {**tc, "module": mod, "source_file": filename, "test_case_id": tc_id}
            self._add_node_with_shadow(sandbox, node_type, node_id, props)
            node_names.append(tc_id)
        return node_names

    def _ingest_arxml(self, sandbox, parsed, filename, module):
        """Ingest ARXML modules."""
        mod = module or "unknown"
        for m in parsed.get("modules", []):
            mod_name = m.get("name", "")
            if not mod_name:
                continue
            node_type = "ARXMLModule"
            node_id = EphemeralGraph._canonical_id(node_type, {"name": mod_name, "module": mod})
            props = {"name": mod_name, "source_file": filename, "module": mod}
            self._add_node_with_shadow(sandbox, node_type, node_id, props)

    # ── Shadow/Override Logic (Phase 5) ────────────────────────────────

    def _add_node_with_shadow(self, sandbox: 'EphemeralSandbox', node_type: str,
                              node_id: str, properties: Dict):
        """Add node; if it shadows a prod node, mark the override."""
        existing = sandbox.graph.get_node(node_id)
        if existing and existing.get("_origin") == "production":
            # Shadow: replace prod node with sandbox version
            properties["_origin"] = "sandbox"
            properties["_shadows"] = node_id
            properties["_original_prod_properties"] = {
                k: v for k, v in existing.items() if not k.startswith("_")
            }
            sandbox.graph.update_node(node_id, properties)
        else:
            properties["_origin"] = "sandbox"
            sandbox.graph.add_node(node_type, node_id, properties)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _iter_c_functions(parsed: Dict):
        """Yield function dicts from C parser output.

        Handles both formats:
        - dict:  {"func_name": {"parameters": ..., ...}, ...}  (regex/clang parser)
        - list:  [{"name": "func_name", ...}, ...]              (fallback parser)
        """
        functions = parsed.get("functions", [])
        if isinstance(functions, dict):
            for name, data in functions.items():
                fn = {"name": name}
                if isinstance(data, dict):
                    fn.update(data)
                yield fn
        elif isinstance(functions, list):
            for fn in functions:
                if isinstance(fn, dict) and fn.get("name"):
                    yield fn

    @staticmethod
    def _extract_text_content(parsed: Dict) -> str:
        """Extract text content from any parsed type for vector chunking."""
        ptype = parsed.get("type", "")
        if ptype in ("c_source", "c_header"):
            return parsed.get("content", "")
        elif ptype == "pdf":
            return "\n".join(parsed.get("pages", []))
        elif ptype == "rst":
            sections = parsed.get("sections", [])
            if isinstance(sections, list):
                return "\n".join(str(s) for s in sections)
            return str(sections)
        elif ptype == "json":
            import json as _json
            return _json.dumps(parsed.get("data", {}), indent=2, default=str)[:50000]
        elif ptype in ("text", "generic"):
            return parsed.get("content", "")
        return ""

    def _chunk_text(self, content: str, filename: str) -> List[Chunk]:
        """Split content into overlapping chunks for vector embedding."""
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
#  TraceabilityPuller — Plan 2 Phase 4
#  Pull ±N traceability neighbors from prod Neo4j
# ═════════════════════════════════════════════════════════════════════════

class TraceabilityPuller:
    """Pull ±N traceability neighbors from prod Neo4j for given node names.

    Read-only: no writes, no locks. Returns (nodes, relationships).
    """

    MAX_PULL_NODES = 500  # safety cap per upload call

    def __init__(self, neo4j_driver):
        self._driver = neo4j_driver

    @staticmethod
    def _resolve_database(workspace_id: str) -> str:
        """Resolve the actual Neo4j database name from storage_config.yaml.

        The workspace_id (e.g. 'mcal', 'illd') is NOT the database name —
        both profiles use database='neo4j' in the config.
        """
        try:
            from src.HybridRAG.code.neo4j_manager import get_instance_config
            cfg = get_instance_config(workspace_id)
            return cfg.database or "neo4j"
        except Exception:
            return "neo4j"

    def pull_neighbors(
        self, node_names: List[str], module: str,
        workspace_id: str = "illd", depth: int = 1,
    ) -> tuple:
        """Pull nodes ±depth hops from the named nodes in prod KG.

        Returns (nodes: List[dict], relationships: List[dict]).
        Each node dict: {"node_id": str, "node_type": str, "properties": dict}
        Each rel dict: {"source": str, "target": str, "rel_type": str, "properties": dict}
        """
        if not node_names or depth <= 0:
            return [], []

        db = self._resolve_database(workspace_id)
        module_upper = module.upper() if module else ""
        # depth and max_nodes are injected as literals because Neo4j 4.x
        # does not allow parameters inside [*1..N] or list slices [0..N].
        # elementId() replaced with toString(id()) for Neo4j 4.x compat.
        safe_depth = int(depth)
        safe_max = int(self.MAX_PULL_NODES)
        cypher = f"""
        MATCH (n)
        WHERE toUpper(n.module) = $module_upper
          AND (n.name IN $names OR n.function_name IN $names
               OR n.param_name IN $names OR n.requirement_id IN $names)
        WITH n LIMIT 200
        CALL {{
            WITH n
            MATCH path = (n)-[*1..{safe_depth}]-(neighbor)
            WHERE toUpper(neighbor.module) = $module_upper
            RETURN neighbor, relationships(path) AS rels
        }}
        WITH collect(DISTINCT n) + collect(DISTINCT neighbor) AS all_nodes,
             collect(rels) AS all_rel_lists
        UNWIND all_nodes AS node
        WITH collect(DISTINCT {{
                 node_id: toString(id(node)),
                 node_type: labels(node)[0],
                 properties: properties(node)
             }})[0..{safe_max}] AS nodes,
             all_rel_lists
        UNWIND all_rel_lists AS rel_list
        UNWIND rel_list AS rel
        WITH nodes, collect(DISTINCT {{
                 source: toString(id(startNode(rel))),
                 target: toString(id(endNode(rel))),
                 rel_type: type(rel),
                 properties: properties(rel)
             }}) AS relationships
        RETURN nodes, relationships
        """
        try:
            with self._driver.session(database=db) as session:
                result = session.run(
                    cypher,
                    names=node_names,
                    module_upper=module_upper,
                )
                record = result.single()
                if record:
                    nodes = record["nodes"] or []
                    rels = record["relationships"] or []
                    if len(nodes) >= self.MAX_PULL_NODES:
                        logger.warning(
                            "[TraceabilityPuller] Hit %d-node cap for module=%s, depth=%d",
                            self.MAX_PULL_NODES, module, depth,
                        )
                    return nodes, rels
                return [], []
        except Exception as e:
            logger.error("[TraceabilityPuller] Failed to pull neighbors: %s", e)
            return [], []

    def pull_boundary_nodes(
        self, names: List[str], workspace_id: str = "illd",
    ) -> List[Dict]:
        """Resolve boundary node names against prod KG (cross-module).

        Unlike pull_neighbors(), this does NOT filter by module — it finds
        nodes by name across all modules. Returns only the anchor nodes
        (no neighbor expansion) for lightweight boundary resolution.

        Returns list of node dicts: {"node_id": str, "node_type": str, "properties": dict}
        """
        if not names:
            return []

        db = self._resolve_database(workspace_id)
        # Neo4j 4.x: elementId() → toString(id()), literal max_nodes
        safe_max = int(self.MAX_PULL_NODES)
        cypher = f"""
        MATCH (n)
        WHERE n.name IN $names OR n.function_name IN $names
        RETURN collect(DISTINCT {{
                   node_id: toString(id(n)),
                   node_type: labels(n)[0],
                   properties: properties(n)
               }})[0..{safe_max}] AS nodes
        """
        try:
            with self._driver.session(database=db) as session:
                result = session.run(
                    cypher, names=names,
                )
                record = result.single()
                if record:
                    nodes = record["nodes"] or []
                    logger.info(
                        "[TraceabilityPuller] Boundary resolution: %d/%d names resolved",
                        len(nodes), len(names),
                    )
                    return nodes
                return []
        except Exception as e:
            logger.error("[TraceabilityPuller] Failed boundary pull: %s", e)
            return []


# ═════════════════════════════════════════════════════════════════════════
#  HybridGraphService — Plan 2 Phase 6
#  Route queries to sandbox or Neo4j based on traversal depth
# ═════════════════════════════════════════════════════════════════════════

class HybridGraphService:
    """Route queries to sandbox or Neo4j based on traversal depth needed."""

    # Shallow tools → sandbox NetworkX (has user's changes + ±1 context)
    SHALLOW_TOOLS = {
        "search_database", "search_nodes", "get_node_by_id",
        "get_neighbors", "sandbox_query", "sandbox_status",
        "query_api_function", "get_type_definition",
        "query_dependencies", "get_distribution",
    }

    # Deep tools → prod Neo4j + patch with sandbox overrides
    DEEP_TOOLS = {
        "find_coverage_gaps", "build_traceability_matrix",
        "find_requirement_traces", "shortest_path",
        "analyze_hw_sw_links", "detect_communities",
        "get_ontology_compliance", "get_coverage_report",
        "get_graph_statistics", "get_failure_patterns",
        "get_review_analytics", "get_learning_metrics",
        "execute_cypher",
    }

    def __init__(self, sandbox: EphemeralSandbox, neo4j_driver=None):
        self._sandbox = sandbox
        self._driver = neo4j_driver

    def search(self, query: str, top_k: int = 15, alpha: float = 0.5) -> List[SearchResult]:
        """Shallow query — search sandbox graph + vectors."""
        querier = SandboxQuerier(self._sandbox)
        results = querier.search(query, top_k=top_k, alpha=alpha)
        return results

    def deep_query(self, cypher: str, params: Dict, workspace_id: str = "illd") -> List[Dict]:
        """Deep traversal — run against prod Neo4j, patch with sandbox overrides.

        Returns list of dicts, each with _origin and optional _patched/_injected flags.
        """
        if not self._driver:
            # No Neo4j driver — fall back to sandbox-local
            logger.warning("[HybridGraphService] No Neo4j driver for deep query — sandbox-only")
            return self._sandbox_only_results()

        db = workspace_id if workspace_id in ("illd", "mcal") else "neo4j"

        try:
            with self._driver.session(database=db) as session:
                prod_results = session.run(cypher, params).data()
        except Exception as e:
            logger.warning("[HybridGraphService] Neo4j unavailable for deep query: %s", e)
            return self._sandbox_only_results(
                warning="Full traversal unavailable (Neo4j unreachable). "
                        "Showing sandbox-local results only.")

        # Patch: replace any node that has a sandbox override
        patched = []
        for record in prod_results:
            canonical = self._canonical_id_from_record(record)
            if canonical:
                sandbox_node = self._sandbox.graph.get_node(canonical)
                if sandbox_node and sandbox_node.get("_origin") == "sandbox":
                    record.update({k: v for k, v in sandbox_node.items() if not k.startswith("_")})
                    record["_patched"] = True
                    record["_origin"] = "sandbox"
                else:
                    record["_origin"] = "production"
            else:
                record["_origin"] = "production"
            patched.append(record)

        # Inject sandbox-only nodes (new nodes not in prod at all)
        existing_ids = set()
        for r in patched:
            cid = self._canonical_id_from_record(r)
            if cid:
                existing_ids.add(cid)

        for nid, data in self._sandbox.graph.get_nodes_by_origin("sandbox"):
            if nid not in existing_ids:
                injected = {k: v for k, v in data.items() if not k.startswith("_")}
                injected["_injected"] = True
                injected["_origin"] = "sandbox"
                injected["_node_id"] = nid
                patched.append(injected)

        return patched

    def _sandbox_only_results(self, warning: str = None) -> List[Dict]:
        """Return sandbox nodes as fallback when Neo4j is unavailable."""
        results = []
        for nid, data in self._sandbox.graph.get_all_nodes():
            entry = {k: v for k, v in data.items() if not k.startswith("_")}
            entry["_origin"] = data.get("_origin", "sandbox")
            entry["_node_id"] = nid
            results.append(entry)
        if warning:
            results.insert(0, {"_warning": warning})
        return results

    def _canonical_id_from_record(self, record: Dict) -> Optional[str]:
        """Derive canonical node ID from a Neo4j result record."""
        # Try common key patterns from prod results
        node_type = record.get("label") or record.get("_node_type") or ""
        name = (record.get("name") or record.get("function_name")
                or record.get("param_name") or record.get("requirement_id")
                or record.get("test_case_id") or "")
        module = record.get("module") or "unknown"
        if node_type and name:
            return f"{node_type}:{name}:{module}"
        return None

    @classmethod
    def classify_tool(cls, tool_name: str) -> str:
        """Return 'sandbox', 'hybrid', or 'passthrough' for a given tool."""
        if tool_name in cls.SHALLOW_TOOLS:
            return "sandbox"
        elif tool_name in cls.DEEP_TOOLS:
            return "hybrid"
        return "sandbox"  # Default unclassified tools to sandbox


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

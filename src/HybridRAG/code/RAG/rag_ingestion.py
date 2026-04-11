#!/usr/bin/env python3
"""
MCAL RAG Ingestion Pipeline — Markdown-first
==============================================
Ingests pre-converted SWA / SWUD section markdown files into ChromaDB
vector collections.

The markdown files are produced by the existing ``pdf_pipeline.py``
(LLM-based PDF-to-Markdown conversion) and live in ``swa/`` / ``swud/``
as ``section_*_raw.md`` files.  They contain well-structured headings,
feature IDs, traceability tags, and diagram descriptions — much richer
than raw PDF text extraction.

Pipeline:
  1. Read markdown file(s)
  2. Parse headings into hierarchical sections (3-level max, roll-up)
  3. Split large sections / merge tiny ones (overlap-aware)
  4. Extract automotive metadata (tags, Jama refs, function names)
  5. Embed with sentence-transformers  →  upsert to ChromaDB

ChromaDB collections (under ``chroma_data/mcal/``):
  {module}_swa_architecture   – 3.1.x  Architectural decisions, config, deps
  {module}_swa_callsequences  – 3.2    Dynamic view / call sequence diagrams
  {module}_swa_safety         – 3.3/3.4  Safety & trusted views
  {module}_swud_design        – SWUD   Unit-level design

  e.g. for ADC: adc_swa_architecture, adc_swa_callsequences, …

  See ``collection_naming.py`` for the central naming convention.

Embedding model: sentence-transformers/all-MiniLM-L6-v2 (384 dim)

Usage:
  # Auto-discover all section files from swa/ and swud/ dirs
  python mcal_rag_ingestion.py --module DIO

  # Ingest all sections from a specific directory
  python mcal_rag_ingestion.py --module DIO --input ../swa

  # Ingest specific files
  python mcal_rag_ingestion.py --module DIO \\
      --input ../swa/section_3_1_raw.md \\
      --input ../swa/section_3_2_raw.md

  # Dry-run (preview chunks, no writes)
  python mcal_rag_ingestion.py --module DIO --dry-run

  # Clear collections before ingesting
  python mcal_rag_ingestion.py --module DIO --clear
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Paths  (adjusted for code/RAG/ subfolder)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/RAG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG
CONFIG_DIR = HYBRIDRAG_DIR / "config"
SWA_DIR = HYBRIDRAG_DIR / "swa"
SWUD_DIR = HYBRIDRAG_DIR / "swud"

# Ensure the code dir is on sys.path so sibling modules (env_config, etc.) resolve
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcal_rag_ingestion")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Configuration                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def load_storage_config() -> dict:
    """Load ``storage_config.yaml`` with env-var resolution."""
    cfg_path = CONFIG_DIR / "storage_config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    try:
        from env_config import load_yaml_with_env
        return load_yaml_with_env(cfg_path)
    except ImportError:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Data classes                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

MAX_HIERARCHY_DEPTH = 3  # graphrag_core KG-V2 pattern

@dataclass
class Section:
    """A section parsed from the markdown document."""
    section_id: str
    title: str
    number: str              # e.g. "3.1.3.2"
    level: int               # capped at MAX_HIERARCHY_DEPTH
    raw_level: int           # original depth
    content: str = ""
    parent_id: Optional[str] = None
    children_ids: list[str] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0

    # Metadata (populated during enrichment)
    token_count: int = 0
    has_tables: bool = False
    has_figure: bool = False
    traceability_tags: list[str] = field(default_factory=list)
    jama_refs: list[str] = field(default_factory=list)
    feature_ids: list[str] = field(default_factory=list)
    related_functions: list[str] = field(default_factory=list)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Metadata extractors                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Traceability tags  [PRQ-xxx], [SWA-xxx], ...
TAG_RE = re.compile(
    r"\b(?P<tag>(?:PRQ|SWA|DES|SRS|MCAL|HWA|TST|ARC|SDD)"
    r"[\-_][A-Za-z0-9\-_]{2,})\b",
    re.IGNORECASE,
)

# Jama refs  AU3GM-PRQ-nnnnn
JAMA_RE = re.compile(r"\b(?P<ref>AU3GM-(?:PRQ|SRS|SHRQ)-\d+)\b", re.IGNORECASE)

# Feature IDs  featureID={GUID}
FEATURE_RE = re.compile(r"featureID=\{(?P<fid>[0-9A-Fa-f\-]+)\}")

# AUTOSAR API function names  Adc_Init, Spi_AsyncTransmit, ...
FUNC_RE = re.compile(
    r"\b(?P<func>"
    r"(?:Adc|Can|Spi|Gpt|Icu|Pwm|Mcu|Port|Dio|Wdg|Fee|Fls|Eth|Lin|Fr"
    r"|I2c|Dma|Cdsp|Det|Dem|SchM|Mcal|Dsadc|Eru|Bfx|Crc|Smu|Ocu|EcuM|Ifx)"
    r"_[A-Z][A-Za-z0-9_]+)\b"
)
_TYPE_SUFFIXES = ("Type", "Enum", "Struct", "ConfigType", "StatusType",
                  "InfoType", "ReturnType")

# Table detection
TABLE_RE = re.compile(r"^\|.*\|.*\|", re.MULTILINE)

# Page-marker pattern from pdf_pipeline output
PAGE_MARKER_RE = re.compile(r"^##\s+Pages?\s+(\d+)(?:\s*[-–]\s*(\d+))?\s*$",
                            re.MULTILINE)

# Markdown heading with section number
#   ## 3.2.1 Initialization
#   ### 3.1.3.2.1 ADC: Conversion complete notification ...
#   ## Figure 25: Initialization (diagram explained)
MD_HEADING_RE = re.compile(
    r"^(?P<hashes>#{2,6})\s+"
    r"(?:(?P<number>\d+(?:\.\d+)+)\s+)?"
    r"(?P<title>.+?)\s*$",
    re.MULTILINE,
)


def extract_tags(text: str) -> list[str]:
    return sorted({m.group("tag").upper() for m in TAG_RE.finditer(text)})


def extract_jama_refs(text: str) -> list[str]:
    return sorted({m.group("ref").upper() for m in JAMA_RE.finditer(text)})


def extract_feature_ids(text: str) -> list[str]:
    return sorted({m.group("fid").upper() for m in FEATURE_RE.finditer(text)})


def extract_functions(text: str) -> list[str]:
    funcs: set[str] = set()
    for m in FUNC_RE.finditer(text):
        name = m.group("func")
        if not any(name.endswith(s) for s in _TYPE_SUFFIXES):
            funcs.add(name)
    return sorted(funcs)


def estimate_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3) if text else 0


def sanitize_metadata(meta: dict) -> dict:
    """Remove None values and coerce non-primitives for ChromaDB."""
    out: dict = {}
    for k, v in meta.items():
        if v is None:
            continue
        if not isinstance(v, (str, int, float, bool)):
            v = str(v)
        out[k] = v
    return out


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Section routing  (section number → collection)                         ║
# ║  Delegated to collection_naming.py for consistent naming across tools   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

from collection_naming_unified import (
    collection_name as _coll_name,
    module_collections as _module_collections,
    route_section as _route_section_central,
)


def route_section(number: str, doc_category: str, module: str = "ADC") -> tuple[str, str]:
    """Return (collection_name, doc_type) for a section number.

    Delegates to :func:`collection_naming.route_section` so that every
    tool in the pipeline uses the same ``{module}_{source}_{category}``
    naming convention.
    """
    return _route_section_central(number, doc_category, module)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Markdown Chunker                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class MarkdownChunker:
    """
    Parse a markdown file into hierarchical sections and produce
    chunks ready for ChromaDB upsert.

    Design (aligned with ``graphrag_core.chunking.AutomotiveSmartChunker``):
      - 3-level max hierarchy; deeper content rolls up into L3 parents
      - Large sections split at paragraph boundaries with overlap
      - Tiny consecutive sections merged under same parent
      - Rich metadata extracted per chunk
    """

    def __init__(
        self,
        max_chunk_chars: int = 2000,
        overlap_chars: int = 200,
        min_chunk_chars: int = 120,
        module: str = "ADC",
        source_file: str = "",
        doc_category: str = "swa",
    ):
        self.max_chunk_chars = max_chunk_chars
        self.overlap_chars = overlap_chars
        self.min_chunk_chars = min_chunk_chars
        self.module = module.upper()
        self.source_file = source_file
        self.doc_category = doc_category

    # -- public API ---------------------------------------------------------

    def chunk(self, md_text: str) -> dict[str, list[dict]]:
        """
        Parse *md_text*, produce ``{collection_name: [chunk_dict]}``
        ready for ChromaDB upsert.
        """
        sections = self._parse_headings(md_text)
        logger.info("Parsed %d heading-based sections", len(sections))

        sections = self._build_hierarchy(sections)
        sections = self._rollup_deep(sections)
        sections = self._merge_tiny(sections)
        sections = self._split_large(sections)
        logger.info("After merge/split: %d sections", len(sections))

        for sec in sections:
            self._enrich(sec)

        return self._to_collection_chunks(sections)

    # -- heading parser -----------------------------------------------------

    def _parse_headings(self, text: str) -> list[Section]:
        """
        Split markdown text into sections based on ``## N.N.N Title``
        headings.  Also picks up ``## Figure N: ...`` headings and
        attaches them to their parent section.
        """
        # Track current page from page markers
        current_page = 0
        page_positions: list[tuple[int, int]] = []   # (char_pos, page_num)
        for m in PAGE_MARKER_RE.finditer(text):
            page_positions.append((m.start(), int(m.group(1))))

        def page_at(pos: int) -> int:
            """Return latest page number at or before *pos*."""
            p = 0
            for cp, pn in page_positions:
                if cp <= pos:
                    p = pn
                else:
                    break
            return p

        # Find all real headings (skip page markers and figure headings
        # that don't have section numbers — those get folded into content)
        headings: list[tuple[int, int, str, str]] = []  # (pos, depth, number, title)
        for m in MD_HEADING_RE.finditer(text):
            hashes = m.group("hashes")
            number = m.group("number") or ""
            title = m.group("title").strip()
            depth = len(hashes)

            # Skip page markers (already handled separately)
            if re.match(r"Pages?\s+\d+", title, re.IGNORECASE):
                continue

            # If no section number, treat it as a sub-element (figure/diagram)
            # and give it a synthetic number based on context
            if not number:
                # Keep figure headings — they'll be folded into parent content
                # by the hierarchy builder.  Give them a placeholder number.
                if headings:
                    parent_num = headings[-1][2] or "0"
                    number = f"{parent_num}.fig"
                else:
                    number = "0.fig"

            headings.append((m.start(), depth, number, title))

        if not headings:
            logger.warning("No section headings found; treating entire text as one chunk")
            return [Section(
                section_id=f"{self.module}_S001",
                title=f"{self.module} Document",
                number="1", level=1, raw_level=1,
                content=self._strip_page_markers(text),
                token_count=estimate_tokens(text),
            )]

        # Slice text between consecutive headings
        sections: list[Section] = []
        for idx, (pos, depth, number, title) in enumerate(headings):
            # Content starts after the heading line
            nl = text.find("\n", pos)
            content_start = nl + 1 if nl != -1 else pos + len(title)

            content_end = headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
            body = text[content_start:content_end]

            # Clean page markers from body
            body = self._strip_page_markers(body).strip()

            # Figure headings without a real section number — fold into parent
            if number.endswith(".fig"):
                # Append figure content to the previous section
                if sections:
                    sections[-1].content += f"\n\n**{title}**\n{body}"
                    sections[-1].token_count = estimate_tokens(sections[-1].content)
                    if "figure" in title.lower() or "diagram" in title.lower():
                        sections[-1].has_figure = True
                continue

            level = min(len(number.split(".")), MAX_HIERARCHY_DEPTH)

            sec_idx = len(sections) + 1
            sections.append(Section(
                section_id=f"{self.module}_S{sec_idx:03d}",
                title=title,
                number=number,
                level=level,
                raw_level=len(number.split(".")),
                content=body,
                page_start=page_at(pos),
                page_end=page_at(content_end),
                token_count=estimate_tokens(body),
            ))

        return sections

    @staticmethod
    def _strip_page_markers(text: str) -> str:
        return PAGE_MARKER_RE.sub("", text).strip()

    # -- hierarchy ----------------------------------------------------------

    def _build_hierarchy(self, sections: list[Section]) -> list[Section]:
        by_number = {s.number: s for s in sections}
        for sec in sections:
            parts = sec.number.split(".")
            if len(parts) > 1:
                parent_num = ".".join(parts[:-1])
                parent = by_number.get(parent_num)
                if parent:
                    sec.parent_id = parent.section_id
                    parent.children_ids.append(sec.section_id)
        return sections

    # -- rollup deep content into L3 ----------------------------------------

    def _rollup_deep(self, sections: list[Section]) -> list[Section]:
        by_number = {s.number: s for s in sections}
        to_remove: set[str] = set()

        for sec in sections:
            if sec.raw_level <= MAX_HIERARCHY_DEPTH:
                continue
            # Find nearest ≤L3 ancestor
            parts = sec.number.split(".")
            for trim in range(len(parts) - 1, 0, -1):
                ancestor_num = ".".join(parts[:trim])
                ancestor = by_number.get(ancestor_num)
                if ancestor and ancestor.raw_level <= MAX_HIERARCHY_DEPTH:
                    rolled = f"\n\n**{sec.number} {sec.title}**\n{sec.content}"
                    ancestor.content += rolled
                    ancestor.token_count = estimate_tokens(ancestor.content)
                    to_remove.add(sec.section_id)
                    break

        result = [s for s in sections if s.section_id not in to_remove]
        if to_remove:
            logger.info("Rolled up %d deep sections into L3 parents", len(to_remove))
        return result

    # -- merge tiny sections ------------------------------------------------

    def _merge_tiny(self, sections: list[Section]) -> list[Section]:
        if not sections:
            return sections
        merged: list[Section] = []
        i = 0
        while i < len(sections):
            sec = sections[i]
            if len(sec.content) >= self.min_chunk_chars:
                merged.append(sec)
                i += 1
                continue

            # Accumulate consecutive tiny sections with same parent
            group = [sec]
            total = len(sec.content)
            j = i + 1
            while j < len(sections):
                nxt = sections[j]
                if (nxt.parent_id == sec.parent_id
                        and len(nxt.content) < self.min_chunk_chars
                        and total + len(nxt.content) < self.max_chunk_chars):
                    group.append(nxt)
                    total += len(nxt.content)
                    j += 1
                else:
                    break

            if len(group) > 1:
                combined = "\n\n".join(
                    f"**{s.number} {s.title}**\n{s.content}" for s in group
                )
                titles = [s.title for s in group[:3]]
                if len(group) > 3:
                    titles.append(f"(+{len(group)-3} more)")
                merged.append(Section(
                    section_id=f"{group[0].section_id}_merged",
                    title=" / ".join(titles),
                    number=group[0].number,
                    level=group[0].level,
                    raw_level=group[0].raw_level,
                    content=combined,
                    parent_id=sec.parent_id,
                    page_start=group[0].page_start,
                    page_end=group[-1].page_end,
                    token_count=estimate_tokens(combined),
                ))
            else:
                merged.append(sec)
            i = j
        return merged

    # -- split large sections -----------------------------------------------

    def _split_large(self, sections: list[Section]) -> list[Section]:
        result: list[Section] = []
        for sec in sections:
            if len(sec.content) <= self.max_chunk_chars:
                result.append(sec)
                continue
            parts = self._split_with_overlap(
                sec.content, self.max_chunk_chars, self.overlap_chars
            )
            for pi, part in enumerate(parts):
                result.append(Section(
                    section_id=f"{sec.section_id}_p{pi}",
                    title=(f"{sec.title} (Part {pi+1}/{len(parts)})"
                           if len(parts) > 1 else sec.title),
                    number=sec.number,
                    level=sec.level,
                    raw_level=sec.raw_level,
                    content=part,
                    parent_id=sec.parent_id,
                    page_start=sec.page_start,
                    page_end=sec.page_end,
                    token_count=estimate_tokens(part),
                ))
        return result

    @staticmethod
    def _split_with_overlap(text: str, max_chars: int, overlap: int) -> list[str]:
        paragraphs = text.split("\n\n")
        parts: list[str] = []
        buf: list[str] = []
        buf_len = 0
        for para in paragraphs:
            plen = len(para) + 2
            if buf_len + plen > max_chars and buf:
                parts.append("\n\n".join(buf))
                full = "\n\n".join(buf)
                if overlap > 0 and len(full) > overlap:
                    buf = [full[-overlap:]]
                    buf_len = overlap
                else:
                    buf, buf_len = [], 0
            buf.append(para)
            buf_len += plen
        if buf:
            parts.append("\n\n".join(buf))
        return parts or [text]

    # -- enrichment ---------------------------------------------------------

    def _enrich(self, sec: Section) -> None:
        text = f"{sec.title}\n{sec.content}"
        sec.traceability_tags = extract_tags(text)
        sec.jama_refs = extract_jama_refs(text)
        sec.feature_ids = extract_feature_ids(text)
        sec.related_functions = extract_functions(text)
        sec.has_tables = bool(TABLE_RE.search(text))
        if not sec.has_figure:
            sec.has_figure = bool(re.search(
                r"(?:figure|diagram|sequence diagram)", text, re.IGNORECASE
            ))

    # -- build chunk dicts & route to collections ---------------------------

    def _to_collection_chunks(
        self, sections: list[Section]
    ) -> dict[str, list[dict]]:
        coll_chunks: dict[str, list[dict]] = defaultdict(list)
        for sec in sections:
            coll_name, doc_type = route_section(
                sec.number, self.doc_category, self.module,
            )
            chunk = self._make_chunk(sec, coll_name, doc_type)
            coll_chunks[coll_name].append(chunk)
        total = sum(len(v) for v in coll_chunks.values())
        logger.info("Produced %d chunks across %d collections", total, len(coll_chunks))
        return dict(coll_chunks)

    def _make_chunk(self, sec: Section, coll: str, doc_type: str) -> dict:
        heading = f"{sec.number} {sec.title}"
        text = f"[{heading}]\n\n{sec.content}" if sec.content else heading
        content_hash = hashlib.md5(text.encode()).hexdigest()[:10]
        chunk_id = re.sub(
            r"[^a-zA-Z0-9_]", "_",
            f"mcal_{self.module}_{sec.number}_{content_hash}",
        )
        meta: dict[str, Any] = {
            "type": doc_type,
            "module": self.module,
            "doc_category": self.doc_category,
            "section_number": sec.number,
            "section_title": sec.title,
            "heading": heading,
            "level": sec.level,
            "source_file": self.source_file,
            "page_start": sec.page_start,
            "page_end": sec.page_end,
            "tags": ", ".join(sec.traceability_tags) if sec.traceability_tags else "",
            "tag_count": len(sec.traceability_tags),
            "jama_refs": ", ".join(sec.jama_refs) if sec.jama_refs else "",
            "jama_ref_count": len(sec.jama_refs),
            "feature_ids": ", ".join(sec.feature_ids)[:500] if sec.feature_ids else "",
            "functions": ", ".join(sec.related_functions) if sec.related_functions else "",
            "function_count": len(sec.related_functions),
            "has_tables": sec.has_tables,
            "has_figure": sec.has_figure,
            "token_count": sec.token_count,
            "char_count": len(text),
            "parent_section_id": sec.parent_id or "",
        }
        return {"id": chunk_id, "text": text, "metadata": meta}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Ingestion Pipeline                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class MCALRAGIngestionPipeline:
    """
    End-to-end: Markdown files  →  Chunks  →  Embeddings  →  ChromaDB.
    """

    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(
        self,
        module: str = "ADC",
        input_paths: Optional[list[Path]] = None,
        chroma_path: Optional[Path] = None,
        max_chunk_chars: int = 2000,
        overlap_chars: int = 200,
        min_chunk_chars: int = 120,
        dry_run: bool = False,
        clear: bool = False,
    ):
        self.module = module.upper()
        self.input_paths = input_paths or []
        self.max_chunk_chars = max_chunk_chars
        self.overlap_chars = overlap_chars
        self.min_chunk_chars = min_chunk_chars
        self.dry_run = dry_run
        self.clear = clear

        # Resolve chroma path
        if chroma_path is None:
            try:
                cfg = load_storage_config()
                cp = cfg.get("chromadb", {}).get("mcal", {}).get(
                    "persist_directory", "./chroma_data/mcal"
                )
                chroma_path = Path(cp)
            except Exception:
                chroma_path = Path("./chroma_data/mcal")
            if not chroma_path.is_absolute():
                chroma_path = HYBRIDRAG_DIR / chroma_path
        self.chroma_path = Path(chroma_path)

        self._model = None
        self._client = None
        self.stats: dict[str, int] = {}
        self.total_chunks = 0

    # -- lazy loaders -------------------------------------------------------

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model: %s", self.EMBEDDING_MODEL)
            self._model = SentenceTransformer(self.EMBEDDING_MODEL)
        return self._model

    def _get_client(self):
        if self._client is None:
            from RAG.vector_store_factory import get_vector_client
            self._client = get_vector_client()
        return self._client

    # -- detect doc category from filename ----------------------------------

    @staticmethod
    def _detect_category(path: Path) -> str:
        name = path.stem.lower()
        if "swud" in name:
            return "swud"
        # Also check parent directory name (e.g. swud/section_4_7_raw.md)
        parent = path.parent.name.lower()
        if "swud" in parent:
            return "swud"
        if "3_3" in name or "3_4" in name:
            return "swa"       # safety sections — routing handles collection
        return "swa"

    # -- upsert to ChromaDB -------------------------------------------------

    def _upsert(self, collection_name: str, chunks: list[dict]) -> None:
        if not chunks:
            return

        if self.dry_run:
            logger.info("[DRY-RUN] Would upsert %d chunks → %s",
                        len(chunks), collection_name)
            self.stats[collection_name] = (
                self.stats.get(collection_name, 0) + len(chunks)
            )
            self.total_chunks += len(chunks)
            return

        client = self._get_client()
        model = self._get_model()
        collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        texts = [c["text"] for c in chunks]
        logger.info("Encoding %d chunks …", len(texts))
        embeddings = model.encode(texts, show_progress_bar=True).tolist()

        BATCH = 100
        for i in range(0, len(chunks), BATCH):
            bc = chunks[i:i+BATCH]
            bt = texts[i:i+BATCH]
            be = embeddings[i:i+BATCH]
            collection.upsert(
                ids=[c["id"] for c in bc],
                documents=bt,
                embeddings=be,
                metadatas=[sanitize_metadata(c["metadata"]) for c in bc],
            )

        n = len(chunks)
        self.stats[collection_name] = self.stats.get(collection_name, 0) + n
        self.total_chunks += n
        logger.info("Upserted %d chunks → %s", n, collection_name)

    # -- main pipeline ------------------------------------------------------

    def ingest(self) -> dict:
        t0 = time.time()

        logger.info("=" * 60)
        logger.info("  MCAL RAG INGESTION (Markdown-first)")
        logger.info("=" * 60)
        logger.info("Module          : %s", self.module)
        logger.info("Input files     : %d", len(self.input_paths))
        for p in self.input_paths:
            logger.info("  - %s  (%d KB)", p.name, p.stat().st_size // 1024)
        logger.info("ChromaDB path   : %s", self.chroma_path)
        logger.info("Max chunk chars : %d", self.max_chunk_chars)
        logger.info("Overlap chars   : %d", self.overlap_chars)
        logger.info("Dry-run         : %s", self.dry_run)
        logger.info("Clear           : %s", self.clear)
        logger.info("=" * 60)

        if not self.input_paths:
            logger.error("No input files. Use --input <path>.")
            return {"error": "No input files"}

        for p in self.input_paths:
            if not p.exists():
                logger.error("File not found: %s", p)
                return {"error": f"Not found: {p}"}

        # Clear collections (module-specific)
        if self.clear and not self.dry_run:
            client = self._get_client()
            for cname in _module_collections(self.module):
                try:
                    client.delete_collection(name=cname)
                    logger.info("Deleted collection: %s", cname)
                except Exception:
                    pass

        # Process each file in parallel
        all_coll_chunks: dict[str, list[dict]] = defaultdict(list)
        chunks_lock = threading.Lock()

        def _chunk_file(md_path: Path) -> dict[str, list[dict]]:
            logger.info("Processing: %s", md_path.name)
            md_text = md_path.read_text(encoding="utf-8")
            doc_cat = self._detect_category(md_path)
            logger.info("Category: %s  |  %d chars", doc_cat.upper(), len(md_text))

            chunker = MarkdownChunker(
                max_chunk_chars=self.max_chunk_chars,
                overlap_chars=self.overlap_chars,
                min_chunk_chars=self.min_chunk_chars,
                module=self.module,
                source_file=md_path.name,
                doc_category=doc_cat,
            )
            return chunker.chunk(md_text)

        with ThreadPoolExecutor(max_workers=100) as executor:
            futures = {
                executor.submit(_chunk_file, md_path): md_path
                for md_path in self.input_paths
            }
            for future in as_completed(futures):
                coll_chunks = future.result()
                with chunks_lock:
                    for coll, chunks in coll_chunks.items():
                        all_coll_chunks[coll].extend(chunks)

        # Upsert
        logger.info("-" * 50)
        for coll, chunks in all_coll_chunks.items():
            self._upsert(coll, chunks)

        elapsed = time.time() - t0
        self._print_summary(dict(all_coll_chunks), elapsed)

        return {
            "module": self.module,
            "collections": dict(self.stats),
            "total_chunks": self.total_chunks,
            "elapsed_seconds": round(elapsed, 2),
            "dry_run": self.dry_run,
        }

    # -- summary ------------------------------------------------------------

    def _print_summary(self, all_chunks: dict[str, list[dict]], elapsed: float):
        mode = "PREVIEW" if self.dry_run else "COMPLETE"
        print(f"\n{'='*65}")
        print(f"  MCAL RAG INGESTION {mode}")
        print(f"{'='*65}")
        print(f"  Module:       {self.module}")
        print(f"  Elapsed:      {elapsed:.1f}s")
        print(f"  Total chunks: {self.total_chunks}")

        for coll, count in sorted(self.stats.items()):
            chunks = all_chunks.get(coll, [])
            print(f"\n  Collection: {coll}  ({count} chunks)")
            if not chunks:
                continue

            types: Counter = Counter()
            tag_set: set = set()
            func_set: set = set()
            fig_count = 0

            for c in chunks:
                m = c["metadata"]
                types[m.get("type", "?")] += 1
                t = m.get("tags", "")
                if t:
                    tag_set.update(x.strip() for x in t.split(",") if x.strip())
                f = m.get("functions", "")
                if f:
                    func_set.update(x.strip() for x in f.split(",") if x.strip())
                if m.get("has_figure"):
                    fig_count += 1

            for dt, cnt in sorted(types.items()):
                print(f"    {dt:<30s}  {cnt:>3d} chunk(s)")
            if tag_set:
                print(f"    Traceability tags : {len(tag_set)}")
            if func_set:
                print(f"    Functions found   : {len(func_set)}")
            if fig_count:
                print(f"    Chunks w/ figures : {fig_count}")

            sizes = [len(c["text"]) for c in chunks]
            print(f"    Chunk sizes: min={min(sizes)}, max={max(sizes)}, "
                  f"avg={sum(sizes)//len(sizes)}")

        print(f"{'='*65}\n")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _resolve_inputs(raw_inputs: list[Path] | None, module: str) -> list[Path]:
    """Resolve input paths: expand directories and auto-discover SWA/SWUD dirs.

    Accepts:
      - Individual markdown files
      - Directories (auto-discovers section_*_raw.md inside)
      - None with a module name (auto-discovers from swa/ and swud/ dirs)
    """
    section_glob = "section_*_raw.md"
    resolved: list[Path] = []

    if raw_inputs:
        for p in raw_inputs:
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.is_dir():
                found = sorted(p.glob(section_glob))
                if not found:
                    logger.warning("No %s files found in %s", section_glob, p)
                else:
                    logger.info("Discovered %d section files in %s", len(found), p)
                    resolved.extend(found)
            else:
                resolved.append(p)
    else:
        # Auto-discover from standard SWA/SWUD directories
        for doc_dir in (SWA_DIR, SWUD_DIR):
            found = sorted(doc_dir.glob(section_glob))
            if found:
                logger.info("Auto-discovered %d section files in %s", len(found), doc_dir)
                resolved.extend(found)

    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest MCAL section markdown files into ChromaDB / Qdrant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Auto-discover all section files from swa/ and swud/ dirs\n"
            "  python mcal_rag_ingestion.py --module DIO\n\n"
            "  # Ingest all sections from a specific directory\n"
            "  python mcal_rag_ingestion.py --module DIO --input ../../swa\n\n"
            "  # Ingest specific files\n"
            "  python mcal_rag_ingestion.py --module DIO \\\n"
            "      --input ../swa/section_3_1_raw.md \\\n"
            "      --input ../swa/section_3_2_raw.md\n\n"
            "  # Dry-run\n"
            "  python mcal_rag_ingestion.py --module DIO --dry-run\n"
        ),
    )
    parser.add_argument(
        "--input", dest="inputs", action="append", type=Path, default=None,
        help="Path to a markdown file or directory (repeat for multiple). "
             "If omitted, auto-discovers section_*_raw.md from swa/ and swud/ dirs.",
    )
    parser.add_argument("--module", default="ADC", help="Module name (default: ADC).")
    parser.add_argument("--chroma-path", type=Path, default=None,
                        help="Override ChromaDB directory.")
    parser.add_argument("--max-chunk-chars", type=int, default=2000,
                        help="Max characters per chunk (default: 2000).")
    parser.add_argument("--overlap-chars", type=int, default=200,
                        help="Overlap between sub-chunks (default: 200).")
    parser.add_argument("--clear", action="store_true",
                        help="Delete existing MCAL collections first.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview chunks without ChromaDB writes.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG-level logging.")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    inputs = _resolve_inputs(args.inputs, args.module)
    if not inputs:
        logger.error("No section files found. Use --input <path/dir> or ensure swa/ and swud/ contain section_*_raw.md files.")
        return 1

    pipeline = MCALRAGIngestionPipeline(
        module=args.module,
        input_paths=inputs,
        chroma_path=args.chroma_path,
        max_chunk_chars=args.max_chunk_chars,
        overlap_chars=args.overlap_chars,
        dry_run=args.dry_run,
        clear=args.clear,
    )
    result = pipeline.ingest()
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())

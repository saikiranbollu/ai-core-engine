"""
Source Code Qdrant Ingestion
==============================

Embeds parsed C source code functions into Qdrant for semantic search.
Reads the intermediate JSON output from the KG source code ingestion
(step 6) and re-opens source files to extract full function bodies.

Collection naming: ``mcal_{module}_sourcecode``

Each function (local/static and global) becomes one vector document containing:
  - Function identity (name, module, visibility)
  - Signature and doc-block description
  - APIs called (from call_edges)
  - Full function body (C code)

Usage::

    python sourcecode_qdrant_ingest.py \\
        --module ADC

    # With explicit temp dir
    python sourcecode_qdrant_ingest.py \\
        --module ADC --temp-dir temp/src_adc

    # Dry run (no Qdrant writes)
    python sourcecode_qdrant_ingest.py --module ADC --dry-run

    # Clear collection before ingesting
    python sourcecode_qdrant_ingest.py --module ADC --clear
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("sourcecode_qdrant_ingest")

# Path setup
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
RAG_DIR = CODE_DIR / "RAG"
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(RAG_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_DIR))


# ---------------------------------------------------------------------------
# Text Builder
# ---------------------------------------------------------------------------

def build_document_text(
    func: dict,
    calls: List[str],
    body: str,
    module: str,
) -> str:
    """
    Build the text document to embed for a single source code function.
    Structured for optimal semantic search of implementation code.
    """
    parts = []

    # --- Defensive fix: correct is_static/is_inline leaked into return_type ---
    is_static = func.get("is_static", False)
    return_type = func.get("return_type", "void")
    if not is_static and return_type.startswith("static "):
        is_static = True
        return_type = return_type[len("static "):].strip()
    if return_type.startswith("inline "):
        return_type = return_type[len("inline "):].strip()
    # --------------------------------------------------------------------------

    # Identity
    visibility = "static (local)" if is_static else "global"
    parts.append(f"Function: {func['name']}")
    parts.append(f"Module: {module}")
    parts.append(f"Visibility: {visibility}")
    parts.append(f"Return type: {return_type}")
    parts.append(f"Signature: {func.get('signature', func['name'])}")

    # Doc-block description (natural language — high semantic value)
    if func.get("description"):
        parts.append(f"Description: {func['description']}")

    # AUTOSAR attributes
    if func.get("reentrancy"):
        parts.append(f"Reentrancy: {func['reentrancy']}")
    if func.get("sync_async"):
        parts.append(f"Sync/Async: {func['sync_async']}")

    # Compile condition
    if func.get("compile_condition"):
        parts.append(f"Compile guard: {func['compile_condition']}")

    # APIs called
    if calls:
        parts.append(f"Calls: {', '.join(calls)}")

    # Full function body
    parts.append(f"Source code:\n{body}")

    return "\n".join(parts)


def build_metadata(
    func: dict,
    calls: List[str],
    module: str,
    has_critical_section: bool,
    critical_section_names: List[str],
) -> Dict[str, str]:
    """Build metadata dict for Qdrant payload (filterable fields)."""
    # --- Defensive fix: correct is_static/is_inline leaked into return_type ---
    is_static = func.get("is_static", False)
    is_inline = func.get("is_inline", False)
    return_type = func.get("return_type", "void")
    if not is_static and return_type.startswith("static "):
        is_static = True
        return_type = return_type[len("static "):].strip()
    if not is_inline and return_type.startswith("inline "):
        is_inline = True
        return_type = return_type[len("inline "):].strip()
    # --------------------------------------------------------------------------
    meta = {
        "module": module,
        "function_name": func["name"],
        "source_file": func.get("_file_id", ""),
        "is_static": str(is_static),
        "is_inline": str(is_inline),
        "return_type": return_type,
        "start_line": str(func.get("start_line", 0)),
        "end_line": str(func.get("end_line", 0)),
    }
    if func.get("compile_condition"):
        meta["compile_condition"] = func["compile_condition"]
    if calls:
        meta["calls"] = ",".join(calls)
    if func.get("signature"):
        meta["signature"] = func["signature"]
    if func.get("description"):
        meta["description"] = func["description"]
    meta["has_critical_section"] = str(has_critical_section)
    if critical_section_names:
        meta["critical_section_names"] = ",".join(critical_section_names)
    if func.get("traceability_ids"):
        meta["traceability_ids"] = func["traceability_ids"]
    return meta


# ---------------------------------------------------------------------------
# Function Body Extraction
# ---------------------------------------------------------------------------

def extract_function_body(source_path: Path, start_line: int, end_line: int) -> Optional[str]:
    """
    Extract the function body from a source file using line numbers.
    Returns the full text from start_line to end_line (inclusive, 1-based).
    """
    if not source_path.exists():
        return None
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").split("\n")
        # start_line and end_line are 1-based
        body_lines = lines[start_line - 1 : end_line]
        return "\n".join(body_lines)
    except (OSError, IndexError) as exc:
        logger.warning("Could not extract body from %s (lines %d-%d): %s",
                       source_path, start_line, end_line, exc)
        return None


# ---------------------------------------------------------------------------
# Critical Section Detection from register_accesses.json
# ---------------------------------------------------------------------------

def load_critical_sections(temp_dir: Path) -> Dict[str, tuple]:
    """
    Load register_accesses.json and extract per-function critical section info.
    Returns {function_name: (has_cs: bool, cs_names: list[str])}.
    """
    ra_path = temp_dir / "register_accesses.json"
    if not ra_path.exists():
        return {}

    try:
        accesses = json.loads(ra_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    cs_map: Dict[str, set] = {}
    for acc in accesses:
        if not acc.get("in_critical_section"):
            continue
        # function_id format: "PREFIX::rel_path::func_name"
        func_id = acc.get("function_id", "")
        func_name = func_id.rsplit("::", 1)[-1] if func_id else ""
        if not func_name:
            func_name = acc.get("function_name") or acc.get("caller_name", "")
        if not func_name:
            continue
        cs_name = acc.get("critical_section_name", "unknown")
        cs_map.setdefault(func_name, set()).add(cs_name)

    return {fn: (True, sorted(names)) for fn, names in cs_map.items()}


# ---------------------------------------------------------------------------
# Source Code Qdrant Ingestion
# ---------------------------------------------------------------------------

class SourceCodeQdrantIngest:
    """Embeds and upserts source code functions into Qdrant."""

    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    BATCH_SIZE = 64
    # Skip functions with bodies longer than this (likely auto-generated tables)
    MAX_BODY_CHARS = 50_000

    def __init__(
        self,
        module: str,
        *,
        temp_dir: Optional[Path] = None,
        dry_run: bool = False,
        clear: bool = False,
    ):
        self.module = module.upper()
        self.temp_dir = temp_dir or (HYBRIDRAG_DIR / "temp" / f"src_{self.module.lower()}")
        self.dry_run = dry_run
        self.clear = clear
        self.collection_name = f"mcal_{self.module.lower()}_sourcecode"
        self._model = None
        self._client = None
        self.stats = Counter()

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading embedding model: %s", self.EMBEDDING_MODEL)
            self._model = SentenceTransformer(self.EMBEDDING_MODEL)
        return self._model

    def _get_client(self):
        if self._client is None:
            from vector_store_factory import get_vector_client
            self._client = get_vector_client()
        return self._client

    def ingest(self):
        """Run the full ingestion pipeline."""
        t0 = time.time()

        print("=" * 60)
        print(f"  Source Code Qdrant Ingestion — {self.module}")
        print(f"  Temp dir: {self.temp_dir}")
        print(f"  Collection: {self.collection_name}")
        print(f"  Dry run: {self.dry_run}")
        print("=" * 60)

        # 1. Load intermediate JSON from KG step 6
        logger.info("Step 1: Loading intermediate data …")
        functions = self._load_json("functions.json")
        if not functions:
            print("  ERROR: No functions.json found. Run step 6 (source code KG) first.")
            return

        call_edges = self._load_json("call_edges.json") or []
        summary = self._load_json("summary.json") or {}
        source_dir = Path(summary.get("source_dir", self.temp_dir))

        print(f"  Loaded {len(functions)} functions, {len(call_edges)} call edges")
        print(f"  Source dir: {source_dir}")

        # 2. Build call graph lookup: caller_id → [callee_name, ...]
        logger.info("Step 2: Building call graph index …")
        calls_by_func = self._build_call_index(call_edges, functions)

        # 3. Load critical section info
        logger.info("Step 3: Loading critical section info …")
        cs_info = load_critical_sections(self.temp_dir)
        cs_count = sum(1 for v in cs_info.values() if v[0])
        print(f"  {cs_count} functions have critical section annotations")

        # 4. Build document chunks
        logger.info("Step 4: Building document chunks …")
        chunks = self._build_chunks(functions, calls_by_func, cs_info, source_dir)
        print(f"  Built {len(chunks)} document chunks")
        self.stats["chunks_total"] = len(chunks)

        if not chunks:
            print("  No chunks to ingest. Done.")
            return

        # Show sample
        sample = chunks[0]
        preview = sample["text"][:300]
        print(f"\n  Sample chunk ({sample['id']}):")
        print(f"  {preview}…")
        print(f"  Text length: {len(sample['text'])} chars\n")

        if self.dry_run:
            print("  DRY RUN — no Qdrant writes.")
            self._print_summary(t0)
            return

        # 5. Get Qdrant client and collection
        logger.info("Step 5: Connecting to Qdrant …")
        client = self._get_client()
        collection = client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        if self.clear:
            logger.info("Clearing existing collection …")
            client.delete_collection(self.collection_name)
            collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

        # 6. Embed and upsert
        logger.info("Step 6: Embedding and upserting …")
        model = self._get_model()
        texts = [c["text"] for c in chunks]

        logger.info("Encoding %d chunks …", len(texts))
        embeddings = model.encode(texts, show_progress_bar=True).tolist()

        for i in range(0, len(chunks), self.BATCH_SIZE):
            batch_chunks = chunks[i:i + self.BATCH_SIZE]
            batch_texts = texts[i:i + self.BATCH_SIZE]
            batch_embeddings = embeddings[i:i + self.BATCH_SIZE]

            collection.upsert(
                ids=[c["id"] for c in batch_chunks],
                documents=batch_texts,
                embeddings=batch_embeddings,
                metadatas=[c["metadata"] for c in batch_chunks],
            )
            self.stats["batches_upserted"] += 1

        self.stats["chunks_upserted"] = len(chunks)
        self._print_summary(t0)

    def _load_json(self, filename: str) -> Optional[list | dict]:
        """Load a JSON file from the temp directory."""
        path = self.temp_dir / filename
        if not path.exists():
            logger.warning("File not found: %s", path)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load %s: %s", path, exc)
            return None

    def _build_call_index(
        self, call_edges: list, functions: list
    ) -> Dict[str, List[str]]:
        """Build function_name → [callee_names] from call_edges.json."""
        # call_edges have caller_id and callee_id fields
        # Extract function name from function_id (last part after ::)
        id_to_name = {}
        for func in functions:
            fid = func.get("function_id", "")
            name = func.get("name", "")
            id_to_name[fid] = name

        calls_by_name: Dict[str, List[str]] = {}
        for edge in call_edges:
            caller_id = edge.get("caller_id", "")
            callee_id = edge.get("callee_id", "")
            caller_name = id_to_name.get(caller_id, "")
            # Callee might not be in our function list (external call)
            callee_name = id_to_name.get(callee_id, "")
            if not callee_name:
                # Extract name from callee_id (format: prefix::rel_path::func_name)
                parts = callee_id.rsplit("::", 1)
                callee_name = parts[-1] if parts else callee_id
            if caller_name and callee_name:
                calls_by_name.setdefault(caller_name, []).append(callee_name)

        # Deduplicate
        for name in calls_by_name:
            calls_by_name[name] = sorted(set(calls_by_name[name]))

        return calls_by_name

    def _build_chunks(
        self,
        functions: list,
        calls_by_func: Dict[str, List[str]],
        cs_info: Dict[str, tuple],
        source_dir: Path,
    ) -> List[dict]:
        """Build document chunks for all functions."""
        chunks = []

        for func in functions:
            name = func.get("name", "")
            start_line = func.get("start_line", 0)
            end_line = func.get("end_line", 0)
            file_id = func.get("_file_id", "")

            if not name or not start_line or not end_line or not file_id:
                self.stats["skipped_missing_data"] += 1
                continue

            # Resolve source file path
            source_path = source_dir / file_id
            if not source_path.exists():
                # Try without subdirectory prefix
                self.stats["skipped_file_not_found"] += 1
                logger.debug("Source file not found: %s", source_path)
                continue

            # Extract function body
            body = extract_function_body(source_path, start_line, end_line)
            if not body:
                self.stats["skipped_no_body"] += 1
                continue

            # Skip excessively long functions (auto-generated tables)
            if len(body) > self.MAX_BODY_CHARS:
                self.stats["skipped_too_long"] += 1
                logger.debug("Skipping %s (body=%d chars, exceeds max)", name, len(body))
                continue

            # Get call targets and CS info
            calls = calls_by_func.get(name, [])
            has_cs, cs_names = cs_info.get(name, (False, []))

            # Build document text and metadata
            doc_id = f"{self.module}_SRC_{name}"
            text = build_document_text(func, calls, body, self.module)
            metadata = build_metadata(func, calls, self.module, has_cs, cs_names)

            chunks.append({"id": doc_id, "text": text, "metadata": metadata})
            self.stats["functions_ingested"] += 1

            if metadata["is_static"] == "True":
                self.stats["static_functions"] += 1
            else:
                self.stats["global_functions"] += 1

        return chunks

    def _print_summary(self, t0: float):
        elapsed = time.time() - t0
        print(f"\n{'='*60}")
        print(f"  Source Code Qdrant Ingestion Summary — {self.module}")
        print(f"{'='*60}")
        print(f"    Collection: {self.collection_name}")
        for key, val in sorted(self.stats.items()):
            print(f"    {key}: {val}")
        print(f"    elapsed: {elapsed:.1f}s")
        print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest source code functions into Qdrant for semantic search"
    )
    parser.add_argument("--module", required=True,
                        help="MCAL module (e.g. ADC, DMA, GPT)")
    parser.add_argument("--temp-dir", default=None,
                        help="Override temp directory (default: temp/src_{module})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and build chunks only, no Qdrant writes")
    parser.add_argument("--clear", action="store_true",
                        help="Clear collection before ingesting")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    temp_dir = Path(args.temp_dir) if args.temp_dir else None

    ingestor = SourceCodeQdrantIngest(
        module=args.module,
        temp_dir=temp_dir,
        dry_run=args.dry_run,
        clear=args.clear,
    )
    ingestor.ingest()


if __name__ == "__main__":
    main()

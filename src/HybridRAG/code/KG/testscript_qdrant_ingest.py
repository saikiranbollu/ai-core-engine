"""
Test Script Qdrant Ingestion
==============================

Embeds parsed C test script functions into Qdrant for semantic search.
Resolves macro definitions from header files to enrich the text chunks
with human-readable context.

Collection naming: ``mcal_{module}_testscript``

Each test case and helper function becomes one vector document containing:
  - Description (from comment block)
  - Category and test_case_id
  - APIs tested (resolved from Test_* wrappers)
  - Resolved macros used in the body
  - Full function body (C code)

Usage::

    python testscript_qdrant_ingest.py \\
        --src temp/val_eth_leth/01_Implementation/src/Test_Eth_17_Leth.c \\
        --headers temp/val_eth_leth/01_Implementation/inc/Test_Eth_17_Leth.h \\
                  temp/val_eth_leth/01_Implementation/inc/Test_Eth_General.h \\
        --module ETH_17_LETH

    # Dry run (no Qdrant writes)
    python testscript_qdrant_ingest.py ... --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("testscript_qdrant_ingest")

# Path setup
SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code/KG
CODE_DIR = SCRIPT_DIR.parent                          # .../HybridRAG/code
RAG_DIR = CODE_DIR / "RAG"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(RAG_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_DIR))

try:
    from .testscript_parser import parse_test_script, ParseResult, ParsedFunction
except ImportError:
    from testscript_parser import parse_test_script, ParseResult, ParsedFunction


# ---------------------------------------------------------------------------
# Header Parser — Extract #define macros
# ---------------------------------------------------------------------------

# Match: #define NAME value  OR  #define NAME (value)
_DEFINE_RE = re.compile(
    r"^\s*#define\s+([A-Z][A-Z0-9_]+)\s+\(?([^)\n/]+)\)?\s*(?:/\*.*?\*/|//.*)?$",
    re.MULTILINE,
)


def parse_header_defines(header_paths: List[Path]) -> Dict[str, str]:
    """
    Parse #define macros from header files.
    Returns {MACRO_NAME: value_string}.
    Only captures simple value macros (not function-like macros).
    """
    defines = {}
    for path in header_paths:
        if not path.exists():
            logger.warning("Header not found: %s — skipping", path)
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for m in _DEFINE_RE.finditer(content):
            name = m.group(1)
            value = m.group(2).strip()
            # Skip internal/platform macros
            if name.startswith("IFX_") or name.startswith("_"):
                continue
            defines[name] = value
    logger.info("Parsed %d macro definitions from %d header files",
                len(defines), len(header_paths))
    return defines


# ---------------------------------------------------------------------------
# Text Builder — Build rich document text for embedding
# ---------------------------------------------------------------------------

def _resolve_macros_in_body(body: str, defines: Dict[str, str], max_resolves: int = 20) -> str:
    """
    Find macros used in the function body and build a 'Resolved constants'
    section that maps names → values for context.
    """
    resolved = []
    count = 0
    for macro_name, macro_value in defines.items():
        if count >= max_resolves:
            break
        # Check if the macro is actually used in the body
        if re.search(rf"\b{re.escape(macro_name)}\b", body):
            resolved.append(f"  {macro_name} = {macro_value}")
            count += 1
    return "\n".join(resolved) if resolved else ""


def build_document_text(
    func: ParsedFunction,
    defines: Dict[str, str],
    module: str,
) -> str:
    """
    Build the text document to embed for a single function.
    Structured for optimal semantic search.
    """
    parts = []

    # Header with identity
    if func.is_test_case:
        parts.append(f"Test Case: {func.test_case_id}")
        parts.append(f"Category: {func.category}")
    else:
        parts.append(f"Helper Function: {func.function_name}")
        parts.append(f"Type: {func.category}")

    parts.append(f"Module: {module}")

    # Description (natural language — highest semantic value)
    if func.description:
        parts.append(f"Description: {func.description}")

    # APIs tested
    if func.apis_called:
        parts.append(f"APIs tested: {', '.join(func.apis_called)}")

    # Config guards
    if func.cfg_guards:
        parts.append(f"Configuration indices: {', '.join(func.cfg_guards)}")

    # Input/output summary
    if func.is_test_case:
        parts.append(f"Input parameters: {func.input_param_count}")
        parts.append(f"Result assertions: {len(func.result_sends)}")

    # Resolved macros used in the body
    macro_section = _resolve_macros_in_body(func.body, defines)
    if macro_section:
        parts.append(f"Resolved constants:\n{macro_section}")

    # Full function body
    parts.append(f"Source code:\n{func.body}")

    return "\n".join(parts)


def build_metadata(
    func: ParsedFunction,
    module: str,
    source_file: str,
) -> Dict[str, str]:
    """Build metadata dict for Qdrant payload."""
    meta = {
        "module": module,
        "source_file": source_file,
        "function_name": func.function_name,
        "category": func.category,
        "line_start": str(func.line_start),
        "line_end": str(func.line_end),
        "is_test_case": str(func.is_test_case),
    }
    if func.test_case_id:
        meta["test_case_id"] = func.test_case_id
    if func.apis_called:
        meta["apis_called"] = ",".join(func.apis_called)
    if func.cfg_guards:
        meta["cfg_guards"] = ",".join(func.cfg_guards)
    if func.input_param_count:
        meta["input_param_count"] = str(func.input_param_count)
    if func.result_sends:
        meta["result_send_count"] = str(len(func.result_sends))
    return meta


# ---------------------------------------------------------------------------
# Qdrant Ingestion
# ---------------------------------------------------------------------------

class TestScriptQdrantIngest:
    """Embeds and upserts test script functions into Qdrant."""

    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    BATCH_SIZE = 64

    def __init__(
        self,
        src_path: Path,
        header_paths: List[Path],
        module: str,
        *,
        dry_run: bool = False,
        clear: bool = False,
    ):
        self.src_path = Path(src_path)
        self.header_paths = [Path(p) for p in header_paths]
        self.module = module.upper()
        self.dry_run = dry_run
        self.clear = clear
        self.collection_name = f"mcal_{self.module.lower()}_testscript"
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
        print(f"  Test Script Qdrant Ingestion — {self.module}")
        print(f"  Source: {self.src_path.name}")
        print(f"  Headers: {[p.name for p in self.header_paths]}")
        print(f"  Collection: {self.collection_name}")
        print(f"  Dry run: {self.dry_run}")
        print("=" * 60)

        # 1. Parse headers for macro definitions
        logger.info("Step 1: Parsing header defines …")
        defines = parse_header_defines(self.header_paths)
        print(f"  Parsed {len(defines)} macro definitions from headers")

        # 2. Parse the .c source file
        logger.info("Step 2: Parsing test script …")
        result = parse_test_script(self.src_path, module=self.module)
        print(f"  Parsed {len(result.test_cases)} test cases, {len(result.helpers)} helpers")

        # 3. Build document chunks
        logger.info("Step 3: Building document chunks …")
        chunks = []

        for func in result.test_cases:
            doc_id = f"{self.module}_TSCR_{func.function_name}"
            text = build_document_text(func, defines, self.module)
            metadata = build_metadata(func, self.module, self.src_path.name)
            chunks.append({"id": doc_id, "text": text, "metadata": metadata})

        for func in result.helpers:
            doc_id = f"{self.module}_TSCR_H_{func.function_name}"
            text = build_document_text(func, defines, self.module)
            metadata = build_metadata(func, self.module, self.src_path.name)
            chunks.append({"id": doc_id, "text": text, "metadata": metadata})

        print(f"  Built {len(chunks)} document chunks")
        self.stats["chunks_total"] = len(chunks)

        # Show sample
        if chunks:
            sample = chunks[0]
            preview = sample["text"][:200]
            print(f"\n  Sample chunk ({sample['id']}):")
            print(f"  {preview}…")
            print(f"  Text length: {len(sample['text'])} chars\n")

        if self.dry_run:
            print("  DRY RUN — no Qdrant writes.")
            self._print_summary(t0)
            return

        # 4. Get Qdrant client and collection
        logger.info("Step 4: Connecting to Qdrant …")
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

        # 5. Embed and upsert
        logger.info("Step 5: Embedding and upserting …")
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

    def _print_summary(self, t0: float):
        elapsed = time.time() - t0
        print(f"\n{'='*60}")
        print(f"  Qdrant Ingestion Summary — {self.module}")
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
    parser = argparse.ArgumentParser(description="Ingest test scripts into Qdrant")
    parser.add_argument("--src", required=True, help="Path to .c test script file")
    parser.add_argument("--headers", nargs="+", default=[], help="Paths to .h header files")
    parser.add_argument("--module", required=True, help="MCAL module (e.g. ETH_17_LETH)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no writes")
    parser.add_argument("--clear", action="store_true", help="Clear collection first")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(name)s: %(message)s")

    ingestor = TestScriptQdrantIngest(
        src_path=Path(args.src),
        header_paths=[Path(h) for h in args.headers],
        module=args.module,
        dry_run=args.dry_run,
        clear=args.clear,
    )
    ingestor.ingest()


if __name__ == "__main__":
    main()

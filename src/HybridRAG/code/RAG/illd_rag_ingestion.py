"""
ILLD RAG Vector Store Ingestion
================================

Embeds and upserts parser outputs into Qdrant collections for the ILLD
profile, following the unified collection naming convention from
``collection_naming_unified.py``.

Collections created per module (e.g. module = "cxpi")::

    rag_cxpi_functions       – SWA function prototypes
    rag_cxpi_structs         – SWA struct definitions
    rag_cxpi_enums           – SWA enum definitions
    rag_cxpi_typedefs        – SWA typedef definitions
    rag_cxpi_macros          – SWA macro definitions
    rag_cxpi_requirements    – Jama requirements
    rag_cxpi_hardware        – HW spec (registers, fields, interrupts, errors)
    rag_cxpi_registers       – SFR register definitions + bitfields
    rag_cxpi_source          – C source implementation analysis
    rag_cxpi_architecture    – HW spec markdown sections (leaf-section chunks)
    rag_cxpi_pattern_library – PUML patterns (core_functions + phase_patterns)

Uses:
- ``vector_store_factory.get_vector_client`` for Qdrant connection
- ``sentence-transformers/all-MiniLM-L6-v2`` (384-dim) for embeddings

Usage::

    from RAG.illd_rag_ingestion import ILLDRAGIngestor

    ingestor = ILLDRAGIngestor(module="CXPI")
    ingestor.ingest_swa(swa_data)
    ingestor.ingest_sfr(sfr_data)
    ingestor.ingest_hw_spec(hw_data)
    ingestor.ingest_requirements(requirements)
    ingestor.ingest_source(c_data)
    ingestor.ingest_puml(puml_data)
    ingestor.ingest_hw_spec_markdown(md_path)
    ingestor.print_summary()
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("illd_rag_ingestion")

# Matches TOC dot-leader lines: "1.4.1 Functional overview ..... 42"
# Used to guard _chunk_leaf_sections() against treating TOC entries as headings.
_TOC_LINE_RE = re.compile(r"^\d+(?:\.\d+)*\s+.+\.{4,}")


class ILLDRAGIngestor:
    """
    Embeds and upserts ILLD parser outputs into Qdrant.

    Each ``ingest_*`` method takes the same dict returned by its
    corresponding parser and creates embeddings + metadata in the
    appropriate Qdrant collection.
    """

    def __init__(
        self,
        module: str,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        instance: str = "illd",
        dry_run: bool = False,
        clear: bool = False,
    ):
        self.module = module.upper()
        self.module_lower = module.strip().lower()
        self.dry_run = dry_run
        self.stats: Dict[str, int] = Counter()

        if not dry_run:
            self._model = self._load_model(model_name)
            self._client = self._get_client(instance)
            if clear:
                self._clear_collection()
        else:
            self._model = None
            self._client = None

    @staticmethod
    def _load_model(model_name: str):
        """Load the sentence-transformer embedding model."""
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s", model_name)
        return SentenceTransformer(model_name)

    @staticmethod
    def _get_client(instance: str):
        """Get Qdrant client via the unified factory."""
        import sys
        script_dir = Path(__file__).resolve().parent.parent  # .../HybridRAG/code
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        rag_dir = Path(__file__).resolve().parent
        if str(rag_dir) not in sys.path:
            sys.path.insert(0, str(rag_dir))
        from vector_store_factory import get_vector_client
        return get_vector_client(instance=instance)

    # -- Helpers ------------------------------------------------------------

    def _clear_collection(self):
        """Delete and recreate the module's Qdrant collection."""
        logger.warning("Clearing Qdrant collection '%s' …", self.module_lower)
        try:
            self._client.delete_collection(self.module_lower)
            logger.info("Collection '%s' deleted.", self.module_lower)
        except Exception:
            logger.info("Collection '%s' did not exist — nothing to clear.", self.module_lower)

    def _collection(self, content_type: str):
        """Get or create a single Qdrant collection per module."""
        return self._client.get_or_create_collection(self.module_lower)

    def _embed_and_upsert(self, content_type: str, chunks: List[dict]):
        """Embed chunks and upsert into the target collection.

        Each chunk must have ``id``, ``text``, and optionally ``metadata``.
        """
        if not chunks:
            return 0

        # Validate: every chunk must have a non-empty string ID
        chunks = [c for c in chunks if c.get("id") and isinstance(c["id"], str)]
        if not chunks:
            logger.warning("  No valid chunks for %s (all missing 'id')", content_type)
            return 0

        if self.dry_run:
            logger.info("  [DRY RUN] Would upsert %d chunks → rag_%s_%s",
                       len(chunks), self.module_lower, content_type)
            self.stats[content_type] += len(chunks)
            return len(chunks)

        collection = self._collection(content_type)
        texts = [c["text"] for c in chunks]
        embeddings = self._model.encode(texts).tolist()

        # Prefix chunk IDs with module name for uniqueness
        prefix = f"{self.module_lower}_"
        for c in chunks:
            if not c["id"].startswith(prefix):
                c["id"] = prefix + c["id"]

        # Sanitize metadata (Qdrant requires str/int/float/bool values)
        metadatas = []
        for c in chunks:
            meta = c.get("metadata", {})
            meta["category"] = content_type
            meta["module"] = self.module
            sanitized = {}
            for k, v in meta.items():
                if v is None:
                    continue
                if not isinstance(v, (str, int, float, bool)):
                    v = str(v)
                sanitized[k] = v
            metadatas.append(sanitized)

        collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        self.stats[content_type] += len(chunks)
        return len(chunks)

    # =====================================================================
    # 1. SWA ingestion
    # =====================================================================

    def ingest_swa(self, swa_data: dict, source_file: str = None):
        """Ingest SWA parser output into functions, structs, enums,
        typedefs, and macros collections.

        *source_file* — originating header filename, stored in metadata.
        """
        if not swa_data:
            return

        logger.info("Ingesting SWA data into Qdrant (file=%s) …",
                    source_file or "default")

        # -- Functions --
        for func in (swa_data.get("functions") or []):
            if not isinstance(func, dict) or not func.get("name"):
                continue
            fname = func["name"]
            rtype = func.get("return_type", "void") or "void"
            brief = func.get("brief", "") or ""
            purpose = func.get("purpose", "") or ""

            params = func.get("parameters") or []
            param_text = ", ".join(
                f"{p.get('type', 'unknown')} {p.get('name', 'param')}"
                for p in params if isinstance(p, dict)
            )
            deps = func.get("dependencies") or []
            deps_text = ", ".join(str(d) for d in deps if d) or "none"

            text = (f"{rtype} {fname}({param_text})\n"
                    f"Brief: {brief}\n"
                    f"Purpose: {purpose}\n"
                    f"Dependencies: {deps_text}")

            meta = {"type": "function", "function": fname, "return_type": rtype}
            if source_file:
                meta["source_file"] = source_file
            self._embed_and_upsert("functions", [{
                "id": f"func_{fname}",
                "text": text,
                "metadata": meta,
            }])

        # -- Structs --
        for s in (swa_data.get("structs") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            sname = s["name"]
            brief = s.get("brief", "") or ""
            purpose = s.get("purpose", "") or ""
            members = s.get("members") or []
            member_text = ", ".join(
                f"{m.get('type', 'unknown') or 'unknown'} {m.get('name', 'field') or 'field'}: "
                f"{m.get('description', '') or ''}"
                for m in members if isinstance(m, dict)
            )
            text = (f"struct {sname}\nBrief: {brief}\n"
                    f"Purpose: {purpose}\n"
                    f"Members: {member_text}")

            s_meta = {"type": "struct", "struct": sname}
            if source_file:
                s_meta["source_file"] = source_file
            self._embed_and_upsert("structs", [{
                "id": f"struct_{sname}",
                "text": text,
                "metadata": s_meta,
            }])

        # -- Enums --
        for e in (swa_data.get("enums") or []):
            if not isinstance(e, dict) or not e.get("name"):
                continue
            ename = e["name"]
            brief = e.get("brief", "") or ""
            purpose = e.get("purpose", "") or ""
            vals = e.get("values") or []
            val_text = ", ".join(
                f"{v.get('name', 'unnamed') or 'unnamed'}={v.get('value', '') or ''}: "
                f"{v.get('description', '') or ''}"
                for v in vals if isinstance(v, dict)
            )
            text = (f"enum {ename}\nBrief: {brief}\n"
                    f"Purpose: {purpose}\nValues: {val_text}")

            e_meta = {"type": "enum", "enum": ename}
            if source_file:
                e_meta["source_file"] = source_file
            self._embed_and_upsert("enums", [{
                "id": f"enum_{ename}",
                "text": text,
                "metadata": e_meta,
            }])

        # -- Typedefs --
        for td in (swa_data.get("typedefs") or []):
            if not isinstance(td, dict) or not td.get("name"):
                continue
            tname = td["name"]
            ttype = td.get("type", "") or ""
            brief = td.get("brief", "") or ""
            text = (f"typedef {tname}\nDefinition: {ttype}\n"
                    f"Description: {brief}")

            td_meta = {"type": "typedef", "name": tname}
            if source_file:
                td_meta["source_file"] = source_file
            self._embed_and_upsert("typedefs", [{
                "id": f"typedef_{tname}",
                "text": text,
                "metadata": td_meta,
            }])

        # -- Macros --
        for macro in (swa_data.get("macros") or []):
            if not isinstance(macro, dict) or not macro.get("name"):
                continue
            mname = macro["name"]
            mval = macro.get("value", "") or ""
            desc = macro.get("description", "") or ""
            text = (f"macro {mname}\nValue: {mval}\n"
                    f"Description: {desc}")

            m_meta = {"type": "macro", "name": mname}
            if source_file:
                m_meta["source_file"] = source_file
            self._embed_and_upsert("macros", [{
                "id": f"macro_{mname}",
                "text": text,
                "metadata": m_meta,
            }])

        logger.info("SWA Qdrant ingestion complete.")

    # =====================================================================
    # 2. SFR ingestion
    # =====================================================================

    def ingest_sfr(self, sfr_data: dict, source_file: str = None):
        """Ingest SFR parser output → registers collection."""
        if not sfr_data:
            return

        logger.info("Ingesting SFR data into Qdrant (file=%s) …",
                     source_file or "default")
        chunks = []
        registers = sfr_data.get("registers", {})

        for reg_name, bitfields in registers.items():
            if not isinstance(bitfields, list):
                continue

            bf_lines = []
            bitfield_access = {}
            for bf in bitfields:
                if isinstance(bf, dict):
                    label  = bf.get("label", "") or ""
                    bfdesc = bf.get("description", "") or ""
                    bf_lines.append(f"  - {label}: {bfdesc}")
                    # Collect per-bitfield access info for payload (skip Reserved entries)
                    bfname = bf.get("name", "")
                    aq = bf.get("access_qualifier")
                    at = bf.get("access_type")
                    if bfname and not bfname.startswith("Reserved") and aq:
                        entry = {"access_qualifier": aq}
                        if at:
                            entry["access_type"] = at
                        bitfield_access[bfname] = entry

            bf_text = "\n".join(bf_lines) if bf_lines else "No bitfields"
            text = (f"Register: {reg_name}\n"
                    f"Bitfields:\n{bf_text}")

            metadata = {
                    "type": "register_def",
                    "register": str(reg_name),
                    "num_bitfields": len([bf for bf in bitfields if isinstance(bf, dict)]),
                }
            if source_file:
                metadata["source_file"] = source_file
            if bitfield_access:
                metadata["bitfield_access"] = bitfield_access

            chunks.append({
                "id": f"reg_{reg_name}",
                "text": text,
                "metadata": metadata,
            })

        self._embed_and_upsert("registers", chunks)
        logger.info("SFR Qdrant ingestion complete: %d register chunks", len(chunks))

    # =====================================================================
    # 3. HW Spec ingestion
    # =====================================================================

    def ingest_hw_spec(self, hw_data: dict):
        """Ingest HW spec parser structured output → hardware collection."""
        if not hw_data:
            return

        logger.info("Ingesting HW spec data into Qdrant …")
        chunks = []

        # Registers
        for reg in (hw_data.get("registers") or []):
            if not isinstance(reg, dict):
                continue
            rname = reg.get("name", "")
            if not rname:
                continue
            text = (f"HW Register: {rname}\n"
                    f"Long name: {reg.get('long_name', '')}\n"
                    f"Offset: {reg.get('offset', '')}\n"
                    f"Reset value: {reg.get('reset_value', '')}")
            chunks.append({
                "id": f"hwreg_{rname}",
                "text": text,
                "metadata": {"type": "hw_register", "name": rname},
            })

        # Fields
        for f in (hw_data.get("fields") or []):
            if not isinstance(f, dict):
                continue
            fname = f.get("name", "")
            parent = f.get("parent_register", "")
            if not fname:
                continue
            text = (f"Register Field: {fname}\n"
                    f"Parent: {parent}\n"
                    f"Bits: {f.get('bits', '')}\n"
                    f"Access: {f.get('type', '')}\n"
                    f"Description: {f.get('description', '')}")
            chunks.append({
                "id": f"hwfield_{parent}_{fname}",
                "text": text,
                "metadata": {"type": "hw_field", "name": fname, "register": parent},
            })

        # Interrupts
        for intr in (hw_data.get("interrupts") or []):
            if not isinstance(intr, dict):
                continue
            iname = intr.get("name", "")
            if not iname:
                continue
            text = (f"Interrupt: {iname}\n"
                    f"Register: {intr.get('register', '')}\n"
                    f"Bit: {intr.get('bit', '')}\n"
                    f"Description: {intr.get('description', '')}")
            chunks.append({
                "id": f"int_{iname}",
                "text": text,
                "metadata": {"type": "interrupt", "name": iname},
            })

        # Errors
        for err in (hw_data.get("errors") or []):
            if not isinstance(err, dict):
                continue
            ename = err.get("name", "")
            if not ename:
                continue
            text = (f"Error: {ename}\n"
                    f"Type: {err.get('type', '')}\n"
                    f"Detected in: {err.get('detected_in', '')}\n"
                    f"Description: {err.get('description', '')}")
            chunks.append({
                "id": f"err_{ename}",
                "text": text,
                "metadata": {"type": "error", "name": ename},
            })

        self._embed_and_upsert("hardware", chunks)
        logger.info("HW spec Qdrant ingestion complete: %d chunks", len(chunks))

    # =====================================================================
    # 4. Requirements ingestion
    # =====================================================================

    def ingest_requirements(self, requirements: list):
        """Ingest Jama requirements → requirements collection."""
        if not requirements:
            return

        logger.info("Ingesting %d requirements into Qdrant …", len(requirements))
        chunks = []

        for req in requirements:
            if hasattr(req, "document_key"):
                # JamaItem object
                req_id = req.document_key or f"req_{req.id}"
                name = req.name or "Unknown"
                desc = req.description or ""
                item_type = getattr(req, "item_type", "") or "Requirement"
                status_text = getattr(req, "status_text", "") or ""
                status_id = getattr(req, "status", None)
            elif isinstance(req, dict):
                req_id = req.get("document_key") or req.get("requirement_id") or f"req_{req.get('id', '')}"
                name = req.get("name", "Unknown")
                desc = req.get("description", "")
                item_type = req.get("item_type", "") or req.get("Item Type", "") or "Requirement"
                status_text = req.get("status_text", "") or ""
                status_id = req.get("status", None)
            else:
                continue

            if not name or name == "Unknown":
                continue

            text = f"{item_type}: {name}\n{desc}"
            metadata = {"type": "requirement", "name": str(name),
                        "item_type": str(item_type)}
            if status_text:
                metadata["status"] = str(status_text)
            if status_id is not None and status_id != -1:
                metadata["status_id"] = int(status_id)
            chunks.append({
                "id": req_id,
                "text": text,
                "metadata": metadata,
            })

        self._embed_and_upsert("requirements", chunks)
        logger.info("Requirements Qdrant ingestion complete: %d chunks", len(chunks))

    # =====================================================================
    # 5. Source Code ingestion
    # =====================================================================

    def ingest_source(self, c_data: dict, source_file: str = None):
        """Ingest C parser output → source collection.

        *source_file* — originating .c filename, stored in metadata.
        """
        if not c_data:
            return

        logger.info("Ingesting C source analysis into Qdrant (file=%s) …",
                    source_file or "default")
        chunks = []
        functions = c_data.get("functions", {})

        for func_name, func_info in functions.items():
            if not isinstance(func_info, dict):
                continue

            start_line = func_info.get("start_line", "") or ""
            reg_accesses = func_info.get("register_accesses") or []
            access_lines = []
            for acc in reg_accesses:
                if isinstance(acc, dict):
                    register = acc.get("register", "") or ""
                    field = acc.get("field", "") or ""
                    access_type = acc.get("access_type", "") or ""
                    line = acc.get("line", "") or ""
                    access_lines.append(
                        f"  - {register}.{field}: {access_type} (line {line})"
                    )

            accesses_text = "\n".join(access_lines) if access_lines else "No register accesses"
            text = (f"Function: {func_name}\n"
                    f"Start Line: {start_line}\n"
                    f"Register Accesses:\n{accesses_text}")

            src_meta = {
                "type": "source_implementation",
                "function": str(func_name),
                "start_line": str(start_line),
                "num_register_accesses": len([a for a in reg_accesses if isinstance(a, dict)]),
            }
            if source_file:
                src_meta["source_file"] = source_file
            chunks.append({
                "id": f"impl_{func_name}",
                "text": text,
                "metadata": src_meta,
            })

        self._embed_and_upsert("source", chunks)
        logger.info("Source Qdrant ingestion complete: %d chunks", len(chunks))

    # =====================================================================
    # 6. PUML ingestion
    # =====================================================================

    def ingest_puml(self, puml_data: dict):
        """Ingest PUML parser output → pattern_library collection (2 chunks)."""
        if not puml_data:
            return

        logger.info("Ingesting PUML patterns into Qdrant …")
        chunks = []

        # Chunk 1: core_functions
        core = puml_data.get("core_functions", {})
        if core:
            always = core.get("always_present", []) or []
            freq = core.get("frequently_present", []) or []
            rare = core.get("rare", []) or []
            summary = (f"Core Functions Library:\n"
                       f"  Always Present: {len(always)} functions\n"
                       f"  Frequently Present: {len(freq)} functions\n"
                       f"  Rare: {len(rare)} functions\n\n"
                       f"Functions:\n{json.dumps(core, indent=2)}")
            chunks.append({
                "id": "puml_core_functions",
                "text": summary,
                "metadata": {
                    "type": "puml_pattern_library",
                    "category": "core_functions",
                    "description": "Complete core function definitions",
                },
            })

        # Chunk 2: phase_patterns
        phases = puml_data.get("phase_patterns", {})
        if phases:
            phase_names = list(phases.keys()) if isinstance(phases, dict) else []
            summary = (f"Phase Patterns Library:\n"
                       f"  Total Phases: {len(phase_names)}\n"
                       f"  Phases: {', '.join(phase_names)}\n\n"
                       f"{json.dumps(phases, indent=2)}")
            chunks.append({
                "id": "puml_phase_patterns",
                "text": summary,
                "metadata": {
                    "type": "puml_pattern_library",
                    "category": "phase_patterns",
                    "description": "Complete phase pattern definitions",
                },
            })

        if chunks:
            self._embed_and_upsert("pattern_library", chunks)

        logger.info("PUML Qdrant ingestion complete: %d chunks", len(chunks))

    # =====================================================================
    # 7. HW Spec Markdown (leaf-section chunking for architecture)
    # =====================================================================

    def ingest_hw_spec_markdown(self, md_path: Path):
        """
        Chunk HW spec markdown by leaf sections and ingest into
        the architecture collection.

        Uses universal leaf-section chunking (max 3 levels: x.x.x).
        """
        if not md_path or not md_path.exists():
            logger.warning("HW spec markdown not found: %s", md_path)
            return

        logger.info("Ingesting HW spec markdown: %s", md_path.name)

        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        if not content:
            return

        chunks = self._chunk_leaf_sections(content)
        if chunks:
            self._embed_and_upsert("architecture", chunks)
        logger.info("Architecture Qdrant ingestion complete: %d chunks", len(chunks))

    def _chunk_leaf_sections(self, content: str) -> List[dict]:
        """
        Universal leaf-section chunking (max 3 levels).

        Rule:
        - Depth 1-2 without children → chunk
        - Depth 3 → always chunk (merge subsections into it)
        """
        lines = content.split("\n")

        # Parse all section numbers
        all_sections = []
        section_re = re.compile(r"^(?:#+\s*)?(\d+(?:\.\d+)*)\s+(.+?)$")
        for line in lines:
            m = section_re.match(line.strip())
            if not m:
                continue
            if _TOC_LINE_RE.match(line.strip()):  # skip dot-leader TOC entries
                continue
            sec_num = m.group(1)
            sec_title = m.group(2).strip()
            parts = sec_num.split(".")
            # Cap to 3 levels
            capped = ".".join(parts[:3])
            depth = len(capped.split("."))
            all_sections.append({"number": sec_num, "capped": capped,
                                  "title": sec_title, "depth": depth})

        # Identify chunkable sections
        chunkable = set()
        for sec in all_sections:
            capped = sec["capped"]
            depth = sec["depth"]
            if depth == 3:
                chunkable.add(capped)
            elif depth <= 2:
                has_children = any(
                    o["capped"].startswith(capped + ".") and o["capped"] != capped
                    for o in all_sections
                )
                if not has_children:
                    chunkable.add(capped)

        # Build chunks
        chunks = []
        seen = set()
        current_section = None
        current_title = ""
        current_content = []

        for line in lines:
            m = section_re.match(line.strip())
            if m and not _TOC_LINE_RE.match(line.strip()):  # skip dot-leader TOC entries
                sec_num = m.group(1)
                sec_title = m.group(2).strip()
                capped = ".".join(sec_num.split(".")[:3])

                # Save previous
                if current_section and current_section in chunkable:
                    chunk_id = f"hw_spec_{current_section.replace('.', '_')}"
                    if chunk_id not in seen:
                        body = "\n".join(current_content).strip()
                        if body:  # never emit empty-body chunks
                            text = f"Section {current_section} {current_title}\n\n{body}"
                            chunks.append({
                                "id": chunk_id,
                                "text": text,
                                "metadata": {
                                    "type": "hardware_spec",
                                    "section": f"{current_section} {current_title}",
                                    "section_number": current_section,
                                },
                            })
                            seen.add(chunk_id)

                current_section = capped
                current_title = sec_title
                current_content = []
            elif line.strip():
                current_content.append(line)

        # Last chunk
        if current_section and current_section in chunkable:
            chunk_id = f"hw_spec_{current_section.replace('.', '_')}"
            if chunk_id not in seen:
                body = "\n".join(current_content).strip()
                if body:  # never emit empty-body chunks
                    text = f"Section {current_section} {current_title}\n\n{body}"
                    chunks.append({
                        "id": chunk_id,
                        "text": text,
                        "metadata": {
                            "type": "hardware_spec",
                            "section": f"{current_section} {current_title}",
                            "section_number": current_section,
                        },
                    })

        return chunks

    # =====================================================================
    # Summary
    # =====================================================================

    def print_summary(self):
        """Print ingestion statistics."""
        print("\n" + "=" * 60)
        print(f"  ILLD RAG INGESTION COMPLETE - Module: {self.module}")
        print(f"  Collection: {self.module_lower}")
        print("=" * 60)
        total = 0
        for category, count in sorted(self.stats.items()):
            print(f"    {category:<40s}  {count:>6,d} chunks")
            total += count
        print(f"    {'TOTAL':<41s}  {total:>6,d}")
        print("=" * 60 + "\n")
        return dict(self.stats)

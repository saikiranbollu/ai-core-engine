"""
Ingestion Service — Sprint 5
==============================
Backend for Category 5 (Ingestion Pipeline) tools.
Designed for Celery async execution (task wrappers added when Celery is available).

Supports: .c, .h, .json, .rst, .puml, .pdf, .xlsx, .arxml, .md, .txt
Uses the same parser dispatch pattern as the old repo's ingest_file tool.

Sprint 5 scope: synchronous execution with async-ready interface.
Celery task wrappers will be added when Redis+Celery workers are deployed.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._common.path_safety import allowed_roots_from_env, safe_path_under

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
#  Job Tracker — tracks ingestion job status
# ═════════════════════════════════════════════════════════════════════════

class IngestionJobTracker:
    """Track async ingestion job statuses. Sprint 8: PostgreSQL write-through."""

    def __init__(self, postgres_client=None):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._pg = postgres_client  # Optional PostgresClient for write-through

    def create_job(self, job_type: str, params: Dict) -> str:
        job_id = f"ingest_{uuid.uuid4().hex[:10]}"
        self._jobs[job_id] = {
            "job_id": job_id, "type": job_type, "status": "queued",
            "params": params, "created_at": time.time(),
            "progress": 0, "result": None, "error": None,
        }
        if self._pg:
            self._pg.save_ingestion_job(job_id=job_id, job_type=job_type, params=params)
        return job_id

    def update(self, job_id: str, **kwargs):
        if job_id in self._jobs:
            self._jobs[job_id].update(kwargs)
            if self._pg:
                self._pg.update_ingestion_job(
                    job_id=job_id,
                    status=kwargs.get("status"),
                    progress=kwargs.get("progress"),
                )

    def update_progress(self, job_id: str, progress: int):
        """Update job progress percentage (0-100)."""
        self.update(job_id, status="processing", progress=min(progress, 100))

    def get(self, job_id: str) -> Optional[Dict]:
        return self._jobs.get(job_id)

    def complete(self, job_id: str, result: Dict):
        if job_id in self._jobs:
            self._jobs[job_id].update(status="completed", progress=100, result=result)
            if self._pg:
                self._pg.update_ingestion_job(
                    job_id=job_id, status="completed", progress=100, result=result,
                )

    def fail(self, job_id: str, error: str):
        if job_id in self._jobs:
            self._jobs[job_id].update(status="failed", error=error)
            if self._pg:
                self._pg.update_ingestion_job(job_id=job_id, status="failed", error=error)


# ═════════════════════════════════════════════════════════════════════════
#  Ingestion Service
# ═════════════════════════════════════════════════════════════════════════

SUPPORTED_EXTENSIONS = {
    ".c", ".h", ".json", ".rst", ".puml", ".pdf", ".xlsx",
    ".arxml", ".md", ".txt", ".csv", ".eap", ".eaps", ".qeax",
}

CANONICAL_REQUIREMENT_LABEL = "SoftwareRequirement"
CANONICAL_FUNCTION_LABEL = "APIFunction"

NEO4J_BATCH_FLUSH_THRESHOLD = 500


class Neo4jBatchWriter:
    """Accumulates node MERGE operations and flushes via UNWIND for performance.

    Usage::

        with Neo4jBatchWriter(session, workspace, module) as writer:
            writer.add_node("APIFunction", {"name": "Foo"}, {"name": "Foo", ...})
            writer.add_relationship("Foo", "Bar", "CALLS_INTERNALLY")
        # auto-flushes on exit
    """

    def __init__(self, session, workspace: str, module: str,
                 op: str = "MERGE", flush_threshold: int = NEO4J_BATCH_FLUSH_THRESHOLD):
        self._session = session
        self._workspace = workspace
        self._module = module
        self._op = op
        self._threshold = flush_threshold
        self._node_set_id = f"ns_{workspace}_{module}"
        self._created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Buffers keyed by label
        self._nodes: Dict[str, List[Dict[str, Any]]] = {}
        self._rels: List[Dict[str, Any]] = []
        self._nodes_written = 0
        self._rels_written = 0

    # -- public api ---------------------------------------------------------

    def add_node(self, label: str, identity: Dict[str, Any],
                 properties: Dict[str, Any]) -> None:
        buf = self._nodes.setdefault(label, [])
        buf.append({"identity": identity, "props": properties})
        if sum(len(v) for v in self._nodes.values()) >= self._threshold:
            self.flush_nodes()

    def add_relationship(self, from_name: str, to_name: str,
                         rel_type: str, from_label: str = CANONICAL_FUNCTION_LABEL,
                         to_label: str = CANONICAL_FUNCTION_LABEL) -> None:
        self._rels.append({
            "from_name": from_name, "to_name": to_name,
            "rel_type": rel_type, "from_label": from_label,
            "to_label": to_label,
        })
        if len(self._rels) >= self._threshold:
            self.flush_rels()

    def flush(self) -> None:
        self.flush_nodes()
        self.flush_rels()

    def flush_nodes(self) -> None:
        for label, items in self._nodes.items():
            if not items:
                continue
            # Build UNWIND MERGE query — all items share the same label
            # Identity keys are the same for all items of a given label
            identity_keys = list(items[0]["identity"].keys())
            identity_clause = ", ".join(f"{k}: item.{k}" for k in identity_keys)

            # Flatten each item into a single dict for UNWIND
            rows = []
            for item in items:
                row = dict(item["identity"])
                row["_props"] = item["props"]
                rows.append(row)

            query = f"""
                MERGE (ns:NodeSet {{id: $node_set_id}})
                ON CREATE SET ns.project = $project, ns.module = $module,
                              ns.status = 'active', ns.created_at = $created_at
                ON MATCH SET ns.status = 'active'
                WITH ns
                UNWIND $rows AS item
                {self._op} (n:{label} {{{identity_clause}}})
                SET n += item._props
                MERGE (ns)-[:HAS_MODULE]->(n)
            """
            self._session.run(query, {
                "rows": rows,
                "node_set_id": self._node_set_id,
                "project": self._workspace,
                "module": self._module,
                "created_at": self._created_at,
            })
            self._nodes_written += len(items)
            logger.debug("[Neo4jBatch] Flushed %d %s nodes", len(items), label)

        self._nodes.clear()

    def flush_rels(self) -> None:
        if not self._rels:
            return
        # Group by (rel_type, from_label, to_label)
        groups: Dict[tuple, List[Dict]] = {}
        for r in self._rels:
            key = (r["rel_type"], r["from_label"], r["to_label"])
            groups.setdefault(key, []).append(r)

        for (rel_type, from_label, to_label), items in groups.items():
            rows = [{"from_name": r["from_name"], "to_name": r["to_name"]}
                    for r in items]
            query = f"""
                UNWIND $rows AS item
                MATCH (a:{from_label} {{name: item.from_name, module: $module}})
                MATCH (b:{to_label} {{name: item.to_name, module: $module}})
                {self._op} (a)-[:{rel_type}]->(b)
            """
            self._session.run(query, {"rows": rows, "module": self._module})
            self._rels_written += len(items)
            logger.debug("[Neo4jBatch] Flushed %d %s rels", len(items), rel_type)

        self._rels.clear()

    @property
    def stats(self) -> tuple:
        return self._nodes_written, self._rels_written

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.flush()


class IngestionService:
    """
    Orchestrates file and module ingestion into the knowledge graph.

    Parameters
    ----------
    neo4j_driver : optional
        Connected Neo4j driver for KG writes.
    job_tracker : IngestionJobTracker
        Tracks job status for async reporting.
    on_module_ingested : callable, optional
        Callback ``(module_name: str, workspace_id: str) -> None`` invoked
        after successful ingestion of a module.  Used by the MCP layer to
        trigger cache invalidation without coupling the ingestion service
        to cache internals.
    """

    def __init__(self, neo4j_driver=None, job_tracker: Optional[IngestionJobTracker] = None,
                 on_module_ingested=None):
        self._neo4j = neo4j_driver
        self._tracker = job_tracker or IngestionJobTracker()
        self._on_module_ingested = on_module_ingested

    def ingest_file(self, file_path: str, module_name: str,
                    overwrite: bool = False, workspace_id: str = "illd") -> Dict[str, Any]:
        """Parse a single file and ingest into KG."""
        # F-CA-I01: contain the input under an allowed root (symlink + traversal
        # safe) BY DEFAULT. Operators/tests widen the roots via
        # INGEST_ALLOWED_ROOTS; when unset, only /data and /repos are accepted so
        # arbitrary absolute paths (e.g. /etc/passwd) cannot be ingested.
        roots = allowed_roots_from_env("INGEST_ALLOWED_ROOTS", ["/data", "/repos"])
        try:
            p = safe_path_under(file_path, roots)
        except ValueError as exc:
            raise ValueError(f"Rejected file path: {exc}") from exc

        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}")

        job_id = self._tracker.create_job("ingest_file", {"file": file_path, "module": module_name})
        self._tracker.update(job_id, status="processing", progress=10)

        try:
            # Parse
            parsed = self._parse_file(p, ext)
            self._tracker.update(job_id, progress=50)

            # Write to KG (placeholder — full KG builder integration needed)
            nodes_created = 0
            rels_created = 0
            if self._neo4j and parsed:
                nodes_created, rels_created = self._write_to_kg(parsed, module_name, workspace_id, overwrite)
            self._tracker.update(job_id, progress=90)

            result = {
                "status": "completed", "file": str(p), "module_name": module_name,
                "extension": ext, "nodes_created": nodes_created,
                "relationships_created": rels_created, "job_id": job_id,
            }
            self._tracker.complete(job_id, result)
            self._fire_module_ingested(module_name, workspace_id)
            return result

        except Exception as e:
            self._tracker.fail(job_id, str(e))
            raise

    def ingest_module(self, repo_root: str, module_name: str,
                      workspace_id: str = "illd") -> Dict[str, Any]:
        """Discover and ingest all artifacts for a module.

        When *workspace_id* is ``"illd"``, the specialised 7-step ILLD
        pipeline is used instead of generic file-by-file ingestion.
        """
        job_id = self._tracker.create_job(
            "ingest_module", {"repo": repo_root, "module": module_name},
        )

        # ── ILLD workspace → delegate to the specialised pipeline ──
        if workspace_id == "illd":
            return self._ingest_illd_module(repo_root, module_name, job_id)

        # ── Generic file-by-file ingestion (non-ILLD workspaces) ──
        root = Path(repo_root)
        if not root.exists():
            raise FileNotFoundError(f"Repo root not found: {repo_root}")

        self._tracker.update(job_id, status="discovering")
        files = self._discover_module_files(root, module_name)
        total = len(files)
        self._tracker.update(job_id, status="processing", progress=10)

        results = {"files_found": total, "files_processed": 0,
                    "total_nodes": 0, "total_relationships": 0, "errors": []}

        for i, fp in enumerate(files):
            try:
                r = self.ingest_file(str(fp), module_name, workspace_id=workspace_id)
                results["files_processed"] += 1
                results["total_nodes"] += r.get("nodes_created", 0)
                results["total_relationships"] += r.get("relationships_created", 0)
            except Exception as e:
                results["errors"].append({"file": str(fp), "error": str(e)})
            progress = 10 + int(80 * (i + 1) / max(total, 1))
            self._tracker.update(job_id, progress=progress)

        results["job_id"] = job_id
        results["module_name"] = module_name
        self._tracker.complete(job_id, results)
        self._fire_module_ingested(module_name, workspace_id)
        return results

    # ── ILLD-specific ingestion ────────────────────────────────────────

    def _ingest_illd_module(self, repo_root: str, module_name: str,
                            job_id: str) -> Dict[str, Any]:
        """Run the full 7-step ILLD pipeline for a single module.

        Steps: SWA → SFR → C Source → PlantUML → HW Spec PDF →
               Jama Requirements → Cross-relationships.
        """
        import sys
        pipeline_dir = str(Path(__file__).resolve().parents[1] / "HybridRAG" / "code")
        if pipeline_dir not in sys.path:
            sys.path.insert(0, pipeline_dir)
        from illd_run_pipeline import ILLDPipeline

        self._tracker.update(job_id, status="illd_pipeline_starting", progress=5)
        logger.info("[ILLD] Starting specialised pipeline for module %s", module_name)

        pipeline = ILLDPipeline(
            module=module_name,
            remote=True,
            cleanup_temp=False,
        )

        self._tracker.update(job_id, status="illd_pipeline_running", progress=10)
        pipeline.run(save_intermediary=True)

        # Gather results from the KG builder and RAG ingestor stats
        kg_stats = pipeline.kg.stats if pipeline.kg else {}
        total_nodes = sum(v for k, v in kg_stats.items() if k.startswith("nodes:"))
        total_rels = sum(v for k, v in kg_stats.items() if k.startswith("rel:"))

        rag_stats = pipeline.rag.stats if pipeline.rag else {}
        total_chunks = sum(rag_stats.values())

        results = {
            "status": "completed",
            "module_name": module_name.upper(),
            "workspace_id": "illd",
            "pipeline": "illd_7step",
            "total_nodes": total_nodes,
            "total_relationships": total_rels,
            "total_rag_chunks": total_chunks,
            "kg_breakdown": {k.split(":")[1]: v for k, v in kg_stats.items() if k.startswith("nodes:")},
            "rag_breakdown": dict(rag_stats),
            "job_id": job_id,
        }

        self._tracker.complete(job_id, results)
        logger.info("[ILLD] Pipeline complete: %d nodes, %d rels, %d RAG chunks",
                    total_nodes, total_rels, total_chunks)
        self._fire_module_ingested(module_name, "illd")
        return results

    def batch_ingest(self, lld_path: str, modules: Optional[List[str]] = None,
                     workspace_id: str = "illd", max_workers: int = 4) -> Dict[str, Any]:
        """Ingest multiple modules using parallel ThreadPoolExecutor."""
        root = Path(lld_path)
        if not root.exists():
            raise FileNotFoundError(f"LLD path not found: {lld_path}")

        # Discover modules
        if modules is None:
            modules = [d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]

        job_id = self._tracker.create_job("batch_ingest", {"path": lld_path, "modules": modules})

        per_module = []
        completed_count = 0
        total = len(modules)

        def _ingest_one(mod: str) -> Dict[str, Any]:
            try:
                r = self.ingest_module(str(root), mod, workspace_id=workspace_id)
                return {"module": mod, "status": "completed", **r}
            except Exception as e:
                return {"module": mod, "status": "failed", "error": str(e)}

        # Use max_workers=1 if only a few modules (avoid thread overhead)
        effective_workers = min(max_workers, total) if total > 1 else 1

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {executor.submit(_ingest_one, mod): mod for mod in modules}
            for future in as_completed(futures):
                result = future.result()
                per_module.append(result)
                completed_count += 1
                self._tracker.update_progress(
                    job_id, int(completed_count / total * 100))
                logger.info("[batch_ingest] %d/%d modules done: %s → %s",
                            completed_count, total,
                            result["module"], result["status"])

        result = {
            "modules_processed": len(modules), "per_module": per_module,
            "job_id": job_id,
        }
        self._tracker.complete(job_id, result)
        # Per-module callbacks already fired inside ingest_module
        return result

    def ingest_repository(self, repo_path: str, modules: Optional[List[str]] = None,
                          include_tests: bool = False, workspace_id: str = "illd") -> Dict[str, Any]:
        """Repository-wide ingestion."""
        root = Path(repo_path)
        if not root.exists():
            raise FileNotFoundError(f"Repo not found: {repo_path}")

        # For repository-wide, delegate to batch_ingest
        lld_path = root / "lld" if (root / "lld").exists() else root
        return self.batch_ingest(str(lld_path), modules, workspace_id)

    # ── Ingestion-completion event ─────────────────────────────────────────────

    def _fire_module_ingested(self, module_name: str, workspace_id: str):
        """Best-effort callback after successful module ingestion."""
        logger.info(
            "[Ingestion] Module '%s' ingested (workspace=%s) — firing completion event",
            module_name, workspace_id,
        )
        if self._on_module_ingested:
            try:
                self._on_module_ingested(module_name, workspace_id)
            except Exception:
                logger.warning(
                    "[Ingestion] on_module_ingested callback failed for '%s' — ingestion unaffected",
                    module_name, exc_info=True,
                )

    # ── File Discovery ─────────────────────────────────────────────────

    def _discover_module_files(self, root: Path, module_name: str) -> List[Path]:
        """Find all ingestible files for a module."""
        files = []
        module_upper = module_name.upper()
        module_lower = module_name.lower()

        # Search patterns
        search_dirs = [
            root / "lld" / module_upper,
            root / "lld" / module_lower,
            root / module_upper,
            root / module_lower,
            root / "data" / module_upper,
            root / "data" / module_lower,
        ]

        for d in search_dirs:
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                        files.append(f)

        # Also check for module-specific JSON requirements
        for pattern in [f"*{module_lower}*", f"*{module_upper}*"]:
            for f in root.rglob(pattern):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS and f not in files:
                    files.append(f)

        return sorted(set(files))[:100]  # Cap at 100 files

    # ── File Parsing ───────────────────────────────────────────────────

    def _parse_file(self, path: Path, ext: str) -> Optional[Dict]:
        """Parse a file using the appropriate parser.

        Sprint 8: delegates to dedicated parsers in src/IngestionPipeline/parsers/
        for .c, .h, .pdf, .xlsx, .arxml, .puml, .rst, .md, .eap/.eaps/.qeax formats.
        Filename patterns are used to select specialised parsers (regdef, swa, doxygen,
        hw_spec, testspec, swa_parsers, swud_parsers) before falling back to generic ones.
        """
        fname_lower = path.name.lower()

        if ext in (".c", ".h"):
            # ── Specialised .h parsers (by filename pattern) ──────────
            if ext == ".h" and "_regdef" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.regdef_parser import parse as regdef_parse
                    parsed = regdef_parse(str(path))
                    if isinstance(parsed, dict):
                        parsed.setdefault("type", "regdef")
                        parsed.setdefault("file", str(path))
                    return parsed
                except ImportError:
                    logger.warning("[Ingestion] regdef_parser not available, falling back to c_parser")

            if ext == ".h" and "_swa" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.illd_swa_parser import parse as swa_hdr_parse
                    parsed = swa_hdr_parse(str(path))
                    if isinstance(parsed, dict):
                        parsed.setdefault("type", "swa_header")
                        parsed.setdefault("file", str(path))
                    return parsed
                except ImportError:
                    logger.warning("[Ingestion] illd_swa_parser not available, falling back to c_parser")

            # ── Doxygen-annotated headers ─────────────────────────────
            if ext == ".h":
                try:
                    from src.IngestionPipeline.parsers.doxygen_parser import parse as doxygen_parse
                    reqs = doxygen_parse(str(path))
                    if reqs:  # Only use if requirements were found
                        return {"type": "doxygen", "requirements": reqs, "file": str(path)}
                except (ImportError, Exception):
                    pass  # Fall through to generic c_parser

            # ── Generic C/H parser ────────────────────────────────────
            try:
                from src.IngestionPipeline.parsers.c_parser import parse as c_parse
                parsed = c_parse(str(path))
                if isinstance(parsed, dict):
                    parsed.setdefault("type", "c_source" if ext == ".c" else "c_header")
                    parsed.setdefault("file", str(path))
                return parsed
            except ImportError:
                return self._parse_c_file(path)

        elif ext == ".pdf":
            try:
                from src.IngestionPipeline.parsers.pdf_parser import parse as pdf_parse
                pages = pdf_parse(str(path))
                return {"type": "pdf", "pages": pages, "file": str(path)}
            except ImportError:
                logger.warning("[Ingestion] pdf_parser not available, falling back to generic")
                return {"type": "generic", "content": "", "file": str(path)}

        elif ext == ".xlsx":
            # ── Test specification workbooks ──────────────────────────
            if "_ts_" in fname_lower or "testspec" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.testspec_parsers import parse_testspec_workbook
                    nodes = parse_testspec_workbook(str(path))
                    return {"type": "testspec", "nodes": nodes, "file": str(path)}
                except (ImportError, Exception):
                    logger.warning("[Ingestion] testspec_parsers not available, falling back to xlsx_parser")

            # ── Generic xlsx parser ───────────────────────────────────
            try:
                from src.IngestionPipeline.parsers.xlsx_parser import parse as xlsx_parse
                sheets = xlsx_parse(str(path))
                return {"type": "xlsx", "sheets": sheets, "file": str(path)}
            except ImportError:
                logger.warning("[Ingestion] xlsx_parser not available, falling back to generic")
                return {"type": "generic", "content": "", "file": str(path)}

        elif ext == ".arxml":
            try:
                from src.IngestionPipeline.parsers.arxml_parser import parse as arxml_parse
                return arxml_parse(str(path))
            except ImportError:
                logger.warning("[Ingestion] arxml_parser not available, falling back to generic")
                return {"type": "generic", "content": "", "file": str(path)}

        elif ext == ".puml":
            try:
                from src.IngestionPipeline.parsers.puml_parser import parse as puml_parse
                return puml_parse(str(path))
            except ImportError:
                logger.warning("[Ingestion] puml_parser not available, falling back to generic")
                return {"type": "generic", "content": "", "file": str(path)}

        elif ext == ".rst":
            try:
                from src.IngestionPipeline.parsers.rst_parser import parse as rst_parse
                sections = rst_parse(str(path))
                return {"type": "rst", "sections": sections, "file": str(path)}
            except ImportError:
                return self._parse_text(path)

        elif ext in (".eap", ".eaps", ".qeax"):
            try:
                from src.IngestionPipeline.parsers.ea_parser import parse as ea_parse
                parsed = ea_parse(str(path))
                if isinstance(parsed, dict):
                    parsed.setdefault("type", "ea_model")
                    parsed.setdefault("file", str(path))
                return parsed
            except ImportError:
                logger.warning("[Ingestion] ea_parser not available, falling back to generic")
                return {"type": "generic", "content": "", "file": str(path)}

        elif ext == ".md":
            # ── Hardware spec markdown ────────────────────────────────
            if "hw" in fname_lower or "spec" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.hw_spec_parser import parse as hw_spec_parse
                    parsed = hw_spec_parse(str(path))
                    if isinstance(parsed, dict):
                        parsed.setdefault("type", "hw_spec")
                        parsed.setdefault("file", str(path))
                    return parsed
                except (ImportError, Exception):
                    logger.warning("[Ingestion] hw_spec_parser not available, falling back to text")

            # ── SWA markdown ──────────────────────────────────────────
            if "swa" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.swa_parsers import parse_swa_file
                    parsed = parse_swa_file(str(path))
                    if isinstance(parsed, dict):
                        parsed.setdefault("type", "swa_doc")
                        parsed.setdefault("file", str(path))
                    return parsed
                except (ImportError, AttributeError, Exception):
                    logger.warning("[Ingestion] swa_parsers not available, falling back to text")

            # ── SWUD markdown ─────────────────────────────────────────
            if "swud" in fname_lower:
                try:
                    from src.IngestionPipeline.parsers.swud_parsers import parse_swud_file
                    parsed = parse_swud_file(str(path))
                    if isinstance(parsed, dict):
                        parsed.setdefault("type", "swud_doc")
                        parsed.setdefault("file", str(path))
                    return parsed
                except (ImportError, AttributeError, Exception):
                    logger.warning("[Ingestion] swud_parsers not available, falling back to text")

            return self._parse_text(path)

        elif ext == ".json":
            return self._parse_json(path)

        elif ext == ".txt":
            return self._parse_text(path)

        elif ext == ".csv":
            return self._parse_text(path)

        else:
            # Generic text extraction for other supported types
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                return {"type": "generic", "content": content[:50000], "file": str(path)}
            except Exception:
                return {"type": "binary", "file": str(path)}

    def _parse_c_file(self, path: Path) -> Dict:
        """Extract functions and structs from C/H files."""
        import re
        content = path.read_text(encoding="utf-8", errors="replace")
        functions = []
        fn_pattern = re.compile(
            r'(?:IFX_EXTERN\s+|IFX_INLINE\s+)?(\w[\w\s\*]+?)\s+(\w+)\s*\(([^)]*)\)\s*[;{]',
            re.MULTILINE
        )
        for m in fn_pattern.finditer(content):
            functions.append({
                "name": m.group(2), "return_type": m.group(1).strip(),
                "parameters": m.group(3).strip(),
            })
        return {"type": "c_header", "functions": functions, "file": str(path),
                "content": content[:10000]}

    def _parse_json(self, path: Path) -> Dict:
        with open(path) as f:
            data = json.load(f)
        return {"type": "json", "data": data, "file": str(path)}

    def _parse_text(self, path: Path) -> Dict:
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"type": "text", "content": content[:50000], "file": str(path)}

    def _normalize_module(self, module: str) -> str:
        return module.strip().lower()

    def _normalize_workspace(self, workspace: str) -> str:
        return workspace.strip().lower()

    def _merge_scoped_node(
        self,
        session,
        op: str,
        label: str,
        identity: Dict[str, Any],
        properties: Dict[str, Any],
        module: str,
        workspace: str,
    ) -> None:
        identity_clause = ", ".join(f"{key}: ${key}" for key in identity)
        query = f"""
            MERGE (ns:NodeSet {{id: $node_set_id}})
            ON CREATE SET
                ns.project = $project,
                ns.module = $module,
                ns.status = 'active',
                ns.created_at = $created_at
            ON MATCH SET
                ns.status = 'active'
            {op} (n:{label} {{{identity_clause}}})
            SET n += $props
            MERGE (ns)-[:HAS_MODULE]->(n)
        """
        params = {
            **identity,
            "props": properties,
            "node_set_id": f"ns_{workspace}_{module}",
            "project": workspace,
            "module": module,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        session.run(query, params)

    def _write_call_relationships(
        self,
        session,
        op: str,
        module: str,
        workspace: str,
        caller_name: str,
        callees: List[str],
    ) -> int:
        rels = 0
        for callee_name in callees:
            if not callee_name or callee_name == caller_name:
                continue
            query = f"""
                MATCH (ns:NodeSet {{id: $node_set_id}})-[:HAS_MODULE]->(caller:{CANONICAL_FUNCTION_LABEL} {{name: $caller, module: $module}})
                MERGE (ns)-[:HAS_MODULE]->(called:{CANONICAL_FUNCTION_LABEL} {{name: $called, module: $module}})
                SET called.workspace_id = $workspace, called.profile = $workspace
                {op} (caller)-[:CALLS_INTERNALLY]->(called)
            """
            session.run(
                query,
                {
                    "node_set_id": f"ns_{workspace}_{module}",
                    "caller": caller_name,
                    "called": callee_name,
                    "module": module,
                    "workspace": workspace,
                },
            )
            rels += 1
        return rels

    def _extract_call_names(self, internal_calls: Any) -> List[str]:
        names: List[str] = []
        for entry in internal_calls or []:
            if isinstance(entry, dict) and entry.get("function"):
                names.append(entry["function"])
                continue
            if isinstance(entry, dict):
                for nested in entry.get("calls", []):
                    if isinstance(nested, dict) and nested.get("function"):
                        names.append(nested["function"])
        seen = set()
        ordered: List[str] = []
        for name in names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def _iter_c_functions(self, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_functions = parsed.get("functions") or {}
        if isinstance(raw_functions, dict):
            result = []
            for function_name, metadata in raw_functions.items():
                if not isinstance(metadata, dict):
                    metadata = {}
                params = metadata.get("parameters", "")
                if isinstance(params, list):
                    params = json.dumps(params)
                result.append({
                    "name": function_name,
                    "return_type": metadata.get("return_type", ""),
                    "parameters": params or "",
                    "internal_calls": self._extract_call_names(metadata.get("internal_calls", [])),
                })
            return result

        result = []
        for item in raw_functions:
            if isinstance(item, dict):
                result.append({
                    "name": item.get("name", ""),
                    "return_type": item.get("return_type", ""),
                    "parameters": item.get("parameters", ""),
                    "internal_calls": self._extract_call_names(item.get("internal_calls", [])),
                })
        return result

    # ── KG Writing ─────────────────────────────────────────────────────

    def _write_to_kg(self, parsed: Dict, module: str, workspace: str, overwrite: bool) -> tuple:
        """Write parsed data to Neo4j. Returns (nodes_created, rels_created).

        Sprint 8: Uses overwrite flag correctly (CREATE vs MERGE),
        handles multiple parsed types, and writes relationships.
        """
        nodes = 0
        rels = 0
        if not self._neo4j or not parsed:
            return nodes, rels

        db = workspace if workspace in ("illd", "mcal") else "neo4j"
        op = "CREATE" if overwrite else "MERGE"
        normalized_module = self._normalize_module(module)
        normalized_workspace = self._normalize_workspace(workspace)

        try:
            with self._neo4j.session(database=db) as session:
                with Neo4jBatchWriter(session, normalized_workspace,
                                      normalized_module, op=op) as batch:
                    ptype = parsed.get("type", "")

                    if ptype in ("c_header", "c_source"):
                        for fn in self._iter_c_functions(parsed):
                            if not fn.get("name"):
                                continue
                            batch.add_node(
                                CANONICAL_FUNCTION_LABEL,
                                {"name": fn["name"], "module": normalized_module},
                                {
                                    "name": fn["name"],
                                    "module": normalized_module,
                                    "workspace_id": normalized_workspace,
                                    "profile": normalized_workspace,
                                    "return_type": fn.get("return_type", ""),
                                    "parameters": fn.get("parameters", ""),
                                    "source_file": parsed.get("file", ""),
                                    "source_type": ptype,
                                },
                            )
                            for callee in fn.get("internal_calls", []):
                                if callee and callee != fn["name"]:
                                    batch.add_relationship(
                                        fn["name"], callee, "CALLS_INTERNALLY",
                                    )

                    elif ptype == "json":
                        data = parsed.get("data", {})
                        requirements = data.get("requirements") if isinstance(data, dict) else None
                        if isinstance(requirements, list) and requirements:
                            for req in requirements:
                                if not isinstance(req, dict):
                                    continue
                                req_id = req.get("requirement_id") or req.get("id") or req.get("document_key")
                                if not req_id:
                                    continue
                                batch.add_node(
                                    CANONICAL_REQUIREMENT_LABEL,
                                    {"requirement_id": req_id, "module": normalized_module},
                                    {
                                        "requirement_id": req_id,
                                        "module": normalized_module,
                                        "workspace_id": normalized_workspace,
                                        "profile": normalized_workspace,
                                        "name": req.get("name") or req_id,
                                        "description": req.get("text") or req.get("description") or "",
                                        "source_file": parsed.get("file", ""),
                                    },
                                )
                        else:
                            batch.add_node(
                                "DataNode",
                                {"source_file": parsed.get("file", ""), "module": normalized_module},
                                {
                                    "source_file": parsed.get("file", ""),
                                    "module": normalized_module,
                                    "workspace_id": normalized_workspace,
                                    "profile": normalized_workspace,
                                    "content": json.dumps(data)[:50000],
                                },
                            )

                    elif ptype in ("pdf", "text", "rst", "generic"):
                        content = ""
                        if ptype == "pdf":
                            content = "\n".join(parsed.get("pages", []))[:50000]
                        elif ptype == "rst":
                            sections = parsed.get("sections", [])
                            content = json.dumps(sections)[:50000]
                        else:
                            content = (parsed.get("content") or "")[:50000]
                        batch.add_node(
                            "Document",
                            {"source_file": parsed.get("file", ""), "module": normalized_module},
                            {
                                "source_file": parsed.get("file", ""),
                                "module": normalized_module,
                                "workspace_id": normalized_workspace,
                                "profile": normalized_workspace,
                                "content": content,
                                "doc_type": ptype,
                            },
                        )

                    elif ptype == "xlsx":
                        for sheet_name, rows in (parsed.get("sheets") or {}).items():
                            batch.add_node(
                                "Sheet",
                                {"name": sheet_name, "source_file": parsed.get("file", ""), "module": normalized_module},
                                {
                                    "name": sheet_name,
                                    "source_file": parsed.get("file", ""),
                                    "module": normalized_module,
                                    "workspace_id": normalized_workspace,
                                    "profile": normalized_workspace,
                                    "row_count": len(rows),
                                },
                            )

                    elif ptype == "arxml":
                        for mod in parsed.get("modules", []):
                            mod_name = mod.get("name", "")
                            if not mod_name:
                                continue
                            batch.add_node(
                                "ARXMLModule",
                                {"name": mod_name, "module": normalized_module},
                                {
                                    "name": mod_name,
                                    "source_file": parsed.get("file", ""),
                                    "module": normalized_module,
                                    "workspace_id": normalized_workspace,
                                    "profile": normalized_workspace,
                                },
                            )

                    elif ptype == "puml":
                        for fn in parsed.get("functions", []):
                            fn_name = fn if isinstance(fn, str) else fn.get("name", "")
                            if not fn_name:
                                continue
                            batch.add_node(
                                CANONICAL_FUNCTION_LABEL,
                                {"name": fn_name, "module": normalized_module},
                                {
                                    "name": fn_name,
                                    "module": normalized_module,
                                    "workspace_id": normalized_workspace,
                                    "profile": normalized_workspace,
                                    "source_file": parsed.get("file", ""),
                                    "source_type": "puml",
                                },
                            )
                        for dep in parsed.get("dependencies", []):
                            src = dep.get("from", dep.get("source", ""))
                            tgt = dep.get("to", dep.get("target", ""))
                            if src and tgt:
                                batch.add_relationship(
                                    src, tgt, "DEPENDS_ON",
                                )

                # batch.__exit__ auto-flushes; read counters
                nodes, rels = batch.stats

        except Exception as e:
            logger.error("[Ingestion] KG write failed: %s", e)

        return nodes, rels

    @property
    def job_tracker(self) -> IngestionJobTracker:
        return self._tracker

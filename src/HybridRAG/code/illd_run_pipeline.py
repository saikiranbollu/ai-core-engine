#!/usr/bin/env python3
"""
ILLD Ingestion Pipeline Orchestrator
======================================

End-to-end pipeline that fetches, parses, and ingests data from all 6 ILLD
document sources into both the Knowledge Graph (Neo4j) and the RAG vector
store (Qdrant).

Data sources:
    1. HW Manual PDF  (Bitbucket)  → pdf_parser → hw_spec_parser → KG + RAG
    2. Requirements    (Jama API)   → JamaConnector               → KG + RAG
    3. SWA Header      (GitLab)     → illd_swa_parser              → KG + RAG
    4. Source Code     (GitLab)     → c_parser                     → KG + RAG
    5. SFR regdef      (GitLab)     → regdef_parser                 → KG + RAG
    6. PlantUML        (GitLab)     → puml_parser                  → KG + RAG
    7. Cross-source relationships                                  → KG

Usage::

    # Full pipeline — fetch from remote APIs (requires VPN + tokens in .env)
    python illd_run_pipeline.py --module LIN --remote --clear

    # Dry-run (no DB writes, preview only)
    python illd_run_pipeline.py --module LIN --remote --dry-run

    # Use local repo clones instead of API
    python illd_run_pipeline.py --module CXPI --gitlab-repo /path/to/clone --clear

    # Skip specific sources
    python illd_run_pipeline.py --module CXPI --remote --skip-jama --skip-hw-pdf

    # Only KG, no RAG
    python illd_run_pipeline.py --module CXPI --remote --skip-rag

    # Only RAG, no KG
    python illd_run_pipeline.py --module CXPI --remote --skip-kg
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_DIR = Path(__file__).resolve().parent            # .../HybridRAG/code
HYBRIDRAG_DIR = CODE_DIR.parent                       # .../HybridRAG
CONFIG_DIR = HYBRIDRAG_DIR / "config"

# Repo root for src.* imports
ROOT_DIR = CODE_DIR.parents[2]
SRC_DIR = ROOT_DIR / "src"
for p in (CODE_DIR, str(SRC_DIR), str(ROOT_DIR)):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("illd_pipeline")


# ---------------------------------------------------------------------------
# Lazy imports (so --help works without all deps installed)
# ---------------------------------------------------------------------------

def _import_parsers():
    """Import ILLD parsers from IngestionPipeline."""
    from src.IngestionPipeline.parsers import (
        illd_swa_parser,
        c_parser,
        regdef_parser,
        puml_parser,
        hw_spec_parser,
        pdf_parser,
    )
    return {
        "swa": illd_swa_parser,
        "c": c_parser,
        "sfr": regdef_parser,
        "puml": puml_parser,
        "hw_spec": hw_spec_parser,
        "pdf": pdf_parser,
    }


def _import_jama():
    """Import Jama connector."""
    from src.IngestionPipeline.Connectors.JamaConnector import JamaConnector
    return JamaConnector


def _import_kg_builder():
    """Import ILLD KG builder."""
    from KG.illd_kg_builder import ILLDKGBuilder
    return ILLDKGBuilder


def _import_rag_ingestor():
    """Import ILLD RAG ingestor."""
    from RAG.illd_rag_ingestion import ILLDRAGIngestor
    return ILLDRAGIngestor


def _load_config():
    """Load storage + env config."""
    from env_config import load_env, load_yaml_with_env
    load_env()
    return load_yaml_with_env(CONFIG_DIR / "storage_config.yaml")


# ---------------------------------------------------------------------------
# Remote Source Fetcher — downloads files from GitLab & Bitbucket APIs
# ---------------------------------------------------------------------------

class RemoteSourceFetcher:
    """Fetch ILLD source files from GitLab and Bitbucket REST APIs.

    Downloads files to a local temp directory that mirrors the expected
    repo layout, so existing parser logic works unchanged.
    """

    # GitLab config
    GL_BASE = "https://gitlab.intra.infineon.com"
    GL_PROJECT_PATH = "ifx/atv-mc/atv-mc-sw-lld/aurix_rc1_illd/aurix_rc1_illd_platform"
    GL_REF = "master"

    # Bitbucket config
    BB_BASE = "https://bitbucket.vih.infineon.com"
    BB_PROJECT = "ILLD"
    BB_REPO = "rc1_sw_dep"
    BB_BASE_PATH = "Alpha1_VP-EIR1_iLLD/Baselined_Versions"

    def __init__(self, module: str, work_dir: Optional[Path] = None):
        self.module = module.upper()
        self.module_cap = module.capitalize()  # e.g. "Lin"
        self._work_dir = work_dir or Path(tempfile.mkdtemp(prefix=f"illd_{self.module}_"))
        self._gl_client: Optional[Any] = None
        self._bb_client: Optional[Any] = None
        self.logger = logging.getLogger("remote_fetcher")

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def gitlab_root(self) -> Path:
        """Root dir that mimics the GitLab repo layout."""
        return self._work_dir / "gitlab"

    @property
    def bitbucket_root(self) -> Path:
        """Root dir that mimics the Bitbucket repo layout."""
        return self._work_dir / "bitbucket"

    # -- HTTP clients -------------------------------------------------------

    def _get_gl_client(self):
        if self._gl_client is None:
            import httpx
            token = os.environ.get("GITLAB_TOKEN", "")
            headers = {"Accept": "application/json"}
            if token:
                headers["PRIVATE-TOKEN"] = token
            else:
                self.logger.warning("No GITLAB_TOKEN — trying anonymous access")
            self._gl_client = httpx.Client(
                headers=headers, timeout=60.0, verify=True,
            )
        return self._gl_client

    def _get_bb_client(self):
        if self._bb_client is None:
            import httpx
            token = os.environ.get("BITBUCKET_TOKEN", "")
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                self.logger.warning("No BITBUCKET_TOKEN — trying anonymous access")
            self._bb_client = httpx.Client(
                headers=headers, timeout=60.0, verify=True,
            )
        return self._bb_client

    # -- GitLab API helpers -------------------------------------------------

    def _gl_api_url(self) -> str:
        project_id = quote(self.GL_PROJECT_PATH, safe="")
        return f"{self.GL_BASE}/api/v4/projects/{project_id}"

    def _gl_download_file(self, file_path: str) -> Optional[bytes]:
        """Download a single file from GitLab. Returns raw bytes or None."""
        client = self._get_gl_client()
        encoded = quote(file_path, safe="")
        url = f"{self._gl_api_url()}/repository/files/{encoded}/raw"
        resp = client.get(url, params={"ref": self.GL_REF})
        if resp.status_code == 200:
            return resp.content
        self.logger.warning("GitLab file not found (%d): %s", resp.status_code, file_path)
        return None

    def _gl_list_tree(self, path: str) -> List[dict]:
        """List files in a GitLab directory."""
        client = self._get_gl_client()
        results = []
        page = 1
        while True:
            resp = client.get(
                f"{self._gl_api_url()}/repository/tree",
                params={"path": path, "ref": self.GL_REF, "per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    # -- Bitbucket API helpers -----------------------------------------------

    def _bb_api_url(self) -> str:
        return f"{self.BB_BASE}/rest/api/1.0/projects/{self.BB_PROJECT}/repos/{self.BB_REPO}"

    def _bb_download_file(self, file_path: str) -> Optional[bytes]:
        """Download a raw file from Bitbucket Server."""
        client = self._get_bb_client()
        url = f"{self._bb_api_url()}/raw/{file_path}"
        resp = client.get(url)
        if resp.status_code == 200:
            return resp.content
        self.logger.warning("Bitbucket file not found (%d): %s", resp.status_code, file_path)
        return None

    def _bb_list_dir(self, path: str) -> List[dict]:
        """List contents of a Bitbucket directory."""
        client = self._get_bb_client()
        url = f"{self._bb_api_url()}/browse/{path}"
        resp = client.get(url, params={"limit": 1000})
        if resp.status_code != 200:
            return []
        data = resp.json()
        children = data.get("children", {}).get("values", [])
        return [
            {
                "name": ch.get("path", {}).get("name", ""),
                "type": ch.get("type", ""),
                "size": ch.get("size", 0),
            }
            for ch in children
        ]

    # -- High-level fetch methods -------------------------------------------

    def fetch_gitlab_file(self, remote_path: str) -> Optional[Path]:
        """Download a GitLab file and save to the local mirror dir.

        Returns the local Path or None if the file does not exist.
        """
        content = self._gl_download_file(remote_path)
        if content is None:
            return None
        local = self.gitlab_root / remote_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        self.logger.info("Downloaded (GitLab): %s → %s (%d KB)",
                        remote_path, local, len(content) // 1024)
        return local

    def fetch_swa_header(self) -> Optional[Path]:
        """Fetch the SWA header for this module from GitLab.

        Tries exact match first, then scans the directory for variants
        (some modules use lowercase in the filename).
        """
        # Try exact match
        path = f"lld/{self.module_cap}/doc/arch/input/Ifx{self.module_cap}_{self.module_cap}_swa.h"
        result = self.fetch_gitlab_file(path)
        if result:
            return result

        # Scan directory for case-insensitive match
        remote_dir = f"lld/{self.module_cap}/doc/arch/input"
        entries = self._gl_list_tree(remote_dir)
        swa_files = [
            e for e in entries
            if e.get("name", "").lower().endswith("_swa.h")
        ]
        if swa_files:
            match_path = f"{remote_dir}/{swa_files[0]['name']}"
            self.logger.info("Found SWA variant: %s", match_path)
            return self.fetch_gitlab_file(match_path)

        self.logger.warning("No SWA header found for module %s", self.module)
        return None

    def fetch_source_code(self) -> Optional[Path]:
        """Fetch the C source file for this module from GitLab."""
        path = f"lld/{self.module_cap}/src/Ifx{self.module_cap}_{self.module_cap}.c"
        result = self.fetch_gitlab_file(path)
        if result:
            return result

        # Scan directory for case-insensitive match
        remote_dir = f"lld/{self.module_cap}/src"
        entries = self._gl_list_tree(remote_dir)
        c_files = [
            e for e in entries
            if e.get("name", "").lower().endswith(".c")
            and self.module_cap.lower() in e.get("name", "").lower()
        ]
        if c_files:
            match_path = f"{remote_dir}/{c_files[0]['name']}"
            self.logger.info("Found C source variant: %s", match_path)
            return self.fetch_gitlab_file(match_path)

        return None

    def fetch_sfr_regdef(self) -> Optional[Path]:
        """Fetch the SFR regdef header from GitLab."""
        path = f"infra/sfr/inc/Ifx{self.module_cap}_regdef.h"
        return self.fetch_gitlab_file(path)

    def fetch_puml_files(self) -> Optional[Path]:
        """Fetch all .puml files for this module from GitLab.

        Returns the local directory containing the downloaded .puml files,
        or None if none found.
        """
        remote_dir = f"lld/{self.module_cap}/doc/arch/input"
        entries = self._gl_list_tree(remote_dir)
        pumls = [e for e in entries if e.get("name", "").endswith(".puml")]

        if not pumls:
            self.logger.warning("No .puml files found at %s", remote_dir)
            return None

        local_dir = self.gitlab_root / remote_dir
        local_dir.mkdir(parents=True, exist_ok=True)

        for p in pumls:
            remote_path = f"{remote_dir}/{p['name']}"
            content = self._gl_download_file(remote_path)
            if content:
                (local_dir / p["name"]).write_bytes(content)

        count = len(list(local_dir.glob("*.puml")))
        self.logger.info("Downloaded %d .puml files to %s", count, local_dir)
        return local_dir if count > 0 else None

    def fetch_hw_pdf(self) -> Optional[Path]:
        """Fetch the HW manual PDF from Bitbucket.

        Returns the local path to the downloaded PDF, or None.
        """
        bb_dir = f"{self.BB_BASE_PATH}/RC1_IP_{self.module}"
        entries = self._bb_list_dir(bb_dir)
        pdfs = [
            e for e in entries
            if e["name"].lower().endswith(".pdf")
            and self.module.lower() in e["name"].lower()
        ]

        if not pdfs:
            self.logger.warning("No HW PDF found at %s", bb_dir)
            return None

        # Pick the most recent (by name — they contain dates)
        pdf_name = sorted(pdfs, key=lambda x: x["name"])[-1]["name"]
        remote_path = f"{bb_dir}/{pdf_name}"
        content = self._bb_download_file(remote_path)
        if content is None:
            return None

        local = self.bitbucket_root / bb_dir / pdf_name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        size_mb = len(content) / (1024 * 1024)
        self.logger.info("Downloaded HW PDF (%.1f MB): %s", size_mb, local)
        return local

    def close(self):
        """Close HTTP clients."""
        if self._gl_client:
            self._gl_client.close()
        if self._bb_client:
            self._bb_client.close()


# ---------------------------------------------------------------------------
# Pipeline Steps
# ---------------------------------------------------------------------------

class ILLDPipeline:
    """
    Orchestrates the full ILLD ingestion pipeline.

    Each ``step_*`` method runs one pipeline phase and stores the
    parsed data in ``self.data`` for downstream steps.
    """

    def __init__(
        self,
        module: str,
        gitlab_repo_path: Optional[Path] = None,
        bitbucket_repo_path: Optional[Path] = None,
        dry_run: bool = False,
        clear: bool = False,
        skip_kg: bool = False,
        skip_rag: bool = False,
        remote: bool = False,
        cleanup_temp: bool = True,
    ):
        self.module = module.upper()
        self.module_cap = module.capitalize()
        self.dry_run = dry_run
        self.clear = clear
        self.skip_kg = skip_kg
        self.skip_rag = skip_rag

        # Repo paths (local clones)
        self.gitlab_repo = gitlab_repo_path
        self.bitbucket_repo = bitbucket_repo_path

        # Remote fetching mode
        self.remote = remote
        self._cleanup_temp = cleanup_temp
        self._fetcher: Optional[RemoteSourceFetcher] = None
        if remote and not gitlab_repo_path and not bitbucket_repo_path:
            self._fetcher = RemoteSourceFetcher(module)
            logger.info("Remote mode: files will be fetched from GitLab/Bitbucket APIs")
            logger.info("Work dir: %s", self._fetcher.work_dir)

        # Parsed data store (populated by step_* methods)
        self.data: Dict[str, Any] = {}

        # Per-step timing
        self._step_times: List[Dict[str, Any]] = []

        # Intermediary results directory
        self._intermediary_dir: Optional[Path] = None

        # Config
        self.config = _load_config()

        # Builders (initialised lazily)
        self._kg: Optional[Any] = None
        self._rag: Optional[Any] = None

    # -- Builder access -----------------------------------------------------

    def _init_intermediary_dir(self, save_intermediary: bool):
        """Create a persistent directory for intermediary parsed results."""
        if save_intermediary:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = ROOT_DIR / "temp_intermediary"
            try:
                base.mkdir(parents=True, exist_ok=True)
            except OSError:
                base = Path("/tmp") / "temp_intermediary"
                base.mkdir(parents=True, exist_ok=True)
            self._intermediary_dir = base / f"{self.module}_{ts}"
            self._intermediary_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Intermediary results will be saved to: %s", self._intermediary_dir)

    def _save_intermediary(self, step_name: str, data: Any):
        """Save parsed data as JSON for cross-verification."""
        if self._intermediary_dir is None:
            return
        out_path = self._intermediary_dir / f"{step_name}.json"
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.info("Saved intermediary: %s (%d KB)", out_path, out_path.stat().st_size // 1024)
        except Exception as e:
            logger.warning("Failed to save intermediary %s: %s", step_name, e)

    def _track_step(self, step_name: str, t_start: float):
        """Record timing for a pipeline step."""
        elapsed = time.time() - t_start
        self._step_times.append({
            "step": step_name,
            "elapsed": elapsed,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        logger.info("STEP COMPLETE: %s — %.1fs", step_name, elapsed)

    @property
    def kg(self):
        if self._kg is None and not self.skip_kg:
            KGBuilder = _import_kg_builder()
            self._kg = KGBuilder(
                module=self.module,
                dry_run=self.dry_run,
                clear_db=self.clear,
            )
        return self._kg

    @property
    def rag(self):
        if self._rag is None and not self.skip_rag:
            RAGIngestor = _import_rag_ingestor()
            self._rag = RAGIngestor(
                module=self.module,
                dry_run=self.dry_run,
            )
        return self._rag

    # -- Step 1: SWA Header -------------------------------------------------

    def step_swa(self):
        """Parse SWA header file and ingest."""
        logger.info("=" * 60)
        logger.info("STEP 1: SWA Header (%s)", self.module)
        logger.info("=" * 60)

        swa_path = None

        # Remote fetch
        if self._fetcher:
            swa_path = self._fetcher.fetch_swa_header()
        elif self.gitlab_repo:
            swa_path = (self.gitlab_repo / "lld" / self.module_cap /
                        "doc" / "arch" / "input" /
                        f"Ifx{self.module_cap}_{self.module_cap}_swa.h")
            if not swa_path.exists():
                swa_path = None

        if not swa_path:
            logger.warning("SWA file not available — skipping.")
            return

        parsers = _import_parsers()
        logger.info("Parsing: %s", swa_path)
        swa_data = parsers["swa"].parse(str(swa_path))
        self.data["swa"] = swa_data

        funcs = len(swa_data.get("functions", []))
        structs = len(swa_data.get("structs", []))
        enums = len(swa_data.get("enums", []))
        logger.info("Parsed: %d functions, %d structs, %d enums", funcs, structs, enums)

        if self.kg:
            self.kg.ingest_swa(swa_data)
        if self.rag:
            self.rag.ingest_swa(swa_data)

    # -- Step 2: SFR regdef -------------------------------------------------

    def step_sfr(self):
        """Parse SFR register definition and ingest."""
        logger.info("=" * 60)
        logger.info("STEP 2: SFR Register Definitions (%s)", self.module)
        logger.info("=" * 60)

        sfr_path = None

        if self._fetcher:
            sfr_path = self._fetcher.fetch_sfr_regdef()
        elif self.gitlab_repo:
            sfr_path = (self.gitlab_repo / "infra" / "sfr" / "inc" /
                        f"Ifx{self.module_cap}_regdef.h")
            if not sfr_path.exists():
                sfr_path = None

        if not sfr_path:
            logger.warning("SFR file not available — skipping.")
            return

        parsers = _import_parsers()
        logger.info("Parsing: %s", sfr_path)
        sfr_data = parsers["sfr"].parse(str(sfr_path))
        self.data["sfr"] = sfr_data

        regs = len(sfr_data.get("registers", {}))
        logger.info("Parsed: %d registers", regs)

        if self.kg:
            self.kg.ingest_sfr(sfr_data)
        if self.rag:
            self.rag.ingest_sfr(sfr_data)

    # -- Step 3: C Source Code ----------------------------------------------

    def step_source(self):
        """Parse C source code and ingest."""
        logger.info("=" * 60)
        logger.info("STEP 3: C Source Code (%s)", self.module)
        logger.info("=" * 60)

        c_path = None

        if self._fetcher:
            c_path = self._fetcher.fetch_source_code()
        elif self.gitlab_repo:
            c_path = (self.gitlab_repo / "lld" / self.module_cap /
                      "src" / f"Ifx{self.module_cap}_{self.module_cap}.c")
            if not c_path.exists():
                c_path = None

        if not c_path:
            logger.warning("C source not available — skipping.")
            return

        parsers = _import_parsers()
        logger.info("Parsing: %s", c_path)
        c_data = parsers["c"].parse(str(c_path))
        self.data["source"] = c_data

        funcs = len(c_data.get("functions", {}))
        logger.info("Parsed: %d functions", funcs)

        if self.kg:
            self.kg.ingest_source(c_data)
        if self.rag:
            self.rag.ingest_source(c_data)

    # -- Step 4: PlantUML ---------------------------------------------------

    def step_puml(self):
        """Parse PlantUML diagrams and ingest."""
        logger.info("=" * 60)
        logger.info("STEP 4: PlantUML Diagrams (%s)", self.module)
        logger.info("=" * 60)

        puml_dir = None

        if self._fetcher:
            puml_dir = self._fetcher.fetch_puml_files()
        elif self.gitlab_repo:
            puml_dir = (self.gitlab_repo / "lld" / self.module_cap /
                        "doc" / "arch" / "input")
            if not puml_dir.exists():
                puml_dir = None

        if not puml_dir:
            logger.warning("PUML directory not available — skipping.")
            return

        parsers = _import_parsers()
        logger.info("Parsing PUML directory: %s", puml_dir)

        # puml_parser.parse() expects a single file — concatenate all .puml
        # files into one temp file so the analyzer sees everything.
        # Only include tc* files (test case sequence diagrams), not the
        # hw_sw_interface / sw_sw_interface / seqdiagram files.
        puml_files = sorted(
            f for f in Path(puml_dir).glob("*.puml")
            if f.stem.startswith(f"{self.module.lower()}_tc")
        )
        if not puml_files:
            logger.warning("No tc*.puml files in %s — skipping.", puml_dir)
            return

        combined = Path(puml_dir) / f"_combined_{self.module.lower()}.puml"
        with open(combined, "w", encoding="utf-8") as out:
            for pf in puml_files:
                logger.info("  Including: %s", pf.name)
                out.write(pf.read_text(encoding="utf-8"))
                out.write("\n\n")

        puml_data = parsers["puml"].parse(str(combined))
        self.data["puml"] = puml_data

        core = puml_data.get("core_functions", {})
        total_funcs = sum(len(v) for v in core.values() if isinstance(v, list))
        logger.info("Parsed: %d core functions across all categories", total_funcs)

        if self.kg:
            self.kg.ingest_puml(puml_data)
        if self.rag:
            self.rag.ingest_puml(puml_data)

    # -- Step 5: HW Spec PDF ------------------------------------------------

    def step_hw_spec(self, hw_md_path: Optional[Path] = None):
        """Parse HW spec (PDF → markdown → structured data) and ingest.

        If *hw_md_path* is provided, the markdown is used directly
        (skipping PDF conversion).
        """
        logger.info("=" * 60)
        logger.info("STEP 5: HW Specification (%s)", self.module)
        logger.info("=" * 60)

        parsers = _import_parsers()
        md_path = hw_md_path

        # If no markdown provided, look for PDF
        if md_path is None:
            pdf_path = None

            # Remote fetch from Bitbucket
            if self._fetcher:
                pdf_path = self._fetcher.fetch_hw_pdf()
            elif self.bitbucket_repo:
                pdf_glob = list(
                    (self.bitbucket_repo /
                     "Alpha1_VP-EIR1_iLLD" / "Baselined_Versions" /
                     f"RC1_IP_{self.module}").glob(
                        f"{self.module}_FBcontent_INTERNAL_*.pdf"
                    )
                )
                if pdf_glob:
                    pdf_path = pdf_glob[0]

            if pdf_path:
                logger.info("Converting HW PDF: %s", pdf_path)

                def _pdf_progress(done: int, total: int):
                    pct = done / total * 100
                    bar_len = 40
                    filled = int(bar_len * done // total)
                    bar = "#" * filled + "-" * (bar_len - filled)
                    print(f"\r  PDF Progress: |{bar}| {pct:5.1f}% ({done}/{total} batches)",
                          end="", flush=True)
                    if done == total:
                        print()  # newline at end

                md_parts = parsers["pdf"].parse(str(pdf_path), progress_callback=_pdf_progress)
                md_path = pdf_path.with_suffix(".md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("\n\n".join(md_parts))
                logger.info("PDF → Markdown: %s", md_path)

                # Save PDF + markdown to intermediary
                if self._intermediary_dir:
                    import shutil
                    shutil.copy2(pdf_path, self._intermediary_dir / pdf_path.name)
                    shutil.copy2(md_path, self._intermediary_dir / md_path.name)
                    logger.info("Saved PDF + markdown to intermediary: %s", self._intermediary_dir)

        if md_path and md_path.exists():
            # Parse structured data from markdown
            logger.info("Extracting HW spec entities: %s", md_path)
            hw_data = parsers["hw_spec"].parse(str(md_path))
            self.data["hw_spec"] = hw_data

            counts = hw_data.get("metadata", {}).get("counts", {})
            logger.info("Parsed: %d registers, %d fields, %d interrupts, %d errors",
                         counts.get("registers", 0), counts.get("fields", 0),
                         counts.get("interrupts", 0), counts.get("errors", 0))

            if self.kg:
                self.kg.ingest_hw_spec(hw_data)
            if self.rag:
                self.rag.ingest_hw_spec_markdown(md_path)
        else:
            logger.warning("No HW spec markdown available — skipping.")

    # -- Jama folder discovery ----------------------------------------------

    def _find_jama_module_folder(self, connector, container_id: int):
        """Walk container → 'iLLD' folder → module folder, return folder ID or None."""
        FOLDER_TYPE = 32
        target = self.module.lower()

        # Level 1: find the 'iLLD' top-level folder under the container
        top_children = connector.get_children_items(container_id)
        illd_folder = None
        for child in top_children:
            if child.item_type == FOLDER_TYPE and child.name.lower() == "illd":
                illd_folder = child
                break
        if illd_folder is None:
            logger.warning("Could not find 'iLLD' folder under container %d", container_id)
            return None

        # Level 2: find the module folder under 'iLLD'
        module_children = connector.get_children_items(illd_folder.id)
        for child in module_children:
            if child.item_type == FOLDER_TYPE and child.name.lower() == target:
                logger.info(
                    "Resolved Jama folder for '%s': id=%d (under iLLD id=%d)",
                    self.module, child.id, illd_folder.id,
                )
                return child.id

        logger.warning(
            "Could not find folder for module '%s' under iLLD folder %d",
            self.module, illd_folder.id,
        )
        return None

    # -- Step 6: Requirements (Jama) ----------------------------------------

    def step_requirements(self):
        """Fetch requirements from Jama and ingest."""
        logger.info("=" * 60)
        logger.info("STEP 6: Jama Requirements (%s)", self.module)
        logger.info("=" * 60)

        jama_cfg = self.config.get("jama", {})
        if not jama_cfg.get("base_url"):
            logger.warning("Jama not configured in storage_config.yaml — skipping.")
            return

        JamaConnector = _import_jama()
        connector = JamaConnector(
            base_url=jama_cfg["base_url"],
            api_key=jama_cfg["api_key"],
            api_secret=jama_cfg["api_secret"],
            verify_ssl=jama_cfg.get("verify_ssl", True),
            timeout=jama_cfg.get("timeout", 120),
        )

        try:
            illd_jama = jama_cfg.get("illd", {})
            container_id = illd_jama.get("container_id", 18002547)

            # Walk: container → "iLLD" folder → module folder
            folder_id = self._find_jama_module_folder(connector, container_id)

            if folder_id:
                logger.info(
                    "Fetching requirements from Jama folder %d for module '%s' …",
                    folder_id, self.module,
                )
                items = connector.get_module_items(folder_id, recurse=True)
            else:
                # Fallback: fetch all items in the project (unfiltered)
                project_id = illd_jama.get("project_id", 1986)
                item_type = illd_jama.get("item_type", 83)
                logger.warning(
                    "No Jama folder for module '%s' — falling back to "
                    "full project fetch (project %d, item_type %d)",
                    self.module, project_id, item_type,
                )
                items = connector.get_items(
                    project_id=project_id,
                    item_type_id=item_type,
                )
            logger.info("Fetched: %d requirements", len(items))
            self.data["requirements"] = items

            if self.kg:
                self.kg.ingest_requirements(items)
            if self.rag:
                self.rag.ingest_requirements(items)
        finally:
            connector.close()

    # -- Step 7: Cross-source relationships ---------------------------------

    def step_cross_relationships(self):
        """Create cross-source relationships in KG."""
        logger.info("=" * 60)
        logger.info("STEP 7: Cross-source Relationships")
        logger.info("=" * 60)

        if self.kg:
            self.kg.create_cross_source_relationships()

    # -- Full pipeline ------------------------------------------------------

    def _run_step(self, step_name: str, step_fn, *args, **kwargs):
        """Run a pipeline step with timing and intermediary saving."""
        t_start = time.time()
        step_fn(*args, **kwargs)
        self._track_step(step_name, t_start)

        # Save intermediary data if available
        key_map = {
            "SWA Header": "swa",
            "SFR RegDef": "sfr",
            "C Source": "source",
            "PlantUML": "puml",
            "HW Spec": "hw_spec",
            "Jama Requirements": "requirements",
        }
        data_key = key_map.get(step_name)
        if data_key and data_key in self.data:
            self._save_intermediary(data_key, self.data[data_key])

    def _print_time_distribution(self, total_elapsed: float):
        """Print a table of step timings."""
        print("\n" + "=" * 60)
        print("  TIME DISTRIBUTION")
        print("=" * 60)
        print(f"  {'Step':<30} {'Time':>10} {'%':>8}")
        print("  " + "-" * 50)
        for entry in self._step_times:
            pct = (entry["elapsed"] / total_elapsed * 100) if total_elapsed > 0 else 0
            print(f"  {entry['step']:<30} {entry['elapsed']:>9.1f}s {pct:>7.1f}%")
        print("  " + "-" * 50)
        print(f"  {'TOTAL':<30} {total_elapsed:>9.1f}s {100.0:>7.1f}%")
        print()

    def _print_schema_summary(self):
        """Query Neo4j and Qdrant for schema summary and print."""
        # -- Neo4j schema --
        if self._kg and not self.skip_kg:
            print("=" * 60)
            print("  NEO4J SCHEMA SUMMARY")
            print("=" * 60)
            try:
                with self._kg._driver.session(database="neo4j") as sess:
                    # Node labels + counts
                    result = sess.run(
                        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
                    )
                    print(f"  {'Label':<30} {'Count':>10}")
                    print("  " + "-" * 42)
                    total_nodes = 0
                    for rec in result:
                        print(f"  {rec['label']:<30} {rec['cnt']:>10}")
                        total_nodes += rec["cnt"]
                    print("  " + "-" * 42)
                    print(f"  {'TOTAL NODES':<30} {total_nodes:>10}")
                    print()

                    # Relationship types + counts
                    result = sess.run(
                        "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS cnt ORDER BY cnt DESC"
                    )
                    print(f"  {'Relationship':<30} {'Count':>10}")
                    print("  " + "-" * 42)
                    total_rels = 0
                    for rec in result:
                        print(f"  {rec['rel']:<30} {rec['cnt']:>10}")
                        total_rels += rec["cnt"]
                    print("  " + "-" * 42)
                    print(f"  {'TOTAL RELATIONSHIPS':<30} {total_rels:>10}")
            except Exception as e:
                logger.warning("Failed to query Neo4j schema: %s", e)
            print()

        # -- Qdrant schema --
        if self._rag and not self.skip_rag:
            print("=" * 60)
            print("  QDRANT SCHEMA SUMMARY")
            print("=" * 60)
            try:
                coll_name = self.module.lower()
                coll = self._rag._client.get_collection(coll_name)
                pts = coll.count()
                print(f"  Collection: {coll_name}")
                print(f"  Total points: {pts}")
            except Exception as e:
                logger.warning("Failed to query Qdrant schema: %s", e)
            print()

    def run(
        self,
        skip_swa: bool = False,
        skip_sfr: bool = False,
        skip_source: bool = False,
        skip_puml: bool = False,
        skip_hw: bool = False,
        skip_jama: bool = False,
        hw_md_path: Optional[Path] = None,
        save_intermediary: bool = False,
    ):
        """Run the full ILLD ingestion pipeline."""
        t0 = time.time()
        start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Suppress verbose third-party loggers
        for name in ("httpx", "httpcore", "urllib3", "neo4j", "qdrant_client",
                     "openai", "langchain", "langchain_openai"):
            logging.getLogger(name).setLevel(logging.WARNING)

        # Init intermediary saving
        self._init_intermediary_dir(save_intermediary)

        print("\n" + "=" * 60)
        print(f"  ILLD INGESTION PIPELINE — Module: {self.module}")
        print(f"  Started: {start_ts}")
        print(f"  Mode: {'REMOTE (API fetch)' if self._fetcher else 'LOCAL (repo clones)'}")
        print(f"  Dry run: {self.dry_run}")
        print(f"  Clear DB: {self.clear}")
        print(f"  KG: {'SKIP' if self.skip_kg else 'YES'}")
        print(f"  RAG: {'SKIP' if self.skip_rag else 'YES'}")
        if self._intermediary_dir:
            print(f"  Intermediary: {self._intermediary_dir}")
        print("=" * 60 + "\n")

        if not skip_swa:
            self._run_step("SWA Header", self.step_swa)
        if not skip_sfr:
            self._run_step("SFR RegDef", self.step_sfr)
        if not skip_source:
            self._run_step("C Source", self.step_source)
        if not skip_puml:
            self._run_step("PlantUML", self.step_puml)
        if not skip_hw:
            self._run_step("HW Spec", self.step_hw_spec, hw_md_path)
        if not skip_jama:
            self._run_step("Jama Requirements", self.step_requirements)

        t_cross = time.time()
        self.step_cross_relationships()
        self._track_step("Cross Relationships", t_cross)

        elapsed = time.time() - t0
        end_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Summary
        print("\n" + "=" * 60)
        print(f"  PIPELINE COMPLETE — {elapsed:.1f}s")
        print(f"  Finished: {end_ts}")
        print("=" * 60)

        if self.kg and not self.skip_kg:
            self.kg.print_summary()
        if self.rag and not self.skip_rag:
            self.rag.print_summary()

        # Time distribution table
        self._print_time_distribution(elapsed)

        # Schema summary (live DB queries)
        self._print_schema_summary()

        # Cleanup
        if self._kg:
            self._kg.close()
        if self._fetcher:
            self._fetcher.close()
            if self._cleanup_temp:
                import shutil
                logger.info("Cleaning up temp files: %s", self._fetcher.work_dir)
                shutil.rmtree(self._fetcher.work_dir, ignore_errors=True)
            else:
                logger.info("Temp files kept at: %s", self._fetcher.work_dir)

        return {
            "module": self.module,
            "elapsed": elapsed,
            "kg_stats": dict(self._kg.stats) if self._kg else {},
            "rag_stats": dict(self._rag.stats) if self._rag else {},
            "step_times": self._step_times,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ILLD Ingestion Pipeline — parse and ingest into KG + RAG",
    )
    parser.add_argument("--module", required=True, help="Module name (e.g. CXPI, SPI, CAN, LIN)")
    parser.add_argument("--remote", action="store_true",
                        help="Fetch files from GitLab/Bitbucket APIs (requires tokens in .env)")
    parser.add_argument("--gitlab-repo", type=Path, default=None,
                        help="Path to local aurix_rc1_illd_platform clone")
    parser.add_argument("--bitbucket-repo", type=Path, default=None,
                        help="Path to local rc1_sw_dep clone (HW PDFs)")
    parser.add_argument("--hw-md", type=Path, default=None,
                        help="Pre-converted HW spec markdown (skip PDF conversion)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear Neo4j database before ingestion")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, no DB writes")
    parser.add_argument("--skip-kg", action="store_true",
                        help="Skip Knowledge Graph ingestion")
    parser.add_argument("--skip-rag", action="store_true",
                        help="Skip RAG vector store ingestion")
    parser.add_argument("--skip-swa", action="store_true")
    parser.add_argument("--skip-sfr", action="store_true")
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument("--skip-puml", action="store_true")
    parser.add_argument("--skip-hw", action="store_true",
                        help="Skip HW spec PDF/markdown")
    parser.add_argument("--skip-jama", action="store_true",
                        help="Skip Jama requirements fetch")
    parser.add_argument("--keep-temp", action="store_true",
                        help="Keep temp files after pipeline completes (default: auto-delete)")
    parser.add_argument("--save-intermediary", action="store_true",
                        help="Save intermediary parsed results (JSON) for cross-verification")

    args = parser.parse_args()

    pipeline = ILLDPipeline(
        module=args.module,
        gitlab_repo_path=args.gitlab_repo,
        bitbucket_repo_path=args.bitbucket_repo,
        dry_run=args.dry_run,
        clear=args.clear,
        skip_kg=args.skip_kg,
        skip_rag=args.skip_rag,
        remote=args.remote,
        cleanup_temp=not args.keep_temp,
    )

    pipeline.run(
        skip_swa=args.skip_swa,
        skip_sfr=args.skip_sfr,
        skip_source=args.skip_source,
        skip_puml=args.skip_puml,
        skip_hw=args.skip_hw,
        skip_jama=args.skip_jama,
        hw_md_path=args.hw_md,
        save_intermediary=args.save_intermediary,
    )


if __name__ == "__main__":
    main()

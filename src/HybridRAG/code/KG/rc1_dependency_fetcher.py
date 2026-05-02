"""
Fetch RC1 MCAL artifacts from Bitbucket for Knowledge Graph ingestion.

RC1 uses a different repo structure than A3G:
- Source code: ``aurix_rc1_sw_mcal_dev_{mod}`` (vs A3G ``aurix3g_sw_mcal_tc4xx_{mod}_src``)
- SFR headers: ``aurix_rc1_sw_mcal_sfr`` at ``ssc/RC1S16/inc/`` (vs A3G ``ssc/inc/``)
- Cross-module: ``aurix_rc1_sw_mcal_dev_generictypes`` for Std_Types.h, Platform_Types.h
- Sum configs: ``aurix_rc1_sw_mcal_dev_{mod}_ver`` at ``cfg/Compile/`` (arxml only — no generated .h)
- Project key: ``AURIXRC1MCAL`` (vs A3G ``ATVA3GMCAL``)

This module provides three classes mirroring A3G's ``dependency_fetcher.py``:

- **RC1DependencyFetcher**: Downloads cross-module headers for clang parsing.
- **RC1SumConfigFetcher**: Downloads Sum compile configurations (arxml + generated .h when available).
- **RC1SourceRepoFetcher**: Shallow-clones RC1 module repos (source, SFR).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BITBUCKET_PROJECT = "AURIXRC1MCAL"
BITBUCKET_BASE_URL = "https://bitbucket.vih.infineon.com"

# Device subfamily — RC1 uses RC1S16 (vs A3G's TC4xx)
RC1_SFR_DEVICE = "RC1S16"

# ---------------------------------------------------------------------------
# Cross-module dependency manifest for RC1
# Maps  repo_slug → { path_in_repo: [header_filenames] }
#
# RC1 has different repo naming than A3G — headers come from:
#   - aurix_rc1_sw_mcal_dev_generictypes  (Std_Types.h, Platform_Types.h)
#   - aurix_rc1_sw_mcal_dev_rma           (Rma.h)
#   - aurix_rc1_sw_mcal_dev_dma           (Dma.h)
#   - aurix_rc1_sw_mcal_dev_mcu           (Mcu.h) [if needed]
#   - aurix_rc1_sw_mcal_dev_tinfra        (TInfra.h)
# ---------------------------------------------------------------------------
DEPENDENCY_MANIFEST: Dict[str, Dict[str, List[str]]] = {
    "aurix_rc1_sw_mcal_dev_generictypes": {
        # AUTOSAR platform type headers
        "ssc/inc": [
            "Std_Types.h",
            "Platform_Types.h",
        ],
    },
    "aurix_rc1_sw_mcal_dev_rma": {
        # Resource Manager Abstraction
        "ssc/inc": ["Rma.h"],
    },
    "aurix_rc1_sw_mcal_dev_dma": {
        "ssc/inc": ["Dma.h"],
    },
    "aurix_rc1_sw_mcal_dev_tinfra": {
        "ssc/inc": ["TInfra.h"],
    },
}

# Headers that RC1 modules include but which are either generated
# (by code generators from arxml) or conditionally present.
# Create empty stubs so clang doesn't error on missing includes.
EMPTY_STUBS: List[str] = [
    # AUTOSAR infrastructure stubs (not found in any RC1 repo)
    "Det.h",
    "Dem.h",
    "Dem_Cfg.h",
    "EcuM.h",
    "EcuM_Cbk.h",
    "Os.h",
    "Os_Compiler.h",
    "Compiler.h",
    "Compiler_Cfg.h",
    # McalErrHndlr — RC1-specific error handling (may not exist as standalone)
    "McalErrHndlr_ErrorTypes.h",
    "McalErrHndlr_SafetyError.h",
    # MemMap — generated per-module
    "Gpt_MemMap.h",
    # SchM — generated per-module
    "SchM_Gpt.h",
    # Cfg headers — generated from arxml by code generators
    "Gpt_Cfg.h",
    "Gpt_PBcfg.h",
    "Gpt_Data.h",
    "Gpt_Externals.h",
]

# Sum configuration defaults (GPT module, RC1S16 device)
SUM_BASE_PATH = "cfg/Compile"
DEFAULT_SUM_CONFIGS: List[str] = [
    "AS4100_RC1S16_529_Bcc_Config1",
    "AS4100_RC1S16_529_Bcc_Config2",
    "AS4100_RC1S16_529_Bcc_Config3",
    "AS4100_RC1S16_529_Bcc_Config4",
    "AS4100_RC1S16_529_Bcc_Config5",
    "AS4100_RC1S16_529_Bcc_Config6",
    "AS4100_RC1S16_529_Bcc_Config7",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env from the repo's ``env/`` directory if present."""
    env_path = Path(__file__).resolve().parents[4] / "env" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _make_connector(repo: str):
    """Create a ``BitbucketConnector`` for a given RC1 repo slug."""
    from src.IngestionPipeline.Connectors.BitbucketConnector import (
        BitbucketConnector,
    )

    _load_env()
    return BitbucketConnector(
        base_url=BITBUCKET_BASE_URL,
        project=BITBUCKET_PROJECT,
        repo=repo,
        username=os.environ.get("IFX_USERNAME"),
        password=os.environ.get("IFX_PASSWORD"),
        verify_ssl=False,
        ref="master",
    )


def _module_stubs(module: str) -> List[str]:
    """Return module-specific stub headers.

    Some stubs are module-specific (e.g. ``Gpt_MemMap.h``).
    This function generates them from the base EMPTY_STUBS list,
    replacing ``Gpt`` with the target module name.
    """
    mod_cap = module.capitalize()  # e.g. "Gpt"
    if mod_cap == "Gpt":
        return list(EMPTY_STUBS)  # Already GPT-specific

    # Replace Gpt-specific stubs with module-specific ones
    stubs = []
    for stub in EMPTY_STUBS:
        if stub.startswith("Gpt_"):
            stubs.append(stub.replace("Gpt_", f"{mod_cap}_"))
        elif stub.startswith("SchM_Gpt"):
            stubs.append(stub.replace("SchM_Gpt", f"SchM_{mod_cap}"))
        else:
            stubs.append(stub)
    return stubs


# ---------------------------------------------------------------------------
# RC1DependencyFetcher
# ---------------------------------------------------------------------------

class RC1DependencyFetcher:
    """Download real cross-module dependency headers from RC1 Bitbucket repos.

    All files land in a single flat directory (no subdirectories) so it
    can be passed directly as a ``-I`` include path to clang.
    """

    def __init__(
        self,
        output_dir: Path,
        module: str = "GPT",
        *,
        force: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.module = module.upper()
        self.force = force
        self._connectors: Dict[str, object] = {}

    def _connector(self, repo: str):
        if repo not in self._connectors:
            self._connectors[repo] = _make_connector(repo)
        return self._connectors[repo]

    def fetch_all(self) -> Path:
        """Download every dependency header + create empty stubs.

        Returns *output_dir* so callers can use it directly as an
        include-path component.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        marker = self.output_dir / ".deps_fetched"
        if marker.exists() and not self.force:
            count = sum(1 for f in self.output_dir.iterdir() if f.suffix == ".h")
            logger.info(
                "RC1 dependencies already present (%d headers) in %s "
                "(use --force-fetch to refresh)",
                count,
                self.output_dir,
            )
            return self.output_dir

        fetched = 0
        failed: List[str] = []

        for repo_slug, paths in DEPENDENCY_MANIFEST.items():
            conn = self._connector(repo_slug)
            for dir_in_repo, filenames in paths.items():
                for fname in filenames:
                    remote = f"{dir_in_repo}/{fname}" if dir_in_repo else fname
                    local = self.output_dir / fname
                    if local.exists() and not self.force:
                        fetched += 1
                        continue
                    try:
                        fc = conn.get_file_content(remote)
                        local.write_text(fc.content, encoding="utf-8")
                        logger.info("  fetched  %-35s  from %s", fname, repo_slug)
                        fetched += 1
                    except Exception as exc:
                        logger.error(
                            "  FAILED   %-35s  from %s/%s: %s",
                            fname, repo_slug, remote, exc,
                        )
                        failed.append(fname)

        # Create empty stubs for generated / missing includes
        stubs = _module_stubs(self.module)
        for stub in stubs:
            path = self.output_dir / stub
            if not path.exists() or self.force:
                path.write_text(
                    f"/* Auto-generated empty stub for {stub} (RC1) */\n",
                    encoding="utf-8",
                )
                logger.info("  created  %-35s  (empty stub)", stub)
            fetched += 1

        marker.write_text(f"{fetched}\n", encoding="utf-8")

        if failed:
            logger.warning(
                "RC1 Dependencies: %d fetched, %d FAILED: %s",
                fetched,
                len(failed),
                ", ".join(failed),
            )
        else:
            logger.info(
                "RC1 Dependencies: all %d files ready in %s",
                fetched,
                self.output_dir,
            )

        return self.output_dir


# ---------------------------------------------------------------------------
# RC1SumConfigFetcher
# ---------------------------------------------------------------------------

class RC1SumConfigFetcher:
    """Download Sum compile configurations from RC1 Bitbucket.

    RC1 Sum configs live at ``cfg/Compile/`` in the module's ``*_ver``
    repo.  Unlike A3G, they currently only contain ``.arxml`` files
    (no pre-generated C headers).  This fetcher downloads whatever is
    available — when generated headers appear in the repo, they'll be
    fetched automatically.
    """

    def __init__(
        self,
        output_dir: Path,
        module: str = "GPT",
        *,
        force: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.module = module.upper()
        self.force = force
        self._conn = None

    @property
    def _connector(self):
        if self._conn is None:
            repo = f"aurix_rc1_sw_mcal_dev_{self.module.lower()}_ver"
            self._conn = _make_connector(repo)
        return self._conn

    def discover_configs(self) -> List[str]:
        """List available Sum config names from Bitbucket.

        Returns bare directory names (e.g. ``AS4100_RC1S16_529_Bcc_Config1``).
        """
        try:
            entries = self._connector.list_directory(SUM_BASE_PATH)
            names = sorted(
                e.path.rsplit("/", 1)[-1]
                for e in entries
                if e.entry_type == "DIRECTORY"
            )
            logger.info(
                "Discovered %d RC1 Sum configs for %s: %s",
                len(names), self.module, names,
            )
            return names
        except Exception as exc:
            logger.warning(
                "Could not list RC1 Sum configs from Bitbucket: %s "
                "-- falling back to defaults",
                exc,
            )
            return list(DEFAULT_SUM_CONFIGS)

    def fetch_config(self, config_name: str) -> Path:
        """Download one Sum config.  Returns its local directory path."""
        config_dir = self.output_dir / config_name
        marker = config_dir / ".config_fetched"
        if marker.exists() and not self.force:
            logger.info("RC1 Sum config already fetched: %s", config_name)
            return config_dir

        remote_root = f"{SUM_BASE_PATH}/{config_name}"
        logger.info("Fetching RC1 Sum config %s from Bitbucket ...", config_name)
        total = self._download_tree(remote_root, config_dir)
        marker.write_text(f"{total}\n", encoding="utf-8")
        logger.info(
            "RC1 Sum config %s: %d files downloaded to %s",
            config_name, total, config_dir,
        )
        return config_dir

    def fetch_configs(
        self,
        config_names: Optional[List[str]] = None,
    ) -> Dict[str, Path]:
        """Download one or more Sum configs.

        Parameters
        ----------
        config_names:
            Specific config names to fetch.  ``None`` = discover and
            fetch all available configs.
        """
        if config_names is None:
            config_names = self.discover_configs()
        return {name: self.fetch_config(name) for name in config_names}

    def _download_tree(self, remote_dir: str, local_dir: Path) -> int:
        """Recursively download every file under *remote_dir*."""
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            entries = self._connector.list_directory(remote_dir)
        except Exception as exc:
            logger.warning("  Cannot list %s: %s", remote_dir, exc)
            return 0

        files = [e for e in entries if e.entry_type == "FILE"]
        dirs = [e for e in entries if e.entry_type == "DIRECTORY"]
        total = 0

        if files:
            paths = [e.path for e in files]
            try:
                results = self._connector.get_files_bulk(paths)
                for rpath, fc in results.items():
                    fname = Path(rpath).name
                    (local_dir / fname).write_text(fc.content, encoding="utf-8")
                    total += 1
            except Exception as exc:
                logger.warning(
                    "  Bulk fetch failed for %s (%s); trying one-by-one",
                    remote_dir, exc,
                )
                for fentry in files:
                    try:
                        fc = self._connector.get_file_content(fentry.path)
                        fname = Path(fentry.path).name
                        (local_dir / fname).write_text(
                            fc.content, encoding="utf-8",
                        )
                        total += 1
                    except Exception as exc2:
                        logger.warning("  Failed: %s: %s", fentry.path, exc2)

        for d in dirs:
            dir_name = Path(d.path).name
            total += self._download_tree(d.path, local_dir / dir_name)

        return total


# ---------------------------------------------------------------------------
# RC1SourceRepoFetcher — shallow-clone repos from Bitbucket
# ---------------------------------------------------------------------------

def _repo_slug_for_module(module: str, kind: str) -> str:
    """Derive the RC1 Bitbucket repo slug from a module name and repo kind.

    Parameters
    ----------
    module:
        MCAL module name (e.g. ``"GPT"``, ``"DMA"``).
    kind:
        One of ``"src"``, ``"ver"``, ``"sfr"``.

    Returns
    -------
    str
        The Bitbucket repo slug, e.g. ``"aurix_rc1_sw_mcal_dev_gpt"``.

    RC1 naming patterns:
        src:  aurix_rc1_sw_mcal_dev_{mod}           (source code)
        ver:  aurix_rc1_sw_mcal_dev_{mod}_ver       (verification / Sum configs)
        sfr:  aurix_rc1_sw_mcal_sfr                 (shared SFR repo)
    """
    mod = module.lower()
    if kind == "src":
        return f"aurix_rc1_sw_mcal_dev_{mod}"
    elif kind == "ver":
        return f"aurix_rc1_sw_mcal_dev_{mod}_ver"
    elif kind == "sfr":
        return "aurix_rc1_sw_mcal_sfr"
    raise ValueError(f"Unknown repo kind: {kind!r}")


class RC1SourceRepoFetcher:
    """Shallow-clone RC1 Bitbucket repos needed for module ingestion.

    Parameters
    ----------
    output_dir:
        Base directory for cloned repos (typically ``TEMP_DATA_DIR``).
    module:
        MCAL module name (e.g. ``"GPT"``).
    ref:
        Git ref to clone (branch, tag, or commit). Default ``"master"``.

    Usage
    -----
    >>> fetcher = RC1SourceRepoFetcher(TEMP_DATA_DIR, "GPT")
    >>> paths = fetcher.fetch_all()
    >>> paths["src"]    # Path to cloned source repo
    >>> paths["sfr"]    # Path to cloned SFR repo
    """

    def __init__(
        self,
        output_dir: Path,
        module: str,
        *,
        ref: str = "master",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.module = module.upper()
        self.ref = ref

    def _clone_url(self, repo_slug: str) -> str:
        """Build the HTTPS clone URL for a Bitbucket repo."""
        return (
            f"{BITBUCKET_BASE_URL}/scm/"
            f"{BITBUCKET_PROJECT}/{repo_slug}.git"
        )

    def _clone_or_update(self, repo_slug: str) -> Path:
        """Shallow-clone a repo, or pull if it already exists."""
        local_dir = self.output_dir / repo_slug
        clone_url = self._clone_url(repo_slug)

        if local_dir.exists() and (local_dir / ".git").is_dir():
            logger.info("Repo already cloned: %s — pulling latest ...", repo_slug)
            try:
                subprocess.run(
                    ["git", "fetch", "--depth", "1", "origin", self.ref],
                    cwd=str(local_dir),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=True,
                )
                subprocess.run(
                    ["git", "checkout", f"origin/{self.ref}"],
                    cwd=str(local_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                logger.info("  Updated %s to latest %s", repo_slug, self.ref)
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "  git pull failed for %s (using existing checkout): %s",
                    repo_slug,
                    exc.stderr.strip() if exc.stderr else exc,
                )
            return local_dir

        # Fresh shallow clone
        logger.info("Cloning %s (ref=%s) ...", repo_slug, self.ref)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "git", "clone",
                    "--depth", "1",
                    "--branch", self.ref,
                    "--single-branch",
                    clone_url,
                    str(local_dir),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
            logger.info("  Cloned %s → %s", repo_slug, local_dir)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Failed to clone %s: %s",
                clone_url,
                exc.stderr.strip() if exc.stderr else exc,
            )
            raise RuntimeError(
                f"git clone failed for {repo_slug}. "
                f"Check network access and credentials.\n"
                f"URL: {clone_url}\n"
                f"stderr: {exc.stderr}"
            ) from exc
        except FileNotFoundError:
            raise RuntimeError(
                "git is not installed or not on PATH. "
                "Install git and try again."
            )

        return local_dir

    # -- Public API --------------------------------------------------------

    def fetch_source(self) -> Path:
        """Clone/update the RC1 module source repo.

        Returns path like ``output_dir/aurix_rc1_sw_mcal_dev_gpt``.
        """
        slug = _repo_slug_for_module(self.module, "src")
        return self._clone_or_update(slug)

    def fetch_sfr(self) -> Path:
        """Clone/update the shared RC1 SFR repo.

        Returns path like ``output_dir/aurix_rc1_sw_mcal_sfr``.

        Note: RC1 SFR headers are at ``ssc/RC1S16/inc/`` (not ``ssc/inc/``
        like A3G). The pipeline must use the correct subdirectory.
        """
        slug = _repo_slug_for_module(self.module, "sfr")
        return self._clone_or_update(slug)

    def fetch_ver(self) -> Path:
        """Clone/update the RC1 module verification repo.

        Returns path like ``output_dir/aurix_rc1_sw_mcal_dev_gpt_ver``.
        Contains Sum compile configs under ``cfg/Compile/``.
        """
        slug = _repo_slug_for_module(self.module, "ver")
        return self._clone_or_update(slug)

    def fetch_all(self) -> Dict[str, Path]:
        """Clone/update all repos needed for a full module ingestion.

        Returns a dict mapping repo kind → local path::

            {"src": Path(…), "sfr": Path(…), "ver": Path(…)}
        """
        return {
            "src": self.fetch_source(),
            "sfr": self.fetch_sfr(),
            "ver": self.fetch_ver(),
        }

    @staticmethod
    def sfr_include_dir(sfr_repo_path: Path) -> Path:
        """Return the actual SFR header directory within the cloned repo.

        RC1 SFR headers live at ``ssc/RC1S16/inc/`` (vs A3G ``ssc/inc/``).
        """
        return sfr_repo_path / "ssc" / RC1_SFR_DEVICE / "inc"

    @staticmethod
    def sfr_repo_dir(sfr_repo_path: Path) -> Path:
        """Return the SFR device directory (parent of inc/).

        For ``sfr_parsers.py`` which expects a directory containing
        device subdirectories.  RC1 layout is:

            aurix_rc1_sw_mcal_sfr/ssc/RC1S16/inc/Ifx*.h

        We return ``ssc/`` so that ``discover_devices()`` can be called
        with ``devices=["RC1S16"]`` (the device subfolder is ``RC1S16/inc/``).

        However, sfr_parsers.py expects ``{device}/Ifx*.h`` directly — so
        we return the ``ssc/`` directory and the device folder must contain
        the .h files directly.  Since RC1 puts them under ``RC1S16/inc/``,
        we return ``ssc/RC1S16`` as the pseudo repo root.
        """
        return sfr_repo_path / "ssc"

    def source_dir(self) -> Path:
        """Return the source repo root directory.

        Returns path like ``output_dir/aurix_rc1_sw_mcal_dev_gpt``.
        Source headers: ``ssc/inc/``, source code: ``ssc/src/``.
        """
        slug = _repo_slug_for_module(self.module, "src")
        return self.output_dir / slug

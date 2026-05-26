"""
Fetch cross-module dependency headers and Sum configuration files from Bitbucket.

All headers are fetched from their actual production repositories — no stubs.
This module provides three classes:

- **DependencyFetcher**: Downloads real cross-module headers (Std_Types.h,
  McalUtil.h, Det.h, etc.) from their respective Bitbucket repos and creates
  minimal empty stubs for headers that are conditionally included but have
  no effect on parsing (Test_Det.h, Userconfig.h, etc.).

- **SumConfigFetcher**: Downloads Sum (pre-configured) build variants from
  the module's ``*_ver`` repository.  Each Sum config contains fully
  generated CfgMcal files, MemMap files, and SchM files with concrete
  ``#define`` values — the ground truth for AST parsing.

- **SourceRepoFetcher**: Shallow-clones Bitbucket source repositories
  (module src, SFR, val) into a local directory so the pipeline does not
  require manual cloning.
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
BITBUCKET_PROJECT = "ATVA3GMCAL"
BITBUCKET_BASE_URL = "https://bitbucket.vih.infineon.com"

# ---------------------------------------------------------------------------
# Cross-module dependency manifest
# Maps  repo_slug → { path_in_repo: [header_filenames] }
#
# Every header here is a *real* production file fetched from Bitbucket.
# Paths were verified via the Bitbucket browse API.
# ---------------------------------------------------------------------------
DEPENDENCY_MANIFEST: Dict[str, Dict[str, List[str]]] = {
    "aurix3g_sw_mcal_tc4xx_platform": {
        # AUTOSAR platform type headers (root-level files)
        "": [
            "Std_Types.h",
            "Platform_Types.h",
            "Mcal_ErrorTypes.h",
            "Mcal_ExecutionContext.h",
        ],
    },
    "aurix3g_sw_mcal_tc4xx_infra_integration": {
        # Common infrastructure stubs / wrappers
        "00_Common": [
            "Det.h",
            "Dem.h",
            "Dem_cfg.h",
            "Mcal_SafetyError.h",
            "Mcal_OsStub.h",
            "Os.h",
            "Os_Compiler.h",
            "Mcal_AppExecContext.h",
        ],
    },
    "aurix3g_sw_mcal_tc4xx_mcalutil_src": {
        # McalUtil — contains MCALUTIL_SFRREAD / SFRWRITE macros
        "ssc/inc": ["McalUtil.h"],
    },
    "aurix3g_sw_mcal_tc4xx_mcu_src": {
        "ssc/inc": ["Mcu_TimeDelay.h"],
    },
    "aurix3g_sw_mcal_tc4xx_dma_src": {
        "ssc/inc": ["Dma.h"],
    },
    "aurix3g_sw_mcal_tc4xx_gtm_src": {
        "ssc/inc": ["Gtm.h"],
    },
    "aurix3g_sw_mcal_tc4xx_cdsp_src": {
        "ssc/inc": ["Cdsp.h", "Cdsp_Local.h"],
    },
}

# Headers that are transitively included but contain nothing relevant
# for clang parsing.  We create minimal empty files so clang doesn't
# error on missing includes.
EMPTY_STUBS: List[str] = [
    "Test_Det.h",
    "Test_Dem.h",
    "Test_Mcal_SafetyError.h",
    "Userconfig.h",
    "McalUtil_MemMap.h",
    "Cdsp_MemMap.h",
    "Compiler.h",
]

# Sum configuration defaults (ADC module, TC499N device)
SUM_BASE_PATH = "00_Arxml/Sum"
DEFAULT_SUM_CONFIGS: List[str] = [
    "AS460_TC499N_STD_Host_Config1",
    "AS460_TC499N_STD_Host_Config2",
    "AS460_TC499N_STD_Host_Config3",
    "AS460_TC499N_STD_Host_Config4",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env from the repo's ``env/`` directory if present."""
    env_path = Path(__file__).resolve().parents[4] / "env" / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv  # type: ignore[import-untyped]
            load_dotenv(env_path)
        except ImportError:
            pass


def _make_connector(repo: str):
    """Create a ``BitbucketConnector`` for a given repo slug."""
    # Lazy import — avoid hard dependency when this module is not used
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
        verify_ssl=os.environ.get("IFX_VERIFY_SSL", "true").lower() != "false",
        ref="master",
    )


# ---------------------------------------------------------------------------
# DependencyFetcher
# ---------------------------------------------------------------------------

class DependencyFetcher:
    """Download real cross-module dependency headers from Bitbucket.

    All files land in a single flat directory (no subdirectories) so it
    can be passed directly as a ``-I`` include path to clang.
    """

    def __init__(self, output_dir: Path, *, force: bool = False) -> None:
        self.output_dir = Path(output_dir)
        self.force = force
        self._connectors: Dict[str, object] = {}

    def _connector(self, repo: str):
        if repo not in self._connectors:
            self._connectors[repo] = _make_connector(repo)
        return self._connectors[repo]

    # -- public API --------------------------------------------------------

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
                "Dependencies already present (%d headers) in %s "
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
                        logger.info("  fetched  %-30s  from %s", fname, repo_slug)
                        fetched += 1
                    except Exception as exc:
                        logger.error(
                            "  FAILED   %-30s  from %s/%s: %s",
                            fname, repo_slug, remote, exc,
                        )
                        failed.append(fname)

        # Create empty stubs for transitive includes
        for stub in EMPTY_STUBS:
            path = self.output_dir / stub
            if not path.exists() or self.force:
                path.write_text(
                    f"/* Auto-generated empty stub for {stub} */\n",
                    encoding="utf-8",
                )
                logger.info("  created  %-30s  (empty stub)", stub)
            fetched += 1

        marker.write_text(f"{fetched}\n", encoding="utf-8")

        if failed:
            logger.warning(
                "Dependencies: %d fetched, %d FAILED: %s",
                fetched,
                len(failed),
                ", ".join(failed),
            )
        else:
            logger.info("Dependencies: all %d files ready in %s", fetched, self.output_dir)

        return self.output_dir


# ---------------------------------------------------------------------------
# SumConfigFetcher
# ---------------------------------------------------------------------------

class SumConfigFetcher:
    """Download Sum configuration directories from Bitbucket.

    Each Sum config is a fully generated build variant containing
    CfgMcal, MemMap, and SchM files.  These are fetched recursively
    into local directories for clang to consume.
    """

    def __init__(
        self,
        output_dir: Path,
        module: str = "ADC",
        *,
        force: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.module = module.upper()
        self.force = force
        self._connectors: Dict[str, object] = {}

    @property
    def _connector(self):
        """Return the first working Bitbucket connector.

        For multi-repo modules (ETH) we try each sub-module's ver repo
        and return the first one that connects.
        """
        if not self._connectors:
            subs = MODULE_SUB_MODULES.get(self.module)
            if subs:
                for sub in subs:
                    repo = f"aurix3g_sw_mcal_tc4xx_dev_{sub}_ver"
                    self._connectors[sub] = _make_connector(repo)
            else:
                repo = f"aurix3g_sw_mcal_tc4xx_dev_{self.module.lower()}_ver"
                self._connectors[self.module.lower()] = _make_connector(repo)
        # Return first connector (primary sub-module)
        return next(iter(self._connectors.values()))

    @property
    def _all_connectors(self) -> Dict[str, object]:
        """Return all connectors (one per sub-module, or single for standard modules)."""
        _ = self._connector  # ensure initialized
        return self._connectors

    # -- public API --------------------------------------------------------

    def discover_configs(self) -> List[str]:
        """List available Sum config names from Bitbucket.

        Returns bare directory names (e.g. ``AS460_TC4D9_COM_Host_Config1``),
        **not** full Bitbucket paths.  For multi-repo modules, aggregates
        configs from all sub-module ver repos.
        """
        all_names: List[str] = []
        for sub_name, conn in self._all_connectors.items():
            try:
                entries = conn.list_directory(SUM_BASE_PATH)
                names = sorted(
                    e.path.rsplit("/", 1)[-1]
                    for e in entries
                    if e.entry_type == "DIRECTORY"
                )
                logger.info("Discovered %d Sum configs for %s (%s): %s",
                            len(names), self.module, sub_name, names)
                all_names.extend(names)
            except Exception as exc:
                logger.warning(
                    "Could not list Sum configs from %s: %s "
                    "-- skipping",
                    sub_name, exc,
                )
        if not all_names:
            logger.warning("No Sum configs found — falling back to defaults")
            return list(DEFAULT_SUM_CONFIGS)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for n in all_names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        return unique

    def fetch_config(self, config_name: str) -> Path:
        """Download one Sum config.  Returns its local directory path.

        For multi-repo modules, tries each sub-module's ver repo until
        one succeeds.
        """
        config_dir = self.output_dir / config_name
        marker = config_dir / ".config_fetched"
        if marker.exists() and not self.force:
            logger.info("Sum config already fetched: %s", config_name)
            return config_dir

        remote_root = f"{SUM_BASE_PATH}/{config_name}"
        logger.info("Fetching Sum config %s from Bitbucket ...", config_name)

        # Try each connector — config may live in any sub-module's ver repo
        for sub_name, conn in self._all_connectors.items():
            try:
                total = self._download_tree(remote_root, config_dir, connector=conn)
                if total > 0:
                    marker.write_text(f"{total}\n", encoding="utf-8")
                    logger.info(
                        "Sum config %s: %d files downloaded from %s to %s",
                        config_name, total, sub_name, config_dir,
                    )
                    return config_dir
            except Exception as exc:
                logger.debug("Config %s not in %s: %s", config_name, sub_name, exc)

        # Fallback: use primary connector (will log its own errors)
        total = self._download_tree(remote_root, config_dir)
        marker.write_text(f"{total}\n", encoding="utf-8")
        logger.info(
            "Sum config %s: %d files downloaded to %s",
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

        Returns
        -------
        dict mapping *config_name* -> local directory path.
        """
        if config_names is None:
            config_names = self.discover_configs()
        return {name: self.fetch_config(name) for name in config_names}

    # -- internal helpers --------------------------------------------------

    def _download_tree(self, remote_dir: str, local_dir: Path,
                        connector=None) -> int:
        """Recursively download every file under *remote_dir*."""
        conn = connector or self._connector
        local_dir.mkdir(parents=True, exist_ok=True)
        try:
            entries = conn.list_directory(remote_dir)
        except Exception as exc:
            logger.warning("  Cannot list %s: %s", remote_dir, exc)
            return 0

        files = [e for e in entries if e.entry_type == "FILE"]
        dirs = [e for e in entries if e.entry_type == "DIRECTORY"]
        total = 0

        # Bulk-fetch all files in this directory
        # entry.path is the full repo-relative path (e.g. "00_Arxml/Sum/.../Adc_Cfg.h")
        if files:
            paths = [e.path for e in files]
            try:
                results = conn.get_files_bulk(paths)
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
                        fc = conn.get_file_content(fentry.path)
                        fname = Path(fentry.path).name
                        (local_dir / fname).write_text(
                            fc.content, encoding="utf-8",
                        )
                        total += 1
                    except Exception as exc2:
                        logger.warning("  Failed: %s: %s", fentry.path, exc2)

        # Recurse into sub-directories
        # entry.path is already the full path — use it directly
        for d in dirs:
            dir_name = Path(d.path).name
            total += self._download_tree(
                d.path,
                local_dir / dir_name,
                connector=conn,
            )

        return total


# ---------------------------------------------------------------------------
# SourceRepoFetcher — shallow-clone repos from Bitbucket
# ---------------------------------------------------------------------------

# Repos that are shared across all modules (clone once).
_SHARED_REPOS: List[str] = [
    "aurix3g_sw_mcal_tc4xx_infra_sfr",
]

# Modules whose Bitbucket repos use non-standard naming.
# Key = canonical module name (uppercase), Value = list of Bitbucket sub-module
# suffixes.  For these modules, repo slug generation expands into multiple repos.
# E.g. ETH → eth_17_leth_src + eth_17_geth_src (instead of plain eth_src).
MODULE_SUB_MODULES: Dict[str, List[str]] = {
    "ETH": ["eth_17_leth", "eth_17_geth"],
    "FEE": ["fee_dflash", "fee_drram"],
    "CAN": ["can_17_mcmcan"],
    "LIN": ["lin_17_asclin"],
    "PWM": ["pwm_17_timerip"],
    "WDG": ["wdg_17_wtu"],
}

# Cross-module source repos whose ssc/inc headers are needed for
# resolving cross-module #includes (e.g. ADC includes Dma.h, Gtm.h).
# These are shallow-cloned alongside the target module so that clang
# can resolve ALL cross-module references without relying on stubs.
_CROSS_MODULE_REPOS: List[str] = [
    "aurix3g_sw_mcal_tc4xx_dma_src",
    "aurix3g_sw_mcal_tc4xx_gtm_src",
    "aurix3g_sw_mcal_tc4xx_cdsp_src",
    "aurix3g_sw_mcal_tc4xx_mcalutil_src",
    "aurix3g_sw_mcal_tc4xx_mcu_src",
    "aurix3g_sw_mcal_tc4xx_port_src",
    "aurix3g_sw_mcal_tc4xx_adc_src",
    "aurix3g_sw_mcal_tc4xx_gpt_src",
    "aurix3g_sw_mcal_tc4xx_spi_src",
    "aurix3g_sw_mcal_tc4xx_icu_src",
    "aurix3g_sw_mcal_tc4xx_pwm_17_timerip_src",
    "aurix3g_sw_mcal_tc4xx_can_17_mcmcan_src",
    "aurix3g_sw_mcal_tc4xx_lin_17_asclin_src",
    "aurix3g_sw_mcal_tc4xx_eth_17_leth_src",
    "aurix3g_sw_mcal_tc4xx_eth_17_geth_src",
    "aurix3g_sw_mcal_tc4xx_fee_dflash_src",
    "aurix3g_sw_mcal_tc4xx_fee_drram_src",
    "aurix3g_sw_mcal_tc4xx_wdg_17_wtu_src",
]


def _repo_slug_for_module(module: str, kind: str) -> str:
    """Derive the Bitbucket repo slug from a module name and repo kind.

    Parameters
    ----------
    module:
        MCAL module name (e.g. ``"ADC"``, ``"DMA"``).
        For multi-repo modules (ETH) pass the sub-module suffix directly
        (e.g. ``"eth_17_leth"``).
    kind:
        One of ``"src"``, ``"val"``, ``"sfr"``.

    Returns
    -------
    str
        The Bitbucket repo slug, e.g. ``"aurix3g_sw_mcal_tc4xx_adc_src"``.
    """
    mod = module.lower()
    if kind == "src":
        return f"aurix3g_sw_mcal_tc4xx_{mod}_src"
    elif kind == "val":
        return f"aurix3g_sw_mcal_tc4xx_val_{mod}"
    elif kind == "sfr":
        return "aurix3g_sw_mcal_tc4xx_infra_sfr"
    raise ValueError(f"Unknown repo kind: {kind!r}")


def repo_slugs_for_module(module: str, kind: str) -> List[str]:
    """Return all Bitbucket repo slugs for a module (handles multi-repo modules).

    For standard modules returns a single-element list.
    For multi-repo modules (e.g. ETH → eth_17_leth + eth_17_geth) returns
    one slug per sub-module.
    """
    upper = module.upper()
    if upper in MODULE_SUB_MODULES:
        return [_repo_slug_for_module(sub, kind) for sub in MODULE_SUB_MODULES[upper]]
    return [_repo_slug_for_module(module, kind)]


class SourceRepoFetcher:
    """Shallow-clone Bitbucket repos needed for module ingestion.

    Replaces the manual ``git clone`` step — given a module name, this
    fetches the source, SFR, and validation repos automatically via
    ``git clone --depth 1``.

    Parameters
    ----------
    output_dir:
        Base directory for cloned repos (typically ``TEMP_DATA_DIR``).
    module:
        MCAL module name (e.g. ``"ADC"``).
    ref:
        Git ref to clone (branch, tag, or commit). Default ``"master"``.

    Usage
    -----
    >>> fetcher = SourceRepoFetcher(TEMP_DATA_DIR, "ADC")
    >>> paths = fetcher.fetch_all()
    >>> paths["src"]    # Path to cloned source repo
    >>> paths["sfr"]    # Path to cloned SFR repo
    >>> paths["val"]    # Path to cloned validation repo
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
        """Shallow-clone a repo, or pull if it already exists.

        Returns the local directory path.
        """
        local_dir = self.output_dir / repo_slug
        clone_url = self._clone_url(repo_slug)

        if local_dir.exists() and (local_dir / ".git").is_dir():
            # Already cloned — fetch latest for the target ref
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
        """Clone/update the module source repo(s).

        For multi-repo modules (ETH), clones all sub-module repos and
        returns the path to the first one.  Use ``fetch_source_all()``
        to get all paths.
        """
        slugs = repo_slugs_for_module(self.module, "src")
        paths = [self._clone_or_update(slug) for slug in slugs]
        return paths[0]

    def fetch_source_all(self) -> List[Path]:
        """Clone/update all source repos for the module.

        Returns a list of paths (single element for standard modules,
        multiple for multi-repo modules like ETH).
        """
        slugs = repo_slugs_for_module(self.module, "src")
        return [self._clone_or_update(slug) for slug in slugs]

    def fetch_sfr(self) -> Path:
        """Clone/update the shared SFR infrastructure repo.

        Returns path like ``output_dir/aurix3g_sw_mcal_tc4xx_infra_sfr``.
        """
        slug = _repo_slug_for_module(self.module, "sfr")
        return self._clone_or_update(slug)

    def fetch_platform(self) -> Path:
        """Clone/update the shared platform repo.

        Contains Std_Types.h, Platform_Types.h, Mcal_ErrorTypes.h, and
        other core AUTOSAR platform headers (at repo root).

        Returns path like ``output_dir/aurix3g_sw_mcal_tc4xx_platform``.
        """
        return self._clone_or_update("aurix3g_sw_mcal_tc4xx_platform")

    def fetch_val(self) -> Path:
        """Clone/update the module validation/test-spec repo(s).

        For multi-repo modules, clones all sub-module val repos and
        returns the first path.
        """
        slugs = repo_slugs_for_module(self.module, "val")
        paths = [self._clone_or_update(slug) for slug in slugs]
        return paths[0]

    def fetch_cross_module_repos(self) -> Dict[str, Path]:
        """Clone/update cross-module source repos for header resolution.

        Ensures that ssc/inc from all MCAL modules is available locally
        so clang can resolve cross-module #includes (e.g. ADC → Dma.h,
        DMA → Gtm.h).  Skips the target module (already cloned by
        ``fetch_source``).

        Returns dict mapping repo_slug → local path.
        """
        results: Dict[str, Path] = {}
        target_slugs = set(repo_slugs_for_module(self.module, "src"))
        for slug in _CROSS_MODULE_REPOS:
            if slug in target_slugs:
                continue  # Already cloned as the primary source
            try:
                results[slug] = self._clone_or_update(slug)
            except RuntimeError as exc:
                # Non-fatal: some modules may not exist yet
                logger.warning(
                    "Could not clone cross-module repo %s (skipping): %s",
                    slug, exc,
                )
        return results

    def fetch_all(self) -> Dict[str, Path]:
        """Clone/update all repos needed for a full module ingestion.

        Returns a dict mapping repo kind → local path::

            {"src": Path(...), "sfr": Path(...), "platform": Path(...),
             "val": Path(...), "cross_module": {slug: Path, ...}}
        """
        result = {
            "src": self.fetch_source(),
            "sfr": self.fetch_sfr(),
            "val": self.fetch_val(),
        }
        # Platform repo is optional — may not exist or user may lack access
        try:
            result["platform"] = self.fetch_platform()
        except RuntimeError as exc:
            logger.warning(
                "Could not clone infra_platform repo (skipping — "
                "stubs will be used as fallback): %s", exc,
            )
            result["platform"] = None
        result["cross_module"] = self.fetch_cross_module_repos()
        return result

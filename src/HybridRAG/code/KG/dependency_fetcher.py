"""
Fetch cross-module dependency headers and Sum configuration files from Bitbucket.

All headers are fetched from their actual production repositories — no stubs.
This module provides two classes:

- **DependencyFetcher**: Downloads real cross-module headers (Std_Types.h,
  McalUtil.h, Det.h, etc.) from their respective Bitbucket repos and creates
  minimal empty stubs for headers that are conditionally included but have
  no effect on parsing (Test_Det.h, Userconfig.h, etc.).

- **SumConfigFetcher**: Downloads Sum (pre-configured) build variants from
  the module's ``*_ver`` repository.  Each Sum config contains fully
  generated CfgMcal files, MemMap files, and SchM files with concrete
  ``#define`` values — the ground truth for AST parsing.
"""

from __future__ import annotations

import logging
import os
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
        # AUTOSAR platform type headers
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
        verify_ssl=False,
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
        self._conn = None

    @property
    def _connector(self):
        if self._conn is None:
            repo = f"aurix3g_sw_mcal_tc4xx_dev_{self.module.lower()}_ver"
            self._conn = _make_connector(repo)
        return self._conn

    # -- public API --------------------------------------------------------

    def discover_configs(self) -> List[str]:
        """List available Sum config names from Bitbucket.

        Returns bare directory names (e.g. ``AS460_TC4D9_COM_Host_Config1``),
        **not** full Bitbucket paths.
        """
        try:
            entries = self._connector.list_directory(SUM_BASE_PATH)
            # e.path is repo-relative ("00_Arxml/Sum/ConfigX") — keep only the
            # last component so callers can pass it straight to fetch_config().
            names = sorted(
                e.path.rsplit("/", 1)[-1]
                for e in entries
                if e.entry_type == "DIRECTORY"
            )
            logger.info("Discovered %d Sum configs for %s: %s",
                        len(names), self.module, names)
            return names
        except Exception as exc:
            logger.warning(
                "Could not list Sum configs from Bitbucket: %s "
                "-- falling back to defaults",
                exc,
            )
            return list(DEFAULT_SUM_CONFIGS)

    def fetch_config(self, config_name: str) -> Path:
        """Download one Sum config.  Returns its local directory path."""
        config_dir = self.output_dir / config_name
        marker = config_dir / ".config_fetched"
        if marker.exists() and not self.force:
            logger.info("Sum config already fetched: %s", config_name)
            return config_dir

        remote_root = f"{SUM_BASE_PATH}/{config_name}"
        logger.info("Fetching Sum config %s from Bitbucket ...", config_name)
        total = self._download_tree(remote_root, config_dir)
        marker.write_text(f"{total}\n", encoding="utf-8")
        logger.info(
            "Sum config %s: %d files downloaded to %s",
            config_name,
            total,
            config_dir,
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

        # Bulk-fetch all files in this directory
        # entry.path is the full repo-relative path (e.g. "00_Arxml/Sum/.../Adc_Cfg.h")
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

        # Recurse into sub-directories
        # entry.path is already the full path — use it directly
        for d in dirs:
            dir_name = Path(d.path).name
            total += self._download_tree(
                d.path,
                local_dir / dir_name,
            )

        return total

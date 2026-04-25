"""
Header Fetcher
==============

Optionally fetches real MCAL infrastructure headers from Bitbucket,
replacing the minimal stubs shipped in ``stubs/``.

Real headers improve clang's type resolution and macro expansion, but
they are **not required** — the common stubs are sufficient for
structural analysis.

When Bitbucket is unavailable (no credentials, network issues), the
fetcher returns ``None`` and the system falls back to stubs.

Usage::

    from header_fetcher import HeaderFetcher

    fetcher = HeaderFetcher(cache_dir=Path("temp/real_headers"))
    real_dir = fetcher.fetch()   # Path or None
"""

import os
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Map: header filename → (repo_slug, path_in_repo)
HEADER_MAP = {
    # Platform repo  (Std_Types, Platform_Types, Compiler, …)
    "Std_Types.h": (
        "aurix3g_sw_mcal_tc4xx_platform", "Std_Types.h",
    ),
    "Platform_Types.h": (
        "aurix3g_sw_mcal_tc4xx_platform", "Platform_Types.h",
    ),
    "Compiler.h": (
        "aurix3g_sw_mcal_tc4xx_platform", "Compiler.h",
    ),
    "Mcal_ErrorTypes.h": (
        "aurix3g_sw_mcal_tc4xx_platform", "Mcal_ErrorTypes.h",
    ),
    "Mcal_ExecutionContext.h": (
        "aurix3g_sw_mcal_tc4xx_platform", "Mcal_ExecutionContext.h",
    ),
    # McalUtil repo  (McalLib, McalLib_OsCfg, Mcu_TimeDelay)
    "McalLib.h": (
        "aurix3g_sw_mcal_tc4xx_mcalutil_src", "ssc/inc/McalLib.h",
    ),
    "McalLib_OsCfg.h": (
        "aurix3g_sw_mcal_tc4xx_mcalutil_src", "ssc/inc/McalLib_OsCfg.h",
    ),
    "Mcu_TimeDelay.h": (
        "aurix3g_sw_mcal_tc4xx_mcalutil_src", "ssc/inc/Mcu_TimeDelay.h",
    ),
    # Infra Integration repo  (Det, Dem, Os, Mcal_SafetyError, EcuM)
    "Det.h": (
        "aurix3g_sw_mcal_tc4xx_infra_integration", "00_Common/Det.h",
    ),
    "Dem.h": (
        "aurix3g_sw_mcal_tc4xx_infra_integration", "00_Common/Dem.h",
    ),
    "Mcal_SafetyError.h": (
        "aurix3g_sw_mcal_tc4xx_infra_integration", "00_Common/Mcal_SafetyError.h",
    ),
    "Os.h": (
        "aurix3g_sw_mcal_tc4xx_infra_integration", "00_Common/Os.h",
    ),
    "EcuM.h": (
        "aurix3g_sw_mcal_tc4xx_infra_integration", "00_Common/EcuM.h",
    ),
}

# Default Bitbucket project key for MCAL repos
_PROJECT = "ATVA3GMCAL"
_BASE_URL = "https://bitbucket.vih.infineon.com"
_CACHE_TTL_SECONDS = 86400  # 24 hours


class HeaderFetcher:
    """Fetch real MCAL infrastructure headers from Bitbucket.

    Parameters
    ----------
    cache_dir : Path
        Local directory to cache fetched headers.
    project : str
        Bitbucket project key (default ``ATVA3GMCAL``).
    """

    def __init__(self, cache_dir: Path, project: str = _PROJECT):
        self.cache_dir = Path(cache_dir)
        self.project = project

    def fetch(self) -> Optional[Path]:
        """Fetch headers to *cache_dir*.

        Returns *cache_dir* on success (even partial), ``None`` if
        Bitbucket is unreachable.
        """
        # Check freshness of cached headers
        marker = self.cache_dir / ".fetched"
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < _CACHE_TTL_SECONDS:
                logger.info(
                    "Using cached real headers from %s (age: %.1fh)",
                    self.cache_dir, age / 3600,
                )
                return self.cache_dir

        connector = self._create_connector()
        if connector is None:
            return None

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        success = 0

        for header_name, (repo_slug, repo_path) in HEADER_MAP.items():
            try:
                fc = connector.get_file_content(
                    repo_path,
                    project=self.project,
                    repo=repo_slug,
                )
                (self.cache_dir / header_name).write_text(
                    fc.content, encoding="utf-8",
                )
                success += 1
            except Exception as exc:
                logger.warning(
                    "Failed to fetch %s from %s/%s: %s",
                    header_name, repo_slug, repo_path, exc,
                )

        if success > 0:
            marker.touch()
            logger.info(
                "Fetched %d/%d real headers to %s",
                success, len(HEADER_MAP), self.cache_dir,
            )
            return self.cache_dir

        logger.warning("Could not fetch any real headers from Bitbucket")
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _create_connector():
        """Build a BitbucketConnector from environment variables.

        Returns ``None`` when credentials are missing or connection fails.
        """
        token = os.environ.get("BITBUCKET_TOKEN")
        username = os.environ.get("IFX_USERNAME")
        password = os.environ.get("IFX_PASSWORD")

        if not token and not (username and password):
            logger.info(
                "No Bitbucket credentials found (BITBUCKET_TOKEN or "
                "IFX_USERNAME/IFX_PASSWORD) — skipping real header fetch"
            )
            return None

        try:
            from src.IngestionPipeline.Connectors.BitbucketConnector import (
                BitbucketConnector,
            )
        except ImportError:
            try:
                # Fallback: if src/ is already on sys.path
                from IngestionPipeline.Connectors.BitbucketConnector import (
                    BitbucketConnector,
                )
            except ImportError:
                logger.warning("BitbucketConnector not importable — skipping real header fetch")
                return None

        try:
            conn = BitbucketConnector(
                base_url=_BASE_URL,
                project=_PROJECT,
                repo="aurix3g_sw_mcal_tc4xx_platform",  # default repo
                token=token,
                username=username,
                password=password,
                ref="master",
                verify_ssl=False,
            )
            conn.ensure_connected()
            return conn
        except Exception as exc:
            logger.info("Bitbucket connection failed — using stubs: %s", exc)
            return None

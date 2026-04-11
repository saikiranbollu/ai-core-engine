"""
Thread Pool Configuration Loader for the AICE Ingestion Pipeline.

Reads ``thread_pool_config.yaml`` and exposes helper functions that return
the configured ``max_workers`` value for each pipeline component.

Usage
-----
>>> from IngestionPipeline.config.thread_pool_settings import get_max_workers
>>> workers = get_max_workers("orchestrator")        # → 4
>>> workers = get_max_workers("connectors.jama")     # → 4
>>> workers = get_max_workers("incremental")         # → 4
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("aice.ingestion.thread_pool_config")

# Default worker counts used when the config file is missing or a key
# is absent.  ``None`` means "use Python's default"
# (``min(32, os.cpu_count() + 4)``).
_DEFAULT_MAX_WORKERS: int = 4

_CONFIG_FILE_NAME = "thread_pool_config.yaml"

# Module-level cache so we only read from disk once.
_cached_config: Optional[Dict[str, Any]] = None


def _locate_config_file() -> Path:
    """Return the absolute path to the config YAML file.

    The file is expected next to this module inside the ``config/`` package.
    """
    return Path(__file__).resolve().parent / _CONFIG_FILE_NAME


def _load_config() -> Dict[str, Any]:
    """Load and cache the YAML configuration."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config_path = _locate_config_file()
    if not config_path.exists():
        logger.warning(
            "Thread pool config file not found at %s – using defaults",
            config_path,
        )
        _cached_config = {}
        return _cached_config

    try:
        import yaml  # type: ignore[import-untyped]

        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        _cached_config = raw.get("thread_pool", {})
        logger.info("Loaded thread pool config from %s", config_path)
    except ImportError:
        logger.warning(
            "PyYAML not installed – falling back to default thread pool settings"
        )
        _cached_config = {}
    except Exception as exc:
        logger.warning(
            "Failed to load thread pool config from %s: %s – using defaults",
            config_path,
            exc,
        )
        _cached_config = {}

    return _cached_config


def reload_config() -> None:
    """Force a reload of the configuration file on next access."""
    global _cached_config
    _cached_config = None


def get_max_workers(component_path: str) -> Optional[int]:
    """Return the ``max_workers`` setting for a pipeline component.

    Parameters
    ----------
    component_path : str
        Dot-separated path into the ``thread_pool`` config tree.
        Examples: ``"orchestrator"``, ``"connectors.jama"``,
        ``"incremental"``.

    Returns
    -------
    int or None
        The configured ``max_workers`` value, or the default (4) if not
        specified.  ``None`` means "use Python's default".
    """
    cfg = _load_config()

    # Walk the nested dict following the dot-path
    node: Any = cfg
    for key in component_path.split("."):
        if isinstance(node, dict):
            node = node.get(key)
        else:
            node = None
            break

    if isinstance(node, dict):
        value = node.get("max_workers")
    elif isinstance(node, int):
        value = node
    else:
        value = None

    if value is None or value == 0:
        return _DEFAULT_MAX_WORKERS

    return int(value)

"""API Key Registry — loads and caches keys from YAML with auto-reload on file change."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from ..config import get_settings as _get_settings

logger = logging.getLogger("aice_mcp.auth")

_api_key_registry: Dict[str, dict] | None = None
_registry_path: Path | None = None
_registry_mtime: float = 0.0


def _resolve_path(path: str | Path | None = None) -> Path:
    """Resolve the registry file path."""
    if path is None:
        _s = _get_settings()
        path = _s.api_key_registry_path
        if not Path(path).is_absolute():
            path = str(Path(__file__).resolve().parent.parent.parent / path)
    return Path(path)


def load_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Load the API-key → principal registry from a YAML file.

    Auto-reloads if the file has been modified since last load (MEG_SW-293).

    File format::

        keys:
          "<api-key>":
            principal_id: "<id>"
            roles:
              illd: ["public"]
              mcal: ["public", "developer"]
    """
    global _api_key_registry, _registry_path, _registry_mtime

    resolved = _resolve_path(path)

    # Auto-reload: check mtime if we have a cached registry
    if _api_key_registry is not None and _registry_path == resolved:
        try:
            current_mtime = os.path.getmtime(resolved)
            if current_mtime <= _registry_mtime:
                return _api_key_registry
            logger.info("API key registry changed on disk, reloading…")
        except OSError:
            return _api_key_registry

    _registry_path = resolved
    if not resolved.is_file():
        logger.warning("API key registry not found at %s — all requests will be denied", resolved)
        _api_key_registry = {}
        _registry_mtime = 0.0
        return _api_key_registry

    with open(resolved, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    _api_key_registry = data.get("keys", {})
    _registry_mtime = os.path.getmtime(resolved)
    logger.info("Loaded %d API keys from %s", len(_api_key_registry), resolved)
    return _api_key_registry


def reload_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Force-reload the API key registry (e.g. after rotation)."""
    global _api_key_registry, _registry_mtime
    _api_key_registry = None
    _registry_mtime = 0.0
    return load_api_keys(path)

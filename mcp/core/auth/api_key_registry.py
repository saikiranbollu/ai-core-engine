"""API Key Registry — loads and caches keys from YAML."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from ..config import get_settings as _get_settings

logger = logging.getLogger("aice_mcp.auth")

_api_key_registry: Dict[str, dict] | None = None


def load_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Load the API-key → principal registry from a YAML file.

    File format::

        keys:
          "<api-key>":
            principal_id: "<id>"
            roles:
              illd: ["public"]
              mcal: ["public", "developer"]
    """
    global _api_key_registry
    if _api_key_registry is not None:
        return _api_key_registry

    if path is None:
        _s = _get_settings()
        path = _s.api_key_registry_path
        if not Path(path).is_absolute():
            path = str(Path(__file__).resolve().parent.parent.parent / path)
    path = Path(path)
    if not path.is_file():
        logger.warning("API key registry not found at %s — all requests will be denied", path)
        _api_key_registry = {}
        return _api_key_registry

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    _api_key_registry = data.get("keys", {})
    logger.info("Loaded %d API keys from %s", len(_api_key_registry), path)
    return _api_key_registry


def reload_api_keys(path: str | Path | None = None) -> Dict[str, dict]:
    """Force-reload the API key registry (e.g. after rotation)."""
    global _api_key_registry
    _api_key_registry = None
    return load_api_keys(path)

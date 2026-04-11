"""Centralised environment and configuration loader.

This module loads secrets from ``env/.env`` (via *python-dotenv*) and
provides helpers that resolve ``${VAR_NAME}`` placeholders inside YAML
configuration files so that **no credentials need to be stored in
version-controlled files**.

Usage
-----
>>> from env_config import load_env, load_yaml_with_env
>>> load_env()                          # call once at startup
>>> cfg = load_yaml_with_env("../config/storage_config.yaml")
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code
_REPO_ROOT = _SCRIPT_DIR.parents[2]                    # repo root
_ENV_FILE = _REPO_ROOT / "env" / ".env"

# Regex that matches  ${VAR}  or  ${VAR:-default}
_ENV_VAR_RE = re.compile(r"\$\{(?P<var>[^}:]+)(?::- *(?P<default>[^}]*))?\}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_env(env_path: Path | str | None = None, *, override: bool = False) -> None:
    """Load the ``.env`` file into ``os.environ``.

    Parameters
    ----------
    env_path : Path or str, optional
        Explicit path to the ``.env`` file.  Defaults to ``<repo>/env/.env``.
    override : bool
        If *True*, values in the ``.env`` file override existing env vars.
    """
    path = Path(env_path) if env_path else _ENV_FILE
    load_dotenv(path, override=override)


def resolve_env_vars(value: str) -> str:
    """Replace ``${VAR}`` / ``${VAR:-default}`` tokens with env values."""

    def _replacer(match: re.Match) -> str:
        var = match.group("var")
        default = match.group("default")
        env_val = os.environ.get(var)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        return match.group(0)  # leave unresolved if no env var & no default

    return _ENV_VAR_RE.sub(_replacer, value)


def _walk_and_resolve(obj: Any) -> Any:
    """Recursively walk a parsed YAML structure and resolve env vars."""
    if isinstance(obj, str):
        return resolve_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_resolve(item) for item in obj]
    return obj


def load_yaml_with_env(path: Path | str) -> dict:
    """Load a YAML file and resolve any ``${VAR}`` placeholders.

    Calls :func:`load_env` automatically if it has not been called yet
    (detected by checking whether ``NEO4J_USERNAME`` is set – a lightweight
    sentinel).
    """
    # Auto-load .env if not already done
    if not os.environ.get("NEO4J_USERNAME"):
        load_env()

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    return _walk_and_resolve(raw)

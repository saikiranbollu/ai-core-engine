"""
Shared SentenceTransformer Singleton
=====================================
Provides a single, process-wide SentenceTransformer instance shared by
SearchService and SemanticCache to avoid loading the model twice
(saves ~80 MB RAM and 2-10 s startup time).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None  # type: Optional[object]
_model_name: Optional[str] = None


def get_shared_model(model_name: Optional[str] = None):
    """Return (and lazily load) the shared SentenceTransformer model.

    Parameters
    ----------
    model_name : str, optional
        HuggingFace model id. Defaults to ``ST_MODEL`` env var or
        ``all-MiniLM-L6-v2``.

    Returns
    -------
    SentenceTransformer | None
        The loaded model, or None if sentence-transformers is not installed.
    """
    global _model, _model_name
    resolved = model_name or os.environ.get("ST_MODEL", "all-MiniLM-L6-v2")

    # Resolve the pinned revision from the central config so the model can be
    # loaded fully offline (HF_HUB_OFFLINE=1). Loading without a revision relies
    # on a 'refs/main' pointer that is not reliably materialised in the baked
    # image, whereas a pinned revision resolves the explicit snapshot directory.
    revision = _resolve_revision(resolved)

    if _model is not None and _model_name == resolved:
        return _model

    with _lock:
        # Double-check after acquiring lock
        if _model is not None and _model_name == resolved:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            if revision:
                _model = SentenceTransformer(resolved, revision=revision)
            else:
                _model = SentenceTransformer(resolved)
            _model_name = resolved
            logger.info(
                "Shared SentenceTransformer model loaded: '%s' (revision=%s)",
                resolved, revision or "<default>",
            )
        except ImportError:
            logger.warning("sentence-transformers not installed — embeddings unavailable")
            return None
    return _model


def _resolve_revision(model_name: str) -> Optional[str]:
    """Return the pinned HF revision for ``model_name``.

    Order of precedence:
    1. ``ST_MODEL_REVISION`` env var (explicit override).
    2. ``embedding.revision`` from storage_config.yaml, but only when the
       requested model matches the configured model (bare or org-qualified).
    """
    env_rev = os.environ.get("ST_MODEL_REVISION")
    if env_rev:
        return env_rev
    try:
        from src.MemoryLayer.memory.semantic_memory.embedder import get_model_config
        cfg_name, cfg_rev = get_model_config()
    except Exception:
        return None
    # Match either the org-qualified name or its bare suffix.
    short = model_name.rsplit("/", 1)[-1]
    cfg_short = cfg_name.rsplit("/", 1)[-1]
    if model_name in (cfg_name, cfg_short) or short == cfg_short:
        return cfg_rev
    return None

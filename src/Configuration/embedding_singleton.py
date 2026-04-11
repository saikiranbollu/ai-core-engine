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

    if _model is not None and _model_name == resolved:
        return _model

    with _lock:
        # Double-check after acquiring lock
        if _model is not None and _model_name == resolved:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(resolved)
            _model_name = resolved
            logger.info("Shared SentenceTransformer model loaded: '%s'", resolved)
        except ImportError:
            logger.warning("sentence-transformers not installed — embeddings unavailable")
            return None
    return _model

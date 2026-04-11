"""
Embedder
========
Loads a sentence-transformers model with a **pinned revision** and generates
fixed-size vector embeddings for text.

The model is loaded once at construction time — not on every call.
HuggingFace Hub downloads the exact pinned commit on the first run and
caches it in ``~/.cache/huggingface/hub/``.  Subsequent runs load from
that local cache (no network needed).

Configuration
-------------
Model name, revision hash, and dimension are read from
``src/HybridRAG/config/storage_config.yaml`` under the ``embedding:`` key:

.. code-block:: yaml

   embedding:
     model: "sentence-transformers/all-MiniLM-L6-v2"
     revision: "c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
     dimension: 384

By pinning the revision (a Git commit SHA on HuggingFace Hub), every
machine — local dev, CI, Kubernetes — always loads **byte-for-byte
identical weights**, even if the model author pushes a new version.

Design note
-----------
model_name / revision can be overridden at constructor time for testing,
but production code reads from the YAML config (single source of truth).
"""

import logging
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
_REPO_ROOT    = Path(__file__).resolve().parent.parent.parent.parent.parent   # ai-core-engine/
_CONFIG_PATH  = _REPO_ROOT / "src" / "HybridRAG" / "config" / "storage_config.yaml"


def _load_embedding_config() -> dict:
    """Read the ``embedding:`` section from storage_config.yaml."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"[Embedder] storage_config.yaml not found at {_CONFIG_PATH}"
        )
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    embedding = cfg.get("embedding")
    if not embedding:
        raise KeyError("[Embedder] 'embedding' section missing in storage_config.yaml")
    return embedding


def get_model_config() -> tuple:
    """
    Return ``(model_name, revision)`` from the central config.

    Other modules (HybridRAG queriers, ingestion pipelines) should call
    this instead of hardcoding model strings.

    Raises
    ------
    FileNotFoundError
        If storage_config.yaml is missing.
    KeyError
        If required keys are missing from the config.

    Returns
    -------
    tuple[str, str]
        (model_name, revision)
    """
    cfg = _load_embedding_config()
    model = cfg.get("model")
    revision = cfg.get("revision")
    if not model:
        raise KeyError("[Embedder] 'embedding.model' missing in storage_config.yaml")
    if not revision:
        raise KeyError("[Embedder] 'embedding.revision' missing in storage_config.yaml")
    return (model, revision)


class Embedder:
    """
    Generates dense vector embeddings for text using a sentence-transformers model.

    Parameters
    ----------
    model_name : str or None
        HuggingFace model identifier.  ``None`` → read from storage_config.yaml.
    revision : str or None
        HuggingFace commit hash.  ``None`` → read from storage_config.yaml.

    Attributes
    ----------
    dimension : int
        Output vector length (e.g. 384 for all-MiniLM-L6-v2).

    Example
    -------
        embedder = Embedder()
        vector = embedder.embed("IfxCxpi_initChannel initialises the CXPI channel")
        # vector is a list of 384 floats
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        revision: Optional[str] = None,
    ):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "[Embedder] 'sentence-transformers' is not installed. "
                "Run: pip install sentence-transformers"
            )

        cfg_name, cfg_revision = get_model_config()
        self._model_name = model_name or cfg_name
        self._revision   = revision   or cfg_revision

        logger.info(
            "[Embedder] Loading model=%s  revision=%s",
            self._model_name, self._revision,
        )
        self._model = SentenceTransformer(
            self._model_name,
            revision=self._revision,
        )
        self.dimension: int = self._model.get_sentence_embedding_dimension()
        logger.info("[Embedder] Model ready — dimension=%d", self.dimension)

    def embed(self, text: str) -> List[float]:
        """
        Generate a single embedding vector for the given text.

        Parameters
        ----------
        text : str
            The text to embed.

        Returns
        -------
        List[float]
            Fixed-length vector of floats (length == dimension).
        """
        vector = self._model.encode(text, convert_to_numpy=True)
        return vector.tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a list of texts in a single forward pass.

        More efficient than calling embed() in a loop.

        Parameters
        ----------
        texts : List[str]
            List of texts to embed.

        Returns
        -------
        List[List[float]]
            One embedding vector per input text.
        """
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return [v.tolist() for v in vectors]

    @property
    def model_name(self) -> str:
        """The model identifier this Embedder was constructed with."""
        return self._model_name

    @property
    def revision(self) -> str:
        """The pinned HuggingFace revision hash."""
        return self._revision

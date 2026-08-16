"""
Embedding service using sentence-transformers (all-MiniLM-L6-v2).

The model loads once on first call and stays in memory.
Runs locally on CPU — no API calls, no data leaving the machine.
"""

import logging
import threading

import numpy as np
from sentence_transformers import SentenceTransformer

from memory_vault.config import settings

logger = logging.getLogger(__name__)

MODEL_NAME = settings.embedding_model

_model: SentenceTransformer | None = None

# Guards construction of the model. Held only while loading, so callers that
# already have a model never queue behind a load.
_load_lock = threading.Lock()

# Held for the duration of every `encode` call. The model is a single shared
# object and its forward pass is not safe to run from several threads at once —
# doing so segfaults the interpreter rather than raising. Embedding runs in a
# worker thread on the async paths, so concurrent requests reach it in parallel
# and something has to serialize them.
_encode_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    """Load the model once, reuse on subsequent calls."""
    global _model
    # Fast path: no lock once the model exists, which is every call but the
    # first. The assignment below is atomic, so a reader either sees None or a
    # fully constructed model.
    if _model is not None:
        return _model

    with _load_lock:
        # Re-check under the lock: another thread may have loaded it while this
        # one waited, and constructing a second model wastes minutes and memory.
        if _model is None:
            logger.info("Loading embedding model: %s", settings.embedding_model)
            model = SentenceTransformer(settings.embedding_model)
            logger.info("Model loaded — dimensions=%d", settings.embedding_dimensions)
            _model = model
        return _model


def embed(text: str) -> list[float]:
    """Embed a single text string. Returns a list of floats (384-d)."""
    model = _get_model()
    with _encode_lock:
        vector: np.ndarray = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch(
    texts: list[str],
    batch_size: int | None = None,
) -> list[list[float]]:
    """Embed a list of texts. Processes in chunks of batch_size."""
    if not texts:
        return []
    model = _get_model()
    bs = batch_size or settings.embedding_batch_size
    with _encode_lock:
        vectors: np.ndarray = model.encode(
            texts,
            batch_size=bs,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > bs,
        )
    return vectors.tolist()

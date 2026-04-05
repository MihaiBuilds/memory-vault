"""
Embedding service using sentence-transformers (all-MiniLM-L6-v2).

The model loads once on first call and stays in memory.
Runs locally on CPU — no API calls, no data leaving the machine.
"""

import logging

import numpy as np
from sentence_transformers import SentenceTransformer

from src.config import settings

logger = logging.getLogger(__name__)

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Load the model once, reuse on subsequent calls."""
    global _model
    if _model is None:
        logger.info("Loading embedding model: %s", settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
        logger.info("Model loaded — dimensions=%d", settings.embedding_dimensions)
    return _model


def embed(text: str) -> list[float]:
    """Embed a single text string. Returns a list of floats (384-d)."""
    model = _get_model()
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
    vectors: np.ndarray = model.encode(
        texts,
        batch_size=bs,
        normalize_embeddings=True,
        show_progress_bar=len(texts) > bs,
    )
    return vectors.tolist()

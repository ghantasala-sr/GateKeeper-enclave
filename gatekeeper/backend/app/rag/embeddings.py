import os
from functools import lru_cache

from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # 384 dimensions, matches chunks.embedding


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    # Container images bake the model in and set EMBEDDING_MODEL_PATH, so nothing is
    # downloaded at runtime. Local dev falls back to the HuggingFace name (cached after first use).
    return SentenceTransformer(os.environ.get("EMBEDDING_MODEL_PATH") or MODEL_NAME)


def embed(texts: list[str]) -> list[list[float]]:
    vectors = _model().encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def to_pgvector(vector: list[float]) -> str:
    """Format a vector as a pgvector literal, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"

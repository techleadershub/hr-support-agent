"""Embedding provider (OpenAI text-embedding-3-small). Used at index-build time
and for the query at retrieval time."""
from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from .. import config


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=config.OPENAI_API_KEY)


def embed_texts(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        batch = texts[i:i + 64]
        resp = _client().embeddings.create(model=config.EMBEDDING_MODEL, input=batch)
        out.extend(d.embedding for d in resp.data)
    return out


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]

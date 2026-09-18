"""Hybrid retriever over the prebuilt policy index.

- vector : cosine similarity on text-embedding-3-small vectors (numpy, in-memory)
- keyword: BM25 over a simple tokeniser
- hybrid : reciprocal-rank fusion of both (default)

The index (data/policy_index.json) is produced by scripts/build_index.py and
committed, so the deployed function needs only the OpenAI key for the query.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from .. import config
from .embeddings import embed_query

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    doc_title: str
    section: str
    text: str
    score: float
    vector_score: float | None
    bm25_score: float | None
    rank: int

    def to_dict(self, include_text: bool = True) -> dict:
        d = {
            "chunk_id": self.chunk_id, "doc_id": self.doc_id, "doc_title": self.doc_title,
            "section": self.section, "score": round(self.score, 4),
            "vector_score": None if self.vector_score is None else round(self.vector_score, 4),
            "bm25_score": None if self.bm25_score is None else round(self.bm25_score, 4),
            "rank": self.rank,
        }
        if include_text:
            d["text"] = self.text
        return d


class PolicyIndex:
    def __init__(self, path=config.INDEX_PATH):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.meta = data["meta"]
        self.chunks: list[dict] = data["chunks"]
        vectors = np.array([c["vector"] for c in self.chunks], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        self.vectors = vectors / np.clip(norms, 1e-9, None)
        self.bm25 = BM25Okapi([_tokenize(c["text"]) for c in self.chunks])

    # ---- individual retrievers ------------------------------------------------
    def vector_search(self, query: str, k: int) -> list[tuple[int, float]]:
        q = np.array(embed_query(query), dtype=np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        sims = self.vectors @ q
        idx = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in idx]

    def keyword_search(self, query: str, k: int) -> list[tuple[int, float]]:
        scores = self.bm25.get_scores(_tokenize(query))
        idx = np.argsort(-scores)[:k]
        return [(int(i), float(scores[i])) for i in idx if scores[i] > 0]

    # ---- public API -------------------------------------------------------------
    def search(self, query: str, top_k: int = 5, mode: str = "hybrid") -> list[Hit]:
        if mode not in ("hybrid", "vector", "keyword"):
            raise ValueError("mode must be hybrid | vector | keyword")
        pool = max(top_k * 4, 20)
        vec = self.vector_search(query, pool) if mode in ("hybrid", "vector") else []
        kw = self.keyword_search(query, pool) if mode in ("hybrid", "keyword") else []

        vec_map = {i: s for i, s in vec}
        kw_map = {i: s for i, s in kw}
        fused: dict[int, float] = {}
        if mode == "hybrid":
            # reciprocal rank fusion, k=60
            for rank, (i, _) in enumerate(vec, start=1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
            for rank, (i, _) in enumerate(kw, start=1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
        elif mode == "vector":
            fused = dict(vec)
        else:
            fused = dict(kw)

        ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
        hits: list[Hit] = []
        for rank, (i, score) in enumerate(ordered, start=1):
            c = self.chunks[i]
            hits.append(Hit(
                chunk_id=c["id"], doc_id=c["doc_id"], doc_title=c["doc_title"], section=c["section"],
                text=c["text"], score=score, vector_score=vec_map.get(i), bm25_score=kw_map.get(i), rank=rank,
            ))
        return hits

    def get_chunk(self, chunk_id: str) -> dict | None:
        for c in self.chunks:
            if c["id"] == chunk_id:
                return {k: v for k, v in c.items() if k != "vector"}
        return None

    def list_chunks(self) -> list[dict]:
        return [{k: v for k, v in c.items() if k not in ("vector", "text")} for c in self.chunks]


@lru_cache(maxsize=1)
def get_index() -> PolicyIndex:
    return PolicyIndex()

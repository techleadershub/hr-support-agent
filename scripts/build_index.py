"""Build data/policy_index.json from knowledge/*.md.

Run after editing any policy document:
    python scripts/build_index.py
Requires OPENAI_API_KEY (for text-embedding-3-small).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.rag.chunking import chunk_directory  # noqa: E402
from app.rag.embeddings import embed_texts  # noqa: E402


def main() -> None:
    chunks = chunk_directory(config.KNOWLEDGE_DIR)
    print(f"{len(chunks)} chunks from {len(list(config.KNOWLEDGE_DIR.glob('*.md')))} documents")
    vectors = embed_texts([c.text for c in chunks])
    payload = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "embedding_model": config.EMBEDDING_MODEL,
            "dimensions": len(vectors[0]),
            "chunk_count": len(chunks),
            "max_chars": 1800,
        },
        "chunks": [dict(c.to_dict(), vector=v) for c, v in zip(chunks, vectors)],
    }
    config.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.INDEX_PATH.write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {config.INDEX_PATH} ({config.INDEX_PATH.stat().st_size // 1024} KB)")
    for c in chunks:
        print(f"  {c.id:60s} {len(c.text):5d} chars")


if __name__ == "__main__":
    main()

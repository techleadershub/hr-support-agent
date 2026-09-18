from __future__ import annotations

from ..rag.retriever import get_index
from .context import ToolContext, ToolError


def search_hr_policy(args: dict, ctx: ToolContext) -> dict:
    question = str(args.get("question", "")).strip()
    if not question:
        raise ToolError("EMPTY_QUERY", "question must not be empty.")
    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(top_k, 10))

    if ctx.chaos.active("policy_search_empty"):
        return {"ok": True, "question": question, "count": 0, "results": [],
                "message": "No matching policy sections found."}

    index = get_index()
    hits = index.search(question, top_k=top_k, mode="hybrid")
    if ctx.chaos.active("policy_search_garbage"):
        # return the tail of the ranking instead of the head
        hits = index.search(question, top_k=len(index.chunks), mode="hybrid")[-top_k:]
        for rank, h in enumerate(hits, start=1):
            h.rank = rank
    return {
        "ok": True,
        "question": question,
        "count": len(hits),
        "results": [
            {"chunk_id": h.chunk_id, "doc_id": h.doc_id, "doc_title": h.doc_title, "section": h.section,
             "score": round(h.score, 4), "text": h.text}
            for h in hits
        ],
        "citation_format": "Cite as [<doc_id> §<clause>] e.g. [NDPL-HR-001 §3.6].",
    }

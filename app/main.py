"""FastAPI application — HR Support & Action Agent.

Endpoints (all under /api, all require X-Access-Key unless noted):
  GET  /api/health                 (no key)  liveness + config summary
  POST /api/chat                   the agent. Headers: X-Chaos, X-Today (optional)
  GET  /api/sessions/{id}          conversation history (simplified)
  DELETE /api/sessions/{id}
  GET  /api/tools                  tool definitions (JSON schema) as given to the model
  POST /api/tools/{name}           call a tool directly, bypassing the LLM. Headers: X-Chaos, X-Today, X-Employee-Id
  POST /api/retrieve               query the RAG index directly (vector | keyword | hybrid)
  GET  /api/chunks, /api/chunks/{id}
  GET  /api/employees              seeded employees (ids only, for picking a persona)
  GET  /api/leave-requests?employee_id=E1001   read back what the agent wrote
  GET  /api/audit?session_id=...   tool audit log
  GET  /api/chaos                  fault-injection scenarios
  POST /api/admin/reset            reseed the HR database to the known state
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db
from .agent.agent import AgentRun
from .chaos import SCENARIOS, Chaos
from .rag.retriever import get_index
from .tools import TOOL_DEFINITIONS, run_tool, tool_names
from .tools.context import ToolContext

app = FastAPI(title="HR Support & Action Agent", version=config.APP_VERSION,
              description="Enterprise HR agent combining RAG over policy documents with transactional leave tools.")


# ---------------------------------------------------------------------------
# auth / common
# ---------------------------------------------------------------------------
def require_key(request: Request, x_access_key: str | None = Header(default=None)) -> None:
    if not config.ACCESS_KEY:
        return
    supplied = x_access_key or request.query_params.get("access_key")
    if supplied != config.ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Access-Key header.")


def parse_chaos(raw: str | None) -> Chaos:
    try:
        return Chaos.from_header(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def parse_today(raw: str | None):
    try:
        return config.today(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Today must be YYYY-MM-DD")


EMPLOYEE_ID_PATTERN = r"^E\d{4}$"


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    employee_id: str = Field(..., pattern=EMPLOYEE_ID_PATTERN, description="Logged-in employee, e.g. E1001")
    session_id: str | None = Field(default=None, description="Omit to start a new conversation")


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: str = Field(default="hybrid", pattern="^(hybrid|vector|keyword)$")
    include_text: bool = True


class ResetRequest(BaseModel):
    clear_sessions: bool = False


# ---------------------------------------------------------------------------
# UI + health
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    index_ok, chunk_count, db_ok = False, 0, False
    try:
        chunk_count = len(get_index().chunks)
        index_ok = True
    except Exception:
        pass
    try:
        db.select("hr_employees", {"select": "employee_id", "limit": "1"})
        db_ok = True
    except Exception:
        pass
    return {"ok": index_ok and db_ok, "version": config.APP_VERSION, "model": config.MODEL, "effort": config.EFFORT,
            "embedding_model": config.EMBEDDING_MODEL, "index_chunks": chunk_count, "db": db_ok,
            "today": config.today().isoformat(), "tools": tool_names(), "auth_required": bool(config.ACCESS_KEY)}


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
@app.post("/api/chat", dependencies=[Depends(require_key)])
def chat(body: ChatRequest,
         x_chaos: str | None = Header(default=None),
         x_today: str | None = Header(default=None)):
    chaos = parse_chaos(x_chaos)
    today = parse_today(x_today)

    # session
    if body.session_id:
        try:
            uuid.UUID(body.session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="session_id must be a UUID")
        sess = db.select("hr_sessions", {"session_id": f"eq.{body.session_id}"}, single=True)
        if not sess:
            raise HTTPException(status_code=404, detail="session not found")
        if sess["employee_id"] != body.employee_id:
            raise HTTPException(status_code=403, detail="session belongs to a different employee")
        session_id = body.session_id
        rows = db.select("hr_messages", {"session_id": f"eq.{session_id}", "order": "id.asc", "select": "role,content"})
        history = [{"role": r["role"], "content": r["content"]} for r in rows]
    else:
        sess = db.insert("hr_sessions", {"employee_id": body.employee_id})[0]
        session_id = sess["session_id"]
        history = []

    run = AgentRun(body.employee_id, session_id, history, body.message, chaos, today)
    result = run.run()

    db.insert("hr_messages", [{"session_id": session_id, "role": m["role"], "content": m["content"]} for m in run.new_messages],
              returning=False)
    db.update("hr_sessions", {"session_id": session_id}, {"updated_at": "now()"})

    return {"session_id": session_id, "employee_id": body.employee_id, "today": today.isoformat(),
            "chaos": chaos.as_list(), **result}


@app.get("/api/sessions/{session_id}", dependencies=[Depends(require_key)])
def get_session(session_id: str):
    sess = db.select("hr_sessions", {"session_id": f"eq.{session_id}"}, single=True)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    rows = db.select("hr_messages", {"session_id": f"eq.{session_id}", "order": "id.asc"})
    turns = []
    for r in rows:
        c = r["content"]
        if isinstance(c, str):
            turns.append({"role": r["role"], "kind": "text", "text": c, "at": r["created_at"]})
        else:
            for b in c:
                if b.get("type") == "text":
                    turns.append({"role": r["role"], "kind": "text", "text": b["text"], "at": r["created_at"]})
                elif b.get("type") == "tool_use":
                    turns.append({"role": r["role"], "kind": "tool_use", "tool": b["name"], "args": b["input"], "at": r["created_at"]})
                elif b.get("type") == "tool_result":
                    turns.append({"role": r["role"], "kind": "tool_result", "is_error": b.get("is_error", False),
                                  "content": b.get("content"), "at": r["created_at"]})
    return {"session": sess, "turns": turns}


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_key)])
def delete_session(session_id: str):
    with db._client() as c:  # noqa: SLF001
        resp = c.delete(f"/hr_sessions", params={"session_id": f"eq.{session_id}"}, headers=db._headers({"Prefer": "return=representation"}))
    db._raise(resp)
    return {"deleted": len(resp.json())}


# ---------------------------------------------------------------------------
# tools (direct access, no LLM)
# ---------------------------------------------------------------------------
@app.get("/api/tools", dependencies=[Depends(require_key)])
def list_tools():
    return {"tools": TOOL_DEFINITIONS}


@app.post("/api/tools/{name}", dependencies=[Depends(require_key)])
def call_tool(name: str, args: dict[str, Any],
              x_chaos: str | None = Header(default=None),
              x_today: str | None = Header(default=None),
              x_employee_id: str | None = Header(default=None)):
    if name not in tool_names():
        raise HTTPException(status_code=404, detail=f"unknown tool; valid: {tool_names()}")
    chaos = parse_chaos(x_chaos)
    today = parse_today(x_today)
    # The authenticated identity is X-Employee-Id if given, else the employee_id argument.
    auth_emp = (x_employee_id or args.get("employee_id") or "E0000").strip().upper()
    ctx = ToolContext(employee_id=auth_emp, today=today, chaos=chaos, session_id=None)
    result, is_error, latency_ms = run_tool(name, args, ctx)
    status = result.get("status", 500) if is_error else 200
    return JSONResponse(status_code=status, content={"tool": name, "args": args, "authenticated_as": auth_emp,
                                                     "is_error": is_error, "latency_ms": latency_ms, "result": result})


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------
@app.post("/api/retrieve", dependencies=[Depends(require_key)])
def retrieve(body: RetrieveRequest):
    index = get_index()
    hits = index.search(body.query, top_k=body.top_k, mode=body.mode)
    return {"query": body.query, "mode": body.mode, "count": len(hits),
            "results": [h.to_dict(include_text=body.include_text) for h in hits], "index": index.meta}


@app.get("/api/chunks", dependencies=[Depends(require_key)])
def list_chunks():
    index = get_index()
    return {"index": index.meta, "chunks": index.list_chunks()}


@app.get("/api/chunks/{chunk_id:path}", dependencies=[Depends(require_key)])
def get_chunk(chunk_id: str):
    c = get_index().get_chunk(chunk_id)
    if not c:
        raise HTTPException(status_code=404, detail="chunk not found")
    return c


# ---------------------------------------------------------------------------
# state inspection + admin
# ---------------------------------------------------------------------------
@app.get("/api/employees", dependencies=[Depends(require_key)])
def employees():
    rows = db.select("hr_employees", {"select": "employee_id,name,department,location,employment_type,status", "order": "employee_id.asc"})
    return {"employees": rows}


@app.get("/api/leave-requests", dependencies=[Depends(require_key)])
def leave_requests(employee_id: str | None = Query(default=None), status: str | None = Query(default=None)):
    params: dict[str, str] = {"order": "applied_at.desc", "limit": "200"}
    if employee_id:
        params["employee_id"] = f"eq.{employee_id.upper()}"
    if status:
        params["status"] = f"eq.{status.upper()}"
    return {"requests": db.select("hr_leave_requests", params)}


@app.get("/api/audit", dependencies=[Depends(require_key)])
def audit(session_id: str | None = Query(default=None), limit: int = Query(default=100, le=500)):
    params: dict[str, str] = {"order": "id.desc", "limit": str(limit)}
    if session_id:
        params["session_id"] = f"eq.{session_id}"
    return {"entries": db.select("hr_audit_log", params)}


@app.get("/api/chaos", dependencies=[Depends(require_key)])
def chaos_scenarios():
    return {"header": "X-Chaos", "scenarios": SCENARIOS}


@app.post("/api/admin/reset", dependencies=[Depends(require_key)])
def reset(body: ResetRequest | None = None):
    body = body or ResetRequest()
    return db.rpc("hr_reset", {"clear_sessions": body.clear_sessions})

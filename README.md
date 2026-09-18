# HR Support & Action Agent

An enterprise-style **HR AI agent** for a fictional Indian company (Nimbus Dynamics Pvt Ltd) that answers
employee policy questions with **RAG + citations** and takes real **HR actions** (leave application) through
tools — built as a realistic system-under-test for AI QA work: RAG evaluation, agentic-workflow testing,
tool-selection/argument validation, state handling, failure injection and multi-turn scenarios.

The agent is deliberately *not* wrapped in cotton wool. The tools enforce some rules, the policy documents
contain others, the seed data has employees in every awkward status, and a fault-injection header can break
any backend on demand. **No tests are included** — writing them is the point.

```
                     Employee (logged in as E10xx)
                                │
                          POST /api/chat
                                │
                    ┌───────────▼────────────┐
                    │  Stage 1: intent       │  structured-output classification
                    │  Stage 2: agent loop   │  Claude + tool use (max 8 iterations)
                    └───┬───────────────┬────┘
                        │               │
            search_hr_policy       get_leave_balance / apply_leave /
            (hybrid RAG:           get_holiday_calendar / get_employee_profile
             vector + BM25)                │
                        │          Supabase Postgres (hr_* tables,
            data/policy_index.json         hr_apply_leave(), hr_reset())
            106 chunks / 14 docs
                        └───────┬───────┘
                                ▼
              { reply, intent, citations, actions, trace, usage }
```

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.12, FastAPI, deployed as a single Vercel Python function |
| LLM | Anthropic Claude (`claude-opus-5` by default, adaptive thinking, effort configurable) |
| RAG | 14 markdown policy docs → section chunks → OpenAI `text-embedding-3-small` vectors (prebuilt, committed) + BM25; reciprocal-rank fusion |
| State | Supabase Postgres via PostgREST: employees, balances, requests, holidays, sessions, messages, audit log |
| UI | Single static page with a live trace panel (`static/index.html`) |

## Run locally

```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                              # fill in keys (see below)
python scripts/build_index.py                     # only needed after editing knowledge/*.md
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. Every `/api/*` call except `/api/health` needs the header `X-Access-Key: <HR_AGENT_ACCESS_KEY>`.

Environment variables are documented in [.env.example](.env.example). The database schema and seed data
live in [scripts/schema.sql](scripts/schema.sql); run it once against a fresh Supabase project, then
`POST /api/admin/reset` at any time to return to the seeded state.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness, model, index size, DB status, effective "today" |
| `POST` | `/api/chat` | the agent. Body `{message, employee_id, session_id?}`. Headers `X-Chaos`, `X-Today` optional |
| `GET` | `/api/sessions/{id}` | simplified conversation history (text / tool_use / tool_result turns) |
| `DELETE` | `/api/sessions/{id}` | delete a conversation |
| `GET` | `/api/tools` | the five tool definitions exactly as the model sees them |
| `POST` | `/api/tools/{name}` | call a tool **directly without the LLM**. Body = tool args. Header `X-Employee-Id` sets the authenticated identity (defaults to `employee_id` arg) |
| `POST` | `/api/retrieve` | query the RAG index: `{query, top_k, mode: hybrid\|vector\|keyword, include_text}` |
| `GET` | `/api/chunks`, `/api/chunks/{id}` | enumerate / read index chunks (for golden datasets) |
| `GET` | `/api/employees` | seeded personas |
| `GET` | `/api/leave-requests?employee_id=&status=` | read back what the agent wrote |
| `GET` | `/api/audit?session_id=` | every tool invocation: args, result, latency, chaos flags |
| `GET` | `/api/chaos` | fault-injection scenarios |
| `POST` | `/api/admin/reset` | reseed HR data (`{"clear_sessions": true}` also wipes conversations) |

### `/api/chat` response shape

```jsonc
{
  "session_id": "uuid",            // pass back for multi-turn
  "employee_id": "E1001",
  "today": "2026-09-18",
  "chaos": [],
  "reply": "…final answer with [NDPL-HR-001 §3.6] citations…",
  "intent": { "intent": "multi_step", "confidence": 0.92, "leave_type": null, "date_mentions": ["next Monday"], "conditional": true, "rationale": "…" },
  "citations": [ { "doc_id": "NDPL-HR-001", "clause": "3.6" } ],
  "retrieved_chunk_ids": [ "01-leave-policy#3-casual-leave-cl", … ],
  "actions": [ /* every apply_leave call with args + result */ ],
  "trace": {
    "steps": [ /* llm_call {stop_reason, reasoning, text, tool_calls} and tool_call {tool, args, result, is_error, latency_ms} in order */ ],
    "tool_calls": [ … ], "retrievals": [ { "query": "…", "chunks": [ {chunk_id, doc_id, section, score} ] } ],
    "iterations": 3, "stopped_because": "end_turn"
  },
  "usage": { "input_tokens": 7714, "output_tokens": 1035, "cache_read_input_tokens": 5950, "cache_creation_input_tokens": 0 },
  "latency_ms": 25551, "model": "claude-opus-5"
}
```

### Determinism aids

- `X-Today: 2026-09-18` (or env `HR_AGENT_TODAY`) freezes the clock so "next Monday" resolves the same way every run.
- `POST /api/admin/reset` restores the exact seed state (balances, requests, request-id sequence).
- Chunk ids are stable across index rebuilds.

### Fault injection (`X-Chaos` header, comma-separated)

| Scenario | Effect |
|---|---|
| `leave_api_down` | `get_leave_balance` → HTTP 503 |
| `leave_api_slow` | `get_leave_balance` delayed ~6 s |
| `apply_leave_500` | `apply_leave` → HTTP 500, nothing written |
| `apply_leave_timeout` | `apply_leave` writes the request **then** times out (HTTP 504) — a ghost write |
| `apply_leave_flaky` | first `apply_leave` in a request → 502, retry succeeds |
| `stale_balance` | balances reported +5 days higher than reality |
| `holiday_api_down` / `holiday_api_empty` | calendar unavailable / silently empty |
| `policy_search_empty` / `policy_search_garbage` | RAG returns nothing / returns the *least* relevant chunks |
| `profile_api_down` / `profile_wrong_location` | profile unavailable / everyone is in Mumbai |
| `slow_tools` | +2 s on every tool |

## The tools

| Tool | Reads/Writes | Enforced by the tool | Left to the agent (policy only) |
|---|---|---|---|
| `search_hr_policy(question, top_k)` | read | — | asking the right question; reading superseded clauses correctly; FAQ vs policy precedence |
| `get_leave_balance(employee_id)` | read | identity, inactive employees | interpreting probation-locked PL, comp-off expiry |
| `apply_leave(employee_id, date, leave_type, end_date?, half_day?, reason?)` | **write** | identity, date format/range, past dates, weekends/holidays *for the employee's location*, balance, overlapping requests, on-leave status, contractor/LOP rules, comp-off expiry, half-day types | notice periods, CL 3-day cap, CL+PL clubbing, probation PL lock, notice-period restrictions, departmental blackouts, sandwich-rule warning, confirming intent before writing |
| `get_holiday_calendar(year?, location?)` | read | valid location | passing the employee's location (regional holidays are omitted otherwise) |
| `get_employee_profile(employee_id)` | read | identity | — |

Business-rule rejections come back as `{"ok": false, "error_code": …}` in a normal tool result; infrastructure
failures come back as `is_error: true` tool results with an HTTP-style status.

## Seed personas

| ID | Who | Why they exist |
|---|---|---|
| E1001 | Ananya Rao, Engineering, Hyderabad, active | the happy path (CL 8 / SL 7 / PL 14 / comp-off 2) |
| E1002 | Rahul Verma, Finance, Hyderabad, active | CL exhausted, PL 2; Finance quarter-close blackout |
| E1003 | Priya Nair, Engineering, Hyderabad, **probation** | PL shows 4.5 but is locked; CL 3 |
| E1004 | Vikram Singh, Sales, **Mumbai**, active | different regional holidays; PL at the 30-day cap; comp-off expiring 20 Sep; a pending Oct request |
| E1005 | Meera Iyer, Product, Bengaluru, **on_leave** (maternity) | no leave can be applied |
| E1006 | Arjun Mehta, Engineering Manager, Hyderabad | manager of E1001/E1003/E1012; has a pending CL request on 25 Sep |
| E1007 | Sneha Kulkarni, Marketing, Bengaluru, **notice_period** | CL/PL not permitted during notice |
| E1008 | Karthik Reddy, IT Ops, Bengaluru, **contractor** | only LOP; no balances |
| E1009 | David Thomas, Sales, Mumbai, **inactive** | records not accessible |
| E1010 / E1011 | COO / VP Sales | approvers; large PL balances |
| E1012 | Fatima Sheikh, Engineering, Hyderabad, **intern** | tiny pro-rated balances; no PL row |

Today (unless overridden) is a Friday in late September 2026: the next Monday is a Hyderabad-only holiday
(Bathukamma, 21 Sep), the Finance Q3 blackout starts 24 Sep, Gandhi Jayanti is Friday 2 Oct, and the
year-end shutdown (28–31 Dec) auto-deducts PL.

## Knowledge base

14 documents, ~9,500 words, under [knowledge/](knowledge/): leave policy (the core, with a *superseded
clauses* section), holiday calendar, probation, attendance, WFH, maternity/paternity, comp off, exit & notice
period, contractors, expenses, code of conduct & data privacy, L&D, departmental blackout periods, and an FAQ
that is explicitly subordinate to the numbered policies. Chunking is one `##` section per chunk with the
document title prepended; `scripts/build_index.py` regenerates `data/policy_index.json`.

## What makes this interesting to test

- Answers must be grounded: the same question can pull a current clause, a superseded clause and an FAQ
  restatement into the top-3.
- "Next Monday" is a holiday for one location and a working day for another; the right answer depends on a
  profile lookup the agent may skip.
- Several policy rules are *not* enforced by `apply_leave`. Whether the agent checks them before writing is
  observable in `trace.steps` ordering.
- Writes are real and persist across turns and sessions until `/api/admin/reset`.
- A timeout scenario creates a request the agent never hears about.
- The identity boundary is enforced in the tool layer, but the *attempt* is visible in the trace.

## Deploy (Vercel)

```bash
vercel                           # preview
vercel --prod                    # production
```

`vercel.json` rewrites every path to `api/index.py` (FastAPI) with a 300 s max duration. Set the same
environment variables as `.env.example` in the Vercel project. The embedding index is committed, so the
function needs no build step; `OPENAI_API_KEY` is used only to embed the query.

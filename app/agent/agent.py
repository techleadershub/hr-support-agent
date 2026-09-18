"""The agent loop: intent → reason → (RAG | tools)* → final answer, with a full trace.

Manual loop (not the SDK tool runner) because every intermediate step — thinking
summaries, tool arguments, tool results, retrieval hits — must be captured for the
trace that test suites assert on.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from functools import lru_cache
from typing import Any

import anthropic

from .. import config
from ..chaos import Chaos
from ..tools import TOOL_DEFINITIONS, run_tool
from ..tools.context import ToolContext
from .intent import classify_intent
from .prompts import build_system_prompt

# matches [NDPL-HR-001 §3.6], [NDPL-HR-001 §3.6, §3.7], [NDPL-HR-FAQ, Applying for leave]
CITATION_RE = re.compile(r"\[(NDPL-HR-(?:\d{3}|FAQ))(?:[\s,:§]*([^\]]*?))?\s*\]")


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, max_retries=2, timeout=120.0)


def _serialise_blocks(content) -> list[dict]:
    """Assistant content blocks → JSON we can store and replay verbatim (thinking signatures included)."""
    return [b.model_dump(mode="json", exclude_none=True) for b in content]


def _extract_citations(text: str) -> list[dict]:
    seen, out = set(), []
    for doc, rest in CITATION_RE.findall(text):
        clauses = [c.strip(" §") for c in re.split(r"[,;]", rest or "") if c.strip(" §")] or [None]
        for clause in clauses:
            key = (doc, clause or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({"doc_id": doc, "clause": clause})
    return out


class AgentRun:
    def __init__(self, employee_id: str, session_id: str, history: list[dict], message: str,
                 chaos: Chaos, today: date):
        self.employee_id = employee_id
        self.session_id = session_id
        self.history = history
        self.message = message
        self.chaos = chaos
        self.today = today
        self.trace: dict[str, Any] = {"intent": None, "steps": [], "tool_calls": [], "retrievals": [],
                                      "iterations": 0, "stopped_because": None}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.new_messages: list[dict] = []   # messages appended this turn (persisted by caller)

    # ------------------------------------------------------------------
    def _add_usage(self, u) -> None:
        self.usage["input_tokens"] += u.input_tokens
        self.usage["output_tokens"] += u.output_tokens
        self.usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0

    def run(self) -> dict:
        client = get_client()
        started = time.perf_counter()

        # ---- stage 1: intent ----------------------------------------------------
        try:
            intent, iusage = classify_intent(client, self.history, self.message, self.today)
            self.usage["input_tokens"] += iusage["input_tokens"]
            self.usage["output_tokens"] += iusage["output_tokens"]
        except anthropic.APIError as e:
            intent = {"intent": "unclear", "confidence": 0.0, "leave_type": None, "date_mentions": [],
                      "conditional": False, "rationale": f"classifier error: {type(e).__name__}"}
        self.trace["intent"] = intent

        # ---- stage 2: agent loop ------------------------------------------------
        system = [{"type": "text", "text": build_system_prompt(self.employee_id, self.today),
                   "cache_control": {"type": "ephemeral"}}]
        messages: list[dict] = list(self.history) + [{"role": "user", "content": self.message}]
        self.new_messages.append({"role": "user", "content": self.message})
        ctx = ToolContext(employee_id=self.employee_id, today=self.today, chaos=self.chaos, session_id=self.session_id)

        final_text = ""
        for iteration in range(1, config.MAX_ITERATIONS + 1):
            self.trace["iterations"] = iteration
            t0 = time.perf_counter()
            try:
                response = client.messages.create(
                    model=config.MODEL,
                    max_tokens=4096,
                    system=system,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": config.EFFORT},
                )
            except anthropic.APIError as e:
                self.trace["steps"].append({"step": len(self.trace["steps"]) + 1, "type": "llm_error",
                                            "error": f"{type(e).__name__}: {getattr(e, 'message', str(e))}"})
                self.trace["stopped_because"] = "llm_error"
                final_text = "I'm unable to reach the assistant service right now. Please try again in a moment or use the HR Portal."
                break
            self._add_usage(response.usage)

            reasoning = " ".join(b.thinking for b in response.content if b.type == "thinking" and b.thinking).strip()
            text_parts = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            step = {
                "step": len(self.trace["steps"]) + 1, "type": "llm_call", "iteration": iteration,
                "stop_reason": response.stop_reason, "reasoning": reasoning or None,
                "text": "\n".join(text_parts).strip() or None,
                "tool_calls": [{"id": b.id, "name": b.name, "args": b.input} for b in tool_uses],
                "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
                "latency_ms": int((time.perf_counter() - t0) * 1000),
            }
            self.trace["steps"].append(step)

            assistant_blocks = _serialise_blocks(response.content)
            messages.append({"role": "assistant", "content": assistant_blocks})
            self.new_messages.append({"role": "assistant", "content": assistant_blocks})

            if response.stop_reason == "refusal":
                self.trace["stopped_because"] = "refusal"
                final_text = "\n".join(text_parts).strip() or "I can't help with that request."
                break
            if response.stop_reason == "max_tokens":
                self.trace["stopped_because"] = "max_tokens"
                final_text = "\n".join(text_parts).strip()
                break
            if response.stop_reason != "tool_use" or not tool_uses:
                self.trace["stopped_because"] = "end_turn"
                final_text = "\n".join(text_parts).strip()
                break

            # execute every tool call from this turn, return all results in ONE user message
            results_blocks: list[dict] = []
            for tu in tool_uses:
                args = tu.input if isinstance(tu.input, dict) else {}
                result, is_error, latency_ms = run_tool(tu.name, args, ctx)
                record = {"step": len(self.trace["steps"]) + 1, "type": "tool_call", "iteration": iteration,
                          "tool_use_id": tu.id, "tool": tu.name, "args": args, "result": result,
                          "is_error": is_error, "latency_ms": latency_ms}
                self.trace["steps"].append(record)
                self.trace["tool_calls"].append({k: record[k] for k in ("tool", "args", "result", "is_error", "latency_ms")})
                if tu.name == "search_hr_policy" and result.get("ok"):
                    self.trace["retrievals"].append({
                        "query": args.get("question"), "count": result.get("count", 0),
                        "chunks": [{k: r[k] for k in ("chunk_id", "doc_id", "section", "score")} for r in result.get("results", [])],
                    })
                results_blocks.append({"type": "tool_result", "tool_use_id": tu.id,
                                       "content": json.dumps(result, default=str), "is_error": is_error})
            messages.append({"role": "user", "content": results_blocks})
            self.new_messages.append({"role": "user", "content": results_blocks})
        else:
            self.trace["stopped_because"] = "max_iterations"
            final_text = ("I wasn't able to complete this in the allowed number of steps. No further action has been taken; "
                          "please try a more specific request or use the HR Portal.")
            self.new_messages.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})

        if not final_text:
            final_text = "I couldn't produce an answer. Please rephrase or use the HR Portal."

        retrieved_ids = sorted({c["chunk_id"] for r in self.trace["retrievals"] for c in r["chunks"]})
        return {
            "reply": final_text,
            "intent": intent,
            "citations": _extract_citations(final_text),
            "retrieved_chunk_ids": retrieved_ids,
            "actions": [tc for tc in self.trace["tool_calls"] if tc["tool"] == "apply_leave"],
            "trace": self.trace,
            "usage": self.usage,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "model": config.MODEL,
        }

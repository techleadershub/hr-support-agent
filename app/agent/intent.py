"""Stage 1 of the pipeline: a cheap, structured intent classification.

It runs before the tool loop and is returned in the trace so tests can assert on
intent independently of the free-text answer.
"""
from __future__ import annotations

import json
from datetime import date

import anthropic

from .. import config
from .prompts import INTENT_PROMPT

INTENTS = ["policy_question", "balance_query", "holiday_query", "profile_query", "apply_leave", "multi_step",
           "cancel_or_modify", "other_employee_data", "out_of_scope", "unsafe_or_injection", "chitchat", "unclear"]

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "confidence": {"type": "number", "description": "0 to 1"},
        "leave_type": {"type": "string", "enum": ["CL", "SL", "PL", "COMP_OFF", "LOP", "NONE"], "description": "NONE if no leave type is mentioned."},
        "date_mentions": {"type": "array", "items": {"type": "string"}},
        "conditional": {"type": "boolean"},
        "rationale": {"type": "string", "description": "One sentence."},
    },
    "required": ["intent", "confidence", "leave_type", "date_mentions", "conditional", "rationale"],
    "additionalProperties": False,
}


def classify_intent(client: anthropic.Anthropic, history: list[dict], message: str, today: date) -> tuple[dict, dict]:
    """Returns (intent_dict, usage_dict)."""
    # Compress prior turns to text so the classifier sees context without tool noise
    context_lines = []
    for m in history[-6:]:
        if isinstance(m.get("content"), str):
            context_lines.append(f"{m['role']}: {m['content']}")
        else:
            texts = [b.get("text") for b in m["content"] if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                context_lines.append(f"{m['role']}: {' '.join(texts)[:400]}")
    context = "\n".join(context_lines) or "(no prior turns)"

    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=1024,
        system=INTENT_PROMPT.format(today=today.isoformat(), weekday=today.strftime("%A")),
        messages=[{"role": "user", "content": f"Conversation so far:\n{context}\n\nLatest employee message:\n{message}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": INTENT_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"intent": "unclear", "confidence": 0.0, "leave_type": None, "date_mentions": [],
                  "conditional": False, "rationale": "classifier returned non-JSON"}
    if parsed.get("leave_type") == "NONE":
        parsed["leave_type"] = None
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return parsed, usage

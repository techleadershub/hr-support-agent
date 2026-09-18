"""Tool registry: Anthropic tool definitions + dispatcher with audit logging."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .. import db
from ..chaos import ChaosError
from .calendar import get_holiday_calendar
from .context import ToolContext, ToolError
from .leave import apply_leave, get_leave_balance
from .policy import search_hr_policy
from .profile import get_employee_profile

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_hr_policy",
        "description": (
            "Semantic search over the Nimbus Dynamics HR policy documents (leave, holidays, probation, attendance, "
            "WFH, maternity/paternity, comp off, exit/notice period, contractors, expenses, code of conduct, L&D, "
            "departmental blackout periods, FAQ). Returns the most relevant policy sections with document ids for "
            "citation. Use this for any question about rules, entitlements, eligibility, notice periods or procedures, "
            "and BEFORE taking any action so the action complies with policy. Retrieval is not guaranteed to surface "
            "every relevant clause; ask focused questions and search again if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "A focused natural-language question, e.g. 'notice period for casual leave'."},
                "top_k": {"type": "integer", "description": "Number of sections to return (1-10). Default 5.", "minimum": 1, "maximum": 10},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_leave_balance",
        "description": (
            "Fetch the authenticated employee's current leave balances (CL, SL, PL, COMP_OFF; LOP for contractors) "
            "in days, plus any PENDING_APPROVAL requests. Only works for the logged-in employee's own id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string", "description": "Employee id, e.g. E1001."}},
            "required": ["employee_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_leave",
        "description": (
            "Submit a leave request to the HR system for the authenticated employee. Creates a PENDING_APPROVAL "
            "request and deducts the days from the balance. This is a WRITE action with side effects: only call it "
            "when the employee has clearly asked to apply, the dates and leave type are unambiguous, and you have "
            "verified balance and policy. The system enforces: valid dates, non-working days, past dates, balance, "
            "overlapping requests, employment status, contractor/LOP rules, comp-off expiry, half-day rules. "
            "It does NOT enforce every policy rule (e.g. notice periods, consecutive-day limits, probation, "
            "blackout windows) — those are the assistant's responsibility to check via search_hr_policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "Employee id, e.g. E1001."},
                "date": {"type": "string", "description": "Start date, ISO format YYYY-MM-DD."},
                "leave_type": {"type": "string", "enum": ["CL", "SL", "PL", "COMP_OFF", "LOP"],
                               "description": "CL=Casual, SL=Sick, PL=Privilege/Earned, COMP_OFF=Compensatory off, LOP=Loss of pay (contractors only)."},
                "end_date": {"type": "string", "description": "Optional end date (inclusive), ISO YYYY-MM-DD. Defaults to `date`."},
                "half_day": {"type": "boolean", "description": "True for a half day (CL, SL, COMP_OFF only; single date)."},
                "reason": {"type": "string", "description": "Optional short reason as given by the employee."},
            },
            "required": ["employee_id", "date", "leave_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_holiday_calendar",
        "description": (
            "List company holidays for a year. Pass `location` (Hyderabad | Mumbai | Bengaluru) to include that "
            "location's regional holidays — without it only national, shutdown and optional holidays are returned. "
            "Get the employee's location from get_employee_profile first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Calendar year, e.g. 2026. Defaults to the current year."},
                "location": {"type": "string", "enum": ["Hyderabad", "Mumbai", "Bengaluru"], "description": "Work location for regional holidays."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_employee_profile",
        "description": (
            "Fetch the authenticated employee's profile: department, designation, location, employment type "
            "(permanent/contractor/intern), status (active/probation/notice_period/on_leave/inactive), join date, "
            "probation end date, manager and tenure. Only works for the logged-in employee's own id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string", "description": "Employee id, e.g. E1001."}},
            "required": ["employee_id"],
            "additionalProperties": False,
        },
    },
]

TOOL_IMPLS: dict[str, Callable[[dict, ToolContext], dict]] = {
    "search_hr_policy": search_hr_policy,
    "get_leave_balance": get_leave_balance,
    "apply_leave": apply_leave,
    "get_holiday_calendar": get_holiday_calendar,
    "get_employee_profile": get_employee_profile,
}


def tool_names() -> list[str]:
    return [t["name"] for t in TOOL_DEFINITIONS]


def run_tool(name: str, args: dict, ctx: ToolContext) -> tuple[dict, bool, int]:
    """Execute a tool. Returns (result_dict, is_error, latency_ms).

    is_error=True only for infrastructure failures (chaos / DB down / unexpected exceptions).
    Business-rule rejections come back as ok=False with is_error=False so the model can
    explain them to the employee.
    """
    impl = TOOL_IMPLS.get(name)
    started = time.perf_counter()
    if impl is None:
        result, is_error = {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"No tool named {name}"}, True
    else:
        try:
            ctx.chaos.maybe_delay_all()
            result, is_error = impl(args, ctx), False
        except ToolError as e:
            result, is_error = e.to_dict(), False
        except ChaosError as e:
            result, is_error = e.to_dict(), True
        except db.DBError as e:
            result, is_error = {"ok": False, "error_code": "HTTP_503", "status": 503,
                                "message": f"HR system of record unavailable: {e.detail}"}, True
        except Exception as e:  # noqa: BLE001
            result, is_error = {"ok": False, "error_code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}"}, True
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        db.insert("hr_audit_log", {
            "session_id": ctx.session_id, "employee_id": ctx.employee_id, "tool": name,
            "args": args, "result": _truncate_for_audit(result), "ok": not is_error and result.get("ok", False),
            "latency_ms": latency_ms, "chaos": ",".join(ctx.chaos.as_list()) or None,
        }, returning=False)
    except Exception:  # audit must never break the agent
        pass
    return result, is_error, latency_ms


def _truncate_for_audit(result: dict) -> dict:
    s = json.dumps(result, default=str)
    if len(s) <= 4000:
        return result
    return {"truncated": True, "preview": s[:4000]}

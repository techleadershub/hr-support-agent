from __future__ import annotations

import time
from datetime import date, timedelta

from .. import db
from ..chaos import ChaosError
from .calendar import non_working_days
from .context import ToolContext, ToolError
from .profile import _authorise, load_employee

LEAVE_TYPES = ("CL", "SL", "PL", "COMP_OFF", "LOP")
TYPE_NAMES = {"CL": "Casual Leave", "SL": "Sick Leave", "PL": "Privilege Leave",
              "COMP_OFF": "Compensatory Off", "LOP": "Loss of Pay"}

# Leave types for which a request spanning non-working days is charged for those days too.
BRIDGED_TYPES = {"CL", "PL", "SL"}
HALF_DAY_TYPES = {"CL", "SL", "COMP_OFF"}
RETRO_WINDOW_DAYS = 3


# ---------------------------------------------------------------------------
# get_leave_balance
# ---------------------------------------------------------------------------
def get_leave_balance(args: dict, ctx: ToolContext) -> dict:
    employee_id = str(args.get("employee_id", "")).strip().upper()
    _authorise(ctx, employee_id)
    if ctx.chaos.active("leave_api_down"):
        raise ChaosError(503, "Leave Management service unavailable", "leave_api_down")
    if ctx.chaos.active("leave_api_slow"):
        time.sleep(6.0)

    emp = load_employee(employee_id)
    if emp["status"] == "inactive":
        raise ToolError("EMPLOYEE_INACTIVE", f"Employee {employee_id} is no longer active; leave records are not accessible.")

    rows = db.select("hr_leave_balances", {"employee_id": f"eq.{employee_id}", "order": "leave_type.asc"})
    balances = {}
    for r in rows:
        avail = float(r["available"])
        if ctx.chaos.active("stale_balance") and r["leave_type"] in ("CL", "PL", "SL"):
            avail += 5.0
        balances[r["leave_type"]] = {
            "name": TYPE_NAMES[r["leave_type"]],
            "entitled": float(r["entitled"]),
            "carried_forward": float(r["carried_forward"]),
            "used": float(r["used"]),
            "available": avail,
            "expires_on": r["expires_on"],
        }
    pending = db.select("hr_leave_requests", {
        "employee_id": f"eq.{employee_id}", "status": "eq.PENDING_APPROVAL",
        "select": "request_id,leave_type,start_date,end_date,days,status",
        "order": "start_date.asc",
    })
    return {
        "ok": True,
        "employee_id": employee_id,
        "employment_type": emp["employment_type"],
        "status": emp["status"],
        "as_of": ctx.today.isoformat(),
        "balances": balances,
        "pending_requests": pending,
        "note": "Balances are in days. 'available' already excludes days consumed by PENDING_APPROVAL requests.",
    }


# ---------------------------------------------------------------------------
# apply_leave
# ---------------------------------------------------------------------------
def _parse_date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except Exception:
        raise ToolError("INVALID_DATE", f"'{field}' must be an ISO date (YYYY-MM-DD); got {value!r}.")


def _count_days(start: date, end: date, leave_type: str, closed: set[date]) -> tuple[float, list[str]]:
    """Returns (days charged, list of charged non-working dates)."""
    bridged: list[str] = []
    working = 0
    d = start
    while d <= end:
        if d in closed:
            bridged.append(d.isoformat())
        else:
            working += 1
        d += timedelta(days=1)
    if leave_type in BRIDGED_TYPES and bridged:
        return float(working + len(bridged)), bridged
    return float(working), []


def apply_leave(args: dict, ctx: ToolContext) -> dict:
    employee_id = str(args.get("employee_id", "")).strip().upper()
    _authorise(ctx, employee_id)
    leave_type = str(args.get("leave_type", "")).strip().upper().replace(" ", "_")
    if leave_type not in LEAVE_TYPES:
        raise ToolError("INVALID_LEAVE_TYPE", f"leave_type must be one of {list(LEAVE_TYPES)}; got {args.get('leave_type')!r}.")
    start = _parse_date(args.get("date"), "date")
    end = _parse_date(args.get("end_date"), "end_date") if args.get("end_date") else start
    half_day = bool(args.get("half_day", False))
    reason = (args.get("reason") or "").strip() or None

    if end < start:
        raise ToolError("INVALID_RANGE", "end_date is before date.")
    if half_day and end != start:
        raise ToolError("INVALID_RANGE", "half_day requests must be for a single date.")
    if half_day and leave_type not in HALF_DAY_TYPES:
        raise ToolError("HALF_DAY_NOT_ALLOWED", f"{TYPE_NAMES[leave_type]} cannot be taken as a half day.")

    emp = load_employee(employee_id)
    if emp["status"] == "inactive":
        raise ToolError("EMPLOYEE_INACTIVE", f"Employee {employee_id} is no longer active.")
    if emp["status"] == "on_leave":
        raise ToolError("EMPLOYEE_ON_LEAVE", "No other leave category can be applied while on maternity/paternity/adoption leave (NDPL-HR-006 §1.6).")
    if emp["employment_type"] == "contractor" and leave_type != "LOP":
        raise ToolError("LEAVE_TYPE_NOT_ELIGIBLE", "Contractors have no paid leave entitlement; only LOP can be recorded (NDPL-HR-009 §3).")
    if emp["employment_type"] != "contractor" and leave_type == "LOP":
        raise ToolError("LOP_REQUIRES_HRBP", "Loss of Pay for permanent employees must be raised with the HRBP; it cannot be applied here (NDPL-HR-001 §6.2).")

    # past-date rules
    if start < ctx.today:
        if leave_type == "SL" and (ctx.today - start).days <= RETRO_WINDOW_DAYS:
            pass
        else:
            raise ToolError("DATE_IN_PAST", f"{start.isoformat()} is in the past. Only Sick Leave may be applied retroactively, within {RETRO_WINDOW_DAYS} days.")

    closed = non_working_days(start.year, emp["location"])
    if end.year != start.year:
        closed |= non_working_days(end.year, emp["location"])
    if start in closed or end in closed:
        bad = start if start in closed else end
        raise ToolError("NON_WORKING_DAY",
                        f"{bad.isoformat()} ({bad.strftime('%A')}) is a weekend, holiday or shutdown day for {emp['location']}; no leave is needed on that date.")

    days, bridged = _count_days(start, end, leave_type, closed)
    if half_day:
        days = 0.5

    # comp-off expiry
    if leave_type == "COMP_OFF":
        bal = db.select("hr_leave_balances", {"employee_id": f"eq.{employee_id}", "leave_type": "eq.COMP_OFF"}, single=True)
        if bal and bal.get("expires_on") and date.fromisoformat(bal["expires_on"]) < start:
            raise ToolError("COMP_OFF_EXPIRED", f"Your Compensatory Off expired on {bal['expires_on']} and cannot be used for {start.isoformat()}.",
                            available=float(bal["available"]))

    # --- chaos on the write path -------------------------------------------------
    if ctx.chaos.active("apply_leave_500"):
        raise ChaosError(500, "Leave Management service error: internal error while creating request", "apply_leave_500")
    if ctx.chaos.active("apply_leave_flaky") and ctx.chaos.bump("apply_leave_flaky") == 1:
        raise ChaosError(502, "Bad Gateway from Leave Management service", "apply_leave_flaky")

    result = db.rpc("hr_apply_leave", {
        "p_employee_id": employee_id, "p_leave_type": leave_type,
        "p_start": start.isoformat(), "p_end": end.isoformat(), "p_days": days,
        "p_half_day": half_day, "p_reason": reason, "p_approver": emp.get("manager_id"),
    })

    if ctx.chaos.active("apply_leave_timeout"):
        # request was created above; the caller never hears about it
        time.sleep(9.0)
        raise ChaosError(504, "Gateway Timeout waiting for Leave Management service", "apply_leave_timeout")

    if not result.get("ok"):
        code = result.get("error_code", "APPLY_FAILED")
        msgs = {
            "OVERLAPPING_REQUEST": f"An existing request ({result.get('existing_request_id')}) already covers one or more of these dates. Cancel it in the HR Portal first.",
            "INSUFFICIENT_BALANCE": f"Insufficient {TYPE_NAMES[leave_type]} balance: {result.get('available')} available, {result.get('requested')} requested.",
            "LEAVE_TYPE_NOT_ELIGIBLE": f"{TYPE_NAMES[leave_type]} is not available for this employee.",
        }
        raise ToolError(code, msgs.get(code, "Leave request could not be created."), **{k: v for k, v in result.items() if k not in ("ok", "error_code")})

    return {
        "ok": True,
        "request_id": result["request_id"],
        "status": result["status"],
        "employee_id": employee_id,
        "leave_type": leave_type,
        "leave_type_name": TYPE_NAMES[leave_type],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "half_day": half_day,
        "days_deducted": float(result["days_deducted"]),
        "bridged_non_working_days": bridged,
        "remaining_balance": None if result.get("remaining_balance") is None else float(result["remaining_balance"]),
        "approver_id": result.get("approver_id"),
        "message": "Request submitted and awaiting manager approval. Leave is not confirmed until status is APPROVED.",
    }

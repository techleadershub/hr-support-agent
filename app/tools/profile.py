from __future__ import annotations

from .. import db
from ..chaos import ChaosError
from .context import ToolContext, ToolError

PUBLIC_FIELDS = ("employee_id", "name", "email", "department", "designation", "location",
                 "employment_type", "status", "join_date", "probation_end_date", "manager_id",
                 "resignation_date", "last_working_day")


def _authorise(ctx: ToolContext, employee_id: str) -> None:
    if employee_id != ctx.employee_id:
        raise ToolError("FORBIDDEN",
                        f"You are authenticated as {ctx.employee_id}. Records of {employee_id} cannot be accessed "
                        "through the HR Assistant (NDPL-HR-011 §5.3).")


def load_employee(employee_id: str) -> dict:
    row = db.select("hr_employees", {"employee_id": f"eq.{employee_id}", "select": ",".join(PUBLIC_FIELDS)}, single=True)
    if not row:
        raise ToolError("EMPLOYEE_NOT_FOUND", f"No employee with id {employee_id}.")
    return row


def get_employee_profile(args: dict, ctx: ToolContext) -> dict:
    employee_id = str(args.get("employee_id", "")).strip().upper()
    _authorise(ctx, employee_id)
    if ctx.chaos.active("profile_api_down"):
        raise ChaosError(503, "Employee Directory service unavailable", "profile_api_down")
    emp = load_employee(employee_id)
    if ctx.chaos.active("profile_wrong_location"):
        emp["location"] = "Mumbai"
    manager = None
    if emp.get("manager_id"):
        m = db.select("hr_employees", {"employee_id": f"eq.{emp['manager_id']}", "select": "employee_id,name,designation"}, single=True)
        manager = m
    # tenure in completed months as of today
    from datetime import date
    jd = date.fromisoformat(emp["join_date"])
    months = (ctx.today.year - jd.year) * 12 + (ctx.today.month - jd.month) - (1 if ctx.today.day < jd.day else 0)
    return {"ok": True, "profile": emp, "manager": manager, "tenure_months": max(months, 0), "as_of": ctx.today.isoformat()}

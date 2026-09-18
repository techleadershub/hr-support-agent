from __future__ import annotations

from datetime import date, timedelta

from .. import db
from ..chaos import ChaosError
from .context import ToolContext, ToolError

LOCATIONS = ("Hyderabad", "Mumbai", "Bengaluru")


def load_holidays(year: int, location: str | None) -> list[dict]:
    """National + shutdown always; regional only for the given location; optional always (flagged)."""
    rows = db.select("hr_holidays", {
        "holiday_date": f"gte.{year}-01-01",
        "and": f"(holiday_date.lte.{year}-12-31)",
        "select": "holiday_date,name,holiday_type,location",
        "order": "holiday_date.asc",
    })
    out = []
    for r in rows:
        if r["holiday_type"] == "regional" and (location is None or r["location"] != location):
            continue
        out.append(r)
    return out


def non_working_days(year: int, location: str | None) -> set[date]:
    """Days on which leave cannot be applied: weekends + national/regional/shutdown holidays.
    Optional holidays are NOT non-working days (they must be applied for)."""
    days = {date.fromisoformat(h["holiday_date"]) for h in load_holidays(year, location)
            if h["holiday_type"] in ("national", "regional", "shutdown")}
    d = date(year, 1, 1)
    while d.year == year:
        if d.weekday() >= 5:
            days.add(d)
        d += timedelta(days=1)
    return days


def get_holiday_calendar(args: dict, ctx: ToolContext) -> dict:
    year = int(args.get("year") or ctx.today.year)
    location = args.get("location")
    if location:
        location = str(location).strip().title()
        if location not in LOCATIONS:
            raise ToolError("UNKNOWN_LOCATION", f"Unknown location '{location}'. Valid: {', '.join(LOCATIONS)}.")
    if ctx.chaos.active("holiday_api_down"):
        raise ChaosError(503, "Holiday Calendar service unavailable", "holiday_api_down")
    if ctx.chaos.active("holiday_api_empty"):
        return {"ok": True, "year": year, "location": location, "holidays": [], "count": 0}
    rows = load_holidays(year, location)
    holidays = []
    for r in rows:
        d = date.fromisoformat(r["holiday_date"])
        holidays.append({"date": r["holiday_date"], "day": d.strftime("%A"), "name": r["name"],
                         "type": r["holiday_type"], "location": r["location"] or "All"})
    note = ("Regional holidays are included only when `location` is supplied. "
            "Optional holidays are not automatic days off; employees apply for up to 2 per year in the HR Portal.")
    return {"ok": True, "year": year, "location": location or "not specified (national + shutdown + optional only)",
            "count": len(holidays), "holidays": holidays, "note": note}

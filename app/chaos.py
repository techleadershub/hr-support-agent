"""Fault injection for resilience testing.

Send `X-Chaos: <scenario>[,<scenario>]` on /api/chat or any /api/tools/* call.
Scenarios are applied inside the tool layer, so the agent experiences them exactly
as it would experience a real backend failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

SCENARIOS: dict[str, str] = {
    "leave_api_down": "get_leave_balance raises HTTP 503 Service Unavailable",
    "leave_api_slow": "get_leave_balance takes ~6 s before responding normally",
    "apply_leave_500": "apply_leave raises HTTP 500 Internal Server Error (no request is created)",
    "apply_leave_timeout": "apply_leave sleeps ~9 s then raises HTTP 504 Gateway Timeout — but the request WAS created (ghost write)",
    "apply_leave_flaky": "apply_leave fails with HTTP 502 on the first call in a session, succeeds on retry",
    "stale_balance": "get_leave_balance returns a balance inflated by +5 days (cache staleness); apply_leave still uses the real balance",
    "holiday_api_down": "get_holiday_calendar raises HTTP 503",
    "holiday_api_empty": "get_holiday_calendar returns an empty list (looks healthy, is wrong)",
    "policy_search_empty": "search_hr_policy returns zero chunks",
    "policy_search_garbage": "search_hr_policy returns chunks from the wrong document (retrieval quality failure)",
    "profile_api_down": "get_employee_profile raises HTTP 503",
    "profile_wrong_location": "get_employee_profile returns location 'Mumbai' for everyone (data corruption)",
    "slow_tools": "every tool call is delayed by ~2 s",
}


class ChaosError(Exception):
    """Raised inside a tool to simulate an upstream failure. Becomes an is_error tool_result."""

    def __init__(self, status: int, message: str, scenario: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.scenario = scenario

    def to_dict(self) -> dict:
        return {"ok": False, "error_code": f"HTTP_{self.status}", "status": self.status,
                "message": self.message, "chaos_scenario": self.scenario}


@dataclass
class Chaos:
    scenarios: set[str] = field(default_factory=set)
    # per-session counters so "flaky" can fail once then succeed
    counters: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_header(cls, raw: str | None) -> "Chaos":
        if not raw:
            return cls()
        wanted = {s.strip() for s in raw.split(",") if s.strip()}
        unknown = wanted - SCENARIOS.keys()
        if unknown:
            raise ValueError(f"unknown chaos scenario(s): {sorted(unknown)}; valid: {sorted(SCENARIOS)}")
        return cls(scenarios=wanted)

    def active(self, name: str) -> bool:
        return name in self.scenarios

    def bump(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def maybe_delay_all(self) -> None:
        if self.active("slow_tools"):
            time.sleep(2.0)

    def as_list(self) -> list[str]:
        return sorted(self.scenarios)

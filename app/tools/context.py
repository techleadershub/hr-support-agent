from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..chaos import Chaos


@dataclass
class ToolContext:
    """Everything a tool needs beyond its own arguments."""
    employee_id: str          # the authenticated employee — tools refuse to act for anyone else
    today: date
    chaos: Chaos
    session_id: str | None = None


class ToolError(Exception):
    """A business-rule rejection. Returned to the model as a normal (non-error) tool
    result so it can explain the rejection to the employee."""

    def __init__(self, error_code: str, message: str, **extra):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"ok": False, "error_code": self.error_code, "message": self.message, **self.extra}

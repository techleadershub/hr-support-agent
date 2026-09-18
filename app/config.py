"""Central configuration. Everything comes from environment variables (see .env.example)."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

ACCESS_KEY = os.getenv("HR_AGENT_ACCESS_KEY", "")
MODEL = os.getenv("HR_AGENT_MODEL", "claude-opus-5")
EFFORT = os.getenv("HR_AGENT_EFFORT", "medium")
MAX_ITERATIONS = int(os.getenv("HR_AGENT_MAX_ITERATIONS", "8"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

KNOWLEDGE_DIR = ROOT / "knowledge"
INDEX_PATH = ROOT / "data" / "policy_index.json"
STATIC_DIR = ROOT / "static"

COMPANY = "Nimbus Dynamics Private Limited"
APP_VERSION = "1.0.0"


def today(override: str | None = None) -> date:
    """'Today' for all date logic. Order of precedence: per-request X-Today header,
    HR_AGENT_TODAY env var, real IST date. Lets test suites pin the clock."""
    for candidate in (override, os.getenv("HR_AGENT_TODAY")):
        if candidate:
            return date.fromisoformat(candidate.strip())
    return datetime.now(IST).date()
